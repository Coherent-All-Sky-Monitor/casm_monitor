"""Read-only browser checks of cached-plot failure handling and playback smoke.

Failures and a 61-second browser clock advance are synthetic, injected only into
this isolated page. No hardware requests or production state changes are allowed.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

BASE='http://127.0.0.1:8061'


def main():
    errors=[];blocked=[];fail=set()
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-dev-shm-usage','--disable-gpu'])
        page=browser.new_page(viewport=dict(width=1500,height=1100),accept_downloads=True)
        page.on('pageerror',lambda e:errors.append(str(e)))
        def guard(route):
            r=route.request;path=urlsplit(r.url).path
            if r.method not in ('GET','HEAD'):
                blocked.append(r.url);route.abort()
            elif path in fail:
                route.fulfill(status=503,json={'detail':'Synthetic refresh failure'})
            else:
                route.continue_()
        page.route('**/api/**',guard)
        def ready(selector):
            page.wait_for_function(selector,polling=50,timeout=90000)
        page.goto(BASE+'/vis',wait_until='domcontentloaded')
        ready("document.querySelectorAll('.antenna-plot svg').length===16 && document.querySelector('.array-results').getAttribute('aria-busy')==='false'")
        before=page.locator('.antenna-plot svg').first.inner_html()
        fail.add('/api/science/array')
        page.get_by_role('button',name='Refresh',exact=True).click()
        page.get_by_text('Synthetic refresh failure',exact=False).wait_for()
        assert page.locator('.antenna-plot svg').first.inner_html()==before
        fail.clear()
        page.get_by_role('button',name='Refresh',exact=True).click()
        ready("!document.querySelector('.workspace-notice') && document.querySelector('.array-results').getAttribute('aria-busy')==='false'")
        page.get_by_role('button',name='History',exact=True).click()
        ready("document.querySelector('.vis-movie .snap-movie-stage')?.getAttribute('aria-busy')==='false'")
        at=page.locator('.vis-movie .snap-movie-stage').get_attribute('data-frame')
        page.get_by_role('button',name='Play visibility playback',exact=True).click()
        page.wait_for_function('(v)=>document.querySelector(".vis-movie .snap-movie-stage")?.dataset.frame!==v',arg=at,timeout=15000)
        page.get_by_role('button',name='Pause visibility playback',exact=True).click()
        page.get_by_role('button',name='Close visibility playback',exact=True).click()
        page.get_by_role('navigation',name='Main navigation').get_by_role('link',name='SNAPs',exact=True).click()
        ready("document.querySelectorAll('.snap-spectrum-card svg').length===16")
        page.get_by_role('button',name='History',exact=True).click()
        ready("document.querySelectorAll('.snap-movie-card').length===16")
        old=page.get_by_label('Playback saved time').input_value()
        page.get_by_role('button',name='Play playback',exact=True).click()
        page.wait_for_function('(v)=>document.querySelector(`[aria-label="Playback saved time"]`)?.value!==v',arg=old,timeout=15000)
        page.get_by_role('button',name='Pause playback',exact=True).click()
        page.get_by_role('button',name='Close playback',exact=True).click()
        before=page.locator('.snap-spectrum-card svg').first.locator('path').evaluate_all('(es)=>es.map(e=>e.getAttribute("d"))')
        fail.update(['/api/snap-workspace/health','/api/snap-workspace/spectra'])
        page.evaluate('''()=>{const original=Date.now;Date.now=()=>original()+61000;
          document.dispatchEvent(new Event('visibilitychange'));}''')
        page.get_by_text('Status refresh failed. Current state is unknown.',exact=True).wait_for()
        page.get_by_text('Synthetic refresh failure',exact=False).wait_for()
        ready("document.querySelectorAll('.snap-spectrum-card svg').length===16")
        after=page.locator('.snap-spectrum-card svg').first.locator('path').evaluate_all('(es)=>es.map(e=>e.getAttribute("d"))')
        assert after==before
        assert page.locator('.snap-health-badge.ok').count()==0
        assert not errors,errors
        assert not blocked,blocked
        browser.close()
    output=Path('/home/casm/scratch/casm-observation-preview/navigation-freshness-20261006.json')
    output.write_text(json.dumps(dict(passed=True,synthetic_failures=True,
        checks=['visibility stale plot retained and retry succeeds','visibility playback advances',
                'SNAP playback advances','SNAP stale plot retained','failed PPS/stream health cannot stay green'],
        errors=errors,blocked=blocked),indent=2)+'\n')
    print('PASS',output,flush=True)


if __name__=='__main__':
    main()
