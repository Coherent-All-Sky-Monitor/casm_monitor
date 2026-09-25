"""The ``snap_read`` job: one serialized, read-only pass over the SNAP boards.

Everything the operator's constraints demand lives here, not in the caller:

* **one reader at a time, across processes and restarts.** The job takes a
  persisted lock (``watermarks`` row ``snap_read/lock``) with one atomic
  compare-and-set that only succeeds if the previous holder's lease has
  expired, and releases it in a ``finally`` conditional on still owning it.
  Ownership is a **unique token** per job (not the host:pid string, which a
  restarted service can repeat), and the lease is **renewed every
  ``LOCK_RENEW_S``** by a background thread for as long as the ssh runs, so a
  read that outlives ``LOCK_TTL_S`` never lets a second reader in. If a renewal
  fails — i.e. somebody else now owns the row — the renewer kills the ssh
  process group immediately, so hardware contact stops before the new owner can
  start its own. A crashed job therefore frees the lock ``LOCK_TTL_S`` after
  its last renewal, and only then.
* **serialized ssh sessions, boards in sequence.** The remote script
  (``casm_monitor/remote/snap_read_remote.py``) is piped to zapdos on stdin, so
  nothing is left on its disk, and it walks the boards one after another with a
  per-board wall-clock budget. A second, getter-only PPS comparison follows
  under the same lease; it never overlaps the spectrum read.
* **read-only.** The remote script calls getters only; this module never sends
  any other command to zapdos.

What a successful pass writes into the store, per board: a ``snap_read`` shard
(the 12x4096 spectra plus a metadata dict, no TTL, kept forever), per-input
``snap.<ip>.adc_rms`` and ``snap.<ip>.band_power_db`` scalars tagged with the
ADC and the correlator input index, a ``snap_read_latest`` row for the API, and
the events ``eq_changed`` / ``feng_id_mismatch`` / ``board_unprogrammed``.

The pass is also *the* hourly zapdos contact: it stamps the same
``zapdos/last_probe_ts`` watermark the liveness probe uses (see
:mod:`casm_monitor.collectors.services`), so the two never contact zapdos in
the same hour.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from ..config import Settings, load_settings
from ..snapmap import Board, all_boards, input_tags, read_layout_inputs
from ..store import ShardWriter, Store

log = logging.getLogger("casm_monitor.jobs.snap_read")

SHARD_STREAM = "snap_read"
LOCK_STREAM = "snap_read"
LOCK_KEY = "lock"
LOCK_TTL_S = 600.0  # 10 min: longer than any read, short enough to self-heal
LOCK_RENEW_S = 60.0  # the holder pushes the lease out again this often
LAST_MANUAL_KEY = "last_manual_ts"
LAST_READ_KEY = "last_read_ts"
# The remote script's ``version`` in meta; an archive from any other version is
# refused rather than half-understood.
REMOTE_VERSION = 1

REMOTE_SCRIPT = Path(__file__).resolve().parent.parent / "remote" / "snap_read_remote.py"

N_INPUTS = 12
N_CHANS = 4096
# FPGA clock = ADC clock (casm_f DEFAULT_SAMPLE_RATE_HZ): a healthy PPS period
# is one second of these ticks. Measured on the live boards 2026-09-08:
# 249,998,690 ticks, which zapdos' own pps_status.py calls the healthy value.
# The board reports its own fs_hz, so this is only the fallback.
FS_HZ = 250_000_000.0
PPS_PERIOD_TOL = 1e-3

LATEST_DDL = """
CREATE TABLE IF NOT EXISTS snap_read_latest (
    ip       TEXT PRIMARY KEY,
    ts       REAL NOT NULL,
    shard_id INTEGER,
    summary  TEXT NOT NULL
)
"""


# -- persisted lock -----------------------------------------------------
def ensure_latest_table(store: Store) -> None:
    """Create ``snap_read_latest`` (no-op on a read-only handle)."""
    if store.read_only:
        return
    store.execute(LATEST_DDL)


def new_lock_token() -> str:
    """A lease token no other job can repeat (a restarted service reuses pids)."""
    return uuid.uuid4().hex


def acquire_lock(
    store: Store,
    holder: str,
    *,
    ttl_s: float = LOCK_TTL_S,
    now: float | None = None,
    token: str | None = None,
) -> str | None:
    """Take the read lock; returns the lease token, or None if somebody holds it.

    One statement, one transaction: the UPDATE branch fires only when the
    stored expiry is in the past, so two processes racing (a click and the
    scheduler, or a restarted service overlapping the old one) cannot both win.
    The token identifies THIS lease: only its owner can renew or release it.
    """
    t = time.time() if now is None else now
    tok = token or new_lock_token()
    value = json.dumps(
        {"holder": holder, "token": tok, "acquired": t, "expires": t + float(ttl_s)}
    )
    cur = store.execute(
        "INSERT INTO watermarks (stream, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(stream, key) DO UPDATE SET value = excluded.value "
        "WHERE CAST(json_extract(watermarks.value, '$.expires') AS REAL) <= ?",
        (LOCK_STREAM, LOCK_KEY, value, t),
    )
    return tok if cur.rowcount == 1 else None


def renew_lock(
    store: Store, token: str, *, ttl_s: float = LOCK_TTL_S, now: float | None = None
) -> bool:
    """Push our lease out by ``ttl_s``; False means we no longer own the lock.

    A False here is not a warning, it is a stop signal: somebody else's read
    may already be talking to the boards (see :class:`LeaseRenewer`).
    """
    t = time.time() if now is None else now
    cur = store.execute(
        "UPDATE watermarks SET value = json_set(value, '$.expires', ?) "
        "WHERE stream = ? AND key = ? AND json_extract(value, '$.token') = ?",
        (t + float(ttl_s), LOCK_STREAM, LOCK_KEY, token),
    )
    return cur.rowcount == 1


def release_lock(store: Store, token: str) -> bool:
    """Release the lock, but only if the stored token is still ours."""
    cur = store.execute(
        "DELETE FROM watermarks WHERE stream = ? AND key = ? "
        "AND json_extract(value, '$.token') = ?",
        (LOCK_STREAM, LOCK_KEY, token),
    )
    return cur.rowcount == 1


def lock_holder(store: Store, *, now: float | None = None) -> dict[str, Any] | None:
    """The live lock record, or None if free/expired (never raises)."""
    t = time.time() if now is None else now
    value = store.get_watermark(LOCK_STREAM, LOCK_KEY)
    if not isinstance(value, dict):
        return None
    try:
        expires = float(value.get("expires", 0.0))
    except (TypeError, ValueError):
        return None
    return value if expires > t else None


# -- npz parsing --------------------------------------------------------
MANDATORY_ARRAYS = ("spectra", "adc_rms", "adc_mean", "adc_power", "eq_coeffs")


def parse_npz(data: bytes, *, requested_ips: Sequence[str] | None = None) -> dict[str, Any]:
    """Turn the remote archive into ``{"meta": ..., "boards": {ip: {...}}}``.

    Raises ``ValueError`` when the archive is unusable, which is what makes a
    truncated ssh transfer — or a partial, half-populated read — a failed job
    rather than a silent empty one. Validated, in order:

    * the archive carries ``meta_json`` and its ``version`` is
      :data:`REMOTE_VERSION` (a zapdos running a different script is refused,
      not half-understood);
    * the ip set answered equals the ip set requested (a board silently dropped
      from the pass is a failure, not a missing card);
    * every board carries all of :data:`MANDATORY_ARRAYS`, and its spectra are
      either populated or accompanied by an error string saying why not (this
      is what a relay board with ``acc_len=0`` reports).
    """
    with np.load(io.BytesIO(data), allow_pickle=False) as npz:
        if "meta_json" not in npz:
            raise ValueError("npz has no meta_json (truncated or wrong stream?)")
        meta = json.loads(str(npz["meta_json"][()]))
        version = meta.get("version", meta.get("script_version"))
        if int(version or 0) != REMOTE_VERSION:
            raise ValueError(
                f"remote script version {version!r} != expected {REMOTE_VERSION}"
            )
        raw_boards = meta.get("boards") or {}
        if requested_ips is not None:
            wanted = {str(ip) for ip in requested_ips}
            answered = {str(ip) for ip in raw_boards}
            listed = {str(ip) for ip in (meta.get("ips") or [])}
            if answered != wanted or listed != wanted:
                raise ValueError(
                    f"archive covers {sorted(answered)} (meta ips {sorted(listed)}), "
                    f"requested {sorted(wanted)}"
                )
        boards: dict[str, Any] = {}
        for ip, board_meta in raw_boards.items():
            entry = dict(board_meta)
            for field in MANDATORY_ARRAYS:
                key = f"{ip}__{field}"
                if key not in npz:
                    raise ValueError(f"board {ip} is missing its {field} array")
                entry[field] = np.asarray(npz[key])
            spectra = entry["spectra"]
            if spectra.ndim != 2:
                raise ValueError(f"board {ip} spectra have shape {spectra.shape}")
            if not np.isfinite(spectra).any() and not (board_meta.get("errors") or {}):
                raise ValueError(
                    f"board {ip} returned no spectra and no error saying why"
                )
            boards[ip] = entry
    return {"meta": {k: v for k, v in meta.items() if k != "boards"}, "boards": boards}


def eq_epoch(eq_coeffs: np.ndarray | None, fft_shift: int | None) -> str | None:
    """sha256 of the EQ coefficients and the FFT shift, first 12 hex digits.

    This is the EQ/gain provenance tag: every spectrum stored by this job
    carries it, so a dB comparison across a coefficient change can be refused
    instead of silently plotted.
    """
    if eq_coeffs is None:
        return None
    digest = hashlib.sha256()
    digest.update(np.ascontiguousarray(eq_coeffs, dtype=np.float32).tobytes())
    digest.update(str(fft_shift).encode())
    return digest.hexdigest()[:12]


def pps_summary(
    pps: dict[str, Any] | None,
    errors: dict[str, Any] | None,
    fs_hz: float | None = None,
) -> dict[str, Any]:
    """``{"ok", "period", "detail"}`` from the raw sync counters.

    ``ok`` means: PPS pulses have been counted and the measured period is
    within 0.1% of one second of the board's own clock. ``loopback on`` is
    reported in the detail but does not by itself make the board bad — it is
    the operator's flag that the board is generating its own sync.
    """
    pps = pps or {}
    errors = errors or {}
    clock = float(fs_hz) if fs_hz and np.isfinite(fs_hz) else FS_HZ
    period = pps.get("period_pps")
    counts = pps.get("count_pps")
    sync_errors = {k: v for k, v in errors.items() if k in
                   ("count_pps", "count_ext", "period_pps", "period", "sync_ctrl")}
    if len(sync_errors) >= 5 or (period is None and counts is None):
        first = next(iter(sync_errors.values()), "no sync registers read")
        return {"ok": False, "period": None, "detail": f"sync unreadable: {first}"}
    ok = (
        period is not None
        and abs(float(period) - clock) <= PPS_PERIOD_TOL * clock
        and bool(counts)
    )
    bits = [
        f"count_pps={counts}",
        f"count_ext={pps.get('count_ext')}",
        f"period_pps={period}",
        f"clk={clock / 1e6:.0f} MHz",
    ]
    if pps.get("loopback"):
        bits.append("loopback on")
    if sync_errors:
        bits.append(f"{len(sync_errors)} sync call(s) failed")
    return {"ok": bool(ok), "period": period, "detail": ", ".join(bits)}


def band_power_db(spectrum: np.ndarray | None) -> float | None:
    """Mean linear power over the band, in dB; None when nothing was read."""
    if spectrum is None:
        return None
    arr = np.asarray(spectrum, dtype=np.float64)
    finite = arr[np.isfinite(arr) & (arr > 0)]
    if finite.size == 0:
        return None
    return float(round(10.0 * np.log10(float(finite.mean())), 4))


# -- store side ---------------------------------------------------------
def _latest_summary(store: Store, ip: str) -> dict[str, Any] | None:
    try:
        rows = store.query("SELECT summary FROM snap_read_latest WHERE ip = ?", (ip,))
    except Exception:  # table not created yet (fresh store, read-only handle)
        return None
    if not rows:
        return None
    try:
        return json.loads(rows[0]["summary"])
    except (TypeError, ValueError):
        return None


def latest_reads(store: Store) -> dict[str, dict[str, Any]]:
    """Per-board summaries for the API (``{}`` before the first read)."""
    try:
        rows = store.query("SELECT ip, ts, shard_id, summary FROM snap_read_latest")
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            summary = json.loads(row["summary"])
        except (TypeError, ValueError):
            continue
        summary["ts"] = row["ts"]
        summary["shard_id"] = row["shard_id"]
        out[str(row["ip"])] = summary
    return out


def ingest(
    store: Store,
    settings: Settings,
    parsed: dict[str, Any],
    *,
    reason: str = "scheduled",
    shards: ShardWriter | None = None,
) -> dict[str, Any]:
    """Write one parsed read into the store; returns the per-board summary."""
    ensure_latest_table(store)
    shards = shards or ShardWriter(store, settings.shards_root)
    layout = read_layout_inputs(settings.snap_layout_csv)
    boards_cfg = {b.ip: b for b in all_boards(settings)}
    out: dict[str, Any] = {}

    for ip, board in (parsed.get("boards") or {}).items():
        cfg: Board | None = boards_cfg.get(ip)
        ts = float(board.get("ts") or time.time())
        spectra = board.get("spectra")
        eq_coeffs = board.get("eq_coeffs")
        errors = dict(board.get("errors") or {})
        epoch = eq_epoch(eq_coeffs, board.get("fft_shift"))
        feng_ids_hw = board.get("feng_ids_hw")
        feng_id_hw = int(feng_ids_hw[0]) if feng_ids_hw else None
        feng_id_cfg = None if cfg is None else cfg.feng_id
        programmed = board.get("programmed")

        summary: dict[str, Any] = {
            "ip": ip,
            "role": "relay" if (cfg is not None and cfg.role == "relay") else "antenna",
            "slot": None if cfg is None else cfg.slot,
            "programmed": programmed,
            "eq_epoch": epoch,
            "feng_id_hw": feng_id_hw,
            "feng_ids_hw": feng_ids_hw,
            "feng_id_cfg": feng_id_cfg,
            "fft_shift": board.get("fft_shift"),
            "overflow_count": board.get("overflow_count"),
            "acc_len": board.get("acc_len"),
            "switch_position": board.get("switch_position"),
            "pps": pps_summary(board.get("pps"), errors, board.get("fs_hz")),
            "fs_hz": board.get("fs_hz"),
            "pps_raw": board.get("pps"),
            "adc_rms": _finite_list(board.get("adc_rms")),
            "adc_mean": _finite_list(board.get("adc_mean")),
            "adc_power": _finite_list(board.get("adc_power")),
            "adc_gain": None,  # no getter exists in casm_f (survey 2026-09-08)
            "errors": errors,
            "timings": board.get("timings") or {},
            "elapsed_s": board.get("elapsed_s"),
            "reason": reason,
            "n_chans": int(board.get("n_chans") or N_CHANS),
        }

        shard_id = None
        if spectra is not None and np.isfinite(np.asarray(spectra)).any():
            meta = {k: v for k, v in summary.items() if k not in ("adc_power",)}
            meta["eq_coeffs"] = np.round(
                np.nan_to_num(np.asarray(eq_coeffs, dtype=np.float64), nan=float("nan")), 6
            ).tolist() if eq_coeffs is not None else None
            row = shards.write(
                SHARD_STREAM,
                np.asarray(spectra, dtype=np.float32),
                t0=ts,
                meta=meta,
            )
            shard_id = int(row["id"])
        summary["shard_id"] = shard_id

        _write_input_scalars(store, ip, cfg, layout, board, ts)
        _emit_events(store, ip, summary, previous=_latest_summary(store, ip), ts=ts)

        store.execute(
            "INSERT INTO snap_read_latest (ip, ts, shard_id, summary) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(ip) DO UPDATE SET ts = excluded.ts, shard_id = excluded.shard_id, "
            "summary = excluded.summary",
            (ip, ts, shard_id, json.dumps(summary, default=str)),
        )
        out[ip] = {
            "shard_id": shard_id,
            "programmed": programmed,
            "eq_epoch": epoch,
            "elapsed_s": summary["elapsed_s"],
            "n_errors": len(errors),
            "pps_ok": summary["pps"]["ok"],
        }

    store.set_watermark(LOCK_STREAM, LAST_READ_KEY, time.time())
    return out


def _finite_list(arr: np.ndarray | None) -> list[float | None] | None:
    """numpy -> JSON: NaN becomes null so the API never emits bare NaN."""
    if arr is None:
        return None
    values = np.asarray(arr, dtype=np.float64).ravel().tolist()
    return [None if not np.isfinite(v) else round(float(v), 6) for v in values]


def _write_input_scalars(
    store: Store,
    ip: str,
    cfg: Board | None,
    layout: dict[tuple[int, int], dict[str, Any]],
    board: dict[str, Any],
    ts: float,
) -> None:
    """Per-input ADC RMS and band power, tagged with the correlator input."""
    rms = board.get("adc_rms")
    spectra = board.get("spectra")
    if rms is None and spectra is None:
        return
    rows: list[tuple[str, float | int | str | None, dict[str, Any] | None]] = []
    for adc in range(N_INPUTS):
        tags = input_tags(layout, None if cfg is None else cfg.feng_id, adc)
        tags["ip"] = ip
        if rms is not None and adc < len(rms) and np.isfinite(rms[adc]):
            rows.append((f"snap.{ip}.adc_rms", round(float(rms[adc]), 4), dict(tags)))
        if spectra is not None and adc < len(spectra):
            power_db = band_power_db(spectra[adc])
            if power_db is not None:
                rows.append((f"snap.{ip}.band_power_db", power_db, dict(tags)))
    store.put_scalars(rows, ts=ts)


def _emit_events(
    store: Store,
    ip: str,
    summary: dict[str, Any],
    *,
    previous: dict[str, Any] | None,
    ts: float,
) -> None:
    """eq_changed / feng_id_mismatch / board_unprogrammed, on change only."""
    epoch = summary.get("eq_epoch")
    prev_epoch = None if previous is None else previous.get("eq_epoch")
    if epoch and prev_epoch and epoch != prev_epoch:
        store.add_event(
            "eq_changed",
            severity="warn",
            subject=f"snap {ip}",
            detail={
                "ip": ip,
                "from": prev_epoch,
                "to": epoch,
                "fft_shift": summary.get("fft_shift"),
            },
            ts=ts,
        )

    hw = summary.get("feng_ids_hw")
    cfg_id = summary.get("feng_id_cfg")
    if hw is not None and cfg_id is not None and list(hw) != [int(cfg_id)]:
        prev_hw = None if previous is None else previous.get("feng_ids_hw")
        if prev_hw is None or list(prev_hw) != list(hw):
            store.add_event(
                "feng_id_mismatch",
                severity="error",
                subject=f"snap {ip}",
                detail={"ip": ip, "feng_ids_hw": list(hw), "feng_id_cfg": int(cfg_id)},
                ts=ts,
            )

    programmed = summary.get("programmed")
    prev_programmed = None if previous is None else previous.get("programmed")
    if programmed is False and prev_programmed is not False:
        store.add_event(
            "board_unprogrammed",
            severity="warn",
            subject=f"snap {ip}",
            detail={"ip": ip, "errors": summary.get("errors")},
            ts=ts,
        )


# -- the lease renewer --------------------------------------------------
class LeaseRenewer:
    """Keeps the read lease alive while the ssh runs, and kills it if it is lost.

    A plain fixed lease is not enough: a seven-board pass plus ingestion can
    outlive ``LOCK_TTL_S``, after which another job would legitimately acquire
    the lock while this one is still contacting hardware. So the holder renews
    every ``interval_s`` in this thread; the moment a renewal returns False
    (the row is somebody else's now, or gone) the ssh **process group** is
    killed, so the boards stop being touched before the new owner starts.

    ``renew`` and ``kill`` are injected so the thread can be tested without a
    store or a real ssh.
    """

    def __init__(
        self,
        renew: Callable[[], bool],
        kill: Callable[[], None],
        *,
        interval_s: float = LOCK_RENEW_S,
    ) -> None:
        self._renew = renew
        self._kill = kill
        self._interval_s = float(interval_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = False
        self.renewals = 0

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                ok = bool(self._renew())
            except Exception as exc:  # a store error is treated as a loss
                log.warning("snap_read: lease renewal raised: %s", exc)
                ok = False
            if ok:
                self.renewals += 1
                continue
            self.lost = True
            log.error("snap_read: lease lost; killing the ssh process group")
            try:
                self._kill()
            except Exception:  # pragma: no cover - the process is already gone
                log.debug("kill after lease loss failed", exc_info=True)
            return

    def __enter__(self) -> "LeaseRenewer":
        self._thread = threading.Thread(target=self._run, name="snap_read-lease", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


def kill_process_group(proc: Any) -> None:
    """SIGKILL the ssh's whole process group (it is started in its own session)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:  # pragma: no cover - already reaped
            pass


# -- the remote call ----------------------------------------------------
def ssh_command(settings: Settings, ips: Sequence[str]) -> list[str]:
    """``ssh zapdos python3 -`` with the board list as the script's argv."""
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        settings.zapdos_ssh,
        "python3",
        "-",
        f"--budget={settings.snap_per_board_timeout_s:g}",
        *ips,
    ]


def read_boards_remote(
    settings: Settings,
    ips: Sequence[str],
    *,
    lease_renew: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run the one ssh session and parse its npz (raises on failure).

    The child is started in its own session (``start_new_session=True``) so the
    lease renewer can kill the whole process group — ssh plus the remote
    python — the instant our ownership of the read lock is lost.
    """
    script = REMOTE_SCRIPT.read_bytes()
    cmd = ssh_command(settings, ips)
    timeout = float(settings.snap_per_board_timeout_s) * len(ips) + 90.0
    print(f"snap_read: {' '.join(cmd)} (timeout {timeout:.0f} s)", flush=True)
    started = time.time()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    renewer: LeaseRenewer | None = None
    try:
        if lease_renew is not None:
            renewer = LeaseRenewer(lease_renew, lambda: kill_process_group(proc))
            renewer.__enter__()
        try:
            stdout, stderr_bytes = proc.communicate(input=script, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_group(proc)
            stdout, stderr_bytes = proc.communicate()
            raise RuntimeError(
                f"ssh read timed out after {timeout:.0f}s: "
                f"{stderr_bytes.decode('utf-8', 'replace').strip()[:500]}"
            )
    finally:
        if renewer is not None:
            renewer.__exit__(None, None, None)
    elapsed = time.time() - started
    stderr = stderr_bytes.decode("utf-8", "replace")
    if stderr:
        print(stderr.strip(), flush=True)
    if renewer is not None and renewer.lost:
        raise RuntimeError(
            "snap_read lost its lock lease during the read; the ssh was killed "
            "and nothing is ingested"
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ssh read failed rc={proc.returncode} after {elapsed:.1f}s: {stderr.strip()[:500]}"
        )
    parsed = parse_npz(stdout, requested_ips=ips)
    parsed["meta"]["ssh_elapsed_s"] = round(elapsed, 3)
    parsed["meta"]["npz_bytes"] = len(stdout)
    return parsed


def _note_zapdos_contact(store: Store, ok: bool, detail: str | None = None) -> None:
    """This read *is* the hourly zapdos contact: keep the liveness scalars fed.

    :func:`casm_monitor.collectors.services.ZapdosCollector` claims the same
    ``zapdos/last_probe_ts`` slot, so when the board read has taken the hour its
    ``ssh true`` probe skips — and would otherwise leave ``services.zapdos_ok``
    stale, which is why the job stamps it here.
    """
    now = time.time()
    previous = store.latest_scalar("services.zapdos_ok")
    value = 1 if ok else 0
    if previous is not None and int(float(previous["value"])) != value:
        store.add_event(
            "service_state",
            severity="warn",
            subject="zapdos ssh",
            detail={"name": "services.zapdos_ok", "from": previous["value"], "to": value,
                    "source": "snap_read", "error": detail},
        )
    store.put_scalar("services.zapdos_ok", value)
    if ok:
        store.set_watermark("zapdos", "last_ok_ts", now)
    last_ok = store.get_watermark("zapdos", "last_ok_ts")
    if last_ok is not None:
        store.put_scalar("services.zapdos_last_ok_age_s", round(now - float(last_ok), 1))
    store.put_scalar("services.zapdos_age_s", 0.0)
    store.set_watermark("zapdos", "last_probe_ts", now)


def run(params: dict[str, Any]) -> dict[str, Any]:
    """Job entry point (runs in the job subprocess, see jobs/run_job.py)."""
    settings = load_settings(params.get("config"))
    reason = str(params.get("reason") or "scheduled")
    requested = params.get("ips")
    ips: list[str]
    if requested:
        known = {b.ip for b in all_boards(settings)}
        ips = [str(ip) for ip in requested]
        unknown = [ip for ip in ips if ip not in known]
        if unknown:
            raise ValueError(f"unknown board ip(s) {unknown}; configured: {sorted(known)}")
    else:
        ips = [b.ip for b in all_boards(settings)]
    if not ips:
        raise ValueError("no SNAP boards configured (snap.antenna_boards / relay_boards)")

    holder = f"{socket.gethostname()}:{os.getpid()}"
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        token = acquire_lock(store, holder)
        if token is None:
            current = lock_holder(store)
            print(f"snap_read: lock held by {current}; not reading", flush=True)
            return {"status": "locked", "lock": current, "ips": ips, "reason": reason}
        try:
            started = time.time()
            try:
                parsed = read_boards_remote(
                    settings, ips, lease_renew=lambda: renew_lock(store, token)
                )
            except Exception as exc:
                _note_zapdos_contact(store, False, str(exc)[:200])
                raise
            _note_zapdos_contact(store, True)
            summary = ingest(store, settings, parsed, reason=reason)
            from .snap_timing import collect as collect_timing
            timing = collect_timing(settings, store, ips, token)
            elapsed = round(time.time() - started, 3)
            store.add_event(
                "snap_read",
                severity="info",
                subject=f"{len(ips)} board(s)",
                detail={"reason": reason, "ips": ips, "elapsed_s": elapsed,
                        "boards": summary, "pps_timing": timing},
            )
            print(json.dumps({"boards": summary, "elapsed_s": elapsed}, indent=1), flush=True)
            return {
                "status": "ok",
                "reason": reason,
                "ips": ips,
                "elapsed_s": elapsed,
                "ssh_elapsed_s": parsed["meta"].get("ssh_elapsed_s"),
                "npz_bytes": parsed["meta"].get("npz_bytes"),
                "boards": summary,
                "pps_timing": timing,
            }
        finally:
            release_lock(store, token)
    finally:
        store.close()
