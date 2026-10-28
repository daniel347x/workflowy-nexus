// ============================================================================
// test_slice_group_durable_read.js — U7 ([ap:256S33]): the DURABLE READ for
// slice-group state — the browser side of the P7LEUA fix.
//
// Covers (spec §9 tests 5–7 + the §14 negative postconditions):
//   • DURABLE (5) — the page-world get_slice_group_config handler with a
//     DELIBERATELY STALE in-memory mirror and CURRENT chrome.storage answers
//     the group fields (expand_groups / active_group) from the STORAGE values
//     via the SW 'get_expand_groups' bridge kind — and never touches the
//     mirror's group fields (getter-trap proven). Fails pre-fix: the pre-fix
//     branch answers from extensionSettings synchronously.
//   • MIGRATION (5b) — the SW handler rides loadExtensionSettings()'s
//     one-time sync→local migration fallback (the ONE merge authority — no
//     new merge logic): with groups only in the sync blob, the reply carries
//     the migrated groups.
//   • LOUD FAILURE (6) — bridge failure (the content-bridge lastError shape)
//     → reply success:false naming the failed durable read; NO mirror
//     fallback for group state (getter-trap proven: the mirror's group
//     fields are never even read). Fails pre-fix (the pre-fix branch always
//     answers success:true from the mirror).
//   • VOCAB (7) — the sync-blob vocabulary fields (postfix words,
//     global-never list, note-session vocab) still come from the in-memory
//     mirror: regression pin for the unchanged data classes.
//   • WIRING LOCKS — mechanical §14 checks over the REAL file bytes: the
//     branch sources group state from requestDurableExpandGroups() and
//     contains NO extensionSettings.expand_groups/active_group (negative
//     postcondition 1); the failure reply is loud (negative postcondition
//     2); the SW handler reuses loadExtensionSettings() and opens no new
//     storage read (no third copy); the set_slice_groups write branch still
//     funnels through persistExpandGroups (write protocol unchanged).
//
// Construction: the repo's extraction idiom (tests/js/README.md) — the
// handler branch, the bridge helper, the SW kind and loadExtensionSettings
// are extracted VERBATIM from the real file bytes and evaluated with mocked
// chrome.storage + a mocked window/content-bridge. A rename or signature
// change fails LOUDLY (extraction throws). Against the pre-U7 tree this
// harness FAILS (requestDurableExpandGroups / get_expand_groups do not
// exist — extraction throws; and the behavioral locks fail on the old
// mirror-sourced branch).
//
// RUN:   node tests/js/test_slice_group_durable_read.js
// EXIT:  0 = all pass, 1 = at least one failure. Zero dependencies.
// ============================================================================
'use strict';

const fs = require('fs');
const path = require('path');

const BG_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'background.js');
const CLIENT_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'glimpse-cache-client.js');
const SHARED_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'hotkeys-shared.js');

const bgSrc = fs.readFileSync(BG_FILE, 'utf8');
const clientSrc = fs.readFileSync(CLIENT_FILE, 'utf8');
const sharedSrc = fs.readFileSync(SHARED_FILE, 'utf8');

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

// --- extraction (verbatim from the REAL file bytes) --------------------------
function extractFunctionFrom(src, name) {
  let idx = src.indexOf(`\n  function ${name}(`);
  if (idx >= 0) idx += 1; // skip the newline
  else idx = src.indexOf(`function ${name}(`);
  if (idx < 0) throw new Error(`function not found: ${name}`);
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

// Top-level / async variant (background.js is flat, not IIFE-indented).
function extractNamedFunctionFrom(src, name) {
  let idx = src.indexOf(`async function ${name}(`);
  if (idx < 0) idx = src.indexOf(`function ${name}(`);
  if (idx < 0) throw new Error(`function not found: ${name}`);
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

// Inner body of an anonymous dispatch branch whose anchor ends with '{'.
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
  const m = src.match(new RegExp(`const ${name} = '([^']+)'`));
  if (!m) throw new Error(`const not found: ${name}`);
  return m[1];
}

function regionBetween(src, startAnchor, endAnchor) {
  const a = src.indexOf(startAnchor);
  if (a < 0) throw new Error('region start not found: ' + startAnchor);
  const b = src.indexOf(endAnchor, a + startAnchor.length);
  if (b < 0) throw new Error('region end not found after start: ' + endAnchor);
  return src.slice(a, b);
}

// --- the shared GlimpseHotkeys closure (dependency set as proven by
// test_favorite_postfix_settings.js; all pure) --------------------------------
const sharedBody = [
  extractConstObjectFromShared('DEFAULT_EXTENSION_SETTINGS'),
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

function extractConstObjectFromShared(name) {
  const marker = `\n  const ${name} =`;
  const idx = sharedSrc.indexOf(marker);
  if (idx < 0) throw new Error(`const not found in shared file: ${name}`);
  const start = sharedSrc.indexOf('{', idx);
  if (start < 0) throw new Error(`no object literal for const: ${name}`);
  let depth = 0;
  for (let i = start; i < sharedSrc.length; i++) {
    if (sharedSrc[i] === '{') depth++;
    else if (sharedSrc[i] === '}') {
      depth--;
      if (depth === 0) {
        const semi = sharedSrc.indexOf(';', i);
        return sharedSrc.slice(idx + 1, semi + 1);
      }
    }
  }
  throw new Error(`unbalanced braces extracting const: ${name}`);
}

const GlimpseHotkeys = new Function(`
  ${sharedBody}
  return {
    DEFAULT_EXTENSION_SETTINGS, mergeExtensionSettings, normalizeExpandGroups,
    resolveActiveGroupId, parseExpandTagPrefixes, sanitizeExpandGroup,
  };
`)();

// --- the SW side: storage keys + loadExtensionSettings + the bridge kind ----
const SETTINGS_STORAGE_KEY = extractConstString(bgSrc, 'SETTINGS_STORAGE_KEY');
const EXPAND_GROUPS_LOCAL_KEY = extractConstString(bgSrc, 'EXPAND_GROUPS_LOCAL_KEY');
const ANCHOR_VIEWS_LOCAL_KEY = extractConstString(bgSrc, 'ANCHOR_VIEWS_LOCAL_KEY');
const BRIDGE_SENTINEL = extractConstString(clientSrc, 'BRIDGE_SENTINEL');

const SW_HANDLER_ANCHOR = "if (message.kind === 'get_expand_groups') {";
const swHandlerBody = extractBlockBodyAfter(bgSrc, SW_HANDLER_ANCHOR);

function makeChromeMock(syncStore, localStore) {
  const get = (store) => (keys, cb) => {
    const out = {};
    for (const k of keys) if (k in store) out[k] = store[k];
    cb(out);
  };
  const set = (store) => (obj, cb) => { Object.assign(store, obj); if (cb) cb(); };
  return {
    storage: {
      sync: { get: get(syncStore), set: set(syncStore) },
      local: { get: get(localStore), set: set(localStore) },
    },
    runtime: { lastError: undefined },
  };
}

function makeSwContext(syncStore, localStore) {
  const chromeMock = makeChromeMock(syncStore, localStore);
  const loadExtensionSettings = new Function(
    'chrome', 'SETTINGS_STORAGE_KEY', 'EXPAND_GROUPS_LOCAL_KEY',
    'ANCHOR_VIEWS_LOCAL_KEY', 'logSw', 'self',
    extractNamedFunctionFrom(bgSrc, 'loadExtensionSettings')
    + '\nreturn loadExtensionSettings;'
  )(chromeMock, SETTINGS_STORAGE_KEY, EXPAND_GROUPS_LOCAL_KEY,
    ANCHOR_VIEWS_LOCAL_KEY, () => {}, { GlimpseHotkeys });
  const handler = new Function(
    'message', 'sendResponse', 'loadExtensionSettings',
    `${SW_HANDLER_ANCHOR}\n${swHandlerBody}\n}`
  );
  return { handler, loadExtensionSettings, chromeMock };
}

// --- the PAGE side: window/content-bridge mock + extracted branch -----------
const CLIENT_BRANCH_ANCHOR =
  "} else if (request.action === 'get_slice_group_config') {";
const clientBranchBody = extractBlockBodyAfter(clientSrc, CLIENT_BRANCH_ANCHOR);

const requestDurableExpandGroupsSrc =
  extractNamedFunctionFrom(clientSrc, 'requestDurableExpandGroups');

// mode 'ok': the bridge relays to the SW handler and relays the response back
// (the content-bridge's transparent field forwarding, replicated).
// mode 'bridge-down': the content-bridge's chrome.runtime.lastError path —
// ok:false with the bridge error, exactly the shipped shape.
function makePageContext({ sw, mode }) {
  const listeners = [];
  const win = {
    posted: [],
    addEventListener(type, fn) { if (type === 'message') listeners.push(fn); },
    removeEventListener(type, fn) {
      const i = listeners.indexOf(fn);
      if (i >= 0) listeners.splice(i, 1);
    },
    postMessage(msg) {
      win.posted.push(msg);
      if (!msg || msg[BRIDGE_SENTINEL] !== true) return;
      if (msg.direction !== 'page-to-sw') return;
      const corr = msg.correlationId;
      const deliver = (payload) => {
        setTimeout(() => {
          const ev = {
            source: win,
            data: {
              [BRIDGE_SENTINEL]: true,
              direction: 'sw-to-page',
              correlationId: corr,
              payload,
            },
          };
          for (const fn of listeners.slice()) fn(ev);
        }, 0);
      };
      if (mode === 'bridge-down') {
        deliver({
          type: 'bridge_response',
          ok: false,
          readyState: 3, // WebSocket.CLOSED
          error: 'Extension context invalidated (simulated)',
        });
        return;
      }
      if (msg.kind !== 'get_expand_groups') return; // nothing else under test
      sw.handler(msg, (resp) => {
        // content-bridge.js: standard envelope + transparent forwarding of
        // every non-internal response field.
        const payload = { type: 'bridge_response', ok: resp ? resp.ok !== false : true };
        if (resp && typeof resp === 'object') {
          for (const k of Object.keys(resp)) {
            if (k === '__glimpse_bridge__' || k === 'ok' || k === 'readyState') continue;
            payload[k] = resp[k];
          }
        }
        deliver(payload);
      }, sw.loadExtensionSettings);
    },
  };
  const requestDurableExpandGroups = new Function(
    'window', 'BRIDGE_SENTINEL',
    requestDurableExpandGroupsSrc + '\nreturn requestDurableExpandGroups;'
  )(win, BRIDGE_SENTINEL);
  const runBranch = new Function(
    'request', 'ws', 'extensionSettings', 'GlimpseHotkeys',
    'requestDurableExpandGroups',
    `if (request.action === 'get_slice_group_config') {\n${clientBranchBody}\n}`
  );
  return { win, runBranch, requestDurableExpandGroups };
}

function makeWs() {
  const ws = {
    sent: [],
    _waiters: [],
    send(s) {
      ws.sent.push(JSON.parse(s));
      const w = ws._waiters.shift();
      if (w) w(ws.sent[ws.sent.length - 1]);
    },
  };
  ws.nextReply = (timeoutMs) => new Promise((resolve, reject) => {
    if (ws.sent.length) { resolve(ws.sent[ws.sent.length - 1]); return; }
    const t = setTimeout(
      () => reject(new Error('no ws reply within ' + timeoutMs + 'ms')),
      timeoutMs);
    ws._waiters.push((r) => { clearTimeout(t); resolve(r); });
  });
  return ws;
}

// The stale in-memory mirror: group fields behind GETTER TRAPS so any
// consultation of the mirror for group state is provable; vocab fields plain.
function makeMirror({ groups, active, vocab, trap }) {
  const es = {};
  Object.defineProperty(es, 'expand_groups', {
    enumerable: true,
    get() { trap.push('expand_groups'); return groups; },
  });
  Object.defineProperty(es, 'active_group', {
    enumerable: true,
    get() { trap.push('active_group'); return active; },
  });
  return Object.assign(es, vocab);
}

const MIRROR_VOCAB = {
  note_session_tags: ['sessm'],
  note_session_local_postfixes: ['locm'],
  note_session_favorite_postfixes: ['favm'],
  f8_postfix_auto_expand: 'MIRRORX',
  f8_postfix_suppress: 'MIRRORS',
  f8_postfix_deprecated: 'MIRRORD',
  f8_global_never_expand_tags: ['wfy-mirror-suppress'],
};

const ROOT = '11111111-2222-3333-4444-555555555555';
const STORAGE_GROUPS = [{
  id: 'grpcur1', name: 'Nexus | Manager Sessions · Curation (uncurated tail) [-2000]',
  prefixes: ['nexus--mgr'], root_uuid: ROOT, description: 'NEW DESC',
  default_sort_by: 'none', default_sort_direction: 'asc',
}];
const STALE_GROUPS = [{
  id: 'grpcur1', name: 'Nexus | Meta-Meta · Curation (uncurated tail)[-2000]',
  prefixes: ['nexus--meta-meta'], root_uuid: ROOT, description: 'OLD DESC',
  default_sort_by: 'none', default_sort_direction: 'asc',
}];

async function runHandler({ mode, localStore, syncStore, mirrorGroups, mirrorActive }) {
  const sw = makeSwContext(syncStore || {}, localStore || {});
  const page = makePageContext({ sw, mode: mode || 'ok' });
  const trap = [];
  const es = makeMirror({
    groups: mirrorGroups || STALE_GROUPS,
    active: mirrorActive || 'grp-stale',
    vocab: MIRROR_VOCAB,
    trap,
  });
  const ws = makeWs();
  page.runBranch(
    { action: 'get_slice_group_config', request_id: 'req-1' },
    ws, es, GlimpseHotkeys, page.requestDurableExpandGroups,
  );
  const reply = await ws.nextReply(3000);
  return { reply, trap, page, ws };
}

(async function main() {
  // -------------------------------------------------------------------------
  console.log('— DURABLE READ (§9 test 5: stale mirror, current storage) —');
  {
    const { reply, trap, page } = await runHandler({
      localStore: {
        [EXPAND_GROUPS_LOCAL_KEY]: {
          expand_groups: STORAGE_GROUPS, active_group: 'grpcur1',
        },
      },
      syncStore: { [SETTINGS_STORAGE_KEY]: {} },
    });
    eq(reply.action, 'get_slice_group_config_result', 'reply action shape unchanged');
    eq(reply.success, true, 'durable read succeeds');
    eq(reply.request_id, 'req-1', 'request_id echoed (broker rail)');
    eq(reply.expand_groups.length, 1, 'one group in the reply');
    eq(reply.expand_groups[0].name, STORAGE_GROUPS[0].name,
      'group NAME is the STORAGE value, not the stale mirror\'s');
    eq(reply.expand_groups[0].description, 'NEW DESC',
      'group description is the STORAGE value');
    eq(reply.active_group, 'grpcur1', 'active_group is the STORAGE value');
    ok(trap.indexOf('expand_groups') < 0 && trap.indexOf('active_group') < 0,
      'the mirror\'s group fields were NEVER consulted (getter trap silent)');
    const posted = page.win.posted.filter((m) => m && m.direction === 'page-to-sw');
    eq(posted.length, 1, 'exactly one page-to-sw bridge post');
    eq(posted[0].kind, 'get_expand_groups', 'bridge kind is get_expand_groups');
    ok(typeof posted[0].correlationId === 'string' && posted[0].correlationId,
      'bridge post carries a correlationId');
  }

  // -------------------------------------------------------------------------
  console.log('— MIGRATION FALLBACK (§9 test 5b: the ONE merge authority) —');
  {
    // Groups ONLY in the sync blob: loadExtensionSettings()'s one-time
    // migration fallback must answer them through the same handler — no new
    // merge logic anywhere (spec §5).
    const syncGroups = [{
      id: 'grpmig1', name: 'Migrated [7]', prefixes: ['mig'],
      root_uuid: ROOT, description: 'from sync',
    }];
    const { reply } = await runHandler({
      localStore: {},
      syncStore: {
        [SETTINGS_STORAGE_KEY]: {
          expand_groups: syncGroups, active_group: 'grpmig1',
        },
      },
    });
    eq(reply.success, true, 'migration path replies success');
    eq(reply.expand_groups.length, 1, 'migrated group present');
    eq(reply.expand_groups[0].name, 'Migrated [7]',
      'migrated name preserved verbatim');
    eq(reply.active_group, 'grpmig1', 'migrated active_group resolves');
  }

  // -------------------------------------------------------------------------
  console.log('— LOUD FAILURE (§9 test 6: bridge down, no mirror fallback) —');
  {
    const { reply, trap } = await runHandler({
      mode: 'bridge-down',
      localStore: {
        [EXPAND_GROUPS_LOCAL_KEY]: {
          expand_groups: STORAGE_GROUPS, active_group: 'grpcur1',
        },
      },
    });
    eq(reply.success, false, 'bridge failure → success:false (loud, never silent)');
    ok(typeof reply.error === 'string'
      && reply.error.indexOf('durable expand_groups read') >= 0,
      'the error NAMES the failed durable read');
    ok(reply.error.indexOf('no in-memory mirror fallback') >= 0,
      'the error states there is no mirror fallback');
    eq(reply.expand_groups, undefined, 'no group state in the failure reply');
    ok(trap.indexOf('expand_groups') < 0 && trap.indexOf('active_group') < 0,
      'the mirror was NEVER consulted for group state on the failure path');
    eq(reply.request_id, 'req-1', 'failure reply still echoes request_id');
  }

  // -------------------------------------------------------------------------
  console.log('— VOCAB FROM THE MIRROR (§9 test 7: unchanged data classes) —');
  {
    const { reply, trap } = await runHandler({
      localStore: {
        [EXPAND_GROUPS_LOCAL_KEY]: {
          expand_groups: STORAGE_GROUPS, active_group: 'grpcur1',
        },
      },
      syncStore: {
        // The sync blob carries DIFFERENT vocab than the mirror: the reply
        // must show the MIRROR's (the page-world extensionSettings remains
        // the vocab source — U7 changes ONLY the group-state source).
        [SETTINGS_STORAGE_KEY]: { f8_postfix_auto_expand: 'STORAGEX' },
      },
    });
    eq(reply.success, true, 'vocab run succeeds');
    eq(reply.f8_postfix_auto_expand, 'MIRRORX', 'auto-expand word from the mirror');
    eq(reply.f8_postfix_suppress, 'MIRRORS', 'suppress word from the mirror');
    eq(reply.f8_postfix_deprecated, 'MIRRORD', 'deprecated word from the mirror');
    eq(reply.f8_global_never_expand_tags, ['wfy-mirror-suppress'],
      'global-never list from the mirror');
    eq(reply.note_session_tags, ['#sessm'],
      'note-session tags from the mirror (#-prefixed, as today)');
    eq(reply.note_session_local_postfixes, ['locm'], 'local postfixes from the mirror');
    eq(reply.note_session_favorite_postfixes, ['favm'], 'favorite postfixes from the mirror');
    ok(trap.indexOf('expand_groups') < 0,
      'vocab reply still never reads the mirror\'s GROUP fields');
  }

  // -------------------------------------------------------------------------
  console.log('— WIRING LOCKS (§14 negative postconditions, real bytes) —');
  {
    const branchRegion = regionBetween(clientSrc, CLIENT_BRANCH_ANCHOR,
      "} else if (request.action === 'get_current_modal_render_spec') {");
    ok(branchRegion.indexOf('requestDurableExpandGroups()') >= 0,
      'the get branch sources group state via requestDurableExpandGroups()');
    ok(branchRegion.indexOf('extensionSettings.expand_groups') < 0,
      'NEGATIVE POSTCONDITION 1: no extensionSettings.expand_groups in the branch');
    ok(branchRegion.indexOf('extensionSettings.active_group') < 0,
      'NEGATIVE POSTCONDITION 1: no extensionSettings.active_group in the branch');
    ok(branchRegion.indexOf('success: false') >= 0
      && branchRegion.indexOf('no in-memory mirror fallback') >= 0,
      'NEGATIVE POSTCONDITION 2: loud failure reply, no silent mirror fallback');

    ok(requestDurableExpandGroupsSrc.indexOf("kind: 'get_expand_groups'") >= 0,
      'the bridge helper posts the get_expand_groups kind');
    ok(requestDurableExpandGroupsSrc.indexOf('10000') >= 0,
      'the helper timeout (10s) stays below the server\'s 15s transport timeout');

    const swRegion = regionBetween(bgSrc, SW_HANDLER_ANCHOR,
      "if (message.kind === 'set_anchor_views') {");
    ok(swRegion.indexOf('loadExtensionSettings()') >= 0,
      'the SW handler reuses loadExtensionSettings() (the ONE merge authority)');
    ok(swRegion.indexOf('chrome.storage.local.get') < 0
      && swRegion.indexOf('chrome.storage.sync.get') < 0
      && swRegion.indexOf('chrome.storage.local.set') < 0
      && swRegion.indexOf('chrome.storage.sync.set') < 0,
      'NO THIRD COPY: the SW handler performs no storage read/write of its own');
    ok(swRegion.indexOf('expand_groups:') >= 0 && swRegion.indexOf('active_group:') >= 0,
      'the SW handler answers both group fields');

    const setRegion = regionBetween(clientSrc,
      "} else if (request.action === 'set_slice_groups') {",
      'ws.send(JSON.stringify(_ack));');
    ok(setRegion.indexOf('persistExpandGroups(') >= 0,
      'WRITE PROTOCOL UNCHANGED: set_slice_groups still funnels through persistExpandGroups');

    // MUST-DISPOSITION pins (expected unchanged; the grammar/parser twins and
    // the display-normalizers are used by this unit, never edited by it).
    ok(clientSrc.indexOf('function persistExpandGroups(groups, activeId) {') >= 0,
      'persistExpandGroups signature intact');
    ok(clientSrc.indexOf('function adoptExtensionSettings(incoming) {') >= 0,
      'adoptExtensionSettings signature intact');
    ok(clientSrc.indexOf('function parseSliceGroupName(name) {') >= 0,
      'parseSliceGroupName (the JS grammar twin) intact');
    ok(sharedSrc.indexOf('function normalizeExpandGroups(rawGroups) {') >= 0
      && sharedSrc.indexOf('function sanitizeExpandGroup(group) {') >= 0,
      'hotkeys-shared normalizers intact');
  }

  console.log('');
  console.log(`${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.error('HARNESS ERROR (extraction/construction failed loudly):', e);
  process.exit(1);
});
