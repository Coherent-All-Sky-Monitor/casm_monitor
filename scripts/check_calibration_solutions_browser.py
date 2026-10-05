"""Real saved rank-1 view, then browser-only A/B fixtures; no submitted jobs."""
import copy
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

BASE='http://127.0.0.1:8061'
TAG='sun_20260930_200911'
OTHER='browser_fixture_regular'


def main():
    out=Path('/home/casm/scratch/casm-observation-preview/screenshots/calibration-solutions-20261005')
    out.mkdir(parents=True,exist_ok=True)
    errors,writes=[],[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1600,'height':1100},accept_downloads=True)
        page.on('pageerror',lambda e:errors.append(str(e)))
        def guard(route):
            if route.request.method not in ('GET','HEAD'):
                writes.append(route.request.url);route.abort()
            else:route.continue_()
        page.route('**/api/**',guard)
        page.goto(f'{BASE}/cal/{TAG}',wait_until='domcontentloaded')
        quality=page.locator('#cal-quality')
        quality.locator('svg').wait_for(timeout=60000)
        assert 'Current deployed' in quality.inner_text()
        assert 'full-band median' in quality.inner_text()
        assert 'in-band' not in quality.inner_text().lower()
        assert page.locator('img[src*=svd_vs_freq]').count()==0
        page.locator('#cal-autos .cal-antenna-panel').first.wait_for(timeout=60000)
        quality.scroll_into_view_if_needed()
        page.screenshot(path=str(out/'rank1-saved-data.png'))
        quality.get_by_role('button',name='Expand rank-1 comparison').click()
        with page.expect_download() as downloaded:
            page.locator('.figure-modal').get_by_role('button',name='Download',exact=True).click()
        assert downloaded.value.failure() is None
        page.keyboard.press('Escape')
        detail=page.request.get(f'{BASE}/api/calibration/builds/{TAG}').json()
        ranks=page.request.get(f'{BASE}/api/calibration/builds/{TAG}/rank1').json()
        family=page.request.get(f'{BASE}/api/calibration/builds/{TAG}/subtraction').json()
        fixture_detail=copy.deepcopy(detail)
        fixture_detail['request']['params']['tag']=OTHER
        fixture_detail['request']['params']['static_window']=None
        fixture_detail['request']['comparison_root_tag']=TAG
        family['versions']['regular']={**family['versions']['static'],'tag':OTHER,'pending':False,'ready':True,'static_window':None}
        regular_curve=copy.deepcopy(next(c for c in ranks['curves'] if c['key']=='static'))
        regular_curve.update(key='regular',label='Regular')
        ranks['curves'].insert(0,regular_curve);ranks['unavailable']={}
        requests=[]
        def fixture(route):
            request=route.request;url=urlparse(request.url);path=url.path;requests.append(path)
            if request.method not in ('GET','HEAD'):
                writes.append(request.url);route.abort();return
            if '/api/calibration/builds/' not in path:
                route.continue_();return
            if path.endswith('/subtraction'):
                data=copy.deepcopy(family);data['selected']='regular' if OTHER in path else 'static'
            elif path.endswith('/rank1'):
                data=ranks
            elif path.endswith('/inspection'):
                # Saved arrays seed UI fixtures only. Never published as scientific evidence.
                original=request.url.replace(OTHER,TAG)
                response=route.fetch(url=original);data=response.json()
                if OTHER in path:
                    data['static_used']=False
                    data['schemes']['matched']=False
                    for panel in data['antenna_panels']:
                        panel['applied_phase']=[None if v is None else v*.8 for v in panel['applied_phase']]
            elif path.endswith('/'+OTHER):data=fixture_detail
            else:route.continue_();return
            route.fulfill(json=data)
        page.unroute('**/api/**',guard);page.route('**/api/**',fixture)
        page.reload(wait_until='domcontentloaded')
        page.locator('#cal-cross .cal-antenna-panel').first.wait_for(timeout=60000)
        controls=page.get_by_role('group',name='Static sky comparison',exact=True)
        controls.get_by_role('button',name='Compare',exact=True).click()
        expect(page.locator('#cal-cross .cal-subtraction-pair').first.locator('section')).to_have_count(2,timeout=60000)
        page.wait_for_function('!document.querySelector(".cal-view-pending")')
        assert page.locator('#cal-quality .cal-rank1-key>span').count()==3
        assert page.locator('#cal-weights .cal-curve-key').first.locator('span').count()==2
        page.get_by_role('group',name='Cross-correlation view',exact=True).get_by_role('button',name='Spectrum',exact=True).click()
        expect(page.locator('#cal-cross .cal-curve-key').first.locator('span')).to_have_count(2)
        page.get_by_role('group',name='Panel layout',exact=True).get_by_role('button',name='SNAP order',exact=True).click()
        page.wait_for_timeout(4500)  # Polling must preserve layout and comparison choices.
        expect(page.get_by_role('group',name='Panel layout').get_by_role('button',name='SNAP order',exact=True)).to_have_attribute('aria-pressed','true')
        page.locator('#cal-cross .cal-plot-button').first.click()
        assert page.locator('.figure-modal .cal-curve-key span').count()==2
        page.keyboard.press('Escape')
        controls.get_by_role('button',name='Without subtraction',exact=True).click()
        page.wait_for_function('!document.querySelector(".cal-view-pending")')
        assert 'Selected for next steps: With subtraction' in page.locator('.cal-subtraction-selector').inner_text()
        page.locator('.cal-solution-actions>div').first.get_by_role('button',name='Use this solution').click()
        expect(page).to_have_url(f'{BASE}/cal/{OTHER}')
        expect(page.locator('.cal-subtraction-selector')).to_contain_text('Selected for next steps: Without subtraction')
        page.get_by_role('button',name='Generate beams',exact=False).first.click()
        assert OTHER in page.url
        page.get_by_role('button',name='Solve & inspect',exact=False).first.click()
        for width in (1280,768,390):
            page.set_viewport_size({'width':width,'height':1000})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
        assert not errors and not writes,(errors,writes)
        report=dict(real_saved_rank1=True,rank1_download=True,fixture_ab_controls=True,
                    paired_crosses_weights=True,polling_preserves_choices=True,selection_carries_forward=True,
                    errors=errors,writes=writes,fixture_scope='A/B presentation only; no new scientific solve')
        (out/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
        browser.close()


if __name__=='__main__':main()
