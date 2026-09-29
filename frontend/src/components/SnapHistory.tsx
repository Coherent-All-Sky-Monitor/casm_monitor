import {useEffect, useRef, useState} from 'react';
import {api, Json, Notice, stamp} from './Workspace';
import {FigureZoom} from './FigureZoom';
import {SnapPlot} from './SnapPlot';
import {snapRange, SNAP_REFERENCE_POWER} from './snapPower';

export type SnapSnapshot = {day:string; ids:number[]; at:number; start:number; boards:string[];lst?:string|null};
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

export async function previousSnapshot(value:SnapSnapshot):Promise<SnapSnapshot|null> {
  const catalog=await api(`/api/snap-workspace/spectra-catalog?day=${value.day}`);
  const index=catalog.snapshots.findIndex((s:SnapSnapshot)=>s.ids.some(id=>value.ids.includes(id)));
  if(index>0)return {...catalog.snapshots[index-1],day:value.day};
  const dates=await api('/api/snap-workspace/spectra-days');
  const day=dates.days.find((d:Json)=>d.day<value.day)?.day;
  if(!day)return null;
  const earlier=await api(`/api/snap-workspace/spectra-catalog?day=${day}`);
  return earlier.snapshots.length?{...earlier.snapshots[earlier.snapshots.length-1],day}:null;
}

export function SavedReadPicker({label,value,onChange}:{label:string;value:SnapSnapshot|null;onChange:(s:SnapSnapshot|null)=>void}) {
  const dates=useSnapResource('/api/snap-workspace/spectra-days',60000);
  const [day,setDay]=useState(value?.day??'');
  const [error,setError]=useState('');
  const [stepping,setStepping]=useState(false);
  const [draft,setDraft]=useState<number|null>(null);
  const timer=useRef(0), sequence=useRef(0), preferredTime=useRef<number|null>(null);
  const selectedDay=day||value?.day||dates.data?.days?.[0]?.day||'';
  const catalog=useSnapResource(selectedDay?`/api/snap-workspace/spectra-catalog?day=${selectedDay}`:null,selectedDay===snapDay(Date.now()/1000)?60000:0);
  const snapshots:SnapSnapshot[]=(catalog.data?.snapshots??[]).map((s:SnapSnapshot)=>({...s,day:selectedDay}));
  const index=snapshots.findIndex(s=>s.ids.join(',')===value?.ids.join(','));
  useEffect(()=>()=>{window.clearTimeout(timer.current);sequence.current++;},[]);
  useEffect(()=>{if(value?.day)setDay(value.day);},[value?.day]);
  useEffect(()=>setDraft(null),[value,catalog.data]);
  useEffect(()=>{
    if(!value&&snapshots.length){
      const target=preferredTime.current;
      const picked=target===null?snapshots[snapshots.length-1]:snapshots.reduce((best,s)=>Math.abs(s.at%86400-target)<Math.abs(best.at%86400-target)?s:best);
      onChange(picked);
    }
  },[value,catalog.data]);
  const days:Json[]=dates.data?.days??[];
  const older=days.find(d=>d.day<selectedDay)?.day;
  const newer=[...days].reverse().find(d=>d.day>selectedDay)?.day;
  const changeDay=(next:string)=>{
    window.clearTimeout(timer.current);sequence.current++;
    preferredTime.current=value?value.at%86400:preferredTime.current;
    setDraft(null);setError('');setDay(next);onChange(null);
  };
  const step=async(direction:number)=>{
    window.clearTimeout(timer.current);setDraft(null);
    setError('');
    if(snapshots[index+direction]){onChange(snapshots[index+direction]);return;}
    const nextDay=direction>0?newer:older;
    if(!nextDay)return;
    const ticket=++sequence.current;
    setStepping(true);
    try {
      const data=await api(`/api/snap-workspace/spectra-catalog?day=${nextDay}`);
      const reads=data.snapshots;
      if(ticket===sequence.current&&reads.length)onChange({...reads[direction>0?0:reads.length-1],day:nextDay});
    }catch(e){if(ticket===sequence.current)setError((e as Error).message);}finally{setStepping(false);}
  };
  const shown=draft??index, time=(s:SnapSnapshot)=>stamp(s.at).slice(11,16);
  const scrub=(position:number)=>{
    setDraft(position);window.clearTimeout(timer.current);
    timer.current=window.setTimeout(()=>onChange(snapshots[position]),160);
  };
  return <div className="snap-saved-picker" aria-label={label}>
    <div className="snap-day-picker"><label htmlFor={`snap-date-${label}`}>{label==='Comparison'?'Compare with':'Date'} · UTC</label><div>
      <button aria-label={`${label} previous day`} title="Previous day with saved data" disabled={stepping||!older} onClick={()=>changeDay(older)}>‹</button>
      <input id={`snap-date-${label}`} type="date" aria-label={`${label} date (UTC)`} disabled={stepping} min={days[days.length-1]?.day} max={days[0]?.day} value={selectedDay} onChange={e=>{if(e.target.value)changeDay(e.target.value);}}/>
      <button aria-label={`${label} next day`} title="Next day with saved data" disabled={stepping||!newer} onClick={()=>changeDay(newer)}>›</button>
    </div></div>
    <div className="snap-time-picker"><div className="snap-time-heading"><strong>{snapshots[shown]?`${time(snapshots[shown])} UTC`:'—'}</strong><small>{snapshots[shown]?`Read ${shown+1} of ${snapshots.length} · ${snapshots[shown].boards.length}/4 SNAPs`:catalog.data?'No saved reads on this date':'Loading saved times…'}</small></div>
      <div className="snap-scrubber"><button aria-label={`${label} previous read`} title="Previous saved read" disabled={stepping||index<0||(index===0&&!older)} onClick={()=>void step(-1)}>‹</button>
        <input type="range" aria-label={`${label} saved time`} aria-valuetext={snapshots[shown]?stamp(snapshots[shown].at):'No saved read'} disabled={stepping||snapshots.length<2} min={0} max={Math.max(0,snapshots.length-1)} step={1} value={Math.max(0,shown)} onChange={e=>scrub(Number(e.target.value))}/>
        <button aria-label={`${label} next read`} title="Next saved read" disabled={stepping||index<0||(index===snapshots.length-1&&!newer)} onClick={()=>void step(1)}>›</button></div>
      <div className="snap-time-labels">{snapshots.length>0&&[0,...(snapshots.length>2?[Math.floor((snapshots.length-1)/2)]:[]),...(snapshots.length>1?[snapshots.length-1]:[])].map(i=><button key={i} onClick={()=>{window.clearTimeout(timer.current);setDraft(null);onChange(snapshots[i]);}}>{time(snapshots[i])}</button>)}</div>
    </div>
    {[dates.error,catalog.error,error].filter(Boolean).map((e,i)=><Notice key={i}>{e}</Notice>)}
  </div>;
}

export function SnapChanges({data,error,onInspect}:{data:Json|null;error:string;onInspect:(item:Json)=>void}) {
  const inputs:Json[]=(data?.inputs??[]).filter((i:Json)=>i.functional);
  const changed=inputs.filter(i=>['attention','watch','config','unknown'].includes(i.state)).sort((a,b)=>['attention','watch','config','unknown'].indexOf(a.state)-['attention','watch','config','unknown'].indexOf(b.state));
  const fine=inputs.filter(i=>i.state==='info');
  const checked=inputs.filter(i=>['stable','info','watch','attention'].includes(i.state)).length;
  const alerts=inputs.filter(i=>['watch','attention'].includes(i.state)).length;
  return <section className="snap-changes" aria-label="Live spectrum changes">
    <div className="snap-changes-heading"><strong>Latest spectrum check <small>· all wired</small></strong><span>{error?'Unavailable':!data?'Checking saved reads…':`${checked}/${inputs.length} checked`}</span></div>
    {data&&!error&&!alerts&&<p className="snap-check-summary">{checked?'No large power / shape changes in checked inputs.':'Not enough comparable saved data.'}</p>}
    {!!error&&<Notice>{error}</Notice>}
    <div className="snap-change-list">{changed.map(i=><button key={`${i.ip}/${i.adc}`} className={`snap-change ${i.state}`} disabled={!i.current_id} onClick={()=>onInspect(i)} title={`${i.reasons?.join(' · ')||i.label}. ${stamp(i.ts)}`}>
      <b>{i.station??`SNAP ${i.snap} ADC ${i.adc}`}</b><span>{i.state==='attention'?'Repeated · ':''}{i.label}</span>
      {i.power_delta_db!=null&&<small>Band power {i.power_delta_db>0?'+':''}{i.power_delta_db.toFixed(2)} dB</small>}
    </button>)}</div>
    {!!fine.length&&<details className="snap-fine-details"><summary>Fine-channel variation · {fine.length} inputs <span>· details</span></summary><p>Small parts of the band differ. These are not antenna-fault alerts.</p><div className="snap-fine-list">{fine.map(i=><button key={`${i.ip}/${i.adc}`} onClick={()=>onInspect(i)}><b>{i.station}</b><span>{i.spike_channels}/4096 channels · {(i.spike_channels/4096*100).toFixed(2)}% of band</span><small>Band power {i.power_delta_db>0?'+':''}{i.power_delta_db?.toFixed(2)} dB</small></button>)}</div></details>}
    <details><summary>Check details</summary><p>Same-setting 24-hour reference; at least six reads spanning four hours. Large-change thresholds: band power ≥6 dB, broad shape ≥3 dB on 10% of channels, or 1% newly zero channels. Historical variation raises the dB limits. Red requires repetition at the hourly cadence. Fine-channel excursions are informational only. No hardware fault is inferred.</p><p>{stamp(data?.checked_at)} · click an entry for the exact reference and difference plot.</p></details>
  </section>;
}

export function ChangeEvidence({item,onClose}:{item:Json;onClose:()=>void}) {
  const comparable=['stable','info','watch','attention'].includes(item.state);
  const {data,error}=useSnapResource(comparable?`/api/snap-workspace/change-detail?ids=${item.sample_ids.join(',')}&adc=${item.adc}`:null);
  const range=snapRange([...(data?.current_spectra_db??[]),...(data?.reference_spectra_db??[])],SNAP_REFERENCE_POWER);
  const differences:number[]=(data?.difference_db??[]).filter((v:number|null)=>v!==null);
  const extent=Math.max(1,...differences.map(Math.abs))*1.08;
  const markers:number[]=(data?.peaks??[]).map((p:Json)=>p.frequency_mhz);
  return <FigureZoom title={`${item.station??`ADC ${item.adc}`} · saved spectrum check`} onClose={onClose}><div className="snap-expanded snap-change-evidence">
    {!comparable?<Notice>{item.label} · no current comparison available</Notice>:error?<Notice>{error}</Notice>:!data?<p>Loading saved evidence…</p>:!data.reference_spectra_db?<Notice>{data.label} · no reference comparison available</Notice>:<>
      <h4>{data.label}</h4><p>{stamp(data.ts)} · band power {data.power_delta_db==null?'has no finite dB value':`${data.power_delta_db>0?'+':''}${data.power_delta_db.toFixed(2)} dB vs reference history`}{data.spike_channels?` · fine-channel excursions: ${data.spike_channels}/4096 (${(data.spike_channels/4096*100).toFixed(2)}% of band)`:''}</p>
      {!!data.new_zero_channels&&<p>{data.new_zero_channels}/4096 channels are now zero where the reference had positive power.</p>}
      <p>Blue: selected read · orange dashed: exact per-channel median of {data.baseline_reads} earlier reads<br/>{stamp(data.baseline_start)} to {stamp(data.baseline_end)}</p>
      <SnapPlot freq={data.freq_mhz} values={data.current_spectra_db} reference={data.reference_spectra_db} range={range} markers={markers}/>
      <h4>Selected read − reference</h4><SnapPlot freq={data.freq_mhz} values={data.difference_db} range={[-extent,extent]} powerReference={1} difference markers={markers}/>
      {!!data.peaks?.length&&<div className="snap-peak-table"><table><thead><tr><th>Frequency</th><th>Blue − orange</th><th>Above threshold before</th></tr></thead><tbody>{data.peaks.map((p:Json)=><tr key={p.channel}><td>{p.frequency_mhz.toFixed(3)} MHz</td><td>{p.difference_db>0?'+':''}{p.difference_db.toFixed(2)} dB</td><td>{p.prior_reads}/{data.baseline_reads} reference reads</td></tr>)}</tbody></table></div>}
      <p>Channel differences above are measured directly between these traces. The shape check removes a uniform {data.shape_offset_db.toFixed(2)} dB offset; band power compares full-band linear means with their historical median. A zero channel has no finite dB and is counted separately. Cause not determined.</p>
    </>}
  </div></FigureZoom>;
}
