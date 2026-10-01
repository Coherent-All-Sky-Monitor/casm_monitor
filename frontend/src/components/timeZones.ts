export const LOCAL = "America/Los_Angeles";

/** Offset-free API timestamps are UTC, never the browser's local zone. */
export function wallTime(utc: string, zone: string): string {
  if (!utc) return "";
  const normalized = utc.replace(" ", "T");
  const d = new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(normalized) ? normalized : normalized + "Z");
  if (!Number.isFinite(d.getTime())) return "";
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(d);
  const p = (key: string) => parts.find(x => x.type === key)?.value;
  return `${p("year")}-${p("month")}-${p("day")}T${p("hour")}:${p("minute")}`;
}

/** Returns a UTC minute without a suffix, preserving the existing selector API. */
export function utcFromWall(wall: string, zone: string, rejectAmbiguous = false): string {
  const naive = Date.parse(wall + "Z");
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(wall) || !Number.isFinite(naive)
      || new Date(naive).toISOString().slice(0, 16) !== wall) {
    throw new Error("Enter a complete, valid date and time.");
  }
  const offsets = [-12, 0, 12].map(h => {
    const d = new Date(naive + h * 3600000);
    return Date.parse(wallTime(d.toISOString(), zone) + "Z") - d.getTime();
  });
  const matches = [...new Set(offsets.map(o => new Date(naive - o).toISOString().slice(0, 16))
    .filter(s => wallTime(s, zone) === wall))].sort();
  if (!matches.length) throw new Error("That local time does not exist across the daylight-saving transition. Select another time or UTC.");
  if (rejectAmbiguous && matches.length > 1) throw new Error("That local time occurs twice when daylight saving ends. Switch to UTC to choose the intended hour.");
  return matches[0]; // Existing plot selectors retain their earlier-occurrence default.
}
