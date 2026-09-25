"""Getter-only timing checks against fake boards; no SSH or hardware."""
from types import SimpleNamespace

from casm_monitor.remote.snap_timing_remote import verify


class Sync:
    def __init__(self, clock, offset=0, frozen=False, unreadable=False):
        self.clock,self.offset,self.frozen,self.unreadable=clock,offset,frozen,unreadable

    def read_uint(self, key):
        assert key=='ext_pps_count'
        if self.unreadable:raise RuntimeError('Access violation')
        return 100 if self.frozen else 100+self.clock[0]

    def get_tt_of_pps(self,wait_for_sync):
        assert wait_for_sync is False
        return 400000000000000000+self.read_uint('ext_pps_count')*250000000+self.offset,self.read_uint('ext_pps_count')

    def period_pps(self):return 250000000


def check(**options):
    clock=[0]
    boards={'a':SimpleNamespace(sync=Sync(clock,**options.get('a',{}))),
            'b':SimpleNamespace(sync=Sync(clock,**options.get('b',{}))),
            'c':SimpleNamespace(sync=Sync(clock,**options.get('c',{})))}
    def advance(_):clock[0]+=1
    return verify(list(boards),connector=boards.__getitem__,sleeper=advance)


def test_aligned_requires_advancing_identical_tt():
    out=check()
    assert all(b['state']=='ok' for b in out['boards'].values())
    assert out['boards']['b']['delta_ticks']=='0'
    assert isinstance(out['boards']['b']['tt_after'],str)  # preserve >53-bit ints


def test_unreadable_peer_does_not_fail_readable_pair():
    out=check(c={'unreadable':True})
    assert out['boards']['a']['state']==out['boards']['b']['state']=='ok'
    assert out['boards']['c']['state']=='unknown'
    assert 'Access violation' in out['boards']['c']['detail']


def test_equal_but_stalled_tt_is_not_green():
    out=check(a={'frozen':True},b={'frozen':True},c={'frozen':True})
    assert all(b['state']!='ok' for b in out['boards'].values())


def test_exact_clock_offset_is_attention():
    out=check(b={'offset':1})
    assert out['boards']['b']['state']=='attention'
    assert out['boards']['b']['delta_ticks']=='1'


def test_reference_alone_cannot_establish_crossboard_alignment():
    out=check(b={'unreadable':True},c={'unreadable':True})
    assert out['boards']['a']['state']=='unknown'


def test_crossed_reference_edge_never_green():
    clock=[0]
    class Crossing(Sync):
        def read_uint(self,key):
            self.clock[0]+=1
            return super().read_uint(key)
    out=verify(['a','b'],connector=lambda _:SimpleNamespace(sync=Crossing(clock)),sleeper=lambda _:None)
    assert all(b['state']=='unknown' for b in out['boards'].values())


def test_saved_timing_expires_and_new_failure_overrides():
    from casm_monitor.web.snap_spectra import apply_timing
    def item():return dict(ip='a',latest_attempt=10,pps_status=dict(period_ok=True))
    report=dict(ts=100,reference_ip='a',boards={'a':dict(state='ok',delta_ticks='0',detail='verified')})
    board=item()
    apply_timing([board],report,101)
    assert board['pps_status']['state']=='ok'
    board=item()
    apply_timing([board],report,5501)
    assert board['pps_status']['state']=='unknown'
    board=item()
    board.update(latest_attempt=101,pps_status=dict(period_ok=None))
    apply_timing([board],report,102)
    assert board['pps_status']['state']=='unknown'


def test_corrupt_or_missing_local_evidence_is_ignored(tmp_path):
    from casm_monitor.web.snap_spectra import timing_evidence
    settings=SimpleNamespace(observation_cache_root=tmp_path)
    assert timing_evidence(settings) is None
    target=tmp_path/'snap_timing'/'latest.json'
    target.parent.mkdir()
    target.write_text('invalid')
    assert timing_evidence(settings) is None
    target.write_text('[]')
    assert timing_evidence(settings) is None
