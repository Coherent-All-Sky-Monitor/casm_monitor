/** Telescope identity; all navigation lives in the single main tab row. */
import { useLocation } from 'react-router-dom';

export default function Header() {
  const {pathname}=useLocation();
  const plots=pathname==='/vis'||pathname==='/snaps';
  return (
    <header className={`header${plots?' header--plots':''}`}>
      <div className="header__identity">
        <span className="header__acronym">CASM</span>
        <h1 className="header__title">Coherent All Sky Monitor</h1>
        <p className="header__location">Owens Valley Radio Observatory · Big Pine, California</p>
      </div>
    </header>
  );
}
