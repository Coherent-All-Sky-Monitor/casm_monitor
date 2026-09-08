import { useEffect, useState } from "react";
import Page from "../components/Page";
import TimeRangePicker from "../components/TimeRangePicker";
import { getEvents } from "../lib/api";
import { resolveSince } from "../lib/timeRange";
import { useUrlParam } from "../lib/useUrlParam";
import type { EventRecord } from "../lib/types";

const REFRESH_MS = 30_000;
const KIND_DEBOUNCE_MS = 300;

function SeverityBadge({ severity }: { severity: string }) {
  return <span className={`severity-${severity}`}>{severity}</span>;
}

function EventRow({ event }: { event: EventRecord }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr>
        <td className="ts">{event.ts}</td>
        <td className="severity">
          <SeverityBadge severity={event.severity} />
        </td>
        <td>{event.kind}</td>
        <td>{event.subject}</td>
        <td>
          <button className="event-detail-toggle" onClick={() => setOpen((o) => !o)}>
            {open ? "hide" : "detail"}
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={5}>
            <pre className="event-detail">{JSON.stringify(event.detail, null, 2)}</pre>
          </td>
        </tr>
      )}
    </>
  );
}

export default function EventsPage() {
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [kind, setKind] = useUrlParam("kind", "");
  const [severity, setSeverity] = useUrlParam("severity", "");
  const [range] = useUrlParam("range_range", "24h");
  const [customFrom] = useUrlParam("range_from", "");

  // The kind filter is a prefix/LIKE match against a table the user may be
  // typing into character by character; debounce so we don't fire a request
  // per keystroke.
  const [debouncedKind, setDebouncedKind] = useState(kind);
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedKind(kind), KIND_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [kind]);

  useEffect(() => {
    let cancelled = false;
    function load() {
      const since = resolveSince(range, customFrom);
      getEvents({
        since: since || undefined,
        kind: debouncedKind || undefined,
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
  }, [debouncedKind, severity, range, customFrom]);

  return (
    <Page title="Events">
      <p>
        State-change timeline: input died/recovered, ADC railed, EQ/gain
        changed, subband went dark, obs restarted, weights uploaded, cal job
        run — replaces re-excavating incidents.md by hand.
      </p>
      <div className="events-toolbar">
        <input
          type="text"
          placeholder="filter kind (prefix match)…"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        />
        <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
          <option value="">all severities</option>
          <option value="info">info</option>
          <option value="warn">warn</option>
          <option value="error">error</option>
        </select>
        <TimeRangePicker paramPrefix="range" defaultRange="24h" />
      </div>
      {events.length === 0 ? (
        <p className="empty-note">no events</p>
      ) : (
        <table className="events-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Severity</th>
              <th>Kind</th>
              <th>Subject</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <EventRow key={event.id} event={event} />
            ))}
          </tbody>
        </table>
      )}
    </Page>
  );
}
