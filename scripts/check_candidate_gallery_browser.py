"""Read-only real-data gallery acceptance. All non-GET API requests are blocked."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = 'http://127.0.0.1:8061'
OUT = Path('/home/casm/scratch/casm-observation-preview/screenshots')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1500, 'height':1100})
        errors, writes = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        def guard(route):
            if route.request.method != 'GET':
                writes.append(route.request.url)
                route.abort()
            else:
                route.continue_()
        page.route('**/api/**', guard)
        page.goto(URL+'/cands')
        page.locator('.candidate-card').first.wait_for(timeout=60000)
        assert page.locator('.candidate-card').count()==12
        assert page.locator('.candidate-list-item.shown').count()==12
        assert page.locator('.candidate-gallery').bounding_box()['x'] < page.locator('.candidate-sidebar').bounding_box()['x']
        page.locator('.candidate-image').first.scroll_into_view_if_needed()
        page.wait_for_function('document.querySelector(".candidate-image img").naturalWidth>0')
        page.evaluate('scrollTo(0,0)')
        page.screenshot(path=str(OUT/'candidates-grid.png'), timeout=60000)
        page.locator('.candidate-image').first.click()
        page.get_by_role('dialog').wait_for()
        page.get_by_role('button',name='Zoom in',exact=True).click()
        assert page.get_by_label('Display zoom').inner_text()=='150%'
        page.screenshot(path=str(OUT/'candidate-expanded.png'))
        page.keyboard.press('Escape')
        older=page.locator('.candidate-list-item:not(.shown)').first
        older_name=older.get_attribute('data-name')
        page.get_by_label('Find a candidate',exact=True).fill(older_name)
        page.locator('.candidate-list-item').click()
        page.wait_for_function('(n)=>document.querySelector(".candidate-card.focused")?.dataset.name===n', arg=older_name)
        assert page.locator('.candidate-card').count()==12
        assert page.locator('.candidate-list-item').get_attribute('aria-pressed')=='true'
        page.locator('.candidate-list-item').click()
        assert page.locator('.candidate-card').count()==11
        assert page.locator('.candidate-list-item').get_attribute('aria-pressed')=='false'
        page.locator('.candidate-list-item').click()
        assert page.locator('.candidate-card').count()==12
        assert page.locator('.candidate-list-item').get_attribute('aria-pressed')=='true'
        page.get_by_label('Find a candidate',exact=True).fill('')
        assert page.locator('.candidate-list-item.shown').count()==12
        page.get_by_role('button',name='Latest 12',exact=True).click()
        assert page.locator('.candidate-card.focused').count()==0
        page.get_by_label('Plots in grid',exact=True).select_option('6')
        assert page.locator('.candidate-card').count()==6
        first=page.locator('.candidate-card').first.get_attribute('data-name')
        page.get_by_role('button',name=f'Hide candidate {first}',exact=True).click()
        assert page.locator('.candidate-card').count()==5
        assert page.locator('.candidate-list-item.shown').count()==5
        page.get_by_role('button',name='Latest 6',exact=True).click()
        for width in [1500,1024,700,390,320]:
            page.set_viewport_size({'width':width,'height':1100})
            page.wait_for_timeout(200)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'), width
            if width==390:
                page.screenshot(path=str(OUT/'candidates-mobile.png'))
        page.set_viewport_size({'width':1500,'height':1100})
        # Existing event detail keeps all images and gets the shared zoom too.
        page.locator('.candidate-card').first.get_by_role('link',name='Event details →').click()
        page.locator('.candidate-image').first.wait_for(timeout=60000)
        page.locator('.candidate-image').first.click()
        page.get_by_role('dialog').wait_for()
        page.keyboard.press('Escape')
        assert not writes, writes
        assert not errors, errors
        # Capture the populated membership map rather than its loading skeleton.
        page.goto(URL+'/observation')
        page.locator('.overview-map button.beamforming').first.wait_for(timeout=60000)
        assert page.locator('.overview-map button.beamforming').count()==16
        assert page.locator('.overview-map button').count()==24
        assert page.locator('.overview-map button.beamforming').first.evaluate('e=>getComputedStyle(e).backgroundColor')=='rgb(22, 78, 54)'
        page.locator('.overview-array').screenshot(path=str(OUT/'overview-beamforming-layout.png'))
        browser.close()
    print(json.dumps({'grid_default':12,'selection':'passed','zoom':'grid and detail',
                      'responsive_widths':[1500,1024,700,390,320], 'screenshots':str(OUT)}))


if __name__=='__main__':
    main()
