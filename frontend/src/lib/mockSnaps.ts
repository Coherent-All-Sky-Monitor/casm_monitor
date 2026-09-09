// Mock backend for the SNAPs tab, selected by the `?mock=1` URL flag while
// the real /api/snaps/* routes are being implemented against
// docs/api-snaps.md concurrently. Shapes mirror lib/types.ts exactly so
// SnapsPage.tsx can swap real <-> mock with one branch. Never imported by
// lib/api.ts itself; SnapsPage picks one or the other.

import {
  ADC_RMS_HEALTHY,
  ANTENNA_IPS,
  BOARD_BAND_MHZ,
  CORR_BAND_MHZ,
  RELAY_IPS,
} from "./snapConstants";
import type {
  SnapBoardInfo,
  SnapBoardReadResponse,
  SnapBoardsResponse,
  SnapHistoryResponse,
  SnapHistorySource,
  SnapLiveResponse,
  SnapMapping,
  SnapTrendResponse,
} from "./types";
import type { SnapReadAccepted, SnapReadThrottled } from "./api";

const NETWORK_DELAY_MS = 60;

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), NETWORK_DELAY_MS));
}

function linspaceDesc(hi: number, lo: number, n: number): number[] {
  const out = new Array<number>(n);
  const step = (hi - lo) / (n - 1);
  for (let i = 0; i < n; i++) out[i] = hi - i * step;
  return out;
}

// Deterministic-but-lively pseudo-noise so repeated polls look "live"
// without being pure random (a diagnosis-legend case, e.g. flat/pinned, can
// still be represented for a given adc below).
function noise(seed: number): number {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

type FeedKind = "normal" | "dead" | "railed" | "ripple";

function feedKind(ip: string, adc: number): FeedKind {
  // ant 3 on .52 dead, ant 7 on .51 railed, ant 2 on .62 has a cable
  // reflection ripple; everything else normal. Fixed so the mock is
  // reproducible across reloads.
  if (ip === ANTENNA_IPS[0] && adc === 3) return "dead";
  if (ip === ANTENNA_IPS[1] && adc === 7) return "railed";
  if (ip === ANTENNA_IPS[2] && adc === 2) return "ripple";
  return "normal";
}

function synthBandpass(
  freqMhz: number[],
  kind: FeedKind,
  seed: number,
  tPhase: number,
): number[] {
  return freqMhz.map((f, i) => {
    if (kind === "dead") return -55 + noise(seed + i * 0.01) * 0.3;
    if (kind === "railed") return -8 + noise(seed + i * 0.01) * 0.15;
    const base = -20 + 6 * Math.sin(((f - 440) / 90) * Math.PI + tPhase * 0.3);
    const ripple = kind === "ripple" ? 1.5 * Math.sin((f / 2.1) + tPhase) : 0;
    const jitter = (noise(seed + i * 0.037 + tPhase) - 0.5) * 0.8;
    return base + ripple + jitter;
  });
}

let boardsCache: SnapBoardInfo[] | null = null;

function buildBoards(): SnapBoardInfo[] {
  if (boardsCache) return boardsCache;
  const stations = ["N21E1", "N21E3", "N16E1", "N16E5", "N02E2", "S2A1", "N03A1", null];
  let packetCounter = 0;
  const antennaBoards: SnapBoardInfo[] = ANTENNA_IPS.map((ip, boardIdx) => ({
    ip,
    feng_id: boardIdx,
    slot: String.fromCharCode(65 + boardIdx), // A, B, C, D
    role: "antenna",
    inputs: Array.from({ length: 12 }, (_, adc) => {
      const wired = !(boardIdx === 3 && adc >= 9); // last board's top 3 inputs unwired
      const idx = packetCounter++;
      return {
        adc,
        packet_idx: wired ? idx : null,
        antenna: wired ? idx + 1 : null,
        station: wired ? stations[idx % stations.length] ?? `N0${idx}E1` : null,
        in_bf: wired && idx % 5 !== 0,
        functional: wired,
      };
    }),
  }));
  const relayBoards: SnapBoardInfo[] = RELAY_IPS.map((ip) => ({
    ip,
    feng_id: null,
    slot: "?",
    role: "relay",
  }));
  boardsCache = [...antennaBoards, ...relayBoards];
  return boardsCache;
}

export function mockGetSnapBoards(): Promise<SnapBoardsResponse> {
  return delay({ boards: buildBoards() });
}

export function mockGetSnapLive(ip: string, nchan?: number): Promise<SnapLiveResponse> {
  const n = nchan ?? 3072;
  const freq_mhz = linspaceDesc(CORR_BAND_MHZ[1], CORR_BAND_MHZ[0], n);
  const board = buildBoards().find((b) => b.ip === ip);
  const tPhase = Date.now() / 4000;
  if (!board || board.role !== "antenna" || !board.inputs) {
    return delay({
      ts: null,
      age_s: null,
      freq_mhz,
      subbands_ok: [true, true, true, true, true, true],
      inputs: [],
      eq_epoch: null,
    });
  }
  const subbands_ok = [true, true, true, true, true, true];
  if (ip === ANTENNA_IPS[2]) subbands_ok[3] = false; // one dark subband for the demo
  const inputs = board.inputs.map((inp) => {
    if (inp.packet_idx === null) {
      return { adc: inp.adc, packet_idx: null, bp: null, mapping: "unmapped" as const };
    }
    const kind = feedKind(ip, inp.adc);
    // The cable-reflection-ripple demo feed also doubles as the "a validation
    // pass disagrees with the formula" demo, so the mismatch sentence has something
    // to show in the mock.
    return {
      adc: inp.adc,
      packet_idx: inp.packet_idx,
      bp: synthBandpass(freq_mhz, kind, inp.adc * 7.3 + 1, tPhase),
      mapping: (kind === "ripple" ? "mismatch" : "formula+verified") satisfies SnapMapping as SnapMapping,
    };
  });
  return delay({
    ts: new Date().toISOString(),
    age_s: 4 + Math.round(noise(tPhase) * 6),
    freq_mhz,
    subbands_ok,
    inputs,
    eq_epoch: "a12aaaaeffdf",
  });
}

const boardReadTimestamps = new Map<string, number>();

export function mockGetSnapBoardRead(ip: string): Promise<SnapBoardReadResponse> {
  const isRelay = RELAY_IPS.includes(ip);
  const lastReadMs = boardReadTimestamps.get(ip) ?? Date.now() - 43 * 60_000;
  const ageS = (Date.now() - lastReadMs) / 1000;
  if (isRelay) {
    return delay({
      ts: new Date(lastReadMs).toISOString(),
      age_s: ageS,
      freq_mhz: null,
      spectra: null,
      adc_rms: null,
      adc_mean: null,
      adc_gain: null,
      eq_epoch: null,
      feng_id_hw: null,
      feng_id_cfg: null,
      pps: { ok: true, period: 1, detail: "locked" },
      programmed: null,
    });
  }
  const freq_mhz = linspaceDesc(BOARD_BAND_MHZ[1], BOARD_BAND_MHZ[0], 4096);
  const board = buildBoards().find((b) => b.ip === ip);
  const boardIdx = ANTENNA_IPS.indexOf(ip);
  const spectra = Array.from({ length: 12 }, (_, adc) => {
    const kind = feedKind(ip, adc);
    return synthBandpass(freq_mhz, kind, adc * 3.1 + 10, 0);
  });
  const adc_rms = Array.from({ length: 12 }, (_, adc) => {
    const kind = feedKind(ip, adc);
    if (kind === "dead") return 0.4;
    if (kind === "railed") return 34.5;
    return ADC_RMS_HEALTHY[0] + noise(adc + boardIdx) * (ADC_RMS_HEALTHY[1] - ADC_RMS_HEALTHY[0] - 15);
  });
  return delay({
    ts: new Date(lastReadMs).toISOString(),
    age_s: ageS,
    freq_mhz,
    spectra,
    adc_rms,
    adc_mean: Array.from({ length: 12 }, () => 0),
    adc_gain: [8, 16, 32, 25, 8, 16, 32, 25, 8, 16, 32, 25],
    eq_epoch: "a12aaaaeffdf",
    feng_id_hw: board?.feng_id ?? null,
    feng_id_cfg: board?.feng_id ?? null,
    pps: { ok: true, period: 1, detail: "locked" },
    programmed: true,
  });
}

let mockJobCounter = 1000;
const mockJobs = new Map<number, { state: string; createdMs: number; ips: string[] | null }>();
let lastManualReadMs: number | null = null;

export function mockPostSnapBoardRead(
  ips: string[] | null,
): Promise<SnapReadThrottled | SnapReadAccepted> {
  const now = Date.now();
  if (lastManualReadMs !== null && now - lastManualReadMs < 5 * 60_000) {
    const retryAfterS = Math.ceil((5 * 60_000 - (now - lastManualReadMs)) / 1000);
    return delay({
      throttled: true,
      detail: "manual board read requested less than 5 minutes ago",
      retryAfterS,
    });
  }
  lastManualReadMs = now;
  const jobId = mockJobCounter++;
  mockJobs.set(jobId, { state: "running", createdMs: now, ips });
  const targets = ips ?? [...ANTENNA_IPS, ...RELAY_IPS];
  setTimeout(() => {
    for (const ip of targets) boardReadTimestamps.set(ip, Date.now());
    const job = mockJobs.get(jobId);
    if (job) job.state = "done";
  }, 2500);
  return delay({ throttled: false, jobId });
}

export function mockGetJob(id: number) {
  const job = mockJobs.get(id);
  return delay({
    id,
    kind: "snap_board_read",
    state: job?.state ?? "done",
    created: new Date(job?.createdMs ?? Date.now()).toISOString(),
    started: new Date(job?.createdMs ?? Date.now()).toISOString(),
    finished: job?.state === "done" ? new Date().toISOString() : null,
  });
}

export function mockGetSnapHistory(
  packetIdx: number,
  t0: string,
  t1: string,
  source: SnapHistorySource,
): Promise<SnapHistoryResponse> {
  const n = 96;
  const startMs = new Date(t0).getTime();
  const endMs = new Date(t1).getTime();
  const stepMs = (endMs - startMs) / Math.max(n - 1, 1);
  const freq_mhz = linspaceDesc(
    source === "board" ? BOARD_BAND_MHZ[1] : CORR_BAND_MHZ[1],
    source === "board" ? BOARD_BAND_MHZ[0] : CORR_BAND_MHZ[0],
    256,
  );
  const kind: FeedKind = packetIdx % 11 === 3 ? "dead" : packetIdx % 13 === 7 ? "ripple" : "normal";
  const t: number[] = [];
  const z_db: number[][] = [];
  for (let i = 0; i < n; i++) {
    const tsMs = startMs + i * stepMs;
    t.push(Math.round(tsMs / 1000));
    z_db.push(synthBandpass(freq_mhz, kind, packetIdx * 3 + i, i * 0.2));
  }
  return delay({ t, freq_mhz, z_db, res: "10min" });
}

export function mockGetSnapTrend(
  packetIdx: number,
  t0: string,
  t1: string,
): Promise<SnapTrendResponse> {
  const n = 60;
  const startMs = new Date(t0).getTime();
  const endMs = new Date(t1).getTime();
  const stepMs = (endMs - startMs) / Math.max(n - 1, 1);
  const t: number[] = [];
  const night_median_db: number[] = [];
  const band_power_db: number[] = [];
  for (let i = 0; i < n; i++) {
    t.push(Math.round((startMs + i * stepMs) / 1000));
    night_median_db.push(-20 + Math.sin(i / 8) * 1.5 + (noise(i + packetIdx) - 0.5));
    band_power_db.push(-18 + Math.sin(i / 8 + 0.5) * 1.5 + (noise(i + packetIdx + 5) - 0.5));
  }
  return delay({
    t,
    night_median_db,
    band_power_db,
    epochs: [
      { ts: new Date(startMs + stepMs * 20).toISOString(), kind: "eq", label: "EQ coeffs reloaded" },
      {
        ts: new Date(startMs + stepMs * 40).toISOString(),
        kind: "obs_restart",
        label: "obs restarted",
      },
    ],
  });
}
