// Every string the Search page says: the one section sentence built from
// /api/search/summary, and the hottest-beams sentence under the occupancy
// heatmap, per frontend/DESIGN.md rule 5 ("state is a sentence").

import type { SearchSummaryResponse } from "./types";

function formatWindow(t0: string, t1: string): string {
  const ms = new Date(t1).getTime() - new Date(t0).getTime();
  const minutes = ms / 60_000;
  if (minutes < 90) return `${Math.round(minutes)} minutes`;
  if (minutes < 24 * 60) return `${(minutes / 60).toFixed(1)} hours`;
  return `${(minutes / 1440).toFixed(1)} days`;
}

/** "12,340 candidates in the last hour, 205 per minute across 8 jobs; corr1
 * at SNR 15, DM 20; corr2 at SNR 15, DM 20; T2 kept 46 clusters and
 * triggered 3 dumps." */
export function summarySentence(s: SearchSummaryResponse): string {
  const minutes = Math.max((new Date(s.t1).getTime() - new Date(s.t0).getTime()) / 60_000, 1 / 60);
  const ratePerMin = s.n_cands / minutes;
  const nJobs = s.per_job.length;
  const parts: string[] = [
    `${s.n_cands.toLocaleString()} candidates in the last ${formatWindow(s.t0, s.t1)}, ` +
      `${ratePerMin.toFixed(0)} per minute across ${nJobs} job${nJobs === 1 ? "" : "s"}`,
  ];
  const thr = s.thresholds;
  for (const node of Object.keys(thr)) {
    parts.push(`${node} at SNR ${thr[node].snr}, DM ${thr[node].dm_min}`);
  }
  parts.push(
    `T2 kept ${s.funnel.n_stored.toLocaleString()} clusters and triggered ${s.funnel.n_triggers.toLocaleString()} dump${
      s.funnel.n_triggers === 1 ? "" : "s"
    }`,
  );
  return `${parts.join("; ")}.`;
}

/** "the hottest beams are 96, 233 and 455." from a 512-entry counts array. */
export function hottestBeamsSentence(counts: number[]): string {
  const ranked = counts
    .map((n, beam) => ({ n, beam }))
    .filter((b) => b.n > 0)
    .sort((a, b) => b.n - a.n)
    .slice(0, 3);
  if (ranked.length === 0) return "No candidates in this window.";
  const beams = ranked.map((b) => b.beam);
  if (beams.length === 1) return `The hottest beam is ${beams[0]}.`;
  const list = `${beams.slice(0, -1).join(", ")} and ${beams[beams.length - 1]}`;
  return `The hottest beams are ${list}.`;
}
