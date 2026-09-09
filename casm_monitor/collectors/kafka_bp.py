"""The correlator-side bandpass collector: Kafka -> frames -> store.

Contract with the broker (hard rules, plan "Kafka: CLIENT ONLY"):

* ``group_id=None`` and an explicit ``assign()`` of partition 0 of each topic.
  Never ``subscribe()``, never a commit, ``enable_auto_commit=False`` — the
  broker keeps no state about us and other consumers are unaffected.
* the read position lives in OUR SQLite ``watermarks`` table
  (``stream='kafka_bp'``, ``key='offset:<topic>'``). On a first start we begin
  at ``end_offset - BACKFILL_MESSAGES`` (about ten minutes), never at the
  beginning of the broker's retention.
* a broker outage is not an error state to crash on: the consumer is closed,
  ``kafka_bp.ok`` goes to 0 and reconnection is retried with a capped
  exponential backoff.

Message layout, measured live on 2026-09-08 (``docs/notes/kafka-bandpass-schema.md``):
every record carries binary headers — ``offset`` (int32, the first channel of
the 512-channel subband this producer owns, 512 x stream), ``nsig``, ``npol``,
``nchan`` (int32), ``freq``/``bw`` (float64) and ``timestamp`` (int64 seconds,
identical across the six producers of one frame). The body is
``nsig x npol x nchan`` float32. Nothing is hardcoded: the shape comes from the
headers of each record.

Frame assembly is keyed on the ``timestamp`` HEADER, not on the Kafka
CreateTime: the six producers publish the same frame 28 s (corr1) to 96 s
(corr2) after its timestamp, so rounding CreateTime to the 10 s cadence would
scatter one frame across seven buckets. A record WITHOUT that header is dropped
and counted (``kafka_bp.dropped_no_timestamp``); there is no CreateTime
fallback, because a fallback silently invents a frame identity that the other
five producers will not share.

A frame is emitted when all six subbands have arrived or ``FRAME_TIMEOUT_S``
after its FIRST record, with the missing subbands left at zero and flagged in
``subbands_ok``. That timeout must exceed the producer skew (measured 2026-09-08
at ~68 s between the corr1 and corr2 producers of one frame), hence the 150 s
default and the ``kafka.frame_timeout_s`` config key. Records are deduplicated
by ``(timestamp, offset)``: a second copy of a subband already in the frame, or
any record for a frame that has already been emitted, is dropped and counted.
``_latest`` only ever moves forward in frame timestamp, so a late straggler
cannot make the live layer go backwards.

Durability (plan: "readers only see committed shards"): the Kafka read
watermark is advanced only past records whose frames are already in a committed
shard. Everything still sitting in the sub/full buffers holds the watermark
back, so a crash replays those records instead of losing them, and the replay is
idempotent because :meth:`ShardWriter.write` skips a shard whose ``(stream, t0)``
is already committed. A flush that fails keeps its buffer and retries on the
next tick; after ``MAX_FLUSH_FAILURES`` attempts the buffer is dropped with an
``error`` event rather than growing without bound.
"""

from __future__ import annotations

import logging
import os
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from ..config import Settings
from ..store import ShardReader
from . import rowmap
from .base import Collector, CollectorContext

log = logging.getLogger("casm_monitor.collect.kafka_bp")

TOPIC_BP = "casm_antenna_bp"
NCHAN = rowmap.NCHAN
SUBBAND_NCHAN = 512
N_SUBBANDS = NCHAN // SUBBAND_NCHAN

FRAME_CADENCE_S = 10.0
# Producer skew measured on the live broker (docs/notes/kafka-bandpass-schema.md):
# corr1's streams publish ~28 s after the frame timestamp, corr2's ~96 s, so a
# frame is not complete for ~68 s. 150 s leaves better than 2x headroom; the
# config key ``kafka.frame_timeout_s`` can raise it further.
FRAME_TIMEOUT_S = 150.0
# The only offsets the six deployed producers use (512 x stream).
EXPECTED_OFFSETS = tuple(SUBBAND_NCHAN * i for i in range(N_SUBBANDS))
MAX_FLUSH_FAILURES = 3
# How many emitted frame timestamps to remember for the late-record check;
# 4096 is > 11 h at the 10 s cadence, far beyond any producer's lateness.
EMITTED_MEMORY = 4096
# ~10 minutes of history on a first start: six producers x one message per
# frame at the 10 s cadence.
BACKFILL_MESSAGES = int(600.0 / FRAME_CADENCE_S) * N_SUBBANDS

STREAM_FULL = "kafka_bp_full"
STREAM_SUB = "kafka_bp_sub"
STREAM_NIGHT = "kafka_bp_night"

SUB_CHAN_AVG = 32  # 3072 / 32 = 96 channels
SUB_NCHAN = NCHAN // SUB_CHAN_AVG
DB_FLOOR = 1e-6

FULL_FLUSH_S = 3600.0  # hourly shards for the 60 s full-resolution stream
SUB_FLUSH_S = 600.0  # the 10 s stream is flushed more often so less is at risk
SCALAR_INTERVAL_S = 10.0
TOPIC_SCALAR_INTERVAL_S = 30.0
POWER_SCALAR_INTERVAL_S = 60.0
ROW_MAP_MAX_AGE_S = 86400.0
BACKOFF_MIN_S = 2.0
BACKOFF_MAX_S = 60.0

NIGHT_TZ = ZoneInfo("America/Los_Angeles")
NIGHT_START_H = 2
NIGHT_END_H = 5

_HEADER_FORMATS = {
    "offset": "<i",
    "nsig": "<i",
    "npol": "<i",
    "ndim": "<i",
    "nbin": "<i",
    "ndat": "<i",
    "nchan": "<i",
    "bw": "<d",
    "out_bw": "<d",
    "freq": "<d",
    "tsamp": "<d",
    "timestamp": "<q",
}


def _min_offset(values: Sequence[int | None]) -> int | None:
    """Smallest non-None offset, or None when there is nothing pending."""
    known = [int(v) for v in values if v is not None]
    return min(known) if known else None


def latest_frame_path(settings: Settings) -> Path:
    """Where the newest assembled frame is mirrored for the web process."""
    return Path(settings.store_root) / "latest" / "kafka_bp_frame.npz"


def to_db(power: np.ndarray) -> np.ndarray:
    """10log10 with a floor, so a dead (zero) input plots as a number."""
    return (10.0 * np.log10(np.maximum(np.asarray(power, dtype=np.float64), DB_FLOOR))).astype(
        np.float32
    )


def decode_headers(headers: Sequence[tuple[str, bytes]] | None) -> dict[str, Any]:
    """Kafka record headers -> python scalars (unknown keys are kept raw)."""
    out: dict[str, Any] = {}
    for key, value in headers or ():
        fmt = _HEADER_FORMATS.get(key)
        if fmt is None or value is None or len(value) != struct.calcsize(fmt):
            out[key] = value
            continue
        out[key] = struct.unpack(fmt, value)[0]
    return out


def decode_bp(body: bytes, headers: dict[str, Any]) -> np.ndarray:
    """One ``casm_antenna_bp`` body as ``(nsig * npol, nchan)`` float32.

    Strict on purpose: the row count comes from the headers and the body must
    be exactly ``nsig * npol * nchan`` float32 of a 512-channel subband. A body
    that does not match its headers is a record we do not understand, and
    reshaping it on a guess would silently mis-assign every input's bandpass,
    so it is refused (the caller drops and counts it).
    """
    flat = np.frombuffer(body, dtype="<f4")
    nchan = int(headers.get("nchan") or 0)
    nsig = int(headers.get("nsig") or 0)
    npol = int(headers.get("npol") or 0)
    if nchan != SUBBAND_NCHAN:
        raise ValueError(f"bp record has nchan={nchan}, expected {SUBBAND_NCHAN}")
    nrow = nsig * npol
    if nsig <= 0 or npol <= 0 or nrow * nchan != flat.size:
        raise ValueError(
            f"bp body of {flat.size} floats does not match headers "
            f"nsig={nsig} npol={npol} nchan={nchan}"
        )
    return flat.reshape(nrow, nchan)


@dataclass
class Frame:
    """One 10 s bandpass frame under assembly."""

    ts: float
    first_seen: float
    nrow: int = 0
    data: np.ndarray | None = None
    subbands_ok: list[bool] = field(default_factory=lambda: [False] * N_SUBBANDS)
    # Lowest Kafka offset that contributed to this frame: the read watermark
    # may not pass it until the frame is in a committed shard.
    offset_min: int | None = None

    def note_offset(self, offset: int) -> None:
        self.offset_min = offset if self.offset_min is None else min(self.offset_min, offset)

    def add(self, subband: int, block: np.ndarray) -> None:
        if not 0 <= subband < N_SUBBANDS:
            raise ValueError(f"subband {subband} out of range")
        if self.data is None:
            self.nrow = block.shape[0]
            self.data = np.zeros((self.nrow, NCHAN), dtype=np.float32)
        if block.shape[0] != self.nrow:
            raise ValueError(f"row count changed mid-frame: {block.shape[0]} != {self.nrow}")
        lo = subband * SUBBAND_NCHAN
        self.data[:, lo : lo + block.shape[1]] = block
        self.subbands_ok[subband] = True

    @property
    def complete(self) -> bool:
        return all(self.subbands_ok)


@dataclass
class AssembledFrame:
    """A frame handed on to the store: only the populated rows are kept."""

    ts: float
    rows: list[int]
    power: np.ndarray  # (n_rows, NCHAN) linear
    subbands_ok: list[bool]
    offset_min: int | None = None


@dataclass
class BufferedSample:
    """One frame waiting in a buffer for its shard, with its offset provenance."""

    ts: float
    values: np.ndarray  # dB
    subbands_ok: list[bool]
    rows: list[int]
    offset_min: int | None = None


class KafkaBandpassCollector(Collector):
    """Long-running group-less consumer of the three antenna-stat topics.

    Runs inside the ordinary collector runner: ``collect()`` is one poll of at
    most a second, so the runner's failure isolation, overlap guard and
    heartbeat all apply unchanged. All the state (consumer, partial frames,
    shard buffers) lives on the instance between calls.
    """

    name = "kafka_bp"
    default_cadence_s = 1.0
    # Generous: one call can also derive the row map (a visibility file read)
    # or the nightly median. The runner's overlap guard stops them piling up.
    timeout_s = 300.0

    def __init__(self, settings: Settings, cadence_s: float | None = None) -> None:
        super().__init__(settings, cadence_s)
        self._consumer: Any = None
        self._topics: list[str] = list(settings.kafka_topics)
        self._frames: dict[float, Frame] = {}
        self._rows: list[int] = []
        self._latest: AssembledFrame | None = None
        self._full_buf: dict[float, BufferedSample] = {}
        self._sub_buf: list[BufferedSample] = []
        self._full_flushed = 0.0
        self._sub_flushed = 0.0
        self._last_scalar = 0.0
        self._last_power_scalar = 0.0
        self._last_topic_scalar = 0.0
        self._topic_seen: dict[str, tuple[int, float]] = {}
        self._backoff_until = 0.0
        self._backoff_s = BACKOFF_MIN_S
        self._ok: int | None = None
        self._frame_timeout_s = max(
            float(getattr(settings, "kafka_frame_timeout_s", FRAME_TIMEOUT_S)), FRAME_CADENCE_S
        )
        # Frames already handed to the store: a record arriving for one of
        # these is a late duplicate and is dropped, never re-emitted.
        self._emitted: OrderedDict[float, None] = OrderedDict()
        # Records seen but not usable, by reason, reported as scalars.
        self._dropped: dict[str, int] = {}
        # Read position bookkeeping: the highest offset consumed per topic, and
        # the highest offset already persisted (what the watermark may show).
        self._offset_seen: dict[str, int] = {}
        self._offset_committed: dict[str, int] = {}
        self._flush_failures: dict[str, int] = {}

    # -- collector entry point -----------------------------------------
    def collect(self, ctx: CollectorContext) -> None:
        now = time.time()
        if self._consumer is None:
            if now < self._backoff_until:
                return
            if not self._connect(ctx):
                return
        try:
            batch = self._consumer.poll(timeout_ms=int(1000 * min(1.0, self.cadence_s)))
        except Exception as exc:
            self._drop_consumer(ctx, f"poll failed: {exc}")
            return
        for tp, records in (batch or {}).items():
            self._handle_records(ctx, tp.topic, records)
        self._flush_expired_frames(ctx)
        self._maybe_flush_shards(ctx, time.time())
        self._advance_watermarks(ctx)
        self._report(ctx, time.time())
        self._maybe_row_map(ctx)
        self._maybe_night_median(ctx)

    def close(self, ctx: CollectorContext) -> None:
        """Flush whatever is buffered, then drop the consumer.

        Called once by :class:`~casm_monitor.collectors.runner.CollectorRunner`
        on SIGTERM/stop, before the store closes: without this a service
        restart loses up to ``SUB_FLUSH_S``/``FULL_FLUSH_S`` of unwritten
        samples (they only otherwise reach a shard on the periodic flush
        inside ``collect()``, per ``_maybe_flush_shards``)."""
        if self._sub_buf:
            try:
                self._flush_sub(ctx)
            except Exception:  # pragma: no cover - best effort
                log.exception("kafka_bp: flush of the sub buffer failed on close")
        if self._full_buf:
            try:
                self._flush_full(ctx)
            except Exception:  # pragma: no cover - best effort
                log.exception("kafka_bp: flush of the full buffer failed on close")
        # Whatever became durable above may now let the read position move on;
        # whatever did not, holds it back and is replayed on the next start.
        try:
            self._advance_watermarks(ctx)
        except Exception:  # pragma: no cover - best effort
            log.exception("kafka_bp: could not persist the read position on close")
        if self._consumer is not None:
            try:
                self._consumer.close(autocommit=False)
            except Exception:  # pragma: no cover - best effort
                log.debug("consumer close failed", exc_info=True)
            self._consumer = None

    # -- broker ---------------------------------------------------------
    def _connect(self, ctx: CollectorContext) -> bool:
        settings = ctx.settings
        try:
            from kafka import KafkaConsumer, TopicPartition

            consumer = KafkaConsumer(
                bootstrap_servers=settings.kafka_bootstrap,
                group_id=None,  # group-less: no broker-side state, no commits
                enable_auto_commit=False,
                request_timeout_ms=15000,
                fetch_max_bytes=64 * 1024 * 1024,
                max_partition_fetch_bytes=16 * 1024 * 1024,
            )
            # Manual assignment of partition 0 only; subscribe() is never called.
            partitions = [TopicPartition(topic, 0) for topic in self._topics]
            consumer.assign(partitions)
            ends = consumer.end_offsets(partitions)
            begins = consumer.beginning_offsets(partitions)
            for tp in partitions:
                consumer.seek(tp, self._start_offset(ctx, tp.topic, begins[tp], ends[tp]))
        except Exception as exc:
            self._drop_consumer(ctx, f"connect failed: {exc}")
            return False
        self._consumer = consumer
        self._backoff_s = BACKOFF_MIN_S
        log.info("kafka_bp consumer assigned %s", ", ".join(self._topics))
        return True

    def _start_offset(self, ctx: CollectorContext, topic: str, begin: int, end: int) -> int:
        """Our own watermark if it is still in retention, else ~10 min back."""
        stored = ctx.store.get_watermark("kafka_bp", f"offset:{topic}")
        if stored is not None:
            wanted = int(stored) + 1
            if begin <= wanted <= end:
                return wanted
            log.warning(
                "kafka_bp watermark %s for %s is outside [%d, %d]; restarting near the end",
                stored, topic, begin, end,
            )
        return max(begin, end - BACKFILL_MESSAGES)

    def _drop_consumer(self, ctx: CollectorContext, message: str) -> None:
        log.warning("kafka_bp: %s", message)
        if self._consumer is not None:
            try:
                self._consumer.close(autocommit=False)
            except Exception:  # pragma: no cover - the socket is already gone
                log.debug("consumer close failed", exc_info=True)
            self._consumer = None
        self._backoff_until = time.time() + self._backoff_s
        self._backoff_s = min(BACKOFF_MAX_S, self._backoff_s * 2)
        ctx.scalar("kafka_bp.error", message[:200])
        self._set_ok(ctx, 0, detail={"error": message[:200]})

    def _set_ok(self, ctx: CollectorContext, value: int, detail: dict[str, Any] | None = None) -> None:
        if value == self._ok:
            ctx.scalar("kafka_bp.ok", value)
            return
        self._ok = value
        ctx.on_change(
            "kafka_bp.ok",
            value,
            kind="service_state",
            severity="warn",
            subject="kafka bandpass consumer",
            detail=detail,
        )

    # -- records --------------------------------------------------------
    def _drop(self, reason: str, offset: Any = None, detail: str = "") -> None:
        """Count a record we refuse to use (reported as a scalar per reason)."""
        self._dropped[reason] = self._dropped.get(reason, 0) + 1
        log.debug("kafka_bp: dropped record at offset %s (%s) %s", offset, reason, detail)

    def _handle_records(self, ctx: CollectorContext, topic: str, records: Sequence[Any]) -> None:
        if not records:
            return
        last = records[-1]
        self._topic_seen[topic] = (int(last.offset), float(last.timestamp) / 1e3)
        for record in records:
            if topic == TOPIC_BP:
                try:
                    self._ingest_bp(ctx, record)
                except Exception as exc:
                    self._drop("undecodable", record.offset, str(exc))
                    log.warning("kafka_bp: undecodable record at offset %s: %s", record.offset, exc)
            # casm_antenna_ts / _hg are decoded minimally for now: their
            # liveness (offset + record timestamp) is all M1 needs, and their
            # payload layouts get their own milestone.
        self._offset_seen[topic] = int(last.offset)

    def _ingest_bp(self, ctx: CollectorContext, record: Any) -> None:
        headers = decode_headers(record.headers)
        offset = headers.get("offset")
        if not isinstance(offset, int) or offset not in EXPECTED_OFFSETS:
            raise ValueError(f"record has offset header {offset!r}, expected one of {EXPECTED_OFFSETS}")
        subband = int(offset) // SUBBAND_NCHAN
        # The header timestamp is the frame identity. There is deliberately no
        # CreateTime fallback: CreateTime trails the frame by 28-96 s depending
        # on the producer, so it would scatter one frame across several keys.
        raw_ts = headers.get("timestamp")
        if not isinstance(raw_ts, int):
            self._drop("no_timestamp", record.offset)
            return
        frame_ts = float(raw_ts)
        block = decode_bp(record.value, headers)
        if frame_ts in self._emitted:
            # The frame has already gone to the store; re-opening it would
            # publish the same 10 s twice.
            self._drop("late_record", record.offset, f"frame {frame_ts:.0f}")
            return
        frame = self._frames.get(frame_ts)
        if frame is None:
            frame = Frame(ts=frame_ts, first_seen=time.time())
            self._frames[frame_ts] = frame
        if frame.subbands_ok[subband]:
            self._drop("duplicate_subband", record.offset, f"frame {frame_ts:.0f} sb {subband}")
            return
        frame.note_offset(int(record.offset))
        frame.add(subband, block)
        if frame.complete:
            self._frames.pop(frame_ts, None)
            self._emit_frame(ctx, frame)

    def _flush_expired_frames(self, ctx: CollectorContext) -> None:
        now = time.time()
        timeout = self._frame_timeout_s
        for ts in sorted(k for k, f in self._frames.items() if now - f.first_seen >= timeout):
            frame = self._frames.pop(ts)
            missing = [i for i, ok in enumerate(frame.subbands_ok) if not ok]
            log.info("kafka_bp frame %.0f incomplete after %.0fs, missing subbands %s",
                     ts, timeout, missing)
            self._emit_frame(ctx, frame)

    def _note_emitted(self, ts: float) -> None:
        self._emitted[ts] = None
        while len(self._emitted) > EMITTED_MEMORY:
            self._emitted.popitem(last=False)

    # -- frame -> store -------------------------------------------------
    def _emit_frame(self, ctx: CollectorContext, frame: Frame) -> None:
        if frame.data is None:
            return
        rows = self._row_set(frame)
        if not rows:
            return
        assembled = AssembledFrame(
            ts=frame.ts,
            rows=rows,
            power=frame.data[rows].astype(np.float32, copy=True),
            subbands_ok=list(frame.subbands_ok),
            offset_min=frame.offset_min,
        )
        self._note_emitted(frame.ts)
        # The live layer only moves forward: a straggler frame that completed
        # after a newer one must not become "the latest bandpass".
        if self._latest is None or assembled.ts >= self._latest.ts:
            self._latest = assembled
            self._write_latest(ctx, assembled)
        self._buffer(ctx, assembled)

    def _row_set(self, frame: Frame) -> list[int]:
        """Populated rows, sticky across frames with a missing subband.

        A row set that shrank only because one producer was late would change
        the shard shape for one sample and make the history ragged, so an
        incomplete frame keeps the last known set.
        """
        assert frame.data is not None
        populated = [int(r) for r in np.flatnonzero(frame.data.any(axis=1))]
        if not frame.complete and self._rows:
            return list(self._rows)
        if populated and populated != self._rows:
            self._rows = populated
        return list(self._rows or populated)

    def _write_latest(self, ctx: CollectorContext, frame: AssembledFrame) -> None:
        """Mirror the newest frame for the web process (atomic replace)."""
        path = latest_frame_path(ctx.settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp-{os.getpid()}.npz")
        try:
            np.savez(
                tmp,
                ts=np.float64(frame.ts),
                written=np.float64(time.time()),
                rows=np.asarray(frame.rows, dtype=np.int32),
                subbands_ok=np.asarray(frame.subbands_ok, dtype=bool),
                power_db=to_db(frame.power),
            )
            os.replace(tmp, path)
        except Exception:
            log.exception("kafka_bp: could not write the latest-frame mirror")
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def _buffer(self, ctx: CollectorContext, frame: AssembledFrame) -> None:
        reduced = frame.power.reshape(frame.power.shape[0], SUB_NCHAN, SUB_CHAN_AVG).mean(axis=2)
        self._sub_buf.append(
            BufferedSample(
                ts=frame.ts,
                values=to_db(reduced),
                subbands_ok=list(frame.subbands_ok),
                rows=list(frame.rows),
                offset_min=frame.offset_min,
            )
        )
        # The full-resolution stream keeps one frame per minute: the one whose
        # timestamp is nearest the minute boundary.
        minute = round(frame.ts / 60.0) * 60.0
        best = self._full_buf.get(minute)
        if best is None or abs(frame.ts - minute) < abs(best.ts - minute):
            keep_offset = frame.offset_min if best is None else _min_offset(
                [frame.offset_min, best.offset_min]
            )
            self._full_buf[minute] = BufferedSample(
                ts=frame.ts,
                values=to_db(frame.power),
                subbands_ok=list(frame.subbands_ok),
                rows=list(frame.rows),
                # Keep the LOWEST offset of the frames this minute has seen:
                # dropping the loser's offset would let the watermark run past
                # a record whose sample is not in any shard.
                offset_min=keep_offset,
            )
        elif best is not None:
            best.offset_min = _min_offset([best.offset_min, frame.offset_min])
        now = time.time()
        if not self._full_flushed:
            self._full_flushed = now
        if not self._sub_flushed:
            self._sub_flushed = now

    def _maybe_flush_shards(self, ctx: CollectorContext, now: float) -> None:
        if self._sub_buf and now - self._sub_flushed >= SUB_FLUSH_S:
            self._flush_sub(ctx)
        if self._full_buf and now - self._full_flushed >= FULL_FLUSH_S:
            self._flush_full(ctx)

    def _flush_sub(self, ctx: CollectorContext) -> None:
        """Write the 10 s buffer; the buffer is only emptied once it is durable."""
        samples = sorted(self._sub_buf, key=lambda item: item.ts)
        if not samples:
            self._sub_flushed = time.time()
            return
        self._sub_flushed = time.time()
        if self._write_stream(ctx, STREAM_SUB, samples, chan_avg=SUB_CHAN_AVG):
            self._sub_buf = [s for s in self._sub_buf if s not in samples]
            self._flush_failures[STREAM_SUB] = 0
        elif self._note_flush_failure(ctx, STREAM_SUB):
            self._sub_buf = [s for s in self._sub_buf if s not in samples]

    def _flush_full(self, ctx: CollectorContext) -> None:
        """Write the 60 s buffer; same keep-on-failure rule as the 10 s one."""
        keys = sorted(self._full_buf)
        samples = [self._full_buf[k] for k in keys]
        if not samples:
            self._full_flushed = time.time()
            return
        self._full_flushed = time.time()
        if self._write_stream(ctx, STREAM_FULL, samples, chan_avg=1):
            for key in keys:
                if self._full_buf.get(key) in samples:
                    self._full_buf.pop(key, None)
            self._flush_failures[STREAM_FULL] = 0
        elif self._note_flush_failure(ctx, STREAM_FULL):
            for key in keys:
                self._full_buf.pop(key, None)

    def _note_flush_failure(self, ctx: CollectorContext, stream: str) -> bool:
        """Count a failed flush; True once the buffer must be dropped.

        Keeping a buffer forever after a persistent store failure would grow
        without bound and hold the Kafka watermark back indefinitely, so after
        ``MAX_FLUSH_FAILURES`` attempts the samples are given up on loudly.
        """
        n = self._flush_failures.get(stream, 0) + 1
        self._flush_failures[stream] = n
        ctx.scalar("kafka_bp.flush_failures", n, tags={"stream": stream})
        if n < MAX_FLUSH_FAILURES:
            log.warning("kafka_bp: flush of %s failed (%d/%d), keeping the buffer",
                        stream, n, MAX_FLUSH_FAILURES)
            return False
        self._flush_failures[stream] = 0
        ctx.event(
            "kafka_bp_flush_failed",
            severity="error",
            subject=f"kafka bandpass {stream}",
            detail={"stream": stream, "attempts": n, "action": "buffer dropped"},
        )
        log.error("kafka_bp: flush of %s failed %d times; dropping the buffer", stream, n)
        return True

    def _write_stream(
        self,
        ctx: CollectorContext,
        stream: str,
        samples: list[BufferedSample],
        *,
        chan_avg: int,
    ) -> bool:
        """Write one shard per row set. Returns False if any write failed."""
        if not samples:
            return True
        samples = self._not_yet_committed(ctx, stream, samples)
        if not samples:
            return True
        # One shard per row set: the array shape must be constant inside a
        # shard, and the meta must describe the rows those samples really had.
        row_sets = {tuple(s.rows) for s in samples}
        ok = True
        for rows in sorted(row_sets):
            group = [s for s in samples if tuple(s.rows) == rows]
            array = np.stack([s.values for s in group]).astype(np.float32)
            times = [float(s.ts) for s in group]
            meta = {
                "rows": list(rows),
                "t": times,
                "subbands_ok": [[bool(v) for v in s.subbands_ok] for s in group],
                "units": "dB",
                "chan_avg": chan_avg,
                "freq_top_mhz": rowmap.FREQ_TOP_MHZ,
                "chan_bw_mhz": rowmap.CHAN_BW_MHZ * chan_avg,
                "mapping": {
                    str(k): v for k, v in rowmap.assignment(rowmap.current_mapping(ctx.store)).items()
                },
            }
            try:
                # Idempotent on replay: ShardWriter skips a (stream, t0) that
                # is already committed, so re-reading from the watermark after
                # a crash cannot duplicate history.
                ctx.shards.write(stream, array, t0=times[0], t1=times[-1], meta=meta)
            except Exception:
                ok = False
                log.exception("kafka_bp: shard write failed for %s", stream)
        return ok

    def _not_yet_committed(
        self, ctx: CollectorContext, stream: str, samples: list[BufferedSample]
    ) -> list[BufferedSample]:
        """Drop samples whose time is already inside a committed shard.

        The ``(stream, t0)`` skip in :meth:`ShardWriter.write` only catches a
        replay that happens to flush on the same boundary. A restart replays
        from the watermark with a different buffer alignment, so without this
        the same 10 s frames would be published again in a shard with a new
        ``t0`` and every history reader would see them twice.
        """
        rows = ctx.store.query("SELECT MAX(t1) AS t FROM shards WHERE stream = ?", (stream,))
        newest = rows[0]["t"] if rows else None
        if newest is None:
            return samples
        keep = [s for s in samples if s.ts > float(newest)]
        if len(keep) != len(samples):
            log.info(
                "kafka_bp: %d %s sample(s) at or before the newest committed shard "
                "(t=%.0f) are already published; not rewriting them",
                len(samples) - len(keep), stream, float(newest),
            )
        return keep

    # -- read position --------------------------------------------------
    def _advance_watermarks(self, ctx: CollectorContext) -> None:
        """Persist the read position, never past a record that is not durable.

        For ``casm_antenna_bp`` the safe position is one below the lowest
        offset still held in a buffer or in a partially assembled frame; for
        the other topics nothing is buffered, so everything consumed is safe.
        """
        pending = _min_offset(
            [s.offset_min for s in self._sub_buf]
            + [s.offset_min for s in self._full_buf.values()]
            + [f.offset_min for f in self._frames.values()]
        )
        for topic, seen in sorted(self._offset_seen.items()):
            safe = seen
            if topic == TOPIC_BP and pending is not None:
                safe = min(seen, pending - 1)
            if safe < 0 or safe <= self._offset_committed.get(topic, -1):
                continue
            ctx.store.set_watermark("kafka_bp", f"offset:{topic}", int(safe))
            self._offset_committed[topic] = int(safe)

    # -- scalars --------------------------------------------------------
    def _report(self, ctx: CollectorContext, now: float) -> None:
        if now - self._last_topic_scalar >= TOPIC_SCALAR_INTERVAL_S and self._topic_seen:
            self._last_topic_scalar = now
            for topic, (offset, ts) in sorted(self._topic_seen.items()):
                ctx.scalar("kafka_bp.topic_offset", offset, tags={"topic": topic})
                ctx.scalar("kafka_bp.topic_age_s", round(now - ts, 1), tags={"topic": topic})
        frame = self._latest
        if frame is None or now - self._last_scalar < SCALAR_INTERVAL_S:
            return
        self._last_scalar = now
        for reason, count in sorted(self._dropped.items()):
            ctx.scalar("kafka_bp.dropped", count, tags={"reason": reason})
        self._set_ok(ctx, 1)
        ctx.scalar("kafka_bp.subbands_ok", int(sum(frame.subbands_ok)))
        ctx.scalar("kafka_bp.frame_age_s", round(now - frame.ts, 1))
        ctx.scalar("kafka_bp.nrow", len(frame.rows))
        if now - self._last_power_scalar >= POWER_SCALAR_INTERVAL_S:
            self._last_power_scalar = now
            self._power_scalars(ctx, frame)

    def _power_scalars(self, ctx: CollectorContext, frame: AssembledFrame) -> None:
        mapping = rowmap.current_mapping(ctx.store)
        mask = rowmap.band_mask(rowmap.freq_axis_mhz())
        band = frame.power[:, mask]
        power_db = to_db(band.mean(axis=1))
        rows = []
        for i, row in enumerate(frame.rows):
            item = mapping.get(int(row)) or {}
            tags = {"row": int(row)}
            if item.get("packet_idx") is not None:
                tags["packet_idx"] = int(item["packet_idx"])
            rows.append((f"kafka_bp.row{int(row)}.power_db", round(float(power_db[i]), 3), tags))
        ctx.store.put_scalars(rows, ts=frame.ts)

    # -- row map --------------------------------------------------------
    def _maybe_row_map(self, ctx: CollectorContext) -> None:
        frame = self._latest
        if frame is None or not all(frame.subbands_ok):
            return
        now = time.time()
        last = ctx.store.get_watermark("kafka_bp", "row_map_ts")
        reason = None
        if last is None:
            reason = "first"
        elif now - float(last) >= ROW_MAP_MAX_AGE_S:
            reason = "daily"
        else:
            newest = self._newest_obs_restart(ctx)
            seen = ctx.store.get_watermark("kafka_bp", "row_map_obs_event")
            if newest is not None and (seen is None or int(newest) > int(seen)):
                reason = "obs_restart"
        if reason is None:
            return
        self._validate_row_map(ctx, frame, reason)

    @staticmethod
    def _newest_obs_restart(ctx: CollectorContext) -> int | None:
        rows = ctx.store.query(
            "SELECT id FROM events WHERE kind = 'obs_restart' ORDER BY id DESC LIMIT 1"
        )
        return int(rows[0]["id"]) if rows else None

    def _validate_row_map(self, ctx: CollectorContext, frame: AssembledFrame, reason: str) -> None:
        """Re-run the shape-correlation VALIDATION of the (unconditional)
        formula mapping and persist its verdict per row. Never changes which
        row a packet_idx maps to -- that is ``rowmap.formula_row``, fixed by
        the layout -- only whether it is flagged ``"mismatch"``."""
        before = rowmap.load_row_map(ctx.store)
        before_assignment = rowmap.assignment(rowmap.current_mapping(ctx.store))
        before_mismatched = {
            row for row, item in before.items() if item.get("status") == "mismatch"
        }
        try:
            mapping, source = rowmap.validate_row_map(
                frame.rows, frame.power, ctx.settings.vis_dir
            )
        except Exception as exc:
            log.warning("kafka_bp: row map validation failed (%s): %s", reason, exc)
            ctx.scalar("kafka_bp.row_map_error", str(exc)[:200])
            # Retry at the next cadence tick would hammer the vis disk; wait
            # the usual day unless an obs restart asks again. The existing
            # formula/validated statuses in the store are left untouched.
            ctx.store.set_watermark("kafka_bp", "row_map_ts", time.time())
            return
        source["reason"] = reason
        source["frame_ts"] = frame.ts
        rowmap.save_row_map(ctx.store, mapping, source, ts=time.time())
        # An input the layout no longer wires must not keep a stored row.
        purged = rowmap.purge_row_map(ctx.store)
        if purged:
            ctx.scalar("kafka_bp.row_map_purged", len(purged))
        ctx.store.set_watermark("kafka_bp", "row_map_ts", time.time())
        newest = self._newest_obs_restart(ctx)
        if newest is not None:
            ctx.store.set_watermark("kafka_bp", "row_map_obs_event", int(newest))
        after_assignment = rowmap.assignment(rowmap.current_mapping(ctx.store))
        verified = sum(1 for item in mapping.values() if item.get("status") == "formula+verified")
        mismatched = {row for row, item in mapping.items() if item.get("status") == "mismatch"}
        ctx.scalar("kafka_bp.row_map_verified", verified)
        ctx.scalar("kafka_bp.row_map_mismatch", len(mismatched))
        ctx.scalar("kafka_bp.row_map_error", "")
        log.info(
            "kafka_bp row map validation (%s): %d verified, %d mismatched of %d checked",
            reason, verified, len(mismatched), len(mapping),
        )
        # A layout change (which packet_idx a wired input's row belongs to)
        # is its own event; a validation disagreement is a different concern.
        if before_assignment and after_assignment != before_assignment:
            ctx.event(
                "kafka_row_map_changed",
                severity="warn",
                subject="kafka bandpass rows",
                detail={
                    "reason": reason,
                    "before": {str(k): v for k, v in sorted(before_assignment.items())},
                    "after": {str(k): v for k, v in sorted(after_assignment.items())},
                },
            )
        new_mismatches = mismatched - before_mismatched
        if new_mismatches:
            ctx.event(
                "kafka_row_map_mismatch",
                severity="warn",
                subject="kafka bandpass rows disagree with the formula mapping",
                detail={
                    "reason": reason,
                    "rows": sorted(new_mismatches),
                    "detail": {
                        str(row): mapping[row] for row in sorted(new_mismatches)
                    },
                },
            )

    # -- nightly median -------------------------------------------------
    def _maybe_night_median(self, ctx: CollectorContext) -> None:
        now_local = datetime.now(NIGHT_TZ)
        if now_local.hour < NIGHT_END_H:
            return
        date = now_local.date().isoformat()
        if str(ctx.store.get_watermark("kafka_bp", "night_median_date") or "") == date:
            return
        t0 = now_local.replace(hour=NIGHT_START_H, minute=0, second=0, microsecond=0).timestamp()
        t1 = now_local.replace(hour=NIGHT_END_H, minute=0, second=0, microsecond=0).timestamp()
        try:
            self._night_median(ctx, date, t0, t1)
        except Exception:
            log.exception("kafka_bp: night median failed for %s", date)
        # Recorded either way: one attempt per day, never a retry loop.
        ctx.store.set_watermark("kafka_bp", "night_median_date", date)

    def _night_median(self, ctx: CollectorContext, date: str, t0: float, t1: float) -> None:
        reader = ShardReader(ctx.store)
        shards = reader.list(STREAM_FULL, t0=t0, t1=t1)
        samples: list[np.ndarray] = []
        rows: list[int] = []
        for shard in shards:
            array, meta = reader.load(shard)
            times = np.asarray(meta.get("t") or [], dtype=np.float64)
            if times.size != array.shape[0]:
                times = np.linspace(shard["t0"], shard["t1"], array.shape[0])
            keep = (times >= t0) & (times <= t1)
            if not keep.any():
                continue
            shard_rows = [int(r) for r in (meta.get("rows") or [])]
            if rows and shard_rows != rows:
                log.warning("kafka_bp night median: row set changed within %s, using the first", date)
                continue
            rows = rows or shard_rows
            samples.append(array[keep])
        if not samples:
            log.info("kafka_bp: no full-resolution frames in the %s 02:00-05:00 PT window", date)
            return
        stacked = np.concatenate(samples, axis=0)
        median = np.median(stacked, axis=0).astype(np.float32)  # (n_rows, NCHAN) dB
        meta = {
            "rows": rows,
            "date": date,
            "window_local": [NIGHT_START_H, NIGHT_END_H],
            "n_samples": int(stacked.shape[0]),
            "units": "dB",
            "freq_top_mhz": rowmap.FREQ_TOP_MHZ,
            "chan_bw_mhz": rowmap.CHAN_BW_MHZ,
        }
        ctx.shards.write(STREAM_NIGHT, median, t0=t0, t1=t1, meta=meta)
        mapping = rowmap.current_mapping(ctx.store)
        mask = rowmap.band_mask(rowmap.freq_axis_mhz())
        band_median = np.median(median[:, mask], axis=1)
        scalars = []
        for i, row in enumerate(rows or list(range(median.shape[0]))):
            item = mapping.get(int(row)) or {}
            tags: dict[str, Any] = {"row": int(row), "date": date}
            if item.get("packet_idx") is not None:
                tags["packet_idx"] = int(item["packet_idx"])
            scalars.append(("snaps.night_median_db", round(float(band_median[i]), 3), tags))
        ctx.store.put_scalars(scalars, ts=t1)
        ctx.event(
            "night_median",
            severity="info",
            subject=f"kafka bandpass {date}",
            detail={"n_samples": int(stacked.shape[0]), "n_rows": len(rows)},
        )


def night_window_utc(day: datetime) -> tuple[float, float]:
    """(t0, t1) unix bounds of the 02:00-05:00 PT window of ``day``."""
    local = day.astimezone(NIGHT_TZ)
    start = local.replace(hour=NIGHT_START_H, minute=0, second=0, microsecond=0)
    return start.timestamp(), (start + timedelta(hours=NIGHT_END_H - NIGHT_START_H)).timestamp()


def read_latest_frame(settings: Settings) -> dict[str, Any] | None:
    """The newest frame mirror, or None when the collector has not run yet."""
    path = latest_frame_path(settings)
    if not path.is_file():
        return None
    try:
        with np.load(path) as npz:
            return {
                "ts": float(npz["ts"]),
                "written": float(npz["written"]),
                "rows": [int(r) for r in npz["rows"]],
                "subbands_ok": [bool(v) for v in npz["subbands_ok"]],
                "power_db": np.asarray(npz["power_db"], dtype=np.float32),
            }
    except Exception:
        log.warning("kafka_bp: latest-frame mirror unreadable", exc_info=True)
        return None


__all__ = [
    "AssembledFrame",
    "Frame",
    "KafkaBandpassCollector",
    "decode_bp",
    "decode_headers",
    "latest_frame_path",
    "read_latest_frame",
    "to_db",
]
