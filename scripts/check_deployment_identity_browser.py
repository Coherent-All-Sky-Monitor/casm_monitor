"""Read-only live deployment labels; never submit release actions."""
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    root = Path('/home/casm/scratch/casm-observation-preview/screenshots/deployment-identity-20261006')
    root.mkdir(parents=True, exist_ok=True)
    base = 'http://127.0.0.1:8061'
    tag = 'sun_20261005_201801'
    errors, blocked = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport=dict(width=1600, height=1050), reduced_motion='reduce')
        def readonly(route):
            if route.request.method not in ('GET', 'HEAD'):
                blocked.append(route.request.url)
                route.abort()
            else:
                route.continue_()
        context.route('**/api/**', readonly)
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(base+'/cal', wait_until='domcontentloaded')
        current = page.get_by_role('region', name='Current deployed calibration')
        expect(current).to_contain_text('Oct 5, 2026', timeout=60000)
        expect(current).to_contain_text('12:11 – 13:11 PDT')
        expect(current).to_contain_text('Oct 6, 2026')
        expect(current).not_to_contain_text('a7658dba9b34376a')
        membership = page.locator('.cal-card').filter(has=page.get_by_role('heading', name='Antenna membership', exact=True))
        expect(membership).to_contain_text('Default: 16 deployed antennas.')
        expect(membership.locator('input[type=checkbox]:checked')).to_have_count(16)
        expect(membership).not_to_contain_text('include_in_beamforming')
        expect(membership).not_to_contain_text('2026-09-09 finding')
        current.get_by_role('button', name='UTC', exact=True).evaluate('e=>e.click()')
        expect(current).to_contain_text('19:11 – 20:11 UTC')
        page.goto(base+'/cal/'+tag, wait_until='domcontentloaded')
        page.locator('.cal-steps').get_by_role('button', name='Export & deploy', exact=False).evaluate('e=>e.click()')
        release = page.locator('.cal-release')
        expect(release.get_by_text('Saved weights · '+tag)).to_be_visible(timeout=60000)
        expect(release.get_by_role('button', name='Copy folder path')).to_be_visible()
        expect(release.get_by_role('button', name='Upload reviewed CB & IB')).to_be_disabled()
        # Hidden technical evidence retains identities; ordinary visible labels do not.
        visible = release.inner_text()
        assert '47aa44d443094c86a0f16f1e3d7c5731' not in visible
        assert 'SHA-256' not in visible
        screenshots = []
        try:
            page.screenshot(path=str(root/'export-labels.png'), animations='disabled', timeout=8000)
            screenshots.append('export-labels.png')
        except Exception as exc:
            screenshots.append('capture unavailable: '+str(exc).splitlines()[0])
        assert not errors, errors
        assert not blocked, blocked
        report = dict(current_calibration='2026-10-05 Sun', labels='readable; hashes remain in metadata',
                      membership='16 deployed antennas; layout mismatch banner hidden',
                      errors=errors, blocked_writes=blocked, screenshots=screenshots)
        (root/'result.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report, indent=2))
        browser.close()


if __name__ == '__main__':
    main()
