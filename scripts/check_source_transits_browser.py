"""Read-only preview acceptance for source-tracking visibility beam galleries."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from check_visibility_ticks import no_overlap

URL = 'http://127.0.0.1:8061'
OUT = Path('/home/casm/scratch/casm-observation-preview')


def main():
    (OUT/'screenshots').mkdir(parents=True,exist_ok=True)
    report = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport=dict(width=1500,height=1100))
        errors = []
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(URL+'/sources')
        page.locator('.history-entry').first.wait_for()
        for query,name in [('Sun','Sun'),('cyg-a','Cyg A')]:
            catalog = page.request.get(URL+'/api/sources',params=dict(q=query)).json()
            assert catalog['kind'] == 'visibility_beam' and catalog['transits']
            if name == 'Sun':
                page.get_by_role('button',name='Sun',exact=True).click()
            else:
                page.get_by_label('Source',exact=True).fill(query)
                page.get_by_role('button',name='Search',exact=True).click()
            expected = len(catalog['transits'])
            page.wait_for_function('(n)=>document.querySelectorAll(".transit-entry .array-dynamic > img").length===n',
                                   arg=expected,timeout=120000)
            page.wait_for_function('Array.from(document.querySelectorAll(".array-dynamic > img")).every(i=>i.naturalWidth>0)')
            assert page.locator('.history-entry').count() == expected
            assert catalog['calibration']['name'] in page.locator('.history-cal').inner_text()
            assert page.get_by_label('Show',exact=True).count() == 0
            for card in page.locator('.transit-entry').all():
                assert 'Frequency (MHz)' in card.inner_text()
                assert 'Beam cross-power (weighted counts)' in card.inner_text()
                assert 'Real(V)' not in card.inner_text()
                assert card.locator('.dynamic-marker').count() == 1
                assert no_overlap(card.locator('.dynamic-frequency > span'),'y') >= 8
                assert no_overlap(card.locator('.dynamic-time > span'),'x') >= 3
            first = page.locator('.transit-entry').first
            first.locator('summary').click()
            assert 'autos excluded' in first.inner_text()
            assert 'not Jy' in first.inner_text()
            first.locator('summary').click()
            page.screenshot(path=str(OUT/'screenshots'/f'source-transits-{catalog["source"]}.png'))
            first.locator('.plot-zoom-trigger').click()
            dialog = page.get_by_role('dialog')
            assert dialog.locator('.dynamic-marker').count() == 1
            assert 'cross-power only' in dialog.inner_text()
            page.get_by_role('button',name='Zoom in',exact=True).click()
            assert page.get_by_label('Display zoom').inner_text() == '150%'
            page.keyboard.press('Escape')
            page.set_viewport_size(dict(width=390,height=844))
            page.wait_for_timeout(150)
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1')
            no_overlap(first.locator('.dynamic-time > span'),'x')
            page.screenshot(path=str(OUT/'screenshots'/f'source-transits-{catalog["source"]}-mobile.png'))
            report[catalog['source']] = dict(dates=expected,calibration=catalog['calibration']['name'],
                utc_dates=[r['date'] for r in catalog['transits']],stream=catalog['coverage']['stream'])
            page.set_viewport_size(dict(width=1500,height=1100))
        page.get_by_role('button',name='B0329',exact=True).click()
        page.get_by_label('Show',exact=True).wait_for()
        assert page.locator('.history-entry').count() == 13
        assert 'PDMP folds from filterbank dumps' in page.locator('.history-caption').inner_text()
        # Missing calibration must not leave a permanent loading indicator.
        page.route('**/api/sources?q=Sun',lambda route:route.fulfill(json={**catalog,
            'source':'sun','name':'Sun','calibration':None,'state':'unavailable'}))
        page.get_by_role('button',name='Sun',exact=True).click()
        page.get_by_text('Current calibration unavailable.',exact=True).wait_for()
        assert page.get_by_text('Loading beam…',exact=True).count() == 0
        assert not errors,errors
        browser.close()
    (OUT/'source-transits-browser.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
    print('Passed: beam dates, axes, calibration, zoom, mobile, missing-cal state and B0329 separation')


if __name__ == '__main__':
    main()
