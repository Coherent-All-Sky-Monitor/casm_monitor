"""Read-only, stdlib-only HELLA process/config probe, also sent over SSH.

Only allowlisted search settings are returned. No process environment, sockets,
GPU calls or pipeline writes. A config newer than its process is not readback.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time

KEYS = frozenset("DM_MIN DM_MAX SNR WIDTH_MIN WIDTH_MAX WIDTH_RATIO GULP NBEAM "
                 "FLAG1 FLAG2 SPEC_MIN SPEC_MAX SUMTHRESHOLD BASELINE_WIDTH "
                 "BASELINE_MODE BASELINE_SMOOTHER U8_CLIP CLUSTER_MINPTS DM_TOL "
                 "DM_PULSE_WIDTH SCRUNCH".split())


def parse_config(text):
    """Last assignment wins, including wrapper overrides and scrunch blocks."""
    lines = [s.split('#', 1)[0].strip() for s in text.splitlines()]
    values, scrunches = {}, []
    i = 0
    while i < len(lines):
        parts = lines[i].split(None, 1)
        i += 1
        if not parts or parts[0] not in KEYS:
            continue
        key, value = parts[0], parts[1] if len(parts) == 2 else ''
        values[key] = value
        if key == 'SCRUNCH':
            count = int(value)
            if not 0 <= count <= 64:
                raise ValueError('Invalid SCRUNCH count')
            scrunches = []
            for _ in range(count):
                if i >= len(lines) or len(lines[i].split()) != 4:
                    raise ValueError('Incomplete SCRUNCH block')
                scrunches.append(lines[i])
                i += 1
    return values, scrunches


def inspect_process(pid_path, *, boot_unix, ticks):
    args = (pid_path / 'cmdline').read_bytes().split(b'\0')
    args = [s.decode(errors='replace') for s in args if s]
    if not args or Path(args[0]).name != 'casm_hella_refactored' or '-c' not in args:
        return None
    config_path = Path(args[args.index('-c') + 1])
    match = re.fullmatch(r'/tmp/hella_([0-7])\.cfg', str(config_path))
    if not match:
        return None
    row = {'stream': int(match[1]), 'pid': int(pid_path.name),
           'config_path': str(config_path), 'status': 'unknown'}
    try:
        # stat field 22, after the parenthesised comm field which may contain spaces.
        fields = (pid_path / 'stat').read_text().rsplit(')', 1)[1].split()
        started = boot_unix + int(fields[19]) / ticks
        before = config_path.stat()
        if before.st_size > 65536:
            raise ValueError('Config exceeds 64 KiB')
        values, scrunches = parse_config(config_path.read_text())
        after = config_path.stat()
        executable = os.readlink(pid_path / 'exe')
        row.update(started_unix=started, config_mtime=after.st_mtime,
                   executable=executable, values=values, scrunches=scrunches)
        if (before.st_mtime_ns, before.st_ino) != (after.st_mtime_ns, after.st_ino):
            row['reason'] = 'Configuration changed during read'
        elif after.st_mtime > started + 2:
            row['reason'] = 'Configuration edited after launch; loaded settings unverified'
        elif executable != '/home/casm/software/vishnu/casm_hella_vishnu/build/src/apps/casm_hella_refactored':
            row['reason'] = 'Unrecognised HELLA build; flag semantics unverified'
        else:
            row['status'] = 'running'
        # Ensure this exact process still exists after reading its config.
        if (pid_path / 'stat').read_text().rsplit(')', 1)[1].split()[19] != fields[19]:
            row.update(status='unknown', reason='Process changed during read')
    except (OSError, ValueError, IndexError) as exc:
        row.update(status='unknown', reason=f'Config read failed: {type(exc).__name__}')
    return row


def probe(proc=Path('/proc')):
    # Derive process start using the node's own clock, not corr1's remote clock.
    boot = time.time() - float((proc / 'uptime').read_text().split()[0])
    ticks = os.sysconf('SC_CLK_TCK')
    rows = []
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            row = inspect_process(path, boot_unix=boot, ticks=ticks)
            if row:
                rows.append(row)
        except (OSError, ValueError, IndexError):
            continue  # Unrelated/vanished process, never an assumed running stream.
    return {'streams': sorted(rows, key=lambda s: (s['stream'], s['pid']))}


if __name__ == '__main__':
    print(json.dumps(probe(), allow_nan=False))
