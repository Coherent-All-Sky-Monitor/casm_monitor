"""Read-only check of navigation size, contrast, focus and narrow-screen layout."""
from pathlib import Path

from playwright.sync_api import sync_playwright

from check_search_browser import luminance

URL='http://127.0.0.1:8061'
OUT=Path('/home/casm/scratch/casm-observation-preview/screenshots')


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport=dict(width=1500,height=1100))
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(URL+'/sources')  # Saved PDMP gallery, no native-data render.
        page.locator('.history-entry').first.wait_for()
        for width in [1500,1024,700,390,320]:
            page.set_viewport_size(dict(width=width,height=1100))
            page.wait_for_timeout(180)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            for selector in ['.header__tabs a','.diagnostic-nav a']:
                for link in page.locator(selector).all():
                    style=link.evaluate('''e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return {
                        color:s.color,bg:s.backgroundColor,font:parseFloat(s.fontSize),weight:Number(s.fontWeight),
                        height:r.height,left:r.left,right:r.right,scroll:e.scrollWidth,width:e.clientWidth};}''')
                    assert style['font']>=15 and style['weight']>=600
                    assert style['height']>=44 and style['left']>=0 and style['right']<=width+1
                    assert style['scroll']<=style['width']+1
                    bg=style['bg'] if style['bg']!='rgba(0, 0, 0, 0)' else 'rgb(0, 0, 0)'
                    light,dark=sorted([luminance(style['color']),luminance(bg)],reverse=True)
                    assert (light+.05)/(dark+.05)>=4.5,style
            assert page.locator('.diagnostic-nav [aria-current=page]').inner_text()=='Source history'
            if width in (1500,390):
                page.locator('.diagnostic-nav').screenshot(path=str(OUT/f'navigation-{width}.png'))
        page.set_viewport_size(dict(width=1500,height=1100))
        page.get_by_role('link',name='Overview',exact=True).focus()
        page.keyboard.press('Tab')
        focused=page.locator(':focus')
        assert focused.inner_text()=='Search (T1)'
        assert focused.evaluate('e=>getComputedStyle(e).outlineWidth')=='3px'
        page.keyboard.press('Enter')
        page.wait_for_url(URL+'/search')
        assert page.locator('.diagnostic-nav [aria-current=page]').inner_text()=='Search (T1)'
        page.locator('.diagnostic-nav').screenshot(path=str(OUT/'navigation-selected-search.png'))
        assert not errors,errors
        browser.close()
    print('Navigation passed: 15–17 px labels, 44+ px targets, contrast, active state, keyboard and 320–1500 px widths')


if __name__=='__main__':
    main()
