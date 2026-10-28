#!/usr/bin/env node
'use strict';
/*
 * test_rail_layout.js — "The Rail, side by side" (ui-next/rail-side-by-side) layout locks.
 *
 *  A. The float layer: every visible float the client draws (cursor menus, pickers, dialogs,
 *     the tooltip) mounts on GlimpseUI.layer() under <html>; ONLY the three invisible helpers
 *     (the clipboard textarea, the file input, the download anchor) still mount on
 *     document.body. A push narrows <body> (and shifts it on a left dock); the floats stay out of it.
 *  B. Fit-or-split: splitName splits a long name at its FIRST semantic separator (at index 8
 *     or later) into a head and a tail, never elides, and reconstructs the name.
 *  C. Drawer roles, as the brief locks them: pickers (Anchor Docks, Slice groups, the Lightning
 *     Rod) and drawers that open alone (Jobs, Tabs, Settings, Help).
 *  D. Settings through the ONE merge authority: glimpse_ui_push defaults ON, glimpse_ui_flip
 *     OFF, glimpse_help_dismissed keeps only known ids, md_preview_dock_position accepts 'auto'.
 *  E. applyPush: left/right docks only, screen only; it narrows <body> and moves Workflowy's fixed
 *     chrome off the deck's edge, and never touches <html>'s overflow or gives <body> a transform
 *     (the document stays Workflowy's scroller; fixed dialogs and the logo stay on the viewport).
 *     The deck's panes scroll on their own (overscroll-behavior: contain).
 *  F. The words and the help layer (leg 2): glimpse-ui-help.js loads between glimpse-ui.js and the
 *     drawers and publishes the keys they read; the manifest carries Dan's name and description;
 *     the drawers paint the one-liners with More…, a ? on five drawers, the Help sections with
 *     their Back links and the tips reset; the tips persist through the ONE settings key.
 *
 * Dependency-free; reads the extension sources as text (the tests/js idiom).
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const DIR = path.join(__dirname, '..', '..', 'workflowy-glimpse-cache');
const read = (f) => fs.readFileSync(path.join(DIR, f), 'utf8');
const client = read('glimpse-cache-client.js');
const ui = read('glimpse-ui.js');
const drawers = read('glimpse-ui-drawers.js');
const shared = read('hotkeys-shared.js');

let total = 0, fails = 0;
function check(name, fn) {
  total++;
  try { fn(); console.log('ok   ' + name); }
  catch (e) { fails++; console.log('FAIL ' + name + '\n     ' + (e && e.message ? e.message : e)); }
}
function countOcc(s, needle) { return s.split(needle).length - 1; }
function extractFunction(src, name) {
  const start = src.indexOf('function ' + name + '(');
  assert(start >= 0, 'function ' + name + ' not found');
  let depth = 0;
  for (let i = src.indexOf('{', start); i < src.length; i++) {
    const c = src[i];
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error('unbalanced function ' + name);
}

// ── A ── the float layer
check('A: only the three invisible helpers still mount on document.body (textarea, file input, download anchor)', () => {
  assert.strictEqual(countOcc(client, 'document.body.appendChild('), 3,
    'a new visible float must mount on glimpseFloatLayer(), never on document.body (a push narrows and shifts <body>)');
  assert(client.includes('function glimpseFloatLayer()'), 'glimpseFloatLayer helper');
  assert(countOcc(client, 'glimpseFloatLayer().appendChild(') >= 13, 'the 13 float sites use the layer');
});
check('A: the deck and the float layer live under <html>, outside the (pushed) <body>', () => {
  assert(ui.includes('document.documentElement.appendChild(deck);'), 'deck under <html>');
  assert(ui.includes("l.id = 'glimpse-nx-layer'; document.documentElement.appendChild(l);"), 'layer under <html>');
  assert(!ui.includes('document.body.appendChild(deck)'), 'the deck no longer mounts on <body>');
});

// ── B ── fit-or-split
check('B: splitName splits at the first separator from index 8, never elides, and reconstructs the name', () => {
  // (values cross a vm realm: compare them as plain JSON, never by prototype)
  const raw = vm.runInNewContext('(' + extractFunction(ui, 'splitName') + ')');
  const splitName = (s) => JSON.parse(JSON.stringify(raw(s)));
  const a = splitName('MANAGER APP — the human side of the Program Conductor Toolkit');
  assert.deepStrictEqual(a, { head: 'MANAGER APP', sep: ' — ', tail: 'the human side of the Program Conductor Toolkit' });
  assert.strictEqual(a.head + a.sep + a.tail, 'MANAGER APP — the human side of the Program Conductor Toolkit');
  const b = splitName('Recruiting: leads and follow-ups (2026)');
  assert.deepStrictEqual(b, { head: 'Recruiting', sep: ': ', tail: 'leads and follow-ups (2026)' });
  const c = splitName('[ap:ECOT1S] 🗺️ NEXUS/CARTOGRAPHER/WORKFLOWY project map');
  assert.deepStrictEqual(c, { head: '[ap:ECOT1S] 🗺️ NEXUS/CARTOGRAPHER/WORKFLOWY project map', sep: '', tail: '' });
  const d = splitName('Short: x');   // a separator before index 8 never splits (the head would be a stub)
  assert.deepStrictEqual(d, { head: 'Short: x', sep: '', tail: '' });
  const e = splitName('Project Maps (the described lists, kept by Dan)');
  assert.strictEqual(e.head, 'Project Maps');
  assert.strictEqual(e.head + e.sep + e.tail, 'Project Maps (the described lists, kept by Dan)');
  [a, b, c, d, e].forEach((p) => { assert(!/…/.test(p.head + p.tail), 'no ellipsis anywhere'); });
  assert(ui.includes("n.appendChild(el('span', 'nx-name-head', p.head));"), 'nameEl paints the head');
  assert(ui.includes("n.appendChild(el('span', 'nx-name-tail', p.tail));"), 'nameEl paints the tail');
});
check('B: the picker rows ask for fit-or-split names and the drawers refit after painting', () => {
  assert(drawers.includes("if (opts.nameSplit && UI.nameEl) label.appendChild(UI.nameEl(opts.label)); else label.textContent = opts.label;"));
  assert(countOcc(drawers, 'nameSplit: true') >= 2, 'Dock rows and slice rows');
  assert(countOcc(drawers, 'try { UI.fitDrawer(); } catch (e) { /* ignore */ }') >= 2, 'anchorsRender and slicesRender refit');
});

// ── C ── drawer roles
check('C: pickers and opens-alone drawers carry their role in the definition', () => {
  assert(drawers.includes("title: 'Anchor Docks', icon: 'anchor', order: 10, picker: true, hint: 'where things go',"));
  assert(drawers.includes("title: 'Slice groups', icon: 'layers', order: 20, picker: true, hint: 'what you look at',"));
  assert(drawers.includes("title: 'Lightning Rod', icon: 'zap', order: 30, picker: true,"));
  ['tabs', 'jobs', 'settings', 'help'].forEach((id) => {
    const start = drawers.indexOf("UI.registerDrawer('" + id + "', {");
    const head = drawers.slice(start, drawers.indexOf('build(container)', start));
    assert(head.includes('opensAlone: true'), id + ' opens alone');
    assert(!head.includes('picker: true'), id + ' is not a picker');
  });
  assert(ui.includes('function isPicker(id) { const def = drawerDef(id); return !!(def && def.picker); }'));
  assert(ui.includes('function opensAlone(id) { const def = drawerDef(id); return !!(def && def.opensAlone); }'));
});
check('C: a pick closes a transient picker and keeps a pinned one (afterPick), and the Reader re-shows on an agent push', () => {
  assert(countOcc(drawers, 'UI.afterPick()') >= 4, 'Dock open, slice Apply, Copy search terms, the Rod');
  assert(!drawers.includes('UI.closeDrawer();\n    try { api().renderAnchor(uuid); }'), 'anchorsOpen no longer closes unconditionally');
  assert(client.includes('GlimpseUI.showReaderPane();'), 'the push receiver re-shows the Reader pane');
  assert(client.indexOf('GlimpseUI.showReaderPane();') < client.indexOf('applySliceGroupIntoDock(gid, {\n                  // ALL view knobs'), 'before the detached render');
});

// ── D ── settings
check('D: mergeExtensionSettings declares the three new keys and accepts dock position auto', () => {
  const ctx = {}; ctx.self = ctx; ctx.globalThis = ctx; ctx.window = ctx;
  vm.runInNewContext(shared, ctx);
  const G = ctx.GlimpseHotkeys;
  assert(G && typeof G.mergeExtensionSettings === 'function', 'GlimpseHotkeys published');
  const d = G.mergeExtensionSettings({});
  assert.strictEqual(d.glimpse_ui_push, true, 'push ON by default (Dan’s call)');
  assert.strictEqual(d.glimpse_ui_flip, false, 'flip OFF by default');
  assert.strictEqual(JSON.stringify(d.glimpse_help_dismissed), '[]', 'no tip dismissed by default');
  const u = G.mergeExtensionSettings({ glimpse_ui_push: 0, glimpse_ui_flip: 'yes', glimpse_help_dismissed: ['tip-docks', 'bogus', 'tip-docks', 'tip-rail'], md_preview_dock_position: 'auto' });
  assert.strictEqual(u.glimpse_ui_push, false);
  assert.strictEqual(u.glimpse_ui_flip, true);
  assert.strictEqual(JSON.stringify(u.glimpse_help_dismissed), '["tip-docks","tip-rail"]', 'known ids only, de-duplicated, in order');
  assert.strictEqual(u.md_preview_dock_position, 'auto');
  assert.strictEqual(G.mergeExtensionSettings({ md_preview_dock_position: 'sideways' }).md_preview_dock_position, 'right');
});

// ── E ── push
// Runs the real applyPush against a stub document and reads the one <style> it writes.
function runPush(side, px, enabled) {
  const sheets = {};
  const doc = { getElementById: (id) => sheets[id] || null, documentElement: { appendChild: (n) => { sheets[n.id] = n; } } };
  const S = { pushFallback: false, pushPx: -1, pushSide: 'unset' };
  const applyPush = new Function('document', 'el', 'S', 'pushEnabled', 'schedulePushCheck',
    extractFunction(ui, 'applyPush') + '\nreturn applyPush;')(doc, () => ({ id: '', textContent: '' }), S, () => enabled !== false, () => {});
  applyPush(side, px);
  return { css: sheets['glimpse-nx-push'] ? sheets['glimpse-nx-push'].textContent : null, S };
}
check('E: applyPush keeps the document Workflowy’s scroller: no overflow on <html>, no containing block on <body> (wheel, Page keys, viewport-anchored dialogs and logo)', () => {
  ['right', 'left'].forEach((side) => {
    const { css } = runPush(side, 676);
    assert(css && css.startsWith('@media screen{') && css.endsWith('}'), side + ': one screen-only sheet (print is untouched)');
    assert(!/html\s*\{/.test(css), side + ': nothing on <html>');
    assert(!/overflow/.test(css), side + ': never changes the scroller (html{overflow:hidden} killed the wheel and Page keys)');
    assert(!/transform|filter|perspective|contain|will-change/.test(css),
      side + ': never makes <body> a containing block (a transform anchored Workflowy’s dialogs and loading logo to the document top)');
  });
  assert(!ui.includes('overflow:hidden!important'), 'no html{overflow:hidden} anywhere in glimpse-ui.js');
});
check('E: applyPush narrows <body> and moves Workflowy’s fixed chrome off the deck’s edge, on left/right docks only', () => {
  const r = runPush('right', 676);
  assert(r.css.includes('body{box-sizing:border-box!important;width:calc(100% - 676px)!important;}'), 'right: <body> narrowed, border-box');
  assert(r.css.includes('body :is(.wf-header,.dialog-backdrop,.fixed[class*="right-"],.fixed[class*="inset-"]){margin-right:676px!important}'),
    'right: the header, the dialog backdrop and right-anchored or inset fixed elements take a right margin');
  assert.strictEqual(r.S.pushPx, 676); assert.strictEqual(r.S.pushSide, 'right');
  const l = runPush('left', 500);
  assert(l.css.includes('body{box-sizing:border-box!important;width:calc(100% - 500px)!important;margin-left:500px!important;}'), 'left: <body> narrowed and shifted');
  assert(l.css.includes('body :is(.wf-header,.dialog-backdrop,.fixed[class*="left-"],.fixed[class*="inset-"]){margin-left:500px!important}'), 'left: the left edge');
  ['top', 'bottom'].forEach((side) => {
    const t = runPush(side, 676);
    assert(t.css === null || t.css === '', side + ': no push');
    assert.strictEqual(t.S.pushPx, 0);
  });
  assert.strictEqual(runPush('right', 676, false).css, null, 'glimpse_ui_push off: no push');
  const body = extractFunction(ui, 'applyPush');
  assert(body.includes("(side === 'right' || side === 'left')"), 'left/right only');
  assert(ui.includes('function pushSelfCheck()'), 'self-check');
  const sc = extractFunction(ui, 'pushSelfCheck');
  assert(sc.includes("document.elementsFromPoint(x, 24)") && sc.includes("getComputedStyle(f).position !== 'fixed'"),
    'the self-check also hit-tests the header line for a fixed element whose own box reaches under the deck');
  assert(ui.includes('S.pushFallback = true;'), 'fallback flag');
  assert(ui.includes('const PIN_PUSH_MIN_VW = 1500;'), 'S’s sizing rule: a pinned panel overlays below 1500 px');
  assert(ui.includes("function pushEnabled() { try { return S.api.settings().glimpse_ui_push !== false; } catch (e) { return true; } }"), 'the Settings switch');
});
check('E: the deck’s panes scroll on their own: reaching a pane’s end never scrolls Workflowy’s page', () => {
  const css = read('glimpse-ui.css');
  assert(css.includes('.nx-deck .nx-drawer-body, .nx-deck .nx-outline, .nx-deck .nx-render,\n.nx-palette-list, .nx-dialog-list { overscroll-behavior: contain; }'));
});

// ── F ── the words and the help layer
check('F: glimpse-ui-help.js loads between glimpse-ui.js and the drawers and publishes the keys they read; the manifest carries Dan’s name', () => {
  const manifest = JSON.parse(read('manifest.json'));
  const main = manifest.content_scripts.find((s) => s.world === 'MAIN');
  const i = main.js.indexOf('glimpse-ui-help.js');
  assert(i > main.js.indexOf('glimpse-ui.js') && i < main.js.indexOf('glimpse-ui-drawers.js'), 'the words load after glimpse-ui.js and before the drawers');
  assert.strictEqual(manifest.name, 'NEXUS for Workflowy', 'Dan’s call 4: the name');
  assert.strictEqual(manifest.description, 'Workflowy context management, with your agents', 'Dan’s call 4: the description');
  const ctx = {}; ctx.self = ctx; ctx.globalThis = ctx; ctx.window = ctx;
  vm.runInNewContext(read('glimpse-ui-help.js'), ctx);
  const H = ctx.GlimpseUiHelp;
  assert(H && Object.isFrozen(H), 'GlimpseUiHelp published, frozen');
  ['systemLine', 'helpIntro', 'bylines', 'titles', 'oneLiners', 'readerEmpty', 'sections', 'f12Numbers', 'syntaxHelp', 'tips', 'strings'].forEach((k) => assert(k in H, 'key ' + k));
  assert.deepStrictEqual(JSON.parse(JSON.stringify(H.tips.map((t) => t.id))), ['tip-rail', 'tip-docks', 'tip-slices'], 'the tip ids are the ones the settings sanitiser knows');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(H.sections.map((s) => s.id))), ['nx-help-reader', 'nx-help-docks', 'nx-help-slices', 'nx-help-rod', 'nx-help-tabs', 'nx-help-jobs']);
  assert(H.readerEmpty.includes('{hotkey}'), 'the empty state carries the hotkey token');
  ['anchors', 'slices', 'jobs'].forEach((k) => assert(typeof H.oneLiners[k] === 'string' && H.oneLiners[k].length > 40, 'one-liner ' + k));
  assert.strictEqual(H.strings.manifestName, manifest.name);
  assert.strictEqual(H.strings.manifestDescription, manifest.description);
});
check('F: the drawers paint the one-liners with More…, a ? on five drawers, the Help sections with Back links and the tips reset; the client reads the words', () => {
  assert(countOcc(drawers, "oneLiner('") >= 3, 'Anchor Docks, Slice groups and Jobs carry the one sentence');
  ['nx-help-docks', 'nx-help-slices', 'nx-help-rod', 'nx-help-tabs', 'nx-help-jobs'].forEach((id) => assert(drawers.includes("helpBtn('" + id + "'"), '? for ' + id));
  assert(drawers.includes('UI.openHelpAt = function openHelpAt(sectionId, backId)'), 'the reveal: More… and ? open Help at a section');
  assert(drawers.includes("'← Back to ' + sec.backLabel"), 'the Back link');
  assert(drawers.includes("api().persistSetting('glimpse_help_dismissed', []);"), 'the Settings drawer resets the tips');
  assert(drawers.includes("(res && typeof res.favorites_notice === 'string') ? res.favorites_notice.trim() : ''"), 'the verbatim favorites_notice stays the else-branch of the composed line');
  assert(ui.includes("const TIP_IDS = ['tip-rail', 'tip-docks', 'tip-slices'];"), 'the tips live in glimpse-ui.js');
  assert(ui.includes("S.api.persistSetting('glimpse_help_dismissed', next)"), 'a dismissal persists through the ONE settings key');
  assert(ui.includes('if (open && !S.tipsShown) { S.tipsShown = true;'), 'offered once per session, on the first deck open');
  assert(client.includes("help.readerEmpty.replace('{hotkey}'"), 'the Reader empty state reads the words module');
  assert(client.includes('GlimpseUiHelp.strings.sliceNotice'), 'the slice notice reads the words module');
});

console.log('\n' + (total - fails) + ' of ' + total + ' checks passed' + (fails ? ' — ' + fails + ' FAILURE(S)' : ''));
process.exit(fails ? 1 : 0);
