import { NavLink } from "react-router-dom";

const TABS: { path: string; label: string }[] = [
  { path: "/snaps", label: "SNAPs" },
  { path: "/vis", label: "Vis" },
  { path: "/search", label: "Search" },
  { path: "/imaging", label: "Imaging" },
  { path: "/cal", label: "Cal" },
  { path: "/cands", label: "Cands" },
  { path: "/events", label: "Events" },
];

export default function TabNav() {
  return (
    <nav className="tab-nav">
      {TABS.map((tab) => (
        <NavLink
          key={tab.path}
          to={tab.path}
          className={({ isActive }) => (isActive ? "active" : "")}
        >
          {tab.label}
        </NavLink>
      ))}
    </nav>
  );
}
