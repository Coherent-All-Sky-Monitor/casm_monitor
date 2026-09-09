// Resolves a TimeRangePicker's URL state into an ISO "since" timestamp for
// API calls. Kept separate from the component so pages can reuse it without
// importing React.

const PRESET_MS: Record<string, number> = {
  "1h": 3600_000,
  "2h": 2 * 3600_000,
  "6h": 6 * 3600_000,
  "24h": 24 * 3600_000,
  "7d": 7 * 24 * 3600_000,
};

/** True if `value` parses to a valid Date (used by TimeRangePicker to flag
 * unparseable custom input rather than letting resolveSince silently drop
 * it). */
export function isValidDateInput(value: string): boolean {
  if (!value) return false;
  return !Number.isNaN(new Date(value).getTime());
}

export function resolveSince(range: string, customFrom: string): string {
  if (range === "custom") {
    if (!customFrom) return "";
    const d = new Date(customFrom);
    if (Number.isNaN(d.getTime())) return "";
    return d.toISOString();
  }
  const ms = PRESET_MS[range] ?? PRESET_MS["1h"];
  return new Date(Date.now() - ms).toISOString();
}
