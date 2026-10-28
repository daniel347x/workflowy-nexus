'use strict';
// Explicit targeted sensitivity checks, never edits the real production files.
const fs = require('fs');
const path = require('path');
const cp = require('child_process');
const assert = require('assert');
const root = path.resolve(__dirname,'../../../..');
const scratch = path.join(root,'tests/.runs/s2-row-mutations');
const ext = 'workflowy-glimpse-cache/';
const files = ['tests/js/test_md_row_list_fixture.js',
  'tests/js/fixtures/md-row-list/capture-baseline.js',
  'tests/js/fixtures/md-row-list/baseline.json',
  ...['glimpse-cache-client.js','glimpse-md-render.js','glimpse-md-render.css','manifest.json'].map(f=>ext+f)];
for (const f of files) {
  fs.mkdirSync(path.dirname(path.join(scratch,f)),{recursive:true});
  fs.copyFileSync(path.join(root,f),path.join(scratch,f));
}
function test(label,args,expected) {
  const r = cp.spawnSync(process.execPath,['tests/js/test_md_row_list_fixture.js',...args],
    {cwd:scratch,encoding:'utf8',timeout:10000});
  fs.writeFileSync(path.join(scratch,label+'.log'),r.stdout+r.stderr);
  assert.strictEqual(r.status,expected,label+': '+r.stderr);
  console.log(label+': exit '+r.status+'; '+r.stdout.trim().split('\n').at(-1));
  return r;
}
const mutations = [
  ['guide', 'glimpse-md-render.css', 'width: calc(var(--glimpseDockDepth, 0) * 14px)', 'width: calc(var(--glimpseDockDepth, 0) * 15px)'],
  ['depth-spacing', 'glimpse-md-render.js', 'const INDENT_PX = 14;', 'const INDENT_PX = 15;'],
  ['selection-marker', 'glimpse-md-render.js', "_isPrimary ? '\\u25CF'", "_isPrimary ? '\\u25CB'"],
  ['timestamp', 'glimpse-md-render.js', "? 'Modified' : 'Created'", "? 'WrongModified' : 'WrongCreated'"],
  ['day-separator', 'glimpse-md-render.js', "sep.className = 'glimpse-md-dock-daysep';", "sep.className = 'wrong-daysep';"],
  ['delegate-state', 'glimpse-cache-client.js', 'primary: dockedSelectedIndex,', 'primary: -1,'],
];
for (const [label,file,oldText,newText] of mutations) {
  const original = fs.readFileSync(path.join(root,ext+file),'utf8');
  assert(original.includes(oldText),label+' mutation target');
  fs.writeFileSync(path.join(scratch,ext+file),original.replace(oldText,newText));
  const r = test(label,[],1);
  assert(r.stderr.includes('AssertionError'),label+' must fail a comparison, not crash');
  fs.writeFileSync(path.join(scratch,ext+file),original);
}
// Exact pre-S2 bytes, in the throwaway copy. No stash and no second painter.
for (const f of files.filter(f=>f.startsWith(ext))) {
  fs.writeFileSync(path.join(scratch,f),cp.execFileSync('git',['show','0c56c96:'+f],{cwd:root,maxBuffer:8*1024*1024}));
}
test('pre-legacy',['--legacy'],0);
for (const check of ['one added export','single owner and thin delegate','dock delegate state binding','scope class and manifest','row CSS declarations/order']) {
  test('pre-'+check.replace(/\W+/g,'-'),['--only='+check],1);
}
console.log('ALL 6 MUTANTS KILLED; original oracle replay PASS; 5 independent pre-move ownership/API/CSS checks FAIL as intended');
