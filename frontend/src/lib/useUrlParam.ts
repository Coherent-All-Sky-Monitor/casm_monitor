// Reads/writes a single URL query parameter, backing the ToggleBar and
// TimeRangePicker components so any view is bookmarkable/shareable
// (docs/plan.md, Visibilities toggle bars).
import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

export function useUrlParam(
  key: string,
  fallback: string,
): [string, (value: string) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const value = searchParams.get(key) ?? fallback;

  const setValue = useCallback(
    (next: string) => {
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams(prev);
          params.set(key, next);
          return params;
        },
        { replace: true },
      );
    },
    [key, setSearchParams],
  );

  return [value, setValue];
}
