import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
const source=readFileSync(new URL('../src/lib/candidateAnnotations.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2020}}).outputText;
const {classifications,rfiMorphologies,candidateRefreshMs,createReviewCache,mergeVotes,disagreement,voteText}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
const vote=(reviewer_id,label,revision,quality='usable')=>({candidate:'261006abcdef',reviewer_id,label,revision,quality});
test('six agreed labels including FRB and Unsure',()=>{
  assert.deepEqual(classifications.map(c=>c.label),['FRB','Pulsar','Solar','RFI','Noise','Unsure']);
});
test('RFI descriptors distinguish frequency combs, bursts and temporal repetition',()=>{
  assert.deepEqual(rfiMorphologies.map(c=>c.value),['narrowband','broadband','bursty','frequency_comb','swept','repeating']);
  assert.equal(candidateRefreshMs,5000);
});
const detail=(votes=[])=>({event:{},plots:[],votes,history:[],csrf_token:'test'});
test('prefetched metadata is reused, concurrent reads deduplicate and votes update',async()=>{
  let calls=0,resolve;
  const cache=createReviewCache(()=>{calls++;return new Promise(r=>resolve=r);});
  const pending=cache.get('a');
  assert.equal(cache.get('a'),pending);
  const value=detail([vote('a','rfi',1)]);resolve(value);await pending;
  assert.equal(await cache.get('a'),value);
  assert.equal(calls,1);
  cache.updateVotes('a',[vote('a','noise',2)]);
  cache.updateVotes('a',[vote('a','frb',1)]);
  assert.equal(cache.peek('a').votes[0].label,'noise');
});
test('review cache expires, has a hard bound and permits fresh conflict reloads',async()=>{
  let clock=0,calls=0;
  const cache=createReviewCache(async()=>{calls++;return detail();},()=>clock,2,100);
  await cache.get('a');await cache.get('b');await cache.get('c');
  assert.equal(cache.peek('a'),null);
  await cache.get('b',true);assert.equal(calls,4);
  clock=101;assert.equal(cache.peek('b'),null);
  await cache.get('b');assert.equal(calls,5);
});
test('a forced fresh read wins over an earlier prefetch and errors can retry',async()=>{
  const pending=[];
  const cache=createReviewCache(()=>new Promise((resolve,reject)=>pending.push({resolve,reject})));
  const old=cache.get('a'),fresh=cache.get('a',true);
  const value=detail([vote('a','noise',2)]);
  pending[1].resolve(value);await fresh;
  pending[0].resolve(detail());await old;
  assert.equal(cache.peek('a'),value);
  const failure=cache.get('b');pending[2].reject(new Error('offline'));
  await assert.rejects(failure,/offline/);
  const retry=cache.get('b');pending[3].resolve(detail());await retry;
  assert.ok(cache.peek('b'));
});
test('late polling cannot roll back a saved label or undo',()=>{
  const original=vote('a','frb',1),updated=vote('a','noise',2),undone=vote('a',null,3,'unreviewed');
  assert.deepEqual(mergeVotes([updated],[original]),[updated]);
  assert.deepEqual(mergeVotes([undone],[original,updated]),[undone]);
  assert.equal(mergeVotes([updated],[vote('b','rfi',4)]).length,2);
});
test('disagreement distinguishes uncertainty and missing data',()=>{
  assert.equal(disagreement([vote('a','frb',1),vote('b','pulsar',2)]),true);
  assert.equal(disagreement([vote('a','frb',1),vote('b','unsure',2)]),false);
  assert.equal(disagreement([vote('a','frb',1),vote('b',null,2,'cannot_assess')]),false);
  assert.equal(voteText(vote('a',null,2,'cannot_assess')),'Cannot assess');
  assert.equal(voteText(vote('a',null,3,'unreviewed')),'Unreviewed');
});
