import {useEffect, useState} from 'react';
import {api, Json, Notice, stamp} from './Workspace';

export type SnapSnapshot = {day:string; ids:number[]; at:number; start:number; boards:string[]};
export const snapDay=(ts:number)=>new Date(ts*1000).toISOString().slice(0,10);
export const snapshotUrl=(s:SnapSnapshot)=>`/api/snap-workspace/spectra?ids=${s.ids.join(',')}`;
const cache=new Map<string,Json>();

/** Keyed responses: never display the previous date under the next date's label. */
export function useSnapResource(url:string|null, interval=0) {
  const [result,setResult]=useState<{url:string;data:Json|null;error:string}|null>(null);
  useEffect(()=>{
    if(!url)return;
    let active=true;
    const read=async(cached=false)=>{
      try {
        const data=cached&&cache.has(url)?cache.get(url)!:await api(url);
        cache.delete(url);cache.set(url,data);
        while(cache.size>8)cache.delete(cache.keys().next().value!);
        if(active)setResult({url,data,error:''});
      } catch(error) {if(active)setResult({url,data:null,error:(error as Error).message});}
    };
    void read(!interval);
    const timer=interval?window.setInterval(()=>{if(!document.hidden)void read();},interval):0;
    return()=>{active=false;window.clearInterval(timer);};
  },[url,interval]);
  return result?.url===url?result:{data:null,error:''};
}

export async function snapshotForRead(id:number, ts:number):Promise<SnapSnapshot> {
  const day=snapDay(ts), catalog=await api(`/api/snap-workspace/spectra-catalog?day=${day}`);
  const found=catalog.snapshots.find((s:SnapSnapshot)=>s.ids.includes(id));
  if(!found)throw new Error('This saved read is no longer available');
  return {...found,day};
}

export function SavedReadPicker({label,value,onChange,preferPrevious=false}:{label:string;value:SnapSnapshot|null;onChange:(s:SnapSnapshot|null)=>void;preferPrevious?:boolean}) {
  const dates=useSnapResource('/api/snap-workspace/spectra-days',60000);
  const [day,setDay]=useState(value?.day??'');
  const [error,setError]=useState('');
  const [stepping,setStepping]=useState(false);
  const selectedDay=day||value?.day||dates.data?.days?.[0]?.day||'';
  const catalog=useSnapResource(selectedDay?`/api/snap-workspace/spectra-catalog?day=${selectedDay}`:null,selectedDay===snapDay(Date.now()/1000)?60000:0);
  const snapshots:SnapSnapshot[]=(catalog.data?.snapshots??[]).map((s:SnapSnapshot)=>({...s,day:selectedDay}));
  const index=snapshots.findIndex(s=>s.ids.join(',')===value?.ids.join(','));
  useEffect(()=>{if(value?.day)setDay(value.day);},[value?.day]);
  useEffect(()=>{
    if(!value&&snapshots.length)onChange(snapshots[Math.max(0,snapshots.length-(preferPrevious?2:1))]);
  },[value,catalog.data]);
  const dayIndex=(dates.data?.days??[]).findIndex((d:Json)=>d.day===selectedDay);
  const step=async(direction:number)=>{
    setError('');
    if(snapshots[index+direction]){onChange(snapshots[index+direction]);return;}
    const nextDay=dates.data?.days?.[dayIndex-direction]?.day;
    if(!nextDay)return;
    setStepping(true);
    try {
      const data=await api(`/api/snap-workspace/spectra-catalog?day=${nextDay}`);
      const reads=data.snapshots;
      if(reads.length)onChange({...reads[direction>0?0:reads.length-1],day:nextDay});
    }catch(e){setError((e as Error).message);}finally{setStepping(false);}
  };
  return <div className="snap-saved-picker" aria-label={label}>
    <label>{label} date · UTC<select disabled={stepping} aria-label={`${label} date (UTC)`} value={selectedDay} onChange={e=>{setDay(e.target.value);onChange(null);}}>
      {!dates.data?.days?.length&&<option value="">No saved dates</option>}
      {(dates.data?.days??[]).map((d:Json)=><option key={d.day} value={d.day}>{d.day} · {d.reads} board reads</option>)}
    </select></label>
    <label>Saved time<select disabled={stepping} aria-label={`${label} saved time`} value={index<0?'':index} onChange={e=>onChange(snapshots[Number(e.target.value)])}>
      {index<0&&<option value="">{catalog.data?'No saved reads':'Loading…'}</option>}
      {snapshots.map((s,i)=><option key={s.ids.join(',')} value={i}>{stamp(s.at).slice(11,19)} · {s.boards.length}/4 boards</option>)}
    </select></label>
    <div className="snap-step-buttons"><button aria-label={`${label} previous read`} disabled={stepping||index<0||(index===0&&dayIndex===(dates.data?.days?.length??0)-1)} onClick={()=>void step(-1)}>← Previous</button><button aria-label={`${label} next read`} disabled={stepping||index<0||(index===snapshots.length-1&&dayIndex===0)} onClick={()=>void step(1)}>Next →</button></div>
    {[dates.error,catalog.error,error].filter(Boolean).map((e,i)=><Notice key={i}>{e}</Notice>)}
  </div>;
}

export function SnapChanges({data,error,onInspect}:{data:Json|null;error:string;onInspect:(item:Json)=>void}) {
  const inputs:Json[]=(data?.inputs??[]).filter((i:Json)=>i.functional);
  const changed=inputs.filter(i=>i.state!=='stable').sort((a,b)=>['attention','watch','config','unknown'].indexOf(a.state)-['attention','watch','config','unknown'].indexOf(b.state));
  const stable=inputs.filter(i=>i.state==='stable').length;
  return <section className="snap-changes" aria-label="Live spectrum changes">
    <div className="snap-changes-heading"><strong>Changes <small>· live wired inputs</small></strong><span>{error?'Check unavailable':!data?'Checking saved reads…':`${stable}/${inputs.length} stable vs history`}</span></div>
    {!!error&&<Notice>{error}</Notice>}
    <div className="snap-change-list">{changed.map(i=><button key={`${i.ip}/${i.adc}`} className={`snap-change ${i.state}`} disabled={!i.current_id} onClick={()=>onInspect(i)} title={`${i.reasons?.join(' · ')||i.label}. ${stamp(i.ts)}`}>
      <b>{i.station??`SNAP ${i.snap} ADC ${i.adc}`}</b><span>{i.state==='attention'?'Needs attention · ':i.state==='watch'?'Watch · ':''}{i.label}</span>
      {i.peaks?.length&&i.label==='New strong spectral peak'?<small>{i.peaks[0].frequency_mhz.toFixed(2)} MHz · +{i.peaks[0].excess_db.toFixed(1)} dB vs shape reference</small>:i.power_delta_db!=null&&<small>Band power {i.power_delta_db>0?'+':''}{i.power_delta_db.toFixed(1)} dB</small>}
    </button>)}</div>
    <details><summary>How changes are checked</summary><p>Compared with the preceding 24-hour median in the same EQ/FFT setting. At least six reads spanning four hours are required. Power changes start at 6 dB, broad shape changes at 3 dB and new peaks at 12 dB; historical variation raises these limits. Red requires the same change in two reads 30–90 minutes apart. Missing data stays unknown. These flags do not diagnose the cause.</p><p>{stamp(data?.checked_at)} · click a badge for saved before/after spectra. The orange overlay is an actual representative earlier read, not the median used by the check.</p></details>
  </section>;
}
