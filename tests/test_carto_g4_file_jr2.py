"""G4 FILE differential, real findings-only front, retained artifacts and prefixes."""
from __future__ import annotations

import asyncio
import copy
import gc
import json
import weakref
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from n07_job_harness import real_job_environment, scrubbed_runtime, launch_real_job, install_carto_reservation
from test_beacon_integration_contract import _joined_complete_detail
from test_carto_g4_folder_jr2 import WeakRow, comparable_detail, synthetic_job, base_folder_definition
from workflowy_mcp import carto_job_record as records, config as cfg, server as srv, beacon_refresh_support as support
from workflowy_mcp.client import api_client_nexus as acn
from workflowy_mcp.carto_job_runtime import CartoJobSpec, CartoJobContext, CartoJobControl, carto_job_scope, run_carto_cpu

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_writers(monkeypatch):
    monkeypatch.setattr(records, "_job_records", {})
    for name in ("_CARTO_CLEARED_JOB_IDS", "_CARTO_CLEARED_JOB_PATHS", "_CARTO_CLEARED_WRITE_LOGGED"):
        monkeypatch.setattr(cfg, name, set())


@pytest.mark.asyncio
async def test_cf90_real_single_file_findings_only_uses_one_row_and_keeps_all_260_findings(real_job_environment, monkeypatch):
    from beacon_findings import make_beacon_finding
    env = real_job_environment
    original = support.analyze_beacon_snapshot
    def analyze(snapshot, **kwargs):
        if Path(snapshot.canonical_path).name == "source.py":
            return [make_beacon_finding(snapshot.canonical_path, i + 1, "duplicate_id", None,
                "advisory", {"members": [{"text": "complete anatomy"}]}, snapshot=snapshot, producer="beacon.refresh") for i in range(260)]
        return original(snapshot, **kwargs)
    monkeypatch.setattr(support, "analyze_beacon_snapshot", analyze)
    ack = await launch_real_job(env, "file")
    await env.tasks[ack["job_id"]]
    control = env.controls[ack["job_id"]]
    assert control.terminal_result["status"] == "completed_with_warnings"
    assert control.terminal_result["warning_count"] == 0
    saved = json.loads(Path(ack["job_file"]).read_text())
    assert saved["record_layout"] == "summary-head/2" and saved["evidence"]["files"]["rows"] == 1
    assert saved["result_summary"]["status"] == "refreshed"
    assert "files" not in saved["result_summary"] and "findings" not in saved["result_summary"]
    row = (await _joined_complete_detail(ack["job_id"]))["result_summary"]
    assert row["status"] == "refreshed" and row["success"] is True and row["warnings"] == []
    assert len(row["findings"]) == 260 and row["projection"]["findings"] == row["findings"]
    assert row["reconcile_children"]["$reconcile_children"]["count"] >= 1
    assert not {"successful", "failed", "skipped", "partial_success", "errors_total", "preview_totals"} & row.keys()
    assert records.record_for(ack["job_file"], create=False) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outline", [None, [], [{"name": "planned", "children": []}]])
async def test_single_file_complete_detail_equals_jr2_base_row_and_releases_the_return(monkeypatch, tmp_path, outline):
    """Independent base exit definitions and the switched writer, identical data."""
    fixture = json.loads((ROOT / "tests/fixtures/carto_job_records/g4_baseline.json").read_text())
    namespace = {}
    for name, entry in fixture["server_definitions"].items():
        exec(compile(entry["source"], "157041c:" + name, "exec"), srv.__dict__, namespace)
    path = tmp_path / "20261027-000000_file-diff.json"
    spec = CartoJobSpec(job_id="file-diff", job_file=path, log_file=None, job_type="CARTO_REFRESH", mode="file",
        root_uuid="file", claim_id="claim", account_scope_id="scope")
    expected_row = {"file": str(tmp_path / "source.py"), "status": "refreshed", "success": True, "changed": False,
        "findings": [{"code": "advisory", "severity": "info", "message": str(i)} for i in range(260)],
        "warnings": [], "events": [], "reconcile_children": outline, "unknown": {"nil": None, "list": []}}
    expected_row["projection"] = {key: copy.deepcopy(expected_row[key]) for key in ("findings", "reconcile_children")}
    async def noop(*args, **kwargs):
        pass
    monkeypatch.setattr(srv, "finalize_claim_async", noop)
    monkeypatch.setattr(srv, "finalize_cancelled_job_claim_async", noop)
    monkeypatch.setattr(srv, "_record_carto_task_terminal", lambda *args: None)
    monkeypatch.setattr(srv, "_ensure_carto_final_delivery", lambda: None)
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(srv, "_carto_live_job_projection", lambda: {})
    results = []
    for switched in (False, True):
        initial = {"id": spec.job_id, "type": spec.job_type, "mode": "file", "status": "running"}
        if switched:
            records.record_for(path).create(initial)
        else:
            path.write_text(json.dumps(initial))
        control = CartoJobControl(task=asyncio.current_task())
        context = CartoJobContext(path, path.with_suffix(""), None, control)
        returned = WeakRow(copy.deepcopy(expected_row))
        ref = weakref.ref(returned)
        outcome = {"result": returned}
        del returned
        with monkeypatch.context() as patch:
            if not switched:
                for name in ("_carto_task_terminal_payload", "_carto_terminal_payload_data"):
                    patch.setattr(srv, name, namespace[name])
            with carto_job_scope(context):
                await srv._finalize_carto_task(spec, control, outcome)
        results.append(await _joined_complete_detail(spec.job_id))
        if switched:
            gc.collect()
            assert ref() is None and "result" not in outcome
            assert "files" not in control.terminal_result["result_summary"]
        path.unlink()
    before, after = results
    assert before["status"] == "completed" and after["status"] == "completed_with_warnings"
    before["status"] = after["status"]
    before["result_summary"] = records.mark_carto_reconcile_children(before["result_summary"])
    assert comparable_detail(after, tmp_path) == comparable_detail(before, tmp_path)
    assert after["result_summary"] == records.mark_carto_reconcile_children(expected_row)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 2])
async def test_file_gesture_routed_to_folder_keeps_aggregate_detail_and_row_views(monkeypatch, tmp_path, count):
    """The existing directory delegate keeps FILE mode, even with one child."""
    old = await synthetic_job(monkeypatch, tmp_path, switched=False, mode="file",
                              method=base_folder_definition(), cases=("clean",) * count)
    old.path.unlink()
    current = await synthetic_job(monkeypatch, tmp_path, mode="file", cases=("clean",) * count)
    assert comparable_detail(current.detail, tmp_path) == comparable_detail(old.detail, tmp_path)
    assert "file" not in current.detail["result_summary"]
    assert len(current.detail["result_summary"]["files"]) == count
    saved = json.loads(current.path.read_text())
    assert "files" not in saved["result_summary"] and saved["evidence"]["files"]["rows"] == count
    view = records.read_carto_job_view(current.path, job_id="g4-folder", options={
        "selection": "files", "files": "all", "files_limit": 100, "files_offset": 0, "slice_offset": 0})
    assert view["result_summary"]["files"] == current.detail["result_summary"]["files"]


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_cf137_file_failed_return_counts_its_only_folder_diagnostic_write(real_job_environment, monkeypatch, existing):
    from n05_region_harness import node_uuid, publish_fixture_nodes
    env = real_job_environment
    if existing:
        nodes = copy.deepcopy(env.initial_nodes)
        nodes.append({"id": node_uuid("g4-existing-diagnostic"), "parent_id": env.ids["folder"],
            "name": "🚨 Parse error: broken.py", "note": f"File: {env.repo / 'broken.py'}\nold error"})
        env.probe.nodes = {n["id"]: {"createdAt": 100, "modifiedAt": 100, "note": "", "priority": 0, **n} for n in nodes}
        publish_fixture_nodes(env.client, nodes)
    ack = await launch_real_job(env, "folder")
    await env.tasks[ack["job_id"]]
    detail = await _joined_complete_detail(ack["job_id"])
    row = next(row for row in detail["result_summary"]["files"] if Path(row["file"]).name == "broken.py")
    assert row["status"] == "failed" and row["api_calls"] == 1
    assert row["nodes_updated" if existing else "nodes_created"] == 1
    assert row["nodes_created" if existing else "nodes_updated"] == 0


def seed_unchanged(env):
    from n05_region_harness import publish_fixture_nodes
    path = env.repo / "source.py"
    path.write_text("def existing():\n    return 2\n", encoding="utf-8")
    prepared = support.prepare_beacon_source(path, dry_run=True)
    nodes = [copy.deepcopy(n) for n in env.initial_nodes if n["id"] not in {env.ids["symbol"], env.ids["obsolete"]}]
    file = next(n for n in nodes if n["id"] == env.ids["file"])
    file["note"] = acn._merge_file_note_header("Path: source.py", 2, "source.py")
    child = prepared["map"]["children"][0]
    nodes.append({**copy.deepcopy(child), "id": env.ids["symbol"], "parent_id": env.ids["file"]})
    env.probe.nodes = {n["id"]: {"createdAt": 100, "modifiedAt": 100, "note": "", "priority": 0, **n} for n in nodes}
    publish_fixture_nodes(env.client, nodes)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unchanged", "changed", "failed", "interrupted"])
async def test_ra_unchanged_leaves_only_evidence_other_files_keep_source_journal_and_trace(real_job_environment, monkeypatch, case):
    from workflowy_mcp.client import workflowy_move_reconcile as driver
    env = real_job_environment
    if case == "unchanged":
        seed_unchanged(env)
    if case in {"failed", "interrupted"}:
        async def injected(**kwargs):
            context = acn.current_carto_job_context()
            await run_carto_cpu("fixture:trace", context.diagnostic_sink.append_batch, (("log", "R-a retained trace", "fixture"),))
            if case == "interrupted":
                context.job_control.request_user_cancel()
                context.job_control.checkpoint("fixture:interrupt")
            raise ValueError("R-a failed reconcile")
        monkeypatch.setattr(driver, "reconcile_tree", injected)
    ack = await launch_real_job(env, "file")
    await env.tasks[ack["job_id"]]
    directory = Path(ack["job_file"]).with_suffix("") / "file_refresh"
    sources = [p for p in directory.glob("*.json") if not p.name.endswith(".weave_journal.json")]
    journals = list(directory.glob("*.weave_journal.json"))
    trace = directory / "reconcile_debug.log"
    detail = await _joined_complete_detail(ack["job_id"])
    if case == "unchanged":
        assert sources == journals == [] and not trace.exists()
        assert detail["result_summary"]["changed"] is False
        assert detail["result_summary"]["_incremental_probe"]["source_json_file"]
        assert (Path(ack["job_file"]).with_suffix("") / "files.jsonl").is_file()
    else:
        assert len(sources) == len(journals) == 1 and trace.is_file() and trace.stat().st_size > 0
        assert detail["status"] == ("cancelled" if case == "interrupted" else "failed" if case == "failed" else "completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["fallback-second-item-fails", "cancel-after-verified-vector"])
async def test_cf125_verified_prefix_progress_and_cancel_journal_cf151_witnessed_placement(tmp_path, monkeypatch, cancel):
    from test_n04_reconcile_insert_after import (FILE, FakeAccount, small_interior, _node, _source,
        _build_client, _install_outer_preparation)
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    from workflowy_mcp.mutation_locks import mutation_region_locks
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    geometry = small_interior()
    geometry.source_nodes.insert(2, _node("added_second"))
    account = FakeAccount(str(tmp_path / "fixture.py"))
    account.seed(FILE, geometry.rows)
    if not cancel:
        account.llm_script = [404]
    client, governor, *_ = _build_client(tmp_path, account)
    live_artifact = acn._write_carto_file_artifact
    _install_outer_preparation(monkeypatch, tmp_path, _source(geometry.source_nodes), [])
    monkeypatch.setattr(acn, "_write_carto_file_artifact", live_artifact)
    job_file = governor.paths.jobs_root / "20261027-000000_prefix.json"
    job_file.parent.mkdir(exist_ok=True)
    job_file.with_suffix("").mkdir()
    claim = install_carto_reservation(job_file, root_uuid=FILE, jobs_root=job_file.parent)
    control = CartoJobControl(task=asyncio.current_task())
    context = CartoJobContext(job_file, job_file.with_suffix(""), None, control, claim_id=claim)
    records.record_for(job_file).create({"id": "prefix", "type": "CARTO_REFRESH", "mode": "file", "status": "running"})
    sends = []
    async def transport(request):
        if request.method == "POST":
            sends.append(request.url.path)
        if not cancel and request.method == "POST" and request.url.path.endswith("/nodes/") and len(account.created) == 1:
            return httpx.Response(400, json={"message": "second fallback item rejected"})
        response = await account(request)
        if cancel and request.method == "GET" and account.counts["llm_ops"]:
            control.request_user_cancel()
        return response
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(transport))
    error = None
    async with client._client, cache_operation_boundary(client, "g4:prefix"):
        async with mutation_region_locks(client=client, operation_label="g4:prefix",
            region_factory=lambda snapshot: (build_region_subtree(FILE, cache_by_id=snapshot.nodes_by_id),)) as held:
            with carto_job_scope(context):
                try:
                    await client.refresh_file_node_beacons(FILE, held_grant=held, force=True)
                except BaseException as caught:
                    error = caught
    count = 2 if cancel else 1
    assert error is not None and len(account.created) == count
    assert context.progress["nodes_created"] == count
    assert json.loads(job_file.read_text())["progress"]["nodes_created"] == count
    journals = list((context.job_dir / "file_refresh").glob("*.weave_journal.json"))
    assert len(journals) == 1
    entries = json.loads(journals[0].read_text())["entries"]
    completed = [entry["node_id"] for entry in entries if entry.get("op") == "create" and entry.get("status") == "completed"]
    assert completed == account.created
    if cancel:
        assert isinstance(error, asyncio.CancelledError)
        assert account.counts["llm_ops"] == 1 and account.counts["v1_create"] == 0
    else:
        assert error.reconcile_create_result.physical_placements == ("bottom",)
        assert account.counts["llm_ops"] == 1 and sum(path.endswith("/nodes/") for path in sends) == 2
