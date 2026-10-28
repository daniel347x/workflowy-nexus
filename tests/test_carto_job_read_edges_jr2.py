"""G2 differential edge faces: FILE shapes, receipt-only evidence and view selection.

These exercise the public one-job path, not just the pure fold. Before G2 the
new views do not exist; the receipt-only and ignored-selector cells also catch
the incomplete first G2 integration without substituting any producer.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_carto_job_record_jr2 import _fresh_reader_and_tables, _joined, _status  # noqa: F401
from workflowy_mcp import carto_job_record as record, server as srv


def _install(tmp_path, monkeypatch, payload, *, layout="v2", lines=(), nodes=()):
    job_id = payload.get("id", "edge")
    path = tmp_path / f"20261027-000000_{job_id}.json"
    payload = dict(payload)
    if layout == "v2":
        payload = {"record_layout": "summary-head/2", **payload}
        folder = path.with_suffix("")
        folder.mkdir(exist_ok=True)
        for name, values in (("files", lines), ("nodes", nodes)):
            if values is not None:
                (folder / f"{name}.jsonl").write_text("".join(
                    json.dumps(value, separators=(",", ":")) + "\n" for value in values), encoding="utf-8")
        payload.setdefault("evidence", {"files": {"path": "files.jsonl", "rows": sum(
            value.get("k") == "row" for value in lines or ())}, "nodes": (
                {"path": "nodes.jsonl", "rows": len(nodes)} if nodes is not None else None)})
    elif layout == "v1":
        payload = {"record_layout": "summary-head/1", **payload}
    else:
        payload = {"result_summary": payload["result_summary"], **payload}
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(tmp_path))
    return path


@pytest.mark.parametrize("layout", ["v2", "v1", "legacy"])
def test_single_file_detail_is_the_complete_file_result_not_an_aggregate(tmp_path, monkeypatch, layout):
    """CF-168: a FILE keeps all findings (>200), nulls, unknown fields and order."""
    findings = [{"code": "finding", "finding_id": str(i), "severity": "info"} for i in range(260)]
    row = {"file": "/repo/file.py", "status": "partial_section", "success": True,
           "changed": False, "findings": findings, "warnings": [], "events": [],
           "unknown_list": [None, [], {}], "reconcile_children": None,
           "projection": {"findings": findings, "reconcile_children": None}, "unknown": {"x": None}}
    summary = ({key: value for key, value in row.items() if not isinstance(value, list)}
               if layout == "v2" else row)
    stored = {**row, "projection": None}
    _install(tmp_path, monkeypatch, {"id": "edge", "type": "CARTO_REFRESH", "mode": "file",
        "status": "completed_with_warnings", "result_summary": summary}, layout=layout,
        lines=[{"k": "row", "sec": "files", "row": stored,
                "proj": ["findings", "reconcile_children"]}], nodes=None)
    detail = _joined("edge", files_limit=7)
    assert detail["result_summary"] == row
    assert list(detail["result_summary"]) == list(row)
    rows = _status(job_id="edge", files="all")["result_summary"]["files"]
    assert rows == [row]


@pytest.mark.parametrize("status", ["running", "failed"])
def test_nodes_only_bulk_evidence_is_not_mistaken_for_a_missing_bundle(tmp_path, monkeypatch, status):
    """An abrupt end before the first file line still carries its landed receipts."""
    receipt = {"node_id": "node-1", "success": True, "satisfied": True, "changed": True}
    _install(tmp_path, monkeypatch, {"id": "edge", "type": "CARTO_BULK_APPLY", "status": status,
        "result_summary": {}, "in_flight": {"file": "/repo/file.py", "status": "failed",
            "changed": False, "findings": [], "warnings": [], "events": [],
            "error": "File attempt interrupted before completion", "base_lines": 0}},
        lines=None, nodes=[{"k": "node", "file": "/repo/file.py", "row": receipt}])
    detail = _joined("edge")
    assert detail["result_summary"]["node_outcomes"] == [receipt]
    row = detail["result_summary"]["files"][0]
    assert row["node_outcomes"] == [receipt] and row["nodes_satisfied"] == 1 and row["changed"] is True
    assert row["status"] == ("failed" if status == "failed" else "completed")


@pytest.mark.parametrize("layout", ["v2", "v1", "legacy"])
def test_problem_filter_includes_a_failed_node_receipt_even_on_a_clean_row(tmp_path, monkeypatch, layout):
    """The problem predicate includes bulk receipts, not just row warnings/status."""
    receipt = {"node_id": "rejected", "success": False, "error": "not satisfied"}
    row = {"file": "/repo/file.py", "status": "completed", "changed": False,
           "findings": [], "warnings": [], "events": [], "nodes_satisfied": 0,
           "node_outcomes": [receipt]}
    _install(tmp_path, monkeypatch, {"id": "edge", "type": "CARTO_BULK_APPLY",
        "status": "completed", "result_summary": ({"files": [row]} if layout != "v2" else {})},
        layout=layout, lines=[{"k": "row", "sec": "files", "row": {**row, "node_outcomes": None}, "nodes": 1}],
        nodes=[{"k": "node", "file": "/repo/file.py", "row": receipt}])
    reply = _status(job_id="edge", files="problems")
    assert len(reply["result_summary"]["files"]) == 1
    assert reply["result_summary"]["files"][0]["node_outcomes"] == [receipt]


@pytest.mark.parametrize("options,ignored", [
    ({"files": "all", "view": "detail"}, {"view"}),
    ({"file": "/repo/file.py", "files": "problems", "view": "detail"}, {"files", "view"}),
])
def test_overridden_selectors_are_served_and_each_ignored_one_warns(tmp_path, monkeypatch, options, ignored):
    """§2.10: file > files > view, each ignored selector is visible, never refused."""
    row = {"file": "/repo/file.py", "status": "completed", "changed": False, "findings": []}
    _install(tmp_path, monkeypatch, {"id": "edge", "type": "CARTO_REFRESH", "mode": "folder",
        "status": "completed", "result_summary": {}}, lines=[{"k": "row", "sec": "files", "row": row}], nodes=None)
    reply = _status(job_id="edge", **options)
    assert reply["success"] and reply["result_summary"]["files"][0]["file"] == row["file"]
    warnings = reply.get("parameter_warnings", [])
    assert all(any(f"Ignored {key}" in warning for warning in warnings) for key in ignored)


def test_unknown_values_and_one_job_options_without_an_id_warn_instead_of_refusing(tmp_path, monkeypatch):
    """The closed public schema still accepts unknown string values as summary requests."""
    _install(tmp_path, monkeypatch, {"id": "edge", "type": "CARTO_REFRESH", "mode": "folder",
        "status": "completed", "error_extra": {"unknown": [None, []]}, "result_summary": {}}, nodes=None)
    before = _status(job_id="edge")
    after = _status(job_id="edge", view="not-a-view")
    warnings = after.pop("parameter_warnings")
    assert warnings and after == before
    normal = _status()
    ignored = _status(view="detail", files_offset=9)
    warnings = ignored.pop("parameter_warnings")
    assert len(warnings) == 2 and ignored == normal
    declared = srv.SYSTEM_REGISTRY.resolve("job.status").params_schema
    assert declared["additionalProperties"] is False


def test_ignored_one_job_options_share_the_existing_inventory_byte_budget(tmp_path, monkeypatch):
    """Warnings annotate the bounded list, not an unbudgeted append after formatting."""
    from workflowy_mcp import job_status_view
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(srv, "_jobs", {str(i): {"job_id": f"job-{i:03d}-" + "x" * 400,
        "kind": "test", "status": "running"} for i in range(100)})
    normal = _status()
    budget = len(json.dumps(normal, ensure_ascii=False).encode())
    monkeypatch.setattr(job_status_view, "JOB_LIST_MAX_BYTES", budget)
    annotated = _status(view="detail")
    assert annotated["parameter_warnings"] == ["Ignored view without job_id"]
    assert len(json.dumps(annotated, ensure_ascii=False).encode()) <= budget
    assert annotated["returned"] < normal["returned"]


def test_moved_values_and_unknown_lines_torn_tail_survive_the_public_page_join(tmp_path, monkeypatch, caplog):
    """Every complete line is evidence; bad/torn lines are counted, never a refusal."""
    row = {"file": "/repo/file.py", "status": "completed", "changed": False, "findings": []}
    value = "λ" * 15000
    path = _install(tmp_path, monkeypatch, {"id": "edge", "type": "CARTO_REFRESH", "mode": "folder",
        "status": "completed", "error_extra": {"$evidence": "files.jsonl", "bytes": 30002},
        "result_summary": {}}, lines=[{"k": "row", "sec": "files", "row": row},
            {"k": "set", "key": ["error_extra"], "v": value}, {"k": "future", "payload": [None, []]}], nodes=None)
    with (path.with_suffix("") / "files.jsonl").open("ab") as handle:
        handle.write(b'not-json\n{"k":"row"')
    first = _joined("edge")
    assert first["error_extra"] == value
    assert first["torn_tail_bytes"] == len(b'{"k":"row"') and first["unreadable_lines"] == 1
    assert first["unrecognized_lines"] == [{"k": "future", "payload": [None, []]}]
    count = len(caplog.records)
    assert _joined("edge") == first
    assert len(caplog.records) == count


def test_one_cached_detail_can_serve_different_row_limits_honestly(tmp_path, monkeypatch):
    """The cached pager borrows sources, not the first request's presentation limit."""
    rows = [{"file": f"/repo/{i}.py", "status": "completed", "findings": []} for i in range(9)]
    _install(tmp_path, monkeypatch, {"id": "edge", "type": "CARTO_REFRESH", "mode": "folder",
        "status": "completed", "result_summary": {}},
        lines=[{"k": "row", "sec": "files", "row": row} for row in rows], nodes=None)
    for limit in (2, 5):
        page = _status(job_id="edge", view="detail", files_limit=limit)
        assert page["files_page"]["limit"] == limit
        assert len(page["result_summary"]["files"]) == limit


@pytest.mark.parametrize("layout", ["v2", "v1", "legacy"])
def test_file_view_keeps_receipts_attributed_to_the_active_file_without_a_path(tmp_path, monkeypatch, layout):
    """A receipt's missing source_path is not permission to omit it from file=."""
    receipt = {"node_id": "implicit", "success": True, "satisfied": True, "changed": False}
    row = {"file": "/repo/file.py", "status": "completed", "changed": False,
           "findings": [], "warnings": [], "events": [], "node_outcomes": [receipt], "nodes_satisfied": 1}
    summary = {"files": [row], "node_outcomes": [receipt]} if layout != "v2" else {}
    _install(tmp_path, monkeypatch, {"id": "edge", "type": "CARTO_BULK_APPLY",
        "status": "completed", "result_summary": summary}, layout=layout,
        lines=[{"k": "row", "sec": "files", "row": {**row, "node_outcomes": None}, "nodes": 1}],
        nodes=[{"k": "node", "file": row["file"], "row": receipt}])
    reply = _status(job_id="edge", file=row["file"])
    assert reply["result_summary"]["files"][0]["node_outcomes"] == [receipt]
    assert reply["result_summary"]["node_outcomes"] == [receipt]


def test_detail_with_ignored_cursor_warning_still_obeys_the_whole_reply_budget(tmp_path, monkeypatch):
    """Parameter warnings take envelope room, never room outside the 512-KiB reply."""
    row = {"file": "/repo/file.py", "status": "completed", "findings": [], "payload": "x" * 800000}
    _install(tmp_path, monkeypatch, {"id": "edge", "type": "CARTO_REFRESH", "mode": "folder",
        "status": "completed", "result_summary": {}}, lines=[{"k": "row", "sec": "files", "row": row}], nodes=None)
    from test_carto_job_record_jr2 import _pages
    pages = _pages("edge", files_offset=7)
    assert all(page["parameter_warnings"] for page in pages)
    assert all(len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()) <= 524288 for page in pages)
    assert record.join_carto_job_detail_pages(pages)["result_summary"]["files"][0]["payload"] == row["payload"]


def test_file_and_files_views_return_the_same_aggregate_row(tmp_path, monkeypatch):
    """Aggregate defaults belong to every primary-row view, but not to FILE results."""
    row = {"file": "/repo/file.py", "status": "completed", "changed": False, "findings": []}
    _install(tmp_path, monkeypatch, {"id": "edge", "type": "CARTO_BULK_APPLY",
        "status": "completed", "result_summary": {}}, lines=[{"k": "row", "sec": "files", "row": row, "nodes": 0}])
    detail = _joined("edge")["result_summary"]["files"]
    all_rows = _status(job_id="edge", files="all")["result_summary"]["files"]
    file_rows = _status(job_id="edge", file=row["file"])["result_summary"]["files"]
    assert all_rows == file_rows == detail
    assert [list(value) for value in all_rows] == [list(value) for value in file_rows]


def test_empty_stream_is_not_missing_and_a_lost_bundle_is_visible_in_all_evidence_views(tmp_path, monkeypatch):
    """Zero append counts do not accuse an empty stream; copied-alone records say so."""
    payload = {"id": "edge", "type": "CARTO_REFRESH", "mode": "folder", "status": "failed",
               "result_summary": {"message": "failed before the first file"}}
    path = _install(tmp_path, monkeypatch, payload, lines=None, nodes=None)
    assert "evidence_missing" not in _joined("edge")
    path.with_suffix("").rmdir()
    for options in ({"view": "detail"}, {"files": "all"}, {"file": "/repo/file.py"}):
        assert _status(job_id="edge", **options)["evidence_missing"] is True


def test_details_wide_event_is_one_reachable_clipped_item_not_an_endless_cursor(tmp_path, monkeypatch):
    """Details' named text preview cannot make an oversized event stall Load more."""
    row = {"file": "/repo/file.py", "status": "completed", "findings": [],
           "events": [{str(i): "x" * 2000 for i in range(600)}]}
    _install(tmp_path, monkeypatch, {"id": "edge", "type": "CARTO_REFRESH", "mode": "folder",
        "status": "completed", "result_summary": {}}, lines=[{"k": "row", "sec": "files", "row": row}], nodes=None)
    page = record.read_carto_problem_page(tmp_path, {"job_id": "edge"})
    assert page["page"]["next"] is None
    assert len(page["rows"]) == 1 and len(page["rows"][0]["events"]) == 1
    assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()) <= 524288
    assert _joined("edge")["result_summary"]["events"] == row["events"]


def test_details_applies_its_named_text_preview_to_unknown_status_strings_too():
    """An unknown string still warns/runs; its slim presentation cannot break the page budget."""
    source = {"file": "/repo/file.py", "status": "future" * 100000, "findings": []}
    row = record._slim_detail_row(source)
    header = record._problem_header({"status": source["status"], "result_summary": {}})
    assert len(row["status"]) == len(header["status"]) == 2000
    assert header["outcome"] == "running" and source["status"] == "future" * 100000


@pytest.mark.parametrize("layout", ["v1", "legacy"])
def test_legacy_details_does_not_invent_run_warnings_when_every_warning_belongs_to_a_row(tmp_path, monkeypatch, layout):
    """CF-165's k<=0 face: per-file warnings occur once, without a fake run row."""
    warning = {"message": "one row warning"}
    row = {"file": "/repo/file.py", "status": "completed", "findings": [], "warnings": [warning]}
    _install(tmp_path, monkeypatch, {"id": "edge", "type": "CARTO_BULK_APPLY", "status": "completed_with_warnings",
        "result_summary": {"files": [row], "warnings": [warning]}}, layout=layout)
    page = record.read_carto_problem_page(tmp_path, {"job_id": "edge"})
    assert [value["sec"] for value in page["rows"]] == ["files"]
    assert page["rows"][0]["warnings"] == [warning]


def test_gc_uses_the_current_terminal_class_not_a_stale_clean_candidate(tmp_path, monkeypatch):
    """R-c keeps failed/warning evidence even when the candidate snapshot was clean."""
    old = {"id": "edge", "type": "CARTO_REFRESH", "status": "completed",
           "created_at": "2000-01-01T00:00:00", "updated_at": "2000-01-01T00:00:00",
           "result_summary": {}}
    path = _install(tmp_path, monkeypatch, old, nodes=None)
    saved = json.loads(path.read_text())
    path.write_text(json.dumps({**saved, "status": "failed"}))
    debug = path.with_suffix("") / "file_refresh"
    debug.mkdir()
    (debug / "diagnostic.json").write_text("debug")
    evidence = path.with_suffix("") / "files.jsonl"
    before = evidence.read_bytes()
    monkeypatch.setattr(srv, "_get_carto_file_refresh_dir", lambda: str(tmp_path / "old-f12"))
    srv._gc_carto_jobs(str(tmp_path), max_age_seconds=100, candidates={"rows": [(path, old)], "errors": []})
    assert not debug.exists()
    assert path.exists() and evidence.read_bytes() == before


def test_ten_inventory_cleanup_startup_and_cancel_passes_do_not_read_large_evidence(tmp_path, monkeypatch, record_property):
    """§3.1's real-size trap: ~60-MB /1, its legacy twin and a 16-MiB /2 sidecar."""
    import asyncio
    import re
    from collections import Counter
    from workflowy_mcp import config
    from test_carto_job_record_jr2 import _open_paths, _reset_reader_caches
    source = Path("/mnt/c/tmp/mci-066-jr2/fixtures-helper/20261026-114908_carto-refresh-folder-844471b0.json")
    if source.exists():
        raw = re.sub(rb'("status"\s*:\s*)"[^"]*"', rb'\1"failed"', source.read_bytes(), count=1)
        source_data = json.loads(raw)
        record_property("large_record_source", "read-only copy of the 59,555,434-byte /1 snapshot")
    else:
        source_data = {"id": "large", "type": "CARTO_REFRESH", "status": "failed",
                       "result_summary": {"failed": 1, "files": [{"file": "large.py", "payload": "x" * 60_000_000}]}}
        raw = json.dumps(config.carto_job_record_layout(source_data)).encode()
        record_property("large_record_source", "portable 60-MB equivalent when the private copy is absent")
    path1 = tmp_path / f"20261027-000000_{source_data['id']}.json"
    path1.write_bytes(raw)
    legacy = {"result_summary": source_data["result_summary"], **source_data, "id": "large-legacy"}
    legacy.pop("record_layout", None)
    path2 = tmp_path / "20261027-000000_large-legacy.json"
    path2.write_text(json.dumps(legacy, indent=2), encoding="utf-8")
    ids = (source_data["id"], "large-legacy", "edge")
    assert path1.stat().st_size > 55_000_000 and path2.stat().st_size > 55_000_000
    del raw, source_data, legacy
    path3 = _install(tmp_path, monkeypatch, {"id": "edge", "type": "CARTO_REFRESH", "status": "failed",
        "result_summary": {"failed": 1}}, lines=[{"k": "row", "sec": "files", "row": {
            "file": "large.py", "payload": "x" * (16 * 1024 * 1024)}}], nodes=None)
    _reset_reader_caches()
    opened = _open_paths(monkeypatch)
    loads, strips = [], []
    original_loads, original_strip = json.loads, config._without_carto_result_tail
    def counted_loads(text, *args, **kwargs):
        if len(text) > 55_000_000:
            # Fail fast under the base reader substitution instead of paying
            # for gigabytes of repeated JSON decodes just to count them later.
            assert not loads, "a second whole-record decode reached the structural trap"
        value = original_loads(text, *args, **kwargs)
        if len(text) > 55_000_000:
            loads.append(value.get("id"))
        return value
    def counted_strip(value):
        strips.append(value.get("id"))
        return original_strip(value)
    monkeypatch.setattr(json, "loads", counted_loads)
    monkeypatch.setattr(config, "_without_carto_result_tail", counted_strip)
    monkeypatch.setattr(srv, "_get_carto_file_refresh_dir", lambda: str(tmp_path / "old-f12"))
    for _ in range(10):
        srv._scan_carto_job_files(str(tmp_path), widget_view=True)
        _status(status="all")
        assert srv._read_carto_history_candidates(tmp_path)["errors"] == []
        srv._read_and_mark_abandoned_carto_tasks(tmp_path)  # terminal selection, no rewrite
        srv._gc_carto_jobs(str(tmp_path), max_age_seconds=10 ** 9)
        for job_id in ids:
            srv._find_carto_job_on_disk(tmp_path, job_id)
            asyncio.run(srv._system_job_cancel_impl(job_id))
    assert not [name for name in opened if Path(name).name in {"files.jsonl", "nodes.jsonl"}]
    counts = Counter(name for name in opened if Path(name).suffix == ".json")
    assert counts == {str(path1): 1, str(path2): 1, str(path3): 1}
    assert loads == strips == ["large-legacy"], "a whole legacy decode is shared, not repeated within a read"
