"""Server-rendered SNAPs tab figures: correlator spectra, board spectra,
waterfalls and per-input trends.

Pure functions: given a :class:`~casm_monitor.store.db.Store` and
:class:`~casm_monitor.config.Settings` they read the last 24 h of cached
Kafka bandpass history (:mod:`casm_monitor.collectors.kafka_bp`, stream
``kafka_bp_sub``), the live frame mirror, and the newest board read
(:mod:`casm_monitor.jobs.snap_read`, table ``snap_read_latest``) and return
PNG bytes. No FastAPI, no threading here; :mod:`casm_monitor.collectors.figures`
calls these from a thread pool, same as :mod:`casm_monitor.figures.vis_figures`.

Reused, never copied: :func:`casm_monitor.web.snaps.board_table` for the
board/input inventory (the same one the live SNAPs API uses),
:mod:`casm_monitor.web.snaps`'s dB<->linear averaging helpers (averaging a
spectrum's dB values directly is a geometric mean and under-reports a hot
channel -- the same reasoning as ``average_db``), and
:func:`casm_monitor.jobs.snap_read.latest_reads` / :func:`casm_monitor.web.
snapread.freq_mhz` for the board-side (500->375 MHz, 4096 chan) half.

House plotting recipes reproduced here (see the task brief): the 3x4-ish
per-board grid, one panel per ADC, dB vs MHz
(``snap_ops/plot_snap_autocorrs.py``, ``casm_vis_analysis/plotting/
autocorr.py``: 0.5 lw lines, ``grid(alpha=0.3)``, grey titles); the viridis
time x freq waterfall (``2026-08-13_17ant_autocorr_waterfalls.png``: time [h]
on the y axis, freq descending on x).
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from typing import Any

import matplotlib

matplotlib.use("Agg")

import numpy as np
from matplotlib import colormaps
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from ..collectors import rowmap
from ..collectors.kafka_bp import STREAM_SUB, SUB_CHAN_AVG, read_latest_frame
from ..config import Settings
from ..jobs.snap_read import latest_reads
from ..store import ShardReader, Store
from ._png import render_pngs

# ``..web.snaps``/``..web.snapread`` (and everything they drag in through
# ``casm_monitor.web``'s ``__init__`` -> ``.app`` -> ``.web.figures``, which
# imports THIS module for its ``KINDS``/``SETS`` constants) are imported
# lazily inside the functions that call them, not at module scope -- a
# module-level import here made ``casm_monitor.figures.snap_figures``
# unimportable first in a fresh interpreter (circular import through
# ``web.figures``), same fix as ``figures/vis_figures.py``.

SETS: tuple[str, ...] = ("beamforming", "all12")
KINDS: tuple[str, ...] = ("spectra_correlator", "spectra_board", "waterfall", "trend")

WINDOW_HOURS = 24.0
FIG_WIDTH_IN = 16.0
DPI_2X = 110
DPI_1X = 55

PAPER = "#ffffff"
INK = "#1f2937"
MUTED = "#6b7280"
HAIRLINE = "#e5e7eb"
SIGNAL = "#2563eb"

# The correlator band (rowmap's 3072 x 93.75/3072 MHz axis) inside the
# board-side 500->375 MHz axis, for the "correlator band lightly shaded"
# panel on ``spectra_board``.
CORRELATOR_BAND_HI_MHZ = rowmap.FREQ_TOP_MHZ
CORRELATOR_BAND_LO_MHZ = rowmap.FREQ_TOP_MHZ - rowmap.NCHAN * rowmap.CHAN_BW_MHZ

N_INPUTS = 12
N_CHANS_BOARD = 4096
WATERFALL_MAX_ROWS = 720
PANEL_H_IN = 1.8


class NoData(RuntimeError):
    """Nothing cached yet -- not a rendering error."""


def board_table() -> list[dict[str, Any]]:
    """Lazy re-export of :func:`casm_monitor.web.snaps.board_table`.

    Kept as a thin wrapper (rather than a module-level ``from ..web.snaps
    import board_table``) so this module stays importable first in a fresh
    interpreter -- see the lazy-import note above the ``SETS``/``KINDS``
    constants.
    """
    from ..web.snaps import board_table as _board_table

    return _board_table()


# -- board/input selection ---------------------------------------------------
def _boards_inputs(boards: list[dict[str, Any]], set_name: str) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Antenna boards, each with the ADCs this ``set_name`` shows.

    ``beamforming`` keeps only in-BF ADCs (a board with none is dropped
    entirely); ``all12`` always keeps the full twelve, wired or not, so an
    unwired ADC still gets its own "unwired" panel.
    """
    out: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for board in boards:
        if board.get("role") != "antenna":
            continue
        items = list(board.get("inputs") or [])
        if set_name == "beamforming":
            items = [it for it in items if it.get("in_bf")]
        if not items:
            continue
        out.append((board, items))
    return out


def _panel_title(board: dict[str, Any], item: dict[str, Any]) -> tuple[str, str]:
    """``"ant 26  N16E1  (S2 A1, pkt 25)"`` / ``"S2 A7  unwired"``, ink if BF.

    ``A<n>`` is the layout's own 0-indexed ``adc`` column, which already
    equals ``packet_idx % 12`` (``packet_idx = feng_id * 12 + adc``) -- it is
    NOT ``adc + 1``. A stray ``+ 1`` here (fixed 2026-09-08, found against
    ``antenna_layouts/current`` on the live ``spectra_correlator@1x.png``)
    made packet_idx 8 (feng 0, adc 8 = ant 9, N21E1) read "S0 A9" instead of
    the correct "S0 A8", and packet_idx 14 (feng 1, adc 2 = ant 15) read
    "S1 A3" instead of "S1 A2".
    """
    feng = board.get("feng_id")
    adc_label = f"S{feng} A{int(item['adc'])}"
    packet_idx = item.get("packet_idx")
    if packet_idx is None:
        return f"{adc_label}  unwired", MUTED
    antenna = item.get("antenna")
    station = item.get("station") or ""
    if antenna is not None:
        label = f"ant {antenna}  {station}  ({adc_label}, pkt {packet_idx})"
    else:
        label = f"{adc_label}  (pkt {packet_idx})"
    return label, (INK if item.get("in_bf") else MUTED)


def _board_label(board: dict[str, Any]) -> str:
    feng = board.get("feng_id")
    return f"SNAP {feng}  {board.get('ip')}  feng {feng}"


# -- grid layout --------------------------------------------------------
def _board_ncols(n_items: int, set_name: str, max_cols: int = 6) -> int:
    """Columns for one board's block: dense, not a fixed-width grid with gaps.

    ``beamforming`` (typically <= ``max_cols`` in-BF ADCs per board): one row,
    exactly as many columns as inputs (a 5-input board is one row of 5, not a
    4-wide grid with a stray wrapped row). ``all12`` always shows the full
    twelve, so it is always ``max_cols`` wide (``max_cols x 2`` for 12).
    """
    if set_name == "beamforming":
        return max(1, min(n_items, max_cols))
    return max_cols


@dataclass(frozen=True)
class _BoardBlock:
    ip: str
    nrows: int
    ncols: int
    cells: list[tuple[Any, int, int]]  # (ax, row, col), populated cells only


def _board_grid(
    boards_inputs: list[tuple[dict[str, Any], list[dict[str, Any]]]], set_name: str
) -> tuple[Figure, dict[tuple[str, int], Any], list[_BoardBlock]]:
    """One figure, boards stacked vertically, each its own dense grid.

    Each board's block is exactly as wide as it needs to be (see
    ``_board_ncols``) and always spans the figure's full width -- filling
    ``FIG_WIDTH_IN`` in wasted the page under the old fixed ``ncols=4`` (a
    5-input board was a 4-wide grid with one wrapped, mostly-empty row).
    Missing panels (fewer ADCs than a full row) are hidden, not left off the
    grid. Returns the per-board cell geometry too, so a caller can apply
    shared y-limits and outer-panel-only tick labels per board.
    """
    if not boards_inputs:
        return _message_figure("no boards configured"), {}, []
    ncols_list = [_board_ncols(len(items), set_name) for _, items in boards_inputs]
    nrows_list = [
        max(1, int(np.ceil(len(items) / ncols))) for (_, items), ncols in zip(boards_inputs, ncols_list)
    ]
    total_rows = sum(nrows_list)
    # 0.85 (not the tighter 0.55 first tried): outer-panel-only tick labels
    # (``_finish_board_blocks``) put real x tick labels on each block's
    # bottom row now, where before every tick was hidden -- the gap between
    # blocks has to fit those labels PLUS the next block's board-name text,
    # not just the text.
    fig_h = PANEL_H_IN * total_rows + 0.85 * len(boards_inputs) + 0.5
    fig = Figure(figsize=(FIG_WIDTH_IN, fig_h))
    FigureCanvasAgg(fig)
    gs = fig.add_gridspec(len(boards_inputs), 1, height_ratios=nrows_list, hspace=0.85)
    axes_map: dict[tuple[str, int], Any] = {}
    blocks: list[_BoardBlock] = []
    for bi, (board, items) in enumerate(boards_inputs):
        nrows, ncols = nrows_list[bi], ncols_list[bi]
        inner = gs[bi].subgridspec(nrows, ncols, hspace=0.35, wspace=0.15)
        first_ax = None
        cells: list[tuple[Any, int, int]] = []
        for k in range(nrows * ncols):
            row, col = k // ncols, k % ncols
            ax = fig.add_subplot(inner[row, col])
            if k < len(items):
                axes_map[(board["ip"], int(items[k]["adc"]))] = ax
                cells.append((ax, row, col))
                if first_ax is None:
                    first_ax = ax
            else:
                ax.set_visible(False)
        if first_ax is not None:
            first_ax.text(
                0.0, 1.5, _board_label(board), transform=first_ax.transAxes,
                ha="left", va="bottom", fontsize=9, color=MUTED,
            )
        blocks.append(_BoardBlock(ip=board["ip"], nrows=nrows, ncols=ncols, cells=cells))
    return fig, axes_map, blocks


PANEL_YLIM_MIN_SPAN_DB = 6.0


def _robust_panel_ylim(ax: Any, min_span: float = PANEL_YLIM_MIN_SPAN_DB) -> tuple[float, float]:
    """``[p1 - 1, p99 + 1]`` dB of this panel's latest spectrum, >= ``min_span`` tall.

    House-grid convention (``casm_vis_analysis/plotting/autocorr.py``): each
    panel autoscales off its own data, not a shared board-wide range -- a
    shared range let one dead/railed input (e.g. an ADC pegged at -60 dB, or
    one with a ripple down to 10 dB) squash every healthy panel on that board
    to a flat line. The "latest" line is the ``SIGNAL``-colored one (plotted
    last in both ``render_spectra_correlator`` and ``render_spectra_board``);
    percentiles (not min/max) keep a single spiky channel from blowing out
    the range the way the old shared min/max did. Falls back to the axis's
    own autoscaled range if there is no signal line yet (e.g. no board read
    cached), still widened to ``min_span``.
    """
    y = None
    for line in ax.get_lines():
        if line.get_color() == SIGNAL:
            data = np.asarray(line.get_ydata(), dtype=np.float64)
            data = data[np.isfinite(data)]
            if data.size:
                y = data
                break
    if y is not None:
        lo, hi = np.percentile(y, [1.0, 99.0])
        lo, hi = float(lo) - 1.0, float(hi) + 1.0
    else:
        lo, hi = ax.get_ylim()
    if hi - lo < min_span:
        mid = 0.5 * (lo + hi)
        lo, hi = mid - min_span / 2.0, mid + min_span / 2.0
    return lo, hi


def _finish_board_blocks(blocks: list[_BoardBlock]) -> None:
    """Per-panel y-limits, tick labels on the outer x axes only.

    Called once all panels in every board's block have been plotted. Each
    panel gets its own robust y-range (``_robust_panel_ylim``) rather than a
    board-wide shared one, so a dead or railed input's panel no longer
    squashes its healthy neighbours; y tick labels stay on every panel since
    the values now genuinely differ panel to panel. The row/column each cell
    occupies is the block's own grid position, not just "last row of the
    board" -- the bottom-most row that actually has a populated cell in that
    column keeps its x tick labels, every other row hides them.
    """
    for block in blocks:
        if not block.cells:
            continue
        last_row_in_col: dict[int, int] = {}
        for _ax, row, col in block.cells:
            last_row_in_col[col] = max(row, last_row_in_col.get(col, row))
        for ax, row, col in block.cells:
            ax.set_ylim(*_robust_panel_ylim(ax))
            ax.tick_params(
                labelbottom=(row == last_row_in_col[col]), labelleft=True,
                labelsize=6,
            )


def _flat_grid(
    items: list[tuple[dict[str, Any], dict[str, Any]]], ncols: int = 6, panel_h: float = 1.6
) -> tuple[Figure, dict[tuple[str, int], Any]]:
    """A plain ``ncols``-wide grid, one panel per (board, input), no board
    grouping (the waterfall/trend layout the brief asks for)."""
    if not items:
        return _message_figure("no inputs in this set"), {}
    n = len(items)
    nrows = max(1, int(np.ceil(n / ncols)))
    fig = Figure(figsize=(FIG_WIDTH_IN, panel_h * nrows + 0.6))
    FigureCanvasAgg(fig)
    axes = fig.subplots(nrows, ncols, squeeze=False)
    axes_map: dict[tuple[str, int], Any] = {}
    for k, (board, item) in enumerate(items):
        ax = axes[k // ncols][k % ncols]
        axes_map[(board["ip"], int(item["adc"]))] = ax
    for k in range(n, nrows * ncols):
        axes[k // ncols][k % ncols].set_visible(False)
    return fig, axes_map


def _style_axes(ax: Any) -> None:
    ax.grid(True, color=HAIRLINE, linewidth=0.5, alpha=0.3)
    for spine in ax.spines.values():
        spine.set_color(HAIRLINE)


def _message_figure(message: str) -> Figure:
    fig = Figure(figsize=(FIG_WIDTH_IN, 1.3))
    FigureCanvasAgg(fig)
    ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
    ax.axis("off")
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=12, color=MUTED)
    return fig


# -- kafka bandpass history (kafka_bp_sub, all wanted rows at once) --------
@dataclass(frozen=True)
class KafkaSubWindow:
    times: np.ndarray  # (T,) unix seconds, ascending
    freq_mhz: np.ndarray  # (F,) descending
    rows: list[int]
    z_db: np.ndarray  # (T, len(rows), F); nan where a row was absent from a shard
    t0: float
    t1: float


def load_kafka_sub_window(
    store: Store, rows: list[int], hours: float = WINDOW_HOURS
) -> KafkaSubWindow:
    """Last ``hours`` of ``kafka_bp_sub`` for every row in ``rows`` at once.

    One pass over the shards regardless of how many rows are wanted (rather
    than one history query per input, per :mod:`casm_monitor.web.snaps`'s
    single-row ``/history`` route) -- the SNAPs figures need every wired
    input's history in the same 30 min tick.
    """
    from ..web.snaps import average_channels

    shards = ShardReader(store)
    t1 = time.time()
    t0 = t1 - float(hours) * 3600.0
    wanted = sorted({int(r) for r in rows})
    if not wanted:
        raise NoData("no rows requested")
    freq: np.ndarray | None = None
    time_chunks: list[np.ndarray] = []
    z_chunks: list[np.ndarray] = []
    for shard in shards.list(STREAM_SUB, t0=t0, t1=t1):
        meta = shard["meta"] or {}
        shard_rows = [int(r) for r in (meta.get("rows") or [])]
        idx_by_row = {r: i for i, r in enumerate(shard_rows)}
        array, _meta = shards.load(shard)
        nchan = array.shape[-1]
        if freq is None:
            freq = average_channels(rowmap.freq_axis_mhz(), nchan) if nchan != rowmap.NCHAN else rowmap.freq_axis_mhz()
        times = np.asarray(meta.get("t") or [], dtype=np.float64)
        if times.size != array.shape[0]:
            times = np.linspace(shard["t0"], shard["t1"], array.shape[0])
        keep = (times >= t0) & (times <= t1)
        if not keep.any():
            continue
        arr_keep = np.asarray(array[keep], dtype=np.float32)
        sub = np.full((arr_keep.shape[0], len(wanted), nchan), np.nan, dtype=np.float32)
        for j, row in enumerate(wanted):
            i = idx_by_row.get(row)
            if i is not None:
                sub[:, j, :] = arr_keep[:, i, :]
        z_chunks.append(sub)
        time_chunks.append(times[keep])
    if not z_chunks:
        raise NoData(f"no kafka bandpass history in the last {hours:.0f} h")
    z = np.concatenate(z_chunks, axis=0)
    times_arr = np.concatenate(time_chunks, axis=0)
    order = np.argsort(times_arr)
    return KafkaSubWindow(
        times=times_arr[order], freq_mhz=freq, rows=wanted, z_db=z[order], t0=t0, t1=t1
    )


def _median_db(z_db_one_row: np.ndarray) -> np.ndarray:
    """24 h median in the power domain (never a geometric mean of dB).

    ``linear`` can be all-nan for a channel/row that never showed up in a
    shard (row wired but never seen in the window); ``nanmean`` of an
    all-nan slice is a legitimate nan result here, not a bug, so its warning
    is suppressed rather than left to spam every render.
    """
    import warnings

    from ..web.snaps import db_to_linear, linear_to_db

    with warnings.catch_warnings(), np.errstate(invalid="ignore"):
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return linear_to_db(np.nanmean(db_to_linear(z_db_one_row), axis=0))


def _decimate_time(z_db: np.ndarray, times: np.ndarray, max_rows: int) -> tuple[np.ndarray, np.ndarray]:
    if times.size <= max_rows:
        return z_db, times
    from ..web.snaps import average_channels, average_db_axis0

    z2 = average_db_axis0(z_db, max_rows)
    t2 = average_channels(times, max_rows)
    return z2, t2


# -- spectra_correlator ---------------------------------------------------
def render_spectra_correlator(
    store: Store, settings: Settings, set_name: str, boards: list[dict[str, Any]]
) -> tuple[Figure, dict[str, Any]]:
    boards_inputs = _boards_inputs(boards, set_name)
    rows = sorted({2 * int(it["packet_idx"]) for _, items in boards_inputs for it in items if it.get("packet_idx") is not None})
    window: KafkaSubWindow | None = None
    if rows:
        try:
            window = load_kafka_sub_window(store, rows)
        except NoData:
            window = None
    frame = read_latest_frame(settings)
    freq_full = rowmap.freq_axis_mhz()
    fig, axes_map, blocks = _board_grid(boards_inputs, set_name)
    handled_legend = False
    for board, items in boards_inputs:
        for item in items:
            ax = axes_map.get((board["ip"], int(item["adc"])))
            if ax is None:
                continue
            title, color = _panel_title(board, item)
            ax.set_title(title, fontsize=9, color=color)
            _style_axes(ax)
            ax.set_xlim(freq_full.max(), freq_full.min())
            packet_idx = item.get("packet_idx")
            if packet_idx is None:
                continue
            row = 2 * int(packet_idx)
            if window is not None and row in window.rows:
                j = window.rows.index(row)
                med = _median_db(window.z_db[:, j, :])
                ax.plot(window.freq_mhz, med, color=MUTED, linewidth=0.5, label="24 h median")
            if frame is not None and row in frame["rows"]:
                k = frame["rows"].index(row)
                ax.plot(freq_full, frame["power_db"][k], color=SIGNAL, linewidth=0.6, label="latest")
            if not handled_legend and ax.get_legend_handles_labels()[0]:
                handled_legend = True
                fig.legend(
                    *ax.get_legend_handles_labels(), loc="upper center",
                    bbox_to_anchor=(0.5, 1.0), ncol=2, fontsize=7, frameon=False,
                )
    _finish_board_blocks(blocks)
    info = {
        "t0": window.t0 if window is not None else None,
        "t1": window.t1 if window is not None else None,
        "n_frames": int(window.times.size) if window is not None else 0,
        "board_read_ts": None,
    }
    return fig, info


# -- spectra_board ----------------------------------------------------------
def render_spectra_board(
    store: Store, settings: Settings, set_name: str, boards: list[dict[str, Any]]
) -> tuple[Figure, dict[str, Any]]:
    from ..web.snaps import linear_to_db
    from ..web.snapread import freq_mhz as board_freq_mhz

    reads = latest_reads(store)
    if not reads:
        return _message_figure("no board read yet"), {
            "t0": None, "t1": None, "n_frames": 0, "board_read_ts": None,
        }
    shards = ShardReader(store)
    spectra_by_ip: dict[str, tuple[np.ndarray, dict[str, Any]]] = {}
    for ip, summary in reads.items():
        shard_id = summary.get("shard_id")
        if shard_id is None:
            continue
        try:
            array, _meta = shards.load(int(shard_id))
        except Exception:
            continue
        spectra_by_ip[ip] = (np.asarray(array, dtype=np.float64), summary)

    boards_inputs = _boards_inputs(boards, set_name)
    fig, axes_map, blocks = _board_grid(boards_inputs, set_name)
    freq_board = np.asarray(board_freq_mhz(N_CHANS_BOARD), dtype=np.float64)
    for board, items in boards_inputs:
        for item in items:
            ax = axes_map.get((board["ip"], int(item["adc"])))
            if ax is None:
                continue
            title, color = _panel_title(board, item)
            entry = spectra_by_ip.get(board["ip"])
            suffix = ""
            ax.axvspan(CORRELATOR_BAND_LO_MHZ, CORRELATOR_BAND_HI_MHZ, color=HAIRLINE, alpha=0.6, zorder=0)
            if entry is not None:
                array, summary = entry
                adc = int(item["adc"])
                if adc < array.shape[0]:
                    spectrum = array[adc]
                    if np.isfinite(spectrum).any():
                        n_chans = int(summary.get("n_chans") or N_CHANS_BOARD)
                        freq = freq_board if n_chans == N_CHANS_BOARD else np.asarray(board_freq_mhz(n_chans))
                        db = linear_to_db(spectrum)
                        ax.plot(freq, db, color=SIGNAL, linewidth=0.6)
                rms_list = summary.get("adc_rms") or []
                if adc < len(rms_list) and rms_list[adc] is not None:
                    suffix = f"  rms {float(rms_list[adc]):.1f}"
            else:
                ax.text(0.5, 0.5, 'No saved spectrum', transform=ax.transAxes,
                        ha='center', va='center', fontsize=8, color=MUTED)
            ax.set_title(f"{title}{suffix}", fontsize=9, color=color)
            _style_axes(ax)
            ax.set_xlim(freq_board.max(), freq_board.min())
    _finish_board_blocks(blocks)
    board_read_ts = max((float(s.get("ts") or 0.0) for s in reads.values()), default=None)
    info = {
        "t0": board_read_ts, "t1": board_read_ts, "n_frames": len(reads), "board_read_ts": board_read_ts,
    }
    return fig, info


# -- waterfall --------------------------------------------------------------
def render_waterfall(
    store: Store, settings: Settings, set_name: str, boards: list[dict[str, Any]]
) -> tuple[Figure, dict[str, Any]]:
    boards_inputs = _boards_inputs(boards, set_name)
    flat = [(board, item) for board, items in boards_inputs for item in items]
    rows = sorted({2 * int(it["packet_idx"]) for _b, it in flat if it.get("packet_idx") is not None})
    window: KafkaSubWindow | None = None
    if rows:
        try:
            window = load_kafka_sub_window(store, rows)
        except NoData:
            window = None
    fig, axes_map = _flat_grid(flat, ncols=6, panel_h=1.8)
    cmap = colormaps["viridis"].copy()
    cmap.set_bad(PAPER)
    for board, item in flat:
        ax = axes_map.get((board["ip"], int(item["adc"])))
        if ax is None:
            continue
        title, color = _panel_title(board, item)
        ax.set_title(title, fontsize=8, color=color)
        ax.set_xticks([])
        ax.set_yticks([])
        packet_idx = item.get("packet_idx")
        if packet_idx is None or window is None:
            continue
        row = 2 * int(packet_idx)
        if row not in window.rows:
            continue
        j = window.rows.index(row)
        z, t = _decimate_time(window.z_db[:, j, :], window.times, WATERFALL_MAX_ROWS)
        if t.size < 2:
            continue
        time_h = (t - window.times[0]) / 3600.0
        # imshow (not pcolormesh) over an explicit extent: identical visual
        # result on this regular (uniform channel/time) grid but much
        # cheaper to rasterize, same reasoning as ``vis_figures.render_matrix``.
        # ``z``'s row 0 is the earliest time -> ``origin="lower"`` so it lands
        # at the bottom, matching pcolormesh's own placement of an ascending
        # Y array; the x-axis is inverted afterwards (descending frequency,
        # unchanged from before) via ``set_xlim``, independent of the extent.
        f_lo, f_hi = float(window.freq_mhz.min()), float(window.freq_mhz.max())
        t_lo, t_hi = float(time_h[0]), float(time_h[-1]) if time_h[-1] > time_h[0] else float(time_h[0]) + 1.0
        ax.imshow(
            z, cmap=cmap, extent=(f_lo, f_hi, t_lo, t_hi), origin="lower",
            aspect="auto", interpolation="nearest", rasterized=True,
        )
        ax.set_xlim(window.freq_mhz.max(), window.freq_mhz.min())
    info = {
        "t0": window.t0 if window is not None else None,
        "t1": window.t1 if window is not None else None,
        "n_frames": int(window.times.size) if window is not None else 0,
        "board_read_ts": None,
    }
    return fig, info


# -- trend --------------------------------------------------------------
def _scalar_series(store: Store, name_clause: str, args: list[Any], packet_idx: int, t0: float, t1: float):
    rows = store.query(
        "SELECT ts, value FROM scalars WHERE " + name_clause +
        " AND json_extract(tags, '$.packet_idx') = ? AND ts >= ? AND ts <= ? ORDER BY ts ASC",
        [*args, int(packet_idx), t0, t1],
    )
    return [float(r["ts"]) for r in rows], [float(r["value"]) for r in rows]


def render_trend(
    store: Store, settings: Settings, set_name: str, boards: list[dict[str, Any]]
) -> tuple[Figure, dict[str, Any]]:
    boards_inputs = _boards_inputs(boards, set_name)
    flat = [
        (board, item)
        for board, items in boards_inputs
        for item in items
        if item.get("packet_idx") is not None
    ]
    if not flat:
        return _message_figure("no wired inputs in this set"), {
            "t0": None, "t1": None, "n_frames": 0, "board_read_ts": None,
        }
    t1 = time.time()
    t0 = t1 - WINDOW_HOURS * 3600.0
    fig, axes_map = _flat_grid(flat, ncols=6, panel_h=1.6)
    n_frames = 0
    for board, item in flat:
        ax = axes_map.get((board["ip"], int(item["adc"])))
        if ax is None:
            continue
        title, color = _panel_title(board, item)
        ax.set_title(title, fontsize=8, color=color)
        _style_axes(ax)
        packet_idx = int(item["packet_idx"])
        t, v = _scalar_series(
            store, "name LIKE ? AND name LIKE ?", ["kafka_bp.row%", "%.power_db"], packet_idx, t0, t1
        )
        if t:
            n_frames = max(n_frames, len(t))
            time_h = (np.asarray(t) - t0) / 3600.0
            ax.plot(time_h, v, color=SIGNAL, linewidth=0.5)
        nt, nv = _scalar_series(store, "name = ?", ["snaps.night_median_db"], packet_idx, t0, t1)
        if nt:
            nt_h = (np.asarray(nt) - t0) / 3600.0
            ax.scatter(nt_h, nv, color=MUTED, s=8, zorder=3)
    fig.supxlabel("Time (h)", fontsize=9)
    fig.supylabel("Band power (dB)", fontsize=9)
    return fig, {"t0": t0, "t1": t1, "n_frames": n_frames, "board_read_ts": None}


# -- PNG bytes / dispatch ----------------------------------------------------
def figure_to_png(fig: Figure, dpi: float) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=PAPER)
    return buf.getvalue()


def _figure_to_pngs(fig: Figure, *, facecolor: str = PAPER) -> dict[str, bytes]:
    """``{"1x": ..., "2x": ...}``, drawing the figure only once (see ``_png``)."""
    return render_pngs(fig, DPI_2X, DPI_1X, facecolor=facecolor)


def dark_scientific_style(fig: Figure) -> None:
    """Restyle the existing scientific figure without changing its data or axes."""
    from matplotlib.text import Text

    fig.set_facecolor("#000000")
    for ax in fig.axes:
        ax.set_facecolor("#000000")
        ax.tick_params(colors="#b8b8b8")
        for spine in ax.spines.values():
            spine.set_color("#444444")
        for line in ax.get_xgridlines() + ax.get_ygridlines():
            line.set_color("#555555")
            line.set_alpha(0.25)
        for line in ax.get_lines():
            if line.get_color() == SIGNAL:
                line.set_color("#77c7cf")
            elif line.get_color() == MUTED:
                line.set_color("#aaaaaa")
        for patch in ax.patches:
            patch.set_facecolor("#242424")
    for label in fig.findobj(match=Text):
        label.set_color("#a8a8a8" if label.get_color() == MUTED else "#e6e6e6")


_RENDERERS = {
    "spectra_correlator": render_spectra_correlator,
    "spectra_board": render_spectra_board,
    "waterfall": render_waterfall,
    "trend": render_trend,
}


def render_kind(
    store: Store,
    settings: Settings,
    kind: str,
    set_name: str,
    *,
    boards: list[dict[str, Any]] | None = None,
    dark: bool = False,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Render one (kind, set) combo; returns ``{"1x": png, "2x": png}`` + info.

    ``info`` carries what the collector's manifest needs: ``t0``, ``t1``,
    ``n_frames``, ``board_read_ts``.
    """
    if set_name not in SETS:
        raise ValueError(f"set must be one of {SETS}")
    renderer = _RENDERERS.get(kind)
    if renderer is None:
        raise ValueError(f"unknown figure kind {kind!r}")
    if boards is None:
        boards = board_table()
    fig, info = renderer(store, settings, set_name, boards)
    try:
        if dark:
            dark_scientific_style(fig)
            if kind == 'spectra_board':
                fig.supxlabel('Frequency (MHz)', color='#e6e6e6', fontsize=11)
                fig.supylabel('Power (dB)', color='#e6e6e6', fontsize=11)
        pngs = _figure_to_pngs(fig, facecolor="#000000" if dark else PAPER)
    finally:
        fig.clear()
    return pngs, info


__all__ = [
    "DPI_1X",
    "DPI_2X",
    "KINDS",
    "SETS",
    "WATERFALL_MAX_ROWS",
    "WINDOW_HOURS",
    "NoData",
    "board_table",
    "figure_to_png",
    "load_kafka_sub_window",
    "render_kind",
    "render_spectra_board",
    "render_spectra_correlator",
    "render_trend",
    "render_waterfall",
]
