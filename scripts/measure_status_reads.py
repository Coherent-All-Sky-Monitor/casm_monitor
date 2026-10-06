"""Bounded read-only status-query/API timings; no hardware or service actions."""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from casm_monitor.config import load_settings
from casm_monitor.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, choices=range(1, 6), default=3)
    args = parser.parse_args()
    settings = load_settings()
    store = Store(settings.db_path, store_root=settings.store_root, read_only=True)
    previous_sql = (
        'SELECT s.ts, s.name, s.tags, s.value FROM scalars s '
        'JOIN (SELECT name, MAX(ts) AS max_ts FROM scalars GROUP BY name) m '
        'ON s.name = m.name AND s.ts = m.max_ts'
    )
    report = dict(at_utc=datetime.now(timezone.utc).isoformat(),
                  database=str(settings.db_path), old_query_s=[], indexed_query_s=[])
    try:
        # Same read snapshot for both methods; no timestamp/freshness changes.
        store.query('BEGIN')
        for _ in range(args.samples):
            start = time.perf_counter()
            old = {r['name']: dict(r) for r in store.query(previous_sql)}
            report['old_query_s'].append(time.perf_counter()-start)
            start = time.perf_counter()
            current = store.latest_scalars()
            report['indexed_query_s'].append(time.perf_counter()-start)
            expected = {name: dict(row, tags=json.loads(row['tags']) if row['tags'] else None)
                        for name, row in old.items()}
            assert current == expected, 'Latest scalar values differ; inspect before deploying'
        report.update(latest_values_equal=True, scalar_names=len(current))
    finally:
        store.close()
    opener = build_opener(ProxyHandler({}))
    report['http'] = {}
    for path in ('/api/health', '/api/observation', '/api/science/catalog',
                 '/api/snap-workspace/beamforming', '/api/snap-workspace/health'):
        elapsed = []
        for _ in range(args.samples):
            start = time.perf_counter()
            with opener.open('http://127.0.0.1:8061'+path, timeout=30) as response:
                size = len(response.read())
                assert response.status == 200
            elapsed.append(time.perf_counter()-start)
        report['http'][path] = dict(seconds=elapsed, bytes=size)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
