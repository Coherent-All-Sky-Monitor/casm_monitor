// Colour recipe for the Visibilities waterfalls (matrix panels and the
// expanded cross-detail view), matching the house recipe in
// casm_vis_analysis/src/casm_vis_analysis/plotting/waterfall.py: autos are a
// viridis dB power waterfall, phase is a fixed +/-180 deg (or +/-pi rad)
// RdBu_r waterfall, real/imag are a per-panel symmetric RdBu_r waterfall
// (range = the 99th percentile of |value|, computed client-side from the
// returned z, falling back to 1.0 for an all-zero/empty panel), and
// amplitude/coherence stay viridis with Plotly's own autorange.

import { COLORSCALE, DIVERGING_COLORSCALE } from "./theme";
import type { VisQuantity, VisUnits } from "./types";

export interface CellColor {
  colorscale: string;
  reversescale: boolean;
  /** Undefined means "let Plotly autorange", used for amp/coh. */
  zmin?: number;
  zmax?: number;
}

/** The fixed phase range for the given units: +/-180 degrees or +/-pi
 * radians, per the house recipe (never data-dependent). */
export function phaseRange(units: VisUnits): [number, number] {
  return units === "rad" ? [-Math.PI, Math.PI] : [-180, 180];
}

/** The 99th percentile of |value| over a waterfall's cells, ignoring nulls
 * and non-finite values; 1.0 if there is nothing to measure. Mirrors
 * `vlim = np.nanpercentile(np.abs(re), 99) or 1.0` in waterfall.py. */
export function percentileAbs99(z: (number | null)[][]): number {
  const vals: number[] = [];
  for (const row of z) {
    for (const v of row) {
      if (v !== null && Number.isFinite(v)) vals.push(Math.abs(v));
    }
  }
  if (vals.length === 0) return 1.0;
  vals.sort((a, b) => a - b);
  const idx = Math.min(vals.length - 1, Math.floor(0.99 * vals.length));
  const p = vals[idx];
  return p > 0 ? p : 1.0;
}

/**
 * The colour recipe for one waterfall panel/plot. `isDiagonal` autos are
 * always treated as amplitude-in-dB viridis regardless of the quantity the
 * rest of the matrix is showing (the house figure's diagonal convention).
 */
export function cellColor(
  quantity: VisQuantity,
  units: VisUnits,
  z: (number | null)[][],
  isDiagonal: boolean,
): CellColor {
  if (isDiagonal) {
    return { colorscale: COLORSCALE, reversescale: false };
  }
  if (quantity === "phase") {
    const [lo, hi] = phaseRange(units);
    return { colorscale: DIVERGING_COLORSCALE, reversescale: true, zmin: lo, zmax: hi };
  }
  if (quantity === "real" || quantity === "imag") {
    const v = percentileAbs99(z);
    return { colorscale: DIVERGING_COLORSCALE, reversescale: true, zmin: -v, zmax: v };
  }
  // amp, coh
  return { colorscale: COLORSCALE, reversescale: false };
}

/** `z[time][freq]` (the API convention) -> `z[freq][time]`, for panels that
 * put time on x and frequency on y (Plotly heatmaps index `z[y][x]`). */
export function transposeZ<T>(z: T[][]): T[][] {
  if (z.length === 0) return [];
  const nT = z.length;
  const nF = z[0].length;
  const out: T[][] = new Array(nF);
  for (let f = 0; f < nF; f++) {
    const row = new Array(nT);
    for (let t = 0; t < nT; t++) row[t] = z[t][f];
    out[f] = row;
  }
  return out;
}
