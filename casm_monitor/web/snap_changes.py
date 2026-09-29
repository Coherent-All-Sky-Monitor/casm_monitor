"""Conservative changes in saved spectra, not an antenna-health classifier.

No hardware calls or storage writes. Compare the newest two reads to the preceding
24-hour median, using only the same recorded EQ/FFT epoch. Normal historical
variability raises the thresholds; missing evidence must never become green.
"""
from __future__ import annotations

import warnings

import numpy as np

METHOD = dict(version=2, baseline_hours=24, minimum_reads=6, minimum_span_hours=4,
              level_db=6, shape_db=3, shape_fraction=.10, spike_db=12,
              zero_fraction=.01, scatter_multiplier=6, confirmation_reads=2,
              note='Changes relative to recent history, not a diagnosis. Thresholds include historical scatter. '
                   'Normal daily variation, sky signals and unrecorded gain changes can contribute.')


def db(values):
    values = np.asarray(values, dtype=float)
    out = np.full(np.shape(values), np.nan, dtype=float)
    np.log10(values, out=out, where=np.isfinite(values) & (values > 0))
    return 10 * out


def median(values, axis=None):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        return np.nanmedian(values, axis=axis)


def assess(samples, adc, *, interval=3600, details=False):
    """Samples are chronological {id, t0, eq_epoch, fft_shift, data, error} rows."""
    current = samples[-1] if samples else None
    out = dict(adc=adc, state='unknown', label='Not enough history', reasons=[],
               current_id=current['id'] if current else None,
               ts=current['t0'] if current else None, reference_id=None, reference_ts=None,
               sample_ids=[s['id'] for s in samples])
    if not current or current.get('data') is None:
        return {**out, 'label':'Saved spectrum unavailable'}
    if len(samples) < 2:
        return out
    previous = samples[-2]
    epoch = (current.get('eq_epoch'), current.get('fft_shift'))
    if epoch[0] is None or epoch[1] is None:
        return {**out, 'label':'EQ/FFT identity unavailable'}
    if epoch != (previous.get('eq_epoch'), previous.get('fft_shift')):
        return {**out, 'state':'config', 'label':'EQ / FFT changed'}
    baseline = [s for s in samples[:-2] if previous['t0'] - 86400 <= s['t0'] < previous['t0']
                and (s.get('eq_epoch'), s.get('fft_shift')) == epoch and s.get('data') is not None]
    baseline = [s for s in baseline if np.isfinite(s['data'][adc]).all() and (s['data'][adc] >= 0).all()
                and np.count_nonzero(s['data'][adc] > 0) >= 4096*.8]
    if len(baseline) < 6 or baseline[-1]['t0'] - baseline[0]['t0'] < 4*3600:
        return out
    raw = np.stack([s['data'][adc] for s in baseline]).astype(float)
    history = db(raw)
    centre = median(history, axis=0)
    support = np.isfinite(history).sum(axis=0) >= max(3, (len(baseline)+1)//2)
    history_power = db(raw.mean(axis=1))
    centre_power = float(median(history_power))
    scatter_power = float(1.4826 * median(np.abs(history_power-centre_power)))
    level_limit = max(METHOD['level_db'], METHOD['scatter_multiplier']*scatter_power)
    history_offset = median(history-centre, axis=1)
    shape_scatter = 1.4826 * median(np.abs(history-centre-history_offset[:,None]), axis=0)
    shape_limit = np.maximum(METHOD['shape_db'], METHOD['scatter_multiplier']*shape_scatter)
    spike_limit = np.maximum(METHOD['spike_db'], 8*shape_scatter)
    ref = baseline[int(np.nanargmin(np.abs(history_power-centre_power)))]
    out.update(reference_id=ref['id'], reference_ts=ref['t0'], baseline_reads=len(baseline),
               baseline_ids=[s['id'] for s in baseline],
               baseline_start=baseline[0]['t0'], baseline_end=baseline[-1]['t0'],
               level_threshold_db=round(level_limit, 2))

    def measure(sample):
        if sample.get('data') is None:
            return None
        a = np.asarray(sample['data'][adc], dtype=float)
        if not np.isfinite(a).all() or (a < 0).any():
            return None
        zeros = support & (a == 0)
        loss = np.count_nonzero(zeros) >= int(np.ceil(4096*METHOD['zero_fraction']))
        diff = db(a)-centre
        usable = support & np.isfinite(diff)
        shift = float(median(diff[usable])) if usable.any() else 0
        power = float(db(np.mean(a))) if np.mean(a) > 0 else None
        delta = power-centre_power if power is not None else None
        shape = usable & (np.abs(diff-shift) > shape_limit)
        spike = usable & (diff-shift > spike_limit)
        flags = {}
        if loss:
            flags['loss'] = zeros
        if delta is not None and abs(delta) > level_limit:
            flags['power_up' if delta > 0 else 'power_down'] = True
        if np.count_nonzero(shape) >= 4096*METHOD['shape_fraction']:
            flags['shape'] = shape
        # Fine-channel excursions remain measurable, but are not antenna alarms.
        # An intermittent line need not change the band power or broad shape.
        peaks = np.flatnonzero(spike)
        peaks = sorted(peaks, key=lambda k: diff[k]-shift, reverse=True)[:8]
        return dict(flags=flags, delta=delta, shift=shift, zeros=int(zeros.sum()),
                    shape_channels=int(shape.sum()), spike_channels=int(spike.sum()),
                    shape_sign=np.sign(diff-shift),
                    peaks=[dict(channel=int(k), frequency_mhz=round(500-k*125/4096,6),
                                excess_db=round(float(diff[k]-shift),2),
                                difference_db=round(float(diff[k]),2),
                                prior_reads=int(np.sum(history[:,k]-centre[k]-history_offset > spike_limit[k]))) for k in peaks])

    now, before = measure(current), measure(previous)
    if now is None:
        return {**out, 'label':'Incomplete spectrum'}
    labels = dict(loss='New zero-power channels', power_up='Power increased', power_down='Power decreased',
                  shape='Broad spectral shape changed')
    confirmed = []
    spacing = current['t0'] - previous['t0']
    if before and .5*interval <= spacing <= 1.5*interval:
        for key, mask in now['flags'].items():
            if key not in before['flags']:
                continue
            if key.startswith('power_'):
                confirmed.append(key)
            else:
                shared = mask & before['flags'][key]
                if key == 'shape':
                    shared &= now['shape_sign'] == before['shape_sign']
                overlap = int(np.count_nonzero(shared))
                minimum = int(np.ceil(4096*(.01 if key == 'loss' else .05)))
                if overlap >= minimum:
                    confirmed.append(key)
    flags = list(now['flags'])
    state = 'attention' if confirmed else 'watch' if flags else 'info' if now['spike_channels'] else 'stable'
    if not flags and (before is None or spacing > 1.5*interval):
        state = 'unknown'
    if details:
        out.update(reference_spectra_db=centre, current_spectra_db=db(current['data'][adc]),
                   difference_db=db(current['data'][adc])-centre,
                   reference_band_power_db=centre_power, shape_offset_db=now['shift'])
    return {**out, 'state':state, 'label':labels[(confirmed or flags)[0]] if flags else
            ('No large power / shape change' if state == 'stable' else
             'Fine-channel variation' if state == 'info' else 'Previous read missing'),
            'reasons':[labels[k] for k in flags], 'confirmed':[labels[k] for k in confirmed],
            'power_delta_db':None if now['delta'] is None else round(now['delta'],2),
            'new_zero_channels':now['zeros'], 'shape_channels':now['shape_channels'],
            'spike_channels':now['spike_channels'], 'peaks':now['peaks']}
