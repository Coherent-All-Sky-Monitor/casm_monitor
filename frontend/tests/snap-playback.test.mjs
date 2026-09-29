import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source=readFileSync(new URL('../src/components/snapPlaybackModel.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const {decodePlaybackBatch,playbackStamp,playbackNext,playbackGap,playbackColumns}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
test('play saved frames in order, stop at end unless looping, empty/single safe',()=>{
  assert.equal(playbackNext(0,3,false),1);
  assert.equal(playbackNext(2,3,false),null);
  assert.equal(playbackNext(2,3,true),0);
  assert.equal(playbackNext(0,0,true),null);
  assert.equal(playbackNext(0,1,true),null);
});
test('UTC and observatory time include date and daylight-saving abbreviation',()=>{
  const stamp=Date.parse('2026-09-29T01:23:45Z')/1000;
  assert.equal(playbackStamp(stamp,'UTC'),'2026-09-29  01:23:45 UTC');
  assert.equal(playbackStamp(stamp,'America/Los_Angeles'),'2026-09-28  18:23:45 PDT');
  assert(playbackStamp(Date.parse('2026-12-29T01:23:45Z')/1000,'America/Los_Angeles').endsWith('PST'));
});
test('missing hours are labelled, not interpolated or hidden by uniform playback',()=>{
  assert.equal(playbackGap(100,3700,3600),null);
  assert.equal(playbackGap(100,10900,3600),'Gap between reads: 3.0 h');
});
test('grid adapts to input count and viewport, keeping legible panel widths',()=>{
  for(const width of [300,700,1400,1900])for(const count of [1,16,24,48]){
    const cols=playbackColumns(count,width,800);
    assert(cols>=1&&cols<=count);
    assert((width-(cols-1)*8)/cols>=200);
  }
  assert.equal(playbackColumns(16,1500,850),4);
});
function packed(){
  const meta={version:1,encoding:'f32le',shape:[2,2,4096],inputs:['board/6','absent/1'],
    freq_mhz:Array.from({length:4096},(_,i)=>500-i*125/4096),
    frames:[1,2].map(id=>({ids:[id],boards:[{ip:'board',ts:100+id,eq_epoch:'e1',fft_shift:3},{ip:'absent',ts:null}]}))};
  const json=new TextEncoder().encode(JSON.stringify(meta)),size=Math.ceil(json.length/4)*4;
  const buffer=new ArrayBuffer(4+size+2*2*4096*4);
  new DataView(buffer).setUint32(0,size,true);
  new Uint8Array(buffer,4,size).fill(32);new Uint8Array(buffer,4,json.length).set(json);
  const values=new Float32Array(buffer,4+size);values.fill(NaN);
  values.set(Array.from({length:4096},(_,i)=>-80+i/100),0);
  values.set(Array.from({length:4096},(_,i)=>-90+i/100),8192);
  values[2]=NaN;
  return buffer;
}
test('packed batches retain channels, ordering, timestamps and missing samples',()=>{
  const result=decodePlaybackBatch(packed(),['board/6','absent/1']);
  assert.deepEqual(result.map(f=>f.ids),[[1],[2]]);
  assert.equal(result[0].boards[0].spectra_db[6].length,4096);
  assert.equal(result[0].boards[0].spectra_db[6][2],null);
  assert(Math.abs(result[1].boards[0].spectra_db[6][4095]+49.05)<1e-5);
  assert.equal(result[0].boards[1].spectra_db[1],null);
  assert.equal(result[0].boards[0].fft_shift,3);
  assert.equal(result[1].boards[0].ts,102);
  assert.equal(result[0].freq_mhz[0],500);
  assert.equal(result[0].freq_mhz[4095],375.030517578125);
});
test('reject truncated, wrong selection and malformed packed batches',()=>{
  const buffer=packed();
  for(const n of [0,3,100,buffer.byteLength-1])assert.throws(()=>decodePlaybackBatch(buffer.slice(0,n),['board/6','absent/1']));
  assert.throws(()=>decodePlaybackBatch(buffer,['absent/1','board/6']));
  new DataView(buffer).setUint32(0,7,true);
  assert.throws(()=>decodePlaybackBatch(buffer,['board/6','absent/1']));
});
