"""Read-only sky planner using canonical source tracks and an operator ranking."""
from datetime import date as calendar_date, datetime, time, timedelta
from functools import lru_cache
from threading import Lock
from zoneinfo import ZoneInfo
import warnings

import numpy as np

LOCAL = ZoneInfo('America/Los_Angeles')
SOURCES = (('sun', 'Sun'), ('cyg-a', 'Cyg A'), ('cas-a', 'Cas A'), ('tau-a', 'Tau A'))
_LOCK = Lock()


def _suggest_static_window(times: np.ndarray, tracks: list[dict]) -> dict | None:
    """Rank full 30-minute windows on the one-minute grid, endpoints included.

    Sun below zero is mandatory throughout. Minimise the maximum above-horizon
    altitude in strict Cyg A, Cas A, Tau A order. Already-set sources tie at zero;
    a deeper Sun position cannot outweigh a lower Cyg A once the Sun is down.
    Exact ties pick the earliest window. This is an altitude-only UI suggestion,
    not a change to canonical quiet-window caps or the calibration recipe.
    """
    minutes = 30
    if len(times) <= minutes:
        return None
    by_id = {track['id']: track for track in tracks}
    maxima = np.array([
        np.max(np.lib.stride_tricks.sliding_window_view(
            np.asarray(by_id[source]['altitude_deg']), minutes + 1), axis=-1)
        for source, _ in SOURCES
    ])
    eligible = np.flatnonzero((maxima[0] < 0) & np.all(np.isfinite(maxima), axis=0))
    if not len(eligible):
        return None
    scores = np.maximum(maxima, 0.)
    index = min(eligible, key=lambda i: (*scores[1:, i], times[i]))
    exceeded = [source for k, (source, _) in enumerate(SOURCES)
                if maxima[k, index] >= by_id[source]['cap_deg']]
    return dict(selection=[float(times[index]), float(times[index + minutes])],
                minutes=minutes,
                altitude_max_deg={source: float(maxima[k, index])
                                  for k, (source, _) in enumerate(SOURCES)},
                meets_altitude_caps=not exceeded, exceeded_caps=exceeded)


@lru_cache(maxsize=32)
def sky_plan(date: str) -> dict:
    """Selected local date, 00:00 through 23:59, sampled each minute.

    Uses civil-day boundaries, including 23/25-hour daylight-saving days.
    No visibility reads, template construction or jobs.
    Suggestions rank night-time source altitudes, not measured data quality.
    Canonical fixed-cut windows are returned separately, without relaxed caps.
    """
    from astropy.utils import iers
    from casm_vis_analysis.sources import source_altaz
    from casm_vis_analysis.offsource import DEFAULT_ALTITUDE_CAPS_OVRO, find_quiet_windows

    day = calendar_date.fromisoformat(date)
    if day.isoformat() != date:
        raise ValueError('Use a date in YYYY-MM-DD form')
    start = datetime.combine(day, time(), LOCAL)
    end = datetime.combine(day + timedelta(days=1), time(), LOCAL)
    times = np.arange(start.timestamp(), end.timestamp(), 60.)
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
                windows=windows, suggestion=_suggest_static_window(times, tracks),
                cadence_s=60, minimum_minutes=15,
                coverage_checked=False, hardware_compatibility_checked=False)
