"""Saved Cyg A transit acceptance. Abort every non-GET API request."""
import hashlib
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    base = 'http://127.0.0.1:8061'
    tag = 'sun_20261004_181133_regular_b65ba0ba'
    identity = 'c6c6627f5c9e4cb9ba29328d30c8d861'
    root = Path('/home/casm/scratch/casm-observation-preview')
    output = root/'screenshots'/'calibration-transit-20261005'
    output.mkdir(parents=True, exist_ok=True)
    action = root/'calibration'/'cal_builds'/tag/'actions'/identity
    originals = {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in action.iterdir() if p.is_file()}
    errors, writes, failed = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width':1600, 'height':1100}, accept_downloads=True)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('response', lambda response: failed.append((response.status,response.url)) if response.status>=400 else None)
        def guard(route):
            if route.request.method not in ('GET','HEAD'):
                writes.append(route.request.url)
                route.abort()
            else:
                route.continue_()
        page.route('**/api/**', guard)
        page.goto(f'{base}/cal/{tag}', wait_until='domcontentloaded')
        navigation = page.get_by_role('navigation', name='Calibration step navigation', exact=True)
        navigation.get_by_role('button', name='Proceed to Validate on sky').click(timeout=60000)
        section = page.locator('.cal-transit-review')
        expect(section).to_contain_text('37 integrations · 11 before / 26 after', timeout=60000)
        expect(section).to_contain_text('Excluded 15 Sun-up integrations')
        info = page.request.get(f'{base}/api/calibration/builds/{tag}/transit/{identity}').json()
        assert all(item['available'] for item in info['items']) and not info['notices']
        assert any(item['label']=='Current deployed weights' for item in info['items'])

        def loaded():
            # ProductImage uses native lazy loading; load every currently shown plot.
            for img in section.locator('img').all():
                img.scroll_into_view_if_needed()
                expect(img).to_be_visible()
                img.evaluate('(i) => i.loading="eager"')
            page.wait_for_function('''() => [...document.querySelectorAll('.cal-transit-review img')].every(i=>i.complete&&i.naturalWidth>0)''', timeout=60000)
        loaded()
        section.locator('.cal-transit-background').scroll_into_view_if_needed()
        page.screenshot(path=str(output/'transit-page.png'))
        first = section.locator('.plot-image-trigger').first
        first.click()
        expect(page.locator('.figure-modal')).to_be_visible()
        with page.expect_download() as download:
            page.locator('.figure-modal').get_by_role('button', name='Download', exact=True).click()
        download.value.save_as(str(output/'calibration-comparison.png'))
        page.keyboard.press('Escape')
        clock = section.get_by_role('group', name='Background time zone')
        clock.get_by_role('button', name='UTC', exact=True).click()
        expect(section).to_contain_text('Sep 25, 01:43 UTC')
        clock.get_by_role('button', name='OVRO local').click()
        expect(section).to_contain_text('Sep 24, 18:43 PDT')
        section.get_by_role('button', name='Raw sum', exact=True).click()
        expect(section.locator('img').first).to_have_attribute('src', f'/api/calibration/builds/{tag}/transit/{identity}/calibration.png?scale=sum', timeout=60000)
        loaded()
        section.get_by_role('button', name='Weighted mean', exact=True).click()
        expect(section.locator('img').first).to_have_attribute('src', f'/api/calibration/builds/{tag}/transit/{identity}/calibration.png?scale=mean', timeout=60000)
        for name in ('Inverse variance', 'Inverse standard deviation', 'Uniform'):
            button = section.get_by_role('group', name='Amplitude transit spectrum').get_by_role('button', name=name, exact=True)
            assert button.bounding_box()['height'] >= 42
            button.click()
            loaded()
        section.locator('summary', has_text='Optional diagnostic').click()
        loaded()
        assert section.locator('img[src*="/timing.png"]').count()==3
        for kind,series in [('amplitude',''), ('transit','amp-inv_std'), ('timing','cal-0')]:
            response = page.request.get(f'{base}/api/calibration/builds/{tag}/transit/{identity}/{kind}.png?series={series}')
            assert response.ok
            (output/(kind+'.png')).write_bytes(response.body())
        for width in (1280,768,390):
            page.set_viewport_size({'width':width,'height':1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
        section.locator('.cal-transit-background').scroll_into_view_if_needed()
        page.screenshot(path=str(output/'transit-mobile.png'))
        assert not errors and not writes and not failed, (errors,writes,failed)
        assert originals == {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in originals}
        report = dict(review=info, original_hashes={str(p):digest for p,digest in originals.items()},
                      unchanged_saved_files=True, zoom_download=True, scale_toggle=True,
                      scheme_selection=True, optional_timing=True, mobile_no_overflow=True,
                      browser_errors=errors, non_get_requests=writes, failed_responses=failed)
        (output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('review','original_hashes')}))
        browser.close()


if __name__=='__main__':
    main()
