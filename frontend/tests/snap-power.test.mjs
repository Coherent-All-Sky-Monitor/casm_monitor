import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source=readFileSync(new URL('../src/components/snapPower.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const {snapRange,SNAP_REFERENCE_POWER:reference}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));

test('prefer 65 dB when there is room above every channel',()=>{
  assert.equal(snapRange([-85,-60,-37],reference)[1],65);
});
test('saved peak needs 75 dB; never silently clip at 65',()=>{
  const [lo,hi]=snapRange([-84.946,-26.48],reference);
  assert.equal(hi,75);assert(lo<15.054);assert(hi>73.52);
});
test('include every finite channel and expand on later extremes',()=>{
  for(const values of [[-400,150],[null,NaN,Infinity,-50],[-20],[-35]]) {
    const [lo,hi]=snapRange(values,reference);
    for(const value of values)if(value!==null&&Number.isFinite(value))assert(value+100>lo&&value+100<hi);
  }
  assert(snapRange([null,NaN],reference).every(Number.isFinite));
});
