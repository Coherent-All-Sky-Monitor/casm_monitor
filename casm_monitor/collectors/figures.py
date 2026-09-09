"""``FigureScheduler`` (M2 figures, reworked 2026-09-09): submits, never renders.

The rendering that used to run in-process here (``FigureCollector``) put
``casm-monitor-collect.service`` (``MemoryMax=8G``) in an OOM crash loop
21:23-22:16 PDT: :func:`casm_monitor.figures.vis_figures.load_window` loaded a
whole 24 h visibilities window into RAM (``vis_avg8`` ~580 MB/input set, with
a ``vis_full`` fallback at ~4.6 GB/input set) and rendered 16 figures
concurrently in a thread pool, and the long run also starved the other
collectors (vis age climbed to 820 s).

The render now happens in :mod:`casm_monitor.jobs.render_figures`, a job kind
run by ``casm-monitor-jobs.service`` (``MemoryMax=256G``, one job at a time),
which also bounds its own memory (never reads ``vis_full``, reduces the
``vis_avg8`` window shard by shard -- see that module and ``figures/
vis_figures.py``'s docstrings).

This collector is cheap and does no I/O beyond a couple of SQLite queries:

* every :data:`CADENCE_S` (60 s) it submits a ``render_figures`` job for all
  of :data:`SCHEDULED_TARGETS` (vis, snaps and, since M4, imaging), but only
  every :data:`SUBMIT_INTERVAL_S` (30 min), refusing (via
  ``Store.submit_job_atomic``'s ``refuse_if_pending``) while one is already
  queued or running;
* it additionally submits a ``snaps``-only job, independent of that 30 min
  cadence, whenever a newer SNAP board read exists than the snaps manifest
  already on disk -- so a "Read boards now" click's ``spectra_board`` panel
  updates promptly without waiting for the next half hour.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ..store.shards import ensure_contained
from .base import Collector, CollectorContext

log = logging.getLogger("casm_monitor.collect.figures")

RENDER_FIGURES_KIND = "render_figures"
#: Targets of the half-hourly scheduled render. ``imaging`` (M4) joined the
#: pair in 2026-09-09: its own pass is incremental (it images at most 64 new
#: integrations into a frame cache and rebuilds the cheap products from it),
#: so adding it does not lengthen the job by a 24 h re-render.
SCHEDULED_TARGETS = ["vis", "snaps", "imaging"]
SUBMIT_INTERVAL_S = 1800.0  # 30 min
LAST_SUBMIT_STREAM = "figures"
LAST_SUBMIT_KEY = "last_submit_ts"


class FigureScheduler(Collector):
    """Submits ``render_figures`` jobs; never touches matplotlib itself."""

    name = "figures"
    default_cadence_s = 60.0
    timeout_s = 30.0

    def collect(self, ctx: CollectorContext) -> None:
        now = time.time()
        job_id, refusal = ctx.store.submit_job_atomic(
            RENDER_FIGURES_KIND,
            {"targets": SCHEDULED_TARGETS, "reason": "scheduled"},
            refuse_if_pending=True,
            claim_slot=(LAST_SUBMIT_STREAM, LAST_SUBMIT_KEY, SUBMIT_INTERVAL_S),
            require_slot=True,
            now=now,
        )
        if refusal is not None:
            reason = refusal.get("reason")
            ctx.scalar("figures.scheduled_skipped", 1, tags={"reason": str(reason)})
            if reason == "slot_taken":
                last = ctx.store.get_watermark(LAST_SUBMIT_STREAM, LAST_SUBMIT_KEY)
                if last is not None:
                    ctx.scalar("figures.last_submit_age_s", round(now - float(last), 1))
        else:
            ctx.event(
                "job_submitted",
                severity="info",
                subject=f"job {job_id} ({RENDER_FIGURES_KIND})",
                detail={"job_id": job_id, "kind": RENDER_FIGURES_KIND, "reason": "scheduled",
                        "targets": SCHEDULED_TARGETS},
            )
            ctx.scalar("figures.scheduled_job_id", job_id)
            ctx.scalar("figures.scheduled_skipped", 0)

        self._maybe_submit_snaps_only(ctx, now)

    # -- "a board read just landed" fast path ----------------------------
    def _maybe_submit_snaps_only(self, ctx: CollectorContext, now: float) -> None:
        board_read_ts = self._latest_board_read_ts(ctx.store)
        if board_read_ts is None:
            return
        manifest_ts = self._newest_snaps_manifest_board_read_ts(ctx.settings)
        if manifest_ts is not None and float(board_read_ts) <= float(manifest_ts):
            return
        job_id, refusal = ctx.store.submit_job_atomic(
            RENDER_FIGURES_KIND,
            {"targets": ["snaps"], "reason": "board_read"},
            refuse_if_pending=True,
            now=now,
        )
        if refusal is not None:
            # A scheduled (or another board-read) render is already
            # queued/running -- it will pick up this board read too (the job
            # re-checks the watermark itself), nothing lost by not submitting
            # a second one.
            return
        ctx.event(
            "job_submitted",
            severity="info",
            subject=f"job {job_id} ({RENDER_FIGURES_KIND})",
            detail={"job_id": job_id, "kind": RENDER_FIGURES_KIND, "reason": "board_read",
                    "targets": ["snaps"], "board_read_ts": board_read_ts},
        )
        ctx.scalar("figures.board_read_job_id", job_id)

    @staticmethod
    def _latest_board_read_ts(store) -> float | None:
        from ..jobs.snap_read import latest_reads

        reads = latest_reads(store)
        if not reads:
            return None
        return max((float(s.get("ts") or 0.0) for s in reads.values()), default=None)

    @staticmethod
    def _newest_snaps_manifest_board_read_ts(settings) -> float | None:
        """Newest ``board_read_ts`` across the snaps sets' manifests, if any."""
        root = ensure_contained(
            Path(settings.store_root) / "figures" / "snaps", settings.store_root
        )
        newest: float | None = None
        if not root.is_dir():
            return None
        for child in root.iterdir():
            manifest_path = child / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text())
            except (OSError, ValueError):
                continue
            ts = manifest.get("board_read_ts")
            if ts is None:
                continue
            ts = float(ts)
            if newest is None or ts > newest:
                newest = ts
        return newest


__all__ = ["FigureScheduler"]
