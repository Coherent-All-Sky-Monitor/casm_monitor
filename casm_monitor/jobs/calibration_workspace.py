"""Durable offline actions for the isolated Calibration workspace."""
from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import load_settings
from ..cal_defaults import sha256_file
from ..observation import read_json
from ..web.calibration_workspace import request_record, save_json, action_params, analysis_window
from .cal_build import build_dir, generate_ib_mask, _numbers


def run(params):
    import shutil
    settings = load_settings()
    expected = Path(settings.observation_cache_root or '/nonexistent') / 'calibration'
    if settings.store_root.resolve() != expected.resolve():
        raise ValueError('Calibration worker must use the isolated calibration store')
    tag = params['tag']
    root = build_dir(settings, tag)
    if shutil.disk_usage(root).free < 10 * 1024**3:
        raise ValueError('Calibration workspace requires at least 10 GiB free disk space')
    if sha256_file(root / 'request.json') != params.get('request_sha256'):
        raise ValueError('Candidate recipe changed after submission')
    record = request_record(settings, tag)
    action = params['action']
    selection = params['selection']
    identity = params['action_id']
    import re
    if not re.fullmatch('[0-9a-f]{32}', identity):
        raise ValueError('Invalid action identity')
    dest = root / 'actions' / identity
    dest.mkdir(parents=True, exist_ok=False)
    if record.get('reference') and sha256_file(record['reference']) != record['reference_sha256']:
        raise ValueError('Recorded reference calibration changed on disk')
    if not record.get('imported'):
        if sha256_file(root / 'selected_layout.csv') != record['selected_layout_sha256'] or sha256_file(root / 'layout_snapshot.csv') != record['layout']['sha256']:
            raise ValueError('Frozen layout changed on disk')
    if action != 'solve':
        selection = action_params(selection, record)
    print(f'[{tag}] {action}: {json.dumps(selection)}', flush=True)
    from importlib.util import find_spec
    modules = ('casm_monitor.jobs.calibration_workspace', 'casm_monitor.jobs.calibration_export', 'casm_vis_analysis.calibration_checks',
               'casm_vis_analysis.beam_power', 'casm_vis_analysis.fringe_stop',
               'casm_vis_analysis.delay', 'casm_vis_analysis.calibration_inspection',
               'casm_vis_analysis.calibration_transit',
               'bf_weights_generator.make_cal_and_weights')
    code = {}
    for module in modules:
        path = find_spec(module).origin
        code[module] = dict(path=path, sha256=sha256_file(path))
    started = time.time()
    if action == 'export':
        from .calibration_export import export_product
        output = export_product(settings, root, dest, record, selection)
    elif action in {'solve', 'grid'}:
        output = build_product(settings, root, dest, record, action, selection)
    else:
        output = diagnose(settings, root, dest, record, action, selection)
    result = dict(action=action, action_id=identity, selection=selection, result=output,
                  code=code,
                  finished=datetime.now(timezone.utc).isoformat(), elapsed_s=round(time.time() - started, 1))
    save_json(dest / 'result.json', result)
    return result


def build_product(settings, root, dest, record, action, selection):
    from bf_weights_generator.make_cal_and_weights import RecipeParams, run as run_recipe
    from .cal_build import validate
    values = record['params']
    for key in ('source_window', 'static_window'):
        if values.get(key):
            analysis_window(values[key], key)
    snapshot = root / 'selected_layout.csv'
    previous = read_json(root / 'summary.json') or {}
    if action == 'solve':
        validate(values, dataclasses.replace(settings, snap_layout_csv=snapshot))
    elif record.get('imported') or previous.get('has_weights'):
        raise ValueError('Grid generation requires a fresh workspace solve')
    recipe = dict(out_dir=str(dest), tag=values['tag'], cal_source='sun',
                  antennas=values['antennas'], ref_ant=values['ref_ant'], layout_csv=str(snapshot),
                  make_weights=action == 'grid', grid_mode='exact', n_beams=512,
                  diagnostics=True, notebook=True, execute_notebook=True,
                  prev_cal_path=record.get('reference'), nearest_sources=('sun', 'cyg-a', 'cas-a'),
                  transit_sources=('sun', 'cyg-a', 'cas-a', 'tau-a'),
                  nearest_date=values['source_window'][0][:10], transit_date=values['source_window'][0][:10])
    if action == 'solve':
        recipe.update(source_window=values['source_window'], static_window=values['static_window'])
    else:
        recipe.update(cal_path=previous['paths']['cal_h5'], alt_min_deg=selection['alt_min'])
    save_json(dest / 'recipe.json', recipe)
    output = run_recipe(RecipeParams(**recipe))
    ib_path = None
    if output.get('weights_file'):
        ib_path = str(generate_ib_mask(output['weights_file'], dest, values['tag'], len(values['antennas']),
                      settings.cal_ib_generator_script, settings.cal_ib_generator_sha256, settings.cal_ib_generator_script))
    report = output.get('report') or {}
    numbers = {**previous.get('numbers', {}), **_numbers(report)}
    summary = dict(tag=values['tag'], kind='calibration_workspace', params=values, source='sun',
                   source_window=values['source_window'], static_window=values['static_window'],
                   antennas=values['antennas'], n_ant=len(values['antennas']), ref_ant=values['ref_ant'],
                   created_utc=record['created'], finished_utc=datetime.now(timezone.utc).isoformat(),
                   layout=record['layout'], selected_layout_sha256=record['selected_layout_sha256'],
                   grid_mode='exact', n_beams=512, prev_cal_path=record.get('reference'),
                   numbers=numbers, notebook_executed=notebook_executed(output.get('notebook')),
                   rank1_median=(report.get('cal') or {}).get('rank1_median', previous.get('rank1_median')),
                   has_weights=bool(output.get('weights_file') and ib_path),
                   paths=dict(out_dir=str(root), cal_h5=output['cal_file'], weights_h5=output.get('weights_file'),
                              ib_h5=ib_path, report_json=output.get('report_json'), notebook=output.get('notebook'),
                              layout_snapshot=str(root / 'layout_snapshot.csv')))
    save_json(root / 'summary.json', summary)
    if action == 'solve':
        summary['inspection'] = diagnose(settings, root, dest, record, 'inspect',
            dict(source='sun', window=values['source_window'], static=bool(values.get('static_window'))))
        save_json(root / 'summary.json', summary)
    return summary


def notebook_executed(path):
    if not path or not Path(path).is_file():
        return False
    notebook = json.loads(Path(path).read_text())
    cells = [cell for cell in notebook.get('cells', []) if cell.get('cell_type') == 'code']
    return bool(cells) and all(cell.get('execution_count') is not None and not any(
        output.get('output_type') == 'error' or 'MISSING FIGURE:' in str(output.get('text', ''))
        for output in cell.get('outputs', [])) for cell in cells)


def diagnose(settings, root, dest, record, action, selection):
    import numpy as np
    from bf_weights_generator import load_calibration_weights
    from casm_vis_analysis.calibration_checks import read_selected, render_phase, stationary_check, transit_center
    summary = read_json(root / 'summary.json') or {}
    cal = load_calibration_weights(summary['paths']['cal_h5'])
    values = record['params']
    def add_comparison(calibrations):
        if not selection.get('compare_subtraction'):
            return
        from ..web.calibration_variants import family, mode
        comparison = family(settings, values['tag'])
        key = 'regular' if mode(record) == 'static' else 'static'
        other = comparison['versions'].get(key)
        if not other or not other['ready']:
            raise ValueError('Prepare both subtraction solutions before running this comparison')
        saved = read_json(build_dir(settings, other['tag']) / 'summary.json')
        calibrations['without subtraction' if key == 'regular' else 'with subtraction'] = load_calibration_weights(saved['paths']['cal_h5'])
    if record.get('imported'):
        from ..web.science import select_layout
        day = selection.get('date', selection.get('window', values['source_window'])[0][:10])
        center = datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()
        layout = select_layout(center, center + 86399)['path']
    else:
        layout = str(root / 'selected_layout.csv')
    if action == 'inspect':
        from casm_vis_analysis.calibration_inspection import save_inspection
        from ..web.science import geometry
        data, mapping, evidence = read_selected(settings.vis_dir, layout, values['antennas'], selection['window'])
        background = None
        night = record.get('night_window') or values.get('static_window')
        if night:
            background, _, night_evidence = read_selected(settings.vis_dir, layout, values['antennas'], night)
            evidence['night'] = night_evidence
        previous = load_calibration_weights(record['reference']) if record.get('reference') else None
        output = save_inspection(data, mapping, cal, dest / 'inspection', source=selection['source'],
            background=background, reference_cal=previous, subtract_static=selection['static'])
        inputs, _ = geometry(layout)
        output.update(inputs=[dict(item, beamforming=item['antenna'] in values['antennas']) for item in inputs], evidence=evidence)
        save_json(dest / 'inspection' / 'inspection.json', output)
        return output
    if action == 'phase':
        antennas = sorted([values['ref_ant'], *selection['targets']])
        data, mapping, evidence = read_selected(settings.vis_dir, layout, antennas, selection['window'])
        static = None
        if selection['static'] and values.get('static_window'):
            background, _, static_evidence = read_selected(settings.vis_dir, layout, antennas, values['static_window'])
            if not np.array_equal(data['freq_mhz'], background['freq_mhz']):
                raise ValueError('Static and source frequency axes differ')
            static = background['vis'].mean(axis=0)
            evidence['static'] = static_evidence
        flags = np.asarray(cal.flags, dtype=bool)
        cal_freq = np.asarray(cal.frequencies_hz) / 1e6
        if cal_freq[0] < cal_freq[-1]:
            cal_freq, flags = cal_freq[::-1], flags[::-1]
        if cal_freq.shape != data['freq_mhz'].shape or not np.allclose(cal_freq, data['freq_mhz'], atol=1e-5, rtol=0):
            raise ValueError('Calibration and data channels differ')
        data['freq_mask'] = ~flags
        return dict(**render_phase(data, mapping, values['ref_ant'], selection['source'], dest, static), evidence=evidence,
                    layout=layout, static_used=static is not None)
    if action == 'transit':
        calibrations = {'candidate': cal}
        add_comparison(calibrations)
        from ..web.calibration_history import current_reference
        deployed = current_reference(settings)
        if selection['compare_reference']:
            if not deployed['sha256']:
                raise ValueError('Current deployed weights are unavailable')
            calibrations['Current deployed weights'] = load_calibration_weights(deployed['path'])
        antennas = sorted(set.intersection(*(set(map(int, item.ant_ids)) for item in calibrations.values())))
        center = transit_center(selection['source'], selection['date'])
        window = [datetime.fromtimestamp(center + offset, timezone.utc).strftime('%Y-%m-%d %H:%M:%S') for offset in (-9000, 9000)]
        if center + 9000 > time.time():
            raise ValueError('The full five-hour transit must be in the past')
        data, mapping, evidence = read_selected(settings.vis_dir, layout, antennas, window)
        output = stationary_check(data, mapping, calibrations, selection['source'], center, dest,
                                  band=(selection['fmin'], selection['fmax']), off_minutes=selection['off_minutes'],
                                  minimum_snr=selection['minimum_snr'])
        if selection.get('compare_amplitudes'):
            from ..web.calibration_inspection import latest_inspection
            from casm_vis_analysis.calibration_inspection import compare_amplitude_transit
            inspection, _ = latest_inspection(root, None if record.get('imported') else bool(values.get('static_window')))
            output['amplitude_comparison'] = compare_amplitude_transit(data, mapping, cal,
                inspection / 'antennas.npz', selection['source'], center, dest,
                band=(selection['fmin'], selection['fmax']), off_minutes=selection['off_minutes'])
        return dict(**output, evidence=evidence, layout=layout, membership_mode='common antennas', deployed_reference=deployed,
                    self_check=selection['source'] == values['source'])
    if action == 'history':
        from bf_weights_generator.sawtooth_compare import sun_peak_window
        from casm_vis_analysis.calibration_inspection import render_history
        from ..web.calibration_history import current_reference
        calibrations = {'candidate': cal}
        deployed = current_reference(settings)
        if not deployed['sha256']:
            raise ValueError('Current deployed weights are unavailable')
        calibrations['reference'] = load_calibration_weights(deployed['path'])
        common = sorted(set.intersection(*(set(map(int, c.ant_ids)) for c in calibrations.values())))
        rows, datasets = [], []
        for offset in range(selection['days']):
            day = (datetime.fromisoformat(selection['date']) - timedelta(days=offset)).strftime('%Y-%m-%d')
            start, end, _ = sun_peak_window(day, selection['minutes'])
            data, mapping, evidence = read_selected(settings.vis_dir, layout, common,
                [start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')])
            datasets.append((day, data, mapping))
            rows.append(dict(date=day, evidence=evidence, static='None: no different-day template reused'))
        label = 'With static subtraction' if values.get('static_window') else 'Without static subtraction'
        output = render_history(datasets, calibrations, selection['ref_ant'], dest,
            labels=dict(candidate_label=label, reference_weights_label='Current deployed weights'))
        return dict(**output, evidence=rows, deployed_reference=deployed,
                    note='Selected solution and current deployed weights on common antennas and channels.')
    raise ValueError('Unknown diagnostic')
