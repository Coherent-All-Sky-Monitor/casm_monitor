import {useEffect,useState,type ReactNode} from 'react';
import {api,Json,Notice} from './Workspace';
import {FigureZoom} from './FigureZoom';
import {Antenna,DynamicSpectrum,location,wiringLabel,Quantity,QUANTITIES} from './vis/ArrayPlots';
import {StationPanels,SnapPanels} from './vis/StationPanels';
import {CalibrationCurve,CalLine} from './CalibrationCurve';
import {LOCAL} from './timeZones';
import {playbackStamp} from './snapPlaybackModel';
import {PowerScale,powerValue,powerLabel} from './powerScale';
import '../array-vis.css';
import '../array-vis-compact.css';
import '../calibration-inspector.css';

const STAGES=[['raw','Raw'],['static','Static-subtracted'],['geometric','Geometric model'],['fringe','Fringe-stopped'],['svd','SVD-corrected']];
const SCHEMES=[['uniform','Uniform','1'],['inv_var','Inv-var','1 / Nᵢ(f)'],['inv_std','Inv-std','1 / √Nᵢ(f)'],['matched','Matched','sᵢ(f) / Nᵢ(f)']];
const AUTOS=[['auto','Raw','#2459a6'],['static','Static template','#b76a1d'],['subtracted','Static-subtracted','#207c5b']];
const cache=new Map<string,Json>();
async function saved(url:string){if(cache.has(url))return cache.get(url)!;const value=await api(url);cache.set(url,value);while(cache.size>10)cache.delete(cache.keys().next().value!);return value;}

function Choices({label,options,selected,onSelect,disabled=()=>false}:{label:string;options:string[][];selected:string|string[];onSelect:(id:string)=>void;disabled?:(id:string)=>boolean}){
  return <div className="cal-choice-control"><span>{label}</span><div className="choice-row" role="group" aria-label={label}>{options.map(([id,name])=>{const active=Array.isArray(selected)?selected.includes(id):selected===id;return <button type="button" key={id} className={active?'active':''} aria-pressed={active} disabled={disabled(id)} onClick={()=>onSelect(id)}>{name}</button>;})}</div></div>;
}

function Panels({inputs,arrangement,draw}:{inputs:Antenna[];arrangement:string;draw:(a:Antenna)=>ReactNode}){
  const props={inputs,selected:inputs.map(a=>a.packet_idx),draw};
  if(arrangement==='snap')return <SnapPanels {...props}/>;
  if(arrangement==='compact')return <StationPanels {...props}/>;
  const rows=[...new Set(inputs.map(a=>location(a).row))].sort((a,b)=>b-a);
  return <div className="cal-physical-scroll"><div className="cal-physical-grid"><div className="cal-physical-row"><strong>North ↑</strong>{[1,2,3,4,5,6].map(c=><span key={c}>E{c}</span>)}</div>{rows.map(row=><div className="cal-physical-row" key={row}><strong>N{String(row).padStart(2,'0')}</strong>{[1,2,3,4,5,6].map(col=>{const a=inputs.find(a=>location(a).row===row&&location(a).col===col);return <div key={col}>{a?draw(a):<span className="cal-empty-slot">×<small>No selected antenna</small></span>}</div>;})}</div>)}</div></div>;
}

export function AmplitudeHelp({scheme}:{scheme:string}){
  return <div className="cal-amplitude-help"><p><strong>{SCHEMES.find(s=>s[0]===scheme)?.[1]}:</strong> aᵢ(f) ∝ <span className="cal-formula">{SCHEMES.find(s=>s[0]===scheme)?.[2]}</span> <span>· largest amplitude = 1 at each frequency</span></p>
    <details><summary>How the four amplitude schemes are calculated</summary>
      <table><thead><tr><th>Scheme</th><th>Voltage amplitude</th><th>Calculation</th></tr></thead><tbody>{SCHEMES.map(([id,name,formula],i)=><tr key={id}><th>{name}</th><td>{formula}</td><td>{['Give every selected antenna an amplitude of 1.','Divide 1 by each antenna’s night-time noise power.','Divide 1 by the square root of each antenna’s night-time noise power.','Divide each antenna’s measured source response by its night-time noise power.'][i]}</td></tr>)}</tbody></table>
      <dl><dt>i, f</dt><dd>Antenna i and frequency f.</dd><dt>Nᵢ(f)</dt><dd>Noise power: the median autocorrelation power during the selected night window.</dd><dt>sᵢ(f)</dt><dd>Source response: fit the static-subtracted, fringe-stopped cross-correlation amplitudes across the selected baselines.</dd><dt>aᵢ(f)</dt><dd>Calculate each frequency separately, then divide by the largest amplitude among the valid antennas. Amplitudes range from 0 to 1.</dd><dt>Applied weight</dt><dd>wᵢ(f) = aᵢ(f) exp(−i φᵢ(f)). The SVD solve supplies antenna phase φᵢ. Beam steering follows.</dd></dl>
      <p>Inv-var and Inv-std require positive night-time noise estimates. Matched also requires a static-subtracted source estimate, at least three antennas and a full-rank fit. Check its performance on an independent transit.</p>
    </details></div>;
}

function CrossZoom({tag,data,antenna,quantity,zone,onClose}:{tag:string;data:Json;antenna:Antenna;quantity:Quantity;zone:string;onClose:()=>void}){
  const [compare,setCompare]=useState(false),[stages,setStages]=useState<Json[]>([]),[error,setError]=useState('');
  useEffect(()=>{let active=true;if(compare)Promise.all(STAGES.filter(([id])=>data.stages.includes(id)&&(id!=='geometric'||quantity==='phase')).map(([stage])=>saved(`/api/calibration/builds/${tag}/inspection?reference=${data.reference}&target=${antenna.antenna}&stage=${stage}&quantity=${quantity}&scheme=${data.scheme}`))).then(v=>{if(active)setStages(v);}).catch(e=>setError(e.message));return()=>{active=false;};},[compare,tag,data.reference,data.scheme,quantity,antenna.antenna]);
  const show=compare?stages:[data],label=quantity==='phase'?'Phase (rad)':`${QUANTITIES.find(q=>q[0]===quantity)?.[1]} (counts)`;
  return <FigureZoom title={`${antenna.station} × ant ${data.reference}`} onClose={onClose} controls={<div className="cal-zoom-options"><button aria-pressed={compare} onClick={()=>setCompare(!compare)}>Compare processing stages</button><span>{wiringLabel(antenna)}</span></div>}>
    {error&&<Notice>{error}</Notice>}{compare&&!stages.length&&<p>Loading saved stages…</p>}
    <div className="cal-stage-comparison">{show.map(d=>{const panel=d.panels.find((p:Json)=>p.antenna===antenna.antenna);return panel&&<section key={d.stage}><h3>{STAGES.find(s=>s[0]===d.stage)?.[1]}</h3><CalibrationCurve freq={d.freq_mhz} lines={[{label:'Data',values:panel.values}]} label={label} phase={quantity==='phase'}/><DynamicSpectrum tile={panel.tile} data={{t0:d.t0,t1:d.t1,integration_s:d.integration_s,freq_mhz:d.preview_freq_mhz}} zone={zone} quantity={quantity}/></section>;})}</div>
  </FigureZoom>;
}

export default function CalibrationInspector({tag,detail,disabled,onAction,onTrial,comparison}:{tag:string;detail:Json;disabled:boolean;onAction:(action:string,values?:Json)=>unknown;onTrial:(antennas:number[],ref:number)=>unknown;comparison?:{tag:string;revision:string}}){
  const candidate=detail.request.params,actions:Json[]=detail.actions??[];
  const [reference,setReference]=useState<number>(candidate.ref_ant),[stage,setStage]=useState('svd'),[quantity,setQuantity]=useState<Quantity>('phase');
  const [scheme,setScheme]=useState('uniform'),[arrangement,setArrangement]=useState('compact'),[view,setView]=useState('dynamic');
  const [data,setData]=useState<Json|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(false);
  const [other,setOther]=useState<Json|null>(null);
  const [weightView,setWeightView]=useState('applied_phase'),[autoViews,setAutoViews]=useState(['auto','subtracted']),[zone,setZone]=useState(LOCAL);
  const [autoCompare,setAutoCompare]=useState('separate'),[autoScale,setAutoScale]=useState<PowerScale>('db');
  const [focus,setFocus]=useState<{kind:string;antenna:Antenna}|null>(null),[draft,setDraft]=useState<number[]>(candidate.antennas),[trialRef,setTrialRef]=useState(candidate.ref_ant);
  const revision=actions.filter(a=>['inspect','solve'].includes(a.action)).map(a=>a.action_id).join(',');
  useEffect(()=>{let active=true;setLoading(true);setError('');setOther(null);
    const get=(id:string,rev:string)=>saved(`/api/calibration/builds/${id}/inspection?reference=${reference}&stage=${stage}&quantity=${quantity}&scheme=${scheme}&revision=${encodeURIComponent(rev)}`);
    Promise.all([get(tag,revision),comparison?get(comparison.tag,comparison.revision):Promise.resolve(null)]).then(([first,second])=>{if(active){setData(first);setOther(second);if(!first.schemes[scheme]||(second&&!second.schemes[scheme]))setScheme('uniform');}}).catch(e=>{if(active)setError(e.message);}).finally(()=>{if(active)setLoading(false);});return()=>{active=false;};},[tag,reference,stage,quantity,scheme,revision,comparison?.tag,comparison?.revision]);
  const fresh=!loading&&!error&&data?.reference===reference&&data?.stage===stage&&data?.quantity===quantity&&data?.scheme===scheme&&(!comparison||!!other);
  const solutionLabel=(d:Json)=>d.static_used?'With subtraction':'Without subtraction';
  const variants:Json[]=data?(other?[data,other]:[data]):[];
  const inputs:Antenna[]=(data?.inputs??[]).filter((a:Antenna)=>candidate.antennas.includes(a.antenna));
  const selected=data?.antenna_panels??[];
  const find=(a:Antenna)=>selected.find((p:Json)=>p.antenna===a.antenna);
  const staticAvailable=selected.some((p:Json)=>(p.static??[]).some((v:number|null)=>v!==null&&Number.isFinite(v)));
  const shownAutos=AUTOS.filter(([id])=>autoViews.includes(id)&&(id==='auto'||staticAvailable));
  const autoControls=<div className="cal-auto-controls">
    <Choices label="Autocorrelation data" options={AUTOS} selected={shownAutos.map(([id])=>id)} disabled={id=>id!=='auto'&&!staticAvailable} onSelect={id=>setAutoViews(v=>v.includes(id)?v.filter(k=>k!==id):[...v,id])}/>
    <Choices label="Compare" options={[["separate","Separate plots"],["overlay","Overlay"]]} selected={autoCompare} onSelect={setAutoCompare}/>
    <Choices label="Power scale" options={[["db","Power (dB)"],["linear","Linear"]]} selected={autoScale} onSelect={v=>setAutoScale(v as PowerScale)}/>
  </div>;
  const weightLabels:Record<string,string>={applied_phase:'Applied phase (rad)',amplitude:'Weight amplitude (relative)',phase_difference:'Phase difference (rad)',amplitude_ratio:'Amplitude ratio',residual:'Baseline residual RMS (rad)',quantization_phase:'Int8 phase error (rad)'};
  const lines=(kind:string,a:Antenna):CalLine[]=>{const p=find(a);if(!p)return [];if(kind==='auto')return shownAutos.map(([id,label,color])=>({label,color,values:p[id]??[]}));
    if(other)return variants.flatMap(d=>{const row=d.antenna_panels.find((v:Json)=>v.antenna===a.antenna);return row?[{label:solutionLabel(d),values:row[weightView],color:d.static_used?'#207c5b':'#2459a6'}]:[];});
    if(weightView==='amplitude')return [{label:'Float weights',values:p.amplitude},{label:'Int8 preview',values:p.quantized_amplitude}];
    if(weightView==='applied_phase')return [{label:solutionLabel(data!),values:p.applied_phase},{label:detail.reference_label??data?.reference_label??'Saved deployed weights',values:p.reference_phase,color:'#a3acb7'}];
    return [{label:weightLabels[weightView],values:p[weightView]}];};
  const curve=(kind:string,a:Antenna)=>{
    const values=lines(kind,a);
    if(kind==='auto')return values.length?<div className={`cal-auto-plots cal-auto-${autoCompare}`} data-auto-scale={autoScale}>{(autoCompare==='separate'?values.map(line=>[line]):[values]).map(group=>{
      const nonpositive=group.reduce((n,line)=>n+line.values.filter(v=>v!==null&&Number.isFinite(v)&&v<=0).length,0);
      return <div className="cal-auto-plot" key={group.map(l=>l.label).join(',')}><h4>{group.length===1?group[0].label:'Overlay'}<small>{powerLabel(autoScale)}</small></h4><CalibrationCurve freq={data!.freq_mhz} lines={group.map(line=>({...line,values:line.values.map(v=>powerValue(v,autoScale))}))} label={powerLabel(autoScale)}/>{autoScale==='db'&&nonpositive>0&&<small>{nonpositive} non-positive samples · use Linear to see signed power</small>}</div>;
    })}</div>:<p className="cal-auto-empty">Select a spectrum above.</p>;
    return <CalibrationCurve freq={data!.freq_mhz} lines={values} label={weightLabels[weightView]} phase={['applied_phase','phase_difference'].includes(weightView)} range={weightView==='amplitude'?[0,1.05]:undefined}/>;
  };
  const cross=(a:Antenna)=>view==='dynamic'?<div className="cal-subtraction-pair">{variants.map(d=>{const panel=d.panels.find((p:Json)=>p.antenna===a.antenna);return panel&&<section key={String(d.static_used)}>{other&&<h4>{solutionLabel(d)}</h4>}<DynamicSpectrum tile={panel.tile} data={{...d,freq_mhz:d.preview_freq_mhz} as any} quantity={quantity} zone={zone}/></section>;})}</div>:<CalibrationCurve freq={data!.freq_mhz} lines={variants.map(d=>({label:solutionLabel(d),values:d.panels.find((p:Json)=>p.antenna===a.antenna)?.values??[],color:d.static_used?'#207c5b':'#2459a6'}))} label={quantity==='phase'?'Phase (rad)':`${QUANTITIES.find(q=>q[0]===quantity)?.[1]} (counts)`} phase={quantity==='phase'}/>;
  const draw=(kind:string)=>(a:Antenna)=><article className="cal-antenna-panel" key={a.antenna}><header><strong>{a.station}</strong><span>ant {a.antenna}{kind==='cross'?` × ${reference}`:''}</span><small>{wiringLabel(a)}</small></header><button className="cal-plot-button" aria-label={`Expand ${kind} ${a.station}`} onClick={()=>setFocus({kind,antenna:a})}>{kind==='cross'?cross(a):curve(kind,a)}</button>{kind==='weight'&&weightView==='amplitude'&&find(a)?.zeroed_channels>0&&<small className="cal-weight-warning">{find(a).zeroed_channels} positive channels round to zero</small>}</article>;
  const changed=[...draft].sort((a,b)=>a-b).join(',')!==[...candidate.antennas].sort((a:number,b:number)=>a-b).join(',');
  return <div className="cal-inspector">
    <section className="cal-card cal-inspector-toolbar"><div className="cal-section-heading"><h3>Array diagnostics</h3><span>{candidate.antennas.length} antennas · saved solve window</span></div>
      <Choices label="Panel layout" options={[["compact","Compact panels"],["snap","SNAP order"],["physical","Station grid"]]} selected={arrangement} onSelect={setArrangement}/>
      <div className="cal-inspector-controls"><label>Display reference<select aria-label="Display reference" value={reference} onChange={e=>setReference(Number(e.target.value))}>{candidate.antennas.map((a:number)=><option key={a} value={a}>{inputs.find(i=>i.antenna===a)?.station??`Ant ${a}`} · {a}</option>)}</select></label><label>Clock<select aria-label="Clock" value={zone} onChange={e=>setZone(e.target.value)}><option value={LOCAL}>OVRO local</option><option value="UTC">UTC</option></select></label><button disabled={disabled||!detail.summary?.paths?.cal_h5} onClick={()=>onAction('inspect')}>{data?'Refresh saved diagnostics':'Prepare array diagnostics'}</button></div>
      {data&&<p className="cal-caption">{playbackStamp(data.t0,zone as any)} — {playbackStamp(data.t1,zone as any)} · {data.samples} integrations{loading?' · loading saved view…':''}</p>}
      <nav className="cal-section-links" aria-label="Diagnostic sections">{[['autos','Autocorrelations'],['cross','Cross-correlations'],['weights','Antenna weights'],['quality','Rank-1 quality'],['history','Cross-day validation']].map(([id,text])=><a key={id} href={`#cal-${id}`}>{text}</a>)}</nav>
      {!detail.request.imported&&<details className="cal-trial"><summary>Change antennas · new solve trial</summary><p>Edits create a new candidate with the same windows and frozen layout. This candidate stays unchanged.</p><div className="cal-antennas">{(data?.inputs??[]).map((a:Antenna)=><label key={a.antenna} className={draft.includes(a.antenna)?'included':''}><input type="checkbox" checked={draft.includes(a.antenna)} onChange={()=>setDraft(v=>v.includes(a.antenna)?v.filter(i=>i!==a.antenna):[...v,a.antenna])}/>{a.station} · {a.antenna}</label>)}</div><label>Solve reference<select value={draft.includes(trialRef)?trialRef:''} onChange={e=>setTrialRef(Number(e.target.value))}><option value="" disabled>Select a reference</option>{draft.map(a=><option key={a} value={a}>Ant {a}</option>)}</select></label><button disabled={disabled||draft.length<2||!draft.includes(trialRef)||!changed} onClick={()=>onTrial(draft,trialRef)}>Solve new antenna trial</button></details>}
    </section>
    {error&&<Notice>{error}</Notice>}{!data&&<div className="cal-empty">Prepare the saved diagnostics once. Plot selections then use the saved arrays.</div>}
    {data&&<div className={!fresh?'cal-view-pending':''} aria-busy={!fresh}>
      <section id="cal-autos" className="cal-diagnostic-section"><div className="cal-section-heading"><h3>Autocorrelations</h3><span>Power versus frequency</span></div>{autoControls}<p className="cal-caption">Select one or more. Raw = source-window average; static template = night-window average. {autoCompare==='separate'?'Separate plots fit each y-axis independently.':'Overlay uses one y-axis per antenna.'} {autoScale==='db'?'Non-positive power is blank in dB; Linear shows signed residuals.':'All finite values are shown, including peaks and signed residuals.'}{!staticAvailable?' No static template saved for this inspection.':''}</p>{shownAutos.length?<Panels inputs={inputs} arrangement={arrangement} draw={draw('auto')}/>:<Notice>Select Raw, Static template or Static-subtracted to show spectra.</Notice>}</section>
      <section id="cal-cross" className="cal-diagnostic-section"><div className="cal-section-heading"><h3>Cross-correlations</h3><span>V(target, reference)</span></div><Choices label="Processing" options={STAGES} selected={stage} disabled={id=>!data.stages.includes(id)} onSelect={id=>{setStage(id);if(id==='geometric')setQuantity('phase');}}/><div className="cal-inspector-controls"><Choices label="Quantity" options={QUANTITIES} selected={quantity} onSelect={v=>setQuantity(v as Quantity)} disabled={()=>stage==='geometric'}/><Choices label="Cross-correlation view" options={[["dynamic","Dynamic spectrum"],["spectrum","Spectrum"]]} selected={view} onSelect={setView}/></div><p className="cal-caption">SVD-corrected applies the solved antenna phases, not a fitted straight-line delay. Expand a baseline to compare stages. {data.preview}.</p><Panels inputs={inputs.filter(a=>a.antenna!==reference)} arrangement={arrangement} draw={draw('cross')}/></section>
      <section id="cal-weights" className="cal-diagnostic-section"><div className="cal-section-heading"><h3>Antenna weights</h3><span>Phase-only SVD + amplitude experiment</span></div><Choices label="Amplitude scheme" options={SCHEMES.map(([id,name])=>[id,name+(variants.some(d=>!d.schemes[id])?' · unavailable':'')])} selected={scheme} onSelect={setScheme} disabled={id=>variants.some(d=>!d.schemes[id])}/><Choices label="Weight view" options={Object.entries(weightLabels)} selected={weightView} onSelect={setWeightView}/><AmplitudeHelp scheme={scheme}/><p className="cal-caption">Residual RMS summarizes corrected baseline phases for each antenna. Int8 preview uses scale 127 before beam steering. Amplitude choices are diagnostic previews; beam generation uses Uniform.</p><Panels inputs={inputs} arrangement={arrangement} draw={draw('weight')}/></section>
    </div>}
    {focus&&data&&(focus.kind==='cross'&&!other?<CrossZoom tag={tag} data={data} antenna={focus.antenna} quantity={quantity} zone={zone} onClose={()=>setFocus(null)}/>:<FigureZoom title={`${focus.antenna.station} · ${focus.kind==='auto'?'Autocorrelation':focus.kind==='cross'?'Subtraction comparison':weightLabels[weightView]}`} onClose={()=>setFocus(null)} controls={focus.kind==='auto'?autoControls:undefined}><p>{wiringLabel(focus.antenna)}</p>{focus.kind==='cross'?cross(focus.antenna):curve(focus.kind,focus.antenna)}</FigureZoom>)}
  </div>;
}
