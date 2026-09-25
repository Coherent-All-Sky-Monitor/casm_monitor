"""Run the getter-only SNAP PPS check and save evidence for the preview.

No production writes, worker restarts or board configuration operations.
GET requests never run this command. It is an explicit operator check.
"""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from casm_monitor.config import load_settings
from casm_monitor.snapmap import all_boards


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Preview evidence JSON path')
    args = parser.parse_args()
    settings = load_settings()
    boards = sorted((b for b in all_boards(settings) if b.role == 'antenna'), key=lambda b:b.feng_id)
    if not boards or boards[0].feng_id != 0:
        raise SystemExit('Configured SNAP 0 reference is required')
    script = Path(__file__).resolve().parents[1]/'casm_monitor/remote/snap_timing_remote.py'
    run = subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',settings.zapdos_ssh,
                          'python3','-',*[b.ip for b in boards]], input=script.read_text(),
                         text=True,capture_output=True,timeout=65,check=True)
    report = json.loads(run.stdout)
    if set(report['boards']) != {b.ip for b in boards} or report['reference_ip'] != boards[0].ip:
        raise SystemExit('Timing response does not cover configured boards')
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
