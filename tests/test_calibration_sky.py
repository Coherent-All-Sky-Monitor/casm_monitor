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


def test_schematic_reuses_legacy_renderer_without_response_or_hits(tmp_path, monkeypatch):
    import numpy as np
    import matplotlib.pyplot as plt
    from bf_weights_generator import plot_transit
    from astropy.time import Time
    monkeypatch.setattr(plot_transit, '_load_beams_from_hdf5', lambda path:
                        (np.full(512, 45.), np.linspace(0, 360, 512), [], 4., 18.))
    monkeypatch.setattr(plot_transit, '_compute_source_track', lambda *a, **k:
                        (Time([1.7e9, 1.7e9 + 60], format='unix'), np.array([40., 42.]), np.array([80., 81.])))
    def forbidden(*args, **kwargs):
        raise AssertionError('Schematic must not predict ellipse hits or exact coverage')
    monkeypatch.setattr(plot_transit, '_check_beam_hits', forbidden)
    monkeypatch.setattr(plot_transit, '_halfpower_extents', forbidden)
    original = plot_transit._plot_two_panel
    def render(tracks, *args, **kwargs):
        assert all(not track['hits'] for track in tracks)
        assert kwargs.get('positions_enu') is None and kwargs.get('weights_path') is None
        return original(tracks, *args, **kwargs)
    monkeypatch.setattr(plot_transit, '_plot_two_panel', render)
    output = tmp_path / 'sky.png'
    result = sky.render_schematic('unused.h5', output, '2026-09-25')
    assert result['beam_count'] == 512
    np.testing.assert_allclose(plt.imread(output)[0, 0, :3], 1)
