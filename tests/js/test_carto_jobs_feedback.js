'use strict';

// CF-151 branches 1–3. All functions are extracted from shipped bytes.
const vm = require('vm');
const { loadPage, source, extract, assert } = require('./test_carto_jobs_twenty_tabs');

async function main() {
  const failures = [];
  function witness(name, fn) {
    try { fn(); console.log('PASS ' + name); }
    catch (error) { failures.push(name + ': ' + error.message); console.error('FAIL ' + name + ': ' + error.message); }
  }
  witness('CF-151/1 unclassifiable launch includes the daemon reason', () => {
    const page = loadPage({ open: true });
    page.context.handleCartoLaunchResult({ action: 'refresh_folder_node_result', success: false,
      error_kind: 'governance_state_unclassifiable', error: 'could not classify the old artifact', blocking_artifacts: ['job.json'] });
    assert(page.host.textContent.includes('could not classify the old artifact'));
    assert(page.host.textContent.includes('job.json'));
  });
  witness('CF-151/2 a throwing list renderer is visible and immediately re-polls', () => {
    const page = loadPage({ open: true });
    page.reset();
    page.context.handleCartoJobsResult = () => { throw new Error('synthetic display failure'); };
    // Legacy action makes this a behavioural RED on the old dispatch too.
    page.receive('{"action": "carto_list_jobs_result", "success": true, "jobs": []}');
    assert(page.host.textContent.includes('synthetic display failure'));
    const polls = page.sends.filter((frame) => frame.action === 'carto_list_jobs');
    assert.strictEqual(polls.length, 1);
    assert.strictEqual(polls[0].since, null, 'Recovery explicitly asks for a full list');
    assert(page.warnings.some((args) => args.join(' ').includes('synthetic display failure')));
  });

  try {
    const page = loadPage({ open: true });
    const context = page.context;
    Object.assign(context, { ensureSocketReady: async () => true, closeTagMutationPopup() {},
      dockedTagMutationBtnEl: null, surfaceRenderFailure() { throw new Error('A delayed reply must not be reported failed'); } });
    vm.runInContext('const pendingMutateNodeTags = new Map(); let mutateNodeTagsReqCounter = 0; const MUTATE_NODE_TAGS_TIMEOUT_MS = 180000;', context);
    if (source.includes('function handleLateMutateNodeTagsResult(')) vm.runInContext(extract('handleLateMutateNodeTagsResult'), context);
    ['handleMutateNodeTagsResult', 'requestMutateNodeTags', '_submitTagMutation'].forEach((name) => vm.runInContext(extract(name), context));
    const submit = context._submitTagMutation('add', { tags: ['example'] }, { harvested: 1,
      spec: { operations: [{ handle: 'node', operation: 'add', tags: ['example'] }] } });
    const submitErrors = [];
    submit.catch((error) => submitErrors.push(error));
    // Flush the cross-realm await chain, not a clock or a timing gate.
    await new Promise(setImmediate);
    const request = page.sends.find((frame) => frame.action === 'mutate_node_tags');
    assert(request, 'The real mutation was sent: ' + submitErrors.map(String).join('; '));
    const timerEntry = [...page.timeouts.entries()].find(([, timer]) => timer.delay === 180000);
    assert(timerEntry, 'The existing 180s timer is unchanged');
    page.timeouts.delete(timerEntry[0]);
    timerEntry[1].fn();
    await submit;
    assert(page.toasts.some((text) => text.includes('outcome unknown; refresh to confirm')));
    assert.strictEqual(page.value('pendingMutateNodeTags.has(' + JSON.stringify(request.request_id) + ')'), true, 'The late-reply hook remains registered');
    context.handleMutateNodeTagsResult({ action: 'mutate_node_tags_result', request_id: request.request_id,
      success: true, summary: { committed: 1, noop: 0, commit_failures: 0 } });
    assert(page.toasts.some((text) => text.includes('Late tag mutation reply: committed 1')));
    assert.strictEqual(page.value('pendingMutateNodeTags.size'), 0, 'The actual late reply consumes its hook');
    assert.strictEqual(page.sends.filter((frame) => frame.action === 'mutate_node_tags').length, 1, 'Silence never re-sends a mutation');
    console.log('PASS CF-151/3 timer reports unknown and the late reply is displayed');
  } catch (error) {
    failures.push('CF-151/3: ' + error.message);
    console.error('FAIL CF-151/3: ' + error.message);
  }
  assert.strictEqual(failures.length, 0, failures.join('\n'));
  console.log('ALL PASS · CF-151 branches 1–3');
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
