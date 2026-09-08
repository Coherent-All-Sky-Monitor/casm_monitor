"""Node-level collectors: disks, GPUs and the store's own footprint."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..store.shards import store_disk_usage
from ..util import run
from .base import Collector, CollectorContext

DISK_WARN_PCT = 90.0


def _key(mount: str) -> str:
    return mount.strip("/").replace("/", "_")


class DisksCollector(Collector):
    """Free space on the mounts the pipeline writes to."""

    name = "disks"
    default_cadence_s = 300.0
    timeout_s = 30.0

    def collect(self, ctx: CollectorContext) -> None:
        for mount in ctx.settings.disks:
            try:
                usage = shutil.disk_usage(mount)
            except OSError as exc:
                ctx.event(
                    "disk_unavailable",
                    severity="warn",
                    subject=mount,
                    detail={"error": str(exc)},
                )
                continue
            pct = 100.0 * usage.used / usage.total if usage.total else 0.0
            name = _key(mount)
            ctx.scalar(f"disk.{name}.pct_used", round(pct, 2), tags={"mount": mount})
            ctx.scalar(
                f"disk.{name}.free_gb", round(usage.free / 1e9, 2), tags={"mount": mount}
            )
            ctx.on_change(
                f"disk.{name}.almost_full",
                1 if pct > DISK_WARN_PCT else 0,
                kind="disk_almost_full",
                severity="warn",
                subject=mount,
                detail={"pct_used": round(pct, 2), "free_gb": round(usage.free / 1e9, 2)},
            )


class GpusCollector(Collector):
    """nvidia-smi utilisation and memory (query only, no GPU work here)."""

    name = "gpus"
    default_cadence_s = 60.0
    timeout_s = 30.0

    def collect(self, ctx: CollectorContext) -> None:
        rc, out, err = run(
            [
                "nvidia-smi",
                "--query-gpu=index,utilization.gpu,memory.used",
                "--format=csv,noheader",
            ],
            timeout=20.0,
        )
        if rc != 0:
            raise RuntimeError(f"nvidia-smi failed (rc={rc}): {err.strip()[:200]}")
        utils: list[float] = []
        n = 0
        for line in out.strip().splitlines():
            fields = [f.strip() for f in line.split(",")]
            if len(fields) < 3:
                continue
            try:
                index = int(fields[0])
                util = float(fields[1].split()[0])
                mem = float(fields[2].split()[0])
            except (ValueError, IndexError):
                continue
            n += 1
            utils.append(util)
            tags = {"gpu": index}
            ctx.scalar("gpu.util_pct", util, tags=tags)
            ctx.scalar("gpu.mem_used_mb", mem, tags=tags)
        ctx.scalar("gpu.n", n)
        if utils:
            ctx.scalar("gpu.util_max_pct", max(utils))
            ctx.scalar("gpu.util_mean_pct", round(sum(utils) / len(utils), 2))


class StoreCollector(Collector):
    """The monitor's own store size and shard bookkeeping."""

    name = "store"
    default_cadence_s = 300.0
    timeout_s = 120.0

    def collect(self, ctx: CollectorContext) -> None:
        root = Path(ctx.settings.store_root)
        total = store_disk_usage(root) if root.exists() else 0
        db = Path(ctx.settings.db_path)
        db_bytes = db.stat().st_size if db.exists() else 0
        counts = ctx.store.shard_counts()
        ctx.scalar("store.bytes", total)
        ctx.scalar("store.sqlite_bytes", db_bytes)
        ctx.scalar("store.shards", int(sum(counts.values())))
        for stream, n in counts.items():
            ctx.scalar("store.shards_by_stream", n, tags={"stream": stream})
        ctx.scalar("store.streams", len(counts))
