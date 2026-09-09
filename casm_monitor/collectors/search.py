"""Raw hella (T1) candidates: tail the eight ``cands_<UTC_START>.dat.<job>``
files and mirror the T2 funnel out of the t2 sqlite.

Sources, all read-only:

* corr1 jobs 0-3 — the files under ``settings.hella_cands_dir`` are local, read
  with ``open``/``seek`` at a byte watermark;
* corr2 jobs 4-7 — the same path on ``settings.corr2_ssh``, pulled with ONE ssh
  per tick that ships the four per-job blocks in one stream (the
  ``=== <job> <size>`` marker pattern of :mod:`casm_monitor.collectors.hella`),
  each block starting at that job's own byte watermark so only new bytes cross
  the wire;
* ``settings.t2_db`` (``/mnt/nvme5/casm_pipeline/db/t2.sqlite``) opened
  ``mode=ro``, whose ``gulp_stats`` rows are copied into our own
  ``gulp_stats_mirror`` so the cands -> clusters -> stored funnel survives a t2
  restart or a t2 database rotation.

The t2d sockets (12345-12352) belong to t2d and are NEVER touched: this
collector only reads files and a read-only sqlite handle.

File format (7 whitespace columns, one header line ``SNR SAMP_START
TIME_START WIDTH DM_IDX DM BEAM_IDX``)::

    snr  samp  time_days  width  dm_idx  dm  beam
    26.0045 8306705 0.100813 6 2 21.0372 0

``samp`` is the sample index since UTC_START, so
``time_unix = utc_start + samp * 1.048576e-3``; ``beam`` is already the global
0-511 index (job ``j`` covers beams ``64j .. 64j+63``).

Width and SNR encoding
----------------------
``width`` is a boxcar *index*, not a duration: hella searches an octave grid
(``WIDTH_RATIO 2.0`` in the live cfg), so the duration is
``2**width * 1.048576 ms``. The wiki's own words, from
``casm-wiki/hella-t1-dm-floor-bucket.md``:

    "Live search config (`/tmp/hella_2.cfg`, log line 2026-08-24): DM range
    30-1000, widths 1-128 samples (7 doubling boxcar trials, 1.049 ms
    sampling), SNR 15."
    "Width boxcar index 5 (33.6 ms): 251 of 506; index 6 (67 ms): zero."

and from ``casm-wiki/hella-vs-transientx.md``:

    "**WIDTH_RATIO stays 2.0, not the fork default 1.5.** The production octave
    boxcar grid is what t2d's `veto_widths [6]` and `width_scale 2.0` are
    written against; ratio 1.5 renumbers the width index and needs a paired t2d
    change."

The SNR side of the encoding is the live threshold: ``casm-wiki/
crab-gp-search.md`` certifies its 2026-08-17 non-detection as "no GP exceeded
hella's SNR 15 threshold in the ~94% of the window that was searched", i.e. the
per-trial SNR floor of everything in these files is the ``SNR`` key of
``/tmp/hella_<job>.cfg`` (15 today, DM_MIN 20), collected by
:mod:`casm_monitor.collectors.hella`. NOTE (2026-09-08): that page carries the
threshold and the width statistics quoted above but no separate "width/SNR
encoding" section; the octave-index rule above is the encoding, taken from the
two hella pages.

Tables (created lazily from here, ``CREATE TABLE IF NOT EXISTS``, so the shared
schema module is untouched):

``cands``
    one row per T1 trial, kept ``raw_ttl_days`` (7 d);
``cand_bins``
    per-gulp binned counts and fixed-edge histograms, kept forever;
``gulp_stats_mirror``
    our copy of the t2 ``gulp_stats`` rows.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ..config import Settings
from ..store import Store
from ..util import parse_iso, utc_start_to_unix
from .base import Collector, CollectorContext

# -- constants -------------------------------------------------------------
TSAMP_S = 1.048576e-3
"""Deployed hella sample time. NOT 1.0 ms (the repo source still says 1.0)."""

GULP_SAMPLES = 8192
"""``GULP`` in the live hella cfg: the file is appended one gulp at a time, so
gulp boundaries are the natural per-gulp bin edges (8192 * tsamp = 8.59 s)."""

JOBS_CORR1 = (0, 1, 2, 3)
JOBS_CORR2 = (4, 5, 6, 7)
BEAMS_PER_JOB = 64
N_BEAMS = 512
WIDTH_INDEX_MAX = 8
"""Highest boxcar index we bin (``WIDTH_MAX 128`` = 2**7, one spare bin)."""

MARKER = b"=== "
_NAME_RE = re.compile(r"^cands_(?P<obs>[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9:]{8})\.dat\.(?P<job>\d+)$")
# The obs string is interpolated into a remote shell command (corr2_command);
# this is the one gate that keeps it a harmless filename fragment.
OBS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}:\d{2}:\d{2}$")

WATERMARK_STREAM = "search"
OBS_KEY = "obs"
GULP_STATS_KEY = "gulp_stats_utc"


def _geomspace(lo: float, hi: float, n: int) -> list[float]:
    """``numpy.geomspace`` without importing numpy in the collector process."""
    step = (math.log(hi) - math.log(lo)) / (n - 1)
    return [math.exp(math.log(lo) + step * i) for i in range(n)]


# Fixed histogram edges. Fixed on purpose: a per-gulp histogram is only
# summable across gulps (which is the whole point of keeping it forever) if
# every gulp used the same edges. Out-of-range values are clipped into the first
# or last bin rather than dropped, so the counts always add up to ``n``.
SNR_EDGES = [round(v, 6) for v in _geomspace(15.0, 100.0, 25)]        # 24 bins
DM_EDGES = [0.0] + [round(v, 6) for v in _geomspace(10.0, 3000.0, 30)]  # 30 bins
WIDTH_EDGES = [float(i) for i in range(WIDTH_INDEX_MAX + 2)]           # 9 bins


@dataclass(frozen=True)
class SearchConfig:
    """Search-collector settings.

    Defaults are the production values. They are overridden from the YAML's
    ``search:`` block when the :class:`~casm_monitor.config.Settings` object
    exposes the raw mapping (it does not today, and that module is owned
    elsewhere), and by ``CASM_MONITOR_SEARCH_*`` environment variables, which is
    how a smoke run points the collector at a scratch directory.
    """

    cands_dir: Path = Path("/mnt/nvme4/data/casm/hella_cands")
    t2_db: Path = Path("/mnt/nvme5/casm_pipeline/db/t2.sqlite")
    corr2_ssh: str = "casm-corr2"
    cadence_s: float = 20.0
    raw_ttl_days: float = 7.0
    backfill_hours: float = 24.0
    # Per file, per tick. A gulp adds a few kB; a storm gulp adds far more, and
    # this is the ceiling on both the ssh payload and one INSERT batch.
    max_tick_bytes: int = 8 << 20
    # Per file, once, on first start: how far back we scan for the backfill.
    max_backfill_bytes: int = 16 << 20
    ssh_timeout_s: float = 30.0
    retention_interval_s: float = 900.0
    # Window used for the ``search.rate_per_min.*`` scalars. Wider than the
    # cadence because hella's writes are bursty (buffered per block), so a
    # 20 s window reads as zero most ticks.
    rate_window_s: float = 300.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def search_config(settings: Settings) -> SearchConfig:
    """Build the config from ``settings``, the YAML ``search:`` block and env."""
    defaults = SearchConfig()
    raw = getattr(settings, "raw", None)
    block: dict[str, Any] = {}
    if isinstance(raw, dict):
        candidate = raw.get("search")
        if isinstance(candidate, dict):
            block = candidate
    cfg = SearchConfig(
        cands_dir=Path(block.get("cands_dir", getattr(settings, "hella_cands_dir", defaults.cands_dir))),
        t2_db=Path(block.get("t2_db", getattr(settings, "t2_db", defaults.t2_db))),
        corr2_ssh=str(block.get("corr2_ssh", getattr(settings, "corr2_ssh", defaults.corr2_ssh))),
        cadence_s=float(block.get("cadence_s", settings.cadence("search", defaults.cadence_s))),
        raw_ttl_days=float(block.get("raw_ttl_days", defaults.raw_ttl_days)),
        backfill_hours=float(block.get("backfill_hours", defaults.backfill_hours)),
        max_tick_bytes=int(block.get("max_tick_bytes", defaults.max_tick_bytes)),
        max_backfill_bytes=int(block.get("max_backfill_bytes", defaults.max_backfill_bytes)),
        ssh_timeout_s=float(block.get("ssh_timeout_s", defaults.ssh_timeout_s)),
        retention_interval_s=float(block.get("retention_interval_s", defaults.retention_interval_s)),
        rate_window_s=float(block.get("rate_window_s", defaults.rate_window_s)),
    )
    env_dir = os.environ.get("CASM_MONITOR_SEARCH_CANDS_DIR")
    env_t2 = os.environ.get("CASM_MONITOR_SEARCH_T2_DB")
    return SearchConfig(
        cands_dir=Path(env_dir) if env_dir else cfg.cands_dir,
        t2_db=Path(env_t2) if env_t2 else cfg.t2_db,
        corr2_ssh=os.environ.get("CASM_MONITOR_SEARCH_CORR2_SSH") or cfg.corr2_ssh,
        cadence_s=_env_float("CASM_MONITOR_SEARCH_CADENCE_S", cfg.cadence_s),
        raw_ttl_days=_env_float("CASM_MONITOR_SEARCH_RAW_TTL_DAYS", cfg.raw_ttl_days),
        backfill_hours=_env_float("CASM_MONITOR_SEARCH_BACKFILL_HOURS", cfg.backfill_hours),
        max_tick_bytes=_env_int("CASM_MONITOR_SEARCH_MAX_TICK_BYTES", cfg.max_tick_bytes),
        max_backfill_bytes=_env_int(
            "CASM_MONITOR_SEARCH_MAX_BACKFILL_BYTES", cfg.max_backfill_bytes
        ),
        ssh_timeout_s=_env_float("CASM_MONITOR_SEARCH_SSH_TIMEOUT_S", cfg.ssh_timeout_s),
        retention_interval_s=_env_float(
            "CASM_MONITOR_SEARCH_RETENTION_INTERVAL_S", cfg.retention_interval_s
        ),
        rate_window_s=_env_float("CASM_MONITOR_SEARCH_RATE_WINDOW_S", cfg.rate_window_s),
    )


# -- our tables ------------------------------------------------------------
TABLES: tuple[str, ...] = (
    # One row per raw T1 trial. Deliberately without a primary key: the
    # watermark, not a uniqueness constraint, is what makes ingest idempotent,
    # and duplicate (samp, beam, dm_idx, width) rows are legitimate in the
    # file (different boxcar/DM trials of the same event can coincide).
    """CREATE TABLE IF NOT EXISTS cands (
        ts_unix REAL NOT NULL,
        job     INTEGER NOT NULL,
        node    TEXT NOT NULL,
        snr     REAL NOT NULL,
        width   INTEGER NOT NULL,
        dm      REAL NOT NULL,
        dm_idx  INTEGER NOT NULL,
        beam    INTEGER NOT NULL,
        samp    INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS cands_ts ON cands (ts_unix)",
    "CREATE INDEX IF NOT EXISTS cands_job_ts ON cands (job, ts_unix)",
    "CREATE INDEX IF NOT EXISTS cands_beam_ts ON cands (beam, ts_unix)",
    # Per-gulp bins, kept forever. Histograms are json arrays of counts against
    # the fixed module-level edges; ``beam_counts`` is 64 long (this job's
    # slice of the 512-beam grid, index = beam - 64 * job).
    """CREATE TABLE IF NOT EXISTS cand_bins (
        gulp_ts       REAL NOT NULL,
        job           INTEGER NOT NULL,
        n             INTEGER NOT NULL,
        snr_hist      TEXT NOT NULL,
        dm_hist       TEXT NOT NULL,
        width_hist    TEXT NOT NULL,
        beam_counts   TEXT NOT NULL,
        snr_max       REAL,
        dm_at_snr_max REAL,
        PRIMARY KEY (gulp_ts, job)
    )""",
    "CREATE INDEX IF NOT EXISTS cand_bins_ts ON cand_bins (gulp_ts)",
    # Our copy of t2's gulp_stats. ``gulp_utc`` is t2's own ISO string (with a
    # 'T' separator and an offset -- never comparable to sqlite's
    # datetime('now'), which uses a space); ``ts_unix`` is the parsed form we
    # actually query on.
    """CREATE TABLE IF NOT EXISTS gulp_stats_mirror (
        gulp_utc      TEXT PRIMARY KEY,
        ts_unix       REAL NOT NULL,
        obs_utc_start TEXT,
        gulp          INTEGER,
        n_jobs        INTEGER,
        n_cands       INTEGER,
        n_clusters    INTEGER,
        n_stored      INTEGER,
        n_would       INTEGER,
        n_vetoed      INTEGER,
        n_shed        INTEGER,
        clustering_ms REAL
    )""",
    "CREATE INDEX IF NOT EXISTS gulp_stats_mirror_ts ON gulp_stats_mirror (ts_unix)",
)


def ensure_tables(store: Store) -> None:
    """Create our three tables if they do not exist (idempotent, cheap)."""
    for sql in TABLES:
        store.execute(sql)


# -- pure parsing ----------------------------------------------------------
def obs_and_job(name: str) -> tuple[str, int] | None:
    """``cands_2026-09-04-16:42:39.dat.3`` -> ``('2026-09-04-16:42:39', 3)``."""
    match = _NAME_RE.match(Path(name).name)
    if match is None:
        return None
    return match.group("obs"), int(match.group("job"))


def cands_path(cands_dir: Path | str, obs: str, job: int) -> str:
    return f"{Path(cands_dir)}/cands_{obs}.dat.{job}"


def find_current_obs(cands_dir: Path | str, jobs: Sequence[int] = JOBS_CORR1) -> str | None:
    """The UTC_START of the newest observation present in ``cands_dir``.

    "Newest" is by file mtime, not by name: after an obs restart the new files
    appear immediately but the previous obs's name can sort higher (a manual
    replay, a clock nudge), and the mtime is what tells us which set hella is
    actually appending to.
    """
    best: tuple[float, str] | None = None
    directory = Path(cands_dir)
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return None
    for entry in entries:
        parsed = obs_and_job(entry.name)
        if parsed is None or parsed[1] not in set(jobs):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, parsed[0])
    return None if best is None else best[1]


def split_job_blocks(data: bytes) -> dict[int, tuple[int, bytes]]:
    """Split the corr2 stream into ``{job: (size, payload)}``.

    Explicit byte-length framing: each marker line is ``=== <job> <size>
    <nbytes>\\n`` and is followed by EXACTLY ``nbytes`` raw bytes, whatever they
    contain. The next marker starts immediately after those bytes, so parsing
    slices by the declared length and never searches the payload for the next
    ``=== `` -- a candidate row that happens to start with those four
    characters, or a payload with no trailing newline, cannot merge into the
    next job's block or corrupt its offset. A ``size`` of -1 means the file did
    not exist on the remote node (``nbytes`` is then 0).
    """
    blocks: dict[int, tuple[int, bytes]] = {}
    pos = 0
    n = len(data)
    while pos < n:
        eol = data.find(b"\n", pos)
        if eol < 0:
            break  # a truncated marker line: nothing more to parse safely
        line = data[pos:eol]
        pos = eol + 1
        if not line.startswith(MARKER):
            break  # protocol desync: stop rather than guess
        parts = line[len(MARKER):].split()
        if len(parts) < 3:
            break
        try:
            job, size, nbytes = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            break
        if nbytes < 0 or pos + nbytes > n:
            break  # short read (ssh cut off mid-stream): drop the partial tail
        blocks[job] = (size, data[pos : pos + nbytes])
        pos += nbytes
    return blocks


def corr2_command(
    cands_dir: Path | str, obs: str, offsets: dict[int, int], max_tick_bytes: int
) -> str:
    """The single shell command sent to corr2.

    One ssh per tick for all four jobs. Every job's payload is capped remotely
    at ``max_tick_bytes`` -- normal tails as well as backfill -- so neither a
    storm gulp nor a first-start backfill can ship more than that over the
    wire; whatever is left is picked up on the following ticks because the
    byte watermark only advances by what was actually consumed. Each job's
    remote path is ``shlex.quote``-d, and ``obs`` is validated by the caller
    before it ever reaches this function.

    A NEGATIVE offset means "the last ``|offset|`` bytes counted back from the
    CURRENT size" (the first-start backfill, where the local watermark does
    not know the remote size yet); the actual starting offset is computed
    remotely from the freshly stat-ed size and reported back in the ``size``
    field of the marker so the caller can derive it (``size - len(payload)``
    when the payload was not truncated by the cap).
    """
    if not OBS_RE.match(str(obs)):
        raise ValueError(f"unsafe obs string for a remote command: {obs!r}")
    cap = max(0, int(max_tick_bytes))
    stmts: list[str] = []
    for job in sorted(offsets):
        offset = int(offsets[job])
        path = shlex.quote(cands_path(cands_dir, obs, job))
        start_expr = f"sz - {abs(offset)}" if offset < 0 else str(offset)
        stmts.append(
            f'if [ ! -f {path} ]; then echo "=== {job} -1 0"; else '
            f": '{job}:{offset}'; "  # no-op, keeps the (job, offset) pair greppable
            f"sz=$(stat -c %s {path}); "
            f"start=$(( {start_expr} )); [ \"$start\" -lt 0 ] && start=0; "
            f"avail=$((sz - start)); [ \"$avail\" -lt 0 ] && avail=0; "
            f'send=$avail; [ "$send" -gt {cap} ] && send={cap}; '
            f'echo "=== {job} $sz $send"; '
            f'tail -c +$((start+1)) {path} | head -c "$send"; fi'
        )
    return "; ".join(stmts)


@dataclass(frozen=True)
class Cand:
    """One parsed T1 trial."""

    ts_unix: float
    job: int
    node: str
    snr: float
    width: int
    dm: float
    dm_idx: int
    beam: int
    samp: int

    def row(self) -> tuple[Any, ...]:
        return (
            self.ts_unix,
            self.job,
            self.node,
            self.snr,
            self.width,
            self.dm,
            self.dm_idx,
            self.beam,
            self.samp,
        )


def parse_chunk(
    chunk: bytes,
    *,
    job: int,
    node: str,
    utc_start_unix: float,
    drop_first_partial: bool = False,
) -> tuple[list[Cand], int, int]:
    """Parse a byte chunk of a cands file.

    Returns ``(cands, consumed_bytes, n_bad)``. ``consumed_bytes`` counts only
    COMPLETE lines: an unterminated last line is a row hella is still writing,
    so it is left for the next tick instead of being parsed half-formed (and the
    byte watermark is advanced by exactly the consumed prefix).

    ``drop_first_partial`` is for the backfill read, which starts at an
    arbitrary byte offset and therefore begins mid-row.
    """
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], 0, 0
    consumed = end + 1
    body = chunk[:consumed]
    lines = body.split(b"\n")[:-1]
    if drop_first_partial and lines:
        lines = lines[1:]
    cands: list[Cand] = []
    n_bad = 0
    for raw in lines:
        parsed = parse_line(raw, job=job, node=node, utc_start_unix=utc_start_unix)
        if parsed is None:
            # The header line (``SNR SAMP_START ...``) lands here too, which is
            # why a bad line is counted and never logged per row.
            n_bad += 1
            continue
        cands.append(parsed)
    return cands, consumed, n_bad


def parse_line(raw: bytes | str, *, job: int, node: str, utc_start_unix: float) -> Cand | None:
    """One ``snr samp time_days width dm_idx dm beam`` row, or None."""
    text = raw.decode("ascii", "replace") if isinstance(raw, bytes) else raw
    parts = text.split()
    if len(parts) != 7:
        return None
    try:
        snr = float(parts[0])
        samp = int(parts[1])
        width = int(float(parts[3]))
        dm_idx = int(float(parts[4]))
        dm = float(parts[5])
        beam = int(float(parts[6]))
    except ValueError:
        return None
    if not (0 <= beam < N_BEAMS) or samp < 0:
        return None
    return Cand(
        ts_unix=utc_start_unix + samp * TSAMP_S,
        job=job,
        node=node,
        snr=snr,
        width=width,
        dm=dm,
        dm_idx=dm_idx,
        beam=beam,
        samp=samp,
    )


# -- binning ---------------------------------------------------------------
def hist_index(value: float, edges: Sequence[float]) -> int:
    """Bin index of ``value`` against ``edges``, clipped into range."""
    n = len(edges) - 1
    if value <= edges[0]:
        return 0
    if value >= edges[-1]:
        return n - 1
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if value < edges[mid + 1]:
            hi = mid
        else:
            lo = mid + 1
    return min(lo, n - 1)


def histogram(values: Iterable[float], edges: Sequence[float]) -> list[int]:
    counts = [0] * (len(edges) - 1)
    for value in values:
        counts[hist_index(value, edges)] += 1
    return counts


def gulp_ts(samp: int, utc_start_unix: float, gulp_samples: int = GULP_SAMPLES) -> float:
    """Start time of the gulp a sample belongs to (the file's append unit)."""
    return utc_start_unix + (int(samp) // int(gulp_samples)) * gulp_samples * TSAMP_S


@dataclass
class Bin:
    """One (gulp, job) aggregate, mergeable with an already stored row."""

    n: int = 0
    snr_hist: list[int] | None = None
    dm_hist: list[int] | None = None
    width_hist: list[int] | None = None
    beam_counts: list[int] | None = None
    snr_max: float | None = None
    dm_at_snr_max: float | None = None

    def __post_init__(self) -> None:
        self.snr_hist = self.snr_hist or [0] * (len(SNR_EDGES) - 1)
        self.dm_hist = self.dm_hist or [0] * (len(DM_EDGES) - 1)
        self.width_hist = self.width_hist or [0] * (len(WIDTH_EDGES) - 1)
        self.beam_counts = self.beam_counts or [0] * BEAMS_PER_JOB

    def add(self, cand: Cand) -> None:
        self.n += 1
        self.snr_hist[hist_index(cand.snr, SNR_EDGES)] += 1
        self.dm_hist[hist_index(cand.dm, DM_EDGES)] += 1
        self.width_hist[hist_index(float(cand.width), WIDTH_EDGES)] += 1
        slot = cand.beam - BEAMS_PER_JOB * cand.job
        if 0 <= slot < BEAMS_PER_JOB:
            self.beam_counts[slot] += 1
        if self.snr_max is None or cand.snr > self.snr_max:
            self.snr_max = cand.snr
            self.dm_at_snr_max = cand.dm

    def merge(self, other: "Bin") -> None:
        self.n += other.n
        for mine, theirs in (
            (self.snr_hist, other.snr_hist),
            (self.dm_hist, other.dm_hist),
            (self.width_hist, other.width_hist),
            (self.beam_counts, other.beam_counts),
        ):
            for i, value in enumerate(theirs):
                if i < len(mine):
                    mine[i] += value
        if other.snr_max is not None and (self.snr_max is None or other.snr_max > self.snr_max):
            self.snr_max = other.snr_max
            self.dm_at_snr_max = other.dm_at_snr_max


def bin_cands(cands: Iterable[Cand], utc_start_unix: float) -> dict[tuple[float, int], Bin]:
    """Group parsed cands into per-(gulp, job) bins."""
    bins: dict[tuple[float, int], Bin] = {}
    for cand in cands:
        key = (round(gulp_ts(cand.samp, utc_start_unix), 6), cand.job)
        bins.setdefault(key, Bin()).add(cand)
    return bins


# -- t2 gulp_stats ---------------------------------------------------------
GULP_STATS_COLUMNS = (
    "gulp_utc",
    "obs_utc_start",
    "gulp",
    "n_jobs",
    "n_cands",
    "n_clusters",
    "n_stored",
    "n_would",
    "n_vetoed",
    "n_shed",
    "clustering_ms",
)


def read_gulp_stats(t2_db: Path | str, since_utc: str | None, limit: int = 20000) -> list[dict[str, Any]]:
    """Rows of t2's ``gulp_stats`` newer than ``since_utc`` (ISO, with 'T').

    Opened ``mode=ro`` on a URI, so we cannot create, journal or write the t2
    database even if it is missing. Comparison is on t2's own ISO strings,
    never on sqlite ``datetime('now')`` (which formats with a space and would
    silently compare unequal).
    """
    path = Path(t2_db)
    if not path.is_file():
        return []
    sql = f"SELECT {', '.join(GULP_STATS_COLUMNS)} FROM gulp_stats"
    args: list[Any] = []
    if since_utc:
        sql += " WHERE gulp_utc > ?"
        args.append(since_utc)
    sql += " ORDER BY gulp_utc ASC LIMIT ?"
    args.append(int(limit))
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


# -- the collector ---------------------------------------------------------
SshRunner = Callable[[str, str, float], tuple[int, bytes, bytes]]


def ssh_bytes(host: str, command: str, timeout: float) -> tuple[int, bytes, bytes]:
    """Run one ssh command capturing BYTES.

    Bytes, not text: the byte watermark must be exact, and a decode step would
    make ``len(chunk)`` a character count instead of a byte count the moment a
    file ever contains a non-ASCII byte.
    """
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, command]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, b"", f"timeout after {timeout}s: ssh {host}".encode()
    except OSError as exc:
        return 127, b"", str(exc).encode()
    return proc.returncode, proc.stdout, proc.stderr


class SearchCollector(Collector):
    """Tail the eight hella cands files and mirror t2's gulp_stats."""

    name = "search"
    default_cadence_s = 20.0
    timeout_s = 60.0

    def __init__(
        self,
        settings: Settings,
        cadence_s: float | None = None,
        *,
        config: SearchConfig | None = None,
        ssh_runner: SshRunner | None = None,
    ) -> None:
        self.config = config or search_config(settings)
        self._ssh = ssh_runner or ssh_bytes
        super().__init__(settings, cadence_s if cadence_s is not None else self.config.cadence_s)
        self._last_retention = 0.0
        self._corr2_detail: dict[str, Any] = {}

    # -- watermarks ----------------------------------------------------
    @staticmethod
    def _file_key(job: int) -> str:
        return f"file.{job}"

    def _offset(self, store: Store, job: int, obs: str) -> int | None:
        """Byte watermark for ``job`` in observation ``obs``; None if unseen."""
        value = store.get_watermark(WATERMARK_STREAM, self._file_key(job))
        if not isinstance(value, dict):
            return None
        if str(value.get("obs") or "") != obs:
            return None
        try:
            return max(0, int(value.get("offset") or 0))
        except (TypeError, ValueError):
            return None

    def _set_offset(self, store: Store, job: int, obs: str, offset: int) -> None:
        store.set_watermark(
            WATERMARK_STREAM, self._file_key(job), {"obs": obs, "offset": int(offset)}
        )

    # -- one tick ------------------------------------------------------
    def collect(self, ctx: CollectorContext) -> None:
        cfg = self.config
        ensure_tables(ctx.store)
        now = time.time()

        obs = find_current_obs(cfg.cands_dir, JOBS_CORR1 + JOBS_CORR2)
        if obs is None:
            ctx.scalar("search.obs", "")
            ctx.scalar("search.total_rate", 0.0)
            self._mirror_gulp_stats(ctx)
            return
        utc_start_unix = utc_start_to_unix(obs)
        if utc_start_unix is None:
            raise RuntimeError(f"cands file name carries an unparseable UTC_START: {obs!r}")

        previous_obs = ctx.store.get_watermark(WATERMARK_STREAM, OBS_KEY)
        if previous_obs != obs:
            # New UTC_START = new files: every byte watermark belongs to the old
            # observation and is dropped (``_offset`` also checks the obs, so a
            # stale row can never be read as an offset into the new file).
            for job in JOBS_CORR1 + JOBS_CORR2:
                ctx.store.execute(
                    "DELETE FROM watermarks WHERE stream = ? AND key = ?",
                    (WATERMARK_STREAM, self._file_key(job)),
                )
            ctx.store.set_watermark(WATERMARK_STREAM, OBS_KEY, obs)
            if previous_obs is not None:
                ctx.event(
                    "search_obs_changed",
                    severity="info",
                    subject=obs,
                    detail={"from": previous_obs, "to": obs},
                )
        ctx.scalar("search.obs", obs)

        # Each job's candidates/bins are inserted and its byte watermark
        # advanced together, one transaction per chunk (see ``_commit_chunk``):
        # nothing here needs a further store write once these return.
        self._read_corr1(ctx, obs, utc_start_unix)
        self._corr2_detail = {}
        corr2_ok = self._read_corr2(ctx, obs, utc_start_unix)
        # One event per transition, in both directions (the strip needs to see
        # corr2 come back, not only go away).
        ctx.on_change(
            "search.corr2_ok",
            1 if corr2_ok else 0,
            kind="search_corr2_state",
            severity="info" if corr2_ok else "warn",
            subject=self.config.corr2_ssh,
            detail=self._corr2_detail,
        )

        self._mirror_gulp_stats(ctx)
        self._rate_scalars(ctx, now)
        if now - self._last_retention >= cfg.retention_interval_s:
            self._apply_retention(ctx, now)
            self._last_retention = now

    # -- corr1 (local files) -------------------------------------------
    def _read_corr1(self, ctx: CollectorContext, obs: str, utc_start_unix: float) -> None:
        cfg = self.config
        for job in JOBS_CORR1:
            path = Path(cands_path(cfg.cands_dir, obs, job))
            try:
                size = path.stat().st_size
            except OSError:
                continue
            offset = self._offset(ctx.store, job, obs)
            backfill = offset is None
            if backfill:
                offset = max(0, size - cfg.max_backfill_bytes)
            if size < offset:
                self._on_truncated(ctx, job, "corr1", offset, size)
                self._set_offset(ctx.store, job, obs, max(0, size - cfg.max_tick_bytes))
                continue
            if size == offset:
                self._set_offset(ctx.store, job, obs, offset)
                continue
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read(cfg.max_tick_bytes)
            except OSError as exc:
                ctx.event(
                    "search_file_unreadable",
                    severity="warn",
                    subject=str(path),
                    detail={"job": job, "error": str(exc)},
                )
                continue
            self._ingest_chunk(
                ctx,
                chunk,
                job=job,
                node="corr1",
                obs=obs,
                offset=offset,
                utc_start_unix=utc_start_unix,
                backfill=backfill,
            )

    # -- corr2 (one ssh) -----------------------------------------------
    def _read_corr2(
        self,
        ctx: CollectorContext,
        obs: str,
        utc_start_unix: float,
    ) -> bool:
        cfg = self.config
        offsets: dict[int, int] = {}
        backfilling: set[int] = set()
        for job in JOBS_CORR2:
            offset = self._offset(ctx.store, job, obs)
            if offset is None:
                # First start for this observation: ask for the last
                # ``max_backfill_bytes`` (negative offset, see corr2_command)
                # so the backfill payload is bounded without a size round trip.
                backfilling.add(job)
                offsets[job] = -max(1, cfg.max_backfill_bytes)
            else:
                offsets[job] = offset
        command = corr2_command(cfg.cands_dir, obs, offsets, cfg.max_tick_bytes)
        rc, stdout, stderr = self._ssh(cfg.corr2_ssh, command, cfg.ssh_timeout_s)
        ctx.scalar("search.corr2_bytes_per_tick", len(stdout))
        if rc != 0:
            self._corr2_detail = {
                "rc": rc,
                "error": stderr.decode("ascii", "replace").strip()[:200],
            }
            return False

        blocks = split_job_blocks(stdout)
        for job in JOBS_CORR2:
            entry = blocks.get(job)
            if entry is None:
                continue
            size, payload = entry
            if size < 0:
                continue  # file absent on corr2 (obs not started there yet)
            if job in backfilling:
                # Where the reply actually started: recomputed the same way
                # corr2_command's remote arithmetic does (from ``size`` and the
                # requested backfill depth), NEVER from ``len(payload)`` -- a
                # payload capped by ``max_tick_bytes`` is shorter than
                # ``size - start`` and would otherwise derive the wrong offset.
                offset = max(0, size - abs(offsets[job]))
            else:
                offset = max(0, offsets.get(job, 0))
                if size < offset:
                    self._on_truncated(ctx, job, "corr2", offset, size)
                    self._set_offset(ctx.store, job, obs, max(0, size - cfg.max_tick_bytes))
                    continue
            self._ingest_chunk(
                ctx,
                payload,
                job=job,
                node="corr2",
                obs=obs,
                offset=offset,
                utc_start_unix=utc_start_unix,
                backfill=job in backfilling,
            )
        return True

    def _on_truncated(
        self, ctx: CollectorContext, job: int, node: str, offset: int, size: int
    ) -> None:
        ctx.event(
            "search_file_truncated",
            severity="warn",
            subject=f"{node} job {job}",
            detail={"job": job, "node": node, "offset": offset, "size": size},
        )

    def _ingest_chunk(
        self,
        ctx: CollectorContext,
        chunk: bytes,
        *,
        job: int,
        node: str,
        obs: str,
        offset: int,
        utc_start_unix: float,
        backfill: bool,
    ) -> None:
        """Parse one chunk and commit it -- rows, bins and the byte watermark
        it took to produce them -- as one transaction (see ``_commit_chunk``).
        """
        cands, consumed, n_bad = parse_chunk(
            chunk,
            job=job,
            node=node,
            utc_start_unix=utc_start_unix,
            drop_first_partial=backfill and offset > 0,
        )
        if backfill and self.config.backfill_hours > 0:
            floor = time.time() - self.config.backfill_hours * 3600.0
            cands = [c for c in cands if c.ts_unix >= floor]
        if n_bad:
            ctx.scalar("search.bad_lines", n_bad, tags={"job": job, "node": node})
        self._commit_chunk(ctx, obs, job, offset + consumed, cands, utc_start_unix)

    # -- writes --------------------------------------------------------
    def _commit_chunk(
        self,
        ctx: CollectorContext,
        obs: str,
        job: int,
        new_offset: int,
        cands: Sequence[Cand],
        utc_start_unix: float,
    ) -> None:
        """Insert ``cands``, merge their bins and advance this job's byte
        watermark to ``new_offset`` in ONE transaction.

        A failure partway through (a bad row, a full disk) rolls the whole
        thing back, including the watermark: the next tick re-reads and
        re-parses the same bytes rather than skipping candidates a half-failed
        write never actually stored (P0 in the 2026-09-09 review).
        """
        bins = bin_cands(cands, utc_start_unix)
        with ctx.store.transaction() as conn:
            if cands:
                conn.executemany(
                    "INSERT INTO cands (ts_unix, job, node, snr, width, dm, dm_idx, beam, samp) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [c.row() for c in cands],
                )
            for (bin_ts, bin_job), fresh in sorted(bins.items()):
                existing = conn.execute(
                    "SELECT n, snr_hist, dm_hist, width_hist, beam_counts, snr_max, dm_at_snr_max "
                    "FROM cand_bins WHERE gulp_ts = ? AND job = ?",
                    (bin_ts, bin_job),
                ).fetchone()
                if existing is not None:
                    # A gulp's rows can arrive over more than one tick (hella
                    # flushes in blocks and the last line of a tick is
                    # partial), so a bin is merged into, never overwritten.
                    merged = Bin(
                        n=int(existing["n"]),
                        snr_hist=json.loads(existing["snr_hist"]),
                        dm_hist=json.loads(existing["dm_hist"]),
                        width_hist=json.loads(existing["width_hist"]),
                        beam_counts=json.loads(existing["beam_counts"]),
                        snr_max=existing["snr_max"],
                        dm_at_snr_max=existing["dm_at_snr_max"],
                    )
                    merged.merge(fresh)
                else:
                    merged = fresh
                conn.execute(
                    "INSERT INTO cand_bins (gulp_ts, job, n, snr_hist, dm_hist, width_hist, "
                    "beam_counts, snr_max, dm_at_snr_max) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(gulp_ts, job) DO UPDATE SET n = excluded.n, "
                    "snr_hist = excluded.snr_hist, dm_hist = excluded.dm_hist, "
                    "width_hist = excluded.width_hist, beam_counts = excluded.beam_counts, "
                    "snr_max = excluded.snr_max, dm_at_snr_max = excluded.dm_at_snr_max",
                    (
                        bin_ts,
                        bin_job,
                        merged.n,
                        json.dumps(merged.snr_hist),
                        json.dumps(merged.dm_hist),
                        json.dumps(merged.width_hist),
                        json.dumps(merged.beam_counts),
                        merged.snr_max,
                        merged.dm_at_snr_max,
                    ),
                )
            conn.execute(
                "INSERT INTO watermarks (stream, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(stream, key) DO UPDATE SET value = excluded.value",
                (
                    WATERMARK_STREAM,
                    self._file_key(job),
                    json.dumps({"obs": obs, "offset": int(new_offset)}),
                ),
            )

    def _mirror_gulp_stats(self, ctx: CollectorContext) -> None:
        since = ctx.store.get_watermark(WATERMARK_STREAM, GULP_STATS_KEY)
        since_str = str(since) if isinstance(since, (str, int, float)) and since else None
        try:
            rows = read_gulp_stats(self.config.t2_db, since_str)
        except sqlite3.Error as exc:
            ctx.scalar("search.t2_ok", 0)
            ctx.event(
                "search_t2_unreadable",
                severity="warn",
                subject=str(self.config.t2_db),
                detail={"error": str(exc)},
            )
            return
        ctx.scalar("search.t2_ok", 1)
        payload: list[tuple[Any, ...]] = []
        newest = since_str
        for row in rows:
            gulp_utc = str(row["gulp_utc"])
            ts_unix = parse_iso(gulp_utc)
            if ts_unix is None:
                continue
            payload.append(
                (
                    gulp_utc,
                    ts_unix,
                    row.get("obs_utc_start"),
                    row.get("gulp"),
                    row.get("n_jobs"),
                    row.get("n_cands"),
                    row.get("n_clusters"),
                    row.get("n_stored"),
                    row.get("n_would"),
                    row.get("n_vetoed"),
                    row.get("n_shed"),
                    row.get("clustering_ms"),
                )
            )
            if newest is None or gulp_utc > newest:
                newest = gulp_utc
        if payload:
            ctx.store.executemany(
                "INSERT INTO gulp_stats_mirror (gulp_utc, ts_unix, obs_utc_start, gulp, n_jobs, "
                "n_cands, n_clusters, n_stored, n_would, n_vetoed, n_shed, clustering_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(gulp_utc) DO NOTHING",
                payload,
            )
            ctx.store.set_watermark(WATERMARK_STREAM, GULP_STATS_KEY, newest)
        latest = ctx.store.query(
            "SELECT n_jobs, n_cands FROM gulp_stats_mirror ORDER BY ts_unix DESC LIMIT 1"
        )
        if latest:
            n_jobs = latest[0]["n_jobs"]
            if n_jobs is not None:
                # The saturation indicator: t2 hears from one job per 64 beams,
                # so a gulp with fewer than 8 jobs searched fewer than 512 beams.
                ctx.scalar("search.beams_searched_est", int(n_jobs) * BEAMS_PER_JOB)

    def _rate_scalars(self, ctx: CollectorContext, now: float) -> None:
        window = max(1.0, self.config.rate_window_s)
        rows = ctx.store.query(
            "SELECT job, COUNT(*) AS n FROM cands WHERE ts_unix >= ? GROUP BY job",
            (now - window,),
        )
        counts = {int(r["job"]): int(r["n"]) for r in rows}
        total = 0.0
        for job in JOBS_CORR1 + JOBS_CORR2:
            rate = counts.get(job, 0) * 60.0 / window
            total += rate
            ctx.scalar(f"search.rate_per_min.{job}", round(rate, 4), tags={"job": job})
        ctx.scalar("search.total_rate", round(total, 4))

    def _apply_retention(self, ctx: CollectorContext, now: float) -> None:
        """Raw rows for ``raw_ttl_days``; the per-gulp bins are kept forever."""
        ttl = self.config.raw_ttl_days
        if ttl <= 0:
            return
        cur = ctx.store.execute("DELETE FROM cands WHERE ts_unix < ?", (now - ttl * 86400.0,))
        deleted = int(cur.rowcount or 0)
        if deleted:
            ctx.scalar("search.cands_expired", deleted)


__all__ = [
    "SearchCollector",
    "SearchConfig",
    "search_config",
    "ensure_tables",
    "find_current_obs",
    "obs_and_job",
    "cands_path",
    "corr2_command",
    "split_job_blocks",
    "parse_chunk",
    "parse_line",
    "bin_cands",
    "histogram",
    "hist_index",
    "gulp_ts",
    "read_gulp_stats",
    "Cand",
    "Bin",
    "SNR_EDGES",
    "DM_EDGES",
    "WIDTH_EDGES",
    "TSAMP_S",
    "GULP_SAMPLES",
    "JOBS_CORR1",
    "JOBS_CORR2",
]
