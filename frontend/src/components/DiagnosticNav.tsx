import { NavLink, useLocation } from "react-router-dom";

export default function DiagnosticNav() {
  const { pathname } = useLocation();
  // SNAPs has its own Latest / History controls, not a second tab bar.
  if (/^\/(snaps|antennas)(\/|$)/.test(pathname)) return null;
  const links = /^\/(cal|events|readiness|review)/.test(pathname)
      ? [["/readiness", "Infrastructure"], ["/review", "Investigation queue"], ["/cal/compare", "Calibration comparison"], ["/cal/transit", "Cyg A transit"], ["/cal", "Build and review"], ["/events", "Events"]]
      : [["/observation", "Overview"], ["/search", "Search (T1)"], ["/vis", "Visibilities"], ["/snaps", "SNAPs"], ["/sources", "Source history"], ["/cands", "Candidates"]];
  return <nav className="diagnostic-nav" aria-label="Diagnostics">{links.map(([to, label]) => <NavLink key={to} to={to}>{label}</NavLink>)}</nav>;
}
