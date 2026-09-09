// Every string the Visibilities page says: panel titles, cross-grid axis
// labels and the one muted section line above the panel grid (integration
// time, obs UTC_START, reference used), mirroring lib/snapText.ts.

import { formatUnixUtc, formatUtcStart } from "./statusSentence";
import type { VisInputInfo, VisObsInfo, VisRef } from "./types";

/** `ant 26  N16E1  (pkt 25)`, the autos panel title, matching the SNAPs
 * house-figure format exactly (lib/snapText.ts panelTitle). */
export function visPanelTitle(input: VisInputInfo): string {
  if (input.antenna !== null) {
    const station = input.station ? `  ${input.station}` : "";
    return `ant ${input.antenna}${station}  (pkt ${input.packet_idx})`;
  }
  return `pkt ${input.packet_idx}  (unwired)`;
}

/** The short row/column label in the crosses triangle: the antenna number,
 * or the packet index if this input carries no antenna. */
export function crossAxisLabel(input: VisInputInfo): string {
  return input.antenna !== null ? String(input.antenna) : `p${input.packet_idx}`;
}

const REF_CLAUSE: Record<VisRef, string> = {
  raw: "raw",
  sun: "fringe-stopped toward the Sun",
  cal: "divided by cal",
};

/** The one muted section line above the Visibilities panel grid: the
 * integration time in UTC, the obs UTC_START, and the reference in force. */
export function integrationSentence(
  ts: number | null,
  obs: VisObsInfo | null,
  ref: VisRef,
  flags: Record<string, unknown>,
): string {
  const parts: string[] = [];
  const when = ts !== null ? formatUnixUtc(ts) : null;
  parts.push(when ? `Integration ${when}.` : "No cached integration yet.");
  const start = obs?.utc_start ? formatUtcStart(obs.utc_start) : null;
  if (start) parts.push(`Observing since ${start} UTC.`);
  const cal = typeof flags.cal_file === "string" ? flags.cal_file : null;
  const refClause = ref === "cal" && cal ? `divided by cal ${cal}` : REF_CLAUSE[ref];
  parts.push(`Reference: ${refClause}.`);
  if (ref === "sun" && flags.sun_below_horizon === true) {
    parts.push("The Sun is below the horizon right now, so the fringe-stopped reference is not meaningful.");
  }
  return parts.join(" ");
}

/** "2026-09-08 02:00-05:00 PT" for the coherence view's default-window
 * sentence. PT is treated as UTC-7 (PDT); the array is not sensitive enough
 * yet for a DST-correctness bug here to matter operationally. */
const PT_OFFSET_H = 7;

export function lastNightWindowPT(): { t0: string; t1: string; sentence: string } {
  const now = new Date();
  const todayUtc0200PT = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 2 + PT_OFFSET_H, 0, 0),
  );
  // If 02:00 PT today has not happened yet (in UTC terms), use last night's.
  const end = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 5 + PT_OFFSET_H, 0, 0),
  );
  const dayOffset = end.getTime() > now.getTime() ? -1 : 0;
  const t0 = new Date(todayUtc0200PT.getTime() + dayOffset * 86400_000);
  const t1 = new Date(end.getTime() + dayOffset * 86400_000);
  const dateStr = new Date(t0.getTime() + PT_OFFSET_H * 3600_000).toISOString().slice(0, 10);
  return {
    t0: t0.toISOString(),
    t1: t1.toISOString(),
    sentence: `Last night, ${dateStr} 02:00-05:00 PT.`,
  };
}
