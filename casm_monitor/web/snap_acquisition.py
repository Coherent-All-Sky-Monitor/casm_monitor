"""Narrow submission path to the existing production SNAP diagnostic queue.

No hardware reader or worker is introduced here. GET reads stored evidence;
only the human-confirmed POST can enqueue the fixed ``snap_read`` job kind.
"""
from __future__ import annotations

import json
import os
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ..collectors.snapread import snap_read_interval_s
from ..jobs.snap_read import LAST_READ_KEY, LOCK_STREAM, latest_reads
from ..snapmap import all_boards
from ..store import Store
from ..util import iso
from .snapread import manual_min_interval_s, manual_refusal, normalise_ips, submit_manual_read


class Acquire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ips: list[str] | None = None
    confirm: bool = False


def submit_existing(settings, ips):
    """Enqueue only after origin/CSRF/confirmation checks; reuse the hourly worker.

    The rest of the workspace keeps its read-only production handle. This
    short-lived handle is exclusively for the atomic, cooldown-limited SNAP
    submission. It replaces the retired 8060 HTTP bridge, not its worker.
    """
    writer = Store(settings.db_path, store_root=settings.store_root)
    try:
        response = submit_manual_read(writer, settings, {"ips": ips})
        if isinstance(response, JSONResponse):
            return response.status_code, json.loads(response.body)
        return 200, response
    finally:
        writer.close()


def build_router(settings, reader, *, submit=None):
    router = APIRouter(prefix="/api/snap-workspace", tags=["snap-acquisition"])
    workspace = os.environ.get("CASM_MONITOR_WORKSPACE") == "1"
    token = secrets.token_urlsafe(32)
    submit = submit or (lambda ips: submit_existing(settings, ips))

    @router.get("/acquisition")
    def status():
        now = time.time()
        last = reader.get_watermark(LOCK_STREAM, LAST_READ_KEY)
        last = float(last) if last is not None else None
        interval = snap_read_interval_s(settings)
        summaries = latest_reads(reader)
        jobs = [dict(row) for row in reader.query(
            "SELECT id,state,created,started,finished FROM jobs WHERE kind='snap_read' ORDER BY id DESC LIMIT 1")]
        boards = []
        for board in all_boards(settings):
            summary = summaries.get(board.ip, {})
            ts = summary.get("ts")
            boards.append({"ip": board.ip, "ts": iso(ts) if ts else None,
                           "age_s": max(0, now - float(ts)) if ts else None,
                           "has_saved_spectra": summary.get("shard_id") is not None})
        return {"configured_interval_s": interval, "latest_read_utc": iso(last) if last else None,
                "age_s": max(0, now - last) if last else None,
                "due": last is None or now - last >= interval,
                "cadence_verified": False, "boards": boards, "latest_job": jobs[0] if jobs else None,
                "manual_refusal": manual_refusal(reader, settings, now=now),
                "manual_min_interval_s": manual_min_interval_s(settings),
                "csrf_token": token, "manual_enabled": workspace and settings.observation_cache_root is not None,
                "note": "Configured cadence is not proof of recent spectra. The existing job worker owns acquisition; the web service adds no scheduler.",
                "operation": "Existing bounded diagnostic acquisition: autocorrelator mux/readout control plus spectra/status reads; no EQ, programming, PPS sync or observation-configuration change."}

    @router.post("/acquire")
    def acquire(body: Acquire, request: Request):
        origin = f"{request.url.scheme}://{request.url.netloc}"
        if (not workspace or settings.observation_cache_root is None or
            request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"} or
            request.headers.get("origin") != origin or request.headers.get("x-casm-workspace") != "1" or
            not secrets.compare_digest(request.headers.get("x-casm-snap-csrf", ""), token)):
            raise HTTPException(403, "Workspace, same-origin and SNAP acquisition CSRF protection required")
        if not body.confirm:
            raise HTTPException(400, "Confirm the bounded diagnostic acquisition")
        ips = normalise_ips({"ips": body.ips}, settings)
        code, receipt = submit(ips)
        return JSONResponse(status_code=code, content=receipt)

    return router
