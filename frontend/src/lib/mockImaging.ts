// Mock backend for the Imaging tab, selected by the `?mock=1` URL flag while
// the real /api/figures/imaging/* and /api/imaging/history routes are
// implemented against docs/api-imaging.md concurrently. Shapes mirror
// lib/types.ts exactly so ImagingPage.tsx can swap real <-> mock with one
// branch, same pattern as lib/mockVis.ts / lib/mockSearch.ts. Never imported
// by lib/api.ts itself.
//
// There is no mock filesystem to serve PNGs/MP4 from, so `mockImagingFigureUrl`
// stands in for `imagingFigureUrl`: it renders a small deterministic all-sky
// placeholder (horizon circle, a few source-like dots) as an inline SVG data
// URI, keyed on the file name so repeated requests for the same file (the
// `1x`/`2x` pair, a prefetched neighbour frame) are stable.

import type { ImagingHistoryResponse, ImagingManifest, ImagingSourceInfo } from "./types";

const NETWORK_DELAY_MS = 60;

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), NETWORK_DELAY_MS));
}

function hashSeed(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h || 1;
}

function noise(seed: number): number {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

const MOCK_SOURCES: ImagingSourceInfo[] = [
  { name: "sun", alt_deg: 12, az_deg: 118, up: true },
  { name: "cyg-a", alt_deg: 61, az_deg: 42, up: true },
  { name: "cas-a", alt_deg: 48, az_deg: 8, up: true },
  { name: "tau-a", alt_deg: -14, az_deg: 260, up: false },
];

/** A stand-in all-sky image: a hairline horizon circle (the strip variant
 * skips it, being a row of thumbnails rather than a single sky) plus a
 * handful of bright dots for sources/RFI, deterministic per `file` name. */
export function mockImagingFigureUrl(file: string): string {
  const seed = hashSeed(file);
  const isStrip = file.includes("strip");
  const w = isStrip ? 960 : 360;
  const h = isStrip ? 120 : 360;
  const nDots = isStrip ? 10 : 4;
  const dots: string[] = [];
  for (let i = 0; i < nDots; i++) {
    const cx = isStrip
      ? (w / nDots) * (i + 0.5) + (noise(seed + i) - 0.5) * (w / nDots) * 0.6
      : w / 2 + (noise(seed + i) - 0.5) * w * 0.6;
    const cy = h / 2 + (noise(seed + i + 50) - 0.5) * h * 0.55;
    const r = 2 + noise(seed + i + 100) * 4;
    dots.push(
      `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${r.toFixed(1)}" fill="#2563eb" opacity="0.85" />`,
    );
  }
  const horizon = isStrip
    ? ""
    : `<circle cx="${w / 2}" cy="${h / 2}" r="${w / 2 - 8}" fill="none" stroke="#9ca3af" stroke-width="1" />`;
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}">` +
    `<rect width="${w}" height="${h}" fill="#ffffff" />${horizon}${dots.join("")}</svg>`;
  return `data:image/svg+xml;base64,${btoa(svg)}`;
}

export function mockGetImagingManifest(): Promise<ImagingManifest> {
  const now = Date.now();
  const rendered = new Date(now).toISOString();
  const latestTs = new Date(now - 3 * 60_000).toISOString();
  const t0 = new Date(now - 24 * 3600_000).toISOString();
  return delay({
    rendered_utc: rendered,
    cal_file: "cal_sep03peak_core17.h5",
    antennas: Array.from({ length: 17 }, (_, i) => i + 1),
    latest: { ts: latestTs, file_1x: "latest@1x.png", file_2x: "latest@2x.png" },
    strip: { t0, t1: rendered, n: 576, file_1x: "strip24h@1x.png", file_2x: "strip24h@2x.png" },
    // Left null (rather than faking video bytes) so the movie view exercises
    // the same "not rendered yet" path the real backend takes before the
    // first mp4 lands.
    movie: { file: null, fps: 4 },
    sources: MOCK_SOURCES,
    psf_ceiling_snr: 42.3,
  });
}

export function mockGetImagingHistory(t0: string, t1: string): Promise<ImagingHistoryResponse> {
  const start = new Date(t0).getTime();
  const end = new Date(t1).getTime();
  const stepMs = 150_000; // ~2.5 min cadence, matching the manifest's strip.n over 24 h
  const frames: ImagingHistoryResponse["frames"] = [];
  if (Number.isFinite(start) && Number.isFinite(end) && end > start) {
    for (let t = start; t <= end; t += stepMs) {
      const unix = Math.round(t / 1000);
      frames.push({ ts: new Date(t).toISOString(), file_1x: `frames/${unix}@1x.png` });
    }
  }
  return delay({ frames });
}
