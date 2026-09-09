// Every prose sentence the Calibration page says, mirroring lib/visText.ts
// and lib/snapText.ts: the page reads as sentences (DESIGN.md #5), not a
// dashboard, so the strings live in one place rather than scattered inline
// template literals.

import { formatUtcStamp } from "./statusSentence";
import type { CalBuildSummary, CalDefaultsResponse } from "./types";

/** "2026-09-08T19:50:00Z" -> "19:50". */
function hhmm(iso: string): string {
  const m = iso.match(/T(\d{2}):(\d{2})/);
  return m ? `${m[1]}:${m[2]}` : iso;
}

/** The New solve section's lede: "The Sun reaches its maximum at 19:50 UTC
 * today; the default window is 19:20 to 20:20 UTC, the static window is
 * last night 03:00 to 03:30 UTC, 17 antennas from the layout, reference
 * antenna 9." */
export function defaultsSentence(d: CalDefaultsResponse): string {
  const [w0, w1] = d.source_window;
  const staticClause = d.static_window
    ? `the static window is ${d.static_note || `${hhmm(d.static_window[0])} to ${hhmm(d.static_window[1])} UTC`}`
    : `no static window (${d.static_note || "none found for this date"})`;
  return (
    `The Sun reaches its maximum at ${hhmm(d.sun_max_utc)} UTC today; ` +
    `the default window is ${hhmm(w0)} to ${hhmm(w1)} UTC, ${staticClause}, ` +
    `${d.antennas_note}, reference antenna ${d.ref_ant}.`
  );
}

/** "solve quality, not beam quality" caption used next to rank-1 median
 * anywhere it appears (builds table header, rank1_vs_freq figure caption). */
export const RANK1_CAPTION = "solve quality, not beam quality";

export const RANK1_FIGURE_CAPTION =
  "rank-1 measures the solve, not the beam; see the wiki's rank1-metric-caveat.";

function fmtMB(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

function fmtWall(s: number): string {
  if (s < 90) return `${Math.round(s)} seconds`;
  return `${Math.round(s / 60)} minutes`;
}

/** The build page's summary sentence: wall time, peak memory, cal and
 * weights paths. */
export function buildSummarySentence(summary: CalBuildSummary): string {
  return (
    `This solve took ${fmtWall(summary.wall_s)} and peaked at ${fmtMB(summary.peak_rss_mb)}. ` +
    `Wrote cal to ${summary.cal_h5} and weights to ${summary.weights_h5}.`
  );
}

/** "1950 to 2020 UTC" window cell for the builds table. */
export function windowCell(sourceWindow: [string, string]): string {
  return `${formatUtcStamp(sourceWindow[0]).slice(11, 16)}-${formatUtcStamp(sourceWindow[1]).slice(11, 16)} UTC`;
}
