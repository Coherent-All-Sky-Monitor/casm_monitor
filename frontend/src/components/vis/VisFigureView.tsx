import { useEffect, useRef, useState } from "react";
import { getVisFigureManifest, visFigureUrl } from "../../lib/api";
import { figureSentence } from "../../lib/visText";
import type { VisFigureKind, VisFigureManifest, VisRef, VisSet } from "../../lib/types";

export type FigureQuantity = "amp" | "phase" | "real" | "imag" | "coh";
export type FigureView = "waterfalls" | "spectra" | "autos";

const ALL_QUANTITIES: FigureQuantity[] = ["amp", "phase", "real", "imag", "coh"];

function kindFor(view: FigureView, quantity: FigureQuantity): VisFigureKind {
  if (view === "autos") return "autos";
  return (view === "waterfalls" ? `matrix_${quantity}` : `spectra_${quantity}`) as VisFigureKind;
}

interface VisFigureViewProps {
  set: VisSet;
  reference: VisRef;
  view: FigureView;
  quantity: FigureQuantity;
}

/**
 * One server-rendered figure: a single `<img>` (the fast path per the
 * operator's 2026-09-08 direction), a muted status sentence and a
 * click-to-open link to the full-resolution PNG. The other four quantities
 * for this (set, ref, view) are prefetched in the background after the first
 * paint so toggling the quantity segmented control is instant.
 */
export default function VisFigureView({ set, reference, view, quantity }: VisFigureViewProps) {
  const [manifest, setManifest] = useState<VisFigureManifest | null>(null);
  const [loadError, setLoadError] = useState(false);
  const prefetched = useRef<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    setManifest(null);
    setLoadError(false);
    const refresh = () => getVisFigureManifest(set, reference)
      .then((m) => { if (!cancelled) { setManifest(m); setLoadError(false); } })
      .catch(() => !cancelled && setLoadError(true));
    refresh();
    const timer = window.setInterval(refresh, 60_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [set, reference]);

  // Prefetch the other quantities' @1x images for this (set, ref, view) once
  // the manifest (and so the current image) has loaded, so a later toggle of
  // `quantity` is a cache hit rather than a fresh request.
  useEffect(() => {
    if (!manifest || view === "autos") return;
    for (const q of ALL_QUANTITIES) {
      if (q === quantity) continue;
      const kind = kindFor(view, q);
      const key = `${set}/${reference}/${kind}`;
      if (prefetched.current.has(key)) continue;
      prefetched.current.add(key);
      const img = new Image();
      img.src = visFigureUrl(set, reference, kind, "1x", manifest.rendered_utc);
    }
  }, [manifest, set, reference, view, quantity]);

  const kind = kindFor(view, quantity);
  const src1x = visFigureUrl(set, reference, kind, "1x", manifest?.rendered_utc);
  const src2x = visFigureUrl(set, reference, kind, "2x", manifest?.rendered_utc);

  return (
    <div className="vis-figure">
      {loadError && (
        <p className="note">
          No figures rendered yet for {set}/{reference} (the figures collector may not have run).
        </p>
      )}
      {!loadError && (
        <>
          <img
            className="vis-figure__img"
            src={src1x}
            srcSet={`${src1x} 1x, ${src2x} 2x`}
            loading="eager"
            alt={`${set} ${reference} ${kind}`}
            onError={() => setLoadError(true)}
          />
          <p className="note">
            {figureSentence(manifest, reference)}{" "}
            <a href={src2x} target="_blank" rel="noreferrer">
              open full size
            </a>
          </p>
        </>
      )}
    </div>
  );
}
