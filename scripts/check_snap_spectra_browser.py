"""SNAP workspace UI check. Acquisition POST is intercepted: NO hardware reads."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = 'http://127.0.0.1:8061'
OUT = Path('/home/casm/scratch/casm-observation-preview/screenshots')


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
        initial = page.locator('.snap-spectrum-card').count()
        assert initial == 24, initial  # Current wiring, not the visibility/cal display preset.
        assert page.get_by_role('button', name='Compact · SNAP order', exact=True).get_attribute('aria-pressed') == 'true'
        assert page.locator('.snap-spectrum-card').first.inner_text().startswith('N01E1')
        assert page.locator('.snap-spectrum-card').first.locator('svg').get_attribute('aria-label').endswith('4096 channels')
        assert page.locator('.snap-spectrum-card').first.locator('.snap-trace').get_attribute('d').count('L') == 4095
        assert page.locator('.diagnostic-nav').count() == 0
        assert page.get_by_role('link',name='Transmitted-band history',exact=True).count() == 0
        page.locator('.snap-health-board').first.wait_for()
        assert page.locator('.snap-health-box').count()==8
        assert page.locator('.snap-health-box').filter(has_text='PPS alignment').count()==4
        plot=page.locator('.snap-spectrum-card').first.locator('svg')
        assert plot.get_attribute('data-x-min')=='374.9'
        assert plot.get_attribute('data-x-max')=='500.1'
        limits=lambda:page.locator('.snap-spectrum-card svg').evaluate_all("es=>[...new Set(es.map(e=>e.dataset.yMin+','+e.dataset.yMax))]")
        assert len(limits())==1
        shared_limits=limits()
        lower=float(plot.get_attribute('data-y-min'))
        assert float(plot.get_attribute('data-y-max'))-lower<100
        positive_path=page.locator('.snap-spectrum-card').first.locator('.snap-trace').get_attribute('d')
        page.get_by_label('Power reference',exact=True).select_option('1')
        assert float(plot.get_attribute('data-y-min'))==lower-100
        assert page.locator('.snap-spectrum-card').first.locator('.snap-trace').get_attribute('d')==positive_path
        page.get_by_label('Power reference',exact=True).select_option('1e-10')
        page.screenshot(path=str(OUT/'snaps-latest.png'), full_page=True)
        page.locator('.snap-health-section').screenshot(path=str(OUT/'snaps-health.png'))
        page.locator('.snap-panel-group').first.screenshot(path=str(OUT/'snaps-compact.png'))
        page.get_by_label('All 12 ADCs per SNAP').check()
        assert page.locator('.snap-spectrum-card').count() == 48
        assert page.locator('.snap-panel-group').first.locator('.snap-spectrum-card').count() == 12
        page.get_by_role('button', name='Compact · station order', exact=True).click()
        assert page.locator('.snap-spectrum-card').count() == 48
        assert page.locator('.snap-panel-group').first.locator('h3').inner_text() == 'N21'
        page.get_by_label('All 12 ADCs per SNAP').uncheck()
        assert page.locator('.snap-spectrum-card').count() == 24
        page.get_by_role('button', name='Station grid', exact=True).click()
        assert page.locator('.snap-empty').count() > 0
        page.get_by_role('button', name='Compact · SNAP order', exact=True).click()
        page.get_by_role('button', name='History', exact=True).click()
        page.get_by_label('Saved acquisition', exact=True).wait_for()
        opts=page.get_by_label('Saved acquisition', exact=True).locator('option').all()
        assert len(opts)>1
        first=opts[-1].get_attribute('value')
        page.get_by_label('Saved acquisition', exact=True).select_option(first)
        page.locator('.snap-spectrum-card').first.wait_for()
        assert limits()==shared_limits, 'History must preserve the shared power scale'
        page.get_by_label('Overlay first saved snapshot').check()
        page.locator('.snap-reference').first.wait_for()
        page.get_by_role('button',name='Expand SNAP 0 ADC 0',exact=True).click()
        page.get_by_role('dialog').wait_for()
        page.get_by_role('dialog').get_by_role('img', name='Full-band power (dB) versus time (UTC)').wait_for(timeout=60000)
        page.screenshot(path=str(OUT/'snaps-history-expanded.png'))
        page.keyboard.press('Escape')
        page.get_by_role('button',name='Latest spectra',exact=True).click()
        page.locator('.snap-spectrum-card').first.wait_for()
        page.get_by_label('Overlay first saved snapshot').uncheck()
        page.get_by_role('button',name='Hide SNAP 0 ADC 0',exact=True).click()
        assert page.locator('.snap-spectrum-card').count()==23
        page.get_by_role('button',name='Show all stations',exact=True).click()
        assert page.locator('.snap-spectrum-card').count()==24
        for width in [1500,1024,700,390,320]:
            page.set_viewport_size({'width':width,'height':1100})
            page.wait_for_timeout(200)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'), width
            plot=page.locator('.snap-spectrum-card').first
            assert plot.locator('svg').evaluate('e=>getComputedStyle(e.parentElement).backgroundColor')=='rgb(255, 255, 255)'
            assert plot.locator('svg').bounding_box()['width']>=250
            if width==390:
                plot.screenshot(path=str(OUT/'snaps-mobile-panel.png'))
        page.set_viewport_size({'width':1500,'height':1100})
        button=page.get_by_role('button',name='Ping now · get spectra',exact=True)
        if button.is_enabled():
            button.click()
            page.get_by_text('Read queued · job 999999.',exact=False).wait_for()
            assert submitted==[{'ips':['192.168.120.52','192.168.120.51','192.168.120.62','192.168.120.73'],'confirm':True}]
        assert not unexpected, unexpected
        page.goto(URL+'/observation')
        page.get_by_role('link',name='SNAP streaming',exact=False).wait_for()
        page.get_by_role('link',name='SNAP PPS alignment',exact=False).wait_for()
        assert page.locator('.overview-stat').count()==6
        page.screenshot(path=str(OUT/'overview-snaps-status.png'))
        assert not errors,errors
        browser.close()
    print(json.dumps({'wired_panels':initial,'all_adcs':48,'history_snapshots':len(opts),
                      'manual_acquisition':'intercepted, no hardware contact','screenshots':str(OUT)}))


if __name__=='__main__':
    main()
