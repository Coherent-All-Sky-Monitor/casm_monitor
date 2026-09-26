import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source=readFileSync(new URL('../src/components/figureDownload.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const {figureFilename}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
test('figure filenames are readable, bounded and path-safe',()=>{
  assert.equal(figureFilename('N16E1 · SNAP 2 SLOT A ADC 1','png'),'N16E1-SNAP-2-SLOT-A-ADC-1.png');
  assert.equal(figureFilename('../../','png'),'figure.png');
  assert.equal(figureFilename('A'.repeat(200),'png').length,144);
});
