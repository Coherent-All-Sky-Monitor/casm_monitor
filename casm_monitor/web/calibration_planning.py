"""Small, read-only sky planner. Source coordinates and window rules stay canonical."""
from datetime import date as calendar_date, datetime, time, timedelta
from functools import lru_cache
from threading import Lock
from zoneinfo import ZoneInfo
import warnings

import numpy as np

LOCAL = ZoneInfo('America/Los_Angeles')
SOURCES = (('sun', 'Sun'), ('cyg-a', 'Cyg A'), ('cas-a', 'Cas A'), ('tau-a', 'Tau A'))
_LOCK = Lock()


@lru_cache(maxsize=32)
def sky_plan(date: str) -> dict:
    """Previous local noon through the end of the solar day, sampled each minute.

    Includes the preceding night and the full selected civil day, including
    daylight-saving days. No visibility reads, template construction or jobs.
    Suggestions only satisfy sky-altitude cuts, not a data-quality check.
    """
    from astropy.utils import iers
    from casm_vis_analysis.sources import source_altaz
    from casm_vis_analysis.offsource import DEFAULT_ALTITUDE_CAPS_OVRO, find_quiet_windows

    day = calendar_date.fromisoformat(date)
    if day.isoformat() != date:
        raise ValueError('Use a date in YYYY-MM-DD form')
    start = datetime.combine(day - timedelta(days=1), time(12), LOCAL)
    end = datetime.combine(day + timedelta(days=1), time(), LOCAL)
    times = np.arange(start.timestamp(), end.timestamp() + 1, 60.)
    caps = dict(DEFAULT_ALTITUDE_CAPS_OVRO)
    # This is display planning with local ephemerides, never an IERS download.
    with _LOCK, warnings.catch_warnings(), iers.conf.set_temp('auto_download', False), iers.conf.set_temp('auto_max_age', None):
        warnings.simplefilter('ignore', iers.IERSWarning)
        tracks = [dict(id=name, label=label, cap_deg=caps[name],
                       altitude_deg=np.round(source_altaz(name, times)[0], 4).tolist())
                  for name, label in SOURCES]
        candidates = find_quiet_windows(times, altitude_caps=caps, min_duration_s=15 * 60)
    windows = []
    for candidate in candidates:
        # Offer a bounded template interval centred within the qualifying range.
        minutes = min(30, int(candidate['duration_s'] // 60))
        t0 = float(candidate['t_start'] + ((candidate['duration_s'] // 60 - minutes) // 2) * 60)
        windows.append(dict(start=candidate['t_start'], end=candidate['t_end'],
                            selection=[t0, t0 + minutes * 60], minutes=minutes))
    return dict(date=date, time_zone=LOCAL.key, times=times.tolist(), tracks=tracks,
                windows=windows, cadence_s=60, minimum_minutes=15,
                coverage_checked=False, hardware_compatibility_checked=False)
