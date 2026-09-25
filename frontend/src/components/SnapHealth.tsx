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
    {error&&<p role="status">Status refresh failed. Current state is unknown.</p>}
    {!data&&!error&&<p>Reading saved status…</p>}
    <div className="snap-health-strip">{(data?.boards??[]).map((b:Json)=>{
      const pps=b.timing_status??b.pps_status;
      return <div className="snap-health-chip" key={b.ip}>
        <div><strong>SNAP {b.feng_id}</strong><span>{b.ip}</span></div>
        <div className="snap-health-badges">{[['Streaming',b.streaming],['PPS timing',pps]].map(([label,raw])=>{
          const s:Json=error?{state:'unknown',detail:'Status refresh failed'}:raw as Json;
          return <span key={label as string} className={`snap-health-badge ${s.state}`} title={`${s.detail??''} · ${stamp(s.ts)}`}>
            {label as string} · <b>{snapHealthLabel(s.state)}</b>{label==='PPS timing'&&s.delta_ticks!=null&&<small> · {s.delta_ticks} ticks</small>}
          </span>;
        })}</div>
        <small className="snap-health-time">PPS · {stamp(b.pps_status.ts)}</small>
      </div>;
    })}</div>
    <details className="snap-health-details"><summary>Board status details</summary>
    <div className="snap-health-grid">{(data?.boards??[]).map((b:Json)=><article className="snap-health-board" key={b.ip}>
      <h4>SNAP {b.feng_id} <span>SLOT {b.slot}</span></h4>
      <p className="snap-board-ip">{b.ip}</p>
      <StatusBox title="Streaming" status={error?{state:'unknown',detail:'Status refresh failed'}:b.streaming}/>
      <StatusBox title="PPS timing" status={error?{state:'unknown',detail:'Status refresh failed'}:b.timing_status??b.pps_status}/>
      <p className="snap-control-note">Control: {b.control_status==='ok'?'last read succeeded':b.control_status==='unavailable'?'read unavailable':b.control_status==='partial'?'partial read':'not verified'} · {stamp(b.latest_attempt)}</p>
    </article>)}</div>
    <p className="snap-health-caption">Streaming uses cached visibility data (≤ 15 min). PPS timing checks advancing timestamps against SNAP 0 and the accepted offsets. Changed offsets or stopped PPS need attention; missing/stale checks stay Unknown after 90 min. The reference is fixed, never automatically relearned. Spectra and PPS are checked hourly; page refresh only loads saved results.</p>
    </details>
  </section>;
}
