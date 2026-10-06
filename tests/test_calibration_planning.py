from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from casm_monitor.web.calibration_planning import SOURCES, _suggest_static_window, sky_plan


@pytest.mark.parametrize('day,hours', [('2026-09-18', 24), ('2026-03-08', 23), ('2026-11-01', 25)])
def test_planner_local_range_and_canonical_tracks(day, hours):
    from astropy.utils import iers
    from casm_vis_analysis.sources import source_altaz
    result = sky_plan(day)
    times = np.array(result['times'])
    assert times[-1] - times[0] == hours * 3600 - 60
    assert len(times) == hours * 60
    assert np.all(np.diff(times) == 60)
    local = ZoneInfo('America/Los_Angeles')
    assert datetime.fromtimestamp(times[0], local).strftime('%Y-%m-%d %H:%M') == day + ' 00:00'
    assert datetime.fromtimestamp(times[-1], local).strftime('%Y-%m-%d %H:%M') == day + ' 23:59'
    assert all(datetime.fromtimestamp(t, local).strftime('%Y-%m-%d') == day for t in times)
    assert len(result['tracks']) == 4
    with iers.conf.set_temp('auto_download', False), iers.conf.set_temp('auto_max_age', None):
        for track in result['tracks']:
            assert np.allclose(track['altitude_deg'], source_altaz(track['id'], times)[0], atol=.00006)
    assert min(result['tracks'][2]['altitude_deg']) > 0
    assert not result['coverage_checked'] and not result['hardware_compatibility_checked']
    for window in result['windows']:
        assert window['start'] <= window['selection'][0] < window['selection'][1] <= window['end']
        assert 15 <= window['minutes'] <= 30
        mask = (times >= window['start']) & (times <= window['end'])
        for track in result['tracks']:
            assert max(np.array(track['altitude_deg'])[mask]) <= track['cap_deg'] + .0001


def test_windows_and_none_match_canonical_finder():
    from astropy.utils import iers
    from casm_vis_analysis.offsource import DEFAULT_ALTITUDE_CAPS_OVRO, find_quiet_windows
    for day in ('2026-05-16', '2026-09-18'):
        result = sky_plan(day)
        with iers.conf.set_temp('auto_download', False), iers.conf.set_temp('auto_max_age', None):
            expected = find_quiet_windows(result['times'], altitude_caps=DEFAULT_ALTITUDE_CAPS_OVRO, min_duration_s=900)
        assert [(w['start'], w['end']) for w in result['windows']] == [(w['t_start'], w['t_end']) for w in expected]
        assert sky_plan(day) is result


@pytest.mark.parametrize('date', ['bad', '2026-02-30', '20260918'])
def test_invalid_dates(date):
    with pytest.raises(ValueError):
        sky_plan(date)


def two_windows(first, second):
    """Two 30-minute windows separated by a sample with Sun above horizon."""
    tracks = [dict(id=source, cap_deg=0 if source in ('sun', 'tau-a') else 15,
                   altitude_deg=[first[k]] * 31 + [90] + [second[k]] * 31)
              for k, (source, _) in enumerate(SOURCES)]
    return np.arange(63) * 60., tracks


@pytest.mark.parametrize('first,second,index', [
    ((-1, 5, 80, 80), (-60, 10, 0, 0), 32),  # Dark Sun takes precedence.
    ((-12, -1, 10, 80), (-60, -50, 20, 0), 0),  # Set Cyg A ties at zero.
    ((-12, -1, 10, 30), (-60, -50, 10, 20), 32),  # Tau A breaks Cas A tie.
    ((-12, -1, 10, -1), (-60, -50, 10, -40), 0),  # Set Tau A ties; earliest.
    ((0, -10, 0, 0), (-1, 80, 80, 80), 32),  # Sun must be strictly down.
])
def test_suggestion_priority(first, second, index):
    times, tracks = two_windows(first, second)
    result = _suggest_static_window(times, tracks)
    assert result['selection'] == [times[index], times[index + 30]]


def test_suggestion_checks_full_window_and_peak_not_average():
    times, tracks = two_windows((-5, 0, 5, -2), (-5, 20, 5, -2))
    tracks[1]['altitude_deg'][15] = 40
    result = _suggest_static_window(times, tracks)
    assert result['selection'][0] == times[32]
    assert result['altitude_max_deg']['cyg-a'] == 20
    assert result['exceeded_caps'] == ['cyg-a'] and not result['meets_altitude_caps']
    tracks[0]['altitude_deg'][-1] = 0  # Include the end sample, not just midpoint.
    assert _suggest_static_window(times, tracks)['selection'][0] == times[0]
    tracks[0]['altitude_deg'][0] = 0
    assert not _suggest_static_window(times, tracks)['dark']


def test_suggestion_no_daylight_short_or_nonfinite_windows():
    times, tracks = two_windows((1, 0, 0, 0), (1, 0, 0, 0))
    assert 'bright solar contamination' in _suggest_static_window(times, tracks)['warning']
    assert _suggest_static_window(times[:30], tracks) is None
    times, tracks = two_windows((-1, 0, 0, 0), (-1, 0, 0, 0))
    tracks[1]['altitude_deg'][0] = float('nan')
    tracks[2]['altitude_deg'][-1] = float('inf')
    assert _suggest_static_window(times, tracks)['warning']
    tracks[1]['altitude_deg'] = [float('nan')] * len(times)
    assert _suggest_static_window(times, tracks) is None


@pytest.mark.parametrize('day', ['2026-05-16', '2026-09-17', '2026-09-18', '2026-03-08', '2026-11-01'])
def test_suggested_night_is_global_priority_minimum_on_local_day(day):
    result = sky_plan(day)
    suggestion = result['suggestion']
    times = np.array(result['times'])
    tracks = result['tracks']
    candidates = []
    for i in range(len(times) - 30):
        maxima = [max(t['altitude_deg'][i:i + 31]) for t in tracks]
        if maxima[0] <= -10:
            candidates.append((tuple(max(0, v) for v in maxima[1:]), times[i]))
    assert suggestion['selection'][0] == min(candidates)[1]
    assert suggestion['selection'][1] - suggestion['selection'][0] == 1800
    assert times[0] <= suggestion['selection'][0] < suggestion['selection'][1] <= times[-1]
    assert suggestion['altitude_max_deg']['sun'] <= -10
    assert suggestion['meets_altitude_caps'] == (len(suggestion['exceeded_caps']) == 0)
