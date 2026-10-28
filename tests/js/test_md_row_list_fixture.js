'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {extract,hostCss,cssRules,run,sha,sourceHash} = require('./fixtures/md-row-list/capture-baseline');
const dir = path.resolve(__dirname,'../../workflowy-glimpse-cache');
const read = file => fs.readFileSync(path.join(dir,file),'utf8');
const bytes = fs.readFileSync(path.join(__dirname,'fixtures/md-row-list/baseline.json'));
const baseline = JSON.parse(bytes);
let checks = 0;
const only = process.argv.find(s=>s.startsWith('--only='));
function check(label,fn) {
  if (only && !label.includes(only.slice(7))) return;
  fn(); checks++; console.log('ok   '+label);
}
check('fixed locale/timezone',()=>{
  assert.strictEqual(new Intl.DateTimeFormat().resolvedOptions().locale,'en-US');
  assert.strictEqual(process.env.TZ,baseline.provenance.timezone);
});
check('immutable pre-edit oracle',()=>{
  assert.strictEqual(baseline.provenance.clientSha256,sourceHash);
  assert.strictEqual(sha(bytes),'43bb24b85a184c5a0239939f96a2fa21121311b7d3033c08f8f0b1359da97767');
});
const client = read('glimpse-cache-client.js');
// "The Rail" (ui-bakeoff/fable): the outline element and the host box-sizing rule
// are built by the presentation module; the client keeps the thin row-list delegate.
const ui = read('glimpse-ui.js');
function uiHostCss() {
  const start = ui.indexOf("style.id = 'glimpse-md-dock-style'");
  const a = ui.indexOf('style.textContent = ', start) + 'style.textContent = '.length;
  const b = ui.indexOf(';\n', a);
  return vm.runInNewContext(ui.slice(a, b));
}
const legacy = process.argv.includes('--legacy');
if (legacy) {
  assert.strictEqual(sha(client),sourceHash);
  for (const c of baseline.cases) check('untouched production replay: '+c.name,()=>assert.deepStrictEqual(run(client,c,null),c.expected));
  check('untouched emitted row CSS',()=>assert.strictEqual(cssRules(hostCss(client)).filter(r=>r.includes('.glimpse-md-dock-')).join('\n'),baseline.rowCss));
} else {
  const shared = read('glimpse-md-render.js');
  const ctx = vm.createContext({}); vm.runInContext(shared,ctx);
  check('one added export',()=>{
    assert(Object.isFrozen(ctx.GlimpseMd));
    assert.deepStrictEqual(Object.keys(ctx.GlimpseMd).sort(),['compileSourceSearch','escapeHtml','renderRowList','sourceHtml','toHtml']);
    assert.strictEqual(ctx.GlimpseMd.renderRowList(null,[],{}),undefined);
    const empty = {innerHTML:'stale'};
    assert.strictEqual(ctx.GlimpseMd.renderRowList(empty,[],{}),undefined);
    assert.strictEqual(empty.innerHTML,'');
  });
  for (const c of baseline.cases) {
    check('shared oracle: '+c.name,()=>assert.deepStrictEqual(run(client,c,shared),c.expected));
    check('real dock delegate oracle: '+c.name,()=>assert.deepStrictEqual(run(client,{...c,delegate:true},shared),c.expected));
    check('canonical name alias: '+c.name,()=>assert.deepStrictEqual(run(client,c,shared,true),c.expected));
  }
  check('explicit text wins even when blank; default/overridden emoji',()=>{
    const c = baseline.cases[0];
    const both = {...c,rows:c.rows.map(r=>({...r,name:'NOT THE LABEL'}))};
    assert.deepStrictEqual(run(client,both,shared),c.expected);
    const custom = run(client,c,shared,false,'CUSTOM');
    assert.strictEqual(custom.tree.children.find(el=>el.className.includes('glimpse-md-dock-row')).children[2].text,'CUSTOM');
  });
  check('single owner and thin delegate',()=>{
    const body = extract(client,'renderDockedList');
    assert(body.includes('GlimpseMd.renderRowList('));
    assert(!/createElement|forEach|INDENT_PX|_rowEl|daysep|formatDockTimestamp/.test(body));
    for (const name of ['formatDockTimestamp','localDayOrdinal','localDayLabel']) {
      assert(!client.includes('function '+name+'('),name+' removed from client');
      assert.strictEqual(shared.split('function '+name+'(').length-1,1,name+' shared once');
    }
    assert.strictEqual(shared.split('function renderRowList(').length-1,1);
    assert(!uiHostCss().includes('.glimpse-md-dock-'));
    assert(!client.includes("'#glimpse-md-dock-panel .glimpse-md-dock-"));
  });
  check('dock delegate state binding',()=>{
    const body = extract(client,'renderDockedList');
    for (const text of ['GlimpseMd.renderRowList(dockedListEl, dockedListRows, {',
      'sortBy: getMdPreviewSortBy()', 'sortDir: getMdPreviewSortDir()',
      'selected: dockedSelectedIndices', 'primary: dockedSelectedIndex',
      'onClick: handleDockRowClick', 'markdownEmoji: MARKDOWN_EMOJI']) assert(body.includes(text),text);
  });
  check('scope class and manifest',()=>{
    assert(ui.includes("el('div', 'nx-outline glimpse-md-row-list')"), 'the outline carries the renderer scope class');
    assert(client.includes('dockedListEl = r.outline;'), 'the client binds its list variable to the outline');
    const main = JSON.parse(read('manifest.json')).content_scripts.find(s=>s.world==='MAIN');
    assert(main.js.indexOf('glimpse-md-render.js') < main.js.indexOf('glimpse-cache-client.js'));
    assert(main.css.includes('glimpse-md-render.css'));
  });
  check('row CSS declarations/order exact, only scope normalized',()=>{
    const rules = cssRules(read('glimpse-md-render.css')).filter(r=>r.includes('.glimpse-md-row-list'));
    assert.strictEqual(rules.shift(),'.glimpse-md-row-list, .glimpse-md-row-list * { box-sizing: border-box; }');
    assert.strictEqual(rules.join('\n'),baseline.rowCss.replace(/#glimpse-md-dock-panel/g,'.glimpse-md-row-list'));
    assert.strictEqual(uiHostCss(),'#glimpse-md-dock-panel, #glimpse-md-dock-panel * { box-sizing: border-box; }');
  });
}
console.log(`ALL PASS: ${checks} checks; ${baseline.cases.length} captured row cases (${legacy ? 'original' : 'shared + delegate + canonical'})`);
