"""Scheduler for the hourly SNAP board read.

This collector never touches hardware. It ticks cheaply (60 s, one SQLite
query), and at most once an hour it submits a ``snap_read`` job — the job is
what contacts zapdos, once, in one ssh session.

The hour is enforced the same way the liveness probe enforces it, and on the
*same* watermark: :func:`casm_monitor.collectors.services.claim_probe_slot` on
``zapdos/last_probe_ts``. That single slot is the point of coordination — when
the board read takes it, :class:`~casm_monitor.collectors.services.ZapdosCollector`
sees the slot gone and skips its own ``ssh true`` for that hour (and the job
stamps ``services.zapdos_ok`` itself, so the strip stays fed). No change to
``services.py`` was needed for this.

If the claim succeeds but a ``snap_read`` job is already queued or running (an
operator clicked "Read boards now" a moment earlier), no second job is
submitted: the click already provides the hour's contact. The claim is not
given back in that case, which is deliberate — the hour has been used.
"""

from __future__ import annotations

import time
from typing import Any

from ..jobs.snap_read import LAST_READ_KEY, LOCK_STREAM as LAST_READ_STREAM
from ..snapmap import all_boards
from .base import Collector, CollectorContext
from .services import claim_probe_slot

SNAP_READ_KIND = "snap_read"


def snap_read_interval_s(settings: Any) -> float:
    """Effective scheduler interval: the config can only make it longer."""
    return max(float(getattr(settings, "snap_read_interval_s", 3600.0)), 3600.0)


def pending_snap_read(store: Any) -> dict[str, Any] | None:
    """A queued or running ``snap_read`` job, if there is one."""
    rows = store.query(
        "SELECT id, state FROM jobs WHERE kind = ? AND state IN ('queued', 'running') "
        "ORDER BY id DESC LIMIT 1",
        (SNAP_READ_KIND,),
    )
    return {"id": int(rows[0]["id"]), "state": str(rows[0]["state"])} if rows else None


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

        if not claim_probe_slot(ctx.store, "zapdos", "last_probe_ts", now, interval):
            last = ctx.store.get_watermark(LAST_READ_STREAM, LAST_READ_KEY)
            if last is not None:
                ctx.scalar("snap.last_read_age_s", round(now - float(last), 1))
            return

        pending = pending_snap_read(ctx.store)
        if pending is not None:
            # A manual read is already in flight: that is this hour's contact.
            ctx.scalar("snap.scheduled_skipped", 1)
            return

        job_id = ctx.store.submit_job(SNAP_READ_KIND, {"ips": None, "reason": "scheduled"})
        ctx.event(
            "job_submitted",
            severity="info",
            subject=f"job {job_id} ({SNAP_READ_KIND})",
            detail={"job_id": job_id, "kind": SNAP_READ_KIND, "reason": "scheduled",
                    "boards": [b.ip for b in boards]},
        )
        ctx.scalar("snap.scheduled_job_id", job_id)
        ctx.scalar("snap.scheduled_skipped", 0)
