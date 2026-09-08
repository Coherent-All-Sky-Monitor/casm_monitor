# Kafka antenna-stat message schema (traced 2026-09-08, read-only)

Source trace by Luna (Codex grunt) over `/home/casm/software/fourier-space/sources/casm/src/`
and `opt/pantheon/include/demeter/Util/BandpassStats.h`; captured samples in
`tests/fixtures/kafka/` (`meta.json` carries offsets and create times).

- Topics `casm_antenna_bp|ts|hg`, one partition each, key constant `casm_antenna`.
- `bp` payload: float32 flattened `[nsig][npol][nchan]`, row = `isig*npol + ipol`.
  Observed 270336 B = 22 x 3072. `nsig` = header `NANT`, `npol` = `NPOL`
  (`BlockFormatAntenna.cpp:33-36`). The code's input mapping
  `isig=(isnap*6+iinput)%nsig, ipol=(isnap*6+iinput)/nsig` (`:148-154`) and its
  `NANT % 6 == 0` check (`:46-47`) do not reproduce 22 rows, so the deployed
  binary/config differs from this source revision. **M1 must map rows to
  correlator inputs empirically** (cross-match each row's bandpass shape with
  the visibility-file autos of the same minute; 16 of 22 rows were populated
  in the 2026-09-08 sample, consistent with the 16-17 live antennas).
- Every producer (streams 0-5; corr1 runs 0-2, corr2 3-5, all to casm-corr1:9092)
  publishes the FULL 3072-channel array with only its own 512-channel subband
  populated (`AppAntennaStat.cpp:43-66`). The subband is identified by a Kafka
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
