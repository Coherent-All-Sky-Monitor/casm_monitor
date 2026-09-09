// Typed client for the casm_monitor backend. Errors are surfaced through the
// toast bus (lib/toast.ts) rather than only console.error, per the M0 spec.

import { emitError } from "./toast";
import type {
  EventRecord,
  EventsResponse,
  HealthResponse,
  JobDetail,
  JobsResponse,
  ScalarsResponse,
  SearchBeamMapResponse,
  SearchFunnelResponse,
  SearchHistField,
  SearchHistResponse,
  SearchRateResponse,
  SearchScatterField,
  SearchScatterResponse,
  SearchSummaryResponse,
  SnapBoardReadResponse,
  SnapBoardsResponse,
  SnapFigureInputSet,
  SnapFigureKind,
  SnapFigureListResponse,
  SnapFigureManifest,
  SnapHistoryResponse,
  SnapHistorySource,
  SnapLiveResponse,
  SnapReadJobResponse,
  SnapTrendResponse,
  StatusResponse,
  VisCoherenceResponse,
  VisInputsResponse,
  VisMatrixResponse,
  VisPairs,
  VisQuantity,
  VisRef,
  VisSet,
  VisSpectraResponse,
  VisFigureKind,
  VisFigureListResponse,
  VisFigureManifest,
  VisTimesResponse,
  VisUnits,
  VisWaterfallResponse,
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

export function getJob(id: string | number): Promise<JobDetail> {
  return request<JobDetail>(`/api/jobs/${id}`);
}

// --- SNAPs (M1) ---------------------------------------------------------
// See docs/api-snaps.md for the full contract.

export function getSnapBoards(): Promise<SnapBoardsResponse> {
  return request<SnapBoardsResponse>("/api/snaps/boards");
}

export function getSnapLive(ip: string, nchan?: number): Promise<SnapLiveResponse> {
  const params = new URLSearchParams();
  params.set("ip", ip);
  if (nchan !== undefined) params.set("nchan", String(nchan));
  return request<SnapLiveResponse>(`/api/snaps/live?${params.toString()}`);
}

export function getSnapBoardRead(ip: string): Promise<SnapBoardReadResponse> {
  const params = new URLSearchParams();
  params.set("ip", ip);
  return request<SnapBoardReadResponse>(`/api/snaps/board-read?${params.toString()}`);
}

export interface SnapReadThrottled {
  throttled: true;
  detail: string;
  retryAfterS: number;
}

export interface SnapReadAccepted {
  throttled: false;
  jobId: number;
}

/**
 * POST a board-read request. Unlike `request()`, a 429 here is an expected,
 * handled outcome (the 5-minute manual-read limit / hourly lock) rather than
 * an error to toast, so this bypasses the shared error path for that status
 * only and lets the caller decide how to present the countdown.
 */
export async function postSnapBoardRead(ips: string[] | null): Promise<SnapReadThrottled | SnapReadAccepted> {
  let res: Response;
  try {
    res = await fetch("/api/snaps/board-read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ips }),
    });
  } catch (err) {
    const message = `network error contacting /api/snaps/board-read: ${(err as Error).message}`;
    emitError(message);
    throw new ApiError(message, 0);
  }
  if (res.status === 429) {
    const body = (await res.json().catch(() => ({}))) as Partial<{
      detail: string;
      retry_after_s: number;
    }>;
    return {
      throttled: true,
      detail: body.detail ?? "board read rate-limited",
      retryAfterS: body.retry_after_s ?? 300,
    };
  }
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    const message = `/api/snaps/board-read failed: ${res.status} ${res.statusText}${body ? ` — ${body}` : ""}`;
    emitError(message);
    throw new ApiError(message, res.status);
  }
  const payload = (await res.json()) as SnapReadJobResponse;
  return { throttled: false, jobId: payload.job_id };
}

export interface SnapHistoryQuery {
  packet_idx: number;
  t0: string;
  t1: string;
  source: SnapHistorySource;
  max_cells?: number;
}

export function getSnapHistory(query: SnapHistoryQuery): Promise<SnapHistoryResponse> {
  const params = new URLSearchParams();
  params.set("packet_idx", String(query.packet_idx));
  params.set("t0", query.t0);
  params.set("t1", query.t1);
  params.set("source", query.source);
  if (query.max_cells !== undefined) params.set("max_cells", String(query.max_cells));
  return request<SnapHistoryResponse>(`/api/snaps/history?${params.toString()}`);
}

export interface SnapTrendQuery {
  packet_idx: number;
  t0: string;
  t1: string;
}

export function getSnapTrend(query: SnapTrendQuery): Promise<SnapTrendResponse> {
  const params = new URLSearchParams();
  params.set("packet_idx", String(query.packet_idx));
  params.set("t0", query.t0);
  params.set("t1", query.t1);
  return request<SnapTrendResponse>(`/api/snaps/trend?${params.toString()}`);
}

// --- Visibilities (M2) --------------------------------------------------
// See docs/api-vis.md for the full contract.

export function getVisInputs(): Promise<VisInputsResponse> {
  return request<VisInputsResponse>("/api/vis/inputs");
}

export function getVisTimes(t0: string, t1: string): Promise<VisTimesResponse> {
  const params = new URLSearchParams({ t0, t1 });
  return request<VisTimesResponse>(`/api/vis/times?${params.toString()}`);
}

export interface VisSpectraQuery {
  ts: "latest" | number;
  set: VisSet;
  pairs: VisPairs;
  quantity: VisQuantity;
  units: VisUnits;
  ref: VisRef;
  nchan?: number;
}

export function getVisSpectra(query: VisSpectraQuery): Promise<VisSpectraResponse> {
  const params = new URLSearchParams({
    ts: String(query.ts),
    set: query.set,
    pairs: query.pairs,
    quantity: query.quantity,
    units: query.units,
    ref: query.ref,
  });
  if (query.nchan !== undefined) params.set("nchan", String(query.nchan));
  return request<VisSpectraResponse>(`/api/vis/spectra?${params.toString()}`);
}

export interface VisMatrixQuery {
  ts: "latest" | number;
  set: VisSet;
  quantity: VisQuantity;
  units: VisUnits;
  fmin?: number;
  fmax?: number;
}

export function getVisMatrix(query: VisMatrixQuery): Promise<VisMatrixResponse> {
  const params = new URLSearchParams({
    ts: String(query.ts),
    set: query.set,
    quantity: query.quantity,
    units: query.units,
  });
  if (query.fmin !== undefined) params.set("fmin", String(query.fmin));
  if (query.fmax !== undefined) params.set("fmax", String(query.fmax));
  return request<VisMatrixResponse>(`/api/vis/matrix?${params.toString()}`);
}

export interface VisWaterfallQuery {
  i: number;
  j: number;
  t0: string;
  t1: string;
  quantity: VisQuantity;
  units: VisUnits;
  ref: VisRef;
  max_cells?: number;
}

export function getVisWaterfall(query: VisWaterfallQuery): Promise<VisWaterfallResponse> {
  const params = new URLSearchParams({
    i: String(query.i),
    j: String(query.j),
    t0: query.t0,
    t1: query.t1,
    quantity: query.quantity,
    units: query.units,
    ref: query.ref,
  });
  if (query.max_cells !== undefined) params.set("max_cells", String(query.max_cells));
  return request<VisWaterfallResponse>(`/api/vis/waterfall?${params.toString()}`);
}

export function getVisCoherence(t0: string, t1: string, set: VisSet): Promise<VisCoherenceResponse> {
  const params = new URLSearchParams({ t0, t1, set });
  return request<VisCoherenceResponse>(`/api/vis/coherence?${params.toString()}`);
}

// --- Visibilities server-rendered figures (M2 figures) ------------------

export function getVisFigureManifest(set: VisSet, ref: VisRef): Promise<VisFigureManifest> {
  const params = new URLSearchParams({ set, ref });
  return request<VisFigureManifest>(`/api/figures/vis/manifest?${params.toString()}`);
}

export function getVisFigureList(): Promise<VisFigureListResponse> {
  return request<VisFigureListResponse>("/api/figures/vis/list");
}

/** Same URL the <img> uses; ``v`` is the manifest's own rendered_utc so the
 * browser cache is bookmarked to the render that actually produced it. */
export function visFigureUrl(
  set: VisSet,
  ref: VisRef,
  kind: VisFigureKind,
  suffix: "1x" | "2x",
  renderedUtc?: string | null,
): string {
  const v = renderedUtc ? `?v=${encodeURIComponent(renderedUtc)}` : "";
  return `/api/figures/vis/${set}/${ref}/${kind}@${suffix}.png${v}`;
}

// --- SNAPs server-rendered figures -----------------------------------------

export function getSnapFigureManifest(set: SnapFigureInputSet): Promise<SnapFigureManifest> {
  const params = new URLSearchParams({ set });
  return request<SnapFigureManifest>(`/api/figures/snaps/manifest?${params.toString()}`);
}

export function getSnapFigureList(): Promise<SnapFigureListResponse> {
  return request<SnapFigureListResponse>("/api/figures/snaps/list");
}

/** Same URL the <img> uses; ``v`` is the manifest's own rendered_utc so the
 * browser cache is bookmarked to the render that actually produced it. */
export function snapFigureUrl(
  set: SnapFigureInputSet,
  kind: SnapFigureKind,
  suffix: "1x" | "2x",
  renderedUtc?: string | null,
): string {
  const v = renderedUtc ? `?v=${encodeURIComponent(renderedUtc)}` : "";
  return `/api/figures/snaps/${set}/${kind}@${suffix}.png${v}`;
}

// --- Search (M2b) ---------------------------------------------------------
// See docs/api-search.md for the full contract.

export function getSearchSummary(t0: string, t1: string): Promise<SearchSummaryResponse> {
  const params = new URLSearchParams({ t0, t1 });
  return request<SearchSummaryResponse>(`/api/search/summary?${params.toString()}`);
}

export interface SearchHistQuery {
  field: SearchHistField;
  t0: string;
  t1: string;
  bins?: number;
  log?: boolean;
}

export function getSearchHist(query: SearchHistQuery): Promise<SearchHistResponse> {
  const params = new URLSearchParams({
    field: query.field,
    t0: query.t0,
    t1: query.t1,
    log: query.log ? "1" : "0",
  });
  if (query.bins !== undefined) params.set("bins", String(query.bins));
  return request<SearchHistResponse>(`/api/search/hist?${params.toString()}`);
}

export interface SearchScatterQuery {
  x: SearchScatterField;
  y: SearchScatterField;
  t0: string;
  t1: string;
  max_points?: number;
}

export function getSearchScatter(query: SearchScatterQuery): Promise<SearchScatterResponse> {
  const params = new URLSearchParams({ x: query.x, y: query.y, t0: query.t0, t1: query.t1 });
  if (query.max_points !== undefined) params.set("max_points", String(query.max_points));
  return request<SearchScatterResponse>(`/api/search/scatter?${params.toString()}`);
}

export function getSearchBeamMap(t0: string, t1: string): Promise<SearchBeamMapResponse> {
  const params = new URLSearchParams({ t0, t1 });
  return request<SearchBeamMapResponse>(`/api/search/beam-map?${params.toString()}`);
}

export function getSearchRate(t0: string, t1: string, stepS?: number): Promise<SearchRateResponse> {
  const params = new URLSearchParams({ t0, t1 });
  if (stepS !== undefined) params.set("step_s", String(stepS));
  return request<SearchRateResponse>(`/api/search/rate?${params.toString()}`);
}

export function getSearchFunnel(t0: string, t1: string, stepS?: number): Promise<SearchFunnelResponse> {
  const params = new URLSearchParams({ t0, t1 });
  if (stepS !== undefined) params.set("step_s", String(stepS));
  return request<SearchFunnelResponse>(`/api/search/funnel?${params.toString()}`);
}

export function statusWebSocketUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/status`;
}
