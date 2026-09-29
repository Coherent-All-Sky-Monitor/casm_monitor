import numpy as np
import pytest

from casm_monitor.web.snap_changes import assess


def history(count=12):
    return [dict(id=i+1, t0=i*3600., eq_epoch='a', fft_shift=3,
                 data=np.ones((12,4096), dtype='float32')) for i in range(count)]


def test_stable_and_warmup():
    assert assess(history(), 0)['state'] == 'stable'
    assert assess(history(5), 0)['state'] == 'unknown'
    assert assess([], 0)['state'] == 'unknown'


@pytest.mark.parametrize('kind', ['power', 'shape', 'spike', 'loss'])
def test_first_change_amber_then_persistent_red(kind):
    rows=history()
    def alter(a):
        if kind=='power': a[0] *= 10
        elif kind=='shape': a[0,:1500] *= 4
        elif kind=='spike': a[0,100] *= 100
        else: a[0,:200] = 0
    alter(rows[-1]['data'])
    out=assess(rows, 0)
    assert out['state']=='watch' and out['reference_id'] not in [rows[-1]['id'], rows[-2]['id']]
    alter(rows[-2]['data'])
    out=assess(rows, 0)
    assert out['state']=='attention' and out['confirmed']
    assert assess(rows, 1)['state']=='stable'  # No spill into other ADCs.


def test_opposite_power_changes_and_different_spikes_are_not_persistent():
    rows=history()
    rows[-1]['data'][0] *= 10
    rows[-2]['data'][0] *= .1
    assert assess(rows,0)['state']=='watch'
    rows=history()
    rows[-1]['data'][0,100] = 100
    rows[-2]['data'][0,200] = 100
    assert assess(rows,0)['state']=='watch'


def test_all_zero_is_loss_not_no_positive_power():
    rows=history()
    rows[-1]['data'][0]=0
    rows[-2]['data'][0]=0
    out=assess(rows,0)
    assert out['state']=='attention' and out['power_delta_db'] is None
    assert out['new_zero_channels']==4096


def test_shape_reversal_does_not_confirm_and_red_label_is_confirmed_change():
    rows=history()
    rows[-1]['data'][0,:1000]*=4
    rows[-2]['data'][0,:1000]/=4
    assert assess(rows,0)['state']=='watch'
    rows=history()
    for row in rows[-2:]: row['data'][0,100]=100
    rows[-1]['data'][0]*=10
    out=assess(rows,0)
    assert out['state']=='attention'
    assert out['label']=='New strong spectral peak'
    assert 'Power increased' in out['reasons']
    assert out['peaks'][0]['frequency_mhz']==round(500-100*125/4096,6)


def test_nan_missing_read_and_long_gap_are_unknown():
    rows=history()
    rows[-1]['data'][0,0]=np.nan
    assert assess(rows,0)['state']=='unknown'
    rows[-1]['data']=None
    assert assess(rows,0)['state']=='unknown'
    rows=history()
    rows[-2]['data']=None
    assert assess(rows,0)['state']=='unknown'
    rows=history()
    rows[-1]['t0']+=7200
    assert assess(rows,0)['state']=='unknown'


def test_rapid_manual_reads_do_not_confirm():
    rows=history()
    rows[-1]['t0']=rows[-2]['t0']+300
    for row in rows[-2:]: row['data'][0]*=10
    assert assess(rows,0)['state']=='watch'


def test_epoch_change_and_missing_identity_are_not_green():
    rows=history()
    rows[-1]['eq_epoch']='b'
    assert assess(rows,0)['state']=='config'
    rows[-2]['eq_epoch']='b'
    assert assess(rows,0)['state']=='unknown'
    rows[-1]['eq_epoch']=None
    assert assess(rows,0)['state']=='unknown'


def test_daily_variation_raises_power_limit_and_fixed_bad_channel_is_not_new():
    rows=history(26)
    for i,row in enumerate(rows):
        row['data'] *= 10**((3*np.sin(i*2*np.pi/24))/10)
        row['data'][:,0]=0
    out=assess(rows,0)
    assert out['state']=='stable'
    assert out['level_threshold_db']>6
    assert out['new_zero_channels']==0
