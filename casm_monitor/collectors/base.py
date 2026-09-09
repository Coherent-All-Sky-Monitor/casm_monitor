"""Collector base class and the context they write through.

A collector is a small synchronous object: it has a name, a cadence, a timeout,
and a ``collect(ctx)`` method that does blocking I/O (sockets, ssh, files). The
runner executes it in a thread pool so one slow collector cannot block the loop,
and isolates its exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..store import ShardWriter, Store


@dataclass
class CollectorContext:
    """Everything a collector is allowed to touch."""

    settings: Settings
    store: Store
    shards: ShardWriter

    def scalar(
        self,
        name: str,
        value: float | int | str | None,
        *,
        tags: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        self.store.put_scalar(name, value, tags=tags, ts=ts)

    def event(
        self,
        kind: str,
        *,
        severity: str = "info",
        subject: str | None = None,
        detail: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> int:
        return self.store.add_event(
            kind, severity=severity, subject=subject, detail=detail, ts=ts
        )

    def on_change(
        self,
        name: str,
        value: float | int | str | None,
        *,
        kind: str,
        severity: str = "info",
        subject: str | None = None,
        detail: dict[str, Any] | None = None,
        record: bool = True,
        tags: dict[str, Any] | None = None,
    ) -> bool:
        """Record a scalar and emit ``kind`` only when its value changed.

        The previous value is the last stored row for ``name``, so state-change
        events survive a collector restart and are not re-emitted on every
        cadence tick.
        """
        previous = self.store.latest_scalar(name)
        prev_value = None if previous is None else previous["value"]
        changed = previous is not None and _differs(prev_value, value)
        if record:
            self.scalar(name, value, tags=tags)
        if changed:
            payload = {"name": name, "from": prev_value, "to": value}
            payload.update(detail or {})
            self.event(kind, severity=severity, subject=subject or name, detail=payload)
        return changed


def _differs(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) > 1e-12
    return str(a) != str(b)


class Collector:
    """Base class. Subclasses set ``name`` and implement ``collect``."""

    name: str = "collector"
    default_cadence_s: float = 60.0
    timeout_s: float = 30.0

    def __init__(self, settings: Settings, cadence_s: float | None = None) -> None:
        self.settings = settings
        self.cadence_s = float(
            cadence_s if cadence_s is not None else settings.cadence(self.name, self.default_cadence_s)
        )

    def collect(self, ctx: CollectorContext) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def close(self, ctx: CollectorContext) -> None:
        """Orderly-shutdown hook, called once by the runner on SIGTERM/stop
        (before the store is closed). No-op by default; a collector that
        buffers anything in memory (e.g. ``KafkaBandpassCollector``'s shard
        buffers) overrides this to flush it so a restart loses nothing."""
