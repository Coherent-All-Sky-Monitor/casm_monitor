# Kafka antenna-stat message schema (traced 2026-09-08, read-only)

Source trace by Luna (Codex grunt) over `/home/casm/software/fourier-space/sources/casm/src/`
and `opt/pantheon/include/demeter/Util/BandpassStats.h`; captured samples in
`tests/fixtures/kafka/` (`meta.json` carries offsets and create times).

- Topics `casm_antenna_bp|ts|hg`, one partition each, key constant `casm_antenna`.
- `bp` payload: float32 flattened `[nsig][npol][nchan]`, row = `isig*npol + ipol`.
  Observed 270336 B; **the reshape is 66 x 2 x 512, not 22 x 3072** — see the
  measured section below, the record headers give the shape. `nsig` = header `NANT`, `npol` = `NPOL`
  (`BlockFormatAntenna.cpp:33-36`). The code's input mapping
  `isig=(isnap*6+iinput)%nsig, ipol=(isnap*6+iinput)/nsig` (`:148-154`) and its
  `NANT % 6 == 0` check (`:46-47`) do not reproduce 22 rows, so the deployed
  binary/config differs from this source revision. **M1 must map rows to
  correlator inputs empirically** (cross-match each row's bandpass shape with
  the visibility-file autos of the same minute; 16 of 22 rows were populated
  in the 2026-09-08 sample, consistent with the 16-17 live antennas).
- Every producer (streams 0-5; corr1 runs 0-2, corr2 3-5, all to casm-corr1:9092)
  publishes ONLY its own 512-channel subband (the "full 3072 with one subband
  populated" reading of `AppAntennaStat.cpp:43-66` is wrong for the deployed
  binary: the `nchan` header is 512). The subband is identified by a Kafka
  RECORD HEADER `offset` = 512 x stream (`BlockFormatAntenna.cpp:276-285`).
  Consumers must read record headers (kafka-python: `record.headers`), then
  assemble six messages per 10 s frame; the memory note
  `missing-subbands-diagnostic.md` documents sb1/sb5 arriving all-zero on 2026-06-12.
- No timestamp/UTC_START in the payload; use the Kafka record timestamp
  (CreateTime) and the `offset` header. `ts` payload = 813120 B, `hg` = 26400 B
  (layouts per `TimeSeriesStats.h` / `HistogramStats.h`: min, mean, max,
  block_stddev, block_mean; bins, clip counts, hist).
- Subband centre frequencies (memory `missing-subbands-diagnostic.md`): sb0 476.26,
  sb1 460.64, sb2 445.31, sb3 429.69, sb4 414.06, sb5 398.44 MHz; band descending.


## Measured against the live broker, 2026-09-08 (M1 implementation)

Read-only, group-less consume of `casm-corr1:9092` (`assign()` on partition 0,
no commits) from `casm_monitor.collectors.kafka_bp`. Fixture headers:
`tests/fixtures/kafka/bp_headers.json` (hex of the raw header bytes).

**Headers are binary, not text.** `offset`, `nsig`, `npol`, `ndim`, `nbin`,
`ndat`, `nchan` are int32 LE; `bw`, `out_bw`, `freq`, `tsamp` float64 LE;
`timestamp` int64 LE. One live record decodes to

    offset 2560  nsig 66  npol 2  ndim 2  nbin 16  ndat 512  nchan 512
    bw -15.625  out_bw -15.625  freq 398.4375  tsamp 131.072  timestamp 1788909540

so the `bp` body is `nsig x npol x nchan` = 66 x 2 x 512 float32 = 270336 B, and
each record carries **only its own subband's 512 channels**. The six `offset`
values seen are 0, 512, 1024, 1536, 2048, 2560 with `freq` 476.2625, 460.6375,
445.3125, 429.6875, 414.0625, 398.4375 MHz — one per stream, descending band.
Only pol 0 is populated (rows 0, 2, 4, ... 94 = 48 signals = 4 SNAPs x 12
inputs); rows 96..131 are zero.

Two consequences the implementation depends on:

* the collector assembles a frame by writing each record into
  `full[:, offset:offset+512]`, giving a (48, 3072) array once all six arrive;
* **frames are keyed on the `timestamp` HEADER, never on CreateTime.** The
  header timestamp increments in exact 10 s steps and is identical across the
  six producers, while CreateTime trails it by ~28 s (corr1 streams 0-2) to
  ~96 s (corr2 streams 3-5). Rounding CreateTime to the 10 s cadence would
  split one frame across several buckets.

The producers' own `freq` header disagrees with the correlator axis by ~0.3 MHz
on subband 0 (484.075 vs 484.375 MHz for the top of the band), so the frequency
axis used everywhere is casm_io's: 484.375 MHz descending, 93.75/3072 MHz per
channel.

### Row -> correlator input mapping

**The mapping is `row = 2 x packet_idx`** (i.e. `isig = packet_idx = antenna -
1`, pol 0) — the source formula `isig=(isnap*6+iinput)%nsig` does not
reproduce it. As of the M1 integration pass (2026-09-08) this formula is the
PRIMARY mapping (`casm_monitor.collectors.rowmap.formula_row`): every wired
input is assigned its row unconditionally, straight from the layout, with no
correlation and no vis file needed on the request path.

The shape-correlation method described below (`casm_monitor/collectors/rowmap.py`,
`validate_rows`/`validate_row_map`) is now a VALIDATION that runs daily and on
every `obs_restart` event, not a gate: each formula row's bandpass is log10'd
over 400-480 MHz, median-normalised, the common bandpass (median of the 12
strongest spectra on each side) is subtracted, and the result is Pearson
correlated against the autocorrelation of every wired input read with casm_io
from the newest complete visibility file. Without the common-mode step every
input correlates with every other above 0.9 and no row's argmax is
distinctive; with it the correct assignment wins by 0.1-0.5. If the row's own
expected input is the argmax, the row is `formula+verified`; if some other
input scores higher, it is `mismatch` (an operator-visible red flag — the
lookup still uses the formula row, this only says the two disagree). A row
never validated yet (service just started, or the vis file was unavailable)
reports plain `formula`. There is no longer a correlation/margin floor that
can leave a wired input unmapped.

Result of the correlation as measured (2026-09-08 16:29 PDT, frame 1788910190,
obs 2026-09-04-16:43:47 file 83, 24 wired inputs, 48 populated rows), now read
as a validation pass rather than an acceptance gate. Every row's argmax already
landed on its own index — with the old 0.9/0.1 floor removed, all 24 wired
inputs come back `formula+verified`, including the four that used to be
rejected on margin alone:

| row | isig | packet_idx | corr (own) | best other | status (old gate) | status (validation) | antenna |
|----:|-----:|-----------:|-----:|----------:|--------|--------|---------|
| 18 | 9 | 9 | 0.982 | 0.859 | mapped | formula+verified | ant 10 N21E2 |
| 28 | 14 | 14 | 0.966 | 0.798 | mapped | formula+verified | ant 15 N11E5 |
| 34 | 17 | 17 | 0.983 | 0.482 | mapped | formula+verified | ant 18 N01E3 |
| 36 | 18 | 18 | 0.945 | 0.757 | mapped | formula+verified | ant 19 N11E2 |
| 44 | 22 | 22 | 0.936 | 0.803 | mapped | formula+verified | ant 23 N11E6 |
| 46 | 23 | 23 | 0.955 | 0.818 | mapped | formula+verified | ant 24 N11E4 |
| 50 | 25 | 25 | 0.948 | 0.324 | mapped | formula+verified | ant 26 N16E1 |
| 52 | 26 | 26 | 0.967 | 0.798 | mapped | formula+verified | ant 27 N02E1 |
| 58 | 29 | 29 | 0.990 | 0.878 | mapped | formula+verified | ant 30 N16E5 |
| 62 | 31 | 31 | 0.953 | 0.680 | mapped | formula+verified | ant 32 N21E3 |
| 70 | 35 | 35 | 0.903 | 0.738 | mapped | formula+verified | ant 36 N16E6 |
| 74 | 37 | 37 | 0.985 | 0.827 | mapped | formula+verified | ant 38 N02E5 |
| 82 | 41 | 41 | 0.961 | 0.770 | mapped | formula+verified | ant 42 N03E5 |
| 88 | 44 | 44 | 0.989 | 0.874 | mapped | formula+verified | ant 45 N04E6 |
| 78 | 39 | 39 | 0.993 | 0.928 | unmapped (margin 0.065) | formula+verified | ant 40 |
| 86 | 43 | 43 | 0.996 | 0.931 | unmapped (margin 0.065) | formula+verified | ant 44 |
| 4 | 2 | 2 | 0.839 | 0.799 | unmapped (dead feed) | formula+verified | dead feed (gated 2026-08-03) |
| 0 | 0 | 0 | 0.267 | 0.219 | unmapped (dead feed) | formula+verified | dead feed (ant 1, no sky) |

An earlier one-off run with the previous complete file (index 82) recovered
all 24 wired inputs correctly on argmax too, so the assignment is stable
across observations; the old acceptance gate was only ever rejecting inputs
whose bandpass genuinely has no distinctive shape to match (a dead feed) or
whose margin over the runner-up happened to be small even though the argmax
was still correct. `rowmap.MIN_CORR`/`MIN_MARGIN` no longer exist.

The mapping, its scores and the source (obs, file index) live in the store's
`kafka_row_map` table (`casm_monitor.collectors.rowmap.current_mapping`
overlays them on the unconditional formula assignment), are re-derived daily
and on every `obs_restart` event, and:

* a change to which packet_idx a row's formula assigns (a layout change)
  raises `kafka_row_map_changed`;
* a validation disagreeing with the formula for a row not previously flagged
  raises `kafka_row_map_mismatch` (detail carries the affected rows).

`/api/snaps/mapping` and the `mapping` field of `/api/snaps/live` expose the
current per-row status (`"formula"` | `"formula+verified"` | `"mismatch"`, or
`"unmapped"` for an input that resolves to no wired packet_idx at all).
