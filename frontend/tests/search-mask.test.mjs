import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
const source=readFileSync(new URL('../src/lib/searchMask.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const {maskSegments}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
const band={nchan:3072,low_mhz:390.655517578125,high_mhz:484.375};
test('native low channel numbers map to masked high-frequency end',()=>{
  const [kept,masked]=maskSegments(band,{enabled:true,first:0,last:471});
  assert.equal(kept.masked,false);assert.equal(kept.count,2600);
  assert.equal(kept.first,472);assert.equal(kept.last,3071);
  assert.equal(kept.low,band.low_mhz);
  assert.equal(masked.count,472);assert.equal(masked.high,band.high_mhz);
  assert.equal(masked.low,470.001220703125);
  assert.equal(kept.fraction+masked.fraction,1);
});
test('interior and opposite-edge masks preserve both kept ranges',()=>{
  const parts=maskSegments(band,{enabled:true,first:100,last:199});
  assert.deepEqual(parts.map(s=>s.masked),[false,true,false]);
  assert.equal(parts.reduce((n,s)=>n+s.count,0),3072);
  assert(parts.every((s,i)=>!i||s.low>parts[i-1].high));
  assert.deepEqual(maskSegments(band,{enabled:true,first:3000,last:3071}).map(s=>s.masked),[true,false]);
});
test('off, full-band and unknown states cannot imply a missing mask is off',()=>{
  assert.deepEqual(maskSegments(band,{enabled:false}).map(s=>s.count),[3072]);
  assert.equal(maskSegments(band,{enabled:true,first:0,last:3071})[0].fraction,1);
  for(const mask of [undefined,{enabled:null},{enabled:true},{enabled:true,first:-1,last:200},{enabled:true,first:0,last:3072}])assert.equal(maskSegments(band,mask),null);
  assert.equal(maskSegments(undefined,{enabled:false}),null);
});
