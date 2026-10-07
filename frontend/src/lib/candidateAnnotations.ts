export const classifications = [
  {value:'frb', label:'FRB', hint:'A plausible dispersed burst for follow-up.'},
  {value:'pulsar', label:'Pulsar', hint:'A pulse supported by a pulsar association or follow-up.'},
  {value:'solar', label:'Solar', hint:'Evidence supports a solar origin.'},
  {value:'rfi', label:'RFI', hint:'The triggering event appears to be interference.'},
  {value:'noise', label:'Noise', hint:'Usable data with no convincing event at the trigger.'},
  {value:'unsure', label:'Unsure', hint:'Needs more investigation.'},
] as const;
export type Classification = typeof classifications[number]['value'];
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
