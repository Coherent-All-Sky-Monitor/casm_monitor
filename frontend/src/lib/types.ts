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

// Visibilities tab (M2). Kept in sync by hand with docs/api-vis.md.

export type VisSet = "live" | "wired";
export type VisPairs = "auto" | "cross" | "all";
export type VisQuantity = "amp" | "phase" | "real" | "imag" | "coh";
export type VisUnits = "linear" | "db" | "log10" | "deg" | "rad";
export type VisRef = "raw" | "sun" | "cal";

export interface VisInputInfo {
  packet_idx: number;
  antenna: number | null;
  station: string | null;
  in_bf: boolean;
}

export interface VisObsInfo {
  utc_start: string | null;
  latest_ts: string | null;
  n_cached: number;
  oldest_ts: string | null;
}

export interface VisInputsResponse {
  sets: { live: number[]; wired: number[] };
  inputs: VisInputInfo[];
  obs: VisObsInfo;
}

export interface VisTimesResponse {
  t: number[];
}

export interface VisBaseline {
  i: number;
  j: number;
  ant_i: number | null;
  ant_j: number | null;
  y: (number | null)[];
}

export interface VisSpectraResponse {
  ts: number | null;
  freq_mhz: number[];
  baselines: VisBaseline[];
  flags: Record<string, unknown>;
}

export interface VisMatrixResponse {
  inputs: number[];
  m: number[][];
}

export type VisWaterfallRes = "10s" | "60s" | "10min" | "1h" | string;

export interface VisWaterfallResponse {
  t: number[];
  freq_mhz: number[];
  z: number[][];
  res: VisWaterfallRes;
}

export interface VisCoherenceResponse {
  inputs: number[];
  m: number[][];
}

// Search tab (M2b). Kept in sync by hand with docs/api-search.md.

export interface SearchJobStat {
  job: number;
  node: string;
  n: number;
  rate_per_min: number;
  last_ts: number | null;
}

export interface SearchThreshold {
  snr: number;
  dm_min: number;
}

export interface SearchFunnelTotals {
  n_cands: number;
  n_clusters: number;
  n_stored: number;
  n_vetoed: number;
  n_triggers: number;
}

export interface SearchSummaryResponse {
  t0: string;
  t1: string;
  n_cands: number;
  per_job: SearchJobStat[];
  thresholds: Record<string, SearchThreshold>;
  funnel: SearchFunnelTotals;
}

export type SearchHistField = "snr" | "dm" | "width" | "beam";

export interface SearchHistResponse {
  edges: number[];
  counts: number[];
}

export type SearchScatterField = "snr" | "dm" | "width" | "beam" | "time";

export interface SearchScatterResponse {
  x: number[];
  y: number[];
  n_total: number;
}

export interface SearchBeamMapResponse {
  counts: number[];
}

export interface SearchRateResponse {
  t: number[];
  per_job: Record<string, number[]>;
  total: number[];
}

export interface SearchFunnelResponse {
  t: number[];
  n_cands: number[];
  n_clusters: number[];
  n_stored: number[];
  n_vetoed: number[];
}

// Visibilities server-rendered figures (docs/plan.md M2 figures brief).

export type VisFigureQuantity = "amp" | "phase" | "real" | "imag" | "coh";
export type VisFigureKind =
  | `matrix_${VisFigureQuantity}`
  | `spectra_${VisFigureQuantity}`
  | "autos";

export interface VisFigureManifest {
  rendered_utc: string;
  set: VisSet;
  ref: VisRef;
  t0: number;
  t1: number;
  n_integrations: number;
  stream: string;
  obs: string | null;
  files: Record<string, { "1x": string; "2x": string }>;
  kinds: string[];
}

export interface VisFigureListCombo {
  set: VisSet;
  ref: VisRef;
  rendered_utc: string | null;
  kinds: string[];
}

export interface VisFigureListResponse {
  kinds: string[];
  sets: VisSet[];
  refs: VisRef[];
  combos: VisFigureListCombo[];
}

// SNAPs server-rendered figures.

export type SnapFigureInputSet = "beamforming" | "all12";
export type SnapFigureKind = "spectra_correlator" | "spectra_board" | "waterfall" | "trend";

export interface SnapFigureManifest {
  rendered_utc: string;
  set: SnapFigureInputSet;
  t0: number | null;
  t1: number | null;
  n_frames: number | null;
  board_read_ts: number | null;
  files: Record<string, { "1x": string; "2x": string }>;
  kinds: string[];
}

export interface SnapFigureListCombo {
  set: SnapFigureInputSet;
  rendered_utc: string | null;
  board_read_ts: number | null;
  kinds: string[];
}

export interface SnapFigureListResponse {
  kinds: string[];
  sets: SnapFigureInputSet[];
  combos: SnapFigureListCombo[];
}

// Imaging tab (M4) server-rendered figures. Kept in sync by hand with
// docs/api-imaging.md.

export type ImagingSourceName = "sun" | "cyg-a" | "cas-a" | "tau-a";

export interface ImagingLatest {
  ts: string;
  /** rendered time minus the latest imaged integration, seconds. */
  lag_s?: number;
  file_1x: string;
  file_2x: string;
}

/** One per-source cutout (`image_around_source`) of the latest integration,
 * for every source more than 10 deg above the horizon, highest first. */
export interface ImagingCutout {
  source: ImagingSourceName;
  alt_deg: number;
  az_deg: number;
  file_1x: string;
  file_2x: string;
  snr: number;
  ceiling_snr: number | null;
}

export interface ImagingStrip {
  t0: string;
  t1: string;
  n: number;
  file_1x: string;
  file_2x: string;
}

export interface ImagingMovie {
  file: string | null;
  fps: number;
}

export interface ImagingSourceInfo {
  name: ImagingSourceName;
  alt_deg: number;
  az_deg: number;
  up: boolean;
}

export interface ImagingManifest {
  rendered_utc: string;
  cal_file: string;
  antennas: number[];
  latest: ImagingLatest;
  strip: ImagingStrip;
  movie: ImagingMovie;
  sources: ImagingSourceInfo[];
  cutouts: ImagingCutout[];
  psf_ceiling_snr: number | null;
  config_fingerprint?: string;
}

export interface ImagingHistoryFrame {
  ts: string;
  file_1x: string;
}

export interface ImagingHistoryResponse {
  frames: ImagingHistoryFrame[];
}

// Calibration tab (M3). Kept in sync by hand with docs/api-cal.md.

export interface CalSourceInfo {
  name: string;
  enabled: boolean;
}

export interface CalDeployedInfo {
  cal_file: string;
  weights_file: string;
  scale: number;
  ib_scale: number;
  product_id: string;
}

export interface CalLayoutInfo {
  path: string;
  sha256: string;
  n_bf: number;
  n_wired: number;
}

export interface CalDefaultsResponse {
  date: string;
  source: string;
  sources: CalSourceInfo[];
  sun_max_utc: string;
  source_window: [string, string];
  window_offset_min: number;
  static_window: [string, string] | null;
  static_note: string;
  antennas: number[];
  antennas_note: string;
  ref_ant: number;
  tag: string;
  deployed: CalDeployedInfo;
  layout: CalLayoutInfo;
}

export interface CalBuildRequest {
  source: string;
  source_window: [string, string];
  static_window: [string, string] | null;
  antennas: number[];
  ref_ant: number;
  tag: string;
}

export interface CalBuildAcceptedResponse {
  job_id: number;
  tag: string;
}

export type CalBuildState = "queued" | "running" | "done" | "failed" | string;

export interface CalBuildListItem {
  tag: string;
  state: CalBuildState;
  created: string;
  source: string;
  source_window: [string, string];
  n_ant: number;
  rank1_median: number | null;
  has_weights: boolean;
  staged: boolean;
  uploaded: boolean;
}

export interface CalBuildsResponse {
  builds: CalBuildListItem[];
}

export interface CalBuildParams {
  source: string;
  source_window: [string, string];
  static_window: [string, string] | null;
  antennas: number[];
  ref_ant: number;
}

export interface CalFigureRef {
  name: string;
  title: string;
}

export interface CalBuildSummary {
  cal_h5: string;
  weights_h5: string;
  ib_h5: string;
  rank1_median: number | null;
  subband_occupancy: number[];
  pointing_fit: Record<string, number>;
  delay_fit_rms_deg: number;
  beam_check: Record<string, number>;
  figs: CalFigureRef[];
  notebook: boolean;
  wall_s: number;
  peak_rss_mb: number;
}

export interface CalStageFile {
  name: string;
  md5: string;
  scale: number;
}

export interface CalStageCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface CalStageInfo {
  staged_utc: string;
  files: CalStageFile[];
  command: string;
  checks: CalStageCheck[];
}

export interface CalUploadRecord {
  ts: string;
  exit_code: number;
  command: string;
  note: string;
  registry_id: string;
}

export interface CalBuildDetailResponse {
  tag: string;
  state: CalBuildState;
  params: CalBuildParams;
  summary: CalBuildSummary | null;
  stage: CalStageInfo | null;
  uploads: CalUploadRecord[];
}

export interface CalStageAcceptedResponse {
  job_id: number;
}

export interface CalUploadRequest {
  confirm_tag: string;
  save_defaults: boolean;
  note: string;
}

export interface CalUploadAcceptedResponse {
  job_id: number;
}

export interface CalLedgerRow {
  date: string;
  weights_file: string;
  cal_file: string;
  scale: number;
  ib_scale: number;
}

export interface CalActiveJob {
  id: string | number;
  kind: string;
  state: JobState;
}

export interface CalStatusResponse {
  allow_upload: boolean;
  casm_track_running: boolean;
  ledger_row: CalLedgerRow | null;
  active_job: CalActiveJob | null;
}

// Candidates tab (M5). Kept in sync by hand with docs/api-cands.md.

export type CandLabel = "frb" | "pulsar" | "rfi" | "unsure";
export type CandView = "candidates" | "all";

export interface CandEventRow {
  name: string;
  event_utc: string;
  snr: number;
  dm: number;
  width: number;
  beam: number;
  tier: string;
  tags: string[];
  n_beams: number;
  n_members: number;
  alt_deg: number | null;
  az_deg: number | null;
  label: CandLabel | null;
  outcome: string | null;
}

export interface CandEventsResponse {
  events: CandEventRow[];
}

export interface CandLabelRecord {
  id: number;
  name: string;
  label: CandLabel;
  who: string;
  notes: string;
  created_utc: string;
}

export interface CandTriggerRecord {
  id: number;
  candname: string;
  stream: number;
  kind: string;
  action: string;
  detail: string;
  dump_utc_start: string | null;
  dump_utc_stop: string | null;
  bytes_written: number | null;
  cleaned_utc: string | null;
  created_utc: string;
}

export interface CandEventDetailResponse {
  event: Record<string, unknown>;
  tags_display: string[];
  triggers: CandTriggerRecord[];
  labels: CandLabelRecord[];
  plots: string[];
  meta: Record<string, unknown>;
  data_status: string;
  label_choices: CandLabel[];
}

export interface CandLabelPostResponse {
  name: string;
  label: CandLabel;
  labels: CandLabelRecord[];
}

export interface CandTransitSource {
  name: string;
  alt: number;
  az: number;
  dec: number;
  transit: string;
}

export interface CandNowSnapshot {
  epoch_ms: number;
  lst_h: number;
  sources: CandTransitSource[];
}

export interface CandWindowStats {
  gulps: number;
  cands: number;
  clusters: number;
  stored: number;
  would: number;
  ms: number | null;
  cands_s: number;
}

export interface CandGulpStatsRow {
  gulp_utc: string;
  n_jobs: number;
  n_cands: number;
  n_clusters: number;
  n_stored: number;
  n_would: number;
  clustering_ms: number;
}

export interface CandStatsResponse {
  hours: number;
  win_label: string;
  presets: { hours: number; label: string }[];
  hour: CandWindowStats;
  win: CandWindowStats;
  now: CandNowSnapshot | null;
  rows: CandGulpStatsRow[];
}

export interface CandInjectionRow {
  id: number;
  inject_utc: string;
  beam: number;
  dm: number;
  amp: number;
  sigma_ms: number;
  est_snr: number | null;
  rec_snr: number | null;
  rec_dm: number | null;
  gate_t1: number | null;
  gate_t2: number | null;
  gate_trigger: number | null;
  event_name: string | null;
  fail_reason: string | null;
}

export interface CandInjectionsResponse {
  injections: CandInjectionRow[];
  day: { n: number; t1: number | null; t2: number | null; tr: number | null; done: number };
}

export interface CandFrbRow {
  name: string;
  event_utc: string;
  snr: number;
  dm: number;
  width: number;
  beam: number;
  notes: string;
  created_utc: string;
}

export interface CandFrbsResponse {
  frbs: CandFrbRow[];
}

export interface CandTransitsResponse {
  snapshot: CandNowSnapshot | null;
}
