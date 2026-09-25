import { NavLink } from "react-router-dom";

const icons:Record<string,string> = {
  '/observation':'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  '/search':'M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0 M15 15l6 6',
  '/vis':'M3 18V6 M3 18h18 M5 12c2-10 4-10 6 0s4 10 6 0 3-8 4-6',
  '/snaps':'M6 6h12v12H6z M10 10h4v4h-4z M9 3v3 M15 3v3 M9 18v3 M15 18v3 M3 9h3 M3 15h3 M18 9h3 M18 15h3',
  '/sources':'M4 6a9 9 0 1 1-1 10 M3 3v5h5 M12 7v5l3 2',
  '/cands':'M3 4h7v6H3z M14 4h7v6h-7z M3 14h7v6H3z M14 14h7v6h-7z',
  '/cal':'M3 6h9 M16 6h5 M3 18h5 M12 18h9 M14 3v6 M10 15v6',
};

export default function DiagnosticNav() {
  const links = [["/observation", "Overview"], ["/search", "Search (T1)"],
    ["/vis", "Visibilities"], ["/snaps", "SNAPs"], ["/sources", "Source history"],
    ["/cands", "Candidates"], ["/cal", "Calibration"]];
  return <nav className="diagnostic-nav" aria-label="Main navigation">{links.map(([to, label]) => <NavLink key={to} to={to}>
    <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d={icons[to]}/></svg>
    <span>{label}</span>
  </NavLink>)}</nav>;
}
