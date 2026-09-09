"""Server-rendered Imaging figures: the all-sky dirty image per integration.

Pure functions, same contract as :mod:`casm_monitor.figures.vis_figures` and
:mod:`casm_monitor.figures.snap_figures`: given a
:class:`~casm_monitor.store.db.Store` and :class:`~casm_monitor.config.Settings`
they produce PNG bytes (and, for the movie, one MP4 on disk). No FastAPI here;
:mod:`casm_monitor.jobs.render_figures` calls these from the job process and
owns the frame cache, the manifest and the retention.

Reused, never rewritten (plan.md "Reuse, never rewrite"):
:func:`casm_imaging.imaging.allsky_snapshots` does every bit of the imaging --
layout, flat-baseline indexing, cal application, bandpass normalisation,
channel averaging and the hemisphere beamform -- and
:func:`casm_imaging.imaging.render_allsky_movie` renders the 24 h movie.
:func:`casm_vis_analysis.sources.source_altaz` answers "is it up", so the
frontend does no astronomy (docs/api-imaging.md).

The CAL is the DEPLOYED one, never a hardcoded path: the cal file comes from
``deployed_weights.csv``'s last row (the standing authority, casm-wiki
``deployed_weights.csv``) through :func:`casm_monitor.cal_defaults.
deployed_product`, the antenna set from the slots actually populated in the
deployed CB weights file (:func:`casm_monitor.cal_defaults.
deployed_cb_antennas`), and ``inactive_antennas`` is derived as "every WIRED
antenna of the layout that is not in that set" -- computed from the layout CSV
at render time, so a ``casm-layout apply`` or a new deployed product changes it
by itself. (The measured 2026-09-09 value of that derivation is
``(1, 3, 7, 12, 14, 27, 33)``, which is exactly what the M4 benchmark used;
it is never written down here.)

Cost, MEASURED 2026-09-09 (plan.md Architecture): 5 snapshots in 26 s at
241 px / 17 antennas / ``freq_avg=32`` / 8 workers, peak RSS 142 MB, i.e.
~5 s per integration. One integration is 137.44 s, so imaging every
integration is ~1 CPU-hour/day and a 64-integration catch-up pass is a few
minutes, well inside the ``render_figures`` timeout.

Drawing style is the monitor's own (white paper, muted annotation), not
``plot_allsky_frame``'s notebook style: that function takes an ``Axes`` but
draws a per-frame peak annotation, local-time titles and grey azimuth labels
sized for a 7.4 in notebook figure, and the strip needs the same frame at
thumbnail scale with none of it. The geometry it encodes -- horizon = unit
circle, radius = cos(alt) altitude rings at 20/40/60 deg, North up and East
LEFT (inverted x) -- is reproduced exactly, and :func:`casm_imaging.imaging.
allsky.altaz_to_lm` is imported rather than re-derived.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")

import numpy as np
from matplotlib import colormaps, patheffects
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.ticker import ScalarFormatter

from ..config import Settings
from ..store import Store
from ._png import render_pngs

# -- constants -------------------------------------------------------------
#: One correlator integration, seconds (the ``layout_64ant`` value the vis
#: collector and the status strip already grade against, config.py
#: ``vis_integration``). The imaging cadence IS the integration cadence:
#: ``allsky_snapshots`` steps in minutes, so this is the step it is given.
INTEGRATION_S = 137.438953472
EVERY_MINUTES = INTEGRATION_S / 60.0

WINDOW_HOURS = 24.0
#: Frames kept on disk (the scrub view's history), days.
FRAME_RETENTION_DAYS = 7.0

NPIX = 241
FREQ_AVG = 32
MIN_BASELINE_M = 5.0
NORMALIZE_BANDPASS = True
#: ``"abs"`` -- the non-negative coherence map, so a sequential colormap
#: (viridis) is the right one; ``"real"`` would need a diverging one.
ESTIMATOR = "abs"
#: Fixed order, exactly as docs/api-imaging.md specifies for ``sources``.
SOURCES: tuple[str, ...] = ("sun", "cyg-a", "cas-a", "tau-a")
#: Snapshot workers. 8 is the benchmarked value (26 s / 5 snapshots) and sits
#: inside the jobs unit's 1600% CPUQuota alongside everything else it runs.
WORKERS = 8

#: ``allsky_snapshots`` speaks local ISO time (it stamps ``time_tz`` onto a
#: naive datetime); the monitor speaks unix/UTC everywhere else, so the
#: conversion happens once, here.
DATA_TZ = "America/Los_Angeles"
DATA_ROOT = "/mnt"
FMT = "layout_64ant"

#: Thumbnails in the 24 h strip: one per 30 min.
STRIP_SLOT_S = 1800.0
STRIP_MAX = 48
#: Frames in the 24 h movie. A full day at the integration cadence is ~629
#: frames and matplotlib draws each one; subsampled to keep the movie render a
#: minute rather than five, and 240 frames at 4 fps is still a 60 s movie.
MOVIE_MAX_FRAMES = 240
MOVIE_FPS = 4
MOVIE_NAME = "allsky24h"

DPI_2X = 110
DPI_1X = 55
LATEST_IN = 7.0
THUMB_IN = 0.62
STRIP_HEIGHT_IN = 1.05

PAPER = "#ffffff"
INK = "#1f2937"
MUTED = "#6b7280"
HAIRLINE = "#e5e7eb"

ALT_RINGS_DEG = (20.0, 40.0, 60.0)


class ImagingUnavailable(RuntimeError):
    """No deployed cal/antenna set to image with -- not a rendering error.

    Raised (and reported as a skip, never as a fake image) when the ledger has
    no cal file, the file is gone, or the deployed CB weights cannot be read
    for their populated slots.
    """


# -- what to image with ----------------------------------------------------
@dataclass(frozen=True)
class ImagingConfig:
    """The deployed cal and the antenna sets derived from it, per render."""

    cal_path: Path
    cal_file: str            # basename only, as docs/api-imaging.md wants
    weights_file: str | None
    antennas: tuple[int, ...]         # the deployed CB set
    inactive_antennas: tuple[int, ...]  # wired minus the CB set
    wired_antennas: tuple[int, ...]
    antennas_source: str


def imaging_config(settings: Settings) -> ImagingConfig:
    """The deployed cal + the antenna sets to image with, all derived.

    Nothing here is hardcoded: the cal path is the ledger's last row, the
    active set is the deployed CB file's populated slots, and the inactive set
    is the layout's wired antennas minus that set.
    """
    from ..cal_defaults import deployed_cb_antennas, deployed_product, layout_info

    deployed = deployed_product(settings)
    cal = deployed.get("cal_file")
    if not cal:
        raise ImagingUnavailable(
            "deployed_weights.csv's last row names no cal file; nothing to image with"
        )
    cal_path = Path(cal)
    if not cal_path.is_file():
        raise ImagingUnavailable(f"deployed cal {cal_path} does not exist")
    cb = deployed_cb_antennas(deployed.get("weights_file"))
    antennas = [int(a) for a in cb["antennas"]]
    if not antennas:
        raise ImagingUnavailable(
            f"deployed CB weights unreadable ({cb['error']}); refusing to guess an "
            "antenna set to image with"
        )
    layout = layout_info(settings.layout_csv)
    wired = [int(a) for a in layout["wired_antennas"]]
    inactive = sorted(set(wired) - set(antennas))
    return ImagingConfig(
        cal_path=cal_path,
        cal_file=cal_path.name,
        weights_file=deployed.get("weights_file"),
        antennas=tuple(sorted(antennas)),
        inactive_antennas=tuple(inactive),
        wired_antennas=tuple(sorted(wired)),
        antennas_source="deployed",
    )


# -- time helpers ----------------------------------------------------------
def _local_iso(ts: float) -> str:
    """Unix seconds -> the naive local ISO string ``allsky_snapshots`` wants.

    It does ``datetime.fromisoformat(s).replace(tzinfo=ZoneInfo(time_tz))``, so
    the string must be NAIVE local time -- an aware one would have its offset
    silently overwritten.
    """
    return datetime.fromtimestamp(ts, ZoneInfo(DATA_TZ)).replace(tzinfo=None).isoformat()


def utc_iso(ts: float) -> str:
    """Unix seconds -> ``YYYY-MM-DDTHH:MM:SSZ`` (the imaging contract's format)."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> float:
    """ISO-8601 (``Z`` or offset, or naive = UTC) -> unix seconds."""
    s = str(text).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# -- imaging ---------------------------------------------------------------
def image_window(
    store: Store | None,
    settings: Settings,
    t0: float,
    t1: float,
    *,
    config: ImagingConfig | None = None,
    workers: int = WORKERS,
    every_minutes: float = EVERY_MINUTES,
) -> list[dict[str, Any]]:
    """All-sky snapshots for ``[t0, t1)`` (unix seconds), one per integration.

    A thin, honest wrapper: every imaging decision is
    :func:`casm_imaging.imaging.allsky_snapshots`'s, and the only thing added
    is the deployed-cal/antenna derivation (:func:`imaging_config`) and the
    unix -> local-ISO conversion its signature wants. Steps that land in a
    data gap are dropped by that function, so the returned list can be shorter
    than the requested grid -- never padded with anything invented.
    """
    from casm_imaging.imaging import allsky_snapshots

    cfg = config or imaging_config(settings)
    started = time.time()
    snaps = allsky_snapshots(
        _local_iso(t0),
        _local_iso(t1),
        every_minutes,
        cal_h5=str(cfg.cal_path),
        inactive_antennas=tuple(cfg.inactive_antennas),
        data_root=DATA_ROOT,
        fmt=FMT,
        time_tz=DATA_TZ,
        min_baseline_m=MIN_BASELINE_M,
        normalize_bandpass=NORMALIZE_BANDPASS,
        freq_avg=FREQ_AVG,
        npix=NPIX,
        estimator=ESTIMATOR,
        sources=SOURCES,
        workers=int(workers),
        verbose=False,
    )
    if store is not None:
        store.put_scalar("figures.imaging.snapshot_s", round(time.time() - started, 3))
        store.put_scalar("figures.imaging.n_snapshots", len(snaps))
    return list(snaps)


def source_marks(ts: float, sources: tuple[str, ...] = SOURCES) -> list[dict[str, Any]]:
    """``sources`` for the manifest: alt/az now, fixed order, ``up = alt > 0``.

    Server-computed on purpose (docs/api-imaging.md): the frontend does no
    astronomy.
    """
    from casm_vis_analysis.sources import source_altaz

    out: list[dict[str, Any]] = []
    for name in sources:
        alt, az = source_altaz(name, np.array([float(ts)]))
        alt_deg = float(np.asarray(alt).ravel()[0])
        az_deg = float(np.asarray(az).ravel()[0])
        out.append(
            {
                "name": name,
                "alt_deg": round(alt_deg, 2),
                "az_deg": round(az_deg, 2),
                "up": bool(alt_deg > 0.0),
            }
        )
    return out


# -- drawing ---------------------------------------------------------------
def _viridis():
    cmap = colormaps["viridis"].copy()
    # Outside the horizon circle the image is NaN; paint it paper, not the
    # colormap's bottom colour, so the disc reads as the sky and not as a
    # square panel (same ``set_bad`` idea as the vis waterfalls).
    cmap.set_bad(PAPER)
    return cmap


def _draw_frame(
    ax: Any,
    snapshot: dict[str, Any],
    *,
    thumbnail: bool = False,
    label_sources: bool = True,
) -> Any:
    """One all-sky frame: viridis disc, horizon, altitude rings, source marks.

    North up, East LEFT (the x axis is inverted) -- the looking-up sky view
    ``casm_imaging.imaging.allsky.plot_allsky_frame`` uses; the altitude rings
    are at radius ``cos(alt)`` for the same reason (the projection is
    ``l = cos(alt) sin(az)``, ``m = cos(alt) cos(az)``).
    """
    from casm_imaging.imaging.allsky import altaz_to_lm

    img = np.asarray(snapshot["image"], dtype=float)
    finite = np.isfinite(img)
    vmax = float(np.nanmax(img)) if finite.any() else 1.0
    im = ax.imshow(
        img,
        origin="lower",
        cmap=_viridis(),
        vmin=0.0,
        vmax=vmax if vmax > 0 else 1.0,
        extent=[-1, 1, -1, 1],
        interpolation="nearest",
    )
    theta = np.linspace(0.0, 2 * np.pi, 241)
    ax.plot(np.cos(theta), np.sin(theta), color="w", lw=0.9 if not thumbnail else 0.5, alpha=0.75)
    for alt in ALT_RINGS_DEG:
        r = float(np.cos(np.deg2rad(alt)))
        ax.plot(
            r * np.cos(theta), r * np.sin(theta),
            color="w", lw=0.5 if not thumbnail else 0.3, ls=":", alpha=0.5,
        )
        if not thumbnail:
            ax.text(0.02, r, f"{alt:.0f}°", color="w", fontsize=7,
                    ha="left", va="bottom", alpha=0.8)
    ax.plot(0, 0, "+", color="w", ms=6 if not thumbnail else 3, alpha=0.7)

    if not thumbnail:
        for az_deg in range(0, 360, 90):
            l, m = altaz_to_lm(0.0, az_deg)
            ax.text(
                1.13 * float(l), 1.13 * float(m), {0: "N", 90: "E", 180: "S", 270: "W"}[az_deg],
                color=MUTED, fontsize=8, ha="center", va="center",
            )

    for name, (l, m, alt) in (snapshot.get("sources") or {}).items():
        if float(alt) <= 0:
            continue
        ax.plot(
            float(l), float(m), "o",
            ms=11 if not thumbnail else 4.5, mfc="none", mec="w",
            mew=1.2 if not thumbnail else 0.7,
        )
        if label_sources and not thumbnail:
            text = ax.annotate(
                name, (float(l), float(m)), xytext=(7, 6), textcoords="offset points",
                color=MUTED, fontsize=8,
            )
            # Muted grey is the house colour for an annotation, but half of
            # these labels land on a bright viridis peak (that is the point of
            # the marker); a thin paper-coloured stroke keeps the colour and
            # makes it legible on any background.
            text.set_path_effects(
                [patheffects.withStroke(linewidth=2.0, foreground=PAPER, alpha=0.85)]
            )

    lim = 1.2 if not thumbnail else 1.05
    ax.set_xlim(lim, -lim)   # East on the LEFT
    ax.set_ylim(-lim, lim)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for side in ax.spines.values():
        side.set_visible(False)
    ax.set_facecolor(PAPER)
    return im


def render_latest(snapshot: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, bytes]:
    """The newest integration's all-sky image, ``{"1x": ..., "2x": ...}``.

    ``meta`` carries the title's provenance: ``cal_file`` (basename) and
    ``n_ant``. Drawn ONCE at 2x and downscaled for 1x (``_png.render_pngs``),
    the same single-render rule the vis/SNAP figures follow.
    """
    meta = meta or {}
    fig = Figure(figsize=(LATEST_IN, LATEST_IN + 0.35), facecolor=PAPER)
    FigureCanvasAgg(fig)
    # Explicit axes rather than a colorbar stolen out of the image axes: the
    # frame is a disc on a square axes, so letting the colorbar eat 5% of it
    # squashes the sky and clips the tick labels.
    ax = fig.add_axes([0.01, 0.02, 0.84, 0.90])
    cax = fig.add_axes([0.885, 0.14, 0.022, 0.66])
    im = _draw_frame(ax, snapshot)
    bar = fig.colorbar(im, cax=cax)
    # The beam response is ~1e-4 in the imager's units; a plain tick label is
    # then all zeros, so the exponent is carried in the offset text.
    fmt = ScalarFormatter(useMathText=True)
    fmt.set_powerlimits((-2, 3))
    bar.ax.yaxis.set_major_formatter(fmt)
    bar.ax.yaxis.get_offset_text().set(color=MUTED, fontsize=7)
    bar.ax.tick_params(labelsize=7, colors=MUTED, length=2)
    bar.outline.set_visible(False)
    bar.set_label("beam response", color=MUTED, fontsize=8)

    bits = [f"all-sky {utc_iso(float(snapshot['time_unix']))}"]
    if meta.get("cal_file"):
        bits.append(f"cal {meta['cal_file']}")
    if meta.get("n_ant"):
        bits.append(f"{int(meta['n_ant'])} antennas")
    fig.text(0.02, 0.975, "  ·  ".join(bits), color=MUTED, fontsize=9, va="top")
    return render_pngs(fig, DPI_2X, DPI_1X, facecolor=PAPER)


def render_strip(snapshots: list[dict[str, Any]]) -> dict[str, bytes]:
    """One row of up to :data:`STRIP_MAX` thumbnails with UTC hour labels.

    A single PNG, not N files (docs/api-imaging.md: "``strip`` is a single
    server-rendered PNG that is itself a row of thumbnails"). Each thumbnail
    autoscales to its own maximum, so a night-time Cyg A frame is readable
    next to a daytime Sun frame ~100x brighter (the same trade-off
    ``render_allsky_movie``'s default ``scale="frame"`` makes).
    """
    snaps = list(snapshots)[:STRIP_MAX]
    n = max(1, len(snaps))
    fig = Figure(figsize=(THUMB_IN * n, STRIP_HEIGHT_IN), facecolor=PAPER)
    FigureCanvasAgg(fig)
    if not snaps:
        ax = fig.add_subplot(111)
        ax.axis("off")
        ax.text(0.5, 0.5, "no integrations cached yet", ha="center", va="center",
                fontsize=10, color=MUTED)
        return render_pngs(fig, DPI_2X, DPI_1X, facecolor=PAPER)

    axes = fig.subplots(1, n, squeeze=False)[0]
    for ax, snap in zip(axes, snaps):
        _draw_frame(ax, snap, thumbnail=True)
    for ax in axes[len(snaps):]:
        ax.axis("off")
    # One hour label per thumbnail whose UTC hour differs from its left
    # neighbour's: a 30-min strip would otherwise print every hour twice.
    prev_hour = None
    for ax, snap in zip(axes, snaps):
        when = datetime.fromtimestamp(float(snap["time_unix"]), timezone.utc)
        label = f"{when.hour:02d}" if when.hour != prev_hour else ""
        prev_hour = when.hour
        ax.set_xlabel(label, color=MUTED, fontsize=7, labelpad=2)
    fig.subplots_adjust(left=0.002, right=0.998, top=0.97, bottom=0.16, wspace=0.02)
    fig.text(0.002, 0.02, "UTC hour", color=MUTED, fontsize=7, va="bottom")
    return render_pngs(fig, DPI_2X, DPI_1X, facecolor=PAPER)


def render_frame(snapshot: dict[str, Any]) -> bytes:
    """One small per-integration PNG for the scrub view (1x only).

    docs/api-imaging.md: history frames have no ``@2x`` -- "scrub is a
    browsing tool, not the open-full-size target" -- so this returns bare
    bytes, not a ``{1x, 2x}`` pair.
    """
    fig = Figure(figsize=(3.2, 3.4), facecolor=PAPER)
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0.02, 0.02, 0.96, 0.88])
    _draw_frame(ax, snapshot)
    fig.text(
        0.5, 0.975, utc_iso(float(snapshot["time_unix"])),
        color=MUTED, fontsize=8, ha="center", va="top",
    )
    import io

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI_1X * 2, facecolor=PAPER)
    return buf.getvalue()


def ffmpeg_path() -> str | None:
    """Where ffmpeg is, or None.

    ``shutil.which`` first (the documented check), then the interpreter's own
    base installation: the service units run the venv's ``casm-monitor-jobs``
    console script under systemd's default PATH, which does NOT contain
    miniconda's ``bin`` -- so a ``which`` alone finds nothing under systemd
    while finding it in every interactive shell, and the movie would be
    permanently null in production and always present in testing.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    import sys

    for base in (Path(sys.executable).parent, Path(sys.base_prefix) / "bin"):
        candidate = base / "ffmpeg"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def ffmpeg_available() -> bool:
    return ffmpeg_path() is not None


def render_movie(
    snapshots: list[dict[str, Any]], outdir: Path, *, fps: int = MOVIE_FPS
) -> Path | None:
    """``allsky24h.mp4`` in ``outdir``, or None when ffmpeg is not installed.

    Delegates entirely to :func:`casm_imaging.imaging.render_allsky_movie`
    (the house movie renderer, per-frame colour scaling). It falls back to a
    GIF of its own accord when ffmpeg is missing, which the API contract has
    no name for, so the ffmpeg check happens HERE and the manifest carries
    ``movie: null`` instead.
    """
    exe = ffmpeg_path()
    if not snapshots or exe is None:
        return None
    from casm_imaging.imaging import render_allsky_movie

    # ``FFMpegWriter`` resolves this rcParam through PATH by default, which is
    # the same PATH ``ffmpeg_path`` had to work around; point it at the
    # resolved binary so the writer cannot fall back to the GIF branch.
    matplotlib.rcParams["animation.ffmpeg_path"] = exe

    step = max(1, int(np.ceil(len(snapshots) / MOVIE_MAX_FRAMES)))
    frames = list(snapshots)[::step]
    out = render_allsky_movie(frames, Path(outdir) / MOVIE_NAME, fps=int(fps))
    out = Path(out)
    return out if out.suffix == ".mp4" and out.is_file() else None


__all__ = [
    "DPI_1X",
    "DPI_2X",
    "EVERY_MINUTES",
    "FRAME_RETENTION_DAYS",
    "INTEGRATION_S",
    "MOVIE_FPS",
    "MOVIE_NAME",
    "SOURCES",
    "STRIP_MAX",
    "STRIP_SLOT_S",
    "WINDOW_HOURS",
    "WORKERS",
    "ImagingConfig",
    "ImagingUnavailable",
    "ffmpeg_available",
    "ffmpeg_path",
    "image_window",
    "imaging_config",
    "parse_utc",
    "render_frame",
    "render_latest",
    "render_movie",
    "render_strip",
    "source_marks",
    "utc_iso",
]
