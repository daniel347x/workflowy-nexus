'use strict';

// ============================================================================
// test_carto_cancel_pending.js — CF-38 browser-side cancel feedback.
//
// Spec [ap:1770RA] §6.5 (318–322) and §9.5 (550–552): the extension's
// `carto_cancel_job_result` branch must call the EXISTING shared feedback
// handler after its unchanged error log, and `handleCartoLaunchResult` must
// render the five prescribed frames on the existing dedicated line:
//
//   modern pending   (cancellation_pending === true)   -> pending text
//   legacy pending   (outcome kill_refused_in_process_job) -> pending text
//   cooperative      (outcome cooperative)             -> has stopped
//   already_terminal (outcome already_terminal)        -> has stopped
//   failed           (success !== true)                -> request failed
//
// Every frame must survive the ~1Hz carto_list_jobs poll repaint (which
// destroys child elements via textContent), must use the ONE existing 8000 ms
// fade, must never claim a stop for a pending reply, and must create no job row.
//
// Construction: the repository's extraction idiom (tests/js/README.md) — the
// real function and the real WS branch are read from the shipped client bytes,
// evaluated against a fake DOM (textContent really destroys children, like the
// poll's repaint) and fake timers. A rename/signature change fails loudly at
// extraction.
//
// Probe: CUT2B_NEUTRALIZE_PENDING_FEEDBACK=1 forces the pending condition
// false in the extracted copy only, so pending replies fall to the generic
// acknowledgement branch. The pending-text assertions then FAIL with exit 1 —
// a behavioral failure, never a source-extraction error.
//
// RUN:  node tests/js/test_carto_cancel_pending.js
// EXIT: 0 = all pass, 1 = at least one failure. Zero dependencies.
// ============================================================================

const fs = require('fs');
const path = require('path');

const CLIENT_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'glimpse-cache-client.js');
const src = fs.readFileSync(CLIENT_FILE, 'utf8');
const FEEDBACK_LINE_ID = 'glimpse-carto-launch-feedback';

// --- extraction (verbatim from the REAL file bytes) -------------------------
function braceSlice(source, start) {
  const open = source.indexOf('{', start);
  if (open < 0) throw new Error('no opening brace after offset ' + start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('unbalanced braces at offset ' + start);
}
function extractIifeFunction(source, name) {
  for (const prefix of ['\n  async function ' + name + '(', '\n  function ' + name + '(']) {
    const idx = source.indexOf(prefix);
    if (idx >= 0) return braceSlice(source, idx + 1);
  }
  throw new Error('function not found in client file: ' + name);
}
// Inner body of an anonymous block whose anchor ends with '{'.
function extractBlockBodyAfter(source, anchor) {
  const a = source.indexOf(anchor);
  if (a < 0) throw new Error('anchor not found: ' + anchor);
  const start = a + anchor.length - 1; // the anchor's own '{'
  if (source[start] !== '{') throw new Error('anchor does not end with {: ' + anchor);
  let depth = 0;
  for (let i = start; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') {
      depth--;
      if (depth === 0) return source.slice(start + 1, i);
    }
  }
  throw new Error('unbalanced braces after anchor: ' + anchor);
}
function countOcc(haystack, needle) {
  let n = 0; let i = haystack.indexOf(needle);
  while (i >= 0) { n++; i = haystack.indexOf(needle, i + needle.length); }
  return n;
}

// --- the real handler + the real WS branch ----------------------------------
const RAW_FEEDBACK_FN = extractIifeFunction(src, 'handleCartoLaunchResult');
const FEEDBACK_LINE_FN = extractIifeFunction(src, '_ensureCartoLaunchFeedbackLine');
const WS_ANCHOR = "} else if (request.action === 'carto_cancel_job_result') {";
const WS_BRANCH_BODY = extractBlockBodyAfter(src, WS_ANCHOR);

const NEUTRALIZE_PENDING_FEEDBACK =
  String(process.env.CUT2B_NEUTRALIZE_PENDING_FEEDBACK || '') !== '';

function neutralizedFeedbackSource(source) {
  // CF-38 probe: force the PENDING condition false, so pending replies fall to
  // the generic acknowledgement branch ("Cancellation requested ...") instead
  // of the prescribed pending text. The other four frames stay correct, so the
  // failure is exactly the pending-text assertion (never an extraction error).
  const pattern =
    /message\.cancellation_pending === true\s*\|\|\s*message\.outcome === 'kill_refused_in_process_job'/;
  if (!pattern.test(source)) {
    throw new Error('CF-38 pending condition not found in extracted handler');
  }
  return source.replace(pattern, 'false');
}

const FEEDBACK_FN = NEUTRALIZE_PENDING_FEEDBACK
  ? neutralizedFeedbackSource(RAW_FEEDBACK_FN)
  : RAW_FEEDBACK_FN;

// --- tiny assert harness (the tests/js idiom) -------------------------------
let passed = 0;
let failed = 0;
function check(label, condition) {
  if (condition) { passed++; console.log('  ok  ' + label); }
  else { failed++; console.log('FAIL  ' + label); }
}

if (NEUTRALIZE_PENDING_FEEDBACK) {
  console.log('PROBE CUT2B_NEUTRALIZE_PENDING_FEEDBACK active: the pending condition ' +
    'is forced false, so pending replies fall to the generic acknowledgement ' +
    'branch (the pending-text assertions below must fail)');
}

// --- fake DOM (textContent destroys children, like the poll repaint) --------
function makeElement(tag) {
  const el = {
    tagName: String(tag || 'div'),
    id: '',
    style: {},
    children: [],
    parentNode: null,
    _textContent: '',
    get textContent() { return this._textContent; },
    set textContent(value) {
      this._textContent = String(value);
      for (const child of this.children.slice()) child.parentNode = null;
      this.children.length = 0;
    },
    appendChild(child) {
      child.parentNode = el;
      el.children.push(child);
      return child;
    },
    insertBefore(child, reference) {
      const index = reference ? el.children.indexOf(reference) : -1;
      child.parentNode = el;
      if (index < 0) el.children.push(child);
      else el.children.splice(index, 0, child);
      return child;
    },
    get firstChild() { return el.children[0] || null; },
  };
  return el;
}
const fakeDocument = { createElement: (tag) => makeElement(tag) };

// --- fake timers (the ONE existing 8000 ms fade) ----------------------------
const timers = new Map();
let timerSequence = 0;
function fakeSetTimeout(callback, delayMs) {
  const id = ++timerSequence;
  timers.set(id, { callbacks: [callback], delayMs });
  return id;
}
function fakeClearTimeout(id) { timers.delete(id); }
function runFadeTimers() {
  const pending = Array.from(timers.entries());
  timers.clear();
  for (const [, timer] of pending) timer.callbacks.forEach((callback) => callback());
}

// --- build the real handler with its closure state --------------------------
function buildFeedback(handlerSource) {
  const factory = new Function(
    'document', 'setTimeout', 'clearTimeout',
    `let cartoJobsEl = null;
     let cartoLaunchFeedbackEl = null;
     let _cartoLaunchFeedbackText = '';
     ${FEEDBACK_LINE_FN}
     ${handlerSource}
     return {
       attach(el) { cartoJobsEl = el; },
       handle: handleCartoLaunchResult,
       ensure: _ensureCartoLaunchFeedbackLine,
       state() { return { text: _cartoLaunchFeedbackText, line: cartoLaunchFeedbackEl }; },
     };`
  );
  return factory(fakeDocument, fakeSetTimeout, fakeClearTimeout);
}

// ============================================================================
// A. The five prescribed frames on the real feedback handler
// ============================================================================
const feedback = buildFeedback(FEEDBACK_FN);
const jobsEl = makeElement('div');
feedback.attach(jobsEl);

const FRAMES = [
  {
    label: 'modern pending',
    pending: true,
    message: {
      action: 'carto_cancel_job_result',
      success: true,
      job_id: 'job-modern',
      outcome: 'kill_refused_in_process_job',
      status: 'cancelling',
      cancellation_requested: true,
      cancellation_pending: true,
    },
    expected: 'Cancellation pending for job job-modern; waiting for a safe stop. Poll job.status.',
  },
  {
    label: 'legacy pending',
    pending: true,
    message: {
      action: 'carto_cancel_job_result',
      success: true,
      job_id: 'job-legacy',
      outcome: 'kill_refused_in_process_job',
      status: 'cancelling',
      cancellation_requested: true,
    },
    expected: 'Cancellation pending for job job-legacy; waiting for a safe stop. Poll job.status.',
  },
  {
    label: 'cooperative',
    message: {
      action: 'carto_cancel_job_result',
      success: true,
      job_id: 'job-coop',
      outcome: 'cooperative',
    },
    expected: 'Job job-coop has stopped.',
  },
  {
    label: 'already_terminal',
    message: {
      action: 'carto_cancel_job_result',
      success: true,
      job_id: 'job-terminal',
      outcome: 'already_terminal',
    },
    expected: 'Job job-terminal has stopped.',
  },
  {
    label: 'failed',
    message: {
      action: 'carto_cancel_job_result',
      success: false,
      job_id: 'job-failed',
      error: 'Job already completed.',
    },
    expected: 'Cancellation request failed: Job already completed.',
  },
];

function feedbackLineText() {
  const line = feedback.state().line;
  return line ? line.textContent : null;
}

for (const frame of FRAMES) {
  feedback.handle(frame.message);
  check(frame.label + ': exact feedback text',
    feedback.state().text === frame.expected);
  check(frame.label + ': line shows the exact text',
    feedbackLineText() === frame.expected);
  if (frame.pending) {
    check(frame.label + ': never claims the job has stopped',
      !feedback.state().text.includes('has stopped'));
  }
  check(frame.label + ': exactly one child (the dedicated line, no job row)',
    jobsEl.children.length === 1
    && jobsEl.children[0].id === FEEDBACK_LINE_ID
    && jobsEl.children[0] === feedback.state().line);

  // The ~1Hz poll repaint writes cartoJobsEl.textContent, which destroys the
  // child elements; the real helper must re-insert the line from closure text.
  const originalLine = feedback.state().line;
  jobsEl.textContent = 'No active CARTO jobs.';
  check(frame.label + ': poll repaint really destroys the child line',
    originalLine.parentNode === null && jobsEl.children.length === 0);
  feedback.ensure();
  const restoredLine = feedback.state().line;
  check(frame.label + ': repaint re-inserts the dedicated feedback line',
    restoredLine !== originalLine
    && restoredLine.parentNode === jobsEl
    && restoredLine.id === FEEDBACK_LINE_ID
    && restoredLine.textContent === frame.expected
    && restoredLine.style.display === 'block');
  check(frame.label + ': repaint creates no job row',
    jobsEl.children.length === 1 && jobsEl.children[0] === restoredLine);

  check(frame.label + ': uses the ONE existing 8000 ms fade timer',
    timers.size === 1
    && Array.from(timers.values())[0].delayMs === 8000);
  runFadeTimers();
  check(frame.label + ': fade clears the text and hides the line',
    feedback.state().text === ''
    && restoredLine.textContent === ''
    && restoredLine.style.display === 'none');
}

// §6.5's fourth success form: neither pending nor an observed stop. Not one of
// the five required frames, but its prescribed text must not be a stop claim.
feedback.handle({
  action: 'carto_cancel_job_result',
  success: true,
  job_id: 'job-ack',
  outcome: 'requested',
});
check('other success: acknowledgement text',
  feedback.state().text === 'Cancellation requested for job job-ack; poll job.status.');
check('other success: never claims the job has stopped',
  !feedback.state().text.includes('has stopped'));
runFadeTimers();

// Failed reply without an error string uses the prescribed fallback.
feedback.handle({
  action: 'carto_cancel_job_result',
  success: false,
  job_id: 'job-failed-no-error',
});
check('failed without error: Unknown error fallback',
  feedback.state().text === 'Cancellation request failed: Unknown error');
runFadeTimers();

// ============================================================================
// B. The real WebSocket cancel-result branch dispatches to the handler
// ============================================================================
const wsBranch = new Function(
  'request', 'handleCartoLaunchResult', 'GLIMPSE_VERSION', 'console',
  // The anchor begins with '}' (it is an else-if leg); give it an `if` to
  // close so the rebuilt snippet is a balanced statement.
  'if (false) {' + WS_ANCHOR + '\n' + WS_BRANCH_BODY + '\n}'
);

function runWsBranch(request) {
  const events = [];
  const branchConsole = {
    error: (...args) => events.push(['error', ...args]),
    log() {},
    warn() {},
  };
  const handlerSpy = (message) => events.push(['handler', message]);
  wsBranch(request, handlerSpy, 'test', branchConsole);
  return events;
}

const failedRequest = {
  action: 'carto_cancel_job_result',
  success: false,
  job_id: 'job-ws-failed',
  error: 'Job already completed.',
};
let events = runWsBranch(failedRequest);
check('WS failed reply: unchanged error log comes first',
  events.length === 2
  && events[0][0] === 'error'
  && events[0][1] === '[GLIMPSE Cache vtest] carto_cancel_job_result error:'
  && events[0][2] === 'Job already completed.');
check('WS failed reply: dispatched once to handleCartoLaunchResult',
  events[1][0] === 'handler' && events[1][1] === failedRequest);

events = runWsBranch({
  action: 'carto_cancel_job_result',
  success: true,
  job_id: 'job-ws-pending',
  cancellation_pending: true,
});
check('WS successful reply: dispatched (this branch was previously silent)',
  events.length === 1 && events[0][0] === 'handler');
check('WS successful reply: no error log', !events.some((e) => e[0] === 'error'));

check('WS branch keeps its unchanged error-log line',
  WS_BRANCH_BODY.includes('carto_cancel_job_result error:'));
check('WS branch calls the existing handleCartoLaunchResult(request)',
  WS_BRANCH_BODY.includes('handleCartoLaunchResult(request);'));
check('WS branch adds no timer or polling change',
  !WS_BRANCH_BODY.includes('setTimeout') && !WS_BRANCH_BODY.includes('setInterval'));

// ============================================================================
// C. Static locks: prescribed vocabulary, ONE timer, no fabricated job row
// ============================================================================
check('handler knows the modern pending marker',
  RAW_FEEDBACK_FN.includes('message.cancellation_pending === true'));
check('handler keeps the legacy pending outcome fallback',
  RAW_FEEDBACK_FN.includes("message.outcome === 'kill_refused_in_process_job'"));
check('handler distinguishes cooperative and already_terminal',
  RAW_FEEDBACK_FN.includes("message.outcome === 'cooperative'")
  && RAW_FEEDBACK_FN.includes("message.outcome === 'already_terminal'"));
check('handler schedules exactly ONE timer and it is the existing 8000 ms fade',
  countOcc(RAW_FEEDBACK_FN, 'setTimeout(') === 1
  && countOcc(RAW_FEEDBACK_FN, 'clearTimeout(') === 1
  && /},\s*8000\);/.test(RAW_FEEDBACK_FN));
check('handler never fabricates a job row or writes the container textContent',
  !RAW_FEEDBACK_FN.includes('appendChild')
  && !/cartoJobsEl\.textContent\s*=/.test(RAW_FEEDBACK_FN));
check('the poll repaint still re-inserts the dedicated feedback line',
  extractIifeFunction(src, 'handleCartoJobsResult').includes('_ensureCartoLaunchFeedbackLine();'));
check('the feedback line is re-inserted by more than one repaint path',
  countOcc(src, '_ensureCartoLaunchFeedbackLine()') >= 2);

console.log(failed === 0 ? 'ALL PASS' : `${failed} FAILURES`);
process.exit(failed === 0 ? 0 : 1);
