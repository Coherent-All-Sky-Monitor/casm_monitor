import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
async function module(path){
  const code=ts.transpileModule(readFileSync(new URL(path,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
  return import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
}
const {powerValue,powerLabel,linearPowerRange}=await module('../src/components/powerScale.ts');
const {dbToLinear}=await module('../src/lib/spectrumUtils.ts');

test('power dB uses ten log10, a fixed reference and no absolute value or floor',()=>{
  for(const [v,db]of [[.01,-20],[1,0],[10,10],[100,20]])assert(Math.abs(powerValue(v,'db')-db)<1e-12);
  for(const v of [null,NaN,Infinity,-Infinity,0,-4])assert.equal(powerValue(v,'db'),null);
  assert.equal(powerValue(-4,'linear'),-4);
  assert.equal(powerValue(0,'linear'),0);
  assert.equal(powerValue(1e-8,'db',1e-10),20);
  assert.equal(powerLabel('db'),'Power (dB)');
  assert.equal(powerLabel('linear'),'Power (counts)');
});
test('SNAP linear power reverses stored dB, not the display reference offset',()=>{
  assert.equal(dbToLinear(-80),1e-8);
  assert.equal(dbToLinear(0),1);
  for(const v of [null,NaN,Infinity,-Infinity,10000])assert.equal(dbToLinear(v),null);
  const mean=(1+100)/2;
  assert.equal(powerValue(mean,'db'),10*Math.log10(mean));
  assert.notEqual(powerValue(mean,'db'),(powerValue(1,'db')+powerValue(100,'db'))/2);
});
test('linear power limits include every finite native value and signed residual',()=>{
  for(const values of [[1e-10,1e-7],[-2,0,10],[100],[0],[null,NaN]]){
    const [lo,hi]=linearPowerRange(values);
    assert(Number.isFinite(lo)&&Number.isFinite(hi)&&hi>lo);
    for(const v of values)if(v!==null&&Number.isFinite(v))assert(v>=lo&&v<=hi);
  }
});
