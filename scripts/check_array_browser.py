"""Exercise the live visibility array overview without changing telescope state."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

URL='http://127.0.0.1:8061'
OUT=Path('/home/casm/scratch/casm-observation-preview/screenshots')


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1600,'height':1100})
        page.on('pageerror',lambda e:errors.append(str(e)))
        requests=[]
        page.on('request',lambda r:requests.append(r.url) if '/api/science/array?' in r.url else None)
        page.goto(URL+'/vis',wait_until='domcontentloaded')
        page.locator('.antenna-plot').first.wait_for(timeout=90000)
        page.wait_for_function('!document.querySelector(".array-results").getAttribute("aria-busy") || document.querySelector(".array-results").getAttribute("aria-busy")==="false"')
        assert page.locator('.antenna-plot').count()==17
        assert page.locator('.map-antenna[aria-pressed=true]').count()==17
        assert page.get_by_role('link',name='Visibilities',exact=True).count()==1
        assert page.get_by_label('Rolling window').input_value()=='24'
        assert all('hours=24' in url for url in requests)
        assert page.locator('.antenna-plot.expanded').count()==0
        assert page.locator('.station-group').count()==7
        assert page.locator('.station-slot.empty').count()==18
        assert page.locator('.station-slot.hidden').count()==7
        assert page.locator('.station-slot.selected').count()==17
        assert page.locator('.antenna-plot').first.evaluate('(el)=>getComputedStyle(el).backgroundColor')=='rgb(255, 255, 255)'
        assert page.locator('.antenna-plot').first.bounding_box()['width']>400
        compact_width=page.locator('.antenna-plot').first.bounding_box()['width']
        page.get_by_role('button',name='Station grid',exact=True).click()
        assert page.locator('.antenna-plot').count()==17
        assert compact_width>1.8*page.locator('.antenna-plot').first.bounding_box()['width']
        page.get_by_role('button',name='Compact panels',exact=True).click()
        page.screenshot(path=str(OUT/'array-autos.png'),full_page=True)
        before=len(requests)
        for q in ['Real(V)','Imag(V)','Phase','|V|']:
            page.get_by_role('button',name=q,exact=True).click()
            assert page.locator('.antenna-plot').count()==17
        assert len(requests)==before,'Quantity switch reread the data'
        page.get_by_role('button',name='Dynamic spectrum',exact=True).click()
        assert page.locator('.array-dynamic img').count()==17
        assert page.locator('.array-dynamic img').first.bounding_box()['height']>=170
        page.screenshot(path=str(OUT/'array-dynamic.png'),full_page=True)
        page.get_by_role('button',name='N21E1, antenna 9',exact=True).click()
        assert page.locator('.antenna-plot').count()==16
        assert page.locator('.station-slot.hidden').count()==8
        assert len(requests)==before,'Antenna toggle reread the data'
        page.get_by_role('button',name='Default 17',exact=True).click()
        assert page.locator('.antenna-plot').count()==17
        page.get_by_role('button',name='Clear',exact=True).click()
        assert page.locator('.antenna-plot').count()==0
        page.get_by_role('button',name='Default 17',exact=True).click()
        page.get_by_role('button',name='Cross-correlations',exact=True).click()
        page.wait_for_function('document.querySelector(".array-results").getAttribute("aria-busy")==="false"',timeout=90000)
        page.get_by_role('button',name='Phase',exact=True).click()
        page.screenshot(path=str(OUT/'array-cross-phase.png'),full_page=True)
        page.get_by_role('button',name='Expand N11E2',exact=True).click()
        assert page.get_by_role('dialog').count()==1
        assert page.get_by_role('dialog').evaluate('(el)=>getComputedStyle(el).backgroundColor')=='rgb(255, 255, 255)'
        page.keyboard.press('Escape')
        assert page.get_by_role('dialog').count()==0
        page.get_by_role('button',name='All pairs',exact=True).click()
        assert page.locator('.array-matrix td button').count()==17*17
        page.screenshot(path=str(OUT/'array-matrix.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth-window.innerWidth')<=1
        page.screenshot(path=str(OUT/'array-mobile.png'),full_page=True)
        page.get_by_role('button',name='Spectrum',exact=True).click()
        assert page.locator('.antenna-plot').first.bounding_box()['width']>300
        assert page.evaluate('document.documentElement.scrollWidth-window.innerWidth')<=1
        page.screenshot(path=str(OUT/'array-mobile-panels.png'),full_page=True)
        # Opening an auto phase detail must keep the exact newest integration.
        page.set_viewport_size({'width':1600,'height':1100})
        with page.expect_response(lambda r:'/api/science/array?' in r.url) as initial:
            page.goto(URL+'/vis',wait_until='domcontentloaded')
        snapshot=initial.value.json()
        page.locator('.antenna-plot').first.wait_for(timeout=90000)
        page.wait_for_function('document.querySelector(".array-results").getAttribute("aria-busy")==="false"')
        page.get_by_role('button',name='Phase',exact=True).click()
        page.get_by_role('button',name='Expand N21E1',exact=True).click()
        with page.expect_response(lambda r:r.url.endswith('/api/science/render'),timeout=45000) as rendered:
            page.get_by_role('button',name='Open stored-pair plot',exact=True).click()
        product=rendered.value.json()
        assert rendered.value.status==200,product
        assert product['selection']['kind']=='phase_spectrum'
        assert product['selection']['spectrum_statistic']=='latest'
        assert product['provenance']['actual_t1']==snapshot['t1']
        assert product['provenance']['samples']==snapshot['samples']
        assert snapshot['selection']['hours']==24
        assert 'Rolling 24 h · updates every minute.' in page.locator('main').inner_text()
        page.get_by_label('Geometry preset').select_option('same_row')
        for label in page.locator('.baseline small').all_text_contents():
            a,b=label.split(' × ')
            assert a.split('E')[0]==b.split('E')[0],label
        # Wait for the real minute timer, without pressing Refresh/Render.
        with page.expect_response(lambda r:r.url.endswith('/api/science/render'),timeout=75000) as refreshed:
            pass
        assert refreshed.value.status==200
        rolling=refreshed.value.json()['selection']
        assert rolling['t1']>product['selection']['t1']
        assert abs(rolling['t1']-rolling['t0']-86400)<1
        assert not errors,errors
        browser.close()
    print(json.dumps({'result':'passed','screenshots':str(OUT),'console_errors':errors}))


if __name__=='__main__':main()
