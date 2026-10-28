// ============================================================================
// test_since_codec.js — unit 4d(a) ([ap:TVIIBN]) JS-twin harness: the
// extension's SinceCodec token grammar, the since wire decoder, and the
// recency-basis carry-through in the ONE spec fan-in.
//
// Why this harness exists: the Python side's codec is locked by
// tests/test_recency_selection.py, but NOTHING under tests/js exercised the
// JS twin — a JS-only regression (e.g. the ribbon emitting a positional tail
// under a token the server now reads as 'newest') would be silent. This
// harness extracts the codec pair + sinceFromWire + _normSpecViewFields
// VERBATIM from the real client bytes (the repo's extraction idiom), so a
// rename or signature change fails loudly at extraction. Against the pre-4d(a)
// tree it fails: fromTokenResult rejected 'newest N' entirely (the recorded
// drift) and 'bottom N' did not exist, sinceFromWire produced objects with no
// selection field, and _normSpecViewFields carried no recent_by.
//
// RUN:   node tests/js/test_since_codec.js
// EXIT:  0 = all pass, 1 = at least one failure. Zero dependencies.
// ============================================================================
'use strict';

const fs = require('fs');
const path = require('path');

const CLIENT_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'glimpse-cache-client.js');
const clientSrc = fs.readFileSync(CLIENT_FILE, 'utf8');

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

// --- extraction (verbatim bytes; a rename fails loudly) ---------------------
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
function extractIifeFunction(src, name) {
  for (const prefix of ['\n  async function ' + name + '(', '\n  function ' + name + '(']) {
    const idx = src.indexOf(prefix);
    if (idx >= 0) return braceSlice(src, idx + 1);
  }
  throw new Error('function not found in client file: ' + name);
}

// The codec object lives at 2-space indent inside the IIFE; its freeze close
// is the first '\n  });' after the anchor (the object literal never contains
// a 2-space-indented '});' of its own).
function extractIifeConst(src, name) {
  const anchor = '\n  const ' + name + ' = Object.freeze({';
  const idx = src.indexOf(anchor);
  if (idx < 0) throw new Error('const not found in client file: ' + name);
  const end = src.indexOf('\n  });', idx);
  if (end < 0) throw new Error('const close not found for: ' + name);
  return src.slice(idx + 1, end + '\n  });'.length);
}

// Evaluate the codec + wire decoder + view-field normalizer against shared
// helpers, exactly as the client IIFE scopes them.
function buildClientScope() {
  const sinceFromWireSrc = extractIifeFunction(clientSrc, 'sinceFromWire');
  const normSpecViewFieldsSrc = extractIifeFunction(clientSrc, '_normSpecViewFields');
  const normalizeSpecSortBySrc = extractIifeFunction(clientSrc, 'normalizeSpecSortBy');
  const codecSrc = extractIifeConst(clientSrc, 'SinceCodec');
  const wireScope = new Function(`return ${sinceFromWireSrc};`);
  const sortByScope = new Function(`return ${normalizeSpecSortBySrc};`);
  const codecScope = new Function('sinceFromWire', `${codecSrc};\nreturn SinceCodec;`);
  const normScope = new Function(
    'sinceFromWire', 'normalizeSpecSortBy',
    `return ${normSpecViewFieldsSrc};`
  );
  return {
    SinceCodec: codecScope(wireScope()),
    sinceFromWire: wireScope(),
    _normSpecViewFields: (o, kind, dms) =>
      normScope(wireScope(), sortByScope())(o, kind, dms),
  };
}

let S;
try {
  S = buildClientScope();
} catch (e) {
  console.error('EXTRACTION FAILURE (a rename or signature change?):', e.message);
  process.exit(1);
}

// --- 1. token grammar: last/newest = the N newest; bottom = positional ------
{
  const last = S.SinceCodec.fromTokenResult('last 3');
  ok(!last.error && last.spec && last.spec.form === 'item_count' && last.spec.count === 3,
     'fromTokenResult("last 3") parses');
  eqJson(last.spec && last.spec.selection, 'newest', '"last 3" selects newest');
  const newest = S.SinceCodec.fromTokenResult('newest 12 items');
  ok(!newest.error && newest.spec && newest.spec.count === 12 &&
     newest.spec.selection === 'newest', '"newest 12 items" parses (the drift is fixed)');
  const bottom = S.SinceCodec.fromTokenResult('bottom 4');
  ok(!bottom.error && bottom.spec && bottom.spec.count === 4 &&
     bottom.spec.selection === 'bottom', '"bottom 4" parses as the positional tail');
  const bad = S.SinceCodec.fromTokenResult('bottom banana');
  ok(!!bad.error && /item-count/.test(bad.error), '"bottom banana" is a loud item-count error');
  ok(S.SinceCodec.fromTokenResult('bottomline 3').spec === null ||
     S.SinceCodec.fromTokenResult('bottomline 3').error !== null,
     'a bottom-prefixed non-token is not silently accepted');
}

// --- 2. toToken: the display spelling round-trips ---------------------------
{
  eqJson(S.SinceCodec.toToken({ form: 'item_count', count: 5, selection: 'newest' }),
         'last 5', 'toToken(newest) stays the "last N" display spelling');
  eqJson(S.SinceCodec.toToken({ form: 'item_count', count: 5, selection: 'bottom' }),
         'bottom 5', 'toToken(bottom) spells the positional tail "bottom N"');
}

// --- 3. wire decode: old blobs (no selection) stay POSITIONAL ---------------
{
  const old = S.sinceFromWire({ form: 'item_count', count: 3, axis: 'created', direction: 'asc' });
  ok(old && old.selection === 'bottom',
     'an old item_count blob without selection decodes POSITIONAL (back-compat)');
  const newest = S.sinceFromWire({ form: 'item_count', count: 3, selection: 'newest' });
  ok(newest && newest.selection === 'newest', 'an explicit newest selection decodes');
  const junk = S.sinceFromWire({ form: 'item_count', count: 3, selection: 'banana' });
  ok(junk && junk.selection === 'bottom', 'an unknown selection degrades to bottom');
  // Round-trip through the token codec.
  eqJson(S.SinceCodec.toToken(S.sinceFromWire({ form: 'item_count', count: 7, selection: 'bottom' })),
         'bottom 7', 'wire -> token round-trip keeps bottom');
}

// --- 4. the recency basis rides the ONE spec fan-in -------------------------
{
  const v = S._normSpecViewFields(
    { since: { form: 'item_count', count: 2, selection: 'newest' }, recent_by: 'modified' },
    'raw_root', 'name'
  );
  eqJson(v.recent_by, 'modified', '_normSpecViewFields carries recent_by');
  const v2 = S._normSpecViewFields({ recent_by: 'banana' }, 'raw_root', 'name');
  eqJson(v2.recent_by, null, 'an unknown recent_by degrades to null (resolve-at-apply)');
  const v3 = S._normSpecViewFields({}, 'raw_root', 'name');
  eqJson(v3.recent_by, null, 'recent_by defaults to null');
  const v4 = S._normSpecViewFields(
    { since: { form: 'item_count', count: 2, selection: 'newest' } }, 'raw_root', 'name');
  ok(v4.since && v4.since.selection === 'newest', 'the spec fan-in keeps the since object verbatim');
}

// --- 5. the LEAF COUNT ribbon spells the positional token -------------------
{
  ok(/bottom \$\{n\}|`bottom ` \+ n|'bottom ' \+ n/.test(
       (clientSrc.match(/setDockedLeafCount\(n\) \{[\s\S]{0,400}/) || [''])[0]),
     'setDockedLeafCount paints "bottom N" (the positional ribbon)');
  ok(clientSrc.includes('since="bottom N"'),
     'the ribbon tooltip teaches since="bottom N"');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
