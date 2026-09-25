import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { FigureZoom } from "./FigureZoom";
import { api, Json, Notice } from "./Workspace";
import { timeTicks } from "./vis/PlotTicks";

export const OUTCOMES = [
  ["recovered","Recovered","#267044"], ["missed_t1","T1 miss","#b42e38"],
  ["missed_t2","T2 miss","#8e2536"], ["fire_failed","Fire failed","#906013"],
  ["pending","Pending","#76539c"], ["unknown","Unknown","#667085"],
] as const;
export function outcome(value:string|null|undefined) {return OUTCOMES.find(([key])=>key===(value||"pending"))??OUTCOMES[5];}
export function localStamp(value:string|number,zone:string,dated=true) {
  const date=new Date(typeof value==='number'?value*1000:value);
  return Number.isFinite(date.getTime())?new Intl.DateTimeFormat('en-GB',{timeZone:zone,...(dated?{month:'short',day:'2-digit'} as const:{}),hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(date):'Unknown';
}

export function OverviewImage({src,title}:{src:string;title:string}) {
  const [zoom,setZoom]=useState<{src:string;title:string}|null>(null);
  return <><button className="overview-image" aria-label={`Enlarge ${title}`} onClick={()=>setZoom({src,title})}><img src={src} alt={title}/></button>
    {zoom&&<FigureZoom title={zoom.title} onClose={()=>setZoom(null)} actions={<a href={zoom.src} target="_blank" rel="noreferrer">Original PNG</a>}><img src={zoom.src} alt={zoom.title}/></FigureZoom>}</>;
}

function Timeline({data,zone,onTrial}:{data:Json;zone:string;onTrial:(shot:Json)=>void}) {
  const t0=Date.parse(data.window_start_utc)/1000,t1=Date.parse(data.window_end_utc)/1000;
  const x=(t:number)=>108+(t-t0)/(t1-t0)*660;
  const ticks=timeTicks(t0,t1,520,zone);
  return <svg className="injection-timeline" viewBox="0 0 800 265" role="img" aria-label="Injection outcomes over the selected interval">
    {ticks.map(t=><g key={t}><line className="overview-guide" x1={x(t)} x2={x(t)} y1="18" y2="210"/><text x={x(t)} y="230" textAnchor="middle">{localStamp(t,zone,false)}</text></g>)}
    {OUTCOMES.map(([key,label],i)=><g key={key}><text x="97" y={34+i*33} textAnchor="end">{label}</text><line className="overview-guide" x1="108" x2="768" y1={30+i*33} y2={30+i*33}/></g>)}
    <line className="overview-axis" x1="108" x2="768" y1="211" y2="211"/>
    {(data.trials??[]).map((shot:Json)=>{const [key,label,colour]=outcome(shot.outcome),row=OUTCOMES.findIndex(o=>o[0]===key);return <g key={shot.file_id} role="button" tabIndex={0}
      aria-label={`${shot.file_id}, ${label}, ${localStamp(shot.inject_utc,zone)}`} onClick={()=>onTrial(shot)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();onTrial(shot);}}}>
      <title>{`${shot.file_id} · ${label} · ${localStamp(shot.inject_utc,zone)} · beam ${shot.beam} · DM ${shot.dm??'unknown'}`}</title>
      <circle cx={x(Date.parse(shot.inject_utc)/1000)} cy={30+row*33} r="9" fill="transparent"/>
      <circle cx={x(Date.parse(shot.inject_utc)/1000)} cy={30+row*33} r="5" fill={colour} stroke="white" strokeWidth="1"/>
    </g>;})}
    <text x="438" y="256" textAnchor="middle">Time ({zone==='UTC'?'UTC':'OVRO local'})</text>
  </svg>;
}

export function InjectionPanel({data,zone}:{data:Json;zone:string}) {
  const [shot,setShot]=useState<Json|null>(null),[zoom,setZoom]=useState<{data:Json;zone:string}|null>(null);
  const showTrial=(s:Json)=>setShot({...s,...(data.recent??[]).find((r:Json)=>r.file_id===s.file_id)});
  const c=data.counts??{},complete=data.status==='ok'&&data.counts_complete;
  const fraction=complete&&c.completed_fired>0?`${Math.round(100*c.recovered/c.completed_fired)}%`:null;
  return <>
    <div className="overview-panel-heading"><h3>Injection recovery</h3><button onClick={()=>setZoom({data,zone})}>Enlarge timeline</button></div>
    <div className="recovery-summary"><strong>{data.status==='unavailable'?'Unavailable':`${c.recovered??'—'} / ${c.completed_fired??'—'}`}</strong><span>recovered / completed fired{fraction&&<b>{fraction}</b>}</span></div>
    {data.status!=='ok'&&<Notice>{data.reason??'Injection counts are incomplete.'}</Notice>}
    <div className="recovery-counts">{OUTCOMES.slice(1).map(([key,label,colour])=><span key={key}><i style={{background:colour}}/>{label} <b>{data.status==='unavailable'?'—':c[key]??0}</b></span>)}</div>
    <Timeline data={data} zone={zone} onTrial={showTrial}/>
    {data.status==='ok'&&!data.trials?.length&&<p className="overview-caption">No trials recorded in this interval. This is not a recovery measurement.</p>}
    <p className="overview-caption">{localStamp(data.window_start_utc,zone)} – {localStamp(data.window_end_utc,zone)} · click a trial for details</p>
    <details className="overview-history"><summary>View trial history and seven-day totals</summary>
      <div className="table-scroll"><table className="science-table"><thead><tr><th>Trial · {zone==='UTC'?'UTC':'OVRO local'}</th><th>Outcome</th><th>Beam / DM (pc cm⁻³)</th></tr></thead><tbody>{(data.trials??[]).map((s:Json)=><tr key={s.file_id}><td><button onClick={()=>showTrial(s)}>{s.file_id}</button><small>{localStamp(s.inject_utc,zone)}</small></td><td>{outcome(s.outcome)[1]}</td><td>{s.beam} / {s.dm?.toFixed(1)??'—'}</td></tr>)}</tbody></table></div>
      <h4>Seven-day totals · UTC days ending on the selected date</h4><div className="table-scroll"><table className="science-table"><thead><tr><th>UTC date</th><th>Recovered / fired</th><th>T1 / T2 misses</th><th>Fire failed / pending</th></tr></thead><tbody>{(data.trend??[]).map((d:Json)=><tr key={d.date_utc}><td>{d.date_utc}</td><td>{d.recovered} / {d.completed_fired}</td><td>{d.missed_t1} / {d.missed_t2}</td><td>{d.fire_failed} / {d.pending}</td></tr>)}</tbody></table></div>
      <p className="overview-caption">Pending, unknown and firing failures are separate from completed fired trials. A saved synthetic replay does not establish live recovery.</p>
      <Link to="/review">Open investigation queue →</Link>
    </details>
    {zoom&&<FigureZoom title="Injection recovery timeline" onClose={()=>setZoom(null)}><Timeline data={zoom.data} zone={zoom.zone} onTrial={s=>{setZoom(null);setShot({...s,...(zoom.data.recent??[]).find((r:Json)=>r.file_id===s.file_id)});}}/></FigureZoom>}
    {shot&&<FigureZoom title={`Injection ${shot.file_id}`} onClose={()=>setShot(null)} actions={<Link to="/review">Investigation queue →</Link>}><div className="injection-detail"><h3>{outcome(shot.outcome)[1]}</h3><p>{localStamp(shot.inject_utc,zone)} · {zone}</p><dl><dt>Beam</dt><dd>{shot.beam??'Unknown'}</dd><dt>DM (pc cm⁻³)</dt><dd>{shot.dm??'Unknown'}</dd><dt>Injected / recovered S/N</dt><dd>{shot.inject_snr??'Unknown'} / {shot.rec_snr??'Unknown'}</dd><dt>Matched cluster</dt><dd>{shot.matched_cluster??'Unknown'}</dd><dt>T1 trials</dt><dd>{shot.n_t1_trials??'Unknown'}</dd></dl><p>{shot.fail_reason||'No failure reason recorded.'}</p>{shot.n_t1_trials==null&&<p>Missing trial evidence is not zero trials.</p>}{shot.replay?.artifacts?.png?.available&&<p><a href={shot.replay.artifacts.png.url} target="_blank" rel="noreferrer">Saved synthetic replay PNG →</a><br/>Synthetic replay, not a recording of live recovery.</p>}</div></FigureZoom>}
  </>;
}

export function VisibilityPreview({catalog,t0,t1,zone}:{catalog:Json;t0:string;t1:string;zone:string}) {
  const [quantity,setQuantity]=useState('amplitude_waterfall'),[product,setProduct]=useState<Json|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(true);
  useEffect(()=>{
    let active=true;setBusy(true);setError('');setProduct(null);
    const load=async()=>{
      try {
        const epoch=[...(catalog.layouts??[])].reverse().find((l:Json)=>Date.parse(l.date+'T00:00:00Z')<=Date.parse(t0));
        const dated=epoch?await api('/api/science/catalog?layout_id='+encodeURIComponent(epoch.id)):catalog;
        const selection=new Set(dated.inspection_inputs??[]);
        const pair=dated.baselines?.find((b:Json)=>selection.has(b.i)&&selection.has(b.j));
        if(!pair)throw new Error('No reference pair available in this layout.');
        if(!active)return;
        const result=await api('/api/science/render',{pairs:[[pair.i,pair.j]],t0,t1,fmin:390.625,fmax:484.375,kind:quantity,reference:'raw',resolution:'avg8',time_tz:zone,...(epoch?{layout_id:epoch.id}:{})});
        const stations=[pair.i,pair.j].map(p=>dated.inputs.find((a:Json)=>a.packet_idx===p)?.station??`input ${p}`);
        if(active)setProduct({...result,pairLabel:`${stations.join(' × ')} · ${pair.length_m.toFixed(2)} m ${pair.orientation}`,pair:[pair.i,pair.j]});
      } catch(e) {if(active)setError((e as Error).message);}
      finally {if(active)setBusy(false);}
    };
    const timer=globalThis.setTimeout(load,350);return()=>{active=false;globalThis.clearTimeout(timer);};
  },[catalog,t0,t1,zone,quantity]);
  const query=new URLSearchParams({view:'detail',t0,t1,time_tz:zone,reference:'raw',kind:quantity,...(product?.pair?{pairs:product.pair.join(',')}:{})}).toString();
  return <><div className="overview-panel-heading"><h3>Visibility snapshot</h3><div className="overview-quantity" role="group" aria-label="Visibility quantity">{[['amplitude_waterfall','Amplitude'],['phase_waterfall','Phase']].map(([q,l])=><button key={q} className={quantity===q?'active':''} aria-pressed={quantity===q} onClick={()=>setQuantity(q)}>{l}</button>)}</div></div>
    {busy&&<p className="overview-loading" role="status">Loading the reference baseline…</p>}{error&&<Notice>{error}</Notice>}
    {product&&<><p className="overview-caption">{product.pairLabel} · Raw · 8-channel complex averages</p>{(product.images??[]).map((im:Json)=><OverviewImage key={im.url} src={im.url} title={quantity==='phase_waterfall'?'Reference baseline phase':'Reference baseline dynamic spectrum'}/>)}<details className="overview-evidence"><summary>Figure details & limitations</summary>{(product.warnings??[]).map((w:string)=><p key={w}>{w}</p>)}<a href={product.metadata_url} target="_blank" rel="noreferrer">Figure metadata</a></details></>}
    <div className="overview-panel-footer"><span>One baseline; not a whole-array health test.</span><Link to={'/vis?'+query}>Visibilities →</Link></div>
  </>;
}
