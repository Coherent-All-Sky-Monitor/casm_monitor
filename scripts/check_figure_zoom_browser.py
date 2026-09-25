"""Read-only preview zoom checks; non-render POSTs are blocked."""
import json
from playwright.sync_api import sync_playwright

URL = 'http://127.0.0.1:8061'


def main():
    errors, forbidden = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport=dict(width=1500,height=1000))
        page.on('pageerror',lambda e:errors.append(str(e)))
        def guard(route):
            if route.request.method!='GET' and not route.request.url.split('?')[0].endswith('/api/science/render'):
                forbidden.append(route.request.url)
                route.abort()
            else:
                route.continue_()
        page.route('**/api/**',guard)
        page.goto(URL+'/observation',wait_until='domcontentloaded')
        trigger=page.get_by_role('button',name='Enlarge injection recovery plot',exact=True)
        trigger.wait_for(timeout=60000)
        assert page.get_by_text('View trial history',exact=False).count()==0
        style=trigger.evaluate('e=>{let s=getComputedStyle(e);return [s.backgroundColor,s.borderTopWidth,s.paddingTop,s.paddingLeft]}')
        assert style==['rgb(255, 255, 255)','0px','0px','0px'],style
        trigger.locator('svg').click(position=dict(x=120,y=15))
        dialog=page.get_by_role('dialog',name='Injection recovery timeline',exact=True)
        dialog.wait_for()
        dialog.get_by_role('button',name='Zoom in',exact=True).click()
        assert dialog.get_by_label('Display zoom').inner_text()=='150%'
        dialog.get_by_role('button',name='Fit',exact=True).click()
        assert dialog.get_by_label('Display zoom').inner_text()=='100%'
        if dialog.locator('[role=button]').count():
            dialog.locator('[role=button]').first.focus()
            page.keyboard.press('Enter')
            page.get_by_role('dialog').locator('.injection-detail').wait_for()
        page.keyboard.press('Escape')
        page.goto(URL+'/cands?section=stats',wait_until='domcontentloaded')
        trigger=page.get_by_role('button',name='Enlarge T1 to T2 funnel',exact=True)
        trigger.wait_for()
        trigger.click()
        dialog=page.get_by_role('dialog',name='T1 to T2 funnel',exact=True)
        dialog.get_by_role('button',name='Zoom in',exact=True).click()
        assert dialog.get_by_label('Display zoom').inner_text()=='150%'
        page.keyboard.press('Escape')
        assert trigger.evaluate('e=>e===document.activeElement')
        assert not errors,errors
        assert not forbidden,forbidden
        browser.close()
    print(json.dumps(dict(injection='white borderless trigger; zoom, Fit and trial detail passed',
                         candidates='shared image zoom and focus return passed',errors=errors,forbidden=forbidden)))


if __name__=='__main__':
    main()
