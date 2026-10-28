'use strict';

// JR4 / S8: execute shipped functions, not a second model of the page.
// The fixture is the daemon harness's raw send stream; that harness checks drift.
// CARTO_JOBS_CLIENT / CARTO_JOBS_DRAWERS select immutable base copies for RED.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const root = path.join(__dirname, '..', '..');
const clientFile = process.env.CARTO_JOBS_CLIENT || path.join(root, 'workflowy-glimpse-cache/glimpse-cache-client.js');
const drawersFile = process.env.CARTO_JOBS_DRAWERS || path.join(root, 'workflowy-glimpse-cache/glimpse-ui-drawers.js');
const source = fs.readFileSync(clientFile, 'utf8');
const drawers = fs.readFileSync(drawersFile, 'utf8');

function braceSlice(text, start) {
  const open = text.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === '{') depth++;
    else if (text[i] === '}' && --depth === 0) return text.slice(start, i + 1);
  }
  throw new Error('Unbalanced shipped source at ' + start);
}

function extract(name, text = source) {
  for (const prefix of [`\n  async function ${name}(`, `\n  function ${name}(`]) {
    const index = text.indexOf(prefix);
    if (index >= 0) return braceSlice(text, index + 1);
  }
  throw new Error('Missing shipped function: ' + name);
}

function makeElement(tag, counters) {
  const element = {
    tagName: tag, id: '', style: {}, children: [], parentNode: null, ownText: '', listeners: {}, title: '',
    classList: { add() {}, remove() {}, toggle() {} },
    get textContent() { return this.ownText + this.children.map((child) => child.textContent).join('\n'); },
    set textContent(value) {
      if (this.id === 'glimpse-carto-jobs') counters.rebuilds++;
      this.ownText = String(value);
      this.children.forEach((child) => { child.parentNode = null; });
      this.children = [];
    },
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
    insertBefore(child, before) {
      child.parentNode = this;
      const at = this.children.indexOf(before);
      this.children.splice(at < 0 ? this.children.length : at, 0, child);
      return child;
    },
    get firstChild() { return this.children[0] || null; },
    addEventListener(event, fn) { this.listeners[event] = fn; },
    setAttribute(key, value) { this[key] = value; },
    querySelector() { return null; },
    click() {
      let stopped = false;
      const event = { currentTarget: this, target: this, stopPropagation() { stopped = true; }, preventDefault() {} };
      if (this.listeners.click) this.listeners.click(event);
      if (!stopped && this.parentNode && this.parentNode.listeners.click) this.parentNode.listeners.click(event);
    },
  };
  return element;
}

function loadPage({ hidden = false, open = false, endpoint = 'tab-A', text = source, drawerText = drawers } = {}) {
  const counts = { received: 0, parsed: 0, parses: 0, rebuilds: 0, badges: 0 };
  const sends = [], warnings = [], copied = [], toasts = [];
  const listeners = new Map(), intervals = new Map(), timeouts = new Map();
  let timerId = 0, drawerOpen = open, time = 0, socket, drawerDefinition;
  const document = {
    hidden,
    createElement(tag) { return makeElement(tag, counts); },
    querySelector() { return null; },
    addEventListener(name, fn) { listeners.set(name, fn); },
    removeEventListener(name) { listeners.delete(name); },
  };
  class BridgeSocket {
    constructor() { this.readyState = 1; socket = this; }
    send(raw) { sends.push(JSON.parse(raw)); }
  }
  const context = vm.createContext({
    document, window: {}, navigator: { clipboard: { writeText(value) { copied.push(value); return Promise.resolve(); } } },
    console: { log() {}, warn(...args) { warnings.push(args); }, error(...args) { warnings.push(args); } },
    JSON: { stringify: JSON.stringify, parse(raw) { counts.parsed += Buffer.byteLength(raw); counts.parses++; return JSON.parse(raw); } },
    Date: { now() { return time; } }, Map, Set, Promise,
    WebSocket: { OPEN: 1, CLOSED: 3 }, BridgeSocket,
    GLIMPSE_VERSION: 'test', WS_URL: 'test', RECONNECT_DELAY: 5000,
    ws: null, isConnecting: false, reconnectInterval: null,
    registerBrokerEndpoint() {}, logWslive() {}, failAllPendingSliceRenders() {},
    getEndpointId() { return endpoint; }, extractNodeName() { return ''; }, resolveCartographerRelPathFromDom() { return null; },
    fallbackCopy(value, done) { copied.push(value); done(true); },
    showDockToast(value) { toasts.push(value); },
    setInterval(fn, delay) { const id = ++timerId; intervals.set(id, { fn, delay }); return id; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout(fn, delay) { const id = ++timerId; timeouts.set(id, { fn, delay }); return id; },
    clearTimeout(id) { timeouts.delete(id); },
    GlimpseUI: {
      currentDrawer() { return drawerOpen ? 'jobs' : null; },
      setJobsCount() { counts.badges++; }, setConnected() {},
      openDrawer() { drawerOpen = true; if (drawerDefinition.onOpen) drawerDefinition.onOpen(); },
    },
  });
  const start = text.indexOf('  let cartoJobsEl = null;');
  const end = text.indexOf('  // Snapshot of the CARTO status', start);
  assert(start >= 0 && end > start, 'Shipped job state boundaries');
  vm.runInContext(text.slice(start, end) + '\nlet uuidNavigatorOutputEl = null;\nlet lastRenderedCartoStatusLines = [];', context);
  const names = [
    '_ensureCartoLaunchFeedbackLine', 'handleCartoLaunchResult', 'formatCartoHeartbeatDiagnostic',
    'warnCartoJobsOnce', 'isCartoJobsDrawerOpen', 'stopCartoJobsPolling', 'pollCartoJobs', 'updateCartoJobsPolling',
    'reportCartoJobsDisplayError', 'displayCartoJobsResult', 'renderLatestCartoJobs', 'dispatchCartoJobsResult', 'handleCartoJobsFrame',
    'requestCartoJobDetails', 'handleCartoJobDetailResult', 'cartoDetailItemText', 'renderCartoJobDetails',
    'handleCartoJobsResult', 'connectWebSocket',
  ];
  for (const name of names) {
    if (text.includes(`\n  function ${name}(`)) vm.runInContext(extract(name, text), context);
  }
  const registration = drawerText.indexOf("  UI.registerDrawer('jobs', {");
  assert(registration >= 0, 'The shipped Jobs drawer exists');
  const ui = { registerDrawer(id, def) { drawerDefinition = def; }, els() { return {}; }, bindActivityHost() {} };
  // Execute the actual Jobs API properties too; only the host references are
  // observed by the fixture, not copied from a second implementation.
  context.wsOpen = () => !!(context.ws && context.ws.readyState === 1);
  const apiStart = text.indexOf('      clearJobHistory: () => {');
  const apiEnd = text.indexOf('      resolveUuidPath:', apiStart);
  assert(apiStart >= 0 && apiEnd > apiStart, 'The shipped Jobs API boundaries');
  const shippedApi = vm.runInContext('({' + text.slice(apiStart, apiEnd) + '})', context);
  const api = { ...shippedApi, bindJobsHost(host, truncated, details) {
    context.host = host; context.truncated = truncated; context.details = details;
    shippedApi.bindJobsHost(host, truncated, details);
  } };
  const el = (tag, className, content) => { const node = document.createElement(tag); if (content) node.textContent = content; return node; };
  const btn = (label, title, click) => { const node = el('button', '', label); node.addEventListener('click', click); return node; };
  new Function('UI', 'api', 'el', 'btn', 'iconBtn', 'oneLiner', 'jobs', 'jobsPaintSkip', 'helpBtn', 'toast',
    braceSlice(drawerText, registration) + ');')(ui, () => api, el, btn, btn, () => null, { els: {} }, () => {}, () => el('div'), () => {});
  const container = document.createElement('div');
  drawerDefinition.build(container);
  context.connectWebSocket();
  socket.onopen();

  return {
    context, counts, sends, warnings, copied, toasts, intervals, timeouts, document, drawerDefinition, api, container,
    get host() { return context.host; }, get details() { return context.details; }, get truncated() { return context.truncated; },
    receive(raw) { counts.received += Buffer.byteLength(raw); socket.onmessage({ data: raw }); },
    visible(value) { document.hidden = !value; const fn = listeners.get('visibilitychange'); if (fn) fn(); },
    open(value = true) { drawerOpen = value; if (value && drawerDefinition.onOpen) drawerDefinition.onOpen(); },
    tick() { time += 1000; for (const timer of [...intervals.values()]) if (timer.delay === 1000) timer.fn(); },
    close() { socket.readyState = 3; socket.onclose(); },
    reconnect() { context.connectWebSocket(); socket.onopen(); },
    reset() { Object.keys(counts).forEach((key) => { counts[key] = 0; }); sends.length = 0; },
    value(expr) { return vm.runInContext(expr, context); },
  };
}

function allElements(element) { return [element, ...element.children.flatMap(allElements)]; }

function measure(stream, hidden, open) {
  const page = loadPage({ hidden, open });
  stream.warmup.forEach((raw) => page.receive(raw));
  page.reset();
  let second = -1;
  const seconds = Array.from({ length: 60 }, () => ({ received: 0, parsed: 0, parses: 0, rebuilds: 0, badges: 0 }));
  for (const frame of stream.frames) {
    const sec = Math.floor(frame.at);
    if (second !== sec) { page.tick(); second = sec; }
    const before = { ...page.counts };
    page.receive(frame.text);
    for (const key of Object.keys(before)) seconds[sec][key] += page.counts[key] - before[key];
  }
  return { page, seconds, totals: { ...page.counts }, polls: page.sends.filter((frame) => frame.action === 'carto_list_jobs').length };
}

function main() {
  const recorded = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/carto_jobs_twenty_tabs/stream.json'), 'utf8'));
  assert.strictEqual(recorded.duration_seconds, 60);
  const arm = process.env.CARTO_JOBS_MEASURE_LEGACY ? 'legacy' : 'v2';
  const measurements = [];
  for (const scenario of ['idle', 'running']) {
    // Twenty page instances, not multiplied results from three samples.
    const tabs = Array.from({ length: 20 }, (_, i) => measure(recorded.scenarios[scenario][arm], i >= 3, i === 0));
    for (const [kind, index, count] of [['visible/open', 0, 1], ['visible/closed', 1, 2], ['hidden', 3, 17]]) {
      const result = tabs[index];
      const metrics = { scenario, arm, kind, tabs: count, totals_per_tab: result.totals, polls_per_tab: result.polls,
        per_second: Object.fromEntries(Object.keys(result.totals).map((key) => [key, result.totals[key] / 60])),
        max_per_second: Object.fromEntries(Object.keys(result.totals).map((key) => [key, Math.max(...result.seconds.map((s) => s[key]))])) };
      console.log('JR4_PAGE_COUNTERS ' + JSON.stringify(metrics));
    }
    measurements.push({ scenario, tabs });
  }
  // This is deliberately RED on the base client, AFTER recording its counters.
  assert(source.includes('function handleCartoJobsFrame('), 'The real raw frame entry must exist (absent before JR4)');
  for (const { scenario, tabs } of measurements) {
    tabs.forEach(({ page, totals, seconds, polls }, i) => {
      if (i >= 3) {
        assert.strictEqual(totals.parsed, 0, 'Hidden tab parses no bytes');
        assert.strictEqual(totals.rebuilds, 0, 'Hidden tab rebuilds nothing');
        assert.strictEqual(totals.badges, 0, 'Hidden tab writes no badge');
        assert.strictEqual(polls, 0, 'Hidden tab sends no polls');
      } else {
        assert.strictEqual(polls, 60, 'Visible tab polls once per second');
        assert(page.sends.every((frame) => frame.action !== 'carto_list_jobs' || Object.hasOwn(frame, 'since')));
        assert(seconds.every((s) => s.parses <= 2));
        assert.strictEqual(totals.badges, 0, 'Progress does not rewrite a stable count');
        if (scenario === 'idle') {
          assert(seconds.every((s) => s.parsed <= 240), 'Idle parses at most two tiny frames/sec');
          assert.strictEqual(totals.rebuilds, 0, 'Idle does not rebuild');
        } else if (i === 0) assert(seconds.every((s) => s.rebuilds <= 2));
        else assert.strictEqual(totals.rebuilds, 0, 'Closed drawer never rebuilds');
      }
    });
  }

  const full = (generation, jobs = []) => JSON.stringify({ action: 'carto_list_jobs_result_v2', success: true, jobs, generation });
  const same = JSON.stringify({ action: 'carto_list_jobs_result_v2', success: true, unchanged: true, generation: 'g2' });
  const legacy = '{"action": "carto_list_jobs_result", "success": true, "jobs": [{"job_id":"old","root_uuid":"old","status":"running"}]}';
  const v2error = '{"action": "carto_list_jobs_result_v2", "success": false, "error": "disk busy"}';
  const hidden = loadPage({ hidden: true, open: true });
  hidden.reset();
  [full('g1'), v2error, legacy].forEach((raw) => hidden.receive(raw));
  assert.strictEqual(hidden.counts.parsed, 0, 'Compact v2, spaced v2 error and spaced legacy all bypass parsing');
  assert.strictEqual(hidden.value('_cartoJobsRawFrame'), legacy, 'Keep exactly the newest RAW text, regardless of spelling');
  hidden.visible(true);
  assert.strictEqual(hidden.counts.parses, 1, 'Visibility consumes that raw text once');
  assert(hidden.host.textContent.includes('old'), 'Legacy-only daemon still shows a list');
  const oldGeneration = hidden.value('_cartoJobsGeneration');
  hidden.receive(full('g1'));
  hidden.visible(false);
  hidden.receive(full('g2', [{ job_id: 'new', root_uuid: 'new', status: 'running' }]));
  hidden.receive(same); // the full frame has been superseded by a tiny broadcast
  hidden.visible(true);
  assert.strictEqual(hidden.sends.at(-1).since, 'g1', 'An unchanged-only wake polls with the last accepted full generation');
  hidden.receive(full('g2', [{ job_id: 'new', root_uuid: 'new', status: 'running' }]));
  assert(hidden.host.textContent.includes('new'), 'Immediate stale-since poll restores the new list');
  hidden.reset();
  hidden.receive(same);
  hidden.receive(full('g2', [{ job_id: 'new', root_uuid: 'new', status: 'running' }]));
  hidden.receive(legacy);
  assert.strictEqual(hidden.counts.rebuilds, 0, 'Unchanged, equal generation and mixed-profile legacy do no DOM work');
  assert.strictEqual(hidden.counts.parses, 2, 'After v2 discovery, legacy duplicates are ignored before parsing');
  assert.strictEqual(hidden.counts.badges, 0);
  assert.strictEqual(oldGeneration, null);

  const closed = loadPage({ open: false });
  closed.receive(full('drawer', [{ job_id: 'on-open', root_uuid: 'on-open', status: 'running' }]));
  assert(!closed.host.textContent.includes('on-open'));
  closed.open();
  assert(closed.host.textContent.includes('on-open'), 'The REAL drawer onOpen paints latest accepted state');
  const rebuilds = closed.counts.rebuilds;
  closed.open();
  assert.strictEqual(closed.counts.rebuilds, rebuilds, 'Already-current drawer DOM is reused');
  closed.visible(false);
  assert.strictEqual([...closed.intervals.values()].filter((timer) => timer.delay === 1000).length, 0);
  closed.visible(true);
  assert.strictEqual([...closed.intervals.values()].filter((timer) => timer.delay === 1000).length, 1);
  closed.close();
  assert.strictEqual([...closed.intervals.values()].filter((timer) => timer.delay === 1000).length, 0, 'Socket close clears the poll');
  assert.strictEqual(closed.value('_cartoJobsProtocol2'), false, 'Reconnect rediscovers protocol support for a rolled-back daemon');

  const legacyOnly = loadPage({ open: true });
  legacyOnly.receive(legacy); legacyOnly.reset(); legacyOnly.receive(legacy);
  assert.strictEqual(legacyOnly.counts.parses, 0, 'Identical legacy-only frame is also cheap');

  const rollback = loadPage({ hidden: true, open: true });
  rollback.receive(full('before-rollback'));
  rollback.close();
  rollback.reconnect();
  rollback.visible(true);
  rollback.receive(legacy);
  assert(rollback.host.textContent.includes('old'), 'Deferred v2 text cannot suppress a legacy daemon after reconnect');
  console.log('ALL PASS · JR4 twenty-tab stream, raw entry, poll and drawer');
}

module.exports = { loadPage, extract, source, drawers, allElements, assert };
if (require.main === module) main();
