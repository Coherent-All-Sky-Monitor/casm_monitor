"""The Visibilities tab API: inputs, spectra, matrices, waterfalls, coherence.

Read-only by construction. Every route reads the layout CSV (re-read per call, so
a ``casm-layout apply`` shows up without a restart), the collector's
latest-integration mirror and committed shards through the store's read-only
handle. No route opens a visibility file, contacts a board or writes anything.

The cached sub-matrix is every WIRED input (``functional=1``); the two selectable
sets are ``live`` (``include_in_beamforming=1``, the default display) and
``wired`` (everything cached) -- the operator's 2026-09-08 decision, no
"all 48 inputs" view.

Every response carries the quantity/units/ref it was computed with, so a
bookmarked URL and its plot cannot disagree. The transforms themselves live in
:mod:`casm_monitor.vis_ops`; this module only selects baselines, clamps sizes and
serialises. Non-finite values are serialised as ``null`` (a flagged cal channel,
a dead input's coherence), never as the JSON-invalid ``NaN``.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from fastapi import APIRouter, HTTPException, Query

from .. import vis_ops
from ..collectors import rowmap
from ..collectors.kafka_bp import night_window_utc
from ..collectors.vis import (
    CHAN_AVG,
    DT_S,
    STREAM_AVG8,
    STREAM_FULL,
    input_sets,
    input_table,
    read_latest_vis,
)
from ..collectors.weights import read_last_ledger_row
from ..config import Settings
from ..store import ShardReader, Store
from ..util import iso, parse_iso

log = logging.getLogger("casm_monitor.web.vis")

PAIR_MODES = ("auto", "cross", "all")
DEFAULT_NCHAN = 768
MAX_NCHAN = 3072
DEFAULT_MAX_CELLS = 400_000
MIN_MAX_CELLS = 1_000
MAX_MAX_CELLS = 2_000_000
# Above this span a waterfall comes from vis_avg8 instead of vis_full (which is
# only kept 3 d anyway).
FULL_SPAN_S = 6 * 3600.0
# Longest span any route will consider, so a bookmarked t0=0 cannot ask the
# store for everything it has.
MAX_SPAN_S = 90 * 86400.0
MAX_TIMES = 50_000
# How close a requested timestamp must be to a cached integration.
TS_TOLERANCE_S = DT_S / 2.0
# Resolution labels for a waterfall's sample spacing.
RES_LABELS = ((1.5 * DT_S, "137s"), (10 * 60.0, "10min"), (3600.0, "1h"), (float("inf"), "6h"))


def _time_arg(field: str, text: str | None) -> float | None:
    """ISO-8601 or a unix timestamp; anything else is a 400."""
    if text is None or text == "":
        return None
    try:
        return float(text)
    except ValueError:
        pass
    parsed = parse_iso(text)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"{field} is not ISO-8601 or a unix timestamp")
    return parsed


def _span(t0: str | None, t1: str | None, default_s: float) -> tuple[float, float]:
    """Resolve and clamp a viewport span; ``t1`` defaults to now."""
    now = time.time()
    end = _time_arg("t1", t1)
    end = now if end is None else end
    start = _time_arg("t0", t0)
    start = end - default_s if start is None else start
    if end <= start:
        raise HTTPException(status_code=400, detail="t1 must be after t0")
    if end - start > MAX_SPAN_S:
        start = end - MAX_SPAN_S
    return start, end


def _finite(values: Any) -> Any:
    """Recursively replace non-finite floats with None (JSON has no NaN)."""
    if isinstance(values, (list, tuple)):
        return [_finite(v) for v in values]
    value = float(values)
    return value if math.isfinite(value) else None


def _round(values: np.ndarray, ndigits: int = 5) -> list:
    arr = np.asarray(values, dtype=np.float64)
    return _finite(np.round(arr, ndigits).tolist())


def res_label(dt_s: float) -> str:
    for limit, label in RES_LABELS:
        if dt_s <= limit:
            return label
    return RES_LABELS[-1][1]


def freq_axis(meta: dict[str, Any], nchan: int) -> np.ndarray:
    """The shard's own descending frequency axis."""
    top = float(meta.get("freq_top_mhz", rowmap.FREQ_TOP_MHZ))
    bw = float(meta.get("chan_bw_mhz", rowmap.CHAN_BW_MHZ))
    return top - np.arange(nchan, dtype=np.float64) * bw


# -- baseline selection -------------------------------------------------
@dataclass(frozen=True)
class Selection:
    """Which stored baselines a request needs, and how they are labelled."""

    inputs: list[int]            # packet_idx of the chosen subset, sorted
    pairs: list[tuple[int, int]]  # (rank_i, rank_j) inside ``inputs``
    flat: np.ndarray             # index of each pair in the stored triangle
    auto_flat: np.ndarray        # index of each input's autocorrelation

    @property
    def n_baselines(self) -> int:
        return len(self.pairs)


def select_baselines(
    stored_inputs: Sequence[int], subset: Sequence[int], pairs: str
) -> Selection:
    """Map a requested input subset + pair mode onto stored triangle indices."""
    from casm_io.correlator.baselines import triu_flat_index

    stored = [int(i) for i in stored_inputs]
    rank_in_store = {p: k for k, p in enumerate(stored)}
    chosen = sorted(p for p in {int(i) for i in subset} if p in rank_in_store)
    if not chosen:
        raise HTTPException(
            status_code=404,
            detail="none of the requested inputs are in the cached sub-matrix",
        )
    n = len(stored)
    combos: list[tuple[int, int]] = []
    for a in range(len(chosen)):
        for b in range(a, len(chosen)):
            if pairs == "auto" and a != b:
                continue
            if pairs == "cross" and a == b:
                continue
            combos.append((a, b))
    flat = np.array(
        [triu_flat_index(n, rank_in_store[chosen[a]], rank_in_store[chosen[b]]) for a, b in combos],
        dtype=int,
    )
    auto_flat = np.array(
        [triu_flat_index(n, rank_in_store[p], rank_in_store[p]) for p in chosen], dtype=int
    )
    return Selection(inputs=chosen, pairs=combos, flat=flat, auto_flat=auto_flat)


# -- layout, positions, cal --------------------------------------------
_positions_cache: dict[tuple[str, float], dict[int, tuple[float, float, float]]] = {}


def input_positions(layout_path: str | Path | None = None) -> dict[int, tuple[float, float, float]]:
    """packet_idx -> ENU position in metres, from ``AntennaMapping``.

    Cached on (path, mtime) so a fringe-stop request does not re-parse the layout
    every time, while a re-pointed ``current`` symlink still takes effect.
    """
    from casm_io.correlator.mapping import AntennaMapping

    path = Path(layout_path if layout_path is not None else rowmap.LAYOUT_CSV)
    try:
        key = (str(path.resolve()), path.stat().st_mtime)
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"layout unreadable: {exc}") from exc
    cached = _positions_cache.get(key)
    if cached is not None:
        return cached
    mapping = AntennaMapping.load(str(path))
    df = mapping.dataframe
    out: dict[int, tuple[float, float, float]] = {}
    for _, row in df.iterrows():
        try:
            out[int(row["packet_index"])] = (
                float(row["x_m"]), float(row["y_m"]), float(row["z_m"])
            )
        except (KeyError, TypeError, ValueError):
            continue
    _positions_cache.clear()
    _positions_cache[key] = out
    return out


def positions_array(inputs: Sequence[int]) -> np.ndarray:
    """(n_inputs, 3) ENU positions in the order of ``inputs``."""
    table = input_positions()
    missing = [int(p) for p in inputs if int(p) not in table]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"layout has no position for input(s) {missing}; cannot fringe-stop",
        )
    return np.array([table[int(p)] for p in inputs], dtype=np.float64)


def deployed_cal_path(settings: Settings) -> Path:
    """The cal HDF5 the deployed weights were built from (ledger last row).

    ``deployed_weights.csv``'s last row is the standing authority (wiki rule);
    its ``cal_file`` field may carry a trailing note in parentheses, and only the
    primary path is used.
    """
    row = read_last_ledger_row(settings.deployed_weights_csv)
    field = ((row or {}).get("cal_file") or "").split("(")[0].strip()
    if not field:
        raise HTTPException(
            status_code=503, detail="deployed_weights.csv has no cal_file in its last row"
        )
    return Path(field)


_cal_cache: dict[tuple[str, float], Any] = {}


def load_deployed_cal(settings: Settings) -> tuple[Any, Path]:
    """Load the deployed cal with the canonical loader, cached on (path, mtime)."""
    from bf_weights_generator.snap_weights import load_calibration_weights

    path = deployed_cal_path(settings)
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"cal file unreadable: {path} ({exc})") from exc
    cal = _cal_cache.get(key)
    if cal is None:
        try:
            cal = load_calibration_weights(str(path))
        except Exception as exc:  # pragma: no cover - depends on the live file
            raise HTTPException(status_code=503, detail=f"cal load failed: {exc}") from exc
        _cal_cache.clear()
        _cal_cache[key] = cal
    return cal, path


def apply_reference(
    v: np.ndarray,
    ref: str,
    *,
    settings: Settings,
    selection: Selection,
    freq_mhz: np.ndarray,
    times_unix: Sequence[float],
) -> tuple[np.ndarray, dict[str, Any]]:
    """Apply ``raw``/``sun``/``cal`` to ``(n_bl, F)`` or ``(T, n_bl, F)`` data."""
    if ref == "raw":
        return v, {"ref": "raw"}
    if ref == "sun":
        from casm_vis_analysis.sources import source_altaz

        positions = positions_array(selection.inputs)
        out = vis_ops.fringe_stop_sun(
            v, freq_mhz, positions, selection.pairs, times_unix
        )
        alt_deg, _az = source_altaz("sun", np.atleast_1d(np.asarray(times_unix, dtype=float)))
        alt = np.atleast_1d(np.asarray(alt_deg, dtype=float))
        return out, {
            "ref": "sun",
            "sign": vis_ops.FRINGE_STOP_SIGN,
            "source": "sun",
            "sun_alt_deg": round(float(alt.mean()), 3),
            # A Sun-stopped reference is meaningless while the Sun is down; the
            # transform still runs (it is just a phase rotation) and says so.
            "sun_below_horizon": bool(alt.max() <= 0.0),
        }
    cal, path = load_deployed_cal(settings)
    gains, have = vis_ops.cal_gains_on_axis(cal, selection.inputs, freq_mhz)
    out = vis_ops.apply_cal(v, gains, have, selection.pairs)
    return out, {
        "ref": "cal",
        "cal_file": path.name,
        "cal_path": str(path),
        "cal_source": getattr(cal, "source", None),
        "cal_ref_ant_id": getattr(cal, "ref_ant_id", None),
        "inputs_without_cal": [
            int(p) for p, ok in zip(selection.inputs, have.tolist()) if not ok
        ],
    }


# -- shard access -------------------------------------------------------
def response_flags(
    frame_flags: dict[str, Any] | None, ref_meta: dict[str, Any]
) -> dict[str, Any]:
    """The ``flags`` block of a response: the integration's own flags plus the
    two reference facts the frontend renders in prose (``docs/api-vis.md``)."""
    flags = dict(frame_flags or {})
    for key in ("sun_below_horizon", "cal_file"):
        if key in ref_meta:
            flags[key] = ref_meta[key]
    return flags


def _shard_meta_times(shard: dict[str, Any]) -> list[float]:
    meta = shard.get("meta") or {}
    times = [float(t) for t in (meta.get("t") or [])]
    return times or [float(shard["t0"])]


def load_baseline_rows(shard: dict[str, Any], flat: Sequence[int]) -> np.ndarray:
    """Read only the selected baseline rows of a shard, as ``(T, n_sel, F)``.

    ``vis_full`` is chunked one baseline per chunk, so a waterfall over 34
    integrations touches 3 chunks per shard (72 KB) instead of the whole 7.4 MB
    array. Any zarr problem falls back to loading the shard whole.
    """
    import zarr

    from ..store.shards import DATA_ARRAY

    idx = [int(k) for k in flat]
    try:
        array = zarr.open_group(store=str(shard["path"]), mode="r")[DATA_ARRAY]
        if array.ndim == 2:
            return np.asarray(array.oindex[idx, :])[None]
        return np.asarray(array.oindex[:, idx, :])
    except Exception:
        log.debug("vis: row-wise shard read failed for %s; loading it whole", shard["path"])
        group = zarr.open_group(store=str(shard["path"]), mode="r")
        whole = np.asarray(group[DATA_ARRAY][...])
        cube = whole[None] if whole.ndim == 2 else whole
        return cube[:, idx, :]


class VisStore:
    """Thin read-only view of the vis shards plus the latest-integration mirror."""

    def __init__(self, settings: Settings, reader: Store) -> None:
        self.settings = settings
        self.reader = reader
        self.shards = ShardReader(reader)
        self._latest_cache: tuple[float, dict[str, Any]] | None = None

    # -- newest integration ------------------------------------------
    def latest(self) -> dict[str, Any] | None:
        """The mirrored newest integration, or the newest ``vis_full`` shard."""
        path = Path(self.settings.store_root) / "latest" / "vis_latest.npz"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime is not None:
            if self._latest_cache is not None and self._latest_cache[0] == mtime:
                return self._latest_cache[1]
            frame = read_latest_vis(self.settings)
            if frame is not None:
                payload = {
                    "vis": frame["vis"],
                    "inputs": frame["inputs"],
                    "freq_mhz": frame["freq_mhz"],
                    "ts": frame["ts"],
                    "obs": frame["obs"],
                    "flags": {"first_of_file": frame["first_of_file"]},
                    "source": "mirror",
                }
                self._latest_cache = (mtime, payload)
                return payload
        rows = self.reader.query(
            "SELECT MAX(t1) AS t FROM shards WHERE stream = ?", (STREAM_FULL,)
        )
        newest = rows[0]["t"] if rows else None
        return None if newest is None else self.at(float(newest))

    def at(self, ts: float) -> dict[str, Any] | None:
        """The cached full-resolution integration nearest ``ts``."""
        window = 2 * DT_S
        candidates = self.shards.list(STREAM_FULL, t0=ts - window, t1=ts + window)
        if not candidates:
            return None
        shard = min(candidates, key=lambda s: abs(float(s["t0"]) - ts))
        if abs(float(shard["t0"]) - ts) > TS_TOLERANCE_S:
            return None
        array, meta = self.shards.load(shard)
        return {
            "vis": np.asarray(array),
            "inputs": [int(i) for i in (meta.get("inputs") or [])],
            "freq_mhz": freq_axis(meta, np.asarray(array).shape[-1]),
            "ts": float(shard["t0"]),
            "obs": meta.get("obs"),
            "flags": meta.get("flags") or {},
            "source": STREAM_FULL,
        }

    def resolve(self, ts: str | None) -> dict[str, Any]:
        """``latest`` or a unix/ISO timestamp -> one integration, or 404."""
        text = (ts or "latest").strip()
        frame = self.latest() if text.lower() == "latest" else self.at(
            float(_time_arg("ts", text) or 0.0)
        )
        if frame is None:
            raise HTTPException(
                status_code=404,
                detail="no cached integration for that time (the collector may not have run yet)",
            )
        return frame

    # -- history ------------------------------------------------------
    def times(self, t0: float, t1: float) -> list[float]:
        """Cached integration times in the span, from both streams."""
        seen: list[float] = []
        for stream in (STREAM_FULL, STREAM_AVG8):
            for shard in self.shards.list(stream, t0=t0, t1=t1):
                seen.extend(t for t in _shard_meta_times(shard) if t0 <= t <= t1)
        seen.sort()
        out: list[float] = []
        for t in seen:
            if not out or t - out[-1] > 1.0:
                out.append(t)
        return out

    def series(
        self, stream: str, t0: float, t1: float, flat: Sequence[int]
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[int]]:
        """Stack one baseline set over time from ``stream``.

        Returns ``(z (T, n_sel, F), times, freq_mhz, inputs)``; a shard whose
        stored input list differs from the newest one is skipped rather than
        stacked into a ragged array (the wired set grows as antennas are wired).
        """
        chunks: list[np.ndarray] = []
        stamps: list[float] = []
        freq: np.ndarray | None = None
        inputs: list[int] = []
        for shard in self.shards.list(stream, t0=t0, t1=t1):
            meta = shard["meta"] or {}
            shard_inputs = [int(i) for i in (meta.get("inputs") or [])]
            if inputs and shard_inputs != inputs:
                continue
            times = np.asarray(_shard_meta_times(shard), dtype=np.float64)
            keep_any = ((times >= t0) & (times <= t1)).any()
            if not keep_any:
                continue
            cube = load_baseline_rows(shard, flat)
            if times.size != cube.shape[0]:
                times = np.linspace(shard["t0"], shard["t1"], cube.shape[0])
            keep = (times >= t0) & (times <= t1)
            if not keep.any():
                continue
            inputs = inputs or shard_inputs
            freq = freq if freq is not None else freq_axis(meta, cube.shape[-1])
            chunks.append(np.asarray(cube[keep], dtype=np.complex64))
            stamps.extend(float(v) for v in times[keep])
        if not chunks:
            return (
                np.zeros((0, len(flat), 0), dtype=np.complex64),
                np.zeros(0),
                np.zeros(0),
                [],
            )
        z = np.concatenate(chunks, axis=0)
        stamp_arr = np.asarray(stamps, dtype=np.float64)
        order = np.argsort(stamp_arr)
        return z[order], stamp_arr[order], np.asarray(freq), inputs


# -- router -------------------------------------------------------------
def build_router(settings: Settings, reader: Store) -> APIRouter:
    """The Visibilities router. Read-only; every size parameter is clamped."""
    router = APIRouter(prefix="/api/vis", tags=["vis"])
    data = VisStore(settings, reader)

    def check(quantity: str, units: str | None, ref: str, pairs: str | None = None):
        try:
            return vis_ops.check_params(quantity, units, ref, pairs=pairs)
        except vis_ops.ParamError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def subset_for(set_name: str, stored_inputs: Sequence[int]) -> list[int]:
        name = (set_name or "live").strip().lower()
        if name not in ("live", "wired"):
            raise HTTPException(status_code=400, detail="set must be live|wired")
        try:
            sets = input_sets()
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"layout unreadable: {exc}") from exc
        return [p for p in sets[name] if p in set(int(i) for i in stored_inputs)]

    def clamp_cells(max_cells: int) -> int:
        return max(MIN_MAX_CELLS, min(int(max_cells), MAX_MAX_CELLS))

    def antenna_of(packet_idx: int) -> int:
        return int(packet_idx) + 1

    # -- inputs ------------------------------------------------------
    @router.get("/inputs")
    def inputs() -> dict[str, Any]:
        try:
            sets = input_sets()
            table = input_table()
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"layout unreadable: {exc}") from exc
        rows = reader.query(
            "SELECT COUNT(*) AS n, MIN(t0) AS oldest, MAX(t1) AS newest "
            "FROM shards WHERE stream = ?",
            (STREAM_FULL,),
        )
        row = rows[0] if rows else {"n": 0, "oldest": None, "newest": None}
        latest = data.latest()
        latest_ts = None if latest is None else float(latest["ts"])
        avg8 = reader.query(
            "SELECT COUNT(*) AS n, MIN(t0) AS oldest FROM shards WHERE stream = ?",
            (STREAM_AVG8,),
        )
        return {
            "sets": {"live": sets["live"], "wired": sets["wired"]},
            "inputs": table,
            "obs": {
                "utc_start": None if latest is None else latest.get("obs"),
                "latest_ts": latest_ts,
                "latest_ts_iso": iso(latest_ts),
                "n_cached": int(row["n"] or 0),
                "oldest_ts": None if row["oldest"] is None else float(row["oldest"]),
                "oldest_ts_iso": iso(row["oldest"]),
                "age_s": None if latest_ts is None else round(max(0.0, time.time() - latest_ts), 1),
                "n_cached_avg8": int((avg8[0]["n"] if avg8 else 0) or 0),
                "oldest_avg8_ts": (
                    None if not avg8 or avg8[0]["oldest"] is None else float(avg8[0]["oldest"])
                ),
            },
            "quantities": list(vis_ops.QUANTITIES),
            "units": {k: list(v) for k, v in vis_ops.UNITS_FOR.items()},
            "refs": list(vis_ops.REFS),
        }

    # -- times -------------------------------------------------------
    @router.get("/times")
    def times(t0: str | None = None, t1: str | None = None) -> dict[str, Any]:
        start, end = _span(t0, t1, 6 * 3600.0)
        values = data.times(start, end)
        n_raw = len(values)
        if n_raw > MAX_TIMES:
            stride = int(np.ceil(n_raw / MAX_TIMES))
            values = values[::stride]
        return {
            "t": values,
            "t0": start,
            "t1": end,
            "n": len(values),
            "n_raw": n_raw,
            "dt_s": DT_S,
        }

    # -- spectra -----------------------------------------------------
    @router.get("/spectra")
    def spectra(
        ts: str = "latest",
        set: str = "live",
        pairs: str = "auto",
        quantity: str = "amp",
        units: str | None = None,
        ref: str = "raw",
        nchan: int = Query(default=DEFAULT_NCHAN, ge=1, le=MAX_NCHAN),
        max_cells: int = DEFAULT_MAX_CELLS,
    ) -> dict[str, Any]:
        if pairs not in PAIR_MODES:
            raise HTTPException(status_code=400, detail=f"pairs must be {'|'.join(PAIR_MODES)}")
        q, u, r = check(quantity, units, ref, pairs)
        frame = data.resolve(ts)
        selection = select_baselines(frame["inputs"], subset_for(set, frame["inputs"]), pairs)
        cells = clamp_cells(max_cells)
        n_chan_out = min(int(nchan), int(frame["vis"].shape[-1]))
        if selection.n_baselines * n_chan_out > cells:
            n_chan_out = max(1, cells // max(1, selection.n_baselines))

        v = np.asarray(frame["vis"])[selection.flat]
        freq = np.asarray(frame["freq_mhz"], dtype=np.float64)
        v, ref_meta = apply_reference(
            v,
            r,
            settings=settings,
            selection=selection,
            freq_mhz=freq,
            times_unix=[frame["ts"]],
        )
        auto_i = auto_j = None
        if q == "coh":
            autos = np.abs(np.asarray(frame["vis"])[selection.auto_flat])
            auto_i = np.stack([autos[a] for a, _ in selection.pairs])
            auto_j = np.stack([autos[b] for _, b in selection.pairs])
        started = time.time()
        y = vis_ops.spectra_values(
            v, q, u, nchan=n_chan_out, auto_i=auto_i, auto_j=auto_j
        )
        freq_out = vis_ops.block_mean(freq, n_chan_out)
        transform_s = time.time() - started
        return {
            "ts": frame["ts"],
            "ts_iso": iso(frame["ts"]),
            "obs": frame.get("obs"),
            "set": set,
            "pairs": pairs,
            "quantity": q,
            "units": u,
            "units_label": vis_ops.units_label(q, u),
            "ref": r,
            "ref_meta": ref_meta,
            "nchan": int(len(freq_out)),
            "freq_mhz": _round(freq_out, 5),
            "baselines": [
                {
                    "i": selection.inputs[a],
                    "j": selection.inputs[b],
                    "ant_i": antenna_of(selection.inputs[a]),
                    "ant_j": antenna_of(selection.inputs[b]),
                    "y": _round(y[k], 5),
                }
                for k, (a, b) in enumerate(selection.pairs)
            ],
            "flags": response_flags(frame.get("flags"), ref_meta),
            "source": frame.get("source"),
            "transform_s": round(transform_s, 3),
        }

    # -- matrix ------------------------------------------------------
    @router.get("/matrix")
    def matrix(
        ts: str = "latest",
        set: str = "wired",
        quantity: str = "amp",
        units: str | None = None,
        ref: str = "raw",
        fmin: float | None = None,
        fmax: float | None = None,
    ) -> dict[str, Any]:
        q, u, r = check(quantity, units, ref)
        frame = data.resolve(ts)
        selection = select_baselines(frame["inputs"], subset_for(set, frame["inputs"]), "all")
        freq = np.asarray(frame["freq_mhz"], dtype=np.float64)
        lo = -np.inf if fmin is None else float(fmin)
        hi = np.inf if fmax is None else float(fmax)
        if lo >= hi:
            raise HTTPException(status_code=400, detail="fmin must be below fmax")
        mask = (freq >= lo) & (freq <= hi)
        if not mask.any():
            raise HTTPException(
                status_code=400,
                detail=f"no channels between {lo} and {hi} MHz (band is "
                f"{freq.min():.3f}-{freq.max():.3f})",
            )
        v = np.asarray(frame["vis"])[selection.flat][:, mask]
        v, ref_meta = apply_reference(
            v,
            r,
            settings=settings,
            selection=selection,
            freq_mhz=freq[mask],
            times_unix=[frame["ts"]],
        )
        auto_i = auto_j = None
        if q == "coh":
            autos = np.abs(np.asarray(frame["vis"])[selection.auto_flat][:, mask])
            auto_i = np.stack([autos[a] for a, _ in selection.pairs])
            auto_j = np.stack([autos[b] for _, b in selection.pairs])
        started = time.time()
        # One value per baseline: the whole selected band is one block.
        values = vis_ops.spectra_values(v, q, u, nchan=1, auto_i=auto_i, auto_j=auto_j)
        values = np.asarray(values, dtype=np.float64).reshape(-1)
        n = len(selection.inputs)
        m = np.full((n, n), np.nan, dtype=np.float64)
        for k, (a, b) in enumerate(selection.pairs):
            m[a, b] = values[k]
            # V_ji = conj(V_ij): the phase flips sign, every other quantity is
            # symmetric (imag flips too).
            m[b, a] = -values[k] if q in ("phase", "imag") and a != b else values[k]
        transform_s = time.time() - started
        return {
            "ts": frame["ts"],
            "ts_iso": iso(frame["ts"]),
            "set": set,
            "quantity": q,
            "units": u,
            "units_label": vis_ops.units_label(q, u),
            "ref": r,
            "ref_meta": ref_meta,
            "fmin": None if fmin is None else float(fmin),
            "fmax": None if fmax is None else float(fmax),
            "band_mhz": [float(freq[mask].min()), float(freq[mask].max())],
            "n_channels": int(mask.sum()),
            "inputs": selection.inputs,
            "antennas": [antenna_of(p) for p in selection.inputs],
            "m": [_round(row, 5) for row in m],
            "flags": response_flags(frame.get("flags"), ref_meta),
            "transform_s": round(transform_s, 3),
        }

    # -- waterfall ---------------------------------------------------
    @router.get("/waterfall")
    def waterfall(
        i: int,
        j: int,
        t0: str | None = None,
        t1: str | None = None,
        quantity: str = "amp",
        units: str | None = None,
        ref: str = "raw",
        max_cells: int = DEFAULT_MAX_CELLS,
    ) -> dict[str, Any]:
        q, u, r = check(quantity, units, ref)
        cells = clamp_cells(max_cells)
        start, end = _span(t0, t1, 6 * 3600.0)
        newest = data.latest()
        if newest is None:
            raise HTTPException(status_code=404, detail="nothing cached yet")
        stored = [int(x) for x in newest["inputs"]]
        lo, hi = sorted((int(i), int(j)))
        selection = select_baselines(stored, [lo, hi], "all" if lo != hi else "auto")
        stream = STREAM_FULL if (end - start) <= FULL_SPAN_S else STREAM_AVG8
        # The cross baseline is the only pair we plot; for lo != hi that is the
        # middle of the three combinations ((lo,lo), (lo,hi), (hi,hi)).
        cross = [k for k, (a, b) in enumerate(selection.pairs) if a != b]
        want = cross[0] if cross else 0
        z, times, freq, _inputs = data.series(stream, start, end, selection.flat)
        if z.shape[0] == 0 and stream == STREAM_FULL:
            stream = STREAM_AVG8
            z, times, freq, _inputs = data.series(stream, start, end, selection.flat)
        if z.shape[0] == 0:
            return {
                "i": lo, "j": hi, "ant_i": antenna_of(lo), "ant_j": antenna_of(hi),
                "t": [], "t_iso": [], "freq_mhz": [], "z": [],
                "res": res_label(DT_S), "stream": stream, "quantity": q, "units": u,
                "units_label": vis_ops.units_label(q, u), "ref": r, "ref_meta": {"ref": r},
                "n_samples_raw": 0,
            }
        v, ref_meta = apply_reference(
            z, r, settings=settings, selection=selection, freq_mhz=freq, times_unix=times
        )
        started = time.time()
        target_t, target_f = vis_ops.decimation_targets(z.shape[0], z.shape[2], cells)
        picked = v[:, want, :]
        if q == "amp":
            power = vis_ops.decimate_axes(np.abs(picked) ** 2, target_t, target_f)
            values = vis_ops.apply_units(np.sqrt(np.maximum(power, 0.0)), q, u)
        elif q == "coh":
            auto_i = vis_ops.decimate_axes(
                np.abs(z[:, selection.pairs.index((0, 0)), :]), target_t, target_f
            )
            last = len(selection.inputs) - 1
            auto_j = vis_ops.decimate_axes(
                np.abs(z[:, selection.pairs.index((last, last)), :]), target_t, target_f
            )
            v_avg = vis_ops.decimate_axes(picked, target_t, target_f)
            values = vis_ops.apply_units(
                vis_ops.quantity_values(v_avg, q, auto_i=auto_i, auto_j=auto_j), q, u
            )
        else:
            v_avg = vis_ops.decimate_axes(picked, target_t, target_f)
            values = vis_ops.apply_units(vis_ops.quantity_values(v_avg, q), q, u)
        t_out = vis_ops.block_mean(times, target_t)
        freq_out = vis_ops.block_mean(freq, target_f)
        transform_s = time.time() - started
        dt = float(np.median(np.diff(t_out))) if t_out.size > 1 else DT_S
        return {
            "i": lo,
            "j": hi,
            "ant_i": antenna_of(lo),
            "ant_j": antenna_of(hi),
            "t": [float(v) for v in t_out],
            "t_iso": [iso(float(v)) for v in t_out],
            "freq_mhz": _round(freq_out, 5),
            "z": [_round(row, 5) for row in np.atleast_2d(values)],
            "res": res_label(dt),
            "stream": stream,
            "quantity": q,
            "units": u,
            "units_label": vis_ops.units_label(q, u),
            "ref": r,
            "ref_meta": ref_meta,
            "n_samples_raw": int(z.shape[0]),
            "max_cells": cells,
            "transform_s": round(transform_s, 3),
        }

    # -- coherence ---------------------------------------------------
    @router.get("/coherence")
    def coherence(
        t0: str | None = None,
        t1: str | None = None,
        set: str = "wired",
        ref: str = "raw",
    ) -> dict[str, Any]:
        _q, _u, r = check("coh", None, ref)
        if t0 is None and t1 is None:
            # Default window: the quiet 02:00-05:00 PT window of last night,
            # the same one the bandpass night median uses.
            from datetime import datetime, timezone

            start, end = night_window_utc(datetime.now(timezone.utc))
            if end > time.time():
                start, end = start - 86400.0, end - 86400.0
        else:
            start, end = _span(t0, t1, 3 * 3600.0)
        newest = data.latest()
        if newest is None:
            raise HTTPException(status_code=404, detail="nothing cached yet")
        selection = select_baselines(
            [int(x) for x in newest["inputs"]], subset_for(set, newest["inputs"]), "all"
        )
        z, times, freq, _inputs = data.series(STREAM_AVG8, start, end, selection.flat)
        if z.shape[0] == 0:
            z, times, freq, _inputs = data.series(STREAM_FULL, start, end, selection.flat)
        n = len(selection.inputs)
        if z.shape[0] == 0:
            return {
                "t0": start, "t1": end, "t0_iso": iso(start), "t1_iso": iso(end),
                "set": set, "ref": r, "inputs": selection.inputs,
                "antennas": [antenna_of(p) for p in selection.inputs],
                "m": [[None] * n for _ in range(n)], "n_samples": 0,
            }
        v, ref_meta = apply_reference(
            z, r, settings=settings, selection=selection, freq_mhz=freq, times_unix=times
        )
        # Coherent average over time AND frequency (the vector mean is the
        # point: an uncalibrated fringe averages away); A_i = |V_ii|.
        mean_v = v.mean(axis=(0, 2))
        autos = np.abs(z).astype(np.float64)
        auto_mean = np.zeros(n, dtype=np.float64)
        for a in range(n):
            auto_mean[a] = autos[:, selection.pairs.index((a, a)), :].mean()
        m = np.full((n, n), np.nan, dtype=np.float64)
        for k, (a, b) in enumerate(selection.pairs):
            value = float(
                vis_ops.quantity_values(
                    mean_v[k], "coh", auto_i=auto_mean[a], auto_j=auto_mean[b]
                )
            )
            m[a, b] = m[b, a] = value
        return {
            "t0": start,
            "t1": end,
            "t0_iso": iso(start),
            "t1_iso": iso(end),
            "set": set,
            "ref": r,
            "ref_meta": ref_meta,
            "inputs": selection.inputs,
            "antennas": [antenna_of(p) for p in selection.inputs],
            "m": [_round(row, 5) for row in m],
            "n_samples": int(z.shape[0]),
            "n_channels": int(z.shape[2]),
            "chan_avg": CHAN_AVG,
        }

    return router


__all__ = ["Selection", "VisStore", "build_router", "select_baselines"]
