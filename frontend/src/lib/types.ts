// Shared response types for the casm_monitor API. Kept in sync by hand with
// docs/plan.md "API contract"; the backend agent implements exactly this.

export type ItemState = "ok" | "warn" | "stale" | "error";

export interface StatusItem {
  value: unknown;
  // Never-collected items report ts/age_s as null (state is still "stale"
  // in that case, per the backend's "never blank" rule) rather than being
  // omitted, so both must be treated as optionally absent here.
  ts: string | null;
  age_s: number | null;
  state: ItemState;
  label: string;
  unit: string | null;
  group: string;
}

export interface StatusGroup {
  name: string;
  keys: string[];
}

export interface StatusResponse {
  ts: string;
  items: Record<string, StatusItem>;
  groups: StatusGroup[];
}

export interface HealthResponse {
  ok: boolean;
  collect_age_s: number;
  version: string;
}

export type EventSeverity = "info" | "warn" | "error";

export interface EventRecord {
  id: string | number;
  ts: string;
  kind: string;
  severity: EventSeverity;
  subject: string;
  detail: Record<string, unknown>;
}

export interface EventsResponse {
  events: EventRecord[];
}

export interface ScalarsResponse {
  name: string;
  t: number[];
  v: number[];
}

export type JobState = "queued" | "running" | "done" | "failed" | "cancelled" | string;

export interface JobRecord {
  id: string | number;
  kind: string;
  state: JobState;
  created: string;
  started: string | null;
  finished: string | null;
}

// The contract is a bare list, not a wrapped object (unlike /api/events).
export type JobsResponse = { jobs: JobRecord[] };

// SNAPs tab (M1). Kept in sync by hand with docs/api-snaps.md, the contract
// the backend agent implements.

export type SnapRole = "antenna" | "relay";

export interface SnapInputInfo {
  adc: number;
  packet_idx: number | null;
  antenna: number | null;
  station: string | null;
  in_bf: boolean;
  functional: boolean;
}

export interface SnapBoardInfo {
  ip: string;
  feng_id: number | null;
  slot: string;
  role: SnapRole;
  // Absent/undefined for relay boards.
  inputs?: SnapInputInfo[];
}

export interface SnapBoardsResponse {
  boards: SnapBoardInfo[];
}

// "formula": the primary row=2*packet_idx assignment, not yet validated (or
// the vis file was unavailable when validation last ran); "formula+verified":
// a daily/obs-restart validation pass confirms it; "mismatch": that
// validation disagrees (an operator-visible flag -- the tile still shows the
// formula row's data); "unmapped": packet_idx itself resolves to no wired
// input. docs/notes/kafka-bandpass-schema.md, 2026-09-08.
export type SnapMapping = "formula" | "formula+verified" | "mismatch" | "unmapped";

export interface SnapLiveInput {
  adc: number;
  packet_idx: number | null;
  bp: number[] | null;
  mapping: SnapMapping;
}

export interface SnapLiveResponse {
  ts: string | null;
  age_s: number | null;
  freq_mhz: number[];
  subbands_ok: boolean[];
  inputs: SnapLiveInput[];
  eq_epoch: string | null;
}

export interface SnapPpsInfo {
  ok: boolean | null;
  period: number | null;
  detail: string;
}

export interface SnapBoardReadResponse {
  ts: string | null;
  age_s: number | null;
  freq_mhz: number[] | null;
  spectra: (number[] | null)[] | null;
  adc_rms: number[] | null;
  adc_mean: number[] | null;
  adc_gain: number[] | null;
  eq_epoch: string | null;
  feng_id_hw: number | null;
  feng_id_cfg: number | null;
  pps: SnapPpsInfo;
  programmed: boolean | null;
}

export interface SnapReadJobResponse {
  job_id: number;
}

export interface SnapReadThrottledResponse {
  detail: string;
  retry_after_s: number;
}

export type SnapHistorySource = "kafka" | "board";
export type SnapHistoryRes = "10s" | "60s" | "10min" | "1h";

export interface SnapHistoryResponse {
  t: number[];
  freq_mhz: number[];
  z_db: number[][];
  res: SnapHistoryRes;
}

export type SnapEpochKind = "eq" | "adc_gain" | "obs_restart";

export interface SnapTrendEpoch {
  ts: string;
  kind: SnapEpochKind;
  label: string;
}

export interface SnapTrendResponse {
  t: number[];
  night_median_db: number[];
  band_power_db: number[];
  epochs: SnapTrendEpoch[];
}

// Subset of JobRecord fields the SNAPs poll relies on; the backend may send
// more, which is fine (JobRecord already allows JobState to widen to string).
export interface JobDetail extends JobRecord {
  detail?: Record<string, unknown>;
}
