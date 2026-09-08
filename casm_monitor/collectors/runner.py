"""The collector runner: one asyncio process driving every collector.

Each collector gets its own loop at its own cadence and runs in a thread (they
all do blocking I/O). Failures are isolated: an exception or a timeout in one
collector is written to ``collector_heartbeat``, recorded as a
``collector_ok = 0`` scalar, and after three consecutive failures raised as an
``error`` event — the other collectors keep their cadence throughout.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
import traceback
from typing import Iterable, Sequence

from ..config import Settings, load_settings
from ..store import ShardWriter, Store, apply_retention
from .base import Collector, CollectorContext
from .hella import HellaCollector
from .nodes import DisksCollector, GpusCollector, StoreCollector
from .obs import ObsCollector
from .services import ServicesCollector, ZapdosCollector
from .sky import SkyCollector
from .weights import WeightsCollector

log = logging.getLogger("casm_monitor.collect")

FAILURES_BEFORE_EVENT = 3
RETENTION_INTERVAL_S = 3600.0


def default_collectors(settings: Settings) -> list[Collector]:
    """The M0 collector set."""
    return [
        ObsCollector(settings),
        HellaCollector(settings, node="corr1"),
        HellaCollector(settings, node="corr2", cadence_s=settings.cadence("hella_corr2", 600.0)),
        ServicesCollector(settings),
        ZapdosCollector(settings),
        DisksCollector(settings),
        GpusCollector(settings),
        WeightsCollector(settings),
        SkyCollector(settings),
        StoreCollector(settings),
    ]


class CollectorRunner:
    """Owns the store handle and the per-collector loops."""

    def __init__(
        self,
        settings: Settings,
        collectors: Sequence[Collector] | None = None,
        *,
        store: Store | None = None,
    ) -> None:
        self.settings = settings
        self._own_store = store is None
        self.store = store or Store(settings.db_path, store_root=settings.store_root)
        self.ctx = CollectorContext(
            settings=settings,
            store=self.store,
            shards=ShardWriter(self.store, settings.shards_root),
        )
        self.collectors = list(collectors) if collectors is not None else default_collectors(settings)
        self.failures: dict[str, int] = {c.name: 0 for c in self.collectors}
        self._stop = asyncio.Event()

    # -- one pass -------------------------------------------------------
    async def run_once(self, collector: Collector) -> bool:
        """Run one collector, isolating its failure. True if it succeeded."""
        loop = asyncio.get_running_loop()
        started = time.time()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, collector.collect, self.ctx),
                timeout=collector.timeout_s,
            )
        except asyncio.TimeoutError:
            self._record_failure(collector, f"timeout after {collector.timeout_s}s")
            return False
        except Exception:
            self._record_failure(collector, traceback.format_exc(limit=6))
            return False
        self.failures[collector.name] = 0
        self.store.heartbeat_ok(collector.name)
        self.store.put_scalar("collector_ok", 1, tags={"collector": collector.name})
        self.store.put_scalar(
            "collector_duration_s", round(time.time() - started, 3), tags={"collector": collector.name}
        )
        return True

    def _record_failure(self, collector: Collector, message: str) -> None:
        self.failures[collector.name] = self.failures.get(collector.name, 0) + 1
        count = self.failures[collector.name]
        self.store.heartbeat_err(collector.name, message)
        self.store.put_scalar("collector_ok", 0, tags={"collector": collector.name})
        log.warning("collector %s failed (%d in a row): %s", collector.name, count, message.strip()[:400])
        if count == FAILURES_BEFORE_EVENT:
            self.store.add_event(
                "collector_failing",
                severity="error",
                subject=collector.name,
                detail={"consecutive_failures": count, "error": message.strip()[-1000:]},
            )

    # -- loops ----------------------------------------------------------
    async def _collector_loop(self, collector: Collector) -> None:
        while not self._stop.is_set():
            await self.run_once(collector)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=collector.cadence_s)
            except asyncio.TimeoutError:
                pass

    async def _retention_loop(self) -> None:
        ttls = {
            stream: self.settings.shard_ttl_s(stream) or 0.0
            for stream in self.settings.shard_ttl_days
        }
        while not self._stop.is_set():
            if ttls:
                try:
                    removed = apply_retention(self.store, ttls)
                    if removed:
                        self.store.add_event(
                            "retention_run",
                            severity="info",
                            subject="shards",
                            detail={"deleted": len(removed)},
                        )
                except Exception:
                    log.exception("retention pass failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RETENTION_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        self.store.add_event(
            "collector_started",
            severity="info",
            subject="casm-monitor-collect",
            detail={"collectors": [c.name for c in self.collectors]},
        )
        tasks = [asyncio.create_task(self._collector_loop(c)) for c in self.collectors]
        tasks.append(asyncio.create_task(self._retention_loop()))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        if self._own_store:
            self.store.close()


async def _amain(settings: Settings, once: bool, only: Iterable[str] | None) -> int:
    runner = CollectorRunner(settings)
    if only:
        wanted = set(only)
        runner.collectors = [c for c in runner.collectors if c.name in wanted]
    try:
        if once:
            ok = True
            for collector in runner.collectors:
                ok = await runner.run_once(collector) and ok
            return 0 if ok else 1
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, runner.stop)
        await runner.run()
        return 0
    finally:
        runner.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CASM monitor collector runner")
    parser.add_argument("--config", default=None, help="path of monitor.yaml")
    parser.add_argument("--once", action="store_true", help="run each collector once and exit")
    parser.add_argument(
        "--only", action="append", default=None, help="run only this collector (repeatable)"
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = load_settings(args.config)
    log.info(
        "store=%s config=%s collectors cadence obs=%.0fs services=%.0fs",
        settings.store_root,
        settings.config_path,
        settings.cadence("obs"),
        settings.cadence("services"),
    )
    return asyncio.run(_amain(settings, args.once, args.only))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
