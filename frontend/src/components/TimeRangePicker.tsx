import { useUrlParam } from "../lib/useUrlParam";
import ToggleBar from "./ToggleBar";

const PRESETS = [
  { value: "1h", label: "1h" },
  { value: "6h", label: "6h" },
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "custom", label: "custom" },
];

export interface TimeRangePickerProps {
  /** Prefix for the URL params this control owns: "<prefix>_range",
   * "<prefix>_from", "<prefix>_to". Defaults to "range". */
  paramPrefix?: string;
  defaultRange?: string;
}

/**
 * Stub time-range control for tabs that will get a real history slider in
 * M1/M2: last 1h/6h/24h/7d, or custom start/end. State lives in the URL.
 */
export default function TimeRangePicker({
  paramPrefix = "range",
  defaultRange = "1h",
}: TimeRangePickerProps) {
  const rangeKey = `${paramPrefix}_range`;
  const fromKey = `${paramPrefix}_from`;
  const toKey = `${paramPrefix}_to`;

  const [range] = useUrlParam(rangeKey, defaultRange);
  const [from, setFrom] = useUrlParam(fromKey, "");
  const [to, setTo] = useUrlParam(toKey, "");

  return (
    <div className="time-range-picker">
      <ToggleBar paramKey={rangeKey} options={PRESETS} defaultValue={defaultRange} />
      {range === "custom" && (
        <>
          <input
            type="datetime-local"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
            aria-label="from"
          />
          <span>–</span>
          <input
            type="datetime-local"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            aria-label="to"
          />
        </>
      )}
    </div>
  );
}
