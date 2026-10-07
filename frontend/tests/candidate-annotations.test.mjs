import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
const source=readFileSync(new URL('../src/lib/candidateAnnotations.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2020}}).outputText;
const {classifications,mergeVotes,disagreement,voteText}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
const vote=(reviewer_id,label,revision,quality='usable')=>({candidate:'261006abcdef',reviewer_id,label,revision,quality});
test('six agreed labels including FRB and Unsure',()=>{
  assert.deepEqual(classifications.map(c=>c.label),['FRB','Pulsar','Solar','RFI','Noise','Unsure']);
});
test('late polling cannot roll back a saved label or undo',()=>{
  const original=vote('a','frb',1),updated=vote('a','noise',2),undone=vote('a',null,3,'unreviewed');
  assert.deepEqual(mergeVotes([updated],[original]),[updated]);
  assert.deepEqual(mergeVotes([undone],[original,updated]),[undone]);
  assert.equal(mergeVotes([updated],[vote('b','rfi',4)]).length,2);
});
test('disagreement distinguishes uncertainty and missing data',()=>{
  assert.equal(disagreement([vote('a','frb',1),vote('b','pulsar',2)]),true);
  assert.equal(disagreement([vote('a','frb',1),vote('b','unsure',2)]),false);
  assert.equal(disagreement([vote('a','frb',1),vote('b',null,2,'cannot_assess')]),false);
  assert.equal(voteText(vote('a',null,2,'cannot_assess')),'Cannot assess');
  assert.equal(voteText(vote('a',null,3,'unreviewed')),'Unreviewed');
});
