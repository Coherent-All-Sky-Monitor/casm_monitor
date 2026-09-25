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


def test_hourly_attempt_supersedes_manual_even_when_unknown(tmp_path):
    import json
    from casm_monitor.web.snap_spectra import timing_evidence
    settings=SimpleNamespace(observation_cache_root=tmp_path)
    target=tmp_path/'snap_timing'/'latest.json'
    target.parent.mkdir()
    manual=dict(version=1,ts=100,boards={'a':dict(state='ok')})
    target.write_text(json.dumps(manual))
    hourly=dict(version=1,ts=110,boards={'a':dict(state='unknown')})
    assert timing_evidence(settings,SimpleNamespace(get_watermark=lambda *a:hourly))==hourly
    assert timing_evidence(settings,SimpleNamespace(get_watermark=lambda *a:{'bad':True}))==manual


def test_hourly_collection_covers_only_authorized_boards(monkeypatch):
    from casm_monitor.jobs import snap_timing as st
    report=dict(version=1,ts=100,reference_ip='a',boards={'a':dict(state='ok'),'b':dict(state='attention')})
    monkeypatch.setattr(st,'antenna_ips',lambda _:['a','b'])
    calls=[]
    def read(settings,ips,renew):
        assert renew()
        calls.append(ips)
        return report
    monkeypatch.setattr(st,'read_remote',read)
    monkeypatch.setattr(st,'renew_lock',lambda store,token:token=='owned')
    saved={}
    store=SimpleNamespace(set_watermark=lambda stream,key,value:saved.update({key:value}),put_scalar=lambda *a:None)
    assert st.collect(None,store,['a'],'owned') is None
    assert not calls and not saved
    assert st.collect(None,store,['a','b','relay'],'owned')==report
    assert calls==[['a','b']] and saved['pps_timing']==report
    def fail(*a):raise RuntimeError('read unavailable')
    monkeypatch.setattr(st,'read_remote',fail)
    failed=st.collect(None,store,['a','b'],'owned')
    assert all(b['state']=='unknown' for b in failed['boards'].values())
    assert saved['pps_timing']==failed


def test_timing_remote_lease_gate_never_starts_ssh(monkeypatch):
    import pytest
    from casm_monitor.jobs import snap_timing as st
    def forbidden(*a,**kw):raise AssertionError('SSH was started without a lease')
    monkeypatch.setattr(st.subprocess,'Popen',forbidden)
    with pytest.raises(RuntimeError,match='lease lost'):
        st.read_remote(SimpleNamespace(zapdos_ssh='unused'),['a','b'],lambda:False)


def baseline_boards(**changes):
    return [dict(ip=ip,pps_status=dict(state='ok' if ip=='a' else 'attention',
            fresh=True, period_ok=True, reference_ip='a', delta_ticks=offset,
            detail='PPS advancing' if ip=='a' else 'TT offset -1 ticks', **changes))
            for ip,offset in [('a','0'),('b','-1')]]


def accepted_baseline():
    return dict(version=1,accepted_at=100,reference_ip='a',offsets={'a':'0','b':'-1'})


def test_baseline_stable_offset_green_but_strict_alignment_unchanged():
    from casm_monitor.web.snap_timing_status import apply_baseline
    boards=baseline_boards()
    apply_baseline(boards,accepted_baseline())
    assert all(b['timing_status']['state']=='ok' for b in boards)
    assert boards[1]['pps_status']['state']=='attention'
    assert boards[1]['timing_status']['baseline_delta_ticks']=='-1'


def test_changed_offset_red_and_baseline_never_follows_it():
    from casm_monitor.web.snap_timing_status import apply_baseline
    boards=baseline_boards()
    boards[1]['pps_status'].update(delta_ticks='-2')
    baseline=accepted_baseline()
    apply_baseline(boards,baseline)
    assert boards[1]['timing_status']['state']=='attention'
    assert '-1 → -2' in boards[1]['timing_status']['detail']
    assert baseline['offsets']['b']=='-1'


def test_stale_failed_stalled_and_bad_period_never_baseline_green():
    from casm_monitor.web.snap_timing_status import apply_baseline
    for change in [dict(fresh=False,state='unknown'),dict(state='unknown'),
                   dict(detail='PPS / telescope time did not advance normally'),
                   dict(period_ok=False)]:
        boards=baseline_boards()
        boards[1]['pps_status'].update(change)
        apply_baseline(boards,accepted_baseline())
        assert boards[1]['timing_status']['state']!='ok'


def test_bad_baseline_unknown_and_changed_reference_attention():
    from casm_monitor.web.snap_timing_status import apply_baseline
    for baseline in [None,{},dict(accepted_baseline(),accepted_at=None),
                     dict(accepted_baseline(),accepted_at=float('nan')),
                     dict(accepted_baseline(),offsets={'a':'0','b':False}),
                     dict(accepted_baseline(),offsets={'a':'1','b':'-1'}),
                     dict(accepted_baseline(),offsets={'a':'0','b':'unknown'})]:
        boards=baseline_boards()
        apply_baseline(boards,baseline)
        assert all(b['timing_status']['state']=='unknown' for b in boards)
    boards=baseline_boards()
    boards[1]['pps_status']['reference_ip']='b'
    apply_baseline(boards,accepted_baseline())
    assert boards[1]['timing_status']['state']=='attention'


def test_acceptance_is_explicit_and_rejects_bad_evidence(monkeypatch):
    import pytest
    from casm_monitor.jobs import snap_timing as st
    monkeypatch.setattr(st,'antenna_ips',lambda _:['a','b'])
    monkeypatch.setattr(st.time,'time',lambda:110)
    report=dict(version=1,ts=100,reference_ip='a',boards={b['ip']:b['pps_status'] for b in baseline_boards()})
    writes=[]
    store=SimpleNamespace(get_watermark=lambda *a:report,set_watermark=lambda *a:writes.append(a))
    baseline=st.accept_baseline(None,store)
    assert baseline['offsets']=={'a':'0','b':'-1'}
    assert writes==[('snap_read','pps_baseline',baseline)]
    report['boards']['b']['detail']='PPS / telescope time did not advance normally'
    with pytest.raises(ValueError,match='Cannot accept'):
        st.accept_baseline(None,store)
    report['ts']=-10000
    with pytest.raises(ValueError,match='fresh complete'):
        st.accept_baseline(None,store)
    assert len(writes)==1
