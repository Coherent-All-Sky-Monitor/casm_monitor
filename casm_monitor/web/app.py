"""FastAPI app: read the store, serve the status strip, events, series and jobs.

Bound to config ``web.host``/``web.port`` (127.0.0.1:8060 — reached with
``ssh -L 8060:127.0.0.1:8060 casm-corr1``). The app opens the SQLite file
read-only for every GET; the only write handle is used exclusively by the job
submit/cancel routes and the events they append.

The SPA built by the frontend into ``casm_monitor/web/static/`` is served at
``/`` with an index.html fallback for unknown non-``/api`` paths. If that
directory does not exist yet, a placeholder page is generated in memory (no file
is ever created in the frontend's directory).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .. import __version__
from ..config import Settings, load_settings
from ..store import Store
from ..util import iso, parse_iso
from .cal import build_router as build_cal_router
from .cands import build_router as build_cands_router
from .snaps import build_router as build_snaps_router
from .snapread import build_router as build_snapread_router
from .status import build_status, collect_age_s
from .vis import build_router as build_vis_router
from .search import build_router as build_search_router
from .figures import build_router as build_figures_router

log = logging.getLogger("casm_monitor.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
WS_PUSH_INTERVAL_S = 10.0
# How long a computed status payload is reused across concurrent callers
# (many WS clients + polling fallbacks landing in the same tick should not
# each hit SQLite on the event loop thread).
STATUS_CACHE_TTL_S = 1.0


class StatusCache:
    """Coalesces concurrent status computations within a short TTL window.

    ``compute`` runs in a threadpool (it does blocking SQLite I/O); the
    result is shared by every caller that arrives within ``ttl_s`` of the
    last computation instead of each triggering its own DB round trip.
    """

    def __init__(self, compute: Any, ttl_s: float = STATUS_CACHE_TTL_S) -> None:
        self._compute = compute
        self._ttl_s = ttl_s
        self._lock = asyncio.Lock()
        self._value: dict[str, Any] | None = None
        self._computed_at = 0.0

    async def get(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._value is not None and (now - self._computed_at) < self._ttl_s:
            return self._value
        async with self._lock:
            now = time.monotonic()
            if self._value is not None and (now - self._computed_at) < self._ttl_s:
                return self._value
            value = await run_in_threadpool(self._compute)
            self._value = value
            self._computed_at = now
            return value


PLACEHOLDER_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>casm_monitor</title>
<style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:44rem}
code{background:#eee;padding:0 .2rem}</style></head>
<body>
<h1>casm_monitor</h1>
<p>The backend is running. The SPA bundle has not been built into
<code>casm_monitor/web/static/</code> yet.</p>
<p>API: <a href="/api/health">/api/health</a> &middot;
<a href="/api/status">/api/status</a> &middot;
<a href="/api/events">/api/events</a> &middot;
<a href="/api/jobs">/api/jobs</a></p>
</body></html>
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    # The write handle also ensures the schema exists so a fresh store can be
    # served before the collector's first pass.
    writer = Store(settings.db_path, store_root=settings.store_root)
    reader = Store(settings.db_path, read_only=True, store_root=settings.store_root)

    ws_tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        # Cancel outstanding WS handlers before tearing down the DB handles
        # they read/write through, otherwise a handler still mid-tick can
        # touch a closed connection.
        for task in list(ws_tasks):
            task.cancel()
        for task in list(ws_tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        reader.close()
        writer.close()

    app = FastAPI(title="casm_monitor", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.reader = reader
    app.state.writer = writer
    app.state.ws_tasks = ws_tasks

    # SNAPs tab (M1): boards, live Kafka bandpass, history, trends. Read-only
    # handle; snaps.py's 501 board-read stubs auto-disable once this module is
    # importable (see its module docstring), so the real board-read router
    # below is the only one ever mounted.
    app.include_router(build_snaps_router(settings, reader))
    app.include_router(build_snapread_router(reader, writer, settings))

    # Visibilities tab (M2): the wired-input sub-matrix the vis collector caches
    # (spectra, matrices, waterfalls, coherence). Read-only handle.
    app.include_router(build_vis_router(settings, reader))

    # Calibration tab (M3): defaults, builds, the staged dry run and the gated
    # upload. Read handle for the GETs, the app's single write handle for the
    # three POSTs that insert a job row.
    app.include_router(build_cal_router(reader, writer, settings))
    app.include_router(build_search_router(reader, settings))
    app.include_router(build_figures_router(settings))

    # Candidates tab (M5): a prefix-aware router over casm_t3's own T2 event
    # store (mount only — see casm_monitor.web.cands module docstring for why
    # t3-web's app itself is never mounted). No reader/writer handle: it opens
    # the T2 sqlite directly through casm_t3's own helpers.
    app.include_router(build_cands_router(settings))

    def status_payload() -> dict[str, Any]:
        return build_status(reader.latest_scalars(), settings.cadences)

    status_cache = StatusCache(status_payload)

    # -- health / status ------------------------------------------------
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "collect_age_s": collect_age_s(reader.heartbeats()),
            "version": __version__,
        }

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return await status_cache.get()

    def require_parsed_iso(field: str, text: str | None) -> float | None:
        """parse_iso, but a non-empty unparseable value is a client error
        (400) rather than silently falling back to "no filter" / "all
        rows"."""
        if not text:
            return None
        parsed = parse_iso(text)
        if parsed is None:
            raise HTTPException(status_code=400, detail=f"{field} is not a valid ISO-8601 timestamp")
        return parsed

    # -- events ---------------------------------------------------------
    @app.get("/api/events")
    def events(
        since: str | None = None,
        kind: str | None = None,
        severity: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if severity is not None and severity not in ("info", "warn", "error"):
            raise HTTPException(status_code=400, detail="severity must be info|warn|error")
        rows = reader.events(
            since=require_parsed_iso("since", since),
            kind=kind,
            severity=severity,
            limit=max(1, min(limit, 5000)),
        )
        for row in rows:
            row["ts"] = iso(row["ts"])
        return {"events": rows}

    # -- scalars --------------------------------------------------------
    @app.get("/api/scalars")
    def scalars(
        name: str,
        t0: str | None = None,
        t1: str | None = None,
        max_points: int = 2000,
    ) -> dict[str, Any]:
        times, values = reader.series(
            name,
            t0=require_parsed_iso("t0", t0),
            t1=require_parsed_iso("t1", t1),
            max_points=max(1, min(max_points, 100_000)),
        )
        return {"name": name, "t": times, "v": values}

    # -- jobs -----------------------------------------------------------
    @app.get("/api/jobs")
    def list_jobs(state: str | None = None, limit: int = 100) -> dict[str, Any]:
        return {"jobs": reader.list_jobs(state=state, limit=max(1, min(limit, 1000)))}

    @app.post("/api/jobs")
    def submit_job(body: dict[str, Any]) -> dict[str, Any]:
        from ..jobs.kinds import KINDS

        kind = str((body or {}).get("kind") or "")
        params = (body or {}).get("params") or {}
        if kind not in KINDS:
            raise HTTPException(status_code=400, detail=f"unknown kind {kind!r}")
        if KINDS[kind].privileged:
            # cal_build/deploy_stage/deploy_upload are not submittable here:
            # this route takes an arbitrary params object, which for the upload
            # was the whole gate (2026-09-09 security review, finding 1). Each
            # has a route of its own that validates the request, and the upload
            # additionally mints the single-use authorization its worker must
            # consume.
            raise HTTPException(
                status_code=403,
                detail=(
                    f"{kind!r} is a privileged job kind and cannot be submitted through "
                    f"POST /api/jobs; use its own route under /api/cal (see docs/api-cal.md)"
                ),
            )
        if not isinstance(params, dict):
            raise HTTPException(status_code=400, detail="params must be an object")
        job_id = writer.submit_job(kind, params)
        writer.add_event(
            "job_submitted",
            severity="info",
            subject=f"job {job_id} ({kind})",
            detail={"job_id": job_id, "kind": kind, "params": params},
        )
        return {"id": job_id, "kind": kind, "state": "queued"}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int) -> dict[str, Any]:
        job = reader.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        return job

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int) -> dict[str, Any]:
        state = writer.cancel_job(job_id)
        if not state:
            raise HTTPException(status_code=404, detail="no such job")
        writer.add_event(
            "job_cancel_requested",
            severity="info",
            subject=f"job {job_id}",
            detail={"job_id": job_id, "state": state},
        )
        return {"id": job_id, "state": state}

    # -- websocket ------------------------------------------------------
    @app.websocket("/ws/status")
    async def ws_status(websocket: WebSocket) -> None:
        await websocket.accept()
        task = asyncio.current_task()
        if task is not None:
            ws_tasks.add(task)
        try:
            while True:
                try:
                    payload = await status_cache.get()
                    await websocket.send_json(payload)
                except WebSocketDisconnect:
                    return
                except Exception:
                    # Client gone / socket half-closed mid-send: this is
                    # routine, not worth a traceback.
                    log.debug("ws send failed", exc_info=True)
                    return

                # Race the push interval against the client closing the
                # socket, so a disconnect is noticed immediately instead of
                # only at the next scheduled tick.
                recv_task = asyncio.ensure_future(websocket.receive())
                sleep_task = asyncio.ensure_future(asyncio.sleep(WS_PUSH_INTERVAL_S))
                done: set[asyncio.Task[Any]] = set()
                pending: set[asyncio.Task[Any]] = {recv_task, sleep_task}
                try:
                    done, pending = await asyncio.wait(
                        {recv_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for p in pending:
                        p.cancel()
                    for p in pending:
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await p

                if recv_task in done:
                    try:
                        message = recv_task.result()
                    except WebSocketDisconnect:
                        return
                    if message.get("type") == "websocket.disconnect":
                        return
                    # Any other client frame is ignored; the socket is
                    # push-only from the server's side.
        except asyncio.CancelledError:
            # Server shutdown: let lifespan's cancellation propagate.
            raise
        finally:
            if task is not None:
                ws_tasks.discard(task)

    # -- SPA ------------------------------------------------------------
    def index_response() -> Response:
        index = STATIC_DIR / "index.html"
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse(PLACEHOLDER_HTML)

    @app.get("/", include_in_schema=False, response_model=None)
    def spa_root() -> Response:
        return index_response()

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    def spa_catch_all(path: str) -> Response:
        if path.startswith("api/") or path.startswith("ws/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = (STATIC_DIR / path).resolve()
        try:
            inside = candidate.is_relative_to(STATIC_DIR.resolve())
        except AttributeError:  # pragma: no cover - py<3.9
            inside = str(candidate).startswith(str(STATIC_DIR.resolve()))
        if inside and candidate.is_file():
            return FileResponse(candidate)
        return index_response()

    return app


def main(argv: Sequence[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description="CASM monitor web service")
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default=None, help="override config web.host")
    parser.add_argument("--port", type=int, default=None, help="override config web.port")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    host = args.host or settings.web_host
    port = int(args.port or settings.web_port)
    log.info("serving casm_monitor %s on http://%s:%d (store %s)", __version__, host, port, settings.store_root)
    uvicorn.run(create_app(settings), host=host, port=port, log_level=args.log_level)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
