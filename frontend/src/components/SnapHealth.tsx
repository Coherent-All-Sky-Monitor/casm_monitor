import { Json, stamp } from './Workspace';
import '../snap-health.css';

export const snapHealthLabel=(state:string)=>state==='ok'?'OK':state==='attention'?'Needs attention':'Unknown';

function StatusBox({title,status}:{title:string;status:Json}) {
  return <div className={`snap-health-box ${status.state}`}>
    <span>{title}</span><strong>{status.label??snapHealthLabel(status.state)}</strong>
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
      <p className="snap-board-ip">{b.ip}</p>
      <StatusBox title="Streaming" status={error?{state:'unknown',detail:'Status refresh failed'}:b.streaming}/>
      <StatusBox title="PPS / source check" status={error?{state:'unknown',detail:'Status refresh failed'}:b.timing_source_status??b.pps_status}/>
      <p className="snap-control-note">Control: {b.control_status==='ok'?'last read succeeded':b.control_status==='unavailable'?'read unavailable':b.control_status==='partial'?'partial read':'not verified'} · {stamp(b.latest_attempt)}</p>
    </article>)}</div>
    <details className="snap-health-caption"><summary>Status details</summary><p>Streaming uses cached visibility data (≤ 15 min). PPS compares advancing timestamps against SNAP 0. “Source coherence seen” is a separate dated, reviewed cross-SNAP source check with the offset retained, not exact sample alignment or a pass for every input. It requires unchanged offset and weights/calibration identities. Timing and source checks expire after 90 min; missing evidence stays unknown. Page refresh does not contact hardware.</p></details>
  </section>;
}
