import { Link } from "react-router-dom";
import { Evidence, Json, Notice, SaveInvestigation, useResource } from "../components/Workspace";

const stateLabel:Record<string,string> = {ok:"Within monitor limits",warn:"Check needed",stale:"Evidence stale / missing",error:"Check failed"};
const age = (s:number|null) => s==null?"No timestamp":s<120?`${Math.round(s)} s ago`:s<7200?`${Math.round(s/60)} min ago`:`${(s/3600).toFixed(1)} h ago`;
function display(item:Json) {
  if(item.value==null)return "Unavailable";
  if(item.unit==="B")return `${(Number(item.value)/1024**3).toFixed(2)} GiB`;
  if(item.unit==="s")return age(Number(item.value)).replace(" ago","");
  if(item.unit==="%")return `${Number(item.value).toFixed(1)}%${item.key.startsWith("nvme")?" used":""}`;
  if(item.key.endsWith("_ok"))return Number(item.value)===1?"Responding":"Not responding";
  return `${item.value}${item.unit?" "+item.unit:""}`;
}
function advice(item:Json) {
  if(item.value==null||item.state==="stale")return "Check the last successful collector read before interpreting this as a telescope fault.";
  if(item.unit==="%"&&item.key.startsWith("nvme"))return "Review storage growth and the retention/archive evidence. No cleanup is authorized here.";
  if(item.group==="vis")return "Inspect recent visibility coverage and acquisition logs.";
  if(item.group==="search")return "Compare T1 distributions with the recorded Hella cap warnings.";
  if(item.group==="weights")return "Compare recorded product identities and calibration evidence. Do not deploy from this warning.";
  return "Inspect the latest service and observation logs; this check does not identify a cause.";
}

export default function ReadinessPage() {
  const {data,error}=useResource("/api/status",30000);
  const items:Json[]=Object.entries(data?.items??{}).map(([key,item]):Json=>({...(item as Json),key}))
    .filter(i=>["node","services","obs","store","vis","search"].includes(i.group)||i.key==="weights_registry_mismatch");
  const relevant=items.filter(i=>!i.key.startsWith("kafka"));
  const attention=relevant.filter(i=>i.state!=="ok").sort((a,b)=>({error:0,stale:1,warn:2}[a.state as "error"]??3)-({error:0,stale:1,warn:2}[b.state as "error"]??3));
  const disks=relevant.filter(i=>i.unit==="%"&&i.key.startsWith("nvme"));
  const checks=relevant.filter(i=>["obs_daemons_state","obs_n_hella","vis_age_s","redis_ok","t2d_ok","zapdos_ok","obs_lmc_ok"].includes(i.key));
  const checked=data?.ts?new Intl.DateTimeFormat("en-US",{timeZone:"America/Los_Angeles",dateStyle:"medium",timeStyle:"long"}).format(new Date(data.ts)):"unknown";
  return <div className="workspace-page">
    <div className="page-heading"><p className="eyebrow">Array readiness</p><h2>What needs attention?</h2><p className="muted">Recorded checks as of {checked}. This is monitoring evidence, not permission to operate the telescope.</p></div>
    {error&&<Notice>Refresh failed; any displayed checks may be stale. {error}</Notice>}
    {!data&&!error&&<p role="status">Loading readiness checks…</p>}
    <div className="workflow-links"><Link to="/search">Search / RFI evidence</Link><Link to="/vis">Visibility coverage</Link><Link to="/cal/compare">Calibration comparison</Link><Link to="/review">Saved investigations</Link></div>
    {data&&<section className="readiness-attention"><h3>{attention.length? `${attention.length} ${attention.length===1?"check needs":"checks need"} review`:"No attention flags in the available checks"}</h3>
      <p className="muted">A stale measurement is an evidence gap, not proof of hardware failure. Scientific readiness still needs phase and recovery review.</p>
      {attention.map(item=><article className="attention-row" key={item.key}><div><h4>{item.label}</h4><span className="caution">{stateLabel[item.state]??item.state}</span> · {display(item)}<small>Evidence {age(item.age_s)}</small></div><p>{advice(item)}</p></article>)}
    </section>}
    <section><h3>Disk space</h3><p className="muted">Used capacity from the existing collector. Its warning threshold is above 90%.</p><div className="disk-list">{disks.map(item=><div className="disk-row" key={item.key}><div className="section-heading"><strong>{item.label.replace(" used","")}</strong><span className={item.state!=="ok"?"caution":""}>{display(item)}</span></div>{item.value!=null&&<meter min={0} max={100} low={0} high={90} optimum={0} value={Number(item.value)} aria-label={item.label}/>}<small className="muted">{stateLabel[item.state]} · evidence {age(item.age_s)}</small></div>)}</div></section>
    <section><h3>Observation and data flow</h3><div className="check-list">{checks.map(item=><div className="check-row" key={item.key}><span>{item.label==="obs UTC_START"?"Observation start":item.label}</span><strong>{display(item)}</strong><small className={item.state!=="ok"?"caution":"muted"}>{stateLabel[item.state]} · {age(item.age_s)}</small></div>)}</div></section>
    <details className="readiness-details"><summary>All measurements and recorded values</summary><table className="science-table"><thead><tr><th>Measurement</th><th>Value</th><th>Status / age</th></tr></thead><tbody>{relevant.map(item=><tr key={item.key}><td>{item.label}</td><td>{display(item)}</td><td>{stateLabel[item.state]} · {age(item.age_s)}</td></tr>)}</tbody></table><Evidence value={data}/></details>
    {data&&<SaveInvestigation selection={{kind:"infrastructure",checked_at:data.ts}} provenance={{status:data}}/>}
    <p className="muted"><a href="http://localhost:9000/medusa/bf_proc/stats.php" target="_blank" rel="noreferrer">Medusa</a> remains unchanged. No cleanup, restart or observation controls are exposed here.</p>
  </div>;
}
