"""Small, cached sky illustration of saved beam coordinates, never a grid solver."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..cal_defaults import deployed_product
from ..observation import read_json
from ..jobs.cal_build import build_dir
from .calibration_planning import LOCAL

_RENDER_LOCK = threading.Lock()
STYLE_VERSION = 3


def product(settings, tag):
    root = build_dir(settings, tag)
    if not (root / 'request.json').is_file():
        raise ValueError('Unknown calibration candidate')
    summary = read_json(root / 'summary.json') or {}
    candidate = summary.get('paths', {}).get('weights_h5')
    if candidate:
        path = Path(candidate)
        if path.is_symlink() or root.resolve() not in path.resolve().parents:
            raise ValueError('Candidate weights must stay inside this calibration revision')
        return path, 'Candidate grid'
    recorded = deployed_product(settings).get('weights_file')
    if not recorded:
        raise ValueError('Generate beams first; no recorded grid is available')
    return Path(recorded), 'Last uploaded grid · reference only'


def render_schematic(weights, output, day):
    """Reuse the legacy ellipse branch; deliberately omit hit/coverage modelling.

    No call to the legacy ellipse hit finder, exact coverage-map cache or writer.
    The source positions are astropy tracks, not inferred from ellipse crossings.
    """
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import numpy as np
    from bf_weights_generator.plot_transit import (
        _compute_source_track, _load_beams_from_hdf5, _plot_two_panel,
    )

    local_start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=LOCAL)
    local_end = local_start + timedelta(days=1)
    start, end = (value.astimezone(timezone.utc) for value in (local_start, local_end))
    zones = dict.fromkeys([local_start.tzname(), (local_end - timedelta(seconds=1)).tzname()])
    time_label = f"OVRO local time ({'/'.join(zones)})"
    alt, az, _, ew, ns = _load_beams_from_hdf5(weights)
    if len(alt) != 512 or len(az) != 512 or not np.isfinite([*alt, *az, ew, ns]).all():
        raise ValueError('Expected 512 finite beam pointings')
    tracks = []
    for source, name, color in [('sun', 'Sun', '#c66615'), ('cyg-a', 'Cyg A', '#7046ad'),
                                 ('cas-a', 'Cas A', '#2463a3'), ('tau-a', 'Tau A', '#b33850')]:
        times, source_alt, source_az = _compute_source_track(source, start, end, dt_minutes=4)
        tracks.append(dict(name=name, times=times, alt=source_alt, az=source_az, hits=[], color=color))
    with plt.style.context(['default', {'font.size': 12, 'axes.labelsize': 13, 'axes.titlesize': 15}]):
        figure = _plot_two_panel(tracks, alt, az, ew, ns,
                                 f'512 beams · source tracks · {day} · {time_label}', LOCAL)
        sky, elevation = figure.axes
        for patch in sky.patches:
            patch.set(facecolor='#9272be', edgecolor='#644492', alpha=.20, linewidth=.45)
        for text in list(sky.texts):
            if 'hit ellipses' in text.get_text():
                text.remove()
            elif text.get_text() in {'N', 'S', 'E', 'W'}:
                text.remove()
        for name, x, y in [('N', .5, .96), ('S', .5, .04), ('E', .96, .5), ('W', .04, .5)]:
            sky.text(x, y, name, transform=sky.transAxes, ha='center', va='center',
                     fontsize=12, weight='bold', bbox=dict(facecolor='white', edgecolor='none', alpha=.8))
        # The legacy helper labels a FWHM-expanded altitude band "coverage".
        # Replace it with the actual pointing range, without a coverage claim.
        for patch in list(elevation.patches):
            patch.remove()
        elevation.axhspan(float(np.min(alt)), float(np.max(alt)), color='#64748b', alpha=.07,
                          label='Pointing elevations')
        sky.set(xlabel='West ← projected zenith angle (°) → East',
                ylabel='South ← projected zenith angle (°) → North', title='Beam ellipses & sky tracks')
        elevation.set(xlim=(start, end), xlabel=time_label, ylabel='Altitude (°)')
        elevation.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3), tz=LOCAL))
        elevation.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=LOCAL))
        elevation.tick_params(axis='x', rotation=0)
        sky.tick_params(axis='x', rotation=0)
        elevation.legend(loc='upper right', fontsize=11)
        for track in tracks:
            visible = np.flatnonzero(track['alt'] > max(0, float(np.min(alt))))
            if visible.size > 4:
                index = visible[len(visible) // 3]
                take = np.array([index, index + 2])
                radius = 90 - track['alt'][take]
                angle = np.deg2rad(track['az'][take])
                x, y = radius * np.sin(angle), radius * np.cos(angle)
                sky.annotate('', xy=(x[1], y[1]), xytext=(x[0], y[0]),
                             arrowprops=dict(arrowstyle='->', color=track['color'], lw=2), zorder=20)
        figure.tight_layout()
        figure.savefig(output, dpi=115, facecolor='white', bbox_inches='tight')
        plt.close(figure)
    return dict(beam_count=len(alt), ellipse_fwhm_deg=[float(ew), float(ns)],
                time_zone=LOCAL.key, time_label=time_label,
                window_utc=[start.isoformat(), end.isoformat()],
                window_local=[local_start.isoformat(), local_end.isoformat()],
                ellipse_frequency_mhz=450, model='Schematic ellipses; exact saved pointings and astropy tracks')


def sky_view(settings, tag, day):
    datetime.strptime(day, '%Y-%m-%d')
    weights, label = product(settings, tag)
    stat = weights.stat()
    identity = hashlib.sha256(json.dumps([STYLE_VERSION, str(weights.resolve()), stat.st_size,
        stat.st_mtime_ns, day], sort_keys=True).encode()).hexdigest()[:24]
    root = Path(settings.store_root) / 'sky_views'
    root.mkdir(parents=True, exist_ok=True)
    png, metadata = root / f'{identity}.png', root / f'{identity}.json'
    with _RENDER_LOCK:
        if not png.is_file() or not metadata.is_file():
            info = render_schematic(weights, png, day)
            metadata.write_text(json.dumps(info))
        info = json.loads(metadata.read_text())
    return dict(**info, date=day, product_label=label, weights_file=str(weights),
                url=f'/api/calibration/sky/{identity}.png')
