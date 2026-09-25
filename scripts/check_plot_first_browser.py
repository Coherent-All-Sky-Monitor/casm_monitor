"""Read-only plot-first layout check; never submits hardware or job requests."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = 'http://127.0.0.1:8061'
OUT = Path('/home/casm/scratch/casm-observation-preview/screenshots')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    positions, errors, unexpected = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-dev-shm-usage','--disable-gpu'])
        page = browser.new_page(viewport=dict(width=1500, height=900))
        page.on('pageerror', lambda e: errors.append(str(e)))

        def guard(route):
            if route.request.method != 'GET' and not route.request.url.split('?')[0].endswith('/api/science/render'):
                unexpected.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        page.route('**/api/**', guard)
        members = page.request.get(URL+'/api/snap-workspace/beamforming').json()
        count = len([i for i in members['inputs'] if i['beamforming']])
        for path, card, key in [('/vis', '.antenna-plot', '.array-layout-panel'),
                                ('/snaps', '.snap-spectrum-card', '.snap-layout-key')]:
            page.goto(URL+path, wait_until='domcontentloaded')
            page.locator(card+' svg').first.wait_for(timeout=90000)
            assert page.locator(card).count() == count
            assert page.locator('.diagnostic-nav svg').count() == 0
            for width, height in [(1500,900),(1366,768),(1024,900),(800,900),(700,900),(390,850),(320,850)]:
                page.set_viewport_size(dict(width=width,height=height))
                page.evaluate('window.scrollTo(0,0)')
                page.wait_for_timeout(100)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), (path,width)
                if width>=1366:
                    box = page.locator(card+' svg').first.bounding_box()
                    map_box = page.locator(key).bounding_box()
                    positions.append(dict(path=path,width=width,plot_top=round(box['y']),map_height=round(map_box['height'])))
                    assert map_box['width'] > map_box['height']*1.5, map_box
                    assert box['y']+80 < height, (path,width,box)
                    if width==1500:
                        try:
                            page.screenshot(path=str(OUT/f'plot-first-{path[1:]}.png'),timeout=10000)
                        except Exception as exc:
                            print('Screenshot unavailable:',str(exc).splitlines()[0])
            page.set_viewport_size(dict(width=1500,height=900))
            if path=='/snaps':
                for label in ['Power reference','Y-axis scale','History days','Snapshot timeline']:
                    assert page.get_by_label(label,exact=True).count()==0
                for label in ['History','Refit scales','Latest spectra']:
                    assert page.get_by_role('button',name=label,exact=True).count()==0
                switch=page.get_by_role('switch',name='All 12 ADCs per SNAP',exact=False)
                assert switch.bounding_box()['height']>=64
                switch.click()
                assert switch.get_attribute('aria-checked')=='true'
                assert page.locator(card).count()==48
                assert page.locator(card+'.in-beamforming').count()==count
                switch.click()
                assert page.locator(card).count()==count
                assert page.locator('.snap-health-badge').count()==8
                assert page.locator('.snap-health-details').get_attribute('open') is None
                page.get_by_text('Board status details',exact=True).click()
                assert page.locator('.snap-health-box').first.is_visible()
                page.get_by_text('Board status details',exact=True).click()
                page.locator('.snap-plot-trigger').first.click()
                page.get_by_role('dialog').get_by_role('img',name='Full-band power (dB) versus time (UTC)').wait_for(timeout=60000)
                page.keyboard.press('Escape')
        page.goto(URL+'/observation',wait_until='domcontentloaded')
        page.locator('.overview-clock time').first.wait_for()
        assert page.locator('.overview-clock').count()==3
        assert 'Local sidereal time' in page.locator('.overview-clock').nth(2).inner_text()
        for width in [1500,1024,700,390,320]:
            page.set_viewport_size(dict(width=width,height=1000))
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
            if width==1500:
                tops=page.locator('.overview-clock').evaluate_all('es=>es.map(e=>e.getBoundingClientRect().top)')
                assert max(tops)-min(tops)<1
        assert not errors,errors
        assert not unexpected,unexpected
        browser.close()
    print(json.dumps(dict(positions=positions,beamforming=count,all_adcs=48,clocks=3,unexpected_requests=unexpected)))


if __name__=='__main__':
    main()
