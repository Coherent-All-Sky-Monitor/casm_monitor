"""Store: scalars, events, shard atomicity/visibility, retention, watermarks."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from casm_monitor.store import (
    ShardReader,
    ShardWriter,
    Store,
    UnsafePathError,
    add_shard_ref,
    apply_retention,
    drop_shard_ref,
)
from casm_monitor.store.shards import iso_compact


def test_scalars_roundtrip_and_latest(store: Store):
    t0 = 1_000_000.0
    store.put_scalar("obs.utc_start", "2026-09-04-08:42:33", ts=t0)
    store.put_scalar("disk.mnt_nvme3.pct_used", 26.0, ts=t0, tags={"mount": "/mnt/nvme3"})
    store.put_scalar("disk.mnt_nvme3.pct_used", 27.5, ts=t0 + 10)

    latest = store.latest_scalars()
    assert latest["obs.utc_start"]["value"] == "2026-09-04-08:42:33"
    assert latest["disk.mnt_nvme3.pct_used"]["value"] == 27.5
    assert store.latest_scalar("disk.mnt_nvme3.pct_used")["ts"] == t0 + 10
    assert store.latest_scalar("nope") is None
    # tags survive as parsed json
    first = store.query(
        "SELECT tags FROM scalars WHERE name = ? ORDER BY ts LIMIT 1", ("disk.mnt_nvme3.pct_used",)
    )
    assert "/mnt/nvme3" in first[0]["tags"]


def test_scalars_series_decimation(store: Store):
    for i in range(1000):
        store.put_scalar("gpu.util_pct", float(i), ts=1000.0 + i)
    t, v = store.series("gpu.util_pct", max_points=100)
    assert len(t) == len(v) <= 101
    assert v[0] == 0.0 and v[-1] == 999.0
    t, v = store.series("gpu.util_pct", t0=1500.0, t1=1509.0, max_points=2000)
    assert v == [float(i) for i in range(500, 510)]


def test_events(store: Store):
    store.add_event("obs_restart", subject="observation", detail={"to": "x"}, ts=10.0)
    store.add_event("collector_failing", severity="error", subject="gpus", ts=20.0)
    rows = store.events()
    assert [r["kind"] for r in rows] == ["collector_failing", "obs_restart"]  # newest first
    assert store.events(severity="error")[0]["subject"] == "gpus"
    assert store.events(kind="obs_restart")[0]["detail"] == {"to": "x"}
    assert store.events(since=15.0) and len(store.events(since=15.0)) == 1
    with pytest.raises(ValueError):
        store.add_event("bad", severity="critical")


def test_watermarks(store: Store):
    assert store.get_watermark("kafka", "end_offset_total", 0) == 0
    store.set_watermark("kafka", "end_offset_total", 12345)
    store.set_watermark("kafka", "end_offset_total", 12400)
    assert store.get_watermark("kafka", "end_offset_total") == 12400


def test_heartbeats(store: Store):
    store.heartbeat_ok("obs", ts=5.0)
    store.heartbeat_err("gpus", "boom", ts=6.0)
    hb = store.heartbeats()
    assert hb["obs"]["last_ok"] == 5.0
    assert hb["gpus"]["last_err"] == "boom" and hb["gpus"]["last_err_ts"] == 6.0


def test_shard_commit_is_atomic(store: Store, shard_writer: ShardWriter):
    arr = np.arange(24, dtype="float32").reshape(2, 12)
    row = shard_writer.write("demo", arr, t0=1000.0, t1=1010.0, meta={"note": "hi"})
    reader = ShardReader(store)
    listed = reader.list("demo")
    assert len(listed) == 1 and listed[0]["path"] == row["path"]
    assert listed[0]["shape"] == [2, 12] and listed[0]["dtype"] == "float32"
    back, meta = reader.load(listed[0])
    assert np.array_equal(back, arr) and meta["note"] == "hi"
    # published under its t0 ISO name, and no temp directory left behind
    sdir = shard_writer.stream_dir("demo")
    assert (sdir / iso_compact(1000.0)).is_dir()
    assert not [p for p in sdir.iterdir() if p.name.startswith(".tmp-")]


def test_uncommitted_shard_is_invisible(store: Store, shard_writer: ShardWriter):
    """A temp directory with no manifest row must not be listed."""
    sdir = shard_writer.stream_dir("demo")
    sdir.mkdir(parents=True, exist_ok=True)
    orphan = sdir / ".tmp-halfwritten"
    orphan.mkdir()
    (orphan / "zarr.json").write_text("{}")
    assert ShardReader(store).list("demo") == []

    # a failed write leaves nothing published either
    class Boom(Exception):
        pass

    def explode(*_a, **_k):
        raise Boom("disk went away")

    import casm_monitor.store.shards as shards_mod

    original = shards_mod.zarr.open_group
    shards_mod.zarr.open_group = explode
    try:
        with pytest.raises(Boom):
            shard_writer.write("demo", np.zeros(3), t0=2000.0)
    finally:
        shards_mod.zarr.open_group = original
    assert ShardReader(store).list("demo") == []
    assert store.list_shards("demo") == []


def test_retention_only_expired_and_never_referenced(store: Store, shard_writer: ShardWriter):
    now = time.time()
    old = shard_writer.write("demo", np.zeros(4), t0=now - 5 * 86400, t1=now - 5 * 86400)
    old_pinned = shard_writer.write(
        "demo", np.zeros(4), t0=now - 6 * 86400, t1=now - 6 * 86400, meta={"pinned": True}
    )
    old_referenced = shard_writer.write("demo", np.zeros(4), t0=now - 7 * 86400, t1=now - 7 * 86400)
    old_job_ref = shard_writer.write("demo", np.zeros(4), t0=now - 8 * 86400, t1=now - 8 * 86400)
    fresh = shard_writer.write("demo", np.zeros(4), t0=now - 60, t1=now - 60)
    untracked = shard_writer.write("other", np.zeros(4), t0=now - 99 * 86400)

    # References are by shard id, never by a string path in some JSON blob.
    add_shard_ref(store, old_referenced["id"], "cal-tab")
    job_id = store.submit_job("noop", {"shard_id": old_job_ref["id"]})  # queued -> referenced

    deleted = apply_retention(store, {"demo": 86400.0}, now=now)
    deleted_paths = {d["path"] for d in deleted}
    assert deleted_paths == {old["path"]}
    assert not Path(old["path"]).exists()
    for keep in (old_pinned, old_referenced, old_job_ref, fresh, untracked):
        assert Path(keep["path"]).exists()
    assert {r["path"] for r in store.list_shards("demo")} == {
        old_pinned["path"],
        old_referenced["path"],
        old_job_ref["path"],
        fresh["path"],
    }
    # 'other' has no TTL configured, so it is never touched
    assert len(store.list_shards("other")) == 1

    # The job reference lapses when the job does; the explicit one does not.
    store.finish_job(job_id, "done", {})
    deleted = apply_retention(store, {"demo": 86400.0}, now=now)
    assert {d["path"] for d in deleted} == {old_job_ref["path"]}
    assert not Path(old_job_ref["path"]).exists()
    assert Path(old_referenced["path"]).exists()
    drop_shard_ref(store, old_referenced["id"])
    assert {d["path"] for d in apply_retention(store, {"demo": 86400.0}, now=now)} == {
        old_referenced["path"]
    }
    assert not Path(old_referenced["path"]).exists()


def test_retention_delete_failure_leaves_manifest_consistent(
    store: Store, shard_writer: ShardWriter, monkeypatch
):
    """A failed rmtree is logged, not swallowed, and retried on the next pass."""
    now = time.time()
    row = shard_writer.write("demo", np.zeros(4), t0=now - 5 * 86400, t1=now - 5 * 86400)
    import casm_monitor.store.shards as shards_mod

    def boom(path, *_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shards_mod.shutil, "rmtree", boom)
    deleted = apply_retention(store, {"demo": 86400.0}, now=now)

    assert {d["path"] for d in deleted} == {row["path"]}
    # manifest row gone (the shard is unpublished), data parked in the trash
    assert store.list_shards("demo") == []
    assert not Path(row["path"]).exists()
    trash = Path(store.store_root) / ".trash" / str(row["id"])
    assert trash.is_dir()
    assert store.events(kind="retention_delete_failed")[0]["detail"]["shard_id"] == row["id"]

    # next pass (rmtree working again) empties the trash
    monkeypatch.undo()
    apply_retention(store, {"demo": 86400.0}, now=now)
    assert not trash.exists()


def test_stream_names_and_paths_are_contained(store: Store, settings, shard_writer: ShardWriter):
    """No write or delete may leave the store root, whatever the name says."""
    for bad in ("../../etc", "/etc/passwd", "..", "demo/../..", "de mo", ""):
        with pytest.raises(UnsafePathError):
            shard_writer.write(bad, np.zeros(2), t0=1000.0)
    with pytest.raises(UnsafePathError):
        shard_writer.stream_dir("../escape")
    # a shards_root outside the store root is refused at construction
    with pytest.raises(UnsafePathError):
        ShardWriter(store, Path(settings.store_root).parent / "elsewhere")

    # an absolute manifest path (hand-edited row / older schema) is refused,
    # left in the manifest, and reported
    outside = Path(settings.store_root).parent / "not_ours"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "keep.txt").write_text("precious")
    store.register_shard(
        stream="demo",
        t0=1000.0,
        t1=1000.0,
        path=str(outside),
        dtype="float32",
        shape=[1],
        meta={},
    )
    deleted = apply_retention(store, {"demo": 1.0}, now=time.time())
    assert deleted == []
    assert (outside / "keep.txt").exists()
    assert len(store.list_shards("demo")) == 1
    assert store.events(kind="retention_path_refused")


def test_read_only_store_cannot_write(settings, store: Store):
    store.put_scalar("x", 1.0)
    ro = Store(settings.db_path, read_only=True, store_root=settings.store_root)
    try:
        assert ro.latest_scalar("x")["value"] == 1.0
        import sqlite3

        with pytest.raises(sqlite3.OperationalError):
            ro.put_scalar("x", 2.0)
    finally:
        ro.close()
