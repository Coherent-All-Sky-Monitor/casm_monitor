import json
from types import SimpleNamespace

import numpy as np
import pytest

from casm_monitor.config import Settings
from casm_monitor.web import calibration_history as history
from casm_monitor.web import calibration_workspace as workspace


def test_saved_history_redraw_excludes_alternate_and_tracks_reference_label(tmp_path, monkeypatch):
    settings = workspace.workspace_settings(Settings(store_root=tmp_path/'production', observation_cache_root=tmp_path/'preview'))
    root = workspace.build_dir(settings, 'test')
    identity = 'a'*32
    dest = root/'actions'/identity
    dest.mkdir(parents=True)
    record = {'params':{'antennas':[9,10], 'static_window':None}, 'reference_sha256':'deployed'}
    (root/'request.json').write_text(json.dumps(record))
    (dest/'result.json').write_text(json.dumps({'action':'history', 'result':{}}))
    path = dest/'history-9-10.npz'
    freq = np.linspace(480,390,8)
    np.savez(path, freq_mhz=freq, **{'2026-10-04_measured':np.ones(8,complex),
        '2026-10-04_candidate_residual':np.zeros(8), '2026-10-04_reference_residual':np.ones(8),
        '2026-10-04_with subtraction_residual':np.ones(8)*2})
    before = path.read_bytes()
    monkeypatch.setattr(history, 'current_reference', lambda settings: {'sha256':'deployed'})
    assert history.reference_label(settings, {'sha256':'deployed'}) == 'Current deployed weights'
    first = history.history_png(settings, 'test', identity, 9, 10)
    assert first.startswith(b'\x89PNG') and path.read_bytes() == before
    monkeypatch.setattr(history, 'current_reference', lambda settings: {'sha256':'new-upload'})
    assert history.reference_label(settings, {'sha256':'deployed'}) == 'Previously deployed weights'
    assert history.history_png(settings, 'test', identity, 9, 10) != first
    with pytest.raises(ValueError): history.history_png(settings, 'test', '../bad', 9, 10)
    with pytest.raises(ValueError): history.history_png(settings, 'test', identity, 9, 99)
    path.rename(dest/'original.npz')
    path.symlink_to(dest/'original.npz')
    with pytest.raises(ValueError, match='symlinks'): history.history_png(settings, 'test', identity, 9, 10)


def test_history_uses_selected_solution_and_current_deployment(tmp_path, monkeypatch):
    import bf_weights_generator
    from bf_weights_generator import sawtooth_compare
    from casm_vis_analysis import calibration_checks, calibration_inspection
    from casm_monitor.jobs import calibration_workspace as worker
    from datetime import datetime, timedelta, timezone
    settings = Settings(store_root=tmp_path/'store')
    day = (datetime.now(timezone.utc)-timedelta(days=2)).strftime('%Y-%m-%d')
    record = {'params':{'tag':'selected', 'source':'sun', 'antennas':[9,10], 'ref_ant':9,
                       'source_window':[day+' 19:00:00',day+' 20:00:00'], 'static_window':None},
              'reference':'old-frozen.h5'}
    loaded=[]
    def load(path):
        loaded.append(path)
        return SimpleNamespace(ant_ids=[9,10])
    monkeypatch.setattr(worker, 'read_json', lambda path: {'paths':{'cal_h5':'selected.h5'}})
    monkeypatch.setattr(bf_weights_generator, 'load_calibration_weights', load)
    monkeypatch.setattr(history, 'current_reference', lambda settings: {'path':'current.h5', 'sha256':'current-hash'})
    now=datetime.now(timezone.utc)-timedelta(days=2)
    monkeypatch.setattr(sawtooth_compare, 'sun_peak_window', lambda *args:(now,now+timedelta(hours=1),None))
    monkeypatch.setattr(calibration_checks, 'read_selected', lambda *args: ({},None,{}))
    def render(datasets, calibrations, reference, dest, **kwargs):
        assert len(datasets)==3 and set(calibrations)=={'candidate','reference'}
        assert kwargs['labels']['candidate_label']=='Without static subtraction'
        assert kwargs['labels']['reference_weights_label']=='Current deployed weights'
        return {'baselines':1}
    monkeypatch.setattr(calibration_inspection, 'render_history', render)
    selection = workspace.action_params({'action':'history', 'date':day, 'days':3, 'compare_subtraction':True}, record)
    assert selection['compare_subtraction'] is False
    result=worker.diagnose(settings,tmp_path,tmp_path,record,'history',selection)
    assert loaded==['selected.h5','current.h5']
    assert result['deployed_reference']['sha256']=='current-hash'
