"""G2 protocol/Details/retention witnesses: counters and complete item walks, not timing."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace, MethodType, FunctionType
from contextlib import asynccontextmanager

import pytest

from workflowy_mcp import carto_job_record as rec, carto_job_widget as widget, server as srv, config as cfg


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    # G3 cleared identities last for a daemon lifetime. These independent GC
    # fixtures reuse ids under different tmp directories, so give each test
    # its own lifetime state instead of suppressing a later fixture's create.
    for name in ("_CARTO_CLEARED_JOB_IDS", "_CARTO_CLEARED_JOB_PATHS", "_CARTO_CLEARED_WRITE_LOGGED"):
        monkeypatch.setattr(cfg, name, set(), raising=False)
    for name in ("_carto_startup_failed_jobs", "_carto_final_obligations", "_carto_inprocess_tasks", "_carto_job_controls", "_jobs"):
        monkeypatch.setattr(srv, name, {})
    monkeypatch.setattr(srv, "_governance_holds", lambda: ())
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(srv, "_get_carto_file_refresh_dir", lambda: str(tmp_path / "old-f12"))
    for name, value in (("_carto_list_jobs_in_flight", False), ("_carto_list_jobs_last_started", float("-inf")),
                        ("_carto_list_jobs_generation", None), ("_carto_list_jobs_full_wanted", False),
                        ("_carto_list_jobs_v2_wanted", False), ("_carto_list_jobs_legacy_wanted", False)):
        monkeypatch.setattr(srv, name, value)
    rec._record_cache.clear()
    rec._record_ids.clear()
    rec._detail_cache.clear()


def row():
    return {"job_id": "job", "kind": "CARTO_REFRESH", "status": "running", "mode": "folder",
            "root_uuid": "root", "outcome": "running", "has_errors": False, "has_warnings": True,
            "has_findings": True, "errors_total": 0, "findings_total": 3, "warning_count": 100,
            "warnings": [{"type": "warning", "message": "m" * 600, "hidden": "no"}] * 100,
            "created_at": "old", "updated_at": "new", "detached": False, "pid": 22,
            "error": "not on wire", "result_summary": {"files": ["NEVER"]},
            "progress": {"total_files": 7, "completed_files": 3, "unrendered": 999},
            "heartbeat": {"applicable": False, "state": "unused", "age_seconds": 0, "identity_mismatch": False, "pid": 9}}


def test_protocol2_closed_fields_generation_and_tiny_reply():
    """Before G2 there is no v2 encoder/generation, and no pre-G6-safe tiny action."""
    text, generation = widget.encode_carto_widget_reply_v2([row()])
    value = json.loads(text)
    assert text.startswith('{"action":"carto_list_jobs_result_v2"')
    actual = value["jobs"][0]
    assert set(actual) == set(widget._PROTOCOL_2_FIELDS) | {"warnings", "progress", "heartbeat"}
    assert set(actual["progress"]) == {"total_files", "completed_files"}
    assert set(actual["heartbeat"]) == set(widget._PROTOCOL_2_HEARTBEAT)
    assert len(actual["warnings"]) == 40 and actual["warning_count"] == 100
    assert "hidden" not in actual["warnings"][0]
    assert len(actual["warnings"][0]["message"]) <= 501
    assert len(widget.encode_carto_widget_unchanged(generation).encode()) <= 120
    assert widget.CARTO_LIST_PROTOCOL_2_ACTION != "carto_list_jobs_result"
    assert widget.encode_carto_widget_reply_v2([{**row(), "created_at": "different", "error": "other"}])[1] == generation
    assert widget.encode_carto_widget_reply_v2([{**row(), "progress": {"completed_files": 4}}])[1] != generation


@pytest.mark.parametrize("changing", [False, True])
def test_twenty_pollers_have_two_scans_per_second_and_no_evidence(monkeypatch, changing):
    """Before G2 legacy full frames are sent even when the generation is unchanged."""
    clock, scans, tasks = [0.0], [], []
    monkeypatch.setattr(srv.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(srv.daemon_lifecycle, "active_lifecycle", lambda: None)
    from workflowy_mcp.coherence import operation_cache
    monkeypatch.setattr(operation_cache, "create_isolated_context_task", lambda coro: tasks.append(asyncio.create_task(coro)))
    async def scan(**kwargs):
        scans.append(clock[0])
        return [{**row(), "progress": {"completed_files": int(clock[0]) if changing else 0}}]
    async def cpu(_phase, function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(srv, "_scan_carto_job_files_async", scan)
    monkeypatch.setattr(srv, "run_carto_cpu", cpu)
    async def exercise():
        ws, generation = Socket(), None
        for tick in range(240):
            clock[0] = tick / 4
            for _ in range(20):
                srv._carto_list_jobs_poll(ws, {"since": generation})
            await asyncio.sleep(0)
            if ws.sent:
                generation = json.loads(ws.sent[-1]).get("generation")
        await asyncio.gather(*tasks)
        return ws
    ws = asyncio.run(exercise())
    assert len(scans) == 120
    assert all(sum(int(stamp) == second for stamp in scans) <= 2 for second in range(60))
    values = [json.loads(text) for text in ws.sent]
    assert len(values) == 120
    assert sum(not value.get("unchanged") for value in values) == (60 if changing else 1)
    assert all(len(text.encode()) <= 120 for text, value in zip(ws.sent, values) if value.get("unchanged"))


def test_coalesced_stale_since_still_gets_full_and_legacy_is_byte_identical(monkeypatch):
    """Before G2 a stale since is discarded by the single-flight admission path."""
    async def exercise():
        release, entered = asyncio.Event(), asyncio.Event()
        async def scan(**kwargs):
            entered.set()
            await release.wait()
            return [row()]
        async def cpu(_phase, function, *args, **kwargs):
            return function(*args, **kwargs)
        monkeypatch.setattr(srv, "_scan_carto_job_files_async", scan)
        monkeypatch.setattr(srv, "run_carto_cpu", cpu)
        monkeypatch.setattr(srv.daemon_lifecycle, "active_lifecycle", lambda: None)
        generation = widget.encode_carto_widget_reply_v2([row()])[1]
        srv._carto_list_jobs_generation = generation
        ws = Socket()
        assert srv._carto_list_jobs_poll(ws, {"since": generation})
        await entered.wait()
        assert not srv._carto_list_jobs_poll(ws, {"since": "stale"})
        release.set()
        while srv._carto_list_jobs_in_flight:
            await asyncio.sleep(0)
        assert "jobs" in json.loads(ws.sent[-1])
        ws = Socket()
        await srv._handle_carto_list_jobs(ws)
        assert ws.sent[-1] == widget.encode_carto_widget_reply([row()])
    asyncio.run(exercise())


def install_details(tmp_path, *, layout="v2", findings=5000, run_warnings=100):
    path = tmp_path / "20261027-000000_problems.json"
    file_row = {"file": "/repo/problem.py", "status": "completed", "changed": False, "warnings": [], "events": [],
                "findings": [{"code": "finding", "severity": "info", "message": "x" * 500, "line": i,
                              "finding_id": str(i)} for i in range(findings)]}
    warnings = [{"message": f"outside FILE {i}"} for i in range(run_warnings)]
    summary = {"successful": 1, "failed": 0, "skipped": 0, "warnings_total": run_warnings,
               "findings_total": findings, "summary_line": "complete", "files": [file_row], "warnings": warnings}
    value = {"id": "problems", "type": "CARTO_BULK_APPLY", "status": "completed_with_warnings",
             "warning_count": run_warnings, "result_summary": summary}
    if layout == "v2":
        folder = path.with_suffix("")
        folder.mkdir()
        lines = [{"k": "row", "sec": "files", "row": file_row, "nodes": 0}]
        lines += [{"k": "item", "key": "run_warnings", "v": warning} for warning in warnings]
        (folder / "files.jsonl").write_text("".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines))
        (folder / "nodes.jsonl").write_text("")
        value.update(record_layout="summary-head/2", evidence={"files": {"path": "files.jsonl", "rows": 1},
                                                              "nodes": {"path": "nodes.jsonl", "rows": 0}})
        value["result_summary"] = {key: item for key, item in summary.items() if key not in {"files", "warnings"}}
        path.write_text(json.dumps(value))
    elif layout == "v1":
        from workflowy_mcp.config import merge_write_carto_job_status
        assert merge_write_carto_job_status(path, value)
    else:
        path.write_text(json.dumps(value))
    return path


@pytest.mark.parametrize("layout", ["v2", "v1", "legacy"])
def test_details_reaches_every_finding_and_run_warning_once_and_targets_one_tab(tmp_path, layout):
    """Before G2 there is no Details handler/run row or per-item continuation."""
    install_details(tmp_path, layout=layout)
    async def exercise():
        ws, cursor = Socket(), {"offset": 0, "item_offset": 0}
        while cursor is not None:
            await srv._handle_carto_job_detail({"request_id": "click", "endpoint_id": "tab-A", "job_id": "problems",
                                               "filter": "problems", **cursor}, ws)
            frame = json.loads(ws.sent[-1])
            assert frame["success"] and frame["request_id"] == "click" and frame["target_endpoint_id"] == "tab-A"
            assert len(ws.sent[-1].encode()) <= 524288
            assert frame["page"]["evidence_version"]
            cursor = frame["page"]["next"]
        return [json.loads(text) for text in ws.sent]
    frames = asyncio.run(exercise())
    assert len(frames) > 1
    assert [item["finding_id"] for frame in frames for row in frame["rows"] for item in row["findings"]] == [str(i) for i in range(5000)]
    assert [item["message"] for frame in frames for row in frame["rows"] if row["sec"] == "run" for item in row["warnings"]] == [f"outside FILE {i}" for i in range(100)]


def test_details_second_click_reads_no_file_and_growth_starts_at_committed_offset(tmp_path, monkeypatch):
    """Before G2 a second Details read has no signature/append-offset index."""
    path = install_details(tmp_path, findings=2)
    data = {"job_id": "problems", "filter": "problems"}
    first = rec.read_carto_problem_page(tmp_path, data)
    open_original, reads = Path.open, []
    def spy(file, mode="r", *args, **kwargs):
        handle = open_original(file, mode, *args, **kwargs)
        if "r" in mode:
            reads.append((str(file), handle.tell()))
        return handle
    monkeypatch.setattr(Path, "open", spy)
    assert rec.read_carto_problem_page(tmp_path, data) == first
    assert reads == []
    side = path.with_suffix("") / "files.jsonl"
    before = side.stat().st_size
    line = json.dumps({"k": "row", "sec": "files", "row": {"file": "later.py", "status": "failed", "error": "later"}}, separators=(",", ":")) + "\n"
    with open_original(side, "a") as handle:
        handle.write(line)
    seeks = []
    def seek_spy(file, mode="r", *args, **kwargs):
        handle = open_original(file, mode, *args, **kwargs)
        if file == side and mode == "rb":
            old = handle.seek
            def seek(offset, *a):
                seeks.append(offset)
                return old(offset, *a)
            handle.seek = seek
        return handle
    monkeypatch.setattr(Path, "open", seek_spy)
    second = rec.read_carto_problem_page(tmp_path, data)
    assert second["page"]["evidence_version"] != first["page"]["evidence_version"]
    assert seeks == [before]
    assert any(row["file"] == "later.py" for row in second["rows"])


def test_unknown_details_job_still_targets_requesting_tab(tmp_path):
    """Before G2 unknown Details requests have no correlated/targeted answer."""
    async def exercise():
        ws = Socket()
        await srv._handle_carto_job_detail({"job_id": "absent", "request_id": "rq", "endpoint_id": "one-tab"}, ws)
        return json.loads(ws.sent[0])
    frame = asyncio.run(exercise())
    assert not frame["success"] and frame["request_id"] == "rq" and frame["target_endpoint_id"] == "one-tab"


def test_broker_correlated_details_reply_reaches_only_its_origin():
    """Existing routing pin: this must already pass at the base; no broker change."""
    from workflowy_mcp import broker as brk
    from test_wslive_broker_endpoint_liveness import RecordingWS
    async def exercise():
        broker = brk.Broker(required_token="tok")
        first, other, mcp = RecordingWS(), RecordingWS(), RecordingWS()
        broker.register_browser_connection(first, "one")
        broker.register_browser_connection(other, "two")
        broker.register_endpoint(first, "ep-one", None, None, None, "first")
        broker.register_endpoint(other, "ep-two", None, None, None, "other")
        broker.track_request("click", first)
        frame = {"action": "carto_job_detail_result", "request_id": "click", "target_endpoint_id": "ep-one",
                 "success": True, "rows": []}
        mcp.feed({"action": "hello", "role": "mcp", "client_id": "mcp", "token": "tok"})
        mcp.feed(frame)
        mcp.done()
        await brk._handle_connection(broker, mcp)
        assert first.sent_with(action="carto_job_detail_result") == [frame]
        assert other.sent_with(action="carto_job_detail_result") == []
    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["etch", "purge", "merge.persist"])
def test_cancelled_generic_jobs_keep_the_cancelled_status(monkeypatch, kind):
    """CF-137 branch 10: before G2 CancelledError is misleadingly recorded failed."""
    from workflowy_mcp.coherence import operation_cache, admission
    @asynccontextmanager
    async def boundary(*args, **kwargs):
        yield
    async def allow(*args, **kwargs):
        return None
    monkeypatch.setattr(operation_cache, "cache_operation_boundary", boundary)
    monkeypatch.setattr(admission, "require_coherence_mutation_admission", allow)
    monkeypatch.setattr(srv, "get_client", lambda: object())
    monkeypatch.setattr(srv.daemon_lifecycle, "active_lifecycle", lambda: None)
    async def exercise():
        started, blocked = asyncio.Event(), asyncio.Event()
        async def work(_job_id):
            started.set()
            await blocked.wait()
            return {"success": True}
        launch = await srv._start_background_job(kind, {}, work)
        await started.wait()
        answer = await srv._system_job_cancel_impl(launch["job_id"])
        assert answer["success"]
        job = srv._jobs[launch["job_id"]]
        await job["_task"]
        assert job["status"] == "cancelled" and job["finished_at"]
    asyncio.run(exercise())


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed", "completed_with_warnings", "running", "future"])
def test_debug_gc_ages_all_terminal_debug_but_keeps_failure_evidence(tmp_path, status):
    """Before R-c failed/warning folders' file_refresh diagnostics never age out."""
    from workflowy_mcp.config import merge_write_carto_job_status, is_carto_job_terminal
    path = tmp_path / "20000101-000000_retained.json"
    payload = {"id": "retained", "type": "CARTO_REFRESH", "status": status,
               "created_at": "2000-01-01T00:00:00", "updated_at": "2000-01-01T00:00:00"}
    assert merge_write_carto_job_status(path, payload)
    folder = path.with_suffix("")
    debug = folder / "file_refresh"
    debug.mkdir(parents=True)
    (debug / "source.json").write_text("debug only")
    (folder / "files.jsonl").write_text("complete evidence\n")
    srv._gc_carto_jobs(str(tmp_path), max_age_seconds=100)
    assert debug.exists() is (not is_carto_job_terminal(status))
    if status in {"failed", "completed_with_warnings", "running", "future"}:
        assert path.exists() and (folder / "files.jsonl").read_text() == "complete evidence\n"
    else:
        assert not path.exists()


def test_gc_call_uses_configured_age(monkeypatch, tmp_path):
    """Before R-d the one GC call silently uses its hard-coded 3600-second default."""
    seen = []
    monkeypatch.setattr(srv, "get_server_config", lambda: SimpleNamespace(retention=SimpleNamespace(carto_job_max_age_seconds=37)))
    monkeypatch.setattr(srv, "_carto_gc_protection", lambda: frozenset())
    monkeypatch.setattr(srv, "_carto_gc_paths", lambda _ids: frozenset())
    monkeypatch.setattr(srv, "_gc_carto_jobs", lambda *args, **kwargs: seen.append(kwargs))
    asyncio.run(srv._run_carto_job_gc_pass())
    assert seen[0]["max_age_seconds"] == 37


@pytest.mark.parametrize("giant", [False, True])
def test_complete_16mb_walk_reads_evidence_about_twice_and_caches_no_rows(tmp_path, monkeypatch, giant):
    """Before G2 there is no bounded, offset-backed complete-detail page walk."""
    path = tmp_path / "20261027-000000_bytes.json"
    folder = path.with_suffix("")
    folder.mkdir()
    count, width = (1, 16 * 1024 * 1024) if giant else (256, 64 * 1024)
    marker = "EVIDENCE_PAYLOAD_NEVER_RETAINED:"
    payload = marker + "x" * (width - len(marker))
    with (folder / "files.jsonl").open("w") as handle:
        for ordinal in range(count):
            value = {"file": f"/repo/{ordinal}.py", "status": "completed", "changed": False,
                     "findings": [], "warnings": [], "events": [], "payload": payload,
                     "unknown": {"nullable": None, "empty": []}}
            handle.write(json.dumps({"k": "row", "sec": "files", "row": value}, separators=(",", ":")) + "\n")
    value = {"record_layout": "summary-head/2", "id": "bytes", "type": "CARTO_REFRESH", "mode": "folder",
             "status": "completed", "evidence": {"files": {"path": "files.jsonl", "rows": count}, "nodes": None},
             "result_summary": {"successful": count, "failed": 0, "skipped": 0, "findings_total": 0,
                                "warnings_total": 0, "errors_total": 0}}
    path.write_text(json.dumps(value))
    original, reads = Path.open, []
    def counting(file, mode="r", *args, **kwargs):
        handle = original(file, mode, *args, **kwargs)
        if file.name == "files.jsonl" and "r" in mode:
            old = handle.read
            def read(*a, **kw):
                result = old(*a, **kw)
                reads.append(len(result.encode() if isinstance(result, str) else result))
                return result
            handle.read = read
        return handle
    monkeypatch.setattr(Path, "open", counting)
    async def exercise():
        pages, cursor = [], {"offset": 0, "slice_offset": 0}
        while cursor is not None:
            page = await srv._system_job_status_impl("bytes", view="detail", **cursor)
            assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()) <= 524288
            pages.append(page)
            meta = page["detail_page"]
            cursor = ({"offset": meta["next_offset"], "slice_offset": meta["next_slice_offset"]}
                      if meta["next_offset"] is not None else None)
        return rec.join_carto_job_detail_pages(pages)
    result = asyncio.run(exercise())
    assert len(result["result_summary"]["files"]) == count
    assert all(item["payload"] == payload and item["unknown"] == {"nullable": None, "empty": []}
               for item in result["result_summary"]["files"])
    size = (folder / "files.jsonl").stat().st_size
    assert reads[0] == size
    assert sum(reads) <= size * 2.05
    assert max(reads[1:]) <= 524288
    # Offset plans, counts, previews and slim rows may survive; the complete
    # payload must not. Follow only instance-owned containers, never module or
    # function globals, so this is a structural memory witness, not a GC timer.
    seen = set()
    def visit(obj):
        if id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, str):
            assert marker not in obj
        elif isinstance(obj, dict):
            for key, item in obj.items():
                visit(key)
                visit(item)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                visit(item)
        elif isinstance(obj, MethodType):
            visit(obj.__self__)
        elif isinstance(obj, FunctionType):
            for cell in obj.__closure__ or ():
                visit(cell.cell_contents)
        elif type(obj).__module__ in {"workflowy_mcp.carto_job_record", "workflowy_mcp.carto_job_paging"}:
            for item in getattr(obj, "__dict__", {}).values():
                visit(item)
            for name in getattr(type(obj), "__slots__", ()):
                visit(getattr(obj, name, None))
    visit(rec._detail_cache)
