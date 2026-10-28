"""Q6 (3PS9ID): bounded stored terminal text, exact complete derived detail.

The original two-cell question is now a strict differential, not an exemption.
The source paths are synthetic and fixed-length, so its 5,253-character witness
does not depend on pytest's temporary directory, worker id or invocation number.
"""
import copy
import json
from pathlib import Path

import pytest

from test_carto_g4_folder_jr2 import synthetic_job, base_folder_definition, comparable_detail
from workflowy_mcp import carto_job_record as records, server as srv


SOURCE_ROOT = Path("/q6/" + "s" * 75)  # Fake physical keys only; no file is opened here.


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 25])
async def test_complete_failed_folder_error_keeps_the_jr2_base_text(monkeypatch, tmp_path, count):
    cases = ("local",) * count
    old = await synthetic_job(monkeypatch, tmp_path, switched=False, method=base_folder_definition(),
                              cases=cases, source_directory=SOURCE_ROOT)
    expected = old.detail["error"]
    last = old.detail["result_summary"]["errors"][-1]
    old.path.unlink()
    current = await synthetic_job(monkeypatch, tmp_path, cases=cases, source_directory=SOURCE_ROOT)
    assert current.detail["result_summary"]["errors"] == old.detail["result_summary"]["errors"]
    print(json.dumps({"count": count, "base_phase": old.detail["error_extra"]["phase"],
        "current_phase": current.detail["error_extra"]["phase"], "base_error_chars": len(expected),
        "current_error_chars": len(current.detail["error"]), "base_contains_last": last in expected,
        "current_contains_last": last in current.detail["error"]}))
    assert current.detail["error"] == expected
    assert comparable_detail(current.detail, tmp_path) == comparable_detail(old.detail, tmp_path)
    assert last in current.detail["error"]
    assert "error_derivation" not in current.detail
    saved = json.loads(current.path.read_text())
    recipe = saved["error_derivation"]["$carto_terminal_text"]
    assert recipe["source"] == "refresh_errors"
    assert saved["error"] == recipe["prefix"] + str(saved["result_summary"]["refresh_errors"]) + recipe["suffix"]
    assert len(json.dumps(saved["error_derivation"])) < 512
    for proof in (current.control.terminal_result, current.proof):
        assert proof["error"] == saved["error"]
        assert dict(proof["error_derivation"]["$carto_terminal_text"]) == recipe
    if count == 25:
        assert len(expected) == 5253 and len(saved["error"]) == 4228
        assert last not in saved["error"] and len(saved["result_summary"]["refresh_errors"]) == 20
        assert len(saved["error"].encode()) > 4096  # The bounded text must remain a readable string inline.
    folded = records.fold_carto_job_evidence((current.path.with_suffix("") / "files.jsonl").read_bytes(),
        status=saved["status"])
    assert records.stitch_carto_job_detail(saved, folded)["error"] == expected
    before_summary_read = current.path.read_bytes()
    original_open = Path.open
    def no_evidence_read(path, mode="r", *args, **kwargs):
        assert path.name not in {"files.jsonl", "nodes.jsonl"} or "r" not in mode
        return original_open(path, mode, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", no_evidence_read)
        summary = records.read_carto_job_view(current.path, job_id="g4-folder", options={"selection": "summary"})
    assert summary["error"] == saved["error"] and summary["error_derivation"] == saved["error_derivation"]
    assert current.path.read_bytes() == before_summary_read


@pytest.mark.parametrize("count", [25, 2000])
@pytest.mark.parametrize("source", ["errors", "refresh_errors"])
def test_derived_terminal_text_stays_at_the_20_item_bound_without_a_run_read(monkeypatch, tmp_path, count, source):
    writer = records.record_for(tmp_path / "bounded.json")
    writer.create({"id": "bounded", "type": "CARTO_REFRESH", "mode": "folder", "status": "running"})
    first_twenty = []
    original_open = Path.open
    def append_only(path, mode="r", *args, **kwargs):
        assert path.name != "files.jsonl" or "r" not in mode, "writer read earlier evidence"
        return original_open(path, mode, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", append_only)
        for i in range(count):
            message = f"failed-{i:04d}: " + "x" * 210
            if i < 20:
                first_twenty.append(message)
            writer.append_file_row({"file": str(tmp_path / f"{i}.py"), "status": "failed", "error": message})
            writer.append_item(source, message)
        metadata = {"job_id": "bounded", "job_type": "CARTO_REFRESH", "mode": "folder", "root_uuid": "root",
            "claim_id": "claim", "pid": 1, "phase": "folder:ignore-summary", "exception": None,
            "stopped": False, "restart_observed": False, "scheduling_failed": False, "updated_at": "fixed",
            "may_have_workflowy_effects": False, "writer_job_file": str(writer.path)}
        payload = srv._carto_terminal_payload_data(metadata, writer.summary(), {})
        proof = writer.finalize(payload)
    recipe = proof["error_derivation"]["$carto_terminal_text"]
    assert recipe == {"formatter": "_carto_terminal_payload_data:str/1", "source": source,
        "prefix": "Cartographer job bounded failed during folder:ignore-summary: RuntimeError: ",
        "suffix": ". The daemon is still serving; inspect the job result before retrying."}
    assert proof["error"] == recipe["prefix"] + str(first_twenty) + recipe["suffix"]
    assert isinstance(proof["error"], str) and len(json.dumps(recipe)) < 512
    saved = json.loads(writer.path.read_text())
    assert saved["error"] == proof["error"] and saved["error_derivation"] == proof["error_derivation"]
    assert saved["result_summary"]["preview_totals"][source] == count
    assert len(saved["result_summary"][source]) == 20
    # The only growing strings are the original per-item evidence lines, not a
    # second derived list-text set line or a full run list in the proof.
    lines = [json.loads(line) for line in (writer.path.with_suffix("") / "files.jsonl").read_text().splitlines()]
    assert len(lines) == 2 * count
    assert not any(line["k"] == "set" for line in lines)


@pytest.mark.parametrize("marker", [{"$carto_terminal_text": {"formatter": "future/99"}},
    {"$carto_terminal_text": {"formatter": "_carto_terminal_payload_data:str/1", "source": [], "prefix": "", "suffix": ""}},
    {"$future_terminal_text": None}])
def test_unknown_terminal_text_recipe_warns_and_keeps_readable_summary_and_evidence(monkeypatch, tmp_path, caplog, marker):
    path = tmp_path / "unknown.json"
    record = {"id": "unknown", "record_layout": "summary-head/2", "status": "failed", "error": "readable bounded text",
              "error_derivation": marker, "result_summary": {"errors": ["preview"]}}
    path.write_text(json.dumps(record))
    original = path.read_bytes()
    head = records.read_carto_job_record_cached(path)
    assert head["error_derivation"] == marker and "Unknown Cartographer terminal-text derivation" in caplog.text
    detail = records.stitch_carto_job_detail(record, {"items": {"errors": ["complete", "last"]}})
    assert detail["error"] == record["error"] and detail["error_derivation"] == marker
    assert detail["result_summary"]["errors"] == ["complete", "last"] and path.read_bytes() == original


@pytest.mark.parametrize("layout", [None, "summary-head/1", "summary-head/2"])
def test_legacy_and_markerless_c2_terminal_strings_are_not_inferred_or_rewritten(tmp_path, layout):
    record = {"status": "failed", "error": "already stored text", "result_summary": {"errors": ["original"]}}
    if layout is not None:
        record["record_layout"] = layout
    before = copy.deepcopy(record)
    detail = records.stitch_carto_job_detail(record, {"items": {"errors": ["different complete item"]}})
    assert detail["error"] == record["error"] and "error_derivation" not in detail and record == before
