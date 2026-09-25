# Dashboard publication check, September 25, 2026

Scope: the dashboard and README on `main`. The uncommitted Calibration
workspace, its service, tests and documentation are excluded. The static
frontend bundle was rebuilt from a clean export of source commit `2a5d04a`,
without the Calibration worktree changes. Existing live files were preserved.

The README uses real September 25 screenshots. Capture routes, UTC timestamps
and SHA-256 hashes are in
`docs/screenshots/2026-09-25-latest/manifest.json`. The capture script permits
saved-data GETs and the existing visibility-render endpoint; acquisition,
Calibration and review writes are blocked. It waits for image decoding and
the selected visibility result before capturing.

## Credential review

A value-redacted pattern scan checked 928 historical blobs reachable from
`main` and the fetched `origin/main`, plus tracked working files. Patterns
covered private-key headers, common provider tokens, credential-bearing URLs,
Bearer/JWT tokens, password assignments/arguments and Redis config passwords.
All password-related matches were reviewed: runtime lookups in
`casm_monitor/config.py` and `casm_monitor/collectors/services.py`, and a
deliberately fake parser fixture in `tests/test_config.py`. No real plaintext
credential was found. New screenshots were visually reviewed. This bounded
review is not a guarantee against every possible secret encoding.

Internal addresses and deployment paths remain by operator choice. Local
credential-file patterns are now ignored. No history rewrite or push was done.

## Checks

The clean-main frontend build and 120 T1/SNAP/source-transit regression tests
passed. Hourly SNAP/PPS implementation and configuration already match the
locked collector checkout; no observing service or hardware setting changed.
