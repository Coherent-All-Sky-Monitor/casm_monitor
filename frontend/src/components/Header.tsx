import { Link, useLocation } from "react-router-dom";

const TABS: { path: string; label: string }[] = [
  { path: "/observation", label: "Observation" },
  { path: "/readiness", label: "Readiness" },
  { path: "/antennas", label: "Antennas" },
];

/** One header line: the app name at left, the tabs as plain words at right,
 * the active one underlined. No clock — the UTC time is the last thing in
 * the status sentence below. */
export default function Header() {
  const { pathname } = useLocation();
  const group = /^\/(snaps|antennas)/.test(pathname) ? "/antennas" : /^\/(cal|events|readiness|review)/.test(pathname) ? "/readiness" : "/observation";
  return (
    <header className="header">
      <h1 className="header__title">CASM · OVRO</h1>
      <nav className="header__tabs">
        {TABS.map((tab) => (
          <Link key={tab.path} to={tab.path} className={group === tab.path ? "active" : ""} aria-current={group === tab.path ? "page" : undefined}>
            {tab.label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
