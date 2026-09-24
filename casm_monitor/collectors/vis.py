"""The reduced-visibility collector (M2): every WIRED input, every integration.

What it does each cadence (30 s, against a 137.44 s integration):

1. find the newest observation in ``vis_dir`` and its files;
2. work out which integrations are COMPLETE and safe to read -- a full-size
   file is complete by definition, the newest file is still growing so its
   integration count is floored from its size, one integration is left as a
   guard band behind the file end, and its ``(size, mtime)`` must be identical
   across two consecutive polls before anything is read from it;
3. read every integration after the ``vis/<obs>`` watermark (oldest first,
   bounded per pass, never older than ``backfill_hours``) with ``casm_io``,
   keeping only the baselines among the wired inputs -- 24 inputs = 300
   baselines today, 0.12 s per integration measured;
4. publish one ``vis_full`` shard per integration (complex64 ``(n_bl, 3072)``,
   TTL 3 d) and one ``vis_avg8`` shard per eight integrations (8x
   channel-averaged, ``(n, n_bl, 384)``, TTL 60 d);
5. record the per-input autocorrelation band power, the subband-dark flags,
   ``vis.age_s``, and mirror the newest integration for the web process.

The binary is never hand-parsed: reads go through
``casm_io.correlator.VisibilityReader`` with the canonical ``layout_64ant``
format and ``freq_order="descending"`` made explicit. Only the file GEOMETRY
(4096-byte optional header, 202,899,456 B per integration, 32 per file) is
known here, and only to decide which integrations exist -- the plan's
"exact-byte live-file reads with guard band and watermark".

The first integration of every FILE is marked (``flags.first_of_file``, event
``vis_first_integration_flagged``) and kept. This records file position, not
data quality; continuous observations can have valid file-boundary samples.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from ..config import Settings
from . import rowmap
from .base import Collector, CollectorContext

log = logging.getLogger("casm_monitor.collect.vis")

# -- file geometry (layout_64ant, verified against the live files) -------
NCHAN = 3072
NSIG = 128
N_BASELINES_FULL = NSIG * (NSIG + 1) // 2  # 8256
INTEGRATION_BYTES = NCHAN * N_BASELINES_FULL * 8  # 202,899,456
INTEGRATIONS_PER_FILE = 32
HEADER_BYTES = 4096
FULL_FILE_BYTES = INTEGRATION_BYTES * INTEGRATIONS_PER_FILE
DT_S = 137.438953472
OBS_TIME_FMT = "%Y-%m-%d-%H:%M:%S"

# One integration is left unread behind the end of a growing file, so a
# partially flushed integration is never handed to a reader.
GUARD_INTEGRATIONS = 1

# -- shard streams ------------------------------------------------------
STREAM_FULL = "vis_full"
STREAM_AVG8 = "vis_avg8"
CHAN_AVG = 8
AVG_NCHAN = NCHAN // CHAN_AVG  # 384
# Integrations buffered into one vis_avg8 shard (~18 min of history).
AVG_BUFFER = 8
# ... and the age at which a short buffer is flushed anyway, so a stalled
# correlator cannot leave the last few integrations unpublished for ever.
AVG_MAX_AGE_S = 25 * 60.0

# -- subband-dark test --------------------------------------------------
SUBBAND_CHANS = 512
N_SUBBANDS = NCHAN // SUBBAND_CHANS  # 6
DARK_FRACTION = 0.01  # a block below 1% of the strongest block is "dark"
# ... but only for an input that carries signal at all. A dead or gated feed sits
# at the correlator's quantisation floor (measured 2026-09-08: input 11 / ant 12
# has block medians of 1-2 counts, while every live input is 1e4-1e8), and there
# a single zero-valued block flickers in and out of the 1% test every
# integration. Below this floor the test says nothing: the input is dead, which
# is not the same finding as "the F-engine dropped a subband".
DARK_MIN_BLOCK_POWER = 16.0
DB_FLOOR = 1e-6


def obs_start_unix(obs: str) -> float:
    """UTC_START base string -> unix seconds."""
    return (
        datetime.strptime(obs, OBS_TIME_FMT).replace(tzinfo=timezone.utc).timestamp()
    )


def integration_time(obs_t0: float, file_idx: int, int_idx: int) -> float:
    """Nominal timestamp of one integration (the reader's own axis agrees)."""
    return obs_t0 + (file_idx * INTEGRATIONS_PER_FILE + int_idx) * DT_S


def header_bytes(path: str | os.PathLike[str]) -> int:
    """0 or 4096, from casm_io's own header sniffer."""
    from casm_io.correlator.header import get_header_offset

    offset, _header = get_header_offset(str(path))
    return int(offset)


def complete_integrations(size: int, offset: int = 0) -> int:
    """Integrations fully written in a file of ``size`` bytes.

    Floor division, so an integration whose bytes are only partly flushed is
    not counted at all.
    """
    payload = int(size) - int(offset)
    return 0 if payload <= 0 else min(payload // INTEGRATION_BYTES, INTEGRATIONS_PER_FILE)


def is_full_size(size: int) -> bool:
    """True for a finished file (32 integrations, with or without a header)."""
    return int(size) in (FULL_FILE_BYTES, FULL_FILE_BYTES + HEADER_BYTES)


def safe_integrations(size: int, offset: int = 0, *, growing: bool = True) -> int:
    """How many integrations of a file may be read right now.

    A finished file offers all of them; a growing one keeps
    :data:`GUARD_INTEGRATIONS` behind its end.
    """
    n = complete_integrations(size, offset)
    if is_full_size(size) or not growing:
        return n
    return max(0, n - GUARD_INTEGRATIONS)


@dataclass(frozen=True)
class VisFile:
    """One ``<obs>.dat.<n>`` file as the collector sees it."""

    index: int
    path: Path
    size: int
    mtime: float

    @property
    def full(self) -> bool:
        return is_full_size(self.size)


def scan_observation_files(vis_dir: str | os.PathLike[str], obs: str) -> list[VisFile]:
    """Every ``<obs>.dat.<n>`` file, sorted by index."""
    out: list[VisFile] = []
    for path in Path(vis_dir).glob(f"{obs}.dat.*"):
        tail = path.name.rpartition(".dat.")[2]
        try:
            index = int(tail)
        except ValueError:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append(VisFile(index=index, path=path, size=stat.st_size, mtime=stat.st_mtime))
    return sorted(out, key=lambda f: f.index)


def newest_observation(vis_dir: str | os.PathLike[str]) -> str | None:
    """The newest observation base string (they sort chronologically)."""
    names = set()
    for path in Path(vis_dir).glob("*.dat.*"):
        base = path.name.rpartition(".dat.")[0]
        if base:
            names.add(base)
    return max(names) if names else None


@dataclass
class PendingIntegration:
    """One integration to read: where it is and when it is."""

    file_idx: int
    int_idx: int
    ts: float
    first_of_file: bool


def pending_integrations(
    files: Sequence[VisFile],
    watermark: tuple[int, int] | None,
    obs_t0: float,
    *,
    stable: dict[int, tuple[int, float]] | None = None,
    oldest_ts: float | None = None,
    limit: int | None = None,
) -> list[PendingIntegration]:
    """Integrations that are complete, unread and inside the backfill window.

    ``watermark`` is the last published ``(file_idx, int_idx)``; ``stable`` maps
    a file index to the ``(size, mtime)`` seen at the PREVIOUS poll, and a
    growing file whose size/mtime moved since then contributes nothing this
    pass. Oldest first, so history fills in order.
    """
    stable = stable or {}
    newest_index = max((f.index for f in files), default=-1)
    out: list[PendingIntegration] = []
    for entry in files:
        growing = not entry.full
        if growing:
            previous = stable.get(entry.index)
            if previous is None or previous != (entry.size, entry.mtime):
                # Not yet observed twice with the same size and mtime: the
                # correlator may be mid-write, so read nothing from it now.
                continue
            if entry.index != newest_index:
                # A short file that is NOT the newest one is a truncated old
                # file, not a growing one: its integrations are all final.
                growing = False
        n_safe = safe_integrations(entry.size, header_bytes(entry.path), growing=growing)
        for int_idx in range(n_safe):
            if watermark is not None and (entry.index, int_idx) <= tuple(watermark):
                continue
            ts = integration_time(obs_t0, entry.index, int_idx)
            if oldest_ts is not None and ts < oldest_ts:
                continue
            out.append(
                PendingIntegration(
                    file_idx=entry.index, int_idx=int_idx, ts=ts, first_of_file=int_idx == 0
                )
            )
    out.sort(key=lambda p: (p.file_idx, p.int_idx))
    return out[: int(limit)] if limit else out


def contiguous_runs(
    items: Sequence[PendingIntegration], max_len: int
) -> list[list[PendingIntegration]]:
    """Group consecutive integrations of the SAME file into short runs.

    One run is one ``casm_io`` read: never across a file boundary (the
    part-boundary memmap bug) and never more than ``max_len`` integrations, so
    peak memory stays at a few tens of MB.
    """
    runs: list[list[PendingIntegration]] = []
    for item in items:
        if (
            runs
            and runs[-1][-1].file_idx == item.file_idx
            and runs[-1][-1].int_idx + 1 == item.int_idx
            and len(runs[-1]) < max_len
        ):
            runs[-1].append(item)
        else:
            runs.append([item])
    return runs


# -- input sets ---------------------------------------------------------
def input_sets(layout_path: str | os.PathLike[str] | None = None) -> dict[str, list[int]]:
    """The DECISION's two sets: ``live`` (in beamforming) and ``wired``.

    ``wired`` (``functional=1``) is what the collector caches; ``live``
    (``include_in_beamforming=1``) is what the tab displays by default. Read at
    every call so a ``casm-layout apply`` grows the set by itself.
    """
    layout = rowmap.read_layout(layout_path if layout_path is not None else rowmap.LAYOUT_CSV)
    wired = rowmap.wired_inputs(layout)
    live = sorted(
        int(row["packet_idx"])
        for row in layout
        if str(row.get("include_in_beamforming", "")).strip() == "1"
        and str(row.get("packet_idx", "")).strip() != ""
    )
    return {"live": [i for i in live if i in set(wired)], "wired": wired}


def input_table(layout_path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """One row per wired input, including layout-recorded SNAP/slot/ADC wiring."""
    layout = rowmap.read_layout(layout_path if layout_path is not None else rowmap.LAYOUT_CSV)
    out = []
    for row in layout:
        if str(row.get("functional", "")).strip() != "1":
            continue
        try:
            packet_idx = int(str(row["packet_idx"]).strip())
        except (KeyError, ValueError):
            continue
        label = f"{(row.get('row') or '').strip()}{(row.get('col') or '').strip()}" or None
        def optional_int(key):
            try:
                return int(str(row.get(key, '')).strip())
            except ValueError:
                return None
        out.append(
            {
                "packet_idx": packet_idx,
                "antenna": int(str(row.get("antenna", "")).strip() or packet_idx + 1),
                "station": label,
                "snap": optional_int("snap"),
                "slot": str(row.get("slot") or "").strip() or None,
                "adc": optional_int("adc"),
                "in_bf": str(row.get("include_in_beamforming", "")).strip() == "1",
            }
        )
    return sorted(out, key=lambda r: r["packet_idx"])


# -- reads --------------------------------------------------------------
def read_run(
    vis_dir: str | os.PathLike[str],
    obs: str,
    file_idx: int,
    int_from: int,
    n_int: int,
    inputs: Sequence[int],
) -> dict[str, Any]:
    """Read ``n_int`` integrations starting at ``int_from`` of one file.

    Uses ``VisibilityReader.read`` with an explicit one-file time window (its
    memmap path materialises only the requested baselines), so no byte offset
    arithmetic and no hand-rolled parsing happens here.
    """
    from casm_io.correlator.formats import load_format
    from casm_io.correlator.reader import VisibilityReader

    fmt = load_format("layout_64ant")
    reader = VisibilityReader(str(vis_dir), obs, fmt=fmt)
    t0 = obs_start_unix(obs)
    k0 = file_idx * INTEGRATIONS_PER_FILE + int_from
    start = datetime.fromtimestamp(t0 + k0 * fmt.dt_raw_s, timezone.utc)
    end = datetime.fromtimestamp(t0 + (k0 + n_int) * fmt.dt_raw_s, timezone.utc)
    return reader.read(
        time_start=start,
        time_end=end,
        inputs=[int(i) for i in sorted(inputs)],
        freq_order="descending",
        verbose=False,
    )


def auto_indices(n_inputs: int) -> list[int]:
    """Flat upper-triangle indices of the autocorrelations."""
    from casm_io.correlator.baselines import triu_flat_index

    return [triu_flat_index(n_inputs, i, i) for i in range(n_inputs)]


def subband_medians(power: np.ndarray) -> np.ndarray:
    """Median power of each 512-channel block, (..., 6)."""
    p = np.asarray(power, dtype=np.float64)
    n = (p.shape[-1] // SUBBAND_CHANS) * SUBBAND_CHANS
    blocks = p[..., :n].reshape(*p.shape[:-1], n // SUBBAND_CHANS, SUBBAND_CHANS)
    return np.median(blocks, axis=-1)


def dark_subbands(
    power: np.ndarray,
    fraction: float = DARK_FRACTION,
    min_block_power: float = DARK_MIN_BLOCK_POWER,
) -> list[int]:
    """Indices of the 512-channel blocks below ``fraction`` of the best block.

    An input with no signal at all (dead or gated feed: every block at the
    quantisation floor) reports no dark subband. "Dark" here means "this input
    delivers five subbands and not the sixth", the F-engine signature, not "this
    feed is dead" -- which the autocorrelation band power already says, and which
    would otherwise flicker in and out of this test every integration.
    """
    med = subband_medians(power)
    best = float(np.max(med)) if med.size else 0.0
    if not np.isfinite(best) or best < float(min_block_power):
        return []
    return [int(k) for k in np.flatnonzero(med < fraction * best)]


def band_power_db(power: np.ndarray, freq_mhz: np.ndarray) -> float:
    """Mean autocorrelation power over 400-480 MHz, in dB."""
    mask = rowmap.band_mask(np.asarray(freq_mhz))
    band = np.asarray(power, dtype=np.float64)[mask] if mask.any() else np.asarray(power)
    return float(10.0 * np.log10(max(float(np.mean(band)), DB_FLOOR)))


# -- latest-integration mirror -----------------------------------------
def latest_vis_path(settings: Settings) -> Path:
    """Where the newest integration is mirrored for the web process."""
    return Path(settings.store_root) / "latest" / "vis_latest.npz"


def write_latest_vis(
    settings: Settings,
    *,
    vis: np.ndarray,
    inputs: Sequence[int],
    freq_mhz: np.ndarray,
    ts: float,
    obs: str,
    file_idx: int,
    int_idx: int,
    first_of_file: bool,
) -> None:
    """Mirror one integration (atomic replace; ~7 MB at 24 inputs)."""
    path = latest_vis_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{os.getpid()}.npz")
    try:
        np.savez(
            tmp,
            ts=np.float64(ts),
            written=np.float64(time.time()),
            vis=np.asarray(vis, dtype=np.complex64),
            inputs=np.asarray(inputs, dtype=np.int32),
            freq_mhz=np.asarray(freq_mhz, dtype=np.float64),
            obs=np.array(obs),
            file_idx=np.int32(file_idx),
            int_idx=np.int32(int_idx),
            first_of_file=np.bool_(first_of_file),
        )
        os.replace(tmp, path)
    except Exception:
        log.exception("vis: could not write the latest-integration mirror")
        tmp.unlink(missing_ok=True)


def avg8_staging_path(settings: Settings, obs: str) -> Path:
    """Where the not-yet-published ``vis_avg8`` buffer for ``obs`` is staged."""
    return Path(settings.store_root) / "staging" / f"vis_avg8_{obs}.npz"


def write_avg8_staging(
    settings: Settings, obs: str, inputs: Sequence[int], samples: Sequence["BufferedIntegration"]
) -> None:
    """Durably persist the CURRENT ``vis_avg8`` buffer (rewritten whole).

    Called after every integration is added to the in-memory buffer, so a
    crash between two ``vis_avg8`` shard flushes (up to :data:`AVG_BUFFER`
    integrations, ~18 min) loses nothing: the next process recovers the
    buffer from this file (:func:`read_avg8_staging`) instead of silently
    dropping it, which the ``vis`` stream's byte/integration watermark already
    treats as read. The file stays small (at most ``AVG_BUFFER`` reduced
    integrations) and short-lived (cleared the moment the buffer is actually
    published), so there is nothing here that needs a separate hourly
    compaction pass.
    """
    path = avg8_staging_path(settings, obs)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{os.getpid()}.npz")
    try:
        values = (
            np.stack([s.values for s in samples]).astype(np.complex64)
            if samples
            else np.zeros((0, 0, 0), dtype=np.complex64)
        )
        np.savez(
            tmp,
            obs=np.array(obs),
            inputs=np.asarray(inputs, dtype=np.int32),
            values=values,
            ts=np.asarray([s.ts for s in samples], dtype=np.float64),
            file_idx=np.asarray([s.file_idx for s in samples], dtype=np.int64),
            int_idx=np.asarray([s.int_idx for s in samples], dtype=np.int64),
            first_of_file=np.asarray([s.first_of_file for s in samples], dtype=bool),
        )
        os.replace(tmp, path)
    except Exception:
        log.exception("vis: could not write the vis_avg8 staging file for %s", obs)
        tmp.unlink(missing_ok=True)


def read_avg8_staging(settings: Settings, obs: str) -> dict[str, Any] | None:
    """The staged ``vis_avg8`` buffer for ``obs``, or None if there is none."""
    path = avg8_staging_path(settings, obs)
    if not path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as npz:
            n = int(npz["ts"].shape[0])
            samples = [
                BufferedIntegration(
                    ts=float(npz["ts"][k]),
                    values=np.asarray(npz["values"][k], dtype=np.complex64),
                    file_idx=int(npz["file_idx"][k]),
                    int_idx=int(npz["int_idx"][k]),
                    first_of_file=bool(npz["first_of_file"][k]),
                )
                for k in range(n)
            ]
            return {"samples": samples, "inputs": [int(i) for i in npz["inputs"]]}
    except Exception:
        log.warning("vis: vis_avg8 staging file unreadable for %s", obs, exc_info=True)
        return None


def clear_avg8_staging(settings: Settings, obs: str) -> None:
    """Remove the staging file for ``obs``: its contents are now in a shard."""
    path = avg8_staging_path(settings, obs)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("vis: could not remove the vis_avg8 staging file for %s", obs, exc_info=True)


def read_latest_vis(settings: Settings) -> dict[str, Any] | None:
    """The newest mirrored integration, or None before the first read."""
    path = latest_vis_path(settings)
    if not path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as npz:
            return {
                "ts": float(npz["ts"]),
                "written": float(npz["written"]),
                "vis": np.asarray(npz["vis"], dtype=np.complex64),
                "inputs": [int(i) for i in npz["inputs"]],
                "freq_mhz": np.asarray(npz["freq_mhz"], dtype=np.float64),
                "obs": str(npz["obs"]),
                "file_idx": int(npz["file_idx"]),
                "int_idx": int(npz["int_idx"]),
                "first_of_file": bool(npz["first_of_file"]),
            }
    except Exception:
        log.warning("vis: latest-integration mirror unreadable", exc_info=True)
        return None


# -- the collector ------------------------------------------------------
@dataclass
class BufferedIntegration:
    """One channel-averaged integration waiting for its ``vis_avg8`` shard."""

    ts: float
    values: np.ndarray  # complex64 (n_bl, 384)
    file_idx: int
    int_idx: int
    first_of_file: bool


class VisCollector(Collector):
    """Caches the wired-input sub-matrix of every integration."""

    name = "vis"
    default_cadence_s = 30.0
    # A backfill pass reads up to ``max_per_pass`` integrations (0.12 s each)
    # plus their shard writes; the runner's overlap guard makes a longer pass
    # safe (it just skips the next tick), but the timeout is generous anyway.
    timeout_s = 600.0

    def __init__(self, settings: Settings, cadence_s: float | None = None) -> None:
        super().__init__(settings, cadence_s)
        self.vis_dir = Path(settings.vis_dir)
        self.backfill_s = float(settings.vis_backfill_hours) * 3600.0
        self.max_per_pass = int(settings.vis_max_per_pass)
        self._seen: dict[int, tuple[int, float]] = {}
        self._seen_obs: str | None = None
        self._buffer: list[BufferedIntegration] = []
        self._buffer_inputs: list[int] = []
        self._buffer_obs: str | None = None

    # -- one pass -----------------------------------------------------
    def collect(self, ctx: CollectorContext) -> None:
        now = time.time()
        obs = newest_observation(self.vis_dir)
        if obs is None:
            ctx.scalar("vis.ok", 0)
            log.warning("vis: no visibility files in %s", self.vis_dir)
            return
        ctx.on_change(
            "vis.obs",
            obs,
            kind="vis_obs_changed",
            severity="info",
            subject=obs,
            detail={"vis_dir": str(self.vis_dir)},
        )
        if obs != self._seen_obs:
            # A new observation restarts the file bookkeeping; anything still
            # buffered belongs to the previous one and is published now.
            self._flush_buffer(ctx, force=True)
            self._seen = {}
            self._seen_obs = obs
            # This also fires on every process start (``self._seen_obs`` is
            # None until the first tick): recover any ``vis_avg8`` integrations
            # a previous process staged but never got to publish, so the
            # ``vis`` watermark having already moved past them (they were read
            # and full-resolution-published before the crash) does not mean
            # they are lost from the channel-averaged stream too.
            self._recover_avg8_staging(ctx, obs)

        sets = input_sets()
        inputs = sets["wired"]
        if not inputs:
            ctx.scalar("vis.ok", 0)
            log.warning("vis: no wired inputs in the layout; nothing to cache")
            return

        files = scan_observation_files(self.vis_dir, obs)
        obs_t0 = obs_start_unix(obs)
        watermark = self._watermark(ctx, obs)
        candidates = pending_integrations(
            files,
            watermark,
            obs_t0,
            stable=self._seen,
            oldest_ts=now - self.backfill_s,
            limit=None,
        )
        self._seen = {f.index: (f.size, f.mtime) for f in files}

        todo = candidates[: self.max_per_pass]
        ctx.scalar("vis.pending", len(candidates) - len(todo))
        ctx.scalar("vis.n_inputs", len(inputs))
        ctx.scalar("vis.n_baselines", len(inputs) * (len(inputs) + 1) // 2)
        ctx.scalar("vis.n_live_inputs", len(sets["live"]))

        read_s = 0.0
        for run in contiguous_runs(todo, AVG_BUFFER):
            read_s += self._process_run(ctx, obs, run, inputs)
        self._flush_buffer(ctx)

        newest_ts = self._newest_ts(ctx, obs, files, obs_t0)
        if newest_ts is not None:
            ctx.scalar("vis.age_s", round(max(0.0, time.time() - newest_ts), 1))
        if todo:
            ctx.scalar("vis.read_s_per_integration", round(read_s / len(todo), 3))
        ctx.scalar("vis.integrations_read", len(todo))
        ctx.scalar("vis.ok", 1)

    # -- helpers ------------------------------------------------------
    @staticmethod
    def _watermark(ctx: CollectorContext, obs: str) -> tuple[int, int] | None:
        value = ctx.store.get_watermark("vis", obs)
        if not value or len(value) != 2:
            return None
        return int(value[0]), int(value[1])

    def _newest_ts(
        self,
        ctx: CollectorContext,
        obs: str,
        files: Sequence[VisFile],
        obs_t0: float,
    ) -> float | None:
        """Timestamp of the newest integration the store holds for ``obs``."""
        mark = self._watermark(ctx, obs)
        if mark is not None:
            return integration_time(obs_t0, mark[0], mark[1])
        return None

    def _process_run(
        self,
        ctx: CollectorContext,
        obs: str,
        run: list[PendingIntegration],
        inputs: Sequence[int],
    ) -> float:
        """Read one run of integrations and publish each of them."""
        started = time.time()
        try:
            result = read_run(
                self.vis_dir, obs, run[0].file_idx, run[0].int_idx, len(run), inputs
            )
        except Exception:
            log.exception(
                "vis: read failed for %s file %d integrations %d..%d",
                obs, run[0].file_idx, run[0].int_idx, run[-1].int_idx,
            )
            ctx.scalar("vis.read_errors", 1)
            return 0.0
        read_s = time.time() - started
        vis = np.asarray(result["vis"])
        freq_mhz = np.asarray(result["freq_mhz"], dtype=np.float64)
        times = np.asarray(result["time_unix"], dtype=np.float64)
        if vis.shape[0] != len(run):
            log.warning(
                "vis: asked for %d integrations of %s file %d and got %d; publishing those",
                len(run), obs, run[0].file_idx, vis.shape[0],
            )
        ordered = sorted(int(i) for i in inputs)
        for k in range(min(vis.shape[0], len(run))):
            item = run[k]
            ts = float(times[k]) if k < times.size else item.ts
            self._publish(ctx, obs, item, ts, vis[k], ordered, freq_mhz)
        return read_s

    def _publish(
        self,
        ctx: CollectorContext,
        obs: str,
        item: PendingIntegration,
        ts: float,
        frame: np.ndarray,
        inputs: list[int],
        freq_mhz: np.ndarray,
    ) -> None:
        """One integration: shard, scalars, events, mirror, watermark."""
        # casm_io hands back (F, n_bl); the store keeps (n_bl, F) so a single
        # baseline is one contiguous row.
        arr = np.ascontiguousarray(np.asarray(frame).T, dtype=np.complex64)
        # The stored autocorrelation element IS the power (real, positive), so
        # the magnitude is the power -- never squared again. Same convention as
        # ``rowmap.read_input_autos``, so the two agree for one integration.
        autos = np.abs(arr[auto_indices(len(inputs))]).astype(np.float64)
        dark = {p: dark_subbands(autos[n]) for n, p in enumerate(inputs)}
        meta = {
            "obs": obs,
            "file_idx": item.file_idx,
            "int_idx": item.int_idx,
            "inputs": inputs,
            "t": [ts],
            "nchan": int(arr.shape[1]),
            "chan_avg": 1,
            "freq_top_mhz": float(freq_mhz[0]),
            "chan_bw_mhz": float(abs(freq_mhz[1] - freq_mhz[0])) if freq_mhz.size > 1 else 0.0,
            "freq_order": "descending",
            "flags": {"first_of_file": bool(item.first_of_file)},
            "subbands_dark": {str(p): v for p, v in dark.items() if v},
        }
        ctx.shards.write(STREAM_FULL, arr, t0=ts, t1=ts, meta=meta, chunks=(1, arr.shape[1]))

        self._buffer_integration(ctx, obs, item, ts, arr, inputs)
        self._scalars(ctx, item, ts, autos, freq_mhz, dark, inputs)
        if item.first_of_file:
            ctx.event(
                "vis_first_integration_flagged",
                severity="info",
                subject=f"{obs}.dat.{item.file_idx}",
                detail={
                    "obs": obs,
                    "file_idx": item.file_idx,
                    "int_idx": item.int_idx,
                    "ts": ts,
                    "note": "first integration of a file; kept and marked for provenance, not a quality rejection",
                },
                ts=ts,
            )
        write_latest_vis(
            ctx.settings,
            vis=arr,
            inputs=inputs,
            freq_mhz=freq_mhz,
            ts=ts,
            obs=obs,
            file_idx=item.file_idx,
            int_idx=item.int_idx,
            first_of_file=item.first_of_file,
        )
        ctx.store.set_watermark("vis", obs, [item.file_idx, item.int_idx])

    def _scalars(
        self,
        ctx: CollectorContext,
        item: PendingIntegration,
        ts: float,
        autos: np.ndarray,
        freq_mhz: np.ndarray,
        dark: dict[int, list[int]],
        inputs: list[int],
    ) -> None:
        rows: list[tuple[str, Any, dict[str, Any]]] = []
        for n, packet_idx in enumerate(inputs):
            tags = {"packet_idx": packet_idx, "antenna": packet_idx + 1}
            rows.append(
                (f"vis.input{packet_idx}.auto_db", round(band_power_db(autos[n], freq_mhz), 3), tags)
            )
        ctx.store.put_scalars(rows, ts=ts)
        for packet_idx, blocks in dark.items():
            ctx.on_change(
                f"vis.input{packet_idx}.subbands_dark",
                len(blocks),
                kind="vis_subband_dark",
                severity="warn" if blocks else "info",
                subject=f"input {packet_idx} (ant {packet_idx + 1})",
                detail={"subbands": blocks, "ts": ts},
                tags={"packet_idx": packet_idx, "antenna": packet_idx + 1},
            )
        ctx.scalar(
            "vis.subbands_dark", sum(len(v) for v in dark.values()), ts=ts
        )
        ctx.scalar("vis.file_idx", item.file_idx, ts=ts)
        ctx.scalar("vis.int_idx", item.int_idx, ts=ts)
        ctx.scalar("vis.first_of_file", int(item.first_of_file), ts=ts)

    # -- vis_avg8 buffer ----------------------------------------------
    def _buffer_integration(
        self,
        ctx: CollectorContext,
        obs: str,
        item: PendingIntegration,
        ts: float,
        arr: np.ndarray,
        inputs: list[int],
    ) -> None:
        """Channel-average by 8 (in the complex plane) and buffer the result."""
        n_keep = (arr.shape[1] // CHAN_AVG) * CHAN_AVG
        reduced = (
            arr[:, :n_keep]
            .reshape(arr.shape[0], n_keep // CHAN_AVG, CHAN_AVG)
            .mean(axis=2)
            .astype(np.complex64)
        )
        if self._buffer and (self._buffer_obs != obs or self._buffer_inputs != inputs):
            self._flush_buffer(ctx, force=True)
        self._buffer_obs = obs
        self._buffer_inputs = list(inputs)
        self._buffer.append(
            BufferedIntegration(
                ts=ts,
                values=reduced,
                file_idx=item.file_idx,
                int_idx=item.int_idx,
                first_of_file=item.first_of_file,
            )
        )
        # Persist the WHOLE buffer (small: at most AVG_BUFFER reduced
        # integrations) before returning, so a crash right after this call
        # loses nothing -- the ``vis`` watermark this integration also just
        # advanced treats it as already read, so RAM is the only other copy.
        write_avg8_staging(ctx.settings, obs, self._buffer_inputs, self._buffer)
        ctx.store.set_watermark(
            STREAM_AVG8, obs, [self._buffer[-1].file_idx, self._buffer[-1].int_idx]
        )
        if len(self._buffer) >= AVG_BUFFER:
            self._flush_buffer(ctx, force=True)

    def _recover_avg8_staging(self, ctx: CollectorContext, obs: str) -> None:
        """Reload a staged ``vis_avg8`` buffer left behind by a crashed run."""
        if self._buffer:
            return
        recovered = read_avg8_staging(ctx.settings, obs)
        if not recovered or not recovered["samples"]:
            return
        self._buffer = recovered["samples"]
        self._buffer_obs = obs
        self._buffer_inputs = recovered["inputs"]
        log.info(
            "vis: recovered %d staged vis_avg8 integration(s) for %s after a restart",
            len(self._buffer), obs,
        )

    def _flush_buffer(self, ctx: CollectorContext, *, force: bool = False) -> None:
        """Publish the buffered ``vis_avg8`` samples as one shard."""
        if not self._buffer:
            return
        oldest = self._buffer[0].ts
        if not force and (time.time() - oldest) < AVG_MAX_AGE_S:
            return
        samples = self._buffer
        obs = self._buffer_obs
        array = np.stack([s.values for s in samples]).astype(np.complex64)
        meta = {
            "obs": obs,
            "inputs": list(self._buffer_inputs),
            "t": [float(s.ts) for s in samples],
            "nchan": int(array.shape[-1]),
            "chan_avg": CHAN_AVG,
            # The MEAN frequency of the first 8-channel block, not the raw
            # native-channel top: ``web/vis.py``'s ``freq_axis`` reads this as
            # channel 0's own frequency and steps down by ``chan_bw_mhz`` per
            # channel, so leaving this at the native top would report every
            # avg8 channel 3.5 native channels (14 kHz) higher than the block
            # it is actually the average of (docs/api-vis.md).
            "freq_top_mhz": rowmap.FREQ_TOP_MHZ - 0.5 * (CHAN_AVG - 1) * rowmap.CHAN_BW_MHZ,
            "chan_bw_mhz": rowmap.CHAN_BW_MHZ * CHAN_AVG,
            "freq_order": "descending",
            "file_idx": [s.file_idx for s in samples],
            "int_idx": [s.int_idx for s in samples],
            "flags": {"first_of_file": [bool(s.first_of_file) for s in samples]},
        }
        try:
            ctx.shards.write(
                STREAM_AVG8,
                array,
                t0=samples[0].ts,
                t1=samples[-1].ts,
                meta=meta,
                chunks=(1, array.shape[1], array.shape[2]),
            )
        except Exception:
            log.exception("vis: vis_avg8 shard write failed; keeping the buffer")
            return
        self._buffer = []
        if obs is not None:
            # Its contents are now durable in the committed shard; the staging
            # copy would otherwise be replayed into a duplicate shard next
            # startup (harmless -- ``ShardWriter.write`` is idempotent by
            # ``t0`` -- but pointless to keep around).
            clear_avg8_staging(ctx.settings, obs)

    def close(self, ctx: CollectorContext) -> None:
        """Flush the channel-averaged buffer so an orderly stop loses nothing."""
        self._flush_buffer(ctx, force=True)


__all__ = [
    "AVG_BUFFER",
    "AVG_NCHAN",
    "CHAN_AVG",
    "DT_S",
    "GUARD_INTEGRATIONS",
    "INTEGRATIONS_PER_FILE",
    "INTEGRATION_BYTES",
    "NCHAN",
    "STREAM_AVG8",
    "STREAM_FULL",
    "VisCollector",
    "VisFile",
    "auto_indices",
    "band_power_db",
    "complete_integrations",
    "contiguous_runs",
    "dark_subbands",
    "input_sets",
    "input_table",
    "integration_time",
    "latest_vis_path",
    "newest_observation",
    "obs_start_unix",
    "pending_integrations",
    "read_latest_vis",
    "read_run",
    "safe_integrations",
    "scan_observation_files",
    "subband_medians",
    "write_latest_vis",
    "avg8_staging_path",
    "write_avg8_staging",
    "read_avg8_staging",
    "clear_avg8_staging",
]
