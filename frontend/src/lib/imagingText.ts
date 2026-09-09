// Every string the Imaging page says: the section sentence above the panel,
// the reading guide under the image and the source labels, mirroring
// lib/visText.ts / lib/snapText.ts.

import { formatClock, formatUtcStamp } from "./statusSentence";
import type { ImagingManifest, ImagingSourceInfo, ImagingSourceName } from "./types";

const PROJECTION_SENTENCE =
  "All-sky dirty image, l/m zenithal projection, horizon at the unit circle, north up, east left";

const SOURCE_LABEL: Record<ImagingSourceName, string> = {
  sun: "Sun",
  "cyg-a": "Cyg A",
  "cas-a": "Cas A",
  "tau-a": "Tau A",
};

export function sourceLabel(name: ImagingSourceName): string {
  return SOURCE_LABEL[name] ?? name;
}

function sourceClause(sources: ImagingSourceInfo[]): string {
  const up = sources.filter((s) => s.up);
  if (up.length === 0) return "No tracked source is above the horizon right now.";
  const parts = up.map((s, i) => {
    const alt = Math.round(s.alt_deg);
    return i === 0 ? `The ${sourceLabel(s.name)} is up at ${alt} deg` : `${sourceLabel(s.name)} at ${alt} deg`;
  });
  // "The Sun" already reads correctly; the other three sources do not take
  // an article, so only the leading "The" survives from the template above
  // when the first up source is the Sun.
  const lead = up[0].name === "sun" ? parts[0] : parts[0].replace(/^The /, "");
  return [lead, ...parts.slice(1)].join("; ") + ".";
}

/** The one section sentence above the Imaging panel: the fixed projection
 * clause, the deployed cal, the latest/rendered times and which sources are
 * up, e.g. "All-sky dirty image, ... east left; deployed cal
 * cal_sep03peak_core17.h5, 17 antennas; latest integration 02:41 UTC,
 * rendered 02:44 UTC. The Sun is up at 12 deg; Cyg A at 61 deg; Cas A at 48
 * deg." */
export function imagingSectionSentence(manifest: ImagingManifest | null): string {
  if (!manifest) return `${PROJECTION_SENTENCE}.`;
  const antClause = `deployed cal ${manifest.cal_file}, ${manifest.antennas.length} antennas`;
  const latestClock = formatClock(manifest.latest.ts) ?? "unknown";
  const renderedClock = formatClock(manifest.rendered_utc) ?? "unknown";
  const timeClause = `latest integration ${latestClock} UTC, rendered ${renderedClock} UTC`;
  return `${PROJECTION_SENTENCE}; ${antClause}; ${timeClause}. ${sourceClause(manifest.sources)}`;
}

/** The muted paragraph under the image: how to read the figure without a
 * legend (DESIGN.md #8, no legends). */
export const IMAGING_READING_GUIDE =
  "Bright compact peaks at a marked source are the source; rings around a peak are the PSF sidelobes; " +
  "a bright patch with no marker is RFI or a satellite pass.";

/** The window sentence under the 24 h strip: its own t0/t1 and frame count,
 * independent of the section sentence above (the strip can lag the latest
 * single frame by one render cycle). */
export function stripSentence(manifest: ImagingManifest | null): string {
  if (!manifest) return "No strip rendered yet.";
  const { strip } = manifest;
  return `${formatUtcStamp(strip.t0)} to ${formatUtcStamp(strip.t1)}, ${strip.n} integrations.`;
}
