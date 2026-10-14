"""Cheap saved-settings read for the Search widget; never probes a host on GET."""
import json
import math
import time

from ..util import iso

MAX_AGE_S = 180
# Deployed fork's definitions.h, descending native channels.
NCHAN, FCH1_MHZ, FOFF_MHZ, TSAMP_S = 3072, 484.375, -0.030517578125, 0.001048576


def number(values, key):
    try:
        value = float(values[key])
        return value if math.isfinite(value) else None
    except (KeyError, ValueError, TypeError):
        return None


def describe(row):
    values = row.get('values', {})
    settings = {key.lower(): number(values, key) for key in (
        'DM_MIN', 'DM_MAX', 'SNR', 'WIDTH_MIN', 'WIDTH_MAX', 'WIDTH_RATIO', 'GULP', 'NBEAM')}
    settings['gulp_s'] = None if settings['gulp'] is None else settings['gulp'] * TSAMP_S
    # Mirror the deployed fork's initialize() width-grid rule. WIDTH_MAX is
    # the smoothing support, whose half-width caps the searched boxcars.
    widths = []
    w, maximum, ratio = (settings[k] for k in ('width_min', 'width_max', 'width_ratio'))
    if w is not None and maximum is not None and ratio is not None and w > 0:
        ratio = ratio if ratio > 1.01 else 2.0
        for _ in range(128):
            wi = max(1, int(w + 0.5))
            if wi > int(maximum) // 2 or len(widths) >= 15:
                break
            if not widths or wi > widths[-1]:
                widths.append(wi)
            w *= ratio
            if not math.isfinite(w):
                break
            if int(w + 0.5) <= wi:
                w = wi + 1
    settings['widths'] = widths
    # FLAG2 is exclusive. With no flagging SCRUNCH pass, FLAG1/2 does not
    # replace samples (the final fixed baseline pass has flag=0).
    start, stop = number(values, 'FLAG1'), number(values, 'FLAG2')
    start = -1 if start is None and 'FLAG1' not in values else start
    stop = -1 if stop is None and 'FLAG2' not in values else stop
    mask = {'enabled': None, 'channels': 0}
    if start is not None and stop is not None:
        lo, hi = max(0, int(start)), min(NCHAN, int(stop))
        if not row.get('scrunches') or hi <= lo:
            mask['enabled'] = False
        else:
            mask.update(enabled=True, first=lo, last=hi-1, channels=hi-lo,
                        freq_low_mhz=FCH1_MHZ+(hi-1)*FOFF_MHZ,
                        freq_high_mhz=FCH1_MHZ+lo*FOFF_MHZ)
    settings['mask'] = mask
    settings['sumthreshold'] = {'enabled': 'SUMTHRESHOLD' in values,
                                'parameters': values.get('SUMTHRESHOLD', '')}
    settings['scrunches'] = row.get('scrunches', [])
    settings['spec_min'] = number(values, 'SPEC_MIN')
    settings['spec_max'] = number(values, 'SPEC_MAX')
    settings['parameters'] = values
    return settings


def configuration(reader, now=None):
    now = time.time() if now is None else now
    rows, checks = [], []
    for node, expected in (('corr1', range(4)), ('corr2', range(4, 8))):
        sample = reader.latest_scalar(f'hella.runtime.{node}')
        age = None if sample is None else now - sample['ts']
        fresh = age is not None and 0 <= age <= MAX_AGE_S
        try:
            payload = json.loads(sample['value']) if sample else {}
        except (ValueError, TypeError):
            payload = {'error': 'Invalid saved configuration'}
        checks.append({'node': node, 'checked_utc': iso(sample['ts']) if sample else None,
                       'age_s': age, 'fresh': fresh, 'error': payload.get('error')})
        for stream in expected:
            found = [r for r in payload.get('streams', []) if r.get('stream') == stream]
            base = {'stream': stream, 'node': node, 'status': 'unknown', 'settings': None}
            if not fresh or payload.get('error'):
                base['reason'] = payload.get('error') or 'Waiting for fresh configuration'
            elif len(found) != 1:
                base.update(status='stopped' if not found else 'unknown',
                            reason='No HELLA process' if not found else 'Multiple HELLA processes')
            else:
                base.update(found[0])
                # Never promote file-only or stale settings to an active ON badge.
                if base['status'] == 'running':
                    base['settings'] = describe(base)
            rows.append(base)
    verified = [r for r in rows if r['settings'] is not None]
    signatures = {json.dumps(r['settings'], sort_keys=True) for r in verified}
    same = len(verified) == 8 and len(signatures) == 1
    return {'checked_utc': iso(now), 'max_age_s': MAX_AGE_S, 'nodes': checks,
            'streams': rows, 'verified_count': len(verified), 'all_match': same,
            'common': verified[0]['settings'] if same else None,
            'band': {'nchan': NCHAN, 'low_mhz': FCH1_MHZ+(NCHAN-1)*FOFF_MHZ,
                     'high_mhz': FCH1_MHZ, 'tsamp_ms': TSAMP_S*1000}}
