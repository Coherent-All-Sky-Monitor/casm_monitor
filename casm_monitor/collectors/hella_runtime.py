"""Minute-cadence process-associated HELLA settings, independent of page views."""
import json
from pathlib import Path
import shlex
import sys

from .. import hella_runtime_probe
from ..util import run
from .base import Collector


class HellaRuntimeCollector(Collector):
    default_cadence_s = 60.0
    timeout_s = 25.0

    def __init__(self, settings, node='corr1', cadence_s=None):
        self.node = node
        self.name = f'hella_runtime_{node}'
        super().__init__(settings, cadence_s)

    def collect(self, ctx):
        script = Path(hella_runtime_probe.__file__)
        command = [sys.executable, str(script)] if self.node == 'corr1' else [
            'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
            self.settings.corr2_ssh, 'python3 -c ' + shlex.quote(script.read_text())]
        try:
            rc, out, _err = run(command, timeout=20.0)
            if rc:
                raise RuntimeError(f'Read failed (exit {rc})')
            payload = json.loads(out)
            if not isinstance(payload.get('streams'), list):
                raise ValueError('Invalid probe response')
        except (OSError, RuntimeError, ValueError) as exc:
            payload = {'streams': [], 'error': str(exc)}
        # Use the collector's receive time, avoiding correlator clock offsets.
        ctx.scalar(f'hella.runtime.{self.node}', json.dumps(payload, allow_nan=False))
