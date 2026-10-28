"""G4 C1: one writer, D3-1, and an honest summary-only exit door."""
from __future__ import annotations

import asyncio
import copy
import gc
import hashlib
import json
import weakref
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor

import pytest

from workflowy_mcp import carto_job_record as records, config as cfg, server as srv
from workflowy_mcp.carto_job_runtime import CartoJobSpec, CartoJobControl
from workflowy_mcp.models.errors import AccountWriteBlockedError, WriteGovernorDeferredError


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr(records, "_job_records", {})
    for name in ("_CARTO_CLEARED_JOB_IDS", "_CARTO_CLEARED_JOB_PATHS", "_CARTO_CLEARED_WRITE_LOGGED"):
        monkeypatch.setattr(cfg, name, set())


def spec_at(tmp_path):
    return CartoJobSpec(job_id="g4-shared", job_file=tmp_path / "20261027-000000_g4-shared.json",
        log_file=None, job_type="CARTO_REFRESH", mode="folder", root_uuid="root",
        claim_id="claim", account_scope_id="scope")


def registered(spec):
    writer = records.record_for(spec.job_file)
    writer.create({"id": spec.job_id, "type": spec.job_type, "mode": spec.mode, "status": "running"})
    return writer


def row(file="a.py", **values):
    return {"file": file, "status": "refreshed", "changed": False, "findings": [],
            "warnings": [], "events": [], **values}


def test_registry_is_one_writer_for_normalized_path_across_threads_and_releases(tmp_path):
    """Before G4: absent registry; never substitute a second instance or resume."""
    path = tmp_path / "job.json"
    assert records.record_for(path, create=False) is None
    assert not path.exists()
    with ThreadPoolExecutor(max_workers=8) as pool:
        writers = list(pool.map(lambda _: records.record_for(path), range(24)))
    assert all(writer is writers[0] for writer in writers)
    assert records.record_for(tmp_path / "x" / ".." / "job.json") is writers[0]
    ref = weakref.ref(writers[0])
    records.release_carto_job_record(path)
    del writers
    gc.collect()
    assert ref() is None and records.record_for(path, create=False) is None
    assert not path.exists()


@pytest.mark.parametrize("outline", [None, [], [{"name": "é", "children": []}]])
@pytest.mark.parametrize("explicit", [False, True])
def test_d3_marker_keeps_null_empty_count_digest_and_finding_anatomy(tmp_path, outline, explicit):
    """Before G4: full outline stored; both sides must transform before equality."""
    spec = spec_at(tmp_path)
    writer = registered(spec)
    finding = {"code": "kept", "members": [{"text": "anatomy"}], "fix": {"resolutions": []},
               "observed": {"candidates": [1, 2]}, "detail": {"unknown": None}}
    original = row(reconcile_children=outline, findings=[finding], unknown={"nil": None, "empty": []})
    original["projection"] = {"reconcile_children": outline, "findings": [finding]}
    if explicit:
        original["projection"]["findings"] = []
    before = copy.deepcopy(original)
    writer.append_file_row(original)
    entry = json.loads((spec.job_file.with_suffix("") / "files.jsonl").read_text())
    expanded = records.expand_carto_projection(entry)
    marker = {"$reconcile_children": None if outline is None else {
        "count": len(outline), "sha256": hashlib.sha256(json.dumps(outline, sort_keys=True,
            separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()}}
    assert expanded["reconcile_children"] == marker
    assert expanded["projection"]["reconcile_children"] == marker
    assert ("proj" not in entry) is explicit
    assert expanded["findings"] == [finding]
    assert expanded["projection"]["findings"] == ([] if explicit else [finding])
    assert expanded["unknown"] == {"nil": None, "empty": []}
    assert original == before
    folded = records.fold_carto_job_evidence([entry])
    detail = records.stitch_carto_job_detail(json.loads(spec.job_file.read_text()), folded)
    assert detail["result_summary"]["files"][0] == {**expanded,
        "check_target": {"verb": "check", "params": {"file_path": "a.py", "min_severity": "info"}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_b4_no_writer_saved_read_error_is_failed_data(tmp_path, missing):
    """CF-170/CF-137 b8: base returned completed with an empty summary."""
    spec = spec_at(tmp_path)
    if not missing:
        spec.job_file.write_text("{not json", encoding="utf-8")
    payload = await srv._carto_task_terminal_payload(spec, CartoJobControl(phase="file:source"), {})
    assert payload["status"] == "failed"
    assert payload["error_extra"]["exception_type"] == ("FileNotFoundError" if missing else "JSONDecodeError")
    assert ("No such file" if missing else "Expecting property") in payload["error"]


@pytest.mark.asyncio
async def test_writer_recovery_and_cpu_attribution_never_read_saved_rows(tmp_path, monkeypatch):
    """Base used a whole-record read and rows-list attribution, losing the result."""
    from j_counterfactual import install_j_counterfactual
    install_j_counterfactual(monkeypatch)
    spec = spec_at(tmp_path)
    writer = registered(spec)
    error = ValueError("local plan error")
    identity = {"phase": "file:plan", "exception_type": "ValueError", "message": str(error), "file": "bad.py"}
    writer.append_file_row(row())
    writer.append_file_row(row("bad.py", status="failed", error=str(error), local_cpu_failure=identity))
    def forbidden(*args, **kwargs):
        raise AssertionError("writer exit read saved rows")
    monkeypatch.setattr(srv, "_read_carto_job_payload", forbidden)
    control = CartoJobControl(phase="folder:complete", cpu_failure=("file:plan", error))
    payload = await srv._carto_task_terminal_payload(spec, control, {})
    assert payload["status"] == "completed_with_warnings"
    assert payload["result_summary"]["failed"] == 1
    assert payload["result_summary"]["successful"] == 1
    assert "files" not in payload["result_summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_cf63_claim_raise_still_freezes_summary_proof_and_attempts_delivery(tmp_path, monkeypatch, stopped):
    """Base lets a claim finalizer bypass proof/delivery; writer summary stays small."""
    spec = spec_at(tmp_path)
    writer = registered(spec)
    writer.append_file_row(row(events=[{"type": "kept", "deep": {"files": [1, 2]}}]))
    seen = []
    async def raises(*args, **kwargs):
        seen.append("claim")
        raise RuntimeError("claim finalization fixture")
    monkeypatch.setattr(srv, "finalize_claim_async", raises)
    monkeypatch.setattr(srv, "finalize_cancelled_job_claim_async", raises)
    monkeypatch.setattr(srv, "_record_carto_task_terminal", lambda spec, control, proof: seen.append(proof))
    monkeypatch.setattr(srv, "_ensure_carto_final_delivery", lambda: seen.append("delivery"))
    control = CartoJobControl(phase="done", user_observed=stopped)
    await srv._finalize_carto_task(spec, control, {})
    assert seen[0] == "claim" and seen[-1] == "delivery" and isinstance(seen[1], Mapping)
    assert control.terminal_result["claim_finalization_error"] == "claim finalization fixture"
    assert records.record_for(spec.job_file, create=False) is None
    assert seen[1]["status"] == ("cancelled" if stopped else "completed_with_warnings")
    for payload in (control.terminal_result, seen[1]):
        summary = payload["result_summary"]
        assert not {"files", "events", "warnings", "findings", "phase8", "present_paths", "node_outcomes"} & summary.keys()
        assert records._WRITER_SUMMARY_MARKER not in summary
    assert json.loads(spec.job_file.read_text())["evidence"]["files"]["rows"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    AccountWriteBlockedError(retry_after=12.5, blocked_until=90, reason="breaker fixture"),
    WriteGovernorDeferredError(retry_after=7.5, deferred_until=80, reason="lane fixture"),
])
async def test_cf151_lane_and_breaker_keep_typed_kind_and_all_details(tmp_path, error):
    """Base demotes both to carto_job_exception and loses retry_after entirely."""
    payload = await srv._carto_task_terminal_payload(spec_at(tmp_path), CartoJobControl(phase="write"),
        {"result": {}, "exception": error})
    assert payload["status"] == "failed" and payload["error_kind"] == error.code
    assert payload["error"] == str(error)
    for key, value in error.details.items():
        assert payload["error_extra"][key] == value
