"""SNAP workspace UI check. Acquisition POST is intercepted: NO hardware reads."""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = 'http://127.0.0.1:8061'
OUT = Path('/home/casm/scratch/casm-observation-preview/screenshots')


def screenshot(target, name):
    if '--no-screenshots' not in sys.argv:
        target.screenshot(path=str(OUT/name),timeout=10000)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1500,'height':1100})
        errors, submitted, unexpected = [], [], []
        page.on('pageerror', lambda e: errors.append(str(e)))

        def acquire(route):
            submitted.append(route.request.post_data_json)
            route.fulfill(status=200, content_type='application/json', body='{"job_id":999999}')

        def guard(route):
            if route.request.method != 'GET':
                unexpected.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        page.route('**/api/**', guard)
        page.route('**/api/snap-workspace/acquire', acquire)
        page.goto(URL+'/snaps')
        page.locator('.snap-spectrum-card').first.wait_for()
        page.wait_for_timeout(400)
        membership=page.request.get(URL+'/api/snap-workspace/beamforming').json()
        assert membership['status']=='complete'
        members={f"{i['ip']}/{i['adc']}" for i in membership['inputs'] if i['beamforming']}
        initial = page.locator('.snap-spectrum-card').count()
        assert initial == len(members), initial
        assert set(page.locator('.snap-spectrum-card').evaluate_all('es=>es.map(e=>e.dataset.inputKey)'))==members
        assert page.locator('.snap-station-map button.in-beamforming').count()==len(members)
        assert page.get_by_role('button', name='Compact · SNAP order', exact=True).get_attribute('aria-pressed') == 'true'
        assert page.locator('.snap-spectrum-card').first.inner_text().startswith('N21E1')
        assert page.locator('.snap-spectrum-card').first.locator('svg').get_attribute('aria-label').endswith('4096 channels')
        assert page.locator('.snap-spectrum-card').first.locator('.snap-trace').get_attribute('d').count('L') == 4095
        assert page.locator('.diagnostic-nav').count() == 1
        assert page.locator('.diagnostic-nav a[aria-current=page]').inner_text() == 'SNAPs'
        assert page.locator('.header nav').count() == 0
        assert page.get_by_role('link',name='Transmitted-band history',exact=True).count() == 0
        page.get_by_text('Board status details',exact=True).click()
        page.locator('.snap-health-board').first.wait_for()
        assert page.locator('.snap-health-box').count()==8
        assert page.locator('.snap-health-box').filter(has_text='PPS timing').count()==4
        assert set(page.locator('.snap-board-ip').all_text_contents())=={i['ip'] for i in membership['inputs']}
        assert all('192.168.120.' in text for text in page.locator('.snap-board-errors summary').all_text_contents())
        page.get_by_text('Board status details',exact=True).click()
        plot=page.locator('.snap-spectrum-card').first.locator('svg')
        assert plot.get_attribute('data-x-min')=='374.9'
        assert plot.get_attribute('data-x-max')=='500.1'
        limits=lambda:page.locator('.snap-spectrum-card svg').evaluate_all("es=>[...new Set(es.map(e=>e.dataset.yMin+','+e.dataset.yMax))]")
        def no_clipping():
            assert len(limits())==1
            assert page.locator('.snap-spectrum-card svg path[fill="#b9540b"]').count()==0
            assert page.get_by_text('points outside displayed limits',exact=False).count()==0
        no_clipping()
        assert len(limits())==1
        shared_limits=limits()
        lower=float(plot.get_attribute('data-y-min'))
        assert float(plot.get_attribute('data-y-max'))-lower<100
        assert page.get_by_label('Power reference',exact=True).count()==0
        assert page.get_by_label('Y-axis scale',exact=True).count()==0
        screenshot(page,'snaps-latest.png')
        screenshot(page.locator('.snap-health-section'),'snaps-health.png')
        screenshot(page.locator('.snap-panel-group').first,'snaps-compact.png')
        page.get_by_role('switch',name='All 12 ADCs per SNAP',exact=False).click()
        assert page.locator('.snap-spectrum-card').count() == 48
        no_clipping()
        assert set(page.locator('.snap-spectrum-card.in-beamforming').evaluate_all('es=>es.map(e=>e.dataset.inputKey)'))==members
        assert page.locator('.snap-panel-group').first.locator('.snap-spectrum-card').count() == 12
        page.get_by_role('button', name='Compact · station order', exact=True).click()
        assert page.locator('.snap-spectrum-card').count() == 48
        assert page.locator('.snap-panel-group').first.locator('h3').inner_text() == 'N21'
        page.get_by_role('switch',name='All 12 ADCs per SNAP',exact=False).click()
        assert page.locator('.snap-spectrum-card').count() == initial
        page.get_by_role('button',name='All wired · 24',exact=True).click()
        assert page.locator('.snap-spectrum-card').count() == 24
        no_clipping()
        shared_limits=limits()  # Changing selection scope intentionally refits its shared limits.
        page.get_by_role('button', name='Station grid', exact=True).click()
        assert page.locator('.snap-empty').count() > 0
        page.get_by_role('button', name='Compact · SNAP order', exact=True).click()
        assert page.get_by_role('button',name='History',exact=True).count()==0
        assert limits()==shared_limits
        page.get_by_role('button',name='Expand SNAP 0 ADC 0',exact=True).click()
        page.get_by_role('dialog').wait_for()
        page.get_by_role('dialog').get_by_role('img', name='Full-band power (dB) versus time (UTC)').wait_for(timeout=60000)
        screenshot(page,'snaps-history-expanded.png')
        page.keyboard.press('Escape')
        page.get_by_role('button',name='Hide SNAP 0 ADC 0',exact=True).click()
        assert page.locator('.snap-spectrum-card').count()==23
        page.get_by_role('button',name='Reset selection',exact=True).click()
        assert page.locator('.snap-spectrum-card').count()==24
        for width in [1500,1024,700,390,320]:
            page.set_viewport_size({'width':width,'height':1100})
            page.wait_for_timeout(200)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'), width
            plot=page.locator('.snap-spectrum-card').first
            assert plot.locator('svg').evaluate('e=>getComputedStyle(e.parentElement).backgroundColor')=='rgb(255, 255, 255)'
            assert plot.locator('svg').bounding_box()['width']>=250
            if width==390:
                screenshot(plot,'snaps-mobile-panel.png')
        page.set_viewport_size({'width':1500,'height':1100})
        button=page.get_by_role('button',name='Ping now · get spectra',exact=True)
        if button.is_enabled():
            button.click()
            page.get_by_text('Read queued · job 999999.',exact=False).wait_for()
            assert submitted==[{'ips':['192.168.120.52','192.168.120.51','192.168.120.62','192.168.120.73'],'confirm':True}]
        assert not unexpected, unexpected
        page.goto(URL+'/observation')
        page.get_by_role('link',name='SNAP streaming',exact=False).wait_for()
        page.get_by_role('link',name='SNAP PPS timing',exact=False).wait_for()
        assert page.locator('.overview-stat').count()==6
        screenshot(page,'overview-snaps-status.png')
        # Display-only fixture: a later saved frame with extreme finite values
        # must expand the shared range. Never write this to the monitor store.
        frame=page.request.get(URL+'/api/snap-workspace/spectra').json()
        target=next(b for b in frame['boards'] if b['feng_id']==2)
        adc=next(i['adc'] for i in target['inputs'] if i['station']=='N16E1')
        target['spectra_db'][adc][0]=-400
        target['spectra_db'][adc][1]=150
        page.route('**/api/snap-workspace/spectra?*',lambda r:r.fulfill(json=frame))
        page.goto(URL+'/snaps')
        page.locator('.snap-spectrum-card svg').first.wait_for()
        plot=page.locator('.snap-spectrum-card svg').first
        assert float(plot.get_attribute('data-y-min')) < -300
        assert float(plot.get_attribute('data-y-max')) > 250
        no_clipping()
        assert not errors,errors
        browser.close()
    print(json.dumps({'beamforming_panels':initial,'wired_panels':24,'all_adcs':48,'trend_days':30,
                      'manual_acquisition':'intercepted, no hardware contact','screenshots':str(OUT)}))


if __name__=='__main__':
    main()
