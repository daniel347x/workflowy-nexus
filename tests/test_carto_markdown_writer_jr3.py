"""G5 C3: one-file Markdown evidence, complete handoffs and bounded ownership."""
from __future__ import annotations

import asyncio
import gc
import json
from pathlib import Path
import threading
import weakref

import pytest

import markdown_roundtrip as mdr
from n07_job_harness import real_job_environment, scrubbed_runtime, launch_real_job  # noqa: F401
from test_beacon_integration_contract import _joined_complete_detail, _markdown_fixture
from workflowy_mcp import beacon_refresh_support as support, carto_job_record as records, config
from workflowy_mcp import markdown_generate_pipeline as pipeline, server as srv
from workflowy_mcp.carto_job_runtime import current_carto_job_context


class Evidence(dict):
    """An evidence object's lifetime, without retaining it in the observer."""


class Findings(list):
    """The ruling's one permitted in-flight list, with an observable lifetime."""


@pytest.fixture(autouse=True)
def isolated_writers(monkeypatch):
    monkeypatch.setattr(records, "_job_records", {})
    for name in ("_CARTO_CLEARED_JOB_IDS", "_CARTO_CLEARED_JOB_PATHS", "_CARTO_CLEARED_WRITE_LOGGED"):
        monkeypatch.setattr(config, name, set())
    from j_counterfactual import install_j_counterfactual
    install_j_counterfactual(monkeypatch)


def _no_tail(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "in_flight":
                continue
            assert not (key in {"files", "node_outcomes", "findings", "events", "present_paths"}
                        and isinstance(item, (list, tuple))), key
            assert key != "phase8", "full phase8 envelope in a record/proof"
            _no_tail(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _no_tail(item)


def _generation_findings(monkeypatch, count):
    """Keep the real preparation and publication plan; replace only test findings."""
    references = []
    prepare = mdr.prepare_generated_markdown_beacons

    def prepared(*args, **kwargs):
        result = prepare(*args, **kwargs)
        result["findings"] = Findings({"code": "jr3_generation", "severity": "info", "finding_id": f"jr3:{index}",
            "message": str(index), "members": [{"complete": index}], "fix": {"resolutions": []},
            "observed": {"version": "generation"}, "detail": {"not_a_preview": True}}
            for index in range(count))
        references.append(weakref.ref(result["findings"]))
        return result

    monkeypatch.setattr(mdr, "prepare_generated_markdown_beacons", prepared)
    return references


async def _finish(env, ack):
    await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    assert not env.probe.violations
    path = Path(ack["job_file"])
    record = json.loads(path.read_text())
    assert record["record_layout"] == "summary-head/2"
    _no_tail(record)
    _no_tail(env.controls[ack["job_id"]].terminal_result)
    assert records.record_for(path, create=False) is None
    return await _joined_complete_detail(ack["job_id"]), record


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [50, 2000])
async def test_markdown_streams_before_phase8_without_aggregation_or_readback(real_job_environment, monkeypatch, count):
    env = real_job_environment
    _markdown_fixture(env)
    findings = _generation_findings(monkeypatch, count)
    row_refs, sizes, handoffs = [], [], []
    append, merged, opened = records.CartoJobRecord.append_file_row, config.merge_write_carto_job_status, Path.open
    inline = pipeline._run_phase8_refresh_inline

    def appended(self, row, **kwargs):
        row["lifetime_probe"] = Evidence()
        row_refs.append(weakref.ref(row["lifetime_probe"]))
        return append(self, row, **kwargs)

    def merge(path, payload, **kwargs):
        if payload.get("type") == "CARTO_MARKDOWN_GENERATE":
            _no_tail(payload)
            sizes.append(len(json.dumps(payload, separators=(",", ":")).encode()))
        return merged(path, payload, **kwargs)

    def open_no_read(self, mode="r", *args, **kwargs):
        assert not (self.name in {"files.jsonl", "nodes.jsonl"} and "r" in mode), "producer read earlier evidence"
        return opened(self, mode, *args, **kwargs)

    async def phase8(*args, **kwargs):
        assert findings[0]() is not None and all(ref() is None for ref in row_refs)
        writer = records.record_for(current_carto_job_context().job_file, create=False)
        assert writer is not None and writer._rows["files"] == 1
        # Inspect writer counters here; an explicit read happens only after the
        # run. The producer itself is under the no-sidecar-read trap.
        handoffs.append(writer._lines["files"])
        return await inline(*args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Markdown save re-aggregated its rows")

    with monkeypatch.context() as patch:
        patch.setattr(records.CartoJobRecord, "append_file_row", appended)
        patch.setattr(config, "merge_write_carto_job_status", merge)
        patch.setattr(Path, "open", open_no_read)
        patch.setattr(pipeline, "_run_phase8_refresh_inline", phase8)
        patch.setattr(support, "aggregate_beacon_outcomes", forbidden)
        patch.setattr(support, "beacon_job_outcome", forbidden)
        ack = await launch_real_job(env, "markdown")
        assert ack["success"]
        await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    gc.collect()
    assert len(handoffs) == 1 and len(row_refs) == 2
    assert findings[0]() is None and all(ref() is None for ref in row_refs)
    assert max(sizes) < 8192 and max(sizes) <= sizes[-1] + 4096
    detail, record = await _finish(env, ack)
    lines = [json.loads(line) for line in (Path(ack["job_file"]).with_suffix("") / "files.jsonl").read_text().splitlines()]
    publication = next(i for i, line in enumerate(lines) if line.get("key") == "publication")
    primary = next(i for i, line in enumerate(lines) if line.get("sec") == "files")
    refresh = next(i for i, line in enumerate(lines) if line.get("sec") == "phase8")
    envelope = next(i for i, line in enumerate(lines) if line.get("key") == "phase8_envelope")
    assert publication < primary < refresh < envelope
    assert len(lines[primary]["row"]["findings"]) == count
    assert lines[primary]["row"]["findings"][-1]["members"] == [{"complete": count - 1}]
    assert detail["result_summary"]["findings_total"] == 0  # clean refresh clears the old set
    assert record["evidence"]["files"]["rows"] == 2 and record["evidence"]["nodes"] is None
    phase8_row = detail["result_summary"]["phase8"]["files"][0]
    assert phase8_row["reconcile_children"] == phase8_row["projection"]["reconcile_children"]
    assert "$reconcile_children" in phase8_row["reconcile_children"]
    assert detail["result_summary"]["phase8"]["result"]["reconcile_children"] == phase8_row["reconcile_children"]


@pytest.mark.asyncio
async def test_cf93_exception_keeps_all_findings_and_cf137_effect_is_unknown(real_job_environment, monkeypatch):
    env = real_job_environment
    path = _markdown_fixture(env)
    _generation_findings(monkeypatch, 260)
    refresh = env.client.refresh_file_node_beacons
    effects = []

    async def failed_after_work(*args, **kwargs):
        result = await refresh(*args, **kwargs)
        effects.append(result["nodes_created"])
        raise RuntimeError("jr3 after a real phase8 create")

    monkeypatch.setattr(env.client, "refresh_file_node_beacons", failed_after_work)
    ack = await launch_real_job(env, "markdown")
    detail, record = await _finish(env, ack)
    summary = detail["result_summary"]
    assert effects and effects[0] >= 1
    assert detail["status"] == "completed_with_warnings" and summary["partial_success"]
    assert summary["publication"]["success"] and summary["files_changed"] == [str(path.resolve())]
    row = summary["phase8"]["files"][0]
    assert row["changed"] is None and row["error"] == "jr3 after a real phase8 create"
    assert len(row["findings"]) == summary["findings_total"] == 260
    assert summary["files"][0]["findings"] == row["findings"] == summary["phase8"]["result"]["findings"]
    assert row["findings"][-1]["detail"] == {"not_a_preview": True}
    assert record["result_summary"]["phase8_failed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("exit", ["success", "exception", "cancel", "restart", "before-phase8", "typed-before-phase8", "cancel-publication"])
async def test_one_file_findings_hold_releases_on_every_exit(real_job_environment, monkeypatch, exit):
    env = real_job_environment
    _markdown_fixture(env)
    references = _generation_findings(monkeypatch, 260)
    refresh, set_phase = env.client.refresh_file_node_beacons, pipeline._set_phase
    primary_refs, phase8_refs, receipt_refs, checks, released_writes = [], [], [], [], []
    append = records.CartoJobRecord.append_file_row
    merge = config.merge_write_carto_job_status
    publish = mdr.publish_generated_markdown_beacons
    entered, release = threading.Event(), threading.Event()

    def published(*args, **kwargs):
        receipt = Evidence(publish(*args, **kwargs))
        receipt_refs.append(weakref.ref(receipt))
        if exit == "cancel-publication":
            entered.set()
            assert release.wait(15), "test did not release publication"
        return receipt

    def appended(self, row, **kwargs):
        if kwargs.get("section", "files") == "files":
            row["primary_lifetime"] = Evidence()
            primary_refs.append(weakref.ref(row["primary_lifetime"]))
        else:
            row["phase8_lifetime"] = Evidence()
            phase8_refs.append(weakref.ref(row["phase8_lifetime"]))
        return append(self, row, **kwargs)

    def merged(path, payload, **kwargs):
        if references and payload.get("status") in {"cancelling", "cancelled", "completed", "completed_with_warnings", "failed"}:
            assert references[0]() is None and all(ref() is None for ref in primary_refs + phase8_refs + receipt_refs), "evidence survived the last handoff"
            released_writes.append(payload["status"])
        return merge(path, payload, **kwargs)

    async def phase(*args, **kwargs):
        await set_phase(*args, **kwargs)
        if args[3] == "phase8-refresh":
            assert references[0]() is not None and all(ref() is None for ref in primary_refs + receipt_refs)
            checks.append("held-findings-only")
            if exit == "before-phase8":
                raise ValueError("jr3 before phase8 starts")
            if exit == "typed-before-phase8":
                from workflowy_mcp.carto_job_governance import AdmissionRefusal
                raise AdmissionRefusal("overlap_conflict", "jr3 before phase8 typed", retry_after=3)

    async def refreshed(*args, **kwargs):
        control = current_carto_job_context().job_control
        if exit in {"cancel", "restart"}:
            if exit == "cancel":
                control.request_user_cancel()
            else:
                control.restart_state["restart_id"] = "jr3-restart"
            control.checkpoint("jr3-inside-phase8", restart_boundary=True)
        result = await refresh(*args, **kwargs)
        if exit == "exception":
            raise RuntimeError("jr3 phase8 local exception")
        return result

    monkeypatch.setattr(records.CartoJobRecord, "append_file_row", appended)
    monkeypatch.setattr(config, "merge_write_carto_job_status", merged)
    monkeypatch.setattr(mdr, "publish_generated_markdown_beacons", published)
    monkeypatch.setattr(pipeline, "_set_phase", phase)
    monkeypatch.setattr(env.client, "refresh_file_node_beacons", refreshed)
    ack = await launch_real_job(env, "markdown")
    try:
        if exit == "cancel-publication":
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 15), 16)
            env.controls[ack["job_id"]].request_user_cancel()
    finally:
        release.set()
    detail, _record = await _finish(env, ack)
    gc.collect()
    assert checks == ([] if exit == "cancel-publication" else ["held-findings-only"])
    assert references[0]() is None and all(ref() is None for ref in primary_refs + phase8_refs + receipt_refs)
    assert len(receipt_refs) == 1
    assert released_writes, "no post-handoff record write was witnessed"
    assert detail["result_summary"]["publication"]["success"]
    assert detail["status"] == ("cancelled" if exit in {"cancel", "restart", "cancel-publication"} else
                                "failed" if exit in {"before-phase8", "typed-before-phase8"} else "completed_with_warnings" if exit == "exception" else "completed")


@pytest.mark.parametrize("failure", ["false", "oserror", "importerror"])
def test_every_markdown_write_fallback_uses_the_writer_and_reports_warning(tmp_path, monkeypatch, failure):
    path = tmp_path / "20261028-000000_markdown-fallback.json"
    writer = records.record_for(path)
    writer.create({"id": "markdown-fallback", "type": "CARTO_MARKDOWN_GENERATE", "status": "cancelled",
                   "progress": {"current_phase": "waiting"}})
    merge = config.merge_write_carto_job_status
    attempts = []

    def failed_once(target, payload, **kwargs):
        attempts.append(payload["record_layout"])
        if len(attempts) == 1:
            if failure == "false":
                return False
            raise (ImportError("jr3 import fault") if failure == "importerror" else OSError("jr3 funnel fault"))
        return merge(target, payload, **kwargs)

    monkeypatch.setattr(config, "merge_write_carto_job_status", failed_once)
    pipeline._write_job_payload(str(path), {"status": "running", "progress": {"current_phase": "fallback"}})
    saved = json.loads(path.read_text())
    assert attempts == ["summary-head/2", "summary-head/2"]
    assert saved["status"] == "cancelled" and saved["progress"]["current_phase"] == "fallback"
    assert saved["warning_count"] == 1
    assert saved["warnings"][0]["type"] == "markdown_job_record_write_failed"
    evidence = [json.loads(line) for line in (path.with_suffix("") / "files.jsonl").read_text().splitlines()]
    assert evidence == [{"k": "item", "key": "run_warnings", "v": saved["warnings"][0]}]


def test_cleared_markdown_writes_and_logs_do_not_recreate_artifacts(tmp_path):
    path = tmp_path / "20261028-000000_cleared-markdown.json"
    records.record_for(path).create({"id": "cleared-markdown", "type": "CARTO_MARKDOWN_GENERATE"})
    with config._CARTO_JOB_STATUS_WRITE_LOCK:
        config.mark_carto_job_cleared(path, "cleared-markdown")
        path.unlink()
    pipeline._write_job_payload(str(path), {"status": "running"})
    pipeline._append_log(str(path.with_suffix(".log")), "late log")
    assert not path.exists() and not path.with_suffix("").exists() and not path.with_suffix(".log").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unattributed", "coherence", "baseexception", "admission", "mutation", "breaker"])
async def test_r6_nonprimary_phase8_file_never_supplies_cpu_attribution(real_job_environment, monkeypatch, failure):
    """Six writer-era replacements for CF-180; the original six R6 arms stay.

    Each appends an actual non-primary phase8 row through the production handoff,
    carrying that arm's exact CPU identity. The accumulator must reject it even
    when the exception's separate run-wide-type gate would also reject it.
    """
    env = real_job_environment
    _markdown_fixture(env)
    from test_beacon_integration_contract import test_r6_runwide_and_unattributed_failures_keep_committed_disk
    terminal = srv._carto_task_terminal_payload
    checked = []

    async def observed(spec, control, outcome):
        writer = records.record_for(spec.job_file, create=False)
        phase, error = control.cpu_failure
        identity = {"phase": phase, "exception_type": type(error).__name__, "message": str(error)}
        row = {"file": str((env.repo / "not-the-primary.md").resolve()), "status": "failed", "error": str(error),
               "findings": [], "local_cpu_failure": {**identity, "file": str((env.repo / "not-the-primary.md").resolve())}}
        writer.append_file_row(row, section="phase8")
        writer.set_value("phase8_envelope", {"execution": "inline", "success": False, "error": str(error)})
        assert not writer.accumulator.local_cpu_failure(identity), "non-primary phase8 row attributed the CPU failure"
        checked.append(failure)
        return await terminal(spec, control, outcome)

    monkeypatch.setattr(srv, "_carto_task_terminal_payload", observed)
    await test_r6_runwide_and_unattributed_failures_keep_committed_disk(env, monkeypatch, failure)
    assert checked == [failure]
