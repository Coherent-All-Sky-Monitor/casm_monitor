"""Read-only browser acceptance for saved array diagnostics; no submitted jobs."""
import json
import base64
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    output=Path('/home/casm/scratch/casm-observation-preview/screenshots/calibration-power-20261005')
    output.mkdir(parents=True,exist_ok=True)
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1600,'height':1100},device_scale_factor=1)
        page.on('pageerror',lambda e:errors.append(str(e)))
        def shot(name):
            # Capture the displayed viewport directly; avoid Playwright waiting
            # for animations on the monitor's continuously updating shell.
            page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            session=page.context.new_cdp_session(page)
            result=session.send('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
            (output/name).write_bytes(base64.b64decode(result['data']))
            session.detach()
        def guard(route):
            if route.request.method not in ('GET','HEAD'):
                errors.append('Unexpected write: '+route.request.url);route.abort()
            else:route.continue_()
        page.route('**/api/**',guard)
        page.goto('http://127.0.0.1:8061/cal/sun_20260930_200911',wait_until='domcontentloaded')
        page.locator('#cal-autos .cal-antenna-panel').first.wait_for(timeout=60000)
        def ready():
            page.wait_for_function('!document.querySelector(".cal-view-pending")')
        ready()
        assert page.locator('#cal-autos .cal-antenna-panel').count()==16
        assert page.locator('#cal-cross .cal-antenna-panel').count()==15
        def choose(group,button,scope=page):
            scope.get_by_role('group',name=group,exact=True).get_by_role('button',name=button,exact=True).click()
        autos=page.locator('#cal-autos')
        assert autos.get_by_role('button',name='Power (dB)',exact=True).get_attribute('aria-pressed')=='true'
        assert autos.locator('.cal-auto-plot').count()==32
        assert autos.get_by_role('button',name='Raw',exact=True).get_attribute('aria-pressed')=='true'
        choose('Autocorrelation data','Raw',autos)
        assert autos.locator('.cal-auto-plot').count()==16
        choose('Power scale','Power (dB)',autos)
        assert autos.locator('.cal-auto-plots[data-auto-scale="db"]').count()==16
        assert autos.locator('svg').first.get_attribute('aria-label').startswith('Power (dB)')
        # Every displayed positive power remains inside the axes, including peaks.
        assert autos.evaluate('''el => [...el.querySelectorAll('svg path[stroke-width]')].every(p => {
          const box=p.getBBox();return box.y>=15.9 && box.y+box.height<=196.1;
        })''')
        autos.evaluate('(el)=>el.scrollIntoView({block:"start"})');shot('autos-subtracted-db.png')
        choose('Autocorrelation data','Raw',autos)
        choose('Autocorrelation data','Static template',autos)
        assert autos.locator('.cal-auto-plot').count()==48
        choose('Compare','Overlay',autos)
        assert autos.locator('.cal-auto-plot').count()==16
        assert autos.locator('.cal-antenna-panel').first.locator('path[stroke-width]').count()==3
        choose('Compare','Separate plots',autos)
        autos.locator('.cal-plot-button').first.click()
        modal=page.get_by_role('dialog')
        assert modal.locator('.cal-auto-plot').count()==3
        choose('Autocorrelation data','Static template',modal)
        assert modal.locator('.cal-auto-plot').count()==2
        shot('autos-comparison-zoom.png')
        with page.expect_download() as downloaded:
            modal.get_by_role('button',name='Download',exact=True).click()
        assert downloaded.value.failure() is None
        modal.get_by_role('button',name='Close ×',exact=True).click()
        choose('Power scale','Linear',autos)
        choose('Autocorrelation data','Raw',autos)
        choose('Autocorrelation data','Static-subtracted',autos)
        assert autos.locator('.cal-antenna-panel').count()==0
        choose('Autocorrelation data','Raw',autos)
        choose('Autocorrelation data','Static-subtracted',autos)
        print(json.dumps(page.evaluate('({width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,images:[...document.images].filter(i=>!i.complete).length})')),flush=True)
        shot('array-inspector.png')
        choose('Panel layout','SNAP order')
        assert page.locator('#cal-autos .snap-group').count()==4
        page.locator('#cal-cross').evaluate('(el)=>el.scrollIntoView({block:"start"})')
        shot('cross-snap-order.png')
        page.get_by_label('Display reference',exact=True).select_option('24');ready()
        choose('Processing','Geometric model');ready()
        assert page.get_by_role('group',name='Quantity',exact=True).get_by_role('button',name='|V|',exact=True).is_disabled()
        choose('Processing','SVD-corrected');ready()
        choose('Quantity','Real(V)');ready()
        choose('Cross-correlation view','Spectrum')
        assert page.locator('#cal-cross .cal-curve').count()==15
        choose('Quantity','Phase');ready()
        choose('Cross-correlation view','Dynamic spectrum')
        page.locator('#cal-cross .cal-plot-button').first.click()
        page.get_by_role('button',name='Compare processing stages',exact=True).click()
        page.wait_for_function('document.querySelectorAll(".cal-stage-comparison section").length === 5')
        shot('phase-stage-comparison.png')
        page.get_by_role('button',name='Close ×',exact=True).click()
        choose('Amplitude scheme','Matched');ready()
        choose('Weight view','Weight amplitude (relative)')
        page.locator('#cal-weights').evaluate('(el)=>el.scrollIntoView({block:"start"})')
        shot('antenna-amplitudes.png')
        page.get_by_text('How the four amplitude schemes are calculated',exact=True).click()
        assert 'voltage variance' in page.locator('.cal-amplitude-help').inner_text()
        page.locator('.cal-amplitude-help').scroll_into_view_if_needed();shot('weight-formulas.png')
        page.get_by_text('How the four amplitude schemes are calculated',exact=True).click()
        page.locator('#cal-weights .cal-plot-button').first.click()
        page.get_by_role('button',name='Zoom in',exact=True).click()
        with page.expect_download() as downloaded:
            page.get_by_role('button',name='Download',exact=True).click()
        assert downloaded.value.failure() is None
        page.get_by_role('button',name='Close ×',exact=True).click()
        choose('Panel layout','Station grid')
        assert page.locator('#cal-autos .cal-physical-row').count()>1
        for width in (1280,768,390):
            page.set_viewport_size({'width':width,'height':1000})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),f'Overflow at {width}'
        choose('Panel layout','Compact panels')
        page.locator('#cal-weights').scroll_into_view_if_needed()
        shot('mobile-weights.png')
        browser.close()
    assert not errors,errors
    print(json.dumps({'errors':errors,'screenshots':str(output),'writes':'none'}))


if __name__=='__main__':main()
