import { useEffect, useState } from "react";
import Segmented from "../components/Segmented";
import TimeRangePicker from "../components/TimeRangePicker";
import { getEvents } from "../lib/api";
import { resolveSince } from "../lib/timeRange";
import { useUrlParam } from "../lib/useUrlParam";
import { formatUtcStamp } from "../lib/statusSentence";
import type { EventRecord } from "../lib/types";

const REFRESH_MS = 30_000;

/** The detail object as one short line: "reason=manual, elapsed_s=9.4". */
function oneLineDetail(detail: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(detail ?? {})) {
    let text: string;
    if (value === null || value === undefined) text = "none";
    else if (Array.isArray(value)) text = `${value.length} items`;
    else if (typeof value === "object") text = `${Object.keys(value).length} entries`;
    else text = String(value);
    parts.push(`${key} ${text}`);
    if (parts.join(", ").length > 90) break;
  }
  return parts.join(", ");
}

export default function EventsPage() {
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [severity] = useUrlParam("severity", "");
  const [range] = useUrlParam("range_range", "24h");
  const [customFrom] = useUrlParam("range_from", "");

  useEffect(() => {
    let cancelled = false;
    function load() {
      getEvents({
        since: resolveSince(range, customFrom) || undefined,
        severity: severity || undefined,
        limit: 500,
      })
        .then((evts) => {
          if (!cancelled) setEvents(evts);
        })
        .catch(() => undefined);
    }
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [severity, range, customFrom]);

  return (
    <div>
      <div className="toolbar">
        <Segmented
          paramKey="severity"
          defaultValue=""
          options={[
            { value: "", label: "all" },
            { value: "info", label: "info" },
            { value: "warn", label: "warn" },
            { value: "error", label: "error" },
          ]}
        />
        <TimeRangePicker paramPrefix="range" defaultRange="24h" />
      </div>
      {events.length === 0 ? (
        <p className="note">No events in this window.</p>
      ) : (
        <table className="events">
          <thead>
            <tr>
              <th>time</th>
              <th>kind</th>
              <th>subject</th>
              <th>detail</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <tr key={event.id}>
                <td>{formatUtcStamp(event.ts)}</td>
                <td className={`kind-${event.severity}`}>{event.kind}</td>
                <td>{event.subject}</td>
                <td>{oneLineDetail(event.detail)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
