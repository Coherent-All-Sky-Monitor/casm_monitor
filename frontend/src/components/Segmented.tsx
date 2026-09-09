import { useUrlParam } from "../lib/useUrlParam";

export interface SegmentedOption {
  value: string;
  label: string;
}

interface SegmentedProps {
  /** URL query key this control reads/writes, so a view is bookmarkable. */
  paramKey: string;
  options: SegmentedOption[];
  defaultValue: string;
  onChange?: (value: string) => void;
}

/**
 * A choice rendered as plain words; the selected one carries a hairline
 * underline. The value lives in the URL query string (docs/plan.md: every
 * view is bookmarkable and shareable).
 */
export default function Segmented({ paramKey, options, defaultValue, onChange }: SegmentedProps) {
  const [value, setValue] = useUrlParam(paramKey, defaultValue);
  return (
    <div className="segmented" role="group" aria-label={paramKey}>
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          className={opt.value === value ? "selected" : ""}
          onClick={() => {
            setValue(opt.value);
            onChange?.(opt.value);
          }}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
