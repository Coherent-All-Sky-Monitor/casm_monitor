import Segmented from "./Segmented";
import { useUrlParam } from "../lib/useUrlParam";
import { isValidDateInput } from "../lib/timeRange";

const PRESETS = [
  { value: "1h", label: "1h" },
  { value: "6h", label: "6h" },
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "custom", label: "custom" },
];

export interface TimeRangePickerProps {
  /** Prefix for the URL params this control owns: "<prefix>_range",
   * "<prefix>_from", "<prefix>_to". */
  paramPrefix?: string;
  defaultRange?: string;
}

/** Four words and a custom escape hatch: 1h 6h 24h 7d custom. */
export default function TimeRangePicker({
  paramPrefix = "range",
  defaultRange = "24h",
}: TimeRangePickerProps) {
  const rangeKey = `${paramPrefix}_range`;
  const fromKey = `${paramPrefix}_from`;
  const toKey = `${paramPrefix}_to`;

  const [range] = useUrlParam(rangeKey, defaultRange);
  const [from, setFrom] = useUrlParam(fromKey, "");
  const [to, setTo] = useUrlParam(toKey, "");

  const invalid =
    range === "custom" &&
    ((from !== "" && !isValidDateInput(from)) || (to !== "" && !isValidDateInput(to)));

  return (
    <div className="range-picker">
      <Segmented paramKey={rangeKey} options={PRESETS} defaultValue={defaultRange} />
      {range === "custom" && (
        <div className="custom-range">
          <input
            type="datetime-local"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
            aria-label="from (local time)"
          />
          <span>to</span>
          <input
            type="datetime-local"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            aria-label="to (local time)"
          />
          {invalid && <span style={{ color: "var(--alert)" }}>Enter a valid local date and time.</span>}
        </div>
      )}
    </div>
  );
}
