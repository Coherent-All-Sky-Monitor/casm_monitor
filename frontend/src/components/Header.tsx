import { Link, useLocation } from "react-router-dom";

const TABS: { path: string; label: string }[] = [
  { path: "/observation", label: "Observation" },
  { path: "/readiness", label: "Readiness" },
  { path: "/snaps", label: "SNAPs" },
];

/** Telescope name and observatory location at left; primary navigation at right. */
export default function Header() {
  const { pathname } = useLocation();
  const group = /^\/(snaps|antennas)/.test(pathname) ? "/snaps" : /^\/(cal|events|readiness|review)/.test(pathname) ? "/readiness" : "/observation";
  return (
    <header className="header">
      <div className="header__identity">
        <h1 className="header__title">Coherent All Sky Monitor (CASM)</h1>
        <p className="header__location">Owens Valley Radio Observatory · Bishop, California</p>
      </div>
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
