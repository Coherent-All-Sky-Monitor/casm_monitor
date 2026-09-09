"""The Search tab API: distributions of the raw hella (T1) candidates.

Everything here is read-only. The routes read our own ``cands`` /
``cand_bins`` / ``gulp_stats_mirror`` tables (written by
:mod:`casm_monitor.collectors.search`) through the app's read-only handle, plus
the ``hella.*`` threshold scalars. The one exception is the trigger count in
``/api/search/summary``, which counts rows in the t2 sqlite opened ``mode=ro``
(triggers are not a per-gulp quantity, so there is nothing to mirror); if that
file is missing or busy the field comes back ``null`` instead of failing the
request. The t2d sockets (12345-12352) are never touched.

Conventions shared by every route:

* ``t0``/``t1`` are ISO-8601 or unix seconds; ``t1`` defaults to now and ``t0``
  to one hour before ``t1``. A span longer than 30 d is CLAMPED (``t0`` moves
  up), not rejected, and the window actually used is echoed back.
* ``t1 <= t0``, an unknown ``field``/axis name and a non-numeric size are 400.
* A store whose search tables do not exist yet (collector never ran) answers
  with empty arrays, not 500, so the tab renders on a fresh store.
* ``width`` is a boxcar INDEX; its duration is ``2**width * 1.048576 ms`` (see
  the collector's module docstring for the wiki quotes behind that).
"""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Sequence

from fastapi import APIRouter, HTTPException, Query

from ..collectors.search import (
    BEAMS_PER_JOB,
    DM_EDGES,
    JOBS_CORR1,
    JOBS_CORR2,
    N_BEAMS,
    SNR_EDGES,
    TSAMP_S,
    WIDTH_EDGES,
    WIDTH_INDEX_MAX,
)
from ..config import Settings
from ..store import Store
from ..util import iso, parse_iso

MAX_SPAN_S = 30 * 86400.0
DEFAULT_SPAN_S = 3600.0
# Guards on one response. The histogram is one SQL GROUP BY over the whole
# window (never a capped row sample -- a storm hour can hold tens of millions
# of trials and every one of them has to count); the scatter route is the one
# that reads rows into Python, so IT stays bounded.
MAX_HIST_BINS = 2048
MAX_SCATTER_POINTS = 200_000
DEFAULT_SCATTER_POINTS = 20_000
MAX_SERIES_BINS = 5000
# Below this row count, a uniform random sample (``ORDER BY random()``) is
# cheap enough to draw directly in SQL; at or above it, an even STRIDE per job
# is used instead (still one pass, no full materialisation) so a storm that
# floods one job cannot make the scatter look like it came from just that job.
SCATTER_RANDOM_MAX_ROWS = 1_000_000

FIELDS = ("snr", "dm", "width", "beam")
AXES = {"dm": "dm", "snr": "snr", "width": "width", "time": "ts_unix", "beam": "beam"}
ALL_JOBS = JOBS_CORR1 + JOBS_CORR2


def node_for_job(job: int) -> str:
    """corr1 runs jobs 0-3, corr2 jobs 4-7."""
    return "corr1" if int(job) in JOBS_CORR1 else "corr2"


def _time_arg(field: str, text: str | None) -> float | None:
    """ISO-8601 or a unix timestamp; anything else is a 400."""
    if text is None or text == "":
        return None
    try:
        return float(text)
    except ValueError:
        pass
    parsed = parse_iso(text)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"{field} is not ISO-8601 or a unix timestamp")
    return parsed


def window(t0: str | None, t1: str | None, *, now: float | None = None) -> tuple[float, float]:
    """Resolve and clamp the requested time window."""
    t_now = time.time() if now is None else now
    end = _time_arg("t1", t1)
    start = _time_arg("t0", t0)
    end = t_now if end is None else end
    start = end - DEFAULT_SPAN_S if start is None else start
    if end <= start:
        raise HTTPException(status_code=400, detail="t1 must be after t0")
    if end - start > MAX_SPAN_S:
        start = end - MAX_SPAN_S
    return start, end


def step_and_bins(start: float, end: float, step_s: float) -> tuple[float, int]:
    """A step that keeps the number of bins under :data:`MAX_SERIES_BINS`.

    Raised, not refused: a 30 d window at 60 s is a legitimate request from a
    zoomed-out view, and silently returning 43200 bins is worse than returning
    the same curve at a coarser step (which the response reports).
    """
    if step_s <= 0:
        raise HTTPException(status_code=400, detail="step_s must be > 0")
    span = end - start
    n = int(span // step_s) + 1
    if n > MAX_SERIES_BINS:
        step_s = span / MAX_SERIES_BINS
        n = MAX_SERIES_BINS
    return float(step_s), max(1, n)


def linspace(lo: float, hi: float, n_bins: int) -> list[float]:
    step = (hi - lo) / n_bins
    return [lo + step * i for i in range(n_bins + 1)]


def log_edges(lo: float, hi: float, n_bins: int) -> list[float]:
    import math

    lo = max(lo, 1e-6)
    hi = max(hi, lo * 10.0)
    step = (math.log(hi) - math.log(lo)) / n_bins
    return [math.exp(math.log(lo) + step * i) for i in range(n_bins + 1)]


def field_edges(field: str, values: Sequence[float], n_bins: int, log: bool) -> list[float]:
    """Bin edges for one field.

    ``width`` and ``beam`` are integer-valued, so their edges are the integers
    (width 0..WIDTH_INDEX_MAX+1, i.e. the same 9 bins the collector stores per
    gulp; beam the 512-beam grid split into ``n_bins``) and ``log`` is ignored
    for them -- a log axis over a beam index is meaningless.
    """
    if field == "width":
        return list(WIDTH_EDGES)
    if field == "beam":
        return linspace(0.0, float(N_BEAMS), min(n_bins, N_BEAMS))
    if not values:
        return list(SNR_EDGES) if field == "snr" else list(DM_EDGES)
    lo, hi = min(values), max(values)
    if hi <= lo:
        hi = lo + max(abs(lo) * 0.01, 1.0)
    if log:
        return log_edges(max(lo, 1e-6), hi, n_bins)
    return linspace(lo, hi, n_bins)


def _field_minmax(reader: Store, column: str, start: float, end: float) -> tuple[float, float] | None:
    """``(MIN(column), MAX(column))`` over the window, or None with no rows.

    One aggregate query, never a row scan: the histogram's data-driven edges
    (``snr``/``dm``) need the TRUE range of the window, not the range of
    whatever earliest-N rows a capped read happened to pull in.
    """
    row = reader.query(
        f"SELECT MIN({column}) AS lo, MAX({column}) AS hi FROM cands "
        "WHERE ts_unix >= ? AND ts_unix <= ?",
        (start, end),
    )
    if not row or row[0]["lo"] is None:
        return None
    return float(row[0]["lo"]), float(row[0]["hi"])


def hist_counts_sql(
    reader: Store, column: str, start: float, end: float, edges: Sequence[float], log: bool
) -> list[int]:
    """Exact per-bin counts over the FULL window, one SQL ``GROUP BY``.

    ``edges`` is always uniformly spaced (linear or log -- see
    :func:`field_edges`: ``linspace``/``log_edges``/the fixed width and beam
    edges are all uniform), so each row's bin is a closed-form expression
    SQLite can evaluate itself; a storm gulp with tens of millions of trials
    is one aggregate pass, not tens of millions of Python floats (2026-09-09
    review: the old code capped at 2M rows ORDER BY ts_unix ASC, silently
    biasing every histogram toward the START of a busy window).
    """
    n = len(edges) - 1
    if n <= 0:
        return []
    lo, hi = float(edges[0]), float(edges[-1])
    if log:
        loglo = math.log(lo)
        step = (math.log(hi) - loglo) / n
        bucket_expr = f"CAST((LN(MAX({column}, {lo!r})) - ({loglo!r})) / ({step!r}) AS INTEGER)"
    else:
        step = (hi - lo) / n
        bucket_expr = f"CAST(({column} - ({lo!r})) / ({step!r}) AS INTEGER)"
    sql = (
        "SELECT CASE "
        f"WHEN {column} <= {lo!r} THEN 0 "
        f"WHEN {column} >= {hi!r} THEN {n - 1} "
        f"ELSE MIN(MAX({bucket_expr}, 0), {n - 1}) "
        "END AS b, COUNT(*) AS c "
        f"FROM cands WHERE ts_unix >= ? AND ts_unix <= ? GROUP BY b"
    )
    counts = [0] * n
    for row in reader.query(sql, (start, end)):
        if row["b"] is None:
            continue
        idx = int(row["b"])
        if 0 <= idx < n:
            counts[idx] += int(row["c"])
    return counts


def _balanced_allocation(counts: dict[int, int], cap: int) -> dict[int, int]:
    """Split ``cap`` points across ``counts`` proportional to each key's share.

    Largest-remainder method: floor each proportional share, then hand the
    leftover (from rounding) to the keys with the biggest fractional part, so
    the total allocated is exactly ``min(cap, sum(counts.values()))``.
    """
    total = sum(counts.values())
    if total <= 0 or cap <= 0:
        return {}
    cap = min(cap, total)
    raw = {job: n * cap / total for job, n in counts.items()}
    alloc = {job: min(int(v), counts[job]) for job, v in raw.items()}
    remaining = cap - sum(alloc.values())
    if remaining > 0:
        by_fraction = sorted(
            counts, key=lambda job: raw[job] - int(raw[job]), reverse=True
        )
        for job in by_fraction:
            if remaining <= 0:
                break
            if alloc[job] < counts[job]:
                alloc[job] += 1
                remaining -= 1
    return {job: n for job, n in alloc.items() if n > 0}


def _scatter_balanced_by_job(
    reader: Store, xcol: str, ycol: str, start: float, end: float, cap: int
) -> list[dict[str, Any]]:
    """Per-job-balanced stride sample: each job's share of ``cap`` points is
    proportional to its share of the rows, taken as an even stride of that
    job's own time-ordered rows (never a random sample at this scale -- one
    ``ORDER BY random()`` pass over tens of millions of rows is the thing this
    branch exists to avoid)."""
    job_counts = {
        int(row["job"]): int(row["n"])
        for row in reader.query(
            "SELECT job, COUNT(*) AS n FROM cands WHERE ts_unix >= ? AND ts_unix <= ? "
            "GROUP BY job",
            (start, end),
        )
    }
    alloc = _balanced_allocation(job_counts, cap)
    rows: list[dict[str, Any]] = []
    for job, job_cap in alloc.items():
        count = job_counts[job]
        stride = max(1, count // max(1, job_cap))
        rows.extend(
            reader.query(
                f"SELECT xv, yv, ts_unix FROM (SELECT {xcol} AS xv, {ycol} AS yv, ts_unix, "
                "ROW_NUMBER() OVER (ORDER BY ts_unix ASC) AS rn FROM cands "
                "WHERE ts_unix >= ? AND ts_unix <= ? AND job = ?) "
                "WHERE (rn - 1) % ? = 0 LIMIT ?",
                (start, end, job, stride, job_cap),
            )
        )
    rows.sort(key=lambda r: r["ts_unix"])
    return rows


def _table_exists(reader: Store, name: str) -> bool:
    return bool(
        reader.query("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,))
    )


def _threshold(reader: Store, node: str) -> dict[str, Any]:
    """``{"snr": .., "dm_min": ..}`` from the hella collector's scalars.

    The value is a float when all four jobs of that node agree and the
    collector's comma-joined string ("15,13") when they do not; both are passed
    through as-is rather than being averaged into a number that is not the
    threshold of any job.
    """
    out: dict[str, Any] = {}
    for key, name in (("snr", f"hella.{node}.snr"), ("dm_min", f"hella.{node}.dm_min")):
        row = reader.latest_scalar(name)
        out[key] = None if row is None else row["value"]
    return out


def _t2_trigger_count(t2_db: Path, start: float, end: float) -> int | None:
    """Triggered rows in the window, from the t2 sqlite (``mode=ro``).

    t2 writes ISO-8601 with a 'T' separator and an offset, so the comparison is
    against ISO strings built the same way -- never against sqlite's
    ``datetime('now')``, which formats with a space and compares unequal.
    """
    if not Path(t2_db).is_file():
        return None
    lo, hi = iso(start), iso(end)
    try:
        conn = sqlite3.connect(f"file:{Path(t2_db)}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM triggers WHERE action = 'triggered' "
            "AND created_utc >= ? AND created_utc <= ?",
            (lo, hi),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def build_router(reader: Store, settings: Settings) -> APIRouter:
    """The Search router (mounted by the app)."""
    router = APIRouter(prefix="/api/search", tags=["search"])

    def have_cands() -> bool:
        return _table_exists(reader, "cands")

    def have_gulps() -> bool:
        return _table_exists(reader, "gulp_stats_mirror")

    def count_in(start: float, end: float) -> int:
        if not have_cands():
            return 0
        return int(reader.query(
            "SELECT COUNT(*) AS n FROM cands WHERE ts_unix >= ? AND ts_unix <= ?", (start, end)
        )[0]["n"])

    @router.get("/summary")
    def summary(t0: str | None = None, t1: str | None = None) -> dict[str, Any]:
        start, end = window(t0, t1)
        span_min = max((end - start) / 60.0, 1e-9)
        per_job_rows: dict[int, dict[str, Any]] = {}
        n_cands = 0
        if have_cands():
            for row in reader.query(
                "SELECT job, COUNT(*) AS n, MAX(ts_unix) AS last_ts FROM cands "
                "WHERE ts_unix >= ? AND ts_unix <= ? GROUP BY job",
                (start, end),
            ):
                per_job_rows[int(row["job"])] = {
                    "n": int(row["n"]),
                    "last_ts": row["last_ts"],
                }
                n_cands += int(row["n"])
        per_job = []
        for job in ALL_JOBS:
            entry = per_job_rows.get(job, {"n": 0, "last_ts": None})
            per_job.append(
                {
                    "job": job,
                    "node": node_for_job(job),
                    "n": entry["n"],
                    "rate_per_min": round(entry["n"] / span_min, 4),
                    # Unix seconds, per docs/api-search.md ("response-body
                    # timestamps are unix seconds"); the ISO form is an extra.
                    "last_ts": entry["last_ts"],
                    "last_ts_iso": iso(entry["last_ts"]) if entry["last_ts"] is not None else None,
                }
            )
        funnel: dict[str, Any] = {
            "n_cands": 0,
            "n_clusters": 0,
            "n_stored": 0,
            "n_vetoed": 0,
            "n_triggers": None,
        }
        if have_gulps():
            row = reader.query(
                "SELECT COALESCE(SUM(n_cands), 0) AS n_cands, "
                "COALESCE(SUM(n_clusters), 0) AS n_clusters, "
                "COALESCE(SUM(n_stored), 0) AS n_stored, "
                "COALESCE(SUM(n_vetoed), 0) AS n_vetoed "
                "FROM gulp_stats_mirror WHERE ts_unix >= ? AND ts_unix <= ?",
                (start, end),
            )[0]
            funnel.update(
                {
                    "n_cands": int(row["n_cands"]),
                    "n_clusters": int(row["n_clusters"]),
                    "n_stored": int(row["n_stored"]),
                    "n_vetoed": int(row["n_vetoed"]),
                }
            )
        funnel["n_triggers"] = _t2_trigger_count(Path(settings.t2_db), start, end)
        return {
            "t0": iso(start),
            "t1": iso(end),
            "n_cands": n_cands,
            "per_job": per_job,
            "thresholds": {"corr1": _threshold(reader, "corr1"), "corr2": _threshold(reader, "corr2")},
            "funnel": funnel,
        }

    @router.get("/hist")
    def hist(
        field: str = Query(default="snr"),
        t0: str | None = None,
        t1: str | None = None,
        bins: int = Query(default=40, ge=1, le=MAX_HIST_BINS),
        log: int = Query(default=0, ge=0, le=1),
    ) -> dict[str, Any]:
        if field not in FIELDS:
            raise HTTPException(status_code=400, detail=f"field must be one of {'|'.join(FIELDS)}")
        start, end = window(t0, t1)
        # The edges are data-driven (snr/dm) from the window's TRUE min/max
        # (one aggregate query), never from a capped sample of rows; the
        # counts are then a single SQL GROUP BY over the whole window, so a
        # storm gulp's millions of extra trials all still count.
        rng = _field_minmax(reader, field, start, end) if have_cands() else None
        edges = field_edges(field, [] if rng is None else [rng[0], rng[1]], int(bins), bool(log))
        counts = hist_counts_sql(reader, field, start, end, edges, bool(log)) if rng else [0] * (
            len(edges) - 1
        )
        return {
            "field": field,
            "t0": iso(start),
            "t1": iso(end),
            "log": bool(log),
            "edges": [round(float(e), 6) for e in edges],
            "counts": counts,
            "n_total": count_in(start, end),
            "n_used": sum(counts),
        }

    @router.get("/scatter")
    def scatter(
        x: str = Query(default="dm"),
        y: str = Query(default="snr"),
        t0: str | None = None,
        t1: str | None = None,
        max_points: int = Query(default=DEFAULT_SCATTER_POINTS, ge=1, le=MAX_SCATTER_POINTS),
    ) -> dict[str, Any]:
        for name, axis in (("x", x), ("y", y)):
            if axis not in AXES:
                raise HTTPException(
                    status_code=400, detail=f"{name} must be one of {'|'.join(AXES)}"
                )
        start, end = window(t0, t1)
        if not have_cands():
            return {"x": [], "y": [], "n_total": 0, "x_field": x, "y_field": y}
        n_total = int(reader.query(
            "SELECT COUNT(*) AS n FROM cands WHERE ts_unix >= ? AND ts_unix <= ?", (start, end)
        )[0]["n"])
        cap = int(max_points)
        xcol, ycol = AXES[x], AXES[y]
        if n_total <= cap:
            rows = reader.query(
                f"SELECT {xcol} AS xv, {ycol} AS yv FROM cands "
                "WHERE ts_unix >= ? AND ts_unix <= ? ORDER BY ts_unix ASC",
                (start, end),
            )
        elif n_total < SCATTER_RANDOM_MAX_ROWS:
            # A uniform TIME stride (the old approach) aliases with anything
            # periodic in the arrival pattern (a job's own gulp cadence, an
            # RFI burst that recurs every Nth trial); a uniform RANDOM sample
            # does not, and is still one SQL pass with no full materialisation.
            # Re-sorted by time afterward so the plotted cloud is still drawn
            # in time order (matches the <=cap and the old-stride behaviour).
            rows = reader.query(
                f"SELECT xv, yv FROM (SELECT {xcol} AS xv, {ycol} AS yv, ts_unix FROM cands "
                "WHERE ts_unix >= ? AND ts_unix <= ? ORDER BY RANDOM() LIMIT ?) "
                "ORDER BY ts_unix ASC",
                (start, end, cap),
            )
        else:
            # At storm scale, a random sample can still come back dominated by
            # whichever job happens to be flooding: split the budget evenly
            # across the jobs that have rows in the window and stride each
            # one's OWN time-ordered rows independently, so no single job's
            # storm can crowd out the others' points.
            rows = _scatter_balanced_by_job(reader, xcol, ycol, start, end, cap)
        return {
            "x": [float(r["xv"]) for r in rows],
            "y": [float(r["yv"]) for r in rows],
            "n_total": n_total,
            "x_field": x,
            "y_field": y,
            "tsamp_s": TSAMP_S,
        }

    @router.get("/beam-map")
    def beam_map(t0: str | None = None, t1: str | None = None) -> dict[str, Any]:
        start, end = window(t0, t1)
        counts = [0] * N_BEAMS
        if have_cands():
            for row in reader.query(
                "SELECT beam, COUNT(*) AS n FROM cands WHERE ts_unix >= ? AND ts_unix <= ? "
                "GROUP BY beam",
                (start, end),
            ):
                beam = int(row["beam"])
                if 0 <= beam < N_BEAMS:
                    counts[beam] = int(row["n"])
        return {
            "t0": iso(start),
            "t1": iso(end),
            "counts": counts,
            "beams_per_job": BEAMS_PER_JOB,
        }

    def bin_axis(start: float, step: float, n: int) -> list[float]:
        return [start + step * i for i in range(n)]

    @router.get("/rate")
    def rate(
        t0: str | None = None,
        t1: str | None = None,
        step_s: float = Query(default=60.0, gt=0.0),
    ) -> dict[str, Any]:
        start, end = window(t0, t1)
        step, n = step_and_bins(start, end, float(step_s))
        per_job = {str(job): [0.0] * n for job in ALL_JOBS}
        total = [0.0] * n
        if have_cands():
            for row in reader.query(
                "SELECT job, CAST((ts_unix - ?) / ? AS INTEGER) AS b, COUNT(*) AS n "
                "FROM cands WHERE ts_unix >= ? AND ts_unix <= ? GROUP BY job, b",
                (start, step, start, end),
            ):
                index = int(row["b"])
                if not (0 <= index < n):
                    continue
                # Counts per bin -> a rate per minute, so the curve does not
                # change shape when the operator changes step_s.
                value = round(int(row["n"]) * 60.0 / step, 4)
                key = str(int(row["job"]))
                if key in per_job:
                    per_job[key][index] += value
                total[index] += value
        return {
            "t0": iso(start),
            "t1": iso(end),
            "step_s": step,
            "units": "candidates per minute",
            "t": bin_axis(start, step, n),
            "t_iso": [iso(v) for v in bin_axis(start, step, n)],
            "per_job": per_job,
            "total": [round(v, 4) for v in total],
        }

    @router.get("/funnel")
    def funnel(
        t0: str | None = None,
        t1: str | None = None,
        step_s: float = Query(default=600.0, gt=0.0),
    ) -> dict[str, Any]:
        start, end = window(t0, t1)
        step, n = step_and_bins(start, end, float(step_s))
        series = {key: [0] * n for key in ("n_cands", "n_clusters", "n_stored", "n_vetoed")}
        if have_gulps():
            for row in reader.query(
                "SELECT CAST((ts_unix - ?) / ? AS INTEGER) AS b, "
                "COALESCE(SUM(n_cands), 0) AS n_cands, "
                "COALESCE(SUM(n_clusters), 0) AS n_clusters, "
                "COALESCE(SUM(n_stored), 0) AS n_stored, "
                "COALESCE(SUM(n_vetoed), 0) AS n_vetoed "
                "FROM gulp_stats_mirror WHERE ts_unix >= ? AND ts_unix <= ? GROUP BY b",
                (start, step, start, end),
            ):
                index = int(row["b"])
                if not (0 <= index < n):
                    continue
                for key in series:
                    series[key][index] += int(row[key])
        return {
            "t0": iso(start),
            "t1": iso(end),
            "step_s": step,
            "t": bin_axis(start, step, n),
            "t_iso": [iso(v) for v in bin_axis(start, step, n)],
            **series,
        }

    return router


__all__ = [
    "build_router",
    "window",
    "step_and_bins",
    "field_edges",
    "node_for_job",
    "WIDTH_INDEX_MAX",
]
