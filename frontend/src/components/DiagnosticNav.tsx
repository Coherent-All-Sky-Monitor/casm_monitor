import { NavLink } from "react-router-dom";

export default function DiagnosticNav() {
  const links = [["/observation", "Overview"], ["/search", "Search (T1)"],
    ["/vis", "Visibilities"], ["/snaps", "SNAPs"], ["/sources", "Source history"],
    ["/cands", "Candidates"], ["/cal", "Calibration"]];
  return <nav className="diagnostic-nav" aria-label="Main navigation">{links.map(([to, label]) => <NavLink key={to} to={to}>
    <span>{label}</span>
  </NavLink>)}</nav>;
}
