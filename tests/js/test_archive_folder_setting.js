// ============================================================================
// test_archive_folder_setting.js — Archive Reads U3: the extension's
// Archive-folder option (§U3.2 deliverable 6, §5.12).
//
// Covers:
//   • DEFAULT — DEFAULT_EXTENSION_SETTINGS carries archive_folder: ''
//     (archive reads off; the shipped default), beside default_markdown_folder.
//   • SANITIZE — mergeExtensionSettings' archive branch: string values are
//     trimmed and stored verbatim; a non-string (number/undefined/null
//     through the branch) falls back to ''; other settings untouched.
//   • REPLY — the get_extension_settings_result reply in
//     glimpse-cache-client.js carries archive_folder (trimmed; non-string
//     falls back to ''), so the daemon can remember the value.
//   • OPTIONS — options.html carries the archive-folder text input with the
//     §5.12 label/help, and options.js binds, renders and persists it.
//
// The behavioral checks extract DEFAULT_EXTENSION_SETTINGS +
// mergeExtensionSettings VERBATIM from hotkeys-shared.js (the repo's
// extraction idiom, tests/js/README.md); the reply/options checks are static
// wiring locks over the real file bytes. Against the pre-U3 tree this
// harness FAILS (the key/branch/locks do not exist) — the structural
// mutation-sensitivity guarantee of the idiom.
//
// RUN:   node tests/js/test_archive_folder_setting.js
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

const sharedSrc = fs.readFileSync(SHARED_FILE, 'utf8');
const clientSrc = fs.readFileSync(CLIENT_FILE, 'utf8');
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
// pure; the same extraction the favorite-postfix harness uses).
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
function regionBetween(src, startAnchor, endAnchor) {
  const a = src.indexOf(startAnchor);
  if (a < 0) throw new Error('region start not found: ' + startAnchor);
  const b = src.indexOf(endAnchor, a + startAnchor.length);
  if (b < 0) throw new Error('region end not found after start: ' + endAnchor);
  return src.slice(a, b);
}

console.log('— DEFAULT —');
eq(DEFAULT_EXTENSION_SETTINGS.archive_folder, '',
  'shipped default archive_folder is \'\' (archive reads off)');
eq(DEFAULT_EXTENSION_SETTINGS.default_markdown_folder, '',
  'the markdown-folder default is untouched');

console.log('— SANITIZE (the trim discipline) —');
eq(mergeExtensionSettings({}).archive_folder, '',
  'empty user settings -> the \'\' default');
eq(mergeExtensionSettings({ archive_folder: '  C:\\archives\\wfy  ' }).archive_folder,
  'C:\\archives\\wfy',
  'string value trimmed and stored verbatim');
eq(mergeExtensionSettings({ archive_folder: '   ' }).archive_folder, '',
  'whitespace-only -> \'\' (archive reads off)');
eq(mergeExtensionSettings({ archive_folder: 42 }).archive_folder, '',
  'non-string -> fallback \'\'');
eq(mergeExtensionSettings({ archive_folder: ['x'] }).archive_folder, '',
  'array -> fallback \'\'');
{
  const m = mergeExtensionSettings({ archive_folder: 'C:\\a' });
  eq(m.default_markdown_folder, '',
    'the archive branch does not touch default_markdown_folder');
  const m2 = mergeExtensionSettings({ default_markdown_folder: 'C:\\md' });
  eq(m2.archive_folder, '',
    'the markdown branch does not touch archive_folder');
}

console.log('— REPLY (the daemon-visible settings reply) —');
{
  const extReply = regionBetween(clientSrc,
    "action: 'get_extension_settings_result',", 'ws.send(JSON.stringify(reply));');
  ok(extReply.includes('archive_folder:'),
    'get_extension_settings_result carries archive_folder');
  ok(extReply.includes("typeof extensionSettings.archive_folder === 'string'"),
    'get_extension_settings_result trims a string and falls back to \'\' otherwise');
}

console.log('— OPTIONS —');
ok(optionsHtmlSrc.includes('id="archive-folder"'),
  'options.html carries the archive-folder field');
ok(optionsHtmlSrc.includes('>Archive folder</label>'),
  'options.html labels the field per §5.12');
ok(optionsHtmlSrc.includes('leave empty to turn archive reads off'),
  'options.html carries the §5.12 help line');
ok(optionsJsSrc.includes("getElementById('archive-folder')"),
  'options.js binds the field');
ok(optionsJsSrc.includes('archiveFolderInput.value = currentSettings.archive_folder'),
  'options.js renders the stored value');
ok(optionsJsSrc.includes("persistSettings({ archive_folder: archiveFolderInput.value })"),
  'options.js persists archive_folder on change');

console.log('');
console.log(`${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
