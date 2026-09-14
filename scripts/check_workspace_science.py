"""Bounded real-data adapter checks; no calibration builds or operational calls.

Historical comparison opens one selected baseline for one hour on each date.
Transit uses only the latest fifteen minutes of native cache, not a full transit
validation. Products and check results remain under the isolated preview root.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

urlopen = build_opener(ProxyHandler({})).open

URL = 'http://127.0.0.1:8061'
ROOT = Path('/home/casm/scratch/casm-observation-preview')


def call(path, body=None):
    req = Request(URL + path, data=None if body is None else json.dumps(body).encode(),
                  headers={'Content-Type': 'application/json', 'Origin': URL,
                           'X-CASM-Workspace': '1'})
    with urlopen(req, timeout=90) as response:
        return json.load(response)


def main():
    catalog = call('/api/science/catalog')
    results = []
    def render(name, path, body):
        start = time.monotonic()
        product = call(path, body)
        for item in product['images']:
            with urlopen(URL + item['url'], timeout=15) as response:
                assert response.read(8) == b'\x89PNG\r\n\x1a\n'
        results.append({'check': name, 'elapsed_s': time.monotonic()-start,
                        'product': product})
        print(name, product['id'], round(results[-1]['elapsed_s'], 2), flush=True)
    selection = dict(pairs=[[8,18]], t0='2026-09-13T19:22:00Z',
                     t1='2026-09-13T20:22:00Z', fmin=410, fmax=470,
                     kind='phase_spectrum', reference='sun', resolution='recorded',
                     compare_t0='2026-09-03T19:22:00Z', compare_t1='2026-09-03T20:22:00Z')
    render('historical phase comparison, mechanics only', '/api/science/render', selection)
    native = [row for row in catalog['availability'] if 'full' in row['stream']]
    assert native, 'Native cache unavailable'
    end = min(time.time()-300, max(row['t1'] for row in native))
    for kind in ('amplitude_waterfall', 'amplitude_spectrum', 'autos'):
        render(kind, '/api/science/render', dict(pairs=[[8,8]] if kind=='autos' else [[8,18]],
               t0=end-900, t1=end, fmin=410, fmax=470, kind=kind,
               reference='raw', resolution='full'))
    render('short stationary Cyg A adapter check, not a full transit', '/api/science/transit',
           dict(calibration_id=catalog['transit_calibrations'][0]['id'], t0=end-900, t1=end,
                fixed_alt_deg=86.5, fixed_az_deg=0, control_alt_deg=70, control_az_deg=90,
                fmin=410, fmax=470))
    try:
        now = time.time()
        render('cached SNAP hour', '/api/snap-workspace/render', dict(packet_idx=8,
               t0=datetime.fromtimestamp(now-3600,timezone.utc).isoformat(),
               t1=datetime.fromtimestamp(now,timezone.utc).isoformat(),fmin=410,fmax=470))
    except HTTPError as exc:
        detail = exc.read().decode()
        if exc.code != 404:
            raise RuntimeError(detail) from exc
        results.append({'check':'cached SNAP hour','status':'unavailable','detail':detail})
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT/'science-validation.json').write_text(json.dumps(
        {'checked_utc':datetime.now(timezone.utc).isoformat(), 'results':results}, indent=2))


if __name__ == '__main__':
    main()
