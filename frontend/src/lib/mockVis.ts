// Mock backend for the Visibilities tab, selected by the `?mock=1` URL flag
// while the real /api/vis/* routes are implemented against docs/api-vis.md
// concurrently. Shapes mirror lib/types.ts exactly so VisPage.tsx can swap
// real <-> mock with one branch. Never imported by lib/api.ts itself.

import type {
  VisBaseline,
  VisCoherenceResponse,
  VisInputInfo,
  VisInputsResponse,
  VisMatrixResponse,
  VisPairs,
  VisQuantity,
  VisRef,
  VisSet,
  VisSpectraResponse,
  VisTimesResponse,
  VisUnits,
  VisWaterfallResponse,
} from "./types";

const NETWORK_DELAY_MS = 60;

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), NETWORK_DELAY_MS));
}

function noise(seed: number): number {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

const CORR_BAND_MHZ: [number, number] = [390.6, 484.4];
const N_WIRED = 24;
const N_LIVE = 17;
const OBS_UTC_START = "2026-09-08T09:00:00Z";
const INTEGRATION_S = 137;

function linspaceDesc(hi: number, lo: number, n: number): number[] {
  const out = new Array<number>(n);
  const step = (hi - lo) / (n - 1);
  for (let i = 0; i < n; i++) out[i] = hi - i * step;
  return out;
}

const STATIONS = ["N21E1", "N21E3", "N16E1", "N16E5", "N02E2", "S2A1", "N03A1", "N11E4", "N16E9"];

let inputsCache: VisInputInfo[] | null = null;

function buildInputs(): VisInputInfo[] {
  if (inputsCache) return inputsCache;
  inputsCache = Array.from({ length: N_WIRED }, (_, packet_idx) => ({
    packet_idx,
    antenna: packet_idx + 1,
    station: STATIONS[packet_idx % STATIONS.length],
    in_bf: packet_idx % Math.ceil(N_WIRED / N_LIVE) !== 0 ? true : packet_idx < N_LIVE,
  }));
  return inputsCache;
}

export function mockGetVisInputs(): Promise<VisInputsResponse> {
  const inputs = buildInputs();
  const live = inputs.filter((i) => i.in_bf).slice(0, N_LIVE).map((i) => i.packet_idx);
  const wired = inputs.map((i) => i.packet_idx);
  return delay({
    sets: { live, wired },
    inputs,
    obs: {
      utc_start: OBS_UTC_START,
      latest_ts: new Date().toISOString(),
      n_cached: 1880,
      oldest_ts: new Date(Date.now() - 3 * 86400_000).toISOString(),
    },
  });
}

export function mockGetVisTimes(t0: string, t1: string): Promise<VisTimesResponse> {
  const startMs = new Date(t0).getTime();
  const endMs = new Date(t1).getTime();
  const t: number[] = [];
  for (let ms = startMs; ms <= endMs; ms += INTEGRATION_S * 1000) {
    t.push(Math.round(ms / 1000));
  }
  return delay({ t });
}

function baseAmp(i: number, j: number, f: number, tPhase: number): number {
  const drift = Math.sin(((f - 440) / 90) * Math.PI + tPhase * 0.2 + i * 0.3 + j * 0.1);
  return (i === j ? 20 : 4) + 3 * drift;
}

/** Baseline (2, 5) carries an uncorrected-delay sawtooth for the phase demo
 * (a linear slope in frequency, roughly steady in time in a waterfall). */
function isDelayDemo(i: number, j: number): boolean {
  return i === 2 && j === 5;
}

/** Baseline (1, 8) carries a drifting fringe for the waterfall demo:
 * near-flat across frequency but a fast, steady phase slope in time, i.e.
 * horizontal stripes marching across the waterfall (waterfalls | spectra
 * matrix, `?mock=1`). */
function isFringeDemo(i: number, j: number): boolean {
  return i === 1 && j === 8;
}

function wrapPi(x: number): number {
  return (((x % (2 * Math.PI)) + 3 * Math.PI) % (2 * Math.PI)) - Math.PI;
}

function quantityValue(
  quantity: VisQuantity,
  units: VisUnits,
  i: number,
  j: number,
  f: number,
  tPhase: number,
): number {
  const amp = baseAmp(i, j, f, tPhase);
  const linearAmp = 10 ** (amp / 10);
  if (quantity === "coh") {
    return i === j ? 1 : Math.max(0, Math.min(1, 0.6 + 0.3 * Math.sin(f / 5 + i - j)));
  }
  if (quantity === "phase") {
    let rad: number;
    if (isDelayDemo(i, j)) {
      // A linear slope wrapped to (-pi, pi]: an uncorrected delay sawtooth.
      const slope = 0.9; // rad per MHz
      rad = wrapPi(f * slope + tPhase);
    } else if (isFringeDemo(i, j)) {
      // A fast, near-frequency-independent phase rate: a fringe drifting in
      // time (horizontal stripes marching across a time-vs-freq waterfall).
      const fringeRateRadPerTUnit = 3.2;
      rad = wrapPi(tPhase * fringeRateRadPerTUnit + 0.05 * (f - 440));
    } else {
      rad = i === j ? 0 : 0.4 * Math.sin(f / 30 + i - j + tPhase * 0.1);
    }
    return units === "deg" ? (rad * 180) / Math.PI : rad;
  }
  const re = linearAmp * Math.cos((i === j ? 0 : 0.2 * (i - j)) + f / 60);
  const im = linearAmp * Math.sin((i === j ? 0 : 0.2 * (i - j)) + f / 60);
  let value = quantity === "real" ? re : quantity === "imag" ? im : linearAmp;
  if (units === "db") value = 10 * Math.sign(value) * Math.log10(Math.max(Math.abs(value), 1e-6));
  else if (units === "log10") value = Math.sign(value) * Math.log10(Math.max(Math.abs(value), 1e-6));
  return value;
}

function refAdjust(ref: VisRef, value: number, seed: number): number {
  if (ref === "raw") return value;
  // Sun-stopping/cal-dividing flattens phase-like ripple and steadies amplitude
  // in the mock, just enough to make the toggle visibly do something.
  return value * (1 - 0.15 * noise(seed)) ;
}

export function mockGetVisSpectra(
  ts: "latest" | number,
  set: VisSet,
  pairs: VisPairs,
  quantity: VisQuantity,
  units: VisUnits,
  ref: VisRef,
  nchan?: number,
): Promise<VisSpectraResponse> {
  const inputs = buildInputs();
  const idxs = (set === "live" ? inputs.filter((i) => i.in_bf) : inputs).map((i) => i.packet_idx);
  const n = nchan ?? 768;
  const freq_mhz = linspaceDesc(CORR_BAND_MHZ[1], CORR_BAND_MHZ[0], n);
  const tPhase = typeof ts === "number" ? ts / 400 : Date.now() / 4000;
  const baselines: VisBaseline[] = [];
  for (const i of idxs) {
    for (const j of idxs) {
      if (pairs === "auto" && i !== j) continue;
      if (pairs === "cross" && i >= j) continue;
      if (quantity === "coh" && i === j) continue;
      const ant_i = inputs.find((x) => x.packet_idx === i)?.antenna ?? null;
      const ant_j = inputs.find((x) => x.packet_idx === j)?.antenna ?? null;
      const y = freq_mhz.map((f, k) =>
        refAdjust(ref, quantityValue(quantity, units, i, j, f, tPhase), i * 31 + j * 7 + k),
      );
      baselines.push({ i, j, ant_i, ant_j, y });
    }
  }
  return delay({
    ts: typeof ts === "number" ? ts : Math.round(Date.now() / 1000),
    freq_mhz,
    baselines,
    flags: { sun_below_horizon: false, cal_file: "cal_sep03peak_core17.h5" },
  });
}

export function mockGetVisMatrix(
  ts: "latest" | number,
  set: VisSet,
  quantity: VisQuantity,
  units: VisUnits,
  fmin?: number,
  fmax?: number,
): Promise<VisMatrixResponse> {
  const inputs = buildInputs();
  const idxs = (set === "live" ? inputs.filter((i) => i.in_bf) : inputs).map((i) => i.packet_idx);
  const f = ((fmin ?? CORR_BAND_MHZ[0]) + (fmax ?? CORR_BAND_MHZ[1])) / 2;
  const tPhase = typeof ts === "number" ? ts / 400 : Date.now() / 4000;
  const m = idxs.map((i) => idxs.map((j) => quantityValue(quantity, units, i, j, f, tPhase)));
  return delay({ inputs: idxs, m });
}

export function mockGetVisWaterfall(
  i: number,
  j: number,
  t0: string,
  t1: string,
  quantity: VisQuantity,
  units: VisUnits,
  ref: VisRef,
): Promise<VisWaterfallResponse> {
  const startMs = new Date(t0).getTime();
  const endMs = new Date(t1).getTime();
  const n = 80;
  const stepMs = (endMs - startMs) / Math.max(n - 1, 1);
  const freq_mhz = linspaceDesc(CORR_BAND_MHZ[1], CORR_BAND_MHZ[0], 200);
  const t: number[] = [];
  const z: number[][] = [];
  for (let k = 0; k < n; k++) {
    const tsMs = startMs + k * stepMs;
    t.push(Math.round(tsMs / 1000));
    const tPhase = tsMs / 4000;
    z.push(freq_mhz.map((f) => refAdjust(ref, quantityValue(quantity, units, i, j, f, tPhase), i + j + k)));
  }
  return delay({ t, freq_mhz, z, res: "60s" });
}

export function mockGetVisCoherence(_t0: string, _t1: string, set: VisSet): Promise<VisCoherenceResponse> {
  const inputs = buildInputs();
  const idxs = (set === "live" ? inputs.filter((i) => i.in_bf) : inputs).map((i) => i.packet_idx);
  const m = idxs.map((i) =>
    idxs.map((j) => (i === j ? 1 : Math.max(0, Math.min(1, 0.75 + 0.2 * Math.sin(i - j))))),
  );
  return delay({ inputs: idxs, m });
}
