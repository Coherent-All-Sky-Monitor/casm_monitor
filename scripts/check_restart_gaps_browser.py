"""Read-only live smoke check of post-restart plots; no acquisition requests."""
import base64
import io
import json
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright


BASE = 'http://127.0.0.1:8061'
OUT = Path('/home/casm/scratch/casm-observation-preview/restart-gap-check')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results, errors, writes = {}, [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1600, 'height':1100})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: writes.append(r.url) if r.method != 'GET' else None)
        for quantity in ['phase', 'amp', 'real', 'imag', 'power_db', 'power_linear']:
            query = dict(mode='auto' if quantity.startswith('power_') else 'cross',
                         reference='raw', reference_input=8, hours=24, images=quantity)
            response = page.request.get(BASE+'/api/science/array?'+urlencode(query), timeout=90000)
            assert response.ok, (quantity, response.status, response.text()[:200])
            body = response.json()
            tile = (body['panels'][0]['power_images'][quantity.removeprefix('power_')]
                    if quantity.startswith('power_') else body['panels'][0]['images'][quantity])
            assert abs(tile['time_start']-(body['t0']-body['integration_s']/2)) < .001
            assert abs(tile['time_end']-(body['t1']+body['integration_s']/2)) < .001
            pixels = np.asarray(Image.open(io.BytesIO(base64.b64decode(tile['src'].split(',')[1]))))
            blank = np.all(pixels == [29,35,43], axis=(0,2))
            assert np.any(~blank), 'No visible samples'
            results[quantity] = dict(samples=body['samples'], t0=body['t0'], t1=body['t1'],
                                     panels=len(body['panels']), blank_columns=int(blank.sum()), width=tile['width'])
        pair_query = dict(pairs='8:9', t0=body['t0'], t1=body['t1'], quantity='phase', reference='raw')
        pair = page.request.get(BASE+'/api/science/array/pairs?'+urlencode(pair_query), timeout=90000)
        assert pair.ok, pair.text()[:200]
        assert pair.json()['panels'][0]['tile']['time_start'] is not None
        page.goto(BASE+'/vis', wait_until='domcontentloaded')
        page.locator('.antenna-plot').first.wait_for(timeout=90000)
        page.get_by_role('button', name='Cross-correlations', exact=True).click()
        page.locator('.array-dynamic img').first.wait_for(timeout=90000)
        page.wait_for_function('Array.from(document.querySelectorAll(".array-dynamic img")).every(i=>i.complete&&i.naturalWidth>0)')
        assert 'Irregular timestamps' not in page.locator('body').inner_text()
        page.screenshot(path=str(OUT/'visibilities.png'))
        page.locator('.plot-zoom-trigger').first.click()
        page.get_by_role('dialog').wait_for()
        page.screenshot(path=str(OUT/'visibility-expanded.png'))
        page.keyboard.press('Escape')
        page.goto(BASE+'/snaps', wait_until='domcontentloaded')
        page.locator('.snap-spectrum-card svg').first.wait_for(timeout=60000)
        page.screenshot(path=str(OUT/'snaps.png'))
        assert not errors, errors
        assert not writes, writes
        browser.close()
    result = dict(views=results, all_pairs='passed', browser_errors=errors, writes=writes)
    (OUT/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
