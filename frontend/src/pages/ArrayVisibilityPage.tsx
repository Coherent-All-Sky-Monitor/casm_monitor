import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, Evidence, Notice, TimeZone, LOCAL } from "../components/Workspace";
import { Antenna, DynamicSpectrum, LayoutMap, location, QUANTITIES, Quantity, Snapshot, Spectrum, wiringLabel } from "../components/vis/ArrayPlots";
import "../array-vis.css";
import "../array-vis-compact.css";
import { FigureZoom } from "../components/FigureZoom";
import { PairMatrix, PairTile } from "../components/vis/PairMatrix";
import { SnapPanels, StationPanels } from "../components/vis/StationPanels";

const cache=new Map<string,Snapshot>();
export default function ArrayVisibilityPage() {
  const [,setParams]=useSearchParams();
  const [mode,setMode]=useState("auto"),[quantity,setQuantity]=useState<Quantity>("amp"),[view,setView]=useState("spectrum");
  const [reference,setReference]=useState("raw"),[referenceInput,setReferenceInput]=useState(8),[hours,setHours]=useState("24");
  const [arrangement,setArrangement]=useState("compact");
  const [zone,setZone]=useState(LOCAL),[statistic,setStatistic]=useState<"latest"|"mean">("latest"),[log,setLog]=useState(true);
  const [fmin,setFmin]=useState("390.625"),[fmax,setFmax]=useState("484.375"),[band,setBand]=useState([390.625,484.375]);
  const [data,setData]=useState<Snapshot|null>(null),[error,setError]=useState(""),[busy,setBusy]=useState(false),[tick,setTick]=useState(0);
  const [selected,setSelected]=useState<number[]|null>(null),[focus,setFocus]=useState<number|null>(null);
  const [pairFocus,setPairFocus]=useState<{a:Antenna;b:Antenna;p:PairTile;data:Snapshot;quantity:Quantity}|null>(null);
  const serial=useRef(0);
  const key=new URLSearchParams({mode,reference,reference_input:String(referenceInput),hours,fmin:String(band[0]),fmax:String(band[1])}).toString();
  useEffect(()=>{const id=++serial.current;const cached=cache.get(key);if(cached)setData(cached);setBusy(true);setError("");api<Snapshot>(`/api/science/array?${key}`).then(d=>{if(id!==serial.current)return;cache.set(key,d);if(cache.size>12)cache.delete(cache.keys().next().value!);setData(d);setSelected(old=>old??d.default_inputs);setBusy(false);}).catch(e=>{if(id===serial.current){setError(e.message);setBusy(false);}});return()=>{serial.current++;};},[key,tick]);
  useEffect(()=>{const timer=window.setInterval(()=>{if(!document.hidden)setTick(t=>t+1);},60000);return()=>window.clearInterval(timer);},[]);
  useEffect(()=>{const close=(e:KeyboardEvent)=>{if(e.key==='Escape')setFocus(null);};window.addEventListener('keydown',close);return()=>window.removeEventListener('keydown',close);},[]);
  const chosen=selected??[];
  const inputs=data?.inputs??[];
  const rows=[...new Set(inputs.filter(a=>chosen.includes(a.packet_idx)).map(a=>location(a).row))].sort((a,b)=>b-a);
  const toggle=(p:number)=>setSelected(s=>(s??[]).includes(p)?(s??[]).filter(i=>i!==p):[...(s??[]),p]);
  const focused=inputs.find(a=>a.packet_idx===focus);
  const panelFor=(a:Antenna)=>data?.panels.find(p=>p.input===a.packet_idx);
  const targetReference=inputs.find(a=>a.packet_idx===(data?.reference_input??referenceInput));
  const ready=data?.mode===mode&&data?.reference===reference&&data?.reference_input===referenceInput;
  const dated=(t:number)=>new Intl.DateTimeFormat('en-US',{timeZone:zone,month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(t*1000));
  const label=QUANTITIES.find(q=>q[0]===quantity)![1];
  const detail=(p:{pair:number[]},source=data,q=quantity,plotView=view)=>{if(!source)return;const dynamic=plotView!=='spectrum';const kind=q==='phase'?(dynamic?'phase_waterfall':'phase_spectrum'):q==='amp'?(dynamic?'amplitude_waterfall':'amplitude_spectrum'):`${q}_${dynamic?'waterfall':'spectrum'}`;setParams({view:'detail',pairs:p.pair.join(','),kind,rolling_hours:hours,reference:source.reference,spectrum_statistic:statistic,time_tz:zone,t0:new Date(Math.floor(source.t0*1000)).toISOString(),t1:new Date(Math.ceil(source.t1*1000)).toISOString(),fmin:String(Math.min(...source.freq_mhz)),fmax:String(Math.max(...source.freq_mhz))});};
  const draw=(a:Antenna,expanded=false)=>{
    const p=panelFor(a);if(!p||!data)return null;
    const plot=view==='dynamic'?<DynamicSpectrum tile={p.images[quantity]} data={data} zone={zone} quantity={quantity}/>:<Spectrum values={p.spectra[quantity][statistic]} freq={data.freq_mhz} quantity={quantity} log={log&&quantity==='amp'}/>;
    return <article className={`antenna-plot ${expanded?'expanded':''}`} key={a.packet_idx} data-packet={a.packet_idx} style={expanded||arrangement!=='physical'?undefined:{gridColumn:location(a).col+1}}>
      <header><button className="plot-title" onClick={()=>setFocus(a.packet_idx)}>{a.station}<small>ant {a.antenna}</small><small className="wiring-label">{wiringLabel(a)}</small></button><span title={targetReference?wiringLabel(targetReference):undefined}>{p.is_auto?'auto':`× ${targetReference?.station??'ref'}`}</span></header>
      {expanded?plot:<div className="plot-zoom-trigger" role="button" tabIndex={0} aria-label={`Enlarge ${a.station} ${label} plot`} onClick={()=>setFocus(a.packet_idx)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();setFocus(a.packet_idx);}}}>{plot}</div>}
      <footer><span>{p.valid_fraction===0?'No samples':p.is_auto&&data.mode==='cross'?'Reference auto':`${label}${view==='spectrum'&&statistic==='mean'?' · mean':''}`}</span>{!expanded&&<button aria-label={`Expand ${a.station}`} onClick={()=>setFocus(a.packet_idx)}>↗</button>}</footer>
    </article>;
  };
  const age=data?Math.max(0,(Date.now()/1000-data.t1)/60):0;
  return <div className="workspace-page array-page"><div className="page-heading array-page-heading"><div><p className="eyebrow">ARRAY SNAPSHOT</p><h2>Visibilities</h2><p className="muted">{chosen.length} antennas selected · Rolling visibilities · {arrangement==='snap'&&view!=='matrix'?'SNAP / ADC order':'station order'}.</p></div><button onClick={()=>setParams({view:'detail'})}>Detailed baseline inspector →</button></div>
    {error&&<Notice>{error}{data?' Showing the previous successful snapshot below.':''}</Notice>}
    <div className="array-overview-controls"><section className="array-layout-panel"><div className="section-heading"><h3>Array layout</h3><span>{chosen.length} / {inputs.length||'…'}</span></div><LayoutMap inputs={inputs} selected={chosen} reference={referenceInput} mode={view==='matrix'?'all':mode} onToggle={toggle}/><div className="array-preset-buttons"><button onClick={()=>setSelected(data?.default_inputs??[])}>Default 17</button><button onClick={()=>setSelected(inputs.map(a=>a.packet_idx))}>All wired</button><button onClick={()=>setSelected([])}>Clear</button></div></section>
    <section className="array-control-panel"><div className="array-control-line"><label>Correlation</label><div className="choice-row">{[['auto','Autocorrelations'],['cross','Cross-correlations']].map(([v,l])=><button key={v} className={mode===v?'active':''} onClick={()=>{setMode(v);setView(v==='cross'?'dynamic':'spectrum');setFocus(null);setPairFocus(null);}}>{l}</button>)}{mode==='cross'&&view!=='matrix'&&<label className="array-reference">Reference<select aria-label="Reference antenna" value={referenceInput} onChange={e=>setReferenceInput(Number(e.target.value))}>{inputs.map(a=><option value={a.packet_idx} key={a.packet_idx}>{a.station} · ant {a.antenna}</option>)}</select></label>}</div></div>
      <div className="array-control-line"><label>Quantity</label><div className="choice-row array-quantity">{QUANTITIES.map(([v,l])=><button key={v} className={quantity===v?'active':''} aria-pressed={quantity===v} onClick={()=>{setQuantity(v);setFocus(null);setPairFocus(null);}}>{l}</button>)}</div></div>
      <div className="array-control-line"><label>View</label><div className="choice-row">{[['spectrum','Spectrum'],['dynamic','Dynamic spectrum'],['matrix','All pairs']].map(([v,l])=><button key={v} className={view===v?'active':''} onClick={()=>{setView(v);if(v==='matrix')setMode('cross');setFocus(null);setPairFocus(null);}}>{l}</button>)}</div></div>
      <div className="field-row array-secondary"><label>Rolling window<select value={hours} onChange={e=>setHours(e.target.value)}><option value="0.5">30 minutes</option><option value="2">2 hours</option><option value="6">6 hours</option><option value="24">24 hours</option></select></label><label>Processing<select value={reference} onChange={e=>setReference(e.target.value)}><option value="raw">Raw</option><option value="sun">Sun fringe-stopped</option></select></label>{view==='spectrum'&&<label>Spectrum at<select value={statistic} onChange={e=>setStatistic(e.target.value as 'latest'|'mean')}><option value="latest">Latest integration</option><option value="mean">Window mean</option></select></label>}{view==='spectrum'&&quantity==='amp'&&<label>Vertical scale<select value={log?'log':'linear'} onChange={e=>setLog(e.target.value==='log')}><option value="log">Logarithmic</option><option value="linear">Linear</option></select></label>}<TimeZone value={zone} onChange={setZone}/></div>
      <div className="array-status" role="status"><span className={`live-dot ${age>15?'stale':''}`}/><span>{data?`Latest ${dated(data.t1)} · ${age.toFixed(0)} min ago · ${data.samples} integrations`:'Loading the array snapshot…'}{busy&&data?' · Updating…':''}</span><button disabled={busy} onClick={()=>setTick(t=>t+1)}>Refresh</button></div>
      <p className="array-caption">{view==='spectrum'?(statistic==='latest'?'One recorded integration. Each panel has its own labelled scale.':'Window mean: mean |V|; Real/Imag from the complex mean; phase = angle of the complex mean.') :view==='dynamic'?'Time → · frequency ↑. Every integration retained. Colour ranges per panel; percentile display limits (phase: −π to π).':'Each cell is a baseline. Antennas are ordered north to south, then west to east.'} {quantity==='phase'?'Zero-amplitude phase is undefined and left blank.':quantity==='amp'?'No channel normalization.':''}</p>
      <details className="array-options"><summary>Frequency range & evidence</summary><form className="field-row" onSubmit={e=>{e.preventDefault();const lo=Number(fmin),hi=Number(fmax);if(lo>0&&hi>lo&&hi<1000)setBand([lo,hi]);else setError('Choose increasing frequency bounds in MHz.');}}><label>Min MHz<input type="number" value={fmin} step="any" onChange={e=>setFmin(e.target.value)}/></label><label>Max MHz<input type="number" value={fmax} step="any" onChange={e=>setFmax(e.target.value)}/></label><button>Apply band</button></form><p>Default 17 is the inspection selection, independent of beam deployment. The layout map shows wired stations; toggle any of them. Spectra retain all cached channels. Dynamic previews average frequency down to ≤128 channels, with no additional time averaging.</p>{data&&<Evidence value={data.provenance}/>}</details>
    </section></div>
    {data&&<section className={`array-results ${busy?'array-pending':''}`} aria-busy={busy}>
      <div className="array-results-heading">
        <h3>{view==='matrix'?'All baselines':data.mode==='auto'?'Autocorrelations':`Baselines to ${targetReference?.station??'reference'}`} <span>· {label}</span></h3>
        <span>{data.reference==='raw'?'Raw':'Sun fringe-stopped'} · {dated(data.t0)}–{dated(data.t1)} {zone===LOCAL?'OVRO local':'UTC'}</span>
      </div>
      {chosen.length===0?<Notice>Select antennas on the layout map, or restore Default 17.</Notice>
        :view==='matrix'?<PairMatrix data={data} selected={chosen} quantity={quantity} onSelect={(a,b,p)=>setPairFocus({a,b,p,data,quantity})}/>
        :<>
          <div className="array-arrangement">
            <div className="choice-row" aria-label="Plot arrangement">
              <button className={arrangement==='compact'?'active':''} aria-pressed={arrangement==='compact'} onClick={()=>setArrangement('compact')}>Compact panels</button>
              <button className={arrangement==='snap'?'active':''} aria-pressed={arrangement==='snap'} onClick={()=>setArrangement('snap')}>SNAP order</button>
              <button className={arrangement==='physical'?'active':''} aria-pressed={arrangement==='physical'} onClick={()=>setArrangement('physical')}>Station grid</button>
            </div>
            <p>{arrangement==='snap'?'Compact panels grouped by SNAP / slot, then ascending ADC.':`${arrangement==='compact'?'Rows packed north → south; panels west → east.':'Aligned station columns, north up and east right.'} Position keys: × no wired antenna · off hidden · ● shown.`}</p>
          </div>
          {arrangement==='compact'?<StationPanels inputs={inputs} selected={chosen} draw={draw}/>
            :arrangement==='snap'?<SnapPanels inputs={inputs} selected={chosen} draw={draw}/>
            :<div className="antenna-grid-scroll"><div className="antenna-plot-grid">
              <div className="antenna-grid-header"><span>North ↑</span>{[1,2,3,4,5,6].map(c=><span key={c}>E{c}</span>)}</div>
              {rows.map(row=><div className="antenna-plot-row" key={row}>
                <span className="antenna-row-label">N{String(row).padStart(2,'0')}</span>
                {inputs.filter(a=>location(a).row===row&&chosen.includes(a.packet_idx)).sort((a,b)=>location(a).col-location(b).col).map(a=>draw(a))}
              </div>)}
            </div></div>}
        </>}
    </section>}
    {focus!==null&&focused&&data&&ready&&<FigureZoom title={`${focused.station} · ${label} ${view==='dynamic'?'dynamic spectrum':'spectrum'}`} onClose={()=>setFocus(null)}
      actions={<><button onClick={()=>detail(panelFor(focused)!)}>Open stored-pair plot</button><p>Cross panels use V(target, reference). Detailed plots use the ascending stored packet pair.</p></>}>
      {draw(focused,true)}
    </FigureZoom>}
    {pairFocus&&<FigureZoom title={`${pairFocus.a.station} × ${pairFocus.b.station} · ${QUANTITIES.find(q=>q[0]===pairFocus.quantity)![1]} dynamic spectrum`} onClose={()=>setPairFocus(null)}
      actions={<><button onClick={()=>detail({pair:pairFocus.p.stored_pair},pairFocus.data,pairFocus.quantity,'dynamic')}>Open stored-pair plot</button><p>V(row, column) · pinned snapshot. Detailed plots use the ascending stored pair{pairFocus.p.pair[0]>pairFocus.p.pair[1]?' (conjugate of this view)':''}.</p></>}>
      <p className="pair-wiring">{pairFocus.a.station} · ant {pairFocus.a.antenna} · {wiringLabel(pairFocus.a)}<br/>{pairFocus.b.station} · ant {pairFocus.b.antenna} · {wiringLabel(pairFocus.b)}</p>
      <DynamicSpectrum tile={pairFocus.p.tile} data={pairFocus.data} zone={zone} quantity={pairFocus.quantity}/>
    </FigureZoom>}
  </div>;
}
