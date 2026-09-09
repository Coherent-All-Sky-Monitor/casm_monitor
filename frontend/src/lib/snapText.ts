// Every string the SNAPs page says, in one place: panel titles, the one
// muted section line per board, and the short alert sentences that replace
// the old badges, chips and coloured strips.

import { formatAge } from "./snapConstants";
import type {
  SnapBoardInfo,
  SnapBoardReadResponse,
  SnapInputInfo,
  SnapLiveResponse,
} from "./types";

/** `ant 26  N16E1  (pkt 25)`, the panel title of the house figures. Inputs
 * with no antenna fall back to their ADC channel so the panel is still
 * identifiable. */
export function panelTitle(input: SnapInputInfo, rms?: number | null): string {
  const suffix = rms === null || rms === undefined ? "" : `  rms ${rms.toFixed(1)}`;
  if (input.antenna !== null) {
    const station = input.station ? `  ${input.station}` : "";
    return `ant ${input.antenna}${station}  (pkt ${input.packet_idx})${suffix}`;
  }
  if (input.packet_idx !== null) {
    return `adc ${input.adc}  (pkt ${input.packet_idx})${suffix}`;
  }
  return `adc ${input.adc}  (unwired)${suffix}`;
}

/** Inputs worth showing by default: those carrying an antenna, ordered by
 * antenna number. The rest (unwired or gated feeds) come after, by ADC, and
 * only when the operator asks for them. */
export function orderInputs(inputs: SnapInputInfo[], showUnwired: boolean): SnapInputInfo[] {
  const wired = inputs
    .filter((i) => i.antenna !== null)
    .sort((a, b) => (a.antenna as number) - (b.antenna as number));
  if (!showUnwired) return wired;
  const rest = inputs.filter((i) => i.antenna === null).sort((a, b) => a.adc - b.adc);
  return [...wired, ...rest];
}

function median(values: number[]): number {
  const s = [...values].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

export interface AdcSummary {
  /** "0.5-1.5", the bulk range in LSB. */
  range: string | null;
  /** "ant 26 at 14.8" for each value well outside the bulk. */
  outliers: string[];
}

/** The board's ADC RMS as one phrase: the bulk range, plus any input sitting
 * far above it named individually (the ant-26 excess case). */
export function summariseAdcRms(
  adcRms: (number | null)[] | null,
  inputs: SnapInputInfo[],
): AdcSummary {
  if (!adcRms) return { range: null, outliers: [] };
  const present = adcRms
    .map((v, adc) => ({ v, adc }))
    .filter((e): e is { v: number; adc: number } => e.v !== null && !Number.isNaN(e.v));
  if (present.length === 0) return { range: null, outliers: [] };
  const cut = Math.max(3 * median(present.map((e) => e.v)), 1);
  const bulk = present.filter((e) => e.v <= cut);
  const outliers = present.filter((e) => e.v > cut);
  const base = bulk.length ? bulk : present;
  const lo = Math.min(...base.map((e) => e.v));
  const hi = Math.max(...base.map((e) => e.v));
  return {
    range: `${lo.toFixed(1)}-${hi.toFixed(1)}`,
    outliers: outliers.map((e) => {
      const input = inputs.find((i) => i.adc === e.adc);
      const who = input?.antenna !== null && input?.antenna !== undefined ? `ant ${input.antenna}` : `adc ${e.adc}`;
      return `${who} at ${e.v.toFixed(1)}`;
    }),
  };
}

/** The one muted line above a board's panels. */
export function boardLine(
  board: SnapBoardInfo,
  read: SnapBoardReadResponse | null,
  inputs: SnapInputInfo[],
): string {
  const parts: string[] = [`SNAP ${board.feng_id ?? "?"}`, board.ip];
  if (board.slot && board.slot !== "?") parts.push(`slot ${board.slot}`);
  parts.push(read?.age_s != null ? `read ${formatAge(read.age_s)}` : "never read");
  const adc = summariseAdcRms(read?.adc_rms ?? null, inputs);
  if (adc.range) parts.push(`ADC ${adc.range} LSB`);
  parts.push(...adc.outliers);
  if (read?.pps?.ok === true) parts.push("PPS ok");
  const epoch = read?.eq_epoch;
  if (epoch) parts.push(`EQ epoch ${epoch}`);
  return `${parts.join(", ")}.`;
}

/** The muted line for an antenna-less relay board. */
export function relayLine(board: SnapBoardInfo, read: SnapBoardReadResponse | null): string {
  const parts: string[] = [`SNAP relay ${board.ip}`];
  parts.push(read?.age_s != null ? `read ${formatAge(read.age_s)}` : "never read");
  if (read?.pps?.ok === true) parts.push("PPS ok");
  if (read?.programmed === true) parts.push("programmed");
  return `${parts.join(", ")}.`;
}

function listSubbands(indices: number[]): string {
  if (indices.length === 1) return `subband ${indices[0]} is dark.`;
  const head = indices.slice(0, -1).join(", ");
  return `subbands ${head} and ${indices[indices.length - 1]} are dark.`;
}

/** Subband indices the correlator is not delivering for this board. */
export function darkSubbands(live: SnapLiveResponse | null): number[] {
  if (!live?.subbands_ok) return [];
  return live.subbands_ok.map((ok, i) => (ok ? -1 : i)).filter((i) => i >= 0);
}

/** Everything wrong with this board, one short sentence each. */
export function boardProblems(
  board: SnapBoardInfo,
  read: SnapBoardReadResponse | null,
  live: SnapLiveResponse | null,
  correlatorStaleS: number,
): string[] {
  const out: string[] = [];
  if (!read) {
    out.push("This board has never been read.");
  } else {
    if (read.feng_id_hw !== null && read.feng_id_cfg !== null && read.feng_id_hw !== read.feng_id_cfg) {
      out.push(`feng id ${read.feng_id_hw} disagrees with config ${read.feng_id_cfg}.`);
    }
    if (read.pps?.ok === false) out.push("PPS is not locked.");
    if (read.programmed === false) out.push("The board is not programmed.");
  }
  const dark = darkSubbands(live);
  if (dark.length) out.push(listSubbands(dark));
  if (live && (live.age_s === null || live.age_s > correlatorStaleS)) {
    out.push(
      live.age_s === null
        ? "No correlator frame has ever arrived for this board."
        : `No correlator frame for ${Math.round(live.age_s / 60)} minutes.`,
    );
  }
  for (const li of live?.inputs ?? []) {
    if (li.mapping !== "mismatch") continue;
    const input = board.inputs?.find((i) => i.adc === li.adc);
    const who = input?.antenna != null ? `ant ${input.antenna}` : `adc ${li.adc}`;
    out.push(`The row mapping for ${who} disagrees with the formula.`);
  }
  return out;
}
