"""Browser-only refresh fixtures: delays, image failures and health expiry.

All API requests are intercepted. No scientific render, hardware query, upload,
annotation or production write reaches the server. Only built frontend assets
are read from 8061. Fixture figures do not represent telescope data.
"""
import asyncio
import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect

BASE = 'http://127.0.0.1:8061'
PIXEL = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==')
IMAGE = 'data:image/png;base64,' + base64.b64encode(PIXEL).decode()


async def main():
    output = Path('/home/casm/scratch/casm-observation-preview/refresh-stability-20261008')
    output.mkdir(parents=True, exist_ok=True)
    errors, blocked, checks = [], [], []
    gates, failures, hits = {}, set(), {}
    epoch = datetime.now(timezone.utc).timestamp()
    iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat()
    inputs = [dict(packet_idx=8+i, antenna=9+i, station=f'N0{i+1}E1',
                   position_enu_m=[0,i,0], snap=0, slot='A', adc=i, beamforming=True)
              for i in range(3)]
    freq = [390.625,410,440,460,484.375]
    tile = dict(src=IMAGE, scale=IMAGE, min=0, max=10, log=False, width=1, height=1)
    t1_version = 0
    snap_version = 0

    def snapshot(q):
        ref = int(q.get('reference_input',['8'])[0]); mode=q.get('mode',['auto'])[0]
        return dict(inputs=inputs, default_inputs=[8,9,10], membership={'status':'complete'},
                    reference_input=ref, reference=q.get('reference',['raw'])[0], mode=mode,
                    freq_mhz=freq, t0=epoch-86400, t1=epoch, samples=10, integration_s=137,
                    provenance={}, matrix={}, panels=[dict(input=a['packet_idx'], pair=[a['packet_idx'],ref],
                    is_auto=mode=='auto' or a['packet_idx']==ref, valid_fraction=1,
                    spectra={k:dict(latest=[1,2,3,2,1],mean=[1,2,3,2,1]) for k in ('real','imag','amp','phase')},
                    images={k:tile for k in ('real','imag','amp','phase')}, power_images={'db':tile,'linear':tile}) for a in inputs])

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        context = await browser.new_context(viewport=dict(width=1440,height=1000))
        page = await context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        await page.clock.install()

        async def route_api(route):
            nonlocal t1_version, snap_version
            request=route.request; url=urlsplit(request.url); path=url.path; q=parse_qs(url.query)
            hits[path]=hits.get(path,0)+1
            # Render POST is fulfilled locally too; no action is forwarded.
            if request.method not in ('GET','HEAD') and path!='/api/science/render':
                blocked.append(path); await route.abort(); return
            key=path
            if path=='/api/science/array': key+=':'+q.get('reference_input',['8'])[0]
            if key in gates: await gates[key].wait()
            if path in failures:
                await route.fulfill(status=503,json={'detail':'Synthetic refresh failure'}); return
            if path.startswith('/api/fixture-plot'):
                await route.fulfill(content_type='image/png',body=PIXEL); return
            streams=[dict(stream=i,node='fixture',status='ok',last_gulp_age_s=1) for i in range(8)]
            if path=='/api/science/array': data=snapshot(q)
            elif path=='/api/science/array/pairs':
                pairs=[[int(i) for i in pair.split(':')] for pair in q['pairs'][0].split(',')]
                data=dict(t0=float(q['t0'][0]),t1=float(q['t1'][0]),quantity=q['quantity'][0],freq_mhz=freq,
                          panels=[dict(pair=pair,stored_pair=sorted(pair),tile=tile,valid_fraction=1) for pair in pairs])
            elif path=='/api/t1/status': data=dict(streams=streams,ledger=dict(last_tick_unix=epoch))
            elif path=='/api/t1':
                data=dict(t0=q['t0'][0],t1=q['t1'][0],time_tz=q['time_tz'][0],streams=streams,ledger={},
                          plot_url=f'/api/fixture-plot-{t1_version}.png',activity_plot_url=f'/api/fixture-plot-{t1_version}.png')
            elif path=='/api/snap-workspace/acquisition':
                data=dict(configured_interval_s=3600,latest_job=dict(finished=str(snap_version)),manual_enabled=False)
            elif path=='/api/snap-workspace/health':
                ok=dict(state='ok',ts=epoch,detail='Fixture',delta_ticks=-1)
                data=dict(boards=[dict(ip='fixture',feng_id=0,slot='A',streaming=ok,pps_status=ok,timing_status=ok)],
                          streaming=dict(state='ok',ok=1,total=1),timing=dict(state='ok',ok=1,total=1),pps=dict(state='ok',ok=1,total=1))
            elif path=='/api/snap-workspace/beamforming':
                data=dict(status='complete',inputs=[dict(ip='fixture',adc=i,antenna=9+i,beamforming=True) for i in range(3)])
            elif path=='/api/snap-workspace/spectra':
                data=dict(freq_mhz=freq,configured_interval_s=3600,boards=[dict(ip='fixture',feng_id=0,slot='A',ts=epoch+snap_version,
                          spectra_db=[[1,2,3,2,1]]*3,inputs=[dict(adc=i,antenna=9+i,station=f'N0{i+1}E1',functional=True,packet_idx=8+i) for i in range(3)])])
            elif path=='/api/science/catalog':
                data=dict(current_layout='fixture',layouts=[],inputs=inputs,inspection_inputs=[8,9,10],
                          baselines=[dict(i=8,j=9,length_m=1,orientation='N-S')])
            elif path=='/api/science/render':
                data=dict(images=[dict(url=f'/api/fixture-plot-{t1_version}.png')])
            elif path=='/api/observation':
                data=dict(clock=dict(utc=iso(epoch),lst='12:00:00'),observation=dict(id='fixture',state=dict(value='running',observed_at=iso(epoch)),vis_age=dict(value=0,observed_at=iso(epoch))),
                          deployment=dict(inspection_state='complete'),layout=dict(points=[dict(packet_idx=i,deployed=True) for i in (8,9,10)]))
            elif path=='/api/observation/injections':
                data=dict(status='ok',counts_complete=True,counts=dict(recovered=1,completed_fired=1),window_start_utc=q['t0'][0],window_end_utc=q['t1'][0],trials=[])
            else: data={}
            await route.fulfill(json=data)

        await context.route('**/api/**',route_api)
        async def visit(path):
            await page.goto(BASE+path,wait_until='domcontentloaded')
        async def ready():
            await expect(page.locator('.array-results')).to_have_attribute('aria-busy','false')
        async def hold(key, action):
            event=asyncio.Event(); gates[key]=event
            await action()
            await page.wait_for_timeout(650)
            return event
        async def release(key,event):
            del gates[key]; event.set()

        await visit('/vis'); await ready()
        plots=page.locator('.array-results')
        old=await plots.inner_html()
        event=await hold('/api/science/array:8',lambda:page.get_by_role('button',name='Refresh',exact=True).click())
        await expect(plots).to_have_attribute('aria-busy','true')
        assert await plots.evaluate('e=>getComputedStyle(e).opacity')=='1'
        assert await plots.evaluate('e=>getComputedStyle(e).pointerEvents')!='none'
        assert await plots.inner_html()==old
        await expect(page.locator('.array-status .refresh-status')).to_have_text('Updating…')
        await page.locator('.plot-zoom-trigger').first.click()
        await page.get_by_role('button',name='Zoom in',exact=True).click()
        await release('/api/science/array:8',event); await ready()
        await expect(page.get_by_label('Display zoom')).to_have_text('150%')
        await page.keyboard.press('Escape')
        checks.append('refresh keeps full opacity, clickable plots and zoom')

        await page.get_by_role('button',name='Cross-correlations',exact=True).click(); await ready()
        old_title=await page.locator('.array-results-heading').inner_text()
        event=await hold('/api/science/array:9',lambda:page.get_by_label('Reference antenna').select_option('9'))
        assert await page.locator('.array-results-heading').inner_text()==old_title
        await page.get_by_label('Reference antenna').select_option('10'); await ready()
        await expect(page.locator('.array-results-heading')).to_contain_text('N03E1')
        await release('/api/science/array:9',event); await page.wait_for_timeout(200)
        await expect(page.locator('.array-results-heading')).to_contain_text('N03E1')
        checks.append('reference identity remains paired; late response cannot replace new reference')
        await page.get_by_role('button',name='All pairs',exact=True).click()
        await expect(page.locator('.pair-dynamics td img')).to_have_count(6)
        matrix_before=await page.locator('.pair-dynamics table').inner_html()
        event=await hold('/api/science/array/pairs',lambda:page.get_by_role('button',name='Phase',exact=True).click())
        assert await page.locator('.pair-dynamics table').inner_html()==matrix_before
        await page.locator('.pair-dynamics td button').first.click()
        await expect(page.get_by_role('dialog')).to_contain_text('|V|')
        await page.keyboard.press('Escape')
        await release('/api/science/array/pairs',event)
        await expect(page.locator('.pair-dynamics')).to_contain_text('Phase (rad)')
        checks.append('upper triangle keeps previous complete matrix and correct zoom labels')

        await visit('/search')
        img=page.locator('.plot-surface img'); await expect(img).to_have_attribute('src','/api/fixture-plot-0.png')
        old_caption=await page.locator('.plot-surface figcaption').inner_text()
        t1_version=1
        event=await hold('/api/fixture-plot-1.png',lambda:page.get_by_role('button',name='Live · rolling 24 h',exact=True).click())
        # Force a different rolling endpoint after the first cached interval.
        await page.clock.fast_forward(301000)
        await page.wait_for_timeout(150)
        assert await img.get_attribute('src')=='/api/fixture-plot-0.png'
        assert await page.locator('.plot-surface figcaption').inner_text()==old_caption
        await img.click(); await page.get_by_role('button',name='Zoom in',exact=True).click()
        await release('/api/fixture-plot-1.png',event)
        await expect(img).to_have_attribute('src','/api/fixture-plot-1.png')
        await expect(page.get_by_label('Display zoom')).to_have_text('150%')
        await expect(page.get_by_role('dialog').locator('img')).to_have_attribute('src','/api/fixture-plot-0.png')
        await page.keyboard.press('Escape')
        t1_version=2; failures.add('/api/fixture-plot-2.png')
        await page.clock.fast_forward(301000)
        await expect(page.get_by_text('Plot image could not be decoded',exact=False)).to_be_visible()
        assert await img.get_attribute('src')=='/api/fixture-plot-1.png'
        failures.clear()
        checks.append('Search commits decoded images and dates together; failed image keeps last plot; open zoom stays pinned')

        await visit('/snaps')
        await expect(page.locator('.snap-spectrum-card')).to_have_count(3)
        await expect(page.locator('.snap-health-badge.ok')).to_have_count(2)
        event=asyncio.Event(); gates['/api/snap-workspace/health']=event
        member_event=asyncio.Event(); gates['/api/snap-workspace/beamforming']=member_event
        await page.clock.fast_forward(31000); await page.wait_for_timeout(150)
        await expect(page.locator('.snap-health-badge.ok')).to_have_count(2)
        await expect(page.locator('.snap-spectrum-card.in-beamforming')).to_have_count(3)
        failures.add('/api/snap-workspace/health')
        await release('/api/snap-workspace/health',event)
        await release('/api/snap-workspace/beamforming',member_event)
        await expect(page.get_by_text('Status refresh failed. Current state is unknown.',exact=True)).to_be_visible()
        await expect(page.locator('.snap-health-badge.ok')).to_have_count(0)
        failures.clear(); await page.clock.fast_forward(31000)
        await expect(page.locator('.snap-health-badge.ok')).to_have_count(2)
        snap_version=1
        event=asyncio.Event(); gates['/api/snap-workspace/spectra']=event
        await page.clock.fast_forward(11000); await page.wait_for_timeout(150)
        await expect(page.locator('.snap-spectrum-card')).to_have_count(3)
        await release('/api/snap-workspace/spectra',event)
        event=asyncio.Event(); gates['/api/snap-workspace/health']=event
        await page.clock.fast_forward(121000); await page.wait_for_timeout(150)
        await expect(page.locator('.snap-health-badge.ok')).to_have_count(0)
        await expect(page.get_by_text('Status refresh is overdue. Current state is unknown.',exact=True)).to_be_visible()
        await release('/api/snap-workspace/health',event)
        checks.append('SNAP refresh keeps health/membership and hourly spectra; failures and hard expiry remove green')

        await visit('/observation')
        preview=page.locator('.overview-visibility')
        await expect(preview.locator('.overview-image img')).to_have_attribute('src','/api/fixture-plot-2.png')
        t1_version=3
        event=await hold('/api/fixture-plot-3.png',lambda:preview.get_by_role('button',name='Phase',exact=True).click())
        await expect(preview.locator('.overview-image img')).to_have_attribute('src','/api/fixture-plot-2.png')
        await preview.get_by_role('button',name='Enlarge Reference baseline dynamic spectrum',exact=True).click()
        await page.get_by_role('button',name='Zoom in',exact=True).click()
        await release('/api/fixture-plot-3.png',event)
        await expect(preview.locator('.overview-image img')).to_have_attribute('src','/api/fixture-plot-3.png')
        await expect(page.get_by_role('dialog').locator('img')).to_have_attribute('src','/api/fixture-plot-2.png')
        await expect(page.get_by_label('Display zoom')).to_have_text('150%')
        await page.keyboard.press('Escape')
        checks.append('Overview replacement decodes off-screen and existing zoom remains open and pinned')

        assert not errors,errors
        assert not blocked,blocked
        result=dict(passed=True,fixture_only=True,checks=checks,errors=errors,blocked_writes=blocked,requests=hits)
        (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2),flush=True)
        await browser.close()


if __name__=='__main__':
    asyncio.run(main())
