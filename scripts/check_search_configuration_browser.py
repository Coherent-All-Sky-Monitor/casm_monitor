"""Search configuration layout/interaction checks, GETs and browser fixtures only."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from playwright.sync_api import sync_playwright, expect

BASE = 'http://127.0.0.1:8061'
OUTPUT = Path('/home/casm/scratch/search-configuration-widget')


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with build_opener(ProxyHandler({})).open(BASE+'/api/t1/configuration', timeout=15) as response:
        live = json.load(response)
    assert live['all_match'], 'Live layout check expects eight matching configurations'
    errors, writes = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1600,'height':1050})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: writes.append(r.url) if r.method not in ('GET','HEAD') else None)
        page.goto(BASE+'/search',wait_until='domcontentloaded')
        card = page.get_by_role('region',name='Current HELLA search configuration')
        expect(card.get_by_text('All 8 match',exact=True)).to_be_visible(timeout=30000)
        expect(card.get_by_role('table',name='Flagging settings')).to_contain_text('ON')
        expect(card.get_by_role('table',name='Search settings')).to_contain_text('1–64')
        expect(card.get_by_role('table',name='Flagging settings')).to_contain_text('470.00–484.38 MHz')
        expect(card.get_by_role('table',name='Flagging settings')).to_contain_text('0–471')
        expect(card.get_by_role('img')).to_have_count(0)
        expect(card.get_by_role('combobox')).to_have_count(0)
        for width in (1600,1440,800,390):
            page.set_viewport_size({'width':width,'height':1050})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), width
            card.screenshot(path=str(OUTPUT/f'configuration-{width}.png'))
        page.set_viewport_size({'width':1600,'height':1050})
        page.screenshot(path=str(OUTPUT/'search-live.png'))
        card.locator('summary').click()
        expect(card.locator('.search-config-table')).to_contain_text('4 · corr2')
        expect(card).to_contain_text('1 1 1000000 1')
        page.close()

        for mode in ('mixed','off','unknown','failure','interior','invalid'):
            fixture = copy.deepcopy(live)
            fixture['checked_utc'] = datetime.now(timezone.utc).isoformat()
            if mode == 'mixed':
                fixture.update(all_match=False,common=None)
                fixture['streams'][4]['settings']['snr'] = 13
            elif mode == 'off':
                fixture['common']['sumthreshold']['enabled'] = False
                fixture['common']['mask']['enabled'] = False
            elif mode == 'unknown':
                fixture.update(all_match=False,common=None,verified_count=0)
                for stream in fixture['streams']:
                    stream.update(status='unknown',settings=None,reason='Configuration edited after launch')
            elif mode == 'interior':
                fixture['common']['mask'].update(enabled=True,first=1000,last=1500)
            elif mode == 'invalid':
                fixture['common']['mask'].update(enabled=True,first=-1,last=100)
            context = browser.new_context(viewport={'width':1440,'height':1050})
            context.route('**/api/t1/configuration',lambda route,_request,body=fixture,mode=mode:route.fulfill(
                status=503 if mode=='failure' else 200,content_type='application/json',body=json.dumps(body)))
            page = context.new_page()
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(BASE+'/search',wait_until='domcontentloaded')
            card = page.get_by_role('region',name='Current HELLA search configuration')
            if mode == 'mixed':
                expect(card).to_contain_text('Settings differ',timeout=15000)
                expect(card.locator('.search-settings-table .on')).to_have_count(0)
                card.locator('summary').click()
                expect(card.locator('.search-config-table tbody tr').nth(4)).to_contain_text('13')
            elif mode == 'off':
                expect(card.locator('.search-settings-table .off')).to_have_count(2)
                expect(card.get_by_role('row',name='Masked frequencies None',exact=True)).to_be_visible()
                expect(card.get_by_role('row',name='Masked channels None',exact=True)).to_be_visible()
            elif mode == 'interior':
                expect(card.get_by_role('table',name='Flagging settings')).to_contain_text('1000–1500')
            elif mode == 'invalid':
                expect(card.get_by_role('row',name='Channel mask Unknown',exact=True)).to_be_visible()
                expect(card.get_by_role('row',name='Masked frequencies —',exact=True)).to_be_visible()
            else:
                expect(card.locator('.search-config-note')).to_contain_text(
                    'Configuration read unavailable' if mode=='failure' else 'Some streams are unverified')
                expect(card.locator('.search-settings-table .on')).to_have_count(0)
                expect(card.get_by_role('row',name='Channel mask Unknown',exact=True)).to_be_visible()
            context.close()
        browser.close()
    assert not errors, errors
    assert not writes, writes
    print(json.dumps({'checks':'live tables/ranges, responsive, no selector/bar, stream details, mixed, off, unknown, failed read, interior/invalid mask',
                      'errors':errors,'writes':writes,'screenshots':str(OUTPUT)},indent=2))


if __name__ == '__main__':
    main()
