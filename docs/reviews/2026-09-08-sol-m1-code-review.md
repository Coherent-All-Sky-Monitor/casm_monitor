Verdict: **needs-fixes**.

### Hard-rule violations

1. **P0 — Reads can overlap after lease expiry.** The lock has a fixed 600-second lease with no renewal, while remote execution/configuration and subsequent ingestion may exceed it; another job may then acquire the lock while the first still contacts hardware. [snap_read.py:55](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/snap_read.py:55), [snap_read.py:88](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/snap_read.py:88), [snap_read.py:444](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/snap_read.py:444)  
   Fix: use a unique lease token, renew it during execution, and terminate the SSH process tree before lease loss.

2. **P0 — Concurrent clicks/scheduler races can enqueue multiple reads.** Refusal, manual timestamp update, and submission are separate transactions; two requests can both pass. Manual jobs also do not reserve the shared hourly zapdos slot until after SSH, and duplicate IPs read one board repeatedly. [snapread.py:184](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/web/snapread.py:184), [snapread.py:190](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/web/snapread.py:190), [snapread.py:192](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/web/snapread.py:192), [snapread.py:64](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/collectors/snapread.py:64)  
   Fix: atomically CAS a shared reservation and enqueue, enforce `max(config,300)`, and deduplicate IPs.

3. **P0 — Per-board budget is not enforced.** `call()` checks time only before invoking a getter; one hung KATCP call can consume the whole multi-board SSH timeout. [snap_read_remote.py:80](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/remote/snap_read_remote.py:80)  
   Fix: impose a hard per-call/per-board alarm and abort that board on expiry.

No prohibited Kafka commits/subscriptions, register writes, or mutating GET routes were found.

### Correctness

4. **P0 — Fifteen-second frame timeout contradicts measured ~68-second producer skew.** Frames emit incomplete, late records recreate duplicate fragments, and older out-of-order frames can replace `_latest`; missing timestamp silently falls back to CreateTime. [kafka_bp.py:59](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/collectors/kafka_bp.py:59), [kafka_bp.py:361](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/collectors/kafka_bp.py:361), [kafka_bp.py:367](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/collectors/kafka_bp.py:367)  
   Fix: require the header timestamp/exact offset and shape, wait beyond producer skew, deduplicate emitted timestamps, and keep latest monotonic.

5. **P1 — Watermarks advance before buffered shards are durable; failed flushes discard buffers.** A crash/reconnect loses acknowledged history, while replay can duplicate prior samples. [kafka_bp.py:350](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/collectors/kafka_bp.py:350), [kafka_bp.py:455](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/collectors/kafka_bp.py:455), [kafka_bp.py:496](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/collectors/kafka_bp.py:496)  
   Fix: retain buffers on failure and checkpoint only after idempotent durable persistence.

6. **P1 — Decimation averages dB values directly; `max_cells` is not a strict bound.** Small requested limits are raised to 1000, and highly time-heavy matrices can still exceed the limit. [snaps.py:178](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/web/snaps.py:178), [snaps.py:270](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/web/snaps.py:270), [snaps.py:308](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/web/snaps.py:308)  
   Fix: average floored linear power, reconvert to dB, and enforce the final cell product.

7. **P1 — Row validation can read a sole growing visibility file and arbitrarily classify flat/NaN spectra; stale removed mappings remain assignable.** [rowmap.py:145](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/collectors/rowmap.py:145), [rowmap.py:212](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/collectors/rowmap.py:212), [rowmap.py:421](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/collectors/rowmap.py:421)  
   Fix: require a stable closed file, mark degenerate correlations unverified, and purge/exclude stale assignments.

8. **P1 — Valid-but-partial NPZs are accepted, and library stdout can corrupt the archive.** [snap_read.py:136](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/jobs/snap_read.py:136), [snap_read_remote.py:280](/home/casm/software/dev/casm_monitor/.claude/worktrees/m0-scaffold/casm_monitor/remote/snap_read_remote.py:280)  
   Fix: isolate archive stdout and validate version, requested IP set, and mandatory arrays before ingestion.
