"""The Visibilities server-rendered figures API.

Read-only: every route reads the ``store_root/figures/vis/<set>/<ref>/`` tree
:mod:`casm_monitor.collectors.figures` writes (manifest + PNGs), and nothing
here ever touches a shard, a board or a file outside that tree. Path
components (``set``, ``ref``, ``kind``) are validated against the exact
whitelists the collector renders (:mod:`casm_monitor.figures.vis_figures`),
never interpolated into a path unchecked, so a crafted ``kind=../../x`` 400s
before any filesystem access.

Caching: every PNG is served with an ``ETag`` (a sha256 prefix of the file's
own bytes), ``Cache-Control: public, max-age=1800`` (the collector's own
render cadence -- a cached copy is never staler than the next render) and
``Last-Modified``; a matching ``If-None-Match`` gets a bare 304. The frontend
appends ``?v=<rendered_utc>`` from the manifest so a new render is a new URL
the browser has never cached, while an unchanged one is a guaranteed cache
hit -- the two together are what makes the "one <img> per view" page fast.
"""

from __future__ import annotations

import hashlib
import json
from email.utils import formatdate
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ..config import Settings
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


def _split_filename(filename: str) -> tuple[str, str]:
    """``"<kind>@<suffix>.png"`` -> ``(kind, suffix)``, whitelisted both ends."""
    if not filename.endswith(".png") or "@" not in filename:
        raise HTTPException(status_code=400, detail="expected <kind>@<1x|2x>.png")
    stem, suffix = filename[: -len(".png")].rsplit("@", 1)
    if stem not in KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {'|'.join(KINDS)}")
    if suffix not in SUFFIXES:
        raise HTTPException(status_code=400, detail="suffix must be 1x or 2x")
    return stem, suffix


def _validated_png_path(outdir: Path, filename: str) -> Path:
    kind, suffix = _split_filename(filename)
    # ``kind``/``suffix`` are already checked against KINDS/SUFFIXES above, so
    # the reconstructed name can contain no "/" or "..": ``ensure_contained``
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
            detail=f"{kind}@{suffix}.png has not been rendered for this set/ref yet",
        )
    return path


def _png_response(path: Path, request: Request) -> Response:
    data = path.read_bytes()
    etag = hashlib.sha256(data).hexdigest()[:ETAG_BYTES]
    inm = request.headers.get("if-none-match")
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=1800",
        "Last-Modified": formatdate(path.stat().st_mtime, usegmt=True),
    }
    if inm and etag in {tag.strip().strip('"') for tag in inm.split(",")}:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="image/png", headers=headers)


def build_router(settings: Settings) -> APIRouter:
    """The figures router. Read-only; every path component is whitelisted."""
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


__all__ = ["build_router"]
