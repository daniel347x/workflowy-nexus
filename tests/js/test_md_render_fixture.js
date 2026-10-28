'use strict';
// Immutable 8dd6e0f output data, not a retained legacy renderer.
// Run: node tests/js/test_md_render_fixture.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const crypto = require('crypto');
const dir = path.join(__dirname, '../../workflowy-glimpse-cache');
const read = file => fs.readFileSync(path.join(dir, file), 'utf8');
const fixtureBytes = fs.readFileSync(path.join(__dirname, 'fixtures/md-render/baseline.json'));
const fixture = JSON.parse(fixtureBytes);
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
let checks = 0;
function check(label, fn) {
  fn();
  checks++;
  console.log('ok   ' + label);
}
check('immutable pre-extraction oracle', () => {
  assert.strictEqual(sha(fixtureBytes), 'e07c2a2608df96366f4d37f56893541deae7c9b086cef80a8960469c3684c8fd');
  assert.strictEqual(fixture.provenance.commit, '8dd6e0f444e00c476bc912f50aa8d80cf0c76e3b');
  for (const [file, hash] of Object.entries(fixture.provenance.assets)) {
    // Git stores LF; normalize checkout CRLF for source assets only, not the oracle.
    assert.strictEqual(sha(read(file).replace(/\r\n/g, '\n')), hash, file);
  }
});
const context = vm.createContext({console:{log(){},warn(){}}});
context.window = context;
for (const file of ['marked.min.js', 'katex/katex.min.js', 'marked-katex-init.js', 'glimpse-md-render.js']) {
  vm.runInContext(read(file), context, {filename:file});
}
const api = context.GlimpseMd;
const marked = context.marked;
const client = read('glimpse-cache-client.js');
function extract(name) {
  const start = client.indexOf(`function ${name}(`);
  assert(start >= 0, name);
  let depth = 0;
  for (let i = client.indexOf('{', start); i < client.length; i++) {
    if (client[i] === '{') depth++;
    else if (client[i] === '}' && --depth === 0) return client.slice(start, i + 1);
  }
  throw Error('unbalanced ' + name);
}
const names = ['escapeHtmlForPreview', 'composedMarkdownToHtml', 'composedContentToHtml', 'compileDockSourceSearch', 'dockedSourceHtml'];
// Real production delegates, not stubs or test copies of their implementation.
const dockFactory = new Function('GlimpseMd', 'state', `
  let dockedSourceViewMode=state.view || 'rendered';
  let dockedRenderContentFormat=state.contentFormat || 'markdown';
  let dockedSourceSearchRegex=GlimpseMd.compileSourceSearch(state.query || '').regex;
  let dockedSourceSearchCount=17, dockedSourceSearchCurrent=9;
  ${names.map(extract).join('\n')}
  return {composedMarkdownToHtml, composedContentToHtml, dockedSourceHtml,
    compileDockSourceSearch, escapeHtmlForPreview,
    state:()=>({count:dockedSourceSearchCount,current:dockedSourceSearchCurrent})};`);
check('frozen shared API', () => {
  assert(Object.isFrozen(api));
  assert.deepStrictEqual(Object.keys(api).sort(), ['compileSourceSearch','escapeHtml','renderRowList','sourceHtml','toHtml']);
});
for (const c of fixture.cases) {
  context.marked = c.marked === 'absent' ? undefined : c.marked === 'throw' ? {parse(){throw Error('fixture');}} : marked;
  const isSource = c.view === 'source' || c.contentFormat === 'raw_text';
  const regex = api.compileSourceSearch(c.query || '').regex;
  if (regex) regex.lastIndex = 500; // every paint resets it, even a reused regex
  let state = {count:17,current:9};
  let calls = 0;
  const opts = {view:c.view, contentFormat:c.contentFormat, findRegex:regex,
    onSearchState:s=>{calls++; state={count:s.count,current:s.current};}};
  check('shared bytes/state: ' + c.name, () => {
    assert.strictEqual(api.toHtml(c.text, opts), c.expected.html);
    assert.deepStrictEqual(state, {count:c.expected.count,current:c.expected.current});
    assert.strictEqual(calls, isSource ? 1 : 0);
    assert.strictEqual(api.toHtml(c.text, opts), c.expected.html, 'reused regex');
  });
  if (isSource) check('sourceHtml bytes/optional callback: ' + c.name, () => {
    state = {count:17,current:9};
    assert.strictEqual(api.sourceHtml(c.text, opts), c.expected.html);
    assert.deepStrictEqual(state, {count:c.expected.count,current:c.expected.current});
    assert.strictEqual(api.sourceHtml(c.text, {findRegex:regex}), c.expected.html);
  });
  const dock = dockFactory(api, c);
  check('production dispatcher bytes/state: ' + c.name, () => {
    assert.strictEqual(dock.composedContentToHtml(c.text), c.expected.html);
    assert.deepStrictEqual(dock.state(), {count:c.expected.count,current:c.expected.current});
    // Explicit content format and the default host format agree.
    assert.strictEqual(dock.composedContentToHtml(c.text, c.contentFormat || 'markdown'), c.expected.html);
  });
  if (c.contentFormat !== 'raw_text') check('production Markdown entry: ' + c.name, () => {
    assert.strictEqual(dock.composedMarkdownToHtml(c.text), c.expected.html);
    assert.deepStrictEqual(dock.state(), {count:c.expected.count,current:c.expected.current});
  });
  if (isSource) check('production source entry: ' + c.name, () => {
    assert.strictEqual(dock.dockedSourceHtml(c.text), c.expected.html);
    assert.deepStrictEqual(dock.state(), {count:c.expected.count,current:c.expected.current});
  });
}
context.marked = marked;
function searchRecord(r) {
  return r.ok ? {ok:true, source:r.regex ? r.regex.source : null, flags:r.regex ? r.regex.flags : null} : {ok:false,error:r.error};
}
for (const c of fixture.search) check('shared + production query: ' + JSON.stringify(c.query), () => {
  assert.deepStrictEqual(searchRecord(api.compileSourceSearch(c.query)), c.expected);
  assert.deepStrictEqual(searchRecord(dockFactory(api, {}).compileDockSourceSearch(c.query)), c.expected);
});
for (const c of fixture.escape) check('shared + external escape delegate: ' + JSON.stringify(c.text), () => {
  assert.strictEqual(api.escapeHtml(c.text), c.html);
  assert.strictEqual(dockFactory(api, {}).escapeHtmlForPreview(c.text), c.html);
});
check('independent consumers do not leak state', () => {
  const a = dockFactory(api, {view:'source',query:'one'});
  const b = dockFactory(api, {view:'source',query:'two'});
  a.composedMarkdownToHtml('one one');
  b.composedMarkdownToHtml('two');
  assert.deepStrictEqual(a.state(), {count:2,current:0});
  assert.deepStrictEqual(b.state(), {count:1,current:0});
  assert.strictEqual(api.toHtml('one'), '<p>one</p>\n');
  assert.strictEqual(api.sourceHtml('one'), '<pre class="glimpse-md-preview-plain">one</pre>');
});
check('manifest MAIN load order', () => {
  const main = JSON.parse(read('manifest.json')).content_scripts.find(s=>s.world === 'MAIN');
  // "The Rail" (ui-bakeoff/fable): the presentation modules load AFTER the shared
  // renderer and BEFORE the client (they publish GlimpseUI, which the client mounts).
  // Re-pinned for "The Rail, side by side" (ui-next/rail-side-by-side, leg 2): glimpse-ui-help.js
  // (the words: GlimpseUiHelp) loads between glimpse-ui.js and the drawers that read it.
  assert.deepStrictEqual(main.js, ['hotkeys-shared.js','marked.min.js','katex/katex.min.js','marked-katex-init.js','glimpse-md-render.js','glimpse-ui.js','glimpse-ui-help.js','glimpse-ui-drawers.js','glimpse-cache-client.js']);
  assert.deepStrictEqual(main.css, ['katex/katex.min.css','katex-overrides.css','glimpse-md-render.css','glimpse-ui.css']);
});
function cssRules(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, '').match(/[^{}]+\{[^{}]*\}/g).map(s=>s.trim());
}
// The host's injected box-sizing rule now lives in the presentation module
// (glimpse-ui.js mounts the deck); the string is byte-identical to the old array.
const ui = read('glimpse-ui.js');
const styleStart = ui.indexOf("style.id = 'glimpse-md-dock-style'");
const strStart = ui.indexOf('style.textContent = ', styleStart) + 'style.textContent = '.length;
const strEnd = ui.indexOf(';\n', strStart);
const hostCss = vm.runInNewContext(ui.slice(strStart, strEnd));
check('render CSS migration: only ID to class; box sizing explicit', () => {
  const rules = cssRules(read('glimpse-md-render.css')).filter(r=>!r.includes('.glimpse-md-row-list'));
  assert.strictEqual(rules.shift(), '.glimpse-md-render, .glimpse-md-render * { box-sizing: border-box; }');
  assert.strictEqual(rules.join('\n'), fixture.renderCss.replace(/#glimpse-md-dock-render/g, '.glimpse-md-render'));
});
check('host rules unchanged; no render rules left in injected array', () => {
  const rowRules = cssRules(read('glimpse-md-render.css'))
    .filter(r=>r.includes('.glimpse-md-row-list .glimpse-md-dock-'))
    .map(r=>r.replace(/\.glimpse-md-row-list/g, '#glimpse-md-dock-panel'));
  assert.strictEqual([...cssRules(hostCss), ...rowRules].join('\n'), fixture.hostCss);
  assert(!/glimpse-md-dock-render|glimpse-md-render|glimpse-dock-src-hit/.test(hostCss));
});
check('render class, retained ID, host geometry in the stylesheet', () => {
  // The Reader's document pane is built by glimpse-ui.js and keeps the retained
  // id + the renderer's scope class; its geometry moved from inline styles to
  // glimpse-ui.css (.nx-render) with "The Rail".
  assert(ui.includes("el('div', 'nx-render glimpse-md-render')"));
  assert(ui.includes("render.id = 'glimpse-md-dock-render';"));
  const uiCss = read('glimpse-ui.css');
  assert(/\.nx-render \{[^}]*flex: 1 1 auto;[^}]*overflow: auto;/.test(uiCss), 'nx-render geometry');
  assert(!client.includes("render.id = 'glimpse-md-dock-render'"), 'the client no longer builds the pane');
});
check('one transformation owner; client functions are delegates only', () => {
  for (const name of names.filter(n=>n !== 'composedContentToHtml')) {
    const body = extract(name);
    assert(body.includes('return GlimpseMd.'), name);
    assert(!/marked\.parse|\.replace\(|\.exec\(|<pre|<mark|const tokens|const pats/.test(body), name);
  }
  const code = client.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
  assert(!/marked\.parse\(/.test(code));
  assert(!/const pats = \[\]|const tokens = \[\]/.test(extract('compileDockSourceSearch')));
  assert.strictEqual((client.match(/\bcomposedMarkdownToHtml\(/g) || []).length - 1, 14, '14 real calls, not the spec\'s 13');
  assert.strictEqual((client.match(/\bescapeHtmlForPreview\(/g) || []).length - 1, 2, 'external escape callers retained');
  assert.strictEqual((read('glimpse-md-render.js').match(/return marked\.parse\(/g) || []).length, 1);
});
console.log(`ALL PASS: ${checks} checks; ${fixture.cases.length} legacy render cases, ${fixture.search.length} query cases, ${fixture.escape.length} escape cases`);
