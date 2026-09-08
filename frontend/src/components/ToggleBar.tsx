import { useUrlParam } from "../lib/useUrlParam";

export interface ToggleOption {
  value: string;
  label: string;
}

interface ToggleBarProps {
  /** URL query key this control reads/writes, e.g. "quantity" or "units". */
  paramKey: string;
  options: ToggleOption[];
  defaultValue: string;
  onChange?: (value: string) => void;
}

/**
 * Generic segmented control whose value lives in the URL query string, so a
 * view (quantity/units toggles on Vis, linear/log on Search, etc.) can be
 * bookmarked and shared. Reused across tabs from M2 onward.
 */
export default function ToggleBar({
  paramKey,
  options,
  defaultValue,
  onChange,
}: ToggleBarProps) {
  const [value, setValue] = useUrlParam(paramKey, defaultValue);

  return (
    <div className="toggle-bar" role="group" aria-label={paramKey}>
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          className={opt.value === value ? "active" : ""}
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
