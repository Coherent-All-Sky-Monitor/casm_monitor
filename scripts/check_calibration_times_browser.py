"""Read-only browser check of calibration membership and local/UTC window entry.

Never clicks a solve, import, render, grid or upload action. All non-GET API
requests are blocked. Evidence uses the served page and saved deployment only.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import expect, sync_playwright

BASE = 'http://127.0.0.1:8061'
LOCAL = ZoneInfo('America/Los_Angeles')


def wall(value, zone=LOCAL):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(zone).strftime('%Y-%m-%dT%H:%M')


def main():
    output = Path('/home/casm/scratch/casm-observation-preview/screenshots/calibration-local-times')
    output.mkdir(parents=True, exist_ok=True)
    errors, writes = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        # Deliberately not the observatory zone: the UI must still use OVRO time.
        context = browser.new_context(viewport={'width': 1440, 'height': 1100}, timezone_id='Asia/Tokyo')
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))

        def guard(route):
            if route.request.method != 'GET':
                writes.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        page.route('**/api/**', guard)
        defaults = page.request.get(BASE + '/api/calibration').json()['defaults']
        membership = page.request.get(BASE + '/api/snap-workspace/beamforming').json()
        antennas = sorted(item['antenna'] for item in membership['inputs'] if item['beamforming'] is True)
        assert membership['status'] == 'complete'
        assert defaults['antennas_source'] == 'deployed'
        assert defaults['antennas'] == antennas
        assert defaults['deployed']['product_id'] == membership['product_id']
        page.goto(BASE + '/cal', wait_until='networkidle')
        solve = page.get_by_role('button', name='Calibrate with the Sun')
        expect(solve).to_be_disabled()
        expect(page.locator('.cal-antennas input:checked')).to_have_count(len(antennas))
        selected = page.locator('.cal-antennas label:has(input:checked)').all_text_contents()
        assert sorted(int(label.split('·')[-1].strip()) for label in selected) == antennas
        group = page.get_by_role('group', name='Solve time zone')
        utc = group.get_by_role('button', name='UTC', exact=True)
        local = group.get_by_role('button', name='OVRO local · PDT/PST', exact=True)
        expect(local).to_have_attribute('aria-pressed', 'true')
        fields = {f'{kind} {edge}': page.get_by_label(f'{kind} {edge}', exact=True)
                  for kind in ('Solve', 'Static') for edge in ('start', 'end')}
        for kind, key in [('Solve', 'source_window'), ('Static', 'static_window')]:
            for i, edge in enumerate(('start', 'end')):
                expect(fields[f'{kind} {edge}']).to_have_value(wall(defaults[key][i]))
        peak = page.get_by_role('region', name='Sun altitude maximum')
        expect(peak.locator('time')).to_have_count(2)
        peak_text = peak.inner_text()
        expected_peak = datetime.fromisoformat(defaults['alt_max_utc'].replace('Z', '+00:00'))
        for instant in [expected_peak, expected_peak.astimezone(LOCAL)]:
            assert instant.strftime('%Y-%m-%d %H:%M:%S %Z') in ' '.join(peak_text.split()), peak_text
        page.screenshot(path=str(output / 'prepare-local.png'), full_page=True)
        utc.click()
        for kind, key in [('Solve', 'source_window'), ('Static', 'static_window')]:
            for i, edge in enumerate(('start', 'end')):
                expect(fields[f'{kind} {edge}']).to_have_value(defaults[key][i][:16])
        page.screenshot(path=str(output / 'prepare-utc.png'), full_page=True)
        local.click()
        edited = expected_peak - timedelta(minutes=20)
        fields['Solve start'].fill(edited.astimezone(LOCAL).strftime('%Y-%m-%dT%H:%M'))
        utc.click()
        expect(fields['Solve start']).to_have_value(edited.strftime('%Y-%m-%dT%H:%M'))
        expect(fields['Solve end']).to_have_value(defaults['source_window'][1][:16])
        review = page.get_by_role('checkbox', name='I reviewed the windows')
        review.check()
        expect(solve).to_be_enabled()
        local.click()
        expect(review).to_be_checked()  # Display-only zone changes preserve the interval.
        for invalid, message in [('2026-03-08T02:30', 'does not exist'), ('2026-11-01T01:30', 'occurs twice'), ('', 'complete, valid')]:
            fields['Solve start'].fill(invalid)
            expect(fields['Solve start']).to_have_attribute('aria-invalid', 'true')
            expect(page.get_by_role('alert')).to_contain_text(message)
            review.check()
            expect(solve).to_be_disabled()
        utc.click()
        expect(fields['Solve start']).to_have_value(edited.strftime('%Y-%m-%dT%H:%M'))
        expect(review).not_to_be_checked()  # Invalid edits need a fresh review.
        # Both repeated-hour instants remain distinct through local/UTC toggles.
        for value in ['2026-11-01T08:30', '2026-11-01T09:30']:
            fields['Solve start'].fill(value)
            local.click()
            expect(fields['Solve start']).to_have_value('2026-11-01T01:30')
            utc.click()
            expect(fields['Solve start']).to_have_value(value)
        local.click()
        fields['Static start'].fill('')
        review.check()
        expect(solve).to_be_disabled()
        static = page.get_by_role('checkbox', name='Subtract a matched night-time')
        static.uncheck()
        review.check()
        expect(solve).to_be_enabled()
        # No action submitted. Reload restores saved defaults, local-first.
        page.reload(wait_until='networkidle')
        expect(local).to_have_attribute('aria-pressed', 'true')
        expect(fields['Solve start']).to_have_value(wall(defaults['source_window'][0]))
        for width in (1440, 768, 390):
            page.set_viewport_size({'width': width, 'height': 1100})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
        page.screenshot(path=str(output / 'prepare-mobile.png'), full_page=True)
        browser.close()
    assert not errors, errors
    assert not writes, writes
    result = dict(antennas=antennas, product_id=membership['product_id'],
                  alt_max_utc=defaults['alt_max_utc'], alt_max_local=expected_peak.astimezone(LOCAL).isoformat(),
                  browser_errors=errors, non_get_requests=writes,
                  checks=['deployed membership', 'local default', 'static date rollover', 'UTC toggle',
                          'local edit to UTC', 'DST gap/fold rejection', 'blank time blocks solve',
                          'both UTC fold choices preserved', 'responsive layout'])
    (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
