"""Acceptance of real local payload generation. Never upload or register a trial."""
import argparse
import dataclasses
import json
import tempfile
from pathlib import Path

from casm_monitor.cal_defaults import deployed_product, parse_scale_pairing, read_last_ledger_row, sha256_file
from casm_monitor.config import load_settings
from casm_monitor.jobs import deploy
from casm_monitor.web.calibration_workspace import workspace_settings


def protected_files(settings):
    files = [settings.deployed_weights_csv]
    recorded = deployed_product(settings)
    files.extend(Path(recorded[key]) for key in ('cal_file', 'weights_file', 'ib_file') if recorded.get(key))
    files.extend(path for path in settings.registry_dir.rglob('*') if path.is_file())
    return {str(path): sha256_file(path) for path in files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--workspace-root', type=Path, required=True)
    parser.add_argument('--selections', action='store_true', help='Exercise CB-only, IB-only and both, on streams 0 and 3; local files only')
    args = parser.parse_args()
    parent = load_settings()
    settings = workspace_settings(dataclasses.replace(parent, observation_cache_root=args.workspace_root))
    summary = deploy.load_summary(settings, args.tag)
    if not summary or not summary.get('has_weights'):
        raise ValueError('Choose a completed offline CB/IB build')
    cb, ib = deploy.canonical_sources(settings, args.tag, summary)
    checks = deploy._checks(str(cb), str(ib) if ib else None, summary['antennas'], expected_beams=512)
    if not all(row['ok'] for row in checks):
        raise ValueError('CB / IB product checks failed: ' + json.dumps(checks))
    pairing = parse_scale_pairing(read_last_ledger_row(settings.deployed_weights_csv))
    output = Path(tempfile.mkdtemp(prefix='dry-run-acceptance-', dir=settings.store_root))
    if args.selections:
        from casm_monitor.jobs.deploy_options import normalize_options, expected_files
        before = protected_files(settings)
        results=[]
        for kind in ('cb','ib','both'):
            options=normalize_options(dict(kind=kind,scale=4096,ib_scale=16,streams=[0,3]),pairing)
            directory=output/kind
            argv=deploy.canonical_argv(settings,args.tag,weights_h5=cb,ib_h5=ib,
                scale=options['scale'],ib_scale=options['ib_scale'],upload=False,options=options,output_dir=directory)
            assert '--no-registry' in argv and not {'--upload','--save-defaults','--dry-run'} & set(argv)
            code,log=deploy._run(argv)
            (output/(kind+'.log')).write_text(log)
            files=deploy.dada_files(directory)
            assert code==0, log
            assert {p.name for p in files}==expected_files(options)
            for path in files:
                with path.open('rb') as stream: header=stream.read(4096).decode('ascii')
                assert ('SCALE 16\n' if path.name.startswith('direct_ib') else 'SCALE 4096\n') in header
            results.append(dict(kind=kind,argv=argv,files={p.name:deploy.md5_file(p,skip=4096) for p in files}))
        after=protected_files(settings)
        assert before==after
        report=dict(selections=results,protected_files_unchanged=True,live_upload=False,
                    scientific_approval=False,protected_files=before)
        (output/'acceptance.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(dict(output=str(output),modes=3,protected_files_unchanged=True,live_upload=False)))
        return
    argv = deploy.canonical_argv(settings, args.tag, weights_h5=cb, ib_h5=ib,
                                 scale=pairing['scale'], ib_scale=pairing['ib_scale'], upload=False)
    argv[argv.index('-o') + 1] = str(output)
    assert '--no-registry' in argv
    assert not {'--upload', '--save-defaults', '--dry-run'} & set(argv)
    before = protected_files(settings)
    code, log = deploy._run(argv)
    after = protected_files(settings)
    files = deploy.dada_files(output)
    report = dict(exit_code=code, argv=argv, checks=checks, output_dir=str(output),
                  payloads={path.name: {'bytes': path.stat().st_size,
                      'payload_md5': deploy.md5_file(path, skip=deploy.HDR_SIZE)} for path in files},
                  protected_files_before=before, protected_files_after=after,
                  protected_files_unchanged=before == after,
                  live_upload=False, scientific_approval=False)
    (output / 'acceptance.json').write_text(json.dumps(report, indent=2))
    (output / 'tool.log').write_text(log)
    assert code == 0, log
    assert len(files) == 12, 'Expected six CB and six IB files'
    assert before == after, 'Recorded weights, ledger or registry changed during acceptance'
    print(json.dumps({'output': str(output), 'files': len(files), 'protected_files_unchanged': True,
                      'live_upload': False}, indent=2))


if __name__ == '__main__':
    main()
