"""Saved subtraction trials and rank-1 curves. Reads never run a solve."""
from pathlib import Path

import numpy as np

from ..cal_defaults import deployed_product, sha256_file
from ..observation import read_json
from ..jobs.cal_build import build_dir
from .calibration_reference import recipe_for


def night_window(record):
    return record.get('night_window') or record['params'].get('static_window')


def mode(record):
    return 'static' if record['params'].get('static_window') else 'regular'


def family(settings, tag):
    root = build_dir(settings, tag)
    record = read_json(root / 'request.json')
    if not record:
        raise ValueError('Unknown calibration candidate')
    group = record.get('comparison_root_tag', tag)
    first = read_json(build_dir(settings, group) / 'request.json')
    if not first:
        raise ValueError('Comparison recipe is unavailable')
    versions = {}
    for path in sorted(settings.cal_builds_root.glob('*/request.json')):
        if path.is_symlink() or path.parent.is_symlink():
            continue
        item = read_json(path)
        if not item or item.get('comparison_root_tag', item['params']['tag']) != group:
            continue
        # A membership trial starts a new family. Reject mismatched imported metadata.
        if any(item['params'].get(k) != first['params'].get(k) for k in ('source_window', 'antennas', 'ref_ant')):
            continue
        if item.get('selected_layout_sha256') != first.get('selected_layout_sha256'):
            continue
        summary = read_json(path.parent / 'summary.json') or {}
        cal = summary.get('paths', {}).get('cal_h5')
        ready = bool(cal and Path(cal).is_file())
        versions[mode(item)] = dict(tag=item['params']['tag'], ready=ready,
            imported=bool(item.get('imported')), source_window=item['params']['source_window'],
            static_window=item['params'].get('static_window'), night_window=night_window(item))
    return dict(root_tag=group, selected=mode(record), versions=versions,
                night_window=night_window(first), can_prepare=bool(night_window(first)) and not first.get('imported', False))


def calibration_curve(path):
    """Original full-band solve diagnostic from the calibration file."""
    import h5py
    with h5py.File(path, 'r') as h:
        freq = np.asarray(h['freqs_mhz'], dtype=float)
        values = np.asarray(h['rank1_ratios'], dtype=float)
        antennas = np.asarray(h['ant_ids'], dtype=int).tolist()
    if freq.ndim != 1 or freq.size < 2 or values.shape != freq.shape or not np.isfinite(freq).all():
        raise ValueError('Invalid saved rank-1 frequency axis')
    finite = np.isfinite(values)
    return dict(freq_mhz=freq.tolist(), values=np.where(finite, values, None).tolist(),
                median=float(np.median(values[finite])) if finite.any() else None,
                antennas=antennas, path=str(path), sha256=sha256_file(path))


def rank1_comparison(settings, tag):
    comparison = family(settings, tag)
    curves, unavailable = [], {}
    for key, label in (('regular', 'Regular'), ('static', 'Static-subtracted')):
        version = comparison['versions'].get(key)
        try:
            if version and version['imported']:
                raise ValueError('Create a reviewed solve to compare subtraction')
            if not version or not version['ready']:
                raise ValueError('Prepare this solution to show its saved rank-1 curve')
            summary = read_json(build_dir(settings, version['tag']) / 'summary.json')
            curve = calibration_curve(summary['paths']['cal_h5'])
            curves.append(dict(**curve, key=key, label=label, source_window=version['source_window']))
        except (ValueError, KeyError, OSError) as exc:
            unavailable[key] = str(exc)
    # Resolve the ledger now, independently of the reference frozen into an old trial.
    product = deployed_product(settings)
    try:
        path = Path(product.get('cal_file') or '')
        if not path.is_file():
            raise ValueError('The current deployed calibration is unavailable')
        curve = calibration_curve(path)
        try:
            recipe, _ = recipe_for(path, max_duration_s=4*3600)
            window = recipe['params']['source_window']
        except (ValueError, OSError):
            window = None
        curves.append(dict(**curve, key='deployed', label='Current deployed weights', source_window=window,
                           uploaded=product.get('date_deployed')))
    except (ValueError, KeyError, OSError) as exc:
        unavailable['deployed'] = str(exc)
    return dict(curves=curves, unavailable=unavailable)
