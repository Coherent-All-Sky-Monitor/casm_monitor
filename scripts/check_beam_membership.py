"""Inspect recorded deployed weights offline and refresh the preview membership cache."""
import argparse
import json
from dataclasses import replace
from pathlib import Path

from casm_monitor.config import load_settings
from casm_monitor.figures.observation import refresh_membership
from casm_monitor.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, required=True,
                        help='Local preview cache directory; no weights or registry files are modified')
    args = parser.parse_args()
    settings = replace(load_settings(), observation_cache_root=args.output_root)
    store = Store(settings.db_path, store_root=settings.store_root, read_only=True)
    try:
        result = refresh_membership(store, settings)
        print(json.dumps({k: result.get(k) for k in
                         ('inspection_state', 'product_id', 'path', 'antennas',
                          'global_metadata_antennas', 'bytes_inspected', 'error')}, indent=2))
        if result['inspection_state'] != 'complete':
            raise SystemExit(1)
    finally:
        store.close()


if __name__ == '__main__':
    main()
