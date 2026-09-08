"""The monitor store: a thin, explicit wrapper over one SQLite file.

The collector process opens it read-write, the job worker opens it read-write
(jobs table only), and the web process opens two handles: a read-only one for
every GET and a narrow read-write one used solely to submit/cancel jobs and to
append events.

All methods are safe to call from several threads of one process (the collector
runs its collectors in a thread pool), guarded by one lock around a single
connection.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from .schema import SCHEMA, SCHEMA_VERSION

JobState = str
Severity = str


def _jdump(obj: Any) -> str | None:
    return None if obj is None else json.dumps(obj, default=str)


def _jload(text: Any) -> Any:
    if text is None or text == "":
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


class Store:
    """SQLite-backed history store."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        read_only: bool = False,
        store_root: str | os.PathLike[str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.read_only = read_only
        self.store_root = Path(store_root) if store_root is not None else self.db_path.parent
        self._lock = threading.Lock()

        if read_only:
            uri = f"file:{self.db_path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=timeout, check_same_thread=False)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), timeout=timeout, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if not read_only:
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.executescript(SCHEMA)
                self._conn.execute(
                    "INSERT OR REPLACE INTO store_meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                self._conn.commit()

    # -- lifecycle ------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level ------------------------------------------------------
    def query(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(args)).fetchall()

    def execute(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(args))
            self._conn.commit()
            return cur

    # -- scalars --------------------------------------------------------
    def put_scalar(
        self,
        name: str,
        value: float | int | str | None,
        *,
        ts: float | None = None,
        tags: dict[str, Any] | None = None,
    ) -> None:
        self.put_scalars([(name, value, tags)], ts=ts)

    def put_scalars(
        self,
        rows: Iterable[tuple[str, float | int | str | None, dict[str, Any] | None]],
        *,
        ts: float | None = None,
    ) -> None:
        t = time.time() if ts is None else ts
        payload = [(t, name, _jdump(tags), value) for name, value, tags in rows]
        if not payload:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO scalars (ts, name, tags, value) VALUES (?, ?, ?, ?)", payload
            )
            self._conn.commit()

    def latest_scalar(self, name: str) -> dict[str, Any] | None:
        rows = self.query(
            "SELECT ts, name, tags, value FROM scalars WHERE name = ? ORDER BY ts DESC, rowid DESC LIMIT 1",
            (name,),
        )
        if not rows:
            return None
        r = rows[0]
        return {"ts": r["ts"], "name": r["name"], "tags": _jload(r["tags"]), "value": r["value"]}

    def latest_scalars(self) -> dict[str, dict[str, Any]]:
        """Newest row per scalar name (ordered by ts, like ``latest_scalar``,
        so a back-dated write with a larger rowid cannot make the two
        disagree about which row is "latest")."""
        rows = self.query(
            "SELECT s.ts, s.name, s.tags, s.value FROM scalars s "
            "JOIN (SELECT name, MAX(ts) AS max_ts FROM scalars GROUP BY name) m "
            "ON s.name = m.name AND s.ts = m.max_ts"
        )
        return {
            r["name"]: {
                "ts": r["ts"],
                "name": r["name"],
                "tags": _jload(r["tags"]),
                "value": r["value"],
            }
            for r in rows
        }

    def series(
        self,
        name: str,
        *,
        t0: float | None = None,
        t1: float | None = None,
        max_points: int = 2000,
    ) -> tuple[list[float], list[Any]]:
        """Time series for one scalar, decimated to at most ``max_points``.

        Decimation is a uniform stride, applied in SQL so we never pull every
        matching row into Python just to throw most of them away. The last
        point is always kept, so a plotted trend keeps its endpoints.
        """
        where_sql = " WHERE name = ?"
        args: list[Any] = [name]
        if t0 is not None:
            where_sql += " AND ts >= ?"
            args.append(t0)
        if t1 is not None:
            where_sql += " AND ts <= ?"
            args.append(t1)

        count_rows = self.query(f"SELECT COUNT(*) AS n FROM scalars{where_sql}", args)
        n = int(count_rows[0]["n"]) if count_rows else 0
        if n == 0:
            return [], []

        if max_points <= 0 or n <= max_points:
            rows = self.query(f"SELECT ts, value FROM scalars{where_sql} ORDER BY ts ASC", args)
            return [r["ts"] for r in rows], [r["value"] for r in rows]

        stride = (n + max_points - 1) // max_points
        sql = (
            "SELECT ts, value FROM ("
            f"  SELECT ts, value, ROW_NUMBER() OVER (ORDER BY ts ASC) AS rn FROM scalars{where_sql}"
            ") WHERE (rn - 1) % ? = 0 OR rn = ? ORDER BY ts ASC"
        )
        rows = self.query(sql, [*args, stride, n])
        return [r["ts"] for r in rows], [r["value"] for r in rows]

    # -- events ---------------------------------------------------------
    def add_event(
        self,
        kind: str,
        *,
        severity: Severity = "info",
        subject: str | None = None,
        detail: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> int:
        if severity not in ("info", "warn", "error"):
            raise ValueError(f"bad severity {severity!r}")
        t = time.time() if ts is None else ts
        cur = self.execute(
            "INSERT INTO events (ts, kind, severity, subject, detail) VALUES (?, ?, ?, ?, ?)",
            (t, kind, severity, subject, _jdump(detail)),
        )
        return int(cur.lastrowid or 0)

    def events(
        self,
        *,
        since: float | None = None,
        kind: str | None = None,
        severity: Severity | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = "SELECT id, ts, kind, severity, subject, detail FROM events WHERE 1=1"
        args: list[Any] = []
        if since is not None:
            sql += " AND ts > ?"
            args.append(since)
        if kind:
            # Prefix match: the Events page filters as the operator types.
            sql += " AND kind LIKE ? || '%'"
            args.append(kind)
        if severity:
            sql += " AND severity = ?"
            args.append(severity)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(int(limit))
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "kind": r["kind"],
                "severity": r["severity"],
                "subject": r["subject"],
                "detail": _jload(r["detail"]),
            }
            for r in self.query(sql, args)
        ]

    # -- watermarks -----------------------------------------------------
    def get_watermark(self, stream: str, key: str, default: Any = None) -> Any:
        rows = self.query(
            "SELECT value FROM watermarks WHERE stream = ? AND key = ?", (stream, key)
        )
        if not rows:
            return default
        return _jload(rows[0]["value"])

    def set_watermark(self, stream: str, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO watermarks (stream, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(stream, key) DO UPDATE SET value = excluded.value",
            (stream, key, _jdump(value)),
        )

    # -- collector heartbeat -------------------------------------------
    def heartbeat_ok(self, name: str, ts: float | None = None) -> None:
        t = time.time() if ts is None else ts
        self.execute(
            "INSERT INTO collector_heartbeat (name, last_ok) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_ok = excluded.last_ok",
            (name, t),
        )

    def heartbeat_err(self, name: str, err: str, ts: float | None = None) -> None:
        t = time.time() if ts is None else ts
        self.execute(
            "INSERT INTO collector_heartbeat (name, last_err, last_err_ts) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_err = excluded.last_err, "
            "last_err_ts = excluded.last_err_ts",
            (name, err[:2000], t),
        )

    def heartbeats(self) -> dict[str, dict[str, Any]]:
        return {
            r["name"]: {
                "last_ok": r["last_ok"],
                "last_err": r["last_err"],
                "last_err_ts": r["last_err_ts"],
            }
            for r in self.query("SELECT name, last_ok, last_err, last_err_ts FROM collector_heartbeat")
        }

    # -- shards (manifest side; writing lives in shards.py) -------------
    def register_shard(
        self,
        *,
        stream: str,
        t0: float,
        t1: float,
        path: str,
        dtype: str,
        shape: Sequence[int],
        meta: dict[str, Any] | None,
        committed_ts: float | None = None,
    ) -> int:
        cur = self.execute(
            "INSERT INTO shards (stream, t0, t1, path, dtype, shape, meta, committed_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stream,
                t0,
                t1,
                path,
                dtype,
                json.dumps(list(shape)),
                _jdump(meta),
                time.time() if committed_ts is None else committed_ts,
            ),
        )
        return int(cur.lastrowid or 0)

    def list_shards(
        self,
        stream: str | None = None,
        *,
        t0: float | None = None,
        t1: float | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Committed shards only — an unpublished temp directory has no row."""
        sql = (
            "SELECT id, stream, t0, t1, path, dtype, shape, meta, committed_ts FROM shards WHERE 1=1"
        )
        args: list[Any] = []
        if stream:
            sql += " AND stream = ?"
            args.append(stream)
        if t0 is not None:
            sql += " AND t1 >= ?"
            args.append(t0)
        if t1 is not None:
            sql += " AND t0 <= ?"
            args.append(t1)
        sql += " ORDER BY t0 ASC, id ASC"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [
            {
                "id": r["id"],
                "stream": r["stream"],
                "t0": r["t0"],
                "t1": r["t1"],
                "path": r["path"],
                "dtype": r["dtype"],
                "shape": _jload(r["shape"]),
                "meta": _jload(r["meta"]) or {},
                "committed_ts": r["committed_ts"],
            }
            for r in self.query(sql, args)
        ]

    def shard_counts(self) -> dict[str, int]:
        return {
            r["stream"]: r["n"]
            for r in self.query("SELECT stream, COUNT(*) AS n FROM shards GROUP BY stream")
        }

    # -- jobs -----------------------------------------------------------
    def submit_job(self, kind: str, params: dict[str, Any] | None = None) -> int:
        cur = self.execute(
            "INSERT INTO jobs (kind, params, state, created) VALUES (?, ?, 'queued', ?)",
            (kind, _jdump(params or {}), time.time()),
        )
        return int(cur.lastrowid or 0)

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._job_row(rows[0]) if rows else None

    def list_jobs(self, *, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs"
        args: list[Any] = []
        if state:
            sql += " WHERE state = ?"
            args.append(state)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [self._job_row(r) for r in self.query(sql, args)]

    @staticmethod
    def _job_row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["params"] = _jload(d.get("params"))
        d["result"] = _jload(d.get("result"))
        return d

    def claim_next_job(self, worker: str | None = None, lease_s: float = 60.0) -> dict[str, Any] | None:
        """Atomically move the oldest queued job to ``running`` with a lease."""
        who = worker or f"{socket.gethostname()}:{os.getpid()}"
        now = time.time()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT id FROM jobs WHERE state = 'queued' ORDER BY id ASC LIMIT 1"
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    return None
                job_id = int(row["id"])
                self._conn.execute(
                    "UPDATE jobs SET state = 'running', started = ?, lease_until = ?, worker = ? "
                    "WHERE id = ? AND state = 'queued'",
                    (now, now + lease_s, who, job_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job_row(row)

    def renew_lease(self, job_id: int, lease_s: float = 60.0) -> None:
        self.execute(
            "UPDATE jobs SET lease_until = ? WHERE id = ? AND state = 'running'",
            (time.time() + lease_s, job_id),
        )

    def set_job_log(self, job_id: int, log_path: str) -> None:
        self.execute("UPDATE jobs SET log_path = ? WHERE id = ?", (log_path, job_id))

    def finish_job(
        self,
        job_id: int,
        state: JobState,
        result: dict[str, Any] | None = None,
    ) -> None:
        if state not in ("done", "failed", "cancelled"):
            raise ValueError(f"bad terminal state {state!r}")
        self.execute(
            "UPDATE jobs SET state = ?, finished = ?, lease_until = NULL, result = ? WHERE id = ?",
            (state, time.time(), _jdump(result), job_id),
        )

    def cancel_job(self, job_id: int) -> str:
        """Cancel a job. Returns the resulting state, or '' if unknown.

        A queued job is cancelled outright; a running one is flipped to
        ``cancelled`` and the worker, which polls the state, kills the
        subprocess and stamps ``finished``.
        """
        job = self.get_job(job_id)
        if job is None:
            return ""
        if job["state"] in ("done", "failed", "cancelled"):
            return job["state"]
        if job["state"] == "queued":
            self.execute(
                "UPDATE jobs SET state = 'cancelled', finished = ?, result = ? WHERE id = ?",
                (time.time(), _jdump({"reason": "cancelled while queued"}), job_id),
            )
        else:
            self.execute("UPDATE jobs SET state = 'cancelled' WHERE id = ?", (job_id,))
        return "cancelled"

    def reconcile_orphaned_jobs(self, now: float | None = None) -> list[int]:
        """Fail every ``running`` job whose lease has expired (worker restart)."""
        t = time.time() if now is None else now
        rows = self.query(
            "SELECT id FROM jobs WHERE state = 'running' AND (lease_until IS NULL OR lease_until < ?)",
            (t,),
        )
        ids = [int(r["id"]) for r in rows]
        for job_id in ids:
            self.execute(
                "UPDATE jobs SET state = 'failed', finished = ?, lease_until = NULL, result = ? "
                "WHERE id = ?",
                (t, _jdump({"reason": "orphaned"}), job_id),
            )
            self.add_event(
                "job_orphaned",
                severity="warn",
                subject=f"job {job_id}",
                detail={"job_id": job_id, "reason": "orphaned"},
                ts=t,
            )
        return ids
