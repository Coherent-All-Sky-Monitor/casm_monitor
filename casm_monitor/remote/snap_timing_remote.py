"""Bounded getter-only PPS verification; piped to zapdos's Python 3.8.

Uses the same period_pps/get_tt_of_pps getters as multi_snap_config.verify(),
NOT its CLI or _run_sync. No programming, initialization, setters, mux writes,
arming, counter resets or recovery. Two common-edge frames also prove liveness.
"""
import contextlib
import json
import logging
import signal
import sys
import time


@contextlib.contextmanager
def deadline(seconds):
    def expired(*_):
        raise TimeoutError('PPS read deadline exceeded')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def connect(ip):
    from casm_f.snap_fengine import SnapFengine
    return SnapFengine(ip, use_microblaze=True)


def sample(board):
    # count_pps() can swallow read failures as zero in the installed driver.
    # Require direct counter reads around its existing TT getter as well.
    before = board.sync.read_uint('ext_pps_count')
    tt, count = board.sync.get_tt_of_pps(wait_for_sync=False)
    after = board.sync.read_uint('ext_pps_count')
    if before != after or count != after:
        raise ValueError('PPS edge crossed during TT read')
    return dict(tt=int(tt), count=int(count), period=int(board.sync.period_pps()))


def verify(ips, *, connector=connect, sleeper=time.sleep):
    """Compare against ips[0]; unavailable peers stay unknown, never recovered."""
    result = dict(version=1, ts=time.time(), reference_ip=ips[0], boards={})
    handles, frames = {}, []
    for ip in ips:
        result['boards'][ip] = dict(state='unknown', detail='Alignment not measured')
        try:
            with deadline(8):
                handles[ip] = connector(ip)
        except Exception as exc:
            result['boards'][ip]['detail'] = 'Control read unavailable: %s' % exc
    ref = ips[0]
    if ref not in handles:
        return result

    def frame():
        before = sample(handles[ref])
        values = {ref: before}
        for ip, board in list(handles.items()):
            if ip == ref:
                continue
            try:
                values[ip] = sample(board)
            except ValueError:  # An edge-crossing frame is discarded, not a fault.
                continue
            except Exception as exc:
                result['boards'][ip]['detail'] = 'Control read unavailable: %s' % exc
                del handles[ip]  # No repeated probing of a failed interface.
        after = sample(handles[ref])
        if (before['tt'], before['count']) != (after['tt'], after['count']):
            return None
        return values

    try:
        with deadline(10):
            for attempt in range(3):
                if attempt:
                    sleeper(1.1)
                try:
                    current = frame()
                except ValueError:
                    continue
                if current:
                    frames.append(current)
                if len(frames) == 2:
                    break
    except Exception as exc:
        result['error'] = str(exc)
    result['ts'] = time.time()
    if len(frames) != 2:
        for ip in handles:
            result['boards'][ip]['detail'] = 'Could not capture two stable PPS frames'
        return result

    first, last = frames
    for ip in handles:
        out = result['boards'][ip]
        if ip not in first or ip not in last:
            out['detail'] = 'PPS boundary crossed; no matched pair of reads'
            continue
        a, b = first[ip], last[ip]
        advances = (b['count']-a['count']) % (1 << 32)
        delta_a, delta_b = a['tt']-first[ref]['tt'], b['tt']-last[ref]['tt']
        out.update(count_before=a['count'], count_after=b['count'],
                   tt_before=str(a['tt']), tt_after=str(b['tt']),
                   delta_ticks=str(delta_b), period_ticks=b['period'])
        if not (0 < advances <= 5 and b['tt'] > a['tt']):
            out.update(state='attention', detail='PPS / telescope time did not advance normally')
        elif not (abs(b['period']-250000000) <= 250000 and
                  abs((b['tt']-a['tt'])/advances-b['period']) <= 250000):
            out.update(state='attention', detail='PPS period or TT progress outside tolerance')
        elif delta_a != delta_b:
            out['detail'] = 'Inconsistent cross-board TT delta; alignment unverified'
        elif delta_b != 0:
            out.update(state='attention', detail='TT offset %s ticks versus %s' % (delta_b, ref))
        else:
            out.update(state='ok', detail='PPS advancing; TT matches %s (0 ticks)' % ref)
    if result['boards'][ref]['state'] != 'ok':
        for ip in handles:
            if result['boards'][ip]['state'] == 'ok':
                result['boards'][ip].update(state='unknown',detail='Reference PPS progress unverified')
    peers = [ip for ip in handles if ip != ref and result['boards'][ip]['state'] == 'ok']
    if result['boards'][ref]['state'] == 'ok':
        result['boards'][ref].update(state='ok' if peers else 'unknown',
            detail='PPS advancing; matched by %d peer board(s)' % len(peers) if peers else
                   'PPS advancing; no peer alignment verified')
    return result


def main():
    logging.disable(logging.CRITICAL)
    if len(sys.argv) < 3:
        raise SystemExit('usage: python3 - <reference-ip> <peer-ip> [...]')
    # Libraries may print diagnostics; stdout must remain one JSON document.
    with contextlib.redirect_stdout(sys.stderr):
        result = verify(sys.argv[1:])
    print(json.dumps(result))


if __name__ == '__main__':
    main()
