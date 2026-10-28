"""G4 FOLDER Ledger edges through real jobs, plus the unreachable subtree lever."""
from __future__ import annotations

import asyncio
import ast
import copy
import inspect
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import nexus_map_codebase as nmc
from n07_job_harness import real_job_environment, scrubbed_runtime, launch_real_job
from test_beacon_integration_contract import _joined_complete_detail
from test_beacon_structure_recovery import block, corrupt
from workflowy_mcp import server as srv
from workflowy_mcp.client import api_client_nexus as acn


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True], ids=["per-file-exception", "cpu-return-cancel"])
async def test_cf92_prepared_hygiene_evidence_survives_exception_or_cancel(real_job_environment, monkeypatch, stop):
    env = real_job_environment
    path = env.repo / "source.py"
    path.write_text("\n".join(corrupt("python", 1) + ["def damaged_metadata():", "    return 0", ""] +
        block("python", "existing") + ["def existing():", "    return 1", ""]), encoding="utf-8")
    path.write_bytes(path.read_bytes().replace(b"comment=", b"comments="))
    if stop:
        original = acn.run_carto_cpu
        async def returned(phase, function, *args, **kwargs):
            value = await original(phase, function, *args, **kwargs)
            if phase == "file:source" and Path(args[0]) == path:
                control = next(iter(env.controls.values()))
                control.request_user_cancel()
                # The caller has not received the worker's return value yet.
                control.checkpoint(phase)
            return value
        monkeypatch.setattr(acn, "run_carto_cpu", returned)
    else:
        async def rejected(request):
            if request.method == "POST" and request.url.path.endswith("/nodes/" + env.ids["file"]):
                raise acn.NetworkError("CF-92 per-file write fixture")
        # Fail the first file's header update through its ordinary API wrapper.
        original = env.client.update_node
        async def update(node_id, *args, **kwargs):
            if str(node_id) == env.ids["file"]:
                raise acn.NetworkError("CF-92 per-file write fixture")
            return await original(node_id, *args, **kwargs)
        monkeypatch.setattr(env.client, "update_node", update)
    ack = await launch_real_job(env, "folder")
    await env.tasks[ack["job_id"]]
    detail = await _joined_complete_detail(ack["job_id"])
    row = next(row for row in detail["result_summary"]["files"] if Path(row["file"]) == path)
    assert row["status"] == "failed" and row["changed"] is True
    assert row["findings"] and row["repair_outcomes"] and row["events"]
    assert str(path) in detail["result_summary"]["files_changed"]
    assert b"comments=Own existing.," not in path.read_bytes()
    assert b"comment=Own existing.," in path.read_bytes()
    assert detail["status"] == ("cancelled" if stop else "completed_with_warnings")


@pytest.mark.asyncio
async def test_cf95_later_file_names_its_real_cause_without_changing_the_first_latch(real_job_environment, monkeypatch):
    env = real_job_environment
    original_map, original_facts = nmc.map_source_snapshot, acn._prepare_carto_file_facts
    def mapped(snapshot, **kwargs):
        if Path(snapshot.canonical_path).name == "source.py":
            raise ValueError("file X mapper cause")
        return original_map(snapshot, **kwargs)
    def facts(nodes, file_id, *args):
        if file_id == env.ids["other-file"]:
            raise TypeError("file Y facts cause")
        return original_facts(nodes, file_id, *args)
    monkeypatch.setattr(nmc, "map_source_snapshot", mapped)
    monkeypatch.setattr(acn, "_prepare_carto_file_facts", facts)
    ack = await launch_real_job(env, "folder")
    await env.tasks[ack["job_id"]]
    detail = await _joined_complete_detail(ack["job_id"])
    rows = {Path(row["file"]).name: row for row in detail["result_summary"]["files"]}
    assert rows["other.py"]["error"] == "file Y facts cause"
    assert rows["other.py"]["exception_type"] == "TypeError"
    assert str(env.controls[ack["job_id"]].cpu_failure[1]) == "file X mapper cause"
    assert detail["status"] == "completed_with_warnings"


@pytest.mark.asyncio
async def test_cf137_mass_delete_message_acknowledges_prior_folder_plan_note_writes(real_job_environment, monkeypatch):
    from n05_region_harness import publish_fixture_nodes
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    env = real_job_environment
    nodes = copy.deepcopy(env.initial_nodes)
    next(node for node in nodes if node["id"] == env.ids["file"])["note"] = f"Path: {env.repo / 'source.py'}"
    env.probe.nodes = {node["id"]: {"createdAt": 100, "modifiedAt": 100, "note": "", "priority": 0, **node} for node in nodes}
    publish_fixture_nodes(env.client, nodes)
    monkeypatch.setattr(acn, "get_server_config", lambda: SimpleNamespace(governance=SimpleNamespace(carto_folder_file_delete_threshold=0)))
    async with cache_operation_boundary(env.client, "g4:mass-delete-text"):
        ack = await srv._start_carto_refresh_job(env.ids["folder"], "folder")
    await env.tasks[ack["job_id"]]
    terminal = env.controls[ack["job_id"]].terminal_result
    assert terminal["status"] == "failed"
    assert "Nothing was deleted; folder-plan note updates may have been written" in terminal["error"]
    assert "no Workflowy write occurred" not in terminal["error"]
    assert any(method != "GET" for method, *_ in env.probe.sends)
    assert not any(method == "DELETE" for method, *_ in env.probe.sends)


@pytest.mark.asyncio
async def test_cf137_create_subtree_cancel_raises_without_counting_a_created_file():
    """Direct unit lever: this nested callback-only cancel is unreachable E2E."""
    source = textwrap.dedent(inspect.getsource(acn.WorkFlowyClientNexus.refresh_folder_cartographer_sync))
    tree = ast.parse(source)
    node = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_create_subtree")
    body = textwrap.dedent("".join(source.splitlines(keepends=True)[node.lineno - 1:node.end_lineno]))
    namespace = {**acn.__dict__, "folder_context": None, "cancel_callback": lambda: True,
                 "row": {}, "dry_run": False, "self": SimpleNamespace()}
    factory = "def factory():\n    new_files_created = 0\n" + textwrap.indent(body, "    ") + "\n    return _create_subtree, lambda: new_files_created\n"
    exec(compile(factory, "g4:create-subtree-unit", "exec"), namespace)
    create, count = namespace["factory"]()
    with pytest.raises(asyncio.CancelledError, match="File subtree creation cancelled"):
        await create("parent", {"name": "not created"})
    assert count() == 0 and namespace["row"] == {}
