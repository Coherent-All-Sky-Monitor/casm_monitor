"""Human-requested investigation records; no task runner or operational actions."""
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .injection_overview import build_injection_overview


class NewReview(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=8000)
    selection: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    plot_url: str | None = Field(default=None, max_length=4096)


def build_router(settings) -> APIRouter:
    router = APIRouter(prefix="/api/review", tags=["review"])
    token = secrets.token_urlsafe(32)
    lock = threading.Lock()
    workspace = os.environ.get("CASM_MONITOR_WORKSPACE") == "1"
    # Explicit preview root is mandatory. Never fall back to production store.
    root = Path(settings.observation_cache_root).resolve() if settings.observation_cache_root else None

    def database(create=False):
        if root is None:
            raise HTTPException(403, "Investigation storage requires an explicit preview artifact root")
        path = root / "investigations.sqlite"
        if path.is_symlink():
            raise HTTPException(403, "Investigation database symlinks are forbidden")
        if not create and not path.exists():
            return None
        if create:
            root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path if create else path.as_uri() + "?mode=ro", uri=not create, timeout=2)
        conn.row_factory = sqlite3.Row
        if create:
            conn.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS plots (id TEXT PRIMARY KEY, png BLOB NOT NULL)")
        return conn

    def saved(limit=2000, offset=0):
        conn = database()
        if conn is None:
            return [], 0
        try:
            count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            items = [json.loads(r[0]) for r in conn.execute(
                "SELECT body FROM records ORDER BY rowid DESC LIMIT ? OFFSET ?", (limit, offset))]
            return items, count
        finally:
            conn.close()

    def find_saved(record_id):
        conn = database()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT body FROM records WHERE id=?", (record_id,)).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            conn.close()

    def persist(item, plot=None):
        raw = json.dumps(item, allow_nan=False)
        if len(raw.encode()) > 65536:
            raise HTTPException(413, "Investigation evidence exceeds 64 KiB")
        conn = database(create=True)
        try:
            with conn:
                conn.execute("INSERT INTO records(id,body) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (item["id"], raw))
                if plot is not None:
                    conn.execute("INSERT INTO plots(id,png) VALUES (?,?)", (item["id"], plot))
        finally:
            conn.close()

    def guard(request):
        if not workspace or root is None:
            raise HTTPException(403, "Explicit local workspace mode and artifact root required")
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if origin != expected or not secrets.compare_digest(request.headers.get("x-casm-review-csrf", ""), token):
            raise HTTPException(403, "Same-origin request and review CSRF token required")

    def virtual():
        evidence = build_injection_overview(settings)
        items = [{"id": "injection-" + shot["file_id"], "title": shot["file_id"] + ": " + shot["outcome"],
                  "state": "queued", "kind": "injection_miss", "created_utc": shot["inject_utc"],
                  "note": "", "selection": {"injection_id": shot["file_id"]},
                  "provenance": {"source": evidence["source"], "recorded_evidence": shot,
                                 "inferred_cause": None}, "virtual": True}
                 for shot in evidence["misses"]]
        return items, evidence

    def mirror_misses(items):
        """Retain first-seen misses without resetting a human-requested record.

        INSERT OR IGNORE is atomic across connections/processes. The local
        lock also serializes this batch with this router's explicit requests.
        No empty database is created for unavailable/empty source evidence.
        """
        if not workspace or root is None or not items:
            return
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for item in items:
            record = {**item, "virtual": False, "first_seen_utc": now,
                      "retention_note": "First-seen recorded miss retained until human review; no automatic investigation."}
            raw = json.dumps(record, allow_nan=False)
            if len(raw.encode()) > 65536:
                raise HTTPException(413, "Injection evidence exceeds 64 KiB record budget")
            rows.append((record["id"], raw))
        with lock:
            conn = database(create=True)
            try:
                with conn:
                    conn.executemany("INSERT OR IGNORE INTO records(id,body) VALUES (?,?)", rows)
            finally:
                conn.close()

    def snapshot(url):
        if root is None:
            raise HTTPException(403, "Preview artifact root required")
        parsed = urlsplit(url)
        science = re.fullmatch(r"/api/science/products/([0-9a-f]{32})/(plot-[0-5]\.png)", parsed.path)
        t1 = re.fullmatch(r"/api/t1/products/([0-9a-f]{64}\.png)", parsed.path)
        snap = re.fullmatch(r"/api/snap-workspace/([0-9a-f]{24})/spectrum\.png", parsed.path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise HTTPException(400, "Save an immutable local science/T1 product URL")
        if science:
            path = root / "science" / science[1] / science[2]
        elif t1:
            path = root / "t1_products" / t1[1]
        elif snap:
            path = root / "snap_views" / snap[1] / "spectrum.png"
        else:
            raise HTTPException(400, "Save an immutable local science/T1 product URL")
        path = path.resolve()
        if not path.is_relative_to(root):
            raise HTTPException(403, "Plot path escapes preview artifact root")
        try:
            with path.open("rb") as handle:
                data = handle.read(20 * 1024 * 1024 + 1)
        except OSError:
            raise HTTPException(404, "Rendered plot unavailable; render it before saving")
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(413, "Plot exceeds 20 MiB snapshot budget")
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(400, "Expected a PNG scientific product")
        return data

    @router.get("")
    def overview(limit: int = Query(default=2000, ge=1, le=2000), offset: int = Query(default=0, ge=0)):
        items, evidence = virtual()
        mirror_misses(items)
        persisted, total = saved(limit, offset) if root else ([], 0)
        known = {x["id"] for x in persisted}
        items = persisted if workspace and root else persisted + [x for x in items if x["id"] not in known]
        return {"items": items, "csrf_token": token, "source_status": evidence["status"],
                "injection_counts_complete": evidence["counts_complete"],
                "injection_window_start_utc": evidence["trend_start_utc"],
                "saved_limit": limit, "saved_offset": offset, "saved_total": total,
                "saved_truncated": offset + len(persisted) < total,
                "next_offset": offset + len(persisted) if offset + len(persisted) < total else None,
                "writes_enabled": workspace and root is not None,
                "mirroring_enabled": workspace and root is not None,
                "policy": "Workspace refresh retains newly seen misses in the local queue. Only an explicit human request changes state to requested. No runner or Slack integration is connected."}

    @router.post("", status_code=201)
    def create(body: NewReview, request: Request):
        guard(request)
        plot = snapshot(body.plot_url) if body.plot_url else None
        item = {**body.model_dump(), "id": secrets.token_hex(12), "state": "queued", "kind": "human_mark",
                "created_utc": datetime.now(timezone.utc).isoformat(), "virtual": False,
                "provenance_note": "Human-supplied selection/provenance; not independently verified by saving."}
        if plot is not None:
            item["saved_plot_url"] = f"/api/review/{item['id']}/plot.png"
            item["plot_sha256"] = hashlib.sha256(plot).hexdigest()
        try:
            with lock:
                persist(item, plot=plot)
        except (ValueError, RecursionError):
            raise HTTPException(400, "Evidence must contain finite JSON values")
        return item

    @router.get("/{record_id}/plot.png")
    def saved_plot(record_id: str):
        conn = database()
        if conn is None:
            raise HTTPException(404, "Saved plot not found")
        try:
            row = conn.execute("SELECT png FROM plots WHERE id=?", (record_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(404, "Saved plot not found")
        return Response(row[0], media_type="image/png", headers={"Content-Disposition": 'inline; filename="investigation.png"'})

    @router.get("/{record_id}")
    def record(record_id: str):
        item = find_saved(record_id) if root else None
        if item is None:
            items, _ = virtual()
            item = next((x for x in items if x["id"] == record_id), None)
        if item is None:
            raise HTTPException(404, "Investigation not found in retained review evidence")
        return item

    @router.post("/{record_id}/request")
    def request_investigation(record_id: str, request: Request):
        guard(request)
        with lock:
            item = find_saved(record_id)
            if item is None:
                items, _ = virtual()
                item = next((x for x in items if x["id"] == record_id), None)
            if item is None:
                raise HTTPException(404, "Investigation not found in retained review evidence")
            if item["state"] != "requested":
                item.update(state="requested", requested_utc=datetime.now(timezone.utc).isoformat(), virtual=False)
                persist(item)
            return item

    return router
