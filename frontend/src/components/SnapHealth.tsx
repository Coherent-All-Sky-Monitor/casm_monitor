import { Json, stamp } from './Workspace';
import '../snap-health.css';

export const snapHealthLabel=(state:string)=>state==='ok'?'OK':state==='attention'?'Needs attention':'Unknown';

function StatusBox({title,status}:{title:string;status:Json}) {
  return <div className={`snap-health-box ${status.state}`}>
    <span>{title}</span><strong>{snapHealthLabel(status.state)}</strong>
    <p>{status.detail}</p><small>{stamp(status.ts)}</small>
  </div>;
}

export function SnapHealth({data,error}:{data:Json|null;error:string}) {
  return <section className="snap-health-section" aria-label="SNAP live status">
    <div className="snap-health-heading"><h3>Live status</h3><span>Saved evidence · checked every 30 s</span></div>
    {error&&<p role="status">Status refresh failed. Current state is unknown.</p>}
    {!data&&!error&&<p>Reading saved status…</p>}
    <div className="snap-health-grid">{(data?.boards??[]).map((b:Json)=><article className="snap-health-board" key={b.ip}>
      <h4>SNAP {b.feng_id} <span>SLOT {b.slot}</span></h4>
      <StatusBox title="Streaming" status={error?{state:'unknown',detail:'Status refresh failed'}:b.streaming}/>
      <StatusBox title="PPS alignment" status={error?{state:'unknown',detail:'Status refresh failed'}:b.pps_status}/>
      <p className="snap-control-note">Control: {b.control_status==='ok'?'last read succeeded':b.control_status==='unavailable'?'read unavailable':b.control_status==='partial'?'partial read':'not verified'} · {stamp(b.latest_attempt)}</p>
    </article>)}</div>
    <p className="snap-health-caption">Streaming uses recent cached visibility data (≤ 15 min), not a live packet counter. PPS compares advancing telescope time with SNAP 0 over two stable pulse windows; unreadable boards stay Unknown. Each check has its own timestamp. Timing checks expire after 90 min; page refresh does not contact hardware.</p>
  </section>;
}
