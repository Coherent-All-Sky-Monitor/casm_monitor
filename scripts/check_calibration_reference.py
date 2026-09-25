"""Numerical acceptance against the saved September-18 stationary-beam notebook.

Reproduce its pointing, antenna set, off-source interval and integration mask.
This compares the maintained cross-only beam API, not a new beam implementation.
Reads one five-hour selected-input window; never writes telescope products.
"""
import json
from pathlib import Path

import numpy as np

from bf_weights_generator import load_calibration_weights
from casm_vis_analysis.beam_power import beam_power_vs_time
from casm_vis_analysis.calibration_checks import read_selected
from casm_vis_analysis.sources import source_altaz


def main():
    base = Path('/mnt/nvme3/vishnu/cal_build_20260917')
    saved_path = base / 'stationary_beam/stationary_beam_cyg_a_new_sep17_core16.npz'
    saved = np.load(saved_path)
    cal = load_calibration_weights(base / 'cal_sep17_core16_exact512.h5')
    data, mapping, _ = read_selected('/mnt/nvme4/data/casm/visibilities_64ant',
        '/home/casm/software/dev/antenna_layouts/casm_antenna_layout_2026-08-31_ant18admitted.csv',
        list(map(int, cal.ant_ids)), ['2026-09-18 01:35:00', '2026-09-18 06:35:00'])
    np.testing.assert_array_equal(data['time_unix'], saved['time_unix'])
    np.testing.assert_array_equal(data['freq_mhz'], saved['freq'])
    fine = np.arange(data['time_unix'][0], data['time_unix'][-1], 30.)
    altitude, azimuth = source_altaz('cyg-a', fine)
    index = int(np.argmax(altitude))
    off = (np.abs(data['time_unix'] - fine[index]) > 70 * 60) & np.isfinite(saved['coh']).all(axis=1)
    static = np.median(data['vis'][off].real, axis=0) + 1j * np.median(data['vis'][off].imag, axis=0)
    working = {**data, 'vis': data['vis'] - static[None]}
    measured = beam_power_vs_time(working, mapping, [('fixed', altitude[index], azimuth[index])],
        cal_weights=cal, return_spectrum=True)['spectrum']['fixed']
    valid = np.isfinite(saved['coh'])
    difference = measured[valid] - saved['coh'][valid]
    relative_rms = float(np.linalg.norm(difference) / np.linalg.norm(saved['coh'][valid]))
    assert relative_rms < 1e-5, relative_rms
    result = dict(reference=str(saved_path), relative_rms=relative_rms,
                  shape=list(measured.shape), matched_pointing=True, matched_offsource_mask=True,
                  comparison='Same signed cross-only dynamic spectrum, before per-channel display masking')
    output = Path('/home/casm/scratch/casm-observation-preview/calibration/reference-validation.json')
    output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
