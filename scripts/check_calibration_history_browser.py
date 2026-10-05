"""Saved-data cross-day UI acceptance; the submit click is intercepted locally."""
import json
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    tag = 'sun_20261004_181133_regular_b65ba0ba'
    out = Path('/home/casm/scratch/casm-observation-preview/screenshots/calibration-history-20261005')
    out.mkdir(parents=True, exist_ok=True)
    errors, intercepted = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1600, 'height':1100}, accept_downloads=True)
        page.on('pageerror', lambda e: errors.append(str(e)))
        def guard(route):
            if route.request.method not in ('GET', 'HEAD'):
                intercepted.append({'url':route.request.url, 'body':route.request.post_data_json})
                route.fulfill(json={'tag':tag})  # No request reaches the server.
            else:
                route.continue_()
        page.route('**/api/**', guard)
        page.goto(f'http://127.0.0.1:8061/cal/{tag}', wait_until='domcontentloaded')
        section = page.locator('#cal-history')
        expect(section).to_contain_text('Without static subtraction', timeout=60000)
        expect(section.get_by_label('Latest day · UTC')).to_have_value('2026-10-04')
        assert page.get_by_text('Compare both subtraction solutions in cross-day', exact=False).count()==0
        first = section.locator('img').first
        first.scroll_into_view_if_needed()
        expect(first).to_have_attribute('src', re.compile('/history/[0-9a-f]+/9/'))
        page.wait_for_function('''() => {const i=document.querySelector('#cal-history img');return i&&i.complete&&i.naturalWidth>0;}''', timeout=60000)
        page.screenshot(path=str(out/'history-page.png'))
        inspection = page.request.get(f'http://127.0.0.1:8061/api/calibration/builds/{tag}/inspection')
        assert inspection.json()['reference_label']=='Current deployed weights'
        first.click()
        expect(page.locator('.figure-modal')).to_be_visible()
        with page.expect_download() as download:
            page.locator('.figure-modal').get_by_role('button',name='Download',exact=True).click()
        download.value.save_as(str(out/'history-expanded.png'))
        page.keyboard.press('Escape')
        section.get_by_label('Latest day · UTC').fill('2026-10-03')
        page.wait_for_timeout(4500)
        expect(section.get_by_label('Latest day · UTC')).to_have_value('2026-10-03')
        section.get_by_role('button', name='Compare days', exact=True).click()
        page.wait_for_timeout(500)
        assert len(intercepted)==1
        request = intercepted[0]
        assert request['url'].endswith(f'/builds/{tag}/actions')
        assert request['body']['action']=='history' and request['body']['days']==3
        assert request['body']['date']=='2026-10-03'
        assert not request['body'].get('compare_subtraction')
        assert not errors, errors
        report = dict(saved_history_redraw=True, download=True, selected_solution_only=True,
                      current_deployed_label=True, errors=errors, server_writes=0,
                      intercepted_submit=request)
        (out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
        browser.close()


if __name__=='__main__':
    main()
