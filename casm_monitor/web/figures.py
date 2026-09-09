"""The Visibilities, SNAPs and Imaging server-rendered figures API.

Read-only: every route reads a ``store_root/figures/<vis|snaps|imaging>/...`` tree
:mod:`casm_monitor.collectors.figures` writes (manifest + PNGs), and nothing
here ever touches a shard, a board or a file outside that tree. Path
components (``set``, ``ref``, ``kind``) are validated against the exact
whitelists the collector renders (:mod:`casm_monitor.figures.vis_figures` /
:mod:`casm_monitor.figures.snap_figures`), never interpolated into a path
unchecked, so a crafted ``kind=../../x`` 400s before any filesystem access.

Caching: every PNG is served with an ``ETag`` (a sha256 prefix of the file's
own bytes), ``Cache-Control: public, max-age=1800`` (the collector's own
render cadence -- a cached copy is never staler than the next render) and
``Last-Modified``; a matching ``If-None-Match`` gets a bare 304. The frontend
appends ``?v=<rendered_utc>`` from the manifest so a new render is a new URL
the browser has never cached, while an unchanged one is a guaranteed cache
hit -- the two together are what makes the "one <img> per view" page fast.

The SNAPs tree has one fewer path component than the Vis one (no ``ref``):
``store_root/figures/snaps/<set>/<kind>@{1x,2x}.png`` plus one
``manifest.json`` per ``<set>``. The Imaging tree (M4) is flatter still --
``store_root/figures/imaging/`` holds one manifest, the four products it names
(``latest@{1x,2x}.png``, ``strip24h@{1x,2x}.png``, ``allsky24h.mp4``), the
per-source cutouts it lists (``cutout_<source>@{1x,2x}.png``) and a
``frames/<unix>@1x.png`` cache the scrub view browses through
``GET /api/imaging/history``.
"""

from __future__ import annotations

import hashlib
import json
import re
from email.utils import formatdate
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ..config import Settings
from ..figures.snap_figures import KINDS as SNAP_KINDS
from ..figures.snap_figures import SETS as SNAP_SETS
from ..figures.vis_figures import KINDS, REFS, SETS
from ..store.shards import UnsafePathError, ensure_contained, safe_name

ETAG_BYTES = 16  # sha256 hex prefix length used as the ETag
SUFFIXES = ("1x", "2x")


def _figures_root(settings: Settings) -> Path:
    return ensure_contained(
        Path(settings.store_root) / "figures" / "vis", Path(settings.store_root)
    )


def _validated_dir(root: Path, set_name: str, ref: str) -> Path:
    if set_name not in SETS:
        raise HTTPException(status_code=400, detail=f"set must be one of {'|'.join(SETS)}")
    if ref not in REFS:
        raise HTTPException(status_code=400, detail=f"ref must be one of {'|'.join(REFS)}")
    try:
        return ensure_contained(
            root / safe_name(set_name, "set") / safe_name(ref, "ref"), root
        )
    except UnsafePathError as exc:  # pragma: no cover - set/ref already whitelisted above
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _read_manifest(outdir: Path) -> dict[str, Any]:
    path = outdir / "manifest.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=404,
            detail="no figures rendered yet for this set/ref (the collector may not have run)",
        ) from exc


def _split_filename(filename: str, kinds: tuple[str, ...] = KINDS) -> tuple[str, str]:
    """``"<kind>@<suffix>.png"`` -> ``(kind, suffix)``, whitelisted both ends."""
    if not filename.endswith(".png") or "@" not in filename:
        raise HTTPException(status_code=400, detail="expected <kind>@<1x|2x>.png")
    stem, suffix = filename[: -len(".png")].rsplit("@", 1)
    if stem not in kinds:
        raise HTTPException(status_code=400, detail=f"kind must be one of {'|'.join(kinds)}")
    if suffix not in SUFFIXES:
        raise HTTPException(status_code=400, detail="suffix must be 1x or 2x")
    return stem, suffix


def _validated_png_path(outdir: Path, filename: str, kinds: tuple[str, ...] = KINDS) -> Path:
    kind, suffix = _split_filename(filename, kinds)
    # ``kind``/``suffix`` are already checked against ``kinds``/SUFFIXES above,
    # so the reconstructed name can contain no "/" or "..": ``ensure_contained``
    # is defense in depth, not the primary check (``safe_name`` itself rejects
    # "@", which every one of these filenames legitimately contains).
    name = f"{kind}@{suffix}.png"
    try:
        path = ensure_contained(outdir / name, outdir)
    except UnsafePathError as exc:  # pragma: no cover - kind/suffix already whitelisted
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"{kind}@{suffix}.png has not been rendered for this set yet",
        )
    return path


def _file_response(
    path: Path,
    request: Request,
    media_type: str = "image/png",
    extra_headers: dict[str, str] | None = None,
) -> Response:
    """One rendered file with the ETag/max-age/304 contract every tab shares."""
    data = path.read_bytes()
    etag = hashlib.sha256(data).hexdigest()[:ETAG_BYTES]
    inm = request.headers.get("if-none-match")
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=1800",
        "Last-Modified": formatdate(path.stat().st_mtime, usegmt=True),
    }
    headers.update(extra_headers or {})
    if inm and etag in {tag.strip().strip('"') for tag in inm.split(",")}:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type=media_type, headers=headers)


def _png_response(path: Path, request: Request) -> Response:
    return _file_response(path, request, "image/png")


def _build_vis_router(settings: Settings) -> APIRouter:
    """The Visibilities figures router. Every path component is whitelisted."""
    router = APIRouter(prefix="/api/figures/vis", tags=["figures"])

    @router.get("/manifest")
    def manifest(set: str, ref: str) -> dict[str, Any]:
        root = _figures_root(settings)
        outdir = _validated_dir(root, set, ref)
        return _read_manifest(outdir)

    @router.get("/list")
    def list_combos() -> dict[str, Any]:
        """Which (set, ref) combos have at least one rendered manifest."""
        root = _figures_root(settings)
        combos: list[dict[str, Any]] = []
        for set_name in SETS:
            for ref in REFS:
                outdir = root / set_name / ref
                path = outdir / "manifest.json"
                if not path.is_file():
                    continue
                try:
                    manifest_body = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                combos.append(
                    {
                        "set": set_name,
                        "ref": ref,
                        "rendered_utc": manifest_body.get("rendered_utc"),
                        "kinds": manifest_body.get("kinds", []),
                    }
                )
        return {"kinds": list(KINDS), "sets": list(SETS), "refs": list(REFS), "combos": combos}

    @router.get("/{set}/{ref}/{filename}")
    def png(set: str, ref: str, filename: str, request: Request) -> Response:
        root = _figures_root(settings)
        outdir = _validated_dir(root, set, ref)
        path = _validated_png_path(outdir, filename)
        return _png_response(path, request)

    return router


# -- SNAPs figures ------------------------------------------------------
def _snap_figures_root(settings: Settings) -> Path:
    return ensure_contained(
        Path(settings.store_root) / "figures" / "snaps", Path(settings.store_root)
    )


def _validated_snap_dir(root: Path, set_name: str) -> Path:
    if set_name not in SNAP_SETS:
        raise HTTPException(status_code=400, detail=f"set must be one of {'|'.join(SNAP_SETS)}")
    try:
        return ensure_contained(root / safe_name(set_name, "set"), root)
    except UnsafePathError as exc:  # pragma: no cover - set already whitelisted above
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _read_snap_manifest(outdir: Path) -> dict[str, Any]:
    path = outdir / "manifest.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=404,
            detail="no figures rendered yet for this set (the collector may not have run)",
        ) from exc


def _build_snaps_router(settings: Settings) -> APIRouter:
    """The SNAPs figures router. Every path component is whitelisted."""
    router = APIRouter(prefix="/api/figures/snaps", tags=["figures"])

    @router.get("/manifest")
    def manifest(set: str) -> dict[str, Any]:
        root = _snap_figures_root(settings)
        outdir = _validated_snap_dir(root, set)
        return _read_snap_manifest(outdir)

    @router.get("/list")
    def list_combos() -> dict[str, Any]:
        """Which sets have at least one rendered manifest."""
        root = _snap_figures_root(settings)
        combos: list[dict[str, Any]] = []
        for set_name in SNAP_SETS:
            path = root / set_name / "manifest.json"
            if not path.is_file():
                continue
            try:
                manifest_body = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            combos.append(
                {
                    "set": set_name,
                    "rendered_utc": manifest_body.get("rendered_utc"),
                    "board_read_ts": manifest_body.get("board_read_ts"),
                    "kinds": manifest_body.get("kinds", []),
                }
            )
        return {"kinds": list(SNAP_KINDS), "sets": list(SNAP_SETS), "combos": combos}

    @router.get("/{set}/{filename}")
    def png(set: str, filename: str, request: Request) -> Response:
        root = _snap_figures_root(settings)
        outdir = _validated_snap_dir(root, set)
        path = _validated_png_path(outdir, filename, SNAP_KINDS)
        return _png_response(path, request)

    return router


# -- Imaging figures ----------------------------------------------------
# The imaging tree is flat (``store_root/figures/imaging/``): the manifest, the
# four products the manifest names, and a ``frames/`` cache of one PNG per
# imaged integration for the scrub view. Serving is whitelist-driven exactly
# like the other two: the four product names are a literal set and a history
# frame must match ``frames/<digits>@1x.png`` -- the ONE route that takes a
# path with a "/" in it, which is why the pattern is anchored and the digits
# are re-joined into the name rather than the request's own string being
# pasted into a path (docs/api-imaging.md: "never an arbitrary path").
IMAGING_PRODUCTS: frozenset[str] = frozenset(
    {"latest@1x.png", "latest@2x.png", "strip24h@1x.png", "strip24h@2x.png", "allsky24h.mp4"}
)
IMAGING_FRAME_RE = re.compile(r"^frames/(\d{1,12})@1x\.png$")
# The M4 per-source cutouts are not a fixed set of names (which sources are up
# changes every pass), so they are whitelisted against the CURRENT MANIFEST's
# own ``cutouts[].file_1x``/``file_2x`` strings -- exactly what
# docs/api-imaging.md promises ("validated against the whitelist the collector
# actually rendered ... not a glob of the filesystem"). The shape is pinned
# first so a crafted name never reaches the manifest lookup as a path.
IMAGING_CUTOUT_RE = re.compile(r"^cutout_[A-Za-z0-9_.-]+@(?:1x|2x)\.png$")
_MEDIA_TYPES = {".png": "image/png", ".mp4": "video/mp4"}


def _imaging_root(settings: Settings) -> Path:
    return ensure_contained(
        Path(settings.store_root) / "figures" / "imaging", Path(settings.store_root)
    )


def _manifest_cutout_files(root: Path) -> set[str]:
    """The cutout filenames the current manifest actually names."""
    manifest = _read_imaging_manifest(root)
    out: set[str] = set()
    for cut in manifest.get("cutouts") or []:
        for key in ("file_1x", "file_2x"):
            value = cut.get(key)
            if isinstance(value, str):
                out.add(value)
    return out


def _validated_imaging_path(root: Path, filename: str) -> Path:
    if filename in IMAGING_PRODUCTS:
        name = filename
    elif IMAGING_CUTOUT_RE.fullmatch(filename):
        if filename not in _manifest_cutout_files(root):
            raise HTTPException(
                status_code=400,
                detail=f"{filename} is not a cutout the current imaging manifest names",
            )
        name = filename
    else:
        m = IMAGING_FRAME_RE.fullmatch(filename)
        if m is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "file must be one of "
                    f"{'|'.join(sorted(IMAGING_PRODUCTS))}, a cutout_<source>@<1x|2x>.png "
                    "the manifest names, or frames/<unix>@1x.png"
                ),
            )
        name = f"frames/{m.group(1)}@1x.png"
    try:
        path = ensure_contained(root / name, root)
    except UnsafePathError as exc:  # pragma: no cover - name already whitelisted
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"{name} has not been rendered yet")
    return path


def _read_imaging_manifest(root: Path, *, required: bool = False) -> dict[str, Any]:
    """The imaging manifest, or ``{}`` (404 when a route needs it to exist)."""
    try:
        return json.loads((root / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        if required:
            raise HTTPException(
                status_code=404,
                detail="no imaging figures rendered yet (the render job may not have run)",
            ) from exc
        return {}


def _imaging_frames(root: Path) -> list[int]:
    """Unix timestamps of the cached frame PNGs on disk, ascending."""
    frames_dir = root / "frames"
    if not frames_dir.is_dir():
        return []
    out: list[int] = []
    for png in frames_dir.glob("*@1x.png"):
        stem = png.name[: -len("@1x.png")]
        if stem.isdigit():
            out.append(int(stem))
    return sorted(out)


def _build_imaging_router(settings: Settings) -> APIRouter:
    """The Imaging figures router (docs/api-imaging.md). Read-only."""
    router = APIRouter(prefix="/api/figures/imaging", tags=["figures"])

    @router.get("/manifest")
    def manifest() -> dict[str, Any]:
        return _read_imaging_manifest(_imaging_root(settings), required=True)

    # Declared after /manifest so the literal route wins; ``:path`` is what
    # lets a history frame's own ``frames/<unix>@1x.png`` arrive as one value.
    @router.get("/{filename:path}")
    def figure(filename: str, request: Request) -> Response:
        root = _imaging_root(settings)
        path = _validated_imaging_path(root, filename)
        media_type = _MEDIA_TYPES.get(path.suffix, "application/octet-stream")
        # ``Accept-Ranges`` on the movie so a browser's <video> element knows
        # it may seek; the body is served whole (the 24 h mp4 is a few MB).
        extra = {"Accept-Ranges": "bytes"} if path.suffix == ".mp4" else None
        return _file_response(path, request, media_type, extra)

    return router


def _build_imaging_history_router(settings: Settings) -> APIRouter:
    """``GET /api/imaging/history?t0&t1`` -- the scrub view's frame list."""
    router = APIRouter(prefix="/api/imaging", tags=["imaging"])

    @router.get("/history")
    def history(t0: str | None = None, t1: str | None = None) -> dict[str, Any]:
        from ..figures.imaging_figures import parse_utc, utc_iso

        root = _imaging_root(settings)
        try:
            start = parse_utc(t0) if t0 else 0.0
            end = parse_utc(t1) if t1 else float("inf")
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"t0/t1 must be ISO-8601 timestamps: {exc}"
            ) from exc
        frames = [
            {"ts": utc_iso(ts), "ts_unix": ts, "file_1x": f"frames/{ts}@1x.png"}
            for ts in _imaging_frames(root)
            if start <= ts <= end
        ]
        # An empty window is NOT a 404 (docs/api-imaging.md: same "empty series
        # is not an error" convention as the vis/search contracts).
        return {"frames": frames}

    return router


def build_router(settings: Settings) -> APIRouter:
    """The figures router (Vis + SNAPs + Imaging). Read-only; every path
    component is whitelisted against the exact files the render job writes."""
    router = APIRouter()
    router.include_router(_build_vis_router(settings))
    router.include_router(_build_snaps_router(settings))
    router.include_router(_build_imaging_router(settings))
    router.include_router(_build_imaging_history_router(settings))
    return router


__all__ = ["build_router"]
