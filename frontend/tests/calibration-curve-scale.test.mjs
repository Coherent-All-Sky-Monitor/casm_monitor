import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source=readFileSync(new URL('../src/components/calibrationCurveScale.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const {signedLog,inverseSignedLog}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));

test('signed log preserves order, sign, zero and negative subtraction residuals',()=>{
  const values=[-1e20,-1e8,-10,-1,-1e-9,0,1e-9,1,10,1e8,1e20];
  const display=values.map(signedLog);
  assert(display.every((v,i)=>Number.isFinite(v)&&(!i||v>display[i-1])));
  values.forEach((v,i)=>{
    assert.equal(Math.sign(display[i]),Math.sign(v));
    assert(Math.abs(inverseSignedLog(display[i])-v)<=Math.max(1e-24,Math.abs(v)*1e-13));
  });
  assert.equal(signedLog(0),0);
});
test('bright peaks are compressed, not clipped or replaced',()=>{
  assert(signedLog(1e10)>signedLog(1e8));
  assert(signedLog(1e10)-signedLog(1e8)<2.01);
  assert(signedLog(-1e10)<signedLog(-1e8));
});
