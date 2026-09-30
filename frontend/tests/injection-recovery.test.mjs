import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source=readFileSync(new URL('../src/components/injectionRecovery.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const {injectionRecovery}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
const evidence=(recovered=25,completed=25)=>({status:'ok',counts_complete:true,counts:{
  recovered,completed_fired:completed,missed_t1:completed-recovered,missed_t2:0,
  fire_failed:0,pending:0,unknown:0,
}});

test('100% nonempty recovery is green, including below the absolute count thresholds',()=>{
  for(const count of [1,11,12,17,18,24,25])
    assert.deepEqual(injectionRecovery(evidence(count,count),true),{health:'ok',label:'All recovered'});
});
test('partial recovery retains the operator count thresholds',()=>{
  for(const [count,health] of [[0,'silent'],[11,'silent'],[12,'orange'],[17,'orange'],[18,'yellow'],[24,'yellow']])
    assert.equal(injectionRecovery(evidence(count),true).health,health);
});
test('stale, incomplete, unavailable and empty evidence cannot turn green',()=>{
  assert.equal(injectionRecovery(evidence(),false).health,'late');
  assert.equal(injectionRecovery({...evidence(),counts_complete:false},true).health,'late');
  assert.equal(injectionRecovery({...evidence(),status:'unavailable'},true).health,'unknown');
  assert.equal(injectionRecovery(null,true).health,'unknown');
  assert.equal(injectionRecovery(evidence(0,0),true).health,'unknown');
});
test('failures, pending trials, unknown outcomes and inconsistent misses cannot turn green',()=>{
  for(const field of ['fire_failed','pending','unknown','missed_t1','missed_t2']) {
    const data=evidence();data.counts[field]=1;
    assert.notEqual(injectionRecovery(data,true).health,'ok',field);
  }
});
