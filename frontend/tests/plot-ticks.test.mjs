import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source=readFileSync(new URL('../src/components/vis/PlotTicks.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const {niceTicks,frequencyLabel,timeTicks}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));

test('default frequency band gets round ten-MHz ticks, finer when enlarged',()=>{
  assert.deepEqual(niceTicks(390.76,484.27,11),[400,410,420,430,440,450,460,470,480]);
  assert.equal(niceTicks(390.76,484.27,22).length,18);
});
test('narrow bands and signed ranges have distinct in-range labels',()=>{
  for(const [lo,hi] of [[412.01,412.09],[-3,7],[0,0.001],[390.76,484.27]]){
    const ticks=niceTicks(lo,hi,10);
    assert(ticks.every(t=>t>=lo&&t<=hi));
    assert.equal(new Set(ticks.map(frequencyLabel)).size,ticks.length);
  }
  assert.deepEqual(niceTicks(1,1),[]);
  assert.deepEqual(niceTicks(NaN,2),[]);
});
test('time labels fit the available width and use round local clock hours',()=>{
  const lo=Date.parse('2026-09-23T20:03:00Z')/1000,hi=lo+86400;
  const narrow=timeTicks(lo,hi,300,'America/Los_Angeles');
  const wide=timeTicks(lo,hi,1000,'America/Los_Angeles');
  assert(narrow.length>=4&&wide.length>narrow.length);
  for(const t of narrow){
    const h=Number(new Intl.DateTimeFormat('en-US',{timeZone:'America/Los_Angeles',hour:'2-digit',hourCycle:'h23'}).format(t*1000));
    assert.equal(h%6,0);
    assert(t>=lo&&t<=hi);
  }
});
test('short windows and DST crossings keep monotonic, bounded timestamps',()=>{
  for(const [lo,span] of [[Date.parse('2026-09-24T20:03:00Z')/1000,1800],[Date.parse('2026-11-01T05:00:00Z')/1000,86400]]){
    const ticks=timeTicks(lo,lo+span,350,'America/Los_Angeles');
    assert(ticks.length>=2);
    assert(ticks.every((t,i)=>t>=lo&&t<=lo+span&&(!i||t>ticks[i-1])));
  }
});
