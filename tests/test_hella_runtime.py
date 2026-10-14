import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from casm_monitor.hella_runtime_probe import parse_config, inspect_process
from casm_monitor.collectors.hella_runtime import HellaRuntimeCollector
from casm_monitor.web.hella_config import configuration, describe

CFG = '''DM_MIN 0
DM_MAX 1000
SNR 10
WIDTH_MIN 1
WIDTH_MAX 128
WIDTH_RATIO 2
GULP 8192
NBEAM 64
FLAG1 -1
FLAG2 -1
SPEC_MIN -0.05
SPEC_MAX 1
FLAG1 0
FLAG2 472
SCRUNCH 1
1 1 1000000 1
SUMTHRESHOLD 8 1.5 16 512 4
'''


def row(stream=0, text=CFG, **overrides):
    values, scrunches = parse_config(text)
    return dict(stream=stream, status='running', values=values, scrunches=scrunches, **overrides)


class Reader:
    def __init__(self, now=1000):
        self.samples = {f'hella.runtime.{node}': {'ts': now, 'value': json.dumps({
            'streams': [row(i) for i in streams]})}
            for node, streams in [('corr1',range(4)),('corr2',range(4,8))]}

    def latest_scalar(self, name):
        return self.samples.get(name)


def test_wrapper_overrides_and_mask_exclusive_upper_channel():
    settings = describe(row())
    assert settings['snr'] == 10
    assert settings['mask']['channels'] == 472
    assert settings['mask']['first'] == 0
    assert settings['mask']['last'] == 471
    assert settings['mask']['freq_low_mhz'] == pytest.approx(470.001220703125)
    assert settings['mask']['freq_high_mhz'] == 484.375
    assert settings['gulp_s'] == pytest.approx(8.589934592)
    assert settings['widths'] == [1, 2, 4, 8, 16, 32, 64]
    assert settings['sumthreshold']['enabled'] is True


def test_disabled_mask_and_flagger_and_default_only_flagger():
    plain = CFG.replace('FLAG1 0\nFLAG2 472\n','').replace('SUMTHRESHOLD 8 1.5 16 512 4\n','')
    assert describe(row(text=plain))['mask']['enabled'] is False
    assert describe(row(text=plain))['sumthreshold']['enabled'] is False
    assert describe(row(text=plain+'SUMTHRESHOLD\n'))['sumthreshold']['enabled'] is True
    assert describe(row(text=CFG.replace('SCRUNCH 1\n1 1 1000000 1','SCRUNCH 0')))['mask']['enabled'] is False


def test_scrunch_parser_and_no_unrelated_config_exposure():
    values, scrunches = parse_config('HOST private\nTOKEN secret\n'+CFG+'SNR 14 # latest\n')
    assert values['SNR'] == '14'
    assert 'TOKEN' not in values and 'HOST' not in values
    assert scrunches == ['1 1 1000000 1']
    with pytest.raises(ValueError):
        parse_config('SCRUNCH 2\n1 1 2 1\n')


def test_matching_stale_missing_and_failed_nodes():
    reader = Reader()
    assert configuration(reader, now=1010)['all_match']
    stale = configuration(reader, now=1181)
    assert stale['verified_count'] == 0 and stale['common'] is None
    assert all(s['settings'] is None for s in stale['streams'])
    del reader.samples['hella.runtime.corr2']
    assert configuration(reader, now=1010)['verified_count'] == 4
    reader.samples['hella.runtime.corr2'] = {'ts':1000,'value':json.dumps({'error':'ssh failed','streams':[]})}
    assert configuration(reader, now=1010)['streams'][4]['status'] == 'unknown'


def test_different_streams_and_unrecognised_process():
    reader = Reader()
    data = json.loads(reader.samples['hella.runtime.corr2']['value'])
    data['streams'][0]['values']['SNR'] = '13'
    reader.samples['hella.runtime.corr2']['value'] = json.dumps(data)
    result = configuration(reader, now=1010)
    assert not result['all_match'] and result['common'] is None
    assert result['streams'][4]['settings']['snr'] == 13
    data['streams'][0]['status'] = 'unknown'
    reader.samples['hella.runtime.corr2']['value'] = json.dumps(data)
    assert configuration(reader, now=1010)['streams'][4]['settings'] is None


def test_missing_duplicate_and_baseline_difference():
    reader = Reader()
    data = json.loads(reader.samples['hella.runtime.corr2']['value'])
    data['streams'].pop()
    data['streams'].append(data['streams'][0])
    reader.samples['hella.runtime.corr2']['value'] = json.dumps(data)
    result = configuration(reader, now=1010)
    assert result['streams'][4]['status'] == 'unknown'
    assert result['streams'][7]['status'] == 'stopped'
    reader = Reader()
    data = json.loads(reader.samples['hella.runtime.corr2']['value'])
    data['streams'][0]['values']['BASELINE_MODE'] = '2'
    reader.samples['hella.runtime.corr2']['value'] = json.dumps(data)
    assert not configuration(reader, now=1010)['all_match']


def test_config_changed_since_launch_is_unknown(monkeypatch):
    stat = '123 (casm hella) S ' + ' '.join(['0']*18+['1000']+['0']*5)
    monkeypatch.setattr(Path,'read_bytes',lambda p: b'/some/casm_hella_refactored\0-c\0/tmp/hella_0.cfg\0')
    monkeypatch.setattr(Path,'read_text',lambda p: stat if p.name=='stat' else CFG)
    monkeypatch.setattr(Path,'stat',lambda p: SimpleNamespace(st_size=500,st_mtime=1050,st_mtime_ns=1050000000000,st_ino=1))
    monkeypatch.setattr('os.readlink',lambda p:'/home/casm/software/vishnu/casm_hella_vishnu/build/src/apps/casm_hella_refactored')
    r = inspect_process(Path('/proc/123'),boot_unix=1000,ticks=100)
    assert r['started_unix'] == 1010
    assert r['status'] == 'unknown'
    assert 'after launch' in r['reason']


def test_collector_failure_overwrites_old_success(monkeypatch):
    class Settings:
        corr2_ssh = 'casm-corr2'
        def cadence(self,name,default): return default
    writes = []
    ctx = SimpleNamespace(scalar=lambda name,value:writes.append((name,json.loads(value))))
    monkeypatch.setattr('casm_monitor.collectors.hella_runtime.run',lambda *a,**k:(1,'','failure'))
    HellaRuntimeCollector(Settings(),node='corr2').collect(ctx)
    assert writes[0][0] == 'hella.runtime.corr2'
    assert writes[0][1]['streams'] == []
    assert writes[0][1]['error']


def test_endpoint_only_reads_saved_state(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from casm_monitor.web.t1 import build_router
    import time
    reader = Reader(now=time.time())
    app = FastAPI()
    app.include_router(build_router(reader,SimpleNamespace(observation_cache_root=tmp_path)))
    with TestClient(app) as client:
        response = client.get('/api/t1/configuration')
    assert response.status_code == 200
    assert response.json()['all_match'] is True
