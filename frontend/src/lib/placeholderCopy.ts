// One sentence per tab that is not built yet, condensed from docs/plan.md
// "What it looks like". Update here if the plan section changes.

export const PLACEHOLDER_COPY: Record<string, string> = {
  vis: "The visibilities of every wired input, as autocorrelation grids, amplitude and phase matrices, per-baseline phase against frequency and waterfalls, over the history store.",
  search:
    "The live distributions of the raw hella candidates from both nodes over SNR, width, DM, beam and time, with the T2 funnel from candidates to clusters to triggers.",
  imaging:
    "An all-sky dirty image per integration with the deployed calibration, source markers and cutouts around whichever of the Sun, Cyg A, Cas A or Tau A is up.",
  cal: "The standard Sun calibration run on demand through the canonical recipe, with its own diagnostic figures, the six verification checks, a staged dry run and an upload that only a human click performs.",
  cands:
    "The T3 candidate pages, served here on the same database so events can be triaged without a separate t3-web instance.",
};
