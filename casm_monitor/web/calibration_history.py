"""Read-only presentation of saved cross-day diagnostics."""
from functools import lru_cache
from io import BytesIO
import re
import threading

import numpy as np

from ..cal_defaults import deployed_product, sha256_file
from ..jobs.cal_build import build_dir
from ..observation import read_json

_RENDER_LOCK = threading.Lock()


def current_reference(settings):
    product = deployed_product(settings)
    path = product.get('cal_file')
    return dict(path=path, sha256=sha256_file(path) if path else None)


def reference_label(settings, reference):
    current = current_reference(settings)
    if reference.get('sha256') and current['sha256'] == reference['sha256']:
        return 'Current deployed weights'
    return 'Previously deployed weights' if reference.get('sha256') else 'Reference unavailable'


@lru_cache(maxsize=32)
def _png(path, mtime, size, first, second, candidate_label, deployed_label):
    from casm_vis_analysis.calibration_inspection import history_figure
    with np.load(path, allow_pickle=False) as saved:
        payload = dict(saved)
    days = sorted(key.removesuffix('_measured') for key in payload if key.endswith('_measured'))
    if not days or len(days) > 3:
        raise ValueError('Expected one to three saved comparison days')
    with _RENDER_LOCK:
        fig = history_figure(payload['freq_mhz'], payload, days, first, second,
                             candidate_label=candidate_label, reference_weights_label=deployed_label)
        output = BytesIO()
        fig.savefig(output, format='png', dpi=125, facecolor='white')
        return output.getvalue()


def history_png(settings, tag, identity, reference, target):
    if not re.fullmatch(r'[0-9a-f]{32}', identity):
        raise ValueError('Unknown history action')
    root = build_dir(settings, tag)
    dest = root / 'actions' / identity
    path = dest / f'history-{reference}-{target}.npz'
    result_path = dest / 'result.json'
    request_path = root / 'request.json'
    layout_path = root / 'selected_layout.csv'
    if any(p.is_symlink() for p in (root, root/'actions', dest, path, result_path, request_path, layout_path)):
        raise ValueError('History symlinks are not permitted')
    result = read_json(result_path) or {}
    record = read_json(request_path) or {}
    if result.get('action') != 'history' or not path.is_file():
        raise ValueError('No completed history arrays for this baseline')
    params = record['params']
    if reference not in params['antennas'] or target not in params['antennas'] or reference == target:
        raise ValueError('Unknown history baseline')
    source = result.get('result', {}).get('deployed_reference',
        dict(path=record.get('reference'), sha256=record.get('reference_sha256')))
    label = 'With static subtraction' if params.get('static_window') else 'Without static subtraction'
    # Use the same canonical antenna labels as new worker-generated figures.
    from casm_io.correlator.mapping import AntennaMapping
    mapping = AntennaMapping.load(str(layout_path)) if layout_path.is_file() else None
    name = mapping.format_antenna if mapping is not None else lambda a: f'Ant {a}'
    stat = path.stat()
    return _png(str(path), stat.st_mtime_ns, stat.st_size, name(reference), name(target), label,
                reference_label(settings, source))
