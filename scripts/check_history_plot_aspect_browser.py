"""GET-only regression for enlarged visibility and SNAP playback proportions.

Use --record-only to capture a pre-fix baseline. Reads saved monitor products;
blocks acquisition, annotations, calibration and all other API mutations.
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8061')
    parser.add_argument('--record-only', action='store_true')
    parser.add_argument('--output', type=Path, default=Path(
        '/home/casm/scratch/casm-observation-preview/history-aspect-20261007'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results, errors, writes = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1500, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))

        def guard(route):
            if route.request.method not in ('GET', 'HEAD'):
                writes.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        page.route('**/api/**', guard)

        def click(locator):
            locator.evaluate('(e)=>e.click()')

        def wait(expression):
            page.wait_for_function(expression, polling=100, timeout=60000)

        for kind, ratio in [('vis', 16 / 9), ('snaps', 324 / 192)]:
            page.goto(args.url + '/' + kind, wait_until='domcontentloaded')
            wait('document.querySelectorAll("' + (
                '.antenna-plot' if kind == 'vis' else '.snap-spectrum-card') + '").length>0')
            click(page.get_by_role('button', name='History', exact=True))
            wait('document.querySelector(".snap-movie-stage")?.getAttribute("aria-busy")==="false"')
            if kind == 'vis':
                page.get_by_label('Playback correlation', exact=True).select_option('cross')
                wait('document.querySelectorAll(".vis-movie-trigger canvas").length>0')
                trigger, play, pause = '.vis-movie-trigger', 'Play baseline playback', 'Pause baseline playback'
                figure, frame = '.vis-movie-dynamic-frame', '.figure-modal canvas'
            else:
                trigger, play, pause = '.snap-movie-card .snap-plot-trigger', 'Play antenna playback', 'Pause antenna playback'
                figure, frame = '.snap-expanded .snap-plot svg', '.snap-expanded'
            click(page.locator(trigger).first)
            wait(f'document.querySelector("{frame}")?.hasAttribute("data-frame")')
            first = int(page.locator(frame).get_attribute('data-frame'))
            click(page.get_by_role('button', name=play, exact=True))
            wait(f'Number(document.querySelector("{frame}")?.dataset.frame)>{first}')
            if not args.record_only:
                moving = page.locator('.figure-modal').locator(figure).bounding_box()
                assert abs(moving['width'] / moving['height'] - ratio) < .02, moving
            click(page.get_by_role('button', name=pause, exact=True))
            current = page.locator(frame).get_attribute('data-frame')
            # Ratios must survive short/wide windows and display zoom. Width-only
            # scaling is intentional; the viewport scrolls rather than flattening.
            for width, height in [(1500, 900), (1920, 800), (1000, 900), (390, 900)]:
                page.set_viewport_size({'width': width, 'height': height})
                for zoom in [100, 150]:
                    modal = page.locator('.figure-modal')
                    click(modal.get_by_role('button', name='Fit' if zoom == 100 else 'Zoom in', exact=True))
                    page.wait_for_timeout(350)
                    bounds = modal.locator(figure).bounding_box()
                    actual = bounds['width'] / bounds['height']
                    result = dict(view=kind, viewport=[width, height], zoom=zoom,
                                  width=bounds['width'], height=bounds['height'],
                                  aspect=actual, expected_aspect=ratio)
                    if kind == 'snaps':
                        viewbox = modal.locator(figure).get_attribute('viewBox').split()
                        result['viewbox_aspect'] = float(viewbox[2]) / float(viewbox[3])
                    results.append(result)
                    if not args.record_only:
                        assert abs(actual - ratio) < .02, result
                        if kind == 'snaps':
                            assert abs(result['viewbox_aspect'] - ratio) < .02, result
                    assert page.locator(frame).get_attribute('data-frame') == current
                    if width == 1920 and zoom == 100:
                        page.screenshot(path=str(args.output / f'{kind}-expanded.png'))
            # Download includes the whole plot even when the window must scroll.
            with page.expect_download() as saved:
                click(page.locator('.figure-modal').get_by_role('button', name='Download', exact=True))
            target = args.output / f'{kind}-download.png'
            saved.value.save_as(str(target))
            assert target.stat().st_size > 1000
            click(page.locator('.figure-modal').get_by_role('button', name='Close ×', exact=True))
            assert page.locator('.snap-movie-stage').get_attribute('data-frame') == current
        assert not errors and not writes, (errors, writes)
        report = dict(result='recorded' if args.record_only else 'passed',
                      measurements=results, page_errors=errors, non_get_requests=writes)
        (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))
        browser.close()


if __name__ == '__main__':
    main()
