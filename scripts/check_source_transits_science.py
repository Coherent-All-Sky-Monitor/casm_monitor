"""Read-only three-integration check against a direct Hermitian quadratic form.

Use existing native shards and the catalogued calibration. No cal/weights build,
archive scan or persistent visibility cache. Report only under /home/casm/scratch.
"""
import json
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import numpy as np

from bf_weights_generator import load_calibration_weights
from casm_io.constants import C_LIGHT_M_S
from casm_io.correlator.mapping import AntennaMapping
from casm_monitor.collectors.vis import STREAM_FULL
from casm_monitor.config import Settings
from casm_monitor.store import Store
from casm_monitor.web.science import select_layout
from casm_monitor.web.vis import VisStore
from casm_vis_analysis.sources import source_enu

URL = 'http://127.0.0.1:8061'
OUT = Path('/home/casm/scratch/casm-observation-preview/source-transits-science.json')


def main():
    opener = build_opener(ProxyHandler({}))
    def get(path):
        with opener.open(URL+path,timeout=120) as response:
            return json.load(response)
    settings = Settings()
    reader = Store(settings.db_path,read_only=True,store_root=settings.store_root)
    report = {}
    try:
        for source in ['sun','cyg_a','cas_a','tau_a']:
            cat = get('/api/sources?q='+source)
            day = cat['transits'][0]['date']
            beam = get(f'/api/sources/transits/{source}/{day}?calibration_id={cat["calibration"]["id"]}')
            stamps = np.array(beam['preview']['time_unix'][:3])
            layout = AntennaMapping.load(select_layout(stamps[0],stamps[-1])['path'])
            cal = load_calibration_weights(cat['calibration']['path'])
            ants = list(map(int,cal.ant_ids))
            packets = [layout.packet_index(a) for a in ants]
            # Read actual packet pairs directly, without the compact mapping
            # helper used by the endpoint. Reconstruct a Hermitian cross matrix.
            pairs = sorted({tuple(sorted((a,b))) for i,a in enumerate(packets) for b in packets[i+1:]})
            z,times,freq,_ = VisStore(settings,reader).series(STREAM_FULL,stamps[0]-.1,stamps[-1]+.1,
                                                           pairs,allow_full_fallback=False)
            np.testing.assert_array_equal(times,stamps)
            matrix = np.zeros((len(times),len(freq),len(ants),len(ants)),complex)
            for i,a in enumerate(packets):
                for j,b in enumerate(packets):
                    if i==j:
                        continue
                    values=z[:,pairs.index(tuple(sorted((a,b)))),:]
                    matrix[:,:,i,j] = values if a<b else values.conj()
            weights,good = cal.weights,cal.flags
            cf=cal.frequencies_hz/1e6
            if cf[0]<cf[-1]:
                cf,weights,good=cf[::-1],weights[:,::-1],good[::-1]
            np.testing.assert_allclose(cf,freq,atol=1e-5,rtol=0)
            good = good & np.isfinite(weights).all(axis=0)
            positions=np.array([layout.dataframe.loc[layout.dataframe.antenna_id==a,['x_m','y_m','z_m']].values[0] for a in ants])
            # One transit direction, held fixed for every integration.
            tau=np.broadcast_to(source_enu(source,np.array([beam['transit_unix']])),(len(times),3))@positions.T/C_LIGHT_M_S
            steering=weights.T[None,:,:]*np.exp(2j*np.pi*freq[None,:,None]*1e6*tau[:,None,:])
            direct=np.einsum('tfi,tfij,tfj->tf',steering,matrix,steering.conj()).real
            direct[:,~good]=np.nan
            assert len(freq)==3072
            expected=np.nanmean(direct.reshape(len(times),128,24),axis=2)
            actual=np.array(beam['preview']['cross_power'][:3],dtype=float)
            np.testing.assert_array_equal(np.isfinite(actual),np.isfinite(expected))
            error=float(np.nanmax(np.abs(actual-expected)))
            scale=float(np.nanmax(np.abs(expected)))
            assert error/max(scale,1)<3e-6,(source,error,scale)
            expected_curve=np.mean(direct[:,good],axis=1)
            actual_curve=np.array(beam['light_curve']['cross_power'][:3],dtype=float)
            np.testing.assert_allclose(actual_curve,expected_curve,rtol=3e-6,atol=scale*3e-6,equal_nan=True)
            assert beam['beam_mode']=='stationary_transit'
            from casm_vis_analysis.calibration_checks import exact_stationary_response
            model_mapping=AntennaMapping(layout.dataframe.loc[layout.dataframe.antenna_id.isin(ants)].copy())
            offsets=np.arange(-7200,7201,60)
            model_freq=freq[good][np.linspace(0,int(good.sum())-1,min(32,int(good.sum()))).astype(int)]
            model=exact_stationary_response(model_mapping,source,beam['transit_unix']+offsets,
                                            model_freq,beam['transit_unix']).mean(axis=1)
            shoulders={}
            for label,sign in [('before',-1),('after',1)]:
                distances=np.abs(offsets[(offsets*sign>0)&(model<=.1)])/60
                shoulders[label]=float(distances.min()) if len(distances) else None
            report[source]=dict(utc_date=day,integrations=len(times),antennas=ants,
                channels=len(freq),preview_bins=128,calibration=cat['calibration']['name'],
                max_abs_error=error,max_abs_expected=scale,scaled_error=error/max(scale,1),
                beam_mode=beam['beam_mode'],
                band_mean_max_abs_error=float(np.nanmax(np.abs(actual_curve-expected_curve))),
                ideal_cross_response_10percent_minutes=shoulders,
                ideal_response_at_window_edges=[float(model[0]),float(model[-1])],
                model_scope='Equal-amplitude exact cross-only array factor at up to 32 good-channel frequencies, no element beam or amplitude taper',
                method='Direct packet-pair Hermitian cross matrix; w.T V conj(w), diagonal zero')
    finally:
        reader.close()
    OUT.write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()
