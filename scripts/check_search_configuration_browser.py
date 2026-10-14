"""Search configuration layout/interaction checks, GETs and browser fixtures only."""
import copy
import json
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
        expect(card.locator('.search-config-metrics')).to_contain_text('ON')
        expect(card.locator('.search-config-metrics')).to_contain_text('1–64')
        for width in (1600,1440,800,390):
            page.set_viewport_size({'width':width,'height':1050})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), width
            card.screenshot(path=str(OUTPUT/f'configuration-{width}.png'))
        page.set_viewport_size({'width':1600,'height':1050})
        page.screenshot(path=str(OUTPUT/'search-live.png'))
        card.get_by_role('button',name='4',exact=True).click()
        expect(card.get_by_text('Stream 4 · corr2',exact=True)).to_be_visible()
        card.locator('summary').click()
        expect(card).to_contain_text('/tmp/hella_4.cfg')
        expect(card).to_contain_text('1 1 1000000 1')
        page.close()

        for mode in ('mixed','off','unknown','failure'):
            fixture = copy.deepcopy(live)
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
            context = browser.new_context(viewport={'width':1440,'height':1050})
            context.route('**/api/t1/configuration',lambda route,_request,body=fixture,mode=mode:route.fulfill(
                status=503 if mode=='failure' else 200,content_type='application/json',body=json.dumps(body)))
            page = context.new_page()
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(BASE+'/search',wait_until='domcontentloaded')
            card = page.get_by_role('region',name='Current HELLA search configuration')
            if mode == 'mixed':
                expect(card).to_contain_text('Settings differ',timeout=15000)
                expect(card.locator('.search-config-metrics .on')).to_have_count(0)
                card.get_by_role('button',name='4',exact=True).click()
                expect(card.locator('.search-config-metrics')).to_contain_text('≥ 13')
            elif mode == 'off':
                expect(card.locator('.search-config-metrics .off')).to_have_text('OFF')
                expect(card.locator('.search-band-track i')).to_have_count(0)
            else:
                expect(card.locator('.search-config-note')).to_contain_text(
                    'Configuration read unavailable' if mode=='failure' else 'Some streams are unverified')
                expect(card.locator('.search-config-metrics .on')).to_have_count(0)
            context.close()
        browser.close()
    assert not errors, errors
    assert not writes, writes
    print(json.dumps({'checks':'live, responsive, stream details, mixed, off, unknown, failed read',
                      'errors':errors,'writes':writes,'screenshots':str(OUTPUT)},indent=2))


if __name__ == '__main__':
    main()
