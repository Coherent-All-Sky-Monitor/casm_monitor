"""Fixture-only SNAP/Overview colour check. All API requests are intercepted.

Only frontend assets are read from 8061; no hardware, actions or data writes
are forwarded. Fixture colours are display tests, not live telescope status.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

from casm_monitor.web.snap_spectra import aggregate_health
from casm_monitor.web.snap_timing_status import apply_baseline, INCONSISTENT


def health_fixture(before, after, unreadable=False):
    ips = ['192.168.120.52', '192.168.120.51', '192.168.120.62', '192.168.120.73']
    now = time.time()
    boards = []
    for n, ip in enumerate(ips):
        a, b = (before, after) if n in (1, 3) else (0, 0)
        state, detail = ('unknown', INCONSISTENT) if a != b else (
            ('attention', f'TT offset {b} ticks') if b else ('ok', 'PPS advancing'))
        pps = dict(state=state, detail=detail, fresh=True, period_ok=True,
                   ts=now, reference_ip=ips[0], delta_ticks_before=str(a), delta_ticks=str(b))
        if unreadable and n in (1, 3):
            pps.update(state='unknown', detail='Control read unavailable')
        boards.append(dict(ip=ip, feng_id=n, slot=['A', 'I', 'D', 'F'][n],
                           latest_attempt=now, control_status='ok', pps_status=pps,
                           streaming=dict(state='ok', detail='Fixture cached visibility', ts=now)))
    apply_baseline(boards, dict(version=1, accepted_at=now-100, reference_ip=ips[0],
                               offsets=dict(zip(ips, ['0', '-1', '0', '-1']))))
    return dict(boards=boards, checked_at=now,
                **{key: aggregate_health(boards, field) for key, field in
                   [('streaming', 'streaming'), ('pps', 'pps_status'), ('timing', 'timing_status')]})


def main():
    output = Path('/home/casm/scratch/casm-observation-preview/snap-timing-policy-20261009-two-tick')
    output.mkdir(parents=True, exist_ok=True)
    errors, writes, results = [], [], []
    cases = [('negative', 'ok', -1, -2, False, 'ok', 'Within tolerance', 'rgb(23, 106, 64)'),
             ('zero', 'ok', 0, -1, False, 'ok', 'Within tolerance', 'rgb(23, 106, 64)'),
             ('limits', 'ok', -2, 2, False, 'ok', 'Within tolerance', 'rgb(23, 106, 64)'),
             ('low', 'attention', -3, -1, False, 'silent', 'Needs attention', 'rgb(180, 46, 56)'),
             ('high', 'attention', 2, 3, False, 'silent', 'Needs attention', 'rgb(180, 46, 56)'),
             ('unreadable', 'unknown', -1, 0, True, 'unknown', 'Unknown', 'rgb(82, 99, 123)')]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for name, state, before, after, unreadable, tone, label, colour in cases:
            fixture = health_fixture(before, after, unreadable)
            context = browser.new_context(viewport=dict(width=1500, height=1000))
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))

            def route_api(route):
                req = route.request
                if req.method not in ('GET', 'HEAD'):
                    writes.append(req.url); route.abort(); return
                path = urlsplit(req.url).path
                value = {}
                if path == '/api/snap-workspace/health': value = fixture
                elif path == '/api/snap-workspace/acquisition':
                    value = dict(configured_interval_s=3600, manual_enabled=False)
                elif path == '/api/snap-workspace/spectra': value = dict(boards=[], freq_mhz=[])
                elif path == '/api/snap-workspace/beamforming': value = dict(status='complete', inputs=[])
                elif path == '/api/observation':
                    value = dict(clock=dict(utc=datetime.now(timezone.utc).isoformat(), lst='12:00:00'),
                                 observation=dict(id='Fixture display test'), layout=dict(points=[]))
                elif path == '/api/observation/injections':
                    value = dict(status='unavailable', window_start_utc='2026-10-07T00:00:00Z',
                                 window_end_utc='2026-10-08T00:00:00Z', trials=[])
                route.fulfill(json=value)

            context.route('**/api/**', route_api)
            page.goto('http://127.0.0.1:8061/snaps', wait_until='domcontentloaded')
            expect(page.locator('.snap-health-chip')).to_have_count(4)
            for n in (1, 3):
                badge = page.locator('.snap-health-chip').nth(n).locator('.snap-health-badge').nth(1)
                expect(badge).to_have_class('snap-health-badge '+state)
                expect(badge).to_contain_text(label)
                expect(badge).to_contain_text(f'{before} → {after} ticks')
                assert badge.evaluate('e=>getComputedStyle(e).backgroundColor') == colour
            # Streaming stays independently green even when PPS is red/unknown.
            for n in range(4):
                expect(page.locator('.snap-health-chip').nth(n).locator('.snap-health-badge').first).to_have_class('snap-health-badge ok')
            page.get_by_text('Board status details', exact=True).click()
            expect(page.locator('.snap-health-board').nth(1).locator('.snap-health-box').nth(1)).to_have_class('snap-health-box '+state)
            page.locator('.snap-health-section').screenshot(path=str(output/f'{name}-snaps.png'))
            page.get_by_role('navigation', name='Main navigation').get_by_role('link', name='Overview', exact=True).click()
            card = page.locator('.overview-stat').filter(has_text='SNAP PPS timing')
            expect(card).to_have_class('overview-stat '+tone)
            expect(card.locator('.overview-status')).to_have_text(label)
            expect(card.locator('strong')).to_have_text('4 / 4' if state == 'ok' else '2 / 4')
            card.screenshot(path=str(output/f'{name}-overview.png'))
            results.append(dict(case=name, state=state, overview_tone=tone, label=label))
            context.close()
        browser.close()
    assert not errors, errors
    assert not writes, writes
    report = dict(passed=True, fixture_only=True, cases=results, errors=errors, blocked_writes=writes)
    (output/'result.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
