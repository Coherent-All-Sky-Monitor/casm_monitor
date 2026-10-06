"""Validated operator choices, shared by staging, review and upload.

No shell fragments, arbitrary destinations or live registry bypasses.
"""
from datetime import datetime


def normalize_options(value, defaults):
    if not isinstance(value, dict):
        raise ValueError('Deployment options must be an object')
    unknown = set(value) - {'kind', 'scale', 'ib_scale', 'streams', 'utc_start'}
    if unknown:
        raise ValueError('Unknown deployment options: '+', '.join(sorted(unknown)))
    kind = value.get('kind', 'both')
    if kind not in ('cb', 'ib', 'both'):
        raise ValueError('Choose CB, IB or both')
    result = {'kind': kind}
    for name in ('scale', 'ib_scale'):
        number = value.get(name, defaults.get(name))
        if type(number) is not int or not 1 <= number <= 2147483647:
            raise ValueError(f'{name} must be a positive 32-bit integer')
        result[name] = number
    streams = value.get('streams', list(range(6)))
    if not isinstance(streams, list) or not streams or any(type(s) is not int or s not in range(6) for s in streams) or len(set(streams)) != len(streams):
        raise ValueError('Select distinct streams in 0..5')
    result['streams'] = sorted(streams)
    utc = value.get('utc_start') or None
    if utc is not None:
        if not isinstance(utc, str):
            raise ValueError('UTC_START must be YYYY-MM-DD-HH:MM:SS')
        try:
            parsed = datetime.strptime(utc, '%Y-%m-%d-%H:%M:%S')
            if parsed.strftime('%Y-%m-%d-%H:%M:%S') != utc:
                raise ValueError()
        except ValueError:
            raise ValueError('UTC_START must be YYYY-MM-DD-HH:MM:SS') from None
    result['utc_start'] = utc
    return result


def expected_files(options):
    prefixes = {'cb': ['direct.dada'], 'ib': ['direct_ib.dada'],
                'both': ['direct.dada', 'direct_ib.dada']}[options['kind']]
    return {f'{prefix}.{stream}' for prefix in prefixes for stream in options['streams']}
