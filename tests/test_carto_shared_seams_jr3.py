"""JR3 C1: CF-175/176/178, before any bulk delta producer is switched.

The starting witnesses are the JR2 audit's test_jr2_findings_probe.py and
test_adj_probe.py (copied, never edited at their source). Defect-direction
cells ran 7/7 at fc79975. These cells assert the required direction instead.
No surrogate/read-encoder cell (CF-177) belongs to JR3.
"""
from __future__ import annotations

import json

import pytest

from workflowy_mcp import carto_job_record as rec, config as cfg
from workflowy_mcp.job_status_view import normalize_job_detail_options
from test_carto_job_writer_jr2 import _Fault, _install_fault


@pytest.fixture(autouse=True)
def isolated_writer_state(monkeypatch):
    for name in ("_record_cache", "_record_ids", "_path_ids", "_detail_cache", "_job_records"):
        monkeypatch.setattr(rec, name, type(getattr(rec, name))())
    for name in ("_CARTO_CLEARED_JOB_IDS", "_CARTO_CLEARED_JOB_PATHS", "_CARTO_CLEARED_WRITE_LOGGED"):
        monkeypatch.setattr(cfg, name, set())


def writer_at(tmp_path, *, type_="CARTO_REFRESH", mode="folder"):
    path = tmp_path / "20261027-000000_probe.json"
    writer = rec.CartoJobRecord(path)
    writer.create({"id": "probe", "type": type_, "mode": mode,
                   "status": "running", "progress": {}})
    return writer, path


def row(file="/repo/same.py", **changes):
    return {"file": file, "status": "refreshed", "changed": False,
            "findings": [], "warnings": [], "events": [], **changes}


def joined(path, *, overlay=None):
    opts = normalize_job_detail_options("probe", view="detail")
    pages, offset, sliced = [], 0, 0
    while True:
        page = rec.read_carto_job_view(path, job_id="probe", overlay=overlay, options={
            **opts, "offset": offset, "slice_offset": sliced})
        pages.append(page)
        meta = page.get("detail_page") or {}
        if meta.get("next_offset") is None:
            return rec.join_carto_job_detail_pages(pages)
        offset, sliced = meta["next_offset"], meta.get("next_slice_offset") or 0


@pytest.mark.parametrize("never_lands", [False, True])
def test_recovered_old_row_does_not_replace_later_same_key(tmp_path, monkeypatch, never_lands):
    writer, path = writer_at(tmp_path)
    _install_fault(monkeypatch, _Fault(path.with_suffix("") / "files.jsonl",
        poison="old-attempt", times=None if never_lands else 2))
    assert not writer.append_file_row(row(attempt="old-attempt", changed=True))
    # Same-key lines wait; another key never does (CF-167/B1).
    assert not writer.append_file_row(row(attempt="latest-attempt", changed=False))
    assert writer.append_file_row(row("/repo/other.py"))
    terminal = writer.finalize({"status": "completed"})
    if never_lands:
        assert len(terminal["_pending_evidence"]["files"]) == 2
    detail = joined(path, overlay=terminal)
    latest = next(item for item in detail["result_summary"]["files"] if item["file"] == "/repo/same.py")
    assert latest["attempt"] == "latest-attempt" and latest["changed"] is False
    assert writer.summary()["findings_total"] == 0


def test_g5_two_delta_lines_wait_behind_the_first_pending_delta(tmp_path, monkeypatch):
    """L1's bulk case: sets, unsets and extends keep their produced order."""
    writer, path = writer_at(tmp_path, type_="CARTO_BULK_APPLY", mode="all")
    writer.append_file_row(row(status="completed", warnings=[{"message": "initial"}], node_outcomes=[]))
    _install_fault(monkeypatch, _Fault(path.with_suffix("") / "files.jsonl",
        poison="first-delta", times=2))
    assert not writer.append_file_row({"file": "/repo/same.py", "attempt": "first-delta", "error": "old"},
        delta={"unset": [], "extend": {"warnings": [{"message": "first"}]}}, nodes_reflected=0)
    assert not writer.append_file_row({"file": "/repo/same.py", "attempt": "second-delta"},
        delta={"unset": ["error"], "extend": {"warnings": [{"message": "second"}]}}, nodes_reflected=0)
    writer.finalize({"status": "completed"})
    current = joined(path)["result_summary"]["files"][0]
    assert current["attempt"] == "second-delta" and "error" not in current
    assert current["warnings"] == [{"message": text} for text in ("initial", "first", "second")]
    assert current["node_outcomes"] == [] and current["nodes_satisfied"] == 0


@pytest.mark.parametrize("kind", ["set", "item", "node"])
def test_same_key_pending_values_and_history_keep_return_order(tmp_path, monkeypatch, kind):
    writer, path = writer_at(tmp_path, type_="CARTO_BULK_APPLY", mode="all")
    stream = "nodes" if kind == "node" else "files"
    _install_fault(monkeypatch, _Fault(path.with_suffix("") / f"{stream}.jsonl", poison="old-attempt", times=2))
    if kind == "set":
        writer.set_value("publication", {"attempt": "old-attempt"})
        writer.set_value("publication", {"attempt": "latest-attempt"})
    elif kind == "item":
        writer.append_item("errors", "old-attempt")
        writer.append_item("errors", "latest-attempt")
    else:
        for attempt in ("old-attempt", "latest-attempt"):
            writer.append_node_rows([{"file": "/repo/same.py", "row": {"node_id": attempt, "success": True}}])
    terminal = writer.finalize({"status": "completed"})
    summary = joined(path, overlay=terminal)["result_summary"]
    if kind == "set":
        assert summary["publication"] == {"attempt": "latest-attempt"}
    else:
        values = summary["errors"] if kind == "item" else [item["node_id"] for item in summary["node_outcomes"]]
        assert values == ["old-attempt", "latest-attempt"]


def test_external_current_head_value_survives_progress_write(tmp_path, monkeypatch):
    writer, path = writer_at(tmp_path)
    writer.update_head(snapshot_path="admission", error=None, error_kind=None)
    current = json.loads(path.read_text())
    current.update(snapshot_path="freshness", new_foreign_key="kept", error="governance refused", error_kind="governance-error")
    current["progress"] = {"foreign_progress": 5, "completed_files": 8}
    assert cfg.merge_write_carto_job_status(path, current)
    writer.update_head(progress={"current_phase": "file-head"})
    current = json.loads(path.read_text())
    assert current["new_foreign_key"] == "kept" and current["snapshot_path"] == "freshness"
    assert current["error"] == "governance refused" and current["error_kind"] == "governance-error"
    assert current["progress"] == {"foreign_progress": 5, "completed_files": 8, "current_phase": "file-head"}
    # An explicit own write still owns this key; status precedence stays in cfg.
    writer.update_head(snapshot_path="writer-selected", progress={"completed_files": 9})
    assert json.loads(path.read_text())["snapshot_path"] == "writer-selected"
    assert json.loads(path.read_text())["progress"]["completed_files"] == 9
    funnel = cfg.merge_write_carto_job_status
    with monkeypatch.context() as failed:
        failed.setattr(cfg, "merge_write_carto_job_status", lambda *args, **kwargs: False)
        assert not writer.update_head(snapshot_path="unlanded-own-value")
    current = json.loads(path.read_text())
    current["snapshot_path"] = "new-foreign-value"
    assert funnel(path, current)
    writer.update_head(progress={"completed_files": 10})
    assert json.loads(path.read_text())["snapshot_path"] == "new-foreign-value"


def test_f2_stale_admission_head_keeps_running_and_freshness_written_by_runner(tmp_path):
    from workflowy_mcp import server as srv, carto_job_governance as gov
    path = tmp_path / "20261027-000000_freshness.json"
    writer = rec.record_for(path)
    initial = writer.initial_payload({"id": "freshness", "type": "CARTO_REFRESH",
        "mode": "file", "status": "preparing", "snapshot_path": None,
        "snapshot_lineage": None, "freshness_disposition": None, "error": None})
    gov.atomic_write_json(path, initial)
    srv._merge_carto_task_status(str(path), {"status": "running", "snapshot_path": "selected.json",
        "snapshot_lineage": {"selected": True}, "freshness_disposition": "performed"})
    writer.update_head(progress={"nodes_created": 1})
    during = json.loads(path.read_text())
    assert during["status"] == "running"
    assert during["snapshot_path"] == "selected.json" and during["snapshot_lineage"] == {"selected": True}
    assert during["freshness_disposition"] == "performed"
    writer.finalize({"status": "completed"})
    final = json.loads(path.read_text())
    assert final["status"] == "completed" and final["snapshot_path"] == "selected.json"
    assert final["snapshot_lineage"] == {"selected": True} and final["freshness_disposition"] == "performed"


@pytest.mark.parametrize("summary_key", [False, True])
def test_f3_moved_value_can_shrink_in_pure_and_paged_detail(tmp_path, summary_key):
    writer, path = writer_at(tmp_path)
    writer.append_file_row(row())
    name = "extension_fact" if summary_key else "error_extra"
    for value in ({"message": "old" * 3000}, {"message": "latest"}):
        writer.update_head(**({"result_summary": {name: value}} if summary_key else {name: value}))
    writer.finalize({"status": "completed"})
    stored = json.loads(path.read_text())
    folded = rec.fold_carto_job_evidence((path.with_suffix("") / "files.jsonl").read_text())
    pure = rec.stitch_carto_job_detail(stored, folded)
    paged = joined(path)
    for detail in (stored, pure, paged):
        target = detail["result_summary"] if summary_key else detail
        assert target[name] == {"message": "latest"}
    # Returning to the old large value must supersede the small set too.
    writer.update_head(**({"result_summary": {name: {"message": "old" * 3000}}} if summary_key else
                          {name: {"message": "old" * 3000}}))
    target = joined(path)["result_summary"] if summary_key else joined(path)
    assert target[name] == {"message": "old" * 3000}


@pytest.mark.asyncio
async def test_folder_recovered_first_attempt_keeps_latest_canonical_result(tmp_path, monkeypatch):
    from test_carto_g4_folder_jr2 import synthetic_job
    path = tmp_path / "20261027-000000_g4-folder.json"
    _install_fault(monkeypatch, _Fault(path.with_suffix("") / "files.jsonl", poison="complete anatomy", times=2))
    result = await synthetic_job(monkeypatch, tmp_path, cases=("changed", "again-clean"))
    assert result.attempted == [0, 1]
    saved = json.loads(path.read_text())
    assert saved["result_summary"]["findings_total"] == 0
    current = result.detail["result_summary"]["files"][0]
    assert current["findings"] == [] and current["changed"] is False
