import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {Link, useSearchParams} from 'react-router-dom';
import type {CandEventRow} from '../lib/types';
import {formatUtcStamp} from '../lib/statusSentence';
import {annotationGet, annotationPost, classifications, disagreement, mergeVotes, voteText} from '../lib/candidateAnnotations';
import type {AnnotationOverview, Classification, ReviewDetail, Reviewer, Vote} from '../lib/candidateAnnotations';
import {CandidateGallery, CandidateImage} from './CandidateGallery';
import '../candidate-annotations.css';

const reviewerKey='casm.candidateReviewer.v1';
function remembered():Reviewer|null {try{return JSON.parse(localStorage.getItem(reviewerKey)??'null');}catch{return null;}}

export function CandidateBadges({votes=[], reviewerId}:{votes?:Vote[]; reviewerId?:string}) {
  const active=votes.filter(v=>v.quality!=='unreviewed');
  return <span className="candidate-badges">{active.map(v=><span key={v.reviewer_id} className={`candidate-badge class-${v.label??'unassessable'}${v.reviewer_id===reviewerId?' mine':''}`} title={`${v.reviewer} · ${voteText(v)} · ${formatUtcStamp(v.created_utc)}${v.note?' · '+v.note:''}`}>
    {v.reviewer} · {voteText(v)}</span>)}{disagreement(active)&&<span className="candidate-disagreement">Different labels</span>}</span>;
}

export function CandidateAnnotations({events,useMock}:{events:CandEventRow[];useMock:boolean}) {
  const [params,setParams]=useSearchParams();
  const classify=params.get('mode')==='classify';
  const requested=params.get('review');
  const [reviewer,setReviewer]=useState<Reviewer|null>(remembered);
  const [editingName,setEditingName]=useState(!reviewer),[name,setName]=useState(reviewer?.name??'');
  const [overview,setOverview]=useState<AnnotationOverview|null>(null),[votes,setVotes]=useState<Vote[]>([]);
  const [error,setError]=useState(''),[nameBusy,setNameBusy]=useState(false);
  const [filter,setFilter]=useState('unreviewed'),[label,setLabel]=useState(''),[search,setSearch]=useState('');
  const [queue,setQueue]=useState<CandEventRow[]>([]),[current,setCurrent]=useState<string|null>(requested);
  const [queueReady,setQueueReady]=useState(false);
  const queueSource=useRef(new Set<string>());
  const eventsRef=useRef(events);eventsRef.current=events;
  const [autoAdvance,setAutoAdvance]=useState(false),[blind,setBlind]=useState(true);
  const [busy,setBusy]=useState(false),[lastSaved,setLastSaved]=useState<string|null>(null);
  const [dirty,setDirty]=useState(false);
  const canLeave=()=>!busy&&(!dirty||window.confirm('Discard unsaved annotation details?'));
  const names=events.map(e=>e.name).join(',');
  const merge=useCallback((incoming:Vote[])=>setVotes(old=>mergeVotes(old,incoming)),[]);
  useEffect(()=>{
    if(useMock)return;
    let cancelled=false,running=false;
    const load=async()=>{
      if(running||document.hidden)return;running=true;
      try{const value=await annotationGet<AnnotationOverview>('?names='+encodeURIComponent(names));if(!cancelled){setOverview(value);merge(value.votes);setError('');}}
      catch(e){if(!cancelled)setError((e as Error).message);}
      finally{running=false;}
    };
    void load();const timer=setInterval(load,15000);
    return()=>{cancelled=true;clearInterval(timer);};
  },[names,useMock,merge]);
  const byName=useMemo(()=>{
    const out:Record<string,Vote[]>={};for(const v of votes)(out[v.candidate]??=[]).push(v);return out;
  },[votes]);
  const mine=(candidate:string)=>(byName[candidate]??[]).find(v=>v.reviewer_id===reviewer?.id);
  const matches=(event:CandEventRow)=>{
    const all=byName[event.name]??[], own=mine(event.name);
    return (!search||`${event.name} ${event.event_utc} ${event.beam}`.toLowerCase().includes(search.toLowerCase()))
      && (filter==='all'||filter==='unreviewed'&&(!own||own.quality==='unreviewed')
        ||filter==='mine'&&!!own&&own.quality!=='unreviewed'||filter==='disagreements'&&disagreement(all)
        ||filter==='unsure'&&all.some(v=>v.label==='unsure'||v.quality==='cannot_assess'));
  };
  const startQueue=(target?:string)=>{
    queueSource.current=new Set(eventsRef.current.map(e=>e.name));
    const list=eventsRef.current.filter(matches);
    if(target&&!list.some(e=>e.name===target)){const event=eventsRef.current.find(e=>e.name===target);if(event)list.unshift(event);}
    setQueue(list);setCurrent(target??list[0]?.name??null);setQueueReady(true);
    if(reviewer)requestAnimationFrame(()=>document.querySelector('.candidate-review-navigation')?.scrollIntoView({block:'start'}));
  };
  useEffect(()=>{
    if(classify&&!queueReady&&overview&&events.length)startQueue(requested??undefined);
    // Freeze the queue once opened; incoming events never move the active plot.
  });
  const changeMode=(value:boolean,target?:string)=>{
    if(!canLeave())return;
    setDirty(false);
    setParams(old=>{const next=new URLSearchParams(old);next.set('mode',value?'classify':'browse');if(target)next.set('review',target);else next.delete('review');return next;});
    if(value)startQueue(target);
  };
  const saveName=async()=>{
    if(!overview||!name.trim()||nameBusy)return;
    setNameBusy(true);setError('');
    try{
      const who=await annotationPost<Reviewer>('/reviewers',{name},overview.csrf_token);
      setReviewer(who);setName(who.name);setEditingName(false);
      try{localStorage.setItem(reviewerKey,JSON.stringify(who));}catch{/* Session still works if browser storage is disabled. */}
      setOverview(o=>o?{...o,reviewers:[...o.reviewers.filter(r=>r.id!==who.id),who]}:o);
      setQueueReady(false);
    }catch(e){setError((e as Error).message);}finally{setNameBusy(false);}
  };
  const selected=queue.find(e=>e.name===current),index=queue.findIndex(e=>e.name===current);
  const move=(delta:number)=>{if(queue[index+delta]&&canLeave()){setDirty(false);setCurrent(queue[index+delta].name);}};
  useEffect(()=>{
    if(!classify||index<0)return;
    const abort=new AbortController();
    const images:HTMLImageElement[]=[];
    for(const event of queue.slice(index+1,index+3)){
      fetch('/api/candidate-annotations/events/'+event.name,{signal:abort.signal,cache:'no-store'})
        .then(r=>r.ok?r.json():null).then((value:ReviewDetail|null)=>{if(!abort.signal.aborted&&value?.plots[0]){const image=new Image();image.src=value.plots[0].url;images.push(image);}}).catch(()=>{});
    }
    return()=>{abort.abort();};
  },[classify,index,queue]);
  const newCount=events.filter(e=>!queueSource.current.has(e.name)).length;
  const validReviewer=!!reviewer&&!!overview?.reviewers.some(r=>r.id===reviewer.id);
  const browseEvents=events.filter(e=>(!label||(byName[e.name]??[]).some(v=>v.label===label&&v.quality==='usable')));
  if(useMock)return <CandidateGallery events={events} useMock/>;
  return <div className="candidate-annotations">
    <div className="candidate-review-toolbar">
      <div className="candidate-modes" aria-label="Candidate mode"><button aria-pressed={!classify} disabled={busy} onClick={()=>changeMode(false)}>Browse</button><button aria-pressed={classify} disabled={busy} onClick={()=>changeMode(true)}>Classify</button></div>
      <div className="candidate-identity">{editingName||!validReviewer?<form onSubmit={e=>{e.preventDefault();void saveName();}}>
        <label>Your name<input aria-label="Reviewer name" list="candidate-reviewers" value={name} onChange={e=>setName(e.target.value)} placeholder="e.g. Vishnu" maxLength={60} disabled={busy}/></label>
        <datalist id="candidate-reviewers">{overview?.reviewers.map(r=><option key={r.id} value={r.name}/>)}</datalist>
        <button disabled={!overview?.writes_enabled||nameBusy||busy||!name.trim()}>{nameBusy?'Saving…':'Use name'}</button>
        {validReviewer&&<button type="button" onClick={()=>setEditingName(false)}>Cancel</button>}
      </form>:<><span>Reviewing as <strong>{reviewer?.name}</strong></span><button disabled={busy} onClick={()=>{if(canLeave()){setDirty(false);setEditingName(true);}}}>Change</button></>}</div>
      <a href="/api/candidate-annotations/export" className="candidate-export">Export labels</a>
    </div>
    {error&&<p role="alert" className="workspace-notice">{error}</p>}
    {overview&&!overview.writes_enabled&&<p className="workspace-notice">Annotations are read-only in this workspace.</p>}
    {!classify?<>
      <label className="candidate-label-filter">Human label<select aria-label="Human label" value={label} onChange={e=>setLabel(e.target.value)}><option value="">All labels</option>{classifications.map(c=><option key={c.value} value={c.value}>{c.label}</option>)}</select></label>
      <CandidateGallery events={browseEvents} useMock={false} votes={byName} reviewerId={reviewer?.id} onClassify={name=>changeMode(true,name)}/>
    </>:<>
      <div className="candidate-review-options"><label>Queue<select aria-label="Review queue" value={filter} disabled={busy} onChange={e=>{if(canLeave()){setDirty(false);setFilter(e.target.value);setQueueReady(false);setCurrent(null);setParams(old=>{const next=new URLSearchParams(old);next.delete('review');return next;});}}}>
        <option value="unreviewed">Unreviewed by me</option><option value="all">All candidates</option><option value="mine">My reviews</option><option value="disagreements">Different labels</option><option value="unsure">Needs investigation</option>
      </select></label><label><input type="checkbox" checked={autoAdvance} onChange={e=>setAutoAdvance(e.target.checked)}/>Advance after saving</label>
        <label><input type="checkbox" checked={blind} onChange={e=>setBlind(e.target.checked)}/>Hide other votes until I label</label>
        <button disabled={busy} onClick={()=>{if(canLeave()){setDirty(false);startQueue();}}}>Refresh queue{newCount>0?` · ${newCount} new in loaded list`:''}</button>
      </div>
      {lastSaved&&autoAdvance&&<p className="candidate-saved">Saved {lastSaved} · <button disabled={busy} onClick={()=>{if(canLeave()){setDirty(false);setCurrent(lastSaved);}}}>Return / undo</button></p>}
      <div className="candidate-workspace candidate-classify-workspace">
        <section className="candidate-review-main">{selected?<>
          <div className="candidate-review-navigation"><button disabled={busy||index<=0} onClick={()=>move(-1)}>← Previous</button><span>{index+1} / {queue.length}</span><button disabled={busy||index>=queue.length-1} onClick={()=>move(1)}>Next →</button></div>
          <CandidateReview key={`${selected.name}/${reviewer?.id??''}`} event={selected} reviewer={validReviewer?reviewer:null} writesEnabled={!!overview?.writes_enabled}
            votes={byName[selected.name]??[]} blind={blind} merge={merge} onBusy={setBusy} onDirty={setDirty} onPrevious={()=>move(-1)} onNext={()=>move(1)}
            onSaved={()=>{setDirty(false);setLastSaved(selected.name);if(autoAdvance&&queue[index+1])setCurrent(queue[index+1].name);}}/>
        </>:<div className="candidate-empty">{overview?'No candidates in this queue. Choose All candidates or refresh the queue.':'Loading annotations…'}</div>}</section>
        <aside className="candidate-sidebar"><h3>Review queue</h3><label className="candidate-filter">Find a candidate<input aria-label="Find review candidate" value={search} onChange={e=>setSearch(e.target.value)} placeholder="Name, UTC date, beam"/></label>
          <p className="candidate-list-note">Queue stays fixed while you review. {queue.filter(e=>{const v=mine(e.name);return v&&v.quality!=='unreviewed';}).length} reviewed by you.</p>
          <div className="candidate-list">{queue.filter(e=>!search||`${e.name} ${e.event_utc} ${e.beam}`.toLowerCase().includes(search.toLowerCase())).map(e=>{
            const own=mine(e.name), hide=blind&&(!own||own.quality==='unreviewed');
            return <button key={e.name} className={`candidate-list-item${e.name===current?' shown':''}`} disabled={busy} onClick={()=>{if(canLeave()){setDirty(false);setCurrent(e.name);}}}>
              <strong>{e.name}</strong><time>{formatUtcStamp(e.event_utc)}</time><span>S/N {e.snr.toFixed(1)} · DM {e.dm.toFixed(1)} · B{e.beam}</span>
              <CandidateBadges votes={hide?(own?[own]:[]):byName[e.name]} reviewerId={reviewer?.id}/>{(!own||own.quality==='unreviewed')&&<small>Unreviewed by you</small>}
            </button>;
          })}</div>
        </aside>
      </div>
    </>}
  </div>;
}

function CandidateReview({event,reviewer,writesEnabled,votes,blind,merge,onBusy,onDirty,onPrevious,onNext,onSaved}:{event:CandEventRow;reviewer:Reviewer|null;writesEnabled:boolean;votes:Vote[];blind:boolean;merge:(votes:Vote[])=>void;onBusy:(busy:boolean)=>void;onDirty:(dirty:boolean)=>void;onPrevious:()=>void;onNext:()=>void;onSaved:()=>void}) {
  const [detail,setDetail]=useState<ReviewDetail|null>(null),[error,setError]=useState(''),[status,setStatus]=useState('');
  const [plotIndex,setPlotIndex]=useState(0),[imageReady,setImageReady]=useState(false),[busy,setBusy]=useState(false);
  const [note,setNote]=useState(''),[morphology,setMorphology]=useState<string[]>([]),[rfiPresent,setRfiPresent]=useState(false),[extra,setExtra]=useState(false);
  const own=votes.find(v=>v.reviewer_id===reviewer?.id);
  const [expected,setExpected]=useState(own?.revision??0);
  const [revealed,setRevealed]=useState(!blind);
  const pending=useRef<{request:Record<string,unknown>;fingerprint:string}|null>(null);
  const saving=useRef(false);
  const [dirty,setDirty]=useState(false);
  const changed=()=>{setDirty(true);onDirty(true);setStatus('Unsaved details');};
  useEffect(()=>{
    const warn=(e:BeforeUnloadEvent)=>{if(dirty||saving.current){e.preventDefault();e.returnValue='';}};
    window.addEventListener('beforeunload',warn);return()=>window.removeEventListener('beforeunload',warn);
  },[dirty]);
  const reviewed=!!own&&own.quality!=='unreviewed';
  const showOthers=!blind||reviewed||revealed;
  useEffect(()=>{if(showOthers)setRevealed(true);},[showOthers]);
  const load=useCallback(async()=>{
    setError('');
    try{
      const result=await annotationGet<ReviewDetail>('/events/'+event.name);setDetail(result);merge(result.votes);
      const v=result.votes.find(v=>v.reviewer_id===reviewer?.id);
      setExpected(v?.revision??0);setNote(v?.note??'');setMorphology(v?.morphology??[]);setRfiPresent(v?.rfi_present??false);
      setStatus('');setDirty(false);onDirty(false);
      pending.current=null;
    }catch(e){setError((e as Error).message);}
  },[event.name,reviewer?.id,merge,onDirty]);
  useEffect(()=>{void load();},[load]);
  const plot=detail?.plots[plotIndex];
  useEffect(()=>{setImageReady(false);},[plot?.sha256]);
  const save=async(label:Classification|null,action='assess',cannot=false,detailsOnly=false)=>{
    if(saving.current||!reviewer||!detail||!writesEnabled)return;
    if(action==='assess'&&!cannot&&(!plot||!imageReady))return;
    const value={reviewer_id:reviewer.id,expected_revision:expected,label,action,quality:cannot?'cannot_assess':'usable',
      note,morphology:label==='rfi'||rfiPresent?morphology:[],rfi_present:rfiPresent,others_revealed:revealed||showOthers,
      plot:plot?.filename??null,plot_sha256:plot?.sha256??null};
    const fingerprint=JSON.stringify(value);
    if(pending.current?.fingerprint!==fingerprint)pending.current={fingerprint,request:{...value,request_id:crypto.randomUUID()}};
    saving.current=true;setBusy(true);onBusy(true);setError('');setStatus('Saving…');
    try{
      const result=await annotationPost<{votes:Vote[];revision:number;backup_warning?:string}>('/events/'+event.name,pending.current.request,detail.csrf_token);
      merge(result.votes);setExpected(result.revision);pending.current=null;
      const v=result.votes.find(v=>v.reviewer_id===reviewer.id);setNote(v?.note??'');setMorphology(v?.morphology??[]);setRfiPresent(v?.rfi_present??false);
      setDirty(false);onDirty(false);setStatus(action==='undo'?'Undone · saved':'Saved');if(result.backup_warning)setError(result.backup_warning);
      // Release navigation before optional advance; double clicks remain guarded synchronously.
      onBusy(false);if(action==='assess'&&!detailsOnly)onSaved();
    }catch(e){setError((e as Error).message);setStatus('Save failed');}
    finally{saving.current=false;setBusy(false);onBusy(false);}
  };
  const saveRef=useRef(save);saveRef.current=save;
  const previousRef=useRef(onPrevious),nextRef=useRef(onNext);previousRef.current=onPrevious;nextRef.current=onNext;
  useEffect(()=>{
    const key=(e:KeyboardEvent)=>{
      if(e.repeat||e.ctrlKey||e.metaKey||e.altKey||saving.current||document.querySelector('dialog[open]')||(e.target as HTMLElement).closest('input,textarea,select,[contenteditable="true"]'))return;
      if(/^[1-6]$/.test(e.key)){e.preventDefault();void saveRef.current(classifications[Number(e.key)-1].value);}
      else if(e.key==='ArrowRight'){e.preventDefault();nextRef.current();}
      else if(e.key==='ArrowLeft'){e.preventDefault();previousRef.current();}
      else if(e.key.toLowerCase()==='u'){e.preventDefault();void saveRef.current(null,'undo');}
    };
    document.addEventListener('keydown',key);return()=>document.removeEventListener('keydown',key);
  },[]);
  const disabled=busy||!reviewer||!writesEnabled||!detail;
  return <article className="candidate-review-card">
    <header><div><h2>{event.name}</h2><time>{formatUtcStamp(event.event_utc)}</time><p>S/N {event.snr.toFixed(1)} · DM {event.dm.toFixed(1)} pc cm⁻³ · Beam {event.beam}</p></div><Link to={`/cands/${event.name}`}>Event details →</Link></header>
    {detail&&detail.plots.length>1&&<label className="candidate-review-plot-select">Plot<select aria-label="Review plot" value={plotIndex} disabled={busy} onChange={e=>setPlotIndex(Number(e.target.value))}>{detail.plots.map((p,i)=><option key={p.sha256} value={i}>{p.filename}{p.retained?' · saved copy':''}</option>)}</select></label>}
    {plot?<CandidateImage key={plot.sha256} url={plot.url} name={event.name} filename={plot.filename} onReady={()=>setImageReady(true)}/>:<p className="candidate-missing">{detail?'No saved plot. You can mark this candidate Cannot assess.':'Loading candidate…'}</p>}
    <div className="candidate-classification-controls">
      {!reviewer&&<p>Enter your name above to start reviewing.</p>}
      <div className="candidate-classification-buttons">{classifications.map((c,i)=><button key={c.value} className={`class-${c.value}`} title={c.hint} disabled={disabled||!imageReady} aria-pressed={own?.label===c.value&&reviewed} onClick={()=>{if(c.value==='rfi')setExtra(true);void save(c.value);}}>{c.label}<kbd>{i+1}</kbd></button>)}</div>
      <div className="candidate-review-save"><span role="status" className={status==='Save failed'?'failed':status.startsWith('Sav')||status.includes('saved')?'saved':''}>{status||(reviewed?`Your label: ${voteText(own!)}`:'Choose a label')}</span>
        <button disabled={disabled||!reviewed} onClick={()=>void save(null,'undo')}>Undo</button>
        <button disabled={disabled} aria-pressed={own?.quality==='cannot_assess'} onClick={()=>void save(null,'assess',true)}>Cannot assess</button>
      </div>
      {error&&<p role="alert" className="candidate-review-error">{error} <button disabled={busy} onClick={()=>void load()}>Reload review</button></p>}
      <details open={extra} onToggle={e=>setExtra(e.currentTarget.open)}><summary>Details & note <span>optional</span></summary>
        <label><input type="checkbox" checked={rfiPresent} disabled={busy} onChange={e=>{setRfiPresent(e.target.checked);changed();}}/>RFI also present</label>
        <div className="candidate-morphology">{[['narrowband','Narrowband'],['broadband','Broadband'],['swept','Swept / drifting'],['repeating','Repeating']].map(([v,t])=><label key={v}><input type="checkbox" disabled={busy} checked={morphology.includes(v)} onChange={e=>{setMorphology(old=>e.target.checked?[...old,v]:old.filter(x=>x!==v));changed();}}/>{t}</label>)}</div>
        <label>Note<textarea aria-label="Annotation note" value={note} disabled={busy} onChange={e=>{setNote(e.target.value);changed();}} maxLength={2000} rows={2} placeholder="What stood out?"/></label>
        <button disabled={disabled||!reviewed||own?.quality==='usable'&&!imageReady} onClick={()=>void save(own?.label??null,'assess',own?.quality==='cannot_assess',true)}>Save details</button><small>Choose a label to save a new review. Save details updates your existing review.</small>
      </details>
      {showOthers?<CandidateBadges votes={votes} reviewerId={reviewer?.id}/>:<button className="candidate-reveal" onClick={()=>setRevealed(true)}>Show other reviewers’ labels</button>}
      <p className="candidate-review-help">1–6 label · ← → previous / next · U undo · Click the plot to zoom or download</p>
    </div>
  </article>;
}
