"""Read-only preview checks for the dated B0329 gallery and saved-plot zoom."""
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = 'http://127.0.0.1:8061'
OUT = Path('/home/casm/scratch/casm-observation-preview/screenshots')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1500, 'height': 1100})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(URL + '/sources')
        page.locator('.history-entry').first.wait_for(timeout=30000)
        assert page.get_by_label('Show', exact=True).input_value() == 'detections'
        assert page.locator('.history-entry').count() == 13
        assert page.locator('.history-notes[open]').count() == 0
        dates = page.locator('.history-entry h3').all_text_contents()
        assert dates == sorted(set(dates), reverse=True)
        assert '2026-05-28' in dates  # Missing plot does not erase detection date.
        assert '2026-09-01' not in dates  # Noise fit, not a detection.
        aug4 = page.locator('.history-entry').filter(has=page.get_by_role('heading', name='2026-08-04', exact=True))
        assert 'clfd_DMlocked.png' in aug4.locator('> .history-image img').get_attribute('alt')
        may25 = page.locator('.history-entry').filter(has=page.get_by_role('heading', name='2026-05-25', exact=True))
        assert may25.locator('> .history-image').count() == 0  # Available PNGs do not show the at-par result.
        visible = page.locator('body').inner_text().lower()
        assert 'provenance' not in visible and 'canonical' not in visible
        page.wait_for_function('document.querySelector(".history-image img").naturalWidth>0')
        page.screenshot(path=str(OUT / 'source-history-detections.png'))
        page.locator('.history-image').first.click()
        assert page.get_by_role('dialog').count() == 1
        page.get_by_role('button', name='Zoom in', exact=True).click()
        assert page.get_by_label('Display zoom').inner_text() == '150%'
        page.keyboard.press('Escape')
        page.get_by_label('Show', exact=True).select_option('all')
        assert page.locator('.history-entry').count() == 19
        sept2 = page.locator('.history-entry').filter(has=page.get_by_role('heading', name='2026-09-02', exact=True))
        sept2.locator('summary').click()
        assert 'withdrawn claim' in sept2.inner_text()
        page.get_by_label('Source', exact=True).fill('unknown')
        page.get_by_role('button', name='Search', exact=True).click()
        page.get_by_text('No saved history for this source.').wait_for()
        assert page.locator('.history-entry').count() == 0
        page.get_by_label('Source', exact=True).fill('B0329')
        page.get_by_role('button', name='Search', exact=True).click()
        page.locator('.history-entry').first.wait_for()
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1')
        page.screenshot(path=str(OUT / 'source-history-mobile.png'))
        assert not errors, errors
        browser.close()
    print('Source history: 13 detection dates, 19 observation dates, notes, zoom, search and mobile passed')


if __name__ == '__main__':
    main()
