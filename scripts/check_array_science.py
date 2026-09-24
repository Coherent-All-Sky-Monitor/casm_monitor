"""Compare live array views with independent arithmetic on the read-only cache.

Writes only a local acceptance report and bounded diagnostic plot artifacts.
Never contacts acquisition or changes calibration/deployment.
"""
import gzip
import json
import time
import urllib.request
from pathlib import Path

import numpy as np

from casm_monitor.config import load_settings
from casm_monitor.collectors.vis import STREAM_AVG8
from casm_monitor.store import Store
from casm_monitor.web.vis import VisStore

BASE = 'http://127.0.0.1:8061'
REPORT = Path('/home/casm/scratch/casm-observation-preview/array-visibility-audit.json')


def request(path, body=None):
    headers = {'Content-Type': 'application/json', 'Origin': BASE, 'X-CASM-Workspace': '1'}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    start = time.monotonic()
    with opener.open(urllib.request.Request(BASE + path, headers=headers,
                     data=None if body is None else json.dumps(body).encode()), timeout=90) as response:
        raw = response.read()
        if response.headers.get('Content-Encoding') == 'gzip':
            raw = gzip.decompress(raw)
    return json.loads(raw), time.monotonic() - start


def quantities(z, mean=False):
    value = np.nanmean(z, axis=0, dtype=np.complex128) if mean else z
    amplitude = np.nanmean(np.abs(z), axis=0, dtype=np.float64) if mean else np.abs(z)
    angle = np.where(np.abs(value) > 0, np.angle(value), np.nan)
    return dict(real=value.real, imag=value.imag, amp=amplitude, phase=angle)


def main():
    settings = load_settings()
    reader = Store(settings.db_path, read_only=True)
    store = VisStore(settings, reader)
    report = dict(snapshots=[], figures=[], result='passed')
    for mode in ('auto', 'cross'):
        data, elapsed = request('/api/science/array?mode=' + mode)
        _, warm = request('/api/science/array?mode=' + mode)
        pairs = [tuple(p['pair']) for p in data['panels']]
        z, stamps, freq, _ = store.series(STREAM_AVG8, data['t0'], data['t1'], pairs, allow_full_fallback=False)
        mask = (freq >= min(data['freq_mhz'])) & (freq <= max(data['freq_mhz']))
        z, freq = z[:, :, mask], freq[mask]
        np.testing.assert_equal(freq, data['freq_mhz'])
        assert np.all(np.diff(freq) < 0), 'Raster assumes descending cached frequency'
        assert len(stamps) == data['samples']
        expected = {'latest': quantities(z[-1]), 'mean': quantities(z, mean=True)}
        checks = 0
        for k, panel in enumerate(data['panels']):
            reverse = mode == 'cross' and panel['input'] > data['reference_input']
            for statistic, values in expected.items():
                for q, v in values.items():
                    sign = -1 if reverse and q in ('imag', 'phase') else 1
                    actual = np.array(panel['spectra'][q][statistic], dtype=float)
                    np.testing.assert_allclose(actual, sign*v[k], rtol=2e-7, atol=1e-8, equal_nan=True)
                    checks += 1
            expected_width = round((stamps[-1]-stamps[0])/data['integration_s']) + 1
            assert all(tile['width'] == expected_width for tile in panel['images'].values())
        report['snapshots'].append(dict(mode=mode, seconds=elapsed, warm_seconds=warm,
            panels=len(pairs), default_antennas=len(data['default_inputs']), samples=len(stamps),
            spectrum_checks=checks, missing_integrations=expected_width-len(stamps),
            t0=data['t0'], t1=data['t1'], frequency_channels=len(freq)))
        ids = sorted(a['packet_idx'] for a in data['inputs'])
        matrix_pairs = [(i,j) for n,i in enumerate(ids) for j in ids[n:]]
        mz, _, mf, _ = store.series(STREAM_AVG8, data['t1'], data['t1'], matrix_pairs, allow_full_fallback=False)
        mf_mask = (mf >= min(freq)) & (mf <= max(freq))
        expected_matrix = quantities(mz[0][:,mf_mask].T, mean=True)
        rank = {a['packet_idx']: n for n,a in enumerate(data['inputs'])}
        for q, values in expected_matrix.items():
            matrix = np.array(data['matrix'][q], dtype=float)
            for value, (i,j) in zip(values, matrix_pairs):
                np.testing.assert_allclose(matrix[rank[i],rank[j]], value, rtol=2e-7, atol=1e-8, equal_nan=True)
                sign = -1 if i != j and q in ('phase','imag') else 1
                np.testing.assert_allclose(matrix[rank[j],rank[i]], sign*value, rtol=2e-7, atol=1e-8, equal_nan=True)
        report['snapshots'][-1]['matrix_pairs_checked'] = len(matrix_pairs)
        print(json.dumps(report['snapshots'][-1]), flush=True)

    # Exercise each detailed renderer with current real data and inspect its evidence.
    for kind in ('autos', 'amplitude_spectrum', 'phase_spectrum', 'amplitude_waterfall',
                 'real_spectrum', 'imag_spectrum', 'real_waterfall', 'imag_waterfall', 'phase_waterfall'):
        auto = kind == 'autos'
        pairs = [(p, p) for p in data['default_inputs'][:6]] if auto else [(8, 18), (0, 26)]
        product, elapsed = request('/api/science/render', dict(pairs=pairs, t0=data['t0'], t1=data['t1'],
            kind=kind, reference='raw', resolution='avg8', spectrum_statistic='latest'))
        assert product['provenance']['normalization'] == 'none'
        assert product['images']
        report['figures'].append(dict(kind=kind, seconds=elapsed, id=product['id'],
                                      images=product['images'], provenance=product['provenance']))
        print(kind + ': passed', flush=True)
    day, elapsed = request('/api/science/array?mode=cross&hours=24')
    report['day_snapshot'] = dict(samples=day['samples'], seconds=elapsed, panels=len(day['panels']))
    assert len(day['default_inputs']) == 17
    sun, elapsed = request('/api/science/array?mode=cross&reference=sun')
    assert sun['reference'] == 'sun' and len(sun['panels']) == 24
    report['sun_snapshot'] = dict(samples=sun['samples'], seconds=elapsed)
    REPORT.write_text(json.dumps(report, indent=2) + '\n')
    print(str(REPORT), flush=True)


if __name__ == '__main__':
    main()
