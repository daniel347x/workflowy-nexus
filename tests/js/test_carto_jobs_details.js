'use strict';

// JR4 §5.3: real shipped request/reply/panel and actual row controls, fake wire.
const { loadPage, source, extract, allElements, assert } = require('./test_carto_jobs_twenty_tabs');
const detailsSends = (page) => page.sends.filter((frame) => frame.action === 'carto_job_detail');
const controls = (page, text) => allElements(page.details).filter((el) => el.tagName === 'button' && el.textContent === text);
const full = (jobs, generation = 'g1', truncated) => JSON.stringify({ action: 'carto_list_jobs_result_v2', success: true, jobs, generation, truncated });
const problemJob = {
  job_id: 'problems', kind: 'CARTO_REFRESH', root_uuid: 'root', mode: 'folder', status: 'completed_with_warnings',
  outcome: 'completed_with_errors', has_errors: true, has_warnings: true, has_findings: true,
  warning_count: 45, warnings: [{ message: 'first warning' }, { message: 'second warning' }],
};

function response(request, { rows = [], next = null, version = 'v1', ...extra } = {}) {
  return JSON.stringify({ action: 'carto_job_detail_result', success: true, request_id: request.request_id,
    target_endpoint_id: request.endpoint_id, job_id: request.job_id,
    job: { status: 'completed_with_warnings', outcome: 'completed_with_errors', errors_total: 1,
      warnings_total: 45, findings_total: 5000, error: 'run failed locally', error_kind: 'local_error', summary_line: 'all evidence is reachable' },
    rows, page: { filter: 'problems', next, total_rows: 3, evidence_version: version }, ...extra });
}

async function main() {
  // RED at the base: neither a Details request nor a panel exists there.
  assert(source.includes('function requestCartoJobDetails('), 'Shipped Details entry must exist');
  ['requestCartoJobDetails', 'handleCartoJobDetailResult', 'renderCartoJobDetails'].forEach((name) => {
    const fn = extract(name);
    assert(!fn.includes('setTimeout(') && !fn.includes('setInterval('), 'Details adds no timer');
  });
  const a = loadPage({ open: true, endpoint: 'A' });
  const b = loadPage({ open: true, endpoint: 'B' });
  a.receive(full([problemJob], 'g1', { jobs_omitted: 7 }));
  b.receive(full([problemJob]));
  assert.strictEqual(detailsSends(a).length, 0, 'A list update never fetches details');
  assert.strictEqual(detailsSends(b).length, 0);
  assert.strictEqual(a.truncated.textContent, '7 older jobs not shown');
  assert(a.host.textContent.includes('completed with errors'), 'Outcome label has human words');
  const label = allElements(a.host).find((el) => el.tagName === 'span' && el.title.includes('45 warnings'));
  assert(label.title.includes('… and 43 more'), 'Tooltip uses exact warning_count, not the capped array');
  const row = a.host.children.find((el) => el.children.includes(label));
  row.click();
  await Promise.resolve();
  assert.strictEqual(a.copied.length, 1, 'Row click still copies status plus tooltip detail');
  assert(a.copied[0].includes('first warning'));
  assert.strictEqual(detailsSends(a).length, 0, 'Copying the row is not a Details click');

  const detailsButton = allElements(row).find((el) => el.textContent === 'Details');
  const timersBefore = a.timeouts.size;
  detailsButton.click();
  assert.strictEqual(detailsSends(a).length, 1, 'Exactly one send per Details click');
  assert.strictEqual(a.copied.length, 1, 'Details stops the row-copy bubble');
  const first = detailsSends(a)[0];
  assert.deepStrictEqual(JSON.parse(JSON.stringify(first)), { action: 'carto_job_detail', request_id: first.request_id,
    endpoint_id: 'A', job_id: 'problems', filter: 'problems', offset: 0, item_offset: 0, limit: 50 });
  assert(a.details.textContent.includes('Waiting for details'));
  assert.strictEqual(controls(a, 'Retry').length, 1, 'Waiting always offers a user-clicked Retry');
  assert.strictEqual(a.timeouts.size, timersBefore, 'No Details timeout or retry timer');
  const panelBeforeUnknown = a.details.textContent;
  a.receive(response({ ...first, request_id: 'not-pending' }));
  assert.strictEqual(a.details.textContent, panelBeforeUnknown, 'A non-pending reply is ignored');
  b.receive(response(first));
  assert.strictEqual(b.details.textContent, '', 'A reply targeted to A never renders in B');
  a.visible(false);
  const run = { sec: 'run', file: null, errors: ['one run error'], warnings: [{ message: 'one run warning' }], events: [{ type: 'run event' }] };
  const file = { sec: 'files', file: '/repo/problem.py', status: 'failed', changed: false, error: 'file error',
    warnings: [{ type: 'warning', message: 'file warning' }], findings: [{ code: 'finding', severity: 'info', message: 'finding zero', line: 0, finding_id: '0' }],
    events: ['file event'], node_errors: [{ error: 'node error' }], errors: [] };
  // Deliberately feed a non-run row first too: presentation still labels Run first.
  a.receive(response(first, { rows: [file, run], next: { offset: 1, item_offset: 1 } }));
  assert(a.details.textContent.includes('one run error'), 'The clicked tab processes its targeted reply even after becoming hidden');
  assert(a.details.textContent.indexOf('\nRun') < a.details.textContent.indexOf('/repo/problem.py'));
  ['one run warning', 'run event', 'file error', 'file warning', 'finding zero', 'node error', 'all evidence is reachable', 'run failed locally'].forEach((text) => assert(a.details.textContent.includes(text), text));
  assert.strictEqual(detailsSends(a).length, 1, 'Receiving a page never fetches the next page');
  assert.strictEqual(controls(a, 'Load more').length, 1);

  // Every item in one large row is reachable, not a hidden-row cap. The
  // daemon's budget/row cursor is pinned independently by the G2 Python test.
  const ids = ['0'];
  for (let start = 1; start < 5000; start += 1000) {
    controls(a, 'Load more')[0].click();
    const request = detailsSends(a).at(-1);
    const end = Math.min(start + 1000, 5000);
    assert.strictEqual(request.offset, 1);
    assert.strictEqual(request.item_offset, start);
    const findings = Array.from({ length: end - start }, (_, i) => {
      const id = String(start + i); ids.push(id); return { code: 'finding', message: 'item-' + id, finding_id: id };
    });
    a.receive(response(request, { rows: [{ sec: 'files', file: '/repo/problem.py', findings }],
      next: end === 5000 ? { offset: 2, item_offset: 0 } : { offset: 1, item_offset: end }, version: start === 1 ? 'v2' : 'v1' }));
  }
  assert(a.details.textContent.includes('The job changed since the first page'), 'CF-169 evidence change is visible, never a refusal');
  controls(a, 'Load more')[0].click();
  const last = detailsSends(a).at(-1);
  assert.strictEqual(last.offset, 2);
  a.receive(response(last, { rows: [{ sec: 'phase8', file: '/repo/final.md', status: 'failed', error: 'phase8 problem' }] }));
  assert(a.details.textContent.includes('Phase 8 · /repo/final.md'));
  assert.strictEqual(controls(a, 'Load more').length, 0);
  assert.strictEqual(ids.length, 5000);
  assert.strictEqual(new Set(ids).size, 5000);
  assert.strictEqual(a.value('cartoJobDetailsState.rows.reduce((n, row) => n + (row.findings || []).length, 0)'), 5000);
  assert(a.details.textContent.includes('item-4999'), 'The last item is rendered');
  assert.strictEqual(detailsSends(a).length, 7, 'One initial click plus exactly six Load more clicks');
  const requestIds = detailsSends(a).map((request) => request.request_id);
  assert.strictEqual(new Set(requestIds).size, requestIds.length, 'Every page click mints a new pending id');

  a.visible(true);
  detailsButton.click();
  const waiting = detailsSends(a).at(-1);
  const n = detailsSends(a).length;
  controls(a, 'Retry')[0].click();
  assert.strictEqual(detailsSends(a).length, n + 1, 'Manual Retry is exactly one additional send');
  const retry = detailsSends(a).at(-1);
  a.receive(response(waiting, { rows: [run] }));
  assert(a.details.textContent.includes('Waiting for details'), 'A superseded request cannot overwrite the latest click');
  a.receive(response(retry, { success: false, error: 'No such job' }));
  assert(a.details.textContent.includes('No such job'));
  assert.strictEqual(controls(a, 'Retry').length, 1, 'A targeted error stays usable');

  const cancelPage = loadPage({ open: true });
  cancelPage.receive(full([{ job_id: 'live', status: 'running', has_warnings: true }]));
  const cancel = allElements(cancelPage.host).find((el) => el.textContent === 'CANCEL');
  cancel.click();
  assert.strictEqual(cancelPage.sends.filter((frame) => frame.action === 'carto_cancel_job').length, 1);
  assert.strictEqual(cancelPage.copied.length, 0, 'CANCEL still stops copy bubbling');
  assert.strictEqual(detailsSends(cancelPage).length, 0);

  const copyStatus = allElements(cancelPage.container).find((el) => el.tagName === 'button' && el.textContent === 'Copy status');
  copyStatus.click();
  await Promise.resolve();
  assert.strictEqual(cancelPage.copied.at(-1), cancelPage.value('lastRenderedCartoStatusLines.join("\\n")'), 'Copy status uses the shipped structured snapshot');
  const clear = allElements(cancelPage.container).find((el) => el.tagName === 'button' && el.textContent === 'Clear history');
  clear.click();
  assert.strictEqual(cancelPage.sends.filter((frame) => frame.action === 'carto_clear_job_history').length, 1, 'Clear history still sends its original action');
  cancelPage.receive(JSON.stringify({ action: 'carto_clear_job_history_result', success: true, deleted_jobs: 1, deleted_artifacts: 2 }));
  assert.strictEqual(cancelPage.sends.at(-1).action, 'carto_list_jobs');
  assert.strictEqual(cancelPage.sends.at(-1).since, null, 'Clear completion refreshes even an unchanged-generation list');
  cancelPage.receive(full([], 'clear'));
  assert(cancelPage.host.textContent.includes('No active CARTO jobs.'));

  const eligibility = loadPage({ open: true });
  eligibility.receive(full([
    { job_id: 'clean', status: 'running' }, { job_id: 'error', status: 'running', has_errors: true },
    { job_id: 'warning', status: 'running', has_warnings: true }, { job_id: 'finding', status: 'running', has_findings: true },
    { job_id: 'failed', status: 'failed' },
  ]));
  assert.strictEqual(allElements(eligibility.host).filter((el) => el.tagName === 'button' && el.textContent === 'Details').length, 4);
  assert.strictEqual(detailsSends(eligibility).length, 0);
  console.log('ALL PASS · JR4 targeted Details, complete paging, copy and CANCEL');
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
