'use strict';

const fs = require('fs');
const path = require('path');

const CLIENT_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'glimpse-cache-client.js');
const src = fs.readFileSync(CLIENT_FILE, 'utf8');

function extractFunction(name) {
  const idx = src.indexOf(`function ${name}(`);
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

const formatter = new Function(
  `${extractFunction('formatCartoHeartbeatDiagnostic')}
   return formatCartoHeartbeatDiagnostic;`
)();

let failures = 0;
function check(label, condition) {
  if (condition) console.log('ok   ' + label);
  else { failures++; console.log('FAIL ' + label); }
}

check('old payload without heartbeat preserves prior row text', formatter({status: 'running'}) === '');
check('null and malformed jobs are harmless', formatter(null) === '' && formatter({heartbeat: 1}) === '');
check('in-process/non-applicable heartbeat stays hidden',
  formatter({heartbeat: {applicable: false, state: 'missing'}}) === '');
check('fresh heartbeat is compact',
  formatter({heartbeat: {applicable: true, state: 'fresh', age_seconds: 1.24}})
    === 'heartbeat 1.2s ago');
check('negative age clamps to zero',
  formatter({heartbeat: {applicable: true, state: 'fresh', age_seconds: -4}})
    === 'heartbeat <0.1s ago');
check('stale heartbeat is neutral diagnostic text, never failed', (() => {
  const text = formatter({heartbeat: {applicable: true, state: 'stale', age_seconds: 18.2}});
  return text === 'heartbeat stale (18s)' && !text.includes('failed');
})());
check('missing heartbeat is unavailable, not failed',
  formatter({heartbeat: {applicable: true, state: 'missing'}}) === 'heartbeat unavailable');
check('identity mismatch is explicit but neutral',
  formatter({heartbeat: {applicable: true, state: 'invalid', identity_mismatch: true}})
    === 'heartbeat identity mismatch');
check('invalid heartbeat is compact and safe',
  formatter({heartbeat: {applicable: true, state: 'invalid', error: 'bad json'}})
    === 'heartbeat invalid');
check('unknown future heartbeat state is ignored',
  formatter({heartbeat: {applicable: true, state: 'future'}}) === '');

const renderer = extractFunction('handleCartoJobsResult');
check('renderer keeps authoritative status rendering', renderer.includes("const status = String(job.status || 'unknown')"));
check('renderer appends heartbeat without replacing status/progress',
  renderer.includes('formatCartoHeartbeatDiagnostic(job)')
  && renderer.includes('progressText')
  && renderer.includes('statusBadge'));
check('cancelling behavior remains status-driven', renderer.includes("canCancelStatus !== 'cancelled'"));

console.log(failures === 0 ? 'ALL PASS' : `${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);
