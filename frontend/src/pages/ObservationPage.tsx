import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

interface Counts { recovered: number; missed_t1: number; missed_t2: number; fire_failed: number; pending: number; unknown: number; completed_fired: number; recovery_fraction: number | null }
interface Injection { file_id: string; inject_utc: string; beam: number; dm?: number; rec_snr?: number; inject_snr?: number; outcome: string | null; fail_reason?: string; replay?: { available: boolean; caption: string; artifacts: { png?: { available: boolean; url: string } } } }
interface Point { antenna: number; name: string; east_m: number; north_m: number; wired: boolean | null; intended: boolean | null; deployed: boolean | null; slot_identity_matches?: boolean; geometry_source?: string; deployed_east_m?: number; deployed_north_m?: number }
interface Overview {
  clock: { utc: string; local: string; lst: string };
  location: { name: string; latitude_deg: number; longitude_deg: number; elevation_m: number };
  observation: { id: string | null; state?: { value: string; age_s: number | null }; vis_age?: { value: number | null; age_s: number | null } };
  layout: { path: string; points: Point[]; counts: { wired: number; intended: number; deployed_union: number | null } };
  deployment: { product_id: string | null; path: string | null; evidence: string; inspection_state: string; beams: { beam: number; antennas: number[] }[]; membership_kind: string };
  solar: { image_url: string | null; observed_start: string | null; observed_end: string | null; rendered_at: string | null; caption: string; status: string };
  injections: { status: string; as_of_utc: string; window_start_utc: string; counts: Counts; trend: (Counts & { date_utc: string })[]; recent: Injection[]; latest_completed?: Injection | null };
}

function clockStamp(value: string | null | undefined, timeZone = "UTC") {
  const date = value ? new Date(value) : null;
  if (!date || !Number.isFinite(date.getTime())) return "Unknown";
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZoneName: "short",
  }).formatToParts(date);
  const field = (name: string) => parts.find(part => part.type === name)?.value ?? "";
  return `${field("year")}-${field("month")}-${field("day")} ${field("hour")}:${field("minute")}:${field("second")} ${field("timeZoneName")}`;
}
function stamp(value: string | null | undefined) { return clockStamp(value); }
function age(value: string | null | undefined) { const s = value ? (Date.now() - Date.parse(value)) / 1000 : NaN; return Number.isFinite(s) ? s < 120 ? `${Math.max(0, Math.round(s))} s ago` : s < 7200 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago` : "age unknown"; }

function Geometry({ data }: { data: Overview }) {
  const [beam, setBeam] = useState("union");
  const [coordinates, setCoordinates] = useState("current");
  const points = data.layout.points.map(p => coordinates === "product" ? {
    ...p, east_m: p.deployed_east_m ?? (p.geometry_source === "weights product" ? p.east_m : NaN),
    north_m: p.deployed_north_m ?? (p.geometry_source === "weights product" ? p.north_m : NaN),
  } : p).filter(p => Number.isFinite(p.east_m) && Number.isFinite(p.north_m));
  const selected = beam === "union" ? null : new Set(data.deployment.beams.find(b => String(b.beam) === beam)?.antennas ?? []);
  const minE = Math.min(...points.map(p => p.east_m), 0), maxE = Math.max(...points.map(p => p.east_m), 1);
  const minN = Math.min(...points.map(p => p.north_m), 0), maxN = Math.max(...points.map(p => p.north_m), 1);
  const span = Math.max(maxE - minE, maxN - minN, 1);
  const x = (e: number) => 50 + 280 * (e - (minE + maxE - span) / 2) / span;
  const y = (n: number) => 305 - 280 * (n - (minN + maxN - span) / 2) / span;
  return (
    <section>
      <h2>Array geometry</h2>
      <label className="note">Coordinates{" "}
        <select value={coordinates} onChange={e => setCoordinates(e.target.value)}>
          <option value="current">Current layout</option><option value="product">Weights product</option>
        </select>
      </label>{" "}
      <label className="note">Deployed membership{" "}
        <select value={beam} onChange={e => setBeam(e.target.value)}>
          <option value="union">Union across beams</option>
          {data.deployment.beams.map(b => <option key={b.beam} value={b.beam}>Beam {b.beam}</option>)}
        </select>
      </label>
      {points.length ? (
        <svg className="array-geometry" viewBox="0 0 370 350" role="img"
          aria-label="Antenna positions in metres east and north, with deployed membership highlighted">
          <line x1="50" x2="330" y1="305" y2="305" stroke="#9ca3af" />
          <line x1="50" x2="50" y1="25" y2="305" stroke="#9ca3af" />
          {[0, .5, 1].map(f => <g key={f}>
            <text x={50 + f * 280} y="323" textAnchor="middle">{((minE + maxE - span) / 2 + f * span).toFixed(0)}</text>
            <text x="43" y={309 - f * 280} textAnchor="end">{((minN + maxN - span) / 2 + f * span).toFixed(0)}</text>
          </g>)}
          <text x="190" y="343" textAnchor="middle">East (m)</text>
          <text transform="translate(12 165) rotate(-90)" textAnchor="middle">North (m)</text>
          {points.map(p => {
            const deployed = selected ? selected.has(p.antenna) && p.slot_identity_matches !== false : p.deployed;
            return <circle key={p.antenna} cx={x(p.east_m)} cy={y(p.north_m)}
              r={deployed ? 4 : 2.7}
              fill={deployed ? "#2563eb" : p.wired ? "#6b7280" : "#fff"}
              stroke={p.intended ? "#1f2937" : "#d1d5db"} strokeWidth={p.intended ? 1.5 : 1}>
              <title>{`Antenna ${p.antenna} · ${p.name}; wired ${p.wired}; intended ${p.intended}; deployed in selection ${deployed}`}</title>
            </circle>;
          })}
        </svg>
      ) : <p className="note">Layout positions unavailable.</p>}
      <p className="note">{coordinates === "current"
        ? "Current-layout positions; product-only antennas retain product-epoch positions."
        : "Weights-product positions only; antennas without recorded product coordinates are omitted."}
        {" "}Membership uses inspected weights; unresolved slot identities are not highlighted.</p>
      <p className="note">Blue: deployed in selection. Grey: wired. Dark outline: intended. Hollow: other layout positions.</p>
      <p>{data.layout.counts.wired} wired · {data.layout.counts.intended} intended · {data.layout.counts.deployed_union ?? "unknown"} in the deployed union</p>
      <p className="note">{data.deployment.product_id ?? "Deployment identity unavailable"} · {data.deployment.inspection_state}</p>
      <details>
        <summary>Membership evidence</summary>
        <p className="note">{data.deployment.evidence}</p>
        <p className="note">{data.deployment.path}</p>
        <p className="note">Layout: {data.layout.path}</p>
      </details>
    </section>
  );
}

export default function ObservationPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState(false);
  const [checked, setChecked] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const refresh = async () => {
      try {
        const response = await fetch("/api/observation", { signal: controller.signal });
        if (!response.ok) throw new Error(String(response.status));
        const value = await response.json() as Overview;
        if (!cancelled) {
          setData(value);
          setError(false);
          setChecked(new Date().toISOString());
        }
      } catch { if (!cancelled) setError(true); }
    };
    refresh();
    const timer = window.setInterval(refresh, 30_000);
    return () => { cancelled = true; controller.abort(); window.clearInterval(timer); };
  }, []);
  if (!data) return <p role="status">{error
    ? "Observation evidence is unavailable. Existing diagnostics remain accessible above."
    : "Loading observation evidence…"}</p>;
  const injections = data.injections;
  const latest = injections?.latest_completed ?? injections?.recent?.[0];
  const replayShot = injections?.recent?.find(shot => shot.replay?.artifacts.png?.available);
  const replay = replayShot?.replay?.artifacts.png;
  const vis = data.observation.vis_age;
  const visAge = vis?.value != null && vis.age_s != null ? Math.round(vis.value + vis.age_s) : null;
  return (
    <div className="observation-page">
      {error && <p className="status__problem" role="status">
        Latest refresh failed. Showing evidence last checked {stamp(checked)}.
      </p>}
      <div className="observatory-line">
        <span>Owens Valley Radio Observatory · {data.location.latitude_deg.toFixed(3)}°, {data.location.longitude_deg.toFixed(3)}°</span>
        <span>{clockStamp(data.clock.local, "America/Los_Angeles")} · {clockStamp(data.clock.utc)} · LST {data.clock.lst ?? "unavailable"}</span>
      </div>
      <div className="observation-summary">
        <h2>Current observation</h2>
        <p>{data.observation.id ?? "Observation identity unavailable"} · {data.observation.state?.value ?? "state unknown"}</p>
        {data.observation.state?.age_s != null && data.observation.state.age_s > 120 &&
          <p className="status__problem">Observation state was last checked {Math.round(data.observation.state.age_s / 60)} min ago.</p>}
        <p className="note">
          Newest cached visibility: {visAge == null ? "age unknown" : `${Math.round(visAge / 60)} min old`}.
          {" "}Evidence checked {stamp(checked)}.
        </p>
      </div>
      <div className="science-columns">
        <section>
          <h2>Solar dynamic spectrum</h2>
          {data.solar.image_url
            ? <a href={data.solar.image_url} target="_blank" rel="noreferrer">
                <img className="science-image" src={data.solar.image_url}
                  alt="Saved solar dynamic spectrum with frequency and observation time axes" />
              </a>
            : <p className="note">Solar product unavailable.</p>}
          <p>Saved solar-context spectrum. Solar variability alone does not diagnose an instrumental fault.</p>
          <p className="note">Saved interval: {stamp(data.solar.observed_start)}–{stamp(data.solar.observed_end)} ({age(data.solar.observed_end)}).</p>
          <details><summary>Processing and provenance</summary>
            <p>{data.solar.caption}</p>
            <p className="note">Rendered {stamp(data.solar.rendered_at)} · {data.solar.status}. This saved spectrum has fixed bounds.</p>
          </details>
          <p><Link to="/vis?view=interactive&mode=history&vis_hist_range_range=1h">Select visibility time and frequency</Link> · <Link to="/imaging">Imaging</Link></p>
        </section>
        <section>
          <h2>FRB injection and recovery</h2>
          {injections?.counts && injections.status !== "unavailable" ? <>
            <p className="recovery-headline">{injections.counts.recovered} / {injections.counts.completed_fired} recovered</p>
            <p className="note">Completed fired trials since {stamp(injections.window_start_utc)}.
              {" "}{injections.counts.missed_t1} T1 misses · {injections.counts.missed_t2} T2 misses ·
              {" "}{injections.counts.fire_failed} firing failures · {injections.counts.pending} pending · {injections.counts.unknown} unknown</p>
            {latest && <p>Latest completed trial: {(latest.outcome ?? "pending").replace(/_/g, " ")} · beam {latest.beam}
              {latest.dm != null && <> · DM {latest.dm.toFixed(1)} pc cm⁻³</>}
              {latest.rec_snr != null && <> · recovered S/N {latest.rec_snr.toFixed(1)}</>}
            </p>}
            {replay?.available && <a href={replay.url} target="_blank" rel="noreferrer">
              <img className="science-image replay-preview" src={replay.url}
                alt={`Saved synthetic replay of injection ${replayShot?.file_id}`} />
            </a>}
            {replay?.available && <p className="note">Saved replay: {replayShot?.file_id} · {stamp(replayShot?.inject_utc)} · beam {replayShot?.beam}.</p>}
            <p className="note">Synthetic replay visualizes the injected signal; live search recovery is established by recorded gates, not this plot.</p>
            {latest?.fail_reason && <details><summary>Latest trial evidence</summary><p>{latest.fail_reason}</p></details>}
            <p className="note">Checked {stamp(injections.as_of_utc)} · {injections.status}</p>
            <p><Link to="/cands?section=injections">Injection records</Link> · <Link to="/search">Search diagnostics</Link></p>
          </> : <p className="note">Injection evidence unavailable.</p>}
        </section>
      </div>
      <div className="science-columns science-context">
        <Geometry data={data} />
        <section>
          <h2>Recovery history</h2>
          <table className="science-table">
            <thead><tr><th>UTC day</th><th>Recovered / completed fired</th><th>T1 / T2 misses</th></tr></thead>
            <tbody>{injections?.trend?.slice(-7).map(day => <tr key={day.date_utc}>
              <td>{day.date_utc}</td><td>{day.recovered} / {day.completed_fired}</td>
              <td>{day.missed_t1} / {day.missed_t2}</td>
            </tr>)}</tbody>
          </table>
          <details className="trial-details">
            <summary>Recent trials and saved evidence</summary>
            <table className="science-table">
              <thead><tr><th>Injected UTC</th><th>Beam</th><th>Outcome</th><th>Plot</th></tr></thead>
              <tbody>{injections?.recent?.slice(0, 5).map(row => <tr key={row.file_id}>
                <td>{stamp(row.inject_utc)}</td><td>{row.beam}</td>
                <td>{(row.outcome ?? "pending").replace(/_/g, " ")}
                  {row.fail_reason && <details><summary>Reason</summary><p>{row.fail_reason}</p></details>}
                </td>
                <td>{row.replay?.artifacts.png?.available
                  ? <a href={row.replay.artifacts.png.url} target="_blank" rel="noreferrer">Synthetic replay</a>
                  : "Unavailable"}</td>
              </tr>)}</tbody>
            </table>
          </details>
        </section>
      </div>
    </div>
  );
}
