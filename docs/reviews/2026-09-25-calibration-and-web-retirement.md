# Calibration and web retirement, September 25, 2026

The operator requested retirement of the legacy web pages and a Calibration
restyle, with an independent Fable 5.1 Harvard-account consultation and no live
weight changes.

## Web services

Stopped and disabled `casm-monitor-web.service` (8060) and `t3-web.service`
(8050). Only 8061 remains listening among those ports. systemd removed the
installed unit/enablement symlinks; source templates and all data remain.
Collector/jobs, T2, injections, T3 collection/plotting and weights-watch processes
were left running. The 8061 web service was restarted to load its route changes.

The SNAP manual request previously depended on 8060. It now shares the existing
atomic queue helper directly, after the same origin/CSRF/confirmation guards.
Generic operational POSTs remain blocked. The collector still owns hourly
acquisition, with no additional reader or scheduler. Its interval remained 3600 s.
Automatic SNAP job 866 completed after retirement at 08:15:50 UTC. The recorded
collector/worker/T2/T3 process IDs matched their pre-retirement values.

## Consultation and changes

Actual model `claude-fable-5-1`, Harvard account, restricted to Read/Grep/Glob.
No consultant commands or mutations. Report:
`/home/casm/scratch/fable-calibration-consult-20260925.out`.

Verified against source before applying:

- The legacy ellipse branch can draw saved pointings and astropy source tracks
  without invoking approximate hit predictions or the exact coverage-map cache.
- Dry-run payload generation must use `--no-registry`; omitting `--upload` alone
  does not prevent registry metadata writes in the installed tool.
- Workspace staging now explicitly checks 512 payload beams, the HDF5 attribute
  and both pointing-array lengths. Other legacy stage callers retain their
  existing contracts.

The Calibration tab uses larger controls, green selections, white diagnostic
figures, plot grids, shared zoom and an ellipse/source-track sky view. Generation
continues through the exact canonical driver. Live upload controls are removed;
workspace settings force authority off and middleware rejects upload requests.
The deployed reference still comes from uploaded-product records, never the
newest experimental calibration.

## Acceptance

The final combined calibration/sky/SNAP/security/science fixture run passed
151 tests. Frontend TypeScript and Vite build passed.
Browser acceptance passed with no page errors, layouts at 1440/1280/768/390 px,
saved plots, the schematic sky view, imported-reference stage denial and zoom.
Real screenshots are in `docs/screenshots/2026-09-25-calibration/`.
Real-data jobs 18 and 19 reused saved candidate
`workspace_acceptance_20260918_1790102134`: three Sun phase baselines and five-hour
Cyg A stationary-beam diagnostics. These were local analysis jobs, not a new
calibration solve or a weight upload. Saved older PNGs were not overwritten.

Real deployment-tool dry run:
`scripts/check_calibration_dry_run.py`, invoked as a Python module, produced six
CB and six IB DADA files under
`/home/casm/scratch/casm-observation-preview/calibration/dry-run-acceptance-x2q9bmyx/`.
`acceptance.json` records arguments, product checks, payload hashes and before/after
hashes of deployed calibration/CB/IB HDF5s, deployment ledger and registry files.
All protected hashes matched. No upload/default flag was present, and no registry
record was written. This acceptance is not scientific approval of the candidate.

The external science API snapshot still has a pre-existing missing configured
`casm_calibrator/calibration.py` source. The maintained guide is updated; no
partial snapshot refresh is substituted for that unresolved migration.

The value-redacted plaintext-credential scan covered the staged working files
and 945 historical blobs. Its only current matches were runtime password
lookups and the existing fake test fixture; no plaintext credentials were found.
