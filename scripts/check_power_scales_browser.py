"""GET-only checks of power units on saved SNAP, visibility and solve data."""
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    output=Path('/home/casm/scratch/casm-observation-preview/screenshots/power-scales-20261005')
    output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1500,'height':1050},accept_downloads=True)
        errors,writes,requests=[],[],[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        def guard(route):
            requests.append(route.request.url)
            if route.request.method not in ('GET','HEAD'):
                writes.append(route.request.url);route.abort()
            else:route.continue_()
        page.route('**/api/**',guard)
        def scale(name,scope=None):
            (scope or page).get_by_role('group',name='Power scale',exact=True).get_by_role('button',name=name,exact=True).click()
        def shot(name):page.screenshot(path=str(output/name),timeout=15000)
        def fits_zoom():
            page.wait_for_timeout(250)
            assert page.locator('.figure-zoom-viewport').evaluate('e=>e.scrollHeight<=e.clientHeight+2')
        def hover_power(scope):
            spectrum=scope.locator('.array-spectrum').first
            spectrum.locator('svg').hover()
            expect(spectrum.locator('.spectrum-readout')).to_contain_text('MHz ·')
            readout=spectrum.locator('.spectrum-readout').inner_text()
            assert readout.endswith(' dB') and 'count' not in readout.lower(),readout
            assert 'Power (dB)' in spectrum.locator('svg').get_attribute('aria-label')
            assert '1 count' not in scope.inner_text().lower()
        page.goto('http://127.0.0.1:8061/snaps')
        page.wait_for_function("document.querySelectorAll('.snap-spectrum-card').length===16")
        expect(page.get_by_role('group',name='Power scale').get_by_role('button',name='Power (dB)',exact=True)).to_have_attribute('aria-pressed','true')
        assert page.locator('.snap-spectrum-card svg[data-power-scale=db]').count()==16
        scale('Linear')
        assert page.locator('.snap-spectrum-card svg[data-power-scale=linear]').count()==16
        values=page.locator('.snap-spectrum-card svg').evaluate_all('es=>es.map(e=>[Number(e.dataset.yMin),Number(e.dataset.yMax)])')
        assert len(set(map(tuple,values)))==1 and 0<=values[0][0]<values[0][1]
        page.locator('.snap-spectrum-card .snap-plot-trigger').first.click()
        modal=page.locator('.figure-modal')
        expect(modal.locator('.snap-plot svg')).to_have_count(2,timeout=60000)
        assert modal.locator('svg[data-power-scale=linear]').count()==2
        scale('Power (dB)',modal)
        assert modal.locator('svg[data-power-scale=db]').count()==2
        modal.get_by_role('button',name='Close ×',exact=True).click()
        page.get_by_role('button',name='History',exact=True).click()
        page.wait_for_function("document.querySelectorAll('.snap-movie-card').length===16",timeout=60000)
        movie=page.locator('.snap-movie')
        assert movie.locator('svg[data-power-scale=db]').count()==16
        before=movie.get_by_label('Playback saved time').input_value()
        scale('Linear',movie)
        assert movie.get_by_label('Playback saved time').input_value()==before
        assert movie.locator('svg[data-power-scale=linear]').count()==16
        movie.locator('.snap-plot-trigger').first.click()
        scale('Power (dB)',page.locator('.figure-modal'))
        assert page.locator('.figure-modal svg[data-power-scale=db]').count()==1
        page.locator('.figure-modal').get_by_role('button',name='Play antenna playback',exact=True).click()
        page.wait_for_function("Number(document.querySelector('.snap-expanded').dataset.frame)>0")
        scale('Linear',page.locator('.figure-modal'))
        assert page.get_by_role('button',name='Play antenna playback',exact=True).count()==1
        fits_zoom()
        shot('snap-antenna-linear.png')
        page.keyboard.press('Escape')
        assert movie.locator('svg[data-power-scale=linear]').count()==16
        movie.get_by_role('button',name='Close playback',exact=True).click()

        page.goto('http://127.0.0.1:8061/vis')
        page.wait_for_function("document.querySelectorAll('.antenna-plot').length===16",timeout=60000)
        assert page.locator('.array-spectrum[data-power-scale=db]').count()==16
        hover_power(page.locator('main'))
        page.locator('.antenna-plot .plot-zoom-trigger').first.click()
        hover_power(page.locator('.figure-modal'))
        page.keyboard.press('Escape')
        db_max=float(page.locator('.array-spectrum svg').first.get_attribute('data-y-max'))
        scale('Linear')
        linear_max=float(page.locator('.array-spectrum svg').first.get_attribute('data-y-max'))
        assert abs(10**(db_max/10)/linear_max-1)<1e-9,(db_max,linear_max)
        scale('Power (dB)')
        page.get_by_role('button',name='Dynamic spectrum',exact=True).click()
        expect(page.locator('.tile-scale').first).to_contain_text('Power (dB)')
        first_image=page.locator('.array-dynamic>img').first.get_attribute('src')
        scale('Linear')
        expect(page.locator('.tile-scale').first).to_contain_text('Power (counts)')
        assert page.locator('.array-dynamic>img').first.get_attribute('src')!=first_image
        page.get_by_role('button',name='History',exact=True).click()
        page.wait_for_function("document.querySelector('.vis-movie .snap-movie-stage').hasAttribute('data-frame')",timeout=90000)
        movie=page.locator('.vis-movie')
        assert movie.locator('canvas[data-power-scale=linear]').count()==16
        index=movie.get_by_label('Visibility saved time').input_value()
        count=sum('/array/playback?' in url for url in requests)
        scale('Power (dB)',movie)
        assert movie.get_by_label('Visibility saved time').input_value()==index
        assert movie.locator('canvas[data-power-scale=db]').count()==16
        assert sum('/array/playback?' in url for url in requests)==count
        movie.locator('.vis-movie-trigger').first.click()
        modal=page.locator('.figure-modal')
        scale('Linear',modal)
        assert modal.locator('canvas[data-power-scale=linear]').count()==1
        fits_zoom()
        shot('visibility-auto-linear.png')
        page.keyboard.press('Escape')
        movie.get_by_label('Playback view',exact=True).select_option('spectrum')
        page.wait_for_function("document.querySelectorAll('.vis-movie .array-spectrum[data-power-scale=linear]').length===16",timeout=60000)
        scale('Power (dB)',movie)
        assert movie.locator('.array-spectrum[data-power-scale=db]').count()==16
        hover_power(movie)
        movie.locator('.vis-movie-trigger').first.click()
        hover_power(page.locator('.figure-modal'))
        page.keyboard.press('Escape')
        movie.get_by_label('Playback correlation',exact=True).select_option('cross')
        page.wait_for_function("document.querySelector('.vis-movie .snap-movie-stage').hasAttribute('data-frame')",timeout=90000)
        assert movie.get_by_role('group',name='Power scale',exact=True).count()==0
        assert movie.locator('canvas[data-power-scale]').count()==0
        page.keyboard.press('Escape')

        page.goto('http://127.0.0.1:8061/cal/sun_20260930_200911')
        page.locator('#cal-autos .cal-antenna-panel').first.wait_for(timeout=60000)
        autos=page.locator('#cal-autos')
        assert autos.locator('.cal-auto-plots[data-auto-scale=db]').count()==16
        assert 'Power (dB)' in autos.locator('svg').first.get_attribute('aria-label')
        scale('Linear',autos)
        assert autos.locator('.cal-auto-plots[data-auto-scale=linear]').count()==16
        assert 'Power (counts)' in autos.locator('svg').first.get_attribute('aria-label')
        autos.locator('.cal-plot-button').first.click()
        scale('Power (dB)',page.locator('.figure-modal'))
        assert page.locator('.figure-modal .cal-auto-plots[data-auto-scale=db]').count()==1
        shot('calibration-autos-db.png')
        with page.expect_download() as downloaded:
            page.locator('.figure-modal').get_by_role('button',name='Download',exact=True).click()
        assert downloaded.value.failure() is None
        assert not errors and not writes,(errors,writes)
        result=dict(result='passed',snap_live_history_zoom=True,visibility_spectrum_dynamic_history_zoom=True,
                    calibration_saved_solve=True,default_db=True,linear_toggle=True,download=True,
                    short_db_hover_live_history_zoom=True,errors=errors,writes=writes)
        (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result));browser.close()


if __name__=='__main__':main()
