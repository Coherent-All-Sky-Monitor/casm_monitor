import {useEffect,useState} from 'react';
import {api,Json,Notice,useResource} from './Workspace';
import CalibrationInspector from './CalibrationInspector';
import {FigureZoom} from './FigureZoom';
import {niceTicks,frequencyLabel} from './vis/PlotTicks';
import {number} from './vis/ArrayPlots';

const ROOT='/api/calibration/builds';
const LABELS:Record<string,string>={regular:'Without subtraction',static:'With subtraction'};
const COLORS:Record<string,string>={regular:'#2459a6',static:'#207c5b',deployed:'#c47722'};

function Rank1Figure({data}:{data:Json}){
  const curves:Json[]=data.curves;
  const frequencies=curves.flatMap(c=>c.freq_mhz),values=curves.flatMap(c=>c.values).filter(v=>v!==null&&Number.isFinite(v));
  const f0=Math.min(...frequencies),f1=Math.max(...frequencies),low=values.length?Math.min(...values):0,high=values.length?Math.max(...values):1;
  const padding=Math.max((high-low)*.05,.1),lo=Math.max(0,low-padding),hi=high+padding;
  const x=(f:number)=>72+(f-f0)/(f1-f0||1)*1005,y=(v:number)=>20+(hi-v)/(hi-lo)*240;
  return <div className="cal-rank1-figure"><svg viewBox="0 0 1100 322" role="img" aria-label="Rank-1 ratio versus frequency">
    <rect width="1100" height="322" fill="white"/>
    {niceTicks(lo,hi,7).map(v=><g key={v}><line x1="72" x2="1077" y1={y(v)} y2={y(v)} stroke="#e4e8ed"/><text x="63" y={y(v)+4} textAnchor="end">{number(v)}</text></g>)}
    {niceTicks(f0,f1,15).map(f=><g key={f}><line x1={x(f)} x2={x(f)} y1="260" y2="265" stroke="#526477"/><text x={x(f)} y="284" textAnchor="middle">{frequencyLabel(f)}</text></g>)}
    <path d="M72,20V260H1077" fill="none" stroke="#526477"/>
    {curves.map(c=>{let path='',connected=false;c.values.forEach((v:number|null,i:number)=>{if(v===null){connected=false;return;}path+=`${connected?'L':'M'}${x(c.freq_mhz[i]).toFixed(2)},${y(v).toFixed(2)} `;connected=true;});return <path key={c.key} d={path} fill="none" stroke={COLORS[c.key]} strokeWidth="1"/>;})}
    <text x="575" y="312" textAnchor="middle">Frequency (MHz)</text><text transform="translate(20 140) rotate(-90)" textAnchor="middle">Rank-1 ratio (σ₁ / σ₂)</text>
  </svg><div className="cal-rank1-key">{curves.map(c=><span key={c.key}><i style={{background:COLORS[c.key]}}/><strong>{c.label}</strong> · full-band median {number(c.median)}<small>{c.source_window?.[0]?.slice(0,10)??'Solve date unavailable'} · {c.antennas.length} antennas</small></span>)}</div></div>;
}

export function CalibrationRank1({tag}:{tag:string}){
  const {data,error}=useResource(`${ROOT}/${tag}/rank1`,10000);
  const [zoom,setZoom]=useState(false);
  return <section id="cal-quality" className="cal-diagnostic-section"><div className="cal-section-heading"><h3>Rank-1 ratio versus frequency</h3><span>Regular · static-subtracted · current deployed</span></div>
    {error&&<Notice>{error}</Notice>}{data&&data.curves?.length>0?<button className="cal-plot-button cal-rank1-trigger" aria-label="Expand rank-1 comparison" onClick={()=>setZoom(true)}><Rank1Figure data={data}/></button>:<p>Reading saved rank-1 curves…</p>}
    {Object.entries(data?.unavailable??{}).map(([key,reason])=><p className="cal-caption" key={key}>{key==='deployed'?'Current deployed':LABELS[key]}: {String(reason)}.</p>)}
    <p className="cal-caption">Each curve uses its original solve data. The legend gives the solve date and antenna count.</p>
    {zoom&&data&&<FigureZoom title="Rank-1 ratio versus frequency" onClose={()=>setZoom(false)}><Rank1Figure data={data}/></FigureZoom>}
  </section>;
}

/** A family contains immutable solves; viewing a branch does not select it for later steps. */
export default function CalibrationSolutions({tag,detail,disabled,onPrepare,onUse,onAction,onTrial}:{
  tag:string;detail:Json;disabled:boolean;onPrepare:(staticOn:boolean)=>Promise<unknown>;
  onUse:(tag:string)=>void;onAction:(tag:string,action:string,values?:Json)=>unknown;
  onTrial:(tag:string,antennas:number[],ref:number)=>unknown;
}){
  const initial=detail.request.params.static_window?'static':'regular';
  const [view,setView]=useState(initial),[viewDetail,setViewDetail]=useState<Json|null>(null),[error,setError]=useState('');
  const {data:family,error:familyError}=useResource(`${ROOT}/${tag}/subtraction`,4000);
  const versions=family?.versions??{},both=versions.regular?.ready&&versions.static?.ready;
  const viewTag=view==='compare'?versions.regular?.ready?versions.regular.tag:tag:versions[view]?.tag??(view===initial?tag:'');
  const compareTag=view==='compare'&&both?versions.static.tag:undefined;
  const [otherRevision,setOtherRevision]=useState('');
  useEffect(()=>{let active=true;setError('');
    if(!viewTag)return;
    if(viewTag===tag)return;
    api(`${ROOT}/${viewTag}`).then(d=>{if(active)setViewDetail(d);}).catch(e=>{if(active)setError(e.message);});
    return()=>{active=false;};
  },[tag,viewTag,detail]);
  useEffect(()=>{let active=true;if(compareTag)api(`${ROOT}/${compareTag}`).then(d=>{if(active)setOtherRevision((d.actions??[]).map((a:Json)=>a.action_id).join(','));}).catch(e=>{if(active)setError(e.message);});return()=>{active=false;};},[compareTag,detail]);
  const showing=viewTag===tag?detail:viewDetail?.request.params.tag===viewTag?viewDetail:null;
  const missing=['regular','static'].filter(key=>!versions[key]?.ready);
  return <><section className="cal-card cal-subtraction-selector">
    <div className="cal-section-heading"><h3>Static sky subtraction</h3><span>Selected for next steps: {LABELS[initial]}</span></div>
    <div className="choice-row" role="group" aria-label="Static sky comparison">{[['regular','Without subtraction'],['static','With subtraction'],['compare','Compare']].map(([key,label])=><button key={key} aria-pressed={view===key} className={view===key?'active':''} onClick={()=>setView(key)}>{label}</button>)}</div>
    <div className="cal-solution-actions">{(['regular','static'] as const).map(key=>{const version=versions[key];return <div key={key}><strong>{LABELS[key]}</strong>{version?.ready?<button disabled={disabled||version.pending||version.tag===tag} onClick={()=>onUse(version.tag)}>{version.tag===tag?'Selected':'Use this solution'}</button>:<button disabled={disabled||version?.pending||!family?.can_prepare} onClick={()=>void onPrepare(key==='static')}>{version?.pending?'Preparing…':`Prepare ${key==='static'?'with':'without'} subtraction`}</button>}</div>;})}</div>
    {!family?.can_prepare&&family&&missing.length>0&&<p className="cal-caption">A reviewed night window is needed to prepare the comparison. Choose it under Prepare.</p>}
    {view==='compare'&&!both&&<p className="cal-caption">Prepare the missing solution once. The paired plots then load from saved results.</p>}
    {(familyError||error)&&<Notice>{familyError||error}</Notice>}
  </section>
  {showing?<CalibrationInspector tag={viewTag} detail={showing} disabled={disabled||Boolean(versions[view]?.pending)}
    comparison={compareTag?{tag:compareTag,revision:otherRevision}:undefined}
    onAction={(action,values)=>onAction(viewTag,action,values)} onTrial={(ants,ref)=>onTrial(viewTag,ants,ref)}/>:<div className="cal-empty">{viewTag?'Loading saved solution…':'Prepare this solution to inspect it.'}</div>}
  <CalibrationRank1 tag={tag}/></>;
}
