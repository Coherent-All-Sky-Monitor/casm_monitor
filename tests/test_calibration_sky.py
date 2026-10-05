import json
from types import SimpleNamespace

import pytest

from casm_monitor.web import calibration_sky as sky


def test_sky_view_is_cached_and_does_not_modify_weights(tmp_path, monkeypatch):
    weights = tmp_path / 'deployed.h5'
    weights.write_bytes(b'recorded weights')
    settings = SimpleNamespace(store_root=tmp_path / 'calibration')
    monkeypatch.setattr(sky, 'product', lambda settings, tag: (weights, 'Reference'))
    calls = []
    def render(source, output, date):
        calls.append((source, date))
        output.write_bytes(b'png')
        return {'beam_count': 512}
    monkeypatch.setattr(sky, 'render_schematic', render)
    first = sky.sky_view(settings, 'test', '2026-09-25')
    assert first == sky.sky_view(settings, 'test', '2026-09-25')
    assert len(calls) == 1 and weights.read_bytes() == b'recorded weights'
    assert first['url'].startswith('/api/calibration/sky/')
    assert sky.sky_view(settings, 'test', '2026-09-24')['url'] != first['url']
    with pytest.raises(ValueError):
        sky.sky_view(settings, 'test', '../../secret')


@pytest.mark.parametrize('day,hours,abbreviation', [
    ('2026-10-04', 24, 'PDT'), ('2026-01-15', 24, 'PST'),
    ('2026-03-08', 23, 'PST/PDT'), ('2026-11-01', 25, 'PDT/PST'),
])
def test_schematic_reuses_legacy_renderer_without_response_or_hits(tmp_path, monkeypatch, day, hours, abbreviation):
    from datetime import datetime, timedelta, timezone
    import numpy as np
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from bf_weights_generator import plot_transit
    from astropy.time import Time
    monkeypatch.setattr(plot_transit, '_load_beams_from_hdf5', lambda path:
                        (np.full(512, 45.), np.linspace(0, 360, 512), [], 4., 18.))
    calls, figures = [], []
    local_start = datetime.fromisoformat(day).replace(tzinfo=sky.LOCAL)
    local_end = local_start + timedelta(days=1)
    def track(source, start, end, **kwargs):
        assert start.tzinfo == end.tzinfo == timezone.utc
        assert start == local_start.astimezone(timezone.utc)
        assert end == local_end.astimezone(timezone.utc)
        assert (end-start).total_seconds() == hours*3600
        calls.append(source)
        return Time([start, end]), np.array([40., 42.]), np.array([80., 81.])
    monkeypatch.setattr(plot_transit, '_compute_source_track', track)
    def forbidden(*args, **kwargs):
        raise AssertionError('Schematic must not predict ellipse hits or exact coverage')
    monkeypatch.setattr(plot_transit, '_check_beam_hits', forbidden)
    monkeypatch.setattr(plot_transit, '_halfpower_extents', forbidden)
    original = plot_transit._plot_two_panel
    def render(tracks, *args, **kwargs):
        assert all(not track['hits'] for track in tracks)
        assert kwargs.get('positions_enu') is None and kwargs.get('weights_path') is None
        assert args[-1] == sky.LOCAL
        figure = original(tracks, *args, **kwargs)
        figures.append(figure)
        return figure
    monkeypatch.setattr(plot_transit, '_plot_two_panel', render)
    output = tmp_path / 'sky.png'
    result = sky.render_schematic('unused.h5', output, day)
    assert result['beam_count'] == 512
    assert calls == ['sun', 'cyg-a', 'cas-a', 'tau-a']
    assert result['time_zone'] == 'America/Los_Angeles'
    assert result['time_label'] == f'OVRO local time ({abbreviation})'
    assert result['window_local'] == [local_start.isoformat(), local_end.isoformat()]
    assert result['window_utc'] == [value.astimezone(timezone.utc).isoformat() for value in (local_start,local_end)]
    elevation = figures[0].axes[1]
    assert elevation.get_xlabel() == result['time_label']
    np.testing.assert_allclose(elevation.get_xlim(), mdates.date2num([local_start,local_end]))
    noon = local_start.replace(hour=12).astimezone(timezone.utc)
    assert elevation.xaxis.get_major_formatter()(mdates.date2num(noon)) == '12:00'
    assert elevation.xaxis.get_major_locator().tz == sky.LOCAL
    assert 'UTC' not in figures[0]._suptitle.get_text()
    np.testing.assert_allclose(plt.imread(output)[0, 0, :3], 1)
