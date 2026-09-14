"""Bounded, reproducible scientific views of already-collected visibility data.

The existing native observation reader is reachable only through an explicitly
selected bounded recording mode. No calibration builder or operational command
is reachable. Plotting and transforms belong to the existing CASM scientific
modules. Only immutable local view artifacts are written here.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .. import vis_ops
from ..collectors import rowmap
from ..collectors.vis import DT_S, STREAM_AVG8, STREAM_FULL, input_table
from ..observation import cache_dir, file_identity, read_json, recorded_product
from .vis import VisStore, input_positions, _shard_meta_times

LAYOUT_ROOT = Path('/home/casm/software/dev/antenna_layouts')
MAX_PAIRS = 6
MAX_CELLS = 12_000_000
MAX_SAMPLES = 4600
PLOT_LOCK = threading.Lock()
VERSION = 1


def code_provenance():
    """Content identities, including editable installed scientific modules."""
    import inspect
    from casm_vis_analysis import fringe_stop, solar_waterfall
    from casm_vis_analysis.plotting import fringe_diag, phase_freq, autocorr
    files = [Path(__file__), Path(__file__).with_name('science_recorded.py'), Path(__file__).with_name('science_transit.py')]
    files.extend(Path(inspect.getfile(m)) for m in [vis_ops, fringe_stop, solar_waterfall, fringe_diag, phase_freq, autocorr])
    import importlib
    files.extend(Path(inspect.getfile(importlib.import_module(name))) for name in [
        'casm_vis_analysis.beam_power','bf_weights_generator.plot_transit',
        'bf_weights_generator.snap_weights','casm_io.correlator.reader'])
    return [{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]


class ScienceRequest(BaseModel):
    pairs: list[tuple[int, int]] = Field(min_length=1, max_length=MAX_PAIRS)
    t0: float
    t1: float
    fmin: float = 390.0
    fmax: float = 485.0
    kind: Literal['phase_waterfall', 'amplitude_waterfall', 'phase_spectrum', 'amplitude_spectrum', 'autos'] = 'phase_waterfall'
    reference: Literal['raw', 'sun'] = 'sun'
    resolution: Literal['avg8', 'full', 'recorded'] = 'avg8'
    layout_id: str | None = None
    compare_t0: float | None = None
    compare_t1: float | None = None
    time_tz: Literal['America/Los_Angeles', 'UTC'] = 'America/Los_Angeles'

    @field_validator('t0', 't1', 'compare_t0', 'compare_t1', mode='before')
    @classmethod
    def parse_time(cls, value):
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError('ISO timestamps require an explicit timezone')
                return parsed.timestamp()
        return value


def layouts():
    """Only canonical dated layouts, not experimental inclusion variants."""
    out = []
    for p in sorted(LAYOUT_ROOT.glob('casm_antenna_layout_*.csv')):
        match = re.fullmatch(r'casm_antenna_layout_(\d{4}-\d{2}-\d{2})\.csv', p.name)
        if match:
            out.append({'id': p.name, 'date': match[1], 'path': str(p.resolve())})
    return out


def select_layout(t0, t1, layout_id=None):
    entries = layouts()
    eligible = [e for e in entries if datetime.fromisoformat(e['date']).replace(tzinfo=timezone.utc).timestamp() <= t0]
    if not eligible:
        raise HTTPException(400, 'No dated layout covers this interval')
    chosen = eligible[-1]
    if layout_id is not None and layout_id != chosen['id']:
        raise HTTPException(400, 'Layout must match the dated layout epoch of the selected interval')
    if any(t0 < datetime.fromisoformat(e['date']).replace(tzinfo=timezone.utc).timestamp() <= t1 for e in entries):
        raise HTTPException(400, 'Split this selection at the dated layout boundary')
    return chosen


def geometry(path):
    from casm_io.correlator.mapping import AntennaMapping
    mapping = AntennaMapping.load(str(path))
    rows = input_table(path)
    positions = input_positions(path)
    by_input = {}
    for r in rows:
        r = dict(r)
        r['label'] = f"{mapping.format_antenna(r['antenna'])} · {r['station']}"
        r['plank'] = re.sub(r'E\d+$', '', r['station'] or '')
        r['position_enu_m'] = positions.get(r['packet_idx'])
        by_input[r['packet_idx']] = r
    baselines = []
    for i, a in by_input.items():
        for j, b in by_input.items():
            if j <= i or i not in positions or j not in positions:
                continue
            d = np.asarray(positions[j]) - positions[i]
            length = float(np.linalg.norm(d))
            orientation = 'NS' if abs(d[0]) <= .1 * max(abs(d[1]), .001) else 'EW' if abs(d[1]) <= .1 * max(abs(d[0]), .001) else 'oblique'
            baselines.append({'i': i, 'j': j, 'label': f"{length:.2f} m {orientation}: {a['label']} × {b['label']}",
                              'length_m': length, 'ns_m': float(d[1]), 'ew_m': float(d[0]),
                              'orientation': orientation, 'planks': [a['plank'], b['plank']],
                              'same_plank': a['plank'] == b['plank']})
    baselines.sort(key=lambda b: (b['orientation'] != 'NS', -b['length_m']))
    return list(by_input.values()), baselines


def catalog(settings, reader, layout_id=None):
    from .science_transit import calibration_catalog
    path = Path(rowmap.LAYOUT_CSV).resolve()
    if layout_id:
        selected = next((e for e in layouts() if e['id'] == layout_id), None)
        if not selected:
            raise HTTPException(400, 'Unknown canonical dated layout')
        path = Path(selected['path'])
    inputs, baselines = geometry(path)
    span = reader.query('SELECT stream, MIN(t0) AS t0, MAX(t1) AS t1, COUNT(*) AS shards FROM shards WHERE stream IN (?,?) GROUP BY stream', (STREAM_AVG8, STREAM_FULL))
    intended = {r['packet_idx'] for r in inputs if r['in_bf']}
    default_inputs = intended
    preset_source = 'Intended participation; no current matching payload inspection available'
    cached = read_json(cache_dir(settings)/'membership.json')
    product = recorded_product(settings,reader.latest_scalars())
    try:
        matched = (cached.get('inspection_state') == 'complete' and cached.get('product_id') == product.get('product_id')
                   and product.get('path') and cached.get('identity') == file_identity(product['path']))
    except OSError:
        matched = False
    if matched and path == Path(rowmap.LAYOUT_CSV).resolve():
        slots = {p['antenna']:p['slot'] for p in cached.get('positions',[])}
        members = set(cached.get('antennas',[]))
        default_inputs = {r['packet_idx'] for r in inputs if r['antenna'] in members and slots.get(r['antenna']) == r['packet_idx']}
        preset_source = 'Recorded inspected payload union, matching packet identities; not a health verdict'
    defaults = [b for b in baselines if b['i'] in default_inputs and b['j'] in default_inputs][:3]
    return {'inputs': inputs, 'baselines': baselines, 'default_pairs': [[b['i'], b['j']] for b in defaults],
            'layouts': layouts(), 'current_layout': file_identity(path), 'availability': [dict(r) for r in span],
            'transit_calibrations': calibration_catalog(settings),
            'preset_source': preset_source,
            'limits': {'max_pairs': MAX_PAIRS, 'max_span_days': 7, 'full_max_hours': 6, 'max_cells': MAX_CELLS},
            'warnings': ['Cache stores packet identities, not a layout snapshot. Dated layout association is inferred, not proven.',
                         'avg8 contains irreversible complex averaging over eight channels before fringe-stopping.']}


def validate_request(req):
    vals = [req.t0, req.t1, req.fmin, req.fmax]
    if not all(np.isfinite(vals)) or not 0 < req.t1 - req.t0 <= 7 * 86400:
        raise HTTPException(400, 'Select a finite positive interval of at most seven days')
    if not 0 < req.fmin < req.fmax < 1000:
        raise HTTPException(400, 'Frequency bounds must be finite, ordered MHz values')
    if req.resolution == 'full' and req.t1 - req.t0 > 6 * 3600:
        raise HTTPException(400, 'Full cached resolution is limited to six hours per request')
    if req.resolution == 'recorded' and req.t1 - req.t0 > 3600:
        raise HTTPException(400, 'Explicit native recording reads are limited to one hour')
    if len(set(req.pairs)) != len(req.pairs) or any(i > j or i < 0 or j >= 256 for i, j in req.pairs):
        raise HTTPException(400, 'Use distinct ascending packet-index pairs, i <= j')
    if req.kind == 'autos' and any(i != j for i, j in req.pairs):
        raise HTTPException(400, 'Autos require i == j')
    if 'phase' in req.kind and any(i == j for i, j in req.pairs):
        raise HTTPException(400, 'Phase views require cross baselines')
    if (req.compare_t0 is None) != (req.compare_t1 is None):
        raise HTTPException(400, 'Provide both comparison bounds')
    if req.compare_t0 is not None:
        if req.kind != 'phase_spectrum' or req.reference != 'sun':
            raise HTTPException(400, 'Day comparison uses Sun fringe-stopped phase spectra')
        if not np.isfinite([req.compare_t0, req.compare_t1]).all() or not 0 < req.compare_t1 - req.compare_t0 <= 6 * 3600:
            raise HTTPException(400, 'Comparison interval must be positive and no longer than six hours')


def bounded_rows(vstore, stream, t0, t1, pairs):
    rows = vstore.shards.list(stream, t0=t0, t1=t1)
    samples = sum(len(_shard_meta_times(r)) for r in rows)
    channels = {int((r.get('meta') or {}).get('nchan', 0)) for r in rows}
    axes = {((r.get('meta') or {}).get('freq_top_mhz'), (r.get('meta') or {}).get('chan_bw_mhz')) for r in rows}
    if not rows:
        raise HTTPException(404, f'No {stream} cache in selected interval; no raw-data fallback is attempted')
    if len(channels) != 1 or 0 in channels or len(axes) != 1:
        raise HTTPException(409, 'Inconsistent cached frequency axes; split this interval')
    if samples > MAX_SAMPLES or samples * max(channels) * len(pairs) > MAX_CELLS:
        raise HTTPException(400, 'Selection exceeds bounded read budget; shorten time range or select fewer baselines')
    return rows


def load_selection(vstore, req, t0, t1, layout):
    if req.resolution == 'recorded':
        from .science_recorded import read_recorded
        z, stamps, freq, evidence, omitted = read_recorded(vstore.settings, req, t0, t1)
        rows = []
        bad_times = set()
    else:
        stream = STREAM_FULL if req.resolution == 'full' else STREAM_AVG8
        rows = bounded_rows(vstore, stream, t0, t1, req.pairs)
        z, stamps, freq, _ = vstore.series(stream, t0, t1, req.pairs, allow_full_fallback=False)
        evidence = [{'id': r.get('id'), 'path': r.get('path'), 't0': r['t0'], 't1': r['t1'], 'obs': (r.get('meta') or {}).get('obs')} for r in rows]
        omitted = 0
    if not len(stamps):
        raise HTTPException(404, 'No integrations in selected interval')
    # Known first-of-file junk is omitted from the analysis, not normalized away.
    bad_times = set()
    for row in rows:
        meta = row.get('meta') or {}
        ts = _shard_meta_times(row)
        flags = (meta.get('flags') or {}).get('first_of_file', False)
        flags = flags if isinstance(flags, list) else [flags] * len(ts)
        bad_times.update(t for t, bad in zip(ts, flags) if bad)
    keep = np.array([t not in bad_times for t in stamps])
    z, stamps = z[keep], stamps[keep]
    mask = (freq >= req.fmin) & (freq <= req.fmax)
    if mask.sum() < 2 or len(stamps) < 2:
        raise HTTPException(404, 'Need at least two unflagged integrations and two cached channels')
    freq, z = freq[mask], z[:, :, mask]
    if not np.isfinite(z).any():
        raise HTTPException(404, 'Selected baselines are absent or wholly invalid in this cache')
    if np.any(np.diff(stamps) <= 0):
        raise HTTPException(409, 'Duplicate/non-monotonic cached integration timestamps')
    if req.reference == 'sun':
        pos = input_positions(layout['path'])
        inputs = sorted({i for p in req.pairs for i in p})
        if any(i not in pos for i in inputs):
            raise HTTPException(400, 'Selected packet has no position in the matching layout')
        ranks = {p: k for k, p in enumerate(inputs)}
        z = vis_ops.fringe_stop_sun(z, freq, np.asarray([pos[i] for i in inputs]), [(ranks[i], ranks[j]) for i, j in req.pairs], stamps)
    return z, stamps, freq, evidence, len(bad_times) + omitted


def _gap_phase(cube, stamps):
    """Insert absent raster cells rather than painting across missing integrations."""
    times, arrays = [], []
    for k, stamp in enumerate(stamps):
        if k and stamp - stamps[k - 1] > 1.5 * DT_S:
            gap_times = [0.5*(stamps[k-1]+stamp)] if stamp-stamps[k-1] < 3*DT_S else [stamps[k-1]+DT_S,stamp-DT_S]
            times.extend(gap_times)
            arrays.extend([np.full_like(cube[k],np.nan) for _ in gap_times])
        times.append(stamp)
        arrays.append(cube[k])
    return np.asarray(arrays), np.asarray(times)


def draw_views(req, z, stamps, freq, labels, comparison=None):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from casm_vis_analysis.plotting.fringe_diag import plot_fringe_diagnostic
    from casm_vis_analysis.plotting.phase_freq import plot_phase_vs_freq
    from casm_vis_analysis.plotting.autocorr import plot_autocorr
    from casm_vis_analysis.solar_waterfall import plot_dynamic_spectrum
    if z.shape != (len(stamps),len(labels),len(freq)):
        raise HTTPException(409,'Reference transform returned inconsistent time/baseline/frequency axes')
    if comparison is not None and comparison[0].shape != (len(comparison[1]),len(labels),len(comparison[2])):
        raise HTTPException(409,'Comparison transform returned inconsistent axes')
    cube = np.swapaxes(z, 1, 2)
    figs = []
    if req.kind == 'phase_waterfall':
        plotted, plot_times = _gap_phase(cube, stamps)
        from casm_vis_analysis.plotting import format_time_range
        for k,label in enumerate(labels):
            fig = plot_fringe_diagnostic([(req.reference,plotted[:,:,k:k+1])],plot_times,freq,[''],[0],0,time_tz=req.time_tz)[0]
            fig.set_size_inches(12,3.5)
            for text in list(fig.texts):
                text.remove()
            ax = fig.axes[0]
            ax.set_title('')
            ax.set_title(label + ' · ' + ('Sun fringe-stopped' if req.reference == 'sun' else 'Raw'),loc='left',fontsize=11,pad=12)
            ax.set_ylabel('Frequency (MHz)',fontsize=10)
            ax.set_xlabel('Hours since first displayed integration',fontsize=10)
            ax.tick_params(labelsize=9)
            cb = fig.colorbar(ax.collections[0],ax=ax,label='Phase (rad)',pad=.02,fraction=.025)
            cb.set_ticks([-np.pi,0,np.pi],labels=['−π','0','π'])
            fig.text(.075,.985,format_time_range(stamps,req.time_tz),ha='left',va='top',fontsize=9,color='#b6c2ce')
            fig.subplots_adjust(left=.075,right=.94,bottom=.18,top=.81)
            figs.append(fig)
    elif req.kind == 'phase_spectrum':
        # Feed a NaN-aware COMPLEX mean, never an average of phase angles.
        avg = vis_ops._nanmean(cube, axis=0, dtype=np.complex128)[None]
        panels = [(f'Selected day · {req.reference}', avg)]
        if comparison is not None:
            cz, ct, cf = comparison
            panels.append(('Comparison day · Sun', vis_ops._nanmean(np.swapaxes(cz, 1, 2), axis=0, dtype=np.complex128)[None]))
        figs = plot_phase_vs_freq(panels, freq, labels, unwrap=False, time_unix=stamps, time_tz=req.time_tz)
        for fig in figs:
            fig.set_size_inches(12, max(3,2.5*len(labels)))
            if comparison is not None:
                from casm_vis_analysis.plotting import format_time_range
                for text in list(fig.texts):
                    text.remove()
                fig.text(.075,.995,'Selected: '+format_time_range(stamps,req.time_tz),ha='left',va='top',fontsize=8,color='#b6c2ce')
                fig.text(.075,.94,'Comparison: '+format_time_range(comparison[1],req.time_tz),ha='left',va='top',fontsize=8,color='#b6c2ce')
            for n,ax in enumerate(fig.axes):
                row = n//len(panels)
                ax.set_title('')
                ax.set_ylabel('Phase (rad)')
                ax.set_title(labels[row]+' · '+panels[n%len(panels)][0],loc='left',fontsize=9)
                ax.set_ylim(-np.pi,np.pi)
            fig.tight_layout(rect=[0,0,1,.86 if comparison is not None else .95])
    elif req.kind == 'amplitude_waterfall':
        for k, label in enumerate(labels):
            fig = plot_dynamic_spectrum(np.abs(z[:, k]), stamps, freq, title=label + ' · ' + req.reference,
                                        tz=req.time_tz, quantity='|cached V|', integration_s=DT_S)
            figs.append(fig)
    else:
        # Reuse the scientific spectral plotter with explicit linear magnitude;
        # its time mean is performed here NaN-aware, before rendering.
        avg = vis_ops._nanmean(np.abs(cube), axis=0, dtype=np.float64)
        fig = plot_autocorr(avg, freq, labels, time_avg=False, scale='linear', ncols=min(3, len(labels)), time_unix=stamps, time_tz=req.time_tz)
        fig.supylabel('Autocorrelation power (counts)' if req.kind == 'autos' else 'Mean |cached V| (counts)')
        figs = [fig]
    for fig in figs:
        for text in fig.texts:
            if text.get_color() in ('0.3','0.35'):
                text.set_color('#b6c2ce')
    return figs


def render_product(settings, reader, req):
    validate_request(req)
    layout = select_layout(req.t0, req.t1, req.layout_id)
    inputs, baselines = geometry(layout['path'])
    labels_by_input = {r['packet_idx']: r['label'] for r in inputs}
    if any(i not in labels_by_input for p in req.pairs for i in p):
        raise HTTPException(400, 'Selected input is not wired in the matching dated layout')
    hardware_labels = [labels_by_input[i] if i == j else f'{labels_by_input[i]} × {labels_by_input[j]}' for i, j in req.pairs]
    by_packet = {r['packet_idx']:r for r in inputs}
    by_pair = {(b['i'],b['j']):b for b in baselines}
    labels=[]
    for i,j in req.pairs:
        a,b = by_packet[i],by_packet[j]
        def station(r):
            return r.get('station') or f"Input {r['packet_idx']}"
        if i==j:
            labels.append(f"{station(a)} · ant {a.get('antenna',i+1)}")
        else:
            pair=by_pair.get((i,j),{})
            prefix=f"{pair['length_m']:.2f} m {pair['orientation']} · " if pair else ''
            labels.append(prefix+station(a)+' × '+station(b))
    vstore = VisStore(settings, reader)
    stream = 'native_recording' if req.resolution == 'recorded' else STREAM_FULL if req.resolution == 'full' else STREAM_AVG8
    def plan(t0, t1):
        if req.resolution == 'recorded':
            from .science_recorded import recorded_plan
            return recorded_plan(settings,req,t0,t1)[1]
        return bounded_rows(vstore,stream,t0,t1,req.pairs)
    rows = plan(req.t0, req.t1)
    comparison_layout = None
    if req.compare_t0 is not None:
        comparison_layout = select_layout(req.compare_t0, req.compare_t1)
        if comparison_layout['id'] != layout['id']:
            raise HTTPException(400, 'Cross-layout calibration comparison requires explicit identity reconciliation; not supported')
        rows += plan(req.compare_t0, req.compare_t1)
    selection = req.model_dump()
    code = code_provenance()
    identity = {'version': VERSION, 'code': code, 'selection': selection, 'layout': file_identity(layout['path']),
                'shards': [(r.get('id'), r.get('path'), r['t0'], r['t1']) for r in rows]}
    pid = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
    root = cache_dir(settings) / 'science'
    dest = root / pid
    if dest.is_symlink() or dest.resolve().parent != root.resolve():
        raise HTTPException(400,'Unsafe scientific artifact directory')
    manifest = dest / 'metadata.json'
    if manifest.is_file():
        return json.loads(manifest.read_text())
    if not PLOT_LOCK.acquire(blocking=False):
        raise HTTPException(429, 'Another scientific view is rendering; retry shortly')
    try:
        z, stamps, freq, evidence, omitted = load_selection(vstore, req, req.t0, req.t1, layout)
        compare = None
        compare_evidence = []
        if comparison_layout:
            cz, ct, cf, compare_evidence, co = load_selection(vstore, req, req.compare_t0, req.compare_t1, comparison_layout)
            if not np.array_equal(cf, freq):
                raise HTTPException(409, 'Comparison frequency axes differ')
            compare = cz, ct, cf
        dest.mkdir(parents=True, exist_ok=True)
        import matplotlib.pyplot as plt
        with plt.style.context('dark_background'):
            figs = draw_views(req, z, stamps, freq, labels, compare)
            images = []
            for n, fig in enumerate(figs):
                name = f'plot-{n}.png'
                fig.savefig(dest / name, dpi=130, bbox_inches='tight', facecolor='#111820')
                plt.close(fig)
                images.append({'url': f'/api/science/products/{pid}/{name}', 'label': labels[n] if len(figs) == len(labels) else req.kind})
        arrays = dict(vis=z, time_unix=stamps, freq_mhz=freq, pairs=np.asarray(req.pairs))
        if compare:
            arrays.update(compare_vis=compare[0], compare_time_unix=compare[1])
        np.savez_compressed(dest / 'data.npz', **arrays)
        warnings = ['Dated layout association is inferred: original cache contains no layout hash.',
                    'Correlator timestamps are nominal; integration midpoint/end interpretation has not been independently verified for this observation.',
                    'Solar variability and changed baseline phase are evidence to investigate, not authority to deploy weights.']
        if req.resolution == 'avg8':
            warnings.append('Eight native channels were complex-averaged before this view. Fringe cancellation is irreversible; use full cached resolution for detailed phase checks.')
        if compare:
            warnings.append('Comparison uses explicitly selected intervals; equal LST/source geometry and analog state must be reviewed before interpreting drift. No calibration is applied or generated.')
        result = {'id': pid, 'images': images, 'metadata_url': f'/api/science/products/{pid}/metadata.json',
                  'data_url': f'/api/science/products/{pid}/data.npz', 'selection': selection,
                  'provenance': {'code': code, 'baseline_labels':hardware_labels, 'layout': identity['layout'], 'stream': stream, 'source_shards': evidence,
                                 'comparison_shards': compare_evidence, 'samples': len(stamps), 'channels': len(freq),
                                 'actual_t0': float(stamps[0]), 'actual_t1': float(stamps[-1]), 'omitted_known_junk_or_missing': omitted,
                                 'rendered_at': time.time(), 'fringe_sign': -1 if req.reference == 'sun' else None,
                                 'normalization': 'per-channel mean magnitude' if req.kind == 'amplitude_waterfall' else 'none',
                                 'phase_statistic': 'angle of complex mean' if req.kind == 'phase_spectrum' else 'angle per cached integration'},
                  'warnings': warnings}
        temporary = dest / 'metadata.tmp'
        temporary.write_text(json.dumps(result, indent=2))
        temporary.replace(manifest)
        return result
    finally:
        PLOT_LOCK.release()


def build_router(settings, reader):
    from .science_transit import TransitRequest, render_transit
    router = APIRouter(prefix='/api/science', tags=['science'])

    @router.get('/catalog')
    def get_catalog(layout_id: str | None = None):
        return catalog(settings, reader, layout_id)

    @router.post('/render')
    def render(req: ScienceRequest):
        return render_product(settings, reader, req)

    # Register an explicitly annotated callable: postponed local type annotations
    # are otherwise invisible to FastAPI's module-level namespace resolution.
    def transit(req):
        return render_transit(settings, reader, req)
    transit.__annotations__['req'] = TransitRequest
    router.post('/transit')(transit)

    @router.get('/products/{pid}/{filename}')
    def product(pid: str, filename: str):
        if not re.fullmatch(r'[a-f0-9]{32}', pid) or not re.fullmatch(r'(metadata\.json|data\.npz|plot-[0-5]\.png)', filename):
            raise HTTPException(404, 'Unknown artifact')
        root = (cache_dir(settings) / 'science').resolve()
        path = (root / pid / filename).resolve()
        if not path.is_relative_to(root) or not path.is_file() or not (root / pid / 'metadata.json').is_file():
            raise HTTPException(404, 'Artifact unavailable')
        return FileResponse(path, filename=filename)

    return router
