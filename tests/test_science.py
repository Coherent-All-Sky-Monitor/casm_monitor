"""Scientific view boundaries and canonical plot reuse, using synthetic arrays."""
import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException

from casm_monitor.web import science


def request(**kw):
    return science.ScienceRequest(**({'pairs': [(8, 18)], 't0': 1789315200., 't1': 1789316200.} | kw))


@pytest.mark.parametrize('change', [dict(t0=float('nan')), dict(t1=1789315200.), dict(t1=1789999999.),
                                   dict(fmin=500), dict(pairs=[(18, 8)]), dict(pairs=[(8, 8)]),
                                   dict(compare_t0=1789228800.), dict(resolution='full', t1=1789400000.)])
def test_invalid_selection(change):
    with pytest.raises(HTTPException):
        science.validate_request(request(**change))


def test_layout_boundary_and_override(monkeypatch):
    monkeypatch.setattr(science, 'layouts', lambda: [dict(id='a', date='2026-09-01'), dict(id='b', date='2026-09-14')])
    assert science.select_layout(1789315200, 1789316200)['id'] == 'a'
    with pytest.raises(HTTPException):
        science.select_layout(1789315200, 1789405200)
    with pytest.raises(HTTPException):
        science.select_layout(1789315200, 1789316200, 'b')


def test_budget_before_data_read():
    rows = [dict(t0=0, t1=1, meta=dict(t=list(range(5000)), nchan=384, freq_top_mhz=484.2))]
    vs = SimpleNamespace(shards=SimpleNamespace(list=lambda *a, **k: rows))
    with pytest.raises(HTTPException) as exc:
        science.bounded_rows(vs, 'vis_avg8', 0, 5000, [(8,18)])
    assert exc.value.status_code == 400


def test_timestamp_timezone_contract():
    assert request(t0='2026-09-14T00:00:00Z').t0 == 1789344000
    with pytest.raises(ValueError):
        request(t0='2026-09-14T00:00:00')


@pytest.mark.parametrize('compare',[False,True])
def test_single_baseline_sun_transform_and_canonical_phase_renderer(compare):
    """Exercise the real reference transform; mocking it hid a broadcast bug."""
    import matplotlib.pyplot as plt
    from casm_monitor import vis_ops
    stamps=1789315200+np.arange(5)*science.DT_S
    freq=np.linspace(470,410,11)
    positions=np.array([[0.,0.,0.],[0.,10.,0.]])
    raw=np.exp(1j*np.arange(55).reshape(5,1,11)/7)
    stopped=vis_ops.fringe_stop_sun(raw,freq,positions,[(0,1)],stamps)
    assert stopped.shape==raw.shape
    comp=None
    if compare:
        earlier=stamps-86400
        comp=(vis_ops.fringe_stop_sun(raw,freq,positions,[(0,1)],earlier),earlier,freq)
        assert comp[0].shape==raw.shape
    figs=science.draw_views(request(kind='phase_spectrum'),stopped,stamps,freq,['10 m NS · N01E1 × N21E1'],comp)
    assert len(figs)==1 and len(figs[0].axes)==(2 if compare else 1)
    if compare:
        captions=' '.join(t.get_text() for t in figs[0].texts)
        assert 'Selected:' in captions and 'Comparison:' in captions
        assert '2026-09-13' in captions and '2026-09-12' in captions
    expected=np.angle(np.mean(stopped[:,0,:],axis=0))
    np.testing.assert_allclose(figs[0].axes[0].lines[0].get_ydata(),expected)
    for fig in figs: plt.close(fig)


@pytest.mark.parametrize('kind',['amplitude_spectrum','autos'])
def test_canonical_spectrum_shape_and_mean(kind):
    import matplotlib.pyplot as plt
    z=np.array([[[1,2,3],[4,5,6]],[[3,4,5],[6,7,8]]],complex)
    stamps=np.array([1789315200,1789315337.])
    freq=np.array([470,440,410])
    figs=science.draw_views(request(kind=kind,reference='raw'),z,stamps,freq,['N01E1','N21E1'])
    for k in range(2):
        np.testing.assert_allclose(figs[0].axes[k].lines[0].get_ydata(),np.mean(np.abs(z[:,k,:]),axis=0))
    for fig in figs: plt.close(fig)


def test_load_uses_selected_rows_and_omits_known_junk(monkeypatch):
    rows = [dict(id='one', t0=0, t1=400, meta=dict(t=[0, 137, 274, 400], nchan=3, freq_top_mhz=430,
                                                    flags=dict(first_of_file=[True, False, False, False])))]
    def series(*args, **kwargs):
        assert kwargs == {'allow_full_fallback': False}
        return np.ones((4,1,3), complex), np.array([0,137,274,400]), np.array([430,420,410]), [8,18]
    vs = SimpleNamespace(shards=SimpleNamespace(list=lambda *a, **k: rows), series=series)
    out = science.load_selection(vs, request(reference='raw'), 0, 400, {'path': 'unused'})
    assert out[0].shape == (3,1,3)
    assert out[-1] == 1
    assert out[1][0] == 137


@pytest.mark.parametrize('kind', ['phase_waterfall', 'phase_spectrum', 'amplitude_waterfall', 'amplitude_spectrum', 'autos'])
def test_canonical_render_and_artifact_cache(kind, settings, store, tmp_path, monkeypatch):
    settings = replace(settings, observation_cache_root=tmp_path / 'artifacts')
    layout = tmp_path / 'layout.csv'
    layout.write_text('fixture')
    monkeypatch.setattr(science, 'select_layout', lambda *a, **k: dict(id='fixture', path=str(layout)))
    monkeypatch.setattr(science, 'geometry', lambda p: ([dict(packet_idx=8, label='ant 9 N01E1 S0A8'), dict(packet_idx=18, label='ant 19 N21E1 S1A6')], []))
    monkeypatch.setattr(science, 'bounded_rows', lambda *a: [dict(id='fixture', path='cache', t0=0, t1=1)])
    t = 1789315200 + np.arange(6) * science.DT_S
    f = np.linspace(480, 400, 8)
    z = np.exp(1j * np.arange(48).reshape(6,1,8)/5)
    monkeypatch.setattr(science, 'load_selection', lambda *a: (z, t, f, [], 0))
    req = request(kind=kind, pairs=[(8,8)] if kind == 'autos' else [(8,18)], reference='raw')
    result = science.render_product(settings, store, req)
    dest = science.cache_dir(settings) / 'science' / result['id']
    assert (dest / 'plot-0.png').stat().st_size > 1000
    if kind == 'phase_waterfall':
        from PIL import Image
        with Image.open(dest / 'plot-0.png') as image:
            assert image.width > 3 * image.height
    assert np.load(dest / 'data.npz')['vis'].shape == (6,1,8)
    assert result['provenance']['samples'] == 6
    assert result['provenance']['baseline_labels']
    monkeypatch.setattr(science, 'load_selection', lambda *a: pytest.fail('cache must avoid another data read'))
    assert science.render_product(settings, store, req)['id'] == result['id']
