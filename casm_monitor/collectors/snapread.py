"""Schedule bounded SNAP diagnostics independently of the liveness probe.

The configured interval (two hours on this host) is enforced against both the
last completed read and a SNAP-specific scheduling slot. An ``ssh true`` probe
cannot consume that slot and starve spectra acquisition. Submission, guards and
timestamps remain one existing Store transaction; queued jobs and the existing
read lease prevent overlap with manual requests. A submitted diagnostic stamps
the liveness slot too, suppressing redundant probes after it. A probe that ran
just before a due diagnostic no longer postpones that diagnostic. No hardware
contact occurs in this collector; the existing worker owns acquisition.
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
SCHEDULE_KEY = "last_scheduled_ts"


def snap_read_interval_s(settings: Any) -> float:
    """Effective scheduler interval: the config can only make it longer."""
    return max(float(getattr(settings, "snap_read_interval_s", 3600.0)), 3600.0)


class SnapReadCollector(Collector):
    """Submit at the configured interval; do no hardware I/O of its own."""

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
            rate_limit=(LAST_READ_STREAM, LAST_READ_KEY, interval),
            claim_slot=(LAST_READ_STREAM, SCHEDULE_KEY, interval),
            require_slot=True,
            stamp=[("zapdos", "last_probe_ts", now)],
            now=now,
        )
        if refusal is not None:
            if refusal.get("reason") in {"slot_taken", "rate_limited"}:
                last = ctx.store.get_watermark(LAST_READ_STREAM, LAST_READ_KEY)
                if last is not None:
                    ctx.scalar("snap.last_read_age_s", round(now - float(last), 1))
                return
            # A manual read is already in flight (queued/running, or its lease
            # is held): do not add another read to the current interval.
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
