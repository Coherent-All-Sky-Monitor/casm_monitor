"""Saved-data-only playback acceptance. Never allow hardware/operational POSTs.

DOM/canvas checks use real cache responses. Failure/retry is an explicitly
intercepted GET. Timing measures committed frames, not physical display refresh.
"""
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = 'http://127.0.0.1:8061'
OUT = Path('/home/casm/scratch/casm-observation-preview/visibility-playback-check')


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport=dict(width=1500, height=1050))
        errors, writes, requests, manifests = [], [], [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: requests.append(r.url))
        def guard(route):
            if route.request.method not in ('GET', 'HEAD'):
                writes.append(route.request.url); route.abort()
            else:
                route.continue_()
        page.route('**/api/**', guard)
        def response(r):
            if '/api/science/array/playback?' in r.url and r.ok:
                manifests.append(r.json())
        page.on('response', response)
        def click(locator):
            locator.evaluate('(e)=>e.click()')
        def ready():
            page.wait_for_function('document.querySelector(".vis-movie .snap-movie-stage")?.getAttribute("aria-busy")==="false"', polling=100, timeout=120000)
        def paused():
            b=page.get_by_role('button', name='Pause visibility playback', exact=True)
            if b.count():click(b)
        def select(label, value):
            page.get_by_label(label, exact=True).select_option(str(value))
        def step(index,label='Visibility saved time',wait=True):
            page.get_by_label(label,exact=True).evaluate('''(e,v)=>{
              const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
              setter.call(e,String(v));e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));
            }''',index)
            if wait:ready()
        def download(name):
            OUT.mkdir(parents=True,exist_ok=True)
            with page.expect_download() as event:
                click(page.locator('.figure-modal').get_by_role('button',name='Download',exact=True))
            saved=event.value
            target=OUT/(name+Path(saved.suggested_filename).suffix)
            saved.save_as(str(target))
            assert target.stat().st_size>1000
            return target.name
        def speed_check(rates):
            results={}
            m=manifests[-1];start=((m['first_frame']+31)//32)*32+26
            for rate in rates:
                step(start);select('Visibility playback speed',rate)
                page.evaluate('''()=>{window.visFrames=[];const el=document.querySelector('.vis-movie .snap-movie-stage');
                  window.visObserver=new MutationObserver(()=>{if(el.dataset.frame)window.visFrames.push([performance.now(),Number(el.dataset.frame)]);});
                  window.visObserver.observe(el,{attributes:true,attributeFilter:['data-frame']});}''')
                click(page.get_by_role('button',name='Play visibility playback',exact=True))
                page.wait_for_function('window.visFrames.length>=66',polling=100,timeout=30000)
                paused();frames=page.evaluate('window.visFrames');page.evaluate('window.visObserver.disconnect()')
                assert all(b[1]==a[1]+1 for a,b in zip(frames,frames[1:])),frames
                intervals=[b[0]-a[0] for a,b in zip(frames,frames[1:])]
                results[str(rate)]=dict(frames=len(frames),mean_ms=statistics.mean(intervals),
                    median_ms=statistics.median(intervals),max_ms=max(intervals))
            return results
        page.goto(URL+'/vis',wait_until='domcontentloaded')
        page.wait_for_function('document.querySelectorAll(".antenna-plot").length>0',polling=100,timeout=60000)
        expected=page.locator('.antenna-plot').count()
        click(page.get_by_role('button',name='History',exact=True))
        ready()
        assert not page.get_by_role('button',name='Video playback',exact=True).count()
        assert page.get_by_role('button',name='Play visibility playback',exact=True).count()==1
        initial_time=page.locator('.vis-movie .snap-clock-row').get_attribute('data-timestamp')
        page.wait_for_timeout(1300)
        assert page.locator('.vis-movie .snap-clock-row').get_attribute('data-timestamp')==initial_time
        assert page.locator('.vis-movie-card').count()==expected
        assert page.locator('.vis-movie [data-clock]').count()==3
        if '--speeds-only' in sys.argv:
            assert page.get_by_label('Visibility playback speed',exact=True).locator('option').evaluate_all('(es)=>es.map(e=>e.value)')==['0.5','1','2','4','8','16','32']
            click(page.locator('[aria-label="Visibility playback period"]').get_by_role('button',name='48 hours',exact=True));ready()
            n=len(manifests[-1]['times'])-manifests[-1]['first_frame']
            autos=speed_check([32])
            select('Playback correlation','cross');ready()
            crosses=speed_check([4,8,16,32])
            assert 'remaining' in page.locator('.vis-movie .snap-movie-cadence').inner_text()
            # The expanded baseline has its own transport and survives buffering.
            before=page.locator('.vis-movie .snap-clock-row').get_attribute('data-timestamp')
            click(page.locator('.vis-movie-trigger').first)
            assert page.locator('.figure-modal .snap-clock-row').get_attribute('data-timestamp')==before
            assert page.locator('.vis-movie canvas').count()==0
            assert page.get_by_role('button',name='Play baseline playback',exact=True).count()==1
            select('Zoomed baseline playback speed',16)
            click(page.locator('.figure-modal').get_by_role('button',name='Zoom in',exact=True))
            at_index=int(page.locator('.vis-movie .snap-movie-stage').get_attribute('data-frame'))
            click(page.get_by_role('button',name='Play baseline playback',exact=True))
            page.wait_for_function('(i)=>Number(document.querySelector(".figure-modal canvas")?.dataset.frame)>=i+12',arg=at_index,polling=50,timeout=10000)
            zoom_download=download('playing-baseline')
            assert page.get_by_role('button',name='Play baseline playback',exact=True).count()==1
            assert page.get_by_label('Display zoom').inner_text()=='150%'
            held=[]
            page.route('**/api/science/array/playback-chunk?*',lambda route:held.append(route))
            target=len(manifests[-1]['times'])-70
            step(target,label='Zoomed baseline saved time',wait=False)
            page.wait_for_function('document.querySelector(".vis-movie .snap-movie-stage")?.getAttribute("aria-busy")==="true"',polling=50,timeout=10000)
            assert page.locator('.figure-modal').count()==1
            assert page.locator('.figure-modal canvas').count()==0
            assert page.locator('.figure-modal').get_by_role('button',name='Download',exact=True).is_disabled()
            page.wait_for_timeout(100)
            page.unroute('**/api/science/array/playback-chunk?*')
            for route in held:route.continue_()
            ready()
            assert page.get_by_label('Display zoom').inner_text()=='150%'
            assert int(page.locator('.figure-modal canvas').get_attribute('data-frame'))==target
            after=page.locator('.figure-modal .snap-clock-row').get_attribute('data-timestamp')
            assert float(after)==manifests[-1]['times'][target]
            page.screenshot(path=str(OUT/'baseline-player.png'),timeout=5000)
            click(page.locator('.figure-modal').get_by_role('button',name='Close ×',exact=True))
            assert page.locator('.vis-movie canvas').count()==expected
            assert page.locator('.vis-movie .snap-clock-row').get_attribute('data-timestamp')==after
            assert page.get_by_role('button',name='Play visibility playback',exact=True).count()==1
            assert not errors,errors
            assert not writes,writes
            result=dict(result='passed',inputs=expected,playback_frames=n,autos=autos,cross_dynamic=crosses,
                        zoom_download=zoom_download,zoom_buffering_survived=True,zoom_return_timestamp=after,
                        page_errors=errors,non_get_requests=writes)
            OUT.mkdir(parents=True,exist_ok=True)
            OUT.joinpath('speed-result.json').write_text(json.dumps(result,indent=2)+'\n')
            page.screenshot(path=str(OUT/'faster-speeds.png'),timeout=5000)
            print(json.dumps(result,indent=2));browser.close();return
        m=manifests[-1];step(m['first_frame']+4)
        at=page.locator('.vis-movie .snap-clock-row').get_attribute('data-timestamp')
        assert float(at)==m['times'][m['first_frame']+4]
        # Quantity/order changes are local and retain the exact timestamp.
        before=len([u for u in requests if '/array/playback?' in u])
        for quantity in ('Real(V)','Imag(V)','Phase','|V|'):
            click(page.locator('[aria-label="Playback quantity"]').get_by_role('button',name=quantity,exact=True))
            ready();assert page.locator('.vis-movie .snap-clock-row').get_attribute('data-timestamp')==at
        select('Playback arrangement','snap');ready()
        actual=[int(n) for n in page.locator('.vis-movie-card').evaluate_all('(es)=>es.map(e=>e.dataset.input)')]
        ants={a['packet_idx']:a for a in m['inputs']}
        assert actual==sorted(m['selected_inputs'],key=lambda i:(ants[i]['snap'],ants[i]['slot'],ants[i]['adc']))
        select('Playback arrangement','physical');ready();assert page.locator('.vis-movie-empty-station').count()>0
        select('Playback arrangement','compact');ready()
        assert len([u for u in requests if '/array/playback?' in u])==before
        # Open/close without losing the frame; download control is available.
        click(page.get_by_role('button',name=f"Enlarge playback {ants[m['selected_inputs'][0]]['station']}",exact=True))
        assert page.locator('.figure-modal [data-clock]').count()==3
        assert page.locator('.figure-modal').get_by_role('button',name='Download',exact=True).count()==1
        click(page.locator('.figure-modal').get_by_role('button',name='Zoom in',exact=True))
        assert page.get_by_label('Display zoom').inner_text()=='150%'
        downloads=[download('spectrum')]
        click(page.locator('.figure-modal').get_by_role('button',name='Close ×',exact=True))
        # Reference changes use actual cross data and never reset the cursor.
        select('Playback correlation','cross');ready();paused()
        assert page.locator('.vis-movie .snap-clock-row').get_attribute('data-timestamp')==at
        select('Playback reference antenna',18);ready()
        assert manifests[-1]['selection']['reference_input']==18
        assert page.locator('.vis-movie .snap-clock-row').get_attribute('data-timestamp')==at
        select('Playback reference antenna',8);ready()
        assert page.locator('.vis-movie canvas').count()==expected
        # Cross a chunk boundary at 4x, measuring actual React frame commits.
        m=manifests[-1];start=((m['first_frame']+31)//32)*32+26;step(start)
        select('Visibility playback speed',4)
        page.evaluate('''()=>{window.visFrames=[];const el=document.querySelector('.vis-movie .snap-movie-stage');
          window.visObserver=new MutationObserver(()=>{if(el.dataset.frame)window.visFrames.push([performance.now(),Number(el.dataset.frame)]);});
          window.visObserver.observe(el,{attributes:true,attributeFilter:['data-frame']});}''')
        click(page.get_by_role('button',name='Play visibility playback',exact=True))
        page.wait_for_function('window.visFrames.length>=18',polling=100,timeout=20000)
        paused();timings=page.evaluate('window.visFrames');page.evaluate('window.visObserver.disconnect()')
        assert all(b[1]==a[1]+1 for a,b in zip(timings,timings[1:])),timings
        intervals=[b[0]-a[0] for a,b in zip(timings,timings[1:])]
        assert max(intervals)<1000,intervals
        # Screenshot is optional on hosts where headless native paint is unavailable.
        OUT.mkdir(parents=True,exist_ok=True)
        screenshot='captured'
        try:page.screenshot(path=str(OUT/'cross-dynamic.png'),timeout=5000)
        except Exception as exc:screenshot='unavailable: '+str(exc).splitlines()[0]
        # Window means use full cached channels, not preview magnitudes.
        select('Playback view','spectrum');ready()
        click(page.locator('.vis-movie-settings summary'))
        select('Playback spectrum statistic','mean');ready()
        click(page.locator('.vis-movie-settings summary'))
        assert page.locator('.vis-movie .array-spectrum svg').count()==expected
        # Upper triangular matrix only; reordered cells retain their own direction.
        select('Playback view','matrix');ready()
        assert page.locator('.vis-movie-matrix td button').count()==expected*(expected+1)//2
        select('Playback arrangement','snap');ready()
        click(page.locator('.vis-movie-matrix td button').nth(1))
        assert page.locator('.figure-modal canvas').count()==1
        downloads.append(download('dynamic-spectrum'))
        click(page.locator('.figure-modal').get_by_role('button',name='Close ×',exact=True))
        # Longer periods are requested explicitly; use autos to bound this smoke test.
        select('Playback correlation','auto');ready()
        for name,span in [('48 hours',2),('Last week',7)]:
            click(page.locator('[aria-label="Visibility playback period"]').get_by_role('button',name=name,exact=True));ready()
            assert manifests[-1]['selection']['span_days']==span
        # Loop runs last -> first on actual saved samples, never an invented frame.
        m=manifests[-1];step(len(m['times'])-2)
        click(page.get_by_label('Loop',exact=True))
        page.evaluate('window.visFrames=[];window.visObserver.observe(document.querySelector(".vis-movie .snap-movie-stage"),{attributes:true,attributeFilter:["data-frame"]})')
        click(page.get_by_role('button',name='Play visibility playback',exact=True))
        page.wait_for_function('window.visFrames.length>=3',polling=100,timeout=30000)
        paused();loop_frames=page.evaluate('window.visFrames.map(x=>x[1])');page.evaluate('window.visObserver.disconnect()')
        assert loop_frames[:3]==[len(m['times'])-1,m['first_frame'],m['first_frame']+1],loop_frames
        # Explicit failure has no stale plot; retry the same historical selection.
        failures=[]
        def fail_once(route):
            if failures:route.fallback()
            else:
                failures.append(route.request.url)
                route.fulfill(status=503,content_type='application/json',body=json.dumps({'detail':'Acceptance test: temporary saved-history failure'}))
        page.route('**/api/science/array/playback?*',fail_once)
        previous_day=datetime.fromtimestamp(m['end']-86400,timezone.utc).date().isoformat()
        page.get_by_label('Visibility playback date (UTC)',exact=True).fill(previous_day)
        page.wait_for_function('document.querySelector(".snap-movie-error")?.textContent.includes("temporary saved-history failure")',polling=100,timeout=15000)
        assert page.locator('.vis-movie .snap-movie-stage [role="img"]').count()==0
        click(page.get_by_role('button',name='Reload saved sequence',exact=True));ready()
        assert page.get_by_label('Visibility playback date (UTC)',exact=True).input_value()==previous_day
        assert datetime.fromtimestamp(manifests[-1]['end'],timezone.utc).date().isoformat()==previous_day
        page.unroute('**/api/science/array/playback?*',fail_once)
        for width in (1500,720,390):
            page.set_viewport_size(dict(width=width,height=1050))
            assert page.evaluate('document.querySelector(".vis-movie").scrollWidth<=innerWidth+1'),width
        assert not writes,writes
        assert not errors,errors
        click(page.get_by_role('button',name='Close visibility playback',exact=True))
        assert page.locator('dialog.vis-movie').count()==0
        assert page.get_by_role('button',name='Live',exact=True).get_attribute('aria-pressed')=='true'
        assert page.locator('.antenna-plot').count()==expected
        result=dict(result='passed',inputs=expected,history_one_click=True,history_opens_paused=True,close_returns_live=True,frame_intervals_ms=intervals,screenshot=screenshot,
                    loop_frames=loop_frames,downloads=downloads,load_failures_retried=len(failures),
                    manifests=len(manifests),page_errors=errors,non_get_requests=writes)
        OUT.joinpath('result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2))
        browser.close()


if __name__=='__main__':main()
