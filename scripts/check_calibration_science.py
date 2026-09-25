"""Run bounded real-data diagnostics through the isolated queue; no deployment."""
import argparse
import json
import time
import urllib.request

BASE = 'http://127.0.0.1:8061'


def request(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {'Content-Type': 'application/json', 'Origin': BASE, 'X-CASM-Workspace': '1'}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(BASE + path, data=data, headers=headers), timeout=30) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default='2026-09-18')
    parser.add_argument('--solve', action='store_true')
    parser.add_argument('--candidate')
    parser.add_argument('--action', choices=['phase', 'transit', 'grid', 'history'])
    args = parser.parse_args()
    if args.solve:
        defaults = request('/api/calibration?date=' + args.date)['defaults']
        body = {**defaults, 'tag': 'workspace_acceptance_' + args.date.replace('-', '') + '_' + str(int(time.time())),
                'reviewed': True, 'hardware_epoch_note': 'Offline implementation acceptance run; not scientific approval or a live deployment.'}
        created = request('/api/calibration/builds', body)
        actions = [created]
    else:
        tag = args.candidate or request('/api/calibration/import-reference', {'date': args.date})['tag']
        actions = [dict(tag=tag, selection=dict(action='phase', source='sun',
            window=[args.date + ' 19:17:00', args.date + ' 20:17:00'], targets=[10, 15, 19], static=bool(args.candidate))),
            dict(tag=tag, selection=dict(action='transit', source='cyg-a', date=args.date,
                fmin=384, fmax=512, off_minutes=90, minimum_snr=5, compare_reference=bool(args.candidate)))]
        if args.action in {'phase', 'transit'}:
            actions = [action for action in actions if action['selection']['action'] == args.action]
            if args.action == 'transit' and args.candidate:
                actions[0]['selection']['compare_reference'] = True
        elif args.action == 'grid':
            actions = [dict(tag=tag, selection=dict(action='grid', alt_min=20, replace_custom_cells=True))]
        elif args.action == 'history':
            actions = [dict(tag=tag, selection=dict(action='history', date=args.date, days=2))]
    for action in actions:
        tag = action['tag']
        submitted = action if args.solve else request(f'/api/calibration/builds/{tag}/actions', action['selection'])
        print(json.dumps(submitted), flush=True)
        deadline = time.monotonic() + (3500 if args.solve else 900)
        previous = None
        while time.monotonic() < deadline:
            result = request(f'/api/calibration/builds/{tag}')
            job = next(item for item in result['jobs'] if item['id'] == submitted['job_id'])
            if job['state'] != previous:
                print(f"Job {job['id']}: {job['state']}", flush=True)
                previous = job['state']
            if job['state'] in {'done', 'failed', 'cancelled'}:
                print(json.dumps(job.get('result'), default=str), flush=True)
                if job['state'] != 'done':
                    print(request(f"/api/calibration/jobs/{job['id']}/log")['text'], flush=True)
                    raise RuntimeError('Scientific acceptance job failed')
                break
            time.sleep(3)
        else:
            raise TimeoutError('Acceptance wait deadline reached; inspect saved worker job')
    print('Real-data acceptance completed; nothing uploaded.', flush=True)


if __name__ == '__main__':
    main()
