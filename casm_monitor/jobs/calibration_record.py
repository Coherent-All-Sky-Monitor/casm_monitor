"""Portable human record and ordered replay of the canonical calibration recipe.

Building this notebook only reads saved products. It executes no analysis jobs.
"""
import base64
import json
from pathlib import Path


def write_record(path, record, summary, files, settings):
    import nbformat as nb
    notebook = nb.v4.new_notebook()
    cells = notebook.cells
    md = lambda text: cells.append(nb.v4.new_markdown_cell(text))
    code = lambda text: cells.append(nb.v4.new_code_cell(text))
    md(f"# CASM calibration record · {summary['tag']}\n\n"
       "The first section preserves the original results. The replay section reruns "
       "the canonical recipe with the recorded time ranges and antenna selection, then "
       "writes a new CB/IB pair to a fresh local directory. It never uploads weights.\n\n"
       "Duplicate this notebook before running replay cells so the original record stays unchanged. "
       "Run with the CASM offline Python environment. Raw visibilities are not copied into "
       "this bundle; replay requires the recorded observations or their archive. "
       "B0329 belongs to the separate filterbank/folding workflow. No new channel mask or delay alarm is applied.\n\n"
       "## Recorded selection\n```json\n" + json.dumps(record, indent=2) + "\n```")
    code("from pathlib import Path\nimport json, hashlib\nfrom IPython.display import Image, display, JSON\n"
         "HERE = Path.cwd().resolve()\n"
         "ROOT = next((p for p in (HERE, *HERE.parents) if (p/'manifest.json').is_file()), None)\n"
         "assert ROOT is not None, 'Open a notebook copy anywhere inside the exported bundle'\n"
         "manifest = json.loads((ROOT / 'manifest.json').read_text())\n"
         "for entry in manifest['files']:\n"
         "    h = hashlib.sha256()\n"
         "    with (ROOT / entry['relative']).open('rb') as stream:\n"
         "        for block in iter(lambda: stream.read(8*1024*1024), b''): h.update(block)\n"
         "    assert h.hexdigest() == entry['sha256'], entry['relative']\n"
         "print('Saved bundle verified; telescope state unchanged.')")
    md("## Original diagnostic figures\n\nThese are the saved solve, inspection, transit and grid results. "
       "Full reports, notebooks, CSVs, static template and diagnostic arrays are alongside them. "
       "The image outputs are embedded so they can be read without the original scratch directory.")
    for entry in files:
        source = Path(entry['source'])
        if source.suffix != '.png':
            continue
        md('### ' + source.stem.replace('_', ' '))
        cell = nb.v4.new_code_cell(f"display(Image(filename=str(ROOT / {entry['relative']!r})))")
        cell.outputs = [nb.v4.new_output('display_data', data={
            'image/png': base64.b64encode(source.read_bytes()).decode('ascii'),
            'text/plain': 'Recorded diagnostic: '+source.name})]
        cells.append(cell)
    recipes = []
    for entry in files:
        if Path(entry['source']).name == 'recipe.json':
            recipes.append(json.loads(Path(entry['source']).read_text()))
    solve = next((r for r in recipes if r.get('source_window') and not r.get('cal_path')), None)
    grid = next((r for r in recipes if r.get('make_weights') and r.get('cal_path')), None)
    md("## Replay · 1. Parameters and fresh output directory\n\n"
       "Run the remaining cells in order to recompute the analysis. This may read several "
       "hours of visibilities. The recorded files above remain unchanged. "
       "A replay uses the installed software; saved reports record the original versions and source hashes.")
    # JSON is loaded rather than pasted as Python, preserving null/true types.
    code("from datetime import datetime, timezone\nfrom uuid import uuid4\n"
         "from bf_weights_generator.make_cal_and_weights import RecipeParams, run as run_recipe\n"
         f"SOLVE = json.loads({json.dumps(solve)!r})\nGRID = json.loads({json.dumps(grid)!r})\n"
         "assert SOLVE and GRID, 'This legacy bundle lacks a solve/grid recipe; inspect its original notebooks.'\n"
         "REPLAY = ROOT / 'replays' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid4().hex[:8])\n"
         "REPLAY.mkdir(parents=True, exist_ok=False)\n"
         "layout = str(ROOT / 'recipe' / 'selected_layout.csv')\n"
         "reference_entry = next((f for f in manifest['files'] if f['role']=='reference_calibration'), None)\n"
         "reference = str(ROOT / reference_entry['relative']) if reference_entry else None\n"
         "for recipe in (SOLVE, GRID):\n"
         "    recipe.update(layout_csv=layout, prev_cal_path=reference, diagnostics=True, notebook=True, execute_notebook=True)\n"
         "    recipe['transit_sources'] = ['sun','cyg-a','cas-a','tau-a']\n"
         "SOLVE.update(out_dir=str(REPLAY/'solve'), cal_path=None, static_path=None, make_weights=False)\n"
         "print('UTC solve:', SOLVE['source_window'], '\\nUTC static:', SOLVE.get('static_window'))\n"
         "print('Antennas:', SOLVE['antennas'], '\\nNew output:', REPLAY)")
    md("## Replay · 2. SVD solve and full diagnostics\n\n"
       "The standard driver reads the selected observations, constructs the optional static "
       "template and solves antenna gains. It saves autocorrelations, raw/fringe-stopped/calibrated "
       "cross phases, fringe waterfalls, rank-1, SVD, gain/delay and static A/B diagnostics.")
    code("solved = run_recipe(RecipeParams(**SOLVE))\n"
         "display(JSON(solved['report']))\n"
         "for png in sorted((REPLAY/'solve'/'figs').rglob('*.png')):\n"
         "    print(png.relative_to(REPLAY)); display(Image(filename=str(png)))")
    md("## Replay · 3. Per-antenna weights and all cross-phase stages\n\n"
       "Rebuild the same saved-array diagnostics used by Solve & inspect, including autos, "
       "each reference-baseline choice, raw/geometric/fringe-stopped/SVD-corrected phase, "
       "amplitude schemes and residual phase. These are arrays, not threshold-based alarms.")
    code("from bf_weights_generator import load_calibration_weights\n"
         "from casm_vis_analysis.calibration_checks import read_selected\n"
         "from casm_vis_analysis.calibration_inspection import save_inspection\n"
         f"VIS_DIR = {str(settings.vis_dir)!r}  # point to the observation archive if moved\n"
         "data, mapping, evidence = read_selected(VIS_DIR, layout, SOLVE['antennas'], SOLVE['source_window'])\n"
         f"NIGHT_WINDOW = {record.get('night_window') or record.get('params', {}).get('static_window')!r}\n"
         "background = read_selected(VIS_DIR, layout, SOLVE['antennas'], NIGHT_WINDOW)[0] if NIGHT_WINDOW else None\n"
         "inspection = save_inspection(data, mapping, load_calibration_weights(solved['cal_file']), REPLAY/'inspection',\n"
         "    source=SOLVE['cal_source'], background=background,\n"
         "    reference_cal=load_calibration_weights(reference) if reference else None,\n"
         "    subtract_static=bool(SOLVE.get('static_window')))\n"
         "display(JSON(inspection))\ndel data, background")
    code("import numpy as np\nimport matplotlib.pyplot as plt\n"
         "with np.load(REPLAY/'inspection'/'antennas.npz') as saved: antenna_data = dict(saved)\n"
         "freq = antenna_data['freq_mhz']\n"
         "for start in range(0, len(inspection['antennas']), 4):\n"
         "    rows = list(range(start, min(start+4, len(inspection['antennas']))))\n"
         "    fig, axes = plt.subplots(len(rows), 4, figsize=(17, 2.8*len(rows)), squeeze=False, layout='constrained')\n"
         "    for row, index in enumerate(rows):\n"
         "        auto = antenna_data['auto'][index]\n"
         "        quantities = [(10*np.log10(np.where(auto>0, auto, np.nan)), 'Auto power (dB)'),\n"
         "            (np.angle(antenna_data['weights'][index]), 'Applied weight phase (rad)'),\n"
         "            (np.abs(antenna_data['weights'][index]), 'Weight amplitude'),\n"
         "            (antenna_data['residual_rms'][index], 'Residual phase RMS (rad)')]\n"
         "        for ax, (values, label) in zip(axes[row], quantities):\n"
         "            ax.plot(freq, values, color='#2459a6', linewidth=1)\n"
         "            ax.set(xlabel='Frequency (MHz)', ylabel=label, title=f\"Antenna {inspection['antennas'][index]}\")\n"
         "            ax.grid(alpha=.2)\n"
         "    plt.show()")
    code("import matplotlib.dates as mdates\n"
         "REFERENCE = SOLVE['ref_ant']  # change to any selected antenna\n"
         "dates = mdates.date2num([datetime.fromtimestamp(t, timezone.utc) for t in inspection['preview_times']])\n"
         "for path in sorted((REPLAY/'inspection').glob('pair_*.npz')):\n"
         "    pair = list(map(int, path.stem.split('_')[1:]))\n"
         "    if REFERENCE not in pair: continue\n"
         "    with np.load(path) as saved: baseline = dict(saved)\n"
         "    fig, axes = plt.subplots(1,4,figsize=(17,3.5),layout='constrained',sharex=True,sharey=True)\n"
         "    for ax, (stage, label) in zip(axes, [('raw','Raw'),('geometric','Geometric'),('fringe','Fringe-stopped'),('svd','SVD-corrected')]):\n"
         "        image = ax.pcolormesh(dates, inspection['preview_freq_mhz'], np.angle(baseline[stage+'_dynamic']).T,\n"
         "            cmap='twilight',vmin=-np.pi,vmax=np.pi,shading='auto',rasterized=True)\n"
         "        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=timezone.utc))\n"
         "        ax.set(xlabel='Time (UTC)',title=label)\n"
         "    axes[0].set_ylabel('Frequency (MHz)')\n"
         "    fig.colorbar(image,ax=axes,label='Phase (rad)')\n"
         "    fig.suptitle(f'Cross phase · antenna {pair[0]} × {pair[1]}')\n"
         "    plt.show()")
    md("## Replay · 4. Generate the recorded beam grid and write CB weights\n\n"
       "Uses the same canonical grid recipe and the calibration just solved. It writes a new "
       "int8 CB file; the exported original is preserved.")
    code("GRID.update(out_dir=str(REPLAY/'grid'), cal_path=solved['cal_file'], make_weights=True)\n"
         "generated = run_recipe(RecipeParams(**GRID))\n"
         "for png in sorted((REPLAY/'grid'/'figs').rglob('*.png')):\n"
         "    display(Image(filename=str(png)))\nprint('CB:', generated['weights_file'])")
    md("## Replay · 5. Write the paired IB weights\n\n"
       "Uses the recorded, checksum-verified IB generator. This completes the local product; "
       "deployment and saving telescope defaults are separate human actions in the monitor.")
    code("from casm_monitor.jobs.cal_build import generate_ib_mask\n"
         "IB_GENERATOR = str(ROOT/'recipe'/'gen_ib_from_cb.py')\n"
         f"IB_GENERATOR_SHA256 = {settings.cal_ib_generator_sha256!r}\n"
         "ib_file = generate_ib_mask(generated['weights_file'], REPLAY/'grid', GRID['tag'], len(SOLVE['antennas']),\n"
         "    IB_GENERATOR, IB_GENERATOR_SHA256, IB_GENERATOR)\n"
         "print('CB:', generated['weights_file'], '\\nIB:', ib_file, '\\nWritten locally. Nothing uploaded.')")
    notebook.metadata.kernelspec = dict(display_name='CASM offline Python', language='python', name='python3')
    nb.validate(notebook)
    nb.write(notebook, path)
