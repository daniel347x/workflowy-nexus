'use strict';

// [ap:EPR86W] part a — the JS twin of the traversal pair's depth leg.
//
// Unit 4a adds `recursive` (default true; false = the scope root + its DIRECT
// children only, raw-root-only today) to the client's spec vocabulary so a
// pushed depth-capped dock render survives the client round-trip: the push
// receiver reconstructs the server's blob through normalizeRenderScopeToSpec
// (which whitelists fields — an unknown field would be DROPPED here, so the
// JS twin is load-bearing, not optional), and adoptRenderScope rebuilds the
// stored spec from the render_scope echo the same way, so Refresh / per-row
// re-projection reuse the SAME traversal.
//
// Every check here fails against the pre-fix client (no recursive key
// anywhere): defaults-normalize drops the echo's key, and the norm function
// never emits it.

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

// _normSpecViewFields: the real function with its two module dependencies
// stubbed to the shapes it needs (normalizeSpecSortBy + sinceFromWire).
{
  const normalizeSpecSortBy = (raw) => (raw == null ? 'none'
    : (['none', 'created', 'modified'].includes(String(raw)) ? String(raw) : 'none'));
  const sinceFromWire = (v) => (v == null ? null : v);
  const factory = new Function('normalizeSpecSortBy', 'sinceFromWire',
    `${extractFunction('_normSpecViewFields')}; return _normSpecViewFields;`);
  const normView = factory(normalizeSpecSortBy, sinceFromWire);

  const base = normView({ match_scope: 'name_and_note', descend_policy: 'all',
    sort_by: 'none', sort_direction: 'asc', since: null, headings_only: false },
  'raw_root', 'name');
  check('recursive defaults true when the echo omits it', base.recursive === true);
  const capped = normView({ recursive: false }, 'raw_root', 'name');
  check('recursive=false survives normalization', capped.recursive === false);
  const lenient = normView({ recursive: 'junk' }, 'raw_root', 'name');
  check('recursive normalizes leniently to true off a non-false value',
    lenient.recursive === true);
}

// normalizeRenderScopeToSpec: the REAL _normSpecViewFields injected, with a
// defaults object shaped like the client's (recursive present, default true).
{
  const defaults = {
    kind: 'raw_root', root_uuid: null, group_id: null, group_name: null,
    lightning: null, match_scope: 'name', descend_policy: 'all',
    recursive: true,
    sort_by: 'none', sort_direction: 'asc', since: null, recent_by: null,
    headings_only: false, as_markdown: true,
    content_format: 'markdown', tree_mode: 'render_tree',
  };
  const normalizeSpecSortBy = function normalizeSpecSortBy(rawSortBy, kind) {
    const k = String(kind || '').trim().toLowerCase();
    if (rawSortBy == null) {
      return (k === 'slice' || k === 'slice_override') ? null : 'none';
    }
    const sb = String(rawSortBy).trim().toLowerCase();
    return (sb === 'none' || sb === 'created' || sb === 'modified') ? sb : 'none';
  };
  const sinceFromWire = (v) => (v == null ? null : v);
  const normViewFactory = new Function('normalizeSpecSortBy', 'sinceFromWire',
    `${extractFunction('_normSpecViewFields')}; return _normSpecViewFields;`);
  const normView = normViewFactory(normalizeSpecSortBy, sinceFromWire);
  const factory = new Function('RENDER_SPEC_DEFAULTS', '_normSpecViewFields',
    `${extractFunction('normalizeRenderScopeToSpec')}; return normalizeRenderScopeToSpec;`);
  const normalize = factory(defaults, normView);

  // A server echo of a depth-capped raw-root render (the render_scope echo
  // carries recursive beside descend_policy) must round-trip the cap.
  const capped = normalize({
    kind: 'raw_root', root_uuid: 'root', recursive: false,
    descend_policy: 'stop_at_child_anchors', sort_by: 'none',
  });
  check('echo round-trip preserves recursive=false', capped.recursive === false);
  check('echo round-trip preserves descend_policy',
    capped.descend_policy === 'stop_at_child_anchors');
  // An older echo without the key adopts the unbounded default.
  const legacy = normalize({ kind: 'raw_root', root_uuid: 'root' });
  check('older echo without recursive adopts true', legacy.recursive === true);
}

// The push receiver forwards the cap from the reconstructed pushSpec into
// applySliceGroupIntoDock (scalar opts), and applySliceGroupIntoDock folds it
// into the _applySpec it renders with.
{
  const receiver = src.includes('recursive: (pushSpec.recursive === false) ? false : true');
  check('push receiver forwards pushSpec.recursive', receiver);
  const apply = extractFunction('applySliceGroupIntoDock');
  check('applySliceGroupIntoDock reads o.recursive', apply.includes('o.recursive === false'));
  check('applySliceGroupIntoDock puts recursive on the _applySpec',
    /_applySpec\s*=\s*\{[\s\S]*?recursive:\s*recursive/.test(apply));
}

// The RENDER_SPEC_DEFAULTS const carries the unbounded default.
{
  const m = /const RENDER_SPEC_DEFAULTS = Object\.freeze\(\{[\s\S]*?\n  \}\);/.exec(src);
  check('RENDER_SPEC_DEFAULTS found', !!m);
  check('RENDER_SPEC_DEFAULTS.recursive defaults true', !!m && /recursive:\s*true/.test(m[0]));
}

console.log(failures === 0 ? 'ALL PASS' : `${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);
