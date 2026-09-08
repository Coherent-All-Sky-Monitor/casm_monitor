// One paragraph per placeholder tab, condensed from docs/plan.md "What it
// looks like". Update here if the plan section changes.

export const PLACEHOLDER_COPY: Record<string, { title: string; milestone: string; body: string }> = {
  snaps: {
    title: "SNAPs",
    milestone: "M1",
    body:
      "One card per board (.52 .51 .62 .73, plus relays .59 .68 .69 shown as " +
      "PPS-only): a live correlator-side Kafka bandpass (10 s, 3072 ch, no " +
      "hardware contact) and a board-side zapdos read (4096 ch, ADC RMS, EQ " +
      "epoch) taken hourly or on a serialized, rate-limited \"Read boards now\" " +
      "click. A history toggle switches the card to a time slider over the " +
      "store: past spectra, a waterfall, the night-median trend with EQ/gain " +
      "epochs marked, and a flat/pinned/all-zero diagnosis legend, labelled " +
      "with the antenna's ant/plank/slot/input from AntennaMapping.",
  },
  vis: {
    title: "Visibilities",
    milestone: "M2",
    body:
      "The cached sub-matrix of every wired input, with a selector for the " +
      "live beamforming set, all wired inputs, or all 48 SNAP inputs computed " +
      "on demand from raw files. Views are autocorr grids, cross amplitude " +
      "and phase matrices, per-baseline phase-vs-frequency and waterfalls, a " +
      "night coherence matrix, and the missing-subband panel. Every view has " +
      "a quantity toggle (amplitude/phase/real/imag/coherence) and a units " +
      "toggle (linear/dB/log10, degrees/radians, with a raw/fringe-stopped/" +
      "cal-divided reference), state kept in the URL, plus a time slider over " +
      "the history store.",
  },
  search: {
    title: "Search",
    milestone: "M2b",
    body:
      "Live distributions of the raw hella (T1) candidates tailed from both " +
      "nodes' cands_<UTC_START>.dat.{0..7} files: histograms and 2-D " +
      "densities over SNR, width, DM, beam and time, per-node/per-job rates, " +
      "the 10k-cap saturation indicator, and the T2 funnel from gulp_stats " +
      "(cands to clusters to stored to triggers). Linear/log axis and time " +
      "window toggles, per-gulp binned counts kept forever, raw rows for 7 " +
      "days.",
  },
  imaging: {
    title: "Imaging",
    milestone: "M4",
    body:
      "An all-sky dirty image per integration (l/m zenithal projection) with " +
      "the deployed cal, source markers, a PSF ceiling and a 24 h movie " +
      "strip, plus per-source cutouts around whichever of Sun/Cyg A/Cas A/" +
      "Tau A is up. History keeps one thumbnail per integration; wall-clock " +
      "and memory are benchmarked before this ships since imaging cost per " +
      "integration is the one open unknown in the plan.",
  },
  cal: {
    title: "Calibration",
    milestone: "M3",
    body:
      "A job runner around bf_weights_generator's canonical recipe: pick " +
      "window, static window, antenna set, ref ant and beam layout for a " +
      "Sun solve (the only calibrator the array is sensitive enough for " +
      "today; other sources appear greyed as \"not yet\"), a live log while " +
      "the job runs, a results page with the driver's own diagnostic figures " +
      "and the six-check verification status, a staged dry-run, and an " +
      "Upload button gated by the safeguards in docs/plan.md Decisions — a " +
      "human click is always the action that deploys weights.",
  },
  cands: {
    title: "Candidates",
    milestone: "M5",
    body:
      "casm_t3's candidate web routes ported as a prefix-aware FastAPI " +
      "router under /cands, keeping the same labeling workflow and sqlite so " +
      "operators can triage T2/T3 events without a separate t3-web instance; " +
      "t3-web stays up in parallel until this route is verified against it.",
  },
};
