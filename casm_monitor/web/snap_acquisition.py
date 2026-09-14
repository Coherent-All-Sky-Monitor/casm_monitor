"""Narrow bridge to the existing production SNAP diagnostic queue.

No hardware reader or worker is introduced here. GET reads stored evidence;
only the human-confirmed POST can submit to one fixed loopback API route.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ..collectors.snapread import snap_read_interval_s
from ..jobs.snap_read import LAST_READ_KEY, LOCK_STREAM, latest_reads
from ..snapmap import all_boards
from ..util import iso
from .snapread import manual_min_interval_s, manual_refusal, normalise_ips

PRODUCTION_URL = "http://127.0.0.1:8060/api/snaps/board-read"
MAX_RESPONSE_BYTES = 65536


class Acquire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ips: list[str] | None = None
    confirm: bool = False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def submit_existing(ips):
    """One request, fixed destination, no proxy/redirect/retry or other job kind."""
    request = urllib.request.Request(PRODUCTION_URL, data=json.dumps({"ips": ips}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HTTPException(502, "Production queue receipt unknown/unavailable; inspect acquisition status before retrying. " + str(exc)) from exc
    with response:
        status = response.code
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise HTTPException(502, "Production queue returned an oversized response; inspect status before retrying")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(502, "Production queue response was not JSON; receipt unknown") from exc
    if not isinstance(body, dict) or status not in {200, 400, 403, 429}:
        raise HTTPException(502, "Production queue did not return a supported receipt; inspect status before retrying")
    if status == 200 and (type(body.get("job_id")) is not int or body["job_id"] <= 0):
        raise HTTPException(502, "Production queue returned no valid job receipt")
    return status, body


def build_router(settings, reader, *, submit=None):
    router = APIRouter(prefix="/api/snap-workspace", tags=["snap-acquisition"])
    workspace = os.environ.get("CASM_MONITOR_WORKSPACE") == "1"
    token = secrets.token_urlsafe(32)
    submit = submit or submit_existing

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
                "note": "Configured cadence is not proof of recent spectra. Existing production job worker owns acquisition; this preview adds no scheduler.",
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
