"""The Candidates tab's API (``/api/cands``): a prefix-aware JSON router over
casm_t3's own T2 event store (docs/plan.md "5. Candidates").

t3-web (``:8050``) is a server-rendered app whose templates use absolute
redirects and whose one write path inserts label/FRB rows straight into the
T2 sqlite; mounting its Jinja app under a prefix is unsafe (the redirects
would escape the prefix) and would fork the labeling path onto two apps
writing the same table. Instead this module IMPORTS casm_t3's own query and
write helpers (``casm_t3.web.app.q`` / ``.q_write`` / ``.held_reason`` /
``.friendly_outcome`` / ``.label`` / ``casm_t2.db`` / ``casm_t3.web.nowpanel``
/ ``casm_t3.web.statsplot``) rather than reimplementing their SQL, and wraps
them in JSON routes of our own. t3-web keeps running on :8050 until this tab
is verified against the live store; nothing here starts, stops, or reaches
into t3-web's process.

``casm_t3.web.app`` addresses its database and candidates tree through two
module-level constants (``DB_PATH``, ``CANDIDATES_DIR``) rather than an
injectable argument, and none of its helpers takes an explicit path (``q``
opens ``DB_PATH`` itself, ``q_write`` calls ``casm_t2.db.connect(DB_PATH)``).
Rather than point those globals at this service's settings ONCE at router
build -- which redirects every previously built router in the process, and
makes two routers built against two ``tmp_path`` stores cross-contaminate
(2026-09-09 review, finding 4) -- every call goes through :func:`_t3_bound`,
a context manager that takes a process-wide lock, sets the globals, and
restores whatever they were on the way out. Concurrency cost is nil in
practice (these are millisecond sqlite reads) and correctness no longer
depends on router-build order.

Everything here is read-only except ``POST /api/cands/events/{name}/label``,
which is gated by the SAME CSRF double-submit cookie/header scheme as the
Calibration tab's upload route (:mod:`casm_monitor.web.cal`): the cookie is
minted on the GETs the tab polls before a label button can even be clicked,
so the token can only exist in a browser that already read this API. That
write is the one place a second writer (t3-web, t2d's own labeller) can
collide with us, so it runs with a 5 s ``busy_timeout`` and a bounded
``SQLITE_BUSY`` retry, and answers 503 rather than 500 if the database is
still locked afterwards.

The funnel PNG is NOT rendered here: a GET route never writes (least of all
into t3's shared temp dir, outside our store root). The 30-minute
``render_figures`` job's ``cands`` target renders it into
``store_root/figures/cands/`` and this router only serves that file.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ..config import Settings
from ..store.shards import ensure_contained
from .cal import CSRF_COOKIE, CSRF_HEADER, CSRF_TOKEN_BYTES  # reuse the exact scheme

log = logging.getLogger("casm_monitor.web.cands")

# Event names: 6-digit UTC date + a random lowercase suffix. 12 chars total
# since 2026-07-31 (6 letters); legacy names before that are 10 chars (4
# letters) and still persist in the DB (casm_t2/casm_t2/db.py's schema
# comment). Case-insensitive because nothing downstream depends on case.
NAME_RE = re.compile(r"^\d{6}[a-zA-Z0-9]{4,6}$")

# Artefact filenames served through the plot route: candidate PNGs and the
# one per-event JSON meta file t3-web also serves from the same directory.
FNAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(png|json)$")

ETAG_BYTES = 16
EVENTS_DEFAULT_LIMIT = 500
EVENTS_MAX_LIMIT = 5000
STATS_ROWS_DEFAULT = 60
#: ``/frbs`` and ``/injections`` pagination (2026-09-09 review, finding 7).
LIST_DEFAULT_LIMIT = 200
LIST_MAX_LIMIT = 2000
#: Auxiliary ``name IN (...)`` queries are chunked so the parameter count can
#: never approach sqlite's ``SQLITE_MAX_VARIABLE_NUMBER`` even at the 5000-row
#: event limit.
IN_CHUNK = 400

#: The label write's busy timeout and retry budget. ``casm_t2.db.connect``
#: opens WAL with ``timeout=30``; this lowers the wait per attempt to 5 s and
#: retries a handful of times, so a genuinely wedged writer answers 503 in
#: bounded time instead of hanging a worker or 500ing on the first collision.
LABEL_BUSY_TIMEOUT_MS = 5000
LABEL_RETRIES = 5
LABEL_BACKOFF_S = 0.05

#: Where the ``render_figures`` job's ``cands`` target writes the funnel PNGs.
CANDS_FIGURES_SUBDIR = ("figures", "cands")
FUNNEL_TTL_S = 60

# t3's module globals are process-wide; one lock serialises every window in
# which they are pointed at our settings.
_T3_LOCK = threading.RLock()


class _BusyTimeoutDb:
    """``casm_t2.db`` with a ``busy_timeout`` applied to every connection.

    ``casm_t3.web.app.q_write`` resolves ``t2db`` in its own module globals,
    so swapping this proxy in (inside :func:`_t3_bound`, under the same lock,
    restored on the way out) is the only way to reach the connection t3's
    ``label()`` writes through without reimplementing that write.
    """

    def __init__(self, module: Any, busy_timeout_ms: int) -> None:
        self._module = module
        self._busy_timeout_ms = int(busy_timeout_ms)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)

    def connect(self, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = self._module.connect(*args, **kwargs)
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return conn


@contextlib.contextmanager
def _t3_bound(t3app: Any, settings: Settings, *, busy_timeout_ms: int | None = None) -> Iterator[Any]:
    """Point ``casm_t3.web.app``'s globals at ``settings`` for one call.

    Takes :data:`_T3_LOCK` for the whole window and restores the previous
    values (whatever they were -- t3-web's own defaults in production, another
    test's ``tmp_path`` store under pytest) unconditionally.
    """
    with _T3_LOCK:
        prev_db = t3app.DB_PATH
        prev_dir = t3app.CANDIDATES_DIR
        prev_t2db = t3app.t2db
        t3app.DB_PATH = str(settings.t2_db)
        t3app.CANDIDATES_DIR = Path(settings.candidates_dir)
        if busy_timeout_ms is not None:
            t3app.t2db = _BusyTimeoutDb(prev_t2db, busy_timeout_ms)
        try:
            yield t3app
        finally:
            t3app.DB_PATH = prev_db
            t3app.CANDIDATES_DIR = prev_dir
            t3app.t2db = prev_t2db


def _mint_csrf_cookie(request: Request, response: Response) -> None:
    """Same minting rule as :func:`casm_monitor.web.cal.build_router`'s
    ``/status`` route: mint ``casm_monitor_csrf`` if the browser does not
    already hold one, leave an existing one alone (two open tabs must not
    invalidate each other)."""
    if request.cookies.get(CSRF_COOKIE):
        return
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(CSRF_TOKEN_BYTES),
        httponly=False,  # the SPA must read it to echo it
        samesite="strict",
        path="/",
    )


def _validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"{name!r} is not a valid event name (YYMMDDxxxx)")
    return name


def _validate_fname(fname: str) -> str:
    base = Path(fname).name
    if base != fname or not FNAME_RE.match(fname):
        raise HTTPException(status_code=400, detail=f"{fname!r} is not a valid artefact filename")
    return fname


def canonical_since(text: str) -> str:
    """An ISO-8601 ``since`` -> the DB's own ``T``-separated UTC form.

    sqlite compares these timestamps as TEXT and ``' ' < 'T'``, so a
    space-separated bound silently selects the wrong same-day rows (the exact
    trap ``casm_t3.web.statsplot.utc_cut`` exists to avoid; this produces the
    same ``%Y-%m-%dT%H:%M:%S`` form it does). A value that is not a timestamp
    at all is a 400, never a lexical comparison against arbitrary text.
    """
    s = str(text).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"since must be an ISO-8601 timestamp (e.g. 2026-09-09T12:00:00): {exc}",
        ) from exc
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _chunks(names: list[str]) -> Iterator[list[str]]:
    for i in range(0, len(names), IN_CHUNK):
        yield names[i : i + IN_CHUNK]


def _file_response(path: Path, request: Request, *, cache: str) -> Response:
    """ETag'd file response, same pattern as :func:`casm_monitor.web.cal._png_response`.

    Candidate PNGs and their JSON meta are immutable once t3 writes them, so
    a hard ``Cache-Control`` is safe; ``cache`` lets the funnel PNG (which IS
    re-rendered periodically) opt into a short one instead.
    """
    data = path.read_bytes()
    etag = hashlib.sha256(data).hexdigest()[:ETAG_BYTES]
    media_type = "application/json" if path.suffix == ".json" else "image/png"
    headers = {
        "ETag": etag,
        "Cache-Control": cache,
        "Last-Modified": formatdate(path.stat().st_mtime, usegmt=True),
    }
    inm = request.headers.get("if-none-match")
    if inm and etag in {t.strip().strip('"') for t in inm.split(",")}:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type=media_type, headers=headers)


def _row_to_event(row: dict[str, Any], labels: dict[str, str], outcomes: dict[str, str]) -> dict[str, Any]:
    tags = [t for t in str(row.get("tags") or "").split(",") if t]
    return {
        "name": row["name"],
        "event_utc": row["event_utc"],
        "snr": row["snr"],
        "dm": row["dm"],
        "width": row["width"],
        "beam": row["beam"],
        "tier": row["tier"],
        "tags": tags,
        "n_beams": row["n_beams"],
        "n_members": row["n_members"],
        "alt_deg": row["alt_deg"],
        "az_deg": row["az_deg"],
        "label": labels.get(row["name"]),
        "outcome": outcomes.get(row["name"]),
    }


def funnel_dir(settings: Settings) -> Path:
    """``store_root/figures/cands`` -- the job's output, this router's input."""
    return ensure_contained(
        Path(settings.store_root).joinpath(*CANDS_FIGURES_SUBDIR), Path(settings.store_root)
    )


def funnel_filename(hours: int) -> str:
    """The PNG name the ``cands`` render target writes for one window.

    The page default (24 h) is the contract's ``funnel@1x.png``; the other
    presets get their own file so switching windows never serves the wrong
    chart.
    """
    return "funnel@1x.png" if int(hours) == 24 else f"funnel_{int(hours)}h@1x.png"


def build_router(settings: Settings) -> APIRouter:
    """The Candidates router. Reuses casm_t3's own query/write functions,
    bound to ``settings.t2_db`` / ``settings.candidates_dir`` per call."""
    # Imported lazily (module-level, once per router build, never per
    # request): casm_t3/casm_t2 pull in yaml/matplotlib/astropy at import
    # time, which every other router in this app avoids paying for unless
    # the Candidates tab is actually mounted.
    from casm_t3.web import app as t3app
    from casm_t3.web import nowpanel, statsplot

    router = APIRouter(prefix="/api/cands", tags=["cands"])

    def bound(*, busy_timeout_ms: int | None = None):
        return _t3_bound(t3app, settings, busy_timeout_ms=busy_timeout_ms)

    def actions_and_held(rows: list[Any]) -> tuple[dict[str, str], dict[str, str]]:
        """name -> outcome phrase, for the SELECTED rows only.

        Restricted to the names actually returned (2026-09-09 review, finding
        7): the whole ``triggers`` table is hundreds of thousands of rows and
        the page only ever shows ``limit`` of them.
        """
        names = [r["name"] for r in rows]
        tcfg = t3app.trigger_cfg()
        actions: dict[str, str] = {}
        for chunk in _chunks(names):
            placeholders = ",".join("?" * len(chunk))
            for r in t3app.q(
                "SELECT candname, action, detail FROM triggers"
                f" WHERE candname IN ({placeholders})"
                " ORDER BY action = 'triggered', id",
                tuple(chunk),
            ):
                actions[r["candname"]] = t3app.friendly_outcome(r["action"], r["detail"])
        held: dict[str, str] = {}
        for r in rows:
            if r["name"] not in actions:
                held[r["name"]] = t3app.held_reason(r["tier"], r["tags"], r["dm"], tcfg)
        return actions, {**held, **actions}

    def latest_labels(rows: list[Any]) -> dict[str, str]:
        """Newest label per SELECTED event name."""
        names = [r["name"] for r in rows]
        out: dict[str, str] = {}
        for chunk in _chunks(names):
            placeholders = ",".join("?" * len(chunk))
            for r in t3app.q(
                f"SELECT name, label FROM labels WHERE name IN ({placeholders})"
                " AND id IN (SELECT MAX(id) FROM labels GROUP BY name)",
                tuple(chunk),
            ):
                out[r["name"]] = r["label"]
        return out

    # -- events -----------------------------------------------------------
    @router.get("/events")
    def events(
        request: Request,
        response: Response,
        tier: str = "",
        tag: str = "",
        view: str = "candidates",
        limit: int = Query(default=EVENTS_DEFAULT_LIMIT, ge=1, le=EVENTS_MAX_LIMIT),
        since: str | None = None,
    ) -> dict[str, Any]:
        _mint_csrf_cookie(request, response)
        where, args = ["name IS NOT NULL"], []
        if view != "all" and not tag:
            # Default: dump attempts only, exactly t3-web's index() default
            # (docs/plan.md quotes this: "each has a plot or a red miss
            # reason"). ``view=all`` includes storm-gated/suppressed events.
            where.append(
                "name IN (SELECT candname FROM triggers WHERE"
                " action IN ('triggered', 'refused_daemon', 'failed')"
                " OR (action = 'refused' AND detail LIKE '%disk%'))"
            )
        if tier:
            where.append("tier = ?")
            args.append(tier)
        if tag:
            where.append(
                "name IN (SELECT name FROM labels WHERE label LIKE ?"
                " AND id IN (SELECT MAX(id) FROM labels GROUP BY name))"
            )
            args.append(f"%{tag}%")
        if since:
            where.append("event_utc > ?")
            args.append(canonical_since(since))
        with bound():
            rows = t3app.q(
                "SELECT name, event_utc, tier, tags, snr, dm, width, beam, n_beams,"
                f" n_members, alt_deg, az_deg FROM clusters WHERE {' AND '.join(where)}"
                " ORDER BY id DESC LIMIT ?",
                (*args, limit),
            )
            labels = latest_labels(rows)
            _actions, outcomes = actions_and_held(rows)
        return {"events": [_row_to_event(dict(r), labels, outcomes) for r in rows]}

    # -- one event ----------------------------------------------------------
    @router.get("/events/{name}")
    def event_detail(name: str, request: Request, response: Response) -> dict[str, Any]:
        _mint_csrf_cookie(request, response)
        _validate_name(name)
        with bound():
            rows = t3app.q("SELECT * FROM clusters WHERE name = ?", (name,))
            if not rows:
                raise HTTPException(status_code=404, detail=f"no such event {name!r}")
            ev = dict(rows[0])
            triggers = [
                dict(r)
                for r in t3app.q("SELECT * FROM triggers WHERE candname = ? ORDER BY id", (name,))
            ]
            labels = [
                dict(r)
                for r in t3app.q("SELECT * FROM labels WHERE name = ? ORDER BY id DESC", (name,))
            ]
            art_dir = t3app.CANDIDATES_DIR / name
            plots = sorted(p.name for p in art_dir.glob("*.png")) if art_dir.exists() else []
            meta: dict[str, Any] = {}
            for j in (
                ([art_dir / f"{name}.json"] if art_dir.exists() else [])
                + [d / f"{name}.json.done" for d in t3app.SPOOL_DIRS]
                + [d / f"{name}.json" for d in t3app.SPOOL_DIRS]
            ):
                if j.exists():
                    meta = json.loads(j.read_text())
                    meta.get("context", {}).pop("members", None)
                    break
            tcfg = t3app.trigger_cfg()
            if meta.get("data_available") is True:
                data_status = "raw dump on disk"
            elif meta.get("data_available") is False:
                data_status = "raw dump deleted after plotting"
            elif any(t["cleaned_utc"] for t in triggers):
                data_status = "raw dump deleted by janitor"
            elif any(t["action"] in ("triggered", "partial") for t in triggers):
                data_status = "unknown (pre-tracking dump)"
            elif triggers:
                last = triggers[-1]
                data_status = f"no dump — {t3app.friendly_outcome(last['action'], last['detail'])}"
            else:
                data_status = (
                    f"no dump attempt — {t3app.held_reason(ev['tier'], ev['tags'], ev['dm'], tcfg)}"
                )
            label_choices = list(t3app.LABELS)
        tags_display = [t for t in str(ev["tags"]).split(",") if t and not t.startswith("src:")]
        return {
            "event": ev,
            "tags_display": tags_display,
            "triggers": triggers,
            "labels": labels,
            "plots": plots,
            "meta": meta,
            "data_status": data_status,
            "label_choices": label_choices,
        }

    # -- artefacts ----------------------------------------------------------
    @router.get("/events/{name}/plot/{fname}", response_model=None)
    def plot(name: str, fname: str, request: Request) -> Response:
        _validate_name(name)
        _validate_fname(fname)
        # Containment is checked against THIS event's own directory, not the
        # whole candidates tree (2026-09-09 review, finding 5): a symlink
        # planted in event A's directory must not be able to serve event B's
        # artefacts through A's URL.
        event_dir = (Path(settings.candidates_dir) / name).resolve()
        path = (event_dir / fname).resolve()
        if not path.is_relative_to(event_dir) or not path.is_file():
            raise HTTPException(status_code=404, detail=f"{fname!r} is not an artefact of {name!r}")
        return _file_response(path, request, cache="public, max-age=86400")

    # -- label --------------------------------------------------------------
    @router.post("/events/{name}/label")
    def post_label(name: str, request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
        cookie = request.cookies.get(CSRF_COOKIE) or ""
        header = request.headers.get(CSRF_HEADER) or ""
        if not cookie or not header or not hmac.compare_digest(cookie, header):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"missing or mismatched CSRF token: GET /api/cands/events sets the "
                    f"{CSRF_COOKIE} cookie and this request must echo it in the "
                    f"{CSRF_HEADER} header"
                ),
            )
        _validate_name(name)
        body = dict(body or {})
        label_value = str(body.get("label") or "")
        with bound():
            if label_value not in t3app.LABELS:
                raise HTTPException(
                    status_code=400,
                    detail=f"label must be one of {'|'.join(t3app.LABELS)}",
                )
            rows = t3app.q("SELECT 1 FROM clusters WHERE name = ?", (name,))
        if not rows:
            raise HTTPException(status_code=404, detail=f"no such event {name!r}")
        note = str(body.get("note") or "")
        # t3's OWN write helper: identical INSERT + the label=='frb' promotion
        # into the frbs table (docs/plan.md: "uses t3's own write helper so
        # the frbs promotion behaviour is identical"). Its Form(...) defaults
        # are irrelevant here since every argument is passed explicitly; the
        # RedirectResponse it returns is discarded, we answer with our own
        # JSON of the row it just wrote. t2d and t3-web write the same file,
        # so SQLITE_BUSY is a normal outcome, not a bug: retry it.
        last_exc: sqlite3.OperationalError | None = None
        for attempt in range(LABEL_RETRIES):
            try:
                with bound(busy_timeout_ms=LABEL_BUSY_TIMEOUT_MS):
                    t3app.label(name=name, label=label_value, notes=note, who="monitor")
                last_exc = None
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                last_exc = exc
                log.warning("cands: label write busy (attempt %d/%d): %s", attempt + 1, LABEL_RETRIES, exc)
                time.sleep(LABEL_BACKOFF_S * (2**attempt))
        if last_exc is not None:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"the T2 database is locked by another writer (t2d/t3-web); "
                    f"gave up after {LABEL_RETRIES} attempts at "
                    f"{LABEL_BUSY_TIMEOUT_MS} ms each — try again in a moment"
                ),
            )
        with bound():
            labels = [
                dict(r)
                for r in t3app.q("SELECT * FROM labels WHERE name = ? ORDER BY id DESC", (name,))
            ]
        return {"name": name, "label": label_value, "labels": labels}

    # -- stats / funnel -------------------------------------------------------
    @router.get("/stats")
    def stats(hours: int = 24, limit: int = Query(default=STATS_ROWS_DEFAULT, ge=1, le=2000)) -> dict[str, Any]:
        if hours not in {h for h, _ in statsplot.WINDOW_PRESETS}:
            hours = 24
        with bound():
            rows = [
                dict(r)
                for r in t3app.q(
                    "SELECT gulp_utc, n_jobs, n_cands, n_clusters, n_stored, n_would,"
                    " clustering_ms FROM gulp_stats ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            ]
            hour_stats = t3app._window_stats(statsplot.utc_cut(1))
            win_stats = t3app._window_stats(statsplot.utc_cut(hours))
        return {
            "hours": hours,
            "win_label": statsplot.window_label(hours),
            "presets": [{"hours": h, "label": lbl} for h, lbl in statsplot.WINDOW_PRESETS],
            "hour": hour_stats,
            "win": win_stats,
            "now": nowpanel.snapshot(),
            "rows": rows,
        }

    @router.get("/stats/plot.png", response_model=None)
    def stats_plot(request: Request, hours: int = 24) -> Response:
        """Serve the funnel PNG the ``render_figures`` job rendered.

        Read-only: the chart is rendered by the 30-minute job's ``cands``
        target into ``store_root/figures/cands/`` (2026-09-09 review, finding
        1 -- a GET route must never render, least of all into t3's shared
        temp dir outside our store root).
        """
        if hours not in {h for h, _ in statsplot.WINDOW_PRESETS}:
            hours = 24
        path = funnel_dir(settings) / funnel_filename(hours)
        if not path.is_file():
            raise HTTPException(
                status_code=404,
                detail=(
                    f"the {statsplot.window_label(hours)} funnel chart has not been rendered "
                    "yet; it is rendered by the render_figures job's 'cands' target "
                    "(every 30 min)"
                ),
            )
        return _file_response(path, request, cache=f"public, max-age={FUNNEL_TTL_S}")

    # -- injections / frbs / transits ----------------------------------------
    @router.get("/injections")
    def injections(
        limit: int = Query(default=LIST_DEFAULT_LIMIT, ge=1, le=LIST_MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        with bound():
            rows = [
                dict(r)
                for r in t3app.q(
                    "SELECT i.*, c.name AS event_name FROM injections i"
                    " LEFT JOIN clusters c ON c.id = i.matched_cluster"
                    " ORDER BY i.id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ]
            day = dict(
                t3app.q(
                    "SELECT count(*) n, sum(gate_t1) t1, sum(gate_t2) t2,"
                    " sum(gate_trigger) tr, count(gate_t1) done FROM injections"
                    " WHERE inject_utc > ?",
                    (statsplot.utc_cut(24),),
                )[0]
            )
        return {"injections": rows, "day": day, "limit": limit, "offset": offset}

    @router.get("/frbs")
    def frbs(
        limit: int = Query(default=LIST_DEFAULT_LIMIT, ge=1, le=LIST_MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        with bound():
            rows = [
                dict(r)
                for r in t3app.q(
                    "SELECT * FROM frbs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
                )
            ]
        return {"frbs": rows, "limit": limit, "offset": offset}

    @router.get("/transits")
    def transits() -> dict[str, Any]:
        return {"snapshot": nowpanel.snapshot()}

    return router


__all__ = [
    "FNAME_RE",
    "NAME_RE",
    "build_router",
    "canonical_since",
    "funnel_dir",
    "funnel_filename",
]
