"""Board-read routes of the SNAPs tab (``/api/snaps/board-read``).

Two routes, one read and one write, per ``docs/api-snaps.md``:

* ``GET`` serves the *last* read out of the store. It never contacts zapdos and
  never blocks on hardware: if a board has never been read it answers
  ``ts: null`` rather than 404, so the card renders a "never read" state.
* ``POST`` submits a ``snap_read`` job and is the only write. Checking the
  limits, stamping the manual timestamp and inserting the job are ONE
  transaction (:meth:`casm_monitor.store.Store.submit_job_atomic`): with them
  as three separate steps two clicks landing together both passed the checks
  and both enqueued a read, which the one-reader-at-a-time rule forbids. It
  refuses with 429 when a read is already in flight (the persisted lease, or a
  queued/running job) or when the last manual request was less than
  ``max(snap.manual_min_interval_s, 300)`` s ago; the refusal carries
  ``retry_after_s`` so the button can count down. A manual read also *claims*
  the shared hourly zapdos slot when it is free, so the scheduler does not add
  a second contact in the same hour. Requested ips are de-duplicated and must
  all be configured boards (unknown ip -> 400).

Wiring: the router is created by :func:`build_router` with the app's read-only
handle and its single write handle, so this module opens no database of its own.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from ..collectors.services import zapdos_interval_s
from ..config import Settings
from ..jobs.snap_read import (
    LAST_MANUAL_KEY,
    LOCK_KEY,
    LOCK_STREAM,
    latest_reads,
    lock_holder,
)
from ..snapmap import all_boards
from ..store import ShardReader, Store
from ..util import iso

SNAP_READ_KIND = "snap_read"
# Floor on the manual rate limit: the config may make the gap between two
# operator-triggered board reads longer, never shorter (plan.md: "minimum 5 min
# between manual reads").
MANUAL_MIN_INTERVAL_FLOOR_S = 300.0
# Descending native channel centres. 375 MHz is the edge, not the last centre.
FREQ_MHZ = 500.0 - np.arange(4096) * (125.0 / 4096)


def freq_mhz(n_chans: int = 4096) -> list[float]:
    """Channel centres: 500 - k * 125/n MHz, descending, rounded to 1 Hz."""
    if n_chans == FREQ_MHZ.size:
        axis = FREQ_MHZ
    else:
        axis = 500.0 - np.arange(int(n_chans)) * (125.0 / int(n_chans))
    return [round(float(f), 6) for f in axis]


def _pending_job(store: Store) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT id, state FROM jobs WHERE kind = ? AND state IN ('queued', 'running') "
        "ORDER BY id DESC LIMIT 1",
        (SNAP_READ_KIND,),
    )
    return {"id": int(rows[0]["id"]), "state": str(rows[0]["state"])} if rows else None


def manual_min_interval_s(settings: Settings) -> float:
    """Effective manual rate limit: the config can only make it longer."""
    return max(float(settings.snap_manual_min_interval_s), MANUAL_MIN_INTERVAL_FLOOR_S)


def normalise_ips(body: dict[str, Any] | None, settings: Settings) -> list[str] | None:
    """Validated, de-duplicated board list from the request body (None = all).

    Duplicates would read one board twice in the same pass, and an ip that is
    not a configured board is a client error, not something to hand to the job.
    """
    ips = (body or {}).get("ips")
    if ips is None:
        return None
    if not isinstance(ips, list) or not all(isinstance(x, str) for x in ips):
        raise HTTPException(status_code=400, detail="ips must be a list of strings or null")
    known = {b.ip for b in all_boards(settings)}
    unknown = [ip for ip in ips if ip not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown board ip(s): {sorted(set(unknown))}")
    seen: list[str] = []
    for ip in ips:
        if ip not in seen:
            seen.append(ip)
    if not seen:
        raise HTTPException(status_code=400, detail="ips must not be an empty list")
    return seen


def refusal_body(refusal: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """The 429 body (``detail`` + ``retry_after_s``) for a store refusal."""
    reason = refusal.get("reason")
    if reason == "locked":
        detail = f"a board read is already running (holder {refusal.get('holder')})"
    elif reason == "pending":
        detail = f"board read job {refusal.get('job_id')} is {refusal.get('state')}"
    elif reason == "rate_limited":
        detail = (
            f"manual reads are limited to one per {manual_min_interval_s(settings):.0f} s "
            f"(last one {float(refusal.get('age_s') or 0.0):.0f} s ago)"
        )
    else:  # pragma: no cover - no other reason can reach the manual path
        detail = f"board read refused ({reason})"
    return {"detail": detail, "retry_after_s": int(refusal.get("retry_after_s") or 1)}


def manual_refusal(
    reader: Store, settings: Settings, *, now: float | None = None
) -> dict[str, Any] | None:
    """Why a manual read would be refused right now, or None if it may run.

    Advisory only (a read-only handle is enough): the POST route does not act
    on this, it calls ``Store.submit_job_atomic`` which re-checks the very same
    conditions inside the transaction that inserts the job. This function
    exists for status/diagnostic callers and for the tests.
    """
    t = time.time() if now is None else now
    holder = lock_holder(reader, now=t)
    if holder is not None:
        expires = float(holder.get("expires", t))
        return {
            "detail": f"a board read is already running (holder {holder.get('holder')})",
            "retry_after_s": max(1, int(round(expires - t))),
        }
    pending = _pending_job(reader)
    if pending is not None:
        return {
            "detail": f"board read job {pending['id']} is {pending['state']}",
            "retry_after_s": 10,
        }
    last_manual = reader.get_watermark(LOCK_STREAM, LAST_MANUAL_KEY)
    if last_manual is not None:
        min_interval = manual_min_interval_s(settings)
        age = t - float(last_manual)
        if age < min_interval:
            return {
                "detail": (
                    f"manual reads are limited to one per {min_interval:.0f} s "
                    f"(last one {age:.0f} s ago)"
                ),
                "retry_after_s": max(1, int(round(min_interval - age))),
            }
    return None


def board_read_payload(
    reader: Store,
    settings: Settings,
    ip: str,
    *,
    shards: ShardReader | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """The ``GET /api/snaps/board-read`` body for one board."""
    t = time.time() if now is None else now
    board = next((b for b in all_boards(settings) if b.ip == ip), None)
    if board is None:
        raise HTTPException(status_code=404, detail=f"unknown board ip {ip!r}")

    summary = latest_reads(reader).get(ip)
    if summary is None:
        return {
            "ts": None,
            "age_s": None,
            "freq_mhz": None,
            "spectra": None,
            "adc_rms": None,
            "adc_mean": None,
            "adc_gain": None,
            "eq_epoch": None,
            "feng_id_hw": None,
            "feng_id_cfg": board.feng_id,
            "pps": {"ok": False, "period": None, "detail": "never read"},
            "programmed": None,
        }

    ts = float(summary.get("ts") or 0.0)
    spectra: list[list[float | None]] | None = None
    shard_id = summary.get("shard_id")
    if shard_id is not None:
        try:
            array, _meta = (shards or ShardReader(reader)).load(int(shard_id))
            spectra = [
                [None if not np.isfinite(v) else float(v) for v in row]
                for row in np.asarray(array, dtype=np.float64)
            ]
        except Exception:
            # A retired or unreadable shard is a missing layer, not a 500.
            spectra = None

    n_chans = int(summary.get("n_chans") or FREQ_MHZ.size)
    return {
        "ts": iso(ts),
        "age_s": round(t - ts, 1),
        "freq_mhz": freq_mhz(n_chans) if spectra is not None else None,
        "spectra": spectra,
        "adc_rms": summary.get("adc_rms"),
        "adc_mean": summary.get("adc_mean"),
        # No coarse-gain getter exists in casm_f (survey 2026-09-08).
        "adc_gain": summary.get("adc_gain"),
        "eq_epoch": summary.get("eq_epoch"),
        "feng_id_hw": summary.get("feng_id_hw"),
        "feng_id_cfg": board.feng_id if board.feng_id is not None else summary.get("feng_id_cfg"),
        "pps": summary.get("pps") or {"ok": False, "period": None, "detail": "no sync data"},
        "programmed": summary.get("programmed"),
    }


def build_router(reader: Store, writer: Store, settings: Settings) -> APIRouter:
    """Router over the app's existing handles (reader for GET, writer for POST)."""
    router = APIRouter(prefix="/api/snaps", tags=["snaps"])
    shard_reader = ShardReader(reader)

    @router.get("/board-read")
    def get_board_read(ip: str = Query(..., description="board IP")) -> dict[str, Any]:
        return board_read_payload(reader, settings, ip, shards=shard_reader)

    @router.post("/board-read")
    def post_board_read(body: dict[str, Any] | None = None) -> Any:
        ips = normalise_ips(body, settings)
        params = {"ips": ips, "reason": "manual"}
        now = time.time()
        # ONE transaction: the limit checks, the manual timestamp, the claim on
        # the hourly zapdos slot and the INSERT. Two clicks racing here cannot
        # both come out with a job.
        job_id, refusal = writer.submit_job_atomic(
            SNAP_READ_KIND,
            params,
            refuse_if_pending=True,
            lease=(LOCK_STREAM, LOCK_KEY),
            rate_limit=(LOCK_STREAM, LAST_MANUAL_KEY, manual_min_interval_s(settings)),
            # A manual read IS the hour's zapdos contact when the slot is free;
            # taking it here stops the scheduler adding a second one. It is not
            # a precondition: an operator may click within an hour of a
            # scheduled read (the manual rate limit is what bounds that).
            claim_slot=("zapdos", "last_probe_ts", zapdos_interval_s(settings)),
            require_slot=False,
            stamp=[(LOCK_STREAM, LAST_MANUAL_KEY, now)],
            now=now,
        )
        if refusal is not None:
            return JSONResponse(status_code=429, content=refusal_body(refusal, settings))
        writer.add_event(
            "job_submitted",
            severity="info",
            subject=f"job {job_id} ({SNAP_READ_KIND})",
            detail={"job_id": job_id, "kind": SNAP_READ_KIND, "params": params},
        )
        return {"job_id": job_id}

    return router
