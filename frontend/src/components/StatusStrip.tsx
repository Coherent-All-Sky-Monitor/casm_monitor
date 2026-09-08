import { useStatus } from "../lib/useStatus";
import type { ItemState, StatusItem } from "../lib/types";

function formatAge(ageS: number | null): string {
  if (ageS === null) return "—";
  if (ageS < 60) return `${Math.round(ageS)}s`;
  if (ageS < 3600) return `${Math.round(ageS / 60)}m`;
  if (ageS < 86400) return `${Math.round(ageS / 3600)}h`;
  return `${Math.round(ageS / 86400)}d`;
}

function formatValue(item: StatusItem): string {
  if (item.value === null || item.value === undefined) return "no data";
  if (typeof item.value === "number") {
    return item.unit ? `${item.value}${item.unit}` : `${item.value}`;
  }
  return `${item.value}${item.unit ? ` ${item.unit}` : ""}`;
}

function Chip({ itemKey, item }: { itemKey: string; item: StatusItem | undefined }) {
  if (!item) {
    return (
      <span className="chip state-stale" title={itemKey}>
        <span className="chip-label">{itemKey}</span>
        <span className="chip-value">no data</span>
      </span>
    );
  }
  const state: ItemState = item.state;
  const icon = state === "error" ? "⚠ " : "";
  const asOf = item.ts === null ? "never collected" : `as of ${item.ts}`;
  return (
    <span
      className={`chip state-${state}`}
      title={`${item.label} (${itemKey}) — ${asOf}`}
    >
      <span className="chip-label">{item.label}</span>
      <span className="chip-value">
        {icon}
        {formatValue(item)}
      </span>
      <span className="chip-age">{formatAge(item.age_s)}</span>
    </span>
  );
}

export default function StatusStrip() {
  const { status, backendStale } = useStatus();

  if (!status) {
    return (
      <div className="status-strip">
        <span className="empty-note">no data</span>
      </div>
    );
  }

  return (
    <>
      {backendStale && (
        <div className="stale-banner">
          backend stale — no status update for over 30 s
        </div>
      )}
      <div className="status-strip">
        {status.groups.map((group) => (
          <div className="status-group" key={group.name}>
            <span className="status-group-name">{group.name}</span>
            {group.keys.map((key) => (
              <Chip key={key} itemKey={key} item={status.items[key]} />
            ))}
          </div>
        ))}
      </div>
    </>
  );
}
