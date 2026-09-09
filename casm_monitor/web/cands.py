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
injectable argument. :func:`build_router` points both at this service's own
``settings`` once, at router-construction time (never per-request), so a test
harness can build the router against a ``tmp_path`` store exactly like every
other router in this package; the live service points them at the same file
t3-web itself reads (``settings.t2_db`` / ``settings.candidates_dir``,
defaults matching t3-web's own constants) so a label written through either
UI is visible in both.

Everything here is read-only except ``POST /api/cands/events/{name}/label``,
which is gated by the SAME CSRF double-submit cookie/header scheme as the
Calibration tab's upload route (:mod:`casm_monitor.web.cal`): the cookie is
minted on the GETs the tab polls before a label button can even be clicked,
so the token can only exist in a browser that already read this API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from email.utils import formatdate
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ..config import Settings
from .cal import CSRF_COOKIE, CSRF_HEADER, CSRF_TOKEN_BYTES  # reuse the exact scheme

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


def _file_response(path: Path, request: Request, *, cache: str) -> Response:
    """ETag'd file response, same pattern as :func:`casm_monitor.web.cal._png_response`.

    Candidate PNGs and their JSON meta are immutable once t3 writes them, so
    a hard ``Cache-Control`` is safe; ``cache`` lets the stats PNG (which IS
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


def build_router(settings: Settings) -> APIRouter:
    """The Candidates router. Reuses casm_t3's own query/write functions,
    pointed at ``settings.t2_db`` / ``settings.candidates_dir``."""
    # Imported lazily (module-level, once per router build, never per
    # request): casm_t3/casm_t2 pull in yaml/matplotlib/astropy at import
    # time, which every other router in this app avoids paying for unless
    # the Candidates tab is actually mounted.
    from casm_t2 import db as t2db
    from casm_t3.web import app as t3app
    from casm_t3.web import nowpanel, statsplot

    # Point t3's own module-level constants at OUR settings, once, here (see
    # the module docstring). In production these already match t3-web's
    # defaults; tests set settings.t2_db / settings.candidates_dir to a
    # tmp_path store the way every other router's tests do.
    t3app.DB_PATH = str(settings.t2_db)
    t3app.CANDIDATES_DIR = Path(settings.candidates_dir)

    router = APIRouter(prefix="/api/cands", tags=["cands"])

    def actions_and_held(rows: list[Any]) -> tuple[dict[str, str], dict[str, str]]:
        """name -> outcome phrase, for every row (dumped/held/missed/...)."""
        tcfg = t3app.trigger_cfg()
        actions: dict[str, str] = {}
        for r in t3app.q(
            "SELECT candname, action, detail FROM triggers ORDER BY action = 'triggered', id"
        ):
            actions[r["candname"]] = t3app.friendly_outcome(r["action"], r["detail"])
        held: dict[str, str] = {}
        for r in rows:
            if r["name"] not in actions:
                held[r["name"]] = t3app.held_reason(r["tier"], r["tags"], r["dm"], tcfg)
        return actions, {**held, **actions}

    def latest_labels() -> dict[str, str]:
        return {
            r["name"]: r["label"]
            for r in t3app.q(
                "SELECT name, label FROM labels WHERE id IN"
                " (SELECT MAX(id) FROM labels GROUP BY name)"
            )
        }

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
            args.append(since)
        rows = t3app.q(
            "SELECT name, event_utc, tier, tags, snr, dm, width, beam, n_beams,"
            f" n_members, alt_deg, az_deg FROM clusters WHERE {' AND '.join(where)}"
            " ORDER BY id DESC LIMIT ?",
            (*args, limit),
        )
        labels = latest_labels()
        _actions, outcomes = actions_and_held(rows)
        return {"events": [_row_to_event(dict(r), labels, outcomes) for r in rows]}

    # -- one event ----------------------------------------------------------
    @router.get("/events/{name}")
    def event_detail(name: str, request: Request, response: Response) -> dict[str, Any]:
        _mint_csrf_cookie(request, response)
        _validate_name(name)
        rows = t3app.q("SELECT * FROM clusters WHERE name = ?", (name,))
        if not rows:
            raise HTTPException(status_code=404, detail=f"no such event {name!r}")
        ev = dict(rows[0])
        triggers = [dict(r) for r in t3app.q("SELECT * FROM triggers WHERE candname = ? ORDER BY id", (name,))]
        labels = [dict(r) for r in t3app.q("SELECT * FROM labels WHERE name = ? ORDER BY id DESC", (name,))]
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
            data_status = f"no dump attempt — {t3app.held_reason(ev['tier'], ev['tags'], ev['dm'], tcfg)}"
        tags_display = [t for t in str(ev["tags"]).split(",") if t and not t.startswith("src:")]
        return {
            "event": ev,
            "tags_display": tags_display,
            "triggers": triggers,
            "labels": labels,
            "plots": plots,
            "meta": meta,
            "data_status": data_status,
            "label_choices": list(t3app.LABELS),
        }

    # -- artefacts ----------------------------------------------------------
    @router.get("/events/{name}/plot/{fname}", response_model=None)
    def plot(name: str, fname: str, request: Request) -> Response:
        _validate_name(name)
        _validate_fname(fname)
        path = (t3app.CANDIDATES_DIR / name / fname).resolve()
        try:
            inside = path.is_relative_to(t3app.CANDIDATES_DIR.resolve())
        except AttributeError:  # pragma: no cover - py<3.9
            inside = str(path).startswith(str(t3app.CANDIDATES_DIR.resolve()))
        if not inside or not path.is_file():
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
        # JSON of the row it just wrote.
        t3app.label(name=name, label=label_value, notes=note, who="monitor")
        labels = [dict(r) for r in t3app.q("SELECT * FROM labels WHERE name = ? ORDER BY id DESC", (name,))]
        return {"name": name, "label": label_value, "labels": labels}

    # -- stats / funnel -------------------------------------------------------
    @router.get("/stats")
    def stats(hours: int = 24, limit: int = Query(default=STATS_ROWS_DEFAULT, ge=1, le=2000)) -> dict[str, Any]:
        if hours not in {h for h, _ in statsplot.WINDOW_PRESETS}:
            hours = 24
        rows = [
            dict(r)
            for r in t3app.q(
                "SELECT gulp_utc, n_jobs, n_cands, n_clusters, n_stored, n_would,"
                " clustering_ms FROM gulp_stats ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        ]
        return {
            "hours": hours,
            "win_label": statsplot.window_label(hours),
            "presets": [{"hours": h, "label": lbl} for h, lbl in statsplot.WINDOW_PRESETS],
            "hour": t3app._window_stats(statsplot.utc_cut(1)),
            "win": t3app._window_stats(statsplot.utc_cut(hours)),
            "now": nowpanel.snapshot(),
            "rows": rows,
        }

    @router.get("/stats/plot.png", response_model=None)
    def stats_plot(request: Request, hours: int = 24) -> Response:
        if hours not in {h for h, _ in statsplot.WINDOW_PRESETS}:
            hours = 24
        png = t3app._stats_png(hours)
        try:
            if not png.exists() or time.time() - png.stat().st_mtime > t3app.STATS_TTL_S:
                statsplot.render(t3app.DB_PATH, png, hours=hours)
        except Exception:
            pass  # the route must still answer even if charting breaks
        if not png.exists():
            raise HTTPException(status_code=404, detail="the funnel chart has not rendered yet")
        return _file_response(png, request, cache=f"public, max-age={int(t3app.STATS_TTL_S)}")

    # -- injections / frbs / transits ----------------------------------------
    @router.get("/injections")
    def injections(limit: int = Query(default=200, ge=1, le=5000)) -> dict[str, Any]:
        rows = [
            dict(r)
            for r in t3app.q(
                "SELECT i.*, c.name AS event_name FROM injections i"
                " LEFT JOIN clusters c ON c.id = i.matched_cluster"
                " ORDER BY i.id DESC LIMIT ?",
                (limit,),
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
        return {"injections": rows, "day": day}

    @router.get("/frbs")
    def frbs() -> dict[str, Any]:
        return {"frbs": [dict(r) for r in t3app.q("SELECT * FROM frbs ORDER BY id DESC")]}

    @router.get("/transits")
    def transits() -> dict[str, Any]:
        return {"snapshot": nowpanel.snapshot()}

    return router


__all__ = ["build_router", "NAME_RE", "FNAME_RE"]
