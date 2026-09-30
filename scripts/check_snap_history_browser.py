"""Live saved-data UI acceptance; block ALL non-GET requests, no hardware reads."""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL='http://127.0.0.1:8061'
OUT=Path('/home/casm/scratch/casm-observation-preview/screenshots')


def capture(page, name):
    if '--no-screenshots' not in sys.argv:
        page.screenshot(path=str(OUT/name),full_page=True,timeout=15000)


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1500,'height':1100},accept_downloads=True)
        errors,mutations=[],[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        def guard(route):
            if route.request.method!='GET':
                mutations.append(route.request.url)
                route.abort()
            else:
                route.continue_()
        page.route('**/api/**',guard)
        page.goto(URL+'/snaps')
        page.locator('.snap-spectrum-card').first.wait_for()
        page.wait_for_function(r"() => /[0-9]+\/[0-9]+ checked/.test(document.querySelector('.snap-changes-heading').textContent)")
        assert page.get_by_role('button',name='Live',exact=True).get_attribute('aria-pressed')=='true'
        assert page.locator('.snap-spectrum-card').count()==16
        capture(page,'snaps-live-changes.png')
        page.get_by_role('button',name='History',exact=True).click()
        page.get_by_role('button',name='Open this read',exact=True).click(timeout=60000)
        dates=page.get_by_label('Spectrum date (UTC)',exact=True)
        options=[d['day'] for d in page.request.get(URL+'/api/snap-workspace/spectra-days').json()['days']]
        assert '2026-09-09' in options
        dates.fill(options[-1])
        times=page.get_by_label('Spectrum saved time',exact=True)
        page.wait_for_function("() => document.querySelector('.snap-time-heading').textContent.includes('Read')")
        page.locator('.snap-spectrum-card').first.wait_for()
        assert options[-1] in page.locator('.snap-spectrum-card').first.locator('footer').inner_text()
        assert 'STALE' not in page.locator('.snap-spectrum-card').first.locator('footer').inner_text()
        times.focus()
        page.keyboard.press('Home')
        page.wait_for_timeout(300)
        page.get_by_role('button',name='Spectrum next read',exact=True).click()
        page.wait_for_function("() => document.querySelector('[aria-label=\"Spectrum saved time\"]').value==='1'")
        page.get_by_role('button',name='Spectrum previous read',exact=True).click()
        page.wait_for_function("() => document.querySelector('[aria-label=\"Spectrum saved time\"]').value==='0'")
        page.get_by_role('button',name='Spectrum next read',exact=True).click()
        page.wait_for_function("() => document.querySelector('[aria-label=\"Spectrum saved time\"]').value==='1'")
        page.get_by_role('button',name='Compare with…',exact=True).click()
        page.get_by_label('Comparison saved time',exact=True).wait_for()
        page.locator('.snap-reference').first.wait_for()
        assert page.get_by_label('Comparison saved time',exact=True).input_value()=='0'
        plots=page.locator('.snap-spectrum-card svg')
        assert len(set(plots.evaluate_all("es=>es.map(e=>e.dataset.yMin+','+e.dataset.yMax)")))==1
        capture(page,'snaps-history-comparison.png')
        page.get_by_role('button',name='Expand SNAP 0 ADC 8',exact=True).click()
        dialog=page.get_by_role('dialog')
        dialog.wait_for()
        dialog.locator('.snap-trend-point').first.wait_for()
        assert dialog.locator('.snap-reference').count()==1
        with page.expect_download() as saved:
            dialog.get_by_role('button',name='Download',exact=True).click()
        download=saved.value
        assert download.suggested_filename.endswith('.png')
        dialog.locator('.snap-trend-point').first.focus()
        page.keyboard.press('Enter')
        page.get_by_role('dialog').locator('.snap-trend-point').first.wait_for()
        page.keyboard.press('Escape')
        page.get_by_role('button',name='Live',exact=True).click()
        page.locator('.snap-spectrum-card').first.wait_for()
        page.get_by_role('button',name='Close comparison',exact=True).click()
        # Exercise the change-to-comparison path when a real check is flagged.
        fine=page.locator('.snap-fine-details summary')
        if fine.count(): fine.click()
        change=page.locator('.snap-fine-list button').first
        inspected=False
        if change.count():
            change.click()
            page.get_by_role('dialog').wait_for()
            page.get_by_role('dialog').locator('.snap-reference').wait_for()
            page.get_by_role('dialog').get_by_role('img',name='Selected minus reference (dB) versus frequency (MHz)').wait_for()
            assert 'exact per-channel median' in page.get_by_role('dialog').inner_text()
            assert page.get_by_role('button',name='Live',exact=True).get_attribute('aria-pressed')=='true'
            capture(page,'snaps-change-comparison.png')
            inspected=True
            page.keyboard.press('Escape')
        page.get_by_role('button',name='History',exact=True).click()
        page.get_by_role('button',name='Open this read',exact=True).click(timeout=60000)
        for width in [390,320]:
            page.set_viewport_size({'width':width,'height':900})
            page.wait_for_timeout(300)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
            assert page.locator('.snap-spectrum-card').first.locator('svg').bounding_box()['width']>=250
        capture(page,'snaps-history-mobile.png')
        assert not errors and not mutations,(errors,mutations)
        print(json.dumps(dict(saved_days=len(options),earliest=options[-1],latest=options[0],
            historical_grid=True,comparison=True,trend_navigation=True,download=True,
            change_comparison=inspected,mobile_widths=[390,320],browser_errors=errors,mutations=mutations)))
        browser.close()


if __name__=='__main__':
    main()
