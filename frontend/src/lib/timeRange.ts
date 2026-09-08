// Resolves a TimeRangePicker's URL state into an ISO "since" timestamp for
// API calls. Kept separate from the component so pages can reuse it without
// importing React.

const PRESET_MS: Record<string, number> = {
  "1h": 3600_000,
  "6h": 6 * 3600_000,
  "24h": 24 * 3600_000,
  "7d": 7 * 24 * 3600_000,
};

export function resolveSince(range: string, customFrom: string): string {
  if (range === "custom") {
    return customFrom ? new Date(customFrom).toISOString() : "";
  }
  const ms = PRESET_MS[range] ?? PRESET_MS["1h"];
  return new Date(Date.now() - ms).toISOString();
}
