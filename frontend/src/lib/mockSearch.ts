// Mock backend for the Search tab, selected by the `?mock=1` URL flag while
// the real /api/search/* routes are implemented against docs/api-search.md
// concurrently. Shapes mirror lib/types.ts exactly so SearchPage.tsx can swap
// real <-> mock with one branch. Never imported by lib/api.ts itself.

import type {
  SearchBeamMapResponse,
  SearchFunnelResponse,
  SearchHistField,
  SearchHistResponse,
  SearchRateResponse,
  SearchScatterField,
  SearchScatterResponse,
  SearchSummaryResponse,
} from "./types";

const NETWORK_DELAY_MS = 60;
const JOBS = [0, 1, 2, 3, 4, 5, 6, 7];

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), NETWORK_DELAY_MS));
}

function noise(seed: number): number {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

function windowMinutes(t0: string, t1: string): number {
  return Math.max((new Date(t1).getTime() - new Date(t0).getTime()) / 60_000, 1 / 60);
}

export function mockGetSearchSummary(t0: string, t1: string): Promise<SearchSummaryResponse> {
  const minutes = windowMinutes(t0, t1);
  const per_job = JOBS.map((job) => {
    const n = Math.round((15 + 10 * noise(job + 1)) * minutes);
    return {
      job,
      node: job < 4 ? "corr1" : "corr2",
      n,
      rate_per_min: Math.round((n / minutes) * 10) / 10,
      last_ts: Math.round(Date.now() / 1000) - Math.round(noise(job) * 30),
    };
  });
  const n_cands = per_job.reduce((s, j) => s + j.n, 0);
  const n_clusters = Math.max(1, Math.round(n_cands / 268));
  return delay({
    t0,
    t1,
    n_cands,
    per_job,
    thresholds: {
      corr1: { snr: 15, dm_min: 20 },
      corr2: { snr: 15, dm_min: 20 },
    },
    funnel: {
      n_cands,
      n_clusters,
      n_stored: n_clusters,
      n_vetoed: n_cands - n_clusters,
      n_triggers: Math.max(0, Math.round(n_clusters / 15)),
    },
  });
}

const HIST_RANGES: Record<SearchHistField, [number, number]> = {
  snr: [7, 60],
  dm: [0, 2000],
  width: [1, 64],
  beam: [0, 512],
};

export function mockGetSearchHist(
  field: SearchHistField,
  _t0: string,
  _t1: string,
  bins = 30,
  log = false,
): Promise<SearchHistResponse> {
  const [lo, hi] = HIST_RANGES[field];
  const edges: number[] = [];
  if (log && lo > 0) {
    const logLo = Math.log10(lo);
    const logHi = Math.log10(hi);
    for (let i = 0; i <= bins; i++) edges.push(10 ** (logLo + ((logHi - logLo) * i) / bins));
  } else {
    for (let i = 0; i <= bins; i++) edges.push(lo + ((hi - lo) * i) / bins);
  }
  // A falling-with-SNR / falling-with-DM shape, flat-ish for width/beam.
  const counts = Array.from({ length: bins }, (_, i) => {
    const mid = (edges[i] + edges[i + 1]) / 2;
    const decay = field === "snr" || field === "dm" ? Math.exp(-mid / (hi / 4)) : 1;
    return Math.max(0, Math.round(400 * decay * (0.6 + 0.5 * noise(i + mid))));
  });
  return delay({ edges, counts });
}

export function mockGetSearchScatter(
  x: SearchScatterField,
  y: SearchScatterField,
  t0: string,
  t1: string,
  maxPoints = 20000,
): Promise<SearchScatterResponse> {
  const n = Math.min(maxPoints, 4000);
  const n_total = Math.round(12000 * windowMinutes(t0, t1));
  const fieldRange = (f: SearchScatterField): [number, number] =>
    f === "time" ? [new Date(t0).getTime() / 1000, new Date(t1).getTime() / 1000] : HIST_RANGES[f];
  const [xlo, xhi] = fieldRange(x);
  const [ylo, yhi] = fieldRange(y);
  const xs: number[] = [];
  const ys: number[] = [];
  for (let i = 0; i < n; i++) {
    const u = noise(i * 2.1);
    const v = noise(i * 3.7 + 1);
    // Skew low-SNR/low-DM-ish fields toward small values so the log view
    // looks like a real candidate cloud, not uniform noise.
    const xv = xlo + (xhi - xlo) * u ** 2;
    const yv = ylo + (yhi - ylo) * v ** 2;
    xs.push(xv);
    ys.push(yv);
  }
  return delay({ x: xs, y: ys, n_total });
}

export function mockGetSearchBeamMap(_t0: string, _t1: string): Promise<SearchBeamMapResponse> {
  const counts = Array.from({ length: 512 }, (_, beam) => {
    const hot = beam === 96 || beam === 233 || beam === 455;
    const base = Math.round(8 + 6 * noise(beam));
    return hot ? base + 120 : base;
  });
  return delay({ counts });
}

export function mockGetSearchRate(t0: string, t1: string, stepS = 60): Promise<SearchRateResponse> {
  const startMs = new Date(t0).getTime();
  const endMs = new Date(t1).getTime();
  const n = Math.max(2, Math.min(2000, Math.round((endMs - startMs) / (stepS * 1000))));
  const t: number[] = [];
  const per_job: Record<string, number[]> = {};
  for (const job of JOBS) per_job[String(job)] = [];
  const total: number[] = [];
  for (let i = 0; i < n; i++) {
    t.push(Math.round((startMs + i * stepS * 1000) / 1000));
    let sum = 0;
    for (const job of JOBS) {
      const v = Math.max(0, Math.round(3 + 2 * Math.sin(i / 6 + job) + 2 * noise(i + job * 13)));
      per_job[String(job)].push(v);
      sum += v;
    }
    total.push(sum);
  }
  return delay({ t, per_job, total });
}

export function mockGetSearchFunnel(t0: string, t1: string, stepS = 600): Promise<SearchFunnelResponse> {
  const startMs = new Date(t0).getTime();
  const endMs = new Date(t1).getTime();
  const n = Math.max(2, Math.min(2000, Math.round((endMs - startMs) / (stepS * 1000))));
  const t: number[] = [];
  const n_cands: number[] = [];
  const n_clusters: number[] = [];
  const n_stored: number[] = [];
  const n_vetoed: number[] = [];
  for (let i = 0; i < n; i++) {
    t.push(Math.round((startMs + i * stepS * 1000) / 1000));
    const c = Math.round(200 + 60 * noise(i));
    const clusters = Math.max(0, Math.round(c / 260 + noise(i + 5)));
    n_cands.push(c);
    n_clusters.push(clusters);
    n_stored.push(clusters);
    n_vetoed.push(c - clusters);
  }
  return delay({ t, n_cands, n_clusters, n_stored, n_vetoed });
}
