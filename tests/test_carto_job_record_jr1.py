"""JR1: smaller record costs, with structural (not timing) regression witnesses."""
from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from workflowy_mcp import beacon_refresh_support as support
from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp import config, server as srv
from workflowy_mcp.carto_job_runtime import CartoJobControl, CartoJobSpec, _freeze_job_data
from workflowy_mcp.client import api_client_nexus as nexus


def _record(count=2):
    files = [{"file": f"/repo/{i}.py", "status": "completed", "changed": False,
              "findings": [], "warnings": [], "events": [{"type": "kept", "ordinal": i}],
              "projection": None if i % 2 else [], "unknown": {"nullable": None, "empty": []}}
             for i in range(count)]
    return {"id": "jr1", "type": "CARTO_REFRESH", "mode": "folder", "status": "running",
            "claim_id": "jr1-claim", "cache_refresh_required": True,
            "progress": {"completed_files": count, "nodes_created": 7},
            "warnings": [{"message": "first"}], "warning_count": 1,
            "result_summary": {"files_refreshed": count, "unknown_head": {"value": None},
                "warnings": [{"type": "retained"}], "events": [{"type": "historic"}],
                "files_changed": [], "present_paths": [row["file"] for row in files],
                "findings": [], "phase8": None, "node_outcomes": [], "files": files}}


def _complete_detail(job_id, **params):
    """The detail view's complete evidence, joined across its pages.

    JR2 G2 makes the default one-job reply the summary view; today's complete
    detail is ``view="detail"``, delivered a page at a time (WaveSpec rev 3
    §2.10.1), and ``join_carto_job_detail_pages`` is that view's own pure join.
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


def test_failed_terminal_write_keeps_unpersisted_proof_evidence_in_detail(tmp_path, monkeypatch):
    """Old behavior: the failed terminal write's complete proof outranks disk."""
    record = _record(1)
    path = tmp_path / "jr1.json"
    assert config.merge_write_carto_job_status(path, record)
    terminal = {**record, "path": str(path), "status": "failed", "status_write_error": "sharing collision",
                "result_summary": _record(2)["result_summary"]}
    proof = _freeze_job_data(terminal)
    for name in ("_carto_startup_failed_jobs", "_carto_inprocess_tasks", "_carto_job_controls", "_jobs"):
        monkeypatch.setattr(srv, name, {})
    monkeypatch.setattr(srv, "_carto_final_obligations", {
        "jr1": SimpleNamespace(terminal_proof={"job_status": proof})})
    monkeypatch.setattr(srv, "_governance_holds", lambda: ())
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(tmp_path))
    detail = _complete_detail("jr1")
    assert detail["result_summary"] == terminal["result_summary"]


class _EvidenceTrap(Mapping):
    """A visit/copy of any frozen evidence container fails, independent of time."""
    def __getitem__(self, key):
        raise AssertionError("finished proof evidence was visited")

    def __iter__(self):
        raise AssertionError("finished proof evidence was visited")

    def __len__(self):
        raise AssertionError("finished proof evidence was visited")


def _trap_evidence(proof):
    summary = dict(proof["result_summary"])
    for key in config.CARTO_RESULT_SUMMARY_TAIL_KEYS:
        summary[key] = (_EvidenceTrap(),) * 500 if key == "files" else _EvidenceTrap()
    # CF-164: inventories must not thaw any non-scalar summary value, even a
    # head-resident mapping/list that JR1 did not classify as tail evidence.
    summary["unknown_head"] = _EvidenceTrap()
    summary["publication_policy"] = _EvidenceTrap()
    summary["missing_paths"] = (_EvidenceTrap(),) * 500
    return MappingProxyType({**proof, "result_summary": MappingProxyType(summary)})


def _install_proofs(monkeypatch, tmp_path, source, proofs):
    for name in ("_carto_startup_failed_jobs", "_carto_final_obligations", "_carto_inprocess_tasks",
                 "_carto_job_controls", "_jobs"):
        monkeypatch.setattr(srv, name, {})
    monkeypatch.setattr(srv, "_governance_holds", lambda: ())
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(tmp_path))
    for jid, proof in proofs.items():
        if source == "obligation":
            srv._carto_final_obligations[jid] = SimpleNamespace(terminal_proof={"job_status": proof})
        elif source == "done_task":
            srv._carto_inprocess_tasks[jid] = SimpleNamespace(done=lambda: True)
            srv._carto_job_controls[jid] = SimpleNamespace(presentation=proof, terminal_result=proof)
        else:
            srv._carto_startup_failed_jobs[jid] = proof


@pytest.mark.parametrize("source", ["obligation", "done_task", "startup"])
@pytest.mark.parametrize("surface", ["widget", "status_list"])
def test_finished_500_file_list_never_visits_proof_evidence(tmp_path, monkeypatch, source, surface):
    record = {**_record(500), "status": "completed_with_warnings"}
    path = tmp_path / "jr1.json"
    assert config.merge_write_carto_job_status(path, record)
    proof = _trap_evidence(_freeze_job_data({**record, "path": str(path)}))
    _install_proofs(monkeypatch, tmp_path, source, {"jr1": proof})
    if surface == "widget":
        (row,) = srv._scan_carto_job_files(str(tmp_path), widget_view=True,
                                         live_projection=srv._carto_live_job_projection())
        assert row["status"] == record["status"]
        assert not set(config.CARTO_RESULT_SUMMARY_TAIL_KEYS) & row["result_summary"].keys()
    else:
        result = asyncio.run(srv._system_job_status_impl(status="all"))
        assert result["inactive_count"] == 1 and result["returned"] == 1
        assert result["detached_jobs"][0]["job_id"] == "jr1"


@pytest.mark.parametrize("source", ["obligation", "done_task"])
@pytest.mark.parametrize("missing_disk", [False, True])
@pytest.mark.parametrize("unrelated_proof", [False, True])
def test_targeted_detail_keeps_all_500_rows_and_thaws_only_its_proof(
        tmp_path, monkeypatch, source, missing_disk, unrelated_proof):
    path = tmp_path / "jr1.json"
    disk = _record(1)
    disk["result_summary"]["disk_only"] = "baseline proof replacement must remove this"
    if not missing_disk:
        assert config.merge_write_carto_job_status(path, disk)
    terminal = {**_record(500), "path": str(path), "status": "failed",
                "status_write_error": "fixture terminal write failed"}
    for index, row in enumerate(terminal["result_summary"]["files"]):
        row["findings"] = [{"code": "retained", "ordinal": index}]
        row["warnings"] = [{"type": "file_warning", "ordinal": index}]
    terminal["result_summary"]["findings"] = [{"code": "preview", "unknown": None}]
    terminal["result_summary"]["node_outcomes"] = [{"unknown": None, "empty": []}]
    proofs = {"jr1": _freeze_job_data(terminal)}
    if unrelated_proof:
        other_path = tmp_path / "other.json"
        other = {**_record(500), "id": "other", "status": "completed"}
        assert config.merge_write_carto_job_status(other_path, other)
        proofs["other"] = _trap_evidence(_freeze_job_data({**other, "path": str(other_path)}))
    _install_proofs(monkeypatch, tmp_path, source, proofs)

    detail = _complete_detail("jr1")
    assert detail["success"] is True and detail["status"] == "failed"
    assert detail["result_summary"] == terminal["result_summary"]
    assert len(detail["result_summary"]["files"]) == 500
    assert set(config.CARTO_RESULT_SUMMARY_TAIL_KEYS) <= detail["result_summary"].keys()
    assert detail["result_summary"]["files"][0]["projection"] == []
    assert detail["result_summary"]["files"][1]["projection"] is None


def test_summary_overlay_merges_instead_of_replacing_disk_evidence(tmp_path):
    record = _record()
    path = tmp_path / "jr1.json"
    assert config.merge_write_carto_job_status(path, record)
    live = {"jr1": _freeze_job_data({"path": str(path), "status": "completed",
                                    "result_summary": {"files_refreshed": 3},
                                    "progress": {"completed_files": 3}})}
    (row,) = srv._scan_carto_job_files(str(tmp_path), live_projection=live)
    assert row["result_summary"] == {**config._without_carto_result_tail(record)["result_summary"], "files_refreshed": 3}
    assert row["progress"] == {"completed_files": 3, "nodes_created": 7}


@pytest.mark.parametrize("proof_last", [False, True])
def test_targeted_detail_keeps_baseline_last_path_wins_for_duplicate_ids(tmp_path, monkeypatch, proof_last):
    path, other_path = tmp_path / "proof.json", tmp_path / "other.json"
    disk, other = _record(1), {**_record(2), "status": "completed_with_warnings"}
    assert config.merge_write_carto_job_status(path, disk)
    assert config.merge_write_carto_job_status(other_path, other)
    terminal = {**_record(3), "path": str(path), "status": "completed_with_warnings"}
    _install_proofs(monkeypatch, tmp_path, "obligation", {"jr1": _freeze_job_data(terminal)})
    order = [other_path, path] if proof_last else [path, other_path]
    iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda directory: iter(order) if directory == tmp_path else iterdir(directory))
    # G2's one-job lookup never content-scans arbitrary filenames. Startup or
    # an inventory populates its disposable id index in document order first.
    srv._scan_carto_job_files(str(tmp_path), live_projection=srv._carto_live_job_projection())
    detail = _complete_detail("jr1")
    assert detail["carto_job_file"] == str(order[-1])
    assert detail["result_summary"] == (terminal if proof_last else other)["result_summary"]


def test_aggregation_and_owned_outcome_do_not_deepcopy_nested_evidence():
    class NoDeepcopy(dict):
        def __deepcopy__(self, memo):
            raise AssertionError("run evidence was deep-copied")
    nested = NoDeepcopy({"proof": [None, [], {"unknown": 1}]})
    row = {"file": "one.py", "unknown": nested}
    aggregate = support.aggregate_beacon_outcomes([row])
    assert aggregate["files"][0] is not row
    assert aggregate["files"][0]["unknown"] is nested
    assert "status" not in row and "findings" not in row
    result = support.beacon_job_outcome(aggregate, owns_aggregate=True)
    assert result["files"][0]["unknown"] is nested and result["status"] == "completed"


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("phase8", [None, {"success": True, "files": [{"file": "/repo/0.py", "findings": []}]},
    {"success": False, "error": "refresh failed", "warnings": [{"type": "duplicate"}] * 2,
     "files": [{"file": "/repo/0.py", "findings": [{"code": "fresh"}], "warnings": [{"type": "row"}]},
               {"file": "discovered.py", "findings": [{"code": "new"}]}]}])
def test_owned_and_default_outcomes_are_equal_without_mutating_caller_rows(owned, phase8):
    rows = _record()["result_summary"]["files"]
    rows[0]["findings"] = [{"code": "old", "unknown": None}]
    rows[0]["warnings"] = [{"type": "duplicate"}] * 2
    rows[0]["omissions"] = [{"type": "omitted"}]
    before = json.dumps(rows, sort_keys=True)
    aggregate = support.aggregate_beacon_outcomes(rows)
    aggregate_before = copy.deepcopy(aggregate)
    expected = support.beacon_job_outcome(aggregate, phase8=phase8)
    got = support.beacon_job_outcome(aggregate, phase8=phase8, owns_aggregate=owned)
    assert got == expected
    assert json.dumps(rows, sort_keys=True) == before
    if not owned:
        assert aggregate == aggregate_before
    assert got["files"][0]["projection"] == [] and got["files"][1]["projection"] is None
    assert got["warnings"][:2] == [{"type": "duplicate"}] * 2


@pytest.mark.parametrize("disk_status", ["failed", "cancelled", "completed_with_warnings", "cancelling"])
def test_merge_uses_no_whole_decode_and_keeps_precedence_and_warnings(tmp_path, monkeypatch, disk_status):
    path = tmp_path / "jr1.json"
    record = {**_record(), "status": disk_status}
    path.write_text(json.dumps(config.carto_job_record_layout(record), indent=2), encoding="utf-8")
    decodes = []
    original = json.loads
    monkeypatch.setattr(json, "loads", lambda *a, **k: decodes.append(1) or original(*a, **k))
    incoming = {**record, "status": "running", "warnings": [{"message": "second"}, {"message": "first"}]}
    assert config.merge_write_carto_job_status(path, incoming)
    assert decodes == [], "precedence must read the head, not decode the whole record"
    written = original(path.read_text(encoding="utf-8"))
    assert written["status"] == disk_status
    assert written["warnings"] == [{"message": "first"}, {"message": "second"}]
    assert written["warning_count"] == 2 and written["result_summary"] == record["result_summary"]


@pytest.mark.parametrize("initial_layout", ["legacy", "summary-head/1", "compact-admission"])
def test_folder_boundary_reads_heads_and_terminal_adds_present_paths(tmp_path, monkeypatch, initial_layout):
    path = tmp_path / "jr1.json"
    record = _record()
    # A real folder boundary has not reached terminal present_paths/phase8 yet.
    summary = support.beacon_job_outcome(support.aggregate_beacon_outcomes(record["result_summary"]["files"]))
    record["result_summary"] = summary
    formatted = config.carto_job_record_layout(record) if initial_layout == "summary-head/1" else record
    path.write_text(json.dumps(formatted, indent=None if initial_layout == "compact-admission" else 2), encoding="utf-8")
    whole = []
    original = json.loads
    monkeypatch.setattr(json, "loads", lambda *a, **k: whole.append(1) or original(*a, **k))
    assert nexus._write_carto_file_artifact("folder-summary", {
        "path": str(path), "summary": summary, "progress": {"completed_files": 2}})
    # Legacy/admission falls back once; the merge writer then also reads that
    # old head. No legacy repair/extra write is introduced by this increment.
    assert len(whole) == (0 if initial_layout == "summary-head/1" else 2)
    assert original(path.read_text())["result_summary"] == summary
    terminal_summary = {**summary, "present_paths": ["/repo/0.py", "/repo/1.py"], "node_outcomes": [], "phase8": None}
    _finalize(path, monkeypatch, {"result": terminal_summary})
    terminal = original(path.read_text())
    assert terminal["status"] == "completed_with_warnings"
    assert terminal["result_summary"] == terminal_summary
    assert set(config.CARTO_RESULT_SUMMARY_TAIL_KEYS) <= terminal["result_summary"].keys()


def _finalize(path, monkeypatch, outcome):
    async def release(*args, **kwargs):
        pass
    monkeypatch.setattr(srv, "finalize_claim_async", release)
    monkeypatch.setattr(srv, "_ensure_carto_final_delivery", lambda: None)
    monkeypatch.setattr(srv, "_carto_final_obligations", {})
    monkeypatch.setattr(srv, "_carto_job_controls", {})
    spec = CartoJobSpec("jr1", path, None, "CARTO_REFRESH", "folder", "root", "jr1-claim", "test")
    asyncio.run(srv._finalize_carto_task(spec, CartoJobControl(), outcome))


@pytest.mark.parametrize("caller", ["progress", "task_status", "abandoned", "cancel_request", "persist_terminal", "finalize"])
def test_whole_read_callers_preserve_every_tail_key(tmp_path, monkeypatch, caller):
    record = _record()
    path = tmp_path / "jr1.json"
    assert config.merge_write_carto_job_status(path, record)
    if caller == "progress":
        result = nexus._write_carto_file_artifact("progress", {"path": str(path), "pending": {"nodes_created": 1},
            "retry_counts": {}, "phase": "applying"})
        assert result["acknowledged"] == {"nodes_created": 1}
    elif caller == "task_status":
        srv._merge_carto_task_status(str(path), {"status": "running"})
    elif caller == "abandoned":
        _, warnings = srv._read_and_mark_abandoned_carto_tasks(tmp_path)
        assert warnings == []
    elif caller == "cancel_request":
        gov._write_task_cancellation_request(path, {"progress": {"nodes_created": 8}})
    elif caller == "persist_terminal":
        gov._persist_terminal_job(path, "failed")
    else:
        _finalize(path, monkeypatch, {"result": {"success": True}})
    written = json.loads(path.read_text())
    for key in (*config.CARTO_RESULT_SUMMARY_TAIL_KEYS, "unknown_head"):
        assert written["result_summary"][key] == record["result_summary"][key]


@pytest.mark.parametrize("summary", [None, [], {}, {"files": []}, {"count": 1, "files": [None, [], {"unknown": "é\n}"}]}])
def test_compact_tail_is_c_encoded_and_every_reader_sees_the_same_record(tmp_path, monkeypatch, summary):
    path = tmp_path / "jr1.json"
    record = {**_record(), "result_summary": summary}
    encoded = []
    dumps = json.dumps
    monkeypatch.setattr(json, "dumps", lambda value, **kw: encoded.append(kw) or dumps(value, **kw))
    assert config.merge_write_carto_job_status(path, record)
    text = path.read_text(encoding="utf-8")
    assert text.startswith('{\n  "record_layout": "summary-head/1",')
    assert json.loads(text) == {"record_layout": "summary-head/1", **record}
    assert config.read_carto_job_record_head(path) == config._without_carto_result_tail(json.loads(text))
    if isinstance(summary, dict) and "files" in summary:
        assert '"files":' + dumps(summary["files"], separators=(",", ":")) in text
        assert {"separators": (",", ":")} in encoded, "tail must use dumps without indent (C encoder)"


@pytest.mark.parametrize("clears", [True, False])
def test_replace_retries_only_the_collision_with_the_existing_ceiling(tmp_path, monkeypatch, caplog, clears):
    path = tmp_path / "jr1.json"
    record = _record()
    clock, sleeps, attempts = [0.0], [], []
    replace = config.os.replace
    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    def collide(src, dst):
        attempts.append((str(src), src.read_text(encoding="utf-8")))
        if not clears or len(attempts) <= 2:
            raise PermissionError("reader has the old record open")
        return replace(src, dst)
    monkeypatch.setattr(gov, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep), raising=False)
    monkeypatch.setattr(config.os, "replace", collide)
    assert config.merge_write_carto_job_status(path, record) is clears
    assert sleeps and set(sleeps) == {0.005}
    assert len(set(attempts)) == 1, "retry must replace the same already-written temp, not serialize again"
    assert not list(tmp_path.glob("*.tmp"))
    if clears:
        assert len(attempts) == 3 and clock[0] == 0.01
        assert json.loads(path.read_text())["result_summary"] == record["result_summary"]
    else:
        assert 2.0 <= clock[0] < 2.005
        assert not path.exists() and "Could not persist Cartographer job status" in caplog.text
