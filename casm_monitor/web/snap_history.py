"""Optional display epoch; older SNAP data remains stored and recoverable."""
import json
import math


def history_start(settings):
    if settings.observation_cache_root is None:
        return None
    try:
        path = settings.observation_cache_root / 'snap_history_epoch.json'
        if path.stat().st_size > 4096:
            return None
        record = json.loads(path.read_text())
        start = record.get('start')
        if record.get('version') == 1 and isinstance(start,(float,int)) and math.isfinite(start) and start > 0:
            return float(start)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None
