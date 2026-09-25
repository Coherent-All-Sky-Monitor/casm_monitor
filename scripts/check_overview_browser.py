"""Read-only Overview acceptance: real data, drill-down and isolated failure fixtures."""
import json
from hashlib import sha256
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.parse import parse_qs,urlparse

from playwright.sync_api import sync_playwright
from check_search_browser import luminance

URL='http://127.0.0.1:8061'
OUT=Path('/home/casm/scratch/casm-observation-preview/screenshots')


def check_header_and_controls(page):
    assert page.locator('.overview-heading').evaluate('''header=>{
      const photo=header.querySelector('.overview-photo').getBoundingClientRect();
      const copy=header.querySelector('.overview-heading-copy').getBoundingClientRect();
      const bounds=header.getBoundingClientRect();
      const image=header.querySelector('img'), frame=image.getBoundingClientRect();
      return copy.right<photo.left && Math.abs(photo.right-bounds.right)<1
        && Math.abs(frame.width/frame.height-4/3)<.01
        && getComputedStyle(image).objectFit==='contain';
    }''')
    assert page.locator('.overview-history-actions').evaluate('''group=>{
      const zone=group.querySelector('select').getBoundingClientRect();
      const live=group.querySelector('button').getBoundingClientRect();
      return Math.abs(zone.top-live.top)<1 && Math.abs(zone.bottom-live.bottom)<1
        && zone.right<live.left && Math.abs(zone.height-38)<1;
    }''')


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport=dict(width=1500,height=1100))
        errors=[];writes=[];records={}
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:writes.append(r.url) if r.method=='POST' else None)
        def capture(response):
            path=urlparse(response.url).path
            if path in ('/api/observation','/api/t1/status','/api/observation/injections','/api/science/catalog') and response.ok:
                records[path]=response.json()
        page.on('response',capture)
        page.goto(URL+'/observation')
        page.get_by_role('heading',name='Overview',exact=True).wait_for()
        telescope_name='Coherent All Sky Monitor (CASM)'
        assert page.title()==telescope_name
        assert page.get_by_role('heading',name=telescope_name,exact=True).count()==1
        assert page.locator('.header__location').inner_text()=='Owens Valley Radio Observatory · Bishop, California'
        assert 'CASM · OVRO' not in page.locator('body').inner_text()
        assert page.get_by_role('link',name='Overview',exact=True).count()==1
        assert not page.get_by_role('link',name='Injection recovery',exact=True).count()
        photo=page.get_by_role('button',name='Enlarge CASM telescope photo',exact=True)
        page.wait_for_function('document.querySelector(".overview-photo img")?.naturalWidth===4032')
        photo_url=photo.locator('img').get_attribute('src')
        assert photo_url.startswith('/assets/casm-telescope-')
        response=page.request.get(URL+photo_url)
        assert response.ok and response.headers['content-type'].startswith('image/jpeg')
        original=Path(__file__).resolve().parents[1]/'frontend/src/assets/casm-telescope.jpg'
        assert sha256(response.body()).digest()==sha256(original.read_bytes()).digest()
        check_header_and_controls(page)
        photo.focus();page.keyboard.press('Enter')
        dialog=page.get_by_role('dialog',name=telescope_name,exact=True)
        dialog.wait_for()
        assert dialog.locator('img').get_attribute('src')==photo_url
        assert dialog.get_by_role('link',name='Original photo',exact=True).get_attribute('href')==photo_url
        dialog.get_by_role('button',name='Zoom in',exact=True).click()
        assert dialog.get_by_label('Display zoom').inner_text()=='150%'
        page.keyboard.press('Escape')
        assert photo.evaluate('(e)=>e===document.activeElement')
        page.locator('.overview-heading').screenshot(path=str(OUT/'overview-photo-header.png'))
        page.locator('.overview-search img').wait_for(timeout=90000)
        page.locator('.overview-visibility img').wait_for(timeout=90000)
        page.wait_for_function('[...document.querySelectorAll(".overview-image img")].every(i=>i.complete&&i.naturalWidth>0)')
        assert page.locator('.overview-image img').count()==2
        assert page.locator('.overview-stats > a').count()==6
        counts=records['/api/observation/injections']['counts']
        assert page.locator('.recovery-summary > strong').inner_text()==f"{counts['recovered']} / {counts['completed_fired']}"
        assert page.locator('.injection-timeline [role=button]').count()==len(records['/api/observation/injections']['trials'])
        assert page.locator('.overview-map button.inspected').count()==len(records['/api/science/catalog']['inspection_inputs'])
        page.locator('.overview-map button.inspected').first.click()
        assert 'SNAP' in page.locator('.overview-antenna-detail').inner_text()
        assert not page.locator('.overview-history').evaluate('(e)=>e.open')
        assert '0–1000' not in page.locator('main').inner_text()
        page.screenshot(path=str(OUT/'overview-desktop.png'),full_page=True)
        for card in page.locator('.overview-stat').all():
            for label in card.locator(':scope > *').all():
                colors=label.evaluate('(e)=>({fg:getComputedStyle(e).color,bg:getComputedStyle(e.matches(".overview-status")?e:e.parentElement).backgroundColor})')
                light,dark=sorted([luminance(colors['fg']),luminance(colors['bg'])],reverse=True)
                assert (light+.05)/(dark+.05)>=4.5,colors
        for name in ['Search activity','Reference baseline dynamic spectrum']:
            page.get_by_role('button',name='Enlarge '+name,exact=True).click()
            page.get_by_role('dialog').wait_for()
            page.get_by_role('button',name='Zoom in',exact=True).click()
            page.keyboard.press('Escape')
        page.get_by_role('button',name='Enlarge timeline',exact=True).click()
        page.get_by_role('dialog').locator('.injection-timeline').wait_for()
        page.keyboard.press('Escape')
        point=page.locator('.injection-timeline [role=button]').first
        point.focus();page.keyboard.press('Enter')
        page.get_by_role('dialog').locator('.injection-detail').wait_for()
        page.keyboard.press('Escape')
        page.get_by_role('button',name='Phase',exact=True).click()
        page.get_by_role('button',name='Enlarge Reference baseline phase',exact=True).wait_for(timeout=90000)
        # Every historical panel receives exactly the same bounds and time zone.
        page.get_by_role('combobox',name='Time zone').select_option('UTC')
        page.get_by_text('Change history interval',exact=True).click()
        earlier=(datetime.fromisoformat(records['/api/observation/injections']['window_start_utc'])-timedelta(days=1)).date().isoformat()
        with page.expect_response(lambda r:'/api/observation/injections?' in r.url and earlier in r.url,timeout=60000) as changed:
            page.get_by_label('Observation day',exact=True).fill(earlier)
        history=changed.value.json()
        assert history['window_start_utc'].startswith(earlier+'T00:00')
        page.locator('.overview-injections .recovery-summary').wait_for(timeout=60000)
        assert 'Historical interval' in page.locator('.overview-controls').inner_text()
        assert 'Live snapshot · Now' in page.locator('main').inner_text()
        vislink=page.locator('.overview-visibility .overview-panel-footer a').get_attribute('href')
        q=parse_qs(urlparse(vislink).query)
        assert q['view']==['detail'] and q['t0'][0].startswith(earlier+'T00:00') and q['time_tz']==['UTC']
        searchlink=page.locator('.overview-search .overview-panel-footer a').get_attribute('href')
        for width in [1500,1000,720,390]:
            page.set_viewport_size(dict(width=width,height=1100))
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1'),width
            check_header_and_controls(page)
        photo.click()
        page.get_by_role('dialog',name=telescope_name,exact=True).wait_for()
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1')
        page.keyboard.press('Escape')
        page.screenshot(path=str(OUT/'overview-mobile.png'),full_page=True)
        with page.expect_response(lambda r:'/api/t1?' in r.url,timeout=60000) as response:
            page.goto(URL+searchlink)
        assert response.value.json()['t0'].startswith(earlier+'T00:00')
        assert page.get_by_role('combobox',name='Time zone').input_value()=='UTC'
        assert 'Historical interval' in page.locator('main').inner_text()
        assert all(urlparse(w).path=='/api/science/render' for w in writes),writes
        # Explicitly labelled fixtures: stale evidence must never remain green.
        fixture=deepcopy(records['/api/observation'])
        old=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
        fixture['clock']['utc']=old
        fixture['observation']['state']['observed_at']=old
        fixture['observation']['vis_age']['observed_at']=old
        stale=deepcopy(records['/api/t1/status']);stale['ledger']['last_tick_unix']=datetime.fromisoformat(old).timestamp()
        page.route('**/api/observation',lambda r:r.fulfill(json=fixture))
        page.route('**/api/t1/status',lambda r:r.fulfill(json=stale))
        page.route('**/api/snap-workspace/health',lambda r:r.fulfill(json={
            'streaming':{'state':'unknown','ok':0,'total':4},
            'pps':{'state':'unknown','ok':0,'total':4},'boards':[]}))
        page.route('**/api/science/render',lambda r:r.fulfill(status=503,json={'detail':'Display test: visibility unavailable'}))
        page.clock.install()
        page.goto(URL+'/observation')
        page.get_by_text('Search status unavailable or stale.',exact=False).wait_for()
        assert page.locator('.overview-stat.ok').count()==0
        page.get_by_text('Display test: visibility unavailable',exact=True).wait_for(timeout=60000)
        assert page.locator('.overview-injections .recovery-summary').count()==1
        assert page.locator('.overview-map button').count()>0
        # Clock-driven check: historical selection pauses history, not live status.
        await_reads=[]
        page.on('request',lambda r:await_reads.append(urlparse(r.url).path))
        page.get_by_text('Change history interval',exact=True).click()
        page.get_by_label('Observation day',exact=True).fill(earlier)
        page.clock.run_for(600)
        page.locator('.overview-injections .recovery-summary').wait_for(timeout=60000)
        await_reads.clear()
        with page.expect_request(lambda r:urlparse(r.url).path=='/api/observation'),page.expect_request(lambda r:urlparse(r.url).path=='/api/t1/status'):
            page.clock.fast_forward(125000)
        assert '/api/observation' in await_reads and '/api/t1/status' in await_reads
        assert '/api/observation/injections' not in await_reads
        fixture['injections'].update(status='unavailable',counts_complete=False)
        fixture['observation']['state'].update(value=None,observed_at=None)
        fixture['observation']['vis_age'].update(value=None,observed_at=None)
        stale['ledger']['last_tick_unix']=None
        empty=deepcopy(history)
        empty.update(trials=[],recent=[],counts_complete=True,status='ok')
        empty['counts']={key:None if key=='recovery_fraction' else 0 for key in empty['counts']}
        page.route('**/api/observation/injections?*',lambda r:r.fulfill(json=empty))
        page.goto(URL+'/observation')
        page.get_by_text('No trials recorded in this interval.',exact=False).wait_for()
        assert page.locator('.overview-stat.ok').count()==0
        assert page.locator('.recovery-summary > span > b').count()==0
        assert page.locator('.overview-stat').nth(2).locator('strong').inner_text()=='Unavailable'
        # Display fixtures: absolute recovered counts in the live 24 h summary.
        # Historical short windows and individual miss markers are not recoloured.
        for recovered,completed,failed,complete,fresh,expected in [
            (0,24,0,True,True,'silent'),(11,24,0,True,True,'silent'),
            (12,24,0,True,True,'orange'),(17,24,0,True,True,'orange'),
            (18,24,0,True,True,'yellow'),(20,24,0,True,True,'yellow'),
            (24,24,0,True,True,'yellow'),(25,25,0,True,True,'yellow'),
            (0,0,0,True,True,'unknown'),
            (0,0,1,True,True,'silent'),(20,24,0,False,True,'late'),
            (20,24,0,True,False,'late'),
        ]:
            fixture['clock']['utc']=page.evaluate('new Date().toISOString()') if fresh else old
            fixture['injections'].update(status='ok' if complete else 'partial',counts_complete=complete)
            fixture['injections']['counts'].update(recovered=recovered,completed_fired=completed,
                missed_t1=completed-recovered,missed_t2=0,fire_failed=failed,pending=0,unknown=0)
            page.goto(URL+'/observation')
            card=page.locator('.overview-stat').nth(2)
            card.get_by_text(f'{recovered} / {completed}',exact=True).wait_for()
            page.wait_for_function('(state)=>document.querySelectorAll(".overview-stat")[2]?.classList.contains(state)',arg=expected)
            assert card.locator('strong').inner_text()==f'{recovered} / {completed}'
            colors=card.locator('.overview-status').evaluate('(e)=>({fg:getComputedStyle(e).color,bg:getComputedStyle(e).backgroundColor})')
            light,dark=sorted([luminance(colors['fg']),luminance(colors['bg'])],reverse=True)
            assert (light+.05)/(dark+.05)>=4.5,colors
            if expected=='yellow':
                assert colors['bg']=='rgb(231, 196, 74)'
                assert card.locator('.overview-status').inner_text()=='Needs attention'
        assert not errors,errors
        browser.close()
    print(json.dumps({'result':'passed','checks':['telescope name and separate observatory location','original CASM photo, top-right layout and keyboard zoom','aligned time-zone and rolling controls','live counts','17 selection matches catalog','map wiring','two compact diagnostics','white figure zoom','trial details','amplitude/phase','shared historical bounds','Search drill-down','mobile','stale evidence','independent failures','history pause/live refresh','recovery threshold boundaries and contrast']},indent=2))


if __name__=='__main__': main()
