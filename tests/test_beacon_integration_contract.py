"""J joined witnesses enter real daemon FILE paths; historical controls are labeled.

J-1 through J-4 cover the real protected joins. Component-only controls remain
controls, not activation evidence for a daemon gesture.
"""
from __future__ import annotations

import asyncio
import ast
import copy
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
from n07_job_harness import real_job_environment, scrubbed_runtime, launch_real_job
from workflowy_mcp.client import api_client_nexus as acn

import markdown_roundtrip as mdr
import nexus_map_codebase as nmc
from beacon_source import read_source_snapshot
from workflowy_mcp import beacon_refresh_support as support, server as srv
from workflowy_mcp.mcp_api_registry import dispatch_operation
from test_beacon_structure_recovery import block, corrupt
from test_beacon_batch_local_admission import setup_batch, _run
from test_beacon_markdown_preparation import heading


@pytest.fixture(autouse=True)
def _j_counterfactual(monkeypatch):
    from j_counterfactual import install_j_counterfactual
    install_j_counterfactual(monkeypatch)


def _ids(nodes):
    return {str(n["id"]) for n in nodes if n.get("id")} | set().union(
        *(_ids(n.get("children") or []) for n in nodes))


class ComponentOnlyControl:
    """Historical component-only control, never a joined production witness."""
    def __init__(self):
        self.calls, self.deleted, self.created = [], [], []
        self.existing = {}

    def _reconcile(self, path, policy):
        self.calls.append("seed/reconcile")
        assert policy["reconcile_children"] is not None
        old = {"id": "file", "children": self.existing.get(str(path), [])}
        desired = {"children": copy.deepcopy(policy["reconcile_children"] + policy["retained_sections"])}
        # Real U03 identity matcher, fake Workflowy execution boundary.
        nmc.reconcile_trees_cartographer(desired, old)
        self.deleted.extend(sorted(_ids(old["children"]) - _ids(desired["children"])))
        for i, node in enumerate(desired["children"]):
            if not node.get("id"):
                node["id"] = f"new-{i}"
                self.created.append(node["id"])
        self.existing[str(path)] = desired["children"]
        return desired

    def refresh(self, paths, *, dry=False, claim="parent", new_files=()):
        rows = []
        for path in paths:
            self.calls.append("prepare")
            prepared = support.prepare_beacon_source(path, dry_run=dry, claim_id=claim)
            self.calls.append("projection")
            policy = support.beacon_refresh_projection(prepared, existing_children=self.existing.get(str(path), []))
            row = {**prepared, "preserve_existing": policy["preserve_existing"],
                   "projection": policy, "new_file": path in new_files}
            if not policy["preserve_existing"]:
                seeded = self._reconcile(path, policy)
                # R1/R2: U03's report is read AFTER seeding too, not lost.
                row["reconcile_report"] = seeded.get("_beacon_reconcile_report", {})
                row["status"] = "refreshed" if policy["state"] == "valid" else "partial_section"
            rows.append(row)
        self.calls.append("aggregate")
        aggregate = support.aggregate_beacon_outcomes(rows)
        self.calls.append("terminal")
        return support.beacon_job_outcome(aggregate)

    def regenerate(self, path, generated, forest, *, phase8=None, fail_before_publish=False):
        self.calls.append("observe-before-phase6")
        before = read_source_snapshot(path)
        self.calls.append("prepare-generated")
        prepared = mdr.prepare_generated_markdown_beacons(generated, forest, before)
        if fail_before_publish:
            return prepared
        self.calls.append("publish-once")
        publication = mdr.publish_generated_markdown_beacons(prepared["publication_plan"])
        row = {"file": str(path), "status": "committed" if publication["success"] else "failed",
               "changed": publication["changed"], "publication": publication,
               **{k: prepared[k] for k in ("outcomes", "findings", "omissions", "relocations", "analysis_error")}}
        self.calls.append("phase8")
        self.calls.append("aggregate")
        aggregate = support.aggregate_beacon_outcomes([row])
        self.calls.append("terminal")
        return support.beacon_job_outcome(aggregate, phase8=phase8)


@pytest.fixture
def component_only():
    return ComponentOnlyControl()


def _write(path, rows):
    path.write_bytes(("\n".join(rows) + "\n").encode())


def _state(path):
    return path.read_bytes(), path.stat().st_mtime_ns


def test_component_only_r1_r2_retention_control(component_only, tmp_path):
    joined = component_only
    path = tmp_path / "retain.py"
    rows = block("python", "keep") + ["def keep():", "    return 1"]
    _write(path, rows)
    original = support.prepare_beacon_source(path)["map"]["children"]
    original[0]["id"] = "existing-uuid"
    joined.existing[str(path)] = original
    _write(path, rows + ["def broken(:"])
    before = _state(path)
    bad = joined.refresh([path])
    assert joined.calls == ["prepare", "projection", "aggregate", "terminal"]
    assert joined.created == joined.deleted == []
    assert _ids(joined.existing[str(path)]) == {"existing-uuid"}
    assert bad["files"][0]["preserve_existing"] and bad["status"] == "failed"
    assert bad["findings_by_code"]["mapper_parse_error"] == 1 and _state(path) == before
    _write(path, rows)
    joined.calls.clear()
    good = joined.refresh([path])
    assert joined.calls == ["prepare", "projection", "seed/reconcile", "aggregate", "terminal"]
    assert joined.created == joined.deleted == []
    assert _ids(joined.existing[str(path)]) == {"existing-uuid"}
    assert good["status"] == "completed"


def test_component_only_r2_r3_folder_control(component_only, tmp_path):
    joined = component_only
    clean, corrupt_file, unreadable, new = [tmp_path / n for n in ("clean.py", "corrupt.py", "unreadable.md", "new.py")]
    _write(clean, block("python", "clean") + ["def clean():", "    return 1"])
    _write(corrupt_file, corrupt("python", 1) + ["def bad():", "    return 1"] + block("python", "good") + ["def good():", "    return 2"])
    unreadable.write_bytes(b"\xffnot utf8\n")
    _write(new, block("python", "new") + ["def fresh():", "    return 3"])
    before = [_state(p) for p in (clean, corrupt_file, unreadable, new)]
    result = joined.refresh([clean, corrupt_file, unreadable, new], new_files=[unreadable, new])
    assert result["status"] == "completed_with_warnings" and result["partial_success"]
    assert (result["successful"], result["failed"], result["skipped"]) == (3, 1, 0)
    assert result["files"][2]["new_file"] and result["files"][2]["error_kind"] == "not_utf8"
    assert result["files"][3]["new_file"] and result["files"][3]["status"] == "refreshed"
    assert result["findings_by_code"]["unterminated_block"] == 1
    assert result["findings_by_code"]["file_unreadable"] == 1
    assert [_state(p) for p in (clean, corrupt_file, unreadable, new)] == before


def test_r2_duplicate_evidence_includes_unmapped_physical_copy(tmp_path):
    path = tmp_path / "duplicate.py"
    _write(path, block("python", "twice") + ["def first():", "    return 1", ""] +
           block("python", "twice") + ["value = 2"])
    result = support.prepare_beacon_source(path)
    occurrences = result["duplicate_source_beacon_ids"]["twice"]
    assert len(occurrences) == 2 and occurrences[0]["line"] < occurrences[1]["line"]
    assert all(row["path"].startswith(str(path) + ":") for row in occurrences)
    assert any(f["code"] == "block_not_mapped" for f in result["findings"])


@pytest.mark.parametrize("reason,satisfied", [("already_satisfied", True), ("already_absent", True),
    ("beacon_id_not_found_on_disk", False), ("note_beacon_id_missing_on_disk", False), (None, False)])
def test_r7_only_explicit_satisfied_noops_allow_followup_refresh(reason, satisfied):
    raw = {"success": False, "operation": "noop", "noop_reason": reason, "findings": [{"code": "duplicate_id"}]}
    result = support.beacon_legacy_outcome(raw)
    assert result["success"] is result["satisfied"] is satisfied
    assert result["writer_success"] is False and result["findings"] == raw["findings"]
    rejected = support.beacon_legacy_outcome({**raw, "noop_reason": "already_satisfied", "error": "rejected edit"})
    assert rejected["success"] is rejected["satisfied"] is False
    assert raw["success"] is False  # data projection never rewrites its input


def test_r7_interrupted_delete_rerun_is_satisfied_but_unknown_labels_are_not(tmp_path):
    path = tmp_path / "rerun.py"
    _write(path, block("python", "good") + ["def good():", "    return 1"])
    note = "AST_QUALNAME: good\nBEACON (AST)\nid: good\n"
    first = nmc.update_beacon_from_node_python(str(path), "good", note)
    assert first["operation"] == "deleted_beacon"
    after = _state(path)
    rerun = nmc.update_beacon_from_node_python(str(path), "good", note)
    assert rerun["operation"] == "noop" and rerun["noop_reason"] == "no_beacon_at_target"
    projected = support.beacon_legacy_outcome({**rerun, "success": False})
    assert projected["success"] is True and projected["noop_reason"] == "already_absent"
    assert _state(path) == after
    unknown = nmc.update_beacon_from_node_python(str(path), "missing #intended", "AST_QUALNAME: missing\nBEACON (AST)\nid: missing\n")
    assert support.beacon_legacy_outcome({**unknown, "success": False})["satisfied"] is False
    assert _state(path) == after
    safe = nmc.update_beacon_from_node_python(str(path), "good #new", "AST_QUALNAME: good\n")
    assert safe["operation"] == "created_beacon" and b"slice_labels=new," in path.read_bytes()


def test_r4_allowed_bulk_projection_keeps_failclosed_trigger_and_all_node_data(monkeypatch):
    results = [
        {"success": True, "writer_success": None, "operation": "updated_beacon", "findings": [{"code": "duplicate_id"}]},
        {"success": False, "operation": "noop", "noop_reason": "already_absent", "findings": [{"code": "orphan_close"}]},
        {"success": False, "operation": "noop", "noop_reason": "beacon_id_not_found_on_disk"},
    ]
    class Client:
        async def update_cached_node_name(self, **kwargs):
            pass
        async def update_beacon_from_node(self, **kwargs):
            return copy.deepcopy(results[int(kwargs["node_id"])])
    monkeypatch.setattr(srv, "_node_name_tags_match_beacon_block_in_note", lambda node: False)
    summary = {"node_updates": 0, "nodes_skipped": 0, "nodes_skipped_in_sync": 0, "errors": []}
    count, cancelled = asyncio.run(srv._bulk_apply_per_file_inner_loop(
        Client(), [{"id": str(i), "name": "node"} for i in range(3)], "one.py", summary,
        lambda: False, lambda msg: None, lambda: None))
    assert count == summary["node_updates"] == 1 and cancelled is False
    assert summary["nodes_skipped"] == 2 and len(summary["errors"]) == 2
    assert summary["nodes_satisfied"] == 1 and len(summary["node_outcomes"]) == 3
    assert summary["node_outcomes"][1]["success"] is False
    assert summary["node_outcomes"][0]["writer_success"] is None
    assert summary["node_outcomes"][1]["noop_reason"] == "already_absent"
    assert [r["findings"] for r in summary["node_outcomes"]] == [r.get("findings", []) for r in results]


def test_component_only_r4_r7_bulk_projection_control():
    """J retargets at the protected job; this exercises supplied adapters only."""
    raw = [{"success": True, "operation": "updated_beacon"},
           {"success": False, "operation": "noop", "noop_reason": "already_absent"},
           {"success": False, "operation": "noop", "noop_reason": "beacon_id_not_found_on_disk"}]
    nodes = [support.beacon_legacy_outcome(r) for r in raw]
    assert [n["success"] for n in nodes] == [True, True, False]
    result = support.beacon_job_outcome(support.aggregate_beacon_outcomes([
        {"file": "good.py", "status": "refreshed", "changed": True, "node_outcomes": nodes[:2],
         "warnings": [{"type": "duplicate_source_beacon_id"}]},
        {"file": "unresolved.py", "status": "failed", "node_outcomes": nodes[2:], "error": "unknown identity"}]))
    assert result["status"] == "completed_with_warnings" and result["partial_success"]
    assert result["warnings_by_type"] == {"duplicate_source_beacon_id": 1}
    assert (result["successful"], result["failed"]) == (1, 1)


async def _joined_bulk_job(env, candidates):
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    async with cache_operation_boundary(env.client, "j:joined-bulk-front"):
        ack = await srv._start_carto_bulk_visible_apply_job(root_uuid=env.ids["file"], mode="file",
            visible_tree_payload={"root": {"id": env.ids["file"], "name": "source.py", "note": "Path: source.py"},
                                  "children": candidates})
    assert ack["success"], ack
    await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    assert not env.probe.violations, env.probe.violations
    return await _joined_complete_detail(ack["job_id"])


@pytest.mark.asyncio
async def test_r4_r7_joined_bulk_keeps_refresh_warning_and_partial_success(real_job_environment, monkeypatch):
    """Real writers/FILE/terminal; rejected nodes share the successful file."""
    env = real_job_environment
    path = env.repo / "source.py"
    original_commit = nmc.commit_beacon_edit_plan
    commits, analyses, writer_rows = [], [], []
    def commit(plan, **kwargs):
        if commits:
            path.write_bytes(path.read_bytes() + b"\n# external save\n")
        result = original_commit(plan, **kwargs)
        commits.append(result)
        return result
    def optional_failure(*args, **kwargs):
        raise ValueError("j3 optional alias diagnostic")
    analyze = support.analyze_beacon_snapshot
    def analyzed(snapshot, **kwargs):
        analyses.append(snapshot.sha256)
        return analyze(snapshot, **kwargs)
    writer = acn.WorkFlowyClientNexus.update_beacon_from_node
    async def written(*args, **kwargs):
        row = await writer(*args, **kwargs)
        writer_rows.append(row)
        return row
    monkeypatch.setattr(nmc, "commit_beacon_edit_plan", commit)
    monkeypatch.setattr(nmc, "normalize_beacon_comment_field_aliases_in_file", optional_failure)
    monkeypatch.setattr(support, "analyze_beacon_snapshot", analyzed)
    monkeypatch.setattr(acn.WorkFlowyClientNexus, "update_beacon_from_node", written)
    rows = _observe_joined_file(monkeypatch)
    terminal = await _joined_bulk_job(env, [
        {"id": env.ids["symbol"], "name": "existing #j3", "note": "AST_QUALNAME: existing"},
        {"id": env.ids["symbol"], "name": "existing #j3", "note": "AST_QUALNAME: existing"},
        {"id": env.ids["obsolete"], "name": "missing #j3", "note": "AST_QUALNAME: missing\nBEACON (AST)\nid: missing\n"},
        {"id": env.ids["obsolete"], "name": "existing #rejected", "note": "AST_QUALNAME: existing"},
    ])
    summary = terminal["result_summary"]
    assert terminal["status"] == "completed_with_warnings", terminal
    assert summary["partial_success"] and (summary["successful"], summary["failed"]) == (0, 1)
    assert summary["files_changed"] == [str(path.resolve())]
    assert summary["node_updates"] == 2 and summary["nodes_skipped"] == 2
    assert summary["nodes_satisfied"] == 1 and len(summary["node_outcomes"]) == 4
    row = summary["files"][0]
    assert row["status"] == "failed" and row["changed"]
    assert row["node_outcomes"] == summary["node_outcomes"]
    assert [r["writer_success"] for r in row["node_outcomes"]] == [r["writer_success"] for r in writer_rows]
    assert len(rows) == len(analyses) == 1
    assert row["findings"] == json.loads(json.dumps(rows[0]["findings"]))
    assert row["refresh_outcome"]["warnings"] == json.loads(json.dumps(rows[0]["warnings"]))
    assert any("j3 optional alias diagnostic" in str(w) for w in summary["warnings"])
    assert commits[0]["success"] and commits[1]["error_kind"] == "source_changed_since_plan"
    assert terminal["warning_count"] == summary["warnings_total"]


@pytest.mark.asyncio
@pytest.mark.parametrize("count,unknown", [(533, False), (1, True)], ids=["many-satisfied", "unknown-only"])
async def test_r4_final_observation_and_precise_refresh_trigger(real_job_environment, monkeypatch, count, unknown):
    env = real_job_environment
    path = env.repo / "source.py"
    if not unknown:
        nmc.update_beacon_from_node_python(str(path), "existing #j3", "AST_QUALNAME: existing")
    before = _state(path)
    analyses = []
    analyze = support.analyze_beacon_snapshot
    def analyzed(snapshot, **kwargs):
        analyses.append(snapshot.sha256)
        return analyze(snapshot, **kwargs)
    monkeypatch.setattr(support, "analyze_beacon_snapshot", analyzed)
    rows = _observe_joined_file(monkeypatch)
    candidate = {"id": env.ids["symbol"], "name": "missing #j3" if unknown else "existing #j3",
                 "note": "AST_QUALNAME: missing\nBEACON (AST)\nid: missing\n" if unknown else "AST_QUALNAME: existing"}
    terminal = await _joined_bulk_job(env, [dict(candidate) for _ in range(count)])
    summary = terminal["result_summary"]
    assert len(analyses) == 1 and len(rows) == (0 if unknown else 1)
    assert _state(path) == before and summary["files_changed"] == []
    assert len(summary["node_outcomes"]) == count
    assert summary["nodes_satisfied"] == (0 if unknown else count)
    assert summary["node_updates"] == (0 if unknown else count)
    assert terminal["status"] == ("failed" if unknown else "completed_with_warnings")
    if not unknown:
        assert summary["failed"] == 0 and summary["findings_by_code"] == {"empty_comment": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True], ids=["control", "cancel-inflight-commit"])
async def test_r4_inflight_commit_receipt_survives_cpu_checkpoint(real_job_environment, monkeypatch, stop):
    """A stop during the SECOND real commit retains both actual node effects."""
    import threading
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    env = real_job_environment
    path = env.repo / "source.py"
    before = path.read_bytes()
    entered, release = threading.Event(), threading.Event()
    commits = []
    commit = nmc.commit_beacon_edit_plan
    def publish(plan, **kwargs):
        if commits:
            entered.set()
            assert release.wait(15), "test did not release publisher"
        result = commit(plan, **kwargs)
        commits.append(result)
        return result
    monkeypatch.setattr(nmc, "commit_beacon_edit_plan", publish)
    async with cache_operation_boundary(env.client, "j:r4-cancel-front"):
        ack = await srv._start_carto_bulk_visible_apply_job(root_uuid=env.ids["file"], mode="file",
            visible_tree_payload={"root": {"id": env.ids["file"], "name": "source.py", "note": "Path: source.py"},
                "children": [
                    {"id": env.ids["symbol"], "name": "existing #j3", "note": "AST_QUALNAME: existing"},
                    {"id": env.ids["symbol"], "name": "existing #j3second", "note": "AST_QUALNAME: existing"}]})
    assert ack["success"], ack
    control, task = env.controls[ack["job_id"]], env.tasks[ack["job_id"]]
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 15), 16)
        if stop:
            control.request_user_cancel()
            assert not control.user_observed and not task.done()
    finally:
        release.set()
    await asyncio.wait_for(task, 60)
    terminal = await _joined_complete_detail(ack["job_id"])
    summary = terminal["result_summary"]
    assert len(commits) == 2 and all(r["success"] and r["changed"] for r in commits)
    assert path.read_bytes() != before and not env.probe.violations
    assert summary["files_changed"] == [str(path.resolve())]
    assert len(summary["node_outcomes"]) == len(summary["files"][0]["node_outcomes"]) == 2
    assert summary["node_updates"] == (1 if stop else 2)
    assert [r["after_sha256"] for r in summary["node_outcomes"]] == [r["after_sha256"] for r in commits]
    assert terminal["status"] == ("cancelled" if stop else "completed_with_warnings")
    if not stop:
        assert summary["failed"] == 0 and summary["findings_by_code"] == {"empty_comment": 1}
    if stop:
        assert control.user_observed and summary["node_outcomes"][-1]["returned_to_caller"] is False
    stored = await _joined_complete_detail(ack["job_id"])
    assert stored["result_summary"]["files_changed"] == summary["files_changed"]


@pytest.mark.asyncio
async def test_r4_nested_bulk_local_mapper_provenance_is_not_runwide(real_job_environment, monkeypatch):
    env = real_job_environment
    def mapped(*args, **kwargs):
        raise RuntimeError("j3 exact local mapper failure")
    monkeypatch.setattr(nmc, "map_source_snapshot", mapped)
    terminal = await _joined_bulk_job(env, [
        {"id": env.ids["symbol"], "name": "existing #j3", "note": "AST_QUALNAME: existing"}])
    summary = terminal["result_summary"]
    assert terminal["status"] == "completed_with_warnings", terminal
    row = summary["files"][0]
    assert row["local_cpu_failure"] == {"file": str((env.repo / "source.py").resolve()),
        "phase": "file:source", "exception_type": "RuntimeError", "message": "j3 exact local mapper failure"}
    assert summary["files_changed"] and summary["partial_success"]
    control = next(iter(env.controls.values()))
    assert control.cpu_failure[1].args == ("j3 exact local mapper failure",)


@pytest.mark.asyncio
@pytest.mark.parametrize("map_failure", [False, True], ids=["clean-observation", "local-observation-failure"])
async def test_r4_actual_physical_files_keep_independent_outcomes(real_job_environment, monkeypatch, map_failure):
    """Actual cache ancestry wins for evidence without changing frozen GUI groups."""
    env = real_job_environment
    observations = []
    analyze = support.analyze_beacon_snapshot
    def analyzed(snapshot, **kwargs):
        observations.append(snapshot.canonical_path)
        return analyze(snapshot, **kwargs)
    monkeypatch.setattr(support, "analyze_beacon_snapshot", analyzed)
    mapped = nmc.map_source_snapshot
    def map_one(snapshot, **kwargs):
        if map_failure and Path(snapshot.canonical_path).name == "other.py":
            raise RuntimeError("j3 other-file final observation failed")
        return mapped(snapshot, **kwargs)
    monkeypatch.setattr(nmc, "map_source_snapshot", map_one)
    rows = _observe_joined_file(monkeypatch)
    terminal = await _joined_bulk_job(env, [
        {"id": env.ids["symbol"], "name": "existing #j3", "note": "AST_QUALNAME: existing"},
        {"id": env.ids["obsolete"], "name": "missing #j3", "note": "AST_QUALNAME: missing\nBEACON (AST)\nid: missing\n"},
        {"id": env.ids["other-symbol"], "name": "other #j3", "note": "AST_QUALNAME: other"},
    ])
    summary = terminal["result_summary"]
    assert terminal["status"] == "completed_with_warnings" and summary["partial_success"]
    assert (summary["successful"], summary["failed"]) == ((0, 2) if map_failure else (1, 1))
    expected = {str((env.repo / p).resolve()) for p in ("source.py", "other.py")}
    assert set(summary["files_changed"]) == expected
    assert set(observations) == (expected - {str((env.repo / "other.py").resolve())} if map_failure else expected)
    assert len(observations) == (1 if map_failure else 2) and len(rows) == 1
    per_file = {r["file"]: r for r in summary["files"]}
    assert len(per_file[str((env.repo / "source.py").resolve())]["node_outcomes"]) == 2
    assert len(per_file[str((env.repo / "other.py").resolve())]["node_outcomes"]) == 1
    if map_failure:
        assert per_file[str((env.repo / "other.py").resolve())]["local_cpu_failure"]["message"] == "j3 other-file final observation failed"


def test_component_only_r5_r6_publication_control(component_only, tmp_path, monkeypatch):
    joined = component_only
    path = tmp_path / "generated.md"
    path.write_bytes(b"old disk\r\n")
    before = _state(path)
    generated = b"# Good\n\nGenerated body.\n"
    forest = {"children": [heading("Good #good", "good", node_id="good-uuid")]}
    joined.regenerate(path, generated, forest, fail_before_publish=True)
    assert _state(path) == before and joined.calls == ["observe-before-phase6", "prepare-generated"]
    joined.calls.clear()
    commits = []
    original = mdr.publish_generated_markdown_beacons
    def publish(plan):
        commits.append(plan)
        return original(plan)
    monkeypatch.setattr(mdr, "publish_generated_markdown_beacons", publish)
    result = joined.regenerate(path, generated, forest, phase8={"success": False, "error": "refresh unavailable"})
    assert joined.calls == ["observe-before-phase6", "prepare-generated", "publish-once", "phase8", "aggregate", "terminal"]
    assert len(commits) == 1 and path.read_bytes() != before[0]
    assert b"id=good," in path.read_bytes() and b"Generated body." in path.read_bytes()
    assert result["files_changed"] == [str(path)] and result["files"][0]["publication"]["success"]
    assert result["status"] == "completed_with_warnings" and result["phase8"]["success"] is False


def _markdown_fixture(env):
    """Seed fake HTTP and its matching COW view with one tagged live heading."""
    from n05_region_harness import publish_fixture_nodes
    from test_beacon_markdown_preparation import note
    nodes = [dict(n) for n in env.initial_nodes]
    for node in nodes:
        if node["id"] == env.ids["heading"]:
            node.update(name="Heading #j4", note=note("j4-live", "live heading", path=["Heading"]))
    env.probe.nodes = {n["id"]: {"createdAt": 100, "modifiedAt": 100, "note": "", "priority": 0, **n} for n in nodes}
    publish_fixture_nodes(env.client, nodes)
    return env.repo / "doc.md"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["existing", "missing", "local-refresh", "mapper-escape", "cas", "proof", "preparation-abort", "heading-failure", "unreadable"])
async def test_r5_r6_one_publication_and_phase8_failure_do_not_erase_disk(real_job_environment, monkeypatch, case):
    """Actual front/pipeline/dispatcher/U08/FILE/terminal; parser/disk fault boundaries only."""
    import beacon_edits
    from workflowy_mcp import markdown_generate_pipeline as pipeline
    env = real_job_environment
    path = _markdown_fixture(env)
    path.write_bytes(b"\xef\xbb\xbf# Old\r\nDisk-only prose.\r\n")
    if case == "missing":
        path.unlink()
    if case == "unreadable":
        path.write_bytes(b"\xffnot utf8")
    before = _state(path) if path.exists() else None
    commits, observations, prepared_rows, operations, rendered = [], [], [], [], []
    prepare, publish, dispatch, render = (mdr.prepare_generated_markdown_beacons,
        mdr.publish_generated_markdown_beacons, pipeline._write_generated_markdown, pipeline._render_markdown_from_tree)
    def rendered_tree(root):
        value = render(root)
        rendered.append(value)
        return value
    def prepared(raw, forest, previous, **kwargs):
        observations.append((previous, kwargs["source_exists"]))
        assert forest is rendered[-1][1] and raw == rendered[-1][0].encode("utf-8")
        assert (_state(path) if path.exists() else None) == before
        if case == "preparation-abort":
            def abort(*args, **kwargs):
                raise RuntimeError("j4 preparation interrupted")
            monkeypatch.setattr(mdr, "_markdown_eof_preparation", abort)
        if case == "heading-failure":
            def rejected(*args, **kwargs):
                raise ValueError("j4 heading metadata failure")
            monkeypatch.setattr(nmc, "plan_beacon_node_mutation", rejected)
        result = prepare(raw, forest, previous, **kwargs)
        prepared_rows.append(result)
        return result
    def published(plan, **kwargs):
        assert (_state(path) if path.exists() else None) == before
        if case == "cas":
            kwargs["before_final_hash"] = lambda: path.write_bytes(b"# External\nSaved by Dan.\n")
        if case == "proof":
            monkeypatch.setattr(beacon_edits, "verify_beacon_edit_plan", lambda *a, **kw: {
                "success": False, "error_kind": "plan_mismatch", "error": "j4 proof fault", "evidence": {"injected": True}})
        result = publish(plan, **kwargs)
        commits.append((result, path.read_bytes() if path.exists() else None))
        return result
    def dispatched(*args, **kwargs):
        operations.append(kwargs.get("operation", "write"))
        return dispatch(*args, **kwargs)
    monkeypatch.setattr(mdr, "prepare_generated_markdown_beacons", prepared)
    monkeypatch.setattr(mdr, "publish_generated_markdown_beacons", published)
    monkeypatch.setattr(pipeline, "_write_generated_markdown", dispatched)
    monkeypatch.setattr(pipeline, "_render_markdown_from_tree", rendered_tree)
    if case in {"local-refresh", "mapper-escape"}:
        def failed_map(snapshot, **kwargs):
            if case == "mapper-escape":
                raise RuntimeError("j4 caught mapper escape")
            return nmc._source_file_error(snapshot.canonical_path, ValueError("j4 local parser failure"),
                repo_root=kwargs.get("repo_root"), diagnostics=kwargs.get("diagnostics"))
        monkeypatch.setattr(nmc, "map_source_snapshot", failed_map)
    ack = await launch_real_job(env, "markdown")
    assert ack["success"], ack
    await asyncio.wait_for(env.tasks[ack["job_id"]], 30)
    control = env.controls[ack["job_id"]]
    terminal = await _joined_complete_detail(ack["job_id"])
    persisted = await _joined_complete_detail(ack["job_id"])
    assert persisted["status"] == terminal["status"]
    assert persisted["record_layout"] == "summary-head/2"
    assert len(env.tasks) == 1 and not env.probe.violations
    if case == "unreadable":
        assert not observations and not commits and not operations and _state(path) == before
        assert terminal["status"] == "failed"
        assert terminal["result_summary"]["files"][0]["error_kind"] == "source_observation_failed"
        return
    assert len(observations) == 1 and observations[0][1] is (case != "missing")
    if case == "preparation-abort":
        assert not commits and not operations and _state(path) == before and terminal["status"] == "failed"
        return
    assert operations == ["publish"] and len(commits) == 1
    summary, publication = terminal["result_summary"], commits[0][0]
    assert summary["publication"] == publication
    assert json.loads(json.dumps(summary["files"][0]["publication_policy"])) == json.loads(json.dumps(prepared_rows[0]["publication_policy"]))
    if case in {"cas", "proof"}:
        assert not publication["success"] and not summary.get("phase8")
        assert not summary["message"].startswith("Generated Markdown at ")  # CF-137 b4
        assert "no generated publication was committed" in summary["message"]
        assert terminal["status"] == "failed" and summary["files_changed"] == [] and not env.probe.sends
        if case == "cas":
            assert path.read_bytes() == b"# External\nSaved by Dan.\n" and publication["error_kind"] == "source_changed_since_plan"
        else:
            assert _state(path) == before and publication["error_kind"] == "plan_mismatch"
        assert summary["ast_beacons_reapplied"] == 0
        return
    assert publication["success"] and commits[0][1] == prepared_rows[0]["final_bytes"]
    assert path.read_bytes() == commits[0][1], "follow-up refresh unexpectedly republished the generated source"
    assert summary["files_changed"] == [str(path.resolve())] and summary["files"][0]["status"] == "committed"
    assert (summary["successful"], summary["failed"], summary["skipped"]) == (1, 0, 0)
    assert summary["phase8"]["execution"] == "inline" and summary["phase8"]["files"]
    assert summary["files"][0]["refresh_outcome"]["findings"] == summary["phase8"]["files"][0]["findings"]
    if case in {"local-refresh", "mapper-escape"}:
        assert terminal["status"] == "completed_with_warnings" and summary["partial_success"]
        assert summary["phase8"]["success"] is False
        if case == "mapper-escape":
            assert control.cpu_failure and control.cpu_failure[0] == "file:source"
            assert summary["phase8"]["files"][0]["local_cpu_failure"]["message"] == "j4 caught mapper escape"
            # Rejection controls on the actual adapter and actual retained
            # latch, after the real admitted job above. No fake terminal hook.
            spec = SimpleNamespace(job_id=ack["job_id"], job_type=terminal["type"], mode="file",
                root_uuid=env.ids["markdown"], claim_id=terminal["claim_id"], job_file=ack["job_file"])
            for mismatch in ("file", "phase", "exception_type", "message"):
                wrong = copy.deepcopy(summary)
                wrong["files"][0].pop("refresh_outcome", None)
                row = wrong["phase8"]["files"][0]
                if mismatch == "file":
                    row["file"] += ".other"
                    row["local_cpu_failure"]["file"] = row["file"]
                else:
                    row["local_cpu_failure"][mismatch] = "not the caught exception"
                denied = await srv._carto_task_terminal_payload(spec, control, {"result": wrong})
                assert denied["status"] == "failed" and denied["error_kind"] == "carto_job_exception"
    else:
        assert summary["phase8"]["success"] is True
    assert summary["ast_beacons_reapplied"] == (0 if case == "heading-failure" else 1)
    assert summary["ast_beacons_skipped"] == (1 if case == "heading-failure" else 0)
    if case == "heading-failure":
        failed_headings = [row for row in prepared_rows[0]["outcomes"] if not row.get("success")]
        warnings = [warning for warning in summary["warnings"] if warning.get("type") == "heading_preparation_failed"]
        assert len(warnings) == len(failed_headings) == 1  # CF-91, not diagnostics counted as headings
        assert warnings[0]["error"] == failed_headings[0]["error"]
        assert summary["warnings_by_type"]["heading_preparation_failed"] == 1
        assert terminal["status"] == "completed_with_warnings" and terminal["warning_count"] >= 1
    if case != "heading-failure":
        assert b"id=j4-live," in commits[0][1] and commits[0][1].endswith(b"\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [250, 0])
async def test_phase8_current_findings_replace_old_version_without_losing_historic_events(real_job_environment, monkeypatch, count):
    import beacon_findings
    env = real_job_environment
    path = _markdown_fixture(env)
    from test_beacon_markdown_preparation import block as md_block
    path.write_bytes(md_block("disk-only", "disk") + b"# Old\n")
    fresh = [{"code": "j4_final", "finding_id": f"j4:{i}"} for i in range(count)]
    old = [{"code": "j4_old", "finding_id": "j4:before-refresh"}]
    analyze = beacon_findings.analyze_beacon_snapshot
    def generated_findings(snapshot, **kwargs):
        if kwargs.get("producer") == "markdown.generate":
            return old
        return analyze(snapshot, **kwargs)
    monkeypatch.setattr(beacon_findings, "analyze_beacon_snapshot", generated_findings)
    def findings(*args, **kwargs):
        return fresh
    monkeypatch.setattr(support, "analyze_beacon_snapshot", findings)
    ack = await launch_real_job(env, "markdown")
    await asyncio.wait_for(env.tasks[ack["job_id"]], 30)
    terminal = await _joined_complete_detail(ack["job_id"])
    summary = terminal["result_summary"]
    assert summary["success"] and summary["findings_total"] == count
    assert len(summary["findings"]) == min(200, count) and summary["findings_truncated"] == max(0, count - 200)
    assert summary["phase8"]["files"][0]["findings"] == fresh
    assert summary["files"][0]["findings"] == summary["files"][0]["refresh_outcome"]["findings"] == fresh
    assert summary["files"][0]["omissions"] and summary["events"]
    assert any(event.get("id") == "disk-only" for event in summary["events"])
    assert summary["publication"]["success"] and summary["files_changed"] == [str(path.resolve())]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unattributed", "coherence", "baseexception", "admission", "mutation", "breaker"])
async def test_r6_runwide_and_unattributed_failures_keep_committed_disk(real_job_environment, monkeypatch, failure):
    from workflowy_mcp.coherence.errors import CoherenceBarrierActiveError
    env = real_job_environment
    path = _markdown_fixture(env)
    before = path.read_bytes()
    if failure == "unattributed":
        def bad_facts(*args, **kwargs):
            raise RuntimeError("j4 unattributed facts failure")
        monkeypatch.setattr(acn, "_prepare_carto_file_facts", bad_facts)
    else:
        def bad_map(*args, **kwargs):
            if failure == "baseexception":
                raise SystemExit("j4 baseexception")
            if failure == "admission":
                from workflowy_mcp.carto_job_governance import AdmissionRefusal
                raise AdmissionRefusal("overlap_conflict", "j4 exact admission refusal", retry_after=3)
            if failure == "mutation":
                from workflowy_mcp.mutation_locks import MutationSerializationConflictError
                raise MutationSerializationConflictError("j4 exact mutation refusal", retry_after=3,
                    contended_keys=["j4"], holder_kind="test", holder_id="j4", reason="overlap")
            if failure == "breaker":
                from workflowy_mcp.models.errors import AccountWriteBlockedError
                raise AccountWriteBlockedError(retry_after=3, blocked_until=17, reason="j4 breaker")
            raise CoherenceBarrierActiveError("j4 typed mapper barrier", dirty_generation=17,
                reason_code="journal_publication_failed", transaction_id="j4-typed")
        monkeypatch.setattr(nmc, "map_source_snapshot", bad_map)
    ack = await launch_real_job(env, "markdown")
    await asyncio.wait_for(env.tasks[ack["job_id"]], 30)
    control = env.controls[ack["job_id"]]
    terminal = await _joined_complete_detail(ack["job_id"])
    assert terminal["status"] == "failed" and control.cpu_failure
    assert terminal["result_summary"]["publication"]["success"]
    assert terminal["result_summary"]["files_changed"] == [str(path.resolve())]
    assert path.read_bytes() != before and b"id=j4-live," in path.read_bytes()
    assert not env.probe.violations
    if failure == "coherence":
        assert terminal["error_kind"] == "coherence_barrier_active"
        assert terminal["error_extra"]["transaction_id"] == "j4-typed"
    elif failure in {"admission", "mutation"}:
        assert terminal["error_kind"] == ("overlap_conflict" if failure == "admission" else "mutation_serialization_conflict")
        assert terminal["error_extra"]["retry_after"] == 3
    elif failure == "breaker":
        assert terminal["error_kind"] == "ACCOUNT_WRITE_BLOCKED"
        assert terminal["error_extra"]["retry_after"] == 3
        assert terminal["error_extra"]["blocked_until"] == 17
        assert terminal["error_extra"]["reason"] == "j4 breaker"
    else:
        assert terminal["error_kind"] == "carto_job_exception"


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["publication", "phase8"])
async def test_r5_r6_cancellation_keeps_real_publication_receipt(real_job_environment, monkeypatch, when):
    import threading
    from n07_job_harness import wait_job_gate
    env = real_job_environment
    path = _markdown_fixture(env)
    before = path.read_bytes()
    entered, release = threading.Event(), threading.Event()
    refresh_entered, refresh_release = asyncio.Event(), asyncio.Event()
    receipts = []
    publish = mdr.publish_generated_markdown_beacons
    def publisher(plan, **kwargs):
        if when == "publication":
            entered.set()
            assert release.wait(15), "test did not release actual publisher"
        value = publish(plan, **kwargs)
        receipts.append(value)
        return value
    monkeypatch.setattr(mdr, "publish_generated_markdown_beacons", publisher)
    async def send_gate(request):
        refresh_entered.set()
        await refresh_release.wait()
    if when == "phase8":
        env.send_gate = send_gate
    ack = await launch_real_job(env, "markdown")
    control, task = env.controls[ack["job_id"]], env.tasks[ack["job_id"]]
    try:
        if when == "publication":
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 15), 16)
        else:
            await wait_job_gate(env, refresh_entered, job_id=ack["job_id"])
            saved = await _joined_complete_detail(ack["job_id"])
            assert saved["result_summary"]["publication"]["success"]
        control.request_user_cancel()
        assert not control.user_observed and not task.done()
    finally:
        release.set()
        refresh_release.set()
    await asyncio.wait_for(task, 30)
    terminal = await _joined_complete_detail(ack["job_id"])
    assert terminal["status"] == "cancelled" and control.user_observed
    assert terminal["record_layout"] == "summary-head/2"
    assert len(receipts) == 1 and receipts[0]["success"] and receipts[0]["changed"]
    assert path.read_bytes() != before and not env.probe.violations
    assert terminal["result_summary"]["publication"] == receipts[0]
    assert terminal["result_summary"]["files_changed"] == [str(path.resolve())]
    assert (await _joined_complete_detail(ack["job_id"]))["result_summary"]["publication"] == receipts[0]


@pytest.mark.asyncio
async def test_phase8_completed_result_survives_return_checkpoint(real_job_environment, monkeypatch):
    """The real FILE result exists before the inline helper's observed-stop checkpoint."""
    from workflowy_mcp.carto_job_runtime import current_carto_job_context
    env = real_job_environment
    _markdown_fixture(env)
    refresh = env.client.refresh_file_node_beacons
    completed = []
    async def returned(*args, **kwargs):
        result = await refresh(*args, **kwargs)
        completed.append(result)
        current_carto_job_context().job_control.request_user_cancel()
        return result
    monkeypatch.setattr(env.client, "refresh_file_node_beacons", returned)
    ack = await launch_real_job(env, "markdown")
    await asyncio.wait_for(env.tasks[ack["job_id"]], 30)
    terminal = await _joined_complete_detail(ack["job_id"])
    assert terminal["status"] == "cancelled" and len(completed) == 1 and completed[0]["success"]
    assert terminal["record_layout"] == "summary-head/2"
    summary = terminal["result_summary"]
    assert summary["publication"]["success"] and summary["phase8"]["success"]
    assert summary["phase8"]["files"][0]["findings"] == completed[0]["findings"]
    assert summary["files"][0]["refresh_outcome"]["status"] == "refreshed"
    assert (await _joined_complete_detail(ack["job_id"]))["result_summary"]["phase8"] == summary["phase8"]


@pytest.mark.parametrize("shape,number", [("opening", 14), ("closing", 7), ("unterminated", 1), ("stray_bracket", 4)])
@pytest.mark.parametrize("operation", ["tags.ensure", "claimed_preparation", "legacy_update", "folder_check"])
def test_never_brittle_four_shapes_four_operations(setup_batch, shape, number, operation):
    """Good block's exact bytes survive or change only by the requested field."""
    build, _state_unused = setup_batch
    client, ids, (path,) = build(names=("good",))
    bad = corrupt("python", number)
    rows = bad + ["def bad():", "    return 0", ""] + block("python", "good") + ["def good():", "    return 1"]
    _write(path, rows)
    before = _state(path)
    if operation == "tags.ensure":
        result = _run([{"symbol": "good", "tags": ["new"]}])
        assert result["summary"]["committed"] == 1
        expected = before[0].replace(b"id=good,\n# slice_labels=one,two,", b"id=good,\n# slice_labels=one,two,new,")
        assert expected != before[0] and path.read_bytes() == expected
    elif operation == "legacy_update":
        result = nmc.update_beacon_from_node_python(str(path), "good #new", "AST_QUALNAME: good\nBEACON (AST)\nid: good\n")
        assert result["operation"] == "updated_beacon"
        assert path.read_bytes() == before[0].replace(b"id=good,\n# slice_labels=one,two,", b"id=good,\n# slice_labels=new,")
    else:
        if operation == "claimed_preparation":
            result = support.prepare_beacon_source(path, claim_id="owned")
            findings = result["findings"]
            assert result["status"] == "prepared"
            assert any(n.get("ast_name") == "good" for n in result["map"]["children"])
        else:
            from workflowy_mcp.beacon_integrity import run_beacon_check
            result = run_beacon_check([{"abs_path": str(path), "display_path": path.name, "node_id": ids["good"]}], min_severity="info")
            findings = result["rows"]
            assert result["counts"]["files_checked"] == 1
        # A stray bracket is tolerated non-record text, not a new check code.
        if shape != "stray_bracket":
            assert any(f["beacon_id"] != "good" for f in findings)
        assert not any(f["beacon_id"] == "good" for f in findings)
        assert _state(path) == before
    assert path.read_bytes().startswith(("\n".join(bad) + "\n").encode())
    # New U09 all-operation answer contract. The inherited U01/U06 local-write
    # assertions above are controls already green at the U09 base, not new fixes.
    final = support.prepare_beacon_source(path)
    answer = support.beacon_job_outcome(support.aggregate_beacon_outcomes([final]))
    assert answer["files"][0]["findings"] == final["findings"]
    assert answer["findings_total"] == len(final["findings"]) and answer["status"] != "failed"


def test_previous_front_acknowledgements_retain_findings(monkeypatch, tmp_path):
    """Actual handler/ack projections, with engines isolated, not gesture mocks."""
    from workflowy_mcp import beacon_fix, beacon_integrity
    finding = {"code": "duplicate_id", "finding_id": "bf1:fixture", "members": [{"line": 1}]}
    result = {"success": True, "changed": True, "summary": {"committed": 1}, "findings": [finding]}
    async def tags(*a, **kw):
        return copy.deepcopy(result)
    monkeypatch.setattr(srv, "_beacon_tags_mutate_impl", tags)
    registry = srv.build_beacon_registry()
    for verb in ("tags.ensure", "tags.remove"):
        ack = asyncio.run(dispatch_operation(registry, verb, {"symbols": ["x"], "tags": ["y"], "root_handles": ["root"]}))
        assert ack["detail"]["findings"] == [finding]
    async def check(*a, **kw):
        return {"success": True, "rows": [finding]}
    monkeypatch.setattr(srv, "_beacon_check_impl", check)
    assert asyncio.run(dispatch_operation(registry, "check", {"file_path": str(tmp_path / "check.py")}))["rows"] == [finding]
    async def snippet(*a, **kw):
        return {"text": "useful", "findings": [finding], "observed": {"file_sha256": "version"}}
    monkeypatch.setattr(srv, "_beacon_snippet_impl", snippet)
    assert asyncio.run(dispatch_operation(registry, "snippet", {"source_handle": "x", "format": "json"}))["findings"] == [finding]
    monkeypatch.setattr(srv, "_current_cache_by_id", lambda: {})
    async def fix(*a, **kw):
        return copy.deepcopy(result)
    monkeypatch.setattr(beacon_fix, "run_beacon_fix", fix)
    assert asyncio.run(dispatch_operation(registry, "fix", {"items": []}))["detail"]["findings"] == [finding]
    class Client:
        async def update_beacon_from_node(self, **kw):
            return {"success": False, "operation": "noop", "noop_reason": "already_satisfied", "source_path": "x.py"}
    async def read(*a, **kw):
        return SimpleNamespace(name="x", note="")
    async def acquire(*a, **kw):
        return []
    monkeypatch.setattr(srv, "get_client", lambda: Client())
    monkeypatch.setattr(srv, "_rate_limiter", None)
    monkeypatch.setattr(srv, "_resolve_beacon_graph_handle", lambda handle: (handle, {}))
    monkeypatch.setattr(srv, "_node_read_impl", read)
    monkeypatch.setattr(srv, "_acquire_single_beacon_q3_lease", acquire)
    monkeypatch.setattr(beacon_integrity, "_observe_beacon_file", lambda p: {"rows": [finding], "file_sha256": "current"})
    ack = asyncio.run(dispatch_operation(registry, "sync_node", {"node_handle": "x"}))
    assert ack["detail"]["findings"] == [finding] and ack["success"] is False
    direct = asyncio.run(srv.update_beacon_from_node("x", "x", ""))
    assert direct["findings"] == [finding] and direct["success"] is False
    assert direct["findings_basis"] == "current_disk_after_legacy_writer"


# @beacon[
#   id=mci-fences@program-symbol-content-pins,
#   role=verifier,
#   slice_labels=mci-harness,
#   kind=ast,
#   comment=Checks registered definition text by qualified AST identity and exact SHA-256, independent of line shifts; declaration spans keep their existing pins.,
# ]
def _assert_program_symbol_pins(source, symbols):
    """Definition coordinates are evidence, not pins; exact source text is pinned."""
    from test_cut_2b_fences import qualified_definitions
    definitions = qualified_definitions(source)
    lines = source.splitlines(keepends=True)
    for row in symbols:
        current = row["current"]
        if not current:
            continue
        if current["kind"] == "declaration":
            # P2(d) changes definition pins only, not the declaration-span fence.
            a, b = current["range"]
        else:
            assert row["symbol"] in definitions, (row["file"], row["symbol"], "missing definition")
            node = definitions[row["symbol"]]
            assert type(node).__name__ == current["kind"], (row["file"], row["symbol"], "definition kind")
            a, b = node.lineno, node.end_lineno
        assert hashlib.sha256("".join(lines[a-1:b]).encode()).hexdigest() == current["sha256"], (row["file"], row["symbol"])


@pytest.mark.parametrize("qualified", ["Container", "Container.chosen"])
def test_program_definition_pins_follow_symbols_and_reject_text_damage(qualified):
    """Line shifts pass; body, signature, comment, prose, whitespace or name edits fail."""
    from test_cut_2b_fences import definition_record, qualified_definitions
    source = ('class Container:\n'
              '    async def chosen(value=1):\n'
              '        """Pinned prose."""\n'
              '        # pinned comment\n'
              '        return value\n')
    node = qualified_definitions(source)[qualified]
    record = definition_record(source, qualified)
    row = {"file": "source-copy.py", "symbol": qualified,
           "current": {"kind": type(node).__name__, **record}}
    _assert_program_symbol_pins(source, [row])
    _assert_program_symbol_pins("# Unrelated insertion.\n\n" + source, [row])
    for old, new in (
        ("return value", "return None"),
        ("value=1", "value=2"),
        ("# pinned comment", "# changed comment"),
        ("Pinned prose.", "Changed prose."),
        ("return value", "return  value"),
        ("Container", "Renamed"),
    ):
        with pytest.raises(AssertionError):
            _assert_program_symbol_pins(source.replace(old, new, 1), [row])


def test_program_evidence_registers_are_complete_current_and_repository_local():
    """Before U09: closure manifests absent; no final test needs git/scratch."""
    root = Path(__file__).resolve().parents[1]
    table = json.loads((root / "tests/beacon_robustness_dispositions.json").read_text())
    rows = table["rows"]
    required = {f"A{area}-D{n}" for area, count in enumerate([14, 18, 19, 17, 15, 15, 16, 14, 15, 15], 1)
                for n in range(1, count + 1)}
    required |= {f"A9-C{n}" for n in range(1, 11)} | {f"CF-{n:02d}" for n in range(1, 28)}
    required |= {f"U{n}" for n in range(1, 15)} | {f"F{n}" for n in range(1, 11)}
    assert len(required) == 219 and required <= {r["id"] for r in rows}
    assert len(rows) == len({r["id"] for r in rows}) == 224
    for row in rows:
        assert row["disposition"] and row["evidence"]
        assert row["tests"] or row["destination"], row["id"]
        assert row["commit"] or row["destination"], row["id"]
        assert row["status"] not in {"missing_per_row_disposition", "in_flight_u09"}
    assert all(not r["edited"] for r in table["curation_starting_data"])
    assert "[ap:3HHUXF]" not in json.dumps(table)  # U01 FINAL is cited by UUID.

    expected = json.loads((root / "tests/beacon_robustness_expected_changes.json").read_text())
    groups = expected["program_lists"]
    assert len(groups["stop_rule_20_records"]) == 20
    assert len({(r["source"], r["parser"]) for r in groups["stop_rule_20_records"]}) == 20
    assert len(groups["window_corrections_258"]) == 258
    assert len({(r["file"], r["beacon_id"]) for r in groups["window_corrections_258"]}) == 258
    keys = {(e["test"], e["case"]) for e in expected["changes"]}
    for group in ("additional_grammar_deltas", "hygiene_policy_changes", "window_corrections_258", "window_additional_deltas"):
        assert groups[group]
        assert all((r["test"], r["case"]) in keys for r in groups[group])

    manifest = json.loads((root / "tests/beacon_robustness_u09_manifest.json").read_text())
    assert len(manifest["protected"]) == 17
    assert len(manifest["u09_symbols"]) == 15
    assert {r["file"] for r in manifest["u09_symbols"]} == {
        "nexus_map_codebase.py", "src/workflowy_mcp/beacon_refresh_support.py", "src/workflowy_mcp/server.py"}
    integration = (root / "docs/BEACON-ROBUSTNESS-INTEGRATION.md").read_text()
    # "worktree" and "main" are historical; "merged" is the joined live tree.
    from test_cut_2b_fences import J_CHANGED, j_sources, verify_j_register
    assert J_CHANGED == {"mgp.run_markdown_generate_pipeline", "mgp._write_generated_markdown",
                         "mgp._run_phase8_refresh_inline", "acn.refresh_file_node_beacons",
                         "acn._prepare_carto_source_file", "acn._plan_carto_file_refresh",
                          "acn.refresh_folder_cartographer_sync", "acn.update_beacon_from_node",
                          "acn._apply_beacon_from_node_data", "srv._handle_refresh_file_node",
                          "srv._carto_task_terminal_payload", "srv._run_carto_bulk_visible_apply_job",
                          "srv._start_carto_bulk_visible_apply_job",
                          "srv._bulk_apply_per_file_inner_loop"}
    verify_j_register(j_sources(), manifest)
    for row in manifest["protected"]:
        versions = row["versions"]
        assert versions["a345dc7"].get("body_sha256") == versions["worktree"].get("body_sha256")
        assert versions["d8da543"].get("body_sha256") == versions["worktree"].get("body_sha256")
        for version in ("worktree", "main"):
            if versions[version]["present"]:
                assert versions[version]["body_sha256"] in integration
        current = versions["merged"]
        from test_cut_2b_fences import definition_record, qualified_definitions
        if row["key"] in {"ww.main", "mgp._launch_phase8_refresh_via_authority"}:
            assert current == {"present": False}, row["key"]
            if row["key"] == "ww.main":
                assert not (root / row["file"]).exists()
            else:
                source = (root / row["file"]).read_text(encoding="utf-8")
                assert "_launch_phase8_refresh_via_authority" not in qualified_definitions(source)
        else:
            assert current["present"] == versions["main"]["present"], row["key"]
            assert current["qualname"] == versions["main"]["qualname"], row["key"]
            actual = definition_record((root / row["file"]).read_text(encoding="utf-8"), current["qualname"])
            for field in ("body_sha256", "sha256", "ast_sha256"):
                assert current[field] == actual[field], (row["key"], field)
            if row["key"] in J_CHANGED:
                assert current["body_sha256"] != versions["main"]["body_sha256"], row["key"]
            else:
                assert current["body_sha256"] == versions["main"]["body_sha256"], row["key"]
    assert sum(r["versions"]["worktree"]["present"] for r in manifest["protected"]) == 11
    transfer = manifest["seam_transfer"]
    assert transfer["base"]["sha256"] == transfer["current"]["sha256"]
    assert "§3/§11.2" in transfer["destination"]
    by_file = {}
    assert len(manifest["program_symbols"]) == 712
    component_row = next(row for row in manifest["program_symbols"]
                         if row["file"] == "src/workflowy_mcp/beacon_refresh_support.py"
                         and row["symbol"] == "prepare_beacon_source")
    component_actual = definition_record(Path(support.__file__).read_text(), "prepare_beacon_source")
    assert component_actual["body_sha256"] == "248fcc81adedc58cbb7b123abf079d35db4fae791b20217dcec76b0028687cfc"
    for field in ("sha256", "body_sha256", "ast_sha256"):
        assert component_row["current"][field] == component_actual[field]
    for row in manifest["program_symbols"]:
        assert row["units"] and row["disposition"] in {"added", "removed", "changed"}
        by_file.setdefault(row["file"], []).append(row)
    for relative, symbols in by_file.items():
        path = root / relative
        if not path.exists():
            assert all(r["current"] is None for r in symbols)
            continue
        _assert_program_symbol_pins(path.read_text(encoding="utf-8"), symbols)


def test_components_have_no_parallel_source_or_execution_authority_and_docs_match():
    """Static authority/activation guard, not a runtime restriction for Dan."""
    root = Path(__file__).resolve().parents[1]
    text = Path(support.__file__).read_text()
    tree = ast.parse(text)
    calls = {n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
             for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, (ast.Attribute, ast.Name))}
    assert {"map_source_snapshot", "beacon_snapshot_analysis_inputs", "analyze_beacon_snapshot",
            "make_beacon_finding", "project_plan_result", "repair_markdown_c0_controls_on_disk",
            "normalize_beacon_comment_field_aliases_in_file", "normalize_duplicate_ast_beacons_in_file"} <= calls
    assert not calls & {"open", "write_bytes", "write_text", "replace", "commit_beacon_edit_plan",
                        "create_node", "delete_node", "reconcile_tree", "acquire_q3_leases_async", "launch_carto_refresh_job"}
    assert not any(n.name.startswith(("parse_", "scan_", "plan_", "analyze_")) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    docs = {p.name: p.read_text() for p in (root / "docs").glob("BEACON-*.md")}
    for name in ("BEACON-ROBUSTNESS.md", "BEACON-ROBUSTNESS-INTEGRATION.md"):
        assert "component-ready" in docs[name].lower()  # historical U09 boundary
        flat = " ".join(docs[name].split())
        assert "J-1" in flat and "J-2" in flat and "J-4" in flat
    integration = docs["BEACON-ROBUSTNESS-INTEGRATION.md"]
    assert {int(n) for n in re.findall(r"^> R([1-7]) ", integration, flags=re.M)} == set(range(1, 8))
    assert "source_changed_since_plan" in docs["BEACON-ROBUSTNESS.md"]
    assert "no mocks of the hook under test" in integration
    assert "What the curation run depends on:" in docs["BEACON-ROBUSTNESS.md"]
    assert "What only J adds:" in docs["BEACON-ROBUSTNESS.md"]


async def _joined_file_job(env, *, preparation=None):
    """Real admission, scheduling, execution and one exit; only HTTP is fake."""
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp import carto_job_governance as gov
    async with cache_operation_boundary(env.client, "j:joined-file-front"):
        initial = gov.compute_footprint(str(env.repo / "source.py"), env.ids["file"],
                                        current_operation_cache().view.nodes_by_id, kind="file") if preparation else None
        ack = await srv._start_carto_refresh_job(env.ids["file"], "file", {"force": True},
                                                preparation=preparation, initial_footprint=initial)
    assert ack["success"], ack
    await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    assert not env.probe.violations, env.probe.violations
    return await _joined_complete_detail(ack["job_id"])


def _observe_joined_file(monkeypatch):
    """Pass-through observer: the original protected body executes every call."""
    rows = []
    original = acn.WorkFlowyClientNexus.refresh_file_node_beacons
    async def observed(*args, **kwargs):
        row = await original(*args, **kwargs)
        rows.append(row)
        return row
    monkeypatch.setattr(acn.WorkFlowyClientNexus, "refresh_file_node_beacons", observed)
    return rows


@pytest.mark.asyncio
async def test_r1_r2_retention_before_seed_and_uuid_recovery(real_job_environment, monkeypatch):
    """Real FILE failure, restore and valid empty under one actual job claim."""
    env = real_job_environment
    path = env.repo / "source.py"
    good = b"def existing():\n    return 1\n\ndef obsolete():\n    return 2\n"
    path.write_bytes(good + b"def broken(:\n")
    before = _state(path)
    old = {key: copy.deepcopy(env.probe.nodes[env.ids[key]]) for key in ("file", "symbol", "obsolete")}
    seeds = []
    original_seed = nmc.reconcile_trees_cartographer
    def seed(*args, **kwargs):
        if kwargs.get("_run_identity_seed", True):
            seeds.append(args[0])
        return original_seed(*args, **kwargs)
    monkeypatch.setattr(nmc, "reconcile_trees_cartographer", seed)
    async def prepare(payload, selection):
        claim = payload["_claim_id"]
        failed = await env.client.refresh_file_node_beacons(env.ids["file"], carto_claim_id=claim, force=True)
        assert failed["status"] == "failed" and failed["preserve_existing"]
        assert failed["reconcile_children"] is None and seeds == [] and env.probe.sends == []
        assert set(failed["retained_uuids"]) == {env.ids["symbol"], env.ids["obsolete"]}
        assert any(f["code"] == "mapper_parse_error" for f in failed["findings"])
        assert _state(path) == before
        assert all(env.probe.nodes[env.ids[key]] == value for key, value in old.items())
        path.write_bytes(good)
        restored = await env.client.refresh_file_node_beacons(env.ids["file"], carto_claim_id=claim, force=True)
        assert restored["success"] and restored["status"] == "refreshed"
        assert env.ids["symbol"] in env.probe.nodes and env.ids["obsolete"] in env.probe.nodes
        assert len(seeds) == 1 and restored["reconcile_report"] == seeds[0].get("_beacon_reconcile_report", {})
        path.write_bytes(b"")
        empty = await env.client.refresh_file_node_beacons(env.ids["file"], carto_claim_id=claim, force=True)
        assert empty["reconcile_children"] == [] and empty["preserve_existing"] is False
        assert env.ids["symbol"] not in env.probe.nodes and env.ids["obsolete"] not in env.probe.nodes
    terminal = await _joined_file_job(env, preparation=prepare)
    assert terminal["status"] in {"completed", "completed_with_warnings"}, terminal


@pytest.mark.asyncio
async def test_r2_physical_duplicate_delegate_includes_unmapped_copy(real_job_environment, monkeypatch):
    env = real_job_environment
    path = env.repo / "source.py"
    _write(path, block("python", "twice") + ["def existing():", "    return 1", ""] +
           block("python", "twice") + ["value = 2"])
    rows = _observe_joined_file(monkeypatch)
    await _joined_file_job(env)
    row = rows[-1]
    occurrences = row["duplicate_source_beacon_ids"]["twice"]
    assert len(occurrences) == 2 and occurrences[0]["line"] < occurrences[1]["line"]
    warnings = [w for w in row["warnings"] if w.get("type") == "duplicate_source_beacon_id"]
    assert len(warnings) == 1 and warnings[0]["matches"] == occurrences
    assert any(f["code"] == "block_not_mapped" for f in row["findings"])


@pytest.mark.asyncio
async def test_real_seeded_root_ambiguity_report_reaches_file_result(real_job_environment, monkeypatch):
    """Real mapper and U03 twins generate the report after preparation finished."""
    from n05_region_harness import publish_fixture_nodes
    env = real_job_environment
    path = env.repo / "source.py"
    _write(path, (block("python", "shared") + ["def twin():", "    return 1", ""]) * 2 +
           ["def safe():", "    return 3"])
    nodes = [dict(n) for n in env.initial_nodes]
    for node in nodes:
        if node["id"] in {env.ids["symbol"], env.ids["obsolete"]}:
            node.update(name="ƒ twin() 🔱 #one #two", note="AST_QUALNAME: twin\n\nBEACON (AST)\nid: shared\n")
    env.probe.nodes = {n["id"]: {"createdAt": 100, "modifiedAt": 100, "note": "", "priority": 0, **n} for n in nodes}
    publish_fixture_nodes(env.client, nodes)
    reports = []
    original_seed = nmc.reconcile_trees_cartographer
    def seeded(source, target, **kwargs):
        value = original_seed(source, target, **kwargs)
        if kwargs.get("_run_identity_seed", True):
            reports.append(copy.deepcopy(source.get("_beacon_reconcile_report") or {}))
        return value
    monkeypatch.setattr(nmc, "reconcile_trees_cartographer", seeded)
    rows = _observe_joined_file(monkeypatch)
    await _joined_file_job(env)
    row = rows[-1]
    assert len(reports) == 1 and reports[0]["ambiguous_count"] == 2
    assert row["success"] and row["ambiguous_count"] == 2
    assert row["reconcile_report"] == reports[0]
    assert row["reconcile_report"]["ambiguous_count"] == 2
    assert row["ambiguities"] == row["reconcile_report"]["ambiguous"]
    assert all(r["beacon_id"] == "shared" for r in row["ambiguities"])
    assert any("safe()" in str(n["name"]) for n in env.probe.nodes.values())


@pytest.mark.asyncio
async def test_r2_final_map_check_and_advisory_error_share_one_observation(real_job_environment, monkeypatch):
    env = real_job_environment
    rows = _observe_joined_file(monkeypatch)
    seen = []
    original = nmc.map_source_snapshot
    def mapped(snapshot, **kwargs):
        seen.append(("map", snapshot.sha256))
        return original(snapshot, **kwargs)
    def check(snapshot, **kwargs):
        seen.append(("check", snapshot.sha256))
        raise ArithmeticError("joined advisory check failure")
    monkeypatch.setattr(nmc, "map_source_snapshot", mapped)
    monkeypatch.setattr(support, "analyze_beacon_snapshot", check)
    await _joined_file_job(env)
    row = rows[-1]
    assert row["success"] and row["status"] == "refreshed"
    assert seen == [("map", row["file_sha256"]), ("check", row["file_sha256"])]
    assert row["analysis_error"]["exception"] == "ArithmeticError"
    assert [f["code"] for f in row["findings"]] == ["check_failed"]
    assert any(n["name"].startswith("ƒ added") for n in env.probe.nodes.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous,missing", [(False, False), (True, False), (False, True)],
                         ids=["localized", "ambiguous-whole-file", "no-old-failed-section"])
async def test_vue_joined_failure_keeps_failed_scope_and_updates_healthy_section(real_job_environment, monkeypatch, ambiguous, missing):
    """Literal HTTP/cache fixture setup; real parse/seed/reconcile and sends."""
    from n05_region_harness import node_uuid, publish_fixture_nodes
    env = real_job_environment
    path = env.repo / "sections.vue"
    path.write_bytes(b"<script>\nfunction first() {}\n</script>\n<script setup>\nfunction second() {}\n</script>\n")
    section, symbol, duplicate = [node_uuid("j-vue-" + name) for name in ("section", "symbol", "duplicate")]
    nodes = [dict(n) for n in env.initial_nodes if n["id"] not in {env.ids["symbol"], env.ids["obsolete"]}]
    next(n for n in nodes if n["id"] == env.ids["file"])["note"] = "Path: sections.vue"
    if not missing:
        nodes.extend([{"id": section, "name": "script", "note": "VUE_SECTION: script_element\nVUE_START_TAG: <script>", "parent_id": env.ids["file"]},
                      {"id": symbol, "name": "first", "note": "AST_QUALNAME: first", "parent_id": section}])
    if ambiguous:
        nodes.append({"id": duplicate, "name": "other script", "note": "VUE_SECTION: script_element\nVUE_START_TAG: <script>", "parent_id": env.ids["file"]})
    # Fixture construction only, before admission; no production hook is replaced.
    env.probe.nodes = {n["id"]: {"createdAt": 100, "modifiedAt": 100, "note": "", "priority": 0, **n} for n in nodes}
    publish_fixture_nodes(env.client, nodes)
    old = copy.deepcopy(env.probe.nodes)
    original = nmc.JsTsOutlineContext.from_source
    def fail_one(**kwargs):
        if "first" in kwargs["source_text"]:
            raise ValueError("one joined section parser")
        return original(**kwargs)
    monkeypatch.setattr(nmc.JsTsOutlineContext, "from_source", fail_one)
    rows = _observe_joined_file(monkeypatch)
    await _joined_file_job(env)
    row = rows[-1]
    if not missing:
        assert env.probe.nodes[section] == old[section] and env.probe.nodes[symbol] == old[symbol]
    assert not any(acn._is_cartographer_parse_error_sentinel(str(n["name"])) for n in env.probe.nodes.values())
    assert any(f["code"] == "mapper_parse_error" for f in row["findings"])
    if ambiguous:
        assert row["status"] == "failed" and row["preserve_existing"] and env.probe.sends == []
        assert env.probe.nodes == old
    else:
        assert row["status"] == "partial_section"
        assert row["sections"][0]["existing_uuid"] == (None if missing else section)
        assert any("second()" in str(n["name"]) for n in env.probe.nodes.values())


@pytest.mark.asyncio
async def test_r2_discovered_file_uses_claimed_preparation_and_keeps_unreadable_row(real_job_environment, monkeypatch):
    env = real_job_environment
    bad, good = env.repo / "unreadable.md", env.repo / "hygiene.py"
    bad.write_bytes(b"\xffnot UTF8\n")
    _write(good, block("python", "fresh") + ["def fresh():", "    return 3"])
    good.write_bytes(good.read_bytes().replace(b"comment=Own fresh.,", b"comments=Own fresh.,"))
    before = _state(bad)
    rows = []
    original = acn.WorkFlowyClientNexus.refresh_folder_cartographer_sync
    async def observed(*args, **kwargs):
        row = await original(*args, **kwargs)
        rows.append(row)
        return row
    monkeypatch.setattr(acn.WorkFlowyClientNexus, "refresh_folder_cartographer_sync", observed)
    ack = await launch_real_job(env, "folder")
    assert ack["success"], ack
    await asyncio.wait_for(env.tasks[ack["job_id"]], 30)
    detail = await _joined_complete_detail(ack["job_id"])
    outcomes = {Path(row["file"]).name: row for row in detail["result_summary"]["files"] if row.get("new_file")}
    assert outcomes[bad.name]["status"] == "failed" and outcomes[bad.name]["error_kind"] == "not_utf8"
    assert outcomes[bad.name]["findings"][0]["code"] == "file_unreadable"
    assert _state(bad) == before
    assert not any(str(n["name"]).endswith("unreadable.md") for n in env.probe.nodes.values())
    assert outcomes[good.name]["status"] == "refreshed" and outcomes[good.name]["repair_outcomes"]
    assert b"comments=" not in good.read_bytes() and b"comment=Own fresh.," in good.read_bytes()
    assert any(str(n["name"]).endswith("hygiene.py") for n in env.probe.nodes.values())
    assert not env.probe.violations


@pytest.mark.asyncio
async def test_br02_joined_external_save_has_one_final_map_check_and_no_hygiene_retry(real_job_environment, monkeypatch):
    env = real_job_environment
    path = env.repo / "source.py"
    _write(path, block("python", "existing") + ["def existing():", "    return 1"])
    path.write_bytes(path.read_bytes().replace(b"comment=", b"comments="))
    external = b"def existing():\n    return 7\n\ndef editor_added():\n    return 8\n"
    publish = nmc.commit_beacon_edit_plan
    attempts, groups = [], []
    def external_save(plan, **kwargs):
        attempts.append(plan)
        path.write_bytes(external)
        return publish(plan, **kwargs)
    def group(*args, **kwargs):
        groups.append(args)
        raise AssertionError("CAS refusal must end optional hygiene")
    monkeypatch.setattr(nmc, "commit_beacon_edit_plan", external_save)
    monkeypatch.setattr(nmc, "normalize_duplicate_ast_beacons_in_file", group)
    rows = _observe_joined_file(monkeypatch)
    await _joined_file_job(env)
    row = rows[-1]
    assert row["success"] and path.read_bytes() == external
    assert len(attempts) == 1 and groups == []
    assert row["repair_outcomes"][0]["error_kind"] == "source_changed_since_plan"
    assert row["file_sha256"] == hashlib.sha256(external).hexdigest()
    assert any("editor_added()" in str(n["name"]) for n in env.probe.nodes.values())


@pytest.mark.asyncio
async def test_many_candidates_joined_file_does_not_multiply_final_analysis(real_job_environment, monkeypatch):
    env = real_job_environment
    path = env.repo / "source.py"
    path.write_bytes(b"\n".join(("\n".join(block("python", f"many-{i}") +
                                          [f"def many_{i}():", "    return 1", ""])).encode()
                               for i in range(533)))
    seen = []
    original = support.analyze_beacon_snapshot
    def analyze(snapshot, **kwargs):
        seen.append(snapshot.sha256)
        return original(snapshot, **kwargs)
    monkeypatch.setattr(support, "analyze_beacon_snapshot", analyze)
    rows = _observe_joined_file(monkeypatch)
    await _joined_file_job(env)
    assert rows[-1]["success"] and seen == [rows[-1]["file_sha256"]]
    assert sum(str(n["name"]).startswith("ƒ many_") for n in env.probe.nodes.values()) == 533


@pytest.mark.asyncio
@pytest.mark.parametrize("shape,number", [("opening", 14), ("closing", 7), ("unterminated", 1), ("stray_bracket", 4)])
async def test_claimed_file_four_corrupt_shapes_leave_healthy_mapping_usable(real_job_environment, monkeypatch, shape, number):
    env = real_job_environment
    path = env.repo / "source.py"
    bad = corrupt("python", number)
    _write(path, bad + ["def broken_metadata():", "    return 0", ""] +
           block("python", "existing") + ["def existing():", "    return 1"])
    before = _state(path)
    rows = _observe_joined_file(monkeypatch)
    await _joined_file_job(env)
    row = rows[-1]
    assert row["success"] and env.ids["symbol"] in env.probe.nodes
    assert row["status"] == "refreshed" and _state(path) == before
    if shape != "stray_bracket":
        assert row["findings"] and any(f["beacon_id"] != "existing" for f in row["findings"])


@pytest.mark.asyncio
async def test_br09_existing_markdown_file_retention_uses_real_job_claim(real_job_environment, monkeypatch):
    """C03 through real admission/claim/CPU/terminal, with all existing UUIDs."""
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    env = real_job_environment
    path = env.repo / "doc.md"
    path.write_bytes(b"# Doc\n\ncaf\xe9\n")
    before, old = _state(path), copy.deepcopy(env.probe.nodes)
    claims, prepared_rows, seeds = [], [], []
    original_prepare, original_seed = acn._prepare_carto_source_file, nmc.reconcile_trees_cartographer
    def prepare(*args, **kwargs):
        value = original_prepare(*args, **kwargs)
        claims.append(kwargs["claimed"])
        prepared_rows.append(value["prepared"])
        return value
    def seeded(*args, **kwargs):
        seeds.append(args[0])
        return original_seed(*args, **kwargs)
    monkeypatch.setattr(acn, "_prepare_carto_source_file", prepare)
    monkeypatch.setattr(nmc, "reconcile_trees_cartographer", seeded)
    rows = _observe_joined_file(monkeypatch)
    async with cache_operation_boundary(env.client, "j:ordinary-markdown-c03-front"):
        ack = await srv._start_carto_refresh_job(env.ids["markdown"], "file")
    assert ack["success"], ack
    await asyncio.wait_for(env.tasks[ack["job_id"]], 30)
    terminal = await _joined_complete_detail(ack["job_id"])
    assert len(rows) == 1 and rows[0]["success"] is False
    row = rows[0]
    assert terminal["status"] == row["status"] == "failed"
    assert len(claims) == 1 and claims[0] == terminal["claim_id"] and claims[0]
    assert row["error_kind"] == "not_utf8" and row["header_freshness"]["available"] is False
    assert set(row["retained_uuids"]) == {env.ids["heading"], env.ids["temporary"]}
    assert row["findings"][0]["code"] == "file_unreadable" and len(row["repair_outcomes"]) == 3
    assert prepared_rows[0]["snapshot"] is None
    assert any(acn._is_cartographer_parse_error_sentinel(n["name"]) for n in prepared_rows[0]["map"]["children"])
    assert seeds == [] and env.probe.sends == [] and env.probe.nodes == old and _state(path) == before
    assert env.controls[ack["job_id"]].cpu_failure is None and not env.probe.violations


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["new", "existing", "all-failed"])
@pytest.mark.parametrize("error_type", [RuntimeError, acn.NetworkError])
async def test_r3_caught_mapper_failure_is_local_with_exact_latch_provenance(real_job_environment, monkeypatch, target, error_type):
    """Real folder/claim/terminal; only the mapper boundary raises."""
    env = real_job_environment
    path = env.repo / ("zz_mapper_escape.py" if target == "new" else "source.py")
    if target == "new":
        path.write_text("def boundary():\n    return 1\n", encoding="utf-8")
    if target == "all-failed":
        (env.repo / "missing.py").write_text("def missing():\n    pass\n", encoding="utf-8")
    original = nmc.map_source_snapshot
    def mapped(snapshot, **kwargs):
        if target == "all-failed" or Path(snapshot.canonical_path) == path:
            raise error_type("J-2 exact mapper boundary")
        return original(snapshot, **kwargs)
    monkeypatch.setattr(nmc, "map_source_snapshot", mapped)
    ack = await launch_real_job(env, "folder")
    assert ack["success"], ack
    await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    control = env.controls[ack["job_id"]]
    terminal = await _joined_complete_detail(ack["job_id"])
    summary = terminal["result_summary"]
    assert terminal["status"] == ("failed" if target == "all-failed" else "completed_with_warnings"), terminal
    assert control.cpu_failure is not None and str(control.cpu_failure[1]) == "J-2 exact mapper boundary"
    assert summary["failed"] >= 1
    row = next(r for r in summary["files"] if Path(r["file"]) == path)
    assert row["status"] == "failed" and row["local_cpu_failure"]["message"] == "J-2 exact mapper boundary"
    assert row["local_cpu_failure"]["file"] == row["file"]
    if target != "all-failed":
        assert summary["successful"] >= 3 and summary["partial_success"]
        assert any("file_refresh_failed" == w.get("type") for w in terminal["warnings"])
    assert terminal["warning_count"] == summary["warnings_total"] == len(summary["warnings"])
    assert not env.probe.violations


async def _joined_complete_detail(job_id, **params):
    """The detail view's complete evidence, joined across its pages (G2).

    ``system.job.status(job_id)`` is the summary view from JR2 G2; today's
    complete detail is ``view="detail"``, delivered a page at a time
    (WaveSpec rev 3 §2.10.1) and rebuilt by the view's own pure join.
    """
    from workflowy_mcp.carto_job_record import join_carto_job_detail_pages

    pages, offset, slice_offset = [], 0, 0
    while True:
        page = await srv._system_job_status_impl(
            job_id=job_id, view="detail", offset=offset, slice_offset=slice_offset, **params)
        pages.append(page)
        cursor = page.get("detail_page") or {}
        offset, slice_offset = cursor.get("next_offset"), cursor.get("next_slice_offset") or 0
        if offset is None:
            result = join_carto_job_detail_pages(pages)
            if result.get("type") in {"CARTO_BULK_APPLY", "CARTO_MARKDOWN_GENERATE"}:
                _assert_job_evidence_has_no_working_values(Path(result["carto_job_file"]))
            return result


def _assert_job_evidence_has_no_working_values(path):
    """G5 leak scan on every joined J-3/J-4 witness, not a source-text grep."""
    def clean(value):
        if isinstance(value, dict):
            assert not {"snapshot", "analysis_inputs", "final_bytes", "publication_plan"}.intersection(value)
            for item in value.values():
                clean(item)
        elif isinstance(value, list):
            for item in value:
                clean(item)
    clean(json.loads(path.read_text(encoding="utf-8")))
    for name in ("files.jsonl", "nodes.jsonl"):
        evidence = path.with_suffix("") / name
        if evidence.exists():
            for line in evidence.read_text(encoding="utf-8").splitlines():
                clean(json.loads(line))


@pytest.mark.asyncio
async def test_r2_r3_folder_clean_corrupt_unreadable_and_new_files(real_job_environment, monkeypatch):
    env = real_job_environment
    (env.repo / "unreadable.md").write_bytes(b"\xffnot utf8")
    _write(env.repo / "corrupt.py", corrupt("python", 1) + ["def useful():", "    pass"])
    ack = await launch_real_job(env, "folder")
    await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    terminal = await _joined_complete_detail(ack["job_id"])
    summary = terminal["result_summary"]
    by_file = {Path(r["file"]).name: r for r in summary["files"]}
    assert set(by_file) == {"source.py", "other.py", "broken.py", "doc.md", "missing.py", "new.py", "corrupt.py", "unreadable.md"}
    assert len(summary["files"]) == 8
    assert summary["successful"] == 6 and summary["failed"] == 2 and summary["skipped"] == 0
    assert terminal["status"] == summary["status"] == "completed_with_warnings"
    assert summary["partial_success"] and by_file["corrupt.py"]["findings"]
    assert by_file["unreadable.md"]["error_kind"] == "not_utf8"
    assert by_file["new.py"]["status"] == "refreshed" and "new_file_outcomes" not in summary
    detail = await _joined_complete_detail(ack["job_id"])
    assert "unreadable.md" in json.dumps(detail)
    live = srv._carto_live_job_projection()
    jobs = await asyncio.to_thread(srv._scan_carto_job_files, str(env.jobs), live_projection=live)
    widget = json.loads(srv._project_carto_widget_reply(jobs, set()))
    shown = next(r for r in widget["jobs"] if r["job_id"] == ack["job_id"])
    assert shown["status"] == "completed_with_warnings"
    assert shown["warning_count"] == summary["warnings_total"] and shown["warnings"]


@pytest.mark.asyncio
async def test_unsupported_file_types_are_plain_leaves_never_warnings(real_job_environment):
    """Dan's ruling (2026-10-28): a file type with no beacon language is how the
    system works. Through the real folder job it keeps its plain leaf and an
    explicit skip_reason, adds no warning, and alone never turns the outcome
    completed_with_warnings, on the record or in the widget."""
    env = real_job_environment
    (env.repo / "broken.py").write_text("def now_ok():\n    pass\n", encoding="utf-8")
    (env.repo / "style.css").write_text("body { color: red; }\n", encoding="utf-8")
    (env.repo / "blob.dat").write_bytes(b"\xff\x00binary")
    ack = await launch_real_job(env, "folder")
    await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    terminal = await _joined_complete_detail(ack["job_id"])
    summary = terminal["result_summary"]
    by_file = {Path(r["file"]).name: r for r in summary["files"]}
    for name in ("style.css", "blob.dat"):
        row = by_file[name]
        assert row["status"] == "refreshed" and row["skip_reason"] == "unsupported_extension", row
        assert not row.get("warnings") and not row["findings"], row
    assert summary["warnings"] == [] and summary["warnings_total"] == 0, summary["warnings"]
    assert summary["skipped"] == 0 and summary["failed"] == 0 and summary["findings_total"] == 0
    assert terminal["status"] == summary["status"] == "completed"
    live = srv._carto_live_job_projection()
    jobs = await asyncio.to_thread(srv._scan_carto_job_files, str(env.jobs), live_projection=live)
    widget = json.loads(srv._project_carto_widget_reply(jobs, set()))
    shown = next(r for r in widget["jobs"] if r["job_id"] == ack["job_id"])
    assert shown["status"] == "completed" and not shown.get("warning_count"), shown


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["typed", "system-exit", "breaker", "governor", "admission"])
async def test_r3_runwide_mapper_escape_retains_prior_work_and_typed_failure(real_job_environment, monkeypatch, failure):
    from workflowy_mcp.coherence.errors import CoherenceBarrierActiveError
    from workflowy_mcp.models.errors import AccountWriteBlockedError, WriteGovernorUnavailableError
    from workflowy_mcp.carto_job_governance import AdmissionRefusal
    env = real_job_environment
    (env.repo / "zz_runwide.py").write_text("def boundary():\n    pass\n", encoding="utf-8")
    error = (CoherenceBarrierActiveError("J-2 typed barrier", dirty_generation=17,
              reason_code="journal_publication_failed", transaction_id="j2-runwide",
              account_scope_id=env.paths.account_root.name, operation="j2:mapper")
             if failure == "typed" else SystemExit("J-2 job-only escape"))
    if failure == "breaker":
        error = AccountWriteBlockedError(retry_after=12, blocked_until=24, reason="J-2 fixture breaker")
    elif failure == "governor":
        error = WriteGovernorUnavailableError(str(env.jobs), "J-2 fixture governor")
    elif failure == "admission":
        error = AdmissionRefusal("overlap_conflict", "J-2 fixture admission", retry_after=12)
    original = nmc.map_source_snapshot
    def mapped(snapshot, **kwargs):
        if Path(snapshot.canonical_path).name == "zz_runwide.py":
            raise error
        return original(snapshot, **kwargs)
    monkeypatch.setattr(nmc, "map_source_snapshot", mapped)
    ack = await launch_real_job(env, "folder")
    await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    control = env.controls[ack["job_id"]]
    terminal = await _joined_complete_detail(ack["job_id"])
    summary = terminal["result_summary"]
    assert terminal["status"] == summary["status"] == "failed"
    assert summary["successful"] >= 3 and summary["partial_success"]
    row = next(r for r in summary["files"] if Path(r["file"]).name == "zz_runwide.py")
    assert row["status"] == "failed" and "local_cpu_failure" not in row
    assert control.cpu_failure[1] is error
    assert summary["infrastructure_error"]["type"] == type(error).__name__
    expected_kind = (getattr(error, "code", "coherence_barrier_active") if failure in {"typed", "breaker"} else
                      "overlap_conflict" if failure == "admission" else "carto_job_exception")
    assert terminal["error_kind"] == expected_kind
    assert terminal["error_extra"]["phase"] == "folder:new-source"
    if failure == "breaker":
        assert terminal["error_extra"]["retry_after"] == 12
        assert terminal["error_extra"]["blocked_until"] == 24
        assert terminal["error_extra"]["reason"] == "J-2 fixture breaker"


@pytest.mark.asyncio
async def test_r3_terminal_honors_findings_only_status_and_exact_preview(real_job_environment, monkeypatch):
    from beacon_findings import make_beacon_finding
    env = real_job_environment
    (env.repo / "broken.py").write_text("def now_ok():\n    pass\n", encoding="utf-8")
    original = support.analyze_beacon_snapshot
    def analyze(snapshot, **kwargs):
        if Path(snapshot.canonical_path).name == "source.py":
            return [make_beacon_finding(snapshot.canonical_path, i + 1, "duplicate_id", None,
                    "advisory", {}, snapshot=snapshot, producer="beacon.refresh") for i in range(250)]
        return original(snapshot, **kwargs)
    monkeypatch.setattr(support, "analyze_beacon_snapshot", analyze)
    ack = await launch_real_job(env, "folder")
    await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    terminal = await _joined_complete_detail(ack["job_id"])
    summary = terminal["result_summary"]
    assert terminal["status"] == summary["status"] == "completed_with_warnings"
    assert summary["warnings"] == []
    assert len(summary["findings"]) == 200
    total = sum(len(r["findings"]) for r in summary["files"])
    assert summary["findings_total"] == total and summary["findings_truncated"] == total - 200
    assert summary["findings_by_code"]["duplicate_id"] == 250
    assert len(next(r for r in summary["files"] if Path(r["file"]).name == "source.py")["findings"]) == 250


@pytest.mark.asyncio
async def test_r3_folder_cancellation_recovers_saved_complete_rows(real_job_environment, monkeypatch):
    env = real_job_environment
    original = acn.WorkFlowyClientNexus.refresh_file_node_beacons
    completed = []
    async def observed(*args, **kwargs):
        value = await original(*args, **kwargs)
        completed.append(value)
        if value.get("success"):
            next(iter(env.controls.values())).request_user_cancel()
        return value
    monkeypatch.setattr(acn.WorkFlowyClientNexus, "refresh_file_node_beacons", observed)
    ack = await launch_real_job(env, "folder")
    await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    terminal = await _joined_complete_detail(ack["job_id"])
    assert terminal["status"] == "cancelled", terminal
    assert terminal["result_summary"]["cancelled"] is True
    assert terminal["result_summary"]["successful"] == 1
    assert terminal["result_summary"]["files"][0]["file"] == completed[0]["file"]
    assert terminal["result_summary"]["files"][0]["status"] == "refreshed"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["satisfied", "absent", "unknown", "rejected"])
async def test_r7_joined_single_f12_gate_uses_real_writer_classification(real_job_environment, monkeypatch, case):
    env = real_job_environment
    path = env.repo / "source.py"
    _write(path, block("python", "existing") + ["def existing():", "    return 2"])
    note = "AST_QUALNAME: existing\nBEACON (AST)\nid: existing\ncomment: Own existing.\n"
    name = "existing #one #two" if case in {"satisfied", "rejected"} else "existing"
    if case == "absent":
        assert nmc.update_beacon_from_node_python(str(path), name, note)["operation"] == "deleted_beacon"
    if case == "satisfied":
        nmc.update_beacon_from_node_python(str(path), name, note)
    if case == "unknown":
        name, note = "missing #one", "AST_QUALNAME: missing\nBEACON (AST)\nid: missing\n"
    if case == "rejected":
        name = "existing #newlabel"
        commit = nmc.commit_beacon_edit_plan
        def external_save(plan, **kwargs):
            path.write_bytes(path.read_bytes() + b"\n# external edit\n")
            return commit(plan, **kwargs)
        monkeypatch.setattr(nmc, "commit_beacon_edit_plan", external_save)
    rows = _observe_joined_file(monkeypatch)
    writer_rows = []
    original = acn.WorkFlowyClientNexus.update_beacon_from_node
    async def writer(*args, **kwargs):
        value = await original(*args, **kwargs)
        writer_rows.append(value)
        return value
    monkeypatch.setattr(acn.WorkFlowyClientNexus, "update_beacon_from_node", writer)
    class Socket:
        async def send(self, data):
            self.reply = json.loads(data)
    socket = Socket()
    await srv._handle_refresh_file_node({"node_id": env.ids["symbol"], "node_name": name, "node_note": note}, socket)
    ack = socket.reply
    assert ack["success"], ack
    await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    terminal = env.controls[ack["job_id"]].terminal_result
    value = writer_rows[-1]
    allowed = case in {"satisfied", "absent"}
    assert value["success"] is allowed and value["satisfied"] is allowed
    assert bool(rows) is allowed
    if allowed:
        assert value["operation"] == "noop" and value["noop_reason"] in {"already_satisfied", "already_absent"}
        assert terminal["status"] in {"completed", "completed_with_warnings"}
    else:
        assert terminal["status"] == "failed"
        assert terminal["result_summary"]["preparation"] == value
        assert terminal["result_summary"]["refresh_launched"] is False
