Verdict: **needs-fixes**. The upload path has multiple critical gate bypasses.

1. **Critical — upload is API-reachable without browser/CSRF provenance.** [`cal.py:452`](</home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/web/cal.py:452>) accepts an ordinary POST, while [`kinds.py:140`](</home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/kinds.py:140>) exposes `deploy_upload` to generic job submission; [`deploy.py:621`](</home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/deploy.py:621>) accepts only forgeable parameters.  
   Fix: exclude privileged kinds from generic POST and require the worker to atomically consume a single-use, user/CSRF/build/stage-digest-bound authorization created by the browser route.

2. **Critical — mutable `stage.json` controls executable argv.** [`deploy.py:507`](</home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/deploy.py:507>) trusts JSON fields, and [`deploy.py:637`](</home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/deploy.py:637>) executes its arbitrary command/flags without validating executable, paths, `--upload`, or absence of `--no-registry`.  
   Fix: reconstruct argv from canonical, contained build artifacts; treat recorded argv as display-only and reject any provenance mismatch.

3. **Critical — staged hashes do not bind uploaded bytes.** [`deploy.py:513`](</home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/deploy.py:513>) checks only names present in the JSON (even an empty map), then the upload command reuses mutable source HDF5 paths and may regenerate files at [`deploy.py:643`](</home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/deploy.py:643>).  
   Fix: require the exact CB/IB file set, immutable contained source snapshots, and upload the verified staged bytes under a lock without a check/use gap.

4. **High — symlink aliases defeat product/tag binding.** [`cal_build.py:279`](</home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/cal_build.py:279>) permits symlinks resolving elsewhere inside the build root, while upload never checks URL tag = summary tag = stage tag.  
   Fix: reject symlinked build/stage components and cross-check all three tags.

5. **High — layout snapshot has a TOCTOU.** [`cal_build.py:343`](</home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/cal_build.py:343>) hashes one resolution, copies later, and passes the live symlink to the solver at line 370; upload compares the pre-copy hash.  
   Fix: hash the copied snapshot and pass that snapshot to `RecipeParams`.

6. **High — `casm-track` gate fails open and races.** [`deploy.py:173`](</home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/deploy.py:173>) treats `ps` failure as “not running,” and tracking can start after line 520.  
   Fix: fail closed and share an interprocess lock/inhibit with `casm-track` across check and upload.

7. **High — audit/registry guarantees are not durable.** Upload occurs before `add_upload`, and registry exceptions are swallowed at [`deploy.py:642`](</home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/deploy.py:642>); success may have neither audit nor registry product.  
   Fix: pre-write an audit intent, finalize it afterward, and require verified registry persistence.

8. **High — build validation violates bindings.** [`cal_build.py:198`](</home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/cal_build.py:198>) permits any configured source; line 215 checks beamforming membership but not wired membership; line 106 dynamically executes any configured IB script.  
   Fix: require Sun, intersect beamforming with wired antennas, reject overlapping windows, and pin the approved IB generator path/hash.
