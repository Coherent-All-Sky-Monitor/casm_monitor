import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
const source=readFileSync(new URL('../src/lib/readCache.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2020}}).outputText;
const {ReadCache}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));

test('simultaneous consumers share one read; fresh return needs no download',async()=>{
  const cache=new ReadCache();let finish,calls=0;
  const loader=()=>{calls++;return new Promise(resolve=>{finish=resolve;});};
  const a=cache.read('same',loader),b=cache.read('same',loader);
  assert.equal(a,b);finish({value:3});assert.deepEqual(await a,{value:3});
  assert.deepEqual(await cache.read('same',loader,30000),{value:3});assert.equal(calls,1);
});
test('exact keys keep dates and quantities separate; forced refresh replaces data',async()=>{
  const cache=new ReadCache();await cache.read('day1-phase',async()=>1);
  assert.equal(cache.peek('day2-phase'),undefined);assert.equal(cache.peek('day1-amp'),undefined);
  assert.equal(await cache.read('day1-phase',async()=>2,0),2);
  assert.equal(cache.peek('day1-phase').data,2);
});
test('failed refresh keeps successful data and timestamp, exposes failure, retries',async()=>{
  const cache=new ReadCache();await cache.read('health',async()=>({state:'ok'}));
  const at=cache.peek('health').at;
  await assert.rejects(cache.read('health',async()=>{throw Error('offline');}));
  assert.equal(cache.peek('health').at,at);assert.equal(cache.error('health'),'offline');
  assert.deepEqual(await cache.read('health',async()=>({state:'unknown'}),30000),{state:'unknown'});
  assert.equal(cache.error('health'),'');
});
test('LRU and byte budgets evict oldest responses, not the selected recent view',async()=>{
  const cache=new ReadCache(2,100);
  await cache.read('a',async()=>1);await cache.read('b',async()=>2);cache.peek('a');
  await cache.read('c',async()=>3);assert.equal(cache.peek('b'),undefined);assert.equal(cache.peek('a').data,1);
  await cache.read('large',async()=>'x'.repeat(100));assert.equal(cache.peek('large'),undefined);
  assert.equal(cache.peek('c').data,3);
});
test('expired entries revalidate; failures without a saved read are not cached successes',async()=>{
  const cache=new ReadCache();
  await assert.rejects(cache.read('missing',async()=>{throw Error('gone');}));assert.equal(cache.peek('missing'),undefined);
  await cache.read('key',async()=>1);assert.equal(await cache.read('key',async()=>2,-1),2);
});
