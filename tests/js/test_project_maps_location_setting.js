// ============================================================================
// test_project_maps_location_setting.js — the extension's Project Maps
// location option: a generic pointer to the Workflowy node where the user
// keeps their Project Maps.
//
// Covers:
//   • DEFAULT — DEFAULT_EXTENSION_SETTINGS carries project_maps_location: ''
//     (none configured), beside default_note_sessions_parent.
//   • SANITIZE — mergeExtensionSettings' project_maps_location branch: string
//     values are trimmed and stored verbatim; a non-string (number/array/null
//     through the branch) falls back to ''; other settings untouched.
//   • REPLY — the get_extension_settings_result reply in
//     glimpse-cache-client.js carries project_maps_location AND
//     default_markdown_folder, each a trimmed string or null.
//   • OPTIONS — options.html carries the project-maps-location text input
//     (label, placeholder, help) directly after the Default Note Sessions
//     parent row and its path line, with no Activate button; options.js
//     binds, renders and persists it on change, and Enter blurs.
//
// The behavioral checks extract DEFAULT_EXTENSION_SETTINGS +
// mergeExtensionSettings VERBATIM from hotkeys-shared.js (the repo's
// extraction idiom, tests/js/README.md); the reply/options checks are static
// wiring locks over the real file bytes. Against the pre-M0 tree this harness
// FAILS (the key/branch/locks do not exist) — the structural
// mutation-sensitivity guarantee of the idiom.
//
// RUN:   node tests/js/test_project_maps_location_setting.js
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
// pure; the same extraction the archive-folder harness uses).
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
eq(DEFAULT_EXTENSION_SETTINGS.project_maps_location, '',
  'shipped default project_maps_location is \'\' (no map configured)');
eq(DEFAULT_EXTENSION_SETTINGS.default_note_sessions_parent, '',
  'the default note-sessions parent default is untouched');
eq(DEFAULT_EXTENSION_SETTINGS.archive_folder, '',
  'the archive-folder default is untouched');

console.log('— SANITIZE (the trim discipline) —');
eq(mergeExtensionSettings({}).project_maps_location, '',
  'empty user settings -> the \'\' default');
eq(mergeExtensionSettings({ project_maps_location: '  [ap:PMAPS1]  ' })
  .project_maps_location, '[ap:PMAPS1]',
  'string value trimmed and stored verbatim (AP-ID form)');
eq(mergeExtensionSettings({ project_maps_location: ' 3f3754e2 ' })
  .project_maps_location, '3f3754e2',
  'string value trimmed and stored verbatim (8-hex form)');
eq(mergeExtensionSettings({ project_maps_location: '   ' })
  .project_maps_location, '',
  'whitespace-only -> \'\' (no map configured)');
eq(mergeExtensionSettings({ project_maps_location: 42 })
  .project_maps_location, '',
  'non-string -> fallback \'\'');
eq(mergeExtensionSettings({ project_maps_location: ['x'] })
  .project_maps_location, '',
  'array -> fallback \'\'');
{
  const m = mergeExtensionSettings({ project_maps_location: 'NODE' });
  eq(m.default_note_sessions_parent, '',
    'the project-maps branch does not touch default_note_sessions_parent');
  const m2 = mergeExtensionSettings({ default_note_sessions_parent: 'PARENT' });
  eq(m2.project_maps_location, '',
    'the default-parent branch does not touch project_maps_location');
}

console.log('— REPLY (the daemon-visible settings reply) —');
{
  const extReply = regionBetween(clientSrc,
    "action: 'get_extension_settings_result',", 'ws.send(JSON.stringify(reply));');
  ok(extReply.includes('project_maps_location:'),
    'get_extension_settings_result carries project_maps_location');
  ok(extReply.includes('default_markdown_folder:'),
    'get_extension_settings_result carries default_markdown_folder');
  ok(/project_maps_location:[\s\S]{0,220}\|\| null/.test(extReply),
    'project_maps_location is the trimmed string || null');
  ok(/default_markdown_folder:[\s\S]{0,220}\|\| null/.test(extReply),
    'default_markdown_folder is the trimmed string || null');
  ok(extReply.includes("typeof extensionSettings.project_maps_location === 'string'"),
    'project_maps_location trims a string and falls back before || null');
  ok(extReply.includes("typeof extensionSettings.default_markdown_folder === 'string'"),
    'default_markdown_folder trims a string and falls back before || null');
}

console.log('— OPTIONS —');
{
  const noteSessionsCard = regionBetween(optionsHtmlSrc,
    'id="note-sessions"', '</section>');
  const parentRow = noteSessionsCard.indexOf('id="default-note-sessions-parent"');
  const parentPath = noteSessionsCard.indexOf('id="default-note-sessions-parent-path"');
  const mapsRow = noteSessionsCard.indexOf('id="project-maps-location"');
  ok(parentRow >= 0 && parentPath > parentRow && mapsRow > parentPath,
    'options.html places the field after the Default Note Sessions parent row and path');
}
ok(optionsHtmlSrc.includes('id="project-maps-location"'),
  'options.html carries the project-maps-location field');
ok(optionsHtmlSrc.includes('>Project Maps location</label>'),
  'options.html labels the field Project Maps location');
ok(optionsHtmlSrc.includes('type="text" spellcheck="false"')
  && optionsHtmlSrc.includes('placeholder="a Workflowy node: UUID, 8-hex or [ap:…]"'),
  'options.html is a text field with the pointer placeholder');
ok(optionsHtmlSrc.includes('Where you keep your Project Maps'),
  'options.html help says where the maps are kept');
ok(optionsHtmlSrc.includes('It is a pointer'),
  'options.html help says it is a pointer and nothing more');
ok(optionsHtmlSrc.includes('NEXUS never interprets, validates or'),
  'options.html help says NEXUS never interprets or validates the contents');
ok(optionsHtmlSrc.includes('Leave blank for none.'),
  'options.html help says blank means none');
ok(!optionsHtmlSrc.includes('project-maps-location-activate'),
  'options.html gives the field no Activate button');
ok(optionsJsSrc.includes("getElementById('project-maps-location')"),
  'options.js binds the field');
ok(optionsJsSrc.includes('projectMapsLocationInput.value = currentSettings.project_maps_location'),
  'options.js renders the stored value');
ok(optionsJsSrc.includes('persistSettings({ project_maps_location: projectMapsLocationInput.value })'),
  'options.js persists project_maps_location on change');
ok(optionsJsSrc.includes('projectMapsLocationInput.blur()'),
  'options.js blurs the field on Enter');

console.log('');
console.log(`${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
