export const classifications = [
  {value:'frb', label:'FRB', hint:'A plausible dispersed burst for follow-up.'},
  {value:'pulsar', label:'Pulsar', hint:'A pulse supported by a pulsar association or follow-up.'},
  {value:'solar', label:'Solar', hint:'Evidence supports a solar origin.'},
  {value:'rfi', label:'RFI', hint:'The triggering event appears to be interference.'},
  {value:'noise', label:'Noise', hint:'Usable data with no convincing event at the trigger.'},
  {value:'unsure', label:'Unsure', hint:'Needs more investigation.'},
] as const;
export type Classification = typeof classifications[number]['value'];
export const candidateRefreshMs = 5000;
export const rfiMorphologies = [
  {value:'narrowband', label:'Narrowband', hint:'Confined to a small part of the frequency band.'},
  {value:'broadband', label:'Broadband', hint:'Extends across much of the frequency band.'},
  {value:'bursty', label:'Bursty', hint:'Short-lived flashes or bursts in time.'},
  {value:'frequency_comb', label:'Frequency comb', hint:'Multiple regularly spaced lines in frequency.'},
  {value:'swept', label:'Swept / drifting', hint:'The interfering frequency changes with time.'},
  {value:'repeating', label:'Repeating in time', hint:'A pattern recurs in time; it need not be strictly periodic.'},
] as const;
export type Reviewer = {id:string; name:string};
export type Vote = {
  candidate:string; reviewer_id:string; reviewer:string; revision:number; created_utc:string;
  label:Classification|null; quality:'usable'|'cannot_assess'|'unreviewed';
  morphology:string[]; rfi_present:boolean; note:string; evidence_id:string|null;
  others_revealed:boolean; action:'assess'|'undo';
};
export type AnnotationOverview = {reviewers:Reviewer[]; votes:Vote[]; csrf_token:string; writes_enabled:boolean};
export type ReviewPlot = {filename:string; sha256:string; url:string; retained?:boolean};
export type ReviewDetail = {
  event:Record<string,unknown>; plots:ReviewPlot[]; votes:Vote[]; history:Vote[]; csrf_token:string;
};
const base = '/api/candidate-annotations';
export async function annotationGet<T>(path=''):Promise<T> {
  const response = await fetch(base+path,{cache:'no-store'});
  if(!response.ok)throw new Error(`Could not load annotations (${response.status}). Try again.`);
  return response.json();
}

/** Small per-tab metadata cache. Exact PNG URLs include their content hash.
 * Saves still verify the plot hash and expected revision on the server. */
export function createReviewCache(
  loader:(name:string)=>Promise<ReviewDetail> = name=>annotationGet('/events/'+encodeURIComponent(name)),
  now:()=>number = Date.now, limit=8, ttlMs=60_000,
) {
  type Entry = {value?:ReviewDetail; at:number; pending?:Promise<ReviewDetail>};
  const entries=new Map<string,Entry>();
  const touch=(name:string,entry:Entry)=>{
    entries.delete(name);entries.set(name,entry);
    while(entries.size>limit)entries.delete(entries.keys().next().value!);
  };
  return {
    peek(name:string):ReviewDetail|null {
      const entry=entries.get(name);
      return entry?.value&&now()-entry.at<ttlMs?entry.value:null;
    },
    get(name:string,force=false):Promise<ReviewDetail> {
      const old=entries.get(name);
      if(!force&&old){
        touch(name,old);
        if(old.pending)return old.pending;
        if(old.value&&now()-old.at<ttlMs)return Promise.resolve(old.value);
      }
      const entry:Entry={at:0};touch(name,entry);
      entry.pending=loader(name).then(value=>{
        entry.value=value;entry.at=now();entry.pending=undefined;return value;
      },error=>{if(entries.get(name)===entry)entries.delete(name);throw error;});
      return entry.pending;
    },
    updateVotes(name:string,votes:Vote[]) {
      const entry=entries.get(name);
      if(entry?.value)entry.value={...entry.value,votes:mergeVotes(entry.value.votes,votes)};
    },
  };
}
export const reviewCache=createReviewCache();
const warmImages=new Map<string,HTMLImageElement>();
export async function prefetchReview(name:string):Promise<void> {
  const detail=await reviewCache.get(name),url=detail.plots[0]?.url;
  if(!url||typeof Image==='undefined'||warmImages.has(url))return;
  const image=new Image();image.decoding='async';image.src=url;warmImages.set(url,image);
  while(warmImages.size>4)warmImages.delete(warmImages.keys().next().value!);
  try{await image.decode();}catch{warmImages.delete(url);}
}
export async function annotationPost<T>(path:string, body:unknown, csrf:string):Promise<T> {
  const send=(token:string)=>fetch(base+path,{method:'POST',headers:{'Content-Type':'application/json','X-CASM-Workspace':'1','X-CASM-Annotations-CSRF':token},body:JSON.stringify(body)});
  let response=await send(csrf);
  // A monitor restart rotates its token. Reuse the request ID when retrying.
  if(response.status===403){const fresh=await annotationGet<AnnotationOverview>();response=await send(fresh.csrf_token);}
  const result=await response.json();
  if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:`Save failed (${response.status}). Please retry.`);
  return result;
}
export function voteText(v:Vote):string {
  return v.quality==='cannot_assess'?'Cannot assess':v.quality==='unreviewed'?'Unreviewed':classifications.find(c=>c.value===v.label)?.label??'Unsure';
}
export function mergeVotes(current:Vote[], incoming:Vote[]):Vote[] {
  const byKey=new Map(current.map(v=>[`${v.candidate}/${v.reviewer_id}`,v]));
  for(const v of incoming){const key=`${v.candidate}/${v.reviewer_id}`;if((byKey.get(key)?.revision??0)<=v.revision)byKey.set(key,v);}
  return [...byKey.values()];
}
export function disagreement(votes:Vote[]):boolean {
  return new Set(votes.filter(v=>v.quality==='usable'&&v.label!=='unsure').map(v=>v.label)).size>1;
}
