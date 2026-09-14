"""T1 stream monitoring: are all eight Hella streams alive, and what did they find.

Two sources, both read-only from this module's point of view:

* the Hella gulp ledger (:mod:`casm_monitor.hella_log`), one row per gulp per
  stream tailed out of ``bf_proc_hella.log`` — this is the only source that
  knows a stream ran at all;
* the ``cand_bins`` table written by the search collector, which only holds
  rows for gulps that emitted candidates.

A gulp present in the ledger with no ``cand_bins`` row is an EMPTY gulp, the
normal healthy state. A time bin with no ledger row is "no gulp recorded",
which is the condition worth alarming on.

The two sources carry different clocks: ``cand_bins.gulp_ts`` is the data clock
(``utc_start + samp * tsamp``), the ledger timestamp is the log wall clock when
the gulp finished. They are never matched gulp to gulp, only counted per bin.
"""
from __future__ import annotations

import io
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import threading
import tempfile
import time
from typing import Literal
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, FileResponse

from ..collectors.search import DM_EDGES, WIDTH_EDGES
from .. import hella_log
from ..util import iso
from .search import window

MAX_ROWS = 700000
TIME_BINS = 480
N_STREAMS = hella_log.N_STREAMS
REFRESH_S = 300
CACHE_TTL_S = 120
# Liveness thresholds on the age of a stream's newest ledger gulp.
OK_AGE_S = 60.0
LATE_AGE_S = 600.0

# Healthy slate at 0 (no gulp emitted anything) to cyan at 1.
EMPTY_COLOR = "#2f4858"
BUSY_COLOR = "#4cc9f0"
CAP_COLOR = "#ff6b35"
QUIET_COLOR = EMPTY_COLOR
FG = "#d0d7de"
SPINE = "#53616d"
TITLE = "#f0f6fc"

_PLOT_LOCK = threading.Lock()


def node_of(stream: int) -> str:
    return "corr1" if stream < 4 else "corr2"


def _bin_index(stamp: float, start: float, end: float) -> int:
    return min(TIME_BINS - 1, max(0, int((stamp - start) / (end - start) * TIME_BINS)))


def stream_rows(ledger_db, cand_counts_last_hour, *, end: float, read_unix=None):
    """Per-stream liveness from the ledger, with the hour's empty fraction.

    Age is measured against the last log read, not wall clock: between ticks
    every stream's wall-clock age grows with the tick interval, which would
    read as "late" on a healthy array.
    """
    hour_start = end - 3600.0
    reference = end if read_unix is None else read_unix
    last = hella_log.read_last_gulps(ledger_db, end)
    walls: dict[int, list[float]] = {k: [] for k in range(N_STREAMS)}
    gulps = [0] * N_STREAMS
    caps = [0] * N_STREAMS
    for stamp, stream, wall_s, cap_hit in hella_log.read_gulps(ledger_db, hour_start, end):
        if not 0 <= stream < N_STREAMS:
            continue
        gulps[stream] += 1
        caps[stream] += int(cap_hit or 0)
        if wall_s is not None:
            walls[stream].append(float(wall_s))
    rows = []
    for stream in range(N_STREAMS):
        newest = last.get(stream)
        age = None if newest is None else max(0.0, reference - newest)
        if age is None:
            status = "unknown"
        elif age < OK_AGE_S:
            status = "ok"
        elif age < LATE_AGE_S:
            status = "late"
        else:
            status = "silent"
        # cand_bins only has rows for gulps that emitted; the rest were empty.
        emitted = min(gulps[stream], cand_counts_last_hour.get(stream, 0))
        rows.append({
            "stream": stream, "node": node_of(stream),
            "last_gulp_unix": newest, "last_gulp_age_s": age,
            "gulps_last_hour": gulps[stream],
            "expected_gulps_per_hour": hella_log.EXPECTED_GULPS_PER_HOUR,
            "empty_fraction_last_hour": (None if not gulps[stream]
                                         else (gulps[stream] - emitted) / gulps[stream]),
            "cap_hits_last_hour": caps[stream],
            "median_wall_s_last_hour": (statistics.median(walls[stream]) if walls[stream] else None),
            "status": status,
        })
    return rows


def build_t1(reader, *, t0=None, t1=None, ledger_db=None):
    end_arg = str(time.time()) if t1 is None else t1
    start, end = window(t0, end_arg)
    if t0 is None:
        start = end - 86400
    if not math.isfinite(start) or not math.isfinite(end) or end - start > 7 * 86400:
        raise HTTPException(400, "T1 windows must be finite and at most seven days")
    edges = np.linspace(start, end, TIME_BINS + 1)
    beam = np.zeros((TIME_BINS, 512), dtype=np.int64)
    dm = np.zeros((TIME_BINS, len(DM_EDGES) - 1), dtype=np.int64)
    dm_totals = np.zeros(len(DM_EDGES) - 1, dtype=np.int64)
    widths = np.zeros(len(WIDTH_EDGES) - 1, dtype=np.int64)
    cands = np.zeros((TIME_BINS, N_STREAMS), dtype=np.int64)
    cand_bin_rows = np.zeros((TIME_BINS, N_STREAMS), dtype=np.int64)
    result = {"status": "unavailable", "t0": iso(start), "t1": iso(end),
              "source": str(reader.db_path), "source_table": "cand_bins", "row_budget": MAX_ROWS,
              "refresh_s": REFRESH_S}

    # -- ledger: gulps and cap hits per bin -----------------------------
    gulps = np.zeros((TIME_BINS, N_STREAMS), dtype=np.int64)
    cap_hits = np.zeros((TIME_BINS, N_STREAMS), dtype=np.int64)
    ledger_rows = hella_log.read_gulps(ledger_db, start, end)
    for stamp, stream, _wall, cap_hit in ledger_rows:
        if not 0 <= stream < N_STREAMS:
            continue
        index = _bin_index(stamp, start, end)
        gulps[index, stream] += 1
        cap_hits[index, stream] += int(cap_hit or 0)
    info = hella_log.ledger_info(ledger_db)
    tick = info["last_tick_unix"]
    result["ledger"] = {"status": info["status"], "rows_in_window": len(ledger_rows),
                        "newest_unix": info["newest_unix"], "log_path": info["log_path"],
                        "watermark_offset": info["watermark_offset"],
                        "last_tick_unix": tick,
                        "read_age_s": None if tick is None else max(0.0, time.time() - tick)}

    # -- candidate bins --------------------------------------------------
    n_rows = 0
    deadline = time.monotonic() + 8.0
    first = last = None
    hour_counts: dict[int, int] = {}
    try:
        connection = sqlite3.connect(Path(reader.db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            connection.execute("PRAGMA query_only=ON")
            cursor = connection.execute("SELECT gulp_ts, job, n, dm_hist, width_hist, beam_counts FROM cand_bins "
                                        "WHERE gulp_ts >= ? AND gulp_ts <= ? ORDER BY gulp_ts DESC LIMIT ?",
                                        (start, end, MAX_ROWS + 1))
            for stamp, stream, count, dh, wh, bh in cursor:
                if n_rows >= MAX_ROWS or (n_rows % 1000 == 0 and time.monotonic() > deadline):
                    result["truncated"] = True
                    result["reason"] = ("Search-bin row/time budget exceeded; newest retained bins shown, "
                                        "counts incomplete. Narrow the interval.")
                    break
                n_rows += 1
                if not 0 <= stream < N_STREAMS:
                    raise ValueError("Invalid stored stream index")
                index = _bin_index(stamp, start, end)
                d, w, b = np.asarray(json.loads(dh)), np.asarray(json.loads(wh)), np.asarray(json.loads(bh))
                if d.shape != (len(DM_EDGES) - 1,) or w.shape != widths.shape or b.shape != (64,):
                    raise ValueError("Stored histogram shape differs from collector contract")
                dm[index] += d.astype(np.int64)
                dm_totals += d.astype(np.int64)
                widths += w.astype(np.int64)
                beam[index, stream * 64:(stream + 1) * 64] += b.astype(np.int64)
                cands[index, stream] += count
                cand_bin_rows[index, stream] += 1
                first = stamp if first is None else min(first, stamp)
                last = stamp if last is None else max(last, stamp)
            hour_counts = {int(s): int(c) for s, c in connection.execute(
                "SELECT job, count(*) FROM cand_bins WHERE gulp_ts >= ? AND gulp_ts <= ? GROUP BY job",
                (end - 3600.0, end))}
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        result["reason"] = f"Search-bin evidence unavailable: {exc}"
        result["streams"] = stream_rows(ledger_db, {}, end=end, read_unix=tick)
        return result

    # A bin is quiet when streams ran there but nothing was emitted.
    quiet = (gulps.sum(axis=1) > 0) & (cand_bin_rows.sum(axis=1) == 0)
    result.update(status="partial" if result.get("truncated") else ("ok" if n_rows else "empty"),
                  n_bin_rows=n_rows, n_candidates=int(cands.sum()),
                  first_data_utc=iso(first) if first is not None else None,
                  last_data_utc=iso(last) if last is not None else None,
                  time_edges_unix=edges.tolist(), time_bin_seconds=(end - start) / TIME_BINS,
                  streams=stream_rows(ledger_db, hour_counts, end=end, read_unix=tick),
                  activity={"gulps": gulps.tolist(), "cands": cands.tolist(),
                            "gulps_with_cands": cand_bin_rows.tolist(),
                            "cap_hits": cap_hits.tolist()},
                  quiet_bins=quiet.tolist(),
                  beam_time_counts=beam.tolist(), dm_time_counts=dm.tolist(),
                  dm_edges=DM_EDGES, width_edges=WIDTH_EDGES,
                  dm_counts=dm_totals.tolist(), width_counts=widths.tolist(),
                  width_units="Hella width index, not FWHM")
    return result


def _style(ax) -> None:
    ax.set_facecolor("#000000")
    ax.tick_params(colors=FG, labelsize=8)
    ax.xaxis.label.set_color(FG)
    ax.yaxis.label.set_color(FG)
    ax.title.set_color(TITLE)
    for spine in ax.spines.values():
        spine.set_color(SPINE)


def _time_axis(ax, data, mdates, zone) -> None:
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M", tz=zone))
    ax.set_xlabel("UTC" if data.get("time_tz") == "UTC" else "OVRO local (PDT/PST)")


def _blank(ax, message: str) -> None:
    ax.text(.5, .5, message, color=FG, ha="center", va="center", fontsize=10, transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])


def render_t1(data):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.colors import LinearSegmentedColormap, ListedColormap, LogNorm
    from matplotlib.patches import Patch
    import matplotlib.dates as mdates

    zone = ZoneInfo(data.get("time_tz", "America/Los_Angeles"))
    with _PLOT_LOCK:
        fig = Figure(figsize=(13, 12.5), layout="constrained", facecolor="#000000")
        FigureCanvasAgg(fig)
        grid = fig.add_gridspec(4, 2, height_ratios=[1.6, 2.4, 2.0, 1.4],
                                width_ratios=[1, .05])
        act_ax = fig.add_subplot(grid[0, 0])
        act_cax = fig.add_subplot(grid[0, 1])
        beam_ax = fig.add_subplot(grid[1, 0])
        beam_cax = fig.add_subplot(grid[1, 1])
        dm_ax = fig.add_subplot(grid[2, 0])
        dm_cax = fig.add_subplot(grid[2, 1])
        hist = grid[3, :].subgridspec(1, 2)
        width_ax = fig.add_subplot(hist[0, 0])
        dmhist_ax = fig.add_subplot(hist[0, 1])
        for ax in (act_ax, beam_ax, dm_ax, width_ax, dmhist_ax):
            _style(ax)
        for cax in (act_cax, beam_cax, dm_cax):
            cax.set_axis_off()

        has_time = "time_edges_unix" in data
        times = (mdates.date2num([datetime.fromtimestamp(t, timezone.utc)
                                  for t in data["time_edges_unix"]]) if has_time else None)
        streams = data.get("streams") or []
        ledger_live = any(row["last_gulp_unix"] is not None for row in streams)

        # -- 1: gulp activity --------------------------------------------
        if not ledger_live or not has_time:
            _blank(act_ax, "No Hella log ledger yet" if not ledger_live
                   else "Search evidence unavailable")
        else:
            activity = data["activity"]
            gulps = np.asarray(activity["gulps"], dtype=float)
            emitting = np.asarray(activity["gulps_with_cands"], dtype=float)
            caps = np.asarray(activity["cap_hits"], dtype=np.int64)
            # Fraction of the bin's gulps that emitted anything. Most gulps are
            # empty, so a per-bin "any candidate" flag would saturate.
            share = np.zeros_like(gulps)
            np.divide(emitting, gulps, out=share, where=gulps > 0)
            colors = LinearSegmentedColormap.from_list("empty_to_busy", [EMPTY_COLOR, BUSY_COLOR])
            edges_y = np.arange(N_STREAMS + 1)
            image = act_ax.pcolormesh(times, edges_y, np.ma.masked_where(gulps.T <= 0, share.T),
                                      cmap=colors, vmin=0, vmax=1)
            # Cap hits sit on top: rare, and never to be averaged away.
            act_ax.pcolormesh(times, edges_y, np.ma.masked_where(caps.T <= 0, caps.T),
                              cmap=ListedColormap([CAP_COLOR]))
            act_ax.axhline(4, color="#e6edf3", linewidth=.9)
            act_ax.set_yticks(np.arange(N_STREAMS) + .5)
            act_ax.set_yticklabels([str(k) for k in range(N_STREAMS)])
            act_ax.set_ylabel("Stream")
            # Pad clears the legend strip drawn just above the axes.
            act_ax.set_title("Gulp activity per stream", pad=20)
            _time_axis(act_ax, data, mdates, zone)
            bar = fig.colorbar(image, cax=act_cax, label="fraction of gulps with candidates")
            act_cax.set_axis_on()
            bar.ax.yaxis.label.set_color(FG)
            bar.ax.tick_params(colors=FG, labelsize=8)
            bar.outline.set_edgecolor(SPINE)
            act_ax.legend(handles=[Patch(facecolor="#000000", edgecolor=SPINE, label="no gulp recorded"),
                                   Patch(facecolor=EMPTY_COLOR, edgecolor=SPINE,
                                         label="0% of gulps had candidates (healthy)"),
                                   Patch(facecolor=BUSY_COLOR, edgecolor=SPINE, label="100%"),
                                   Patch(facecolor=CAP_COLOR, edgecolor=SPINE, label="cap hit")],
                          loc="lower left", bbox_to_anchor=(0, 1.02), ncol=4, fontsize=7.5,
                          frameon=False, labelcolor=FG)

        # -- 3/4/5: candidates -------------------------------------------
        if not data.get("n_bin_rows"):
            _blank(beam_ax, "No candidates recorded in this interval\n"
                            "streams may still be alive, see the panels above")
            for ax in (dm_ax, width_ax, dmhist_ax):
                _blank(ax, "No candidates recorded in this interval")
        else:
            beams = np.asarray(data["beam_time_counts"], dtype=float).T
            image = beam_ax.pcolormesh(times, np.arange(513), np.ma.masked_less_equal(beams, 0),
                                       cmap="viridis", norm=LogNorm(vmin=1, vmax=max(2, beams.max())))
            bar = fig.colorbar(image, cax=beam_cax, label="candidates per beam per 3 min")
            beam_cax.set_axis_on()
            bar.ax.yaxis.label.set_color(FG)
            bar.ax.tick_params(colors=FG, labelsize=8)
            bar.outline.set_edgecolor(SPINE)
            for k in range(1, N_STREAMS):
                beam_ax.axhline(64 * k, color="#8b949e", linewidth=.5)
                beam_ax.text(times[-1], 64 * k - 32, f"stream {k - 1} ", color="#8b949e",
                             ha="right", va="center", fontsize=7)
            beam_ax.text(times[-1], 64 * N_STREAMS - 32, f"stream {N_STREAMS - 1} ",
                         color="#8b949e", ha="right", va="center", fontsize=7)
            beam_ax.set(title="Beam occupancy", ylabel="Beam index", ylim=(0, 512))
            _time_axis(beam_ax, data, mdates, zone)

            edges_dm = np.asarray(data["dm_edges"], dtype=float)
            # Drop the DM < 10 floor bucket from the display; normalise each
            # time column by all of its candidates so colour is a fraction.
            values = np.asarray(data["dm_time_counts"], dtype=float)
            column = values.sum(axis=1)
            fraction = np.zeros_like(values)
            np.divide(values, column[:, None], out=fraction, where=column[:, None] > 0)
            image = dm_ax.pcolormesh(times, edges_dm[1:],
                                     np.ma.masked_less_equal(fraction[:, 1:].T, 0),
                                     cmap="magma", vmin=0, vmax=1)
            for index, is_quiet in enumerate(data.get("quiet_bins") or []):
                if is_quiet:
                    dm_ax.axvspan(times[index], times[index + 1], facecolor=QUIET_COLOR,
                                  edgecolor="none", zorder=0)
            bar = fig.colorbar(image, cax=dm_cax, label="fraction of candidates in bin")
            dm_cax.set_axis_on()
            bar.ax.yaxis.label.set_color(FG)
            bar.ax.tick_params(colors=FG, labelsize=8)
            bar.outline.set_edgecolor(SPINE)
            dm_ax.set_yscale("log")
            dm_ax.set(title="DM distribution over time", ylabel="DM (pc cm⁻³)",
                      ylim=(edges_dm[1], edges_dm[-1]))
            _time_axis(dm_ax, data, mdates, zone)

            width_counts = np.asarray(data["width_counts"], dtype=float)
            width_ax.bar(np.arange(len(width_counts)), width_counts, color="#4cc9f0")
            width_ax.set(title="Width distribution", xlabel="Hella width index (not FWHM)",
                         ylabel="candidates")
            dm_counts = np.asarray(data["dm_counts"], dtype=float)
            dmhist_ax.stairs(dm_counts[1:], edges_dm[1:], fill=True, color="#4cc9f0")
            dmhist_ax.set_xscale("log")
            dmhist_ax.set(title="DM distribution", xlabel="DM (pc cm⁻³)", ylabel="candidates")

        if has_time:
            for ax in (act_ax, beam_ax, dm_ax):
                ax.set_xlim(times[0], times[-1])
        bounds = [datetime.fromisoformat(data[k].replace("Z", "+00:00")).astimezone(zone)
                  .strftime("%Y-%m-%d %H:%M") for k in ("t0", "t1")]
        label = "UTC" if data.get("time_tz") == "UTC" else "OVRO local"
        fig.suptitle(f"T1 · Hella streams · {bounds[0]} to {bounds[1]} {label}",
                     color=TITLE, fontsize=13)
        output = io.BytesIO()
        fig.savefig(output, format="png", dpi=130, facecolor=fig.get_facecolor())
        return output.getvalue()


def build_router(reader, settings):
    router = APIRouter(prefix="/api/t1", tags=["t1"])
    cache = {}
    lock = threading.Lock()
    ledger_db = hella_log.ledger_path(settings.observation_cache_root)

    def get(t0, t1, time_tz):
        # Short in-process cache: JSON and PNG requests for one view share a build.
        key = (t0, t1, time_tz)
        with lock:
            if key in cache and time.monotonic() - cache[key][0] < CACHE_TTL_S:
                return cache[key][1]
            result = build_t1(reader, t0=t0, t1=t1, ledger_db=ledger_db)
            result["time_tz"] = time_tz
            if len(cache) > 2:
                cache.clear()
            cache[key] = (time.monotonic(), result)
            return result

    @router.get("")
    def overview(t0: str | None = None, t1: str | None = None,
                 time_tz: Literal["America/Los_Angeles", "UTC"] = "America/Los_Angeles"):
        data = get(t0, t1, time_tz)
        if settings.observation_cache_root and "plot_url" not in data:
            content = render_t1(data)
            digest = hashlib.sha256(content).hexdigest()
            artifact_root = Path(settings.observation_cache_root).resolve()
            root = (artifact_root / "t1_products").resolve()
            if not root.is_relative_to(artifact_root):
                raise HTTPException(403, "T1 artifact directory escapes preview root")
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{digest}.png"
            if not path.exists():
                # Publish a complete image atomically; a same-hash replacement
                # has identical bytes and does not change product identity.
                with tempfile.NamedTemporaryFile(dir=root, suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            data["plot_url"] = f"/api/t1/products/{digest}.png"
        return data

    @router.get("/products/{filename}")
    def product(filename: str):
        if not settings.observation_cache_root or not re.fullmatch(r"[0-9a-f]{64}\.png", filename):
            raise HTTPException(404, "T1 product not found")
        root = (Path(settings.observation_cache_root).resolve() / "t1_products").resolve()
        path = (root / filename).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "T1 product not found")
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "private,max-age=31536000,immutable"})

    @router.get("/plot.png")
    def plot(t0: str | None = None, t1: str | None = None,
             time_tz: Literal["America/Los_Angeles", "UTC"] = "America/Los_Angeles"):
        return Response(render_t1(get(t0, t1, time_tz)), media_type="image/png",
                        headers={"Content-Disposition": 'inline; filename="casm-t1.png"'})

    return router
