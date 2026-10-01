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
        overview = page.request.get(BASE + '/api/calibration').json()
        defaults = overview['defaults']
        assert defaults['defaults_basis']['kind'] == 'uploaded_recipe'
        recorded = json.loads(Path(defaults['defaults_basis']['report']).read_text())['params']
        for key in ('source_window', 'static_window'):
            assert [wall(t) for t in defaults[key]] == [wall(t + 'Z' if 'T' not in t else t) for t in recorded[key]]
        # Reproduce an old browser's remembered candidate; /cal must ignore it.
        remembered = next((b['tag'] for b in overview['builds'] if b['has_cal']), 'old_candidate')
        context.add_init_script('localStorage.setItem("casm-calibration-candidate", ' + json.dumps(remembered) + ');')
        membership = page.request.get(BASE + '/api/snap-workspace/beamforming').json()
        antennas = sorted(item['antenna'] for item in membership['inputs'] if item['beamforming'] is True)
        assert membership['status'] == 'complete'
        assert defaults['antennas_source'] == 'deployed'
        assert defaults['antennas'] == antennas
        assert defaults['deployed']['product_id'] == membership['product_id']
        page.goto(BASE + '/cal', wait_until='networkidle')
        solve = page.get_by_role('button', name='Calibrate with the Sun')
        expect(solve).to_be_disabled()
        expect(page.get_by_role('combobox', name='Saved candidate', exact=True)).to_have_value('')
        expect(page.get_by_label('Calibration starting point')).to_contain_text('last uploaded weights')
        expect(page.locator('.cal-antennas input:checked')).to_have_count(len(antennas))
        selected = page.locator('.cal-antennas label:has(input:checked)').all_text_contents()
        assert sorted(int(label.split('·')[-1].strip()) for label in selected) == antennas
        group = page.get_by_role('group', name='Solve time zone')
        utc = group.get_by_role('button', name='UTC', exact=True)
        local = group.get_by_role('button', name='OVRO local · PDT/PST', exact=True)
        expect(local).to_have_attribute('aria-pressed', 'true')
        planner = page.get_by_role('region', name='Night-time template planner')
        expect(planner.locator('path[data-source]')).to_have_count(4)
        assert page.locator('.cal-steps button[data-state=pending]').count() == 5
        assert page.locator('.cal-steps button.selected').evaluate('(el)=>getComputedStyle(el).backgroundColor') == 'rgb(22, 30, 44)'
        chart = planner.locator('svg')
        expect(chart).to_have_attribute('data-altitude-min', '0')
        expect(chart).to_have_attribute('data-altitude-max', '90')
        expect(chart.locator('[data-low-altitude-band="20"]')).to_have_count(1)
        expect(chart.locator('[data-altitude-line="20"]')).to_have_count(1)
        expect(chart.locator('[data-window="Suggested static"]')).to_have_attribute('fill-opacity', '0.14')
        expect(chart.locator('[data-window="Suggested static"]')).to_have_attribute('stroke-dasharray', '4 3')
        expect(chart.locator('[data-window="Solar solve"]')).to_have_count(1)
        assert wall(chart.get_attribute('data-start')) == defaults['date'] + 'T00:00'
        assert wall(chart.get_attribute('data-end')) == defaults['date'] + 'T23:59'
        expect(chart).to_contain_text('23:59')
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
        expect(page.locator('.cal-steps button').first).to_have_attribute('data-state', 'complete')
        assert page.locator('.cal-steps button').first.evaluate('(el)=>getComputedStyle(el).backgroundColor') == 'rgb(24, 59, 43)'
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
        # Selected local day only: September has no qualifying interval; May has one.
        page.get_by_label('Solar date', exact=True).fill('2026-09-18')
        expect(planner).to_contain_text('No interval of at least 15 min')
        expect(planner.locator('svg')).to_have_attribute('data-start', '2026-09-18T07:00:00.000Z')
        suggested = page.request.get(BASE + '/api/calibration/sky-plan?date=2026-09-18').json()['suggestion']
        expect(planner.get_by_label('Suggested night-time static window')).to_contain_text('Above usual altitude cuts: Cas A, Tau A')
        review.check()
        planner.get_by_role('button', name='Use suggested window').click()
        expect(review).not_to_be_checked()
        for i, edge in enumerate(('start', 'end')):
            expect(fields[f'Static {edge}']).to_have_value(datetime.fromtimestamp(suggested['selection'][i], LOCAL).strftime('%Y-%m-%dT%H:%M'))
        expect(planner).to_contain_text('Selected static window exceeds altitude limits: Cas A, Tau A.')
        page.screenshot(path=str(output / 'suggested-static-september.png'), full_page=True)
        page.get_by_label('Solar date', exact=True).fill('2026-05-16')
        expect(chart).to_have_attribute('data-start', '2026-05-16T07:00:00.000Z')
        planner.locator('summary').click()
        expect(planner.get_by_role('button', name='Use 30 min')).to_have_count(1)
        sky = page.request.get(BASE + '/api/calibration/sky-plan?date=2026-05-16').json()
        assert wall(chart.get_attribute('data-start')) == '2026-05-16T00:00'
        assert wall(chart.get_attribute('data-end')) == '2026-05-16T23:59'
        review.check()
        planner.get_by_role('button', name='Use 30 min').first.click()
        expect(review).not_to_be_checked()
        expect(planner).to_contain_text('Selected static window meets the sampled altitude limits.')
        for i, edge in enumerate(('start', 'end')):
            expected = datetime.fromtimestamp(sky['windows'][0]['selection'][i], LOCAL).strftime('%Y-%m-%dT%H:%M')
            expect(fields[f'Static {edge}']).to_have_value(expected)
        page.screenshot(path=str(output / 'sky-planner-may.png'), full_page=True)
        planner.get_by_role('button', name='Enlarge calibrator altitude plot').click()
        dialog = page.get_by_role('dialog')
        expect(dialog.locator('path[data-source]')).to_have_count(4)
        dialog.get_by_role('button', name='Zoom in', exact=True).click()
        expect(dialog.get_by_label('Display zoom')).to_have_text('150%')
        with page.expect_download() as exported:
            dialog.get_by_role('button', name='Download', exact=True).click()
        exported.value.save_as(output / 'altitude-zoom.png')
        dialog.get_by_role('button', name='Close ×', exact=True).click()
        for width in (1440, 768, 390):
            page.set_viewport_size({'width': width, 'height': 1100})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
        page.screenshot(path=str(output / 'prepare-mobile.png'), full_page=True)
        review.check()
        expect(page.locator('.cal-steps button').first).to_have_attribute('data-state', 'complete')
        page.get_by_role('button', name='New calibration', exact=True).click()
        expect(review).not_to_be_checked()
        expect(page.locator('.cal-steps button[data-state=pending]')).to_have_count(5)
        expect(page.get_by_label('Solar date', exact=True)).to_have_value(defaults['date'])
        for kind, key in [('Solve', 'source_window'), ('Static', 'static_window')]:
            for i, edge in enumerate(('start', 'end')):
                expect(fields[f'{kind} {edge}']).to_have_value(wall(defaults[key][i]))
        browser.close()
    assert not errors, errors
    assert not writes, writes
    result = dict(antennas=antennas, product_id=membership['product_id'],
                  alt_max_utc=defaults['alt_max_utc'], alt_max_local=expected_peak.astimezone(LOCAL).isoformat(),
                  browser_errors=errors, non_get_requests=writes,
                  checks=['deployed membership', 'local default', 'static date rollover', 'UTC toggle',
                          'local edit to UTC', 'DST gap/fold rejection', 'blank time blocks solve',
                          'both UTC fold choices preserved', 'responsive layout'])
    result['checks'] += ['four real altitude tracks', 'no qualifying window shown accurately',
                         'apply canonical sky-only suggestion', 'review reset', 'gray-first stages', 'zoom and download',
                         'stored candidate ignored', 'new calibration resets review', 'selected PT day 00:00–23:59',
                         'altitude 0–90 with 20-degree gray band', 'low-alpha night suggestion',
                         'ranked suggestion leaves form unchanged until selected', 'above-cap suggestion labelled',
                         'uploaded recipe defaults and new-calibration reset']
    (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
