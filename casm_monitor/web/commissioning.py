"""Isolated, explicitly requested canonical calibration builds. No deployment path."""
from __future__ import annotations

import csv
import dataclasses
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ..cal_defaults import cal_defaults, deployed_product, layout_info
from ..jobs.cal_build import ParamError, validate
from ..observation import cache_dir, read_json

PYTHON = '/home/casm/software/dev/casm_venvs/casm_offline_env/bin/python'
LIMITS = dict(wall_seconds=1800, cpu_seconds=1800, address_space_bytes=24 * 1024**3,
              max_file_bytes=2 * 1024**3, minimum_free_bytes=10 * 1024**3)
_lock = threading.Lock()


def local_root(settings) -> Path:
    if settings.observation_cache_root is None:
        raise HTTPException(503, 'An explicitly configured isolated observation root is required')
    root = cache_dir(settings) / 'commissioning'
    if root.is_symlink():
        raise HTTPException(503, 'Commissioning output must not be a symlink')
    root = root.resolve()
    if root == Path(settings.store_root).resolve() or Path(settings.store_root).resolve() in root.parents:
        raise HTTPException(503, 'Commissioning output must be outside the production store')
    return root


def build_path(settings, identity: str) -> Path:
    if not re.fullmatch(r'[0-9a-f]{32}', identity):
        raise HTTPException(404, 'Unknown build')
    root = local_root(settings)
    path = root / identity
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise HTTPException(404, 'Unknown build')
    return path


def save(path: Path, record: dict):
    temporary = path / 'state.tmp'
    temporary.write_text(json.dumps(record, indent=2))
    temporary.replace(path / 'state.json')


def artifacts(path: Path) -> list[dict]:
    result = []
    candidates = list(path.glob('report_*.json')) + list(path.glob('cal_*_diagnostics.ipynb'))
    candidates += list((path / 'figs').glob('*.png'))
    candidates += list((path / 'figs').glob('fringe_*/*.png'))
    for item in sorted(candidates)[:150]:
        if item.is_symlink() or not item.is_file() or path.resolve() not in item.resolve().parents:
            continue
        relative = str(item.relative_to(path))
        token = hashlib.sha256(relative.encode()).hexdigest()[:20]
        result.append(dict(id=token, name=item.name, path=relative,
                           url=f'/api/commissioning/{path.name}/artifacts/{token}',
                           kind='image' if item.suffix == '.png' else 'notebook' if item.suffix == '.ipynb' else 'report'))
    return result


def build_view(path: Path) -> dict:
    record = read_json(path / 'state.json')
    if not record:
        raise HTTPException(404, 'Unknown build')
    if record.get('state') == 'running':
        owner_alive = False
        try:
            owner = int(record.get('supervisor_pid', 0))
            if owner > 0:
                os.kill(owner, 0)
                owner_alive = True
        except (ValueError, OSError):
            pass
        if not owner_alive:
            record.update(state='interrupted', recorded_state='running',
                          warning='Web supervision ended. The builder may still hold its process lease; inspect logs and products. Completion is unknown and is never retried automatically.')
    record['artifacts'] = artifacts(path)
    return record


def recorded_artifacts(settings) -> list[dict]:
    """Existing diagnostic files adjacent to ledger products, never guessed live state."""
    deployed = deployed_product(settings)
    roots = {Path(deployed[key]).parent for key in ('weights_file', 'cal_file') if deployed.get(key)}
    result, seen = [], set()
    for root in sorted(roots):
        if not root.is_dir() or root.is_symlink():
            continue
        for item in artifacts(root):
            path = (root / item['path']).resolve()
            if path in seen:
                continue
            seen.add(path)
            token = hashlib.sha256(str(path).encode()).hexdigest()[:20]
            result.append({**item, 'id': token, 'path': str(path),
                           'url': f'/api/commissioning-recorded/artifacts/{token}',
                           'provenance': 'Adjacent to ledger-recorded product; not a fresh live-payload verification'})
    return result


def stage(settings, body: dict) -> dict:
    """Snapshot reviewed wiring/inclusion and validate using the existing admission code."""
    if body.get('reviewed') is not True:
        raise HTTPException(400, 'Review the solve window, static window and antenna selection first')
    root = local_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    layout = layout_info(settings.layout_csv)
    antennas = body.get('antennas')
    if not isinstance(antennas, list) or not antennas or any(type(a) is not int for a in antennas):
        raise HTTPException(400, 'antennas must be a non-empty list of integer IDs')
    if len(set(antennas)) != len(antennas) or not set(antennas) <= set(layout['wired_antennas']):
        raise HTTPException(400, 'Select distinct functional antennas from the current wiring snapshot')
    original = Path(layout['resolved']).read_text()
    reader = csv.DictReader(io.StringIO(original))
    rows, fields = list(reader), reader.fieldnames
    if not fields or 'include_in_beamforming' not in fields:
        raise HTTPException(400, 'Layout lacks explicit beamforming membership')
    for row in rows:
        row['include_in_beamforming'] = '1' if int(row['antenna']) in antennas else '0'
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fields)
    writer.writeheader()
    writer.writerows(rows)
    identity = uuid.uuid4().hex
    path = root / identity
    path.mkdir()
    snapshot = path / 'layout_snapshot.csv'
    snapshot.write_text(buffer.getvalue())
    # Only the isolated snapshot has reviewed inclusion changes. current/CASMAN stay untouched.
    try:
        params = validate({**body, 'prev_cal_path': None}, dataclasses.replace(settings, snap_layout_csv=snapshot))
    except (ParamError, ValueError, OSError) as exc:
        save(path, {'id': identity, 'state': 'refused', 'error': str(exc)})
        raise HTTPException(400, str(exc)) from exc
    # Bound every driver read: source/static <= 1 hour each, independent source <= 2 hours.
    for key in ('source_window', 'static_window'):
        window = params.get(key)
        if window and (datetime.fromisoformat(window[1]) - datetime.fromisoformat(window[0])).total_seconds() > 3600:
            raise HTTPException(400, f'{key} must be at most one hour for a web build')
    date = params['source_window'][0][:10]
    recipe = dict(out_dir=str(path), tag=params['tag'], cal_source='sun',
                  source_window=params['source_window'], static_window=params['static_window'],
                  antennas=params['antennas'], ref_ant=params['ref_ant'], layout_csv=str(snapshot),
                  grid_mode='exact', n_beams=512, prev_cal_path=params['prev_cal_path'],
                  diagnostics=True, notebook=True, execute_notebook=True,
                  nearest_sources=['sun'], nearest_date=date, transit_date=date,
                  beam_check=True, beam_check_source='cyg-a', beam_check_fallback=None,
                  beam_check_date=date, beam_check_minutes=60)
    config = path / 'recipe.json'
    config.write_text(json.dumps(recipe, indent=2))
    record = dict(id=identity, state='staged', created_utc=datetime.now(timezone.utc).isoformat(),
                  recipe=recipe, command=shlex.join([PYTHON, '-m', 'bf_weights_generator.make_cal_and_weights', '--config', str(config)]),
                  recipe_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
                  layout_sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                  original_layout_sha256=layout['sha256'], original_layout_path=layout['resolved'],
                  reviewed_antennas=params['antennas'], budget=LIMITS,
                  deployment_enabled=False, restart_defaults_enabled=False,
                  warnings=['Staging creates a reviewed recipe, not calibration products.',
                            'Address-space, CPU and file limits are per process/file; the wall deadline covers the process group. This is not an aggregate cgroup memory limit.',
                            'A build makes CB weights and diagnostics only; the required paired IB product and deployment review remain separate.',
                            'Higher rank-1 is not proof of improved beam sensitivity. Review independent Cyg A diagnostics and any SKIPPED sections.',
                            'The exact all-sky grid is regenerated; this does not preserve custom B0329 factorial cells in an existing product.'])
    save(path, record)
    return build_view(path)


def execute(path: Path, lease=None):
    """Run only the canonical builder, with local output and enforced process limits."""
    record = read_json(path / 'state.json')
    try:
        for name in ('.runtime', '.tmp', '.ipython', '.mpl', '.cache'):
            (path / name).mkdir(exist_ok=True)
        with (path / 'build.log').open('wb') as log:
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'MPLBACKEND': 'Agg',
                   'OPENBLAS_NUM_THREADS': '2', 'OMP_NUM_THREADS': '2',
                   'MPLCONFIGDIR': str(path / '.mpl'), 'XDG_CACHE_HOME': str(path / '.cache'),
                   'JUPYTER_RUNTIME_DIR': str(path / '.runtime'), 'IPYTHONDIR': str(path / '.ipython'),
                   'TMPDIR': str(path / '.tmp')}
            command = ['/usr/bin/prlimit', f"--as={LIMITS['address_space_bytes']}",
                       f"--cpu={LIMITS['cpu_seconds']}", f"--fsize={LIMITS['max_file_bytes']}", '--',
                       PYTHON, '-m', 'bf_weights_generator.make_cal_and_weights', '--config', str(path / 'recipe.json')]
            # timeout terminates the process group, including notebook kernels.
            command = ['/usr/bin/timeout', '--signal=TERM', '--kill-after=10s', str(LIMITS['wall_seconds']), *command]
            result = subprocess.run(command, cwd=path, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    check=False, timeout=LIMITS['wall_seconds'] + 30,
                                    pass_fds=() if lease is None else (lease.fileno(),))
        record.update(state='review_required' if result.returncode == 0 else 'failed', exit_code=result.returncode)
    except Exception as exc:
        record.update(state='failed', error=str(exc))
    finally:
        record['finished_utc'] = datetime.now(timezone.utc).isoformat()
        save(path, record)
        if lease is not None:
            lease.close()
        _lock.release()


def start(settings, identity: str, body: dict) -> dict:
    path = build_path(settings, identity)
    record = build_view(path)
    if body.get('confirm_id') != identity:
        raise HTTPException(400, 'Confirm this exact staged build ID')
    if record['state'] != 'staged':
        raise HTTPException(409, 'Only a staged build may be started')
    if not Path('/usr/bin/prlimit').is_file() or not Path('/usr/bin/timeout').is_file():
        raise HTTPException(503, 'Required process limit tools are unavailable')
    if shutil.disk_usage(path).free < LIMITS['minimum_free_bytes']:
        raise HTTPException(409, 'Less than 10 GiB free in the isolated build filesystem')
    for filename, key in [('recipe.json', 'recipe_sha256'), ('layout_snapshot.csv', 'layout_sha256')]:
        if hashlib.sha256((path / filename).read_bytes()).hexdigest() != record[key]:
            raise HTTPException(409, 'Reviewed recipe or layout changed; stage a new request')
    if layout_info(settings.layout_csv)['sha256'] != record['original_layout_sha256']:
        raise HTTPException(409, 'Current wiring changed since review; stage a new request')
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, 'Another local calibration build is running')
    lease = None
    try:
        lease = (local_root(settings) / 'build.lock').open('a')
        try:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HTTPException(409, 'An isolated builder process still owns the build lease') from exc
        record = build_view(path)
        if record['state'] != 'staged':
            raise HTTPException(409, 'This build was already started')
        record.update(state='running', supervisor_pid=os.getpid(), started_utc=datetime.now(timezone.utc).isoformat())
        save(path, record)
        threading.Thread(target=execute, args=(path, lease), daemon=True).start()
    except Exception:
        if lease is not None:
            lease.close()
        _lock.release()
        raise
    return record


def build_router(settings) -> APIRouter:
    router = APIRouter()

    @router.get('/api/commissioning')
    def overview(date: str = Query(default='')):
        date = date or datetime.now(timezone.utc).date().isoformat()
        try:
            defaults = cal_defaults(date, settings)
            defaults['wired_antennas'] = layout_info(settings.layout_csv)['wired_antennas']
            defaults['antennas_note'] = ('Initial selection uses the ledger-recorded weights at one frequency channel, '
                                        'not a new all-frequency membership or live-payload verification. Review against the current wiring list.')
            defaults['deployed']['evidence_note'] = 'Ledger-recorded reference; a mixed factorial weights product can use several calibration solutions.'
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        root = local_root(settings)
        builds = []
        if root.is_dir():
            for path in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
                if path.is_dir() and not path.is_symlink() and re.fullmatch('[0-9a-f]{32}', path.name):
                    try:
                        builds.append(build_view(path))
                    except HTTPException:
                        pass
        return dict(defaults=defaults, builds=builds,
                    recorded_diagnostics=recorded_artifacts(settings),
                    authority=dict(build_requires_explicit_start=True, deployment=False, restart_defaults=False),
                    budget=LIMITS,
                    warnings=['Review the proposed Sun window and static conditions; defaults are not a scientific validation.',
                              'Build output remains local. No deploy script or registry write is invoked.',
                              'Ledger calibration is a reference, not a guarantee of one calibration for every beam in a mixed factorial product.'])

    @router.get('/api/commissioning-recorded/artifacts/{artifact_id}')
    def recorded_artifact(artifact_id: str):
        for item in recorded_artifacts(settings):
            if item['id'] == artifact_id:
                return FileResponse(item['path'], filename=item['name'],
                                    content_disposition_type='inline' if item['kind'] == 'image' else 'attachment')
        raise HTTPException(404, 'Recorded diagnostic artifact unavailable')

    @router.post('/api/commissioning/stage')
    def stage_route(body: dict):
        return stage(settings, body)

    @router.post('/api/commissioning/{identity}/start')
    def start_route(identity: str, body: dict):
        return start(settings, identity, body)

    @router.get('/api/commissioning/{identity}')
    def detail(identity: str):
        return build_view(build_path(settings, identity))

    @router.get('/api/commissioning/{identity}/log')
    def log(identity: str):
        path = build_path(settings, identity) / 'build.log'
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, 'No build log yet')
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 128_000))
            return {'text': stream.read(128_000).decode(errors='replace'), 'tail_bytes': 128_000}

    @router.get('/api/commissioning/{identity}/artifacts/{artifact_id}')
    def artifact(identity: str, artifact_id: str):
        path = build_path(settings, identity)
        for item in artifacts(path):
            if item['id'] == artifact_id:
                return FileResponse(path / item['path'], filename=item['name'],
                                    content_disposition_type='inline' if item['kind'] == 'image' else 'attachment')
        raise HTTPException(404, 'Unknown diagnostic artifact')
    return router
