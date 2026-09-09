"""Server-rendered Visibilities figures: matrix, spectra (1D) and autos.

Pure functions: given a :class:`~casm_monitor.store.db.Store` and
:class:`~casm_monitor.config.Settings` they read the last 24 h of cached
visibilities through :class:`casm_monitor.web.vis.VisStore` (read-only,
imported rather than re-implemented -- ``vis.py``/``vis_ops.py`` own the shard
layout and the quantity/reference maths) and return PNG bytes. No FastAPI, no
threading here; :mod:`casm_monitor.collectors.figures` calls these from a
thread pool.

Reused, never copied (per the M2 figures brief): ``vis_ops`` for
quantity/units conversion and channel averaging, ``web.vis.VisStore`` /
``select_baselines`` / ``apply_reference`` for baseline selection and the
raw/sun/cal reference transforms.

Colour and normalisation recipes are the ``casm_vis_analysis`` house plotters'
(``plotting/waterfall.py``, ``plotting/autocorr.py``), reproduced here because
those functions build their own ``plt.subplots`` figures (global pyplot state,
one shared figure manager) and only plot a fixed diagonal-power/cross-phase
pair -- neither signature accepts an arbitrary quantity or a
``Figure``/``FigureCanvasAgg`` object, which this module needs for thread-safe
concurrent rendering. The lines reproduced:

* diagonal autocorrelation power, dB, viridis, ``set_bad("white")``:
  ``waterfall.py`` lines 147-152 (``power_db = 10 * np.log10(np.abs(bl_vis) +
  1e-30)`` on a viridis ``pcolormesh``).
* off-diagonal cross phase, RdBu, fixed ``-pi..pi``: ``waterfall.py`` lines
  156-162.
* real/imag symmetric-about-zero RdBu_r at the 99th percentile of
  ``|values|``: ``waterfall.py`` lines 119-128 (the ``median_recipe`` branch's
  ``vlim = float(np.nanpercentile(np.abs(re), 99)) or 1.0`` /
  ``Normalize(-vlim, vlim)``).
* the 1D diagonal spectrum toggle (``diag_spectra``): ``waterfall.py`` lines
  137-145.
* the autocorrelation grid (dB vs MHz, one panel per input, ``ncols`` grid,
  ``fig.supxlabel``/``fig.supylabel``, ``format_time_range`` header):
  ``autocorr.py`` lines 34-90.

Coherence (0..1, sequential, no house recipe) uses viridis over a fixed
``[0, 1]`` range; amplitude (dB, sequential, no house recipe for a
non-diagonal amplitude matrix) uses viridis over the window's own min/max.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import matplotlib

matplotlib.use("Agg")

import numpy as np
from matplotlib import colormaps
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import Normalize
from matplotlib.figure import Figure

from .. import vis_ops
from ..collectors.vis import STREAM_AVG8, STREAM_FULL, input_sets
from ..config import Settings
from ..store import Store
from ..web.vis import Selection, VisStore, apply_reference, select_baselines

# -- constants ------------------------------------------------------------
SETS: tuple[str, ...] = ("live", "wired")
REFS: tuple[str, ...] = ("raw", "sun", "cal")
QUANTITIES: tuple[str, ...] = vis_ops.QUANTITIES  # ("amp", "phase", "real", "imag", "coh")
MATRIX_KINDS: tuple[str, ...] = tuple(f"matrix_{q}" for q in QUANTITIES)
SPECTRA_KINDS: tuple[str, ...] = tuple(f"spectra_{q}" for q in QUANTITIES)
KINDS: tuple[str, ...] = MATRIX_KINDS + SPECTRA_KINDS + ("autos",)

WINDOW_HOURS = 24.0
PANEL_IN = 1.6
DPI_2X = 110
DPI_1X = 55
LOG_FLOOR = vis_ops.LOG_FLOOR
# Below this many avg8 samples in the 24 h window, fall back to vis_full (the
# operator's "falling back to vis_full when avg8 is thin").
MIN_AVG8_SAMPLES = 20

PAPER = "#ffffff"
INK = "#1f2937"
MUTED = "#6b7280"
HAIRLINE = "#e5e7eb"
SIGNAL = "#2563eb"


class NoData(RuntimeError):
    """Nothing cached yet for this (set, window) -- not a rendering error."""


# -- data loading -----------------------------------------------------------
@dataclass(frozen=True)
class WindowData:
    """24 h of one input set's cached baselines, autos included (rank a==b)."""

    times: np.ndarray          # (T,) unix seconds
    freq_mhz: np.ndarray       # (F,)
    z: np.ndarray              # (T, n_pairs, F) complex64; selection.pairs order
    selection: Selection
    obs: str | None
    stream: str
    t0: float
    t1: float

    @property
    def inputs(self) -> list[int]:
        return self.selection.inputs

    @property
    def antennas(self) -> list[int]:
        return [p + 1 for p in self.selection.inputs]


def load_window(
    store: Store, settings: Settings, set_name: str, hours: float = WINDOW_HOURS
) -> WindowData:
    """Last ``hours`` of the wired/live sub-matrix, autos and crosses both.

    Raises :class:`NoData` (never returns an empty/garbage figure's worth of
    data) when the collector has not cached anything for this set yet.
    """
    vis_store = VisStore(settings, store)
    newest = vis_store.latest()
    if newest is None:
        raise NoData("nothing cached yet")
    stored_inputs = [int(x) for x in newest["inputs"]]
    try:
        sets = input_sets()
    except OSError as exc:
        raise NoData(f"layout unreadable: {exc}") from exc
    subset = [p for p in sets.get(set_name, []) if p in stored_inputs]
    if not subset:
        raise NoData(f"no {set_name} inputs among the cached sub-matrix")
    selection = select_baselines(stored_inputs, subset, "all")
    t1 = time.time()
    t0 = t1 - float(hours) * 3600.0
    z, times, freq, _inputs = vis_store.series(STREAM_AVG8, t0, t1, selection.pair_ids)
    stream = STREAM_AVG8
    if times.size < MIN_AVG8_SAMPLES:
        z_full, times_full, freq_full, _ = vis_store.series(STREAM_FULL, t0, t1, selection.pair_ids)
        if times_full.size > times.size:
            z, times, freq, stream = z_full, times_full, freq_full, STREAM_FULL
    if times.size == 0:
        raise NoData(f"no cached integrations for {set_name} in the last {hours:.0f} h")
    return WindowData(
        times=times, freq_mhz=freq, z=z, selection=selection,
        obs=newest.get("obs"), stream=stream, t0=t0, t1=t1,
    )


def _diag_index(selection: Selection) -> dict[int, int]:
    """rank -> index into ``selection.pairs`` of that rank's autocorrelation."""
    return {a: k for k, (a, b) in enumerate(selection.pairs) if a == b}


@dataclass(frozen=True)
class QuantityData:
    """One (quantity, units, ref) computed over a whole :class:`WindowData`."""

    values: dict[tuple[int, int], np.ndarray]  # (rank_i, rank_j) -> (T, F), units applied
    diag_db: dict[int, np.ndarray]             # rank -> (T, F) autopower, dB (always, per waterfall.py)
    ref_meta: dict[str, Any]


def compute_quantity(
    window: WindowData, quantity: str, units: str, ref: str, settings: Settings
) -> QuantityData:
    """Apply ``ref`` once over the whole cached cube, then form ``quantity``.

    The reference transform (fringe-stop / cal-divide) is linear per baseline
    and a no-op geometry-wise on an autocorrelation (``r_i - r_i = 0``), so one
    ``apply_reference`` call over the combined auto+cross cube is exact for
    both the requested cross quantity and the diagonal's own autopower.
    """
    v_ref, ref_meta = apply_reference(
        window.z, ref, settings=settings, selection=window.selection,
        freq_mhz=window.freq_mhz, times_unix=window.times,
    )
    diag_idx = _diag_index(window.selection)
    auto_power = {a: np.abs(v_ref[:, k, :]).astype(np.float64) for a, k in diag_idx.items()}
    diag_db = {a: 10.0 * np.log10(np.maximum(p, LOG_FLOOR)) for a, p in auto_power.items()}

    values: dict[tuple[int, int], np.ndarray] = {}
    for k, (a, b) in enumerate(window.selection.pairs):
        if a == b:
            continue
        raw = vis_ops.quantity_values(
            v_ref[:, k, :], quantity,
            auto_i=auto_power[a] if quantity == "coh" else None,
            auto_j=auto_power[b] if quantity == "coh" else None,
        )
        values[(a, b)] = vis_ops.apply_units(raw, quantity, units)
    return QuantityData(values=values, diag_db=diag_db, ref_meta=ref_meta)


# -- colour recipes (waterfall.py, cited in the module docstring) -----------
def _cmap(name: str):
    cm = colormaps[name].copy()
    cm.set_bad(PAPER)
    return cm


def _matrix_norm_cmap(quantity: str, units: str, values: dict[tuple[int, int], np.ndarray]):
    if quantity == "phase":
        lim = 180.0 if units == "deg" else float(np.pi)
        return Normalize(-lim, lim), _cmap("RdBu")
    if quantity == "coh":
        return Normalize(0.0, 1.0), _cmap("viridis")
    finite = np.concatenate([v[np.isfinite(v)].ravel() for v in values.values()]) if values else np.array([])
    if quantity == "amp":
        lo, hi = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
        if lo >= hi:
            hi = lo + 1.0
        return Normalize(lo, hi), _cmap("viridis")
    # real, imag: symmetric about zero at the 99th percentile of |values|.
    vlim = float(np.nanpercentile(np.abs(finite), 99)) if finite.size else 1.0
    vlim = vlim or 1.0
    return Normalize(-vlim, vlim), _cmap("RdBu_r")


def _window_title(window: WindowData, quantity: str, units: str, ref: str, set_name: str) -> str:
    t0_iso = datetime.fromtimestamp(window.t0, timezone.utc).strftime("%Y-%m-%d %H:%M")
    t1_iso = datetime.fromtimestamp(window.t1, timezone.utc).strftime("%Y-%m-%d %H:%M")
    return (
        f"{set_name}  {quantity}/{units}  ref={ref}  "
        f"{t0_iso}–{t1_iso} UTC  ({len(window.times)} samples, {window.stream})"
    )


# -- matrix figure ------------------------------------------------------
def render_matrix(
    window: WindowData, quantity: str, units: str, qdata: QuantityData, set_name: str, ref: str
) -> Figure:
    """The upper-triangle waterfall matrix: time (h) x freq (MHz, descending)."""
    n = len(window.inputs)
    fig = Figure(figsize=(PANEL_IN * n, PANEL_IN * n + 0.3))
    FigureCanvasAgg(fig)
    axes = fig.subplots(n, n, squeeze=False)
    time_h = (window.times - window.times[0]) / 3600.0
    norm, cmap = _matrix_norm_cmap(quantity, units, qdata.values)
    diag_cmap = _cmap("viridis")
    for i in range(n):
        for j in range(n):
            ax = axes[i][j]
            if j < i:
                ax.set_visible(False)
                continue
            ax.set_xticks([])
            ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_color(HAIRLINE)
            if i == j:
                ax.pcolormesh(
                    time_h, window.freq_mhz, qdata.diag_db[i].T, cmap=diag_cmap, shading="auto"
                )
                ax.set_title(f"ant {window.antennas[i]}", fontsize=6, color=INK)
            else:
                vals = qdata.values[(i, j)]
                ax.pcolormesh(time_h, window.freq_mhz, vals.T, cmap=cmap, shading="auto", norm=norm)
                ax.set_title(
                    f"ant {window.antennas[i]} × ant {window.antennas[j]}",
                    fontsize=5, color=MUTED,
                )
    fig.text(
        0.5, 0.997, _window_title(window, quantity, units, ref, set_name),
        ha="center", va="top", fontsize=8, color=MUTED,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return fig


# -- 1D spectra figure ----------------------------------------------------
def render_spectra(
    window: WindowData, quantity: str, units: str, qdata: QuantityData, set_name: str, ref: str
) -> Figure:
    """Same triangle; each panel the time-median spectrum, a thin blue line."""
    n = len(window.inputs)
    fig = Figure(figsize=(PANEL_IN * n, PANEL_IN * n + 0.3))
    FigureCanvasAgg(fig)
    axes = fig.subplots(n, n, squeeze=False)
    for i in range(n):
        for j in range(n):
            ax = axes[i][j]
            if j < i:
                ax.set_visible(False)
                continue
            ax.set_xticks([])
            ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_color(HAIRLINE)
            ax.grid(True, color=HAIRLINE, linewidth=0.5, alpha=0.7)
            if i == j:
                y = np.nanmedian(qdata.diag_db[i], axis=0)
                ax.set_title(f"ant {window.antennas[i]}", fontsize=6, color=INK)
            else:
                y = np.nanmedian(qdata.values[(i, j)], axis=0)
                ax.set_title(
                    f"ant {window.antennas[i]} × ant {window.antennas[j]}",
                    fontsize=5, color=MUTED,
                )
            ax.plot(window.freq_mhz, y, color=SIGNAL, linewidth=0.5)
    fig.text(
        0.5, 0.997, _window_title(window, quantity, units, ref, set_name),
        ha="center", va="top", fontsize=8, color=MUTED,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return fig


# -- autos grid ----------------------------------------------------------
def render_autos(window: WindowData, set_name: str, ref: str) -> Figure:
    """dB vs MHz, one panel per input: latest integration + 24 h median."""
    n = len(window.inputs)
    ncols = 4
    nrows = int(np.ceil(n / ncols))
    fig = Figure(figsize=(3.2 * ncols, 2.2 * nrows + 0.4))
    FigureCanvasAgg(fig)
    axes = fig.subplots(nrows, ncols, squeeze=False, sharex=True)
    diag_idx = _diag_index(window.selection)
    latest_power = np.abs(window.z[-1]).astype(np.float64)
    for a, packet_idx in enumerate(window.selection.inputs):
        ax = axes[a // ncols][a % ncols]
        k = diag_idx[a]
        latest_db = 10.0 * np.log10(np.maximum(latest_power[k], LOG_FLOOR))
        median_db = 10.0 * np.log10(
            np.maximum(np.nanmedian(np.abs(window.z[:, k, :]).astype(np.float64), axis=0), LOG_FLOOR)
        )
        ax.plot(window.freq_mhz, median_db, color=MUTED, linewidth=0.5, label="24 h median")
        ax.plot(window.freq_mhz, latest_db, color=SIGNAL, linewidth=0.5, label="latest")
        ax.set_title(f"ant {packet_idx + 1}", fontsize=9, color=INK)
        ax.grid(True, color=HAIRLINE, linewidth=0.5, alpha=0.7)
        for spine in ax.spines.values():
            spine.set_color(HAIRLINE)
    for a in range(n, nrows * ncols):
        axes[a // ncols][a % ncols].set_visible(False)
    handles, labels = axes[0][0].get_legend_handles_labels()
    if handles:
        fig.legend(handles, labels, loc="upper right", fontsize=7, frameon=False)
    fig.supxlabel("Frequency (MHz)", fontsize=9)
    fig.supylabel("Power (dB)", fontsize=9)
    fig.text(
        0.5, 0.997, _window_title(window, "autos", "db", ref, set_name),
        ha="center", va="top", fontsize=8, color=MUTED,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    return fig


# -- PNG bytes ------------------------------------------------------------
def figure_to_png(fig: Figure, dpi: float) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=PAPER)
    return buf.getvalue()


def render_kind(
    store: Store,
    settings: Settings,
    kind: str,
    set_name: str,
    ref: str,
    *,
    window: WindowData | None = None,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Render one (kind, set, ref) combo; returns ``{"1x": png, "2x": png}`` + info.

    ``info`` carries what the collector's manifest needs: ``t0``, ``t1``,
    ``n_integrations``, ``rendered_utc``. ``window`` lets a caller that already
    loaded the (set-only, ref-independent) 24 h window reuse it across every
    ``kind``/``ref`` combination instead of re-reading the shards each time.
    """
    if set_name not in SETS:
        raise ValueError(f"set must be one of {SETS}")
    if ref not in REFS:
        raise ValueError(f"ref must be one of {REFS}")
    if window is None:
        window = load_window(store, settings, set_name)
    if kind == "autos":
        fig = render_autos(window, set_name, ref)
    elif kind in MATRIX_KINDS or kind in SPECTRA_KINDS:
        quantity = kind.split("_", 1)[1]
        units = vis_ops.DEFAULT_UNITS[quantity]
        qdata = compute_quantity(window, quantity, units, ref, settings)
        if kind in MATRIX_KINDS:
            fig = render_matrix(window, quantity, units, qdata, set_name, ref)
        else:
            fig = render_spectra(window, quantity, units, qdata, set_name, ref)
    else:
        raise ValueError(f"unknown figure kind {kind!r}")
    try:
        png_1x = figure_to_png(fig, DPI_1X)
        png_2x = figure_to_png(fig, DPI_2X)
    finally:
        fig.clear()
    info = {
        # The actual span of CACHED data used, not the query boundary
        # (``window.t0``/``window.t1`` is ``[now - 24h, now]``): the newest
        # integration is normally a few minutes behind "now", and the
        # manifest's ``t1`` is also the skip-when-unchanged watermark (a
        # bare "now" would never repeat and the skip would never fire).
        "t0": float(window.times[0]),
        "t1": float(window.times[-1]),
        "n_integrations": int(len(window.times)),
        "stream": window.stream,
        "obs": window.obs,
        "inputs": window.inputs,
    }
    return {"1x": png_1x, "2x": png_2x}, info


__all__ = [
    "DPI_1X",
    "DPI_2X",
    "KINDS",
    "MATRIX_KINDS",
    "MIN_AVG8_SAMPLES",
    "NoData",
    "PANEL_IN",
    "QuantityData",
    "REFS",
    "SETS",
    "SPECTRA_KINDS",
    "WINDOW_HOURS",
    "WindowData",
    "compute_quantity",
    "figure_to_png",
    "load_window",
    "render_autos",
    "render_kind",
    "render_matrix",
    "render_spectra",
]
