Verdict: **needs fixes**.

1. **High — GET route writes outside the store root.** `casm_monitor/web/cands.py:345-357` renders a stats PNG into t3’s shared temporary cache. This violates both route read-only and write-location rules. Fix: pre-render into `store_root` via a job; GET should only serve it.

2. **High — Imaging cache lacks calibration/config identity.** `casm_monitor/jobs/render_figures.py:539-584,668-695` keys frames only by rounded Unix time, so a deployed-cal or antenna-set change mixes old frames into products while the manifest advertises the new configuration (`:792-813`). Fix: include a deployed-cal/antenna/config fingerprint in cache metadata/key and invalidate mismatches.

3. **High — M4 is functionally incomplete.** The plan requires source cutouts and PSF ceiling (`docs/plan.md:80-83,285-288`), but only all-sky products exist and `psf_ceiling_snr` is always null (`render_figures.py:781-811`). Fix: call the required `image_around_source`/PSF helpers and publish their outputs.

4. **High — t3 module globals make routers cross-contaminate.** `casm_monitor/web/cands.py:151-152` mutates process-global `DB_PATH`/`CANDIDATES_DIR`; later router construction redirects every previously built router, especially parallel tests. Separate t3-web processes are unaffected. Fix: change/bind t3 helpers to accept explicit paths rather than mutating globals.

5. **Medium — Candidate symlink can escape its event directory.** `cands.py:277-283` checks containment under all of `CANDIDATES_DIR`, permitting a symlink in event A to expose a file from event B. Fix: resolve and require containment beneath the resolved `CANDIDATES_DIR/name`.

6. **Medium — Concurrent label writes can fail as HTTP 500.** `cands.py:318` has no `SQLITE_BUSY` retry or demonstrated WAL/busy-timeout setup; WAL alone does not eliminate writer contention with t3-web. Fix: verify WAL/busy timeout at startup and retry boundedly while still invoking `t3app.label()`.

7. **Medium — Candidate list work is unbounded despite result limits.** `cands.py:160-177` loads all triggers and latest labels; `/frbs` is wholly unbounded (`:381-383`). Fix: restrict auxiliary queries to selected event names and add clamped pagination to FRBs.

8. **Medium — `since` has the SQLite T-vs-space trap.** `cands.py:210-212` compares unchecked text lexically; space-form timestamps return incorrect same-day rows. Fix: parse ISO-8601 and canonicalize to the database’s `T` format or reject invalid forms.

9. **Medium — Movie cap does not bound RSS and may omit newest frame.** All frames are loaded before subsampling (`render_figures.py:733-737`; `imaging_figures.py:523-525`). Fix: select ≤240 timestamp indices, including both endpoints, before loading NPZs.

10. **Low — Retention misses orphan halves and is skipped when config fails.** `render_figures.py:609-622,656-666`. Fix: expire PNG/NPZ independently before resolving imaging configuration.

Direct imaging traversal, CSRF comparison, deployed antenna derivation, gap advancement, strip selection, and ffmpeg fallback look sound. `docs/api-imaging.md` was absent from the checkout, limiting contract verification.
