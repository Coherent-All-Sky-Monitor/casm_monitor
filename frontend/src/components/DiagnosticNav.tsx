import { NavLink, useLocation } from "react-router-dom";

export default function DiagnosticNav() {
  const { pathname } = useLocation();
  const links = /^\/(snaps|vis|antennas)/.test(pathname)
    ? [["/antennas", "Overview"], ["/snaps", "SNAP spectra"], ["/vis", "Visibilities"]]
    : /^\/(cal|events|readiness)/.test(pathname)
      ? [["/readiness", "Overview"], ["/cal", "Calibration products"], ["/events", "History and events"]]
      : [["/observation", "Science overview"], ["/search", "Search diagnostics"], ["/cands", "Candidate review"], ["/imaging", "Imaging"]];
  return <nav className="diagnostic-nav" aria-label="Diagnostics">{links.map(([to, label]) => <NavLink key={to} to={to}>{label}</NavLink>)}</nav>;
}
