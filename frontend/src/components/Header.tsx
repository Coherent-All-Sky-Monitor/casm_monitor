/** Telescope identity; all navigation lives in the single main tab row. */
export default function Header() {
  return (
    <header className="header">
      <div className="header__identity">
        <span className="header__acronym">CASM</span>
        <h1 className="header__title">Coherent All Sky Monitor</h1>
        <p className="header__location">Owens Valley Radio Observatory · Bishop, California</p>
      </div>
    </header>
  );
}
