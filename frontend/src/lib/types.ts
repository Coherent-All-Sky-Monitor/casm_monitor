// Shared response types for the casm_monitor API. Kept in sync by hand with
// docs/plan.md "API contract"; the backend agent implements exactly this.

export type ItemState = "ok" | "warn" | "stale" | "error";

export interface StatusItem {
  value: unknown;
  ts: string;
  age_s: number;
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
