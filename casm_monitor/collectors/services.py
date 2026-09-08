"""Liveness of the services the array depends on.

Strictly read-only probes:

* redis — ``PING`` only, with the password read from medusa.cfg at runtime. No
  key is ever read, written or expired.
* kafka — a group-less ``KafkaConsumer`` (``group_id=None``): bootstrap
  metadata plus ``end_offsets`` for the configured topics. It never subscribes
  and never commits, so the broker keeps no state about us. ``kafka_ok`` is 1
  only when end offsets were obtained for *every* configured topic; each topic
  keeps its own offset scalar and its own ``kafka_advancing`` flag (a stalled
  subband producer is a per-topic fact, not a summed one), and the first topic
  that stalls is named in ``services.kafka_stalled_topic``.
* t2d — ``pgrep`` plus a check that port 12345 is *listening* (parsed out of
  ``ss -ltn``). The t2d sockets belong to t2d: we never connect to them.
* zapdos — one ``ssh -o BatchMode=yes -o ConnectTimeout=5 zapdos true`` at most
  once per hour. The hour is not negotiable: the effective interval is
  ``max(config, 3600)`` in code, and the slot is taken with one atomic
  compare-and-set on the persisted watermark, so neither a mis-edited config nor
  two collector processes (a restart overlapping the old one) can double-probe.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..config import medusa_endpoints
from ..store import Store
from ..util import run
from .base import Collector, CollectorContext

T2D_PORT = 12345

# Floor for the zapdos probe interval; the config can only make it longer.
ZAPDOS_MIN_INTERVAL_S = 3600.0


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
        topics = list(settings.kafka_topics)
        offsets: dict[str, int] = {}  # only topics whose end offsets we really got
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
                for topic in topics:
                    parts = consumer.partitions_for_topic(topic) or set()
                    tps = [TopicPartition(topic, p) for p in sorted(parts)]
                    ctx.scalar("kafka.partitions", len(tps), tags={"topic": topic})
                    if not tps:
                        # No metadata for this topic: an unknown offset, not 0.
                        continue
                    ends = consumer.end_offsets(tps)
                    if not ends:
                        continue
                    offsets[topic] = int(sum(ends.values()))
            finally:
                consumer.close(autocommit=False)
        except Exception as exc:
            ctx.scalar("services.kafka_error", str(exc)[:200])

        # Healthy means: every configured topic answered. A partial answer is
        # not "kafka is up", it is "we do not know about topic X".
        ok = 1 if topics and len(offsets) == len(topics) else 0
        missing = [t for t in topics if t not in offsets]
        ctx.scalar("services.kafka_topics_ok", len(offsets))
        ctx.scalar("services.kafka_topics_missing", ",".join(missing) if missing else "")

        stalled: list[str] = []
        for topic in topics:
            if topic not in offsets:
                continue
            now_offset = offsets[topic]
            previous = ctx.store.get_watermark("kafka", f"end_offset:{topic}")
            # The first observation of a topic cannot be a stall; only a second
            # poll that sees the same offset means that producer stopped.
            advancing = 1 if previous is None or now_offset > int(previous) else 0
            ctx.store.set_watermark("kafka", f"end_offset:{topic}", now_offset)
            ctx.scalar("kafka.end_offset", now_offset, tags={"topic": topic})
            ctx.scalar("kafka.advancing", advancing, tags={"topic": topic})
            # Change detection needs a per-topic name of its own: on_change
            # compares the last row of one scalar name and ignores tags.
            ctx.on_change(
                f"kafka.{topic}.advancing",
                advancing,
                kind="kafka_stalled",
                severity="warn",
                subject=topic,
            )
            if not advancing:
                stalled.append(topic)

        if offsets:
            ctx.scalar("kafka.end_offset_total", sum(offsets.values()))
        ctx.on_change(
            "services.kafka_ok", ok, kind="service_state", severity="warn", subject="kafka"
        )
        # The strip needs one number plus the name of the first offender.
        ctx.on_change(
            "services.kafka_advancing",
            0 if stalled else 1,
            kind="kafka_stalled",
            severity="warn",
            subject="kafka offsets",
            detail={"stalled_topics": stalled} if stalled else None,
        )
        ctx.scalar("services.kafka_stalled_topic", stalled[0] if stalled else "")

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


def zapdos_interval_s(settings: Any) -> float:
    """The effective probe interval: the config can only make it longer."""
    return max(float(getattr(settings, "zapdos_min_interval_s", ZAPDOS_MIN_INTERVAL_S)),
               ZAPDOS_MIN_INTERVAL_S)


def claim_probe_slot(store: Store, stream: str, key: str, now: float, min_interval: float) -> bool:
    """Take the probe slot with one atomic compare-and-set on the watermark.

    A single ``INSERT ... ON CONFLICT DO UPDATE ... WHERE`` statement is one
    SQLite transaction, and SQLite serialises writers across processes, so of
    two collector instances racing on the same store exactly one gets the slot
    (``rowcount == 1``); the loser sees 0 rows and does not probe.
    """
    cur = store.execute(
        "INSERT INTO watermarks (stream, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(stream, key) DO UPDATE SET value = excluded.value "
        "WHERE CAST(watermarks.value AS REAL) <= ?",
        (stream, key, json.dumps(now), now - float(min_interval)),
    )
    return cur.rowcount == 1


class ZapdosCollector(Collector):
    """A single reachability probe of zapdos, at most once an hour."""

    name = "zapdos"
    default_cadence_s = 3600.0
    timeout_s = 30.0

    def collect(self, ctx: CollectorContext) -> None:
        now = time.time()
        min_interval = zapdos_interval_s(ctx.settings)
        if not claim_probe_slot(ctx.store, "zapdos", "last_probe_ts", now, min_interval):
            # Somebody (this process an hour ago, or a concurrent instance a
            # millisecond ago) holds the slot. Report the age and do nothing.
            last = ctx.store.get_watermark("zapdos", "last_probe_ts")
            if last is not None:
                ctx.scalar("services.zapdos_age_s", round(now - float(last), 1))
            return
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
