"""Small display responses from saved calibration diagnostics; no raw-data reads."""
from pathlib import Path
import base64
import io
import json
from functools import lru_cache

import numpy as np

from .science_array import finite_list, phase


@lru_cache(maxsize=12)
def arrays(path, mtime):
    with np.load(path, allow_pickle=False) as saved:
        return dict(saved)


def load_arrays(path):
    return arrays(str(path), path.stat().st_mtime_ns)


def latest_inspection(root, expected_static=None):
    if root.is_symlink() or (root / 'actions').is_symlink():
        raise ValueError('Inspection symlinks are not permitted')
    paths = [p for p in root.glob('actions/*/inspection/inspection.json')
             if (p.parent.parent / 'result.json').is_file()]
    if any(p.is_symlink() for path in paths for p in (path, path.parent, path.parent.parent)):
        raise ValueError('Inspection symlinks are not permitted')
    if expected_static is not None:
        paths = [p for p in paths if json.loads(p.read_text()).get('static_used') is expected_static]
    if not paths:
        raise ValueError('Prepare the array diagnostics for this saved calibration first')
    path = max(paths, key=lambda p: p.stat().st_mtime_ns)
    if any(p.is_symlink() for p in (path, path.parent, path.parent.parent)):
        raise ValueError('Inspection symlinks are not permitted')
    return path.parent, json.loads(path.read_text())


def tile(values, quantity, freq):
    from matplotlib import colormaps
    from PIL import Image
    finite = values[np.isfinite(values)]
    if quantity == 'phase':
        low, high, cmap = -np.pi, np.pi, 'twilight_shifted'
    elif quantity == 'amp':
        low = 0.
        high = max(float(np.max(finite)) if finite.size else 1., 1e-12)
        cmap = 'magma'
    else:
        high = max(float(np.max(np.abs(finite))) if finite.size else 1., 1e-12)
        low, cmap = -high, 'RdBu_r'
    rgb = colormaps[cmap](np.clip(np.nan_to_num((values-low)/(high-low)), 0, 1), bytes=True)
    rgb[~np.isfinite(values)] = (238, 240, 243, 255)
    # Raster top is high frequency, regardless of native file ordering.
    if freq[0] < freq[-1]:
        rgb = rgb[:, ::-1]
    def png(pixels):
        buf = io.BytesIO()
        Image.fromarray(pixels).save(buf, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    return dict(src=png(rgb.transpose(1, 0, 2)), scale=png(colormaps[cmap](np.linspace(0, 1, 256), bytes=True)[None, :, :]),
                min=low, max=high, log=False, width=values.shape[0], height=values.shape[1])


def component(z, amplitude, quantity):
    return amplitude if quantity == 'amp' else z.real if quantity == 'real' else z.imag if quantity == 'imag' else phase(z)


def snapshot(root, reference=None, stage='svd', quantity='phase', scheme='uniform', target=None, *, expected_static=None):
    dest, meta = latest_inspection(root, expected_static)
    if stage not in meta['stages'] or quantity not in ('amp', 'real', 'imag', 'phase'):
        raise ValueError('Unknown processing stage or quantity')
    if stage == 'geometric' and quantity != 'phase':
        raise ValueError('The geometric model is phase only')
    if scheme not in meta['schemes']:
        raise ValueError('Unknown amplitude scheme')
    ants = meta['antennas']
    ref = reference if reference is not None else meta['ref_ant']
    if ref not in ants or (target is not None and (target not in ants or target == ref)):
        raise ValueError('Choose a reference and distinct target from this solve')
    antdata = load_arrays(dest / 'antennas.npz')
    # Phase weights are re-referenced for display; no phase or amplitude re-solve.
    weights = antdata['phase_weights']
    applied = weights * weights[ants.index(ref)].conj()[None, :]
    recorded = antdata['reference_weights']
    refphase = recorded / np.abs(recorded)
    refphase = refphase * refphase[ants.index(ref)].conj()[None, :]
    amplitude = antdata['amp_'+scheme]
    # Quantization uses original cal reference (not arbitrary display reference).
    actual = amplitude*weights
    quantized = (np.rint(np.nan_to_num(actual.real)*127) + 1j*np.rint(np.nan_to_num(actual.imag)*127))/127
    panels = []
    for antenna in ([target] if target is not None else ants):
        if antenna == ref:
            continue
        a, b = sorted((ref, antenna))
        saved = load_arrays(dest / f'pair_{a}_{b}.npz')
        z, dynamic = saved[stage+'_mean'], saved[stage+'_dynamic']
        # Panel convention matches Visibilities: V(target, reference).
        if antenna > ref:
            z, dynamic = z.conj(), dynamic.conj()
        panels.append(dict(antenna=antenna, values=finite_list(component(z, saved[stage+'_amp'], quantity)),
            tile=tile(component(dynamic, saved[stage+'_dynamic_amp'], quantity), quantity, meta['preview_freq_mhz'])))
    antenna_panels = []
    for i, antenna in enumerate(ants):
        reference_amp = np.abs(recorded[i])
        # Compare normalized amplitudes with common per-channel max convention.
        norm = np.max(np.where(np.isfinite(recorded), np.abs(recorded), 0), axis=0)
        reference_amp = np.divide(reference_amp, norm, out=np.full_like(norm, np.nan), where=norm > 0)
        ratio = np.divide(amplitude[i], reference_amp, out=np.full_like(norm, np.nan), where=reference_amp > 0)
        antenna_panels.append(dict(antenna=antenna,
            auto=finite_list(antdata['auto'][i]), night=finite_list(antdata['night'][i]),
            static=finite_list(antdata['static_auto'][i]),
            subtracted=finite_list(antdata['auto'][i]-antdata['static_auto'][i]),
            applied_phase=finite_list(phase(applied[i])), reference_phase=finite_list(phase(refphase[i])),
            phase_difference=finite_list(phase(applied[i]*refphase[i].conj())),
            amplitude=finite_list(amplitude[i]), amplitude_ratio=finite_list(ratio),
            quantized_amplitude=finite_list(np.where(np.isfinite(amplitude[i]), np.abs(quantized[i]), np.nan)),
            quantization_phase=finite_list(phase(quantized[i]*actual[i].conj())),
            zeroed_channels=int(np.sum(np.isfinite(amplitude[i]) & (amplitude[i] > 0) & (np.abs(quantized[i]) == 0))),
            residual=finite_list(antdata['residual_rms'][i])))
    return dict(**meta, inspection_id=dest.parent.name, reference=ref, stage=stage, quantity=quantity, scheme=scheme,
                panels=panels, antenna_panels=antenna_panels)
