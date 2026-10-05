import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
const source=readFileSync(new URL('../src/components/vis/visibilityPlaybackModel.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const {lowerBound,windowFirst,component,chunkValue,RollingSpectra,decodeVisibilityChunk,movieOrder,VISIBILITY_SPEEDS,visibilityCompactColumns,visibilityDuration}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));

test('compact visibility movies keep four readable columns, regardless of screen height',()=>{
  for(const width of [1244,1464,1884,2524])assert.equal(visibilityCompactColumns(16,width),4);
  assert.equal(visibilityCompactColumns(16,1000),3);
  assert.equal(visibilityCompactColumns(16,720),2);
  assert.equal(visibilityCompactColumns(16,390),1);
  assert.equal(visibilityCompactColumns(2,1900),2);
  assert.equal(visibilityCompactColumns(0,0),1);
  for(const width of [300,720,1000,1244,1900]){
    const cols=visibilityCompactColumns(16,width);
    assert((width-(cols-1)*8)/cols>=300);
  }
});

test('visibility speed choices and duration estimates count saved frames, not look-back context',()=>{
  assert.deepEqual(VISIBILITY_SPEEDS,[.5,1,2,4,8,16,32]);
  assert.equal(visibilityDuration(1258,4),'5m 15s');
  assert.equal(visibilityDuration(1258,8),'2m 38s');
  assert.equal(visibilityDuration(1258,16),'1m 19s');
  assert.equal(visibilityDuration(1258,32),'40s');
  assert.equal(visibilityDuration(1,.5),'2s');assert.equal(visibilityDuration(0,32),'0s');
});

test('time seek and rolling window use actual timestamps, including gaps',()=>{
  const m={times:[0,137,411,548],window_s:275};
  assert.equal(lowerBound(m.times,138),2);assert.equal(windowFirst(m,3),2);
});
test('phase is atan2, wraps correctly, zero is undefined; amplitude is not normalized',()=>{
  assert.equal(component(3,4,'amp'),5);assert.equal(component(3,4,'phase'),Math.atan2(4,3));
  assert.equal(component(0,0,'phase'),null);assert.equal(component(NaN,1,'real'),null);
  assert(component(-1,.01,'phase')>3);assert(component(-1,-.01,'phase')<-3);
});
test('window mean preserves scalar amplitude and circular phase, including rolling and seeking',()=>{
  const c={base:0,count:4,pairs:1,channels:2,components:2,values:new Float32Array([1,2,0,0,-1,-2,NaN,NaN,3,4,1,0,1,0,-1,0])};
  const r=new RollingSpectra(1,2);
  r.update(0,1,()=>c);assert.deepEqual(r.spectrum(0,'real'),[0,0]);assert.deepEqual(r.spectrum(0,'phase'),[null,null]);
  assert.deepEqual(r.spectrum(0,'amp'),[Math.sqrt(5),0]);
  r.update(1,2,()=>c);assert.deepEqual(r.spectrum(0,'real'),[1,1]);assert.equal(r.spectrum(0,'amp')[0],(Math.sqrt(5)+5)/2);
  r.update(0,3,()=>c);assert.deepEqual(r.spectrum(0,'real'),[1,0]);assert.equal(r.spectrum(0,'phase')[1],null);
});
test('preview amplitude survives complex cancellation',()=>{
  const c={base:0,count:1,pairs:1,channels:1,components:3,values:new Float32Array([0,0,5])};
  assert.equal(chunkValue(c,0,0,0,'amp'),5);assert.equal(chunkValue(c,0,0,0,'phase'),null);
});
test('binary identity, pair direction, timestamps and lengths are checked',()=>{
  const m={id:'abc',pairs:[[18,8]],times:[100,237],freq_mhz:[450,440],preview_freq_mhz:[445],chunk_size:32};
  const meta={version:1,id:'abc',base:0,encoding:'complex',dtype:'f32le',shape:[2,1,2,2],times:m.times,pairs:m.pairs};
  let json=JSON.stringify(meta);json+=' '.repeat((4-json.length%4)%4);
  const buffer=new ArrayBuffer(4+json.length+8*4);new DataView(buffer).setUint32(0,json.length,true);new Uint8Array(buffer,4,json.length).set(new TextEncoder().encode(json));
  new Float32Array(buffer,4+json.length).set([3,4,1,2,0,0,NaN,NaN]);
  const c=decodeVisibilityChunk(buffer,m,0,'complex');assert.equal(chunkValue(c,0,0,0,'amp'),5);
  assert.throws(()=>decodeVisibilityChunk(buffer,{...m,id:'other'},0,'complex'));
  assert.throws(()=>decodeVisibilityChunk(buffer,{...m,pairs:[[8,18]]},0,'complex'));
  assert.throws(()=>decodeVisibilityChunk(buffer.slice(0,-4),m,0,'complex'));
});
test('SNAP order follows wiring metadata and compact follows position',()=>{
  const a=[{packet_idx:18,snap:1,adc:6,position_enu_m:[0,-5,0]},{packet_idx:8,snap:0,adc:8,position_enu_m:[0,0,0]},{packet_idx:2,snap:0,adc:2,position_enu_m:[1,0,0]}];
  assert.deepEqual(movieOrder(a,[2,8,18],'snap').map(a=>a.packet_idx),[2,8,18]);
  assert.deepEqual(movieOrder(a,[2,8,18],'compact').map(a=>a.packet_idx),[8,2,18]);
});
