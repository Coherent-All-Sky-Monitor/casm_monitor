"""Shared PNG-encoding helper for the figure renderers.

A leaf module (no ``casm_monitor`` imports at all) so it can be imported from
both :mod:`casm_monitor.figures.vis_figures` and :mod:`casm_monitor.figures.
snap_figures` without adding any circular-import risk.

Rendering a :class:`~matplotlib.figure.Figure` twice (once per DPI, the
original approach) draws the whole figure -- the expensive part, not the PNG
encode -- twice. Instead this renders ONCE at the higher (2x) DPI and
produces the 1x PNG by downscaling the 2x pixels with Pillow (``LANCZOS``,
matching quality to a native low-DPI render for a regular raster). If Pillow
is not importable, it falls back to a second native ``savefig`` at the low
DPI (slower, but always correct) -- see :data:`PIL_AVAILABLE`.
"""

from __future__ import annotations

import io

from matplotlib.figure import Figure

try:
    from PIL import Image

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only if Pillow is absent
    Image = None  # type: ignore[assignment]
    PIL_AVAILABLE = False


def _savefig_png(fig: Figure, dpi: float, facecolor: str) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=facecolor)
    return buf.getvalue()


def render_pngs(
    fig: Figure, dpi_2x: float, dpi_1x: float, facecolor: str = "#ffffff"
) -> dict[str, bytes]:
    """``{"1x": ..., "2x": ...}`` PNG bytes, drawing the figure only once.

    ``dpi_1x`` is expected to be exactly ``dpi_2x / 2`` (the module constants
    this is called with always are); the downscale factor is derived from the
    ratio rather than hard-coded so a caller that changes the DPIs still gets
    a correct (if non-integer-ratio, PIL ``resize``-based) result.
    """
    png_2x = _savefig_png(fig, dpi_2x, facecolor)
    if not PIL_AVAILABLE:
        return {"1x": _savefig_png(fig, dpi_1x, facecolor), "2x": png_2x}
    ratio = dpi_2x / dpi_1x
    with Image.open(io.BytesIO(png_2x)) as im:
        im.load()
        if ratio == int(ratio) and int(ratio) > 1:
            im_1x = im.reduce(int(ratio))
        else:
            new_size = (max(1, round(im.width / ratio)), max(1, round(im.height / ratio)))
            im_1x = im.resize(new_size, Image.LANCZOS)
        out = io.BytesIO()
        im_1x.save(out, format="PNG")
    return {"1x": out.getvalue(), "2x": png_2x}


__all__ = ["PIL_AVAILABLE", "render_pngs"]
