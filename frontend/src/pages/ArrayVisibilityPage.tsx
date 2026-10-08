import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Evidence, Notice, TimeZone, LOCAL } from "../components/Workspace";
import {useCachedResource, useTabState} from '../components/CachedResource';
import { Antenna, DynamicSpectrum, LayoutMap, location, QUANTITIES, Quantity, Snapshot, Spectrum, wiringLabel } from "../components/vis/ArrayPlots";
import "../array-vis.css";
import "../array-vis-compact.css";
import { FigureZoom } from "../components/FigureZoom";
import { PairMatrix, PairTile } from "../components/vis/PairMatrix";
import { SnapPanels, StationPanels } from "../components/vis/StationPanels";
import { VisibilityPlayback } from "../components/vis/VisibilityPlayback";
import {PowerScale,powerLabel} from '../components/powerScale';
import {PowerScaleControl} from '../components/PowerScaleControl';
import {useDelayedBusy,usePreparedView} from '../components/RefreshState';

type ArrayView={data:Snapshot;quantity:Quantity;view:string;powerScale:PowerScale;statistic:'latest'|'mean';log:boolean;zone:string;hours:string;band:number[]};
const arrayImages=({data,view,quantity,powerScale}:ArrayView)=>view==='dynamic'?data.panels.flatMap(p=>{
  const tile=data.mode==='auto'&&quantity==='amp'?p.power_images?.[powerScale]:p.images[quantity];
  return tile?[tile.src,tile.scale]:[];
}):[];

const useSelection=<T,>(key:string, initial:T)=>useTabState('vis.'+key,initial);
export default function ArrayVisibilityPage() {
  const [,setParams]=useSearchParams();
  const [mode,setMode]=useSelection('mode',"auto"),[quantity,setQuantity]=useSelection<Quantity>('quantity',"amp"),[view,setView]=useSelection('view',"spectrum");
  const [reference,setReference]=useSelection('reference',"raw"),[referenceInput,setReferenceInput]=useSelection('referenceInput',8),[hours,setHours]=useSelection('hours',"24");
  const [arrangement,setArrangement]=useSelection('arrangement',"compact");
  const [powerScale,setPowerScale]=useSelection<PowerScale>('powerScale','db');
  const [playback,setPlayback]=useState(false);
  const [zone,setZone]=useSelection('zone',LOCAL),[statistic,setStatistic]=useSelection<"latest"|"mean">('statistic',"latest"),[log,setLog]=useSelection('log',true);
  const [fmin,setFmin]=useSelection('fmin',"390.625"),[fmax,setFmax]=useSelection('fmax',"484.375"),[band,setBand]=useSelection('band',[390.625,484.375]);
  const [validationError,setError]=useState("");
  const [selected,setSelected]=useSelection<number[]|null>('selected',null),[focus,setFocus]=useState<number|null>(null);
  const [pairFocus,setPairFocus]=useState<{a:Antenna;b:Antenna;p:PairTile;data:Snapshot;quantity:Quantity}|null>(null);
  const images=view==='dynamic'?(mode==='auto'&&quantity==='amp'?'power_'+powerScale:quantity):'none';
  const key=new URLSearchParams({mode,reference,reference_input:String(referenceInput),hours,fmin:String(band[0]),fmax:String(band[1]),images}).toString();
  const {data:loaded,error:readError,busy:reading,refresh}=useCachedResource<Snapshot>(`/api/science/array?${key}`,playback?0:60000,{maxAge:60000});
  const candidate=useMemo(()=>loaded?{data:loaded,quantity,view,powerScale,statistic,log,zone,hours,band}:null,[loaded,quantity,view,powerScale,statistic,log,zone,hours,band]);
  const {shown,preparing,error:imageError}=usePreparedView(candidate,arrayImages);
  const data=shown?.data??null;
  const display=shown??{quantity,view,powerScale,statistic,log,zone,hours,band};
  const busy=reading||preparing,updating=useDelayedBusy(busy);
  const error=validationError||readError||imageError;
  useEffect(()=>{const close=(e:KeyboardEvent)=>{if(e.key==='Escape')setFocus(null);};window.addEventListener('keydown',close);return()=>window.removeEventListener('keydown',close);},[]);
  const inputs=data?.inputs??[];
  const chosen=(selected??data?.default_inputs??[]).filter(p=>inputs.some(a=>a.packet_idx===p));
  const membershipKnown=data?.membership?.status==='complete';
  const presetLabel=`Beamforming · ${membershipKnown?data?.default_inputs.length:'unknown'}`;
  const rows=[...new Set(inputs.filter(a=>chosen.includes(a.packet_idx)).map(a=>location(a).row))].sort((a,b)=>b-a);
  const toggle=(p:number)=>setSelected(chosen.includes(p)?chosen.filter(i=>i!==p):[...chosen,p]);
  const focused=inputs.find(a=>a.packet_idx===focus);
  const panelFor=(a:Antenna)=>data?.panels.find(p=>p.input===a.packet_idx);
  const targetReference=inputs.find(a=>a.packet_idx===(data?.reference_input??referenceInput));
  const dated=(t:number)=>new Intl.DateTimeFormat('en-US',{timeZone:display.zone,month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(t*1000));
  const label=data?.mode==='auto'&&display.quantity==='amp'?'Power':QUANTITIES.find(q=>q[0]===display.quantity)![1];
  const detail=(p:{pair:number[]},source=data,q=display.quantity,plotView=display.view)=>{if(!source)return;const dynamic=plotView!=='spectrum';const kind=q==='phase'?(dynamic?'phase_waterfall':'phase_spectrum'):q==='amp'?(dynamic?'amplitude_waterfall':'amplitude_spectrum'):`${q}_${dynamic?'waterfall':'spectrum'}`;setParams({view:'detail',pairs:p.pair.join(','),kind,rolling_hours:display.hours,reference:source.reference,spectrum_statistic:display.statistic,time_tz:display.zone,t0:new Date(Math.floor(source.t0*1000)).toISOString(),t1:new Date(Math.ceil(source.t1*1000)).toISOString(),fmin:String(Math.min(...source.freq_mhz)),fmax:String(Math.max(...source.freq_mhz))});};
  const draw=(a:Antenna,expanded=false)=>{
    const p=panelFor(a);if(!p||!data)return null;
    const {quantity,view,powerScale,statistic,log,zone}=display;
    const autoPower=data.mode==='auto'&&quantity==='amp';
    const tile=autoPower?p.power_images?.[powerScale]:p.images[quantity];
    const plot=view==='dynamic'?(tile?<DynamicSpectrum tile={tile} data={data} zone={zone} quantity={quantity} valueLabel={autoPower?powerLabel(powerScale):undefined}/>:<p>{readError?'Dynamic spectrum unavailable · refresh to retry.':'Loading dynamic spectrum…'}</p>):<Spectrum values={p.spectra[autoPower?'real':quantity][statistic]} freq={data.freq_mhz} quantity={quantity} log={log&&quantity==='amp'&&!autoPower} powerScale={autoPower?powerScale:undefined}/>;
    return <article className={`antenna-plot ${a.beamforming?'in-beamforming':''} ${expanded?'expanded':''}`} key={a.packet_idx} data-packet={a.packet_idx} style={expanded||arrangement!=='physical'?undefined:{gridColumn:location(a).col+1}}>
      <header><button className="plot-title" onClick={()=>setFocus(a.packet_idx)}>{a.station}<small>ant {a.antenna}</small><small className="wiring-label">{wiringLabel(a)}</small></button><span title={targetReference?wiringLabel(targetReference):undefined}>{p.is_auto?'auto':`× ${targetReference?.station??'ref'}`}</span></header>
      {expanded?plot:<div className="plot-zoom-trigger" role="button" tabIndex={0} aria-label={`Enlarge ${a.station} ${label} plot`} onClick={()=>setFocus(a.packet_idx)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();setFocus(a.packet_idx);}}}>{plot}</div>}
      <footer><span>{p.valid_fraction===0?'No samples':p.is_auto&&data.mode==='cross'?'Reference auto':`${label}${view==='spectrum'&&statistic==='mean'?' · mean':''}`}</span>{!expanded&&<button aria-label={`Expand ${a.station}`} onClick={()=>setFocus(a.packet_idx)}>↗</button>}</footer>
    </article>;
  };
  const age=data?Math.max(0,(Date.now()/1000-data.t1)/60):0;
  return <div className="workspace-page array-page"><div className="page-heading array-page-heading"><div><p className="eyebrow">ARRAY SNAPSHOT</p><h2>Visibilities</h2><p className="muted">{chosen.length} antennas selected · Rolling visibilities · {arrangement==='snap'&&view!=='matrix'?'SNAP / ADC order':'station order'}.</p></div><button onClick={()=>setParams({view:'detail'})}>Detailed baseline inspector →</button></div>
    <div className="vis-history-mode"><button aria-pressed={!playback} onClick={()=>setPlayback(false)}>Live</button><button aria-pressed={playback} disabled={!data||!chosen.length} onClick={()=>setPlayback(true)}>History</button><p>Live · rolling snapshot</p></div>
    {error&&<Notice>{error}{data?' Showing the previous successful snapshot below.':''}</Notice>}
    <div className="array-overview-controls"><section className="array-layout-panel"><div className="section-heading"><h3>Array layout</h3><span>{chosen.length} / {inputs.length||'…'}</span></div><LayoutMap inputs={inputs} selected={chosen} reference={referenceInput} mode={view==='matrix'?'all':mode} onToggle={toggle}/><div className="array-preset-buttons"><button className={selected===null?'active':''} aria-pressed={selected===null} onClick={()=>setSelected(null)}>{presetLabel}</button><button onClick={()=>setSelected(inputs.map(a=>a.packet_idx))}>All wired</button><button onClick={()=>setSelected([])}>Clear</button></div>{data&&!membershipKnown&&<Notice>Beamforming membership is unavailable or does not match this layout. Select wired antennas manually.</Notice>}</section>
    <section className="array-control-panel"><div className="array-control-line"><label>Correlation</label><div className="choice-row">{[['auto','Autocorrelations'],['cross','Cross-correlations']].map(([v,l])=><button key={v} className={mode===v?'active':''} onClick={()=>{setMode(v);setView(v==='cross'?'dynamic':'spectrum');setFocus(null);setPairFocus(null);}}>{l}</button>)}{mode==='cross'&&view!=='matrix'&&<label className="array-reference">Reference<select aria-label="Reference antenna" value={referenceInput} onChange={e=>setReferenceInput(Number(e.target.value))}>{inputs.map(a=><option value={a.packet_idx} key={a.packet_idx}>{a.station} · ant {a.antenna}</option>)}</select></label>}</div></div>
      <div className="array-control-line"><label>Quantity</label><div className="choice-row array-quantity">{QUANTITIES.map(([v,l])=><button key={v} className={quantity===v?'active':''} aria-pressed={quantity===v} onClick={()=>{setQuantity(v);setFocus(null);setPairFocus(null);}}>{mode==='auto'&&v==='amp'?'Power':l}</button>)}</div></div>
      {mode==='auto'&&quantity==='amp'&&<div className="array-control-line"><label>Power scale</label><PowerScaleControl value={powerScale} onChange={setPowerScale}/></div>}
      <div className="array-control-line"><label>View</label><div className="choice-row">{[['spectrum','Spectrum'],['dynamic','Dynamic spectrum'],['matrix','All pairs']].map(([v,l])=><button key={v} className={view===v?'active':''} onClick={()=>{setView(v);if(v==='matrix')setMode('cross');setFocus(null);setPairFocus(null);}}>{l}</button>)}</div></div>
      <details className="array-options"><summary>Time, processing & scales · {hours} h · {reference==='raw'?'Raw':'Sun fringe-stopped'}</summary><div className="field-row array-secondary"><label>Rolling window<select value={hours} onChange={e=>setHours(e.target.value)}><option value="0.5">30 minutes</option><option value="2">2 hours</option><option value="6">6 hours</option><option value="24">24 hours</option></select></label><label>Processing<select value={reference} onChange={e=>setReference(e.target.value)}><option value="raw">Raw</option><option value="sun">Sun fringe-stopped</option></select></label>{view==='spectrum'&&<label>Spectrum at<select value={statistic} onChange={e=>setStatistic(e.target.value as 'latest'|'mean')}><option value="latest">Latest integration</option><option value="mean">Window mean</option></select></label>}{view==='spectrum'&&quantity==='amp'&&mode!=='auto'&&<label>Vertical scale<select value={log?'log':'linear'} onChange={e=>setLog(e.target.value==='log')}><option value="log">Logarithmic</option><option value="linear">Linear</option></select></label>}<TimeZone value={zone} onChange={setZone}/></div></details>
      <div className="array-status" role="status"><span className={`live-dot ${age>15?'stale':''}`}/><span>{data?`Latest ${dated(data.t1)} · ${age.toFixed(0)} min ago · ${data.samples} integrations`:'Loading the array snapshot…'}</span><span className="refresh-status">{updating&&data?'Updating…':''}</span><button disabled={busy} onClick={()=>{setError('');refresh();}}>Refresh</button></div>
      <details className="array-options"><summary>Plot notes</summary><p className="array-caption">{view==='spectrum'?(statistic==='latest'?'One recorded integration. Each panel has its own labelled scale.':'Window mean: mean |V|; Real/Imag from the complex mean; phase = angle of the complex mean.') :view==='dynamic'?'Time → · frequency ↑. Every integration retained. Colour ranges per panel; percentile display limits (phase: −π to π).':'Each cell is a baseline. Antennas are ordered north to south, then west to east.'} {quantity==='phase'?'Zero-amplitude phase is undefined and left blank.':quantity==='amp'?(mode==='auto'?'Power is Re(Vᵢᵢ). Non-positive power is blank in dB. Averaging precedes conversion. No channel normalization.':'No channel normalization.'):''}</p></details>
      <details className="array-options"><summary>Frequency range & evidence</summary><form className="field-row" onSubmit={e=>{e.preventDefault();const lo=Number(fmin),hi=Number(fmax);if(lo>0&&hi>lo&&hi<1000)setBand([lo,hi]);else setError('Choose increasing frequency bounds in MHz.');}}><label>Min MHz<input type="number" value={fmin} step="any" onChange={e=>setFmin(e.target.value)}/></label><label>Max MHz<input type="number" value={fmax} step="any" onChange={e=>setFmax(e.target.value)}/></label><button>Apply band</button></form><p>Beamforming follows the inspected nonzero coherent-beam weights in the latest recorded deployment, matched to this layout. It is current membership, not historical membership or an antenna-health check. Toggle any wired station; manual selections survive refresh. Spectra retain all cached channels. Dynamic previews average frequency down to ≤128 channels, with no additional time averaging.</p>{data&&<Evidence value={data.provenance}/>}</details>
    </section></div>
    {data&&<section className="array-results" aria-busy={busy}>
      <div className="array-results-heading">
        <h3>{display.view==='matrix'?'All baselines':data.mode==='auto'?'Autocorrelations':`Baselines to ${targetReference?.station??'reference'}`} {display.view!=='matrix'&&<span>· {label}</span>}</h3>
        {display.view!=='matrix'&&<span>{data.reference==='raw'?'Raw':'Sun fringe-stopped'} · {dated(data.t0)}–{dated(data.t1)} {display.zone===LOCAL?'OVRO local':'UTC'}</span>}
      </div>
      {chosen.length===0?<Notice>Select antennas on the layout map, or restore the Beamforming selection.</Notice>
        :display.view==='matrix'?<PairMatrix data={data} selected={chosen} quantity={display.quantity} zone={display.zone} onSelect={(a,b,p,source,q)=>setPairFocus({a,b,p,data:source,quantity:q})}/>
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
    {playback&&data&&<VisibilityPlayback inputs={inputs} initial={{mode:data.mode,view:display.view,quantity:display.quantity,reference:data.reference,referenceInput:data.reference_input,arrangement,windowHours:Number(display.hours),statistic:display.statistic,log:display.log,powerScale:display.powerScale,zone:display.zone as 'UTC'|'America/Los_Angeles',band:display.band,selected:chosen,end:data.t1}} onClose={()=>setPlayback(false)} onInspect={(pair,t0,t1,o)=>{setPlayback(false);setParams({view:'detail',pairs:pair.join(','),kind:`${o.quantity==='amp'?'amplitude':o.quantity}_${o.view==='spectrum'?'spectrum':'waterfall'}`,t0:new Date(t0*1000).toISOString(),t1:new Date(t1*1000).toISOString(),reference:o.reference,spectrum_statistic:o.statistic,time_tz:o.zone,fmin:String(o.band[0]),fmax:String(o.band[1])});}}/>}
    {focus!==null&&focused&&data&&<FigureZoom title={`${focused.station} · ${label} ${display.view==='dynamic'?'dynamic spectrum':'spectrum'}`} onClose={()=>setFocus(null)}
      controls={data.mode==='auto'&&display.quantity==='amp'?<PowerScaleControl value={display.powerScale} onChange={setPowerScale}/>:undefined}
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
