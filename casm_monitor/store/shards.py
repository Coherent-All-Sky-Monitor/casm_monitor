"""Immutable array shards: zarr v3 groups published by a SQLite manifest row.

Contract (from the plan): a shard is written into a temp directory, fsynced,
renamed into its final name, and only then does one SQLite transaction insert
the manifest row. Readers list shards through the manifest, so a half-written
shard is invisible; retention deletes only manifest rows it selected (and never
a pinned or job-referenced one).
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import zarr

from .db import Store

DATA_ARRAY = "data"


def iso_compact(ts: float) -> str:
    """UTC timestamp -> directory-safe ISO name, e.g. 2026-09-08T15:04:05Z."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    """Writes one array (+meta) per shard under ``store_root/shards/<stream>/``."""

    def __init__(self, store: Store, shards_root: str | os.PathLike[str] | None = None) -> None:
        self.store = store
        self.root = Path(shards_root) if shards_root else Path(store.store_root) / "shards"

    def stream_dir(self, stream: str) -> Path:
        return self.root / stream

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
        sdir = self.stream_dir(stream)
        sdir.mkdir(parents=True, exist_ok=True)
        final = sdir / iso_compact(t0)
        if final.exists():
            # Same t0 twice (a replay): keep the published shard, do not clobber.
            rows = self.store.list_shards(stream, t0=t0, t1=t0)
            for r in rows:
                if r["path"] == str(final):
                    return r
            final = sdir / f"{iso_compact(t0)}_{uuid.uuid4().hex[:6]}"

        tmp = sdir / f".tmp-{uuid.uuid4().hex}"
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


def apply_retention(
    store: Store,
    ttl_s: dict[str, float],
    *,
    now: float | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Delete shards older than their per-stream TTL.

    Never touches a stream without a TTL, never a shard whose meta carries
    ``pinned: true``, and never one referenced by a job that is still queued or
    running (its params/result mention the path).
    """
    t = time.time() if now is None else now
    referenced = _referenced_paths(store)
    deleted: list[dict[str, Any]] = []
    for stream, ttl in ttl_s.items():
        cutoff = t - float(ttl)
        for row in store.list_shards(stream):
            if row["t1"] >= cutoff:
                continue
            if (row["meta"] or {}).get("pinned"):
                continue
            if row["path"] in referenced:
                continue
            if not dry_run:
                shutil.rmtree(row["path"], ignore_errors=True)
                store.execute("DELETE FROM shards WHERE id = ?", (row["id"],))
            deleted.append(row)
    return deleted


def _referenced_paths(store: Store) -> set[str]:
    """Shard paths mentioned by an unfinished job (params or result JSON)."""
    out: set[str] = set()
    for r in store.query("SELECT params, result FROM jobs WHERE state IN ('queued', 'running')"):
        for blob in (r["params"], r["result"]):
            if not blob:
                continue
            try:
                obj = json.loads(blob)
            except (TypeError, ValueError):
                continue
            _collect_strings(obj, out)
    return out


def _collect_strings(obj: Any, out: set[str]) -> None:
    if isinstance(obj, str):
        out.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_strings(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_strings(v, out)


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
