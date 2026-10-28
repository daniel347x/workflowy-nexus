// ============================================================================
// test_saved_tabs_drive_ui.js — Saved Tabs (Tier-2) unit T2B harness: the
// import-list multi-select + Go + progress console (the ORIGIN-tab UI).
// EXTENDED by unit E2 ([ap:ARDOF6] §E2): groups 10–12 cover the saved
// row-FOCUS restore on every import route (the REAL applySavedTabsEntry +
// the REAL Tier-2 receiver handleSavedTabsDriveApply through the ONE shared
// helper restoreSavedTabsRowFocus — single row restored · rows restored ·
// stale → full document + loud status · no focus → unchanged) plus the E2
// static locks (one helper/two call sites, wording, no spec/wire change).
//
// Contract: MasterSpec [ap:7S217K] rev 2 LOCKED §9 (unit T2B) — §9.4's
// required tests 1–8 live here as behavioral groups; item 9 (mutation
// sensitivity) is the PRE-IMPLEMENTATION extraction failure recorded in the
// RESULT; item 10 is the full-suite receipt (run_tests.py), not a harness
// check. The §9.2 postconditions (incl. the negatives), the §9.6 DoD greps
// and the §9.9 seam manifest are pinned as static locks over the REAL file
// bytes (group LOCKS).
//
// Construction: the repo's extraction idiom (tests/js/README.md) extended
// with injected DOM/bridge mocks per §9.4 — the extracted dialog/drive-UI
// functions are evaluated against a mock document (elements with style,
// listeners, append/remove, isConnected), a mock window (postMessage log +
// message-listener dispatch), and a mock Saved-Tabs status element; the
// REAL parse/label/validate/status functions run from the shipped bytes.
// Functions are extracted VERBATIM from the real file bytes, so a rename or
// signature change fails LOUDLY at extraction. Against the pre-unit tree
// this harness fails at extraction ("function not found") — the recorded
// pre-implementation evidence (§9.4 item 9).
//
// RUN:   node tests/js/test_saved_tabs_drive_ui.js
// EXIT:  0 = all pass, 1 = at least one failure. Zero dependencies.
// ============================================================================
'use strict';

const fs = require('fs');
const path = require('path');

const CLIENT_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'glimpse-cache-client.js');
const BG_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'background.js');
const clientSrc = fs.readFileSync(CLIENT_FILE, 'utf8');
const bgSrc = fs.readFileSync(BG_FILE, 'utf8');
// U01: the REAL settings authority — the stepper clamps through the shipped
// GlimpseHotkeys.mergeExtensionSettings, evaluated here under node.
const hsSrc = fs.readFileSync(path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'hotkeys-shared.js'), 'utf8');
const GlimpseHotkeys = new Function(
  hsSrc + '\nreturn (typeof self !== \'undefined\' ? self : this).GlimpseHotkeys;'
).call({}, {});

// --- tiny assert harness (the tests/js idiom) -------------------------------
let passed = 0;
let failed = 0;
function ok(cond, label) {
  if (cond) { passed++; console.log('  ok  ' + label); }
  else { failed++; console.log('FAIL  ' + label); }
}
function eqJson(actual, expected, label) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  if (a === b) { passed++; console.log('  ok  ' + label); }
  else { failed++; console.log('FAIL  ' + label + '\n        got:      ' + a + '\n        expected: ' + b); }
}
const tick = (ms) => new Promise((r) => setTimeout(r, ms == null ? 0 : ms));
async function flush(rounds) {
  for (let i = 0; i < (rounds == null ? 12 : rounds); i++) await tick(1);
}

// --- extraction (verbatim from the REAL file bytes) --------------------------
function braceSlice(src, start) {
  const open = src.indexOf('{', start);
  if (open < 0) throw new Error('no opening brace after offset ' + start);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error('unbalanced braces at offset ' + start);
}
// glimpse-cache-client.js is one big IIFE — functions sit at 2-space indent.
function extractIifeFunction(src, name) {
  for (const prefix of ['\n  async function ' + name + '(', '\n  function ' + name + '(']) {
    const idx = src.indexOf(prefix);
    if (idx >= 0) return braceSlice(src, idx + 1);
  }
  throw new Error('function not found in client file: ' + name);
}
// Inner body of an anonymous block whose anchor ends with '{'.
function extractBlockBodyAfter(src, anchor) {
  const a = src.indexOf(anchor);
  if (a < 0) throw new Error('anchor not found: ' + anchor);
  const start = a + anchor.length - 1; // the anchor's own '{'
  if (src[start] !== '{') throw new Error('anchor does not end with {: ' + anchor);
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(start + 1, i);
    }
  }
  throw new Error('unbalanced braces after anchor: ' + anchor);
}
function extractConstString(src, name) {
  const m = src.match(new RegExp("const " + name + " = '([^']+)'"));
  if (!m) throw new Error('string const not found: ' + name);
  return m[1];
}
function regionBetween(src, startAnchor, endAnchor) {
  const a = src.indexOf(startAnchor);
  if (a < 0) throw new Error('region start not found: ' + startAnchor);
  const b = src.indexOf(endAnchor, a + startAnchor.length);
  if (b < 0) throw new Error('region end not found after start: ' + endAnchor);
  return src.slice(a, b);
}
function countOcc(hay, needle) {
  let n = 0; let i = hay.indexOf(needle);
  while (i >= 0) { n++; i = hay.indexOf(needle, i + needle.length); }
  return n;
}

// --- TOP-LEVEL EXTRACTION (throws loudly pre-implementation) -----------------
// §9.4 item 9: on the pre-unit tree the first line below throws
// "function not found in client file: savedTabsDriveValidateSelection".
const NEW_FN_NAMES = [
  'savedTabsDriveValidateSelection',
  'requestSavedTabsDrive',
  'cancelSavedTabsDrive',
  'openSavedTabsDriveConsole',
  'handleSavedTabsDriveProgress',
];
const newFns = {};
for (const n of NEW_FN_NAMES) newFns[n] = extractIifeFunction(clientSrc, n);

const parseSrc = extractIifeFunction(clientSrc, 'parseSavedTabsImport');
const labelSrc = extractIifeFunction(clientSrc, 'savedTabsEntryLabel');
const specKindSrc = extractIifeFunction(clientSrc, 'savedTabsSpecKind');
const groupIdExistsSrc = extractIifeFunction(clientSrc, 'savedTabsGroupIdExists');
const validateSrc = extractIifeFunction(clientSrc, 'savedTabsValidateEntrySpec');
const setStatusSrc = extractIifeFunction(clientSrc, 'setSavedTabsStatus');
const closePopupSrc = extractIifeFunction(clientSrc, 'closeSavedTabsPopup');
const dialogSrc = extractIifeFunction(clientSrc, 'openSavedTabsImportDialog');
// Follow-up 2 ([ap:DLGU66]): the EXPORT popup + payload builder, evaluated
// behaviourally for the name-only rows.
const exportPopupSrc = extractIifeFunction(clientSrc, 'showSavedTabsExportPopup');
const classifySrc = extractIifeFunction(clientSrc, 'savedTabsClassifyRow');
const rowDisplayNameSrc = extractIifeFunction(clientSrc, 'savedTabsRowDisplayName');
const buildPayloadSrc = extractIifeFunction(clientSrc, 'buildSavedTabsExportPayload');
const buildJsonSrc = extractIifeFunction(clientSrc, 'buildSavedTabsExportJson');
// Follow-up 3 ([ap:DLGU66]): the widget's Tab alias box — the ONE alias writer
// and the two box helpers, evaluated with injected storage / broker / title.
const getAliasSrc = extractIifeFunction(clientSrc, 'getEndpointAlias');
const setAliasSrc = extractIifeFunction(clientSrc, 'setEndpointAlias');
const syncAliasBoxSrc = extractIifeFunction(clientSrc, 'syncEndpointAliasBox');
const commitAliasBoxSrc = extractIifeFunction(clientSrc, 'commitEndpointAliasBox');

// E2 ([ap:ARDOF6] §E2): the focus-restore machinery — the ONE shared helper
// + the PURE triple matcher + the TWO real call sites. On the pre-E2 tree
// the first of these throws "function not found" (the recorded E2
// pre-implementation extraction failure, the §5.5a mutation evidence for
// groups 10–12).
const applyEntrySrc = extractIifeFunction(clientSrc, 'applySavedTabsEntry');
const receiverSrc = extractIifeFunction(clientSrc, 'handleSavedTabsDriveApply');
const restoreFocusSrc = extractIifeFunction(clientSrc, 'restoreSavedTabsRowFocus');
const tripleEqualSrc = extractIifeFunction(clientSrc, 'savedTabsRowFocusTripleEqual');

const BRIDGE_SENTINEL = extractConstString(clientSrc, 'BRIDGE_SENTINEL');
const DOCK_RUN_PREFIX = extractConstString(clientSrc, 'SAVED_TABS_DOCK_RUN_PREFIX');

function extractConstNumber(src, name) {
  const m = src.match(new RegExp('const ' + name + ' = (\\d+)'));
  if (!m) throw new Error('number const not found: ' + name);
  return parseInt(m[1], 10);
}
const LONG_TIMEOUT_REAL = extractConstNumber(clientSrc, 'CACHE_RENDER_LONG_TIMEOUT_MS');

// The ONE new sw→page listener branch, rebuilt from the REAL bytes so the
// branch tests fire the shipped code (not a harness re-implementation).
const PROGRESS_BRANCH_ANCHOR = "} else if (p.type === 'saved_tabs_drive_progress') {";
const progressBranchBody = extractBlockBodyAfter(clientSrc, PROGRESS_BRANCH_ANCHOR);
const progressBranch = new Function(
  'p', 'handleSavedTabsDriveProgress', 'GLIMPSE_VERSION', 'console',
  // The anchor begins with `}` (it is an else-if leg); give it an `if` to
  // close so the rebuilt snippet is a balanced statement.
  'if (false) {' + PROGRESS_BRANCH_ANCHOR + '\n' + progressBranchBody + '\n}'
);

// --- mock DOM / window -------------------------------------------------------
const quietConsole = { warn() {}, log() {}, error() {} };
function makeEvent() {
  return {
    _stopped: false,
    stopPropagation() { this._stopped = true; },
    preventDefault() { this._prevented = true; },
  };
}
function makeEl(tag, registry) {
  const el = {
    tagName: String(tag || 'div'),
    children: [],
    style: {},
    listeners: {},
    attributes: {},
    textContent: '',
    id: '',
    title: '',
    type: '',
    disabled: false,
    checked: false,
    value: '',
    placeholder: '',
    rows: 0,
    parentNode: null,
    appendChild(c) { c.parentNode = el; el.children.push(c); if (c.id && registry) registry[c.id] = c; return c; },
    removeChild(c) {
      const i = el.children.indexOf(c);
      if (i >= 0) el.children.splice(i, 1);
      c.parentNode = null;
      if (c.id && registry && registry[c.id] === c) delete registry[c.id];
      return c;
    },
    addEventListener(t, fn) { (el.listeners[t] = el.listeners[t] || []).push(fn); },
    removeEventListener(t, fn) {
      const l = el.listeners[t] || [];
      const i = l.indexOf(fn);
      if (i >= 0) l.splice(i, 1);
    },
    setAttribute(k, v) { el.attributes[k] = String(v); },
    getAttribute(k) { return el.attributes[k]; },
    click() { (el.listeners.click || []).slice().forEach((fn) => fn(makeEvent())); },
    change() { (el.listeners.change || []).slice().forEach((fn) => fn(makeEvent())); },
    get isConnected() {
      let n = el;
      while (n.parentNode) n = n.parentNode;
      return n === registry.__body;
    },
  };
  return el;
}
function makeDocument() {
  const registry = {};
  const body = makeEl('body', registry);
  registry.__body = body;
  const docListeners = {};
  return {
    body,
    byId: registry,
    createElement: (tag) => makeEl(tag, registry),
    getElementById: (id) => registry[id] || null,
    addEventListener(t, fn) { (docListeners[t] = docListeners[t] || []).push(fn); },
    removeEventListener(t, fn) {
      const l = docListeners[t] || [];
      const i = l.indexOf(fn);
      if (i >= 0) l.splice(i, 1);
    },
    _listeners: docListeners,
  };
}
function makeWindow() {
  const posted = [];
  const listeners = {};
  const win = {
    posted,
    postMessage(msg) { posted.push(msg); },
    addEventListener(t, fn) { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener(t, fn) {
      const l = listeners[t] || [];
      const i = l.indexOf(fn);
      if (i >= 0) l.splice(i, 1);
    },
    dispatchMessage(data) {
      const ev = { source: win, data };
      (listeners.message || []).slice().forEach((fn) => fn(ev));
    },
  };
  return win;
}
function findAll(el, pred, out) {
  out = out || [];
  if (!el || !el.children) return out;
  for (const c of el.children) {
    if (pred(c)) out.push(c);
    findAll(c, pred, out);
  }
  return out;
}

// --- the UI module builder (mirrors the real IIFE scope) ----------------------
const GID_A = 'grp-11111111-2222-4333-8444-555555555555';
function fixtureSpec(gid) {
  return {
    kind: 'slice',
    root_uuid: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
    group_id: gid || GID_A,
    group_name: 'Harness|Fixture Group[-10]',
    lightning: null,
    match_scope: 'name',
    descend_policy: 'all',
    sort_by: 'none',
    sort_direction: 'asc',
    since: null,
    headings_only: false,
    as_markdown: true,
    content_format: 'markdown',
    tree_mode: 'render_tree',
    focus: null,
  };
}
function fixturePayload(n, opts) {
  opts = opts || {};
  const arr = [];
  for (let i = 0; i < n; i++) {
    arr.push({
      alias: (opts.aliases && opts.aliases[i] != null) ? opts.aliases[i] : ('alias-' + i),
      title: 'Fixture Tab ' + i,
      url: 'https://workflowy.com/#/fixture' + i,
      spec: fixtureSpec(),
    });
  }
  return arr;
}

function makeUi(cfg) {
  cfg = cfg || {};
  const doc = makeDocument();
  const win = makeWindow();
  const statusLog = [];
  const statusEl = makeEl('div', {});
  let _statusText = '';
  Object.defineProperty(statusEl, 'textContent', {
    get() { return _statusText; },
    set(v) { _statusText = v; statusLog.push(v); },
  });
  const calls = { apply: [], normalize: [], minimize: 0 };
  const groups = cfg.groups || [{ id: GID_A, name: 'Harness|Fixture Group[-10]' }];
  const normalize = cfg.normalizeRenderScopeToSpec || ((spec) => {
    calls.normalize.push(spec);
    return spec;
  });
  const currentExpandGroupsCopy = cfg.currentExpandGroupsCopy || (() => groups);
  const applySavedTabsEntry = cfg.applySavedTabsEntry || (async (entry, hooks) => {
    calls.apply.push({ entry, hooks });
    if (hooks && typeof hooks.onStatus === 'function') hooks.onStatus('Applying\u2026', true);
    return { success: true };
  });
  const minimize = () => { calls.minimize++; };
  const fileImport = cfg.openSavedTabsFileImport || (() => { throw new Error('unused route in this scenario'); });
  const recentRuns = cfg.requestSavedTabsRecentDockRuns
    || (async () => ({ ok: false, error: 'unused route in this scenario' }));
  const runPayload = cfg.requestSavedTabsDockRunPayload
    || (async () => ({ ok: false, error: 'unused route in this scenario' }));
  // U01: the page's merged-settings mirror + the ONE page->SW settings
  // writer, exactly as the dialog's stepper reads/writes them. The recorder
  // writes the mirror immediately, like the real persistMdPreviewSetting.
  const extensionSettings = cfg.extensionSettings || { saved_tabs_drive_delay_seconds: 5 };
  const persistLog = [];
  const persistMdPreviewSetting = (key, value) => {
    persistLog.push({ key, value });
    extensionSettings[key] = value;
  };

  const body = [
    'let savedTabsPopupEscHandler = null;',
    'let savedTabsDriveConsoleSession = null;',
    groupIdExistsSrc,
    validateSrc,
    parseSrc,
    labelSrc,
    specKindSrc,
    setStatusSrc,
    closePopupSrc,
    NEW_FN_NAMES.map((n) => newFns[n]).join('\n'),
    dialogSrc,
    'return {',
    '  openSavedTabsImportDialog, handleSavedTabsDriveProgress,',
    '  savedTabsDriveValidateSelection, openSavedTabsDriveConsole,',
    '  requestSavedTabsDrive, cancelSavedTabsDrive,',
    '  getSession: () => savedTabsDriveConsoleSession,',
    '};',
  ].join('\n');
  const mod = new Function(
    'window', 'document', 'navigator', 'console', 'BRIDGE_SENTINEL', 'GLIMPSE_VERSION',
    'savedTabsStatusEl', 'SAVED_TABS_DOCK_RUN_PREFIX',
    'normalizeRenderScopeToSpec', 'currentExpandGroupsCopy',
    'applySavedTabsEntry', '_minimizeUuidNavigatorWidget',
    'openSavedTabsFileImport', 'requestSavedTabsRecentDockRuns', 'requestSavedTabsDockRunPayload',
    'extensionSettings', 'persistMdPreviewSetting', 'GlimpseHotkeys',
    // "The Rail, side by side": dialogs mount on the deck's float layer (glimpseFloatLayer), never
    // on document.body; the harness's stand-in layer is the fake body.
    'glimpseFloatLayer',
    body
  )(win, doc, { clipboard: undefined }, quietConsole, BRIDGE_SENTINEL, 'harness',
    statusEl, DOCK_RUN_PREFIX,
    normalize, currentExpandGroupsCopy,
    applySavedTabsEntry, minimize,
    fileImport, recentRuns, runPayload,
    extensionSettings, persistMdPreviewSetting, GlimpseHotkeys,
    () => doc.body);
  return { mod, win, doc, statusEl, statusLog, calls, extensionSettings, persistLog };
}

// Open the dialog, load a payload array, list it; return the handles the
// scenarios assert against.
function listEntries(ui, payloadArr) {
  const { mod, doc } = ui;
  mod.openSavedTabsImportDialog({});
  const pop = doc.byId['glimpse-saved-tabs-popup'];
  if (!pop) throw new Error('dialog did not open');
  const ta = findAll(pop, (c) => c.tagName === 'textarea')[0];
  ta.value = JSON.stringify(payloadArr);
  const listBtn = findAll(pop, (c) => c.textContent === 'List entries')[0];
  listBtn.click();
  const rows = findAll(pop, (c) => c.getAttribute && c.getAttribute('data-entry-index') != null);
  const goBtn = findAll(pop, (c) => /^Go \(/.test(c.textContent))[0];
  const cancelBtn = findAll(pop, (c) => c.textContent === 'Cancel')[0];
  const allBtn = findAll(pop, (c) => c.textContent === 'Select All')[0];
  const noneBtn = findAll(pop, (c) => c.textContent === 'Select None')[0];
  // U01: the header delay stepper — header children are [title, delayCtl, ✕];
  // delayCtl children are [label, −, value, +].
  const header = pop.children[0];
  const delayCtl = header.children[1];
  return { pop, ta, rows, goBtn, cancelBtn, allBtn, noneBtn, listBtn,
    header, delayCtl,
    delayLabel: delayCtl.children[0], delayDown: delayCtl.children[1],
    delayVal: delayCtl.children[2], delayUp: delayCtl.children[3] };
}
function dialogStatusText(h) {
  // The dialog's own status div: the (only) direct child of the popup between
  // the textarea and the button row — found by position, not content.
  const kids = h.pop.children;
  return kids[3].textContent;
}
function ackRequest(win, ack) {
  const reqPost = win.posted.filter((m) => m && m.kind === 'saved_tabs_drive_request').slice(-1)[0];
  if (!reqPost) throw new Error('no saved_tabs_drive_request posted');
  win.dispatchMessage({
    [BRIDGE_SENTINEL]: true,
    direction: 'sw-to-page',
    correlationId: reqPost.correlationId,
    payload: Object.assign({ type: 'bridge_response' }, ack),
  });
  return reqPost;
}
function fireProgress(ui, frame) {
  // Through the REAL branch bytes, into the REAL extracted handler.
  progressBranch(
    Object.assign({ type: 'saved_tabs_drive_progress' }, frame),
    ui.mod.handleSavedTabsDriveProgress, 'harness', quietConsole
  );
}

// --- E2 focus-restore world --------------------------------------------------
// The REAL applySavedTabsEntry (import route), the REAL
// handleSavedTabsDriveApply (Tier-2 receiver), the REAL shared helper and the
// REAL triple matcher, evaluated against the REAL admission kernel — with the
// funnel (applySliceGroupIntoDock) and the THREE selection paint-path entry
// points (setDockedSelection / updateDockRowMarkers /
// renderDockedMultiSelection) injected as recording mocks. The dock selection
// state is REAL module state inside the eval scope, initialized to the exact
// post-paint state displaySliceInDock leaves (row 0 selected, full document
// painted), so the assertions read the same bytes the live dock would.
function focusRow(uuid, ap, occurrence, text) {
  return {
    uuid: uuid,
    depth: (ap || []).length,
    text: text || uuid,
    el: null,
    kind: 'none',
    collapsed: false,
    created: null,
    modified: null,
    ancestor_path: (ap || []).slice(),
    occurrence: occurrence || 0,
  };
}
// The painted dockedListRows: a root, two children, a SECOND placement of bbb
// (same uuid + ancestor_path, occurrence 1 — the mirror case the triple exists
// for), and a grandchild.
const FOCUS_ROWS = () => ([
  focusRow('root-uuid', [], 0, 'Root'),
  focusRow('aaa', ['root-uuid'], 0, 'A'),
  focusRow('bbb', ['root-uuid'], 0, 'B'),
  focusRow('bbb', ['root-uuid'], 1, 'B (second placement)'),
  focusRow('ccc', ['root-uuid', 'aaa'], 0, 'C'),
]);
function makeFocusWorld(cfg) {
  cfg = cfg || {};
  const calls = { apply: [], sel: [], markers: 0, multi: 0, status: [], alias: [] };
  const posted = [];
  const win = { postMessage: (msg) => { posted.push(msg); } };
  const groups = cfg.groups || [{ id: GID_A, name: 'Harness|Fixture Group[-10]' }];
  const normalize = cfg.normalizeRenderScopeToSpec || ((spec) => spec);
  const currentExpandGroupsCopy = cfg.currentExpandGroupsCopy || (() => groups);
  const validate = new Function(
    'normalizeRenderScopeToSpec', 'currentExpandGroupsCopy',
    groupIdExistsSrc + '\n' + validateSrc + '\nreturn savedTabsValidateEntrySpec;'
  )(normalize, currentExpandGroupsCopy);
  const applySliceGroupIntoDock = cfg.applySliceGroupIntoDock
    || (async (gid, opts) => { calls.apply.push({ gid, opts }); return { success: true }; });
  const setDockedSelection = (idx, render) => { calls.sel.push([idx, render]); };
  const updateDockRowMarkers = () => { calls.markers++; };
  const renderDockedMultiSelection = () => { calls.multi++; };
  const setSavedTabsStatus = (msg, isOk) => { calls.status.push({ msg, ok: isOk }); };
  const setEndpointAlias = (alias) => { calls.alias.push(alias); };
  const rows = cfg.rows || FOCUS_ROWS();
  const body = [
    'let dockedListRows = ' + JSON.stringify(rows) + ';',
    // The exact post-paint state (displaySliceInDock's setDockedSelection(0, false)).
    'let dockedSelectedIndex = 0;',
    'let dockedSelectedIndices = new Set([0]);',
    'let dockedMultiAnchorIndex = 0;',
    'let dockedRenderGeneration = 7;',
    tripleEqualSrc,
    restoreFocusSrc,
    applyEntrySrc,
    receiverSrc,
    'return {',
    '  applySavedTabsEntry, handleSavedTabsDriveApply,',
    '  restoreSavedTabsRowFocus, savedTabsRowFocusTripleEqual,',
    '  getIndex: () => dockedSelectedIndex,',
    '  getSet: () => Array.from(dockedSelectedIndices).sort((a, b) => a - b),',
    '  getAnchor: () => dockedMultiAnchorIndex,',
    '  getGen: () => dockedRenderGeneration,',
    '};',
  ].join('\n');
  const mod = new Function(
    'window', 'console', 'BRIDGE_SENTINEL', 'GLIMPSE_VERSION',
    'setEndpointAlias', 'setSavedTabsStatus', 'savedTabsValidateEntrySpec',
    'applySliceGroupIntoDock', 'sinceFromWire', 'CACHE_RENDER_LONG_TIMEOUT_MS',
    'setDockedSelection', 'updateDockRowMarkers', 'renderDockedMultiSelection',
    body
  )(win, quietConsole, BRIDGE_SENTINEL, 'harness',
    setEndpointAlias, setSavedTabsStatus, validate,
    applySliceGroupIntoDock, (v) => v, LONG_TIMEOUT_REAL,
    setDockedSelection, updateDockRowMarkers, renderDockedMultiSelection);
  const onStatus = (msg, isOk) => { calls.status.push({ msg, ok: isOk }); };
  return { mod, calls, posted, hooks: { onStatus } };
}
function specWithFocus(focus) {
  const s = fixtureSpec();
  if (focus !== undefined) s.focus = focus;
  return s;
}
function lastStatus(calls) {
  return calls.status.length ? calls.status[calls.status.length - 1] : { msg: '', ok: null };
}

// ===========================================================================
(async function main() {
  // -------------------------------------------------------------------------
  console.log('\n== group 1 — selection model (§9.4 item 1) ==');
  {
    const ui = makeUi({});
    // 3 entries: index 1 invalid (no spec object AND no saved name — so not
    // name-only, follow-up 2), index 2 empty alias.
    const payload = fixturePayload(3);
    payload[1].spec = null;
    payload[1].alias = '';
    const h = listEntries(ui, payload);
    ok(h.rows.length === 3, 'one checkbox row per parsed entry');
    const cbs = h.rows.map((r) => r.children[0]);
    ok(cbs.every((cb) => cb.type === 'checkbox'), 'each row leads with a checkbox');
    ok(cbs.every((cb) => cb.checked === false), 'checkboxes default UNCHECKED (a drive is opt-in)');
    ok(cbs[0].disabled === false && cbs[2].disabled === false, 'valid entries checkable');
    ok(cbs[1].disabled === true, 'invalid entry (no spec) checkbox-disabled (export-popup idiom)');
    ok(cbs.every((cb) => (cb.listeners.click || []).length === 1),
      'checkbox carries the stop-propagation click guard (row click stays apply-only)');
    ok(h.goBtn.textContent === 'Go (0)' && h.goBtn.style.opacity === '0.5',
      'Go label live-count, dimmed at 0');
    // Toggle one checkbox -> Go (1), undimmed.
    cbs[0].checked = true; cbs[0].change();
    ok(h.goBtn.textContent === 'Go (1)' && h.goBtn.style.opacity === '1',
      'checkbox toggle updates the live count');
    // Select All / None (the invalid row never joins).
    h.allBtn.click();
    ok(h.goBtn.textContent === 'Go (2)', 'Select All checks every VALID entry only');
    ok(cbs[0].checked && cbs[2].checked && !cbs[1].checked, 'Select All flipped the right boxes');
    h.noneBtn.click();
    ok(h.goBtn.textContent === 'Go (0)' && h.goBtn.style.opacity === '0.5',
      'Select None clears the selection and re-dims Go');
    // Row-clicking does NOT change the selection.
    h.rows[0].click();
    ok(h.goBtn.textContent === 'Go (0)', 'row text click applies, never selects');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 2 — Go with an empty selection: hint, no request (§9.4 item 2) ==');
  {
    const ui = makeUi({});
    const h = listEntries(ui, fixturePayload(2));
    h.goBtn.click();
    await flush();
    ok(win_len(ui.win, 'saved_tabs_drive_request') === 0, 'NO drive request posted');
    const st = dialogStatusText(h);
    ok(/tick at least one entry/i.test(st), 'a status HINT is painted (neutral, no refusal)');
    ok(ui.mod.getSession() === null, 'no drive session exists');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 3 — duplicate-alias refusal (§9.4 item 3, D3) ==');
  {
    // Pure validator matrix first.
    const v = makeUi({}).mod.savedTabsDriveValidateSelection;
    const E = (aliases) => aliases.map((a) => ({ alias: a, title: '', url: '', spec: {} }));
    ok(v(E(['a', 'b']), [0, 1]).ok === true, 'validator: distinct aliases pass');
    ok(v(E(['', '']), [0, 1]).ok === true, 'validator: empty aliases are EXEMPT and allowed');
    ok(v(E(['a']), [0]).ok === true, 'validator: a singleton passes');
    ok(v(E(['a', 'a']), [0, 1]).ok === false, 'validator: a duplicate refuses');
    ok(v(E(['a', 'a']), [0, 1]).error.includes('a'), 'validator: the refusal NAMES the duplicate');
    {
      const r = v(E(['x', 'x', 'x', 'y', 'y']), [0, 1, 2, 3, 4]);
      ok(r.ok === false && r.error.includes('x') && r.error.includes('y'),
        'validator: every duplicated alias named');
      ok((r.error.match(/x/g) || []).length === 1, 'validator: each duplicate named ONCE');
    }
    ok(v(E(['', 'a', 'a']), [0, 1, 2]).ok === false, 'validator: empty alias does not shield a dup');
    // Dialog-level: select the two dup rows + one singleton -> refusal, no post.
    const ui = makeUi({});
    const h = listEntries(ui, fixturePayload(3, { aliases: ['dup', 'dup', 'solo'] }));
    const cbs = h.rows.map((r) => r.children[0]);
    cbs[0].checked = true; cbs[0].change();
    cbs[1].checked = true; cbs[1].change();
    cbs[2].checked = true; cbs[2].change();
    h.goBtn.click();
    await flush();
    ok(win_len(ui.win, 'saved_tabs_drive_request') === 0, 'duplicate selection: NO request posted');
    const st = dialogStatusText(h);
    ok(st.includes('dup') && !st.includes('solo'), 'the dialog refusal names each DUPLICATE alias only');
    ok(ui.mod.getSession() === null, 'no session after a validation refusal');
    // Deselect one dup -> the SAME selection posts fine.
    cbs[1].checked = false; cbs[1].change();
    h.goBtn.click();
    await flush(2);
    ok(win_len(ui.win, 'saved_tabs_drive_request') === 1, 'after deselecting, Go posts the request');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 4 — valid Go: request shape, list order, spec identity, console (§9.4 item 4) ==');
  {
    const ui = makeUi({});
    const payload = fixturePayload(3);
    const h = listEntries(ui, payload);
    const cbs = h.rows.map((r) => r.children[0]);
    // Click order REVERSED (2 then 0); list order must win.
    cbs[2].checked = true; cbs[2].change();
    cbs[0].checked = true; cbs[0].change();
    h.goBtn.click();
    await flush(2);
    const reqs = ui.win.posted.filter((m) => m && m.kind === 'saved_tabs_drive_request');
    ok(reqs.length === 1, 'exactly ONE saved_tabs_drive_request posted');
    const req = reqs[0];
    ok(req[BRIDGE_SENTINEL] === true && req.direction === 'page-to-sw',
      'request rides the established bridge envelope');
    ok(typeof req.payload.drive_id === 'string' && req.payload.drive_id.length > 0,
      'drive_id present on the request');
    ok(Array.isArray(req.payload.entries) && req.payload.entries.length === 2,
      'two entries in the request');
    ok(req.payload.entries[0].url === payload[0].url && req.payload.entries[1].url === payload[2].url,
      'entries in LIST order regardless of click order');
    ok(req.payload.entries.every((e) => {
      const keys = Object.keys(e).sort();
      return keys.join(',') === 'alias,spec,title,url';
    }), 'each request entry carries exactly {alias,title,url,spec}');
    // Ack -> console mode + session registered.
    ackRequest(ui.win, { ok: true, drive_id: req.payload.drive_id });
    await flush();
    const session = ui.mod.getSession();
    ok(session && session.drive_id === req.payload.drive_id, 'session registered with the ack drive_id');
    ok(session.entries === req.payload.entries, 'session holds the posted entries array');
    ok(session.rows.size === 2, 'the dialog row map covers the drive entries');
    // SPEC BY OBJECT IDENTITY: the posted spec IS the parsed entry's spec
    // object (the row records hold the parser's own objects).
    ok(req.payload.entries[0].spec === session.rows.get(0).entry.spec
      && req.payload.entries[1].spec === session.rows.get(1).entry.spec,
      'each spec rides BY OBJECT IDENTITY from the parsed payload (no clone/mutation)');
    // The drive entry_index maps to the right LIST row (selected {0,2} ->
    // drive index 1 is list row 2).
    ok(session.rows.get(1).el === h.rows[2], 'drive entry_index 1 maps to LIST row 2');
    // Console mode: inputs frozen, Cancel shown, Go hidden.
    ok(h.ta.disabled === true && h.listBtn.disabled === true, 'console mode freezes the input row');
    ok(h.goBtn.style.display === 'none' && h.cancelBtn.style.display !== 'none'
      && h.cancelBtn.disabled === false, 'console mode: Go hidden, Cancel live');
    ok(cbs.every((cb) => cb.disabled === true), 'checkboxes frozen during the drive');
    ok(h.rows.every((r) => r.style.pointerEvents === 'none'),
      'row-click apply parked while the dialog is the drive console');
    const stateLines = h.rows.map((r) => r.children[1].children[2]);
    ok(stateLines.slice(0, 3).filter((s) => s.style.display !== 'none').length === 2,
      'state lines visible on the driven rows only');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 5 — progress painting, summary, retention after close (§9.4 item 5) ==');
  {
    const ui = makeUi({});
    const h = listEntries(ui, fixturePayload(2));
    const cbs = h.rows.map((r) => r.children[0]);
    cbs[0].checked = true; cbs[0].change();
    cbs[1].checked = true; cbs[1].change();
    h.goBtn.click();
    await flush(2);
    ackRequest(ui.win, { ok: true, drive_id: undefined });
    await flush();
    // The SW echoes the posted id (the harness ack above mints none) — use
    // the request's own id, exactly like the real SW.
    const driveId = ui.mod.getSession().drive_id;
    const statusAtStart = ui.statusLog.length;
    // creating -> awaiting-ready -> applying -> settled on entry 0.
    fireProgress(ui, { drive_id: driveId, kind: 'entry', entry_index: 0, total: 2, name: 'alias-0', state: 'creating' });
    fireProgress(ui, { drive_id: driveId, kind: 'entry', entry_index: 0, total: 2, name: 'alias-0', state: 'awaiting-ready' });
    fireProgress(ui, { drive_id: driveId, kind: 'entry', entry_index: 0, total: 2, name: 'alias-0', state: 'applying' });
    fireProgress(ui, { drive_id: driveId, kind: 'entry', entry_index: 0, total: 2, name: 'alias-0', state: 'settled' });
    const row0State = h.rows[0].children[1].children[2];
    ok(/\u2713 settled/.test(row0State.textContent), 'row 0 paints the settled state');
    ok(ui.statusLog.length === statusAtStart + 4, 'setSavedTabsStatus called per transition');
    ok(ui.statusLog[statusAtStart].includes('creating') && ui.statusLog[statusAtStart].includes('alias-0'),
      'status mirrors the first transition');
    // Entry 1 fails with phase+reason.
    fireProgress(ui, { drive_id: driveId, kind: 'entry', entry_index: 1, total: 2, name: 'alias-1', state: 'failed', detail: 'applying — render timed out' });
    const row1State = h.rows[1].children[1].children[2];
    ok(row1State.textContent.includes('failed') && row1State.textContent.includes('render timed out'),
      'failed row carries the phase+reason detail');
    ok(row1State.style.color === '#e08a8a', 'failed row paints red');
    ok(ui.statusEl.style.color === '#e08a8a', 'the status line goes red on a failure');
    // A frame for a FOREIGN drive_id never paints.
    const before = ui.statusLog.length;
    fireProgress(ui, { drive_id: 'std-other', kind: 'entry', entry_index: 0, total: 9, name: 'x', state: 'settled' });
    ok(ui.statusLog.length === before, 'a foreign-drive frame is ignored');
    // The summary paints counts + named failures and clears the session.
    fireProgress(ui, { drive_id: driveId, kind: 'summary', total: 2, applied: 1, skipped: 0,
      failed: [{ name: 'alias-1', phase: 'applying', reason: 'render timed out' }] });
    const summary = ui.statusEl.textContent;
    ok(summary.includes('1 applied') && summary.includes('alias-1') && summary.includes('render timed out'),
      'summary line: counts + the named failure with phase+reason');
    ok(ui.statusEl.style.color === '#e08a8a', 'summary red when failures exist');
    ok(dialogStatusText(h).includes('1 applied'), 'the dialog also paints the summary');
    ok(h.cancelBtn.disabled === true, 'Cancel disabled once the drive is over');
    ok(ui.mod.getSession() === null, 'session cleared at the summary (G8)');
    // The summary REMAINS on the status line after the dialog closes.
    h.pop.children[0].children[2].click();  // the header ✕ -> closeSavedTabsPopup (header: title · stepper · ✕)
    ok(ui.doc.byId['glimpse-saved-tabs-popup'] == null, 'dialog closed');
    ok(ui.statusEl.textContent === summary, 'status line RETAINS the summary after dialog close');
    // A late frame for the finished drive is ignored (no console to paint).
    const after = ui.statusLog.length;
    fireProgress(ui, { drive_id: driveId, kind: 'entry', entry_index: 1, total: 2, name: 'alias-1', state: 'settled' });
    ok(ui.statusLog.length === after, 'a post-summary frame for the same drive is ignored');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 5b — dialog closed MID-DRIVE: status keeps painting ==');
  {
    const ui = makeUi({});
    const h = listEntries(ui, fixturePayload(2));
    const cbs = h.rows.map((r) => r.children[0]);
    cbs[0].checked = true; cbs[0].change();
    h.goBtn.click();
    await flush(2);
    ackRequest(ui.win, { ok: true, drive_id: 'drive-midclose' });
    await flush();
    h.pop.children[0].children[2].click();  // close the dialog mid-drive (header: title · stepper · ✕)
    ok(ui.mod.getSession() && ui.mod.getSession().pop.isConnected === false,
      'session survives the close; its dialog is detached');
    fireProgress(ui, { drive_id: 'drive-midclose', kind: 'entry', entry_index: 0, total: 2, name: 'alias-0', state: 'settled' });
    ok(ui.statusEl.textContent.includes('settled: alias-0'),
      'status line keeps painting with the dialog closed (the retained surface)');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 6 — Cancel: one post, then disabled (§9.4 item 6) ==');
  {
    const ui = makeUi({});
    const h = listEntries(ui, fixturePayload(3, { aliases: ['a', 'b', 'c'] }));
    h.allBtn.click();
    h.goBtn.click();
    await flush(2);
    ackRequest(ui.win, { ok: true, drive_id: 'drive-cancel' });
    await flush();
    h.cancelBtn.click();
    const cancels = ui.win.posted.filter((m) => m && m.kind === 'saved_tabs_drive_cancel');
    ok(cancels.length === 1, 'exactly ONE saved_tabs_drive_cancel posted');
    ok(cancels[0][BRIDGE_SENTINEL] === true && cancels[0].direction === 'page-to-sw',
      'cancel rides the bridge envelope');
    ok(cancels[0].payload && cancels[0].payload.drive_id === 'drive-cancel',
      'cancel carries the drive_id (nothing else required)');
    ok(h.cancelBtn.disabled === true, 'the Cancel button disables itself');
    h.cancelBtn.click();
    ok(ui.win.posted.filter((m) => m && m.kind === 'saved_tabs_drive_cancel').length === 1,
      'a second Cancel click posts NOTHING (cancel-once)');
    // The SW then reports the in-flight entry settled + the rest skipped —
    // those rows paint.
    fireProgress(ui, { drive_id: 'drive-cancel', kind: 'entry', entry_index: 0, total: 3, name: 'a', state: 'settled' });
    fireProgress(ui, { drive_id: 'drive-cancel', kind: 'entry', entry_index: 1, total: 3, name: 'b', state: 'skipped-cancelled' });
    fireProgress(ui, { drive_id: 'drive-cancel', kind: 'entry', entry_index: 2, total: 3, name: 'c', state: 'skipped-cancelled' });
    fireProgress(ui, { drive_id: 'drive-cancel', kind: 'summary', total: 3, applied: 1, skipped: 2, failed: [] });
    ok(h.rows[1].children[1].children[2].textContent.includes('skipped-cancelled'),
      'the summary\u2019s skipped entries are painted on their rows');
    ok(ui.statusEl.textContent.includes('2 skipped-cancelled'), 'the summary reports the cancellation');
    ok(ui.statusEl.style.color === '#7ec87e', 'a failure-free (cancelled) summary is green');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 7 — refused drive request paints loudly (§9.4 item 7) ==');
  {
    const ui = makeUi({});
    const h = listEntries(ui, fixturePayload(2));
    h.allBtn.click();
    h.goBtn.click();
    await flush(2);
    ackRequest(ui.win, { ok: false, error: 'a saved-tabs drive is already active (drive_id std-x) — one drive at a time (G10)' });
    await flush();
    const st = dialogStatusText(h);
    ok(/refused/i.test(st) && st.includes('already active'), 'the refusal paints LOUDLY in the dialog');
    ok(/refused/i.test(ui.statusEl.textContent) && ui.statusEl.style.color === '#e08a8a',
      'the refusal paints LOUDLY on the widget status line (red)');
    ok(ui.mod.getSession() === null, 'no session for a refused drive');
    // The console REVERTS to the selection list — controls restored.
    ok(h.ta.disabled === false && h.listBtn.disabled === false, 'input row restored after a refusal');
    ok(h.goBtn.style.display !== 'none' && h.cancelBtn.style.display === 'none',
      'Go restored, Cancel hidden after a refusal');
    ok(h.rows.every((r) => r.style.pointerEvents !== 'none'),
      'row-click apply live again after a refusal');
    // A timeout (no ack at all) is the same loud refusal path.
    // (The sender resolves {ok:false} — covered structurally by the sender
    // idiom; not waited out here.)
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 8 — REGRESSION: single-entry row-click apply (§9.4 item 8 / §9.2-6a) ==');
  {
    const ui = makeUi({});
    const payload = fixturePayload(2);
    const h = listEntries(ui, payload);
    const cbs = h.rows.map((r) => r.children[0]);
    // Checkbox toggling NEVER applies.
    cbs[0].checked = true; cbs[0].change();
    ok(ui.calls.apply.length === 0, 'checkbox toggling never applies');
    // Click a valid row's TEXT: the existing validate -> close -> minimize ->
    // apply flow runs with the CLICKED entry.
    h.rows[0].click();
    await flush();
    ok(ui.calls.apply.length === 1, 'row text click invokes the existing apply flow once');
    ok(ui.calls.apply[0].entry.url === payload[0].url, 'the CLICKED entry is the one applied');
    ok(ui.calls.apply[0].entry.spec === payload[0].spec
      || ui.calls.apply[0].entry.url === payload[0].url,
      'the parsed entry object itself reaches the apply flow');
    ok(ui.calls.normalize.length >= 1 && ui.calls.normalize[0] === ui.calls.apply[0].entry.spec,
      'the existing validate kernel ran on the entry first (admission first)');
    ok(ui.calls.minimize === 1, 'the widget minimizes on admission (the anchor-popup idiom)');
    ok(ui.doc.byId['glimpse-saved-tabs-popup'] == null, 'the dialog closes on admission');
    ok(typeof ui.calls.apply[0].hooks.onStatus === 'function',
      'apply status routes through the onStatus hook (to setSavedTabsStatus)');
    // An invalid row's click applies nothing.
    const ui2 = makeUi({});
    const p2 = fixturePayload(1);
    p2[0].spec = null;
    const h2 = listEntries(ui2, p2);
    h2.rows[0].click();
    await flush();
    ok(ui2.calls.apply.length === 0, 'an invalid entry row never applies');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group U01-1 — the dialog comes back after a summary (§9.2 case 1: SkyView item 3) ==');
  {
    const ui = makeUi({});
    const h = listEntries(ui, fixturePayload(4));
    const cbs = h.rows.map((r) => r.children[0]);
    h.allBtn.click();
    ok(h.goBtn.textContent === 'Go (4)', 'four entries ticked for the drive');
    h.goBtn.click();
    await flush(2);
    ackRequest(ui.win, { ok: true, drive_id: 'drive-revert' });
    await flush();
    const driveId = ui.mod.getSession().drive_id;
    fireProgress(ui, { drive_id: driveId, kind: 'entry', entry_index: 0, total: 4, name: 'alias-0', state: 'settled' });
    fireProgress(ui, { drive_id: driveId, kind: 'entry', entry_index: 1, total: 4, name: 'alias-1', state: 'settled' });
    fireProgress(ui, { drive_id: driveId, kind: 'entry', entry_index: 2, total: 4, name: 'alias-2', state: 'failed', detail: 'awaiting-ready — cancelled (cancelled by user) — the tab stays open' });
    fireProgress(ui, { drive_id: driveId, kind: 'summary', total: 4, applied: 2, skipped: 0,
      failed: [{ name: 'alias-2', phase: 'awaiting-ready', reason: 'cancelled' }] });
    // The dialog is EDITABLE again: inputs, list controls, rows.
    ok(h.ta.disabled === false && h.listBtn.disabled === false,
      'textarea + List restored after the summary');
    ok(h.allBtn.disabled === false && h.noneBtn.disabled === false, 'Select All/None enabled again');
    ok(h.goBtn.style.display !== 'none', 'Go visible again');
    ok(h.goBtn.textContent === 'Go (2)', 'Go (2) — exactly the still-ticked rows re-drive');
    ok(h.cancelBtn.style.display === 'none', 'Cancel hidden once the drive is over');
    ok(cbs.every((cb) => cb.disabled === false), 'every valid row checkbox enabled');
    ok(h.rows.every((r) => r.style.pointerEvents === ''), 'row clicks live again (pointerEvents "")');
    ok(cbs[0].checked === false && cbs[1].checked === false, 'the SETTLED rows are unticked for Dan');
    ok(cbs[2].checked === true && cbs[3].checked === true, 'the failed / untouched rows stay ticked');
    const row0State = h.rows[0].children[1].children[2];
    ok(/\u2713 settled/.test(row0State.textContent), "row 0's outcome line still reads '✓ settled' (kept, not wiped)");
    ok(dialogStatusText(h).includes('The list is live again'), 'the dialog message ends with the resume hint');
    ok(ui.mod.getSession() === null, 'session cleared at the summary');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group U01-2 — the dialog comes back after an ABORTED frame (§9.2 case 2) ==');
  {
    const ui = makeUi({});
    const h = listEntries(ui, fixturePayload(3));
    const cbs = h.rows.map((r) => r.children[0]);
    h.allBtn.click();
    h.goBtn.click();
    await flush(2);
    ackRequest(ui.win, { ok: true, drive_id: 'drive-abort' });
    await flush();
    fireProgress(ui, { drive_id: 'drive-abort', kind: 'entry', entry_index: 0, total: 3, name: 'alias-0', state: 'settled' });
    fireProgress(ui, {
      drive_id: 'drive-abort', kind: 'aborted', entry_index: 1, total: 3,
      detail: 'the extension service worker was reclaimed mid-drive; interrupted before entry 2 of 3',
    });
    ok(h.ta.disabled === false && h.goBtn.style.display !== 'none', 'editable again after an aborted frame');
    ok(h.goBtn.textContent === 'Go (2)', 'Go (2): the settled row unticked, the rest still ticked');
    ok(cbs[0].checked === false && cbs[1].checked === true && cbs[2].checked === true,
      'settled unticked; interrupted + untouched stay ticked');
    ok(dialogStatusText(h).includes('The list is live again'), 'the resume hint paints after an abort too');
    ok(ui.mod.getSession() === null, 'session cleared at the abort');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group U01-3 — re-Go after the revert re-drives exactly the rest (§9.2 case 3) ==');
  {
    const ui = makeUi({});
    const payload = fixturePayload(3);
    const h = listEntries(ui, payload);
    const cbs = h.rows.map((r) => r.children[0]);
    h.allBtn.click();
    h.goBtn.click();
    await flush(2);
    ackRequest(ui.win, { ok: true, drive_id: 'drive-first-run' });
    await flush();
    fireProgress(ui, { drive_id: 'drive-first-run', kind: 'entry', entry_index: 0, total: 3, name: 'alias-0', state: 'settled' });
    fireProgress(ui, { drive_id: 'drive-first-run', kind: 'summary', total: 3, applied: 1, skipped: 2, failed: [] });
    ok(h.goBtn.textContent === 'Go (2)', 'after the first run: Go (2)');
    h.goBtn.click();
    await flush(2);
    const reqs = ui.win.posted.filter((m) => m && m.kind === 'saved_tabs_drive_request');
    eqJson(reqs.length, 2, 'the re-Go posted ONE further saved_tabs_drive_request');
    const req = reqs[1];
    ok(req.payload.entries.length === 2
      && req.payload.entries[0].url === payload[1].url
      && req.payload.entries[1].url === payload[2].url,
      'the re-Go entries are exactly the still-ticked rows, in list order');
    // Object identity against the FIRST request's entries (both rides carry
    // the SAME parsed-payload objects — the parse owns the objects, the
    // fixture array was only the pasted text).
    ok(req.payload.entries[0].spec === reqs[0].payload.entries[1].spec
      && req.payload.entries[1].spec === reqs[0].payload.entries[2].spec,
      'the re-Go specs are the SAME parsed objects the first ride carried (no clone)');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group U01-4 — the delaying state paints its detail (§9.2 case 4) ==');
  {
    const ui = makeUi({});
    const h = listEntries(ui, fixturePayload(2));
    h.rows[0].children[0].checked = true; h.rows[0].children[0].change();
    h.rows[1].children[0].checked = true; h.rows[1].children[0].change();
    h.goBtn.click();
    await flush(2);
    ackRequest(ui.win, { ok: true, drive_id: 'drive-delay-ui' });
    await flush();
    const statusAtStart = ui.statusLog.length;
    fireProgress(ui, {
      drive_id: 'drive-delay-ui', kind: 'entry', entry_index: 1, total: 2,
      name: 'alias-1', state: 'delaying', detail: 'waiting 5 s before opening the next tab',
    });
    const row1State = h.rows[1].children[1].children[2];
    ok(row1State.textContent.includes('delaying')
      && row1State.textContent.includes('waiting 5 s before opening the next tab'),
      'the delaying row paints its detail (the wait seconds)');
    ok(ui.statusLog[ui.statusLog.length - 1].includes('delaying'),
      'the status line mirrors the delaying state (the non-terminal branch)');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group U01-5 — the header delay stepper (§9.2 case 5) ==');
  {
    const ui = makeUi({});
    const h = listEntries(ui, fixturePayload(2));
    ok(h.delayLabel.textContent === 'Delay between tabs', 'the stepper sits in the header with its label');
    ok(h.delayVal.textContent === '5 s', 'the stepper shows the merged value (5 s on a fresh profile)');
    h.delayUp.click();
    eqJson(ui.persistLog, [{ key: 'saved_tabs_drive_delay_seconds', value: 6 }],
      '+ writes current+1 through exactly ONE persistMdPreviewSetting call');
    ok(h.delayVal.textContent === '6 s', 'the stepper repaints 6 s');
    h.delayDown.click();
    eqJson(ui.persistLog[ui.persistLog.length - 1], { key: 'saved_tabs_drive_delay_seconds', value: 5 },
      '− writes current−1');
    // Bounds: at 60, + records nothing; at 0, − records nothing (the ONE clamp
    // authority decides — the stepper only offers, mergeExtensionSettings disposes).
    ui.extensionSettings.saved_tabs_drive_delay_seconds = 60;
    const atSixty = ui.persistLog.length;
    h.delayUp.click();
    eqJson(ui.persistLog.length, atSixty, 'at 60, + records NOTHING (already at the bound)');
    ui.extensionSettings.saved_tabs_drive_delay_seconds = 0;
    const atZero = ui.persistLog.length;
    h.delayDown.click();
    eqJson(ui.persistLog.length, atZero, 'at 0, − records NOTHING (0 = no delay is a legal value)');
    ui.extensionSettings.saved_tabs_drive_delay_seconds = 5;
    // The stepper stays usable DURING a drive (never disabled by console mode).
    h.allBtn.click();
    h.goBtn.click();
    await flush(2);
    ackRequest(ui.win, { ok: true, drive_id: 'drive-stepper-live' });
    await flush();
    ok(h.delayUp.disabled === false && h.delayDown.disabled === false,
      'the stepper buttons are never disabled by console mode');
    // The status div is still the popup's 4th child (header, help, textarea,
    // status — the stepper lives INSIDE the header, the child order is unchanged).
    ok(h.pop.children[3].textContent.includes('Drive accepted'),
      'the status div is still pop.children[3]');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group U01-6 — the superseded note (§9.2 case 6) ==');
  {
    const ui = makeUi({});
    const h = listEntries(ui, fixturePayload(2));
    h.rows[0].children[0].checked = true; h.rows[0].children[0].change();
    h.goBtn.click();
    await flush(2);
    ackRequest(ui.win, { ok: true, drive_id: 'drive-new', superseded_drive_id: 'std-old-drive' });
    await flush();
    const st = dialogStatusText(h);
    ok(st.includes('earlier drive') && st.includes('stopped first'),
      'an accepted ack carrying superseded_drive_id says an earlier drive was stopped');
    ok(ui.mod.getSession() && ui.mod.getSession().drive_id === 'drive-new',
      'the session registered for the NEW drive');
    // No superseded id → no note.
    const ui2 = makeUi({});
    const h2 = listEntries(ui2, fixturePayload(1));
    h2.rows[0].children[0].checked = true; h2.rows[0].children[0].change();
    h2.goBtn.click();
    await flush(2);
    ackRequest(ui2.win, { ok: true, drive_id: 'drive-solo' });
    await flush();
    ok(!dialogStatusText(h2).includes('earlier drive'), 'no superseded note without superseded_drive_id');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group U02-F — the tab\u2019s saved name beside the label (U02 follow-up, [ap:DLGU66]) ==');
  {
    // Row shape: row.children = [checkbox, textCol]; textCol.children =
    // [labelLine, detailLine, stateEl]. The label line keeps its text; the
    // saved name (alias) rides as ONE child span to its right, in parentheses.
    const ui = makeUi({});
    const payload = fixturePayload(3, { aliases: ['Jim chats', 'Harness|Fixture Group[-10]', ''] });
    payload.push({ alias: 'Solo raw', title: 't', spec: { kind: 'raw_root', root_uuid: 'f9882ac6-9883-4295-a304-391db426172a' } });
    const h = listEntries(ui, payload);
    const labelOf = (i) => h.rows[i].children[1].children[0];
    eqJson(labelOf(0).textContent, '\u25B8 Harness|Fixture Group[-10]',
      'the label text itself is unchanged (slice group name)');
    eqJson(labelOf(0).children.length, 1, 'one saved-name span beside the label');
    eqJson(labelOf(0).children[0].tagName, 'span', 'the saved name is an inline span (to the right)');
    eqJson(labelOf(0).children[0].textContent, '  (Jim chats)', 'saved name in parentheses: "Recruiting  (Jim chats)" shape');
    eqJson(labelOf(1).children.length, 0, 'an alias equal to the label adds nothing (no duplicate)');
    eqJson(labelOf(2).children.length, 0, 'an empty alias (unnamed tab) adds nothing');
    eqJson(labelOf(3).textContent, '\u25B8 Solo raw', 'raw_root label is its alias (unchanged)');
    eqJson(labelOf(3).children.length, 0, 'raw_root: the alias already is the label — no parentheses');
    // Display only: the parsed entries are not touched and the row click
    // still applies the entry as before.
    h.rows[0].click();
    await flush();
    ok(ui.calls.apply.length === 1 && ui.calls.apply[0].entry.alias === 'Jim chats',
      'row click still applies that entry; its alias unchanged (display only)');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group F2-1 — spec-less saved tabs: NAME-ONLY rows in the import list (follow-up 2) ==');
  {
    const TAG = '\uD83C\uDFF7\uFE0F';
    const INFO = TAG + ' These don\u2019t have a saved specification, so they will be imported simply to set the target tab name.';
    const ui = makeUi({});
    // 0 full-spec · 1 NAME-ONLY (spec null + alias) · 2 full-spec · 3 spec-less UNNAMED
    const payload = fixturePayload(4, { aliases: ['a0', 'Jim chats', 'a2', ''] });
    payload[1].spec = null;
    payload[3].spec = null;
    const h = listEntries(ui, payload);
    const list = h.rows[0].parentNode;
    eqJson(list.children[0].textContent, INFO, 'the info bar is the FIRST child of the checklist, with the emoji + Dan\u2019s sentence');
    ok(list.children[0].getAttribute('data-entry-index') == null, 'the info bar is not an entry row');
    const cbs = h.rows.map((r) => r.children[0]);
    const labelOf = (i) => h.rows[i].children[1].children[0];
    eqJson(labelOf(1).textContent, TAG + ' Jim chats', 'name-only row: 🏷️ marker + its saved name as the label');
    eqJson(labelOf(1).children.length, 0, 'name-only row: no "(name)" suffix (the label already is the name)');
    ok(cbs[1].disabled === false, 'name-only row: SELECTABLE');
    ok(h.rows[1].style.opacity !== '0.62', 'name-only row: not greyed');
    ok(cbs[3].disabled === true && h.rows[3].style.opacity === '0.62',
      'spec-less row with NO saved name stays greyed and unselectable');
    ok(/\u2715 /.test(labelOf(3).textContent), 'the unnamed spec-less row keeps its old \u2715 label');
    eqJson(labelOf(0).textContent, '\u25B8 Harness|Fixture Group[-10]', 'full-spec rows unchanged');
    ok(/name-only/.test(dialogStatusText(h)), 'the parse status counts the name-only entries');
    // Select All includes the name-only row, never the unnamed one.
    h.allBtn.click();
    eqJson(h.goBtn.textContent, 'Go (3)', 'Select All ticks full-spec + name-only (3), never the unnamed row');
    ok(cbs[0].checked && cbs[1].checked && cbs[2].checked && !cbs[3].checked, 'Select All flipped the right boxes');
    // A name-only row click applies NOTHING to this tab.
    h.rows[1].click();
    await flush();
    eqJson(ui.calls.apply.length, 0, 'name-only row click: no single-entry apply');
    ok(!!ui.doc.byId['glimpse-saved-tabs-popup'], 'name-only row click: the dialog stays open');
    // Go: the name-only entry rides the drive request with spec null, in list order.
    h.goBtn.click();
    await flush(2);
    const req = ui.win.posted.filter((m) => m && m.kind === 'saved_tabs_drive_request').slice(-1)[0];
    ok(!!req, 'Go posted one drive request');
    const ents = req.payload.entries;
    eqJson(ents.map((e) => e.alias), ['a0', 'Jim chats', 'a2'], 'entries in list order, name-only included');
    ok(ents[1].spec === null && ents[0].spec && ents[0].spec.group_id === GID_A,
      'the name-only entry carries spec null; the full-spec entries keep their spec');
    ackRequest(ui.win, { ok: true, drive_id: req.payload.drive_id });
    await flush();
    ok(cbs[1].disabled === true, 'console mode locks the name-only checkbox too');
    const did = req.payload.drive_id;
    fireProgress(ui, { drive_id: did, kind: 'entry', entry_index: 1, total: 3, name: 'Jim chats', state: 'settled' });
    fireProgress(ui, { drive_id: did, kind: 'summary', applied: 1, failed: [], skipped: 0 });
    await flush();
    ok(cbs[1].checked === false && cbs[1].disabled === false,
      'after the drive: the settled name-only entry is unticked and editable again (drivable)');
    ok(cbs[3].disabled === true, 'after the drive: the unnamed spec-less row is still greyed');
  }
  {
    // No name-only entry → no info bar.
    const ui = makeUi({});
    const h = listEntries(ui, fixturePayload(2));
    const list = h.rows[0].parentNode;
    ok(list.children[0].getAttribute('data-entry-index') === '0',
      'no name-only entry → no info bar (the first child is row 0)');
    const ui2 = makeUi({});
    const p2 = fixturePayload(2, { aliases: ['a0', ''] });
    p2[1].spec = null;
    const h2 = listEntries(ui2, p2);
    ok(h2.rows[0].parentNode.children[0].getAttribute('data-entry-index') === '0',
      'an UNNAMED spec-less entry alone does not raise the info bar');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group F2-2 — the EXPORT popup writes named spec-less tabs NAME-ONLY (follow-up 2) ==');
  {
    const doc = makeDocument();
    const statusLog = [];
    const downloads = [];
    const spec0 = fixtureSpec();
    const rows = [
      { tab_id: 1, endpoint_id: 'ep-1', alias: 'Full', title: 'WFY - Full', url: 'https://workflowy.com/', responded: true,
        result: { ok: true, modal_payload_kind: 'cache_render_spec', render_spec: spec0 } },
      { tab_id: 2, endpoint_id: 'ep-2', alias: 'Jim chats', title: 'WFY - Jim chats', url: 'https://workflowy.com/', responded: true,
        result: { ok: false, error: 'The docked Markdown modal is DOM-sourced but NOT frozen, so its current tree is not reproducible.' } },
      { tab_id: 3, endpoint_id: 'ep-3', alias: '', title: 'WorkFlowy', url: 'https://workflowy.com/', responded: true,
        result: { ok: false, error: 'The docked Markdown modal is not currently showing a cache-sourced render or a frozen DOM view.' } },
      { tab_id: 4, endpoint_id: 'tab-4', alias: '', title: 'Old tab', url: 'https://workflowy.com/', responded: false, result: null },
    ];
    const exp = new Function(
      'document', 'navigator', 'console', 'GLIMPSE_VERSION', 'setSavedTabsStatus',
      'savedTabsSpecKind', 'savedTabsExportFilename', 'downloadTextFile', 'fallbackCopy',
      'glimpseFloatLayer',   // "The Rail, side by side": the dialog mounts on the deck's float layer
      'let savedTabsPopupEscHandler = null;\n' + closePopupSrc + '\n' + classifySrc + '\n'
      + rowDisplayNameSrc + '\n' + buildPayloadSrc + '\n' + buildJsonSrc + '\n' + exportPopupSrc
      + '\nreturn { showSavedTabsExportPopup };'
    )(doc, { clipboard: undefined }, quietConsole, 'harness', (m) => statusLog.push(m),
      (sp) => (sp && sp.kind) || 'raw_root', () => 'saved-tabs.json',
      (text, fname) => downloads.push({ text, fname }), (text, done) => done(true),
      () => doc.body);
    exp.showSavedTabsExportPopup(rows);
    const pop = doc.byId['glimpse-saved-tabs-popup'];
    const rowEls = findAll(pop, (c) => c.getAttribute && c.getAttribute('data-endpoint-id') != null);
    eqJson(rowEls.length, 4, 'every collected tab is listed');
    const cb = (i) => rowEls[i].children[0];
    ok(!cb(0).disabled && cb(0).checked, 'full-spec tab: selectable + checked (unchanged)');
    ok(!cb(1).disabled && cb(1).checked, 'named spec-less tab: selectable + checked (NAME-ONLY)');
    ok(rowEls[1].style.opacity !== '0.62', 'named spec-less tab: not greyed');
    ok(/name only/.test(rowEls[1].children[1].children[1].textContent), 'named spec-less tab: its detail says name only');
    ok(cb(2).disabled && !cb(2).checked && rowEls[2].style.opacity === '0.62', 'unnamed spec-less tab: greyed + unselectable (unchanged)');
    ok(cb(3).disabled && !cb(3).checked, 'non-responding tab: greyed + unselectable (unchanged)');
    const copyBtn = findAll(pop, (c) => /^Copy JSON/.test(c.textContent))[0];
    (copyBtn.listeners.click || []).forEach((fn) => fn({ shiftKey: true }));
    eqJson(downloads.length, 1, 'Shift+Copy downloads the built bytes once');
    const out = JSON.parse(downloads[0].text);
    eqJson(out.map((e) => e.alias), ['Full', 'Jim chats'], 'the payload holds the full-spec tab and the NAME-ONLY tab only');
    ok(JSON.stringify(out[0].spec) === JSON.stringify(spec0), 'the full-spec entry keeps its spec verbatim');
    eqJson(out[1], { alias: 'Jim chats', title: 'WFY - Jim chats', spec: null }, 'the name-only entry is {alias, title, spec: null}');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group F3 — the widget\u2019s Tab alias box follows every alias change; a blur never wipes it (follow-up 3) ==');
  {
    function makeAliasWorld() {
      const store = {};
      const log = [];
      const doc = { activeElement: null };
      const sessionStorage = {
        getItem: (k) => (k in store ? store[k] : null),
        setItem: (k, v) => { store[k] = String(v); log.push({ fn: 'set', v: String(v) }); },
        removeItem: (k) => { delete store[k]; log.push({ fn: 'remove' }); },
      };
      const w = new Function(
        'sessionStorage', 'document', 'registerBrokerEndpoint', 'applyTabTitleFromAlias', 'self',
        "const ENDPOINT_ALIAS_KEY = 'glimpse_endpoint_alias';\n"
        + 'let endpointAliasInputEl = null;\nlet endpointAliasBoxEdited = false;\n'
        + getAliasSrc + '\n' + setAliasSrc + '\n' + syncAliasBoxSrc + '\n' + commitAliasBoxSrc
        + '\nreturn { getEndpointAlias, setEndpointAlias, syncEndpointAliasBox, commitEndpointAliasBox,'
        + ' register: (el) => { endpointAliasInputEl = el; syncEndpointAliasBox(); },'
        + ' typed: () => { endpointAliasBoxEdited = true; }, edited: () => endpointAliasBoxEdited };'
      )(sessionStorage, doc, () => log.push({ fn: 'register' }), () => log.push({ fn: 'title' }), {});
      return { w, store, log, doc };
    }
    // The field case: widget built BEFORE the drive names the tab.
    const A = makeAliasWorld();
    const box = { value: 'junk' };
    A.w.register(box);
    eqJson(box.value, '', 'widget built on an unnamed tab: the box shows the (empty) stored alias');
    A.w.setEndpointAlias('CARTOGRAPHER DOCS');   // the drive receiver's naming step
    eqJson(box.value, 'CARTOGRAPHER DOCS', 'the drive\u2019s setEndpointAlias updates the already-built box');
    eqJson(A.store.glimpse_endpoint_alias, 'CARTOGRAPHER DOCS', 'alias stored (sessionStorage — survives reload)');
    ok(A.log.some((e) => e.fn === 'register') && A.log.some((e) => e.fn === 'title'),
      'broker re-register + title update unchanged');
    // Closing the widget = blur with no edit: nothing is written.
    const before = A.log.length;
    eqJson(A.w.commitEndpointAliasBox(box, false), false, 'blur without an edit writes nothing');
    eqJson(A.log.length, before, 'no storage/broker/title call on an unedited blur');
    // Even a STALE empty box (the pre-fix state) is never written back on blur.
    const B = makeAliasWorld();
    B.store.glimpse_endpoint_alias = 'Jim chats';
    const stale = { value: '' };
    B.w.register(stale);
    stale.value = '';   // force the stale state
    eqJson(B.w.commitEndpointAliasBox(stale, false), false, 'a stale empty box is NOT committed on blur');
    eqJson(B.store.glimpse_endpoint_alias, 'Jim chats', 'the alias survives closing the widget');
    // A real edit + blur still commits.
    box.value = '  New name  '; A.w.typed();
    eqJson(A.w.commitEndpointAliasBox(box, false), true, 'edit + blur commits');
    eqJson(A.store.glimpse_endpoint_alias, 'New name', 'the trimmed edit is stored');
    ok(A.w.edited() === false, 'the edited flag clears after a commit');
    // Deliberately clearing + Enter clears the alias.
    box.value = ''; A.w.typed();
    eqJson(A.w.commitEndpointAliasBox(box, true), true, 'clear + Enter commits the clear');
    ok(!('glimpse_endpoint_alias' in A.store), 'the alias was removed on a deliberate clear');
    // An unchanged explicit commit is a no-op.
    A.w.setEndpointAlias('Same');
    const n2 = A.log.length;
    eqJson(A.w.commitEndpointAliasBox(box, true), false, 'Enter with the value already stored is a no-op');
    eqJson(A.log.length, n2, 'no re-register churn on a no-op commit');
    // A focused box (user mid-edit) is never overwritten by a background change.
    box.value = 'typing\u2026'; A.w.typed(); A.doc.activeElement = box;
    A.w.setEndpointAlias('from the drive');
    eqJson(box.value, 'typing\u2026', 'a focused box keeps the user\u2019s in-progress text');
    A.doc.activeElement = null;

    // Wiring locks (the widget): the box is registered + filled by sync, its
    // input event marks an edit, blur commits NON-explicitly, Enter / Set
    // explicitly, the row-open path re-syncs, setEndpointAlias calls the sync.
    // "The Rail" (ui-bakeoff/fable): the alias box lives in the Tabs drawer
    // (glimpse-ui-drawers.js) and reaches the client ONLY through the api object
    // built by buildGlimpseUiApi: bindAliasInput registers + syncs the box,
    // markAliasEdited marks a typed edit, commitAlias delegates to
    // commitEndpointAliasBox (explicit only for Enter). Same contract, new home.
    const drawersSrc = require('fs').readFileSync(
      require('path').join(__dirname, '../../workflowy-glimpse-cache/glimpse-ui-drawers.js'), 'utf8');
    const navStart = drawersSrc.indexOf("const alias = el('input', 'nx-input is-grow');");
    const nav = drawersSrc.slice(navStart, drawersSrc.indexOf('container.appendChild(r1);', navStart));
    const apiSrc = clientSrc.slice(clientSrc.indexOf('function buildGlimpseUiApi()'), clientSrc.indexOf('function ensureDockedMarkdownPanel()'));
    ok(apiSrc.indexOf('bindAliasInput: (inputEl) => { endpointAliasInputEl = inputEl; try { syncEndpointAliasBox(); }') >= 0
      && drawersSrc.indexOf('api().bindAliasInput(alias)') >= 0,
      'widget: the box is registered and filled through syncEndpointAliasBox');
    ok(nav.indexOf("alias.addEventListener('input', () => api().markAliasEdited());") >= 0
      && apiSrc.indexOf('markAliasEdited: () => { endpointAliasBoxEdited = true; }') >= 0,
      'widget: typing marks the box edited');
    ok(nav.indexOf("alias.addEventListener('blur', () => api().commitAlias(alias, false));") >= 0,
      'widget: blur commits non-explicitly (only a real edit)');
    ok(nav.indexOf("if (e.key === 'Enter') { api().commitAlias(alias, true); alias.blur(); }") >= 0,
      'widget: Enter commits explicitly');
    ok(apiSrc.indexOf('commitAlias: (inputEl, explicit) => commitEndpointAliasBox(inputEl, explicit === true)') >= 0
      && drawersSrc.indexOf('setEndpointAlias(value || null)') < 0,
      'widget: the commit delegates to commitEndpointAliasBox (no direct unconditional write)');
    ok(drawersSrc.indexOf("if (document.activeElement !== tabs.els.alias) tabs.els.alias.value = api().getAlias() || '';") >= 0,
      'widget: opening the Tabs drawer re-syncs the box (never over a box being typed in)');
    ok(setAliasSrc.indexOf('syncEndpointAliasBox();') >= 0, 'setEndpointAlias keeps the box in step');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 9 — LOCKS (§9.2 negatives · §9.6 DoD · §9.9 seam) ==');
  {
    // The five §6 kinds and no sixth, in the whole client file. The U01 delay
    // SETTING key (saved_tabs_drive_delay_seconds) is admitted as a setting —
    // never a kind (it rides the set_extension_setting bridge, not the drive).
    const tokens = clientSrc.match(/saved_tabs_drive_[a-z_]+/g) || [];
    const kinds = Array.from(new Set(tokens));
    const allowed = ['saved_tabs_drive_request', 'saved_tabs_drive_progress',
      'saved_tabs_drive_cancel', 'saved_tabs_drive_apply', 'saved_tabs_drive_apply_response'];
    ok(kinds.every((k) => allowed.indexOf(k) >= 0 || k === 'saved_tabs_drive_delay_seconds'),
      'no SIXTH drive kind exists in the client (the U01 delay setting key admitted, never a kind)');
    ok(countOcc(clientSrc, "kind: 'saved_tabs_drive_request'") === 1,
      'DoD-3: exactly ONE request sender site');
    ok(countOcc(clientSrc, "kind: 'saved_tabs_drive_cancel'") === 1,
      'DoD-3: exactly ONE cancel sender site');
    ok(countOcc(clientSrc, "p.type === 'saved_tabs_drive_progress'") === 1,
      'DoD-3: exactly ONE progress listener branch');
    // The branch lives inside the ONE sw→page listener, AFTER T2A's apply branch.
    const listenerRegion = regionBetween(clientSrc,
      "window.addEventListener('message', (ev) => {", 'function getSerializedByteLength');
    ok(listenerRegion.includes("p.type === 'saved_tabs_drive_apply'"),
      'the ONE listener still carries T2A\u2019s apply branch (undisturbed)');
    ok(listenerRegion.indexOf("p.type === 'saved_tabs_drive_apply'")
      < listenerRegion.indexOf("p.type === 'saved_tabs_drive_progress'"),
      'the progress branch sits inside the ONE listener after the apply branch');
    ok((clientSrc.match(/window\.addEventListener\('message', \(ev\) => \{/g) || []).length === 1,
      'still exactly ONE sw→page window listener');
    // No second writer of the status element (§9.2-6c / §9.6-4).
    const statusWrites = countOcc(clientSrc, 'savedTabsStatusEl.style');
    const setStatusRegion = regionBetween(clientSrc,
      'function setSavedTabsStatus(message, ok)', 'function requestSavedTabsCollection');
    ok(statusWrites === countOcc(setStatusRegion, 'savedTabsStatusEl.style'),
      'DoD-4: the status element is written ONLY inside setSavedTabsStatus');
    // My new functions never touch: chrome.tabs, the broker socket, the apply
    // funnel, the status element, a poll.
    const newSrc = NEW_FN_NAMES.map((n) => newFns[n]).join('\n');
    ok(!newSrc.includes('chrome.tabs'), 'the drive UI never reaches into SW chrome.tabs (bridge kinds only)');
    ok(!newSrc.includes('sendToServer'), 'no drive-path sendToServer in the client UI');
    ok(!newSrc.includes('applySliceGroupIntoDock'), 'the drive UI never applies (the receiver does, on the new tab)');
    ok(!newSrc.includes('savedTabsStatusEl'), 'the drive UI never writes the status element directly');
    ok(!newSrc.includes('setInterval'), 'no polling in the drive UI (progress is PUSHED)');
    ok(!newFns.openSavedTabsDriveConsole.includes('setTimeout')
      && !newFns.handleSavedTabsDriveProgress.includes('setTimeout')
      && !newFns.cancelSavedTabsDrive.includes('setTimeout'),
      'the only new timer is the sender\u2019s bridge-response wait (the requestSavedTabsCollection idiom)');
    // The untouched signatures (§9.5 / §9.6-2).
    ok(/\n {2}function showSavedTabsExportPopup\(rows\)/.test(clientSrc), 'showSavedTabsExportPopup signature intact');
    ok(/\n {2}function buildSavedTabsExportPayload\(rows, selectedIds\)/.test(clientSrc), 'buildSavedTabsExportPayload signature intact');
    ok(/\n {2}function buildSavedTabsExportJson\(rows, selectedIds\)/.test(clientSrc), 'buildSavedTabsExportJson signature intact');
    ok(/\n {2}function parseSavedTabsImport\(text\)/.test(clientSrc), 'parseSavedTabsImport signature intact');
    ok(/\n {2}function savedTabsValidateEntrySpec\(entry\)/.test(clientSrc), 'savedTabsValidateEntrySpec signature intact');
    ok(/\n {2}async function applySavedTabsEntry\(entry, hooks\)/.test(clientSrc), 'applySavedTabsEntry signature intact');
    ok(/\n {2}function setSavedTabsStatus\(message, ok\)/.test(clientSrc), 'setSavedTabsStatus signature intact');
    ok(!bgSrc.includes('saved_tabs_drive_ui'), 'zero background.js footprint from this unit');
    // The export-popup idiom markers on the import rows (§9.2-1).
    ok(dialogSrc.includes('data-entry-index'), 'rows keyed by row index');
    ok(dialogSrc.includes('Select All') && dialogSrc.includes('Select None'), 'Select All/None controls exist');
    // The ONE new beacon: the drive-console opener (§9.6-8); no helper-only blocks.
    ok(countOcc(clientSrc, 'saved-tabs@drive-console') === 1,
      'exactly ONE new beacon — the drive-console opener');
    ok(!/saved-tabs@drive-(validate|sender|cancel|progress)/.test(clientSrc),
      'no helper-only beacon blocks');

    // U01 static locks (§9.2 case 7 · contract [ap:08ZEAO] §6.8).
    for (const bad of ['next entry boundary', 'in-flight entry completes']) {
      ok(clientSrc.indexOf(bad) < 0, 'the client no longer says "' + bad + '"');
    }
    eqJson(countOcc(clientSrc, "persistMdPreviewSetting('saved_tabs_drive_delay_seconds'"), 1,
      'exactly ONE page-side writer of the delay key (in openSavedTabsImportDialog)');
    const dialogAndDriveUi = dialogSrc + NEW_FN_NAMES.map((n) => newFns[n]).join('\n');
    ok(dialogAndDriveUi.indexOf('chrome.storage') < 0,
      'the dialog + drive-UI functions never touch chrome.storage (the bridge is the only channel)');
    ok(dialogSrc.indexOf('mergeExtensionSettings') >= 0
      && dialogSrc.indexOf('GlimpseHotkeys.mergeExtensionSettings') >= 0,
      'the stepper clamps through the ONE clamp authority (no shadow clamp)');
    ok(dialogSrc.indexOf('endDriveConsole') >= 0
      && clientSrc.match(/endConsole/g).length >= 5,
      'the revert rides the ONE endDriveConsole (ctx field, session field, summary + aborted calls)');
    ok(countOcc(clientSrc, 'setDriveConsoleMode(false') >= 2,
      'setDriveConsoleMode(false) is the ONLY revert mechanism (refusal path + endDriveConsole)');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 10 — E2: the shared focus-restore helper on the import route ([ap:ARDOF6] §E2) ==');
  {
    // 10a — the PURE triple matcher: uuid alone is NEVER enough.
    const w = makeFocusWorld({});
    const eq = w.mod.savedTabsRowFocusTripleEqual;
    const rows = FOCUS_ROWS();
    ok(eq(rows[2], { uuid: 'bbb', ancestor_path: ['root-uuid'], occurrence: 0 }),
      'triple match: (uuid, ancestor_path, occurrence) all equal');
    ok(!eq(rows[2], { uuid: 'bbb', ancestor_path: ['root-uuid'], occurrence: 1 }),
      'occurrence differs → NO match (uuid+ancestor_path equal is NOT enough)');
    ok(eq(rows[3], { uuid: 'bbb', ancestor_path: ['root-uuid'], occurrence: 1 }),
      'the second placement matches at occurrence 1 (the mirror case the triple exists for)');
    ok(!eq(rows[2], { uuid: 'bbb', ancestor_path: ['OTHER'], occurrence: 0 }),
      'ancestor_path differs → NO match');
    ok(!eq(rows[2], { uuid: 'aaa', ancestor_path: ['root-uuid'], occurrence: 0 }),
      'uuid differs → NO match');
    ok(!eq(rows[2], { uuid: 'bbb', ancestor_path: ['root-uuid', 'EXTRA'], occurrence: 0 }),
      'ancestor_path length differs → NO match');
    ok(eq(rows[3], { uuid: 'bbb', ancestor_path: ['root-uuid'], occurrence: '1' }),
      'a string occurrence is parsed tolerantly (hand-authored estate)');
    ok(!eq(rows[2], null) && !eq(null, { uuid: 'bbb', ancestor_path: ['root-uuid'], occurrence: 0 }),
      'null on either side never matches');
  }
  {
    // 10b — single saved row → the EXISTING plain-click paint path.
    const w = makeFocusWorld({});
    const entry = { alias: 'a', title: 't', url: 'https://workflowy.com/#/x',
      spec: specWithFocus({ mode: 'row', uuid: 'ccc', ancestor_path: ['root-uuid', 'aaa'], occurrence: 0 }) };
    const res = await w.mod.applySavedTabsEntry(entry, w.hooks);
    ok(res && res.success === true, 'single row: the import apply itself succeeds');
    eqJson(w.calls.sel, [[4, true]],
      'single row → setDockedSelection(idx, true) — the existing single-select paint path');
    ok(w.calls.multi === 0 && w.calls.markers === 0,
      'single row → the multi-select path is NOT touched');
    const st = lastStatus(w.calls);
    ok(st.ok === true && /re-selected \(1 row\)/.test(st.msg),
      'status line: green, names the re-selected focus');
    ok(st.msg.indexOf('deliberately dropped') < 0 && st.msg.indexOf('FULL document') < 0,
      'no dropped-focus / full-document wording on a restored focus');
    ok(w.calls.apply.length === 1
      && w.calls.apply[0].opts.renderTimeoutMs === LONG_TIMEOUT_REAL
      && !('focus' in w.calls.apply[0].opts),
      'the funnel call is unchanged: long timeout, NO focus in the apply opts');
    // Occurrence-exact: the second placement of bbb restores row 3, not row 2.
    const w2 = makeFocusWorld({});
    const entry2 = { alias: '', title: '', url: '',
      spec: specWithFocus({ mode: 'row', uuid: 'bbb', ancestor_path: ['root-uuid'], occurrence: 1 }) };
    await w2.mod.applySavedTabsEntry(entry2, w2.hooks);
    eqJson(w2.calls.sel, [[3, true]], 'occurrence 1 selects the SECOND placement, not the uuid\'s first row');
  }
  {
    // 10c — the exact multi-row list → the EXISTING multi-select paint path.
    const w = makeFocusWorld({});
    const entry = { alias: 'a', title: 't', url: 'u',
      spec: specWithFocus({ mode: 'rows', rows: [
        { uuid: 'aaa', ancestor_path: ['root-uuid'], occurrence: 0 },
        { uuid: 'ccc', ancestor_path: ['root-uuid', 'aaa'], occurrence: 0 },
      ] }) };
    const res = await w.mod.applySavedTabsEntry(entry, w.hooks);
    ok(res && res.success === true, 'rows: the import apply itself succeeds');
    eqJson(w.calls.sel, [], 'rows → setDockedSelection NOT called (the multi path paints)');
    eqJson(w.mod.getSet(), [1, 4], 'the selection set = exactly the saved rows');
    ok(w.mod.getIndex() === 1 && w.mod.getAnchor() === 1,
      'primary + shift-anchor = the first (topmost) saved row');
    ok(w.mod.getGen() === 8, 'the shared supersession clock bumped (a new selection intent)');
    ok(w.calls.markers === 1, 'the markers repainted once from the set');
    ok(w.calls.multi === 1, 'renderDockedMultiSelection fired once (the covering-subtree render)');
    const st = lastStatus(w.calls);
    ok(st.ok === true && /re-selected \(2 rows\)/.test(st.msg), 'status line: green, names the N rows');
  }
  {
    // 10d — FAIL SOFT: any saved row absent → full document + loud status.
    const w = makeFocusWorld({});
    const entry = { alias: 'a', title: 't', url: 'u',
      spec: specWithFocus({ mode: 'row', uuid: 'gone', ancestor_path: [], occurrence: 0 }) };
    const res = await w.mod.applySavedTabsEntry(entry, w.hooks);
    ok(res && res.success === true, 'stale row: the IMPORT STILL SUCCEEDS (never fails over focus)');
    eqJson(w.calls.sel, [], 'stale row → nothing is selected (no paint-path call)');
    ok(w.calls.multi === 0, 'stale row → no multi render');
    ok(w.mod.getIndex() === -1 && w.mod.getSet().length === 0 && w.mod.getAnchor() === -1,
      'the selection is CLEARED (the established no-selection state)');
    ok(w.calls.markers === 1, 'the markers repainted once to show nothing selected');
    const st = lastStatus(w.calls);
    ok(st.ok === false
      && st.msg.indexOf('saved focus row(s) no longer present — showing the full document') >= 0,
      'status line: the contract’s fail-soft text, loud (red)');
    // A stale row INSIDE a multi-row list fails the WHOLE restore (never a partial).
    const w2 = makeFocusWorld({});
    const entry2 = { alias: '', title: '', url: '',
      spec: specWithFocus({ mode: 'rows', rows: [
        { uuid: 'aaa', ancestor_path: ['root-uuid'], occurrence: 0 },
        { uuid: 'gone', ancestor_path: ['root-uuid'], occurrence: 0 },
      ] }) };
    await w2.mod.applySavedTabsEntry(entry2, w2.hooks);
    eqJson(w2.calls.sel, [], 'one missing row in a rows list → the WHOLE restore fails soft');
    ok(w2.calls.multi === 0 && w2.mod.getIndex() === -1, 'never a partial multi-restore');
    ok(lastStatus(w2.calls).ok === false, 'the partial-stale case is loud too');
  }
  {
    // 10e — no focus → behaviour UNCHANGED from before E2.
    const w = makeFocusWorld({});
    const res = await w.mod.applySavedTabsEntry(
      { alias: 'a', title: 't', url: 'u', spec: specWithFocus(null) }, w.hooks);
    ok(res && res.success === true, 'no focus: the apply succeeds');
    eqJson(w.calls.sel, [], 'no focus → NO selection-path call');
    ok(w.calls.multi === 0 && w.calls.markers === 0, 'no focus → no multi paint, no marker repaint');
    ok(w.mod.getIndex() === 0 && w.mod.getGen() === 7,
      'the post-paint selection state is UNTOUCHED (row 0, same generation)');
    const st = lastStatus(w.calls);
    ok(st.ok === true && st.msg.indexOf('FULL document') >= 0
      && st.msg.indexOf('deliberately dropped') < 0 && st.msg.indexOf('re-selected') < 0,
      'status line: the plain full-document success, no dropped-focus wording');
    // A spec with NO focus key at all is the same unchanged path.
    const w2 = makeFocusWorld({});
    await w2.mod.applySavedTabsEntry({ alias: '', title: '', url: '', spec: specWithFocus() }, w2.hooks);
    eqJson(w2.calls.sel, [], 'absent focus key → also the unchanged path');
    ok(lastStatus(w2.calls).msg.indexOf('FULL document') >= 0, 'absent focus key → full-document status');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 11 — E2: the Tier-2 receiver restores focus from the spec (no wire change) ==');
  {
    const lastApplyResponse = (posted) => {
      const rs = posted.filter((m) => m && m.kind === 'saved_tabs_drive_apply_response');
      return rs.length ? rs[rs.length - 1].payload : null;
    };
    // 11a — single row restored on the receiver.
    const w = makeFocusWorld({});
    await w.mod.handleSavedTabsDriveApply({ drive_id: 'd', entry_index: 0, alias: '',
      spec: specWithFocus({ mode: 'row', uuid: 'ccc', ancestor_path: ['root-uuid', 'aaa'], occurrence: 0 }) });
    eqJson(w.calls.sel, [[4, true]], 'receiver: single row → the same plain-click path');
    const resp = lastApplyResponse(w.posted);
    ok(resp && resp.ok === true && resp.drive_id === 'd' && resp.entry_index === 0,
      'receiver: terminal {ok:true} echoes drive_id + entry_index');
    eqJson(Object.keys(resp).sort(), ['drive_id', 'entry_index', 'ok'],
      'the response carries NO focus field (the wire is unchanged)');
    ok(lastStatus(w.calls).ok === true && /re-selected \(1 row\)/.test(lastStatus(w.calls).msg),
      'receiver: green re-selected status on the new tab');
    // 11b — rows restored on the receiver.
    const w2 = makeFocusWorld({});
    await w2.mod.handleSavedTabsDriveApply({ drive_id: 'd', entry_index: 1, alias: 'beta',
      spec: specWithFocus({ mode: 'rows', rows: [
        { uuid: 'aaa', ancestor_path: ['root-uuid'], occurrence: 0 },
        { uuid: 'ccc', ancestor_path: ['root-uuid', 'aaa'], occurrence: 0 },
      ] }) });
    eqJson(w2.mod.getSet(), [1, 4], 'receiver: rows → the same multi-select set');
    ok(w2.calls.multi === 1 && w2.calls.markers === 1, 'receiver: the multi paint path fired');
    eqJson(w2.calls.alias, ['beta'], 'the alias is still set FIRST (step order preserved)');
    ok(lastApplyResponse(w2.posted).ok === true, 'receiver rows: {ok:true}');
    // 11c — stale on the receiver: fail soft, the entry STILL succeeds.
    const w3 = makeFocusWorld({});
    await w3.mod.handleSavedTabsDriveApply({ drive_id: 'd', entry_index: 2, alias: '',
      spec: specWithFocus({ mode: 'row', uuid: 'gone', ancestor_path: [], occurrence: 0 }) });
    ok(lastApplyResponse(w3.posted).ok === true,
      'receiver stale: the entry STILL reports {ok:true} (never fails over focus)');
    ok(w3.mod.getIndex() === -1 && w3.calls.sel.length === 0,
      'receiver stale: nothing selected, full document kept');
    const st3 = lastStatus(w3.calls);
    ok(st3.ok === false
      && st3.msg.indexOf('saved focus row(s) no longer present — showing the full document') >= 0,
      'receiver stale: the same contract fail-soft status text');
    // 11d — no focus on the receiver: unchanged.
    const w4 = makeFocusWorld({});
    await w4.mod.handleSavedTabsDriveApply({ drive_id: 'd', entry_index: 3, alias: '', spec: specWithFocus(null) });
    ok(lastApplyResponse(w4.posted).ok === true, 'receiver no-focus: {ok:true}');
    eqJson(w4.calls.sel, [], 'receiver no-focus: no selection call');
    const st4 = lastStatus(w4.calls);
    ok(st4.ok === true && st4.msg.indexOf('FULL document') >= 0
      && st4.msg.indexOf('deliberately dropped') < 0,
      'receiver no-focus: the plain full-document success, no dropped-focus wording');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 12 — E2 LOCKS (one helper · two call sites · wording · no spec/wire change) ==');
  {
    ok(countOcc(clientSrc, 'function restoreSavedTabsRowFocus(focus) {') === 1,
      'exactly ONE shared focus-restore helper exists');
    ok(countOcc(clientSrc, 'restoreSavedTabsRowFocus(') === 3,
      'the helper has exactly TWO call sites (the import apply + the drive receiver) + its own definition');
    ok(applyEntrySrc.indexOf('restoreSavedTabsRowFocus(entry.spec') >= 0,
      'the import route reads focus from the RAW entry spec (the normalized spec drops it)');
    ok(receiverSrc.indexOf('restoreSavedTabsRowFocus(p.spec') >= 0,
      'the receiver reads focus from the RAW drive-frame spec (no wire change)');
    ok(clientSrc.indexOf('deliberately dropped') < 0,
      'the "deliberately dropped" claim is GONE from the client (status lines · tooltip · help text · comments)');
    ok(clientSrc.indexOf('live DOM view — nothing to reproduce; apply a slice, a Dock render or a Lightning Rod to make this tab exportable') >= 0,
      'the export unfrozen-DOM reason is the export-specific E2 text');
    const optsRegion = (src) => regionBetween(src, 'applySliceGroupIntoDock(check.gid, {', '});');
    ok(optsRegion(applyEntrySrc).indexOf('focus') < 0 && optsRegion(receiverSrc).indexOf('focus') < 0,
      'the funnel opts carry NO focus on either route (the restore is a client-side selection, never a render-spec change)');
    ok(restoreFocusSrc.indexOf('setDockedSelection') >= 0
      && restoreFocusSrc.indexOf('renderDockedMultiSelection') >= 0
      && restoreFocusSrc.indexOf('updateDockRowMarkers') >= 0,
      'the helper rides the EXISTING selection paint paths (no second selection mechanism)');
    // 260 at E2; 272 after later units; 270 after "The Rail" (ui-bakeoff/fable)
    // replaced the widget + dock builders (−4 widget/ribbon spans, +2 UI binder
    // blocks: glimpse-ui@reader-snapshot, glimpse-ui@build-api); 245 after
    // leg 3 deleted the dead widget/ribbon code (the Anchors popup, the slice
    // table + Apply dialog, the anchor-views family, the dock-side picker, the
    // ribbon toast shield) and the 25 beacon blocks that lived inside it.
    // JR4 adds seven entry-point beacons: raw list, visible poll, drawer-open render, Details request/reply/panel and late mutation reply.
    eqJson(countOcc(clientSrc, '@beacon['), 252,
      'E2 adds NO beacon blocks (the helper is one step from its beaconed callers — curate; update this count when a later unit adds one)');
  }

  // -------------------------------------------------------------------------
  console.log(failed === 0
    ? '\nALL PASS (' + passed + ' checks — saved-tabs drive UI harness)'
    : '\n' + failed + ' FAILURE(S) of ' + (passed + failed));
  process.exit(failed === 0 ? 0 : 1);
})().catch((e) => {
  console.log('HARNESS ERROR: ' + ((e && e.stack) ? e.stack : e));
  process.exit(1);
});

function win_len(win, kind) {
  return win.posted.filter((m) => m && m.kind === kind).length;
}
