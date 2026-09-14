"""Read the canonical pulsar attempt ledger; expose bounded saved evidence only."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

WIKI = Path('/home/casm/software/dev/casm-wiki')
MAX_ENTRIES = 3000
MAX_IMAGES = 80


def attempt_rows(wiki: Path = WIKI) -> list[dict]:
    """Preserve ledger prose, including nulls and retractions; never infer S/N."""
    path = wiki / 'detections.md'
    if not path.is_file() or path.stat().st_size > 1_000_000:
        return []
    section = path.read_text().split('## B0329+54 attempt table', 1)[-1].split('## Fold recipe', 1)[0]
    rows = []
    for line in section.splitlines():
        if not re.match(r'^\| 20\d\d-\d\d-\d\d', line):
            continue
        cols = re.split(r'(?<!\\)\|', line)[1:-1]
        if len(cols) != 4:
            continue
        date, directory, config, outcome = [x.strip().replace('\\|', '|') for x in cols]
        identity = hashlib.sha256(line.encode()).hexdigest()[:16]
        roots = []
        for entry in re.findall(r'`([^`]+)`', directory):
            if entry.startswith(('/home/casm/', '/mnt/')) and not any(c in entry for c in '*{}'):
                roots.append(entry.rstrip('/'))
        upper = outcome.upper()
        # backticked spans are filenames/paths (e.g. `..._cb_detection.png`), not claims
        upper_prose = re.sub(r'`[^`]+`', '', upper)
        status = ('non_detection' if 'NON-DETECTION' in upper or upper.startswith('NULL') else
                  'contested' if 'CONTESTED' in upper else
                  'detection' if 'DETECTION' in upper_prose else 'recorded_attempt')
        rows.append(dict(id=identity, date=date, source='B0329+54', directory=directory,
                         config=config, outcome=outcome, status=status,
                         contains_retraction='RETRACT' in (config + outcome).upper(),
                         roots=roots, provenance=str(path), snr=None, width_ms=None))
    return sorted(rows, key=lambda r: r['date'], reverse=True)


def _expand_braces(token: str) -> list[str]:
    """Shell-style {a,b} expansion, e.g. name.{png,log} or dir/{a.png,b.png}."""
    match = re.search(r'\{([^{}]*)\}', token)
    if not match:
        return [token]
    prefix, suffix = token[:match.start()], token[match.end():]
    out = []
    for alt in match.group(1).split(','):
        out.extend(_expand_braces(prefix + alt + suffix))
    return out


def referenced_pngs(text: str) -> list[str]:
    """Basenames of every backticked .png reference, in order of appearance."""
    names, seen = [], set()
    for token in re.findall(r'`([^`]+)`', text):
        for expanded in _expand_braces(token):
            if expanded.lower().endswith('.png'):
                name = Path(expanded).name
                if name not in seen:
                    names.append(name)
                    seen.add(name)
    return names


def images_for(row: dict, wiki: Path = WIKI) -> tuple[list[Path], bool]:
    """At most two directory levels under ledger roots, never raw data trees."""
    roots = [Path(p) for p in row['roots']]
    evidence = wiki / 'evidence' / row['date'][:10]
    if evidence.is_dir():
        roots.append(evidence)
    result, seen, inspected = [], set(), 0
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        queue = [(root, 0)]
        while queue:
            directory, depth = queue.pop(0)
            try:
                entries = directory.iterdir()
                for path in entries:
                    inspected += 1
                    if inspected > MAX_ENTRIES or len(result) >= MAX_IMAGES:
                        return sorted(result), True
                    if path.is_symlink():
                        continue
                    if path.is_file() and path.suffix.lower() == '.png' and ('pdmp' in path.name.lower() or 'fold' in path.name.lower()):
                        resolved = path.resolve()
                        if resolved not in seen and path.stat().st_size <= 30_000_000:
                            result.append(resolved)
                            seen.add(resolved)
                    elif depth < 2 and path.is_dir() and path.name.lower() not in {'fil', 'raw', 'dumps', 'work', 'foldkit'}:
                        queue.append((path, depth + 1))
            except OSError:
                continue
    return sorted(result), False


def source_history(query: str, wiki: Path = WIKI) -> dict:
    if query.strip().lower().replace(' ', '') not in {'', 'b0329', 'b0329+54', '0329', 'psrb0329+54'}:
        return {'sources': [], 'state': 'no_match'}
    attempts = attempt_rows(wiki)
    for row in attempts:
        images, partial = images_for(row, wiki)
        row['artifacts'] = [dict(id=hashlib.sha256(str(p).encode()).hexdigest()[:20], name=p.name,
                                 url=f"/api/sources/B0329/attempts/{row['id']}/artifacts/{hashlib.sha256(str(p).encode()).hexdigest()[:20]}") for p in images]
        row['artifact_scan_partial'] = partial
        row['evidence_note'] = 'Saved files associated by ledger directory, not newly validated detections. Full recorded S/N, width and qualifications remain in outcome text.'
    _promote_headline_artifacts(attempts)
    return {'state': 'ready' if attempts else 'unavailable',
            'sources': [{'name': 'B0329+54', 'attempts': attempts}],
            'provenance': str(wiki / 'detections.md'),
            'note': 'All canonical attempt rows, including non-detections and retractions. Artifact discovery is bounded; missing plots do not erase an attempt.'}


def _promote_headline_artifacts(rows: list[dict]) -> None:
    """Put the ledger-named plot first; borrow it from a same-date row if this
    row's own scan missed it. Never invents plots, only reorders/borrows."""
    for row in rows:
        refs = referenced_pngs(row['outcome']) + referenced_pngs(row['directory'])
        by_name = {a['name']: a for a in row['artifacts']}
        ordered, placed = [], set()
        for name in refs:
            if name in placed:
                continue
            match = by_name.get(name)
            if match is None:
                same_date = row['date'][:10]
                for other in rows:
                    if other is row or other['date'][:10] != same_date:
                        continue
                    match = next((a for a in other['artifacts'] if a['name'] == name), None)
                    if match is not None:
                        break
            if match is not None:
                ordered.append(match)
                placed.add(name)
        for a in row['artifacts']:
            if a['name'] not in placed:
                ordered.append(a)
        row['artifacts'] = ordered
        row['headline_from_ledger'] = bool(ordered) and ordered[0]['name'] in placed


def build_router(wiki: Path = WIKI) -> APIRouter:
    router = APIRouter()

    @router.get('/api/sources')
    def sources(q: str = Query('B0329', max_length=80)):
        return source_history(q, wiki)

    @router.get('/api/sources/B0329/attempts/{attempt_id}/artifacts/{artifact_id}')
    def artifact(attempt_id: str, artifact_id: str):
        row = next((r for r in attempt_rows(wiki) if r['id'] == attempt_id), None)
        if row:
            for path in images_for(row, wiki)[0]:
                if hashlib.sha256(str(path).encode()).hexdigest()[:20] == artifact_id:
                    return FileResponse(path, media_type='image/png', filename=path.name,
                                        content_disposition_type='inline')
        raise HTTPException(404, 'Saved plot unavailable')
    return router
