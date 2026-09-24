"""Read-only browser acceptance for denser, nonoverlapping visibility axes."""
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT=Path('/home/casm/scratch/casm-observation-preview/screenshots')


def no_overlap(locator, direction):
    boxes=locator.evaluate_all('(els)=>els.map(e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,text:e.textContent};})')
    boxes.sort(key=lambda b:b[direction])
    extent='width' if direction=='x' else 'height'
    for a,b in zip(boxes,boxes[1:]):
        assert a[direction]+a[extent]+1<=b[direction],(a,b)
    return len(boxes)


def check_ticks(page):
    for panel in page.locator('.antenna-plot').all():
        for selector,direction in [('.spectrum-x-tick','x'),('.spectrum-y-tick','y'),('.dynamic-frequency > span','y'),('.dynamic-time > span','x')]:
            no_overlap(panel.locator(selector),direction)


def main():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1600,'height':1100})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto('http://127.0.0.1:8061/vis',wait_until='domcontentloaded')
        page.locator('.antenna-plot').first.wait_for(timeout=90000)
        page.wait_for_function('document.querySelector(".array-results").getAttribute("aria-busy")==="false"')
        for width in [1600,390]:
            page.set_viewport_size({'width':width,'height':1100})
            for arrangement in ['Compact panels','Station grid']:
                page.get_by_role('button',name=arrangement,exact=True).click()
                for view in ['Spectrum','Dynamic spectrum']:
                    page.get_by_role('button',name=view,exact=True).click()
                    page.wait_for_timeout(150)  # ResizeObserver settles after layout/viewport changes.
                    check_ticks(page)
                    if view=='Spectrum': assert page.locator('.antenna-plot').first.locator('.spectrum-x-tick').count()>=7
                    else:
                        count=page.locator('.antenna-plot').first.locator('.dynamic-frequency > span').count()
                        assert count>=8,(width,arrangement,count,page.locator('.array-dynamic > img').first.bounding_box())
            assert page.evaluate('document.documentElement.scrollWidth-window.innerWidth')<=1
        page.set_viewport_size({'width':1600,'height':1100})
        page.get_by_role('button',name='Compact panels',exact=True).click()
        page.locator('.plot-zoom-trigger').first.click()
        page.wait_for_timeout(150)
        dialog=page.get_by_role('dialog')
        assert no_overlap(dialog.locator('.dynamic-frequency > span'),'y')>=17
        assert no_overlap(dialog.locator('.dynamic-time > span'),'x')>=8
        page.screenshot(path=str(OUT/'array-dense-ticks-expanded.png'))
        page.keyboard.press('Escape')
        page.locator('.antenna-plot').first.screenshot(path=str(OUT/'array-dense-ticks.png'))
        page.get_by_role('button',name='Spectrum',exact=True).click()
        for q in ['Phase','Real(V)','Imag(V)','|V|']:
            page.get_by_role('button',name=q,exact=True).click();check_ticks(page)
        page.locator('.antenna-plot').first.screenshot(path=str(OUT/'array-spectrum-ticks.png'))
        page.get_by_text('Frequency range & evidence',exact=True).click()
        page.get_by_label('Min MHz',exact=True).fill('412')
        page.get_by_label('Max MHz',exact=True).fill('414')
        with page.expect_response(lambda r:'/api/science/array?' in r.url):
            page.get_by_role('button',name='Apply band',exact=True).click()
        page.wait_for_function('document.querySelector(".array-results").getAttribute("aria-busy")==="false"')
        check_ticks(page)
        page.get_by_role('button',name='Dynamic spectrum',exact=True).click()
        page.wait_for_timeout(150);check_ticks(page)
        assert not errors,errors
        browser.close()
    print('Passed: round, nonoverlapping x/y ticks on compact, station-grid, mobile, enlarged and narrow-band views')


if __name__=='__main__':main()
