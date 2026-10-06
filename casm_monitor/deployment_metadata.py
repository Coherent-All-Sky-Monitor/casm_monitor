"""Read a permanent export only when its saved file hashes still match.

These helpers do not select the current product. Callers must first establish a
consistent registry association; a newly written export alone is never deployed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=64)
def _digest(path, inode, size, mtime_ns):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024**2), b''):
            h.update(chunk)
    return h.hexdigest()


def digest(path):
    path = Path(path).resolve()
    before = path.stat()
    identity = (before.st_ino, before.st_size, before.st_mtime_ns)
    value = _digest(str(path), *identity)
    after = path.stat()
    if identity != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('Export file changed during verification')
    return value


def exported_file(root, entry):
    path = (root / entry['relative']).resolve()
    if root not in path.parents or digest(path) != entry['sha256']:
        raise ValueError('Export file is outside its directory or has changed')
    return path


def manifest_for(path, role):
    path = Path(path).resolve()
    root = path.parent.parent
    manifest_path = root / 'manifest.json'
    if not manifest_path.is_file():
        return None
    if manifest_path.stat().st_size > 2_000_000:
        raise ValueError('Export manifest exceeds metadata limit')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('state') != 'written':
        raise ValueError('Export was not completed')
    entries = [e for e in manifest['files'] if e['role'] == role]
    if len(entries) != 1 or exported_file(root, entries[0]) != path:
        raise ValueError('Export does not bind this product')
    return root, manifest


def calibration_for_weights(path):
    bound = manifest_for(path, 'CB')
    if bound is None:
        return None
    root, manifest = bound
    entries = [e for e in manifest['files'] if e['role'] == 'cal_h5']
    if len(entries) != 1:
        raise ValueError('Export has no unique calibration')
    return str(exported_file(root, entries[0]))


def upload_snapshot(plan):
    """Freeze reviewed paths/options; later workspace edits cannot rewrite history."""
    stage = plan['stage']
    options = stage.get('options') or {}
    cal = (plan.get('summary', {}).get('paths') or {}).get('cal_h5')
    weights = str(plan['weights_h5'])
    cal = calibration_for_weights(weights) or cal
    return dict(kind=options.get('kind', 'both'),
                streams=options.get('streams', list(range(6))),
                utc_start=options.get('utc_start'),
                weights_h5=weights, ib_h5=str(plan['ib_h5']) if plan.get('ib_h5') else None,
                cal_h5=str(cal) if cal else None,
                cal_sha256=digest(cal) if cal and Path(cal).is_file() else None,
                scale=plan['scale'], ib_scale=plan['ib_scale'])


def successful_uploads(settings):
    """Read successful immutable receipts only, without creating a database."""
    paths = {Path(settings.db_path)}
    if settings.observation_cache_root:
        paths.add(Path(settings.observation_cache_root) / 'calibration' / 'monitor.sqlite')
    rows = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                results = conn.execute("SELECT ts, product_id, save_defaults, hashes FROM uploads "
                    "WHERE state='done' AND exit_code=0 AND registry='ok' ORDER BY ts DESC").fetchall()
            for item in results:
                try:
                    hashes = json.loads(item['hashes'] or '{}')
                    snapshot = hashes.get('deployment')
                    if isinstance(snapshot, dict):
                        rows.append(dict(snapshot, ts=item['ts'], product_id=item['product_id'],
                                         save_defaults=bool(item['save_defaults']),
                                         completed=hashes.get('completed_utc_unix', item['ts'])))
                except (ValueError, TypeError):
                    continue
        except sqlite3.Error:
            continue
    return sorted(rows, key=lambda item: item['ts'], reverse=True)
