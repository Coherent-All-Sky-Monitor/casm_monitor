from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from casm_monitor.web.calibration_planning import sky_plan


@pytest.mark.parametrize('day,hours', [('2026-09-18', 36), ('2026-03-08', 35), ('2026-11-01', 37)])
def test_planner_local_range_and_canonical_tracks(day, hours):
    from astropy.utils import iers
    from casm_vis_analysis.sources import source_altaz
    result = sky_plan(day)
    times = np.array(result['times'])
    assert times[-1] - times[0] == hours * 3600
    assert np.all(np.diff(times) == 60)
    assert datetime.fromtimestamp(times[0], ZoneInfo('America/Los_Angeles')).hour == 12
    assert datetime.fromtimestamp(times[-1], ZoneInfo('America/Los_Angeles')).hour == 0
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
