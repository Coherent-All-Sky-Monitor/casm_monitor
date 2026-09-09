"""Immutable array shards: zarr v3 groups published by a SQLite manifest row.

Contract (from the plan): a shard is written into a temp directory, fsynced,
renamed into its final name, and only then does one SQLite transaction insert
the manifest row. Readers list shards through the manifest, so a half-written
shard is invisible; retention deletes only manifest rows it selected (and never
a pinned or referenced one).

Containment (hard rule): every path this module writes to or deletes is
resolved and refused unless it lies under the resolved ``store_root``. Stream
names and shard directory names are single path components matching
``[A-Za-z0-9_.-]+``, so never ``..``, never absolute, never a crafted name that
walks out of the store. This holds whether the name comes from a config file, a
collector or a manifest row.

Retention semantics: a shard is kept while its ``meta`` says ``pinned``, or
while something holds an explicit reference to its **id** — a ``shard_refs``
row, or an unfinished job whose params/result carry ``shard_id``/``shard_ids``.
Deletion is manifest-row-first (one statement, one transaction), then a rename
into ``store_root/.trash/<id>`` and an rmtree; a failing rmtree is logged and
its trash entry left for the next pass, never swallowed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import zarr

from .db import Store

log = logging.getLogger("casm_monitor.store")

DATA_ARRAY = "data"
TRASH_DIRNAME = ".trash"
SAFE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")

SHARD_REFS_DDL = """
CREATE TABLE IF NOT EXISTS shard_refs (
    shard_id INTEGER NOT NULL,
    owner    TEXT NOT NULL,   -- 'job:<id>' (lapses with that job) or any tag
    created  REAL NOT NULL,
    PRIMARY KEY (shard_id, owner)
)
"""

_JOB_OWNER_RE = re.compile(r"^job:(\d+)$")


class UnsafePathError(ValueError):
    """A path or name the store refuses to write to or delete."""


def safe_name(value: str, what: str = "name") -> str:
    """Validate one path component (stream name, shard directory name)."""
    text = str(value)
    if not SAFE_NAME_RE.fullmatch(text) or text in {".", ".."}:
        raise UnsafePathError(f"unsafe {what} {value!r}: expected [A-Za-z0-9_.-]+")
    return text


def ensure_contained(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> Path:
    """Resolve ``path`` and refuse it unless it is inside ``root``."""
    resolved_root = Path(root).resolve()
    resolved = Path(path).resolve()
    if resolved != resolved_root and not resolved.is_relative_to(resolved_root):
        raise UnsafePathError(f"path {resolved} is outside {resolved_root}")
    return resolved


def iso_compact(ts: float) -> str:
    """UTC timestamp -> directory-safe name, e.g. 2026-09-08T15-04-05Z.

    Hyphens, not colons, in the time part: a shard directory name has to be one
    safe path component (:func:`safe_name`).
    """
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _fsync_tree(root: Path) -> None:
    """fsync every file and directory under ``root`` (durability before rename)."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            fd = os.open(os.path.join(dirpath, fn), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(dirpath, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ShardWriter:
    """Writes one array (+meta) per shard under ``store_root/shards/<stream>/``.

    ``store_root`` comes from the :class:`Store`; a ``shards_root`` outside it is
    refused at construction time rather than at the first write.
    """

    def __init__(self, store: Store, shards_root: str | os.PathLike[str] | None = None) -> None:
        self.store = store
        self.store_root = Path(store.store_root).resolve()
        root = Path(shards_root) if shards_root else self.store_root / "shards"
        self.root = ensure_contained(root, self.store_root)

    def stream_dir(self, stream: str) -> Path:
        return ensure_contained(self.root / safe_name(stream, "stream"), self.store_root)

    def write(
        self,
        stream: str,
        array: np.ndarray,
        *,
        t0: float,
        t1: float | None = None,
        meta: dict[str, Any] | None = None,
        chunks: tuple[int, ...] | None = None,
    ) -> dict[str, Any]:
        """Write and publish a shard; returns the manifest row as a dict."""
        arr = np.asarray(array)
        t1 = t0 if t1 is None else t1
        meta = dict(meta or {})
        # Idempotent by (stream, t0): a replay after a crash re-derives the same
        # samples from the same records, and a second copy of an already
        # committed shard would duplicate that history for every reader. The
        # committed one wins and this call is a no-op.
        for row in self.store.list_shards(stream, t0=t0, t1=t0):
            if float(row["t0"]) == float(t0):
                log.info(
                    "shard for %s t0=%.3f is already committed (id %s); skipping the rewrite",
                    stream, t0, row["id"],
                )
                return row
        sdir = self.stream_dir(stream)
        sdir.mkdir(parents=True, exist_ok=True)
        final = self._shard_path(sdir, iso_compact(t0))
        if final.exists():
            # Same t0 twice (a replay): keep the published shard, do not clobber.
            rows = self.store.list_shards(stream, t0=t0, t1=t0)
            for r in rows:
                if r["path"] == str(final):
                    return r
            final = self._shard_path(sdir, f"{iso_compact(t0)}_{uuid.uuid4().hex[:6]}")

        tmp = self._shard_path(sdir, f".tmp-{uuid.uuid4().hex}")
        try:
            group = zarr.open_group(store=str(tmp), mode="w", zarr_format=3)
            zarr_array = group.create_array(
                DATA_ARRAY, shape=arr.shape, dtype=arr.dtype, chunks=chunks or arr.shape
            )
            zarr_array[...] = arr
            group.attrs["meta"] = json.loads(json.dumps(meta, default=str))
            group.attrs["t0"] = t0
            group.attrs["t1"] = t1
            group.attrs["stream"] = stream
            del zarr_array, group
            _fsync_tree(tmp)
            os.rename(tmp, final)
            _fsync_dir(sdir)
        except Exception:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            raise

        shard_id = self.store.register_shard(
            stream=stream,
            t0=t0,
            t1=t1,
            path=str(final),
            dtype=str(arr.dtype),
            shape=arr.shape,
            meta=meta,
            committed_ts=time.time(),
        )
        return {
            "id": shard_id,
            "stream": stream,
            "t0": t0,
            "t1": t1,
            "path": str(final),
            "dtype": str(arr.dtype),
            "shape": list(arr.shape),
            "meta": meta,
        }

    def _shard_path(self, sdir: Path, name: str) -> Path:
        """One safe component under ``sdir``, resolved inside the store root."""
        safe_name(name[1:] if name.startswith(".tmp-") else name, "shard id")
        return ensure_contained(sdir / name, self.store_root)


class ShardReader:
    """Reads shards, listing only committed ones (manifest rows)."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def list(
        self,
        stream: str | None = None,
        *,
        t0: float | None = None,
        t1: float | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_shards(stream, t0=t0, t1=t1, limit=limit)

    def load(self, shard: dict[str, Any] | int) -> tuple[np.ndarray, dict[str, Any]]:
        """Load a shard's array and meta by manifest row (or row id)."""
        if isinstance(shard, int):
            rows = self.store.query(
                "SELECT id, stream, t0, t1, path, dtype, shape, meta FROM shards WHERE id = ?",
                (shard,),
            )
            if not rows:
                raise KeyError(f"no committed shard with id {shard}")
            path = rows[0]["path"]
            meta = json.loads(rows[0]["meta"] or "{}")
        else:
            path = shard["path"]
            meta = shard.get("meta") or {}
        group = zarr.open_group(store=str(path), mode="r")
        return np.asarray(group[DATA_ARRAY][...]), meta


# -- explicit references ------------------------------------------------
def ensure_shard_refs(store: Store) -> None:
    """Create the ``shard_refs`` table if this store has not got it yet."""
    if store.read_only:
        return
    store.execute(SHARD_REFS_DDL)


def add_shard_ref(store: Store, shard_id: int, owner: str) -> None:
    """Pin a shard by id. ``owner='job:<id>'`` lapses with that job."""
    ensure_shard_refs(store)
    store.execute(
        "INSERT OR REPLACE INTO shard_refs (shard_id, owner, created) VALUES (?, ?, ?)",
        (int(shard_id), str(owner), time.time()),
    )


def drop_shard_ref(store: Store, shard_id: int, owner: str | None = None) -> None:
    """Release one reference (or every reference) to a shard id."""
    ensure_shard_refs(store)
    if owner is None:
        store.execute("DELETE FROM shard_refs WHERE shard_id = ?", (int(shard_id),))
    else:
        store.execute(
            "DELETE FROM shard_refs WHERE shard_id = ? AND owner = ?", (int(shard_id), str(owner))
        )


def referenced_shard_ids(store: Store) -> set[int]:
    """Shard ids something still holds a reference to, by id, never by path."""
    ensure_shard_refs(store)
    out: set[int] = set()
    job_states = {int(r["id"]): str(r["state"]) for r in store.query("SELECT id, state FROM jobs")}
    for r in store.query("SELECT shard_id, owner FROM shard_refs"):
        match = _JOB_OWNER_RE.match(str(r["owner"]))
        if match and job_states.get(int(match.group(1))) not in ("queued", "running"):
            continue  # the job that pinned it has finished
        out.add(int(r["shard_id"]))
    for r in store.query("SELECT params, result FROM jobs WHERE state IN ('queued', 'running')"):
        for blob in (r["params"], r["result"]):
            if not blob:
                continue
            try:
                obj = json.loads(blob)
            except (TypeError, ValueError):
                continue
            _collect_shard_ids(obj, out)
    return out


def _collect_shard_ids(obj: Any, out: set[int]) -> None:
    """Collect ints under any ``shard_id``/``shard_ids`` key, at any depth."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key) in ("shard_id", "shard_ids"):
                items = value if isinstance(value, (list, tuple)) else [value]
                for item in items:
                    try:
                        out.add(int(item))
                    except (TypeError, ValueError):
                        continue
            _collect_shard_ids(value, out)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _collect_shard_ids(value, out)


# -- retention ----------------------------------------------------------
def apply_retention(
    store: Store,
    ttl_s: dict[str, float],
    *,
    now: float | None = None,
    dry_run: bool = False,
    store_root: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Delete shards older than their per-stream TTL.

    Never touches a stream without a TTL, never a shard whose meta carries
    ``pinned: true``, never one referenced by id (see
    :func:`referenced_shard_ids`), and never a path outside the store root: such
    a row is refused, logged as a ``retention_path_refused`` event and left in
    the manifest for a human.

    Returns the manifest rows that were retired (the row is gone from the
    manifest even when the data deletion has to be retried next pass).
    """
    t = time.time() if now is None else now
    root = Path(store_root if store_root is not None else store.store_root).resolve()
    trash = root / TRASH_DIRNAME
    referenced = referenced_shard_ids(store)
    deleted: list[dict[str, Any]] = []

    if not dry_run:
        _empty_trash(trash)

    for stream, ttl in ttl_s.items():
        cutoff = t - float(ttl)
        for row in store.list_shards(stream):
            if row["t1"] >= cutoff:
                continue
            if (row["meta"] or {}).get("pinned"):
                continue
            if int(row["id"]) in referenced:
                continue
            try:
                path = ensure_contained(row["path"], root)
                safe_name(Path(row["path"]).name, "shard id")
            except UnsafePathError as exc:
                log.error("retention refused shard %s: %s", row["id"], exc)
                store.add_event(
                    "retention_path_refused",
                    severity="error",
                    subject=str(row["path"]),
                    detail={"shard_id": row["id"], "stream": stream, "error": str(exc)},
                )
                continue
            if dry_run:
                deleted.append(row)
                continue
            _retire_shard(store, row, path, trash)
            deleted.append(row)
    return deleted


def _retire_shard(store: Store, row: dict[str, Any], path: Path, trash: Path) -> None:
    """Unpublish then delete one shard: manifest row first, data second."""
    store.execute("DELETE FROM shards WHERE id = ?", (row["id"],))
    dest = trash / str(row["id"])
    try:
        trash.mkdir(parents=True, exist_ok=True)
        if dest.exists():  # an earlier pass could not delete it; keep both
            dest = trash / f"{row['id']}-{uuid.uuid4().hex[:6]}"
        if path.exists():
            os.rename(path, ensure_contained(dest, trash))
        if dest.exists():
            shutil.rmtree(dest)
    except OSError as exc:
        # Never silent: the row is already gone (the shard is unpublished), the
        # bytes stay under .trash for the next pass to retry.
        log.error("retention could not delete shard %s (%s): %s", row["id"], path, exc)
        store.add_event(
            "retention_delete_failed",
            severity="warn",
            subject=str(path),
            detail={"shard_id": row["id"], "trash": str(dest), "error": str(exc)},
        )


def _empty_trash(trash: Path) -> None:
    """Retry the deletions an earlier pass could not finish."""
    if not trash.is_dir():
        return
    for entry in sorted(trash.iterdir()):
        try:
            target = ensure_contained(entry, trash)
            safe_name(entry.name, "trash entry")
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except (OSError, UnsafePathError) as exc:
            log.warning("trash entry %s still not deletable: %s", entry, exc)


def store_disk_usage(store_root: str | os.PathLike[str]) -> int:
    """Total bytes under ``store_root`` (cheap enough at M0 sizes)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(store_root):
        for fn in filenames:
            try:
                total += os.stat(os.path.join(dirpath, fn)).st_size
            except OSError:
                pass
    return total
