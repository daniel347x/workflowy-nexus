'use strict';
// Explicit sensitivity/replay check, not part of normal fixture tests.
// Run from the worktree root. All mutations stay in tmp/md-mutations.
const fs = require('fs');
const path = require('path');
const cp = require('child_process');
const root = process.cwd();
const scratch = path.join(root, 'tmp/md-mutations');
fs.mkdirSync(path.join(scratch, 'tests/js/fixtures/md-render'), {recursive:true});
fs.mkdirSync(path.join(scratch, 'workflowy-glimpse-cache'), {recursive:true});
for (const f of ['test_md_render_fixture.js','test_dock_source_view.js']) fs.copyFileSync(path.join(root,'tests/js',f),path.join(scratch,'tests/js',f));
fs.copyFileSync(path.join(root,'tests/js/fixtures/md-render/baseline.json'),path.join(scratch,'tests/js/fixtures/md-render/baseline.json'));
const ext = 'workflowy-glimpse-cache/';
for (const f of ['marked.min.js','marked-katex-init.js','glimpse-md-render.js','glimpse-md-render.css','glimpse-cache-client.js','manifest.json']) fs.copyFileSync(root+'/'+ext+f,scratch+'/'+ext+f);
fs.cpSync(root+'/'+ext+'katex',scratch+'/'+ext+'katex',{recursive:true});
const mutations = [
  ['breaks:true','glimpse-md-render.js','{ breaks: true }','{ breaks: false }'],
  ['mark class','glimpse-md-render.js',"? 'glimpse-dock-src-hit glimpse-dock-src-hit--current'","? 'glimpse-dock-src-hit wrong-current'"],
  ['count adapter','glimpse-cache-client.js','dockedSourceSearchCount = count;','dockedSourceSearchCount = count + 1;'],
  ['current adapter','glimpse-cache-client.js','dockedSourceSearchCurrent = current;','dockedSourceSearchCurrent = current + 1;'],
  ['CSS declaration','glimpse-md-render.css','font-size: 1.5em','font-size: 1.6em'],
  ['manifest order','manifest.json','"marked-katex-init.js", "glimpse-md-render.js"','"glimpse-md-render.js", "marked-katex-init.js"'],
];
for (const [label,file,oldText,newText] of mutations) {
  const original=fs.readFileSync(root+'/'+ext+file,'utf8');
  if (!original.includes(oldText)) throw Error('missing mutation '+label);
  fs.writeFileSync(scratch+'/'+ext+file,original.replace(oldText,newText));
  const tests = label === 'mark class' ? ['test_dock_source_view.js','test_md_render_fixture.js'] : ['test_md_render_fixture.js'];
  for (const test of tests) {
    const r=cp.spawnSync(process.execPath,['tests/js/'+test],{cwd:scratch,encoding:'utf8',timeout:30000});
    if (r.status !== 1) throw Error('mutation not killed '+label+': '+r.status+' '+r.stderr);
    const lines = r.stdout.trim().split('\n');
    console.log(label+': '+test+' exit '+r.status+'; '+(lines.find(l=>l.startsWith('FAIL')) || 'after '+lines[lines.length-1])+'; '+r.stderr.split('\n').find(l=>l.includes('AssertionError')));
  }
  fs.writeFileSync(scratch+'/'+ext+file,original);
}
console.log('ALL 6 MUTANTS KILLED (7 expected failing test invocations); production files untouched');
// Replay the archived capture against the exact pre-change client bytes.
fs.writeFileSync(scratch+'/'+ext+'glimpse-cache-client.js', cp.execFileSync('git',['show','8dd6e0f:'+ext+'glimpse-cache-client.js'],{cwd:root,maxBuffer:8*1024*1024}));
fs.writeFileSync(scratch+'/'+ext+'manifest.json', cp.execFileSync('git',['show','8dd6e0f:'+ext+'manifest.json'],{cwd:root}));
const replay=cp.spawnSync(process.execPath,[root+'/tests/js/fixtures/md-render/capture-baseline.js'],{cwd:scratch,encoding:'utf8',timeout:30000});
if (replay.status !== 0) throw Error(replay.stderr);
const assert=require('assert');
const expected=JSON.parse(fs.readFileSync(root+'/tests/js/fixtures/md-render/baseline.json'));
const actual=JSON.parse(fs.readFileSync(scratch+'/tests/js/fixtures/md-render/baseline.json'));
for (const key of ['cases','search','escape','renderCss','hostCss']) assert.deepStrictEqual(actual[key],expected[key],key);
console.log('Baseline replay PASS: 21 render cases, 9 queries, 4 escapes, 28 render rules, 10 host rules');
// Restore immutable fixture and remove additive files to represent pre-change.
fs.copyFileSync(root+'/tests/js/fixtures/md-render/baseline.json',scratch+'/tests/js/fixtures/md-render/baseline.json');
fs.unlinkSync(scratch+'/'+ext+'glimpse-md-render.js');
fs.unlinkSync(scratch+'/'+ext+'glimpse-md-render.css');
const before=cp.spawnSync(process.execPath,['tests/js/test_md_render_fixture.js'],{cwd:scratch,encoding:'utf8',timeout:30000});
assert.strictEqual(before.status,1);
assert(before.stderr.includes('ENOENT') && before.stderr.includes('glimpse-md-render.js'));
console.log('Pre-change new-API check FAILS as expected (exit 1, missing glimpse-md-render.js); oracle replay PASSES');
