import { useEffect, useRef, useState } from "react";
import { getSnapFigureManifest, snapFigureUrl } from "../../lib/api";
import { snapFigureSentence } from "../../lib/snapText";
import type { SnapFigureInputSet, SnapFigureKind, SnapFigureManifest } from "../../lib/types";

export type SnapFigureViewKind = "correlator" | "board" | "waterfalls" | "trend";

const KIND_MAP: Record<SnapFigureViewKind, SnapFigureKind> = {
  correlator: "spectra_correlator",
  board: "spectra_board",
  waterfalls: "waterfall",
  trend: "trend",
};

const ALL_VIEWS: SnapFigureViewKind[] = ["correlator", "board", "waterfalls", "trend"];

interface SnapFigureViewProps {
  inputSet: SnapFigureInputSet;
  view: SnapFigureViewKind;
}

/**
 * One server-rendered SNAPs figure: a single `<img>` (the fast path, same
 * pattern as the Vis tab's `VisFigureView`), a muted status sentence and a
 * click-to-open link to the full-resolution PNG. The other three views for
 * this input set are prefetched at `@1x` after the first paint so toggling
 * the view control is instant.
 */
export default function SnapFigureView({ inputSet, view }: SnapFigureViewProps) {
  const [manifest, setManifest] = useState<SnapFigureManifest | null>(null);
  const [loadError, setLoadError] = useState(false);
  const prefetched = useRef<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    setManifest(null);
    setLoadError(false);
    const refresh = () => getSnapFigureManifest(inputSet)
      .then((m) => { if (!cancelled) { setManifest(m); setLoadError(false); } })
      .catch(() => !cancelled && setLoadError(true));
    refresh();
    const timer = window.setInterval(refresh, 60_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [inputSet]);

  useEffect(() => {
    if (!manifest) return;
    for (const v of ALL_VIEWS) {
      if (v === view) continue;
      const kind = KIND_MAP[v];
      const key = `${inputSet}/${kind}`;
      if (prefetched.current.has(key)) continue;
      prefetched.current.add(key);
      const img = new Image();
      img.src = snapFigureUrl(inputSet, kind, "1x", manifest.rendered_utc);
    }
  }, [manifest, inputSet, view]);

  const kind = KIND_MAP[view];
  const src1x = snapFigureUrl(inputSet, kind, "1x", manifest?.rendered_utc);
  const src2x = snapFigureUrl(inputSet, kind, "2x", manifest?.rendered_utc);

  return (
    <div className="vis-figure">
      {loadError && (
        <p className="note">
          No figures rendered yet for {inputSet} (the figures collector may not have run).
        </p>
      )}
      {!loadError && (
        <>
          <img
            className="vis-figure__img"
            src={src1x}
            srcSet={`${src1x} 1x, ${src2x} 2x`}
            loading="eager"
            alt={`${inputSet} ${kind}`}
            onError={() => setLoadError(true)}
          />
          <p className="note">
            {snapFigureSentence(manifest)}{" "}
            <a href={src2x} target="_blank" rel="noreferrer">
              open full size
            </a>
          </p>
        </>
      )}
    </div>
  );
}
