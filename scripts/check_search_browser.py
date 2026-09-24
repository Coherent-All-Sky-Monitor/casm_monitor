"""Read-only acceptance of Search (T1) labels, white PNGs and pinned display zoom."""
import io
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

URL='http://127.0.0.1:8061'
OUT=Path('/home/casm/scratch/casm-observation-preview/screenshots')


def luminance(colour):
    rgb=np.array([float(v) for v in re.findall(r'[\d.]+',colour)[:3]])/255
    linear=np.where(rgb<=.04045,rgb/12.92,((rgb+.055)/1.055)**2.4)
    return linear @ [0.2126,0.7152,0.0722]


def check_stream_cards(page):
    for card in page.locator('.stream-cell').all():
        details=card.evaluate('''(card)=>{
          const background=getComputedStyle(card).backgroundColor;
          const labels=[...card.querySelectorAll('h3,.stream-node,.stream-label,strong,.stream-status,dt,dd,.stream-expected')];
          const box=card.getBoundingClientRect();
          return {background,labels:labels.map(el=>{
            const css=getComputedStyle(el),r=el.getBoundingClientRect();
            return {text:el.textContent,colour:css.color,
              background:el.matches('.stream-node,.stream-status')?css.backgroundColor:background,
              inside:r.left>=box.left&&r.right<=box.right&&r.top>=box.top&&r.bottom<=box.bottom};
          })};
        }''')
        assert details['background']=='rgb(247, 248, 251)'
        for label in details['labels']:
            light,dark=sorted([luminance(label['colour']),luminance(label['background'])],reverse=True)
            assert (light+.05)/(dark+.05)>=4.5,label
            assert label['inside'],label
        for row in card.locator('.stream-heading,.stream-reading,.stream-metrics > div').all():
            assert row.evaluate('''row=>{
              const [a,b]=[...row.children].map(e=>e.getBoundingClientRect());
              return a.right<=b.left||b.right<=a.left||a.bottom<=b.top||b.bottom<=a.top;
            }''')


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
        check_stream_cards(page)
        for stream in payload['streams']:
            card=page.get_by_role('article',name=f"Stream {stream['stream']}",exact=True)
            assert card.locator('.stream-node').inner_text()==stream['node']
            assert card.locator('.stream-status').inner_text().lower()==stream['status']
        page.locator('.stream-strip').screenshot(path=str(OUT/'search-stream-cards.png'))
        assert 'Updates every 5 minutes' in page.locator('main').inner_text()
        assert payload['refresh_s']==300 and payload['time_bin_seconds']==180
        assert payload['dm_edges'][1]==10 and payload['dm_edges'][-1]==3000
        assert sum(map(sum,payload['activity']['cands']))==payload['n_candidates']
        original=page.locator('.plot-surface img').get_attribute('src')
        png=page.request.get(URL+original).body()
        raster=Image.open(io.BytesIO(png)).convert('RGB')
        assert raster.getpixel((0,0))==(255,255,255)
        assert raster.width==1950 and raster.height==1875
        pixels=np.asarray(raster)
        assert np.any(np.all(pixels==(69,106,154),axis=-1))  # Muted-blue histogram bars.
        assert np.any(np.all(pixels==(57,19,95),axis=-1))  # Deep-purple maximum count.
        page.locator('.plot-surface img').screenshot(path=str(OUT/'search-purple-figure.png'))
        page.screenshot(path=str(OUT/'search-purple-desktop.png'),full_page=True)
        page.screenshot(path=str(OUT/'search-stream-cards-desktop.png'),full_page=True)
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
        check_stream_cards(page)
        page.screenshot(path=str(OUT/'search-white-mobile.png'),full_page=True)
        trigger.click()
        page.get_by_role('button',name='Zoom in',exact=True).click()
        page.screenshot(path=str(OUT/'search-white-mobile-zoom.png'))
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1')
        page.keyboard.press('Escape')
        # Display fixtures exercise all four statuses, including missing evidence.
        fixture=deepcopy(payload)
        for index,stream in enumerate(fixture['streams']):
            state=index%4
            stream.update(status=['ok','late','silent','unknown'][state],
                last_gulp_age_s=[47,120,7200,None][state],
                empty_fraction_last_hour=[.94,0,.25,None][state])
        page.route('**/api/t1?*',lambda route:route.fulfill(json=fixture))
        page.reload()
        page.locator('.stream-cell.unknown').first.wait_for()
        assert page.locator('.stream-cell.unknown strong').first.inner_text()=='no gulps'
        assert page.locator('.stream-cell.unknown dd').last.inner_text()=='n/a'
        assert page.locator('.stream-cell.late dd').last.inner_text()=='0%'
        for width,columns in [(1500,8),(1200,4),(950,4),(700,2),(500,2),(390,2),(320,2)]:
            page.set_viewport_size(dict(width=width,height=1000))
            assert page.locator('.stream-strip').evaluate('(e)=>getComputedStyle(e).gridTemplateColumns.split(" ").length')==columns
            assert page.locator('.stream-strip').evaluate('(e)=>e.getBoundingClientRect().right<=window.innerWidth')
            # The existing global header (outside these cards) overflows at 320 px.
            if width>=390:
                assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1'),width
            check_stream_cards(page)
        page.set_viewport_size(dict(width=1500,height=1100))
        page.locator('.stream-strip').screenshot(path=str(OUT/'search-stream-states-fixture.png'))
        assert not errors,errors
        browser.close()
    print(json.dumps(dict(result='passed',stream_cards=8,white_png=[1950,1875],
        checks=['tab name','data counts','keyboard zoom','pinned PNG','no zoom reread','UTC','historical','mobile',
                'four stream states','card text contrast','card layout at seven widths']),indent=2))


if __name__=='__main__':
    main()
