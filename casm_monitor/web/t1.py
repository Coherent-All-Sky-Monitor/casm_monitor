"""Bounded scientific views of existing search bins and local Hella log evidence.

The deployed fork emits cluster peaks. Emitted row counts MUST NOT be used as
the denominator of the pre-clustering 10,000 raw-peak cap.
"""
from __future__ import annotations

import io
import hashlib
import json
import math
import os
import re
import sqlite3
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
from ..util import iso
from .search import window

MAX_ROWS = 700000
TIME_BINS = 480
LOG_BYTES = 2 * 1024 * 1024
LOG_PATH = Path("/data/casm/logs/bf_proc_hella.log")
NOTE = ("Counts are emitted T1 candidates (cluster peaks in the deployed fork), not the raw peaks "
        "subject to Hella's 10,000 cap per instance-gulp. Missing rows are not zero-candidate gulps. "
        "No fraction of searched or skipped beams is inferred from candidate occupancy.")
_PLOT_LOCK = threading.Lock()


def log_evidence(path=LOG_PATH, *, start, end):
    result = {"source": str(path), "status": "unavailable", "budget_bytes": LOG_BYTES,
              "cap_warnings": [], "complete_window": False,
              "note": "Bounded local-time log tail only; absence of warnings does not establish absence of saturation."}
    try:
        with Path(path).open("rb") as handle:
            size = handle.seek(0, 2)
            offset = max(0, size - LOG_BYTES)
            handle.seek(offset)
            lines = handle.read(LOG_BYTES).decode("utf-8", "replace").splitlines()
        if offset:
            lines = lines[1:]
        stamps = []
        for line in lines:
            match = re.search(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\]", line)
            if not match:
                continue
            stamp = datetime.fromisoformat(match[1]).replace(tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
            stamps.append(stamp)
            cap = re.search(r"Only processed (\d+)/(\d+) beams.*?(\d+) peaks", line)
            if cap and start <= stamp <= end:
                result["cap_warnings"].append({"utc": iso(stamp), "processed_beams": int(cap[1]),
                                               "instance_beams": int(cap[2]), "raw_peaks": int(cap[3]),
                                               "log_line": line[:1000]})
        result.update(status="partial", bytes_read=min(size, LOG_BYTES),
                      first_utc=iso(min(stamps)) if stamps else None,
                      last_utc=iso(max(stamps)) if stamps else None)
    except (OSError, ValueError) as exc:
        result["reason"] = str(exc)
    return result


def build_t1(reader, *, t0=None, t1=None, log_path=LOG_PATH):
    end_arg = str(time.time()) if t1 is None else t1
    start, end = window(t0, end_arg)
    if t0 is None:
        start = end - 86400
    if not math.isfinite(start) or not math.isfinite(end) or end - start > 7 * 86400:
        raise HTTPException(400, "T1 windows must be finite and at most seven days")
    edges = np.linspace(start, end, TIME_BINS + 1)
    beam = np.zeros((TIME_BINS, 512), dtype=np.int64)
    dm = np.zeros((TIME_BINS, len(DM_EDGES) - 1), dtype=np.int64)
    widths = np.zeros(len(WIDTH_EDGES) - 1, dtype=np.int64)
    totals = np.zeros((TIME_BINS, 8), dtype=np.int64)
    peaks = np.zeros((TIME_BINS, 8), dtype=np.int64)
    observed = np.zeros((TIME_BINS, 8), dtype=np.int64)
    result = {"status": "unavailable", "t0": iso(start), "t1": iso(end), "note": NOTE,
              "source": str(reader.db_path), "source_table": "cand_bins", "row_budget": MAX_ROWS,
              "log": log_evidence(log_path, start=start, end=end), "raw_peak_cap": 10000,
              "cap_fraction": None, "skipped_beams": None}
    n_rows = 0
    deadline = time.monotonic() + 8.0
    first = last = None
    try:
        connection = sqlite3.connect(Path(reader.db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            connection.execute("PRAGMA query_only=ON")
            cursor = connection.execute("SELECT gulp_ts, job, n, dm_hist, width_hist, beam_counts FROM cand_bins "
                                        "WHERE gulp_ts >= ? AND gulp_ts <= ? ORDER BY gulp_ts DESC LIMIT ?",
                                        (start, end, MAX_ROWS + 1))
            for stamp, job, count, dh, wh, bh in cursor:
                if n_rows >= MAX_ROWS or (n_rows % 1000 == 0 and time.monotonic() > deadline):
                    result["truncated"] = True
                    result["reason"] = "Search-bin row/time budget exceeded; newest retained bins shown, counts incomplete. Narrow the interval."
                    break
                n_rows += 1
                if not 0 <= job < 8:
                    raise ValueError("Invalid stored job index")
                idx = min(TIME_BINS - 1, max(0, int((stamp - start) / (end - start) * TIME_BINS)))
                d, w, b = np.asarray(json.loads(dh)), np.asarray(json.loads(wh)), np.asarray(json.loads(bh))
                if d.shape != (len(DM_EDGES) - 1,) or w.shape != widths.shape or b.shape != (64,):
                    raise ValueError("Stored histogram shape differs from collector contract")
                dm[idx] += d.astype(np.int64)
                widths += w.astype(np.int64)
                beam[idx, job * 64:(job + 1) * 64] += b.astype(np.int64)
                totals[idx, job] += count
                peaks[idx, job] = max(peaks[idx, job], count)
                observed[idx, job] += 1
                first = stamp if first is None else min(first, stamp)
                last = stamp if last is None else max(last, stamp)
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        result["reason"] = f"Search-bin evidence unavailable: {exc}"
        return result
    result.update(status="partial" if result.get("truncated") else ("ok" if n_rows else "empty"),
                  n_bin_rows=n_rows, n_candidates=int(totals.sum()),
                  first_data_utc=iso(first) if first is not None else None,
                  last_data_utc=iso(last) if last is not None else None,
                  time_edges_unix=edges.tolist(), time_bin_seconds=(end - start) / TIME_BINS,
                  per_job_counts=totals.tolist(), per_job_peak_per_gulp=peaks.tolist(),
                  observed_instance_gulps=observed.tolist(), beam_time_counts=beam.tolist(),
                  dm_time_counts=dm.tolist(), dm_edges=DM_EDGES, width_edges=WIDTH_EDGES,
                  width_counts=widths.tolist(), width_units="Hella width index, not FWHM",
                  coverage_note="Indexed monitor bins only; acquisition gaps and absent zero-candidate gulps are unknown. Per-time-bin curves show maximum emitted candidates in one stored instance-gulp, not sums over gulps.")
    return result


def render_t1(data):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    import matplotlib.dates as mdates
    from matplotlib.colors import LogNorm

    with _PLOT_LOCK:
        fig = Figure(figsize=(13, 10), layout="constrained", facecolor="#000000")
        FigureCanvasAgg(fig)
        axes = fig.subplots(2, 2)
        for ax in axes.flat:
            ax.set_facecolor("#000000")
            ax.tick_params(colors="#c9d1d9")
            ax.xaxis.label.set_color("#c9d1d9")
            ax.yaxis.label.set_color("#c9d1d9")
            ax.title.set_color("#e6edf3")
            for spine in ax.spines.values():
                spine.set_color("#53616d")
        if data["status"] not in ("ok", "partial", "empty"):
            for ax in axes.flat:
                ax.text(.5, .5, "Search evidence unavailable", color="white", ha="center", transform=ax.transAxes)
        elif not data["n_bin_rows"]:
            for ax in axes.flat:
                ax.text(.5, .5, "No stored candidate bins in this interval\nNot evidence of zero candidates", color="white", ha="center", transform=ax.transAxes)
        else:
            times = mdates.date2num([datetime.fromtimestamp(t, timezone.utc) for t in data["time_edges_unix"]])
            p = np.asarray(data["per_job_peak_per_gulp"], float)
            p[np.asarray(data["observed_instance_gulps"]) == 0] = np.nan
            for job in range(8):
                axes[0, 0].plot(times[:-1], p[:, job], linewidth=.8, label=f"job {job}")
            axes[0, 0].set(title="Emitted candidates per instance-gulp", ylabel="Maximum in display bin")
            axes[0, 0].legend(fontsize=7, ncol=4, facecolor="#dce1e5")
            for ax, key, yedges, title, label in (
                (axes[0, 1], "beam_time_counts", np.arange(513), "Candidate occupancy", "Beam index"),
                (axes[1, 0], "dm_time_counts", data["dm_edges"], "DM distribution over time", "DM (pc cm⁻³)"),
            ):
                values = np.asarray(data[key]).T
                masked = np.ma.masked_less_equal(values, 0)
                im = ax.pcolormesh(times, yedges, masked, cmap="inferno", norm=LogNorm(vmin=1, vmax=max(2, values.max())))
                colorbar = fig.colorbar(im, ax=ax, label="Emitted candidates")
                colorbar.ax.yaxis.label.set_color("#c9d1d9")
                colorbar.ax.tick_params(colors="#c9d1d9")
                ax.set(title=title, ylabel=label)
            axes[1, 1].bar(np.arange(len(data["width_counts"])), data["width_counts"], color="#70b4cb")
            axes[1, 1].set(title="Width distribution", xlabel="Hella width index (not FWHM)", ylabel="Emitted candidates")
            for ax in (axes[0, 0], axes[0, 1], axes[1, 0]):
                ax.xaxis_date()
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M", tz=ZoneInfo(data.get('time_tz','America/Los_Angeles'))))
                ax.set_xlabel('UTC' if data.get('time_tz') == 'UTC' else 'OVRO local (PDT/PST)')
        axes[1, 0].set_ylim(0,1000)
        zone = ZoneInfo(data.get('time_tz','America/Los_Angeles'))
        bounds = [datetime.fromisoformat(data[k].replace('Z','+00:00')).astimezone(zone).strftime('%Y-%m-%d %H:%M %Z') for k in ('t0','t1')]
        fig.suptitle(f"CASM · T1 search evidence\n{bounds[0]} to {bounds[1]}", color="#e6edf3", fontsize=12)
        output = io.BytesIO()
        fig.savefig(output, format="png", dpi=130, facecolor=fig.get_facecolor())
        return output.getvalue()


def build_router(reader, settings):
    router = APIRouter(prefix="/api/t1", tags=["t1"])
    cache = {}
    lock = threading.Lock()

    def get(t0, t1, time_tz):
        # Thirty-second in-process cache prevents duplicate JSON/PNG reads.
        key = (t0, t1, time_tz)
        with lock:
            if key in cache and time.monotonic() - cache[key][0] < 30:
                return cache[key][1]
            result = build_t1(reader, t0=t0, t1=t1)
            result['time_tz'] = time_tz
            result['display_dm_max'] = 1000
            result['display_note'] = 'DM panel displays 0–1000 pc cm⁻³; totals and exported bins retain all recorded candidates.'
            if len(cache) > 2:
                cache.clear()
            cache[key] = (time.monotonic(), result)
            return result

    @router.get("")
    def overview(t0: str | None = None, t1: str | None = None, time_tz: Literal['America/Los_Angeles', 'UTC'] = 'America/Los_Angeles'):
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
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private,max-age=31536000,immutable"})

    @router.get("/plot.png")
    def plot(t0: str | None = None, t1: str | None = None, time_tz: Literal['America/Los_Angeles', 'UTC'] = 'America/Los_Angeles'):
        return Response(render_t1(get(t0, t1, time_tz)), media_type="image/png", headers={"Content-Disposition": 'inline; filename="casm-t1.png"'})

    return router
