"""Independent, append-only candidate assessments and retained training evidence.

No T2/T3 writes, catalog promotion, Slack messages, or hardware calls. Self-named
reviewers are a trusted-team convenience, not authenticated identities.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .cands import saved_plots

log = logging.getLogger(__name__)
LABELS = ("frb", "pulsar", "solar", "rfi", "noise", "unsure")
MORPHOLOGY = ("narrowband", "broadband", "swept", "repeating")
NAME = re.compile(r"^\d{6}[a-zA-Z0-9]{4,6}$")
SCHEMA = """
CREATE TABLE IF NOT EXISTS reviewers (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE, created_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts (
 sha256 TEXT PRIMARY KEY, suffix TEXT NOT NULL, bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS evidence (
 id TEXT PRIMARY KEY, candidate TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS annotations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL,
 candidate TEXT NOT NULL, reviewer_id TEXT NOT NULL REFERENCES reviewers(id),
 created_utc TEXT NOT NULL, parent_id INTEGER NOT NULL, evidence_id TEXT,
 body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS annotation_pair ON annotations(candidate, reviewer_id, id DESC);
CREATE INDEX IF NOT EXISTS annotation_reviewer ON annotations(reviewer_id, id DESC);
"""


class ReviewerInput(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class Assessment(BaseModel):
    reviewer_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_id: str = Field(pattern=r"^[0-9a-f-]{32,36}$")
    expected_revision: int = Field(ge=0)
    label: Literal["frb", "pulsar", "solar", "rfi", "noise", "unsure"] | None = None
    quality: Literal["usable", "cannot_assess"] = "usable"
    morphology: list[Literal["narrowband", "broadband", "swept", "repeating"]] = Field(default_factory=list, max_length=4)
    rfi_present: bool = False
    note: str = Field(default="", max_length=2000)
    plot: str | None = Field(default=None, max_length=200)
    plot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    others_revealed: bool = False
    action: Literal["assess", "undo"] = "assess"


def annotation_root(settings) -> Path:
    root = (settings.candidate_annotations_root or settings.store_root.parent / "casm_candidate_annotations").resolve()
    # Do not put durable labels into the production store or candidate archive.
    for protected in (settings.store_root, settings.candidates_dir, settings.candidate_archive_root):
        p = protected.resolve()
        if root == p or p in root.parents or root in p.parents:
            raise ValueError("Candidate annotation storage must be separate from production data")
    return root


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def encoded(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def safe_file(root: Path, name: str, filename: str) -> Path | None:
    if not NAME.fullmatch(name) or Path(filename).name != filename:
        raise HTTPException(400, "Invalid candidate or product name")
    directory = root / name
    path = directory / filename
    if directory.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) or not directory.resolve().is_relative_to(root.resolve()):
        raise HTTPException(403, "Product escapes the candidate directory")
    return path if path.is_file() else None


class AnnotationStore:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "annotations.sqlite3"
        self.lock = threading.Lock()
        self.last_backup = 0.0

    @contextmanager
    def connect(self, write=False):
        if self.path.is_symlink():
            raise HTTPException(403, "Annotation database cannot be a symlink")
        if not write and not self.path.exists():
            yield None
            return
        if write:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path if write else self.path.as_uri() + "?mode=ro", uri=not write, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(SCHEMA)
                os.chmod(self.path, 0o600)
            yield conn
        finally:
            conn.close()

    @staticmethod
    def votes(conn, names=None):
        if conn is None:
            return []
        where, args = "", ()
        if names is not None:
            if not names:
                return []
            where = "WHERE candidate IN (" + ",".join("?" for _ in names) + ")"
            args = tuple(names)
        rows = conn.execute(f"""SELECT a.*, r.name AS reviewer FROM annotations a
          JOIN reviewers r ON r.id=a.reviewer_id
          WHERE a.id IN (SELECT MAX(id) FROM annotations {where} GROUP BY candidate, reviewer_id)
          ORDER BY a.id""", args)
        return [AnnotationStore.vote(row) for row in rows]

    @staticmethod
    def vote(row):
        return {**json.loads(row["body"]), "revision": row["id"], "candidate": row["candidate"],
                "reviewer_id": row["reviewer_id"], "reviewer": row["reviewer"],
                "created_utc": row["created_utc"], "evidence_id": row["evidence_id"]}

    def retain(self, source: Path, limit: int, conn) -> dict:
        """Copy before hashing/committing: retained bytes never alias source files."""
        directory = self.root / "artifacts"
        directory.mkdir(mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".retain-")
        digest, count = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
                while chunk := inp.read(1024 * 1024):
                    count += len(chunk)
                    if count > limit:
                        raise HTTPException(413, "Candidate product exceeds the retention size limit")
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            sha = digest.hexdigest()
            suffix = source.suffix.lower()
            target = directory / (sha + suffix)
            if not target.exists():
                os.replace(temporary, target)
                dirfd = os.open(directory, os.O_DIRECTORY)
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
            conn.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?)", (sha, suffix, count))
            return {"sha256": sha, "suffix": suffix, "bytes": count, "source": str(source),
                    "url": f"/api/candidate-annotations/artifacts/{sha}{suffix}"}
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def backup(self, conn):
        # Snapshot includes WAL commits. These local copies supplement, not
        # replace, an operator's off-host backup of the complete directory.
        if time.monotonic() - self.last_backup < 300:
            return
        directory = self.root / "backups"
        directory.mkdir(mode=0o700, exist_ok=True)
        target = directory / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".sqlite3")
        temporary = target.with_suffix(".tmp")
        with sqlite3.connect(temporary) as backup:
            conn.backup(backup)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        self.last_backup = time.monotonic()


def build_router(settings) -> APIRouter:
    router = APIRouter(prefix="/api/candidate-annotations", tags=["candidate annotations"])
    store = AnnotationStore(annotation_root(settings))
    enabled = os.environ.get("CASM_MONITOR_WORKSPACE") == "1" and settings.observation_cache_root is not None
    csrf = secrets.token_urlsafe(32)

    def guard(request):
        if (not enabled or request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}
                or request.headers.get("origin") != f"{request.url.scheme}://{request.url.netloc}"
                or request.headers.get("x-casm-workspace") != "1"
                or not secrets.compare_digest(request.headers.get("x-casm-annotations-csrf", ""), csrf)):
            raise HTTPException(403, "Local workspace and same-origin annotation token required")

    def source_event(name):
        if not NAME.fullmatch(name):
            raise HTTPException(400, "Invalid candidate name")
        try:
            conn = sqlite3.connect(settings.t2_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT * FROM clusters WHERE name=?", (name,)).fetchone()
                if row is None:
                    raise HTTPException(404, "Candidate not found")
                triggers = [dict(r) for r in conn.execute("SELECT * FROM triggers WHERE candname=?", (name,))]
                # Preserve recorded injection associations separately from human labels.
                cols = {r[1] for r in conn.execute("PRAGMA table_info(injections)")}
                injection_key = "matched_cluster" if "matched_cluster" in cols else None
                injections = [dict(r) for r in conn.execute("SELECT * FROM injections WHERE matched_cluster=?", (row["id"],))] if injection_key else []
                return {"event": dict(row), "triggers": triggers, "injections": injections,
                        "injection_association": "recorded" if injection_key else "unavailable"}
            finally:
                conn.close()
        except sqlite3.Error:
            raise HTTPException(503, "Candidate ledger unavailable")

    def plot_info(name):
        out = []
        for filename in saved_plots(settings.candidates_dir, name):
            path = safe_file(settings.candidates_dir, name, filename)
            if path and path.stat().st_size <= 20 * 1024 * 1024:
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                out.append({"filename": filename, "sha256": sha,
                            "url": f"/api/candidate-annotations/events/{name}/plot/{filename}?sha256={sha}"})
        return out

    @router.get("")
    def overview(response: Response, names: str = Query(default="", max_length=14000)):
        selected = list(dict.fromkeys(names.split(","))) if names else []
        if len(selected) > 500 or any(not NAME.fullmatch(n) for n in selected):
            raise HTTPException(400, "Request at most 500 valid candidate names")
        response.headers["Cache-Control"] = "no-store"
        with store.connect() as conn:
            reviewers = [dict(r) for r in conn.execute("SELECT id,name FROM reviewers ORDER BY name_key")] if conn else []
            votes = store.votes(conn, selected)
        return {"reviewers": reviewers, "votes": votes, "csrf_token": csrf, "writes_enabled": enabled,
                "labels": LABELS, "morphology": MORPHOLOGY, "taxonomy_version": 1}

    @router.post("/reviewers")
    def reviewer(body: ReviewerInput, request: Request):
        guard(request)
        name = " ".join(unicodedata.normalize("NFKC", body.name).split())
        if not name or len(name) > 60 or any(unicodedata.category(c).startswith("C") for c in name):
            raise HTTPException(400, "Enter a readable reviewer name")
        with store.lock, store.connect(write=True) as conn:
            with conn:
                conn.execute("INSERT OR IGNORE INTO reviewers VALUES (?,?,?,?)",
                             (secrets.token_hex(16), name, name.casefold(), stamp()))
            return dict(conn.execute("SELECT id,name FROM reviewers WHERE name_key=?", (name.casefold(),)).fetchone())

    @router.get("/events/{name}")
    def detail(name: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            data = source_event(name)
        except HTTPException as exc:
            if exc.status_code not in {404, 503}:
                raise
            with store.connect() as conn:
                row = conn.execute("SELECT body FROM evidence WHERE candidate=? ORDER BY rowid DESC LIMIT 1", (name,)).fetchone() if conn else None
            if row is None:
                raise
            data = json.loads(row[0])["source"]
        with store.connect() as conn:
            votes = store.votes(conn, [name])
            history = [store.vote(r) for r in conn.execute(
                "SELECT a.*,r.name AS reviewer FROM annotations a JOIN reviewers r ON r.id=a.reviewer_id WHERE candidate=? ORDER BY a.id DESC LIMIT 200", (name,))] if conn else []
            saved = [json.loads(r[0]) for r in conn.execute("SELECT body FROM evidence WHERE candidate=?", (name,))] if conn else []
        plots = plot_info(name)
        known = {p["sha256"] for p in plots}
        for evidence in saved:
            p = evidence.get("plot")
            if p and p["sha256"] not in known:
                plots.append({"filename": Path(p["source"]).name, "sha256": p["sha256"], "url": p["url"], "retained": True})
                known.add(p["sha256"])
        return {**data, "plots": plots, "votes": votes, "history": history, "csrf_token": csrf}

    @router.get("/events/{name}/plot/{filename}")
    def exact_plot(name: str, filename: str, sha256: str = Query(pattern=r"^[0-9a-f]{64}$")):
        path = safe_file(settings.candidates_dir, name, filename)
        if not filename.endswith(".png") or path is None:
            raise HTTPException(404, "Plot unavailable")
        if path.stat().st_size > 20 * 1024 * 1024:
            raise HTTPException(413, "Plot exceeds 20 MiB")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise HTTPException(409, "Plot changed; reload this candidate")
        return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @router.post("/events/{name}")
    def assess(name: str, body: Assessment, request: Request):
        guard(request)
        if not NAME.fullmatch(name):
            raise HTTPException(400, "Invalid candidate name")
        payload_hash = hashlib.sha256(encoded({"candidate": name, **body.model_dump()}).encode()).hexdigest()
        with store.lock, store.connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                duplicate = conn.execute("SELECT * FROM annotations WHERE request_id=?", (body.request_id,)).fetchone()
                if duplicate:
                    if duplicate["payload_hash"] != payload_hash:
                        raise HTTPException(409, "Request ID already used for a different annotation")
                    conn.rollback()
                    return {"votes": store.votes(conn, [name]), "revision": duplicate["id"]}
                if not conn.execute("SELECT 1 FROM reviewers WHERE id=?", (body.reviewer_id,)).fetchone():
                    raise HTTPException(400, "Choose your reviewer name first")
                latest = conn.execute("SELECT * FROM annotations WHERE candidate=? AND reviewer_id=? ORDER BY id DESC LIMIT 1", (name, body.reviewer_id)).fetchone()
                revision = latest["id"] if latest else 0
                if revision != body.expected_revision:
                    raise HTTPException(409, "Your label changed in another window. Reload before saving.")
                evidence_id = None
                if body.action == "undo":
                    if not latest:
                        raise HTTPException(400, "No assessment to undo")
                    previous = conn.execute("SELECT * FROM annotations WHERE id=? AND candidate=? AND reviewer_id=?",
                                            (latest["parent_id"], name, body.reviewer_id)).fetchone()
                    if previous:
                        state = json.loads(previous["body"])
                        evidence_id = previous["evidence_id"]
                        parent = previous["parent_id"]
                    else:
                        state = {"label": None, "quality": "unreviewed", "morphology": [], "rfi_present": False, "note": "", "others_revealed": body.others_revealed}
                        parent = 0
                    if json.loads(latest["body"])["quality"] == "unreviewed":
                        raise HTTPException(400, "No assessment to undo")
                else:
                    if body.quality == "usable" and body.label is None:
                        raise HTTPException(400, "Choose a classification")
                    if body.quality == "cannot_assess" and body.label is not None:
                        raise HTTPException(400, "Unassessable data has no scientific class")
                    if body.morphology and body.label != "rfi" and not body.rfi_present:
                        raise HTTPException(400, "RFI details require RFI or RFI present")
                    try:
                        source = source_event(name)
                    except HTTPException as exc:
                        if exc.status_code not in {404, 503}:
                            raise
                        previous_evidence = conn.execute("SELECT body FROM evidence WHERE candidate=? ORDER BY rowid DESC LIMIT 1", (name,)).fetchone()
                        if previous_evidence is None:
                            raise
                        source = json.loads(previous_evidence[0])["source"]
                    products = {"source": source, "candidate": name, "plot": None, "metadata": [], "filterbank": None,
                                "filterbank_status": "unavailable", "captured_utc": stamp()}
                    # Reuse already retained immutable bytes across people and revisions.
                    saved = conn.execute("SELECT id,body FROM evidence WHERE candidate=?", (name,)).fetchall()
                    reuse = next((r for r in saved if body.plot_sha256 and (json.loads(r["body"]).get("plot") or {}).get("sha256") == body.plot_sha256), None)
                    if reuse:
                        evidence_id = reuse["id"]
                    else:
                        if body.plot:
                            path = safe_file(settings.candidates_dir, name, body.plot)
                            if not body.plot.endswith(".png") or path is None:
                                raise HTTPException(409, "Plot unavailable; reload or mark Cannot assess")
                            products["plot"] = store.retain(path, 20 * 1024 * 1024, conn)
                            if products["plot"]["sha256"] != body.plot_sha256:
                                raise HTTPException(409, "Plot changed since you viewed it. Reload before labeling.")
                        elif body.quality == "usable":
                            raise HTTPException(400, "View a plot before classifying; missing data can be marked Cannot assess")
                        for directory in (settings.candidates_dir, settings.candidate_archive_root):
                            meta = safe_file(directory, name, name + ".json")
                            if meta:
                                products["metadata"].append(store.retain(meta, 4 * 1024 * 1024, conn))
                        fil = safe_file(settings.candidate_archive_root, name, name + ".fil")
                        if fil:
                            if fil.stat().st_size <= 256 * 1024 * 1024:
                                products["filterbank"] = store.retain(fil, 256 * 1024 * 1024, conn)
                                products["filterbank_status"] = "retained"
                            else:
                                products["filterbank_status"] = "over_256_MiB_limit"
                        evidence_id = secrets.token_hex(16)
                        conn.execute("INSERT INTO evidence VALUES (?,?,?)", (evidence_id, name, encoded(products)))
                    state = {"label": body.label, "quality": body.quality, "morphology": sorted(set(body.morphology)),
                             "rfi_present": body.rfi_present, "note": body.note.strip(), "others_revealed": body.others_revealed}
                    parent = revision
                state.update(action=body.action, taxonomy_version=1)
                cur = conn.execute("INSERT INTO annotations(request_id,payload_hash,candidate,reviewer_id,created_utc,parent_id,evidence_id,body) VALUES (?,?,?,?,?,?,?,?)",
                                   (body.request_id, payload_hash, name, body.reviewer_id, stamp(), parent, evidence_id, encoded(state)))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            backup_warning = None
            try:
                store.backup(conn)
            except (OSError, sqlite3.Error):
                log.exception("Annotation committed, local backup failed")
                backup_warning = "Label saved; local backup failed. Export labels and check storage."
            return {"votes": store.votes(conn, [name]), "revision": cur.lastrowid, "backup_warning": backup_warning}

    @router.get("/artifacts/{filename}")
    def artifact(filename: str):
        match = re.fullmatch(r"([0-9a-f]{64})(\.png|\.json|\.fil)", filename)
        if not match:
            raise HTTPException(404, "Unknown artifact")
        with store.connect() as conn:
            row = conn.execute("SELECT suffix FROM artifacts WHERE sha256=?", (match[1],)).fetchone() if conn else None
        if row is None or row["suffix"] != match[2]:
            raise HTTPException(404, "Unknown artifact")
        path = store.root / "artifacts" / filename
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "Retained artifact unavailable")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @router.get("/export")
    def export():
        def records():
            yield encoded({"type": "manifest", "schema_version": 1, "taxonomy_version": 1,
                           "exported_utc": stamp(), "labels": LABELS, "human_assessments": True}) + "\n"
            with store.connect() as conn:
                if conn:
                    conn.execute("BEGIN")
                    for table in ("reviewers", "artifacts", "evidence", "annotations"):
                        for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                            item = dict(row)
                            if "body" in item:
                                item["body"] = json.loads(item["body"])
                            yield encoded({"type": table, **item}) + "\n"
        return StreamingResponse(records(), media_type="application/x-ndjson",
                                 headers={"Content-Disposition": 'attachment; filename="candidate-annotations.jsonl"', "Cache-Control": "no-store"})

    return router
