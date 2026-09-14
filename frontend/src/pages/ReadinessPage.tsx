import { Link } from "react-router-dom";
import StatusLine from "../components/StatusLine";

export default function ReadinessPage() {
  return <section><h2>Array readiness</h2><p className="note">Recorded service and product state. Stale or missing evidence needs investigation; it does not establish a hardware fault.</p><StatusLine /><p><Link to="/events">Inspect changes and events</Link> · <Link to="/cal">Inspect calibration products</Link></p><p className="note">Calibration transfer and independent stationary-source transit comparisons are planned commissioning diagnostics. Operational actions require explicit human authority.</p></section>;
}
