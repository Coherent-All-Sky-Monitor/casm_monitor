"""Isolated calibration workspace; durable jobs, immutable recipes, no live defaults."""
from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..cal_defaults import cal_defaults, deployed_product, layout_info, sha256_file
from ..jobs.cal_build import build_dir, validate, _window
from ..observation import read_json

KIND = 'calibration_workspace'
SOURCES = ('sun', 'cyg-a', 'cas-a')
MAX_ANALYSIS_WINDOW_S = 4 * 3600


def analysis_window(value, field):
    window, start, end = _window(value, field)
    if end - start > MAX_ANALYSIS_WINDOW_S:
        raise ValueError(f'{field} is limited to four hours')
    return window, start, end


def workspace_defaults(settings, date: str = '') -> dict:
    """Initial form uses the ledger's uploaded calibration, never latest builds.

    An explicit different date gets computed defaults. An unavailable or unbound
    report gets a labelled fallback, not dates guessed from a product filename.
    Antennas still come from populated deployed slots, not the historical layout.
    """
    from .calibration_reference import LOCAL, recipe_for, utc

    recorded = None
    reason = None
    try:
        product = deployed_product(settings)
        if not product.get('cal_file') or not product.get('weights_file'):
            raise ValueError('No uploaded calibration and weights recorded in the deployment ledger')
        calibration = Path(product['cal_file'])
        if not calibration.is_file():
            raise ValueError('The uploaded calibration file is unavailable')
        recipe, report = recipe_for(calibration, max_duration_s=MAX_ANALYSIS_WINDOW_S)
        params = recipe['params']
        if 'static_window' not in params:
            raise ValueError('The uploaded recipe does not record a static window or explicit no-static choice')
        static = params['static_window']
        if static is None and params.get('static_path'):
            raise ValueError('The uploaded recipe uses a static file without recording its time window')
        if static is not None:
            analysis_window(static, 'Recorded static window')
        recorded = dict(date=(recipe['t0'] + (recipe['t1'] - recipe['t0']) / 2).astimezone(LOCAL).date().isoformat(),
                        source_window=[recipe['t0'].isoformat(), recipe['t1'].isoformat()],
                        static_window=None if static is None else [utc(t).isoformat() for t in static],
                        report=str(report), report_sha256=sha256_file(report),
                        date_uploaded=product.get('date_deployed'))
    except (ValueError, OSError, TypeError) as exc:
        reason = str(exc)
    if not date:
        if recorded:
            date = recorded['date']
        else:
            days = sorted({path.name[:10] for path in Path(settings.vis_dir).glob('*.dat*') if re.match(r'\d{4}-\d{2}-\d{2}', path.name)})
            latest = datetime.strptime(days[-1], '%Y-%m-%d') if days else datetime.now(timezone.utc)
            date = (latest - timedelta(days=1)).strftime('%Y-%m-%d')
    defaults = cal_defaults(date, settings)
    if recorded and date == recorded['date']:
        defaults.update(source_window=recorded['source_window'], static_window=recorded['static_window'])
        t0, t1 = map(utc, recorded['source_window'])
        defaults.update(window_half_min=(t1 - t0).total_seconds() / 120,
                        window_offset_min=(t1 - t0).total_seconds() / 120,
                        window_center_offset_min=((t0 + (t1 - t0) / 2) - utc(defaults['alt_max_utc'])).total_seconds() / 60,
                        static_note='Recorded uploaded recipe; review data coverage and hardware compatibility before reuse.',
                        defaults_basis=dict(kind='uploaded_recipe', label='Windows from last uploaded weights', **recorded))
    else:
        defaults['defaults_basis'] = dict(kind='date_based' if recorded else 'fallback',
            label='Computed windows for selected date' if recorded else 'Uploaded recipe unavailable · date-based fallback',
            reason=reason)
    defaults['wired_antennas'] = layout_info(settings.layout_csv)['wired_antennas']
    return defaults


def workspace_settings(settings):
    if settings.observation_cache_root is None:
        raise ValueError('Calibration workspace requires an isolated observation root')
    base = Path(settings.observation_cache_root)
    root = base / 'calibration'
    if base.is_symlink() or root.is_symlink():
        raise ValueError('Calibration workspace root must not be a symlink')
    if Path(settings.store_root).resolve() in (root.resolve(), *root.resolve().parents):
        raise ValueError('Calibration workspace must be outside the production store')
    # This workspace is deliberately offline, even if another deployment
    # surface is armed in the parent config or environment.
    return dataclasses.replace(settings, store_root=root, allow_upload=False)


def save_json(path, value):
    import numpy as np
    def encode(item):
        if isinstance(item, np.ndarray):
            return item.tolist()
        if isinstance(item, np.generic):
            return item.item()
        return str(item)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, default=encode))
    temporary.replace(path)


def request_record(settings, tag):
    record = read_json(build_dir(settings, tag) / 'request.json')
    if not record:
        raise ValueError('Unknown calibration candidate')
    return record


def artifact_entries(settings, tag):
    root = build_dir(settings, tag)
    result = []
    for path in sorted(root.rglob('*')):
        if path.suffix not in {'.png', '.json', '.npz', '.csv', '.ipynb', '.h5'} or not path.is_file():
            continue
        if path.is_symlink() or root.resolve() not in path.resolve().parents:
            continue
        relative = str(path.relative_to(root))
        token = hashlib.sha256(relative.encode()).hexdigest()[:24]
        result.append(dict(id=token, name=path.name, relative=relative,
                           created=path.stat().st_mtime,
                           kind='image' if path.suffix == '.png' else 'download',
                           url=f'/api/calibration/builds/{tag}/artifacts/{token}'))
    return result


def review_gate(settings, tag):
    root = build_dir(settings, tag)
    summary = read_json(root / 'summary.json') or {}
    review = read_json(root / 'review.json') or {}
    if not review.get('accepted') or not summary.get('has_weights'):
        raise HTTPException(409, 'Review the solve, independent transit and exact-grid product before staging')
    if review.get('summary_sha256') != sha256_file(root / 'summary.json'):
        raise HTTPException(409, 'Product changed since review; review this revision again')


def action_params(body, record):
    action = body.get('action')
    if action not in {'inspect', 'phase', 'transit', 'history', 'grid'}:
        raise ValueError('Unknown analysis action')
    params = dict(action=action)
    if action == 'grid':
        altitude = float(body.get('alt_min', 20))
        if not math.isfinite(altitude) or not 10 <= altitude <= 60:
            raise ValueError('Minimum elevation must be 10..60 degrees')
        if body.get('replace_custom_cells') is not True:
            raise ValueError('Confirm that this is a new plain 512-beam grid, not preservation of custom cells')
        return {**params, 'alt_min': altitude, 'replace_custom_cells': True}
    source = body.get('source', 'cyg-a' if action == 'transit' else 'sun')
    if source not in SOURCES:
        raise ValueError('Source must be Sun, Cyg A or Cas A')
    params['source'] = source
    if action == 'inspect':
        window, _, _ = analysis_window(body.get('window', record['params']['source_window']), 'Inspection window')
        return {**params, 'window': window, 'static': bool(body.get('static', True))}
    if action == 'phase':
        window, start, end = analysis_window(body.get('window', record['params']['source_window']), 'Phase window')
        targets = body.get('targets', [antenna for antenna in record['params']['antennas'] if antenna != record['params']['ref_ant']][:3])
        if not isinstance(targets, list) or not 1 <= len(targets) <= 3 or any(type(ant) is not int for ant in targets):
            raise ValueError('Select one to three integer target antenna IDs')
        if len(set(targets)) != len(targets) or not set(targets) <= set(record['params']['antennas']) - {record['params']['ref_ant']}:
            raise ValueError('Targets must be distinct selected antennas other than the reference')
        return {**params, 'window': window, 'targets': targets, 'static': bool(body.get('static', True))}
    day = str(body.get('date', record['params']['source_window'][0][:10]))
    parsed = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - parsed
    if not timedelta(0) < age < timedelta(days=30):
        raise ValueError('Choose a date within the last 30 days')
    params['date'] = day
    if action == 'history':
        reference = body.get('ref_ant', record['params']['ref_ant'])
        if type(reference) is not int or reference not in record['params']['antennas']:
            raise ValueError('History reference must be a selected antenna')
        days = int(body.get('days', 3))
        if not 1 <= days <= 3:
            raise ValueError('Choose one to three days')
        _, start, end = analysis_window(record['params']['source_window'], 'History source window')
        minutes = float(body.get('minutes', (end - start) / 60))
        if not math.isfinite(minutes) or not 1 <= minutes <= MAX_ANALYSIS_WINDOW_S / 60:
            raise ValueError('History windows must be between one minute and four hours')
        return {**params, 'days': days, 'minutes': minutes, 'ref_ant': reference}
    low, high = float(body.get('fmin', 384)), float(body.get('fmax', 512))
    off = float(body.get('off_minutes', 90))
    snr = float(body.get('minimum_snr', 5))
    if not all(math.isfinite(value) for value in (low, high, off, snr)) or not 0 < low < high < 1000 or not 50 <= off <= 120 or not 3 <= snr <= 30:
        raise ValueError('Use a valid MHz band, off-source boundary 50..120 minutes and S/N 3..30')
    return {**params, 'fmin': low, 'fmax': high, 'off_minutes': off, 'minimum_snr': snr,
            'compare_reference': bool(body.get('compare_reference', True)),
            'compare_amplitudes': bool(body.get('compare_amplitudes', False))}


def build_router(settings, store):
    router = APIRouter(prefix='/api/calibration')

    @router.get('/sky-plan')
    def sky_plan(date: str):
        from .calibration_planning import sky_plan as plan
        try:
            return plan(date)
        except (ValueError, OverflowError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get('')
    def overview(date: str = ''):
        try:
            defaults = workspace_defaults(settings, date)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        builds = []
        for path in sorted(settings.cal_builds_root.glob('*/request.json'), key=lambda item: item.stat().st_mtime, reverse=True)[:100]:
            if path.parent.is_symlink():
                continue
            record = read_json(path)
            summary = read_json(path.parent / 'summary.json') or {}
            if record:
                builds.append(dict(tag=record['params']['tag'], created=record['created'],
                    antennas=record['params']['antennas'], has_cal=bool(summary.get('paths', {}).get('cal_h5')),
                    has_weights=bool(summary.get('has_weights')), imported=bool(record.get('imported'))))
        return dict(defaults=defaults, builds=builds, jobs=store.list_jobs(limit=60),
                    isolation=str(settings.store_root), upload_armed=bool(settings.allow_upload),
                    runtime_activation='Unverified — no consumer identity acknowledgement',
                    budget='One durable worker · 64 GiB process cap · 60-minute execution deadline (not data duration)')

    def queue(tag, action, params):
        identity = uuid.uuid4().hex
        job_id, refusal = store.submit_job_atomic(KIND, dict(tag=tag, action=action, action_id=identity, selection=params,
            request_sha256=sha256_file(build_dir(settings, tag) / 'request.json')), refuse_if_pending=True)
        if refusal:
            raise HTTPException(409, f'An analysis is already queued/running: {refusal}')
        return dict(tag=tag, job_id=job_id, action_id=identity)

    @router.post('/builds')
    def create(body: dict):
        return create_revision(body)

    def create_revision(body, parent=None):
        if body.get('reviewed') is not True:
            raise HTTPException(400, 'Review the selected windows, antenna membership and hardware epoch first')
        tag = str(body.get('tag', ''))
        try:
            root = build_dir(settings, tag)
            if root.exists():
                raise HTTPException(409, 'Tag already exists; use a new immutable revision')
            antennas = body.get('antennas')
            layout = layout_info(parent['layout']['snapshot'] if parent else settings.layout_csv)
            if parent and sha256_file(layout['resolved']) != parent['layout']['sha256']:
                raise ValueError('The parent layout snapshot has changed')
            if parent and parent.get('reference') and sha256_file(parent['reference']) != parent['reference_sha256']:
                raise ValueError('The parent reference calibration has changed')
            if not isinstance(antennas, list) or not 2 <= len(antennas) <= 32 or any(type(ant) is not int for ant in antennas) or len(set(antennas)) != len(antennas) or not set(antennas) <= set(layout['wired_antennas']):
                raise ValueError('Select 2..32 distinct wired antenna IDs')
            root.mkdir(parents=True)
            original = Path(layout['resolved']).read_bytes()
            snapshot = root / 'layout_snapshot.csv'
            snapshot.write_bytes(original)
            layout.update(snapshot=str(snapshot), sha256=sha256_file(snapshot))
            reader = csv.DictReader(io.StringIO(original.decode()))
            rows, fields = list(reader), reader.fieldnames
            for row in rows:
                row['include_in_beamforming'] = '1' if int(row['antenna']) in antennas else '0'
            private = root / 'selected_layout.csv'
            with private.open('w') as stream:
                output = csv.DictWriter(stream, fields)
                output.writeheader()
                output.writerows(rows)
            reference = parent.get('reference') if parent else deployed_product(settings).get('cal_file')
            params = validate({**body, 'prev_cal_path': reference}, dataclasses.replace(settings, snap_layout_csv=private))
            for key in ('source_window', 'static_window'):
                if params[key]:
                    analysis_window(params[key], key)
            record = dict(params=params, layout=layout, selected_layout_sha256=sha256_file(private),
                          created=datetime.now(timezone.utc).isoformat(), reference=reference,
                          reference_sha256=sha256_file(reference) if reference else None,
                          hardware_epoch_note=str(body.get('hardware_epoch_note', ''))[:2000])
            if parent:
                record['parent_tag'] = parent['params']['tag']
            save_json(root / 'request.json', record)
            return queue(tag, 'solve', {})
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post('/builds/{tag}/trial')
    def trial(tag: str, body: dict):
        try:
            parent = request_record(settings, tag)
            if parent.get('imported'):
                raise ValueError('Prepare a new solve to change membership; an imported reference has no frozen solve recipe')
            values = {**parent['params'], 'tag': str(body.get('tag', '')),
                      'antennas': body.get('antennas'), 'ref_ant': body.get('ref_ant'),
                      'reviewed': body.get('reviewed') is True,
                      'hardware_epoch_note': parent.get('hardware_epoch_note', '')}
            return create_revision(values, parent)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post('/import-reference')
    def import_reference(body: dict):
        from bf_weights_generator import load_calibration_weights
        date = str(body.get('date', datetime.now(timezone.utc).date().isoformat()))
        try:
            datetime.strptime(date, '%Y-%m-%d')
            defaults = cal_defaults(date, settings)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        deployed = deployed_product(settings)
        cal_path = deployed.get('cal_file')
        if not cal_path or not Path(cal_path).is_file():
            raise HTTPException(409, 'No readable ledger-recorded calibration')
        cal = load_calibration_weights(cal_path)
        tag = 'recorded-' + sha256_file(cal_path)[:12]
        root = build_dir(settings, tag)
        if not root.exists():
            root.mkdir(parents=True)
            antennas = list(map(int, cal.ant_ids))
            params = dict(tag=tag, source='sun', source_window=defaults['source_window'], static_window=None,
                          antennas=antennas, ref_ant=9 if 9 in antennas else antennas[0])
            record = dict(params=params, created=datetime.now(timezone.utc).isoformat(), imported=True,
                          reference=cal_path, reference_sha256=sha256_file(cal_path),
                          original_product=deployed, hardware_epoch_note='Imported reference: use dated layout for diagnostic reads; not a new solve or deployable build.')
            save_json(root / 'request.json', record)
            save_json(root / 'summary.json', dict(tag=tag, paths={'cal_h5': cal_path}, has_weights=False, imported=True))
        return dict(tag=tag)

    @router.get('/builds/{tag}')
    def detail(tag: str):
        try:
            record = request_record(settings, tag)
            root = build_dir(settings, tag)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        actions = []
        for path in sorted(root.glob('actions/*/result.json')):
            actions.append(read_json(path))
        actions.sort(key=lambda item: item.get('finished', ''))
        jobs = [job for job in store.list_jobs(limit=300) if job['params'].get('tag', job['params'].get('build_tag')) == tag]
        return dict(request=record, summary=read_json(root / 'summary.json'), actions=actions,
                    review=read_json(root / 'review.json'), artifacts=artifact_entries(settings, tag), jobs=jobs)

    @router.get('/builds/{tag}/sky')
    def sky(tag: str, date: str):
        from .calibration_sky import sky_view
        try:
            return sky_view(settings, tag, date)
        except (ValueError, OSError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get('/builds/{tag}/inspection')
    def inspection(tag: str, reference: int | None = None, stage: str = 'svd',
                   quantity: str = 'phase', scheme: str = 'uniform', target: int | None = None):
        from .calibration_inspection import snapshot
        try:
            request_record(settings, tag)
            return snapshot(build_dir(settings, tag), reference, stage, quantity, scheme, target)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get('/sky/{filename}')
    def sky_image(filename: str):
        if not re.fullmatch(r'[0-9a-f]{24}\.png', filename):
            raise HTTPException(404, 'Unknown sky view')
        path = Path(settings.store_root) / 'sky_views' / filename
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, 'Unknown sky view')
        return FileResponse(path, media_type='image/png', headers={'Cache-Control': 'private, max-age=86400'})

    @router.post('/builds/{tag}/actions')
    def action(tag: str, body: dict):
        try:
            record = request_record(settings, tag)
            params = action_params(body, record)
            summary = read_json(build_dir(settings, tag) / 'summary.json') or {}
            if not summary.get('paths', {}).get('cal_h5'):
                raise ValueError('A completed calibration is required first')
            if params['action'] == 'grid' and (record.get('imported') or summary.get('has_weights')):
                raise ValueError('Generate a new grid only once per newly solved revision')
            return queue(tag, params['action'], params)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post('/builds/{tag}/review')
    def review(tag: str, body: dict):
        root = build_dir(settings, tag)
        summary = read_json(root / 'summary.json') or {}
        actions = [read_json(path) or {} for path in root.glob('actions/*/result.json')]
        if body.get('accepted') is not True or not str(body.get('note', '')).strip():
            raise HTTPException(400, 'Explicit review acceptance and a note are required')
        independent = [action for action in actions if action.get('action') == 'transit' and action.get('selection', {}).get('source') != 'sun']
        if not summary.get('has_weights') or not independent or not summary.get('notebook_executed'):
            raise HTTPException(409, 'Need completed diagnostics, independent-source transit and matched CB/IB product before review')
        failures = summary.get('numbers', {}).get('diagnostic_failures') or summary.get('numbers', {}).get('diagnostics_error')
        if failures:
            raise HTTPException(409, f'Required diagnostics reported failures: {failures}')
        record = dict(accepted=True, note=str(body['note'])[:4000], at=datetime.now(timezone.utc).isoformat(), summary_sha256=sha256_file(root / 'summary.json'))
        save_json(root / 'review.json', record)
        return record

    @router.get('/builds/{tag}/artifacts/{token}')
    def artifact(tag: str, token: str):
        for item in artifact_entries(settings, tag):
            if item['id'] == token:
                return FileResponse(build_dir(settings, tag) / item['relative'], filename=item['name'],
                    content_disposition_type='inline' if item['kind'] == 'image' else 'attachment')
        raise HTTPException(404, 'Unknown artifact')

    @router.get('/jobs/{identity}/log')
    def log(identity: int):
        job = store.get_job(identity)
        if not job:
            raise HTTPException(404, 'Unknown job')
        path = settings.jobs_root / str(identity) / 'log.txt'
        if not path.is_file():
            return {'text': 'Waiting for worker…'}
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 48000))
            return {'text': stream.read().decode(errors='replace')}

    @router.post('/jobs/{identity}/cancel')
    def cancel(identity: int, body: dict):
        if not store.get_job(identity):
            raise HTTPException(404, 'Unknown job')
        return dict(state=store.cancel_job(identity))

    return router
