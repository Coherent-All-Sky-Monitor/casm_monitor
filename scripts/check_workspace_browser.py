"""Exercise the isolated workspace with bounded real data, no builds or requests."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

URL='http://127.0.0.1:8061'
OUTPUT=Path('/home/casm/scratch/casm-observation-preview/screenshots')

def main():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1050})
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(URL+'/observation',wait_until='networkidle')
        page.get_by_role('heading',name='Science and search recovery').wait_for()
        assert page.evaluate('getComputedStyle(document.body).backgroundColor')=='rgb(0, 0, 0)'
        assert page.locator('.js-plotly-plot').count()==0
        assert page.get_by_role('link',name='Imaging',exact=True).count()==0
        page.wait_for_function('document.querySelectorAll(".monitoring-overview .plot-surface img").length >= 3 && Array.from(document.querySelectorAll(".monitoring-overview .plot-surface img")).every(i=>i.complete&&i.naturalWidth>0)',timeout=60000)
        overview_metadata=page.locator('.monitoring-overview a',has_text='Provenance JSON').first.get_attribute('href')
        assert page.request.get(URL+overview_metadata).json()['selection']['reference']=='raw'
        assert page.get_by_role('combobox',name='Time zone').input_value()=='America/Los_Angeles'
        page.screenshot(path=str(OUTPUT/'workspace-observation.png'),full_page=False)
        page.goto(URL+'/vis',wait_until='networkidle')
        page.locator('.antenna-plot').first.wait_for(timeout=90000)
        assert page.locator('.antenna-plot').count()==17
        page.get_by_role('button',name='Detailed baseline inspector →').click()
        page.locator('.baseline.selected').first.wait_for()
        # Default rolling phase loads without pressing Render selection.
        page.locator('.product-plots img').first.wait_for(timeout=45000)
        page.wait_for_function('Array.from(document.querySelectorAll(".product-plots img")).every(i=>i.complete&&i.naturalWidth>0)')
        page.screenshot(path=str(OUTPUT/'workspace-phase.png'),full_page=True)
        assert page.get_by_role('link',name='Download data').count()>0
        page.get_by_role('button',name='Mark for investigation').first.click()
        page.get_by_placeholder('Describe the feature and the question to investigate.').fill('Browser validation only; not submitted.')
        assert page.get_by_role('button',name='Save plot and note').count()==1
        page.goto(URL+'/search',wait_until='networkidle')
        # Default Hella plots load without pressing Inspect interval.
        page.locator('.plot-surface img').wait_for(timeout=30000)
        page.wait_for_function('document.querySelector(".plot-surface img").naturalWidth>0')
        page.screenshot(path=str(OUTPUT/'workspace-t1.png'),full_page=True)
        with page.expect_response(lambda r:'/api/t1?' in r.url and 'time_tz=UTC' in r.url) as changed:
            page.get_by_role('combobox',name='Time zone').select_option('UTC')
        payload=changed.value.json()
        assert payload['display_dm_max']==1000 and payload['time_tz']=='UTC'
        with page.expect_response(lambda r:'/api/t1?' in r.url and '2026-09-12T00' in r.url) as historical:
            page.get_by_label('Observation day').fill('2026-09-12')
        assert historical.value.json()['t0'].startswith('2026-09-12T00:00')
        assert 'Historical interval' in page.locator('main').inner_text()
        page.goto(URL+'/review',wait_until='networkidle')
        page.get_by_role('heading',name='Investigation queue',exact=True).wait_for()
        assert page.get_by_role('button',name='Request investigation').count()>0
        page.screenshot(path=str(OUTPUT/'workspace-review.png'),full_page=False)
        page.goto(URL+'/cal',wait_until='networkidle')
        page.get_by_role('button',name='Stage reviewed recipe').wait_for(timeout=20000)
        assert page.get_by_role('button',name='Stage reviewed recipe').is_disabled()
        page.screenshot(path=str(OUTPUT/'workspace-calibration.png'),full_page=False)
        page.goto(URL+'/cal/compare',wait_until='networkidle')
        page.get_by_role('heading',name='Does the baseline phase still match?').wait_for()
        page.get_by_role('heading',name='Two matched clock windows').wait_for()
        assert page.get_by_role('button',name='Read both windows and compare phase').is_enabled()
        assert page.locator('.product-plots img').count()==0
        page.screenshot(path=str(OUTPUT/'workspace-calibration-comparison.png'),full_page=True)
        page.goto(URL+'/sources',wait_until='networkidle')
        page.locator('.history-entry').first.wait_for(timeout=20000)
        assert page.locator('.history-entry').count()>=20
        page.screenshot(path=str(OUTPUT/'workspace-source-history.png'),full_page=False)
        page.goto(URL+'/antennas',wait_until='networkidle')
        page.get_by_role('heading',name='SNAP spectra and history').wait_for()
        assert page.get_by_role('button',name='Get latest spectra').count()==1
        page.locator('.product-plots img').first.wait_for(timeout=30000)
        page.wait_for_function('document.querySelector(".product-plots img").naturalWidth>0')
        assert page.locator('.js-plotly-plot').count()==0
        page.screenshot(path=str(OUTPUT/'workspace-snaps.png'),full_page=True)
        page.get_by_role('button',name='Selected input history').click()
        assert page.get_by_role('button',name='Render history').is_enabled()
        page.screenshot(path=str(OUTPUT/'workspace-antennas.png'),full_page=False)
        page.goto(URL+'/readiness',wait_until='networkidle')
        page.get_by_role('heading',name='What needs attention?').wait_for()
        assert page.locator('.disk-row').count()==3
        assert not page.locator('.readiness-details').evaluate('(e)=>e.open')
        page.screenshot(path=str(OUTPUT/'workspace-readiness.png'),full_page=False)
        page.set_viewport_size({'width':390,'height':844})
        page.goto(URL+'/vis',wait_until='networkidle')
        overflow=page.evaluate('document.documentElement.scrollWidth-window.innerWidth')
        assert overflow<=1,overflow
        page.screenshot(path=str(OUTPUT/'workspace-mobile.png'),full_page=True)
        assert page.request.post(URL+'/api/jobs',data='{}',headers={'Content-Type':'application/json','Origin':URL,'X-CASM-Workspace':'1'}).status==403
        page.route('**/api/observation',lambda route:route.fulfill(status=503,json={'detail':'Evidence temporarily unavailable'}))
        page.goto(URL+'/observation',wait_until='networkidle')
        assert 'Evidence temporarily unavailable' in page.locator('main').inner_text()
        assert not errors,errors
        browser.close()
    print(json.dumps({'result':'passed','mobile_overflow_px':overflow,'screenshots':str(OUTPUT)}))

if __name__=='__main__':
    main()
