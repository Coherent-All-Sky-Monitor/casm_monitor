"""Liveness of the services the array depends on.

Strictly read-only probes:

* redis — ``PING`` only, with the password read from medusa.cfg at runtime. No
  key is ever read, written or expired.
* kafka — a group-less ``KafkaConsumer`` (``group_id=None``): bootstrap
  metadata plus ``end_offsets`` for the configured topics. It never subscribes
  and never commits, so the broker keeps no state about us; liveness means "end
  offsets advanced since the last poll", persisted in our own watermark table.
* t2d — ``pgrep`` plus a check that port 12345 is *listening* (parsed out of
  ``ss -ltn``). The t2d sockets belong to t2d: we never connect to them.
* zapdos — one ``ssh -o BatchMode=yes -o ConnectTimeout=5 zapdos true`` at most
  once per hour, enforced both by the collector cadence and by a persisted
  watermark so a restart loop cannot turn it into a hammer.
"""

from __future__ import annotations

import time
from typing import Any

from ..config import medusa_endpoints
from ..util import run
from .base import Collector, CollectorContext

T2D_PORT = 12345


def _listening_ports() -> set[int]:
    """Ports in LISTEN state, from ``ss -ltn`` (no connection is opened)."""
    rc, out, _err = run(["ss", "-ltn"], timeout=10.0)
    ports: set[int] = set()
    if rc != 0:
        return ports
    for line in out.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        local = fields[3]
        if ":" in local:
            tail = local.rsplit(":", 1)[-1]
            if tail.isdigit():
                ports.add(int(tail))
    return ports


class ServicesCollector(Collector):
    """redis / kafka / t2d liveness."""

    name = "services"
    default_cadence_s = 30.0
    timeout_s = 30.0

    def collect(self, ctx: CollectorContext) -> None:
        self._redis(ctx)
        self._kafka(ctx)
        self._t2d(ctx)
        if ctx.settings.check_head_node:
            self._head_node(ctx)

    # -- redis ----------------------------------------------------------
    def _redis(self, ctx: CollectorContext) -> None:
        endpoints = medusa_endpoints(ctx.settings)
        ok = 0
        ping_ms: float | None = None
        try:
            import redis  # imported lazily so the store/web never need it

            client = redis.Redis(
                host=endpoints.redis_host,
                port=endpoints.redis_port,
                password=endpoints.redis_password,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            t_start = time.monotonic()
            ok = 1 if client.ping() else 0
            ping_ms = (time.monotonic() - t_start) * 1e3
            client.close()
        except Exception as exc:  # redis raises a family of its own errors
            ctx.scalar("services.redis_error", str(exc)[:200])
        ctx.on_change(
            "services.redis_ok", ok, kind="service_state", severity="warn", subject="redis"
        )
        if ping_ms is not None:
            ctx.scalar("services.redis_ping_ms", round(ping_ms, 3))

    # -- kafka ----------------------------------------------------------
    def _kafka(self, ctx: CollectorContext) -> None:
        settings = ctx.settings
        ok = 0
        advancing = 0
        try:
            from kafka import KafkaConsumer, TopicPartition

            consumer = KafkaConsumer(
                bootstrap_servers=settings.kafka_bootstrap,
                group_id=None,  # group-less: no broker-side state, no commits
                enable_auto_commit=False,
                consumer_timeout_ms=1000,
                request_timeout_ms=8000,
            )
            try:
                ok = 1
                total_now = 0
                for topic in settings.kafka_topics:
                    parts = consumer.partitions_for_topic(topic) or set()
                    tps = [TopicPartition(topic, p) for p in sorted(parts)]
                    if not tps:
                        ctx.scalar("kafka.end_offset", 0, tags={"topic": topic})
                        continue
                    ends = consumer.end_offsets(tps)
                    topic_total = int(sum(ends.values()))
                    total_now += topic_total
                    ctx.scalar("kafka.end_offset", topic_total, tags={"topic": topic})
                    ctx.scalar("kafka.partitions", len(tps), tags={"topic": topic})
                previous = ctx.store.get_watermark("kafka", "end_offset_total")
                # The first observation cannot be a stall: only a second poll
                # that sees the same total means the bus stopped moving.
                if previous is None or total_now > int(previous):
                    advancing = 1
                ctx.store.set_watermark("kafka", "end_offset_total", total_now)
                ctx.scalar("kafka.end_offset_total", total_now)
            finally:
                consumer.close(autocommit=False)
        except Exception as exc:
            ctx.scalar("services.kafka_error", str(exc)[:200])
        ctx.on_change(
            "services.kafka_ok", ok, kind="service_state", severity="warn", subject="kafka"
        )
        ctx.on_change(
            "services.kafka_advancing",
            advancing,
            kind="kafka_stalled",
            severity="warn",
            subject="kafka offsets",
        )

    # -- t2d ------------------------------------------------------------
    def _t2d(self, ctx: CollectorContext) -> None:
        rc, out, _err = run(["pgrep", "-x", "t2d"], timeout=10.0)
        running = 1 if rc == 0 and out.strip() else 0
        listening = 1 if T2D_PORT in _listening_ports() else 0
        ctx.scalar("services.t2d_port_listening", listening)
        ctx.on_change(
            "services.t2d_ok",
            1 if (running and listening) else 0,
            kind="service_state",
            severity="warn",
            subject="t2d",
        )

    # -- optional head node --------------------------------------------
    def _head_node(self, ctx: CollectorContext) -> None:
        rc, _out, err = run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", ctx.settings.corr2_ssh, "true"],
            timeout=20.0,
        )
        ctx.on_change(
            "services.corr2_ok",
            1 if rc == 0 else 0,
            kind="service_state",
            severity="warn",
            subject="casm-corr2 ssh",
            detail={"stderr": err.strip()[:200]} if rc != 0 else None,
        )


class ZapdosCollector(Collector):
    """A single reachability probe of zapdos, at most once an hour."""

    name = "zapdos"
    default_cadence_s = 3600.0
    timeout_s = 30.0

    def collect(self, ctx: CollectorContext) -> None:
        now = time.time()
        last: Any = ctx.store.get_watermark("zapdos", "last_probe_ts")
        min_interval = float(ctx.settings.zapdos_min_interval_s)
        if last is not None and now - float(last) < min_interval:
            # Hard rate limit: survives restarts, so nothing can poll zapdos
            # more often than once per hour.
            ctx.scalar("services.zapdos_age_s", round(now - float(last), 1))
            return
        ctx.store.set_watermark("zapdos", "last_probe_ts", now)
        rc, _out, err = run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", ctx.settings.zapdos_ssh, "true"],
            timeout=25.0,
        )
        ok = 1 if rc == 0 else 0
        ctx.on_change(
            "services.zapdos_ok",
            ok,
            kind="service_state",
            severity="warn",
            subject="zapdos ssh",
            detail={"stderr": err.strip()[:200]} if rc != 0 else None,
        )
        if ok:
            ctx.store.set_watermark("zapdos", "last_ok_ts", now)
        last_ok = ctx.store.get_watermark("zapdos", "last_ok_ts")
        if last_ok is not None:
            ctx.scalar("services.zapdos_last_ok_age_s", round(now - float(last_ok), 1))
        ctx.scalar("services.zapdos_age_s", 0.0)
