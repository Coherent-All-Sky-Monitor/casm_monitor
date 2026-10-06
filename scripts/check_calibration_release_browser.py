"""Read-only page checks plus browser-only export/review fixtures; block real POSTs."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright
from casm_monitor.config import load_settings
from check_calibration_dry_run import protected_files


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dom-only',action='store_true',help='Check rendered DOM and controls when screenshot rendering is unavailable')
    args=parser.parse_args()
    base = 'http://127.0.0.1:8061'
    tag = 'sun_20261004_181133_regular_b65ba0ba'
    build_url = f'/api/calibration/builds/{tag}'
    deploy_url = f'/api/calibration/deployment/builds/{tag}'
    output = Path('/home/casm/scratch/casm-observation-preview/screenshots/calibration-release-20261005')
    output.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    before = protected_files(settings)
    errors, blocked, fixture_calls = [], [], []
    fixture = dict(enabled=False, exported=False, staged=False, reviewed=False, armed=False)
    item = dict(export_id='a'*32, manifest_sha256='b'*64, created_utc='2026-10-05T22:00:00Z',
                directory=f'/tmp/browser-fixture-only/20261004/{tag}__20261005T220000Z_'+'a'*32,
                files=[dict(role=role, relative=f'{role}/{name}', sha256='c'*64) for role,name in
                       [('CB','weights_fixture.h5'),('IB','ib_fixture.h5'),('cal_h5','cal_fixture.h5')]])
    stage = dict(checks_ok=True, export_id=item['export_id'], export_manifest_sha256=item['manifest_sha256'],
                 digest='d'*64, stage_dir='/tmp/browser-fixture-only/stage', checks=[dict(name='paired_CB_IB',ok=True)])
    review = dict(accepted=True, export_id=item['export_id'], stage_digest=stage['digest'],
                  at='2026-10-05T22:10:00Z', note='Browser-only review fixture')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width':1600,'height':1100}, reduced_motion='reduce')
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        actual_detail = page.request.get(base+build_url).json()
        status = page.request.get(base+'/api/calibration/deployment/status').json()
        assert status['allow_upload'] is False
        def guard(route):
            path = urlparse(route.request.url).path
            if route.request.method not in ('GET','HEAD'):
                if fixture['enabled'] and path in (build_url+'/export',deploy_url+'/stage',build_url+'/review',deploy_url+'/upload'):
                    body = route.request.post_data_json
                    fixture_calls.append(dict(path=path,body=body))
                    if path.endswith('/export'):
                        fixture['exported']=True
                    elif path.endswith('/stage'):
                        assert body['export_id']==item['export_id'] and not body['save_defaults']
                        fixture['staged']=True
                    elif path.endswith('/review'):
                        assert body['export_id']==item['export_id'] and body['stage_digest']==stage['digest']
                        fixture['reviewed']=True
                    elif path.endswith('/upload'):
                        assert body['confirm_tag']==tag and not body['save_defaults']
                        assert route.request.headers.get('x-csrf-token')
                    route.fulfill(json={})
                else:
                    blocked.append(path)
                    route.abort()
            elif fixture['enabled'] and path==build_url:
                route.fulfill(json={**actual_detail,'jobs':[], 'export':item if fixture['exported'] else None,
                                    'review':review if fixture['reviewed'] else {}})
            elif fixture['enabled'] and path==deploy_url:
                route.fulfill(json={'stage':stage if fixture['staged'] else None,'uploads':[]})
            elif fixture['enabled'] and path=='/api/calibration/deployment/status':
                route.fulfill(json={**status,'allow_upload':fixture['armed'],'casm_track_running':False})
            else:
                route.continue_()
        context.route('**/api/**',guard)
        def capture(name):
            if not args.dom_only:
                page.screenshot(path=str(output/name),animations='disabled',timeout=15000)
        def click(locator):
            expect(locator).to_be_enabled(timeout=60000)
            if args.dom_only:
                locator.evaluate('element=>element.click()')
            else:
                locator.click()
        page.goto(base+'/cal',wait_until='domcontentloaded')
        cards=page.get_by_role('region',name='Current deployed calibration')
        expect(cards).to_contain_text('Sep 17, 2026',timeout=60000)
        expect(cards).to_contain_text('Calibrator · Sun')
        expect(cards).to_contain_text('12:17 – 13:17 PDT')
        expect(cards).to_contain_text('Sep 18, 2026')
        expect(cards).to_contain_text('00:51 PDT')
        cards.evaluate("e=>e.scrollIntoView({block:'center',behavior:'instant'})")
        capture('current-calibration.png')
        click(cards.get_by_role('button',name='UTC',exact=True))
        expect(cards).to_contain_text('19:17 – 20:17 UTC')
        expect(cards).to_contain_text('07:51 UTC')
        click(cards.get_by_role('button',name='OVRO local'))
        for width in (1280,768,390):
            page.set_viewport_size({'width':width,'height':1100})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
        cards.evaluate("e=>e.scrollIntoView({block:'center',behavior:'instant'})")
        capture('current-calibration-mobile.png')
        page.set_viewport_size({'width':1600,'height':1100})
        page.goto(base+f'/cal/{tag}',wait_until='domcontentloaded')
        click(page.locator('.cal-steps').get_by_role('button',name='Export & deploy',exact=False))
        release=page.locator('.cal-release')
        expect(release.get_by_role('button',name='Write CB & IB to disk')).to_be_enabled(timeout=60000)
        expect(release.get_by_role('button',name='Upload reviewed CB & IB')).to_be_disabled()
        expect(release).to_contain_text('Live upload remains disabled')
        release.evaluate("e=>e.scrollIntoView({block:'start',behavior:'instant'})")
        capture('export-page.png')
        # From here, every release mutation is fulfilled in browser memory.
        fixture['enabled']=True
        page.reload(wait_until='domcontentloaded')
        click(page.locator('.cal-steps').get_by_role('button',name='Export & deploy',exact=False))
        expect(release.get_by_role('button',name='Run upload dry run')).to_be_disabled()
        click(release.get_by_role('button',name='Write CB & IB to disk'))
        expect(release).to_contain_text(item['directory'],timeout=15000)
        click(release.get_by_role('button',name='Run upload dry run'))
        expect(release).to_contain_text('Passed for this export.',timeout=15000)
        release.get_by_label('Review note').fill('Browser-only review fixture')
        click(release.get_by_role('checkbox',name='I vetted this exported'))
        click(release.get_by_role('button',name='Save review',exact=True))
        expect(release).to_contain_text('Approved')
        release.get_by_label('Type the build ID').fill(tag)
        expect(release.get_by_role('button',name='Upload reviewed CB & IB')).to_be_disabled()
        fixture['armed']=True  # browser-only status fixture; server remains unarmed
        expect(release.get_by_role('button',name='Upload reviewed CB & IB')).to_be_enabled(timeout=20000)
        page.once('dialog',lambda dialog:dialog.dismiss())
        click(release.get_by_role('button',name='Upload reviewed CB & IB'))
        assert len(fixture_calls)==3
        page.once('dialog',lambda dialog:dialog.accept())
        click(release.get_by_role('button',name='Upload reviewed CB & IB'))
        expect(page.get_by_text('Upload requested for the reviewed product.',exact=False)).to_be_visible()
        assert len(fixture_calls)==4
        release.evaluate("e=>e.scrollIntoView({block:'start',behavior:'instant'})")
        capture('export-receipt-browser-fixture.png')
        for width in (1280,768,390):
            page.set_viewport_size({'width':width,'height':1100})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
        after=protected_files(settings)
        assert before==after
        assert page.request.get(base+'/api/calibration/deployment/status').json()['allow_upload'] is False
        assert not blocked and not errors,(blocked,errors)
        report=dict(current_calibration_local_utc=True,mobile_no_overflow=True,screenshots_captured=not args.dom_only,
                    simulated_export_review_upload=True,production_upload_disabled=True,
                    protected_files_unchanged=True,protected_files=before,fixture_calls=fixture_calls,
                    real_non_get_requests=blocked,browser_errors=errors)
        (output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('protected_files','fixture_calls')}))
        browser.close()


if __name__=='__main__':
    main()
