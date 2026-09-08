## Ranked findings

1. **P0 — Hard-rule violation: unconstrained write/delete paths.** `store/shards.py:63-66,83,194` accepts absolute/traversing shard roots/stream names and deletes manifest-supplied paths; `jobs/worker.py:67-85` trusts `jobs_root`. Either can modify outside the store root, including potentially `/home/casm/software/fourier-space`.  
   **Fix:** Resolve every write/delete target and reject it unless `target.is_relative_to(resolved_store_root)`; restrict stream names to safe components.

2. **P0 — Hard-rule violation under literal scope.** `deploy/install.sh:14-22` creates symlinks/drop-ins under `$XDG_CONFIG_HOME`/`$HOME`, outside the store root.  
   **Fix:** Make installation an explicitly documented operator exception, or have the script only emit installation instructions.

3. **P0 — Zapdos hourly guarantee is not absolute.** `collectors/services.py:179-188` trusts configurable `zapdos_min_interval_s` and performs a non-atomic watermark read/set, allowing sub-hour configuration or concurrent restart instances to double-probe.  
   **Fix:** Clamp to ≥3600 seconds and acquire the probe slot with one transactional compare-and-set.

4. **P1 — Retention can lose or orphan shards.** `store/shards.py:182-195,200-223` snapshots references before deletion, recognizes only exact string paths—not shard IDs—and removes data before the manifest row; `ignore_errors=True` can hide failed deletion.  
   **Fix:** Maintain explicit shard-reference rows, transactionally mark/remove the manifest, validate containment, then rename to store-local trash and delete safely.

5. **P1 — Collector timeout does not stop work.** `collectors/runner.py:80-85,113-119` cancels only the asyncio future; the executor thread continues and later iterations can overlap it, duplicating external probes and store writes.  
   **Fix:** Use cancellable subprocesses/cooperative deadlines and prevent another run while the timed-out invocation remains alive.

6. **P1 — Lease and cancellation races.** `jobs/worker.py:115,129-145` renews and finishes without checking worker ownership/current state; completion can overwrite cancellation or an orphan-reconciliation result.  
   **Fix:** Make renew/finish conditional SQL updates on `state='running'`, `worker_id`, and valid lease; treat zero updated rows as lost ownership.

7. **P1 — Worker shutdown leaves jobs running.** `jobs/worker.py:56-57,138-166,207` sets `_stop` but supervision never checks it; the new-session child may outlive a systemd-killed worker.  
   **Fix:** On worker stop, terminate/reap the active process group in `finally` before exit.

8. **P1 — Hella cfg concatenation is broken.** `collectors/hella.py:51,101-103` splits on `OUTPUT ` although shown files use `OUTPUTPATH`; concatenation can collapse all jobs into one block.  
   **Fix:** Parse each filename independently, preserving its job ID.

9. **P2 — Fragile process detection.** `collectors/obs.py:93-110,164-165` uses substring matching, incomplete wrapper detection, and ignores `ps` failure, causing false process counts/state changes.  
   **Fix:** Check the return code and parse argv/executable tokens with `shlex`.

10. **P2 — Kafka/registry false conclusions.** `services.py:105-123` reports Kafka healthy before successful topic offsets and compares only summed offsets; `weights.py:103-150` checks one event and silently substitutes an unrelated newest product.  
    **Fix:** Track successful per-topic/partition offsets; validate every latest stream event and flag missing referenced products.

11. **P2 — Restart hammering.** `deploy/systemd/*.service:8-9` restarts every five seconds without rate limiting; collectors immediately hit LMC, Redis, Kafka, and SSH.  
    **Fix:** Add `StartLimitIntervalSec`/`StartLimitBurst`, longer backoff, and `Restart=on-failure`.

Redis uses PING only; Kafka has no group/subscription/commit; LMC and SNAP forbidden calls were not found.

**Verdict: needs-fixes.**
