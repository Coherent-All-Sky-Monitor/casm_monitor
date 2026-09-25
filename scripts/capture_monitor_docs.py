"""Capture real preview screenshots and a bounded resource sample, no fixtures.

Run on corr1 with the offline venv and preview service already running. No
acquisition, calibration jobs, review writes or service restarts are requested.
Output is documentation evidence, not an isolated load benchmark.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from playwright.sync_api import sync_playwright

URL = 'http://127.0.0.1:8061'
ROOT = Path(__file__).resolve().parents[1]
UNITS = ['casm-monitor-preview', 'casm-monitor-web', 'casm-monitor-collect',
         'casm-monitor-jobs', 'casm-monitor-calibration']


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def services():
    output = {}
    for unit in UNITS:
        props = dict(line.split('=', 1) for line in command('systemctl', '--user',
            'show', unit, '-p', 'MainPID,ControlGroup,MemoryMax,CPUQuotaPerSecUSec').splitlines())
        if props.get('MainPID', '0') == '0':
            continue
        output[unit] = props
    return output


def sample(units):
    output = {}
    for unit, props in units.items():
        cg = Path('/sys/fs/cgroup') / props['ControlGroup'].lstrip('/')
        mem = dict(line.split() for line in (cg/'memory.stat').read_text().splitlines())
        cpu = dict(line.split() for line in (cg/'cpu.stat').read_text().splitlines())
        status = dict(line.split(':', 1) for line in Path(f'/proc/{props["MainPID"]}/status').read_text().splitlines())
        output[unit] = dict(cpu_usec=int(cpu['usage_usec']),
            cgroup_bytes=int((cg/'memory.current').read_text()),
            anon_bytes=int(mem['anon']), file_bytes=int(mem['file']),
            rss_bytes=int(status['VmRSS'].split()[0])*1024,
            swap_bytes=int(status.get('VmSwap','0').split()[0])*1024)
    return output


def screenshots_only(output):
    """Capture current public-facing views without sampling host resources."""
    report = dict(captured_at_utc=datetime.now(timezone.utc).isoformat(),
                  git_head=command('git', '-C', str(ROOT), 'rev-parse', 'HEAD'),
                  method='Real saved data; no fixtures, acquisition or Calibration actions.',
                  screenshots=[])
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                   args=['--disable-dev-shm-usage', '--disable-gpu'])
        page = browser.new_page(viewport=dict(width=1500, height=1100))
        errors, denied = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))

        def guard(route):
            request = route.request
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.url.split('?')[0] != URL+'/api/science/render':
                denied.append(request.url)
                route.abort()
            else:
                route.continue_()

        page.route('**/*', guard)

        def shot(name, selector, full=True):
            page.locator(selector).first.wait_for(timeout=60000)
            page.evaluate("document.querySelectorAll('img').forEach(i=>i.loading='eager')")
            page.wait_for_function("""selector => {
                const nodes = [...document.querySelectorAll(selector)];
                return nodes.length && nodes.every(e => e.tagName !== 'IMG' || (e.complete && e.naturalWidth > 0));
            }""", arg=selector, timeout=60000)
            page.locator(selector).evaluate_all("""async nodes => {
                await Promise.all(nodes.filter(e => e.tagName === 'IMG').map(e => e.decode()));
            }""")
            page.evaluate('document.fonts.ready')
            page.evaluate('window.scrollTo(0, 0)')
            page.evaluate('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
            assert not errors, errors
            assert not denied, denied
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            dest = output/f'{name}.png'
            page.screenshot(path=str(dest), full_page=full, animations='disabled', timeout=20000)
            report['screenshots'].append(dict(file=dest.name, route=page.url.removeprefix(URL),
                captured_at_utc=datetime.now(timezone.utc).isoformat(),
                sha256=hashlib.sha256(dest.read_bytes()).hexdigest(),
                viewport=page.viewport_size, full_page=full))
            (output/'manifest.json').write_text(json.dumps(report, indent=2)+'\n')
            print('Captured', name, flush=True)

        page.goto(URL+'/observation', wait_until='domcontentloaded')
        page.locator('.overview-image img').first.wait_for(timeout=60000)
        shot('overview', '.overview-photo img, .overview-image img')
        page.goto(URL+'/vis', wait_until='domcontentloaded')
        page.locator('.array-results[aria-busy=false]').wait_for(timeout=60000)
        shot('visibilities-autos', '.antenna-plot svg')
        page.get_by_role('button', name='Cross-correlations', exact=True).click()
        page.wait_for_function("document.querySelector('.array-results[aria-busy=false] .array-results-heading')?.textContent.includes('Baselines to ')", timeout=60000)
        shot('visibilities-crosses', '.antenna-plot .array-dynamic > img')
        page.goto(URL+'/search', wait_until='domcontentloaded')
        shot('search', '.plot-surface img')
        page.goto(URL+'/snaps', wait_until='domcontentloaded')
        page.locator('.snap-health-badge').first.wait_for(timeout=60000)
        shot('snaps', '.snap-spectrum-card svg')
        page.goto(URL+'/cands', wait_until='domcontentloaded')
        page.get_by_label('Plots in grid', exact=True).select_option('6')
        shot('candidates', '.candidate-image img')
        page.goto(URL+'/sources', wait_until='domcontentloaded')
        page.set_viewport_size(dict(width=1500, height=1800))
        shot('sources-b0329', '.history-entry > .history-image img', full=False)
        page.set_viewport_size(dict(width=1500, height=1100))
        page.get_by_role('button', name='Sun', exact=True).click()
        page.locator('.transit-entry .beam-light-curve').first.wait_for(timeout=60000)
        page.wait_for_function('!document.querySelector(".transit-entry [role=status]")', timeout=60000)
        shot('sources-sun', '.transit-entry .array-dynamic > img')
        browser.close()
    print('Saved screenshot manifest:', output/'manifest.json', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--screenshots-only', action='store_true',
                        help='Capture current dashboard views without host-resource measurements or Calibration actions.')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.screenshots_only:
        screenshots_only(args.output)
        return
    units = services()
    report = dict(captured_at_utc=datetime.now(timezone.utc).isoformat(),
        base_url=URL, git_head=command('git','-C',str(ROOT),'rev-parse','HEAD'),
        working_tree=command('git','-C',str(ROOT),'status','--short'),
        logical_cpus=os.cpu_count(), services=units, phases=[], screenshots=[],
        method='Live shared preview, not isolated; CPU percent is one core=100%. '
               'Memory sampled every second. Browser is separate from web cgroup. No mocked responses.')

    def phase(name, action):
        before=sample(units); start=time.monotonic(); observations=[before]
        done=threading.Event()
        def poll():
            while not done.wait(1):
                observations.append(sample(units))
        thread=threading.Thread(target=poll); thread.start()
        try:
            action()
        finally:
            done.set(); thread.join()
        duration=time.monotonic()-start; after=sample(units); observations.append(after)
        record=dict(name=name, wall_seconds=duration, services={})
        for unit in units:
            record['services'][unit]=dict(before=before[unit], after=after[unit],
                mean_cpu_percent=(after[unit]['cpu_usec']-before[unit]['cpu_usec'])/duration/1e4,
                sampled_peak_cgroup_bytes=max(s[unit]['cgroup_bytes'] for s in observations),
                sampled_peak_rss_bytes=max(s[unit]['rss_bytes'] for s in observations))
        report['phases'].append(record)
        (args.output/'measurements.json').write_text(json.dumps(report,indent=2)+'\n')
        print(name, json.dumps(record['services']['casm-monitor-preview']), flush=True)

    phase('20s without this audit browser (other clients may be active)',lambda:threading.Event().wait(20))
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport=dict(width=1500,height=1100),device_scale_factor=1)
        errors=[]; denied=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        def guard(route):
            request=route.request
            if request.method not in ('GET','HEAD','OPTIONS') and request.url not in (
                URL+'/api/science/render', URL+'/api/snap-workspace/render'):
                denied.append(request.url);route.abort()
            else:
                route.continue_()
        page.route('**/*',guard)
        def shot(name,full=False):
            page.evaluate('document.fonts.ready')
            page.evaluate('window.scrollTo(0,0)')
            page.wait_for_timeout(150)
            assert not errors, errors
            assert not denied, denied
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1')
            dest=args.output/f'{name}.png'
            page.screenshot(path=str(dest),full_page=full)
            report['screenshots'].append(dict(file=dest.name,url=page.url,
                captured_at_utc=datetime.now(timezone.utc).isoformat(),
                sha256=hashlib.sha256(dest.read_bytes()).hexdigest(),
                viewport=dict(width=1500,height=1100),full_page=full))
        def overview():
            page.goto(URL+'/observation')
            page.wait_for_function('document.querySelectorAll(".overview-image img").length===2 && [...document.querySelectorAll(".overview-image img")].every(i=>i.complete&&i.naturalWidth>0)',timeout=120000)
            shot('overview',True)
        phase('Overview rolling 24h',overview)
        def vis():
            page.goto(URL+'/vis')
            page.wait_for_function('document.querySelectorAll(".antenna-plot").length===17',timeout=120000)
            shot('visibilities-autos',True)
            page.get_by_role('button',name='Cross-correlations',exact=True).click()
            page.wait_for_function('document.querySelectorAll(".antenna-plot .array-dynamic > img").length>=16 && [...document.querySelectorAll(".antenna-plot .array-dynamic > img")].every(i=>i.complete&&i.naturalWidth>0)',timeout=120000)
            shot('visibilities-crosses',True)
        phase('Visibilities autos then cross dynamic spectra',vis)
        def search():
            page.goto(URL+'/search')
            page.wait_for_function('document.querySelector(".plot-surface img")?.naturalWidth>0',timeout=120000)
            shot('search',True)
        phase('Search T1 rolling 24h',search)
        def history():
            page.goto(URL+'/sources')
            page.locator('.history-entry').first.wait_for()
            page.wait_for_function('[...document.querySelectorAll(".history-entry > .history-image img")].slice(0,2).every(i=>i.complete&&i.naturalWidth>0)')
            shot('sources-b0329')
            for name in ['Sun','Cyg A','Cas A','Tau A']:
                page.get_by_role('button',name=name,exact=True).click()
                page.locator('.transit-entry .beam-light-curve').first.wait_for(timeout=120000)
                page.wait_for_function('!document.querySelector(".transit-entry [role=status]") && [...document.querySelectorAll(".transit-entry .array-dynamic > img")].every(i=>i.complete&&i.naturalWidth>0)',timeout=120000)
                assert page.locator('.transit-entry .beam-light-curve').count()==page.locator('.transit-entry').count()
                shot('sources-'+name.lower().replace(' ','-'),True)
        phase('Source history five selectors',history)
        browser.close()
    report['disk_usage_bytes']=command('du','-x','-B1','--max-depth=1','/home/casm/scratch/casm-observation-preview')
    report['filesystems']=command('df','-B1','/home/casm/scratch/casm-observation-preview','/mnt/nvme3','/mnt/nvme5')
    (args.output/'measurements.json').write_text(json.dumps(report,indent=2)+'\n')
    print('Saved real screenshots and measurements:',args.output,flush=True)


if __name__=='__main__':
    main()
