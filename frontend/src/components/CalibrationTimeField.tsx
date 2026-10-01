import { useEffect, useState } from "react";
import { utcFromWall, wallTime } from "./timeZones";

/** Keep invalid edits visible and block submission; the saved value stays UTC. */
export default function CalibrationTimeField({id, label, value, zone, onChange, onError}: {
  id: string; label: string; value: string; zone: string;
  onChange: (utc: string) => void; onError: (id: string, invalid: boolean) => void;
}) {
  const [draft, setDraft] = useState(() => wallTime(value, zone));
  const [error, setError] = useState("");
  useEffect(() => {
    setDraft(wallTime(value, zone)); setError(""); onError(id, !value);
  }, [value, zone, id, onError]);
  const edit = (wall: string) => {
    setDraft(wall);
    try {
      const utc = utcFromWall(wall, zone, true) + ":00Z";
      setError(""); onError(id, false); onChange(utc);
    } catch (reason) {
      setError((reason as Error).message); onError(id, true);
    }
  };
  return <label htmlFor={id}>{label} · {zone === "UTC" ? "UTC" : "OVRO local"}
    <input id={id} aria-label={label} type="datetime-local" step={60} value={draft}
      aria-invalid={Boolean(error)} aria-describedby={error ? `${id}-error` : undefined}
      onChange={event => edit(event.target.value)}/>
    {error && <span id={`${id}-error`} className="cal-time-error" role="alert">{error}</span>}
  </label>;
}
