"""Getter-only PPS evidence, serialized with the spectrum reader's lease."""
from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path

from ..snapmap import all_boards
from .snap_read import LeaseRenewer, kill_process_group, renew_lock

TIMING_KEY = 'pps_timing'
REMOTE_SCRIPT = Path(__file__).resolve().parent.parent / 'remote' / 'snap_timing_remote.py'


def antenna_ips(settings):
    boards = sorted((b for b in all_boards(settings) if b.role == 'antenna'), key=lambda b:b.feng_id)
    if len(boards) < 2 or boards[0].feng_id != 0:
        raise ValueError('PPS comparison requires configured SNAP 0 and at least one peer')
    return [b.ip for b in boards]


def read_remote(settings, ips, lease_renew):
    """One bounded SSH, after spectra, under the same renewable read lease."""
    if not lease_renew():
        raise RuntimeError('SNAP read lease lost before PPS check')
    cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
           settings.zapdos_ssh, 'python3', '-', *ips]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    with LeaseRenewer(lease_renew, lambda:kill_process_group(proc)) as renewer:
        try:
            stdout, stderr = proc.communicate(input=REMOTE_SCRIPT.read_bytes(), timeout=65)
        except subprocess.TimeoutExpired:
            kill_process_group(proc)
            proc.communicate()
            raise RuntimeError('PPS read timed out after 65 seconds')
    if renewer.lost:
        raise RuntimeError('SNAP read lease lost during PPS check')
    if proc.returncode:
        raise RuntimeError(f'PPS SSH failed ({proc.returncode}): {stderr.decode(errors="replace")[:400]}')
    if len(stdout) > 65536:
        raise ValueError('PPS report exceeds size limit')
    report = json.loads(stdout)
    if (not isinstance(report, dict) or report.get('version') != 1
            or report.get('reference_ip') != ips[0]
            or set(report.get('boards', {})) != set(ips)
            or not isinstance(report.get('ts'), (float,int))
            or not math.isfinite(report['ts']) or abs(time.time()-report['ts']) > 120
            or any(not isinstance(b,dict) or b.get('state') not in {'ok','attention','unknown'}
                   for b in report['boards'].values())):
        raise ValueError('Invalid or incomplete PPS response')
    return report


def collect(settings, store, requested_ips, token):
    """Publish a new attempt, including failure; never keep an old green on error.

    Partial manual spectrum requests do not authorize contacting other boards.
    Both scheduled all-board reads and the UI's four-antenna read include PPS.
    """
    ips = antenna_ips(settings)
    if not set(ips) <= set(requested_ips):
        return None
    try:
        report = read_remote(settings, ips, lambda:renew_lock(store, token))
    except Exception as exc:
        report = dict(version=1, ts=time.time(), reference_ip=ips[0],
                      error=str(exc)[:500], boards={ip:dict(state='unknown',
                      detail='PPS check failed: '+str(exc)[:300]) for ip in ips})
    store.set_watermark('snap_read', TIMING_KEY, report)
    store.put_scalar('snap.pps_checked_at', report['ts'])
    store.put_scalar('snap.pps_exact_ok', sum(b['state']=='ok' for b in report['boards'].values()))
    return report


def accept_baseline(settings, store):
    """Explicit operator acceptance of a fresh complete read; never auto-learn."""
    report = store.get_watermark('snap_read', TIMING_KEY)
    ips = antenna_ips(settings)
    if (not isinstance(report,dict) or report.get('version') != 1
            or not isinstance(report.get('ts'), (int, float))
            or not math.isfinite(report['ts']) or report.get('reference_ip') != ips[0]
            or set(report.get('boards',{})) != set(ips)
            or not -60 <= time.time()-report.get('ts',0) <= 5400):
        raise ValueError('Need a fresh complete PPS read before accepting offsets')
    offsets = {}
    for ip, board in report['boards'].items():
        if not (board.get('state') == 'ok' or board.get('state') == 'attention'
                and board.get('detail','').startswith('TT offset ')):
            raise ValueError('Cannot accept unreadable, stalled or inconsistent PPS')
        offsets[ip] = str(int(board['delta_ticks']))
    if offsets[ips[0]] != '0':
        raise ValueError('Reference board must have zero self-offset')
    baseline = dict(version=1, accepted_at=time.time(), measured_at=report['ts'],
                    reference_ip=ips[0], offsets=offsets)
    store.set_watermark('snap_read', 'pps_baseline', baseline)
    return baseline
