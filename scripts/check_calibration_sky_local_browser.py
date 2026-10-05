"""Check Generate beams' local-day illustration without submitting any jobs."""
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    base = 'http://127.0.0.1:8061'
    tag = 'sun_20261004_181133_regular_b65ba0ba'
    output = Path('/home/casm/scratch/casm-observation-preview/screenshots/calibration-sky-local-20261005')
    output.mkdir(parents=True, exist_ok=True)
    errors, writes, frames = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width':1600, 'height':1100}, reduced_motion='reduce',
                                      timezone_id='Asia/Tokyo', accept_downloads=True)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))

        def guard(route):
            if route.request.method not in ('GET', 'HEAD'):
                writes.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        page.route('**/api/**', guard)
        page.goto(f'{base}/cal/{tag}', wait_until='domcontentloaded')
        expect(page.locator('#cal-history')).to_be_visible(timeout=60000)
        navigation = page.get_by_role('navigation', name='Calibration step navigation', exact=True)
        expect(navigation).to_contain_text('Without static subtraction')
        expect(navigation).to_be_in_viewport()
        page.evaluate('window.scrollTo(0, 2500)')
        expect(navigation).to_be_in_viewport()
        page.screenshot(path=str(output/'proceed-inspect.png'))
        navigation.get_by_role('button', name='Proceed to Validate on sky').click()
        expect(page.locator('.cal-steps [aria-current="step"]')).to_contain_text('Validate on sky')
        expect(page.locator('.cal-steps [aria-current="step"]')).to_be_focused()
        expect(page.locator('.cal-steps')).to_be_in_viewport()
        settings = page.locator('details').filter(has=page.locator('summary', has_text='Transit settings'))
        if settings.get_attribute('open') is None:
            settings.locator(':scope > summary').click()
        page.get_by_label('Validation source').select_option('cas-a')
        page.get_by_label('Transit date · UTC').fill('2026-10-03')
        url = f'{base}/api/calibration/builds/{tag}/sky?date=2026-10-04'
        with page.expect_response(url, timeout=60000) as response:
            navigation.get_by_role('button', name='Proceed to Generate beams').click()
        expect(page.locator('.cal-steps [aria-current="step"]')).to_contain_text('Generate beams')
        assert page.url.endswith('/cal/'+tag)
        info = response.value.json()
        assert info['date']=='2026-10-04' and info['time_zone']=='America/Los_Angeles'
        assert info['time_label']=='OVRO local time (PDT)'
        assert info['window_local']==['2026-10-04T00:00:00-07:00','2026-10-05T00:00:00-07:00']
        assert info['window_utc']==['2026-10-04T07:00:00+00:00','2026-10-05T07:00:00+00:00']
        frames.append(info)
        sky = page.locator('.cal-sky')
        expect(sky.get_by_label('Track date · OVRO local')).to_have_value('2026-10-04')
        assert 'UTC' not in sky.inner_text()
        page.wait_for_function('''() => {const img=document.querySelector('.cal-sky img');return img&&img.complete&&img.naturalWidth>0;}''', timeout=60000)
        sky.screenshot(path=str(output/'local-sky.png'))
        sky.locator('.plot-image-trigger').click()
        expect(page.locator('.figure-modal')).to_be_visible()
        with page.expect_download() as download:
            page.locator('.figure-modal').get_by_role('button', name='Download', exact=True).click()
        download.value.save_as(str(output/'local-sky-expanded.png'))
        page.keyboard.press('Escape')
        with page.expect_response(f'{base}/api/calibration/builds/{tag}/sky?date=2026-01-15', timeout=60000) as response:
            sky.get_by_label('Track date · OVRO local').fill('2026-01-15')
        winter = response.value.json()
        assert winter['time_label']=='OVRO local time (PST)'
        assert winter['window_utc']==['2026-01-15T08:00:00+00:00','2026-01-16T08:00:00+00:00']
        frames.append(winter)
        page.wait_for_timeout(4500)
        expect(sky.get_by_label('Track date · OVRO local')).to_have_value('2026-01-15')
        for width in (1280, 768, 390):
            page.set_viewport_size({'width':width, 'height':1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            expect(navigation).to_be_in_viewport()
        page.screenshot(path=str(output/'proceed-mobile.png'))
        navigation.get_by_role('button', name='Proceed to Upload dry run').click()
        expect(page.locator('.cal-steps [aria-current="step"]')).to_contain_text('Upload dry run')
        expect(page.locator('.cal-steps [aria-current="step"]')).to_be_in_viewport()
        expect(navigation.get_by_role('button', name='Proceed to')).to_have_count(0)
        navigation.get_by_role('button', name='Back to Generate beams').click()
        navigation.get_by_role('button', name='Back to Validate on sky').click()
        expect(page.locator('.cal-steps [aria-current="step"]')).to_be_in_viewport()
        settings = page.locator('details').filter(has=page.locator('summary', has_text='Transit settings'))
        if settings.get_attribute('open') is None:
            settings.locator(':scope > summary').click()
        expect(page.get_by_label('Validation source')).to_have_value('cas-a')
        expect(page.get_by_label('Transit date · UTC')).to_have_value('2026-10-03')
        navigation.get_by_role('button', name='Back to Solve & inspect').click()
        expect(page.locator('#cal-history')).to_contain_text('Without static subtraction')
        assert page.url.endswith('/cal/'+tag)
        assert not errors and not writes, (errors, writes)
        report = dict(time_zone='America/Los_Angeles', local_dates=True, daylight_saving=True,
                      zoom_download=True, proceed_back=True, preserved_solution_and_settings=True,
                      browser_errors=errors, non_get_requests=writes,
                      frames=frames)
        (output/'result.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report))
        browser.close()


if __name__=='__main__':
    main()
