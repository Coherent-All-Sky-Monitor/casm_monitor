"""Scheduler for the hourly SNAP board read.

This collector never touches hardware. It ticks cheaply (60 s, one SQLite
query), and at most once an hour it submits a ``snap_read`` job — the job is
what contacts zapdos, once, in one ssh session.

The hour is enforced on the same watermark the liveness probe uses,
``zapdos/last_probe_ts``. That single slot is the point of coordination — when
the board read takes it, :class:`~casm_monitor.collectors.services.ZapdosCollector`
sees the slot gone and skips its own ``ssh true`` for that hour (and the job
stamps ``services.zapdos_ok`` itself, so the strip stays fed). No change to
``services.py`` was needed for this.

Claiming the slot and submitting the job are ONE transaction
(:meth:`casm_monitor.store.Store.submit_job_atomic`, ``require_slot=True``), so
a manual read that lands in the same millisecond cannot slip between the two
steps. The tick submits nothing if a ``snap_read`` job is already queued or
running or the read lease is held — an operator clicked "Read boards now" a
moment earlier and that click already claimed the slot and provides the hour's
contact — and in that case the slot is left exactly as it was.
"""

from __future__ import annotations

import time
from typing import Any

from ..jobs.snap_read import (
    LAST_READ_KEY,
    LOCK_KEY,
    LOCK_STREAM as LAST_READ_STREAM,
)
from ..snapmap import all_boards
from .base import Collector, CollectorContext

SNAP_READ_KIND = "snap_read"


def snap_read_interval_s(settings: Any) -> float:
    """Effective scheduler interval: the config can only make it longer."""
    return max(float(getattr(settings, "snap_read_interval_s", 3600.0)), 3600.0)


class SnapReadCollector(Collector):
    """Submits one ``snap_read`` job per hour; does no I/O of its own."""

    name = "snapread"
    default_cadence_s = 60.0
    timeout_s = 30.0

    def collect(self, ctx: CollectorContext) -> None:
        now = time.time()
        interval = snap_read_interval_s(ctx.settings)
        boards = all_boards(ctx.settings)
        ctx.scalar("snap.boards_configured", len(boards))
        if not boards:
            return

        job_id, refusal = ctx.store.submit_job_atomic(
            SNAP_READ_KIND,
            {"ips": None, "reason": "scheduled"},
            refuse_if_pending=True,
            lease=(LAST_READ_STREAM, LOCK_KEY),
            claim_slot=("zapdos", "last_probe_ts", interval),
            require_slot=True,
            now=now,
        )
        if refusal is not None:
            if refusal.get("reason") == "slot_taken":
                last = ctx.store.get_watermark(LAST_READ_STREAM, LAST_READ_KEY)
                if last is not None:
                    ctx.scalar("snap.last_read_age_s", round(now - float(last), 1))
                return
            # A manual read is already in flight (queued/running, or its lease
            # is held): that is this hour's contact, and it claimed the slot.
            ctx.scalar("snap.scheduled_skipped", 1)
            return

        ctx.event(
            "job_submitted",
            severity="info",
            subject=f"job {job_id} ({SNAP_READ_KIND})",
            detail={"job_id": job_id, "kind": SNAP_READ_KIND, "reason": "scheduled",
                    "boards": [b.ip for b in boards]},
        )
        ctx.scalar("snap.scheduled_job_id", job_id)
        ctx.scalar("snap.scheduled_skipped", 0)
