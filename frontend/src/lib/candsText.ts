// Every prose sentence the Candidates tab says, per frontend/DESIGN.md rule
// 5 ("state is a sentence, not a colour-coded widget").

import type { CandEventRow, CandNowSnapshot, CandStatsResponse } from "./types";

/** "42 events in the last window, 6 tier A, 30 dumped, 5 labelled." */
export function eventsSentence(rows: CandEventRow[]): string {
  if (rows.length === 0) return "No events match this filter.";
  const tierA = rows.filter((r) => r.tier === "A").length;
  const dumped = rows.filter((r) => r.outcome === "dumped").length;
  const labelled = rows.filter((r) => r.label !== null).length;
  return (
    `${rows.length} event${rows.length === 1 ? "" : "s"}, ${tierA} tier A, ` +
    `${dumped} dumped, ${labelled} labelled.`
  );
}

/** "9,200 gulps, 260,000 candidates, 900 clusters, 60 trigger-worthy over
 * the last 24 h." */
export function statsSentence(s: CandStatsResponse): string {
  const w = s.win;
  return (
    `${w.gulps.toLocaleString()} gulps, ${w.cands.toLocaleString()} candidates, ` +
    `${w.clusters.toLocaleString()} clusters, ${w.would.toLocaleString()} trigger-worthy ` +
    `over the last ${s.win_label}.`
  );
}

/** "LST 04:12; Sun at 12 deg alt, transits in 1.2 h; Cas A at -20 deg alt,
 * transits 5.0 h ago." */
export function nowSentence(now: CandNowSnapshot | null): string {
  if (!now) return "Live sky position is unavailable right now.";
  const lstH = Math.floor(now.lst_h);
  const lstM = Math.round((now.lst_h - lstH) * 60);
  const parts = now.sources
    .slice(0, 3)
    .map((s) => `${s.name} at ${s.alt.toFixed(0)} deg alt, transits ${s.transit}`);
  return `LST ${String(lstH).padStart(2, "0")}:${String(lstM).padStart(2, "0")}; ${parts.join("; ")}.`;
}
