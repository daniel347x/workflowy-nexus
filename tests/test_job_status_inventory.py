"""Bounded active-job inventory: existing scanner/detail contracts stay intact."""
from __future__ import annotations

import asyncio
import copy
import json
from unittest.mock import Mock

import pytest
from workflowy_mcp import server
from workflowy_mcp.config import CARTO_TERMINAL_STATUSES
from workflowy_mcp.job_status_view import JOB_LIST_MAX_BYTES, summarize_job_inventory
from workflowy_mcp.mcp_api_registry import dispatch_operation


def job(jid, status="running", kind="CARTO_REFRESH", **kwargs):
    return {"job_id": jid, "status": status, "kind": kind, **kwargs}


def setup(monkeypatch, memory=None, detached=None, directory="test-jobs"):
    monkeypatch.setattr(server, "_jobs", memory or {})
    scan = Mock(return_value=detached or [])
    monkeypatch.setattr(server, "_scan_carto_job_files", scan)
    base_dir = Mock(return_value=directory)
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", base_dir)
    monkeypatch.setattr(server, "get_client", Mock(side_effect=AssertionError("job.status must stay cache/client-free")))
    return scan, base_dir


def _write_job_record(directory, job_id, record, *, stamp="20260101-000000"):
    """One real record file: the one-job path locates it by its file name."""
    path = directory / f"{stamp}_{job_id}.json"
    path.write_text(json.dumps({"id": job_id, "type": "CARTO_REFRESH", **record}), encoding="utf-8")
    return path


def _complete_detail(job_id, **params):
    """The complete detail, rebuilt from the detail view's own pages.

    From JR2 G2 ``system.job.status(job_id)`` is the summary view; the complete
    evidence is ``view="detail"``, delivered a page at a time (WaveSpec rev 3
    §2.10.1).  ``join_carto_job_detail_pages`` is that view's pure join, so the
    result is exactly one complete detail -- today's shape.
    """
    from workflowy_mcp.carto_job_record import join_carto_job_detail_pages

    pages, offset, slice_offset = [], 0, 0
    while True:
        page = asyncio.run(server._system_job_status_impl(
            job_id=job_id, view="detail", offset=offset, slice_offset=slice_offset, **params))
        pages.append(page)
        cursor = page.get("detail_page") or {}
        offset, slice_offset = cursor.get("next_offset"), cursor.get("next_slice_offset") or 0
        if offset is None:
            return join_carto_job_detail_pages(pages)


def dispatch(params=None):
    return asyncio.run(dispatch_operation(
        server.build_system_registry(), "job.status", params or {},
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    ))


def ids(result):
    return [row["job_id"] for row in result["in_memory_jobs"] + result["detached_jobs"]]


def test_default_returns_only_active_and_counts_every_terminal_state(monkeypatch):
    memory = {"mem": job("mem", "waiting", "etch"), "done": job("done", "completed", "etch")}
    detached = [job("run"), job("warnings", "completed_with_warnings"), job("failed", "failed"), job("cancel", "cancelled")]
    before = copy.deepcopy((memory, detached))
    scan, _ = setup(monkeypatch, memory, detached)
    result = dispatch()
    assert set(ids(result)) == {"mem", "run"}
    assert result["total"] == 6 and result["active_count"] == 2 and result["inactive_count"] == 4
    assert result["inactive_by_status"] == {status: 1 for status in CARTO_TERMINAL_STATUSES}
    assert result["matched"] == result["returned"] == 2
    assert result["omitted"] == 0 and result["truncated"] is False
    scan.assert_called_once_with("test-jobs")
    assert (memory, detached) == before


@pytest.mark.parametrize("status", ["preparing", "spawning", "running", "waiting", "queued", "cancelling", "future_state", "", None])
def test_unknown_and_transitional_statuses_are_conservatively_active(monkeypatch, status):
    setup(monkeypatch, detached=[job("a", status)])
    result = dispatch()
    assert ids(result) == ["a"] and result["active_count"] == 1


def test_megabytes_of_nested_history_never_escape_list_view(monkeypatch):
    huge = "PAYLOAD_SENTINEL" * 100_000
    detached = [job(str(n), "completed", progress={"files": huge}, result_summary=huge, warnings=[huge], log_file=huge) for n in range(1000)]
    detached.append(job("active", progress={"files": huge}, warnings=[huge], result_summary=huge, payload=huge))
    setup(monkeypatch, detached=detached)
    result = dispatch()
    encoded = json.dumps(result).encode()
    assert result["inactive_count"] == 1000 and ids(result) == ["active"]
    assert len(encoded) < 1500 and b"PAYLOAD_SENTINEL" not in encoded
    history = dispatch({"status": "all", "limit": 200})
    assert history["returned"] == 200 and history["omitted"] == 801 and history["truncated"]
    assert len(json.dumps(history, ensure_ascii=False).encode("utf-8")) <= JOB_LIST_MAX_BYTES
    assert "PAYLOAD_SENTINEL" not in json.dumps(history)


def test_newest_first_and_global_limit_across_memory_and_detached(monkeypatch):
    setup(monkeypatch,
        {"old": job("old", started_at="2026-01-01T00:00:00"), "middle": job("middle", started_at="2026-01-02T00:00:00Z")},
        [job("new", created_at="2026-01-03T01:00:00+01:00")],
    )
    result = dispatch({"limit": 2})
    assert set(ids(result)) == {"new", "middle"}
    assert result["active_count"] == result["matched"] == 3
    assert result["returned"] == 2 and result["omitted"] == 1 and result["truncated"]


def test_default_limit_is_50(monkeypatch):
    setup(monkeypatch, detached=[job(str(n)) for n in range(51)])
    result = dispatch()
    assert result["returned"] == 50 and result["omitted"] == 1


@pytest.mark.parametrize("params,expected", [
    ({"status": "inactive"}, {"done", "bad"}),
    ({"status": "FAILED"}, {"bad"}),
    ({"status": "all", "kind": "ETCH"}, {"mem", "done"}),
    ({"kind": "carto_refresh"}, {"run"}),
    ({"status": "running"}, {"run"}),
    ({"status": "no_such_status"}, set()),
])
def test_filters_do_not_change_whole_inventory_counts(monkeypatch, params, expected):
    setup(monkeypatch,
        {"mem": job("mem", "waiting", "etch"), "done": job("done", "completed", "etch")},
        [job("run"), job("bad", "failed")],
    )
    result = dispatch(params)
    assert set(ids(result)) == expected
    assert result["total"] == 4 and result["active_count"] == result["inactive_count"] == 2
    assert result["matched"] == len(expected)


def test_utf8_budget_applies_even_to_pathological_scalar_fields_and_keeps_ids_exact(monkeypatch):
    detached = [job(f"job-{n:03}", kind="📊" * 1000, mode="📊" * 1000, root_uuid="📊" * 1000) for n in range(200)]
    setup(monkeypatch, detached=detached)
    result = dispatch({"limit": 200})
    assert 0 < result["returned"] < 200 and result["truncated"]
    assert result["omitted"] == 200 - result["returned"]
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= JOB_LIST_MAX_BYTES
    assert all(row["job_id"] in {j["job_id"] for j in detached} for row in result["detached_jobs"])


def test_one_oversized_identifier_does_not_blow_the_budget():
    result = summarize_job_inventory([], [job("x" * 100_000)])
    assert result["returned"] == 0 and result["omitted"] == 1 and result["truncated"]
    assert len(json.dumps(result).encode()) < 1500


def test_empty_inventory_and_persisted_in_process_flag(monkeypatch):
    setup(monkeypatch)
    result = dispatch()
    assert result["total"] == result["returned"] == result["inactive_count"] == 0
    assert result["truncated"] is False
    setup(monkeypatch, detached=[job("bulk", detached=False)])
    assert dispatch()["detached_jobs"][0]["detached"] is False


def test_single_detached_detail_is_lossless_including_coherence_refusal(tmp_path, monkeypatch):
    detail = job("failed", "failed", error_kind="coherence_barrier_active", error_extra={"refresh_required": True, "recovery": {"family": "system"}}, progress={"full": "detail"}, warnings=["keep"])
    # The record's own path, so the value is the same however the reply names it.
    detail["carto_job_file"] = str(tmp_path / "20260101-000000_failed.json")
    before = copy.deepcopy(detail)
    _write_job_record(tmp_path, "failed", detail)
    setup(monkeypatch, directory=str(tmp_path))
    # The default one-job reply is the summary view from G2; the complete
    # detail is view="detail", joined across its pages.  Losslessness is the
    # assertion: every value the record carried comes back whole (the typed
    # error_extra and its recovery payload included), beside only the
    # documented additive and page keys.
    joined = _complete_detail("failed")
    assert joined["success"] is True
    assert {key: joined[key] for key in detail} == detail
    assert detail == before


def test_memory_detail_keeps_original_redaction_and_does_not_scan(monkeypatch):
    stored = job("mem", "failed", payload="private", _task="private task", result={"full": "keep"}, error_extra={"preserved": True})
    scan, _ = setup(monkeypatch, memory={"mem": stored})
    expected = {k: v for k, v in stored.items() if k not in ("payload", "_task")}
    result = dispatch({"job_id": "mem", "view": "detail"})
    assert result["success"] is True
    # The in-memory detail view is the same reply as its summary view, paged
    # rather than previewed: every stored value kept, the private execution
    # state still redacted, and no record scan on the way.
    assert {key: result[key] for key in expected} == expected
    assert "payload" not in result and "_task" not in result
    scan.assert_not_called()


def test_unknown_id_retains_existing_failure(monkeypatch):
    setup(monkeypatch)
    result = dispatch({"job_id": "unknown"})
    # The original failure envelope is retained verbatim ...
    assert {k: v for k, v in result.items() if k != "operation_doc"} == {
        "success": False,
        "error": "Unknown job_id: unknown",
    }
    # ... and dispatch_operation now appends the operation doc to explicit
    # failure outcomes (the immediate failure-documentation funnel).
    assert result["operation_doc"] == server.build_system_registry().describe_full("job.status")


@pytest.mark.parametrize("params,code", [
    ({"job_id": ""}, "invalid_parameter_type"),
    ({"status": " "}, "invalid_parameter_type"),
    ({"kind": ""}, "invalid_parameter_type"),
    ({"limit": 0}, "invalid_parameter_value"),
    ({"limit": 201}, "invalid_parameter_value"),
    ({"limit": True}, "invalid_parameter_type"),
    ({"limit": "10"}, "invalid_parameter_type"),
    ({"job_id": "x", "status": "active"}, "invalid_parameter_combination"),
    ({"job_id": "x", "limit": 50}, "invalid_parameter_combination"),
    ({"job_id": "x", "kind": "etch"}, "invalid_parameter_combination"),
])
def test_invalid_options_fail_before_directory_or_scan(monkeypatch, params, code):
    scan, directory = setup(monkeypatch)
    result = dispatch(params)
    assert result["error"]["code"] == code
    scan.assert_not_called()
    directory.assert_not_called()


def test_legacy_and_consolidated_use_same_view(monkeypatch):
    setup(monkeypatch, detached=[job("a"), job("b", "failed")])
    assert asyncio.run(server.mcp_job_status()) == dispatch()
    assert asyncio.run(server.mcp_job_status(status="inactive", limit=5)) == dispatch({"status": "inactive", "limit": 5})


def test_discovery_exposes_safe_default_and_filters():
    spec = server.SYSTEM_REGISTRY.resolve("job.status")
    assert spec.cache_backed is False and spec.side_effects == ("pure_read",)
    described = server.SYSTEM_REGISTRY.describe_full("job.status")
    # The closed schema declares every one-job view parameter (WaveSpec §2.10.1).
    assert {"job_id", "status", "kind", "limit", "view", "files", "file",
            "files_offset", "files_limit", "offset", "slice_offset"} == set(described["params_schema"]["properties"])
    tool = next(tool for tool in asyncio.run(server.mcp.list_tools()) if tool.name == "system")
    assert "ACTIVE" in tool.description and "32 KiB" in tool.description
