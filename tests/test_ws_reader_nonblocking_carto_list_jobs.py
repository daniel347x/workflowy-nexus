"""APPLY-Timeout fix -- the widget's carto_list_jobs poll never blocks the pump.

RECON AND FIX socket [ap:7125X0] (program [ap:HJNU1W]).  The widget in EVERY
Workflowy tab polls ``carto_list_jobs`` once a second.  ``websocket_handler``
used to answer it INLINE: a synchronous ``_scan_carto_job_files()`` (every job
JSON read and liveness-classified -- 0.25 s on the live machine with 223 jobs)
followed by an awaited multi-MB send.  With several tabs polling, the pump fell
behind without bound; the tab's ``get_slice_group_config_result`` (the nested
read slice-group APPLY needs) sat 58 s in the queue and APPLY timed out, until
the connection died on a keepalive ping timeout (daemon log, 2026-09-28).

Post-fix (``_carto_list_jobs_poll`` + ``_handle_carto_list_jobs``):
* the pump only admits the poll and ``continue``s; the scan runs in a worker
  thread inside a spawned task;
* polls are coalesced (single-flight + 0.5 s spacing) -- the reply is
  untargeted, so the broker broadcasts it to every tab;
* the reply omits exactly the rows the widget hides (completed / cancelled).

Job-list fix ([ap:FK5O8K], 2026-10-26): a 522-file folder refresh's records
(per-file evidence, tens of MB each) made every poll decode ~230 MB and send
17-27 MB, past the 16 MiB frame limit, and starved the refresh.  Records are
now written head-first; the widget scan reads only heads, cached by
mtime/size, and the reply carries compact rows under a hard byte budget.

Sits beside ``test_ws_reader_nonblocking_refresh`` (the nodes-cache-refresh /
F12 fix) and ``test_ws_reader_nonblocking_long_handlers`` (U4), and reuses
their pump harness.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import MappingProxyType

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workflowy_mcp import carto_job_widget as widget  # noqa: E402
from workflowy_mcp import config as carto_config  # noqa: E402
from workflowy_mcp import server as srv  # noqa: E402
from test_ws_reader_nonblocking_refresh import (  # noqa: E402
    PUMP_TIMEOUT_S,
    _install_client_stub,
    _QueueWebSocket,
)


@pytest.fixture(autouse=True)
def _fresh_poll_state(monkeypatch):
    """Every test starts with no scan in flight, no recent scan, no cached record."""
    monkeypatch.setattr(srv, "_carto_list_jobs_in_flight", False, raising=False)
    monkeypatch.setattr(srv, "_carto_list_jobs_last_started", float("-inf"), raising=False)
    # The per-path record cache may move to the shared reader (G2); reset it
    # wherever this module still exposes it.
    monkeypatch.setattr(widget, "_record_cache", {}, raising=False)


async def _drain_until(predicate, *, timeout=PUMP_TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached before the deadline")
        await asyncio.sleep(0.01)


def _results(ws):
    return [f for f in ws.sent if f.get("action") == "carto_list_jobs_result"]


# ---------------------------------------------------------------------------
# The incident, reproduced: a slow scan must not hold the pump
# ---------------------------------------------------------------------------


def test_pump_answers_ping_while_the_jobs_scan_is_blocked(monkeypatch):
    """CHANGE-DETECTING (red against the pre-fix inline clause): the scan
    blocks on a THREADING event that only the pong sets.  A scan resident on
    the event loop (inline, or spawned without to_thread) can never be
    released, so the pump could never answer the ping -> timeout."""
    _install_client_stub(monkeypatch)
    release = threading.Event()
    scan_threads: list[int] = []

    def _blocked_scan(base_dir_str=None, **_view):
        scan_threads.append(threading.get_ident())
        assert release.wait(timeout=PUMP_TIMEOUT_S), (
            "the jobs scan was never released: the pump could not answer the "
            "ping while the scan ran (inline or on the event loop)"
        )
        return [{"job_id": "J-run", "status": "running"}]

    monkeypatch.setattr(srv, "_scan_carto_job_files", _blocked_scan)

    async def exercise():
        loop_thread = threading.get_ident()
        ws = _QueueWebSocket([{"action": "carto_list_jobs"}, {"action": "ping"}])

        async def _release_on_pong():
            await ws.pong_seen.wait()
            release.set()

        releaser = asyncio.ensure_future(_release_on_pong())
        pump = asyncio.ensure_future(srv.websocket_handler(ws))
        try:
            await asyncio.wait_for(ws.result_seen.wait(), timeout=PUMP_TIMEOUT_S)
        finally:
            ws.close()
            await asyncio.wait_for(pump, timeout=PUMP_TIMEOUT_S)
            releaser.cancel()
        return ws, loop_thread

    ws, loop_thread = asyncio.run(exercise())
    actions = [f.get("action") for f in ws.sent]
    assert actions.index("pong") < actions.index("carto_list_jobs_result")
    assert scan_threads and scan_threads[0] != loop_thread  # ran in a worker thread
    assert _results(ws) == [
        {
            "action": "carto_list_jobs_result",
            "success": True,
            "jobs": [{"job_id": "J-run", "status": "running"}],
        }
    ]


def test_correlated_reply_behind_a_poll_burst_is_delivered_while_the_scan_runs(monkeypatch):
    """The APPLY shape: a nested Direction-A read is waiting on a request_id
    Future; its reply arrives BEHIND a burst of polls from many tabs.  The
    pump must hand the reply to its waiter while the (slow) scan is still
    running -- pre-fix it sat behind every queued poll."""
    _install_client_stub(monkeypatch)
    release = threading.Event()
    scan_started = threading.Event()

    def _slow_scan(base_dir_str=None, **_view):
        scan_started.set()
        release.wait(timeout=1.0)  # one "slow" scan (pre-fix: blocks the pump)
        return []

    monkeypatch.setattr(srv, "_scan_carto_job_files", _slow_scan)

    async def exercise():
        ws = _QueueWebSocket([{"action": "carto_list_jobs"}])
        pump = asyncio.ensure_future(srv.websocket_handler(ws))
        try:
            await _drain_until(scan_started.is_set, timeout=PUMP_TIMEOUT_S * 2)
            demux = srv._ws_message_queue
            demux.register_correlated_waiter("rq-apply-1")
            # Eleven more tabs poll, THEN the tab's reply to APPLY's nested read.
            for _ in range(11):
                ws.feed({"action": "carto_list_jobs"})
            fed_at = time.monotonic()
            ws.feed(
                {
                    "action": "get_slice_group_config_result",
                    "request_id": "rq-apply-1",
                    "success": True,
                }
            )
            reply = await demux.wait_for_correlated("rq-apply-1", PUMP_TIMEOUT_S)
            # Wall clock, not loop time: a pump blocked in a synchronous scan
            # freezes the loop's own timers, so only the wall clock tells.
            delay = time.monotonic() - fed_at
            scan_still_running = not release.is_set()
        finally:
            release.set()
            ws.close()
            await asyncio.wait_for(pump, timeout=PUMP_TIMEOUT_S * 20)
        return reply, delay, scan_still_running

    reply, delay, scan_still_running = asyncio.run(exercise())
    assert delay < 1.0, f"the correlated reply waited {delay:.1f}s behind the poll burst"
    assert scan_still_running
    assert reply["request_id"] == "rq-apply-1"


# ---------------------------------------------------------------------------
# Coalescing: the load no longer multiplies with the number of tabs
# ---------------------------------------------------------------------------


def test_a_burst_of_polls_runs_one_scan_and_sends_one_broadcast(monkeypatch):
    _install_client_stub(monkeypatch)
    calls: list[float] = []

    def _scan(base_dir_str=None, **_view):
        calls.append(time.monotonic())
        time.sleep(0.05)
        return [{"job_id": "J-run", "status": "running"}]

    monkeypatch.setattr(srv, "_scan_carto_job_files", _scan)

    async def exercise():
        ws = _QueueWebSocket([{"action": "carto_list_jobs"} for _ in range(40)])
        pump = asyncio.ensure_future(srv.websocket_handler(ws))
        await asyncio.wait_for(ws.result_seen.wait(), timeout=PUMP_TIMEOUT_S)
        ws.close()
        await asyncio.wait_for(pump, timeout=PUMP_TIMEOUT_S)
        await asyncio.sleep(0.1)
        return ws

    ws = asyncio.run(exercise())
    assert len(calls) == 1
    assert len(_results(ws)) == 1


def test_polls_are_spaced_and_single_flight(monkeypatch):
    """Unit lock on the admission rule: one scan in flight at a time, and no
    new scan within the spacing window of the last one's start."""
    spawned: list[object] = []

    def _fake_spawn(coro):
        spawned.append(coro)
        coro.close()

    import workflowy_mcp.coherence.operation_cache as opc

    monkeypatch.setattr(opc, "create_isolated_context_task", _fake_spawn)
    clock = {"now": 1000.0}
    monkeypatch.setattr(srv.time, "monotonic", lambda: clock["now"])

    assert srv._carto_list_jobs_poll(object()) is True  # first poll spawns
    assert srv._carto_list_jobs_poll(object()) is False  # in flight -> dropped
    srv._carto_list_jobs_in_flight = False  # the scan finished
    clock["now"] += srv._CARTO_LIST_JOBS_MIN_SPACING_S / 2
    assert srv._carto_list_jobs_poll(object()) is False  # inside the spacing window
    clock["now"] += srv._CARTO_LIST_JOBS_MIN_SPACING_S
    assert srv._carto_list_jobs_poll(object()) is True  # next second's poll spawns
    assert len(spawned) == 2
    # One tab polling once a second is never throttled below 1 Hz.
    assert srv._CARTO_LIST_JOBS_MIN_SPACING_S < 1.0


# ---------------------------------------------------------------------------
# Payload: only the rows the widget shows; wire shapes unchanged
# ---------------------------------------------------------------------------


def test_reply_omits_exactly_the_rows_the_widget_hides(monkeypatch):
    rows = [
        {"job_id": "a", "status": "completed"},
        {"job_id": "b", "status": "Cancelled"},
        {"job_id": "c", "status": "failed"},
        {"job_id": "d", "status": "completed_with_warnings"},
        {"job_id": "e", "status": "running"},
        {"job_id": "f", "status": "some_future_status"},
        {"job_id": "g", "status": None},
    ]
    monkeypatch.setattr(srv, "_scan_carto_job_files", lambda base_dir_str=None, **_view: rows)

    async def exercise():
        ws = _QueueWebSocket()
        await srv._handle_carto_list_jobs(ws)
        return ws

    ws = asyncio.run(exercise())
    (result,) = _results(ws)
    assert set(result) == {"action", "success", "jobs"}
    assert result["success"] is True
    assert [j["job_id"] for j in result["jobs"]] == ["c", "d", "e", "f", "g"]
    assert srv._carto_list_jobs_in_flight is False


def test_scan_failure_keeps_the_failure_frame_and_log_line(monkeypatch):
    logged: list[str] = []

    def _boom(base_dir_str=None, **_view):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(srv, "_scan_carto_job_files", _boom)
    monkeypatch.setattr(srv, "log_event", lambda msg, comp="SERVER": logged.append(msg))
    srv._carto_list_jobs_in_flight = True

    async def exercise():
        ws = _QueueWebSocket()
        await srv._handle_carto_list_jobs(ws)
        return ws

    ws = asyncio.run(exercise())
    assert _results(ws) == [
        {"action": "carto_list_jobs_result", "success": False, "error": "disk gone"}
    ]
    assert logged == ["carto_list_jobs handler error: disk gone"]
    assert srv._carto_list_jobs_in_flight is False  # released even on failure


def test_closed_socket_on_send_releases_the_single_flight(monkeypatch):
    monkeypatch.setattr(srv, "_scan_carto_job_files", lambda base_dir_str=None, **_view: [])
    monkeypatch.setattr(srv, "log_event", lambda *a, **k: None)
    srv._carto_list_jobs_in_flight = True

    class _DeadSocket:
        async def send(self, payload):
            raise ConnectionError("closed")

    asyncio.run(srv._handle_carto_list_jobs(_DeadSocket()))
    assert srv._carto_list_jobs_in_flight is False


# ---------------------------------------------------------------------------
# The once-a-second watcher scans off the event loop too
# ---------------------------------------------------------------------------


def test_job_tracker_scans_in_a_worker_thread(monkeypatch):
    # LE1PTI: N07's tracker no longer scans files. The unchanged thread oracle
    # follows that I/O to the shared async status/widget inventory seam.
    threads: list[int] = []

    def _scan(base_dir_str=None, *, include_inventory=False):
        threads.append(threading.get_ident())
        assert include_inventory
        return {"rows": [], "observations": [], "uncertain": [], "complete": True}

    monkeypatch.setattr(srv, "_scan_carto_job_files", _scan)

    async def exercise():
        loop_thread = threading.get_ident()
        await srv._scan_carto_job_files_async(include_inventory=True)
        return loop_thread

    loop_thread = asyncio.run(exercise())
    assert threads and threads[0] != loop_thread


def test_no_inline_scan_remains_in_the_reader_loop():
    import inspect

    source = inspect.getsource(srv.websocket_handler)
    assert "_scan_carto_job_files" not in source
    source = inspect.getsource(srv._dispatch_ws_work)
    assert "_carto_list_jobs_poll(websocket)" in source


# ---------------------------------------------------------------------------
# Payload size: head-first records, head-only reads, compact rows, a budget
# (job-list fix [ap:FK5O8K]; incident [ap:3VXWG7])
# ---------------------------------------------------------------------------


_TAIL = carto_config.CARTO_RESULT_SUMMARY_TAIL_KEYS
_WIDGET_FIELDS = {"job_id", "kind", "status", "mode", "root_uuid", "detached", "created_at", "updated_at",
                  "progress", "error", "error_kind", "warning_count", "warnings", "heartbeat"}


def _file_evidence(index):
    """One file's retained evidence, shaped like a J-3 folder-refresh row."""
    path = f"/repo/src/pkg/module_{index:04d}.py"
    return {
        "file": path, "status": "refreshed", "changed": index % 7 == 0, "success": True,
        "findings": [{"code": "beacon_unanchored", "severity": "warning", "line": n * 10,
                      "message": f"beacon {n} in {path} has no anchor", "sha256": f"{index:08x}{n:056x}"}
                     for n in range(6)],
        "warnings": [], "events": [{"type": "repair", "file": path, "outcome": "kept"}],
        "check_target": {"verb": "check", "params": {"file_path": path, "min_severity": "info"}},
        "before_sha256": "a" * 64, "after_sha256": "b" * 64,
    }


def _folder_job(job_id, *, status, files, created_at, warnings=0):
    """A folder-refresh record in the pre-fix key order (status after result_summary)."""
    evidence = [_file_evidence(index) for index in range(files)]
    warning_rows = [{"type": "file_refresh_failed", "file": f"/repo/src/w{n}.py", "error": f"warning {n}",
                     "disk_committed": False} for n in range(warnings)]
    return {
        "id": job_id, "type": "CARTO_REFRESH", "mode": "folder",
        "root_uuid": "25a49a99-1dbc-4f66-9cd3-d8a9f60ec976",
        "created_at": created_at, "updated_at": created_at, "execution_model": "daemon_task",
        "detached": False, "cache_refresh_required": True,
        "progress": {"total_files": 522, "completed_files": files, "current_phase": "folder:file-complete",
                     "current_file": "0bdc6f99-aa99-445a-8ac1-2dc80500908f",
                     "current_file_relpath": f"src/pkg/module_{files:04d}.py",
                     "nodes_created": 16, "nodes_updated": 37, "nodes_moved": 398, "nodes_deleted": 0},
        "result_summary": {
            "files_refreshed": files, "errors": [], "warnings_total": warnings,
            "summary_line": f"{files} files", "status": status, "files": evidence,
            "findings": [finding for row in evidence for finding in row["findings"]],
            "warnings": warning_rows, "files_changed": [row["file"] for row in evidence if row["changed"]],
        },
        "error": None, "logs_path": f"/logs/{job_id}.log", "status": status,
        "warnings": warning_rows, "warning_count": warnings, "pid": 4242,
    }


def _write_legacy(path, record):
    """A record written before the fix: plain key order, no layout marker."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)


def _poll():
    async def exercise():
        ws = _QueueWebSocket()
        await srv._handle_carto_list_jobs(ws)
        return ws

    (result,) = _results(asyncio.run(exercise()))
    return result


def _complete_detail(job_id, **params):
    """The detail view's complete evidence: walk its pages, then join them.

    From G2 the list/scan reads are head-only; ``system.job.status(job_id)``
    defaults to the summary view, so complete per-file rows come from
    ``view="detail"``, a page at a time (WaveSpec rev 3 §2.10.1), joined by the
    view's own pure join.
    """
    from workflowy_mcp.carto_job_record import join_carto_job_detail_pages

    pages, offset, slice_offset = [], 0, 0
    while True:
        page = asyncio.run(srv._system_job_status_impl(
            job_id=job_id, view="detail", offset=offset, slice_offset=slice_offset, **params))
        pages.append(page)
        cursor = page.get("detail_page") or {}
        offset, slice_offset = cursor.get("next_offset"), cursor.get("next_slice_offset") or 0
        if offset is None:
            return join_carto_job_detail_pages(pages)


def _trap_shared_reader(monkeypatch, heads, reads, whole):
    """Count the shared reader's record reads, head decodes and whole decodes.

    From G2 the widget scan and ``system.job.status`` read recorded jobs through
    the shared reader (``carto_job_record.read_carto_job_record_cached``); its
    head decode is still ``read_carto_job_record_head``, and a whole decode is
    marked by ``_without_carto_result_tail`` exactly as §2.9/§3.1's counters
    take it.  ``reads`` counts the record file reads themselves, so "no whole
    decode on a repeat poll" is proven the strong way: a decoded record must
    have been read, and an unchanged signature is not read at all.
    """
    import builtins
    import io

    read_head = carto_config.read_carto_job_record_head
    strip = carto_config._without_carto_result_tail
    original_open = builtins.open

    def head(path):
        heads.append(Path(path).name)
        return read_head(path)

    def tail(record):
        # The head path strips nothing; a head-first decode returns above this.
        # A legacy-order (or oversized-head) record falls through to here: one
        # whole decode, once per signature.
        whole.append(record.get("id"))
        return strip(record)

    def counted_open(file, mode="r", *args, **kwargs):
        if str(file).endswith(".json") and "r" in str(mode):
            reads.append(Path(file).name)
        return original_open(file, mode, *args, **kwargs)

    # ``pathlib.Path.open``/``read_text`` go through ``io.open``; plain open()
    # goes through ``builtins.open``.  Both count.
    monkeypatch.setattr(io, "open", counted_open)
    monkeypatch.setattr(builtins, "open", counted_open)
    modules = [widget, carto_config]
    try:
        from workflowy_mcp import carto_job_record as record_module
    except ImportError:  # the shared reader arrives with G2
        record_module = None
    if record_module is not None:
        modules.append(record_module)
    for module in modules:
        if "read_carto_job_record_head" in vars(module):
            monkeypatch.setattr(module, "read_carto_job_record_head", head)
        if "_without_carto_result_tail" in vars(module):
            monkeypatch.setattr(module, "_without_carto_result_tail", tail)


def test_job_records_are_written_head_first_and_read_without_the_evidence(tmp_path):
    path = tmp_path / "job.json"
    record = _folder_job("carto-refresh-folder-1", status="running", files=500,
                         created_at="2026-10-26T09:27:39+00:00", warnings=3)
    assert carto_config.merge_write_carto_job_status(path, dict(record))
    text = path.read_text(encoding="utf-8")
    # The summary leads; result_summary is last, its per-file evidence at its end.
    assert text.startswith('{\n  "record_layout": "summary-head/1",\n  "id": "carto-refresh-folder-1",')
    assert text.index('"status"') < text.index('"error"') < text.index('"progress"') < text.index('"result_summary"')
    assert text.index('"summary_line"') < text.index('"files_changed"') < text.index('"files":[')
    # Key order only: every reader of the full record sees the same values.
    assert json.loads(text) == {"record_layout": carto_config.CARTO_JOB_RECORD_LAYOUT, **record}

    head = carto_config.read_carto_job_record_head(path)
    assert head == {"record_layout": carto_config.CARTO_JOB_RECORD_LAYOUT, **record, "result_summary": {
        key: value for key, value in record["result_summary"].items() if key not in _TAIL}}
    assert len(json.dumps(head)) < len(text) // 100


def test_head_decoder_never_mistakes_a_cut_window_for_a_head():
    """Every prefix of a head-first record either asks for more text or yields
    exactly its head (a window can end mid-number: ``1.`` is not ``1``); a
    record in the old key order is never taken for a head-first one."""
    record = _folder_job("carto-refresh-folder-2", status="running", files=2,
                         created_at="2026-10-26T09:27:39+00:00", warnings=2)
    record["progress"]["ratio"] = 0.625
    head_first = json.dumps(carto_config.carto_job_record_layout(record), indent=2)
    expected = carto_config._without_carto_result_tail(json.loads(head_first))
    for cut in range(len(head_first)):
        found = carto_config._decode_carto_job_head(head_first[:cut], False)
        assert found is None or found == expected, cut
    assert carto_config._decode_carto_job_head(head_first, True) == expected
    legacy = json.dumps(record, indent=2)
    for cut in range(0, len(legacy) + 1, 5):
        assert carto_config._decode_carto_job_head(legacy[:cut], cut >= len(legacy)) in (None, False), cut


def test_widget_reply_for_large_folder_jobs_stays_small_and_compact(tmp_path, monkeypatch):
    """The incident's shape: one running 500-file folder refresh plus four
    finished ones with warnings, two of them written before the fix."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    running_id = "carto-refresh-folder-84cef45f"
    running_path = jobs / f"20261026-092739_{running_id}.json"
    assert carto_config.merge_write_carto_job_status(running_path, _folder_job(
        running_id, status="running", files=500, created_at="2026-10-26T09:27:39+00:00"))
    for day in range(4):
        job_id = f"carto-refresh-folder-old{day}"
        path = jobs / f"2026102{day}-010000_{job_id}.json"
        record = _folder_job(job_id, status="completed_with_warnings", files=500,
                             created_at=f"2026-10-2{day}T01:00:00+00:00", warnings=146)
        if day % 2:
            _write_legacy(path, record)
        else:
            assert carto_config.merge_write_carto_job_status(path, record)
    live = MappingProxyType({running_id: MappingProxyType({
        "id": running_id, "type": "CARTO_REFRESH", "mode": "folder", "path": str(running_path),
        "status": "running", "execution_model": "daemon_task", "detached": False,
        "cache_refresh_required": True, "progress": MappingProxyType({"nodes_moved": 401}),
    })})
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(jobs))
    monkeypatch.setattr(srv, "_carto_live_job_projection", lambda: live)

    result = _poll()
    reply_bytes = len(json.dumps(result))
    # What the pre-fix poll decoded and sent: every row's full result_summary.
    full_records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(jobs.glob("*.json"))]
    assert len(json.dumps(full_records)) > 5_000_000
    assert all(len(record["result_summary"]["files"]) == 500 for record in full_records)
    # From G2 the scan is head-only (no per-file evidence); the evidence is
    # still complete through the detail view, joined across its pages.
    for job_id in [running_id, *(f"carto-refresh-folder-old{day}" for day in range(4))]:
        assert len(_complete_detail(job_id)["result_summary"]["files"]) == 500
    assert reply_bytes < 64 * 1024 < widget.CARTO_WIDGET_REPLY_BUDGET_BYTES
    assert set(result) == {"action", "success", "jobs"}  # under budget: nothing left out
    rows = {row["job_id"]: row for row in result["jobs"]}
    assert len(rows) == 5 and all(set(row) <= _WIDGET_FIELDS for row in rows.values())
    running = rows[running_id]
    assert running["status"] == "running"
    assert running["progress"]["completed_files"] == 500 and running["progress"]["nodes_moved"] == 401
    assert running["progress"]["current_file_relpath"] == "src/pkg/module_0500.py"
    for day in range(4):
        finished = rows[f"carto-refresh-folder-old{day}"]
        assert finished["status"] == "completed_with_warnings" and finished["warning_count"] == 146
        assert len(finished["warnings"]) == 146
        assert finished["warnings"][0] == {"type": "file_refresh_failed", "file": "/repo/src/w0.py", "error": "warning 0"}


def test_repeat_polls_never_decode_the_full_record(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    running_path = jobs / "20261026-092739_carto-refresh-folder-run.json"
    finished_path = jobs / "20261025-010000_carto-refresh-folder-done.json"
    legacy_path = jobs / "20261024-010000_carto-refresh-folder-old.json"
    running = _folder_job("carto-refresh-folder-run", status="running", files=300,
                          created_at="2026-10-26T09:27:39+00:00")
    assert carto_config.merge_write_carto_job_status(running_path, dict(running))
    assert carto_config.merge_write_carto_job_status(finished_path, _folder_job(
        "carto-refresh-folder-done", status="failed", files=300, created_at="2026-10-25T01:00:00+00:00"))
    _write_legacy(legacy_path, _folder_job("carto-refresh-folder-old", status="completed_with_warnings",
                                           files=300, created_at="2026-10-24T01:00:00+00:00", warnings=5))
    live = MappingProxyType({"carto-refresh-folder-run": MappingProxyType({
        "id": "carto-refresh-folder-run", "type": "CARTO_REFRESH", "path": str(running_path), "status": "running"})})
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(jobs))
    monkeypatch.setattr(srv, "_carto_live_job_projection", lambda: live)
    heads, reads, whole = [], [], []
    _trap_shared_reader(monkeypatch, heads, reads, whole)

    first = _poll()
    names = sorted(path.name for path in (running_path, finished_path, legacy_path))
    # One read per record and not one more; whenever the reader decodes a head
    # it does so through the head reader, and only for these records.
    assert sorted(reads) == names
    assert set(heads) <= set(names)
    assert whole == ["carto-refresh-folder-old"]  # the pre-fix record, decoded whole once
    assert _poll() == first
    assert len(reads) == 3 and len(whole) == 1  # unchanged files: no read at all

    # The running job writes progress: only its head is read again.
    running["progress"] = {**running["progress"], "completed_files": 301,
                           "current_file_relpath": "src/pkg/a_much_longer_module_name_0301.py"}
    assert carto_config.merge_write_carto_job_status(running_path, dict(running))
    heads_before, reads_before, whole_before = len(heads), len(reads), len(whole)
    third = _poll()
    assert set(heads[heads_before:]) <= {running_path.name}
    assert reads[reads_before:] == [running_path.name]  # one read, one head, never the whole record again
    assert len(whole) == whole_before
    shown = next(row for row in third["jobs"] if row["job_id"] == "carto-refresh-folder-run")
    assert shown["progress"]["completed_files"] == 301

    # Cleared history: the remaining records stay cached by signature, so the
    # poll answers from what it already holds -- no read, no decode.
    reads_after_third, heads_after_third, whole_after_third = len(reads), len(heads), len(whole)
    legacy_path.unlink()
    assert len(_poll()["jobs"]) == 2
    assert len(reads) == reads_after_third and len(heads) == heads_after_third
    assert len(whole) == whole_after_third  # no decode of a record it already holds


def test_reply_over_budget_omits_the_oldest_finished_jobs_first_and_says_so():
    def finished(n):
        return {"job_id": f"done-{n}", "kind": "CARTO_REFRESH", "status": "completed_with_warnings",
                "created_at": f"2026-10-2{n}T00:00:00", "warning_count": 200, "result_summary": {"files": ["x"] * 999},
                "warnings": [{"type": "w", "message": "m" * 400, "file": f"/f{n}/{i}.py", "extra": "dropped"}
                             for i in range(200)]}

    rows = [finished(0), finished(1), {"job_id": "live", "kind": "CARTO_REFRESH", "status": "running",
                                       "created_at": "2026-10-19T00:00:00", "progress": {"completed_files": 5}}]
    rows += [finished(n) for n in range(2, 6)]
    budget = 200_000
    text = widget.encode_carto_widget_reply(rows, budget=budget)
    reply = json.loads(text)
    assert len(text) <= budget
    # The active job stays although it is the oldest; the oldest finished go first.
    assert [row["job_id"] for row in reply["jobs"]] == ["live", "done-4", "done-5"]
    assert reply["truncated"]["jobs_omitted"] == 4 and reply["truncated"]["budget_bytes"] == budget
    assert reply["truncated"]["warnings_trimmed_to"] is None
    assert all(set(warning) == {"type", "message", "file"} for row in reply["jobs"] for warning in row.get("warnings", []))
    assert all("result_summary" not in row for row in reply["jobs"])

    # An active job's own warnings trim to the tooltip's 40 before it is dropped.
    loud = {**finished(9), "job_id": "loud", "status": "running"}
    reply = json.loads(widget.encode_carto_widget_reply([finished(0), loud], budget=30_000))
    assert [row["job_id"] for row in reply["jobs"]] == ["loud"]
    assert len(reply["jobs"][0]["warnings"]) == 40 and reply["jobs"][0]["warning_count"] == 200
    assert reply["truncated"] == {**reply["truncated"], "jobs_omitted": 1, "warnings_trimmed_to": 40}
