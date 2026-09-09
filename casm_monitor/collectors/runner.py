"""The collector runner: one asyncio process driving every collector.

Each collector gets its own loop at its own cadence and runs in a thread (they
all do blocking I/O). Failures are isolated: an exception or a timeout in one
collector is written to ``collector_heartbeat``, recorded as a
``collector_ok = 0`` scalar, and after three consecutive failures raised as an
``error`` event — the other collectors keep their cadence throughout.

A timeout cancels only our wait, never the thread (Python cannot interrupt one).
So the runner keeps the ``concurrent.futures`` handle of the invocation and
refuses to start the next iteration of that collector while the previous thread
is still alive: the skip is recorded as ``collector_overlap = 1`` plus one
``collector_overlap`` event per stretch of skips. That is what stops a slow
collector from duplicating external probes and store writes. The other half of
the guarantee lives in :func:`casm_monitor.util.run`: every subprocess probe
(ssh, nvidia-smi, ps, ss, pgrep) has a subprocess-level timeout, so the thread
really does end instead of hanging for ever.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import logging
import signal
import time
import traceback
from typing import Iterable, Sequence

from ..config import Settings, load_settings
from ..store import ShardWriter, Store, apply_retention
from .base import Collector, CollectorContext
from .hella import HellaCollector
from .kafka_bp import KafkaBandpassCollector
from .nodes import DisksCollector, GpusCollector, StoreCollector
from .obs import ObsCollector
from .services import ServicesCollector, ZapdosCollector
from .sky import SkyCollector
from .snapread import SnapReadCollector
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
        KafkaBandpassCollector(settings),
        ZapdosCollector(settings),
        DisksCollector(settings),
        GpusCollector(settings),
        WeightsCollector(settings),
        SkyCollector(settings),
        SnapReadCollector(settings),
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
        # Our own pool (never the default executor) so we hold the real thread
        # handle and can tell "still running" from "we stopped waiting".
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(4, len(self.collectors) + 2), thread_name_prefix="collector"
        )
        self._inflight: dict[str, concurrent.futures.Future] = {}
        self._overlapping: set[str] = set()

    # -- one pass -------------------------------------------------------
    async def run_once(self, collector: Collector) -> bool:
        """Run one collector, isolating its failure. True if it succeeded.

        Returns False both for a failure and for a skipped run (a previous
        invocation of this collector is still in its thread).
        """
        if self._skip_if_overlapping(collector):
            return False
        started = time.time()
        future = self._pool.submit(collector.collect, self.ctx)
        self._inflight[collector.name] = future
        try:
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=collector.timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # The thread keeps going; the overlap guard above is what protects
            # the next iteration from running on top of it.
            self._record_failure(collector, f"timeout after {collector.timeout_s}s")
            return False
        except Exception:
            self._record_failure(collector, traceback.format_exc(limit=6))
            return False
        self.failures[collector.name] = 0
        self._overlapping.discard(collector.name)
        self.store.heartbeat_ok(collector.name)
        self.store.put_scalar("collector_ok", 1, tags={"collector": collector.name})
        self.store.put_scalar("collector_overlap", 0, tags={"collector": collector.name})
        self.store.put_scalar(
            "collector_duration_s", round(time.time() - started, 3), tags={"collector": collector.name}
        )
        return True

    def _skip_if_overlapping(self, collector: Collector) -> bool:
        """True when the previous invocation of ``collector`` is still alive."""
        previous = self._inflight.get(collector.name)
        if previous is None or previous.done():
            return False
        self.store.put_scalar("collector_overlap", 1, tags={"collector": collector.name})
        log.warning(
            "collector %s still running from the previous iteration; skipping this one",
            collector.name,
        )
        if collector.name not in self._overlapping:
            self._overlapping.add(collector.name)
            self.store.add_event(
                "collector_overlap",
                severity="warn",
                subject=collector.name,
                detail={"timeout_s": collector.timeout_s, "cadence_s": collector.cadence_s},
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
                    removed = apply_retention(
                        self.store, ttls, store_root=self.settings.store_root
                    )
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
        # Give every collector a chance to flush anything buffered in memory
        # (e.g. KafkaBandpassCollector's shard buffers) before the store
        # closes, so an orderly SIGTERM/stop loses nothing. Isolated per
        # collector, same as a collect() failure: one broken close() must not
        # stop the others or the shutdown itself.
        for collector in self.collectors:
            try:
                collector.close(self.ctx)
            except Exception:
                log.exception("collector %s close() failed", collector.name)
        # wait=False: a collector thread stuck in a syscall must not stop the
        # process from exiting (systemd would kill us anyway).
        self._pool.shutdown(wait=False, cancel_futures=True)
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
