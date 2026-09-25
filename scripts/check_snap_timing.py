"""Run the getter-only SNAP PPS check and save evidence for the preview.

Writes monitoring evidence only. Uses the spectrum reader's persisted lease.
No worker restarts or board configuration operations. GET never runs this command.
"""
import argparse
import json
import tempfile
from pathlib import Path

from casm_monitor.config import load_settings
from casm_monitor.jobs.snap_read import acquire_lock, release_lock
from casm_monitor.jobs.snap_timing import accept_baseline, antenna_ips, collect
from casm_monitor.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--output', type=Path, help='Run getter-only check and save preview evidence JSON')
    mode.add_argument('--accept-saved-baseline', action='store_true',
                      help='Explicitly accept the latest saved offsets; does not contact hardware')
    args = parser.parse_args()
    settings = load_settings()
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        if args.accept_saved_baseline:
            print(json.dumps(accept_baseline(settings, store),indent=2))
            return
        token = acquire_lock(store, 'manual-pps-check')
        if token is None:
            raise SystemExit('SNAP diagnostic reader busy; no PPS read attempted')
        try:
            report = collect(settings, store, antenna_ips(settings), token)
        finally:
            release_lock(store, token)
    finally:
        store.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    # Atomic publication of local monitoring evidence, never a production DB write.
    with tempfile.NamedTemporaryFile(mode='w',dir=args.output.parent,delete=False) as f:
        json.dump(report,f,indent=2)
        f.write('\n')
        staged = Path(f.name)
    staged.replace(args.output)
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()
