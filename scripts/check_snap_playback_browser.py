"""GET-only real-data playback checks; --dom-only avoids broken local RAF/paint.

DOM-only mode checks data, controls and layout bounds, not native hit targets,
painting or PNG export. No acquisition or production writes are allowed.
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dom-only',action='store_true')
    parser.add_argument('--all-adcs',action='store_true')
    parser.add_argument('--output',type=Path,default=Path('/home/casm/scratch/casm-observation-preview/screenshots'))
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        page=b.new_page(viewport={'width':1500,'height':1100},accept_downloads=True)
        errors,mutations,requests=[],[],[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        def guard(r):
            requests.append(r.request.url)
            if r.request.method!='GET':mutations.append(r.request.url);r.abort()
            else:r.continue_()
        page.route('**/api/**',guard)
        def wait(expr):page.wait_for_function(expr,polling=100,timeout=60000)
        def click(name):
            el=page.get_by_role('button',name=name,exact=True)
            expect(el).to_be_enabled(timeout=60000)
            if args.dom_only:el.evaluate('e=>e.click()')
            else:el.click()
        def field(label,value,kind='input'):
            el=page.get_by_label(label,exact=True)
            if args.dom_only or label=='Playback saved time':el.evaluate('''(e,v)=>{
              Object.getOwnPropertyDescriptor(e.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype,'value').set.call(e,v);
              e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));}''',value)
            elif kind=='select':el.select_option(value)
            else:el.fill(value)
        page.goto('http://127.0.0.1:8061/snaps')
        wait("document.querySelectorAll('.snap-spectrum-card').length===16")
        assert page.get_by_role('button',name='Live',exact=True).get_attribute('aria-pressed')=='true'
        assert not page.get_by_role('button',name='Video playback',exact=True).count()
        assert not any('/spectra-playback?' in u for u in requests)
        count=48 if args.all_adcs else 16
        suffix='-all-adcs' if args.all_adcs else ''
        if args.all_adcs:
            switch=page.get_by_role('switch',name='All 12 ADCs per SNAP',exact=False)
            switch.evaluate('e=>e.click()') if args.dom_only else switch.click()
            wait("document.querySelectorAll('.snap-spectrum-card').length===48")
        click('History')
        wait(f"document.querySelectorAll('.snap-spectrum-card').length==={count}")
        click('Video playback')
        wait(f"document.querySelectorAll('.snap-movie-card').length==={count}")
        def check_clocks(selector):
            values=page.locator(selector).evaluate('''e=>({at:Number(e.dataset.timestamp),
                utc:e.querySelector('[data-clock=utc]').dateTime,local:e.querySelector('[data-clock=local]').dateTime,
                lst:e.querySelector('[data-clock=lst]').textContent,
                tops:Array.from(e.children).map(v=>v.getBoundingClientRect().top)})''')
            assert values['utc']==values['local']
            assert len(values['lst'].split(':'))==3 and values['lst']!='Unavailable',values
            return values
        clocks=check_clocks('.snap-movie-clock .snap-clock-row')
        assert max(clocks['tops'])-min(clocks['tops'])<3,clocks
        wait("document.querySelector('[aria-label=\"Playback saved time\"]').value!=='0'")
        click('Pause playback')
        old=page.get_by_label('Playback saved time').input_value()
        page.wait_for_timeout(1400)
        assert page.get_by_label('Playback saved time').input_value()==old
        axes=page.locator('.snap-movie-card svg').evaluate_all('es=>es.map(e=>e.dataset.yMin+","+e.dataset.yMax)')
        assert len(set(axes))==1
        click('Playback next read')
        wait("!document.querySelector('.snap-movie-stage').classList.contains('buffering')")
        assert axes==page.locator('.snap-movie-card svg').evaluate_all('es=>es.map(e=>e.dataset.yMin+","+e.dataset.yMax)')
        assert all('outside displayed limits' not in t for t in page.locator('.snap-movie-card .snap-readout').all_text_contents())
        # Every default panel fits in the desktop stage without scrolling.
        if not args.all_adcs:assert page.locator('.snap-movie-stage').evaluate('e=>e.scrollHeight<=e.clientHeight+2')
        field('Playback time zone','America/Los_Angeles','select')
        assert 'PDT' in page.locator('.snap-movie-clock').inner_text() or 'PST' in page.locator('.snap-movie-clock').inner_text()
        for label,span in [('Last 48 hours',2),('Last week',7)]:
            click(label)
            wait(f"document.querySelectorAll('.snap-movie-card').length==={count}")
            click('Pause playback')
            assert page.get_by_role('button',name=label,exact=True).get_attribute('aria-pressed')=='true'
            assert any(f'span_days={span}' in u for u in requests)
            assert page.get_by_label('Playback time zone').input_value()=='America/Los_Angeles'
        # Run through a batch boundary at 4x; no per-frame JSON round trips.
        field('Playback speed','4','select')
        field('Playback saved time','0')
        wait("!document.querySelector('.snap-movie-stage').classList.contains('buffering')")
        mark=len(requests)
        page.evaluate('''()=>{window.playbackTiming={frames:[],buffering:0};
          let last='';window.playbackObserver=new MutationObserver(()=>{
            const clock=document.querySelector('.snap-movie-clock'),m=clock.textContent.match(/read (\\d+) \\/ /);
            if(m&&m[1]!==last){last=m[1];window.playbackTiming.frames.push({index:Number(last),at:performance.now()});}
            if(document.querySelector('.snap-movie-stage').getAttribute('aria-busy')==='true')window.playbackTiming.buffering++;
          });window.playbackObserver.observe(document.querySelector('.snap-movie-content'),{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['aria-busy']});}''')
        click('Play playback')
        wait('window.playbackTiming.frames.length>=18')
        click('Pause playback')
        timing=page.evaluate('()=>{window.playbackObserver.disconnect();return window.playbackTiming;}')
        intervals=[round(b['at']-a['at'],1) for a,b in zip(timing['frames'],timing['frames'][1:])]
        assert max(intervals)<450,intervals
        assert timing['buffering']==0,timing
        assert all(b['index']==a['index']+1 for a,b in zip(timing['frames'],timing['frames'][1:])),timing
        assert not any('/spectra?ids=' in u for u in requests[mark:])
        timing['intervals_ms']=intervals
        timing['packed_requests']=sum('/spectra-playback-frames?' in u for u in requests[mark:])
        # Freeze the real DOM for optional alternate-browser visual inspection.
        html=page.evaluate('''()=>{const d=document.documentElement.cloneNode(true);
          d.querySelectorAll('script').forEach(e=>e.remove());
          const base=document.createElement('base');base.href=location.origin+'/';d.querySelector('head').prepend(base);
          return '<!doctype html>'+d.outerHTML;}''')
        (args.output/f'snaps-playback{suffix}-dom.html').write_text(html)
        if not args.dom_only:page.screenshot(path=str(args.output/f'snaps-playback{suffix}.png'),timeout=15000)
        page.locator('.snap-movie-card .snap-plot-trigger').first.evaluate('e=>e.click()') if args.dom_only else page.locator('.snap-movie-card .snap-plot-trigger').first.click()
        wait("document.querySelector('.figure-modal')!==null")
        check_clocks('.figure-modal .snap-clock-row')
        assert page.get_by_role('button',name='Download',exact=True).count()==1
        if not args.dom_only:
            with page.expect_download():click('Download')
        page.keyboard.press('Escape')
        wait("document.querySelector('.figure-modal')===null")
        assert page.get_by_role('dialog',name='SNAP history playback',exact=True).count()==1
        # Loop's first batch is prefetched while the final batch is on screen.
        maximum=int(page.get_by_label('Playback saved time').get_attribute('max'))
        page.get_by_label('Loop',exact=True).evaluate('e=>e.click()')
        field('Playback saved time',str(maximum-2))
        wait("!document.querySelector('.snap-movie-stage').classList.contains('buffering')")
        page.wait_for_timeout(2000)
        click('Play playback')
        wait("document.querySelector('[aria-label=\"Playback saved time\"]').value==='0'")
        click('Pause playback')
        page.get_by_label('Loop',exact=True).evaluate('e=>e.click()')
        field('Playback saved time',page.get_by_label('Playback saved time').get_attribute('max'))
        wait("document.querySelector('.snap-movie-clock').textContent.includes('End of playback')")
        click('Replay history')
        wait("document.querySelector('[aria-label=\"Playback saved time\"]').value==='0'")
        click('Pause playback')
        for width in [390,320]:
            page.set_viewport_size({'width':width,'height':900})
            assert page.locator('.snap-movie').evaluate('e=>e.scrollWidth<=e.clientWidth+2')
        wait("!document.querySelector('.snap-movie-stage').classList.contains('buffering')")
        click('Open this read')
        wait("document.querySelector('.snap-movie')===null")
        assert page.get_by_role('button',name='History',exact=True).get_attribute('aria-pressed')=='true'
        wait("document.querySelector('.snap-history-bar .snap-clock-row')!==null")
        check_clocks('.snap-history-bar .snap-clock-row')
        page.locator('.snap-spectrum-card .snap-plot-trigger').first.evaluate('e=>e.click()')
        wait("document.querySelector('.figure-modal .snap-clock-row')!==null")
        check_clocks('.figure-modal .snap-clock-row')
        page.keyboard.press('Escape')
        wait("document.querySelector('.figure-modal')===null")
        # A failed saved-data request pauses and can be retried, not filled with
        # a stale frame carrying the requested timestamp.
        failed=[False]
        def fail_once(route):
            if failed[0]:route.fallback()
            else:failed[0]=True;route.fulfill(status=503,body='Test: saved read temporarily unavailable')
        page.route('**/spectra-playback-frames?**',fail_once)
        click('Video playback')
        wait("document.querySelector('.snap-movie-error')!==null")
        assert page.get_by_role('button',name='Play playback',exact=True).count()==1
        click('Retry saved read')
        wait("document.querySelectorAll('.snap-movie-card').length>0&&!document.querySelector('.snap-movie-stage').classList.contains('buffering')")
        click('Close playback')
        page.unroute('**/spectra-playback-frames?**',fail_once)
        page.reload()
        wait("document.querySelectorAll('.snap-spectrum-card').length===16")
        assert page.get_by_role('button',name='Live',exact=True).get_attribute('aria-pressed')=='true'
        assert not errors and not mutations,(errors,mutations)
        result=dict(mode='DOM-only' if args.dom_only else 'native-browser',live_default=True,play_pause=True,
                    day_48h_week=True,timezone=True,shared_fixed_axes=True,inputs=count,desktop_all_panels=not args.all_adcs,
                    mobile_bounds=True,zoom=True,download_verified=not args.dom_only,
                    playback_4x=timing,
                    loop=True,error_retry=True,
                    simultaneous_utc_local_lst=True,historical_zoom_clocks=True,
                    page_errors=errors,mutations=mutations,paint_verified=not args.dom_only)
        (args.output/f'snaps-playback{suffix}-check.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result));b.close()


if __name__=='__main__':main()
