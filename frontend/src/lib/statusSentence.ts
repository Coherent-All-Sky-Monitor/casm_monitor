// Turns /api/status into the one or two plain sentences that sit under the
// header, plus one short alert sentence per item that is not "ok".
//
// The status payload is a flat map of labelled items in groups (see
// docs/plan.md "API contract"); this module knows the handful of keys worth
// saying in prose and falls back to "<label> is <value>" for anything else,
// so a new collector key still reads as a sentence instead of disappearing.

import type { StatusItem, StatusResponse } from "./types";

export interface StatusSentences {
  /** The normal, muted line(s). */
  main: string;
  /** One short sentence each, alert colour. */
  problems: string[];
}

function num(value: unknown): number | null {
  return typeof value === "number" && !Number.isNaN(value) ? value : null;
}

/** Seconds as prose: "35 seconds", "4 minutes", "2 hours", "3 days". */
export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "an unknown time";
  if (seconds < 90) return `${Math.round(seconds)} seconds`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} hours`;
  return `${Math.round(seconds / 86400)} days`;
}

/** "2026-09-04-16:43:47" (the medusa UTC_START form) -> "2026-09-04 16:43". */
function formatUtcStart(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const m = value.match(/^(\d{4}-\d{2}-\d{2})[-T ](\d{2}):(\d{2})/);
  return m ? `${m[1]} ${m[2]}:${m[3]}` : value;
}

/** "2026-09-09T00:44:19Z" -> "00:44". */
export function formatClock(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const m = iso.match(/T(\d{2}):(\d{2})/);
  return m ? `${m[1]}:${m[2]}` : null;
}

/** "2026-09-09T00:44:19Z" -> "2026-09-09 00:44 UTC". */
export function formatUtcStamp(iso: string | null | undefined): string {
  if (!iso) return "unknown time";
  const m = iso.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/);
  return m ? `${m[1]} ${m[2]}:${m[3]} UTC` : iso;
}

/** Unix seconds -> "2026-09-08 14:20 UTC" (history slider, waterfall axes). */
export function formatUnixUtc(t: number): string {
  return formatUtcStamp(new Date(t * 1000).toISOString());
}

/** The deployed-weights clause, read off the weights filename when it carries
 * the usual "<...>_YYYYMMDD_<n>ant_<...>" shape, e.g.
 * weights_b0329_20260904_17ant_512_int8.h5. */
function weightsClause(items: Record<string, StatusItem>): string | null {
  const file = items.weights_file?.value;
  if (typeof file !== "string" || !file) return null;
  const date = file.match(/(\d{4})(\d{2})(\d{2})/);
  const ants = file.match(/(\d+)ant/);
  if (!date) return "the deployed weights";
  const when = `${date[1]}-${date[2]}-${date[3]}`;
  return ants
    ? `the weights of ${when} (${ants[1]} antennas)`
    : `the weights of ${when}`;
}

function hellaClause(items: Record<string, StatusItem>): string | null {
  const s1 = num(items.hella_corr1_snr?.value);
  const d1 = num(items.hella_corr1_dm_min?.value);
  const s2 = num(items.hella_corr2_snr?.value);
  const d2 = num(items.hella_corr2_dm_min?.value);
  if (s1 === null && s2 === null) return null;
  const one = s1 !== null ? `SNR ${s1}${d1 !== null ? `, DM ${d1}` : ""}` : null;
  const two = s2 !== null ? `SNR ${s2}${d2 !== null ? `, DM ${d2}` : ""}` : null;
  if (one && two && one === two) return `hella at ${one} on both nodes.`;
  if (one && two) return `hella at ${one} on corr1 and ${two} on corr2.`;
  return `hella at ${(one ?? two) as string} on ${one ? "corr1" : "corr2"}.`;
}

function servicesClause(status: StatusResponse): string | null {
  const group = status.groups.find((g) => g.name === "services");
  if (!group || group.keys.length === 0) return null;
  const bad = group.keys.filter((k) => (status.items[k]?.state ?? "stale") !== "ok");
  return bad.length === 0 ? "All services up." : null;
}

/** One short alert sentence for an item that is not "ok". */
function problemSentence(key: string, item: StatusItem): string {
  const disk = key.match(/^(nvme\d)_pct_used$/);
  if (disk) {
    const pct = num(item.value);
    return pct === null
      ? `${disk[1]} usage is unknown.`
      : `${disk[1]} is ${Math.round(pct)}% full.`;
  }
  if (key === "kafka_bp_ok" || key === "kafka_advancing") {
    return `Kafka has not delivered a frame for ${formatDuration(item.age_s)}.`;
  }
  if (key === "kafka_bp_subbands_ok") {
    const n = num(item.value);
    return n === null
      ? "Kafka subband delivery is unknown."
      : `Kafka is delivering ${n} of 6 subbands.`;
  }
  if (key === "obs_daemons_state" || key === "obs_utc_start") {
    return `${item.label} is ${item.value ?? "unknown"}.`;
  }
  if (key === "weights_registry_mismatch") {
    return "The weights registry disagrees with the ledger.";
  }
  if (item.value === null || item.value === undefined || item.ts === null) {
    return `${item.label} has never been collected.`;
  }
  if (key.endsWith("_ok") && num(item.value) === 0) {
    return `${item.label} is down.`;
  }
  if (item.state === "stale") {
    return `${item.label} has not updated for ${formatDuration(item.age_s)}.`;
  }
  const unit = item.unit ? `${item.unit === "%" ? "" : " "}${item.unit}` : "";
  return `${item.label} is ${item.value}${unit}.`;
}

export function buildStatusSentences(
  status: StatusResponse | null,
  backendStale: boolean,
): StatusSentences {
  if (!status) {
    return { main: "Waiting for the first status update.", problems: [] };
  }
  const items = status.items;
  const parts: string[] = [];

  const start = formatUtcStart(items.obs_utc_start?.value);
  const observing = items.obs_daemons_state?.value === "running";
  const weights = weightsClause(items);
  if (start && observing) {
    parts.push(`Observing since ${start} UTC${weights ? ` with ${weights}` : ""}.`);
  } else if (start) {
    parts.push(`Not observing; last obs started ${start} UTC${weights ? ` with ${weights}` : ""}.`);
  } else if (weights) {
    parts.push(`Not observing. Deployed: ${weights}.`);
  }

  const hella = hellaClause(items);
  if (hella) parts.push(hella);

  const services = servicesClause(status);
  if (services) parts.push(services);

  const clock = formatClock(status.ts);
  if (clock) parts.push(`${clock} UTC.`);

  const problems: string[] = [];
  if (backendStale) {
    problems.push("The monitor backend has not sent an update for over 30 seconds.");
  }
  for (const group of status.groups) {
    for (const key of group.keys) {
      const item = items[key];
      if (!item || item.state === "ok") continue;
      problems.push(problemSentence(key, item));
    }
  }

  return { main: parts.join(" "), problems };
}
