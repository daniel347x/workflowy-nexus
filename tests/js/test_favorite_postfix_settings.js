// ============================================================================
// test_favorite_postfix_settings.js — Unit 4 (extension): the favorite-anchor
// postfix settings shape + the Anchors-popup favorites wiring; extended in
// U6 item 2 for the Saved-Tabs Dock picker's ⭐ paint + Favorites-only toggle.
//
// Covers (contract [ap:7CYWWE] §6.11 rev 1 / DoD §11.9):
//   • DEFAULT — DEFAULT_EXTENSION_SETTINGS carries
//     note_session_favorite_postfixes: ['fav'] beside the local list (§6.11.1).
//   • MERGE — mergeExtensionSettings' favorite branch: array OR CSV string,
//     sanitizeF8PostfixWord per token (strip one '#', trim, reject
//     whitespace/empty), order-preserving de-dup, and the fallback-to-default
//     ['fav'] discipline on empty/invalid input (§6.11.2); the local branch
//     is byte-identical in behaviour (cross-contamination locks both ways).
//   • REPLIES — BOTH Direction-A settings replies in glimpse-cache-client.js
//     carry note_session_favorite_postfixes with the ['fav'] fallback idiom
//     (§6.11.4: get_extension_settings_result AND get_slice_group_config_result).
//   • WIRE — requestAnchorListForWidget puts favorites_only in the
//     list_anchors_for_widget frame; the Anchors DRAWER (glimpse-ui-drawers.js,
//     'The Rail', ui-bakeoff/fable leg 3: the Anchors popup is deleted) defaults
//     favoritesOnly TRUE (the public shortlist default) and sends BOTH flags
//     from its state on every request, while the Saved-Tabs Dock picker
//     defaults favoritesOnly OFF in its orchestrator (full inventory) and sends
//     the toggle state (U6 item 2; §6.11.5/§6.11.6).
//   • PAINT — the Anchors drawer lights the row's ⭐ button from is_favorite
//     (tree and flat shapes through ONE row painter; the name text stays
//     clean), marks local Docks from is_local, its Favorites / Local toggles
//     flip state and re-request (NOT persisted; only the collapse state is),
//     and the server-composed favorites_notice line tolerates a pre-favorites
//     reply. The Saved-Tabs picker keeps its own three (U6 item 2): ⭐ in its
//     addRows walker (before [local], OUT of nameStr), its own Favorites-only
//     toggle with J6 mutual preservation, and the same absent-tolerant notice.
//   • OPTIONS — the options.html field exists and options.js loads + persists
//     it with the ['fav'] fallback (§6.11.3).
//
// The behavioral checks extract mergeExtensionSettings + friends VERBATIM
// from hotkeys-shared.js (the repo's extraction idiom, tests/js/README.md);
// the UI/wire checks are static wiring locks over the real file bytes (the
// popup/reply assemblies are anonymous page-world blocks, not extractable
// named functions). Against the pre-Unit-4 tree this harness FAILS (the
// favorite key/branch/locks do not exist) — the structural mutation-
// sensitivity guarantee of the idiom (stash-verified; see the Unit 4
// extension RESULT).
//
// RUN:   node tests/js/test_favorite_postfix_settings.js
// EXIT:  0 = all pass, 1 = at least one failure. Zero dependencies.
// ============================================================================
'use strict';

const fs = require('fs');
const path = require('path');

const SHARED_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'hotkeys-shared.js');
const CLIENT_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'glimpse-cache-client.js');
const OPTIONS_JS_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'options.js');
const OPTIONS_HTML_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'options.html');
const DRAWERS_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'glimpse-ui-drawers.js');

const sharedSrc = fs.readFileSync(SHARED_FILE, 'utf8');
const clientSrc = fs.readFileSync(CLIENT_FILE, 'utf8');
const drawersSrc = fs.readFileSync(DRAWERS_FILE, 'utf8');
const optionsJsSrc = fs.readFileSync(OPTIONS_JS_FILE, 'utf8');
const optionsHtmlSrc = fs.readFileSync(OPTIONS_HTML_FILE, 'utf8');

// --- tiny assert harness (the tests/js idiom) -------------------------------
let passed = 0;
let failed = 0;
function ok(cond, label) {
  if (cond) { passed++; console.log('  ok  ' + label); }
  else { failed++; console.log('FAIL  ' + label); }
}
function eq(actual, expected, label) {
  ok(JSON.stringify(actual) === JSON.stringify(expected),
    label + '  (got ' + JSON.stringify(actual) + ')');
}

// --- extraction (verbatim from the REAL file bytes; a rename fails LOUDLY) --
function extractFunctionFrom(src, name) {
  let idx = src.indexOf(`\n  function ${name}(`);
  if (idx >= 0) idx += 1; // skip the newline
  else idx = src.indexOf(`function ${name}(`);
  if (idx < 0) throw new Error(`function not found in shared file: ${name}`);
  const start = src.indexOf('{', idx);
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(idx, i + 1);
    }
  }
  throw new Error(`unbalanced braces extracting: ${name}`);
}

function extractConstObjectFrom(src, name) {
  const marker = `\n  const ${name} =`;
  const idx = src.indexOf(marker);
  if (idx < 0) throw new Error(`const not found in shared file: ${name}`);
  const start = src.indexOf('{', idx);
  if (start < 0) throw new Error(`no object literal for const: ${name}`);
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) {
        const semi = src.indexOf(';', i);
        return src.slice(idx + 1, semi + 1);
      }
    }
  }
  throw new Error(`unbalanced braces extracting const: ${name}`);
}

// mergeExtensionSettings' dependency closure inside hotkeys-shared.js (all
// pure; genGroupId/genAnchorViewId fall back to time+random without `self`).
const body = [
  extractConstObjectFrom(sharedSrc, 'DEFAULT_EXTENSION_SETTINGS'),
  extractFunctionFrom(sharedSrc, 'sanitizeExpandTagPrefix'),
  extractFunctionFrom(sharedSrc, 'parseExpandTagPrefixes'),
  extractFunctionFrom(sharedSrc, 'sanitizeF8PostfixWord'),
  extractFunctionFrom(sharedSrc, 'genGroupId'),
  extractFunctionFrom(sharedSrc, 'makeDefaultExpandGroup'),
  extractFunctionFrom(sharedSrc, 'sanitizeExpandGroup'),
  extractFunctionFrom(sharedSrc, 'normalizeExpandGroups'),
  extractFunctionFrom(sharedSrc, 'resolveActiveGroupId'),
  extractFunctionFrom(sharedSrc, 'genAnchorViewId'),
  extractFunctionFrom(sharedSrc, 'makeDefaultAnchorView'),
  extractFunctionFrom(sharedSrc, 'sanitizeAnchorView'),
  extractFunctionFrom(sharedSrc, 'normalizeAnchorViews'),
  extractFunctionFrom(sharedSrc, 'parseMarkerTags'),
  extractFunctionFrom(sharedSrc, 'mergeExtensionSettings'),
].join('\n');

const api = new Function(`
  ${body}
  return { DEFAULT_EXTENSION_SETTINGS, mergeExtensionSettings };
`)();

const { DEFAULT_EXTENSION_SETTINGS, mergeExtensionSettings } = api;

// --- static-lock helper ------------------------------------------------------
// Slice the region between two anchors in a source file and assert on it, so
// a key is locked to the RIGHT reply/frame (not merely present somewhere).
function regionBetween(src, startAnchor, endAnchor) {
  const a = src.indexOf(startAnchor);
  if (a < 0) throw new Error('region start not found: ' + startAnchor);
  const b = src.indexOf(endAnchor, a + startAnchor.length);
  if (b < 0) throw new Error('region end not found after start: ' + endAnchor);
  return src.slice(a, b);
}

console.log('— DEFAULT (§6.11.1) —');
eq(DEFAULT_EXTENSION_SETTINGS.note_session_favorite_postfixes, ['fav'],
  'shipped default note_session_favorite_postfixes is [\'fav\']');
eq(DEFAULT_EXTENSION_SETTINGS.note_session_local_postfixes, ['local'],
  'local default untouched ([\'local\'])');

console.log('— MERGE (§6.11.2) —');
eq(mergeExtensionSettings({}).note_session_favorite_postfixes, ['fav'],
  'empty user settings -> default [\'fav\']');
eq(mergeExtensionSettings({ note_session_favorite_postfixes: ['star'] })
    .note_session_favorite_postfixes, ['star'],
  'array value honoured');
eq(mergeExtensionSettings({ note_session_favorite_postfixes: 'star, fav' })
    .note_session_favorite_postfixes, ['star', 'fav'],
  'CSV string accepted');
eq(mergeExtensionSettings({ note_session_favorite_postfixes: 'fav, star, fav' })
    .note_session_favorite_postfixes, ['fav', 'star'],
  'order-preserving de-dup');
eq(mergeExtensionSettings({ note_session_favorite_postfixes: [' #star ', 'bad word', 'fav'] })
    .note_session_favorite_postfixes, ['star', 'fav'],
  'per-token sanitize (strip one #, trim, reject whitespace)');
eq(mergeExtensionSettings({ note_session_favorite_postfixes: ['', '  ', 42] })
    .note_session_favorite_postfixes, ['fav'],
  'all-invalid -> fallback [\'fav\']');
eq(mergeExtensionSettings({ note_session_favorite_postfixes: [] })
    .note_session_favorite_postfixes, ['fav'],
  'explicit empty array -> fallback [\'fav\'] (fallback-to-default, NOT honor-empty)');
{
  const m = mergeExtensionSettings({ note_session_favorite_postfixes: ['star'] });
  eq(m.note_session_local_postfixes, ['local'],
    'favorite merge does not touch the local words');
  const m2 = mergeExtensionSettings({ note_session_local_postfixes: ['nested'] });
  eq(m2.note_session_favorite_postfixes, ['fav'],
    'local merge does not touch the favorite words');
}

console.log('— REPLIES (§6.11.4) —');
{
  const extReply = regionBetween(clientSrc,
    "action: 'get_extension_settings_result',", 'ws.send(JSON.stringify(reply));');
  ok(extReply.includes('note_session_favorite_postfixes:'),
    'get_extension_settings_result carries note_session_favorite_postfixes');
  ok(extReply.includes("? favoritePostfixes : ['fav'],"),
    'get_extension_settings_result falls back to [\'fav\']');
  const cfgReply = regionBetween(clientSrc,
    "action: 'get_slice_group_config_result',", 'ws.send(JSON.stringify(_cfgReply));');
  ok(cfgReply.includes('note_session_favorite_postfixes:'),
    'get_slice_group_config_result carries note_session_favorite_postfixes (Stage-0 lens boundary)');
  ok(cfgReply.includes(": ['fav'],"),
    'get_slice_group_config_result falls back to [\'fav\']');
}

console.log('— WIRE (§6.11.5/§6.11.6) —');
{
  const frameRegion = regionBetween(clientSrc,
    "action: 'list_anchors_for_widget',", 'ws.send(JSON.stringify(frame));');
  ok(frameRegion.includes('favorites_only: favoritesOnly,'),
    'list_anchors_for_widget frame carries favorites_only beside show_local_anchors');
  // Leg 3 ('The Rail'): the popup orchestrator is deleted; the Anchors drawer's
  // state object is the one default, and every request sends both flags from it.
  const anchorsRegion = regionBetween(drawersSrc,
    '// ═════════════════════════ ANCHORS', '// ═════════════════════════ SLICES');
  ok(anchorsRegion.includes('opts: { favoritesOnly: true, showLocal: false },'),
    'Anchors drawer defaults favoritesOnly TRUE (the public shortlist default)');
  ok(anchorsRegion.includes('api().listAnchors({ favoritesOnly: anchors.opts.favoritesOnly, showLocalAnchors: anchors.opts.showLocal })'),
    'Anchors drawer sends favoritesOnly AND showLocalAnchors from its state on every request');
  ok(clientSrc.includes("listAnchors: (opts) => requestAnchorListForWidget(Object.assign({ treeView: true, includeRawText: false }, opts || {})),"),
    'api.listAnchors routes to requestAnchorListForWidget (the ONE list_anchors_for_widget sender)');
  const pickerRegion = regionBetween(clientSrc,
    'async function openSavedTabsDockPicker(opts) {',
    'showSavedTabsDockPickerPopup(res, !!o.showLocalAnchors, favoritesOnly);');
  ok(pickerRegion.includes('const favoritesOnly = !!o.favoritesOnly;'),
    'Saved-Tabs Dock picker defaults favoritesOnly OFF in ONE place (the orchestrator; full inventory preserved)');
  ok(pickerRegion.includes('favoritesOnly: favoritesOnly,'),
    'Saved-Tabs Dock picker sends the toggle state as favorites_only (default false = full inventory)');
}

console.log('— PAINT (Anchors drawer) —');
{
  // Leg 3 ('The Rail'): the popup's tree + flat painters are deleted. The drawer
  // walks either server shape through ONE row painter: ⭐ is the row's star
  // button lit from is_favorite (never appended to the name), local is a label
  // class from is_local; the toggles flip state and re-request.
  const anchorsRegion = regionBetween(drawersSrc,
    '// ═════════════════════════ ANCHORS', '// ═════════════════════════ SLICES');
  ok(anchorsRegion.includes("(res.anchor_tree && Array.isArray(res.anchor_tree.children)) ? res.anchor_tree.children : (Array.isArray(res.sessions) ? res.sessions : [])"),
    'tree and flat reply shapes feed the ONE row painter');
  ok(anchorsRegion.includes("'nx-star' + (node.is_favorite ? ' is-on' : '')"),
    'the row’s ⭐ button lights from is_favorite');
  ok(!anchorsRegion.includes("' ⭐'"),
    'the name text stays clean: ⭐ is the star button’s state, never appended to the name');
  // Re-pinned for "The Rail, side by side" (ui-next/rail-side-by-side): the 8-hex sub-line under
  // each Dock name is gone (the row clean-up: no id line, no ellipsis), so the local mark moved
  // from the sub-label into the row's tooltip; the label class stays.
  ok(anchorsRegion.includes("labelClass: node.is_local ? 'is-local' : ''")
      && anchorsRegion.includes("(node.is_local ? '  ·  local Dock' : '')"),
    'local Docks are marked from is_local (label class + the tooltip’s local Dock mark)');
  ok(anchorsRegion.includes("const fav = btn('Favorites'"),
    'favorites-only/all toggle button exists in the drawer tools');
  ok(anchorsRegion.includes('anchors.opts.favoritesOnly = !anchors.opts.favoritesOnly; anchorsLoad(); anchorsRender();'),
    'Favorites toggle flips the flag and re-requests (the re-request idiom)');
  ok(anchorsRegion.includes('anchors.opts.showLocal = !anchors.opts.showLocal; anchorsLoad(); anchorsRender();'),
    'Local toggle flips its flag and re-requests; both flags ride every request, so the two toggles never reset each other');
  ok(!/LS\.set\((?!'glimpse_anchor_collapsed')/.test(anchorsRegion)
      && !anchorsRegion.includes('saveAnchorOpts({ favoritesOnly'),
    'favorites / local toggles are NOT persisted (only the collapse state is; the show-local precedent)');
  ok(anchorsRegion.includes("(res && typeof res.favorites_notice === 'string') ? res.favorites_notice.trim() : ''"),
    'drawer paints the server-composed favorites_notice verbatim and tolerates a pre-favorites reply (absent key -> no line)');
}

console.log('— PAINT (picker — U6 item 2) —');
{
  const pickerPopRegion = regionBetween(clientSrc,
    'function showSavedTabsDockPickerPopup(res, showLocalAnchors, favoritesOnly) {',
    '//   id=saved-tabs@file-import,');
  ok(pickerPopRegion.includes("(node && node.is_favorite) ? ' ⭐' : ''"),
    'picker addRows paints ⭐ from is_favorite');
  const pickerPaintRegion = regionBetween(clientSrc,
    'nameEl.textContent = String((node && node.name) ||',
    "nameEl.style.fontSize = '11px';");
  ok(pickerPaintRegion.indexOf("(node && node.is_favorite) ? ' ⭐' : ''")
      < pickerPaintRegion.indexOf("(node && node.is_local) ? '  [local]' : ''"),
    'picker ⭐ paints before [local] (server compact ⭐-before-Ⓛ order, inside the nameEl paint expression)');
  const pickerNameStrRegion = regionBetween(clientSrc,
    "const nameStr = String((node && node.name) || '(unnamed)')",
    "const row = document.createElement('div');");
  ok(!pickerNameStrRegion.includes('is_favorite'),
    'picker keeps ⭐ OUT of nameStr (the tooltip/status/last-used text reuse it)');
  ok(pickerPopRegion.includes('is_favorite: !!(s && s.is_favorite)'),
    'picker flat fallback carries is_favorite into addRows');
  ok(pickerPopRegion.includes('favoritesBtn'),
    'picker favorites-only/all toggle button exists in headerControls');
  ok(pickerPopRegion.includes('openSavedTabsDockPicker({ favoritesOnly: !favoritesOnlyActive, showLocalAnchors: showLocalActive })'),
    'picker favorites toggle re-requests flipped, carrying show-local (J6 mutual preservation)');
  ok(pickerPopRegion.includes('openSavedTabsDockPicker({ showLocalAnchors: !showLocalActive, favoritesOnly: favoritesOnlyActive })'),
    'picker show-local toggle carries the favorites state (the two toggles never reset each other)');
  ok(pickerPopRegion.includes('res.favorites_notice'),
    'picker paints the server-composed favorites_notice verbatim');
  ok(pickerPopRegion.includes("typeof res.favorites_notice === 'string'"),
    'picker notice paint tolerates a pre-Unit-4 reply (absent key -> no line)');
}

console.log('— OPTIONS (§6.11.3) —');
ok(optionsHtmlSrc.includes('id="note-session-favorite-postfixes"'),
  'options.html carries the favorite-postfixes field');
ok(optionsJsSrc.includes("getElementById('note-session-favorite-postfixes')"),
  'options.js binds the field');
ok(optionsJsSrc.includes('persistSettings({ note_session_favorite_postfixes: finalWords })'),
  'options.js persists note_session_favorite_postfixes on change');
ok(optionsJsSrc.includes("const finalWords = words.length ? words : ['fav'];"),
  'options.js change handler falls back to [\'fav\']');
{
  const renderRegion = regionBetween(optionsJsSrc,
    'if (favoriteAnchorPostfixesInput) {',
    'favoriteAnchorPostfixesInput.value = GlimpseHotkeys.formatExpandTagPrefixes(words);');
  ok(renderRegion.includes("? currentSettings.note_session_favorite_postfixes\n        : ['fav'];"),
    'options.js load path falls back to [\'fav\']');
}

console.log('');
console.log(`${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
