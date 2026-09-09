// The panel grid's column count, mirroring the breakpoints in styles.css
// (6 at >= 1600 px, 4 at >= 1100, 3 below). The grid itself is laid out by
// CSS; JS needs the same number only to decide which panels are on the left
// column and the bottom row, since those are the only ones with axis ticks.
import { useEffect, useState } from "react";

export function columnsForWidth(width: number): number {
  if (width >= 1600) return 6;
  if (width >= 1100) return 4;
  return 3;
}

export function useColumns(): number {
  const [cols, setCols] = useState(() => columnsForWidth(window.innerWidth));
  useEffect(() => {
    const onResize = () => setCols(columnsForWidth(window.innerWidth));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return cols;
}

/** True for the panels that carry x tick labels: the last row of the grid. */
export function isBottomRow(index: number, total: number, cols: number): boolean {
  return index >= total - ((total % cols) || cols);
}

/** True for the panels that carry y tick labels: the left column. */
export function isLeftColumn(index: number, cols: number): boolean {
  return index % cols === 0;
}
