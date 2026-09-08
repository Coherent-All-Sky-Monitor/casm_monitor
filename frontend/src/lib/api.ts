// Typed client for the casm_monitor backend. Errors are surfaced through the
// toast bus (lib/toast.ts) rather than only console.error, per the M0 spec.

import { emitError } from "./toast";
import type {
  EventRecord,
  EventsResponse,
  HealthResponse,
  JobsResponse,
  ScalarsResponse,
  StatusResponse,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch (err) {
    const message = `network error contacting ${path}: ${(err as Error).message}`;
    emitError(message);
    throw new ApiError(message, 0);
  }
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    const message = `${path} failed: ${res.status} ${res.statusText}${body ? ` — ${body}` : ""}`;
    emitError(message);
    throw new ApiError(message, res.status);
  }
  try {
    return (await res.json()) as T;
  } catch (err) {
    const message = `${path} returned invalid JSON: ${(err as Error).message}`;
    emitError(message);
    throw new ApiError(message, res.status);
  }
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/api/health");
}

export function getStatus(): Promise<StatusResponse> {
  return request<StatusResponse>("/api/status");
}

export interface EventsQuery {
  since?: string;
  kind?: string;
  severity?: string;
  limit?: number;
}

export function getEvents(query: EventsQuery = {}): Promise<EventRecord[]> {
  const params = new URLSearchParams();
  if (query.since) params.set("since", query.since);
  if (query.kind) params.set("kind", query.kind);
  if (query.severity) params.set("severity", query.severity);
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  const qs = params.toString();
  return request<EventsResponse>(`/api/events${qs ? `?${qs}` : ""}`).then(
    (r) => r.events,
  );
}

export interface ScalarsQuery {
  name: string;
  t0?: string;
  t1?: string;
  max_points?: number;
}

export function getScalars(query: ScalarsQuery): Promise<ScalarsResponse> {
  const params = new URLSearchParams();
  params.set("name", query.name);
  if (query.t0) params.set("t0", query.t0);
  if (query.t1) params.set("t1", query.t1);
  if (query.max_points !== undefined) {
    params.set("max_points", String(query.max_points));
  }
  return request<ScalarsResponse>(`/api/scalars?${params.toString()}`);
}

export function getJobs(): Promise<JobsResponse> {
  return request<JobsResponse>("/api/jobs");
}

export function statusWebSocketUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/status`;
}
