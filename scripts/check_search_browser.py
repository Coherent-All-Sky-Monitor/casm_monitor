"""Read-only acceptance of Search (T1) labels, white PNGs and pinned display zoom."""
import io
import json
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

URL='http://127.0.0.1:8061'
OUT=Path('/home/casm/scratch/casm-observation-preview/screenshots')


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport=dict(width=1500,height=1100))
        errors=[]
        reads=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:reads.append(r.url) if '/api/t1?' in r.url else None)
        with page.expect_response(lambda r:'/api/t1?' in r.url,timeout=60000) as response:
            page.goto(URL+'/search')
        payload=response.value.json()
        page.get_by_role('heading',name='Search (T1)',exact=True).wait_for()
        assert page.get_by_role('link',name='Search (T1)',exact=True).count()==1
        assert page.get_by_role('link',name='T1 / RFI',exact=True).count()==0
        trigger=page.get_by_role('button',name='Enlarge Search (T1) plots',exact=True)
        trigger.wait_for()
        assert trigger.evaluate('(e)=>getComputedStyle(e).padding')=='0px'
        assert trigger.evaluate('(e)=>getComputedStyle(e).backgroundColor')=='rgb(255, 255, 255)'
        page.wait_for_function('document.querySelector(".plot-surface img").naturalWidth>0')
        assert page.locator('.stream-cell').count()==8
        assert 'Updates every 5 minutes' in page.locator('main').inner_text()
        assert payload['refresh_s']==300 and payload['time_bin_seconds']==180
        assert payload['dm_edges'][1]==10 and payload['dm_edges'][-1]==3000
        assert sum(map(sum,payload['activity']['cands']))==payload['n_candidates']
        original=page.locator('.plot-surface img').get_attribute('src')
        png=page.request.get(URL+original).body()
        raster=Image.open(io.BytesIO(png)).convert('RGB')
        assert raster.getpixel((0,0))==(255,255,255)
        assert raster.width==1950 and raster.height==1875
        page.screenshot(path=str(OUT/'search-white-desktop.png'),full_page=True)
        requests=len(reads)
        trigger.focus()
        page.keyboard.press('Enter')
        dialog=page.get_by_role('dialog')
        assert dialog.locator('img').get_attribute('src')==original
        page.get_by_role('button',name='Zoom in',exact=True).click()
        assert page.get_by_label('Display zoom').inner_text()=='150%'
        assert dialog.locator('.figure-zoom-viewport').evaluate('(e)=>e.scrollWidth>e.clientWidth')
        page.get_by_role('button',name='Fit',exact=True).click()
        assert page.get_by_label('Display zoom').inner_text()=='100%'
        page.keyboard.press('Escape')
        assert trigger.evaluate('(e)=>e===document.activeElement')
        assert len(reads)==requests  # Zoom never re-bins or re-queries the data.
        assert page.get_by_role('link',name='Download PNG',exact=True).get_attribute('href')==original
        with page.expect_response(lambda r:'/api/t1?' in r.url and 'time_tz=UTC' in r.url,timeout=60000) as changed:
            page.get_by_role('combobox',name='Time zone').select_option('UTC')
        assert changed.value.json()['time_tz']=='UTC'
        page.wait_for_function('(old)=>document.querySelector(".plot-surface img").getAttribute("src")!==old',arg=original)
        assert '· UTC ·' in page.locator('.plot-surface figcaption').inner_text()
        earlier=(datetime.fromisoformat(payload['t0'][:10])-timedelta(days=1)).date().isoformat()
        with page.expect_response(lambda r:'/api/t1?' in r.url,timeout=60000):
            page.get_by_label('Observation day',exact=True).fill(earlier)
        assert 'Historical interval' in page.locator('main').inner_text()
        page.set_viewport_size(dict(width=390,height=844))
        assert page.locator('.stream-strip').evaluate('(e)=>getComputedStyle(e).gridTemplateColumns.split(" ").length')==2
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1')
        page.screenshot(path=str(OUT/'search-white-mobile.png'),full_page=True)
        trigger.click()
        page.get_by_role('button',name='Zoom in',exact=True).click()
        page.screenshot(path=str(OUT/'search-white-mobile-zoom.png'))
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1')
        page.keyboard.press('Escape')
        assert not errors,errors
        browser.close()
    print(json.dumps(dict(result='passed',stream_cards=8,white_png=[1950,1875],
        checks=['tab name','data counts','keyboard zoom','pinned PNG','no zoom reread','UTC','historical','mobile']),indent=2))


if __name__=='__main__':
    main()
