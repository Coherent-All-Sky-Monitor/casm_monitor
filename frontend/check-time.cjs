// Run with node check-time.cjs; uses the existing TypeScript/React dependencies.
const fs = require('fs');
const ts = require('typescript');
const assert = require('node:assert/strict');
const source = fs.readFileSync('src/components/TimeControls.tsx','utf8');
const js = ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const compiled = {exports:{}};
new Function('require','module','exports',js)(require,compiled,compiled.exports);
const {utcFromWall,wallTime,LOCAL} = compiled.exports;
assert.equal(utcFromWall('2026-09-13T12:00',LOCAL),'2026-09-13T19:00');
assert.equal(utcFromWall('2026-01-13T12:00',LOCAL),'2026-01-13T20:00');
assert.throws(()=>utcFromWall('2026-03-08T02:30',LOCAL));
assert.equal(utcFromWall('2026-11-01T01:30',LOCAL),'2026-11-01T08:30');
assert.equal(wallTime('2026-09-13T19:00',LOCAL),'2026-09-13T12:00');
assert.equal(utcFromWall('2026-09-13T12:00','UTC'),'2026-09-13T12:00');
for(const [date,next,hours] of [['2026-03-08','2026-03-09',23],['2026-11-01','2026-11-02',25]]) {
  assert.equal((Date.parse(utcFromWall(next+'T00:00',LOCAL)+'Z')-Date.parse(utcFromWall(date+'T00:00',LOCAL)+'Z'))/3600000,hours);
}
console.log('PDT/PST, UTC, DST gap/repeated hour and 23/25-hour days: passed');
