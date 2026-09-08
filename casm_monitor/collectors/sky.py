"""Sky positions: alt/az now and the next altitude maximum within 24 h.

Uses ``casm_vis_analysis.sources.source_altaz`` so the monitor and the
calibration recipe agree on the OVRO location and the source catalog. Catalog
keys verified against ``sources.CATALOG``: ``cas_a``, ``tau_a``, ``cyg_a``,
``vir_a``, ``b0329_54``, plus the special-cased ``sun``.

"Next altitude maximum" is found on a 1-minute grid over the coming 24 h, the
same coarse-then-exact convention the beam-grid tooling uses (never an analytic
ellipse).
"""

from __future__ import annotations

import time

import numpy as np

from ..util import iso
from .base import Collector, CollectorContext

SOURCES = ("sun", "cyg_a", "cas_a")
GRID_STEP_S = 60.0
GRID_SPAN_S = 86400.0


def next_altitude_maximum(
    name: str, t_start: float, *, span_s: float = GRID_SPAN_S, step_s: float = GRID_STEP_S
) -> tuple[float, float]:
    """(unix time, altitude) of the largest altitude on the grid ahead.

    The grid maximum is the transit when one occurs in the window; if the source
    is setting and does not rise again inside the window the first sample wins,
    which is the honest answer for a 24 h horizon.
    """
    from casm_vis_analysis.sources import source_altaz

    times = t_start + np.arange(0.0, span_s + step_s, step_s)
    alt, _az = source_altaz(name, times)
    idx = int(np.argmax(alt))
    return float(times[idx]), float(alt[idx])


class SkyCollector(Collector):
    """Alt/az now and next transit for the calibrator sources."""

    name = "sky"
    default_cadence_s = 60.0
    timeout_s = 60.0

    def collect(self, ctx: CollectorContext) -> None:
        from casm_vis_analysis.sources import source_altaz

        now = time.time()
        for source in SOURCES:
            alt, az = source_altaz(source, now)
            alt_now = float(np.atleast_1d(alt)[0])
            az_now = float(np.atleast_1d(az)[0])
            ctx.scalar(f"sky.{source}.alt_deg", round(alt_now, 3))
            ctx.scalar(f"sky.{source}.az_deg", round(az_now, 3))
            t_max, alt_max = next_altitude_maximum(source, now)
            ctx.scalar(f"sky.{source}.next_max_utc", iso(t_max))
            ctx.scalar(f"sky.{source}.next_max_alt_deg", round(alt_max, 3))
            ctx.scalar(f"sky.{source}.next_max_in_hr", round((t_max - now) / 3600.0, 3))
            ctx.on_change(
                f"sky.{source}.up",
                1 if alt_now > 0 else 0,
                kind="source_rise_set",
                severity="info",
                subject=source,
                detail={"alt_deg": round(alt_now, 3)},
            )
