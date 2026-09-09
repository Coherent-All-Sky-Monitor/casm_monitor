import { NavLink } from "react-router-dom";

const TABS: { path: string; label: string }[] = [
  { path: "/snaps", label: "SNAPs" },
  { path: "/vis", label: "Visibilities" },
  { path: "/search", label: "Search" },
  { path: "/imaging", label: "Imaging" },
  { path: "/cal", label: "Calibration" },
  { path: "/cands", label: "Candidates" },
  { path: "/events", label: "Events" },
];

/** One header line: the app name at left, the tabs as plain words at right,
 * the active one underlined. No clock — the UTC time is the last thing in
 * the status sentence below. */
export default function Header() {
  return (
    <header className="header">
      <h1 className="header__title">CASM monitor</h1>
      <nav className="header__tabs">
        {TABS.map((tab) => (
          <NavLink key={tab.path} to={tab.path} className={({ isActive }) => (isActive ? "active" : "")}>
            {tab.label}
          </NavLink>
        ))}
      </nav>
    </header>
  );
}
