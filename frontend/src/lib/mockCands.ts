// Mock backend for the Candidates tab, selected by the `?mock=1` URL flag.
// Shapes mirror lib/types.ts exactly (docs/api-cands.md) so CandsPage /
// CandEventPage can swap real <-> mock with one branch. Never imported by
// lib/api.ts itself.

import type { CalActionResult, CandEventsQuery } from "./api";
import type {
  CandEventDetailResponse,
  CandEventsResponse,
  CandFrbsResponse,
  CandInjectionsResponse,
  CandLabel,
  CandLabelPostResponse,
  CandStatsResponse,
  CandTransitsResponse,
} from "./types";

const NETWORK_DELAY_MS = 60;

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), NETWORK_DELAY_MS));
}

// A 1x1 transparent PNG so <img> tags never 404 against the mock.
export const MOCK_PNG_DATA_URL =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

const NAMES = ["260909aabbcc", "260909ccddee", "260908ffgghh", "260908iijjkk"];
const TIERS = ["A", "A", "B", "C"];
const LABELS: (CandLabel | null)[] = ["frb", null, "rfi", null];
const OUTCOMES = ["dumped", "storm: 60 s gate", "RFI: too many beams", "S/N below tier B (15)"];

function eventRow(i: number) {
  return {
    name: NAMES[i],
    event_utc: new Date(Date.now() - i * 3_600_000).toISOString().replace("Z", ""),
    snr: 30 - i * 3.4,
    dm: 120 - i * 15,
    width: (i % 4) + 1,
    beam: 40 + i * 17,
    tier: TIERS[i],
    tags: i === 2 ? ["rfi_wide"] : [],
    n_beams: 2 + i,
    n_members: 5 + i,
    alt_deg: 45.0 + i,
    az_deg: 90.0 - i * 3,
    label: LABELS[i],
    outcome: OUTCOMES[i],
  };
}

export function mockGetCandEvents(query: CandEventsQuery = {}): Promise<CandEventsResponse> {
  let rows = NAMES.map((_, i) => eventRow(i));
  // Mimic the real "candidates" default: drop the one row whose outcome is a
  // held-reason phrase (never reached a dump attempt) rather than a trigger
  // outcome (dumped/storm-gated).
  if (query.view !== "all") rows = rows.filter((r) => !r.outcome.startsWith("S/N below"));
  if (query.tier) rows = rows.filter((r) => r.tier === query.tier);
  if (query.tag) rows = rows.filter((r) => r.label === query.tag);
  return delay({ events: rows });
}

export function mockGetCandEvent(name: string): Promise<CandEventDetailResponse> {
  const i = Math.max(0, NAMES.indexOf(name));
  const row = eventRow(i);
  return delay({
    event: row,
    tags_display: row.tags,
    triggers: [
      {
        id: 1,
        candname: name,
        stream: 0,
        kind: "intensity",
        action: i === 0 ? "triggered" : "refused",
        detail: i === 0 ? "ring ok" : "spacing",
        dump_utc_start: i === 0 ? row.event_utc : null,
        dump_utc_stop: i === 0 ? row.event_utc : null,
        bytes_written: i === 0 ? 12_400_000 : null,
        cleaned_utc: null,
        created_utc: row.event_utc,
      },
    ],
    labels:
      row.label === null
        ? []
        : [{ id: 1, name, label: row.label, who: "monitor", notes: "looks real", created_utc: row.event_utc }],
    plots: i === 0 ? [`${name}.png`] : [],
    meta: { data_available: i === 0 },
    data_status: i === 0 ? "raw dump on disk" : `no dump — ${row.outcome}`,
    label_choices: ["frb", "pulsar", "rfi", "unsure"],
  });
}

export function mockCandPlotUrl(_name: string, _fname: string): string {
  return MOCK_PNG_DATA_URL;
}

let mockLabelStore: Record<string, CandLabel> = {};

export function mockPostCandLabel(
  name: string,
  label: CandLabel,
  note: string,
): Promise<CalActionResult<CandLabelPostResponse>> {
  mockLabelStore[name] = label;
  return delay({
    ok: true,
    status: 200,
    body: {
      name,
      label,
      labels: [{ id: 99, name, label, who: "monitor", notes: note, created_utc: new Date().toISOString() }],
    },
    detail: null,
  });
}

export function mockGetCandStats(hours: number): Promise<CandStatsResponse> {
  return delay({
    hours,
    win_label: `${hours} h`,
    presets: [
      { hours: 12, label: "12 h" },
      { hours: 24, label: "24 h" },
      { hours: 48, label: "48 h" },
      { hours: 168, label: "1 week" },
      { hours: 720, label: "1 month" },
    ],
    hour: { gulps: 420, cands: 12000, clusters: 40, stored: 40, would: 3, ms: 5.2, cands_s: 7.9 },
    win: { gulps: 9200, cands: 260000, clusters: 900, stored: 900, would: 60, ms: 5.4, cands_s: 7.6 },
    now: {
      epoch_ms: Date.now(),
      lst_h: 4.2,
      sources: [
        { name: "Sun", alt: 12.0, az: 90.0, dec: 4.0, transit: "in 1.2 h" },
        { name: "Cas A", alt: -20.0, az: 10.0, dec: 58.8, transit: "5.0 h ago" },
      ],
    },
    rows: Array.from({ length: 10 }, (_, i) => ({
      gulp_utc: new Date(Date.now() - i * 8192 * 1.048576).toISOString(),
      n_jobs: 8,
      n_cands: 1000 - i * 10,
      n_clusters: 12,
      n_stored: 3,
      n_would: 1,
      clustering_ms: 5.5,
    })),
  });
}

export function mockCandStatsPlotUrl(_hours: number): string {
  return MOCK_PNG_DATA_URL;
}

export function mockGetCandInjections(): Promise<CandInjectionsResponse> {
  return delay({
    injections: [
      {
        id: 1,
        inject_utc: new Date().toISOString(),
        beam: 200,
        dm: 50.0,
        amp: 1.0,
        sigma_ms: 2.0,
        est_snr: 20.0,
        rec_snr: 19.4,
        rec_dm: 49.8,
        gate_t1: 1,
        gate_t2: 1,
        gate_trigger: 0,
        event_name: null,
        fail_reason: null,
      },
    ],
    day: { n: 40, t1: 38, t2: 36, tr: 30, done: 40 },
  });
}

export function mockGetCandFrbs(): Promise<CandFrbsResponse> {
  return delay({
    frbs: [
      {
        name: NAMES[0],
        event_utc: eventRow(0).event_utc,
        snr: 30.0,
        dm: 120.0,
        width: 1,
        beam: 40,
        notes: "looks real",
        created_utc: eventRow(0).event_utc,
      },
    ],
  });
}

export function mockGetCandTransits(): Promise<CandTransitsResponse> {
  return mockGetCandStats(24).then((r) => ({ snapshot: r.now }));
}

export function _resetMockLabelStore(): void {
  mockLabelStore = {};
}
