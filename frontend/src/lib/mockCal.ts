// Mock backend for the Calibration tab, selected by the `?mock=1` URL flag
// while the real /api/cal/* routes are implemented against docs/api-cal.md
// concurrently. Shapes mirror lib/types.ts exactly so CalPage/CalBuildPage
// can swap real <-> mock with one branch. Never imported by lib/api.ts
// itself.
//
// `?mock=1&upload=1` additionally flips `allow_upload` to true (and clears
// `casm_track_running`) so both upload-disabled and upload-enabled states
// are reachable without a backend, per the spec's verification note.

import type { CalActionResult } from "./api";
import type {
  CalBuildAcceptedResponse,
  CalBuildDetailResponse,
  CalBuildListItem,
  CalBuildRequest,
  CalBuildsResponse,
  CalBuildState,
  CalDefaultsResponse,
  CalStageAcceptedResponse,
  CalStatusResponse,
  CalUploadAcceptedResponse,
  CalUploadRequest,
  JobDetail,
} from "./types";

const NETWORK_DELAY_MS = 60;

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), NETWORK_DELAY_MS));
}

function allowUploadFromUrl(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("upload") === "1";
}

const DEFAULT_ANTENNAS = [1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18];

const FIG_NAMES = [
  "phase_raw_sawtooth",
  "phase_stage2_fringe_stopped",
  "phase_stage3_calibrated",
  "gain_delay_fits",
  "svd_vs_freq",
  "rank1_vs_freq",
  "beam_check_sun",
  "cal_diff",
  "beam_grid",
  "source_transit",
  "autocorr",
];

const FIG_TITLES: Record<string, string> = {
  phase_raw_sawtooth: "raw phase, sawtooth (pre fringe-stop)",
  phase_stage2_fringe_stopped: "phase after fringe-stopping",
  phase_stage3_calibrated: "phase after calibration",
  gain_delay_fits: "per-antenna gain/delay fits",
  svd_vs_freq: "SVD singular values vs frequency",
  rank1_vs_freq: "rank-1 fraction vs frequency",
  beam_check_sun: "beam check: sun",
  cal_diff: "diff vs the currently deployed cal",
  beam_grid: "beam grid",
  source_transit: "source transit",
  autocorr: "autocorrelations",
};

/** A small deterministic inline-SVG standing in for a server-rendered PNG,
 * so the mock can exercise the figure-list layout without a real backend. */
export function mockCalFigureUrl(tag: string, name: string): string {
  let seed = 0;
  for (const ch of `${tag}/${name}`) seed = (seed * 31 + ch.charCodeAt(0)) % 997;
  const bars = Array.from({ length: 24 }, (_, i) => {
    const h = 10 + ((seed * (i + 3)) % 70);
    const x = i * 28;
    return `<rect x="${x}" y="${100 - h}" width="22" height="${h}" fill="#9ca3af" />`;
  }).join("");
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="672" height="140">
    <rect width="672" height="140" fill="#ffffff" />
    ${bars}
    <text x="8" y="132" font-family="sans-serif" font-size="12" fill="#6b7280">${name} (mock)</text>
  </svg>`;
  return `data:image/svg+xml;base64,${btoa(svg)}`;
}

export function mockGetCalDefaults(date: string): Promise<CalDefaultsResponse> {
  return delay({
    date,
    source: "sun",
    sources: [
      { name: "sun", enabled: true },
      { name: "cyga", enabled: false },
      { name: "casa", enabled: false },
    ],
    sun_max_utc: `${date}T19:50:00Z`,
    source_window: [`${date}T19:20:00Z`, `${date}T20:20:00Z`],
    window_offset_min: 30,
    static_window: [`${date}T03:00:00Z`, `${date}T03:30:00Z`],
    static_note: "last night, 03:00 to 03:30 UTC",
    antennas: DEFAULT_ANTENNAS,
    antennas_note: `${DEFAULT_ANTENNAS.length} antennas from the layout`,
    ref_ant: 9,
    tag: `${date.replace(/-/g, "")}_1950`,
    deployed: {
      cal_file: "/data/casm/cal/20260807_cal.h5",
      weights_file: "/data/casm/default_weights_64ant_512beam/weights.h5",
      scale: 128,
      ib_scale: 512,
      product_id: "20260807_1720",
    },
    layout: {
      path: "/home/user/antenna_layouts/current",
      sha256: "a12aaaaeffdf000000000000000000000000000000000000000000000000",
      n_bf: DEFAULT_ANTENNAS.length,
      n_wired: 24,
    },
  });
}

interface MockBuild extends CalBuildListItem {
  static_window: [string, string] | null;
  antennas: number[];
  ref_ant: number;
  createdMs: number;
}

const builds: MockBuild[] = [
  {
    tag: "20260907_1946",
    state: "done",
    created: "2026-09-07T19:48:00Z",
    createdMs: Date.parse("2026-09-07T19:48:00Z"),
    source: "sun",
    source_window: ["2026-09-07T19:16:00Z", "2026-09-07T20:16:00Z"],
    static_window: ["2026-09-07T03:00:00Z", "2026-09-07T03:30:00Z"],
    antennas: DEFAULT_ANTENNAS,
    ref_ant: 9,
    n_ant: DEFAULT_ANTENNAS.length,
    rank1_median: 0.94,
    has_weights: true,
    staged: true,
    uploaded: true,
  },
  {
    tag: "20260906_1943",
    state: "failed",
    created: "2026-09-06T19:45:00Z",
    createdMs: Date.parse("2026-09-06T19:45:00Z"),
    source: "sun",
    source_window: ["2026-09-06T19:13:00Z", "2026-09-06T20:13:00Z"],
    static_window: null,
    antennas: DEFAULT_ANTENNAS.slice(0, 15),
    ref_ant: 9,
    n_ant: 15,
    rank1_median: null,
    has_weights: false,
    staged: false,
    uploaded: false,
  },
];

let jobSeq = 1000;
const jobs = new Map<number, JobDetail>();

function makeJob(kind: string): number {
  const id = jobSeq++;
  jobs.set(id, { id, kind, state: "queued", created: new Date().toISOString(), started: null, finished: null });
  setTimeout(() => {
    const j = jobs.get(id);
    if (j) jobs.set(id, { ...j, state: "running", started: new Date().toISOString() });
  }, 200);
  return id;
}

function finishJob(id: number, effect: () => void) {
  setTimeout(() => {
    effect();
    const j = jobs.get(id);
    if (j) jobs.set(id, { ...j, state: "done", finished: new Date().toISOString() });
  }, 900);
}

export function mockGetJob(id: number): Promise<JobDetail> {
  const job = jobs.get(id);
  if (!job) return Promise.reject(new Error("no such job"));
  return delay(job);
}

export function mockGetCalBuilds(): Promise<CalBuildsResponse> {
  const sorted = [...builds].sort((a, b) => b.createdMs - a.createdMs);
  return delay({ builds: sorted.map(({ createdMs: _createdMs, static_window: _sw, antennas: _a, ref_ant: _r, ...rest }) => rest) });
}

function findBuild(tag: string): MockBuild | undefined {
  return builds.find((b) => b.tag === tag);
}

export function mockGetCalBuild(tag: string): Promise<CalBuildDetailResponse> {
  const b = findBuild(tag);
  if (!b) return Promise.reject(new Error(`no such build ${tag}`));
  const summary =
    b.state === "done"
      ? {
          cal_h5: `/mnt/nvme5/casm_pipeline/cal/${tag}/cal.h5`,
          weights_h5: `/mnt/nvme5/casm_pipeline/cal/${tag}/weights.h5`,
          ib_h5: `/mnt/nvme5/casm_pipeline/cal/${tag}/ib_weights.h5`,
          rank1_median: b.rank1_median,
          subband_occupancy: [1.0, 1.0, 0.98, 1.0, 0.91, 1.0],
          pointing_fit: { az_off_deg: 0.02, el_off_deg: -0.01 },
          delay_fit_rms_deg: 3.2,
          beam_check: { peak_snr: 42.1, fwhm_min: 0.9 },
          figs: FIG_NAMES.map((name) => ({ name, title: FIG_TITLES[name] })),
          notebook: true,
          wall_s: 143.2,
          peak_rss_mb: 8210,
        }
      : null;
  const stage = b.staged
    ? {
        staged_utc: b.created,
        files: [
          { name: "weights.h5", md5: "9f8c1234deadbeef", scale: 128 },
          { name: "ib_weights.h5", md5: "1ab2cafefeedface", scale: 512 },
        ],
        command: `scp weights.h5 ib_weights.h5 user@zapdos:/data/casm/staged/${tag}/`,
        checks: [
          { name: "shape matches deployed layout", ok: true, detail: `${b.n_ant}x512` },
          { name: "scale within [64, 512]", ok: true, detail: "128" },
        ],
      }
    : null;
  return delay({
    tag: b.tag,
    state: b.state,
    params: {
      source: b.source,
      source_window: b.source_window,
      static_window: b.static_window,
      antennas: b.antennas,
      ref_ant: b.ref_ant,
    },
    summary,
    stage,
    uploads: b.uploaded
      ? [
          {
            ts: b.created,
            exit_code: 0,
            command: `casm-deploy-weights --tag ${tag}`,
            note: "routine nightly solve",
            registry_id: b.created.replace(/[-:TZ]/g, "").slice(0, 13),
          },
        ]
      : [],
  });
}

export function mockPostCalBuild(body: CalBuildRequest): Promise<CalActionResult<CalBuildAcceptedResponse>> {
  if (findBuild(body.tag)) {
    return delay({ ok: false, status: 409, body: null, detail: `tag '${body.tag}' already exists` });
  }
  if (builds.some((b) => b.state === "queued" || b.state === "running")) {
    return delay({ ok: false, status: 409, body: null, detail: "a build is already running" });
  }
  const now = new Date();
  const b: MockBuild = {
    tag: body.tag,
    state: "queued" as CalBuildState,
    created: now.toISOString(),
    createdMs: now.getTime(),
    source: body.source,
    source_window: body.source_window,
    static_window: body.static_window,
    antennas: body.antennas,
    ref_ant: body.ref_ant,
    n_ant: body.antennas.length,
    rank1_median: null,
    has_weights: false,
    staged: false,
    uploaded: false,
  };
  builds.unshift(b);
  const jobId = makeJob("cal_build");
  finishJob(jobId, () => {
    b.state = "running";
    setTimeout(() => {
      b.state = "done";
      b.rank1_median = 0.9 + Math.random() * 0.08;
      b.has_weights = true;
    }, 900);
  });
  return delay({ ok: true, status: 200, body: { job_id: jobId, tag: body.tag }, detail: null });
}

export function mockPostCalStage(tag: string): Promise<CalActionResult<CalStageAcceptedResponse>> {
  const b = findBuild(tag);
  if (!b) return delay({ ok: false, status: 404, body: null, detail: "no such build" });
  if (b.state !== "done") {
    return delay({ ok: false, status: 409, body: null, detail: `build '${tag}' is not done yet` });
  }
  const jobId = makeJob("cal_stage");
  finishJob(jobId, () => {
    b.staged = true;
  });
  return delay({ ok: true, status: 200, body: { job_id: jobId }, detail: null });
}

export function mockPostCalUpload(
  tag: string,
  body: CalUploadRequest,
): Promise<CalActionResult<CalUploadAcceptedResponse>> {
  const allowUpload = allowUploadFromUrl();
  if (!allowUpload) {
    return delay({
      ok: false,
      status: 403,
      body: null,
      detail: "uploads are disabled on this service (CASM_MONITOR_ALLOW_UPLOAD)",
    });
  }
  const b = findBuild(tag);
  if (!b) return delay({ ok: false, status: 404, body: null, detail: "no such build" });
  if (!b.staged) {
    return delay({ ok: false, status: 409, body: null, detail: `tag '${tag}' has not been staged` });
  }
  if (body.confirm_tag !== tag) {
    return delay({ ok: false, status: 400, body: null, detail: "confirm_tag does not match" });
  }
  const jobId = makeJob("cal_upload");
  finishJob(jobId, () => {
    b.uploaded = true;
  });
  return delay({ ok: true, status: 200, body: { job_id: jobId }, detail: null });
}

export function mockGetCalStatus(): Promise<CalStatusResponse> {
  const allowUpload = allowUploadFromUrl();
  return delay({
    allow_upload: allowUpload,
    casm_track_running: false,
    ledger_row: {
      date: "2026-08-07",
      weights_file: "/data/casm/default_weights_64ant_512beam/weights.h5",
      cal_file: "/data/casm/cal/20260807_cal.h5",
      scale: 128,
      ib_scale: 512,
    },
    active_job: null,
  });
}
