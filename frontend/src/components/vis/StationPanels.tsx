import type { CSSProperties, ReactNode } from "react";
import { Antenna, location } from "./ArrayPlots";

/** Pack occupied panels; keep the physical six-slot row visible as a small key. */
export function StationPanels({ inputs, selected, draw }: {
  inputs: Antenna[];
  selected: number[];
  draw: (antenna: Antenna) => ReactNode;
}) {
  const rows = [...new Set(inputs.map(a => location(a).row))].sort((a, b) => b - a);
  return <div className="compact-station-grid">
    {rows.map(row => {
      const wired = inputs.filter(a => location(a).row === row);
      const visible = wired.filter(a => selected.includes(a.packet_idx))
        .sort((a, b) => location(a).col - location(b).col);
      if (!visible.length) return null;
      const name = `N${String(row).padStart(2, "0")}`;
      const style = {
        "--station-columns": Math.min(3, visible.length),
        "--station-columns-medium": Math.min(2, visible.length),
      } as CSSProperties;
      return <section className="station-group" key={row} style={style} aria-label={`${name} plots`}>
        <header className="station-group-heading">
          <h4>{name}</h4>
          <div className="station-slot-key" aria-label={`${name} station positions, west to east`}>
            {[1, 2, 3, 4, 5, 6].map(col => {
              const antenna = wired.find(a => location(a).col === col);
              const state = !antenna ? "empty" : selected.includes(antenna.packet_idx) ? "selected" : "hidden";
              const description = state === "empty" ? "no wired antenna" : state === "hidden" ? "antenna hidden" : "antenna shown";
              return <span key={col} className={`station-slot ${state}`} title={`${name}E${col}: ${description}`} aria-label={`${name}E${col}: ${description}`}>
                E{col}<small>{state === "empty" ? "×" : state === "hidden" ? "off" : "●"}</small>
              </span>;
            })}
          </div>
        </header>
        <div className="station-panels">{visible.map(a => draw(a))}</div>
      </section>;
    })}
  </div>;
}
