"""Bounded, read-only tab-navigation acceptance. Never acquires or uploads.

Route guarding disables HTTP cache in Playwright. Revisit timings therefore test
the application memory cache, not fingerprinted-asset caching. Optional network
emulation is a synthetic browser link, not a measurement of an actual SSH tunnel.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


READY = {
    'Overview': "document.querySelector('.overview-injections svg') && [...document.querySelectorAll('.overview-diagnostics img')].length>=2 && [...document.querySelectorAll('.overview-diagnostics img')].every(i=>i.complete&&i.naturalWidth)",
    'Search (T1)': "document.querySelectorAll('.stream-cell').length===8 && [...document.querySelectorAll('.plot-surface img')].some(i=>i.complete&&i.naturalWidth)",
    'Visibilities': "document.querySelectorAll('.array-results .antenna-plot svg').length>=16",
    'SNAPs': "document.querySelectorAll('.snap-spectrum-card svg').length>=16",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8061')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--latency-ms', type=float, default=0)
    parser.add_argument('--mbps', type=float, default=0)
    parser.add_argument('--controls', action='store_true')
    args = parser.parse_args()
    report = dict(at_utc=datetime.now(timezone.utc).isoformat(), phases=[], errors=[], blocked=[],
                  synthetic_latency_ms=args.latency_ms, synthetic_mbps=args.mbps)
    requests=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-dev-shm-usage','--disable-gpu'])
        page=browser.new_page(viewport=dict(width=1500,height=1100))
        page.set_default_timeout(30000)
        page.on('pageerror',lambda e:report['errors'].append(str(e)))
        def guard(route):
            r=route.request
            if r.method not in ('GET','HEAD','OPTIONS') and urlsplit(r.url).path!='/api/science/render':
                report['blocked'].append(r.url);route.abort()
            else:
                route.continue_()
        page.route('**/api/**',guard)
        if args.latency_ms or args.mbps:
            cdp=page.context.new_cdp_session(page)
            cdp.send('Network.enable')
            cdp.send('Network.emulateNetworkConditions',dict(offline=False,latency=args.latency_ms,
                     downloadThroughput=args.mbps*125000 if args.mbps else -1,
                     uploadThroughput=args.mbps*125000 if args.mbps else -1))
        def finished(r):
            if '/api/' not in r.url:return
            try:
                requests.append(dict(url=r.url.removeprefix(args.url),method=r.method,
                    duration_ms=r.timing.get('responseEnd'),bytes=r.sizes().get('responseBodySize')))
            except Exception:
                pass
        page.on('requestfinished',finished)
        page.goto(args.url+'/observation',wait_until='domcontentloaded')
        page.wait_for_function(READY['Overview'],timeout=90000)
        def nav(label):
            return page.get_by_role('navigation',name='Main navigation').get_by_role('link',name=label,exact=True)
        for label in ['Search (T1)','Visibilities','SNAPs','Overview','Search (T1)','Visibilities','SNAPs']:
            mark=len(requests)
            started=nav(label).evaluate('e=>{const t=performance.now();e.click();return t}')
            page.wait_for_function('(label)=>document.querySelector("main h2")?.textContent===label',arg=label,polling=25,timeout=5000)
            shell=page.evaluate('performance.now()')-started
            page.wait_for_function(READY[label],polling=25,timeout=90000)
            loaded=page.evaluate('performance.now()')-started
            page.wait_for_timeout(400)
            phase=dict(tab=label,shell_ms=round(shell),plots_dom_ready_ms=round(loaded),requests=requests[mark:])
            report['phases'].append(phase)
            args.output.write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps(phase),flush=True)
        snap_reads=[r for r in report['phases'][2]['requests'] if '/spectra?v=' in r['url']]
        assert len(snap_reads)==1, snap_reads
        assert not any('/spectra?v=' in r['url'] for r in report['phases'][-1]['requests'])
        assert all('images=none' in r['url'] for r in requests if '/api/science/array?' in r['url'])
        if args.controls:
            switch=page.get_by_role('switch',name='All 12 ADCs per SNAP',exact=False)
            switch.click();assert page.locator('.snap-spectrum-card').count()==48
            page.get_by_role('button',name='Compact · station order',exact=True).click()
            nav('Visibilities').click();page.wait_for_function(READY['Visibilities'])
            nav('SNAPs').click();page.wait_for_function(READY['SNAPs'])
            assert page.get_by_role('switch',name='All 12 ADCs per SNAP',exact=False).get_attribute('aria-checked')=='true'
            assert page.locator('.snap-spectrum-card').count()==48
            assert page.get_by_role('button',name='Compact · station order',exact=True).get_attribute('aria-pressed')=='true'
            switch.click()
            page.locator('.snap-plot-trigger').first.click();page.get_by_role('dialog').wait_for()
            page.get_by_role('button',name='Zoom in',exact=True).click();page.keyboard.press('Escape')
            nav('Visibilities').click();page.wait_for_function(READY['Visibilities'])
            page.get_by_role('button',name='Cross-correlations',exact=True).click()
            page.wait_for_function("document.querySelectorAll('.antenna-plot img').length>=16",timeout=90000)
            for quantity in ['Real(V)','Imag(V)','Phase','|V|']:
                page.get_by_role('button',name=quantity,exact=True).click()
                page.wait_for_function("document.querySelectorAll('.antenna-plot img').length>=16",timeout=90000)
                page.wait_for_function("document.querySelector('.array-results')?.getAttribute('aria-busy')==='false'")
            page.get_by_role('button',name='SNAP order',exact=True).click()
            ref=page.get_by_label('Reference antenna');old=ref.input_value()
            replacement=ref.locator('option').evaluate_all('(es)=>es.map(e=>e.value)').pop()
            ref.select_option(replacement)
            page.wait_for_function("document.querySelectorAll('.antenna-plot img').length>=16",timeout=90000)
            nav('SNAPs').click();page.wait_for_function(READY['SNAPs'])
            nav('Visibilities').click()
            page.wait_for_function("document.querySelectorAll('.antenna-plot img').length>=16")
            assert page.get_by_label('Reference antenna').input_value()==replacement
            assert page.get_by_role('button',name='SNAP order',exact=True).get_attribute('aria-pressed')=='true'
            page.locator('.plot-zoom-trigger').first.click();page.get_by_role('dialog').wait_for()
            page.get_by_role('button',name='Zoom in',exact=True).click();page.keyboard.press('Escape')
            # Bounded triangle: six tiles, including diagonals, no eager full matrix.
            page.get_by_role('button',name='Clear',exact=True).click()
            for button in page.locator('.map-antenna').all()[:3]:button.click()
            page.get_by_role('button',name='All pairs',exact=True).click()
            page.wait_for_function("document.querySelectorAll('.pair-dynamics td img').length===6",timeout=90000)
            assert page.locator('.matrix-unused').count()==3
            report['controls']='Passed: SNAP 48/default inputs, saved selections, zoom, all cross quantities, changed reference, upper triangle'
            page.get_by_role('button',name='Autocorrelations',exact=True).click()
            report['reference_before_test']=old
        assert not report['errors'], report['errors']
        assert not report['blocked'], report['blocked']
        args.output.write_text(json.dumps(report,indent=2)+'\n')
        browser.close()
    print('PASS',args.output,flush=True)


if __name__=='__main__':
    main()
