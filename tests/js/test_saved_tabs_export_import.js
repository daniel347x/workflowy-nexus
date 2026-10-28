// ============================================================================
// test_saved_tabs_export_import.js — Saved Tabs (Tier-0) export/import harness.
//
// Covers the PURE functions of the widget's Saved-Tabs render-state
// export/import feature (glimpse-cache-client.js):
//   • buildSavedTabsExportPayload / buildSavedTabsExportJson — the ONE
//     payload-format assembly ({alias,title,spec} — no url since U02; spec VERBATIM),
//     COMPACT single-line JSON since R4 (paste-safe: the Workflowy UI note
//     editor NBSP-mangles the leading indent spaces of multi-line pastes —
//     Ledger ST-007 / ruling [ap:K6IJS0]), byte-diffed against the 10 real
//     tab specs harvested 2026-09-14 (the fixtures below are copied from
//     the harvest record [ap:O5V39E]).
//   • savedTabsClassifyRow — exportable vs the not-exportable reasons
//     (builder refusals, frozen-DOM captures, non-responding tabs).
//   • parseSavedTabsImport — tolerant parse (canonical array / single
//     wrapper object / bare render_spec blob), actionable throws;
//     whitespace-agnostic — legacy PRETTY payloads (pre-R4 exports and the
//     two existing pretty dock runs) keep parsing on every tolerated shape.
//   • savedTabsSpecKind / savedTabsEntryLabel — human-friendly label
//     derivation (group name, wrapper title/alias fallback, lightning
//     selection labels).
//   • savedTabsGroupIdExists — the import pre-validation kernel (EXACT
//     string match; the grp-/bare-hex flavors must never cross-match).
//
// RUN:   node tests/js/test_saved_tabs_export_import.js
// EXIT:  0 = all pass, 1 = at least one failure. Zero dependencies.
//
// Uses the repo's extraction idiom (see tests/js/README.md): functions are
// extracted VERBATIM from the real client file bytes, so a rename or
// signature change fails LOUDLY here. Before the feature existed, this
// harness failed at extraction ("function not found") — that run is the
// recorded pre-implementation evidence.
// ============================================================================
'use strict';

const fs = require('fs');
const path = require('path');

const CLIENT_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'glimpse-cache-client.js');
const src = fs.readFileSync(CLIENT_FILE, 'utf8');

function extractFunction(name) {
  // Anchor on the REAL definition (newline + the IIFE's 2-space indent), not
  // the `role=function NAME(` line inside its preceding @beacon comment.
  let idx = src.indexOf(`\n  function ${name}(`);
  if (idx >= 0) idx += 1; // skip the newline
  else idx = src.indexOf(`function ${name}(`);
  if (idx < 0) throw new Error(`function not found in client file: ${name}`);
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

// The pure Saved-Tabs functions have NO module-level dependencies (by
// contract — payload assembly must be a pure function so Tier 1 can reuse
// it), so a plain concatenated eval is enough.
const body = [
  'savedTabsSpecKind',
  'savedTabsClassifyRow',
  'savedTabsGroupIdExists',
  'parseSavedTabsImport',
  'savedTabsEntryLabel',
  'buildSavedTabsExportPayload',
  'buildSavedTabsExportJson',
].map(extractFunction).join('\n');

const api = new Function(`
  ${body}
  return {
    savedTabsSpecKind,
    savedTabsClassifyRow,
    savedTabsGroupIdExists,
    parseSavedTabsImport,
    savedTabsEntryLabel,
    buildSavedTabsExportPayload,
    buildSavedTabsExportJson,
  };
`)();

let failures = 0;
function check(label, cond) {
  if (cond) { console.log('ok   ' + label); }
  else { failures++; console.log('FAIL ' + label); }
}

// ---------------------------------------------------------------------------
// FIXTURES — the 10 real tab specs harvested 2026-09-14 ([ap:O5V39E]).
// These are the BYTE-SHAPE the export button must emit: each fixture is the
// exact compact JSON the modal's ⚡ focus/export button produced for that tab
// (focus-button key order, all 15 fields). The round-trip tests below assert
// JSON.stringify(payloadRow.spec) === fixture — byte equality.
// ---------------------------------------------------------------------------
const FIXTURES = [
  // 1. META META — ⚠ BARE 32-hex group_id flavor.
  '{"kind":"slice","root_uuid":"adee3c89-5cf4-4c77-8887-a7c08b28f5cf","group_id":"47967a42c31241589a64e321668a1691","group_name":"Nexus|Meta-Meta Session · OpenCode Launch Engineering[-1000]","lightning":null,"match_scope":"name","descend_policy":"all","sort_by":"none","sort_direction":"asc","since":null,"headings_only":false,"as_markdown":true,"content_format":"markdown","tree_mode":"render_tree","focus":{"mode":"row","uuid":"adee3c89-5cf4-4c77-8887-a7c08b28f5cf","ancestor_path":[],"occurrence":0}}',
  // 2. Program Conductor Toolkit — grp-<uuid-with-dashes> flavor.
  '{"kind":"slice","root_uuid":"f382b87e-bce3-4bb6-9cc3-455ebe272896","group_id":"grp-e471c51c-aa6c-4362-899b-8bdc256f9e89","group_name":"Nexus|Program Conductor Toolkit[-500]","lightning":null,"match_scope":"name","descend_policy":"all","sort_by":"none","sort_direction":"asc","since":null,"headings_only":false,"as_markdown":true,"content_format":"markdown","tree_mode":"render_tree","focus":{"mode":"row","uuid":"f382b87e-bce3-4bb6-9cc3-455ebe272896","ancestor_path":[],"occurrence":0}}',
  // 3. CARTOGRAPHER docs.
  '{"kind":"slice","root_uuid":"b49affa1-3930-95e3-b2fe-ad9b881285e2","group_id":"grp-9b11892d-d13e-4785-88da-427b66725ee2","group_name":"Load GLIMPSE|Load GLIMPSE - Source Code[-5]","lightning":null,"match_scope":"name","descend_policy":"all","sort_by":"none","sort_direction":"asc","since":null,"headings_only":false,"as_markdown":true,"content_format":"markdown","tree_mode":"render_tree","focus":{"mode":"row","uuid":"b49affa1-3930-95e3-b2fe-ad9b881285e2","ancestor_path":[],"occurrence":0}}',
  // 4. CONVRISK BACKGROUND — the only sort_by:'created' tab.
  '{"kind":"slice","root_uuid":"c8af1a07-3d81-4fc3-bfbd-652bfbadbac3","group_id":"grp-9e852018-b482-4179-933f-79a7eb91a6c3","group_name":"Work|Conversational|CONVRISK - CURRENT (limited to anchor dock)[-20]","lightning":null,"match_scope":"name","descend_policy":"all","sort_by":"created","sort_direction":"asc","since":null,"headings_only":false,"as_markdown":true,"content_format":"markdown","tree_mode":"render_tree","focus":{"mode":"row","uuid":"c8af1a07-3d81-4fc3-bfbd-652bfbadbac3","ancestor_path":[],"occurrence":0}}',
  // 5. CONVRISK DEMO DASHBOARD — ⚠ BARE 32-hex group_id flavor.
  '{"kind":"slice","root_uuid":"4ed09e84-1c78-4287-b365-740aed542f17","group_id":"278e0a941d554fbd80de2b71834d3376","group_name":"Work|Conversational|CONVRISK Dashboard Agent Mirror: Source Code[1010]","lightning":null,"match_scope":"name","descend_policy":"all","sort_by":"none","sort_direction":"asc","since":null,"headings_only":false,"as_markdown":true,"content_format":"markdown","tree_mode":"render_tree","focus":{"mode":"row","uuid":"4ed09e84-1c78-4287-b365-740aed542f17","ancestor_path":[],"occurrence":0}}',
  // 6. DB AUTOPILOT — ⚠ BARE 32-hex group_id flavor.
  '{"kind":"slice","root_uuid":"f6940b45-d631-4341-9a33-12ad85711945","group_id":"0f487354660446188f29158e5500bf97","group_name":"Work|Conversational|DB Autopilot[1020]","lightning":null,"match_scope":"name","descend_policy":"all","sort_by":"none","sort_direction":"asc","since":null,"headings_only":false,"as_markdown":true,"content_format":"markdown","tree_mode":"render_tree","focus":{"mode":"row","uuid":"f6940b45-d631-4341-9a33-12ad85711945","ancestor_path":[],"occurrence":0}}',
  // 7. PROGRAM: ML Pipeline Demo Sprint.
  '{"kind":"slice","root_uuid":"b8bbc187-c992-467e-b608-d26bce9d2a2b","group_id":"grp-3a38c740-4e38-441b-80ff-c9a69d02be2c","group_name":"Work|Conversational|ML Pipeline Demo Sprint[1050]","lightning":null,"match_scope":"name","descend_policy":"all","sort_by":"none","sort_direction":"asc","since":null,"headings_only":false,"as_markdown":true,"content_format":"markdown","tree_mode":"render_tree","focus":{"mode":"row","uuid":"b8bbc187-c992-467e-b608-d26bce9d2a2b","ancestor_path":[],"occurrence":0}}',
  // 8. CONVRISK: AGENT ROOT — raw_root.
  '{"kind":"raw_root","root_uuid":"7d600ebc-5e59-4cf0-9f3e-063224d1c668","group_id":null,"group_name":null,"lightning":null,"match_scope":"name_and_note","descend_policy":"all","sort_by":"none","sort_direction":"asc","since":null,"headings_only":false,"as_markdown":true,"content_format":"markdown","tree_mode":"render_tree","focus":{"mode":"row","uuid":"7d600ebc-5e59-4cf0-9f3e-063224d1c668","ancestor_path":[],"occurrence":0}}',
  // 9. SHORT HISTORIC SUMMARY — raw_root.
  '{"kind":"raw_root","root_uuid":"f9882ac6-9883-4295-a304-391db426172a","group_id":null,"group_name":null,"lightning":null,"match_scope":"name_and_note","descend_policy":"all","sort_by":"none","sort_direction":"asc","since":null,"headings_only":false,"as_markdown":true,"content_format":"markdown","tree_mode":"render_tree","focus":{"mode":"row","uuid":"f9882ac6-9883-4295-a304-391db426172a","ancestor_path":[],"occurrence":0}}',
  // 10. CONVRISK BEACON SLICE.
  '{"kind":"slice","root_uuid":"d7d88e40-c06f-42c2-ac36-c91812a88873","group_id":"grp-210a3249-589c-4f20-8312-847ea1315169","group_name":"Work|Conversational|CONVRISK SOURCE CODE ALL BEACONS[1000]","lightning":null,"match_scope":"name","descend_policy":"all","sort_by":"none","sort_direction":"asc","since":null,"headings_only":false,"as_markdown":true,"content_format":"markdown","tree_mode":"render_tree","focus":{"mode":"row","uuid":"d7d88e40-c06f-42c2-ac36-c91812a88873","ancestor_path":[],"occurrence":0}}',
];

// The third stored group_id flavor: grp-<uuid4().hex> (NO dashes, server-
// minted per the server.py comment). Not present in the 10 fixtures, so it
// is synthesized on fixture 1's spec — it must round-trip byte-exact too.
const GRP_HEX_NODASH = '{"kind":"slice","root_uuid":"adee3c89-5cf4-4c77-8887-a7c08b28f5cf","group_id":"grp-47967a42c31241589a64e321668a1691","group_name":"Nexus|Meta-Meta Session · OpenCode Launch Engineering[-1000]","lightning":null,"match_scope":"name","descend_policy":"all","sort_by":"none","sort_direction":"asc","since":null,"headings_only":false,"as_markdown":true,"content_format":"markdown","tree_mode":"render_tree","focus":{"mode":"row","uuid":"adee3c89-5cf4-4c77-8887-a7c08b28f5cf","ancestor_path":[],"occurrence":0}}';

// Build a collected row exactly as the SW collection assembles it:
// url/title LIVE from chrome.tabs, endpoint_id/alias/result from the tab's
// page-world response (result.render_spec = the builder's stored blob).
function makeRow(i, fixtureStr, opts) {
  const o = opts || {};
  return {
    tab_id: 100 + i,
    endpoint_id: (o.endpoint_id !== undefined) ? o.endpoint_id : ('tab-fixture-' + i),
    alias: (o.alias !== undefined) ? o.alias : ('TAB' + i),
    title: (o.title !== undefined) ? o.title : ('WFY - TAB' + i),
    url: (o.url !== undefined) ? o.url : ('https://workflowy.com/#/fixture' + i),
    responded: (o.responded !== undefined) ? o.responded : true,
    result: (o.result !== undefined) ? o.result : {
      ok: true,
      modal_payload_kind: 'cache_render_spec',
      render_spec: JSON.parse(fixtureStr),
    },
  };
}

// ----------------------- classify + payload byte-round-trip ----------------
const rows = FIXTURES.map((f, i) => makeRow(i, f));

let allExportable = true;
rows.forEach((r, i) => {
  const c = api.savedTabsClassifyRow(r);
  if (!c.exportable || c.reason !== '') {
    allExportable = false;
    console.log('     fixture ' + (i + 1) + ' classified NOT exportable: ' + c.reason);
  }
});
check('all 10 harvest fixtures classify exportable', allExportable);

const payload = api.buildSavedTabsExportPayload(rows, null);
check('payload has one row per fixture', payload.length === FIXTURES.length);
// U02 ([ap:DLGU66], Dan's ruling): the export no longer writes the page url
// — the rows still CARRY one (live from chrome.tabs), the payload drops it.
check('payload row keys are exactly {alias,title,spec} in order (U02: no url)',
  payload.every((p) => JSON.stringify(Object.keys(p)) === '["alias","title","spec"]'));
check('U02: no payload row carries a url even though every collected row has one',
  rows.every((r) => typeof r.url === 'string' && r.url.length > 0)
  && payload.every((p) => !Object.prototype.hasOwnProperty.call(p, 'url')));

let byteExact = true;
payload.forEach((p, i) => {
  const re = JSON.stringify(p.spec);
  if (re !== FIXTURES[i]) {
    byteExact = false;
    console.log('     fixture ' + (i + 1) + ' spec round-trip differs:');
    console.log('       got:      ' + re);
    console.log('       expected: ' + FIXTURES[i]);
  }
});
check('every fixture spec round-trips BYTE-EXACT through the payload', byteExact);

check('alias/title ride alongside every spec (no url, U02)',
  payload[0].alias === 'TAB0' && payload[0].title === 'WFY - TAB0'
  && payload[0].url === undefined);

// The three group_id flavors: bare 32-hex (fixture 1/5/6), grp-dashed
// (fixture 2 etc.), grp-hex-nodash (synthesized) — all VERBATIM, never
// re-minted or re-prefixed ([ap:HIH414] ruling 4-Q2).
const flavorRows = [makeRow(0, FIXTURES[0]), makeRow(1, FIXTURES[1]), makeRow(2, GRP_HEX_NODASH)];
const flavorPayload = api.buildSavedTabsExportPayload(flavorRows, null);
check('bare 32-hex group_id survives verbatim',
  flavorPayload[0].spec.group_id === '47967a42c31241589a64e321668a1691');
check('grp-dashed group_id survives verbatim',
  flavorPayload[1].spec.group_id === 'grp-e471c51c-aa6c-4362-899b-8bdc256f9e89');
check('grp-hex-nodash group_id survives verbatim (byte-equal)',
  JSON.stringify(flavorPayload[2].spec) === GRP_HEX_NODASH);

// buildSavedTabsExportJson is the ONE serializer (Tier-1 file/dock routes
// must produce the same bytes). R4: its output is COMPACT single-line JSON —
// the Workflowy UI note editor converts every leading indent space of a
// pasted multi-line payload to U+00A0 NBSP and JSON.parse rejects NBSP
// (Ledger ST-007); a compact payload has no leading whitespace to mangle.
const json = api.buildSavedTabsExportJson(rows, null);
check('export JSON = JSON.stringify(payload) — COMPACT, the one format',
  json === JSON.stringify(payload));
check('export JSON contains NO newline character (single-line, paste-safe)',
  json.indexOf('\n') === -1 && json.indexOf('\r') === -1);
check('export JSON parses back to byte-equal specs',
  JSON.stringify(JSON.parse(json)[0].spec) === FIXTURES[0]);

// ------------------------------ selection ----------------------------------
const sel = ['tab-fixture-1', 'tab-fixture-3'];
const selPayload = api.buildSavedTabsExportPayload(rows, sel);
check('selection filters by endpoint_id', selPayload.length === 2
  && selPayload[0].alias === 'TAB1' && selPayload[1].alias === 'TAB3');

const refusedRow = makeRow(99, FIXTURES[0], {
  endpoint_id: 'tab-refused',
  responded: true,
  result: { ok: false, error: 'The docked Markdown modal is DOM-sourced but NOT frozen, so its current tree is not reproducible. Freeze the dock first (the 🔒 Freeze button) to pin the current view, then retry render_spec="modal".' },
});
// Follow-up 2 ([ap:DLGU66]): a NOT-exportable row enters ONLY name-only, and
// only when its tab has an alias. An unnamed refused row never enters.
const refusedUnnamed = Object.assign({}, refusedRow, { alias: '', endpoint_id: 'tab-refused-unnamed' });
const selWithRefused = api.buildSavedTabsExportPayload(
  rows.concat([refusedUnnamed]), sel.concat(['tab-refused-unnamed']));
check('a selected, NOT-exportable, UNNAMED row never enters the payload',
  selWithRefused.length === 2);
const selWithNamed = api.buildSavedTabsExportPayload(
  rows.concat([refusedRow]), sel.concat(['tab-refused']));
check('follow-up 2: a selected, NOT-exportable row WITH an alias enters NAME-ONLY',
  selWithNamed.length === 3
  && JSON.stringify(selWithNamed[2]) === JSON.stringify({ alias: 'TAB99', title: 'WFY - TAB99', spec: null }));
check('follow-up 2: the name-only row keeps the {alias,title,spec} key order',
  JSON.stringify(Object.keys(selWithNamed[2])) === '["alias","title","spec"]');
check('follow-up 2: a whitespace-only alias is not a name (row stays out)',
  api.buildSavedTabsExportPayload([Object.assign({}, refusedRow, { alias: '   ' })], null).length === 0);
check('follow-up 2: a NON-RESPONDING tab (alias never reported) stays out',
  api.buildSavedTabsExportPayload([makeRow(95, FIXTURES[0], { alias: '', responded: false, result: null })], null).length === 0);
check('follow-up 2: full-spec rows are untouched by the name-only rule (byte-equal specs)',
  JSON.stringify(selWithNamed[0].spec) === FIXTURES[1] && JSON.stringify(selWithNamed[1].spec) === FIXTURES[3]);
// The import side still parses a name-only entry as spec-less (invalid) with
// its alias intact — the dialog derives name-only from exactly that.
const noParse = api.parseSavedTabsImport(JSON.stringify([selWithNamed[2]]));
check('follow-up 2: a name-only payload entry parses as spec-less (valid:false) with its alias',
  noParse.entries.length === 1 && noParse.entries[0].valid === false
  && noParse.entries[0].spec === null && noParse.entries[0].alias === 'TAB99');

// A row with no endpoint_id at all is keyed by the 'tab-<tab_id>' fallback
// (the same derivation the popup's checkboxes use) — selection must agree.
const noEpRow = makeRow(50, FIXTURES[2], { endpoint_id: '' });
const noEpPayload = api.buildSavedTabsExportPayload([noEpRow], ['tab-150']);
check('endpoint_id-less row selects via the tab-<id> fallback key',
  noEpPayload.length === 1 && JSON.stringify(noEpPayload[0].spec) === FIXTURES[2]);

// --------------------------- classification --------------------------------
const cRef = api.savedTabsClassifyRow(refusedRow);
// E2 ([ap:ARDOF6] §E2 wording fix): in the EXPORT context freezing does NOT
// help (a frozen DOM capture is not exportable either) — the reason is the
// export-specific text, never the agent-readback path's 'freeze first'.
check('unfrozen live-DOM refusal -> not exportable, reason is the EXPORT-specific E2 text (no freeze advice)',
  !cRef.exportable
  && cRef.reason === 'live DOM view — nothing to reproduce; apply a slice, a Dock render or a Lightning Rod to make this tab exportable'
  && !/freeze/i.test(cRef.reason));

const nothingApplied = makeRow(98, FIXTURES[0], {
  responded: true,
  result: { ok: false, error: 'The docked Markdown modal is not currently showing a cache-sourced render or a frozen DOM view. Apply a slice (or push a scope), or freeze a DOM view, then retry render_spec="modal".' },
});
const cNA = api.savedTabsClassifyRow(nothingApplied);
check('nothing-applied refusal -> not exportable, reason says nothing applied',
  !cNA.exportable && /nothing applied/i.test(cNA.reason));

const frozenDom = makeRow(97, FIXTURES[0], {
  responded: true,
  result: { ok: true, modal_payload_kind: 'dom_captured_tree' },
});
const cFD = api.savedTabsClassifyRow(frozenDom);
check('frozen DOM capture -> not exportable (ephemeral tree, no spec)',
  !cFD.exportable && /frozen DOM capture/i.test(cFD.reason));

const noResp = makeRow(96, FIXTURES[0], { responded: false, result: null });
const cNR = api.savedTabsClassifyRow(noResp);
check('non-responding tab -> not exportable, reason says no response',
  !cNR.exportable && /no response/i.test(cNR.reason));

const malformed = api.savedTabsClassifyRow({ responded: true, result: { ok: true, modal_payload_kind: 'cache_render_spec' } });
check('cache_render_spec without a render_spec object -> not exportable',
  !malformed.exportable && malformed.reason.length > 0);

// --------------------------- tolerant parse --------------------------------
// The canonical import shape is the serializer's own COMPACT bytes; PRETTY is
// the LEGACY form (pre-R4 exports + the two existing pretty dock runs in dock
// 0d53381d) and must keep parsing on every tolerated shape — the parser is
// whitespace-agnostic, ZERO behavior changes (contract [ap:K6IJS0] §3).
let parsed = api.parseSavedTabsImport(json);
check('canonical COMPACT array parses to the same entry count',
  parsed.entries.length === payload.length);
check('canonical COMPACT entries are valid and carry wrapper fields (url absent → \'\', U02)',
  parsed.entries[0].valid === true && parsed.entries[0].alias === 'TAB0'
  && parsed.entries[0].url === ''
  && JSON.stringify(parsed.entries[0].spec) === FIXTURES[0]);

// LEGACY payloads carry the pre-U02 {alias,title,url,spec} shape (every
// run already in dock 0d53381d): they must keep importing — the url parses
// as before and is simply never used for navigation (U02).
const legacyPayload = payload.map((p, i) => ({
  alias: p.alias, title: p.title, url: 'https://workflowy.com/#/fixture' + i, spec: p.spec,
}));
const legacyPretty = JSON.stringify(legacyPayload, null, 2);
parsed = api.parseSavedTabsImport(legacyPretty);
check('legacy PRETTY array parses to the same entry count',
  parsed.entries.length === payload.length);
check('legacy PRETTY (url-carrying, pre-U02) entries are valid and re-compact byte-equal',
  parsed.entries[0].valid === true && parsed.entries[0].alias === 'TAB0'
  && parsed.entries[0].url === 'https://workflowy.com/#/fixture0'
  && JSON.stringify(parsed.entries[0].spec) === FIXTURES[0]);
parsed = api.parseSavedTabsImport(JSON.stringify(legacyPayload));
check('legacy COMPACT url-carrying payload (pre-U02) still imports, every entry valid',
  parsed.entries.length === payload.length && parsed.entries.every((e) => e.valid === true));

parsed = api.parseSavedTabsImport(JSON.stringify(payload[0]));
check('single {alias,title,url,spec} object parses as one entry',
  parsed.entries.length === 1 && parsed.entries[0].valid === true);

parsed = api.parseSavedTabsImport(JSON.stringify(payload[0], null, 2));
check('legacy PRETTY single wrapper object parses as one entry',
  parsed.entries.length === 1 && parsed.entries[0].valid === true
  && JSON.stringify(parsed.entries[0].spec) === FIXTURES[0]);

parsed = api.parseSavedTabsImport(FIXTURES[7]);
check('bare render_spec blob (the ⚡ button output) parses as one entry',
  parsed.entries.length === 1 && parsed.entries[0].valid === true
  && parsed.entries[0].spec.kind === 'raw_root'
  && parsed.entries[0].alias === '');

parsed = api.parseSavedTabsImport(JSON.stringify(JSON.parse(FIXTURES[7]), null, 2));
check('legacy PRETTY bare render_spec blob parses as one entry',
  parsed.entries.length === 1 && parsed.entries[0].valid === true
  && parsed.entries[0].spec.kind === 'raw_root'
  && parsed.entries[0].alias === '');

parsed = api.parseSavedTabsImport('[{"alias":"x","title":"t","url":"u"},{"alias":"y","title":"t2","url":"u2","spec":{"kind":"raw_root","root_uuid":"7d600ebc-5e59-4cf0-9f3e-063224d1c668"}}]');
check('entry without a spec object is kept but flagged invalid',
  parsed.entries.length === 2 && parsed.entries[0].valid === false
  && parsed.entries[0].invalid_reason.length > 0 && parsed.entries[1].valid === true);

parsed = api.parseSavedTabsImport('[]');
check('empty array parses to zero entries (no throw)', parsed.entries.length === 0);

let threw = null;
try { api.parseSavedTabsImport('{not json'); } catch (e) { threw = e; }
check('bad JSON throws with an actionable message',
  threw instanceof Error && /invalid JSON/i.test(threw.message));

threw = null;
try { api.parseSavedTabsImport('42'); } catch (e) { threw = e; }
check('non-object JSON throws (wrong shape)', threw instanceof Error);

threw = null;
try { api.parseSavedTabsImport('{"foo":1,"bar":[2]}'); } catch (e) { threw = e; }
check('wrong-shape object throws with the expected-format message',
  threw instanceof Error && /alias, title, url, spec/.test(threw.message));

threw = null;
try {
  api.parseSavedTabsImport(JSON.stringify({
    kind: 'glimpse-slice-groups-export', version: 1, active_group: null, expand_groups: [],
  }));
} catch (e) { threw = e; }
check('slice-groups export blob throws with a redirect to the right Import button',
  threw instanceof Error && /slice-groups/i.test(threw.message) && /Import/i.test(threw.message));

threw = null;
try {
  api.parseSavedTabsImport(JSON.stringify({ kind: 'glimpse_anchor_views_export', version: 1, anchor_views: [] }));
} catch (e) { threw = e; }
check('anchor-views export blob throws (wrong file)', threw instanceof Error);

// ------------------------- kind + label derivation --------------------------
check('specKind: explicit kind wins', api.savedTabsSpecKind({ kind: 'slice' }) === 'slice');
check('specKind: lightning presence derives lightning_rod',
  api.savedTabsSpecKind({ lightning: { mode: 'render_tags' } }) === 'lightning_rod');
check('specKind: group_id+root_uuid derives slice_override',
  api.savedTabsSpecKind({ group_id: 'g', root_uuid: 'r' }) === 'slice_override');
check('specKind: group_id alone derives slice', api.savedTabsSpecKind({ group_id: 'g' }) === 'slice');
check('specKind: nothing derives raw_root', api.savedTabsSpecKind({}) === 'raw_root');

check('label: slice -> group_name',
  api.savedTabsEntryLabel({ alias: 'A', title: 'T', spec: JSON.parse(FIXTURES[1]) })
    === 'Nexus|Program Conductor Toolkit[-500]');
check('label: slice without group_name -> group_id',
  api.savedTabsEntryLabel({ spec: { kind: 'slice', group_id: 'grp-abc', group_name: null } }) === 'grp-abc');
check('label: slice_override mentions the override root (short)',
  /root adee3c89/.test(api.savedTabsEntryLabel({
    spec: { kind: 'slice_override', group_id: 'g', group_name: 'G', root_uuid: 'adee3c89-5cf4-4c77-8887-a7c08b28f5cf' },
  })));
check('label: raw_root -> wrapper alias first',
  api.savedTabsEntryLabel({ alias: 'CONVRISK: AGENT ROOT', title: 'WFY - CONVRISK: AGENT ROOT', spec: JSON.parse(FIXTURES[7]) })
    === 'CONVRISK: AGENT ROOT');
check('label: raw_root without alias -> wrapper title',
  api.savedTabsEntryLabel({ alias: '', title: 'WFY - SHORT HISTORIC SUMMARY', spec: JSON.parse(FIXTURES[8]) })
    === 'WFY - SHORT HISTORIC SUMMARY');
check('label: raw_root without alias/title -> kind + shortened root_uuid',
  api.savedTabsEntryLabel({ spec: JSON.parse(FIXTURES[8]) }) === 'raw_root f9882ac6');
check('label: lightning render_tags -> tag list (max 4 + overflow)',
  api.savedTabsEntryLabel({ spec: { kind: 'lightning_rod', lightning: { mode: 'render_tags', tags: ['a', '#b', 'c', 'd', 'e'] } } })
    === '\u26A1 tags: #a #b #c #d +1');
check('label: lightning render_search -> quoted query',
  /search: "invoice OR receipt"/.test(api.savedTabsEntryLabel({
    spec: { kind: 'lightning_rod', lightning: { mode: 'render_search', query: 'invoice OR receipt' } },
  })));
check('label: lightning render_symbols -> symbol count',
  api.savedTabsEntryLabel({ spec: { kind: 'lightning_rod', lightning: { mode: 'render_symbols', symbols: ['a', 'b', 'c'] } } })
    === '\u26A1 symbols \u00D73');
check('label: lightning render_lens -> lens (+query when present)',
  api.savedTabsEntryLabel({ spec: { kind: 'lightning_rod', lightning: { mode: 'render_lens', lens: {} } } }) === '\u26A1 lens'
  && /lens: "q"/.test(api.savedTabsEntryLabel({ spec: { kind: 'lightning_rod', lightning: { mode: 'render_lens', lens: {}, query: 'q' } } })));
check('label: invalid entry -> says so', /invalid entry/i.test(api.savedTabsEntryLabel({ spec: null })));

// ------------------------ group pre-validation kernel -----------------------
const groups = [
  { id: '47967a42c31241589a64e321668a1691', name: 'bare' },
  { id: 'grp-e471c51c-aa6c-4362-899b-8bdc256f9e89', name: 'dashed' },
  { id: 'grp-47967a42c31241589a64e321668a1691', name: 'hexnodash' },
];
check('groupIdExists: exact bare-hex match', api.savedTabsGroupIdExists('47967a42c31241589a64e321668a1691', groups));
check('groupIdExists: exact grp-dashed match', api.savedTabsGroupIdExists('grp-e471c51c-aa6c-4362-899b-8bdc256f9e89', groups));
check('groupIdExists: exact grp-hex-nodash match', api.savedTabsGroupIdExists('grp-47967a42c31241589a64e321668a1691', groups));
// The pre-validation MIRRORS _handle_render_slice_modal's exact-string match:
// a flavor the render handler would NOT exact-match must NOT validate here —
// otherwise a stale/foreign id rides into the server's silent active/first
// fallback and paints the WRONG slice "successfully" ([ap:6RHNGJ] §4/§7.1).
check('groupIdExists: dashed-UUID flavor of a bare-stored id does NOT match',
  !api.savedTabsGroupIdExists('grp-47967a42-5c31-2415-8964-e321668a1691', groups));
check('groupIdExists: bare form of a grp-dashed-stored id does NOT match',
  !api.savedTabsGroupIdExists('e471c51caa6c4362899b8bdc256f9e89', groups));
check('groupIdExists: unknown id refused', !api.savedTabsGroupIdExists('grp-deadbeef', groups));
check('groupIdExists: non-string refused', !api.savedTabsGroupIdExists(null, groups) && !api.savedTabsGroupIdExists(42, groups));
check('groupIdExists: empty/missing store refused',
  !api.savedTabsGroupIdExists('x', []) && !api.savedTabsGroupIdExists('x', null));

console.log(failures === 0 ? 'ALL PASS' : `${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);
