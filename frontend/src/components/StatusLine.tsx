import { useState } from "react";
import { useStatus } from "../lib/useStatus";
import { buildStatusSentences } from "../lib/statusSentence";
import type { StatusItem } from "../lib/types";

function formatAge(ageS: number | null): string {
  if (ageS === null) return "never";
  if (ageS < 60) return `${Math.round(ageS)} s`;
  if (ageS < 3600) return `${Math.round(ageS / 60)} min`;
  if (ageS < 86400) return `${(ageS / 3600).toFixed(1)} h`;
  return `${Math.round(ageS / 86400)} d`;
}

function formatValue(item: StatusItem): string {
  if (item.value === null || item.value === undefined) return "no data";
  return item.unit ? `${item.value} ${item.unit}` : String(item.value);
}

/**
 * The status sentence: one or two plain sentences derived from /api/status,
 * with anything not "ok" as its own short sentence in alert colour. Clicking
 * anywhere on it toggles the full item table (label, value, age), collapsed
 * by default.
 */
export default function StatusLine() {
  const { status, backendStale } = useStatus();
  const [open, setOpen] = useState(false);
  const { main, problems } = buildStatusSentences(status, backendStale);

  return (
    <div className="status">
      <p
        className="status__sentence"
        onClick={() => setOpen((o) => !o)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") setOpen((o) => !o);
        }}
      >
        {main}
      </p>
      {problems.map((text) => (
        <p key={text} className="status__problem" onClick={() => setOpen((o) => !o)}>
          {text}
        </p>
      ))}
      {open && status && (
        <table className="status__table">
          <tbody>
            {status.groups.flatMap((group) =>
              group.keys.map((key) => {
                const item = status.items[key];
                if (!item) return null;
                return (
                  <tr key={key} className={item.state}>
                    <td>{item.label}</td>
                    <td>{formatValue(item)}</td>
                    <td>{formatAge(item.age_s)}</td>
                  </tr>
                );
              }),
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
