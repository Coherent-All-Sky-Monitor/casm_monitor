"""Read-only check of deployed-calibration cards and local/UTC presentation.

Uses the recorded calibration, never submits jobs, and blocks non-GET API calls.
Run against the built frontend served on 8061.
"""
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import expect, sync_playwright

BASE = 'http://127.0.0.1:8061'


def instant(value):
    ledger = re.match(r'^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}) UTC', value)
    return datetime.fromisoformat(
        f'{ledger[1]}T{ledger[2]}+00:00' if ledger else value.replace('Z', '+00:00'))


def date_label(value, zone):
    date = instant(value).astimezone(ZoneInfo(zone))
    return f'{date:%b} {date.day}, {date.year}'


def main():
    output = Path('/home/casm/scratch/casm-observation-preview/screenshots/calibration-cards')
    output.mkdir(parents=True, exist_ok=True)
    errors, writes, sizes = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport=dict(width=1440, height=1000),
                                      timezone_id='Asia/Tokyo', reduced_motion='reduce')

        def readonly(route):
            if route.request.method not in ('GET', 'HEAD'):
                writes.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        context.route('**/api/**', readonly)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        recorded = context.request.get(BASE + '/api/calibration').json()['defaults']['current_calibration']
        assert not recorded.get('unavailable'), recorded
        page.goto(BASE + '/cal', wait_until='domcontentloaded')
        current = page.get_by_role('region', name='Current deployed calibration')
        cards = current.locator('.cal-current-card')
        expect(cards).to_have_count(3, timeout=30000)
        assert cards.locator(':scope > span').all_text_contents() == ['Calibrator', 'Solve window', 'Last uploaded']
        zones = current.get_by_role('group', name='Current calibration time zone')
        expect(zones.get_by_role('button', name='OVRO local', exact=True)).to_have_attribute('aria-pressed', 'true')
        for zone, button in [('America/Los_Angeles', 'OVRO local'), ('UTC', 'UTC')]:
            zones.get_by_role('button', name=button, exact=True).click()
            expect(zones.get_by_role('button', name=button, exact=True)).to_have_attribute('aria-pressed', 'true')
            expect(cards.nth(0)).to_contain_text(date_label(recorded['source_window'][0], zone))
            expect(cards.nth(2)).to_contain_text(date_label(recorded['date_uploaded'], zone))
            start, end = [instant(t).astimezone(ZoneInfo(zone)) for t in recorded['source_window']]
            expect(cards.nth(1).locator('strong')).to_have_text(f'{start:%H:%M} – {end:%H:%M %Z}')
            expected = instant(recorded['date_uploaded']).astimezone(ZoneInfo(zone))
            expect(cards.nth(2).locator('small')).to_have_text(f'{expected:%H:%M %Z}')
        zones.get_by_role('button', name='OVRO local', exact=True).click()
        for width in (1600, 1440, 1100, 1001, 900, 768, 390, 320):
            page.set_viewport_size(dict(width=width, height=1000))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
            boxes = cards.evaluate_all('els=>els.map(e=>({top:e.getBoundingClientRect().top, height:e.getBoundingClientRect().height, overflow:e.scrollWidth>e.clientWidth+1, background:getComputedStyle(e).backgroundColor}))')
            assert all(not box['overflow'] for box in boxes), (width, boxes)
            assert all(box['background'] == 'rgb(13, 24, 37)' for box in boxes)
            if width > 1000:
                assert len({box['top'] for box in boxes}) == 1, (width, boxes)
                assert len({box['height'] for box in boxes}) == 1, (width, boxes)
            else:
                assert len({box['top'] for box in boxes}) == 3, (width, boxes)
            sizes.append(width)
            if width in (1440, 390):
                current.screenshot(path=str(output / f'cards-{width}.png'), animations='disabled', timeout=15000)
        assert not errors, errors
        assert not writes, writes
        result = dict(source=recorded['source'], source_window=recorded['source_window'],
                      date_uploaded=recorded['date_uploaded'], widths=sizes,
                      checks=['Overview colours', 'equal desktop cards', 'stacked mobile cards',
                              'no overflow', 'OVRO local default', 'local/UTC dates and times'],
                      browser_errors=errors, blocked_writes=writes)
        (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2), flush=True)
        browser.close()


if __name__ == '__main__':
    main()
