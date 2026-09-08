## Ranked objections

1. **Unsafe reads of the live visibility file.** `size // 203 MB` uses a rounded size and can expose an integration while its final pages are still being written; the reader similarly floors the exact byte count and immediately memmaps it ([plan:119](/home/casm/.claude/plans/virtual-humming-magpie.md:119), [reader.py:126](/home/casm/software/dev/casm_io/casm_io/correlator/reader.py:126)). The archive deliberately ignores files modified within two hours because the writer is live ([head-node.md:17](/home/casm/software/dev/casm-wiki/head-node.md:17)).  
   **Fix:** Use the exact header offset and 202,899,456-byte integration size, retain a one-integration guard band, require stable size/mtime across polls, and persist an `(observation,file,integration)` watermark.

2. **Kafka semantics are unspecified and the accounting is wrong.** `590 KB/msg` treats six stream producers as one full-array message; the collector must assemble corr1 streams 0–2 and corr2 streams 3–5. Moreover, 48×96×4 bytes every 10 s is **0.159 GB/day**, not 0.04 ([plan:114](/home/casm/.claude/plans/virtual-humming-magpie.md:114)); Kafka and Redis are aggregation stages, not interchangeable raw feeds ([AntennaMonitor.h:17](/home/casm/software/fourier-space/sources/casm/src/casm/AntennaMonitor.h:17), [AntennaMonitor.h:101](/home/casm/software/fourier-space/sources/casm/src/casm/AntennaMonitor.h:101)).  
   **Fix:** Specify message schema/key, six-stream completeness timeout, stable consumer group, `earliest` recovery, commit-after-durable-write, and idempotent producer/stream/timestamp keys.

3. **Zarr append/read/retention concurrency has no consistency protocol.** Separate collector and web processes can expose resized arrays before chunks exist, while retention can remove chunks under readers ([plan:91](/home/casm/.claude/plans/virtual-humming-magpie.md:91), [plan:99](/home/casm/.claude/plans/virtual-humming-magpie.md:99)).  
   **Fix:** Write immutable time shards to temporary names, fsync/rename, then publish them through one SQLite transaction; readers consume only committed manifests.

4. **The “job runner” has no service or recovery model.** The diagram launches subprocesses, but only collector and web units exist ([plan:95](/home/casm/.claude/plans/virtual-humming-magpie.md:95), [plan:98](/home/casm/.claude/plans/virtual-humming-magpie.md:98)); a web restart can kill/orphan work and leave jobs permanently “running.”  
   **Fix:** Add a third single-worker service with a durable queue, leases, restart reconciliation, cancellation, timeout, and systemd CPU/memory limits.

5. **Memory feasibility ignores calibration and worker processes.** The canonical driver requests full visibilities without baseline selection ([make_cal_and_weights.py:403](/home/casm/software/dev/bf_weights_generator/bf_weights_generator/make_cal_and_weights.py:403)); the reader then materializes and concatenates full complex cubes ([reader.py:149](/home/casm/software/dev/casm_io/casm_io/correlator/reader.py:149), [reader.py:1078](/home/casm/software/dev/casm_io/casm_io/correlator/reader.py:1078)). A 47-integration solve starts near 9.5 GB before subtraction, fringe-stop, SVD, diagnostics, or multiprocessing.  
   **Fix:** Serialize heavy jobs and benchmark peak RSS/I/O; enforce `MemoryMax`, worker caps, and admission control before M3/M4.

6. **The promised tracked-beam mode does not exist.** `grid_mode="track"` constructs a bounding box; the driver explicitly says beams-along-track plus exact-grid fill has no helper ([plan:62](/home/casm/.claude/plans/virtual-humming-magpie.md:62), [make_cal_and_weights.py:214](/home/casm/software/dev/bf_weights_generator/bf_weights_generator/make_cal_and_weights.py:214)).  
   **Fix:** Remove this UI option from M3 or implement and validate it upstream before advertising it.

7. **Layout provenance can silently produce valid-looking bad weights.** The driver defaults to a fixed 2026-08-07 layout ([make_cal_and_weights.py:91](/home/casm/software/dev/bf_weights_generator/bf_weights_generator/make_cal_and_weights.py:91)), while the known antenna/layout intersection can yield near-empty weights that pass verification ([weights-and-deploy.md:140](/home/casm/software/dev/casm-wiki/weights-and-deploy.md:140)).  
   **Fix:** Snapshot the chosen layout into every job, display its date/hash and exact active set, and require populated CB/IB slot counts to equal that set.

8. **Upload safety remains optional.** Option B introduces an application upload path despite the binding manual-upload rule ([plan:160](/home/casm/.claude/plans/virtual-humming-magpie.md:160), [weights-and-deploy.md:113](/home/casm/software/dev/casm-wiki/weights-and-deploy.md:113)).  
   **Fix:** Delete option B and ensure the service account lacks upload-capable SSH credentials; permit only dry-run arguments.

9. **Browser/API memory targets are incompatible with unbounded waterfalls.** A 14-day minute-resolution spectrum is ~62 million cells per input before JSON; a 30-day full-frequency baseline is ~463 MB binary, contradicting the 1 GB web target ([plan:42](/home/casm/.claude/plans/virtual-humming-magpie.md:42), [plan:210](/home/casm/.claude/plans/virtual-humming-magpie.md:210)).  
   **Fix:** Define server-side pyramids, hard point/byte limits, pagination, and resolution selected by viewport.

10. **The candidate app is neither read-only nor mount-safe.** It writes labels/FRB rows and uses absolute redirects that escape `/cands` ([app.py:12](/home/casm/software/dev/casm_t3/casm_t3/web/app.py:12), [app.py:225](/home/casm/software/dev/casm_t3/casm_t3/web/app.py:225), [app.py:246](/home/casm/software/dev/casm_t3/casm_t3/web/app.py:246)).  
    **Fix:** Port a router with prefix-aware URLs and explicitly preserve or disable labeling; do not claim a direct read-only mount.

## Go/no-go

**NO-GO as written.** M0 may proceed only after the persistence, Kafka, live-file, job-supervision, and upload-safety contracts are designed; M2–M4 require measured I/O, latency, and peak-RSS benchmarks.
