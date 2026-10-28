'use strict';

// FILE-NOTE HEADER FRESHNESS (Dan 2026) — extension-side parity harness.
//
// The Python fix refreshes a FILE node's LINE COUNT on every refresh while
// keeping the exact `Path:`-first + `LINE COUNT: ` + (dropped Source-SHA1)
// header grammar. The extension's F12 routing classifies the SAME note, so
// this harness feeds REPRESENTATIVE REFRESHED notes (counts 0 / 1 / 5, with
// a preserved body suffix, and with the legacy SHA gone) to the REAL client
// functions — extracted from the actual source, the established tests/js
// pattern — and pins the classification verdicts:
//
//   getCartographerHeaderInfo(noteText)   — first-line Path/Root + count scan
//   pathHeaderLooksLikeFile(header, name) — extension fallback heuristic
//   classifyF12Target(name, note)         — file / folder / node / unsupported
//   noteHasDummyPathBlock(noteText)       — saved-regime dummy block detection
//
// Pinned: a refreshed FILE (any count, including 0) still classifies 'file';
// an extension-less name is still 'file' because LINE COUNT is present;
// folder and saved-dummy verdicts are unchanged; a count-less lookalike body
// line never promotes a node to 'file'.
//
// Run: node tests/js/test_carto_file_header_freshness.js   (exit 0 = pass)

const fs = require('fs');
const path = require('path');

const CLIENT_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'glimpse-cache-client.js');
const src = fs.readFileSync(CLIENT_FILE, 'utf8');

function extractFunction(name) {
  // Line-start anchored so beacon `role=function name(...)` comment lines are
  // never mistaken for the definition; keeps any `async` keyword.
  const re = new RegExp(`^[ \\t]*(?:async[ \\t]+)?function[ \\t]+${name}[ \\t]*\\(`, 'm');
  const m = re.exec(src);
  if (!m) throw new Error(`function not found in client file: ${name}`);
  const idx = m.index;
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

// Evaluate the four real functions together (classifyF12Target calls the
// other three).
const {
  getCartographerHeaderInfo,
  pathHeaderLooksLikeFile,
  classifyF12Target,
  noteHasDummyPathBlock,
} = new Function(
  `${extractFunction('getCartographerHeaderInfo')}
   ${extractFunction('pathHeaderLooksLikeFile')}
   ${extractFunction('classifyF12Target')}
   ${extractFunction('noteHasDummyPathBlock')}
   return { getCartographerHeaderInfo, pathHeaderLooksLikeFile, classifyF12Target, noteHasDummyPathBlock };`
)();

let failures = 0;
function check(label, condition) {
  if (condition) console.log('ok   ' + label);
  else { failures++; console.log('FAIL ' + label); }
}

// ---------------------------------------------------------------------------
// A. representative REFRESHED notes — every count, suffix preserved
// ---------------------------------------------------------------------------

function refreshedNote(count, suffix) {
  // The post-fix header shape produced by _merge_file_note_header: Path:
  // first, exactly one LINE COUNT line, Source-SHA1 dropped from the owned
  // header, user suffix below preserved verbatim.
  return `Path: x.py\nLINE COUNT: ${count}\n${suffix}`;
}

for (const count of ['0', '1', '5']) {
  const note = refreshedNote(count, '\nUser notes below.\n');
  const info = getCartographerHeaderInfo(note);
  check(`count ${count}: header parsed, first-line Path, hasLineCount`,
    info.firstHeader === 'Path: x.py' && info.hasLineCount === true);
  check(`count ${count}: classifyF12Target → file`,
    classifyF12Target('🐍 x.py', note) === 'file');
}

// ---------------------------------------------------------------------------
// B. LINE COUNT is load-bearing for an extension-less name
// ---------------------------------------------------------------------------

const extCounted = 'Path: LICENSE\nLINE COUNT: 5';
const extBare = 'Path: LICENSE';
check('extension-less + count → file (count is load-bearing)',
  classifyF12Target('LICENSE', extCounted) === 'file');
check('extension-less WITHOUT count → folder (unchanged fallback)',
  classifyF12Target('LICENSE', extBare) === 'folder');
check('pathHeaderLooksLikeFile: counted extension-less is not the extension path',
  pathHeaderLooksLikeFile('Path: LICENSE', 'LICENSE') === false);

// ---------------------------------------------------------------------------
// C. folder and saved-dummy verdicts unchanged on refreshed-shape notes
// ---------------------------------------------------------------------------

check('Root-first note → folder (never touched by the fix)',
  classifyF12Target('🗺️ repo', 'Root: /repo\n') === 'folder');
check('folder Path without count or extension → folder',
  classifyF12Target('📂 src', 'Path: src') === 'folder');

const dummyNote = 'MD_PATH:\nPath: /abs/subset.md\n---\nbody\n';
check('saved dummy block still detected',
  noteHasDummyPathBlock(dummyNote) === true);
check('saved dummy block still classifies file',
  classifyF12Target('subset', dummyNote) === 'file');

// ---------------------------------------------------------------------------
// D. a body line that merely LOOKS like metadata never promotes or demotes
// ---------------------------------------------------------------------------

const lookalike = 'Path: x.py\nLINE COUNT: 2\n\nprose quoting LINE COUNT: 999\n';
check('refreshed note with lookalike body line → file',
  classifyF12Target('🐍 x.py', lookalike) === 'file');
const proseOnly = 'just a note\nLINE COUNT: 3 quoted in prose\n';
check('prose-only note stays unsupported (strict first-line header)',
  classifyF12Target('some node', proseOnly) === 'unsupported');
check('count scan finds the line-start lookalike but no header means no file',
  (() => { const i = getCartographerHeaderInfo(proseOnly); return i.firstHeader === '' && i.hasLineCount === true; })());

// ---------------------------------------------------------------------------

if (failures > 0) {
  console.log(`\n${failures} check(s) FAILED`);
  process.exit(1);
}
console.log('\nall checks passed');
process.exit(0);
