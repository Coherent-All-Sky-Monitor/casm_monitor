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
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .. import __version__
from ..config import Settings, load_settings
from ..store import Store
from ..util import iso, parse_iso
from .status import build_status, collect_age_s

log = logging.getLogger("casm_monitor.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
WS_PUSH_INTERVAL_S = 10.0

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

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        reader.close()
        writer.close()

    app = FastAPI(title="casm_monitor", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.reader = reader
    app.state.writer = writer

    def status_payload() -> dict[str, Any]:
        return build_status(reader.latest_scalars(), settings.cadences)

    # -- health / status ------------------------------------------------
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "collect_age_s": collect_age_s(reader.heartbeats()),
            "version": __version__,
        }

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return status_payload()

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
            since=parse_iso(since), kind=kind, severity=severity, limit=max(1, min(limit, 5000))
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
            t0=parse_iso(t0),
            t1=parse_iso(t1),
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
        try:
            while True:
                await websocket.send_json(status_payload())
                await asyncio.sleep(WS_PUSH_INTERVAL_S)
        except WebSocketDisconnect:
            return
        except (RuntimeError, ConnectionError):  # pragma: no cover - client gone
            return

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
