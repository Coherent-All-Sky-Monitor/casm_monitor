"""Gulp ledger tailed from the Hella search log.

The eight Hella search processes ("streams" 0-7, 64 beams each, streams 0-3 on
corr1 and 4-7 on corr2) all append to ONE file,
``/data/casm/logs/bf_proc_hella.log``. That file is tens of gigabytes and is
never rotated, so it is only ever read forwards from a persisted byte
watermark; on a first run the tail rule starts at ``size - 96 MiB``.

Each stream writes one ``processed ... [wall]`` line per gulp (8192 samples,
8.59 s). The ledger keeps one row per gulp per stream with the stage timings,
so a stream that is alive but emitting nothing is distinguishable from a stream
that has stopped. Empty gulps are the normal state.

Line shapes (stream index is the line prefix, timestamps are OVRO local)::

    4 [2026-09-14 10:59:20.295] [info] processed 8.72415232 s in read 0.125628 flag 0.522513 dedisp 3.7786329 smooth 1.0319849 peak 0.17115594 output 0.000261 [5.6301756]
    7 [2026-09-14 10:58:51.404] [warning] Only processed 25/64 beams - detected 10000 peaks
    3 [2026-09-14 11:06:55.977] [info] chanstat blanked 472 specflag 0 usable 0.8464 clip_lo 3.656e-06 clip_hi 1.987e-06

The cap warning PRECEDES the ``processed`` line of the gulp it describes; the
``chanstat`` line FOLLOWS it. Both are attached to that gulp's row.

Rows go into their own sqlite file inside the workspace artifact root, never
into the production store (the app runs read-only against it).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("casm_monitor.hella_log")

DEFAULT_LOG_PATH = Path("/data/casm/logs/bf_proc_hella.log")
# Tick often: tailing new bytes is cheap, and a stream's reported staleness can
# never be fresher than the last read.
DEFAULT_INTERVAL_S = 60.0
LEDGER_NAME = "hella_gulps.sqlite"
# Log clock is OVRO wall time, not UTC.
LOG_TZ = ZoneInfo("America/Los_Angeles")
N_STREAMS = 8
BEAMS_PER_STREAM = 64
# 8192 samples at 1.048576 ms.
GULP_S = 8.59
EXPECTED_GULPS_PER_HOUR = 3600.0 / GULP_S
# First-run tail: how far back from EOF to start.
FIRST_TAIL_BYTES = 96 * 1024 * 1024
# Bytes one tick may consume; the rest is picked up next tick.
MAX_TICK_BYTES = 256 * 1024 * 1024
RETAIN_S = 14 * 86400.0

_TS = r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\]"
_NUM = r"([0-9.eE+-]+)"
PROCESSED_RE = re.compile(
    rf"^([0-7]) {_TS} \[info\] processed \S+ s in read {_NUM} flag {_NUM} dedisp {_NUM} "
    rf"smooth {_NUM} peak {_NUM} output {_NUM} \[{_NUM}\]")
CAP_RE = re.compile(rf"^([0-7]) {_TS} \[warning\] Only processed (\d+)/(\d+) beams.*?(\d+) peaks")
CHANSTAT_RE = re.compile(rf"^([0-7]) {_TS} \[info\] chanstat blanked (\d+)")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS gulps (
        ts_unix          REAL NOT NULL,
        stream           INTEGER NOT NULL,
        wall_s           REAL,
        read_s           REAL,
        flag_s           REAL,
        dedisp_s         REAL,
        smooth_s         REAL,
        peak_s           REAL,
        cap_hit          INTEGER NOT NULL DEFAULT 0,
        beams_processed  INTEGER,
        blanked          INTEGER,
        PRIMARY KEY (ts_unix, stream)
    )""",
    "CREATE INDEX IF NOT EXISTS gulps_ts ON gulps (ts_unix)",
    "CREATE TABLE IF NOT EXISTS watermark (key TEXT PRIMARY KEY, value TEXT)",
)


def log_path_from_env() -> Path:
    return Path(os.environ.get("CASM_MONITOR_HELLA_LOG") or DEFAULT_LOG_PATH)


def interval_from_env() -> float:
    try:
        value = float(os.environ.get("CASM_MONITOR_HELLA_LOG_INTERVAL_S") or DEFAULT_INTERVAL_S)
    except ValueError:
        return DEFAULT_INTERVAL_S
    return value if value > 0 else DEFAULT_INTERVAL_S


def ledger_path(artifact_root) -> Path | None:
    """The ledger file inside the workspace artifact root."""
    if artifact_root is None:
        return None
    return Path(artifact_root) / LEDGER_NAME


def _stamp(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=LOG_TZ).timestamp()


class GulpParser:
    """Turns log lines into gulp rows, carrying per-stream state across chunks.

    A cap warning is held until that stream's next ``processed`` line; a
    ``chanstat`` count is written back onto that stream's last emitted row,
    which is re-yielded so the caller's upsert picks the value up.
    """

    def __init__(self) -> None:
        self._pending_cap: dict[int, tuple[int, int, int]] = {}
        self._last_row: dict[int, dict] = {}

    def feed(self, lines) -> list[dict]:
        rows: list[dict] = []
        for line in lines:
            if not line or line[0] not in "01234567":
                continue
            match = PROCESSED_RE.match(line)
            if match:
                stream = int(match[1])
                cap = self._pending_cap.pop(stream, None)
                row = {"ts_unix": _stamp(match[2]), "stream": stream,
                       "read_s": float(match[3]), "flag_s": float(match[4]),
                       "dedisp_s": float(match[5]), "smooth_s": float(match[6]),
                       "peak_s": float(match[7]), "wall_s": float(match[9]),
                       "cap_hit": 1 if cap else 0,
                       "beams_processed": cap[0] if cap else None,
                       "beams_total": cap[1] if cap else BEAMS_PER_STREAM,
                       "peaks": cap[2] if cap else None, "blanked": None}
                self._last_row[stream] = row
                rows.append(row)
                continue
            match = CAP_RE.match(line)
            if match:
                self._pending_cap[int(match[1])] = (int(match[3]), int(match[4]), int(match[5]))
                continue
            match = CHANSTAT_RE.match(line)
            if match:
                row = self._last_row.get(int(match[1]))
                if row is not None and row["blanked"] is None:
                    row["blanked"] = int(match[3])
                    rows.append(row)
        return rows


def parse_lines(lines) -> list[dict]:
    """One-shot parse of a complete chunk."""
    return GulpParser().feed(lines)


class HellaLogLedger:
    """Byte-offset tailer that keeps the gulp ledger up to date.

    Drive it with :meth:`tick`; the refresh thread is a thin wrapper so tests
    can run ticks synchronously on a temporary file.
    """

    def __init__(self, db_path, log_path=None, *, first_tail_bytes: int | None = None,
                 max_tick_bytes: int | None = None, retain_s: float = RETAIN_S) -> None:
        self.db_path = Path(db_path)
        self.log_path = Path(log_path) if log_path is not None else log_path_from_env()
        self.first_tail_bytes = FIRST_TAIL_BYTES if first_tail_bytes is None else int(first_tail_bytes)
        self.max_tick_bytes = MAX_TICK_BYTES if max_tick_bytes is None else int(max_tick_bytes)
        self.retain_s = float(retain_s)
        self._parser = GulpParser()
        self._connection: sqlite3.Connection | None = None

    # -- storage ---------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        if self._connection is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path, timeout=10)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            for statement in SCHEMA:
                connection.execute(statement)
            connection.commit()
            self._connection = connection
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _watermark(self, connection) -> dict[str, str]:
        return dict(connection.execute("SELECT key, value FROM watermark").fetchall())

    def _set_watermark(self, connection, values: dict) -> None:
        connection.executemany("INSERT INTO watermark (key, value) VALUES (?,?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               [(k, str(v)) for k, v in values.items()])

    # -- tailing ---------------------------------------------------------
    def tick(self) -> dict:
        """Read new bytes, upsert rows, prune. Returns this tick's stats."""
        connection = self.connect()
        try:
            stat = self.log_path.stat()
        except OSError as exc:
            return {"status": "missing_log", "reason": str(exc), "rows": 0}
        marks = self._watermark(connection)
        offset = float(marks.get("offset", -1))
        inode = marks.get("inode")
        # Truncation or a replaced file invalidates the offset: back to the tail rule.
        fresh = (offset < 0 or inode != str(stat.st_ino) or stat.st_size < offset)
        start = max(0, stat.st_size - self.first_tail_bytes) if fresh else int(offset)
        end = min(stat.st_size, start + self.max_tick_bytes)
        rows: list[dict] = []
        consumed = start
        if end > start:
            with self.log_path.open("rb") as handle:
                handle.seek(start)
                chunk = handle.read(end - start)
            base = start
            if fresh and start > 0:
                # Never parse the partial line the tail rule landed in.
                cut = chunk.find(b"\n")
                if cut < 0:
                    chunk, base = b"", end
                else:
                    chunk, base = chunk[cut + 1:], base + cut + 1
            tail = chunk.rfind(b"\n")
            if tail < 0:
                consumed = base
                chunk = b""
            else:
                consumed = base + tail + 1
                chunk = chunk[:tail + 1]
            if chunk:
                rows = self._parser.feed(chunk.decode("utf-8", "replace").splitlines())
        now = time.time()
        if rows:
            connection.executemany(
                "INSERT INTO gulps (ts_unix, stream, wall_s, read_s, flag_s, dedisp_s, smooth_s, "
                "peak_s, cap_hit, beams_processed, blanked) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ts_unix, stream) DO UPDATE SET wall_s=excluded.wall_s, "
                "read_s=excluded.read_s, flag_s=excluded.flag_s, dedisp_s=excluded.dedisp_s, "
                "smooth_s=excluded.smooth_s, peak_s=excluded.peak_s, "
                "cap_hit=max(gulps.cap_hit, excluded.cap_hit), "
                "beams_processed=coalesce(excluded.beams_processed, gulps.beams_processed), "
                "blanked=coalesce(excluded.blanked, gulps.blanked)",
                [(r["ts_unix"], r["stream"], r["wall_s"], r["read_s"], r["flag_s"], r["dedisp_s"],
                  r["smooth_s"], r["peak_s"], r["cap_hit"], r["beams_processed"], r["blanked"])
                 for r in rows])
        connection.execute("DELETE FROM gulps WHERE ts_unix < ?", (now - self.retain_s,))
        self._set_watermark(connection, {"offset": consumed, "inode": stat.st_ino,
                                         "size": stat.st_size, "last_tick_unix": now,
                                         "log_path": str(self.log_path)})
        connection.commit()
        return {"status": "ok", "rows": len(rows), "offset": consumed,
                "bytes_read": max(0, consumed - start),
                "behind_bytes": max(0, stat.st_size - consumed)}


def ledger_info(db_path) -> dict:
    """Watermark and freshness of the ledger file, without writing to it."""
    info = {"status": "empty", "log_path": None, "watermark_offset": None,
            "last_tick_unix": None, "newest_unix": None}
    path = Path(db_path) if db_path is not None else None
    if path is None or not path.is_file():
        info["status"] = "missing_log"
        return info
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            marks = dict(connection.execute("SELECT key, value FROM watermark").fetchall())
            newest = connection.execute("SELECT max(ts_unix) FROM gulps").fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        info.update(status="missing_log", reason=str(exc))
        return info
    info.update(status="ok" if newest is not None else "empty",
                log_path=marks.get("log_path"),
                watermark_offset=int(float(marks["offset"])) if "offset" in marks else None,
                last_tick_unix=float(marks["last_tick_unix"]) if "last_tick_unix" in marks else None,
                newest_unix=newest)
    return info


def _query(db_path, sql: str, params: tuple) -> list[tuple]:
    path = Path(db_path) if db_path is not None else None
    if path is None or not path.is_file():
        return []
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            return connection.execute(sql).fetchall() if not params else \
                connection.execute(sql, params).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return []


def read_gulps(db_path, start: float, end: float) -> list[tuple[float, int, float, int]]:
    """``(ts_unix, stream, wall_s, cap_hit)`` inside the window, oldest first."""
    return _query(db_path, "SELECT ts_unix, stream, wall_s, cap_hit FROM gulps "
                           "WHERE ts_unix >= ? AND ts_unix <= ? ORDER BY ts_unix", (start, end))


def read_last_gulps(db_path, end: float) -> dict[int, float]:
    """Newest ledger timestamp per stream at or before ``end``."""
    rows = _query(db_path, "SELECT stream, max(ts_unix) FROM gulps WHERE ts_unix <= ? "
                           "GROUP BY stream", (end,))
    return {int(stream): float(ts) for stream, ts in rows if ts is not None}


class LedgerRefresher:
    """Daemon thread that ticks the ledger; never propagates an exception."""

    def __init__(self, ledger: HellaLogLedger, interval_s: float | None = None) -> None:
        self.ledger = ledger
        self.interval_s = interval_from_env() if interval_s is None else float(interval_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="hella-ledger", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.ledger.tick()
            except Exception:
                log.warning("hella ledger tick failed", exc_info=True)
            self._stop.wait(self.interval_s)
        try:
            self.ledger.close()
        except Exception:
            log.debug("hella ledger close failed", exc_info=True)
