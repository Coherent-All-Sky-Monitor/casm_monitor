"""Browser acceptance of the isolated calibration UI; never submit a live upload."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    output = Path('/home/casm/scratch/casm-observation-preview/screenshots')
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        page.on('pageerror', lambda error: errors.append(str(error)))
        # Browser acceptance may inspect/import a reference, but cannot submit
        # a solve, a hardware request or either upload path.
        def guard(route):
            request = route.request
            if request.method == 'POST' and not request.url.endswith('/api/calibration/import-reference'):
                errors.append('Unexpected POST: ' + request.url)
                route.abort()
            else:
                route.continue_()
        page.route('**/api/**', guard)
        page.goto('http://127.0.0.1:8061/cal', wait_until='networkidle')
        page.get_by_role('heading', name='Calibration', exact=True).wait_for()
        page.get_by_role('button', name='Calibrate with the Sun').wait_for()
        assert page.get_by_role('button', name='Calibrate with the Sun').is_disabled()
        assert page.locator('a[aria-current=page]', has_text='Calibration').count() >= 1
        page.screenshot(path=str(output / 'calibration-prepare.png'), full_page=True)
        page.get_by_role('button', name='Inspect deployed calibration', exact=True).click()
        page.get_by_role('heading', name='Matched phase views', exact=True).wait_for()
        phase_settings = page.locator('details').filter(has=page.locator('summary', has_text='Phase settings'))
        if phase_settings.get_attribute('open') is None:
            phase_settings.locator(':scope > summary').click()
        page.get_by_role('heading', name='Phase progression', exact=True).wait_for()
        page.get_by_role('button', name='Render matched phase views').wait_for()
        page.wait_for_function('!Array.from(document.querySelectorAll("button")).find(button => button.textContent === "Render matched phase views").disabled')
        page.screenshot(path=str(output / 'calibration-inspect.png'), full_page=True)
        page.get_by_role('button', name='03Validate on sky').click()
        transit_settings = page.locator('details').filter(has=page.locator('summary', has_text='Transit settings'))
        if transit_settings.get_attribute('open') is None:
            transit_settings.locator(':scope > summary').click()
        page.get_by_role('heading', name='A source crossing a stationary beam').wait_for()
        assert page.get_by_role('button', name='Run stationary transit check').is_enabled()
        page.screenshot(path=str(output / 'calibration-transit.png'), full_page=True)
        page.get_by_role('button', name='05Upload dry run').click()
        assert page.get_by_role('button', name='Upload reviewed product', exact=True).count() == 0
        assert page.get_by_role('button', name='Run upload dry run', exact=True).is_disabled()
        page.screenshot(path=str(output / 'calibration-deploy.png'), full_page=True)
        for width in (1280, 768, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), f'Horizontal overflow at {width}'
        overview = page.request.get('http://127.0.0.1:8061/api/calibration').json()
        examples = [item for item in overview['builds'] if item['tag'].startswith('workspace_acceptance_') and item['has_weights']]
        if examples:
            page.set_viewport_size({'width': 1440, 'height': 1100})
            page.goto('http://127.0.0.1:8061/cal/' + examples[0]['tag'], wait_until='networkidle')
            page.wait_for_function('document.querySelector(".cal-gallery img")?.naturalWidth > 0')
            page.screenshot(path=str(output / 'calibration-saved-example.png'), full_page=False)
            page.get_by_role('button', name='03Validate on sky').click()
            page.wait_for_function('document.querySelector(".cal-gallery img")?.naturalWidth > 0')
            page.screenshot(path=str(output / 'calibration-saved-transit.png'), full_page=True)
            page.locator('.cal-gallery .plot-image-trigger').first.click()
            page.get_by_role('button', name='Zoom in', exact=True).click()
            assert page.get_by_label('Display zoom').inner_text() == '150%'
            page.get_by_role('button', name='Close ×', exact=True).click()
            page.get_by_role('button', name='04Generate beams').click()
            page.wait_for_function('document.querySelector(".cal-sky img")?.naturalWidth > 0')
            page.screenshot(path=str(output / 'calibration-sky.png'), full_page=True)
            assert 'ellipse' in page.locator('.cal-sky').inner_text().lower()
            for width in (1280, 768, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), f'Sky overflow at {width}'
        browser.close()
    assert not errors, errors
    print(json.dumps({'browser_errors': errors, 'screenshots': str(output), 'live_upload': 'not requested'}))


if __name__ == '__main__':
    main()
