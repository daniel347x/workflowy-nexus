'use strict';

const fs = require('fs');
const path = require('path');

const CLIENT_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'glimpse-cache-client.js');
const src = fs.readFileSync(CLIENT_FILE, 'utf8');

function extractFunction(name) {
  const rx = new RegExp(`(^|\\n)\\s*(?:async\\s+)?function ${name}\\(`);
  const match = rx.exec(src);
  const idx = match ? match.index + match[1].length : -1;
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
  throw new Error(`unbalanced function: ${name}`);
}

let failures = 0;
function check(label, condition) {
  if (condition) console.log('ok   ' + label);
  else { failures++; console.log('FAIL ' + label); }
}

// Opaque selection packing: the nested LensSpec and nullable query must survive
// byte-for-byte; non-lens modes must not acquire a lens field.
{
  const factory = new Function(`${extractFunction('buildLightningFrame')}; return buildLightningFrame;`);
  const pack = factory();
  const lens = {
    kind: 'merge', operation: 'union', inputs: [
      { kind: 'group', group_handle: 'grp-a', root_handle: null },
      { kind: 'subtree', root_handle: '[ap:ROOT01]', descend: 'all' },
    ],
  };
  const srcSelection = {
    mode: 'render_lens', lens, query: null, query_mode: 'smart',
    match_scope: 'name_and_note',
  };
  const packed = pack(srcSelection);
  check('render_lens pack preserves LensSpec object', packed.lens === lens);
  check('render_lens pack preserves nullable query', Object.prototype.hasOwnProperty.call(packed, 'query') && packed.query === null);
  check('render_lens pack preserves query_mode', packed.query_mode === 'smart');
  check('render_lens pack preserves match_scope', packed.match_scope === 'name_and_note');
  check('other modes omit lens', !Object.prototype.hasOwnProperty.call(pack({ mode: 'render_search', query: 'x' }), 'lens'));
}

// Server-adopted render_scope -> stored spec. The opaque selection must be the
// exact same object; JavaScript is a carrier, not a LensSpec interpreter.
{
  const defaults = {
    kind: 'raw_root', root_uuid: null, group_id: null, group_name: null,
    lightning: null, match_scope: 'name_and_note', descend_policy: 'all',
    sort_by: 'none', sort_direction: 'asc', since: null,
    headings_only: false, as_markdown: true,
    content_format: 'markdown', tree_mode: 'render_tree',
  };
  const normView = (s, kind) => ({
    match_scope: s.match_scope || 'name_and_note',
    descend_policy: s.descend_policy || 'all',
    sort_by: s.sort_by || 'none', sort_direction: s.sort_direction || 'asc',
    since: s.since == null ? null : s.since,
    headings_only: !!s.headings_only,
  });
  const factory = new Function('RENDER_SPEC_DEFAULTS', '_normSpecViewFields',
    `${extractFunction('normalizeRenderScopeToSpec')}; return normalizeRenderScopeToSpec;`);
  const normalize = factory(defaults, normView);
  const selection = {
    mode: 'render_lens', lens: { kind: 'group', group_handle: 'grp-a' },
    query: null, query_mode: 'smart', match_scope: 'name_and_note',
  };
  const spec = normalize({
    kind: 'lightning_rod', root_uuid: 'root', selection,
    sort_by: 'none', sort_direction: 'asc', since: null,
    headings_only: false, content_format: 'markdown', tree_mode: 'render_tree',
  });
  check('normalizeRenderScopeToSpec preserves adopted selection verbatim', spec.lightning === selection);
  check('lightning null sort normalizes to explicit none', spec.sort_by === 'none');
}

// Generated cache timeout: interactive floor, monotonic row growth, and 180s cap.
{
  const factory = new Function('INTERACTIVE_WS_REPLY_TIMEOUT_MS', 'CACHE_RENDER_LONG_TIMEOUT_MS',
    `${extractFunction('computeCacheRenderTimeoutMs')}; return computeCacheRenderTimeoutMs;`);
  const timeout = factory(30000, 180000);
  check('row timeout respects interactive floor', timeout(0) === 30000);
  check('row timeout grows with row count', timeout(50) > timeout(1));
  check('row timeout respects long-render cap', timeout(100000) === 180000);
}

// Transport death still wins immediately over any generated timer.
{
  const pending = new Map();
  let settled = false;
  const timer = setTimeout(() => {}, 180000);
  pending.set('x', { timer, resolve: (v) => { settled = (v === null); } });
  const factory = new Function('pendingSliceModalRenders', 'GLIMPSE_VERSION',
    `${extractFunction('failAllPendingSliceRenders')}; return failAllPendingSliceRenders;`);
  factory(pending, 'test')('test-close');
  check('socket-close cleanup settles pending row immediately', settled && pending.size === 0);
}

// The push receiver remains generic: any lightning selection satisfies scope.
check('push receiver scope gate accepts generic lightning',
  src.includes('if (!gid && !rid2 && !lightning2)'));

// Lens Refresh always selects the long-render ceiling, independent of prior rows.
check('render_lens Refresh uses long-render ceiling',
  src.includes("String(_refreshSpec.lightning.mode || '') === 'render_lens'")
  && src.includes('requestSliceModalRender(_refreshSpec, _refreshOpts)'));

// Regression: the obsolete literal row timeout is gone from the row request.
const rowFn = extractFunction('requestSliceRowRender');
check('requestSliceRowRender has no stale 12000ms literal', !rowFn.includes('12000'));
check('requestSliceRowRender uses generated timeout', rowFn.includes('computeCacheRenderTimeoutMs'));

console.log(failures === 0 ? 'ALL PASS' : `${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);
