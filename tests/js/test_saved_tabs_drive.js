// ============================================================================
// test_saved_tabs_drive.js — Saved Tabs (Tier-2) unit T2A harness: the
// service-worker multi-tab DRIVE + the new-tab apply receiver.
//
// Contract: MasterSpec [ap:7S217K] rev 2 LOCKED §8 (unit T2A) — §8.4's
// required tests 1–13 live here as behavioral groups; item 14 (mutation
// sensitivity) is the PRE-IMPLEMENTATION extraction failure recorded in the
// RESULT; item 15 is the full-suite receipt (run_tests.py), not a harness
// check. The §8.2 negative postconditions and the §8.6 DoD greps are pinned
// as static locks over the REAL file bytes (group LOCKS).
//
// Construction: the repo's extraction idiom (tests/js/README.md) extended
// with injected mocks per §8.4 — the extracted drive/receiver functions are
// evaluated against a mock chrome (windows.create / tabs.create /
// tabs.onRemoved / storage.session), mock maps (tabToEndpoint,
// pendingTabReadiness), a mock sendToTab, and a scripted event log;
// assertions on the log prove sequencing (A5/G6). Functions are extracted
// VERBATIM from the real file bytes, so a rename or signature change fails
// LOUDLY at extraction. Against the pre-unit tree this harness fails at
// extraction ("function not found") — the recorded pre-implementation
// evidence (§8.4 item 14).
//
// Timing tests inject SHRUNKEN caps through the eval scope (the real
// normative constants are pinned byte-exact by the static locks — the
// harnesses GREP them, per §6 "stable surface").
//
// RUN:   node tests/js/test_saved_tabs_drive.js
// EXIT:  0 = all pass, 1 = at least one failure. Zero dependencies.
// ============================================================================
'use strict';

const fs = require('fs');
const path = require('path');

const BG_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'background.js');
const CLIENT_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'glimpse-cache-client.js');
const HOTKEYS_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'hotkeys-shared.js');
const bgSrc = fs.readFileSync(BG_FILE, 'utf8');
const clientSrc = fs.readFileSync(CLIENT_FILE, 'utf8');
const hsSrc = fs.readFileSync(HOTKEYS_FILE, 'utf8');

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

// background.js is FLAT (top level, column 0). The newline-anchored search
// can never match a `role=` line inside a preceding @beacon comment.
function extractFlatFunction(src, name) {
  for (const prefix of ['\nasync function ' + name + '(', '\nfunction ' + name + '(']) {
    const idx = src.indexOf(prefix);
    if (idx >= 0) return braceSlice(src, idx + 1);
  }
  throw new Error('function not found in background.js: ' + name);
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

function extractConstNumber(src, name) {
  const m = src.match(new RegExp('const ' + name + ' = (\\d+);'));
  if (!m) throw new Error('numeric const not found: ' + name);
  return Number(m[1]);
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
const SW_FN_NAMES = [
  'savedTabsDriveEntryName',
  // U02: savedTabsDriveUrlIsValid is GONE (the drive ignores the saved url);
  // its absence is locked in the static group below.
  'savedTabsDriveMarkerWrite',
  'savedTabsDriveCancelSignal',
  'stopSavedTabsDrive',
  'savedTabsDriveDelayMs',
  'armSavedTabsDriveTabReadiness',
  'armSavedTabsDriveApplyResponse',
  'emitSavedTabsDriveProgress',
  'handleSavedTabsDriveRequest',
  'runSavedTabsDrive',
  'handleSavedTabsDriveCancel',
  'handleSavedTabsDriveApplyResponse',
  'checkSavedTabsDriveEvictionMarker',
];
const swFns = {};
for (const n of SW_FN_NAMES) swFns[n] = extractFlatFunction(bgSrc, n);

const READY_MS_REAL = extractConstNumber(bgSrc, 'SAVED_TABS_DRIVE_READY_TIMEOUT_MS');
const APPLY_MS_REAL = extractConstNumber(bgSrc, 'SAVED_TABS_DRIVE_APPLY_TIMEOUT_MS');
const SESSION_KEY_REAL = extractConstString(bgSrc, 'SAVED_TABS_DRIVE_SESSION_KEY');
// U02: the ONE url every drive tab opens at (extraction fails loudly if gone).
const TAB_URL_REAL = extractConstString(bgSrc, 'SAVED_TABS_DRIVE_TAB_URL');

// The readiness hook: extracted from the ONE frame-inspection branch and
// rebuilt as notifyReady(tabId, epId, act) so the HAPPY-PATH tests fire the
// REAL hook bytes (not a harness re-implementation).
const HOOK_ANCHOR = "if (act === 'endpoint_register' || act === 'endpoint_update') {";
const hookBody = extractBlockBodyAfter(bgSrc, HOOK_ANCHOR);
const notifyReady = new Function(
  'tabId', 'epId', 'act', 'pendingTabReadiness',
  HOOK_ANCHOR + '\n' + hookBody + '\n}'
);

const BRIDGE_SENTINEL = extractConstString(clientSrc, 'BRIDGE_SENTINEL');
const CLIENT_LONG_TIMEOUT_REAL = extractConstNumber(clientSrc, 'CACHE_RENDER_LONG_TIMEOUT_MS');
const receiverSrc = extractIifeFunction(clientSrc, 'handleSavedTabsDriveApply');
const validateSrc = extractIifeFunction(clientSrc, 'savedTabsValidateEntrySpec');
const groupIdExistsSrc = extractIifeFunction(clientSrc, 'savedTabsGroupIdExists');
// E2 ([ap:ARDOF6] §E2): the receiver now calls the ONE shared focus-restore
// helper — extracted REAL here so the receiver runs its true focus path
// (every T2A fixture carries focus:null → the helper returns 'none'; the
// behavioural focus checks live in test_saved_tabs_drive_ui.js groups 10–12).
const restoreFocusSrc = extractIifeFunction(clientSrc, 'restoreSavedTabsRowFocus');
const tripleEqualSrc = extractIifeFunction(clientSrc, 'savedTabsRowFocusTripleEqual');

// --- SW module builder (mirrors the real module scope) ------------------------
function makeSwModule(cfg) {
  cfg = cfg || {};
  const state = {
    log: [],            // the ONE ordered event log every assertion reads
    sessionStore: cfg.sessionStore || {},
    tabToEndpoint: cfg.tabToEndpoint || new Map(),
    sendBehavior: cfg.sendBehavior || (() => true),
    readyCapMs: cfg.readyCapMs != null ? cfg.readyCapMs : READY_MS_REAL,
    applyCapMs: cfg.applyCapMs != null ? cfg.applyCapMs : APPLY_MS_REAL,
    // U01: windows the mock has created and still considers open (chrome.
    // windows.get resolves only for these; anything else rejects).
    openWindows: cfg.openWindows || new Set(),
    // U01: the merged-settings value savedTabsDriveDelayMs reads per gap.
    // Mutate between entries to prove a mid-drive change takes effect (F13).
    delaySeconds: (cfg.delaySeconds != null) ? cfg.delaySeconds : 0,
  };
  const seq = (ev) => { ev.seq = state.log.length; ev.t = Date.now(); state.log.push(ev); return ev; };
  const chrome = {
    windows: {
      create: async (opts) => {
        seq({ ev: 'windows.create', opts });
        if (state.windowsCreate) {
          const win = await state.windowsCreate(opts, state);
          if (win && win.id != null) state.openWindows.add(win.id);
          return win;
        }
        throw new Error('windows.create not scripted for this scenario');
      },
      // U01: the drive's window-reuse resolution (STEP 3). Resolves only for
      // windows this mock created and still considers open.
      get: async (id) => {
        seq({ ev: 'windows.get', id });
        if (state.openWindows.has(id)) return { id };
        throw new Error('window not found (closed): ' + id);
      },
    },
    tabs: {
      create: async (opts) => {
        seq({ ev: 'tabs.create', opts });
        if (state.tabsCreate) return state.tabsCreate(opts, state);
        throw new Error('tabs.create not scripted for this scenario');
      },
      // The drive code itself never calls tabs.sendMessage directly (delivery
      // rides the injected sendToTab); a direct call would be a spec violation.
      sendMessage: async () => { throw new Error('unexpected chrome.tabs.sendMessage in drive code'); },
      onRemoved: {
        _listeners: [],
        addListener(fn) { chrome.tabs.onRemoved._listeners.push(fn); },
        removeListener(fn) {
          const i = chrome.tabs.onRemoved._listeners.indexOf(fn);
          if (i >= 0) chrome.tabs.onRemoved._listeners.splice(i, 1);
        },
      },
    },
    storage: {
      session: {
        async get(key) {
          const out = {};
          if (key in state.sessionStore) out[key] = state.sessionStore[key];
          seq({ ev: 'marker-get', key });
          return out;
        },
        async set(obj) {
          Object.assign(state.sessionStore, obj);
          seq({ ev: 'marker-set', value: JSON.parse(JSON.stringify(obj[SESSION_KEY_REAL])) });
        },
        async remove(key) {
          delete state.sessionStore[key];
          seq({ ev: 'marker-remove', key });
        },
      },
    },
    alarms: { create: () => {} },
    runtime: { lastError: undefined },
  };
  const sendToTab = async (tabId, payload) => {
    const delivered = state.sendBehavior(tabId, payload);
    seq({ ev: 'send', tabId, delivered, _raw: payload });
    return delivered;
  };
  const logSw = (msg, ...rest) => {
    state.log.push({ ev: 'logSw', msg: rest.length ? String(msg) + ' ' + rest.map((r) => (r && r.message) ? r.message : String(r)).join(' ') : String(msg) });
  };
  const mod = new Function(
    'chrome', 'sendToTab', 'tabToEndpoint', 'logSw',
    'SAVED_TABS_DRIVE_READY_TIMEOUT_MS', 'SAVED_TABS_DRIVE_APPLY_TIMEOUT_MS',
    'SAVED_TABS_DRIVE_SESSION_KEY', 'loadExtensionSettings',
    'SAVED_TABS_DRIVE_TAB_URL',
    'let activeSavedTabsDrive = null;\n'
    + 'let savedTabsDriveLastWindowId = null;\n'
    + 'const pendingTabReadiness = new Map();\n'
    + SW_FN_NAMES.map((n) => swFns[n]).join('\n')
    + '\nreturn { handleSavedTabsDriveRequest, runSavedTabsDrive,'
    + ' handleSavedTabsDriveCancel, handleSavedTabsDriveApplyResponse,'
    + ' checkSavedTabsDriveEvictionMarker, armSavedTabsDriveTabReadiness,'
    + ' pendingTabReadiness, getDrive: () => activeSavedTabsDrive,'
    + ' getLastWindowId: () => savedTabsDriveLastWindowId,'
    + ' setLastWindowId: (v) => { savedTabsDriveLastWindowId = v; } };'
  )(chrome, sendToTab, state.tabToEndpoint, logSw,
    state.readyCapMs, state.applyCapMs, SESSION_KEY_REAL,
    // U01: the merged-settings reader the delay reads per gap — mocked over
    // state.delaySeconds so existing tests run with 0 (no delaying frames)
    // and delay tests can shrink/flip the value like the caps.
    async () => ({ saved_tabs_drive_delay_seconds: state.delaySeconds }),
    TAB_URL_REAL);
  mod.state = state;
  mod.chrome = chrome;
  mod.seq = seq;
  // The harness's frame-arrival simulator: does what the REAL inspection
  // branch does — populate the routing map, then run the REAL extracted hook.
  mod.deliverRegisterFrame = (tabId, epId, act) => {
    state.tabToEndpoint.set(tabId, epId);
    seq({ ev: 'register-frame', tabId, epId, act: act || 'endpoint_register' });
    notifyReady(tabId, epId, act || 'endpoint_register', mod.pendingTabReadiness);
  };
  mod.sends = () => state.log.filter((e) => e.ev === 'send');
  mod.progressFrames = () => mod.sends()
    .filter((e) => e._raw && e._raw.type === 'saved_tabs_drive_progress');
  mod.applyFrames = () => mod.sends()
    .filter((e) => e._raw && e._raw.type === 'saved_tabs_drive_apply');
  mod.creates = () => state.log.filter((e) => e.ev === 'windows.create' || e.ev === 'tabs.create');
  mod.markers = () => state.log.filter((e) => e.ev === 'marker-set' || e.ev === 'marker-remove');
  mod.logs = () => state.log.filter((e) => e.ev === 'logSw');
  return mod;
}

function driveRequestMessage(entries, driveId) {
  return {
    __glimpse_bridge__: true,
    direction: 'page-to-sw',
    kind: 'saved_tabs_drive_request',
    payload: (driveId === undefined)
      ? { entries }
      : { drive_id: driveId, entries },
  };
}
function senderFor(tabId) { return { tab: { id: tabId } }; }
function responseCollector() {
  const responses = [];
  return { responses, sendResponse: (r) => responses.push(r) };
}

// Fixture entries: the LEGACY (pre-U02) payload shape ({alias, title, url,
// spec}) — every url a zoom deep-link the drive must IGNORE (U02); specs
// are plain objects standing in for the verbatim render_spec blobs (the
// drive never reads them — A1).
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
function fixtureEntries(n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    out.push({
      alias: (i % 2 === 0) ? ('alpha-' + i) : '',       // odd entries: EMPTY alias (D3 skip path)
      title: 'Fixture Tab ' + i,
      url: 'https://workflowy.com/#/11111111-000' + i,
      spec: fixtureSpec(),
    });
  }
  return out;
}

// The standard auto-pilot: creates hand out scripted tab ids and schedule the
// register frame; apply frames schedule the correlated response (and optional
// cancel) through the REAL SW handlers.
function autoPilot(mod, cfg) {
  cfg = cfg || {};
  const tabIds = cfg.tabIds || [101, 102, 103, 104, 105];
  let next = 0;
  const scheduleReady = (tabId) => {
    if (cfg.skipReadyFor && cfg.skipReadyFor.indexOf(tabId) >= 0) return;
    setTimeout(() => mod.deliverRegisterFrame(tabId, 'tab-ep-' + tabId,
      cfg.readyAct || 'endpoint_register'), cfg.readyDelayMs || 0);
  };
  mod.state.windowsCreate = async () => {
    const tabId = tabIds[next++];
    scheduleReady(tabId);
    return { id: cfg.windowId != null ? cfg.windowId : 500, tabs: [{ id: tabId }] };
  };
  mod.state.tabsCreate = async () => {
    const tabId = tabIds[next++];
    scheduleReady(tabId);
    return { id: tabId };
  };
  mod.state.sendBehavior = (tabId, payload) => {
    if (tabId === cfg.originTabId) {
      if (cfg.originDeliver) return cfg.originDeliver(payload);
      return true;
    }
    if (payload && payload.type === 'saved_tabs_drive_apply') {
      const idx = payload.entry_index;
      if (cfg.applyDeliver && !cfg.applyDeliver(payload)) return false;
      const mode = Array.isArray(cfg.respond) ? cfg.respond[idx]
        : (cfg.respond === undefined ? true : cfg.respond);
      if (mode === 'timeout' || mode === 'none') return true; // no response scheduled
      setTimeout(() => {
        mod.state.log.push({ ev: 'apply-response-in', idx, ok: mode === true });
        mod.handleSavedTabsDriveApplyResponse({
          __glimpse_bridge__: true, direction: 'page-to-sw',
          kind: 'saved_tabs_drive_apply_response',
          payload: (mode === true)
            ? { drive_id: payload.drive_id, entry_index: idx, ok: true }
            : { drive_id: payload.drive_id, entry_index: idx, ok: false,
                error: (typeof mode === 'string') ? mode : 'scripted apply failure' },
        });
        if (cfg.cancelAfterEntry != null && idx === cfg.cancelAfterEntry) {
          mod.handleSavedTabsDriveCancel({
            __glimpse_bridge__: true, direction: 'page-to-sw',
            kind: 'saved_tabs_drive_cancel',
            payload: { drive_id: payload.drive_id },
          }, senderFor(cfg.originTabId == null ? 1 : cfg.originTabId));
        }
      }, 0);
      return true;
    }
    return true;
  };
  return { tabIdAt: (i) => tabIds[i] };
}

function startDrive(mod, entries, driveId, originTabId) {
  const rc = responseCollector();
  mod.handleSavedTabsDriveRequest(
    driveRequestMessage(entries, driveId),
    senderFor(originTabId == null ? 1 : originTabId),
    rc.sendResponse
  );
  return rc.responses;
}

// --- receiver builder (client side) -------------------------------------------
function makeReceiver(cfg) {
  cfg = cfg || {};
  const posted = [];
  const calls = [];
  const win = { postMessage: (msg) => { posted.push(msg); } };
  const quietConsole = { warn: () => {}, log: () => {}, error: () => {} };
  const groups = cfg.groups || [{ id: GID_A, name: 'Harness|Fixture Group[-10]' }];
  const normalize = cfg.normalizeRenderScopeToSpec || ((spec) => {
    calls.push({ fn: 'normalize', spec });
    return spec; // the fixtures are already normalized-shape
  });
  const currentExpandGroupsCopy = cfg.currentExpandGroupsCopy || (() => groups);
  // The REAL admission kernel (savedTabsValidateEntrySpec + the exact-match
  // savedTabsGroupIdExists) with only its two data sources injected.
  const validate = cfg.validate || new Function(
    'normalizeRenderScopeToSpec', 'currentExpandGroupsCopy',
    groupIdExistsSrc + '\n' + validateSrc + '\nreturn savedTabsValidateEntrySpec;'
  )(normalize, currentExpandGroupsCopy);
  const setEndpointAlias = cfg.setEndpointAlias || ((alias) => { calls.push({ fn: 'alias', alias }); });
  const setSavedTabsStatus = cfg.setSavedTabsStatus
    || ((msg, isOk) => { calls.push({ fn: 'status', msg, ok: isOk }); });
  const applySliceGroupIntoDock = cfg.applySliceGroupIntoDock
    || (async (gid, opts) => {
      calls.push({ fn: 'apply', gid, opts });
      return (cfg.applyResult !== undefined) ? cfg.applyResult : { success: true };
    });
  const sinceFromWire = cfg.sinceFromWire || ((v) => v);
  // E2: the receiver's focus-restore dependency rides the REAL shared helper;
  // the selection paint-path entry points are recording mocks (never reached
  // by the T2A fixtures — focus:null → 'none'), and the dock selection state
  // is real module state inside the eval scope.
  const setDockedSelection = cfg.setDockedSelection
    || ((idx, render) => { calls.push({ fn: 'sel', idx, render }); });
  const updateDockRowMarkers = cfg.updateDockRowMarkers
    || (() => { calls.push({ fn: 'markers' }); });
  const renderDockedMultiSelection = cfg.renderDockedMultiSelection
    || (() => { calls.push({ fn: 'multi' }); });
  const receiverBody = [
    'let dockedListRows = [];',
    'let dockedSelectedIndex = 0;',
    'let dockedSelectedIndices = new Set([0]);',
    'let dockedMultiAnchorIndex = 0;',
    'let dockedRenderGeneration = 1;',
    tripleEqualSrc,
    restoreFocusSrc,
    receiverSrc,
    'return handleSavedTabsDriveApply;',
  ].join('\n');
  const receiver = new Function(
    'window', 'BRIDGE_SENTINEL', 'GLIMPSE_VERSION', 'console',
    'setEndpointAlias', 'setSavedTabsStatus', 'savedTabsValidateEntrySpec',
    'applySliceGroupIntoDock', 'sinceFromWire', 'CACHE_RENDER_LONG_TIMEOUT_MS',
    'setDockedSelection', 'updateDockRowMarkers', 'renderDockedMultiSelection',
    receiverBody
  )(win, BRIDGE_SENTINEL, 'harness', quietConsole, setEndpointAlias,
    setSavedTabsStatus, validate, applySliceGroupIntoDock, sinceFromWire,
    CLIENT_LONG_TIMEOUT_REAL,
    setDockedSelection, updateDockRowMarkers, renderDockedMultiSelection);
  return { receiver, posted, calls };
}
function lastResponse(posted) {
  const resps = posted.filter((m) => m && m[BRIDGE_SENTINEL] === true
    && m.direction === 'page-to-sw' && m.kind === 'saved_tabs_drive_apply_response');
  return resps.length ? resps[resps.length - 1].payload : null;
}

// ===========================================================================
(async function main() {
  // -------------------------------------------------------------------------
  console.log('\n== group 1 — happy path + strict ordering (§8.4 items 1–2) ==');
  {
    const entries = fixtureEntries(2);
    const mod = makeSwModule({});
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102] });
    const responses = startDrive(mod, entries, 'drive-happy', 1);
    await flush(40);
    // The acceptance ack is IMMEDIATE and carries the drive_id (§6).
    eqJson(responses.length, 1, 'request answered exactly once');
    ok(responses[0].ok === true && responses[0].drive_id === 'drive-happy',
      'ack {ok:true, drive_id} (acceptance only)');
    ok(responses[0].__glimpse_bridge__ === true, 'ack rides the bridge envelope shape');
    // G1 rev-2: ONE new window; first entry windows.create (unfocused), later
    // entries tabs.create in that window (active:false).
    const creates = mod.creates();
    eqJson(creates.length, 2, 'exactly one create per entry');
    // U02: both opens go to PLAIN https://workflowy.com/ — never the entry's
    // saved zoom url (the fixtures carry '#/…' deep-links on purpose).
    ok(creates[0].ev === 'windows.create' && creates[0].opts.focused === false
      && creates[0].opts.url === 'https://workflowy.com/'
      && creates[0].opts.url !== entries[0].url,
      'entry 0: chrome.windows.create(plain workflowy.com, focused:false) — the NEW drive window; saved url ignored (U02)');
    ok(creates[1].ev === 'tabs.create' && creates[1].opts.windowId === 500
      && creates[1].opts.active === false && creates[1].opts.url === 'https://workflowy.com/'
      && creates[1].opts.url !== entries[1].url,
      'entry 1: chrome.tabs.create(plain workflowy.com, windowId:<driveWindow>, active:false); saved url ignored (U02)');
    // Progress frames: the full per-entry transition sequence + summary.
    const pf = mod.progressFrames().map((e) => e._raw);
    const seqStr = pf.map((f) => (f.kind === 'summary' ? 'summary' : f.entry_index + ':' + f.state)).join(',');
    eqJson(seqStr, '0:creating,0:awaiting-ready,0:applying,0:settled,1:creating,1:awaiting-ready,1:applying,1:settled,summary',
      'progress frame sequence: every transition, strictly ordered');
    ok(pf.every((f) => f.drive_id === 'drive-happy' && f.total === 2),
      'every progress frame carries drive_id + total');
    ok(pf[0].name === 'alpha-0' && pf[4].name === 'Fixture Tab 1',
      'name = alias→title→url fallback (empty alias falls to title, D3)');
    // Item 2 — ordering guarantee: entry 1's create NEVER precedes entry 0's
    // terminal state (A5/G6), on the ONE event log.
    const settled0 = mod.state.log.find((e) => e.ev === 'send' && e._raw
      && e._raw.type === 'saved_tabs_drive_progress' && e._raw.entry_index === 0
      && e._raw.state === 'settled');
    const create1 = mod.state.log.find((e) => e.ev === 'tabs.create');
    ok(!!settled0 && !!create1 && create1.seq > settled0.seq,
      "entry 1's create index > entry 0's settle index (strictly sequential)");
    // Apply frames: ONE combined alias+spec frame per entry; the spec rides BY
    // OBJECT IDENTITY through the SW (G4 — no clone/mutation on the drive path).
    const af = mod.applyFrames();
    eqJson(af.length, 2, 'exactly one saved_tabs_drive_apply frame per entry');
    ok(af[0]._raw.alias === 'alpha-0' && af[1]._raw.alias === '',
      'alias forwarded verbatim (empty alias rides as empty string)');
    ok(af[0]._raw.spec === entries[0].spec && af[1]._raw.spec === entries[1].spec,
      'spec delivered BY OBJECT IDENTITY (no clone in the SW)');
    ok(af[0]._raw.entry_index === 0 && af[0]._raw.drive_id === 'drive-happy',
      'apply frame correlates by drive_id + entry_index');
    // Summary shape (§6): counts + named failures.
    const summary = pf[pf.length - 1];
    eqJson(summary.kind, 'summary', 'terminal summary frame');
    eqJson(summary.applied, 2, 'summary.applied');
    eqJson(summary.failed, [], 'summary.failed (empty)');
    eqJson(summary.skipped, 0, 'summary.skipped');
    // G8: no drive state remains; readiness map drained; marker cleared.
    ok(mod.getDrive() === null, 'singleton cleared after the drive (G8)');
    eqJson(mod.pendingTabReadiness.size, 0, 'pendingTabReadiness drained (G8)');
    const mk = mod.markers();
    ok(mk.length >= 3 && mk[0].ev === 'marker-set' && mk[mk.length - 1].ev === 'marker-remove',
      'marker: written at start, updated per boundary, cleared at end (D1)');
    eqJson(mk[0].value, { drive_id: 'drive-happy', origin_tab_id: 1, total: 2, next_index: 0 },
      'marker start shape {drive_id, origin_tab_id, total, next_index:0}');
    ok(mk.some((m) => m.ev === 'marker-set' && m.value.next_index === 1),
      'marker updated to next_index:1 at the entry-1 boundary');
    // Readiness fired through the REAL hook bytes (register frames logged).
    const frames = mod.state.log.filter((e) => e.ev === 'register-frame');
    eqJson(frames.length, 2, 'both register frames ran the extracted hook');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 2 — ready timeout + early tab removal (§8.4 items 3–4) ==');
  {
    // Item 3: ready cap elapses with no register frame → failed
    // (awaiting-ready); the drive CONTINUES; the summary names it.
    const entries = fixtureEntries(2);
    const mod = makeSwModule({ readyCapMs: 40 });
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102], skipReadyFor: [101] });
    startDrive(mod, entries, 'drive-ready-timeout', 1);
    await flush(80);
    const pf = mod.progressFrames().map((e) => e._raw);
    const failedFrame = pf.find((f) => f.entry_index === 0 && f.state === 'failed');
    ok(!!failedFrame && /awaiting-ready/.test(failedFrame.detail),
      'entry 0 failed with phase awaiting-ready (detail carries phase+reason)');
    ok(pf.some((f) => f.entry_index === 1 && f.state === 'settled'),
      'entry 1 still settled — a failing entry never aborts the rest (D7)');
    const summary = pf[pf.length - 1];
    eqJson(summary.applied, 1, 'summary applied=1');
    eqJson(summary.failed.length, 1, 'summary failed=1');
    ok(summary.failed[0].phase === 'awaiting-ready' && summary.failed[0].name === 'alpha-0'
      && /180000|40ms|within/.test(summary.failed[0].reason),
      'summary names the failure {name, phase, reason}');
    ok(!mod.applyFrames().some((f) => f._raw.entry_index === 0),
      'no apply frame for the never-ready entry');
    ok(mod.getDrive() === null && mod.pendingTabReadiness.size === 0,
      'state+waiters cleared even on the timeout path (G8)');
  }
  {
    // Item 4: created tab removed before ready → IMMEDIATE rejection via the
    // one-shot onRemoved listener — no cap wait (cap injected HUGE to prove it).
    const entries = fixtureEntries(2);
    const mod = makeSwModule({ readyCapMs: 60000 });
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102], skipReadyFor: [101] });
    const t0 = Date.now();
    startDrive(mod, entries, 'drive-removed', 1);
    // Fire onRemoved for tab 101 as soon as its waiter is armed.
    for (let i = 0; i < 200 && !mod.pendingTabReadiness.has(101); i++) await tick(1);
    ok(mod.pendingTabReadiness.has(101), 'readiness waiter armed for the created tab');
    const listeners = mod.chrome.tabs.onRemoved._listeners.slice();
    ok(listeners.length === 1, 'exactly ONE one-shot onRemoved listener armed');
    listeners[0](101);
    await flush(60);
    const elapsed = Date.now() - t0;
    const pf = mod.progressFrames().map((e) => e._raw);
    const failedFrame = pf.find((f) => f.entry_index === 0 && f.state === 'failed');
    ok(!!failedFrame && /awaiting-ready/.test(failedFrame.detail)
      && /closed|removed/i.test(failedFrame.detail),
      'removed tab → immediate failed (awaiting-ready), reason names the closure');
    ok(elapsed < 5000, 'rejection was immediate — no 60s cap wait (elapsed ' + elapsed + 'ms)');
    ok(pf.some((f) => f.entry_index === 1 && f.state === 'settled'), 'drive continued to entry 1');
    eqJson(mod.chrome.tabs.onRemoved._listeners.length, 0,
      'all one-shot onRemoved listeners cleaned up after the drive');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 3 — delivery + response failures (§8.4 items 5–6) ==');
  {
    // Item 5: sendToTab false on the apply frame → failed (applying — tab
    // unreachable); the drive continues.
    const entries = fixtureEntries(2);
    const mod = makeSwModule({});
    autoPilot(mod, {
      originTabId: 1, tabIds: [101, 102],
      applyDeliver: (payload) => payload.entry_index !== 0, // entry 0's tab is dead
    });
    startDrive(mod, entries, 'drive-deadtab', 1);
    await flush(40);
    const pf = mod.progressFrames().map((e) => e._raw);
    const f0 = pf.find((f) => f.entry_index === 0 && f.state === 'failed');
    ok(!!f0 && /applying/.test(f0.detail) && /unreachable/.test(f0.detail),
      'sendToTab false → failed (applying — tab unreachable)');
    ok(pf.some((f) => f.entry_index === 1 && f.state === 'settled'), 'entry 1 settled (drive continues)');
    eqJson(pf[pf.length - 1].failed[0].phase, 'applying', 'summary failure phase = applying');
  }
  {
    // Item 6a: apply response ok:false → failed with the receiver's reason.
    const entries = fixtureEntries(2);
    const mod = makeSwModule({});
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102], respond: [false, true] });
    startDrive(mod, entries, 'drive-applyfail', 1);
    await flush(40);
    const pf = mod.progressFrames().map((e) => e._raw);
    const f0 = pf.find((f) => f.entry_index === 0 && f.state === 'failed');
    ok(!!f0 && /applying/.test(f0.detail) && /scripted apply failure/.test(f0.detail),
      'ok:false response → failed (applying) with the receiver’s reason');
    eqJson(pf[pf.length - 1].applied, 1, 'summary applied=1 (entry 1 ok)');
    eqJson(pf[pf.length - 1].failed[0].reason, 'scripted apply failure',
      'summary failure reason is the receiver’s error verbatim (A9)');
  }
  {
    // Item 6b: apply response never arrives → failed at the apply cap.
    const entries = fixtureEntries(2);
    const mod = makeSwModule({ applyCapMs: 40 });
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102], respond: ['timeout', true] });
    startDrive(mod, entries, 'drive-applytimeout', 1);
    await flush(80);
    const pf = mod.progressFrames().map((e) => e._raw);
    const f0 = pf.find((f) => f.entry_index === 0 && f.state === 'failed');
    ok(!!f0 && /applying/.test(f0.detail) && /210000|40ms|within|response/i.test(f0.detail),
      'apply cap elapsed → failed (applying, timeout detail)');
    ok(pf.some((f) => f.entry_index === 1 && f.state === 'settled'), 'entry 1 settled (drive continues)');
    ok(mod.getDrive() === null, 'singleton cleared (G8)');
  }
  {
    // U02 (fix note [ap:DLGU66]): the saved page url is IGNORED — there is no
    // url admission any more. An empty, a foreign, a zoom+search, a missing and
    // a non-string url each open a tab at PLAIN https://workflowy.com/, and the
    // entry applies and settles like any other (old sets keep importing).
    const entries = [
      { alias: 'u-empty', title: 'x', url: '', spec: fixtureSpec() },
      { alias: 'u-foreign', title: 'x', url: 'https://evil.example.com/workflowy', spec: fixtureSpec() },
      { alias: 'u-zoom', title: 'x', url: 'https://workflowy.com/#/73ec9b6621a3?q=%23opc-basic', spec: fixtureSpec() },
      { alias: 'u-missing', title: 'x', spec: fixtureSpec() },
      { alias: 'u-odd', title: 'x', url: 42, spec: fixtureSpec() },
    ];
    const mod = makeSwModule({});
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102, 103, 104, 105] });
    startDrive(mod, entries, 'drive-urls', 1);
    await flush(60);
    const pf = mod.progressFrames().map((e) => e._raw);
    eqJson(pf.filter((f) => f.state === 'failed').length, 0,
      'no entry fails over its url (U02 — url admission is gone)');
    eqJson(mod.creates().length, 5, 'every entry got its tab');
    ok(mod.creates().every((c) => c.opts.url === 'https://workflowy.com/'),
      'every tab opened at plain https://workflowy.com/ whatever the saved url (U02)');
    eqJson(mod.applyFrames().length, 5, 'every entry was delivered its apply frame');
    ok(mod.applyFrames().every((e) => e._raw.spec === entries[e._raw.entry_index].spec),
      'the spec still rides by object identity (A1/G4)');
    eqJson(pf[pf.length - 1].applied, 5, 'summary applied=5');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 4 — receiver matrix (§8.4 items 7–9) ==');
  {
    // Item 7a: EMPTY alias → no alias-set; apply proceeds; ok:true response.
    const spec = fixtureSpec();
    const rx = makeReceiver({});
    await rx.receiver({ drive_id: 'd', entry_index: 3, alias: '', spec });
    eqJson(rx.calls.filter((c) => c.fn === 'alias').length, 0,
      'empty alias → setEndpointAlias NOT called (D3 skip)');
    eqJson(rx.calls.filter((c) => c.fn === 'apply').length, 1, 'apply proceeded');
    const resp = lastResponse(rx.posted);
    ok(resp && resp.ok === true && resp.drive_id === 'd' && resp.entry_index === 3,
      'terminal {ok:true} response echoes drive_id + entry_index');
  }
  {
    // Item 7b: non-empty alias → setEndpointAlias BEFORE the apply (G3 order).
    const rx = makeReceiver({});
    await rx.receiver({ drive_id: 'd', entry_index: 0, alias: 'beta', spec: fixtureSpec() });
    const order = rx.calls.map((c) => c.fn).join(',');
    ok(order.indexOf('alias') >= 0 && order.indexOf('alias') < order.indexOf('apply'),
      'setEndpointAlias called BEFORE applySliceGroupIntoDock (call order: ' + order + ')');
    eqJson(rx.calls.find((c) => c.fn === 'alias').alias, 'beta', 'alias passed verbatim');
  }
  {
    // Follow-up 2 ([ap:DLGU66]): a NAME-ONLY frame (spec null + a saved name)
    // sets the alias and answers ok:true with NOTHING applied — no admission,
    // no render, no focus restore.
    const rx = makeReceiver({});
    await rx.receiver({ drive_id: 'd', entry_index: 4, alias: 'Jim chats', spec: null });
    eqJson(rx.calls.filter((c) => c.fn === 'alias').map((c) => c.alias), ['Jim chats'],
      'name-only: the saved name is set (setEndpointAlias, once)');
    eqJson(rx.calls.filter((c) => c.fn === 'apply' || c.fn === 'normalize' || c.fn === 'sel'
      || c.fn === 'multi' || c.fn === 'markers').length, 0,
      'name-only: nothing applied — no normalize/admission, no apply, no focus paint');
    const r1 = lastResponse(rx.posted);
    ok(r1 && r1.ok === true && r1.drive_id === 'd' && r1.entry_index === 4 && r1.error === undefined,
      'name-only: the terminal response is {ok:true} for that entry');
    ok(rx.calls.some((c) => c.fn === 'status' && c.ok === true && /no saved specification/.test(c.msg)),
      'name-only: a green status says it was named and nothing was applied');
  }
  {
    // Follow-up 2: a spec-less frame WITHOUT a name still fails admission
    // (unchanged) — it would do nothing, and never reports a false success.
    const rx = makeReceiver({});
    await rx.receiver({ drive_id: 'd', entry_index: 5, alias: '', spec: null });
    eqJson(rx.calls.filter((c) => c.fn === 'alias' || c.fn === 'apply').length, 0,
      'spec-less + unnamed: no alias-set, no apply');
    const r2 = lastResponse(rx.posted);
    ok(r2 && r2.ok === false && /no spec object/.test(r2.error),
      'spec-less + unnamed: {ok:false} from the admission kernel, as before');
  }
  {
    // Item 8: admission failure → {ok:false} response; apply NEVER called;
    // refusal surfaced locally through the ONE status writer.
    const spec = fixtureSpec('grp-doesnotexist-on-this-browser');
    const rx = makeReceiver({});
    await rx.receiver({ drive_id: 'd', entry_index: 1, alias: 'x', spec });
    eqJson(rx.calls.filter((c) => c.fn === 'apply').length, 0,
      'admission failure → applySliceGroupIntoDock NEVER called');
    const resp = lastResponse(rx.posted);
    ok(resp && resp.ok === false && /grp-doesnotexist-on-this-browser/.test(resp.error),
      '{ok:false, error} names the refused group');
    const red = rx.calls.filter((c) => c.fn === 'status' && c.ok === false);
    ok(red.length >= 1 && /not found on this browser/.test(red[0].msg),
      'refusal surfaced locally through setSavedTabsStatus (red)');
    // Alias WAS still set first (step 1 precedes admission — G3).
    eqJson(rx.calls.filter((c) => c.fn === 'alias').length, 1,
      'alias set happened before the refusal (step order 1→2)');
  }
  {
    // Item 9: the entry's spec object rides BY IDENTITY into the apply opts
    // path — no clone, no mutation, no re-key (G4/A1 + [ap:HVAWAF]: the
    // funnel's normalization happens INSIDE savedTabsValidateEntrySpec, which
    // receives the very object; the drive-side payload is never touched).
    const spec = fixtureSpec();
    const snapshot = JSON.stringify(spec);
    Object.freeze(spec);
    let normalizedArg = 'NOT-CALLED';
    const rx = makeReceiver({
      normalizeRenderScopeToSpec: (s) => { normalizedArg = s; return s; },
    });
    await rx.receiver({ drive_id: 'd', entry_index: 0, alias: '', spec });
    ok(normalizedArg === spec,
      'savedTabsValidateEntrySpec/normalization received the payload spec BY IDENTITY');
    eqJson(JSON.stringify(spec), snapshot, 'spec object NOT mutated by the receiver');
    const applyCall = rx.calls.find((c) => c.fn === 'apply');
    ok(!!applyCall && applyCall.gid === GID_A, 'apply called with the admitted gid');
    // Receiver-identical opts of applySavedTabsEntry (P5), spec-derived:
    eqJson(applyCall.opts.renderTimeoutMs, CLIENT_LONG_TIMEOUT_REAL,
      'renderTimeoutMs = CACHE_RENDER_LONG_TIMEOUT_MS');
    ok(!('renderGeneration' in applyCall.opts),
      'renderGeneration OMITTED (displaySliceInDock claims at paint → force-freeze, G5)');
    eqJson(applyCall.opts.sortBy, 'none', 'sortBy spec-derived');
    eqJson(applyCall.opts.sortDir, 'asc', 'sortDir spec-derived');
    eqJson(applyCall.opts.headingsOnly, false, 'headingsOnly spec-derived');
    eqJson(applyCall.opts.descendPolicy, 'all', 'descendPolicy spec-derived');
    eqJson(applyCall.opts.matchScope, 'name', 'matchScope spec-derived');
    eqJson(applyCall.opts.rootUuid, spec.root_uuid, 'rootUuid from the validated rid');
    eqJson(applyCall.opts.lightning, null, 'lightning null for a slice spec');
    ok(typeof applyCall.opts.fallbackMessage === 'string' && applyCall.opts.fallbackMessage.length > 0,
      'fallbackMessage set (§6 receiver step 3)');
    ok(lastResponse(rx.posted).ok === true, 'ok:true response on success');
  }
  {
    // Step-4 settle semantics: res.success !== true / res null / a THROW all
    // answer {ok:false} — the response is ALWAYS sent (§6).
    const rx1 = makeReceiver({ applyResult: { success: false, error: 'server said no' } });
    await rx1.receiver({ drive_id: 'd', entry_index: 0, alias: '', spec: fixtureSpec() });
    const r1 = lastResponse(rx1.posted);
    ok(r1 && r1.ok === false && r1.error === 'server said no', 'success:false → {ok:false, error}');
    const rx2 = makeReceiver({ applyResult: null });
    await rx2.receiver({ drive_id: 'd', entry_index: 0, alias: '', spec: fixtureSpec() });
    const r2 = lastResponse(rx2.posted);
    ok(r2 && r2.ok === false && /timed out|no result/i.test(r2.error),
      'resolve(null) (cap/socket-death settle, P7) → loud {ok:false}, never silent');
    const rx3 = makeReceiver({
      applySliceGroupIntoDock: async () => { throw new Error('boom'); },
    });
    await rx3.receiver({ drive_id: 'd', entry_index: 0, alias: '', spec: fixtureSpec() });
    const r3 = lastResponse(rx3.posted);
    ok(r3 && r3.ok === false && /boom/.test(r3.error), 'a JS throw still answers {ok:false} (always-sent)');
    eqJson(rx3.posted.filter((m) => m.kind === 'saved_tabs_drive_apply_response').length, 1,
      'exactly ONE response per apply (no double-send)');
    // No spec at all → admission refuses; response still sent.
    const rx4 = makeReceiver({});
    await rx4.receiver({ drive_id: 'd', entry_index: 0, alias: '' });
    const r4 = lastResponse(rx4.posted);
    ok(r4 && r4.ok === false && /no spec/i.test(r4.error), 'missing spec → {ok:false} (admission)');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 5 — mutex, cancel, eviction marker, origin death (§8.4 items 10–13) ==');
  {
    // U01 (F5, §9.1 case 5): SUPERSEDE instead of refuse — a request that
    // arrives while a drive is parked in its ready wait is ACKED {ok:true,
    // superseded_drive_id}, stops the old drive at once, and begins its own
    // first step only after the old run has returned (I4). Never refused.
    const entries = fixtureEntries(2);
    const mod = makeSwModule({});
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102], skipReadyFor: [101] });
    const first = startDrive(mod, entries, 'drive-first', 1);
    // Park drive-first inside its ready wait for entry 0 (the real 180 s cap).
    for (let i = 0; i < 200 && !mod.pendingTabReadiness.has(101); i++) await tick(1);
    ok(mod.pendingTabReadiness.has(101), 'drive A parked in its ready wait (F5 setup)');
    const second = startDrive(mod, fixtureEntries(1), 'drive-second', 1);
    ok(first[0] && first[0].ok === true, 'first request accepted');
    ok(second[0] && second[0].ok === true && second[0].superseded_drive_id === 'drive-first',
      'second request ACKED {ok:true, superseded_drive_id:"drive-first"} — never refused');
    await flush(60);
    const pf = mod.progressFrames().map((e) => e._raw);
    // The superseded drive reported its own end to its own origin: the parked
    // entry failed-cancelled, the rest skipped-cancelled, then its summary.
    const aFailed = pf.find((f) => f.drive_id === 'drive-first' && f.entry_index === 0
      && f.state === 'failed');
    ok(!!aFailed && /cancelled/.test(aFailed.detail),
      'superseded drive: in-flight entry failed with a cancelled reason');
    ok(pf.some((f) => f.drive_id === 'drive-first' && f.state === 'skipped-cancelled'
      && f.entry_index === 1), 'superseded drive: remaining entry skipped-cancelled');
    const aSummary = pf.filter((f) => f.drive_id === 'drive-first').pop();
    eqJson(aSummary.kind, 'summary', 'superseded drive emitted its terminal summary');
    eqJson([aSummary.applied, aSummary.skipped], [0, 1], 'superseded drive summary: applied=0, skipped=1');
    eqJson(mod.pendingTabReadiness.size, 0, 'the superseded drive left no readiness waiter behind');
    // Ordered event log: B's first create strictly after A's run returned
    // (marker cleared); B reuses A's window — zero new windows for B.
    const aMarkerClear = mod.state.log.find((e) => e.ev === 'marker-remove');
    const bFirstCreate = mod.state.log.find((e) => e.ev === 'tabs.create');
    const windowCreates = mod.creates().filter((e) => e.ev === 'windows.create');
    ok(!!aMarkerClear && !!bFirstCreate && bFirstCreate.seq > aMarkerClear.seq,
      "drive B's first create comes after drive A's run returned (marker cleared)");
    eqJson(windowCreates.length, 1,
      'exactly ONE windows.create in the scenario (A\'s) — B never created a window');
    ok(bFirstCreate.opts.windowId === 500,
      "drive B's first create is a tabs.create INTO drive A's window (windowId 500)");
    ok(mod.state.log.some((e) => e.ev === 'windows.get' && e.id === 500),
      'the reuse resolution rode chrome.windows.get(500)');
    eqJson(mod.getLastWindowId(), 500, 'the remembered window id survives for the next drive');
    // Drive B settled its entry; nothing remains afterward.
    const bSummary = pf.filter((f) => f.drive_id === 'drive-second').pop();
    eqJson(bSummary.kind, 'summary', 'drive B emitted its summary');
    eqJson(bSummary.applied, 1, 'drive B settled its entry');
    ok(mod.getDrive() === null, 'singleton cleared after both drives (G8)');
    // Empty entries → refusal (the first named refusal, still standing).
    const rcEmpty = responseCollector();
    mod.handleSavedTabsDriveRequest(driveRequestMessage([], 'd-empty'), senderFor(1), rcEmpty.sendResponse);
    ok(rcEmpty.responses[0].ok === false && /no entries/.test(rcEmpty.responses[0].error),
      'empty entries → {ok:false} refusal');
    // No sender tab → refusal (a drive with no progress console cannot report).
    const rcNoTab = responseCollector();
    mod.handleSavedTabsDriveRequest(driveRequestMessage(fixtureEntries(1), 'd-notab'), {}, rcNoTab.sendResponse);
    ok(rcNoTab.responses[0].ok === false, 'no sender tab → {ok:false} refusal');
    // Missing drive_id → minted (console-posted requests work, §7); ack carries it.
    const mod2 = makeSwModule({});
    autoPilot(mod2, { originTabId: 7, tabIds: [201] });
    const rcMint = responseCollector();
    mod2.handleSavedTabsDriveRequest(driveRequestMessage(fixtureEntries(1), undefined), senderFor(7), rcMint.sendResponse);
    ok(rcMint.responses[0].ok === true && typeof rcMint.responses[0].drive_id === 'string'
      && rcMint.responses[0].drive_id.length > 0,
      'missing drive_id → one is minted and echoed in the ack');
    await flush(30);
    ok(mod2.getDrive() === null, 'minted drive completed and cleared');
  }
  {
    // Item 11 (U01): cancel arriving BETWEEN entries — nothing is in flight,
    // so the just-settled entry stands and the remaining entries are
    // skipped-cancelled; the summary reports the cancellation.
    const entries = fixtureEntries(3);
    const mod = makeSwModule({});
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102, 103], cancelAfterEntry: 0 });
    startDrive(mod, entries, 'drive-cancel', 1);
    await flush(60);
    const pf = mod.progressFrames().map((e) => e._raw);
    ok(pf.some((f) => f.entry_index === 0 && f.state === 'settled'),
      'entry 0 settled before the cancel landed (nothing was interrupted)');
    const skipped = pf.filter((f) => f.state === 'skipped-cancelled');
    eqJson(skipped.map((f) => f.entry_index), [1, 2],
      'remaining entries each got a skipped-cancelled frame');
    eqJson(mod.creates().length, 1, 'no tab created for a skipped entry');
    const summary = pf[pf.length - 1];
    eqJson([summary.applied, summary.failed.length, summary.skipped], [1, 0, 2],
      'summary reports the cancellation: applied=1, skipped=2');
    ok(mod.getDrive() === null, 'singleton cleared after a cancelled drive (G8)');
    ok(mod.getLastWindowId() === 500, 'the cancelled drive still remembered its window (tabs stay open)');
    // A cancel for a DIFFERENT drive_id is ignored for the RUNNING drive (a
    // stale dialog can never cancel a newer drive) — and with no sender tab
    // it cannot even be answered.
    const mod2 = makeSwModule({});
    autoPilot(mod2, { originTabId: 1, tabIds: [101, 102] });
    startDrive(mod2, fixtureEntries(2), 'drive-live', 1);
    mod2.handleSavedTabsDriveCancel({ payload: { drive_id: 'drive-STALE' } });
    ok(mod2.getDrive() && mod2.getDrive().cancel_requested === false,
      'mismatched drive_id cancel ignored');
    await flush(40);
    eqJson(mod2.progressFrames().map((e) => e._raw).pop().applied, 2,
      'the live drive was undisturbed by the stale cancel');
  }
  {
    // Item 12: simulated SW RESTART with a surviving marker → exactly one
    // 'aborted' progress attempt to the origin tab + marker cleared + NO
    // drive activity (D1 loud abort; U1 no resume).
    const sessionStore = {};
    sessionStore[SESSION_KEY_REAL] = {
      drive_id: 'drive-old', origin_tab_id: 9, total: 3, next_index: 1,
    };
    const mod = makeSwModule({ sessionStore });
    await mod.checkSavedTabsDriveEvictionMarker();
    await flush(4);
    const aborts = mod.sends().filter((e) => e._raw && e._raw.kind === 'aborted');
    eqJson(aborts.length, 1, 'exactly ONE aborted progress attempt');
    ok(aborts[0].tabId === 9 && aborts[0]._raw.drive_id === 'drive-old'
      && aborts[0]._raw.total === 3 && aborts[0]._raw.entry_index === 1,
      'the aborted frame names the interruption point (origin tab, drive_id, entry 2 of 3)');
    ok(/entry 2 of 3/.test(aborts[0]._raw.detail), 'detail names the interruption point human-readably');
    ok(!(SESSION_KEY_REAL in mod.state.sessionStore), 'marker cleared on wake');
    ok(mod.state.log.some((e) => e.ev === 'logSw' && /INTERRUPTED/.test(e.msg)),
      'the abort is loud in the SW console too');
    eqJson(mod.creates().length, 0, 'NO drive activity on the restart check (no creates)');
    ok(mod.getDrive() === null, 'no singleton resurrected (U1: no resume)');
    // A clean store → the check is a silent no-op.
    const mod2 = makeSwModule({});
    await mod2.checkSavedTabsDriveEvictionMarker();
    await flush(4);
    eqJson(mod2.sends().length, 0, 'no marker → no frames, no logs of note');
    eqJson(mod2.creates().length, 0, 'no marker → no activity');
  }
  {
    // Item 13 (U01/D8 amended): origin progress-send failure mid-drive → the
    // drive STOPS AT ONCE — the in-flight wait is interrupted, its entry is
    // reported failed-cancelled, the rest are skipped, the summary goes to
    // the SW console, state+marker cleared.
    const entries = fixtureEntries(3);
    const mod = makeSwModule({});
    autoPilot(mod, {
      originTabId: 1, tabIds: [101, 102, 103],
      // The origin tab dies right after entry 0 settles: every later send to
      // it fails (entry 1's 'creating' frame is the first to observe it).
      originDeliver: (payload) => payload.entry_index === 0,
    });
    startDrive(mod, entries, 'drive-origindied', 1);
    await flush(60);
    eqJson(mod.creates().length, 2, 'entries 0 and 1 created; entry 2 never (skipped)');
    const applyIdx = mod.applyFrames().map((e) => e._raw.entry_index);
    eqJson(applyIdx, [0], 'the in-flight entry was interrupted — NO apply for entry 1 (D8, U01)');
    const logText = mod.logs().map((e) => e.msg).join('\n');
    ok(/origin tab unreachable/.test(logText), 'origin death logged loudly at detection');
    ok(/summary \(origin tab gone\)/.test(logText) && /applied 1/.test(logText),
      'the summary went to the SW console (logSw), not the dead tab');
    ok(/awaiting-ready/.test(logText) && /cancelled \(origin tab unreachable\)/.test(logText),
      'entry 1 failed at awaiting-ready with the interrupted (cancelled) reason');
    const aborted = mod.sends().filter((e) => e._raw && e._raw.kind === 'aborted');
    eqJson(aborted.length, 1, 'ONE best-effort aborted frame attempted (§6 kinds table)');
    ok(!mod.sends().some((e) => e._raw && e._raw.kind === 'summary'),
      'no summary frame to the dead origin (console instead)');
    ok(mod.getDrive() === null && !(SESSION_KEY_REAL in mod.state.sessionStore),
      'singleton + marker cleared after origin death (G8)');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 6 — the inter-tab delay (U01: F14 pacing, F13 live re-read) ==');
  {
    // F14: with the setting > 0, exactly one delaying frame precedes each
    // later create and none precedes the drive's first create; with 0, no
    // delaying frame at all. The sleep's duration is the setting in ms.
    const entries = fixtureEntries(2);
    const mod = makeSwModule({ delaySeconds: 0.02 });
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102] });
    startDrive(mod, entries, 'drive-delayed', 1);
    await flush(150);
    const pf = mod.progressFrames().map((e) => e._raw);
    const seqStr = pf.map((f) => (f.kind === 'summary' ? 'summary' : f.entry_index + ':' + f.state)).join(',');
    eqJson(seqStr, '0:creating,0:awaiting-ready,0:applying,0:settled,1:delaying,1:creating,1:awaiting-ready,1:applying,1:settled,summary',
      'delaying frame exactly between the entries (never before the first create)');
    const delayFrame = pf.find((f) => f.state === 'delaying');
    ok(!!delayFrame && /waiting 0.02 s/.test(delayFrame.detail),
      'the delaying frame announces the wait (detail carries the seconds)');
    const delaySend = mod.progressFrames().find((e) => e._raw.state === 'delaying');
    const create1 = mod.state.log.find((e) => e.ev === 'tabs.create');
    ok(delaySend && create1 && (create1.t - delaySend.t) >= 15,
      'the sleep really waited (create followed the delaying frame by '
        + (create1.t - delaySend.t) + 'ms)');
    eqJson(mod.state.log.filter((e) => e.ev === 'tabs.create').length, 1,
      'exactly one inter-tab sleep for one inter-entry gap');
    eqJson(mod.state.log.filter((e) => e.ev === 'setInterval').length, 0,
      'no setInterval anywhere (the sleep is a plain setTimeout)');
    // F14b: setting 0 → no delaying frame, no sleep.
    const mod0 = makeSwModule({ delaySeconds: 0 });
    autoPilot(mod0, { originTabId: 1, tabIds: [201, 202] });
    startDrive(mod0, entries, 'drive-nodelay', 1);
    await flush(60);
    ok(!mod0.progressFrames().some((e) => e._raw.state === 'delaying'),
      'delay 0 → no delaying frame, no sleep');
  }
  {
    // F13: a setting change between gaps is honoured at the NEXT gap (the
    // merged settings are re-read at every gap).
    const entries = fixtureEntries(3);
    const mod = makeSwModule({ delaySeconds: 0.02 });
    autoPilot(mod, {
      originTabId: 1, tabIds: [101, 102, 103],
      originDeliver: (payload) => {
        // Flip AFTER entry 1 settles: gap 1 (before entry 1) must still see
        // the original 0.02, gap 2 (before entry 2) the new 0.05.
        if (payload.entry_index === 1 && payload.state === 'settled') mod.state.delaySeconds = 0.05;
        return true;
      },
    });
    startDrive(mod, entries, 'drive-live-change', 1);
    await flush(200);
    const delays = mod.progressFrames().filter((e) => e._raw.state === 'delaying')
      .map((e) => e._raw.detail);
    eqJson(delays.length, 2, 'one delaying frame per gap (two gaps for three entries)');
    ok(/waiting 0.02 s/.test(delays[0]) && /waiting 0.05 s/.test(delays[1]),
      'the second gap used the NEW value read live (0.02 s then 0.05 s)');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 7 — IMMEDIATE cancel: the stop interrupts every drive wait (U01 F1–F3) ==');
  {
    // F1: cancel during the inter-tab delay — the sleep ends at once, no
    // further create, the delayed entry and the rest skipped-cancelled.
    const entries = fixtureEntries(3);
    const mod = makeSwModule({ delaySeconds: 1 });   // a 1000 ms sleep to interrupt
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102, 103] });
    const t0 = Date.now();
    startDrive(mod, entries, 'drive-cxl-delay', 1);
    for (let i = 0; i < 400 && !mod.progressFrames().some((e) => e._raw.state === 'delaying'); i++) await tick(2);
    ok(mod.progressFrames().some((e) => e._raw.state === 'delaying'), 'the drive paused in its inter-tab gap');
    mod.handleSavedTabsDriveCancel({
      __glimpse_bridge__: true, direction: 'page-to-sw',
      kind: 'saved_tabs_drive_cancel', payload: { drive_id: 'drive-cxl-delay' },
    }, senderFor(1));
    await flush(40);
    const elapsed = Date.now() - t0;
    ok(elapsed < 500, 'the 1000 ms sleep was interrupted at once (elapsed ' + elapsed + 'ms)');
    eqJson(mod.creates().length, 1, 'no create after the cancel (the interrupted gap produced nothing)');
    const pf = mod.progressFrames().map((e) => e._raw);
    eqJson(pf.filter((f) => f.state === 'skipped-cancelled').map((f) => f.entry_index), [1, 2],
      'the delayed entry and the rest are skipped-cancelled');
    eqJson(pf[pf.length - 1].kind, 'summary', 'summary emitted after a mid-delay cancel');
    ok(mod.getDrive() === null && !(SESSION_KEY_REAL in mod.state.sessionStore),
      'singleton + marker cleared (G8)');
  }
  {
    // F2 (Dan's desktop-variant case): cancel during the ready wait — the
    // race rejects at once (no 180 s cap wait), the waiter is torn down (no
    // map entry, no onRemoved listener, no cap timer), the entry reports
    // failed (awaiting-ready — cancelled …), the rest are skipped, the
    // created tab is left open.
    const entries = fixtureEntries(3);
    const mod = makeSwModule({});                      // the REAL 180000 ms cap left in place
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102, 103], skipReadyFor: [101] });
    const t0 = Date.now();
    startDrive(mod, entries, 'drive-cxl-ready', 1);
    for (let i = 0; i < 200 && !mod.pendingTabReadiness.has(101); i++) await tick(1);
    ok(mod.pendingTabReadiness.has(101), 'the readiness waiter is armed (F2 setup)');
    mod.handleSavedTabsDriveCancel({
      __glimpse_bridge__: true, direction: 'page-to-sw',
      kind: 'saved_tabs_drive_cancel', payload: { drive_id: 'drive-cxl-ready' },
    }, senderFor(1));
    await flush(40);
    const elapsed = Date.now() - t0;
    ok(elapsed < 500, 'the ready wait rejected within ms of the cancel — no cap wait (' + elapsed + 'ms)');
    const pf = mod.progressFrames().map((e) => e._raw);
    const failed = pf.find((f) => f.entry_index === 0 && f.state === 'failed');
    ok(!!failed && /awaiting-ready/.test(failed.detail) && /cancelled/.test(failed.detail),
      'entry 0 failed: awaiting-ready — cancelled (…) — the tab stays open');
    eqJson(mod.pendingTabReadiness.size, 0, 'no readiness waiter left behind (teardown)');
    eqJson(mod.chrome.tabs.onRemoved._listeners.length, 0, 'no onRemoved listener left behind (teardown)');
    eqJson(pf.filter((f) => f.state === 'skipped-cancelled').map((f) => f.entry_index), [1, 2],
      'the rest are skipped-cancelled');
    ok(pf[pf.length - 1].kind === 'summary' && mod.getDrive() === null,
      'summary emitted; singleton cleared (G8)');
  }
  {
    // F3: cancel during the apply-response wait — the race rejects at once
    // (cap injected far longer than the test), drive.pending_apply is torn
    // down, the entry reports failed (applying — cancelled … may still
    // finish), the rest are skipped.
    const entries = fixtureEntries(2);
    const mod = makeSwModule({ applyCapMs: 60000 });
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102], respond: ['timeout', true] });
    const t0 = Date.now();
    startDrive(mod, entries, 'drive-cxl-apply', 1);
    for (let i = 0; i < 400 && !(mod.getDrive() && mod.getDrive().pending_apply); i++) await tick(1);
    ok(mod.getDrive() && !!mod.getDrive().pending_apply, 'the apply-response waiter is armed (F3 setup)');
    mod.handleSavedTabsDriveCancel({
      __glimpse_bridge__: true, direction: 'page-to-sw',
      kind: 'saved_tabs_drive_cancel', payload: { drive_id: 'drive-cxl-apply' },
    }, senderFor(1));
    await flush(40);
    const elapsed = Date.now() - t0;
    ok(elapsed < 500, 'the apply wait rejected within ms of the cancel — no cap wait (' + elapsed + 'ms)');
    const pf = mod.progressFrames().map((e) => e._raw);
    const failed = pf.find((f) => f.entry_index === 0 && f.state === 'failed');
    ok(!!failed && /applying/.test(failed.detail) && /cancelled/.test(failed.detail),
      'entry 0 failed: applying — cancelled (…) — the tab may still finish its render');
    eqJson(pf.filter((f) => f.state === 'skipped-cancelled').map((f) => f.entry_index), [1],
      'the rest skipped-cancelled');
    ok(mod.getDrive() === null, 'singleton cleared (the waiter was torn down with it)');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 8 — double supersede (F6) + unknown-drive cancel (F10) ==');
  {
    // F6: A running (parked), B requested, C requested BEFORE B starts → B
    // runs stopped (all its entries skipped-cancelled + its summary); only
    // C creates tabs; C runs after both.
    const mod = makeSwModule({});
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102, 103], skipReadyFor: [101] });
    startDrive(mod, fixtureEntries(2), 'drive-A', 1);
    for (let i = 0; i < 200 && !mod.pendingTabReadiness.has(101); i++) await tick(1);
    const ackB = startDrive(mod, fixtureEntries(1), 'drive-B', 1);
    const ackC = startDrive(mod, fixtureEntries(1), 'drive-C', 1);
    ok(ackB[0].ok === true && ackB[0].superseded_drive_id === 'drive-A', 'B acked, superseding A');
    ok(ackC[0].ok === true && ackC[0].superseded_drive_id === 'drive-B', 'C acked, superseding B');
    await flush(100);
    const pf = mod.progressFrames().map((e) => e._raw);
    const bFrames = pf.filter((f) => f.drive_id === 'drive-B');
    ok(bFrames.length >= 2 && bFrames[0].state === 'skipped-cancelled',
      'B (superseded before it started) ran stopped: first entry skipped-cancelled');
    const bSummary = bFrames[bFrames.length - 1];
    eqJson(bSummary.kind, 'summary', 'B still emitted its summary');
    eqJson([bSummary.applied, bSummary.skipped], [0, 1], 'B summary: applied=0, skipped=1');
    const cFrames = pf.filter((f) => f.drive_id === 'drive-C');
    ok(cFrames.some((f) => f.state === 'settled'), 'C settled its entry');
    eqJson(mod.creates().filter((e) => e.ev === 'tabs.create').length, 1,
      'only C created a tab (A created the window; B created nothing)');
    ok(mod.getDrive() === null, 'singleton cleared after the chain (G8)');
    eqJson(mod.pendingTabReadiness.size, 0, 'no readiness waiters left behind');
  }
  {
    // F10: a cancel naming no running drive is answered with exactly ONE
    // aborted frame to the sender tab (carrying the stale drive_id); any
    // running drive is undisturbed.
    const mod = makeSwModule({});
    autoPilot(mod, { originTabId: 1, tabIds: [101] });
    startDrive(mod, fixtureEntries(1), 'drive-live', 1);
    await flush(30);
    ok(mod.getDrive() === null, 'the live drive completed (F10 setup)');
    mod.handleSavedTabsDriveCancel({ payload: { drive_id: 'drive-GONE' } }, senderFor(42));
    const aborted = mod.sends().filter((e) => e._raw && e._raw.kind === 'aborted');
    eqJson(aborted.length, 1, 'exactly ONE aborted frame for the stale cancel (no drive at all)');
    ok(aborted[0].tabId === 42 && aborted[0]._raw.drive_id === 'drive-GONE'
      && aborted[0]._raw.total === 0,
      'the aborted frame names the stale drive_id and rides to the sender tab');
    // A stale cancel while ANOTHER drive runs: the frame names the stale id;
    // the running drive is untouched.
    const mod2 = makeSwModule({});
    autoPilot(mod2, { originTabId: 1, tabIds: [201, 202] });
    startDrive(mod2, fixtureEntries(2), 'drive-live2', 1);
    mod2.handleSavedTabsDriveCancel({ payload: { drive_id: 'drive-STALE' } }, senderFor(42));
    await flush(40);
    const aborted2 = mod2.sends().filter((e) => e._raw && e._raw.kind === 'aborted');
    eqJson(aborted2.length, 1, 'exactly ONE aborted frame for the stale cancel (a drive was running)');
    ok(aborted2[0]._raw.drive_id === 'drive-STALE' && aborted2[0].tabId === 42,
      "the aborted frame carries the STALE id — never the live drive's");
    ok(mod2.getDrive() === null && mod2.creates().length === 2,
      'the live drive was undisturbed and completed normally');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 9 — window reuse across drives (U01: F7 reuse, F8 closed, F9 mid-drive loss) ==');
  {
    // F7: a second drive after a completed first one with its window open →
    // windows.get(<drive-1 window>) then tabs.create only; ZERO windows.create.
    const mod = makeSwModule({});
    autoPilot(mod, { originTabId: 1, tabIds: [101, 102] });
    startDrive(mod, fixtureEntries(2), 'drive-1', 1);
    await flush(60);
    eqJson(mod.getLastWindowId(), 500, 'drive 1 remembered its window');
    autoPilot(mod, { originTabId: 1, tabIds: [103] });
    startDrive(mod, fixtureEntries(1), 'drive-2', 1);
    await flush(40);
    const gets = mod.state.log.filter((e) => e.ev === 'windows.get');
    eqJson(gets.length, 1, 'exactly one windows.get (drive 2\'s first create)');
    ok(gets[0].id === 500, 'the get probed the REMEMBERED window (500)');
    const drive2Creates = mod.creates().slice(-1);
    ok(drive2Creates[0].ev === 'tabs.create' && drive2Creates[0].opts.windowId === 500,
      'drive 2 opened its tab INTO drive 1\'s window');
    eqJson(mod.creates().filter((e) => e.ev === 'windows.create').length, 1,
      'zero NEW windows across both drives (F7)');
  }
  {
    // F8: the remembered window was closed before the next drive → windows.get
    // rejects → remembered id cleared → ONE new window, which becomes the
    // remembered one.
    const mod = makeSwModule({});
    autoPilot(mod, { originTabId: 1, tabIds: [101] });
    startDrive(mod, fixtureEntries(1), 'drive-1', 1);
    await flush(40);
    eqJson(mod.getLastWindowId(), 500, 'drive 1 remembered its window');
    mod.state.openWindows.delete(500);   // Dan closed the drive window
    autoPilot(mod, { originTabId: 1, tabIds: [102], windowId: 600 });
    startDrive(mod, fixtureEntries(1), 'drive-2', 1);
    await flush(40);
    const gets = mod.state.log.filter((e) => e.ev === 'windows.get');
    ok(gets.length === 1 && gets[0].id === 500, 'the stale remembered id was probed once');
    eqJson(mod.creates().filter((e) => e.ev === 'windows.create').length, 2,
      'windows.get rejected → ONE new window for drive 2');
    eqJson(mod.getLastWindowId(), 600, 'the remembered id now points at the NEW window');
  }
  {
    // F9: a tabs.create rejection mid-drive → failed (creating), the drive
    // unbinds from its window, the NEXT entry re-resolves (the still-open
    // remembered window) and lands there.
    const entries = fixtureEntries(3);
    const mod = makeSwModule({});
    autoPilot(mod, { originTabId: 1, tabIds: [101, 999, 103] });
    let scriptedFailures = 0;
    const origTabsCreate = mod.state.tabsCreate;
    mod.state.tabsCreate = async (opts) => {
      scriptedFailures += 1;
      if (scriptedFailures === 1) throw new Error('scripted tabs.create failure');
      return origTabsCreate(opts);
    };
    startDrive(mod, entries, 'drive-tabfail', 1);
    await flush(100);
    const pf = mod.progressFrames().map((e) => e._raw);
    const f1 = pf.find((f) => f.entry_index === 1 && f.state === 'failed');
    ok(!!f1 && /creating/.test(f1.detail) && /scripted tabs.create failure/.test(f1.detail),
      'the failed create reported failed (creating) with its reason');
    ok(pf.some((f) => f.entry_index === 2 && f.state === 'settled'),
      'the drive continued and entry 2 settled (D7)');
    const tcs = mod.state.log.filter((e) => e.ev === 'tabs.create');
    eqJson(tcs.length, 2, 'two tabs.create attempts (the failed one + entry 2\'s)');
    ok(tcs[1].opts.windowId === 500,
      'entry 2 re-resolved the remembered window (still open) and created into it');
    eqJson(mod.getLastWindowId(), 500, 'the remembered window is unchanged by the failed create');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group 10 — the delay SETTING: one default + one clamp, the real merge (U01 §6.2/§9.3) ==');
  {
    // The REAL hotkeys-shared.js evaluated under node — the delay's ONE
    // default (DEFAULT_EXTENSION_SETTINGS) and ONE clamp
    // (mergeExtensionSettings) live there and nowhere else (§6.2/§6.8-5).
    const hsApi = new Function(
      hsSrc + '\nreturn (typeof self !== \'undefined\' ? self : this).GlimpseHotkeys;'
    ).call({}, {});
    const merge = hsApi.mergeExtensionSettings;
    const d = (u) => merge(u).saved_tabs_drive_delay_seconds;
    eqJson(d({}), 5, 'merge({}) → the shipped default 5');
    eqJson(d({ saved_tabs_drive_delay_seconds: 7.4 }), 7, '7.4 → 7 (whole seconds)');
    eqJson(d({ saved_tabs_drive_delay_seconds: -3 }), 0, '-3 → 0 (floored at 0 = no delay)');
    eqJson(d({ saved_tabs_drive_delay_seconds: 999 }), 60, '999 → 60 (the one-minute ceiling)');
    eqJson(d({ saved_tabs_drive_delay_seconds: 'x' }), 5, "garbage ('x') → the shipped 5");
    eqJson(d({ saved_tabs_drive_delay_seconds: 0 }), 0, 'an explicit 0 is preserved (0 = no delay)');
    // The literal default occurs exactly once, inside DEFAULT_EXTENSION_SETTINGS.
    eqJson(countOcc(hsSrc, 'saved_tabs_drive_delay_seconds: 5,'), 1,
      'exactly ONE default literal in hotkeys-shared.js');
    eqJson(countOcc(hsSrc, 'userSettings.saved_tabs_drive_delay_seconds'), 1,
      'exactly ONE clamp site in mergeExtensionSettings');
    ok(hsSrc.indexOf('Math.max(0, Math.min(60, Math.round(stdd)))') >= 0,
      'the clamp expression is Math.max(0, Math.min(60, …)) (§6.2)');
  }

  // -------------------------------------------------------------------------
  console.log('\n== group LOCKS — static wiring + negative postconditions (§8.2/§8.6) ==');
  {
    // DoD-5: the normative constants exist with their EXACT values (the
    // harnesses grep them — §6 stable surface).
    eqJson(READY_MS_REAL, 180000, 'SAVED_TABS_DRIVE_READY_TIMEOUT_MS = 180000');
    eqJson(APPLY_MS_REAL, 210000, 'SAVED_TABS_DRIVE_APPLY_TIMEOUT_MS = 210000');
    eqJson(SESSION_KEY_REAL, 'glimpse_saved_tabs_drive_active', 'SAVED_TABS_DRIVE_SESSION_KEY value');
    ok(bgSrc.indexOf('const SAVED_TABS_DRIVE_READY_TIMEOUT_MS = 180000;') >= 0
      && bgSrc.indexOf('const SAVED_TABS_DRIVE_APPLY_TIMEOUT_MS = 210000;') >= 0,
      'both caps declared byte-exact in background.js (DoD-5 grep)');

    // DoD-3: exactly the FIVE §6 kinds and no sixth drive kind, per file.
    // Kind tokens are matched at their USE sites (kind/type positions); the
    // blanket-token check additionally proves nothing else in the file
    // spellings looks like a drive kind — the only non-kind token allowed is
    // the §6-normative session-key VALUE 'glimpse_saved_tabs_drive_active'
    // (a storage key, not a wire kind; §6 mandates that exact string).
    const kindUseRe = /(?:kind|type)\s*(?::|===)\s*'(saved_tabs_drive_[a-z_]+)'/g;
    const kindUses = (s) => {
      const out = new Set();
      let m;
      while ((m = kindUseRe.exec(s)) !== null) out.add(m[1]);
      kindUseRe.lastIndex = 0;
      return out;
    };
    const bgKinds = kindUses(bgSrc);
    const clientKinds = kindUses(clientSrc);
    const FIVE = new Set(['saved_tabs_drive_request', 'saved_tabs_drive_progress',
      'saved_tabs_drive_cancel', 'saved_tabs_drive_apply', 'saved_tabs_drive_apply_response']);
    eqJson(Array.from(bgKinds).sort(), Array.from(FIVE).sort(),
      'background.js: exactly the five §6 kinds at kind/type positions, no sixth');
    ok(Array.from(clientKinds).every((k) => FIVE.has(k))
      && clientKinds.has('saved_tabs_drive_apply')
      && clientKinds.has('saved_tabs_drive_apply_response'),
      'client: only §6 kinds (apply branch + response); request/cancel/progress senders are T2B’s');
    const bgTokens = new Set((bgSrc.match(/saved_tabs_drive_[a-z_]+/g) || []));
    const clientTokens = new Set((clientSrc.match(/saved_tabs_drive_[a-z_]+/g) || []));
    ok(Array.from(bgTokens).every((t) => FIVE.has(t) || t === 'saved_tabs_drive_active'
      || t === 'saved_tabs_drive_delay_seconds'),
      'no sixth drive-kind TOKEN anywhere in background.js (kinds + the §6 session-key value '
        + '+ the U01 delay SETTING key only — still no sixth kind)');
    ok(Array.from(clientTokens).every((t) => FIVE.has(t) || t === 'saved_tabs_drive_delay_seconds'),
      'no sixth drive-kind TOKEN anywhere in the client (the delay setting key admitted, never a kind)');

    // DoD-4 / postcondition 2: pendingTabReadiness — the Map declaration, the
    // driver-side arm/cleanup, and its ONE notification site INSIDE the frame-
    // inspection branch (immediately after tabToEndpoint.set; register+update).
    ok(bgSrc.indexOf('const pendingTabReadiness = new Map();') >= 0,
      'pendingTabReadiness declared as a Map');
    eqJson(countOcc(bgSrc, HOOK_ANCHOR), 1, 'the readiness hook guard exists exactly once');
    const hookPos = bgSrc.indexOf(HOOK_ANCHOR);
    ok(hookPos > bgSrc.indexOf('tabToEndpoint.set(tabId, epId);'),
      'the hook sits after tabToEndpoint.set(tabId, epId) in the inspection branch');
    ok(hookPos < bgSrc.indexOf('const ok = sendToServer(message.payload);'),
      'the hook sits inside the inspection branch (before its relay tail)');
    eqJson(countOcc(bgSrc, 'readyWaiter.resolve('), 1,
      'exactly ONE waiter-notification (resolve) site in background.js');
    ok(extractBlockBodyAfter(bgSrc, HOOK_ANCHOR).indexOf('pendingTabReadiness.get(tabId)') >= 0,
      'the notification site reads pendingTabReadiness');

    // DoD-5a: no polling readiness mechanism — no setInterval anywhere in
    // background.js, and the drive section's only timers are the two caps
    // plus the U01 inter-tab delay sleep (interruptible; never a poll).
    eqJson(countOcc(bgSrc, 'setInterval'), 0, 'background.js contains no setInterval at all');
    const driveSection = regionBetween(bgSrc,
      '// Saved Tabs (Tier-2)', '// Outbound queue');
    eqJson(countOcc(driveSection, 'setTimeout('), 3,
      'the drive section has exactly three setTimeout calls (the two named caps + the U01 delay sleep)');
    ok(driveSection.indexOf('SAVED_TABS_DRIVE_READY_TIMEOUT_MS);') >= 0
      && driveSection.indexOf('SAVED_TABS_DRIVE_APPLY_TIMEOUT_MS);') >= 0,
      'both cap timers are the named cap constants');
    ok(swFns.runSavedTabsDrive.indexOf('timer = setTimeout(r, delayMs)') >= 0
      && swFns.runSavedTabsDrive.indexOf('clearTimeout(timer)') >= 0,
      'the third timer is the U01 inter-tab delay sleep, cleared on every exit');
    eqJson(countOcc(driveSection, 'sendToServer'), 0,
      'no sendToServer on any drive path (DoD-6 — the drive never touches the broker socket)');
    for (const n of SW_FN_NAMES) {
      ok(swFns[n].indexOf('sendToServer') < 0, 'drive symbol ' + n + ' never calls sendToServer');
    }

    // DoD-5c: nothing ever closes a tab.
    eqJson(countOcc(bgSrc, 'chrome.tabs.remove'), 0, 'no chrome.tabs.remove in background.js');
    eqJson(countOcc(clientSrc, 'chrome.tabs.remove'), 0, 'no chrome.tabs.remove in the client');

    // G1 create shapes: the NEW window + same-window tabs, unfocused/inactive.
    const runSrc = swFns.runSavedTabsDrive;
    ok(runSrc.indexOf('chrome.windows.create({ url: SAVED_TABS_DRIVE_TAB_URL, focused: false })') >= 0,
      'first entry: chrome.windows.create({url: SAVED_TABS_DRIVE_TAB_URL, focused:false}) — ONE new window (G1 rev-2, U02)');
    ok(runSrc.indexOf('chrome.tabs.create({ url: SAVED_TABS_DRIVE_TAB_URL, windowId: drive.window_id, active: false })') >= 0,
      'later entries: chrome.tabs.create({url: SAVED_TABS_DRIVE_TAB_URL, windowId:<driveWindowId>, active:false}) (G1 rev-2, U02)');
    // U02 locks (fix note [ap:DLGU66]): the drive never navigates to the
    // saved url; the ONE tab url is plain Workflowy, defined once; the old
    // url-admission helper is gone (no dead second site).
    eqJson(TAB_URL_REAL, 'https://workflowy.com/', "SAVED_TABS_DRIVE_TAB_URL = 'https://workflowy.com/' (U02)");
    eqJson(countOcc(bgSrc, 'const SAVED_TABS_DRIVE_TAB_URL = '), 1, 'the drive tab url is defined exactly once (U02)');
    eqJson(countOcc(runSrc, 'entry.url'), 0, 'runSavedTabsDrive never reads entry.url (U02)');
    eqJson(countOcc(bgSrc, 'savedTabsDriveUrlIsValid'), 0, 'the url-admission helper is gone from background.js (U02)');
    eqJson(countOcc(runSrc, 'url: SAVED_TABS_DRIVE_TAB_URL'), 2, 'both create calls (and only they) open the drive tab url (U02)');
    eqJson(countOcc(bgSrc, 'chrome.windows.create('), 1, 'windows.create appears exactly once (the driver)');
    eqJson(countOcc(bgSrc, 'chrome.tabs.create('), 1, 'tabs.create appears exactly once (the driver)');
    ok(runSrc.indexOf('sendToTab(tabId, {') >= 0
      && runSrc.indexOf("type: 'saved_tabs_drive_apply',") >= 0,
      'step-5 delivery rides the existing sendToTab primitive with ONE combined frame');

    // U01 §6.8 static locks (contract [ap:08ZEAO] — the mechanical done).
    eqJson(countOcc(bgSrc, 'cancel_requested = true'), 1,
      'exactly ONE cancel_requested = true assignment in background.js (§6.8-2)');
    ok(swFns.stopSavedTabsDrive.indexOf('cancel_requested = true') >= 0,
      'the one assignment lives inside stopSavedTabsDrive (the ONE writer)');
    ok(runSrc.indexOf('untilCancelled(readyPromise)') >= 0
      && runSrc.indexOf('untilCancelled(armSavedTabsDriveApplyResponse(drive, i))') >= 0,
      'both the ready wait and the apply wait are raced against the cancel signal (§6.8-2)');
    ok(runSrc.indexOf('await untilCancelled(new Promise((r) => { timer = setTimeout(r, delayMs); }))') >= 0,
      'the delay sleep is raced too (§6.8-2)');
    eqJson(countOcc(bgSrc, 'chrome.windows.get('), 1,
      'chrome.windows.get appears exactly once (the STEP-3 reuse resolution, §6.1.3)');
    for (const bad of ['already active', 'next entry boundary', 'in-flight entry completes', 'one drive at a time']) {
      ok(bgSrc.toLowerCase().indexOf(bad.toLowerCase()) < 0,
        'background.js no longer says "' + bad + '" (§6.8-1/4)');
    }
    ok(bgSrc.indexOf('superseded_drive_id') >= 0,
      'the ack carries superseded_drive_id (§5.3 protocol delta)');
    ok(bgSrc.indexOf("kind: 'aborted',") >= 0
      && swFns.handleSavedTabsDriveCancel.indexOf("kind: 'aborted',") >= 0,
      'the unknown-drive cancel is answered with an aborted frame (§6.1.5)');
    ok(bgSrc.indexOf('let savedTabsDriveLastWindowId = null;') >= 0,
      'the remembered-window module state exists (memory-only by design)');
    ok(bgSrc.indexOf('savedTabsDriveLastWindowId') >= 0
      && bgSrc.indexOf('SAVED_TABS_DRIVE_LAST_WINDOW') < 0,
      'the window id is NEVER persisted (no second session key, §6.8-6)');

    // Postcondition 4: chrome.storage.session is used for exactly ONE key.
    const sessionUses = countOcc(bgSrc, 'chrome.storage.session.');
    ok(sessionUses >= 3, 'chrome.storage.session present (get/set/remove)');
    eqJson(countOcc(driveSection, 'chrome.storage.session.'), sessionUses,
      'every chrome.storage.session use lives in the drive section');
    ok(countOcc(bgSrc, 'chrome.storage.session.get(SAVED_TABS_DRIVE_SESSION_KEY)') >= 1
      && countOcc(bgSrc, '[SAVED_TABS_DRIVE_SESSION_KEY]') >= 1
      && countOcc(bgSrc, 'chrome.storage.session.remove(SAVED_TABS_DRIVE_SESSION_KEY)') >= 1,
      'the session key used is exactly SAVED_TABS_DRIVE_SESSION_KEY');
    const sessionKeyLiterals = bgSrc.match(/chrome\.storage\.session\.\w+\(\s*'([^']+)'/g) || [];
    eqJson(sessionKeyLiterals.length, 0,
      'no string-literal session key anywhere (always the named constant)');

    // The onMessage table wiring: ONE branch per page→SW drive kind, calling
    // the extracted handlers.
    for (const kind of ['saved_tabs_drive_request', 'saved_tabs_drive_cancel',
      'saved_tabs_drive_apply_response']) {
      eqJson(countOcc(bgSrc, "message.kind === '" + kind + "'"), 1,
        'exactly one onMessage branch for ' + kind);
    }
    const reqBranch = regionBetween(bgSrc,
      "if (message.kind === 'saved_tabs_drive_request') {",
      "if (message.kind === 'saved_tabs_drive_cancel') {");
    ok(reqBranch.indexOf('handleSavedTabsDriveRequest(message, sender, sendResponse);') >= 0,
      'the request branch calls handleSavedTabsDriveRequest');
    const cancelBranch = regionBetween(bgSrc,
      "if (message.kind === 'saved_tabs_drive_cancel') {",
      "if (message.kind === 'saved_tabs_drive_apply_response') {");
    ok(cancelBranch.indexOf('handleSavedTabsDriveCancel(message, sender);') >= 0,
      'the cancel branch calls handleSavedTabsDriveCancel with the sender (U01 aborted reply)');
    const respBranch = extractBlockBodyAfter(bgSrc,
      "if (message.kind === 'saved_tabs_drive_apply_response') {");
    ok(respBranch.indexOf('handleSavedTabsDriveApplyResponse(message);') >= 0,
      'the response branch calls handleSavedTabsDriveApplyResponse');

    // The startup check runs at top level on EVERY SW wake (D1), in the
    // bootstrap tail after the existing keepalive/connect calls.
    eqJson(countOcc(bgSrc, '\ncheckSavedTabsDriveEvictionMarker();'), 1,
      'exactly one top-level checkSavedTabsDriveEvictionMarker() call');
    ok(bgSrc.slice(-700).indexOf('ensureKeepaliveAlarm();') >= 0
      && bgSrc.slice(-700).indexOf('checkSavedTabsDriveEvictionMarker();') >= 0
      && bgSrc.lastIndexOf('ensureKeepaliveAlarm();') < bgSrc.indexOf('\ncheckSavedTabsDriveEvictionMarker();'),
      'the startup check rides the top-level bootstrap tail');

    // Postcondition 3 (client): exactly ONE new listener branch + ONE new
    // receiver function; no other drive machinery in the client.
    eqJson(countOcc(clientSrc, "p.type === 'saved_tabs_drive_apply'"), 1,
      'exactly one saved_tabs_drive_apply listener branch');
    eqJson(countOcc(clientSrc, 'async function handleSavedTabsDriveApply('), 1,
      'exactly one receiver function definition');
    const listenerRegion = regionBetween(clientSrc,
      "if (p.type === 'hotkey_config') {",
      'function getSerializedByteLength(s) {');
    ok(listenerRegion.indexOf("} else if (p.type === 'extension_settings') {") >= 0
      && listenerRegion.indexOf("} else if (p.type === 'saved_tabs_spec_request') {") >= 0
      && listenerRegion.indexOf("} else if (p.type === 'saved_tabs_drive_apply') {") >= 0,
      'the drive branch joined the ONE sw→page listener as a further else-if (P9) — existing branches intact');
    ok(listenerRegion.indexOf('handleSavedTabsDriveApply(p)') >= 0,
      'the branch calls the receiver');

    // Receiver source locks (§8.2 postcondition 5d + §8.9 anti-patterns).
    ok(receiverSrc.indexOf('savedTabsValidateEntrySpec({ spec: p.spec })') >= 0,
      'admission runs the SAME kernel with the payload spec by reference (§6 step 2)');
    ok(receiverSrc.indexOf('setEndpointAlias(p.alias)') >= 0,
      'step 1: non-empty alias → setEndpointAlias');
    ok(receiverSrc.indexOf('applySliceGroupIntoDock(check.gid, {') >= 0,
      'step 3: the ONE shared funnel with the admitted gid');
    ok(receiverSrc.indexOf('renderTimeoutMs: CACHE_RENDER_LONG_TIMEOUT_MS') >= 0,
      'the receiver-identical long render timeout (P5)');
    ok(receiverSrc.indexOf('renderGeneration') < 0,
      'renderGeneration omitted (claims at paint → force-freeze, G5)');
    ok(receiverSrc.indexOf("kind: 'saved_tabs_drive_apply_response'") >= 0,
      'step 5: the response rides the page→SW bridge (saved_tabs_spec_response precedent)');
    ok(receiverSrc.indexOf('applySavedTabsEntry(') < 0,
      'NOT a wrapper around applySavedTabsEntry (§8.9 named anti-pattern)');
    ok(receiverSrc.indexOf('JSON.parse(JSON.stringify') < 0
      && receiverSrc.indexOf('structuredClone') < 0,
      'no spec clone anywhere in the receiver (G4/A1)');
    ok(!/\.spec\s*=[^=>]/.test(receiverSrc) && !/spec\.[a-z_]+\s*=[^=>]/.test(receiverSrc),
      'no assignment to any spec field in the receiver (postcondition 5d)');
    ok(receiverSrc.indexOf('requestSliceModalRender') < 0
      && receiverSrc.indexOf('normalizeRenderScopeToSpec') < 0,
      'no private apply path: the receiver goes through validate+funnel only (§8.9)');

    // Compatibility (8.5): the called machinery is byte-identical (signature
    // pins — the unit CALLS these, it does not modify them).
    ok(clientSrc.indexOf('async function applySavedTabsEntry(entry, hooks) {') >= 0,
      'applySavedTabsEntry intact');
    ok(clientSrc.indexOf('function savedTabsValidateEntrySpec(entry) {') >= 0,
      'savedTabsValidateEntrySpec intact');
    ok(clientSrc.indexOf('function handleSavedTabsSpecRequest(payload) {') >= 0,
      'handleSavedTabsSpecRequest (collect/survey path) intact');
    ok(clientSrc.indexOf('function setSavedTabsStatus(message, ok) {') >= 0,
      'setSavedTabsStatus (the ONE status writer) intact');
    ok(bgSrc.indexOf('async function collectSavedTabsSpecs(sendResponse) {') >= 0,
      'collectSavedTabsSpecs (collect/survey path) intact');
    ok(bgSrc.indexOf('async function sendToTab(tabId, payload) {') >= 0,
      'sendToTab intact');

    // Beacons (§8.9 test): the drive orchestrator and the receiver — the two
    // jump-to entry points — carry beacon blocks; thin helpers do not.
    ok(bgSrc.indexOf('id=saved-tabs@drive-orchestrator,') >= 0,
      'the drive orchestrator carries a beacon block');
    ok(clientSrc.indexOf('id=saved-tabs@drive-apply-receiver,') >= 0,
      'the receiver carries a beacon block');
    const driveBeaconCount = (bgSrc.match(/id=saved-tabs@drive-/g) || []).length;
    eqJson(driveBeaconCount, 1, 'no helper-only beacon blocks in background.js (curate, don’t carpet-bomb)');
  }

  console.log('');
  console.log(passed + ' passed, ' + failed + ' failed');
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.error('HARNESS ERROR (extraction/construction failed loudly):', e);
  process.exit(1);
});
