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
from typing import TYPE_CHECKING, Any

import matplotlib

matplotlib.use("Agg")

import numpy as np
from matplotlib import colormaps
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import Normalize
from matplotlib.figure import Figure

from .. import vis_ops
from ..collectors.vis import STREAM_AVG8, input_sets
from ..config import Settings
from ..store import Store
from ._png import render_pngs

# ``..web.vis`` (and everything it drags in -- ``casm_monitor.web``, whose
# ``__init__`` imports ``.app``, which imports ``.web.figures``, which
# imports THIS module for its ``KINDS``/``REFS``/``SETS`` constants) is
# imported lazily inside the functions that actually call it, not at module
# scope: a module-level import here made ``casm_monitor.figures.vis_figures``
# unimportable first in a fresh interpreter (circular import through
# ``web.figures``). ``from __future__ import annotations`` means the
# ``Selection`` type hints below never need the real symbol at runtime, only
# for static type checkers (``TYPE_CHECKING``).
if TYPE_CHECKING:
    from ..web.vis import Selection

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
# Memory bound (2026-09-09 OOM fix): the figures job NEVER reads vis_full (4.6
# GB/set) and never concatenates a whole vis_avg8 window (580 MB/set) either.
# Every avg8 shard is loaded, channel-block-averaged down to NCHAN_FIG and
# folded into at most MAX_TIME_BINS time bins (accumulated as running linear
# sums, same recipe as ``VisStore.coherence_accumulate``), then dropped -- peak
# memory is one shard's selected rows plus the small (bins x pairs x
# NCHAN_FIG) accumulator, never the whole window.
NCHAN_FIG = 96
MAX_TIME_BINS = 288  # 24 h / 5 min
# Below this many raw avg8 integrations in the window there is not enough
# signal to render anything meaningful; render a placeholder instead (NEVER
# fall back to vis_full to get more).
MIN_INTEGRATIONS = 6

PAPER = "#ffffff"
INK = "#1f2937"
MUTED = "#6b7280"
HAIRLINE = "#e5e7eb"
SIGNAL = "#2563eb"


class NoData(RuntimeError):
    """Nothing cached yet for this (set, window) -- not a rendering error."""


class NotEnoughData(RuntimeError):
    """Cached, but fewer than :data:`MIN_INTEGRATIONS` avg8 samples exist.

    NEVER handled by falling back to ``vis_full`` (that fallback was the 4.6
    GB/set OOM path, 2026-09-09) -- the caller renders a placeholder instead.
    """


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


def _shard_times(shard: dict[str, Any]) -> np.ndarray:
    meta = shard.get("meta") or {}
    times = [float(t) for t in (meta.get("t") or [])]
    return np.asarray(times or [float(shard["t0"])], dtype=np.float64)


def load_window(
    store: Store, settings: Settings, set_name: str, hours: float = WINDOW_HOURS
) -> WindowData:
    """Last ``hours`` of the wired/live sub-matrix, autos and crosses both.

    Reads ``vis_avg8`` ONLY (never ``vis_full``, the 4.6 GB/set OOM path,
    2026-09-09), one shard at a time, reduced immediately to at most
    :data:`MAX_TIME_BINS` time bins x :data:`NCHAN_FIG` channels per baseline
    (accumulated as running linear sums, converted once at the end) so peak
    memory is one shard's selected rows, never the whole window.

    Raises :class:`NoData` (nothing cached yet for this set) or
    :class:`NotEnoughData` (cached, but fewer than :data:`MIN_INTEGRATIONS`
    avg8 samples in the window) -- never returns an empty/garbage figure's
    worth of data, and never falls back to ``vis_full`` to get more.
    """
    from casm_io.correlator.baselines import triu_flat_index

    from ..web.vis import VisStore, freq_axis, load_baseline_rows, select_baselines

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
    pair_ids = selection.pair_ids
    n_pairs = len(pair_ids)

    t1 = time.time()
    t0 = t1 - float(hours) * 3600.0
    bin_width = float(hours) * 3600.0 / MAX_TIME_BINS

    sum_re = np.zeros((MAX_TIME_BINS, n_pairs, NCHAN_FIG), dtype=np.float64)
    sum_im = np.zeros((MAX_TIME_BINS, n_pairs, NCHAN_FIG), dtype=np.float64)
    cnt = np.zeros((MAX_TIME_BINS, n_pairs), dtype=np.int64)
    # Bin CENTRE timestamps are data-derived (the mean of the raw sample
    # timestamps that landed in each bin), not ``t0 + idx*bin_width``: the
    # latter depends on ``t0 = time.time() - hours*3600`` at query time, which
    # ticks forward every call even when the cached data has not changed, so
    # it would break the skip-when-unchanged watermark (``manifest.t1`` would
    # never repeat) every single pass.
    sum_t = np.zeros(MAX_TIME_BINS, dtype=np.float64)
    n_bin_samples = np.zeros(MAX_TIME_BINS, dtype=np.int64)
    freq_out: np.ndarray | None = None
    n_integrations = 0

    for shard in vis_store.shards.list(STREAM_AVG8, t0=t0, t1=t1):
        meta = shard["meta"] or {}
        shard_inputs = [int(i) for i in (meta.get("inputs") or [])]
        if not shard_inputs:
            continue
        times = _shard_times(shard)
        keep = (times >= t0) & (times <= t1)
        if not keep.any():
            continue
        rank_of = {p: k for k, p in enumerate(shard_inputs)}
        n_shard = len(shard_inputs)
        flat: list[int] = []
        missing: list[int] = []
        for k, (pi, pj) in enumerate(pair_ids):
            ri, rj = rank_of.get(pi), rank_of.get(pj)
            if ri is None or rj is None:
                flat.append(0)
                missing.append(k)
            else:
                a, b = (ri, rj) if ri <= rj else (rj, ri)
                flat.append(triu_flat_index(n_shard, a, b))
        cube = load_baseline_rows(shard, flat)  # (T, n_pairs, F) -- this shard only
        if times.size != cube.shape[0]:
            times = np.linspace(shard["t0"], shard["t1"], cube.shape[0])
            keep = (times >= t0) & (times <= t1)
            if not keep.any():
                continue
        cube = np.asarray(cube[keep], dtype=np.complex64)
        times_k = times[keep]
        n_integrations += int(times_k.size)
        if missing:
            cube[:, missing, :] = np.nan
        if freq_out is None:
            freq_full = freq_axis(meta, cube.shape[-1])
            freq_out = vis_ops.block_mean(freq_full, NCHAN_FIG)
        reduced = vis_ops.block_mean(cube, NCHAN_FIG)  # (T, n_pairs, NCHAN_FIG)
        good = np.isfinite(reduced.real) & np.isfinite(reduced.imag)
        reduced = np.where(good, reduced, 0)
        bin_idx = np.clip(((times_k - t0) / bin_width).astype(np.int64), 0, MAX_TIME_BINS - 1)
        for local_t, b in enumerate(bin_idx):
            sum_re[b] += reduced[local_t].real
            sum_im[b] += reduced[local_t].imag
            cnt[b] += good[local_t].any(axis=-1)
            sum_t[b] += times_k[local_t]
            n_bin_samples[b] += 1
        del cube, reduced

    if n_integrations == 0:
        raise NoData(f"no cached integrations for {set_name} in the last {hours:.0f} h")
    if n_integrations < MIN_INTEGRATIONS:
        raise NotEnoughData(
            f"only {n_integrations} avg8 integration(s) cached for {set_name} in the "
            f"last {hours:.0f} h (need {MIN_INTEGRATIONS})"
        )

    have_bin = cnt.max(axis=1) > 0
    denom = np.maximum(cnt, 1)[..., None].astype(np.float64)
    z = ((sum_re / denom) + 1j * (sum_im / denom)).astype(np.complex64)
    z = np.where(cnt[..., None] > 0, z, np.nan)
    bin_times = sum_t / np.maximum(n_bin_samples, 1)
    return WindowData(
        times=bin_times[have_bin],
        freq_mhz=freq_out if freq_out is not None else np.zeros(0),
        z=z[have_bin],
        selection=selection,
        obs=newest.get("obs"),
        stream=STREAM_AVG8,
        t0=t0,
        t1=t1,
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
    from ..web.vis import apply_reference

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
def _time_freq_extent(time_h: np.ndarray, freq_mhz: np.ndarray) -> tuple[float, float, float, float]:
    """``(left, right, bottom, top)`` for an ``imshow`` with ``origin="upper"``.

    ``freq_mhz`` is always descending (``freq_axis``'s own contract, see its
    docstring) so ``freq_mhz[0]`` -- the highest frequency, row 0 of every
    ``(F, T)`` image array here -- belongs at the TOP of the image, which is
    what ``origin="upper"`` does with the extent's own top value. Degenerate
    (single-sample/single-channel) windows get a 1-unit-wide box rather than
    a zero-area extent, matching ``pcolormesh``'s own tolerance of that case.
    """
    t0, t1 = float(time_h[0]), float(time_h[-1])
    if t1 <= t0:
        t1 = t0 + 1.0
    f_lo, f_hi = float(freq_mhz[-1]), float(freq_mhz[0])
    if f_hi <= f_lo:
        f_hi = f_lo + 1.0
    return (t0, t1, f_lo, f_hi)


def render_matrix(
    window: WindowData, quantity: str, units: str, qdata: QuantityData, set_name: str, ref: str
) -> Figure:
    """The upper-triangle waterfall matrix: time (h) x freq (MHz, descending).

    ``imshow`` (not ``pcolormesh``) over an explicit ``extent``: identical
    visual result on this module's regular (uniform time-bin, uniform
    channel) grids, but an order of magnitude cheaper to rasterize -- a
    ``QuadMesh`` builds and paints one polygon per cell, an ``AxesImage``
    blits one array. Every panel gets the SAME fixed ``extent``, so there is
    nothing to autoscale/share: panels are deliberately NOT ``sharex``/
    ``sharey``-linked (measured 2026-09-08: linking ~300 panels' axes through
    matplotlib's ``Grouper`` cost 16 s of pure bookkeeping before a single
    pixel was drawn -- each join walks the whole existing group, so it is
    quadratic in panel count -- while setting each axis's limits directly
    from ``extent`` is O(1) per panel and gives the identical fixed view).
    Only the lower triangle is skipped entirely (no ``Axes`` object ever
    created for it, rather than created-then-hidden). Panel labels are
    lightweight ``ax.text`` calls, not ``set_title`` (title placement costs a
    text-extent computation that ``fig.tight_layout`` -- also dropped in
    favour of one fixed ``subplots_adjust`` -- would otherwise redo per
    panel).
    """
    n = len(window.inputs)
    fig = Figure(figsize=(PANEL_IN * n, PANEL_IN * n + 0.3))
    FigureCanvasAgg(fig)
    gs = fig.add_gridspec(n, n, wspace=0.06, hspace=0.06)
    time_h = (window.times - window.times[0]) / 3600.0
    extent = _time_freq_extent(time_h, window.freq_mhz)
    norm, cmap = _matrix_norm_cmap(quantity, units, qdata.values)
    diag_cmap = _cmap("viridis")
    for i in range(n):
        for j in range(i, n):
            ax = fig.add_subplot(gs[i, j])
            ax.set_xticks([])
            ax.set_yticks([])
            ax.set_xlim(extent[0], extent[1])
            ax.set_ylim(extent[2], extent[3])
            for spine in ax.spines.values():
                spine.set_color(HAIRLINE)
            if i == j:
                img, use_cmap, use_norm = qdata.diag_db[i], diag_cmap, None
                label, color = f"ant {window.antennas[i]}", INK
            else:
                img, use_cmap, use_norm = qdata.values[(i, j)], cmap, norm
                label = f"ant {window.antennas[i]} × ant {window.antennas[j]}"
                color = MUTED
            ax.imshow(
                img.T, cmap=use_cmap, norm=use_norm, extent=extent, origin="upper",
                aspect="auto", interpolation="nearest", rasterized=True,
            )
            ax.text(
                0.04, 0.93, label, transform=ax.transAxes, ha="left", va="top",
                fontsize=6 if i == j else 5, color=color,
            )
    fig.text(
        0.5, 0.997, _window_title(window, quantity, units, ref, set_name),
        ha="center", va="top", fontsize=8, color=MUTED,
    )
    fig.subplots_adjust(left=0.01, right=0.99, top=0.965, bottom=0.01, wspace=0.06, hspace=0.06)
    return fig


# -- 1D spectra figure ----------------------------------------------------
def render_spectra(
    window: WindowData, quantity: str, units: str, qdata: QuantityData, set_name: str, ref: str
) -> Figure:
    """Same triangle; each panel the time-median spectrum, a thin blue line."""
    n = len(window.inputs)
    fig = Figure(figsize=(PANEL_IN * n, PANEL_IN * n + 0.3))
    FigureCanvasAgg(fig)
    gs = fig.add_gridspec(n, n, wspace=0.06, hspace=0.06)
    # Every panel plots the same ``window.freq_mhz`` x-range, so the x-limits
    # are set directly on each axis rather than via ``sharex`` (measured
    # 2026-09-08, see ``render_matrix``'s docstring: linking ~300 panels
    # through matplotlib's ``Grouper`` is quadratic in panel count). Y is
    # left on its per-panel default autoscale, same as before this change --
    # each panel's own value range is what is worth seeing here.
    x_lo, x_hi = float(window.freq_mhz.min()), float(window.freq_mhz.max())
    for i in range(n):
        for j in range(i, n):
            ax = fig.add_subplot(gs[i, j])
            ax.set_xticks([])
            ax.set_yticks([])
            ax.set_xlim(x_lo, x_hi)
            for spine in ax.spines.values():
                spine.set_color(HAIRLINE)
            ax.grid(True, color=HAIRLINE, linewidth=0.5, alpha=0.7)
            if i == j:
                y = np.nanmedian(qdata.diag_db[i], axis=0)
                label, color = f"ant {window.antennas[i]}", INK
            else:
                y = np.nanmedian(qdata.values[(i, j)], axis=0)
                label = f"ant {window.antennas[i]} × ant {window.antennas[j]}"
                color = MUTED
            ax.plot(window.freq_mhz, y, color=SIGNAL, linewidth=0.5)
            ax.text(
                0.04, 0.93, label, transform=ax.transAxes, ha="left", va="top",
                fontsize=6 if i == j else 5, color=color,
            )
    fig.text(
        0.5, 0.997, _window_title(window, quantity, units, ref, set_name),
        ha="center", va="top", fontsize=8, color=MUTED,
    )
    fig.subplots_adjust(left=0.01, right=0.99, top=0.965, bottom=0.01, wspace=0.06, hspace=0.06)
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


# -- placeholder (not enough data yet) ------------------------------------
def render_placeholder(message: str, set_name: str, kind: str) -> Figure:
    """A plain "not enough data yet" figure -- never a crash, never a stale PNG."""
    fig = Figure(figsize=(6.0, 3.0))
    FigureCanvasAgg(fig)
    ax = fig.subplots(1, 1)
    ax.axis("off")
    ax.text(
        0.5, 0.6, "not enough data yet", ha="center", va="center",
        fontsize=14, color=INK, transform=ax.transAxes,
    )
    ax.text(
        0.5, 0.35, f"{set_name} / {kind}: {message}", ha="center", va="center",
        fontsize=8, color=MUTED, wrap=True, transform=ax.transAxes,
    )
    return fig


# -- PNG bytes ------------------------------------------------------------
def figure_to_png(fig: Figure, dpi: float) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=PAPER)
    return buf.getvalue()


def _figure_to_pngs(fig: Figure) -> dict[str, bytes]:
    """``{"1x": ..., "2x": ...}``, drawing the figure only once (see ``_png``)."""
    return render_pngs(fig, DPI_2X, DPI_1X, facecolor=PAPER)


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
        pngs = _figure_to_pngs(fig)
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
    return pngs, info


def render_placeholder_kind(message: str, set_name: str, kind: str) -> dict[str, bytes]:
    """PNG bytes for the "not enough data yet" placeholder, one kind."""
    fig = render_placeholder(message, set_name, kind)
    try:
        return _figure_to_pngs(fig)
    finally:
        fig.clear()


__all__ = [
    "DPI_1X",
    "DPI_2X",
    "KINDS",
    "MATRIX_KINDS",
    "MAX_TIME_BINS",
    "MIN_INTEGRATIONS",
    "NCHAN_FIG",
    "NoData",
    "NotEnoughData",
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
    "render_placeholder",
    "render_placeholder_kind",
    "render_spectra",
]
