"""Bounded audit of saved SNAP features and their API representation. No hardware.

Reads at most four 12x4096 shards per antenna board, selected from real stored
timestamps. Output is an evidence report, not a diagnosis of the signal source.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import numpy as np

from casm_monitor.config import load_settings
from casm_monitor.store import ShardReader, Store
from casm_monitor.web.snaps import board_table
from casm_monitor.web.snapread import freq_mhz
from casm_monitor.web.snap_spectra import power_db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    settings = load_settings()
    store = Store(settings.db_path, store_root=settings.store_root, read_only=True)
    shards = ShardReader(store)
    # Explicit loopback GET, without an outbound HTTP proxy.
    with build_opener(ProxyHandler({})).open('http://127.0.0.1:8061/api/snap-workspace/spectra', timeout=30) as response:
        api = json.load(response)
    freq = np.asarray(freq_mhz())
    assert np.array_equal(freq, api['freq_mhz'])
    band = (freq >= 485) & (freq <= 495)
    side = ((freq >= 480) & (freq < 485)) | ((freq > 495) & (freq <= 498))
    rows_out = []
    for board in board_table(settings.snap_layout_csv, settings.snap_map_csv):
        if board['role'] != 'antenna':
            continue
        rows = store.query("SELECT id,t0,path,json_extract(meta,'$.eq_epoch') epoch FROM shards "
                           "WHERE stream='snap_read' AND json_extract(meta,'$.ip')=? ORDER BY t0", (board['ip'],))
        if not rows:
            continue
        targets = {0, len(rows)-1, max(0,len(rows)-2),
                   min(range(len(rows)), key=lambda k: abs(rows[k]['t0']-(rows[-1]['t0']-86400)))}
        displayed = next(b for b in api['boards'] if b['ip'] == board['ip'])
        for k in sorted(targets):
            row = rows[k]
            data, _ = shards.load(row['id'])
            db = power_db(data)
            if row['id'] == displayed['shard_id']:
                view = np.asarray(displayed['spectra_db'],dtype=float)
                np.testing.assert_allclose(db, view, rtol=0, atol=0.000051, equal_nan=True)
            for inp in board['inputs']:
                if not inp['functional']:
                    continue
                v = db[inp['adc']]
                if not np.isfinite(v[band]).any():
                    continue
                ch = int(np.flatnonzero(band)[np.nanargmax(v[band])])
                floor = float(np.nanmedian(v[side]))
                rows_out.append(dict(station=inp['station'],snap=board['feng_id'],adc=inp['adc'],
                    ts=row['t0'],utc=datetime.fromtimestamp(row['t0'],timezone.utc).isoformat(),
                    shard_id=row['id'],path=row['path'],eq_epoch=row['epoch'],
                    latest_displayed=row['id']==displayed['shard_id'],channel=ch,
                    peak_mhz=float(freq[ch]),native_peak_power=float(data[inp['adc'],ch]),
                    peak_db_native=float(v[ch]),side_median_db_native=floor,
                    peak_excess_db=float(v[ch]-floor),
                    band_median_excess_db=float(np.nanmedian(v[band])-floor),
                    channels_over_side_by_6db=int(np.count_nonzero(v[band]>floor+6))))
    report = dict(checked_utc=datetime.now(timezone.utc).isoformat(),
        band_mhz=[485,495],sidebands_mhz=[[480,485],[495,498]],
        api_matches_native_shards=True,hardware_contact=False,rows=rows_out)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({**report,'rows':[r for r in rows_out if r['station'] in ('N11E1','N11E2','N11E5','N16E1','N16E5','N16E6')]},indent=2))
    store.close()


if __name__ == '__main__':
    main()
