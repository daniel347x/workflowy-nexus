// ============================================================================
// test_dock_source_view.js — the repo's FIRST JavaScript test harness.
//
// Covers the docked-Markdown-modal "Source view + find-in-source" feature
// (glimpse-md-render.js): compileSourceSearch (the highlight-only JS
// mini-twin of the server's build_search_predicate smart grammar) and
// sourceHtml (the single <pre> paint choke for the dock's text views).
//
// RUN:   node tests/js/test_dock_source_view.js
// EXIT:  0 = all pass, 1 = at least one failure. Zero dependencies — no npm
//        install, no package.json, no framework. Not collected by pytest
//        (pytest only gathers test_*.py), so it can never interfere with the
//        Python suite.
//
// Loads the actual shared API in a Node VM; legacy test names below are local
// aliases only. Production dock adapters are covered by test_md_render_fixture.
// ============================================================================
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'glimpse-md-render.js'), 'utf8'), context);

function makeApi(regex) {
  const state = { regex, count: 0, current: -1 };
  return {
    compileDockSourceSearch: context.GlimpseMd.compileSourceSearch,
    dockedSourceHtml: t => context.GlimpseMd.sourceHtml(t, {
      findRegex: regex,
      onSearchState: s => Object.assign(state, s),
    }),
    _state: state,
  };
}

let failures = 0;
function check(label, cond) {
  if (cond) { console.log('ok   ' + label); }
  else { failures++; console.log('FAIL ' + label); }
}
function testRx(rx, s) {
  rx.lastIndex = 0; // /g regexes are stateful across .test() calls
  return rx.test(s);
}

const api = makeApi(null);

// ------------------------- compileDockSourceSearch -------------------------
let r = api.compileDockSourceSearch('');
check('blank query -> ok, regex null', r.ok && r.regex === null);

r = api.compileDockSourceSearch('   ');
check('whitespace-only query -> regex null', r.ok && r.regex === null);

r = api.compileDockSourceSearch('invoice OR receipt -draft');
check('smart query compiles', r.ok && r.regex !== null);
check('  matches invoice (case-insensitive)', testRx(r.regex, 'an INVOICE here'));
check('  matches receipt', testRx(r.regex, 'a Receipt here'));
check('  negated term is not a highlight term', !testRx(r.regex, 'draft only'));

r = api.compileDockSourceSearch('"27 inch monitor"');
check('quoted phrase compiles', r.ok
  && testRx(r.regex, 'a 27 inch monitor sale')
  && !testRx(r.regex, '27 monitor inch'));

r = api.compileDockSourceSearch('inv*2026');
check('glob * compiles', r.ok
  && testRx(r.regex, 'see inv-99-2026 now')
  && !testRx(r.regex, 'inv 2027'));

r = api.compileDockSourceSearch('monit?r');
check('glob ? compiles', r.ok
  && testRx(r.regex, 'a monitor')
  && !testRx(r.regex, 'a monitr'));

r = api.compileDockSourceSearch('"inv*2026"');
check('glob inside quotes still globs (server parity)', r.ok && testRx(r.regex, 'inv-x-2026'));

r = api.compileDockSourceSearch('invoice OR "big receipt" OR mon*');
check('OR mix of term/phrase/glob compiles', r.ok
  && testRx(r.regex, 'invoice')
  && testRx(r.regex, 'a big receipt')
  && testRx(r.regex, 'monitor'));

r = api.compileDockSourceSearch('"unbalanced');
check('unbalanced quotes -> loud failure', r.ok === false && typeof r.error === 'string');

r = api.compileDockSourceSearch('*');
check('bare * (empty-match pattern) dropped -> regex null', r.ok && r.regex === null);

r = api.compileDockSourceSearch('-draft');
check('all-negated query -> regex null', r.ok && r.regex === null);

r = api.compileDockSourceSearch('OR');
check('keywords-only query -> regex null', r.ok && r.regex === null);

r = api.compileDockSourceSearch('a.c');
check('regex metachars escaped (a.c matches literally, not abc)', r.ok
  && testRx(r.regex, 'a.c') && !testRx(r.regex, 'abc'));

// ----------------------------- dockedSourceHtml ----------------------------
let h = makeApi(api.compileDockSourceSearch('invoice').regex);
let html = h.dockedSourceHtml('invoice and Invoice and <b>nothing</b>');
check('highlight count = 2', h._state.count === 2);
check('first match is --current (index 0)', h._state.current === 0);
check('first mark carries both classes', html.indexOf('class="glimpse-dock-src-hit glimpse-dock-src-hit--current"') >= 0);
check('second mark is plain', html.split('class="glimpse-dock-src-hit"').length === 2);
check('non-match text is HTML-escaped', html.indexOf('&lt;b&gt;') >= 0);
check('pre wrapper', html.startsWith('<pre class="glimpse-md-preview-plain">'));

h = makeApi(api.compileDockSourceSearch('^edge').regex);
html = h.dockedSourceHtml('^edge start and end ^edge');
check('matches at both string boundaries', h._state.count === 2);

h = makeApi(null);
html = h.dockedSourceHtml('plain <x>');
check('no-regex passthrough escapes, no marks', html.indexOf('&lt;x&gt;') >= 0 && html.indexOf('<mark') < 0);
check('no-regex count/current reset', h._state.count === 0 && h._state.current === -1);

h = makeApi(api.compileDockSourceSearch('nomatch').regex);
html = h.dockedSourceHtml('nothing here');
check('zero matches -> count 0, current -1, no marks', h._state.count === 0
  && h._state.current === -1 && html.indexOf('<mark') < 0);

console.log(failures === 0 ? 'ALL PASS' : `${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);
