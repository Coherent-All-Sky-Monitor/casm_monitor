"""Read-only live membership, unified navigation and heading acceptance check."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = 'http://127.0.0.1:8061'
OUT = Path('/home/casm/scratch/casm-observation-preview/screenshots')
TABS = ['Overview', 'Search (T1)', 'Visibilities', 'SNAPs', 'Source history', 'Candidates', 'Calibration']


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    screenshots, errors, unexpected = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        page = browser.new_page(viewport=dict(width=1500, height=1100))
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script('''const original=window.setInterval;
            window.setInterval=(fn,ms,...args)=>ms===60000?
                (window.arrayRefreshForTest=fn,987654):original(fn,ms,...args);''')

        def guard(route):
            if route.request.method != 'GET' and not route.request.url.endswith('/api/science/render'):
                unexpected.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        page.route('**/api/**', guard)
        membership = page.request.get(URL+'/api/snap-workspace/beamforming').json()
        overview = page.request.get(URL+'/api/observation').json()
        assert membership['status'] == 'complete'
        members = {i['packet_idx'] for i in membership['inputs'] if i['beamforming']}
        assert members == {i['packet_idx'] for i in overview['layout']['points'] if i.get('deployed') is True}
        page.goto(URL+'/vis', wait_until='domcontentloaded')
        page.wait_for_function('document.querySelector(".array-results")?.getAttribute("aria-busy")==="false"', polling=100, timeout=90000)
        panels = lambda: set(page.locator('.antenna-plot').evaluate_all('es=>es.map(e=>Number(e.dataset.packet))'))
        assert panels() == members
        assert page.locator('.map-antenna.beamforming').count() == len(members)
        assert page.locator('.antenna-plot.in-beamforming').count() == len(members)
        green = page.locator('.map-antenna.beamforming').first.evaluate('e=>getComputedStyle(e).backgroundColor')
        assert green == 'rgb(22, 78, 54)'
        assert page.locator('.diagnostic-nav a').all_text_contents() == TABS
        assert page.locator('.header nav').count() == 0
        assert page.locator('.diagnostic-nav a[aria-current=page]').inner_text() == 'Visibilities'
        page.get_by_role('button', name='All wired', exact=True).click(force=True)
        assert len(panels()) == page.locator('.map-antenna').count()
        assert page.locator('.antenna-plot.in-beamforming').count() == len(members)
        page.get_by_role('button', name=f'Beamforming · {len(members)}', exact=True).click(force=True)
        page.locator('.map-antenna.beamforming').first.click(force=True)
        custom = panels()
        assert len(custom) == len(members)-1
        with page.expect_response(lambda r: '/api/science/array?' in r.url, timeout=90000):
            page.evaluate('window.arrayRefreshForTest()')
        page.wait_for_function('document.querySelector(".array-results")?.getAttribute("aria-busy")==="false"', polling=100)
        assert panels() == custom, 'Manual selection lost on refresh'
        page.get_by_role('button', name=f'Beamforming · {len(members)}', exact=True).click(force=True)
        assert panels() == members
        for arrangement in ['SNAP order', 'Station grid', 'Compact panels']:
            page.get_by_role('button', name=arrangement, exact=True).click(force=True)
            assert panels() == members

        def screenshot(name):
            try:
                page.screenshot(path=str(OUT/name), timeout=10000, animations='disabled')
                screenshots.append(name)
            except Exception as exc:
                print('Screenshot unavailable:', name, str(exc).splitlines()[0])

        screenshot('vis-beamforming-default.png')
        page.goto(URL+'/observation', wait_until='domcontentloaded')
        page.wait_for_function('document.querySelectorAll(".overview-map .beamforming").length>0', polling=100, timeout=90000)
        page.wait_for_function('document.querySelector(".overview-photo img")?.complete && document.querySelector(".overview-photo img")?.naturalWidth>0',polling=100,timeout=30000)
        page.locator('.overview-photo img').evaluate('img=>img.decode()')
        page.wait_for_function("!document.querySelector('a.overview-stat[href=\"/snaps\"] strong')?.textContent?.includes('Unknown')",polling=100,timeout=30000)
        assert page.locator('.overview-map .beamforming').first.evaluate('e=>getComputedStyle(e).backgroundColor') == green
        for width in [1500, 1024, 700, 390, 320]:
            page.set_viewport_size(dict(width=width, height=1100))
            page.wait_for_timeout(150)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), width
            assert page.locator('.header__title').evaluate('e=>parseFloat(getComputedStyle(e).fontSize)') >= 27
            assert page.locator('.overview-heading h2').evaluate('e=>parseFloat(getComputedStyle(e).fontSize)') >= 32
            clocks = page.locator('.overview-clock').evaluate_all('es=>es.map(e=>e.getBoundingClientRect().top)')
            if width >= 600:
                assert abs(clocks[0]-clocks[1]) < 1
            if width in (1500, 390):
                screenshot(f'overview-heading-{width}.png')
        page.goto(URL+'/cal', wait_until='domcontentloaded')
        page.wait_for_function('document.querySelector(".diagnostic-nav a[aria-current=page]")?.textContent==="Calibration"', polling=100)
        assert page.locator('.diagnostic-nav a').all_text_contents() == TABS
        page.goto(URL+'/snaps', wait_until='domcontentloaded')
        page.wait_for_function('document.querySelector(".diagnostic-nav a[aria-current=page]")?.textContent==="SNAPs"', polling=100)
        assert page.locator('.diagnostic-nav').count() == 1
        for path,selector in [('/vis','.array-map .map-antenna'),('/snaps','.snap-station-map button.shown')]:
            page.goto(URL+path,wait_until='domcontentloaded')
            page.locator(selector).first.wait_for(timeout=90000)
            for width in [1500,1024,800,700,390,320]:
                page.set_viewport_size(dict(width=width,height=1100))
                page.wait_for_timeout(100)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(path,width)
                assert page.locator(selector).first.bounding_box()['height']>=24
                assert page.locator(selector).first.evaluate('e=>parseFloat(getComputedStyle(e).fontSize)')>=15
                if width==1500:
                    screenshot('layout-'+path.strip('/')+'.png')
        assert not errors, errors
        assert not unexpected, unexpected
        browser.close()
    print(json.dumps(dict(members=sorted(members), navigation=TABS, screenshots=screenshots,
                          hardware_or_job_requests=unexpected)))


if __name__ == '__main__':
    main()
