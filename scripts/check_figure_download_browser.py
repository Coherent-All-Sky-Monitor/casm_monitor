"""Download actual saved figures; block all API writes and hardware requests."""
import hashlib
import json
import tempfile
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

URL = 'http://127.0.0.1:8061'


def main():
    output = Path(tempfile.mkdtemp(prefix='casm-figure-download-'))
    results, errors, forbidden = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport=dict(width=1440, height=1000))
        page.on('pageerror', lambda e: errors.append(str(e)))

        def guard(route):
            if route.request.method != 'GET':
                # The visibility page can request its normal cached-detail render
                # on mount. Block it too; exports must not require that request.
                if not route.request.url.split('?')[0].endswith('/api/science/render'):
                    forbidden.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        page.route('**/api/**', guard)

        def download(label, original=False):
            dialog = page.get_by_role('dialog')
            dialog.wait_for()
            content = dialog.locator('.figure-zoom-content')
            content.locator('img,svg').first.wait_for()
            expected = None
            if original:
                src = content.locator('img').get_attribute('src')
                expected = page.request.get(URL + src if src.startswith('/') else src).body()
            with page.expect_download(timeout=30000) as event:
                dialog.get_by_role('button', name='Download', exact=True).click()
            received = event.value
            assert received.failure() is None
            path = output / (label + '-' + received.suggested_filename)
            received.save_as(path)
            image = Image.open(path)
            assert image.width > 200 and image.height > 100, image.size
            assert image.convert('RGB').resize((100, 100)).getextrema() != ((255, 255),) * 3
            if expected:
                assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(expected).digest()
            assert dialog.get_by_role('alert').count() == 0
            results.append(dict(view=label, name=received.suggested_filename, size=image.size,
                                original_bytes=bool(expected)))

        page.goto(URL + '/observation')
        page.get_by_role('button', name='Enlarge injection recovery plot', exact=True).wait_for(timeout=60000)
        page.get_by_role('button', name='Enlarge injection recovery plot', exact=True).locator('svg').click(position=dict(x=120, y=15))
        download('injection-svg')
        page.keyboard.press('Escape')

        page.goto(URL + '/search')
        page.get_by_role('button', name='Enlarge Search (T1) plots', exact=True).click(timeout=60000)
        page.get_by_role('button', name='Zoom in', exact=True).click()
        download('search-original', original=True)
        image_url = URL + page.get_by_role('dialog').locator('img').get_attribute('src')
        page.route(image_url, lambda r: r.fulfill(status=503, body='Temporary test failure'))
        page.get_by_role('button', name='Download', exact=True).click()
        page.get_by_role('alert').filter(has_text='Download failed').wait_for()
        assert page.get_by_role('button', name='Download', exact=True).is_enabled()
        page.unroute(image_url)
        download('search-retry', original=True)
        page.keyboard.press('Escape')

        page.goto(URL + '/snaps')
        page.locator('.snap-plot-trigger').first.click(timeout=60000)
        page.get_by_role('dialog').get_by_role('img', name='Full-band power (dB) versus time (UTC)').wait_for(timeout=60000)
        download('snap-spectrum-and-trend')
        page.get_by_role('button', name='Zoom in', exact=True).click()
        download('snap-zoomed')
        page.get_by_role('button', name='Fit', exact=True).click()
        page.set_viewport_size(dict(width=390, height=844))
        assert page.get_by_role('button', name='Download', exact=True).is_visible()
        assert page.get_by_role('dialog').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
        page.keyboard.press('Escape')
        page.set_viewport_size(dict(width=1440, height=1000))

        page.goto(URL + '/vis')
        page.locator('.plot-zoom-trigger').first.wait_for(timeout=60000)
        page.locator('.plot-zoom-trigger').first.click()
        download('visibility-spectrum')
        page.keyboard.press('Escape')
        page.get_by_role('button', name='Cross-correlations', exact=True).click()
        page.locator('.array-dynamic > img').first.wait_for(timeout=60000)
        page.locator('.plot-zoom-trigger').first.click()
        download('visibility-dynamic')
        page.keyboard.press('Escape')

        page.goto(URL + '/sources')
        page.locator('.history-image').first.click(timeout=60000)
        download('b0329-original', original=True)
        page.keyboard.press('Escape')
        page.get_by_role('button', name='Sun', exact=True).click()
        page.locator('.transit-entry .plot-zoom-trigger').first.click(timeout=60000)
        download('source-waterfall-and-curve')
        page.keyboard.press('Escape')

        page.goto(URL + '/cands')
        page.locator('.candidate-image').first.click(timeout=60000)
        download('candidate-original', original=True)
        page.keyboard.press('Escape')

        page.goto(URL + '/cal/workspace_acceptance_20260918_1790102134')
        page.locator('.plot-image-trigger').first.click(timeout=60000)
        download('calibration-original', original=True)
        assert not errors, errors
        assert not forbidden, forbidden
        browser.close()
    print(json.dumps(dict(output=str(output), downloads=results, errors=errors, forbidden=forbidden), indent=2))


if __name__ == '__main__':
    main()
