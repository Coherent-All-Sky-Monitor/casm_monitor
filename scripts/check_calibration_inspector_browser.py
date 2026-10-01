"""Read-only browser acceptance for saved array diagnostics; no submitted jobs."""
import json
import base64
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    output=Path('/home/casm/scratch/casm-observation-preview/screenshots/calibration-array-20261001')
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
            if route.request.method=='POST':
                errors.append('Unexpected POST: '+route.request.url);route.abort()
            else:route.continue_()
        page.route('**/api/**',guard)
        page.goto('http://127.0.0.1:8061/cal/sun_20260930_200911',wait_until='domcontentloaded')
        page.locator('#cal-autos .cal-antenna-panel').first.wait_for(timeout=60000)
        def ready():
            page.wait_for_function('!document.querySelector(".cal-view-pending")')
        ready()
        assert page.locator('#cal-autos .cal-antenna-panel').count()==16
        assert page.locator('#cal-cross .cal-antenna-panel').count()==15
        print(json.dumps(page.evaluate('({width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,images:[...document.images].filter(i=>!i.complete).length})')),flush=True)
        shot('array-inspector.png')
        page.get_by_label('Panel layout',exact=True).select_option('snap')
        assert page.locator('#cal-autos .snap-group').count()==4
        page.locator('#cal-cross').evaluate('(el)=>el.scrollIntoView({block:"start"})')
        shot('cross-snap-order.png')
        page.get_by_label('Display reference',exact=True).select_option('24');ready()
        page.get_by_label('Processing',exact=True).select_option('geometric');ready()
        assert page.get_by_label('Quantity',exact=True).is_disabled()
        page.get_by_label('Processing',exact=True).select_option('svd');ready()
        page.locator('#cal-cross .cal-plot-button').first.click()
        page.get_by_role('button',name='Compare processing stages',exact=True).click()
        page.wait_for_function('document.querySelectorAll(".cal-stage-comparison section").length === 5')
        shot('phase-stage-comparison.png')
        page.get_by_role('button',name='Close ×',exact=True).click()
        page.get_by_label('Amplitude scheme',exact=True).select_option('matched');ready()
        page.get_by_label('Weight view',exact=True).select_option('amplitude')
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
        page.get_by_label('Panel layout',exact=True).select_option('physical')
        assert page.locator('#cal-autos .cal-physical-row').count()>1
        for width in (1280,768,390):
            page.set_viewport_size({'width':width,'height':1000})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),f'Overflow at {width}'
        page.get_by_label('Panel layout',exact=True).select_option('compact')
        page.locator('#cal-weights').scroll_into_view_if_needed()
        shot('mobile-weights.png')
        browser.close()
    assert not errors,errors
    print(json.dumps({'errors':errors,'screenshots':str(output),'writes':'none'}))


if __name__=='__main__':main()
