"""The Calibration tab's API (``/api/cal``).

Reads: the defaults for a build (Sun altitude maximum, layout, deployed
product), the list of builds under ``store_root/cal_builds/`` with their job
state, one build's ``summary.json`` + ``stage.json`` + upload audit rows, its
figures, its executed notebook and its log.

Writes: exactly three POSTs, each of which only ever inserts a job row through
``Store.submit_job_atomic`` on the app's single write handle:

* ``POST /api/cal/build`` -> a ``cal_build`` job (400 on any parameter the
  validator refuses, 409 when a build with that tag exists or one is running);
* ``POST /api/cal/builds/{tag}/stage`` -> a ``deploy_stage`` job (the dry run);
* ``POST /api/cal/builds/{tag}/upload`` -> a ``deploy_upload`` job. 403 when
  uploads are disabled (with the env flag named in the detail) or when the
  CSRF double-submit token is missing/mismatched, 400 when the typed
  ``confirm_tag`` does not match, 409 when the build is not staged or any
  other safeguard fails. This route ALSO mints the single-use
  ``upload_authorizations`` row, in the same transaction as the job, and the
  job's params carry nothing but its id: the worker consumes it atomically and
  re-derives every gate itself, so this route gives the browser a useful
  status code without being the only gate (2026-09-09 security review).

The three cal job kinds are ``privileged`` and are refused by the generic
``POST /api/jobs`` (403): these routes are the only way in.

Nothing here runs the deploy tool, the driver, or ssh: those live in the job
worker (:mod:`casm_monitor.jobs.cal_build`, :mod:`casm_monitor.jobs.deploy`).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from email.utils import formatdate
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse

from ..cal_defaults import cal_defaults, deployed_product, layout_info
from ..collectors.weights import read_last_ledger_row
from ..config import Settings
from ..jobs.cal_build import (
    ParamError,
    build_dir,
    list_builds,
    load_summary,
    validate,
)
from ..jobs.deploy import (
    CasmTrackCheckError,
    DeployError,
    casm_track_processes,
    load_stage,
    stage_digest,
    stage_json_path,
    upload_plan,
)
from ..store import Store
from ..store.shards import UnsafePathError
from ..util import iso

BUILD_KIND = "cal_build"
STAGE_KIND = "deploy_stage"
UPLOAD_KIND = "deploy_upload"
JOB_KINDS = (BUILD_KIND, STAGE_KIND, UPLOAD_KIND)
#: How many recent jobs the build list looks back over when pairing jobs to tags.
JOB_SCAN_LIMIT = 500
ETAG_BYTES = 16

#: CSRF double-submit (2026-09-09 security review, finding 1). ``GET
#: /api/cal/status`` — which the tab polls before it can offer the button —
#: sets this cookie; the upload POST must echo the same value in the header,
#: and the two are compared with :func:`hmac.compare_digest`. The service
#: binds 127.0.0.1 and has no login, so this does not authenticate anybody:
#: what it proves is that the request came from something that had read the
#: page's own status response, not from a blind POST (a curl one-liner pasted
#: into the wrong terminal, another page in the same browser).
CSRF_COOKIE = "casm_monitor_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_TOKEN_BYTES = 32


#: Titles for the driver's figure stems, in the order the frontend renders them
#: (docs/api-cal.md). A figure whose stem is not here is still served and still
#: listed, with its stem as the title, so a new driver figure never disappears.
FIG_TITLES: dict[str, str] = {
    "autocorr": "autocorrelations over the solve window",
    "phase_raw_sawtooth": "raw phase, sawtooth (pre fringe-stop)",
    "phase_stage1_raw": "raw phase, unwrapped",
    "phase_stage2_fringe_stopped": "phase after fringe-stopping",
    "phase_stage3_calibrated": "phase after calibration",
    "gain_delay_fits": "per-antenna gain/delay fits",
    "rank1_vs_freq": "rank-1 ratio vs frequency (solve quality, not beam quality)",
    "svd_vs_freq": "SVD singular values vs frequency",
    "cal_diff": "diff vs the currently deployed cal",
    "beam_grid": "beam grid",
    "source_transit": "source transit through the new grid",
}


def fig_name(filename: str, tag: str) -> str:
    """``"rank1_vs_freq_<tag>.png"`` -> ``"rank1_vs_freq"``.

    Files in the fringe subdirectory keep their relative path as the name:
    they have no stem the driver shares across builds.
    """
    if "/" in filename:
        return filename
    stem = filename[: -len(".png")] if filename.endswith(".png") else filename
    suffix = f"_{tag}"
    return stem[: -len(suffix)] if stem.endswith(suffix) else stem


def fig_entries(summary: dict[str, Any]) -> list[dict[str, str]]:
    """``[{name, file, title}]`` for every figure the build recorded."""
    tag = str(summary.get("tag") or "")
    entries = []
    for filename in summary.get("figs") or []:
        name = fig_name(filename, tag)
        base = name.split("/")[-1]
        title = FIG_TITLES.get(name) or FIG_TITLES.get(base)
        if title is None and name.startswith("beam_check"):
            title = f"beam check: {name.split('beam_check_', 1)[-1]}"
        entries.append({"name": name, "file": filename, "title": title or base})
    order = list(FIG_TITLES)

    def rank(entry: dict[str, str]) -> tuple[int, str]:
        try:
            return order.index(entry["name"]), entry["name"]
        except ValueError:
            return len(order), entry["name"]

    return sorted(entries, key=rank)


def resolve_fig(summary: dict[str, Any], name: str) -> str | None:
    """Map a requested figure name to a recorded file, or None.

    Accepts the recorded filename, the same without ``.png``, or the stem the
    build list publishes (``rank1_vs_freq``). Only names the build's own
    ``summary.json`` lists can resolve, so nothing from the request reaches the
    filesystem unchecked.
    """
    files = list(summary.get("figs") or [])
    if name in files:
        return name
    candidates = {name, name[: -len(".png")] if name.endswith(".png") else f"{name}.png"}
    for entry in fig_entries(summary):
        if entry["name"] in candidates or entry["file"] in candidates:
            return entry["file"]
    return None


def summary_view(summary: dict[str, Any]) -> dict[str, Any]:
    """summary.json plus the flat fields docs/api-cal.md asks the tab for."""
    numbers = summary.get("numbers") or {}
    paths = summary.get("paths") or {}
    view = dict(summary)
    delay = (numbers.get("gain_delay_fits") or {}).get("median_resid_rms_rad")
    view.update(
        cal_h5=paths.get("cal_h5"),
        weights_h5=paths.get("weights_h5"),
        ib_h5=paths.get("ib_h5"),
        notebook=bool(paths.get("notebook")),
        notebook_path=paths.get("notebook"),
        figs=fig_entries(summary),
        figs_files=list(summary.get("figs") or []),
        subband_occupancy=[
            row.get("good_frac") for row in (numbers.get("cal_subbands") or [])
        ],
        pointing_fit=numbers.get("pointing_fit"),
        delay_fit_rms_rad=delay,
        delay_fit_rms_deg=None if delay is None else round(float(delay) * 180.0 / 3.141592653589793, 3),
        beam_check=numbers.get("beam_check"),
    )
    return view


def stage_view(stage: dict[str, Any] | None) -> dict[str, Any] | None:
    """stage.json plus the per-file table and the single command string."""
    if stage is None:
        return None
    view = dict(stage)
    view["files"] = [
        {
            "name": name,
            "md5": md5,
            "payload_md5": (stage.get("payload_md5s") or {}).get(name),
            "size": (stage.get("sizes") or {}).get(name),
            "scale": stage.get("ib_scale") if name.startswith("direct_ib") else stage.get("scale"),
        }
        for name, md5 in sorted((stage.get("md5s") or {}).items())
    ]
    view["command"] = stage.get("upload_command_str")
    return view


def upload_view(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["ts"] = iso(row.get("ts"))
    out["registry_id"] = row.get("product_id")
    out["command"] = " ".join(row.get("command") or [])
    return out


def _tag_of(job: dict[str, Any]) -> str | None:
    params = job.get("params") or {}
    if not isinstance(params, dict):
        return None
    tag = params.get("tag") or params.get("build_tag")
    return str(tag) if tag else None


def _jobs_by_tag(reader: Store) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """``{tag: {kind: [jobs, newest first]}}`` for the three cal job kinds."""
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for job in reader.list_jobs(limit=JOB_SCAN_LIMIT):
        kind = str(job.get("kind"))
        if kind not in JOB_KINDS:
            continue
        tag = _tag_of(job)
        if tag is None:
            continue
        out.setdefault(tag, {}).setdefault(kind, []).append(job)
    return out


def _validated_dir(settings: Settings, tag: str) -> Path:
    try:
        return build_dir(settings, tag)
    except UnsafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_summary(settings: Settings, tag: str) -> dict[str, Any]:
    summary = load_summary(settings, tag)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"no completed build {tag!r}")
    return summary


def _png_response(path: Path, request: Request) -> Response:
    data = path.read_bytes()
    etag = hashlib.sha256(data).hexdigest()[:ETAG_BYTES]
    headers = {
        "ETag": etag,
        # A build's figures are immutable once written, so they may be cached
        # hard; a rebuild is a new tag and therefore a new URL.
        "Cache-Control": "public, max-age=86400",
        "Last-Modified": formatdate(path.stat().st_mtime, usegmt=True),
    }
    inm = request.headers.get("if-none-match")
    if inm and etag in {t.strip().strip('"') for t in inm.split(",")}:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="image/png", headers=headers)


def build_row(
    settings: Settings,
    reader: Store,
    tag: str,
    jobs: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """One row of ``GET /api/cal/builds``."""
    jobs = jobs or {}
    summary = load_summary(settings, tag) or {}
    stage = load_stage(settings, tag)
    build_jobs = jobs.get(BUILD_KIND) or []
    job = build_jobs[0] if build_jobs else None
    if job is not None:
        state = str(job.get("state"))
    else:
        state = "done" if summary else "unknown"
    uploads = reader.uploads(build_tag=tag, limit=20)
    return {
        "tag": tag,
        "state": state,
        "job_id": None if job is None else int(job["id"]),
        "created": iso(job["created"]) if job else summary.get("created_utc"),
        "finished": summary.get("finished_utc"),
        "source": summary.get("source"),
        "source_window": summary.get("source_window"),
        "static_window": summary.get("static_window"),
        "n_ant": summary.get("n_ant"),
        "rank1_median": summary.get("rank1_median"),
        "wall_s": summary.get("wall_s"),
        "peak_rss_mb": summary.get("peak_rss_mb"),
        "has_weights": bool(summary.get("has_weights")),
        "staged": bool(stage and stage.get("checks_ok")),
        "stage_attempted": stage is not None,
        "uploaded": any(int(u.get("exit_code") or 1) == 0 for u in uploads),
        "n_uploads": len(uploads),
    }


def build_router(reader: Store, writer: Store, settings: Settings) -> APIRouter:
    """Router over the app's existing handles (reader for GET, writer for POST)."""
    router = APIRouter(prefix="/api/cal", tags=["cal"])

    # -- defaults / status ----------------------------------------------
    @router.get("/defaults")
    def defaults(date: str = Query(..., description="UTC date, YYYY-MM-DD")) -> dict[str, Any]:
        try:
            return cal_defaults(date, settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/status")
    def status(request: Request, response: Response) -> dict[str, Any]:
        # The CSRF cookie is minted here (and only here): the tab always polls
        # this route, so by the time the upload button exists the browser holds
        # a token to echo. An existing cookie is left alone so two open tabs
        # do not invalidate each other.
        token = request.cookies.get(CSRF_COOKIE)
        if not token:
            token = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
            response.set_cookie(
                CSRF_COOKIE,
                token,
                httponly=False,  # the SPA must read it to echo it
                samesite="strict",
                path="/",
            )
        track_error = None
        try:
            tracks = casm_track_processes()
        except CasmTrackCheckError as exc:
            # Fails CLOSED, here too: an unusable ps is reported as "running"
            # so the tab keeps the button disabled and says why.
            tracks = []
            track_error = str(exc)
        active = None
        for job in reader.list_jobs(limit=50):
            if str(job.get("kind")) in JOB_KINDS and job.get("state") in ("queued", "running"):
                active = {
                    "id": int(job["id"]),
                    "kind": job["kind"],
                    "state": job["state"],
                    "tag": _tag_of(job),
                }
                break
        return {
            "allow_upload": bool(settings.allow_upload),
            "allow_upload_env": "CASM_MONITOR_ALLOW_UPLOAD",
            "casm_track_running": bool(tracks) or track_error is not None,
            "casm_track_processes": tracks,
            "casm_track_error": track_error,
            "csrf_header": CSRF_HEADER,
            "csrf_cookie": CSRF_COOKIE,
            "ledger_row": read_last_ledger_row(settings.deployed_weights_csv),
            "deployed": deployed_product(settings),
            "layout": layout_info(settings.layout_csv),
            "active_job": active,
        }

    # -- builds ----------------------------------------------------------
    @router.get("/builds")
    def builds() -> dict[str, Any]:
        jobs = _jobs_by_tag(reader)
        tags = sorted(set(list_builds(settings)) | set(jobs))
        rows = [build_row(settings, reader, tag, jobs.get(tag)) for tag in tags]
        rows.sort(key=lambda r: (r.get("created") or "", r["tag"]), reverse=True)
        return {"builds": rows}

    @router.get("/builds/{tag}")
    def build(tag: str) -> dict[str, Any]:
        _validated_dir(settings, tag)
        summary = load_summary(settings, tag)
        jobs = _jobs_by_tag(reader).get(tag, {})
        if summary is None and not jobs:
            raise HTTPException(status_code=404, detail=f"no build {tag!r}")
        row = build_row(settings, reader, tag, jobs)
        return {
            "tag": tag,
            "state": row["state"],
            "params": (summary or {}).get("params"),
            "summary": None if summary is None else summary_view(summary),
            "stage": stage_view(load_stage(settings, tag)),
            "uploads": [upload_view(u) for u in reader.uploads(build_tag=tag, limit=50)],
            "jobs": {kind: rows for kind, rows in jobs.items()},
            "row": row,
        }

    @router.post("/build")
    def post_build(body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            params = validate(dict(body or {}), settings)
        except ParamError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except UnsafePathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tag = params["tag"]
        if _validated_dir(settings, tag).exists():
            raise HTTPException(
                status_code=409,
                detail=f"build {tag!r} already exists; choose another tag",
            )
        job_id, refusal = writer.submit_job_atomic(BUILD_KIND, params, refuse_if_pending=True)
        if refusal is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cal_build job {refusal.get('job_id')} is {refusal.get('state')}; "
                    f"one build at a time"
                ),
            )
        writer.add_event(
            "job_submitted",
            severity="info",
            subject=f"job {job_id} ({BUILD_KIND})",
            detail={"job_id": job_id, "kind": BUILD_KIND, "tag": tag, "params": params},
        )
        return {"job_id": job_id, "tag": tag}

    # -- artefacts -------------------------------------------------------
    @router.get("/builds/{tag}/figs/{name:path}", response_model=None)
    def fig(tag: str, name: str, request: Request) -> Response:
        summary = _require_summary(settings, tag)
        # The whitelist IS the summary: only figures this build recorded can be
        # served, so no path from the request reaches the filesystem unchecked.
        # Both spellings resolve: the recorded filename and the stem the build
        # detail publishes (docs/api-cal.md ``figs[].name``).
        filename = resolve_fig(summary, name)
        if filename is None:
            raise HTTPException(status_code=404, detail=f"{name!r} is not a figure of {tag!r}")
        path = _validated_dir(settings, tag) / "figs" / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"{filename} is missing from the build")
        return _png_response(path, request)

    @router.get("/builds/{tag}/notebook", response_model=None)
    def notebook(tag: str, format: str = Query("ipynb", pattern="^(ipynb|html)$")) -> Response:
        summary = _require_summary(settings, tag)
        path = (summary.get("paths") or {}).get("notebook")
        if not path or not Path(path).is_file():
            raise HTTPException(status_code=404, detail=f"build {tag!r} has no notebook")
        nb_path = Path(path)
        if format == "ipynb":
            return FileResponse(
                nb_path, media_type="application/x-ipynb+json", filename=nb_path.name
            )
        try:
            import nbformat
            from nbconvert import HTMLExporter
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail="nbconvert is not installed; download the .ipynb instead",
            ) from exc
        nb = nbformat.reads(nb_path.read_text(), as_version=4)
        body, _resources = HTMLExporter(template_name="lab").from_notebook_node(nb)
        return HTMLResponse(body)

    @router.get("/builds/{tag}/log", response_model=None)
    def log(tag: str, tail_kb: int = Query(256, ge=1, le=8192)) -> Response:
        path = _validated_dir(settings, tag) / "build.log"
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"build {tag!r} has no log yet")
        size = path.stat().st_size
        limit = int(tail_kb) * 1024
        with path.open("rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            data = fh.read()
        return Response(
            content=data.decode("utf-8", errors="replace"),
            media_type="text/plain; charset=utf-8",
            headers={"X-Log-Size": str(size)},
        )

    # -- stage / upload --------------------------------------------------
    @router.post("/builds/{tag}/stage")
    def post_stage(tag: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        summary = _require_summary(settings, tag)
        if not summary.get("has_weights"):
            raise HTTPException(
                status_code=409, detail=f"build {tag!r} produced no weights file to stage"
            )
        params = {"build_tag": tag, "save_defaults": bool((body or {}).get("save_defaults", False))}
        job_id, refusal = writer.submit_job_atomic(STAGE_KIND, params, refuse_if_pending=True)
        if refusal is not None:
            raise HTTPException(
                status_code=409,
                detail=f"deploy_stage job {refusal.get('job_id')} is {refusal.get('state')}",
            )
        writer.add_event(
            "job_submitted",
            severity="info",
            subject=f"job {job_id} ({STAGE_KIND})",
            detail={"job_id": job_id, "kind": STAGE_KIND, "tag": tag},
        )
        return {"job_id": job_id, "tag": tag}

    @router.post("/builds/{tag}/upload")
    def post_upload(
        tag: str, request: Request, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = dict(body or {})
        # CSRF double-submit BEFORE anything else: this is the one route that
        # can put bytes on the correlator nodes, and it must be reachable only
        # from something that read GET /api/cal/status first.
        cookie = request.cookies.get(CSRF_COOKIE) or ""
        header = request.headers.get(CSRF_HEADER) or ""
        if not cookie or not header or not hmac.compare_digest(cookie, header):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"missing or mismatched CSRF token: GET /api/cal/status sets the "
                    f"{CSRF_COOKIE} cookie and this request must echo it in the "
                    f"{CSRF_HEADER} header"
                ),
            )
        if not settings.allow_upload:
            raise HTTPException(
                status_code=403,
                detail=(
                    "uploads are disabled on this service: set "
                    "CASM_MONITOR_ALLOW_UPLOAD=1 in the casm-monitor-jobs and "
                    "casm-monitor-web unit environments (and cal.allow_upload: true "
                    "in the config), then restart them"
                ),
            )
        _require_summary(settings, tag)
        confirm = str(body.get("confirm_tag") or "")
        if confirm != tag:
            raise HTTPException(
                status_code=400,
                detail=f"confirm_tag {confirm!r} does not match the build tag {tag!r}",
            )
        stage = load_stage(settings, tag)
        if stage is None:
            raise HTTPException(
                status_code=409,
                detail=f"build {tag!r} has not been staged; run the dry run first",
            )
        save_defaults = bool(body.get("save_defaults", False))
        try:
            # The same gate the worker runs, so the operator gets a status code
            # instead of a job that fails a second later. The worker re-runs
            # every one of these checks itself, under the deploy lock.
            upload_plan(settings, tag, confirm, stage=stage, save_defaults=save_defaults)
        except DeployError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # The authorization row and the job are inserted in ONE transaction;
        # the job's params carry nothing but the authorization's id.
        job_id, auth_id, job_refusal = writer.submit_upload_job_authorized(
            UPLOAD_KIND,
            build_tag=tag,
            confirm_tag=confirm,
            stage_digest=stage_digest(stage),
            note=body.get("note"),
            save_defaults=save_defaults,
        )
        if job_refusal is not None:
            raise HTTPException(
                status_code=409,
                detail=f"deploy_upload job {job_refusal.get('job_id')} is "
                f"{job_refusal.get('state')}",
            )
        writer.add_event(
            "upload_requested",
            severity="warn",
            subject=tag,
            detail={
                "job_id": job_id,
                "auth_id": auth_id,
                "tag": tag,
                "save_defaults": save_defaults,
                "note": body.get("note"),
                "stage_json": str(stage_json_path(settings, tag)),
                "requested_utc": iso(time.time()),
            },
        )
        return {"job_id": job_id, "auth_id": auth_id, "tag": tag}

    return router


__all__ = ["build_router", "build_row"]
