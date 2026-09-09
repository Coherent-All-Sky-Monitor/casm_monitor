// Typed client for the casm_monitor backend. Errors are surfaced through the
// toast bus (lib/toast.ts) rather than only console.error, per the M0 spec.

import { emitError } from "./toast";
import type {
  CalBuildAcceptedResponse,
  CalBuildDetailResponse,
  CalBuildRequest,
  CalBuildsResponse,
  CalDefaultsResponse,
  CalStageAcceptedResponse,
  CalStatusResponse,
  CalUploadAcceptedResponse,
  CalUploadRequest,
  CandEventDetailResponse,
  CandEventsResponse,
  CandFrbsResponse,
  CandInjectionsResponse,
  CandLabel,
  CandLabelPostResponse,
  CandStatsResponse,
  CandTransitsResponse,
  CandView,
  EventRecord,
  EventsResponse,
  HealthResponse,
  ImagingHistoryResponse,
  ImagingManifest,
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

// --- Imaging (M4) --------------------------------------------------------
// See docs/api-imaging.md for the full contract.

export function getImagingManifest(): Promise<ImagingManifest> {
  return request<ImagingManifest>("/api/figures/imaging/manifest");
}

/** Same URL an `<img>`/`<video>` uses; `v` is the manifest's own
 * `rendered_utc` (or, for a scrub-view history frame, that frame's own
 * `ts`) so the browser cache is bookmarked to the render that produced it —
 * same pattern as `visFigureUrl`/`snapFigureUrl`. `file` may itself carry a
 * `/` (history frames are `frames/<unix>@1x.png`), served under the same
 * whitelisted route. */
export function imagingFigureUrl(file: string, v?: string | null): string {
  const qs = v ? `?v=${encodeURIComponent(v)}` : "";
  return `/api/figures/imaging/${file}${qs}`;
}

export interface ImagingHistoryQuery {
  t0: string;
  t1: string;
}

export function getImagingHistory(query: ImagingHistoryQuery): Promise<ImagingHistoryResponse> {
  const params = new URLSearchParams({ t0: query.t0, t1: query.t1 });
  return request<ImagingHistoryResponse>(`/api/imaging/history?${params.toString()}`);
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

// --- Calibration (M3) ---------------------------------------------------
// See docs/api-cal.md for the full contract.

export function getCalDefaults(date: string): Promise<CalDefaultsResponse> {
  const params = new URLSearchParams({ date });
  return request<CalDefaultsResponse>(`/api/cal/defaults?${params.toString()}`);
}

export function getCalBuilds(): Promise<CalBuildsResponse> {
  return request<CalBuildsResponse>("/api/cal/builds");
}

export function getCalBuild(tag: string): Promise<CalBuildDetailResponse> {
  return request<CalBuildDetailResponse>(`/api/cal/builds/${encodeURIComponent(tag)}`);
}

export function getCalStatus(): Promise<CalStatusResponse> {
  return request<CalStatusResponse>("/api/cal/status");
}

export function calFigureUrl(tag: string, name: string): string {
  return `/api/cal/builds/${encodeURIComponent(tag)}/figs/${encodeURIComponent(name)}.png`;
}

export function calLogUrl(tag: string): string {
  return `/api/cal/builds/${encodeURIComponent(tag)}/log`;
}

export function calNotebookUrl(tag: string): string {
  return `/api/cal/builds/${encodeURIComponent(tag)}/notebook`;
}

export function getCalLog(tag: string): Promise<string> {
  return fetch(calLogUrl(tag)).then((res) => {
    if (!res.ok) throw new ApiError(`${calLogUrl(tag)} failed: ${res.status}`, res.status);
    return res.text();
  });
}

/** A structured result for the three write actions below: the caller (the
 * Calibration page) renders `detail` inline as prose rather than a generic
 * toast, since a 409/403 here is an expected, meaningful outcome (tag
 * collision, build running, uploads disabled, not staged) — same pattern as
 * `postSnapBoardRead`'s 429 handling. */
export interface CalActionResult<T> {
  ok: boolean;
  status: number;
  body: T | null;
  detail: string | null;
}

/** The CSRF double-submit token the backend sets on `GET /api/cal/status`
 * (`casm_monitor_csrf`). The upload POST must echo it in `X-CSRF-Token` or the
 * backend answers 403; the page always polls status first, so by the time the
 * Upload button exists the cookie is there. */
const CSRF_COOKIE = "casm_monitor_csrf";
const CSRF_HEADER = "X-CSRF-Token";

function csrfToken(): string {
  const match = document.cookie.split("; ").find((row) => row.startsWith(`${CSRF_COOKIE}=`));
  return match ? decodeURIComponent(match.slice(CSRF_COOKIE.length + 1)) : "";
}

async function postJson<T>(
  path: string,
  body: unknown,
  extraHeaders: Record<string, string> = {},
): Promise<CalActionResult<T>> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...extraHeaders },
      body: JSON.stringify(body ?? {}),
    });
  } catch (err) {
    const message = `network error contacting ${path}: ${(err as Error).message}`;
    emitError(message);
    return { ok: false, status: 0, body: null, detail: message };
  }
  const json = await res.json().catch(() => null);
  if (!res.ok) {
    const detail =
      (json as { detail?: string } | null)?.detail ?? `${path} failed: ${res.status} ${res.statusText}`;
    return { ok: false, status: res.status, body: null, detail };
  }
  return { ok: true, status: res.status, body: json as T, detail: null };
}

export function postCalBuild(body: CalBuildRequest): Promise<CalActionResult<CalBuildAcceptedResponse>> {
  return postJson<CalBuildAcceptedResponse>("/api/cal/build", body);
}

export function postCalStage(tag: string): Promise<CalActionResult<CalStageAcceptedResponse>> {
  return postJson<CalStageAcceptedResponse>(`/api/cal/builds/${encodeURIComponent(tag)}/stage`, {});
}

export function postCalUpload(
  tag: string,
  body: CalUploadRequest,
): Promise<CalActionResult<CalUploadAcceptedResponse>> {
  return postJson<CalUploadAcceptedResponse>(
    `/api/cal/builds/${encodeURIComponent(tag)}/upload`,
    body,
    { [CSRF_HEADER]: csrfToken() },
  );
}

// --- Candidates (M5) -----------------------------------------------------
// See docs/api-cands.md for the full contract.

export interface CandEventsQuery {
  tier?: string;
  tag?: string;
  view?: CandView;
  limit?: number;
  since?: string;
}

export function getCandEvents(query: CandEventsQuery = {}): Promise<CandEventsResponse> {
  const params = new URLSearchParams();
  if (query.tier) params.set("tier", query.tier);
  if (query.tag) params.set("tag", query.tag);
  if (query.view) params.set("view", query.view);
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  if (query.since) params.set("since", query.since);
  const qs = params.toString();
  return request<CandEventsResponse>(`/api/cands/events${qs ? `?${qs}` : ""}`);
}

export function getCandEvent(name: string): Promise<CandEventDetailResponse> {
  return request<CandEventDetailResponse>(`/api/cands/events/${encodeURIComponent(name)}`);
}

export function candPlotUrl(name: string, fname: string): string {
  return `/api/cands/events/${encodeURIComponent(name)}/plot/${encodeURIComponent(fname)}`;
}

/** Same CSRF double-submit cookie the Calibration tab mints
 * (`casm_monitor_csrf` — `GET /api/cands/events` also mints it, so the tab
 * always has one by the time a label button is clickable). */
export function postCandLabel(
  name: string,
  label: CandLabel,
  note: string,
): Promise<CalActionResult<CandLabelPostResponse>> {
  return postJson<CandLabelPostResponse>(
    `/api/cands/events/${encodeURIComponent(name)}/label`,
    { label, note },
    { [CSRF_HEADER]: csrfToken() },
  );
}

export function getCandStats(hours: number): Promise<CandStatsResponse> {
  return request<CandStatsResponse>(`/api/cands/stats?hours=${hours}`);
}

export function candStatsPlotUrl(hours: number): string {
  return `/api/cands/stats/plot.png?hours=${hours}`;
}

export function getCandInjections(limit = 200): Promise<CandInjectionsResponse> {
  return request<CandInjectionsResponse>(`/api/cands/injections?limit=${limit}`);
}

export function getCandFrbs(): Promise<CandFrbsResponse> {
  return request<CandFrbsResponse>("/api/cands/frbs");
}

export function getCandTransits(): Promise<CandTransitsResponse> {
  return request<CandTransitsResponse>("/api/cands/transits");
}

export function statusWebSocketUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/status`;
}
