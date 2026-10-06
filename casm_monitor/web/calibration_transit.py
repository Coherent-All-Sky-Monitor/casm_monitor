"""Read-only, cached redraws of saved transit arrays, including pre-v2 products."""
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import re
import threading

import numpy as np

from ..cal_defaults import sha256_file
from ..jobs.cal_build import build_dir
from ..observation import read_json
from .calibration_history import reference_label

_LOCK = threading.RLock()
CAL_LABELS = {'candidate', 'reference', 'Current deployed weights', 'without subtraction', 'with subtraction'}
STYLE_VERSION = 3


def _safe(path, root):
    if root.is_symlink() or any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
        raise ValueError('Transit symlinks are not permitted')
    if root.resolve() not in path.resolve().parents or not path.is_file():
        raise ValueError('Saved transit file is unavailable')
    return str(path)


def _legacy_cal(settings, root, key, action):
    if key in ('reference', 'Current deployed weights'):
        ref = action['result'].get('deployed_reference', {})
        path = ref.get('path')
        return path if path and ref.get('sha256') == sha256_file(path) else None
    if key == 'candidate':
        return (read_json(root/'summary.json') or {}).get('paths', {}).get('cal_h5')
    from .calibration_variants import family
    versions = family(settings, root.name)['versions']
    version = versions.get('static' if key == 'with subtraction' else 'regular', {})
    if version.get('ready'):
        summary = read_json(build_dir(settings, version['tag'])/'summary.json') or {}
        return summary.get('paths', {}).get('cal_h5')
    return None


def _key(settings, tag, identity, scale):
    from casm_vis_analysis.calibration_transit import SCHEME_LABELS
    if not re.fullmatch(r'[0-9a-f]{32}', identity) or scale not in ('mean', 'sum'):
        raise ValueError('Unknown transit action or power scale')
    root = build_dir(settings, tag)
    dest = root/'actions'/identity
    result_path = _safe(dest/'result.json', root)
    request_path = _safe(root/'request.json', root)
    action, request = read_json(result_path), read_json(request_path)
    if action.get('action') != 'transit':
        raise ValueError('No saved transit check')
    profile = None
    for path in sorted(root.glob('actions/*/result.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        _safe(path, root)
        previous = read_json(path) or {}
        candidate = path.parent/'inspection'/'antennas.npz'
        if previous.get('finished', '') <= action.get('finished', '') and candidate.is_file():
            profile = _safe(candidate, root)
            break
    entries, files = [], [result_path, request_path]
    for key in action['result'].get('metrics', {}):
        if key not in CAL_LABELS:
            continue
        path = _safe(dest/f'transit-{key}.npz', root)
        legacy_cal = _legacy_cal(settings, root, key, action) if action['result'].get('schema_version', 1) < 2 else None
        if legacy_cal and not Path(legacy_cal).is_file():
            legacy_cal = None
        label = key.capitalize()
        if key == 'candidate':
            label = 'With static subtraction' if request['params'].get('static_window') else 'Without static subtraction'
        elif key in ('reference', 'Current deployed weights'):
            label = reference_label(settings, action['result'].get('deployed_reference', {}))
        entries.append((f'cal-{len(entries)}', 'calibration', label, path, legacy_cal, None))
        files.extend([path, *([legacy_cal] if legacy_cal else [])])
    for key in action['result'].get('amplitude_comparison', {}).get('schemes', {}):
        if key not in SCHEME_LABELS:
            continue
        path = _safe(dest/f'amplitude-{key}-transit.npz', root)
        entries.append((f'amp-{key}', 'amplitude', SCHEME_LABELS[key], path, None, profile))
        files.append(path)
    if profile:
        files.append(profile)
    if not entries:
        raise ValueError('No saved transit arrays')
    fingerprints = tuple((p, Path(p).stat().st_mtime_ns, Path(p).stat().st_size) for p in sorted(set(files)))
    return (STYLE_VERSION, result_path, tuple(entries), fingerprints, scale)


def _indices(original, selected):
    lookup = {round(float(f), 6): i for i, f in enumerate(original)}
    try:
        indices = [lookup[round(float(f), 6)] for f in selected]
    except KeyError as exc:
        raise ValueError('Saved normalization frequency grids differ') from exc
    return indices


def _denominator(saved, entry, action):
    from casm_vis_analysis.calibration_transit import pair_weight_sum
    if 'pair_weight_sum' in saved:
        return saved['pair_weight_sum']
    identity, kind, label, path, cal_path, profile = entry
    ants = action['result']['antennas']
    if kind == 'amplitude' and profile:
        ants = action['result']['amplitude_comparison']['antennas']
        with np.load(profile, allow_pickle=False) as data:
            rows = [list(data['antennas']).index(a) for a in ants]
            columns = _indices(data['freq_mhz'], saved['freq_mhz'])
            return pair_weight_sum(data['amp_'+identity.removeprefix('amp-')][np.ix_(rows, columns)])
    if cal_path:
        from bf_weights_generator import load_calibration_weights
        cal = load_calibration_weights(cal_path)
        rows = [list(cal.ant_ids).index(a) for a in ants]
        columns = _indices(np.asarray(cal.frequencies_hz)/1e6, saved['freq_mhz'])
        return pair_weight_sum(np.asarray(cal.weights)[np.ix_(rows, columns)])
    return None


@lru_cache(maxsize=4)
def _review(version, result_path, entries, fingerprints, scale):
    from casm_vis_analysis.calibration_transit import background_selection, prepare_series
    action = read_json(result_path)
    result, selection = action['result'], action['selection']
    center = result['center_unix']
    items, arrays, notices = [], {}, []
    first_times, group_freqs, background, off = None, {}, None, None
    with _LOCK:
        for entry in entries:
            identity, kind, label, path, *_ = entry
            with np.load(path, allow_pickle=False) as data:
                saved = dict(data)
            times, freq = saved['time_unix'], saved['freq_mhz']
            if first_times is None:
                first_times = times
                off, sun_alt, background = background_selection(times, center, selection['source'], selection['off_minutes'])
            # Amplitude and calibration groups may have different flagged channel
            # sets. Comparisons within each group must still be exactly matched.
            if not np.array_equal(first_times, times):
                raise ValueError('Saved transit integration grids differ')
            if kind in group_freqs and not np.array_equal(group_freqs[kind], freq):
                raise ValueError('Saved comparison frequency grids differ')
            group_freqs[kind] = freq
            item = dict(id=identity, kind=kind, label=label, available=False)
            if background['valid']:
                try:
                    denominator = _denominator(saved, entry, action) if scale == 'mean' else None
                    series = prepare_series(times, freq, saved['spectrum'], center, off, pair_sum=denominator,
                        scale=scale, model=saved.get('exact_response'), minimum_snr=selection['minimum_snr'])
                    arrays[identity] = series
                    item.update(available=True, channels=len(freq), peak_over_off_rms=series['peak_over_off_rms'],
                                metrics=series['metrics']['summary'] if series['metrics'] else None)
                except (ValueError, OSError, KeyError) as exc:
                    item['reason'] = str(exc)
                    notices.append(f'{label}: {exc}')
            items.append(item)
    return dict(source=selection['source'], date=selection['date'], center_unix=center, scale=scale,
                background=background, items=items, notices=notices,
                updated_from_saved_arrays=True), arrays


def transit_review(settings, tag, identity, scale='mean'):
    return _review(*_key(settings, tag, identity, scale))[0]


@lru_cache(maxsize=24)
def _png(key, kind, identity):
    from casm_vis_analysis.calibration_transit import transit_figure, comparison_figure, timing_figure
    info, arrays = _review(*key)
    if not info['background']['valid']:
        raise ValueError(info['background']['reason'])
    items = {item['id']: item for item in info['items']}
    with _LOCK:
        if kind in ('calibration', 'amplitude'):
            group = {item['label']: arrays[item['id']] for item in info['items'] if item['kind'] == kind and item['available']}
            expected = sum(item['kind'] == kind for item in info['items'])
            if not group or len(group) != expected:
                raise ValueError('Complete matched comparison is unavailable; use Raw sum or rerun this check')
            figure = comparison_figure(group, f"{info['source']} · {kind} comparison")
        elif kind in ('transit', 'timing') and identity in arrays:
            title = f"{info['source']} · {items[identity]['label']}"
            figure = (transit_figure if kind == 'transit' else timing_figure)(arrays[identity], title)
        else:
            raise ValueError('Unknown saved transit plot')
        output = BytesIO()
        figure.savefig(output, format='png', dpi=130, facecolor='white')
        figure.clear()
        return output.getvalue()


def transit_png(settings, tag, identity, kind, series='', scale='mean'):
    return _png(_key(settings, tag, identity, scale), kind, series)
