"""Synthetic browser acceptance: caching only, no science/hardware reads."""
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

URL = 'http://127.0.0.1:8061'
PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII='


def main():
    dates = ['2026-09-24','2026-09-23','2026-09-22']
    cal = {'id':'a'*32,'name':'deployed.h5','path':'/fixture/deployed.h5'}
    reads, errors, blocked = Counter(), [], []
    def row(source,day):
        t = datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()+43200
        return dict(date=day,transit_unix=t,cache_key=f'{source}-{day}-{cal["id"]}',t0=t-7200,t1=t+7200)

    def route_api(route):
        url = urlparse(route.request.url)
        if route.request.method != 'GET':
            blocked.append(route.request.url)
            route.abort()
        elif url.path == '/api/sources':
            name = parse_qs(url.query)['q'][0]
            source = {'Sun':'sun','Cyg A':'cyg_a','Cas A':'cas_a','Tau A':'tau_a'}.get(name)
            body = dict(sources=[]) if not source else dict(kind='visibility_beam',source=source,name=name,
                calibration=cal,window_hours=4,max_transits=3,transits=[row(source,d) for d in dates])
            route.fulfill(json=body)
        elif url.path.startswith('/api/sources/transits/'):
            source,day = url.path.split('/')[-2:]
            reads[source] += 1
            r = row(source,day)
            route.fulfill(json=dict(**r,source=source,name=source,calibration=cal,samples=3,antenna_ids=[9,19],
                partial=False,details={},integration_s=7200,freq_mhz=[480,400],
                tile=dict(src=PNG,scale='symmetric',min=-1,max=1,log=False,width=1,height=1),
                light_curve=dict(time_unix=[r['t0'],r['transit_unix'],r['t1']],cross_power=[0,1,0],channels=2,freq_range_mhz=[400,480])))
        else:
            route.fulfill(json={})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,args=['--disable-dev-shm-usage'])
        page = browser.new_page(viewport=dict(width=1500,height=1100))
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('**/api/**',route_api)
        page.goto(URL+'/sources',wait_until='domcontentloaded')

        def choose(name):
            page.get_by_role('button',name=name,exact=True).click(force=True)
            page.wait_for_function('document.querySelectorAll(".source-history-page > section:not([hidden]) .array-dynamic img").length===3',polling=100)

        choose('Sun')
        assert reads['sun']==3
        choose('Cyg A')
        choose('Sun')
        assert reads==Counter(sun=3,cyg_a=3),reads
        page.reload(wait_until='domcontentloaded')
        choose('Sun')
        assert reads['sun']==3,'Reload ignored persistent browser cache'
        dates[:] = ['2026-09-25','2026-09-24','2026-09-23']
        page.reload(wait_until='domcontentloaded')
        choose('Sun')
        assert reads['sun']==4,'Only the new completed date should load'
        keys = page.evaluate('async()=> (await (await caches.open("casm-transit-figures-v1")).keys()).map(r=>r.url)')
        sun_keys = [k for k in keys if '/sun/' in k]
        assert len(sun_keys)==3 and not any('2026-09-22' in k for k in sun_keys)
        # A newly uploaded deployment changes the cal identity; a trial build
        # does not change this catalogue (covered by the Python ledger test).
        cal['id']='b'*32
        cal['name']='new-deployed.h5'
        page.reload(wait_until='domcontentloaded')
        choose('Sun')
        assert reads['sun']==7,'New deployed calibration should refresh all three dates'
        assert not errors,errors
        assert not blocked,blocked
        browser.close()
    print('PASS: instant source return, browser-reload cache, three-date eviction, deployment invalidation; no hardware requests')


if __name__=='__main__':
    main()
