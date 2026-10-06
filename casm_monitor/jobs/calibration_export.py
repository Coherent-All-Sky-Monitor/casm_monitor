"""Immutable, identified copies of existing CB/IB products for human review.

This module never generates weights, executes the deploy tool or contacts a node.
Only configured export roots are writable. Completion is published last.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil

from ..cal_defaults import deployed_product, sha256_file
from ..observation import read_json
from .cal_build import build_dir, load_summary
from .deploy import canonical_sources, no_symlinks_inside, stage_digest, load_stage


def real_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Use an absolute directory without .. components')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Export paths must not contain symlinks')
    return path


def export_parent(settings, value):
    path = real_path(value)
    roots = [real_path(root) for root in settings.cal_export_roots]
    if not any(path == root or root in path.parents for root in roots):
        raise ValueError('Choose an export directory within: '+', '.join(map(str, roots)))
    if any(path == p or p in path.parents for p in (Path(settings.store_root), Path(settings.cal_builds_root))):
        raise ValueError('Export directory must be separate from the workspace store')
    return path


def export_product(settings, root, dest, record, selection):
    tag, identity = root.name, dest.name
    parent = export_parent(settings, selection['parent'])
    summary_path = root/'summary.json'
    summary_hash = sha256_file(summary_path)
    if summary_hash != selection['summary_sha256']:
        raise ValueError('Generated product changed after export was requested')
    summary = load_summary(settings, tag)
    if record.get('imported') or not summary.get('has_weights'):
        raise ValueError('Generate both CB and IB weights before exporting')
    cb, ib = canonical_sources(settings, tag, summary)
    if ib is None:
        raise ValueError('The paired IB weights are missing')
    inputs = dict(CB=cb, IB=ib)
    for key in ('cal_h5', 'report_json', 'notebook'):
        value = summary['paths'].get(key)
        if not value:
            raise ValueError(f'Generated product has no {key}')
        inputs[key] = no_symlinks_inside(Path(value), root)
    for name in ('request.json', 'summary.json', 'layout_snapshot.csv', 'selected_layout.csv'):
        inputs[name] = no_symlinks_inside(root/name, root)
    if any(not p.is_file() for p in inputs.values()):
        raise ValueError('A required calibration product or recipe file is missing')
    # Hash before copying, then verify both ends. A changed input cannot be
    # silently published as the product the operator requested.
    hashes = {key:sha256_file(path) for key,path in inputs.items()}
    needed = sum(p.stat().st_size for p in inputs.values()) + 1024**3
    existing = next(p for p in (parent, *parent.parents) if p.exists())
    if shutil.disk_usage(existing).free < needed:
        raise ValueError('Not enough free space for this export plus a 1 GiB reserve')
    created = datetime.now(timezone.utc)
    solve_date = str(summary['source_window'][0])[:10].replace('-', '')
    if not re.fullmatch(r'\d{8}', solve_date):
        raise ValueError('Missing calibration solve date')
    directory = parent/solve_date/f"{tag}__{created.strftime('%Y%m%dT%H%M%SZ')}_{identity}"
    partial = directory.with_name(directory.name+'.incomplete')
    real_path(partial)
    partial.mkdir(parents=True, exist_ok=False)
    files = []
    for key,path in inputs.items():
        folder = 'CB' if key == 'CB' else 'IB' if key == 'IB' else 'calibration' if key == 'cal_h5' else 'recipe'
        relative = Path(folder)/path.name
        target = partial/relative
        target.parent.mkdir(exist_ok=True)
        with path.open('rb') as source, target.open('xb') as output:
            shutil.copyfileobj(source, output, length=1024**2)
        if sha256_file(target) != hashes[key] or sha256_file(path) != hashes[key]:
            raise ValueError(f'{key} changed during export; incomplete directory retained: {partial}')
        files.append(dict(role=key, relative=str(relative), source=str(path), sha256=hashes[key], bytes=target.stat().st_size))
    if sha256_file(summary_path) != summary_hash:
        raise ValueError('Build summary changed during export; incomplete copy retained')
    manifest = dict(schema_version=1, export_id=identity, tag=tag, state='written', created_utc=created.isoformat(),
        solve_date_utc=solve_date, source=summary.get('source'), source_window=summary['source_window'],
        static_window=summary.get('static_window'), antennas=summary['antennas'], n_beams=summary.get('n_beams'),
        summary_sha256=summary_hash, directory=str(directory), files=files,
        comparison_deployment=deployed_product(settings), uploaded=False)
    with (partial/'manifest.json').open('x') as output:
        json.dump(manifest, output, indent=2)
        output.write('\n')
    real_path(parent)
    if directory.exists():
        raise ValueError('Export already exists; refusing to overwrite')
    partial.rename(directory)
    return dict(**manifest, manifest_sha256=sha256_file(directory/'manifest.json'))


def verify_export(settings, tag, identity):
    if not re.fullmatch(r'[0-9a-f]{32}', str(identity)):
        raise ValueError('Select a completed weight export')
    root = build_dir(settings, tag)
    path = no_symlinks_inside(root/'actions'/identity/'result.json', root)
    saved = read_json(path) or {}
    item = saved.get('result') or {}
    if saved.get('action') != 'export' or item.get('export_id') != identity or item.get('tag') != tag:
        raise ValueError('Unknown weight export for this build')
    directory = real_path(item['directory'])
    export_parent(settings, directory)
    manifest_path = directory/'manifest.json'
    real_path(manifest_path)
    if sha256_file(manifest_path) != item.get('manifest_sha256'):
        raise ValueError('Export manifest changed; write a new export before continuing')
    manifest = read_json(manifest_path)
    if manifest != {key:value for key,value in item.items() if key != 'manifest_sha256'}:
        raise ValueError('Export receipt and manifest differ')
    if manifest['summary_sha256'] != sha256_file(root/'summary.json'):
        raise ValueError('Generated product changed since export')
    roles = set()
    for file in manifest['files']:
        target = no_symlinks_inside(directory/file['relative'], directory)
        source = no_symlinks_inside(Path(file['source']), root)
        if sha256_file(target) != file['sha256'] or sha256_file(source) != file['sha256']:
            raise ValueError('Export or original file changed: '+file['role'])
        roles.add(file['role'])
    if not {'CB','IB','cal_h5','summary.json','request.json','notebook'}.issubset(roles):
        raise ValueError('Incomplete CB/IB export')
    return item


def verify_review(settings, tag, stage=None):
    root = build_dir(settings, tag)
    review = read_json(root/'review.json') or {}
    stage = stage or load_stage(settings, tag)
    if not review.get('accepted') or not stage or not stage.get('checks_ok'):
        raise ValueError('Complete the export, dry run and human review first')
    if review.get('summary_sha256') != sha256_file(root/'summary.json'):
        raise ValueError('Generated product changed since review')
    if review.get('stage_digest') != stage_digest(stage) or review.get('export_id') != stage.get('export_id'):
        raise ValueError('Dry run or export changed since review; review the exact product again')
    item = verify_export(settings, tag, review['export_id'])
    if item['manifest_sha256'] != stage.get('export_manifest_sha256'):
        raise ValueError('Dry run is not bound to the reviewed export')
    return review
