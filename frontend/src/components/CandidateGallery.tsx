import { useState } from 'react';
import { Link } from 'react-router-dom';
import { candPlotUrl } from '../lib/api';
import { mockCandPlotUrl } from '../lib/mockCands';
import { formatUtcStamp } from '../lib/statusSentence';
import type { CandEventRow } from '../lib/types';
import { FigureZoom } from './FigureZoom';
import '../candidate-gallery.css';

export function CandidateImage({url,name,filename}:{url:string;name:string;filename:string}) {
  const [open,setOpen]=useState(false),[failed,setFailed]=useState(false);
  return <>{failed?<p className="candidate-missing" role="status">Saved plot unavailable. The file may have moved or expired.</p>:
    <button className="candidate-image" aria-label={`Enlarge candidate ${name} ${filename}`} onClick={()=>setOpen(true)}>
      <img src={url} alt={`Candidate ${name} · ${filename}`} width="1600" height="1600" loading="lazy" decoding="async" onError={()=>setFailed(true)}/>
    </button>}
    {open&&<FigureZoom title={`Candidate ${name}`} onClose={()=>setOpen(false)} actions={<a href={url} target="_blank" rel="noreferrer">Open original PNG ↗</a>}><img src={url} alt={`Candidate ${name} · ${filename}`} draggable={false}/></FigureZoom>}
  </>;
}

export function CandidateGallery({events,useMock}:{events:CandEventRow[];useMock:boolean}) {
  const [count,setCount]=useState(12),[pinned,setPinned]=useState<string[]|null>(null),[focused,setFocused]=useState<string|null>(null),[search,setSearch]=useState('');
  const [files,setFiles]=useState<Record<string,string>>({});
  const available=events.filter(e=>e.plots?.length);
  const names=(pinned??available.map(e=>e.name)).filter(n=>events.some(e=>e.name===n)).slice(0,count);
  const shown=names.map(n=>events.find(e=>e.name===n)!);
  const results=events.filter(e=>`${e.name} ${e.event_utc} ${e.beam} ${e.label??''} ${e.outcome??''}`.toLowerCase().includes(search.toLowerCase()));
  const plotUrl=useMock?mockCandPlotUrl:candPlotUrl;
  const suffix=useMock?'?mock=1':'';
  const select=(name:string)=>{
    if(names.includes(name)){
      setPinned(names.filter(n=>n!==name));
      if(focused===name)setFocused(null);
      return;
    }
    setPinned([name,...names].slice(0,count));
    setFocused(name);
    requestAnimationFrame(()=>document.getElementById(`candidate-${name}`)?.scrollIntoView({block:'nearest',behavior:'smooth'}));
  };
  return <div className="candidate-workspace">
    <section className="candidate-gallery" aria-label="Candidate plot grid">
      <div className="candidate-grid-toolbar"><div><h2>Candidate plots</h2><p>{shown.length} shown · {available.length} with saved plots in the loaded list</p></div>
        <label>Plots in grid<select aria-label="Plots in grid" value={count} onChange={e=>{setCount(Number(e.target.value));setPinned(null);setFocused(null);}}>{[6,12,24,48].map(n=><option key={n} value={n}>{n}</option>)}</select></label>
        <button onClick={()=>{setPinned(null);setFocused(null);}}>Latest {count}</button>
      </div>
      {pinned!==null&&<p className="candidate-selection-note">Custom selection. New events remain in the list; Latest {count} restores the rolling grid.</p>}
      {!shown.length&&<p className="candidate-empty">No saved plots in this selection. Choose an event from the list for its status.</p>}
      <div className="candidate-plot-grid">{shown.map(e=>{
        const plots=e.plots??[],filename=plots.includes(files[e.name])?files[e.name]:plots.find(f=>f===`${e.name}.png`)??plots[0];
        return <article id={`candidate-${e.name}`} key={e.name} data-name={e.name} className={`candidate-card${focused===e.name?' focused':''}`}>
          <header><div><h3>{e.name}</h3><time>{formatUtcStamp(e.event_utc)}</time></div><button aria-label={`Hide candidate ${e.name}`} onClick={()=>setPinned(names.filter(n=>n!==e.name))}>×</button></header>
          <p className="candidate-card-metrics">S/N {e.snr.toFixed(1)} · DM {e.dm.toFixed(1)} pc cm⁻³ · Beam {e.beam}</p>
          {filename?<CandidateImage key={filename} url={plotUrl(e.name,filename)} name={e.name} filename={filename}/>:<p className="candidate-missing">No saved plot · {e.outcome??'plot not available'}</p>}
          <footer><Link to={`/cands/${encodeURIComponent(e.name)}${suffix}`}>Event details →</Link>{plots.length>1&&<label>Plot<select aria-label={`Plot for ${e.name}`} value={filename} onChange={v=>setFiles(f=>({...f,[e.name]:v.target.value}))}>{plots.map(p=><option key={p}>{p}</option>)}</select></label>}<span>{e.label??e.outcome??''}</span></footer>
        </article>;
      })}</div>
    </section>
    <aside className="candidate-sidebar" aria-label="Candidate selector"><details open><summary>Candidates <span>{events.length} loaded</span></summary>
      <label className="candidate-filter">Find a candidate<input type="search" value={search} onChange={e=>setSearch(e.target.value)} placeholder="Name, UTC date, beam or label"/></label>
      <p className="candidate-list-note">Click to show / hide · outlined rows are shown · newest first · refreshes every 15 s</p>
      <div className="candidate-list">{results.map(e=>{const index=names.indexOf(e.name);return <button key={e.name} data-name={e.name} className={`candidate-list-item${index>=0?' shown':''}${focused===e.name?' focused':''}`} aria-pressed={index>=0} onClick={()=>select(e.name)}>
        <span className="candidate-list-title"><strong>{e.name}</strong>{index>=0&&<b>Shown {index+1}</b>}</span><time>{formatUtcStamp(e.event_utc)}</time>
        <span>S/N {e.snr.toFixed(1)} · DM {e.dm.toFixed(1)} · B{e.beam}</span>
        <small>{e.label??e.outcome??'Unlabelled'}{!e.plots?.length?' · No plot':''}</small>
      </button>;})}{!results.length&&<p>No candidates match this search.</p>}</div>
      <p className="candidate-list-note">Up to 500 filtered events. No plot is not a non-detection.</p>
    </details></aside>
  </div>;
}
