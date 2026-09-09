import type { ReactNode } from "react";

export interface PanelProps {
  title: string;
  children: ReactNode;
}

/** A plain panel: a muted 12 px title, no box, no shadow, no card — just the
 * title above whatever it holds (frontend/DESIGN.md). Used by the
 * Visibilities and Search tabs wherever the SNAPs-specific SpectrumPanel
 * shape does not apply. */
export default function Panel({ title, children }: PanelProps) {
  return (
    <div className="panel-plain">
      <div className="panel-plain__title">{title}</div>
      {children}
    </div>
  );
}
