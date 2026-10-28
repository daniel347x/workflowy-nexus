"""U5 — Cartographer refresh FAIL-CLOSED guards (spec [ap:HFYAG2], tether [ap:2DXBJA]).

Proves, against the LOCKED UnitSpec's fault matrix (§7) and required tests (§9):

- **Guard 1 (root-validity refusal)** — a folder refresh whose resolved root is
  missing / not a directory / unmounted, or for which no ``Root:``/``Path:``
  header resolved at all, raises ``NetworkError`` BEFORE any write — on
  ``dry_run`` too.  There is no force bypass for an invalid root.
- **Guard 2a (FILE-node delete threshold)** — the folder missing-file sweep
  refuses before the per-file refresh loop and the deletion pass when
  ``missing_count`` exceeds ``governance.carto_folder_file_delete_threshold``,
  naming the count, the threshold and the override; an explicit per-run
  ``force=True`` proceeds; normal churn (≤ threshold) is never blocked; the
  dry run plans without refusing.
- **Guard 2b (armed reconcile threshold)** — the one live ``reconcile_tree``
  call site in ``refresh_file_node_beacons`` passes
  ``max_delete_threshold=governance.carto_reconcile_delete_threshold`` and the
  per-run ``force``, so an emptied-file reconcile aborts before its DELETE
  phase unless forced.
- **The force channel end-to-end** — ``_beacon_map_refresh_impl(force=True)``
  → ``extra_job_fields`` → job JSON ``force: true`` → the real daemon runner
  CARTO_REFRESH branch (FILE and FOLDER) → both refresh functions' ``force``
  kwarg; the F12+1 WS handlers forward a confirmed override and nothing else;
  the MCP registry exposes ``force`` with never-the-default wording.
- **Config** — the two ``GovernanceConfiguration`` thresholds with
  conservative defaults (10 / 100), camelCase aliases, ``ge=0``, and the
  closed schema (``extra="forbid"``) unchanged.

Harness pattern after ``tests/test_s06_folder_refresh_cleanup.py`` — fake the
Workflowy client surface; no live account.  Pre-fix status per spec §9 (each
marked below and mutation-verified for the unit's RESULT): tests 1, 2, 3 and
the reconcile test fail against the pre-unit tree (the log-and-continue
proceeded and swept; no caller passed the reconcile threshold); the force and
normal-churn pins pass pre- and post-fix by construction — they prove the
override works and that healthy runs are untouched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from n07_job_harness import real_job_environment, scrubbed_runtime
from test_cut_2b_fences import assert_worker_absent

from workflowy_mcp import server
from workflowy_mcp.client import api_client_nexus
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
from workflowy_mcp.mcp_api_registry import dispatch_operation
from workflowy_mcp.models import NetworkError
from workflowy_mcp.models.config import GovernanceConfiguration, ServerConfig

# ---------------------------------------------------------------------------
# Shared folder-sync harness (s06 pattern): fake the client surface, observe
# every mutation call, run the REAL refresh_folder_cartographer_sync.
# ---------------------------------------------------------------------------


def _install_folder_fakes(monkeypatch, nodes, resolve_table):
    observed: dict = {"moves": [], "creates": [], "updates": [], "deletes": [], "refreshed": []}

    async def fake_export_nodes_impl(_client, node_id=None, **_kwargs):
        return {"nodes": [dict(n) for n in nodes]}

    def fake_resolve(*, node_id, nodes_by_id):
        return {"abs_path": resolve_table.get(str(node_id), ""), "root_abs": None}

    async def fake_refresh_file(_self, file_node_id, **kwargs):
        observed["refreshed"].append((str(file_node_id), kwargs))
        return {"success": True, "refreshed_file_nodes": 1}

    async def fake_move(_self, node_id, parent, position):
        observed["moves"].append((node_id, parent, position))
        return True

    async def fake_create(_self, req, _internal_call=False, **_kw):
        created_id = f"NEW-{len(observed['creates']) + 1}"
        observed["creates"].append(
            {"name": req.name, "parent_id": req.parent_id, "id": created_id}
        )
        return SimpleNamespace(id=created_id)

    async def fake_update(_self, node_id, req):
        observed["updates"].append(node_id)

    async def fake_delete(_self, node_id):
        observed["deletes"].append(node_id)
        return True

    monkeypatch.setattr(api_client_nexus, "export_nodes_impl", fake_export_nodes_impl)
    monkeypatch.setattr(
        api_client_nexus, "resolve_cartographer_path_from_node", fake_resolve
    )
    monkeypatch.setattr(
        WorkFlowyClientNexus, "refresh_file_node_beacons", fake_refresh_file
    )
    monkeypatch.setattr(WorkFlowyClientNexus, "move_node", fake_move)
    monkeypatch.setattr(WorkFlowyClientNexus, "create_node", fake_create)
    monkeypatch.setattr(WorkFlowyClientNexus, "update_node", fake_update)
    monkeypatch.setattr(WorkFlowyClientNexus, "delete_node", fake_delete)
    monkeypatch.delenv("CARTO_JOB_FILE", raising=False)
    return observed


def _install_governance(monkeypatch, *, folder_threshold=10, reconcile_threshold=100):
    """Pin the two U5 thresholds the guards read via get_server_config().

    ``raising=False`` keeps the mutation check pure: on a pre-fix tree (where
    api_client_nexus does not yet read server config) the patch is a no-op and
    the guard tests fail on the MISSING GUARD itself — the actual pre-fix
    behavior — not on a harness AttributeError.
    """
    governance = SimpleNamespace(
        carto_folder_file_delete_threshold=folder_threshold,
        carto_reconcile_delete_threshold=reconcile_threshold,
    )
    monkeypatch.setattr(
        api_client_nexus,
        "get_server_config",
        lambda **_kw: SimpleNamespace(governance=governance),
        raising=False,
    )


def _folder_nodes(root: Path, *, present: list[str], missing: list[str]):
    """FOLDER + present/missing FILE nodes in the strict-classifier shape
    (first-line Path: header, resolved abs candidate, basename in the name)."""
    nodes = [{"id": "FOLDER", "name": "📂 folder", "note": f"Path: {root}"}]
    table = {"FOLDER": str(root)}
    for i, name in enumerate(present):
        nid = f"P{i}"
        nodes.append(
            {
                "id": nid,
                "name": f"🐍 {name}",
                "note": f"Path: {root / name}",
                "parent_id": "FOLDER",
            }
        )
        table[nid] = str(root / name)
    for i, name in enumerate(missing):
        nid = f"M{i}"
        nodes.append(
            {
                "id": nid,
                "name": f"🐍 {name}",
                "note": f"Path: {root / name}",
                "parent_id": "FOLDER",
            }
        )
        table[nid] = str(root / name)
    return nodes, table


def _run_folder_sync(*, dry_run=False, force=False):
    client = object.__new__(WorkFlowyClientNexus)
    # Pass force ONLY when set: pre-fix trees (mutation check) accept the
    # non-force call and must then fail on the MISSING GUARD, not on a
    # TypeError for the missing parameter.
    kwargs: dict = {"dry_run": dry_run}
    if force:
        kwargs["force"] = True
    return asyncio.run(
        client.refresh_folder_cartographer_sync("FOLDER", **kwargs)
    )


def _assert_zero_writes(observed):
    assert observed["deletes"] == []
    assert observed["updates"] == []
    assert observed["creates"] == []
    assert observed["moves"] == []
    assert observed["refreshed"] == []


# ---------------------------------------------------------------------------
# Guard 1 — root-validity refusal (folder path)
# ---------------------------------------------------------------------------


def test_guard1_root_not_a_directory_refuses_with_zero_writes(monkeypatch, tmp_path):
    """Spec §9 test 1.  Root: resolves to a FILE on disk → isdir False →
    refusal naming the path; zero delete_node calls; dry_run raises too.

    FAILS PRE-FIX: the pre-unit tree logged the invalid root and continued
    with root_path=None, then swept every FILE node.
    """
    not_a_dir = tmp_path / "actually_a_file.py"
    not_a_dir.write_bytes(b"x = 1\n")
    nodes = [
        {"id": "FOLDER", "name": "📂 folder", "note": f"Path: {not_a_dir}"},
        {
            "id": "M0",
            "name": "🐍 missing.py",
            "note": f"Path: {tmp_path / 'missing.py'}",
            "parent_id": "FOLDER",
        },
    ]
    observed = _install_folder_fakes(
        monkeypatch,
        nodes,
        {"FOLDER": str(not_a_dir), "M0": str(tmp_path / "missing.py")},
    )
    _install_governance(monkeypatch)

    with pytest.raises(NetworkError) as excinfo:
        _run_folder_sync()
    message = str(excinfo.value)
    assert "root-validity guard" in message
    assert str(not_a_dir) in message
    _assert_zero_writes(observed)

    # Guard 1 fires on dry_run too (the file-level guard's idiom).
    with pytest.raises(NetworkError):
        _run_folder_sync(dry_run=True)
    _assert_zero_writes(observed)


def test_guard1_root_missing_or_unmounted_refuses_with_zero_writes(monkeypatch, tmp_path):
    """Spec §9 test 2 — the XY2N10 repro in miniature.  Root: resolves but
    the path does not exist (moved repo / unmounted drive) → refusal; nothing
    queued, nothing deleted.

    FAILS PRE-FIX: the pre-unit tree logged and continued; every FILE node
    then counted as missing and was swept.
    """
    unmounted = tmp_path / "unmounted-drive" / "__repos" / "workflowy-nexus-mcp-tools"
    nodes, table = _folder_nodes(unmounted, present=[], missing=["a.py", "b.py", "c.py"])
    nodes[0]["note"] = f"Root: {unmounted}"
    observed = _install_folder_fakes(monkeypatch, nodes, table)
    _install_governance(monkeypatch)

    with pytest.raises(NetworkError) as excinfo:
        _run_folder_sync()
    message = str(excinfo.value)
    assert "root-validity guard" in message
    assert str(unmounted) in message
    _assert_zero_writes(observed)


def test_guard1_no_resolved_root_header_refuses(monkeypatch, tmp_path):
    """Spec §6 guard 1's falsy branch: no Root:/Path: header resolves at all
    → refusal naming that fact.  (Pre-fix this ran with root_path=None and
    swept whatever the per-node resolution could not save.)"""
    nodes, table = _folder_nodes(tmp_path, present=[], missing=["a.py"])
    nodes[0]["note"] = "just prose — no Cartographer header at all"
    observed = _install_folder_fakes(monkeypatch, nodes, table)
    _install_governance(monkeypatch)

    with pytest.raises(NetworkError) as excinfo:
        _run_folder_sync()
    message = str(excinfo.value)
    assert "root-validity guard" in message
    assert "no Root:/Path:" in message
    _assert_zero_writes(observed)


# ---------------------------------------------------------------------------
# Guard 2a — FILE-node delete threshold on the folder sweep
# ---------------------------------------------------------------------------


def test_guard2a_all_files_missing_refuses_over_threshold_with_zero_writes(
    monkeypatch, tmp_path
):
    """Spec §9 test 3.  Valid but EMPTY root against N FILE nodes on record
    (unmounted content / wrong directory): missing_count=N above the
    threshold → refusal naming the count, the threshold, the resolved root
    and the override instruction; zero writes.

    FAILS PRE-FIX: all N nodes were swept.
    """
    root = tmp_path / "empty-root"
    root.mkdir()
    nodes, table = _folder_nodes(
        root, present=[], missing=["a.py", "b.py", "c.py", "d.py"]
    )
    observed = _install_folder_fakes(monkeypatch, nodes, table)
    _install_governance(monkeypatch, folder_threshold=2)

    with pytest.raises(NetworkError) as excinfo:
        _run_folder_sync()
    message = str(excinfo.value)
    assert "mass-deletion guard" in message
    assert "4 FILE nodes" in message  # the would-be count
    assert "threshold of 2" in message
    assert str(root) in message
    assert "force=True" in message  # the override instruction
    _assert_zero_writes(observed)


def test_guard2a_force_override_proceeds_and_sweeps_exactly_the_missing_set(
    monkeypatch, tmp_path
):
    """Spec §9 test 4.  Same over-threshold shape with the explicit per-run
    force=True → proceeds; the sweep deletes exactly the missing set; the
    present file still refreshes; the folder's force passes through to the
    per-file calls.

    PASSES PRE- AND POST-FIX for the sweep itself (pre-fix always swept) —
    this pins that the override channel works and stays explicit; the
    force-passthrough assertion is post-fix only.
    """
    root = tmp_path
    (root / "present.py").write_bytes(b"def present():\n    return 1\n")
    nodes, table = _folder_nodes(
        root, present=["present.py"], missing=["m1.py", "m2.py", "m3.py"]
    )
    observed = _install_folder_fakes(monkeypatch, nodes, table)
    _install_governance(monkeypatch, folder_threshold=2)

    result = _run_folder_sync(force=True)
    assert result["success"] is True
    assert sorted(observed["deletes"]) == ["M0", "M1", "M2"]
    assert result["files_deleted"] == 3
    assert [nid for nid, _kw in observed["refreshed"]] == ["P0"]
    # The folder function's force threads into every per-file refresh call.
    assert all(kwargs.get("force") is True for _nid, kwargs in observed["refreshed"])


def test_guard2a_normal_churn_under_threshold_proceeds_without_force(
    monkeypatch, tmp_path
):
    """Spec §9 test 5 — fault-matrix row "root valid + missing_count ≤
    threshold (normal temp-file churn)".  2 of 50 FILE paths missing against
    the default threshold 10 → proceeds WITHOUT force: normal churn is never
    blocked and the healthy path is unchanged."""
    root = tmp_path
    present = [f"mod{i:02d}.py" for i in range(48)]
    for name in present:
        (root / name).write_bytes(b"def f():\n    return 1\n")
    nodes, table = _folder_nodes(root, present=present, missing=["temp1.py", "temp2.py"])
    observed = _install_folder_fakes(monkeypatch, nodes, table)
    _install_governance(monkeypatch)  # defaults: folder threshold 10

    result = _run_folder_sync()  # no force
    assert result["success"] is True
    assert sorted(observed["deletes"]) == ["M0", "M1"]
    assert result["files_deleted"] == 2
    assert len(observed["refreshed"]) == 48


def test_guard2a_dry_run_plans_without_threshold_refusal(monkeypatch, tmp_path):
    """Spec §7 fault matrix, dry-run row: guard 2 does NOT fire on dry_run —
    the dry run plans (zero writes, as always); the real run is the one that
    refuses.  (Guard 1, by contrast, raises on dry_run too — pinned above.)"""
    root = tmp_path / "empty-root"
    root.mkdir()
    nodes, table = _folder_nodes(
        root, present=[], missing=["a.py", "b.py", "c.py", "d.py"]
    )
    observed = _install_folder_fakes(monkeypatch, nodes, table)
    _install_governance(monkeypatch, folder_threshold=2)

    result = _run_folder_sync(dry_run=True)  # missing_count 4 > 2 — but dry run
    assert result["success"] is True
    assert observed["deletes"] == []
    assert observed["creates"] == []
    assert result["files_deleted"] == 0


# ---------------------------------------------------------------------------
# Guard 2b — the armed reconcile threshold on the per-file path
# ---------------------------------------------------------------------------


def test_guard2b_emptied_file_reconcile_aborts_without_force_and_proceeds_with_it(
    monkeypatch, tmp_path
):
    """Spec §9 test 6.  A FILE whose on-disk source was truncated/blanked:
    the parse yields a near-empty tree and reconcile_tree plans to delete the
    FILE node's whole (5-symbol) subtree.  With the caller-passed
    max_delete_threshold (2) the armed reconcile aborts BEFORE its DELETE
    phase; with force=True the same plan proceeds.

    FAILS PRE-FIX: no caller passed the threshold, so the pre-unit tree
    deleted all 5 symbol roots without force.
    """
    source = tmp_path / "fixture.py"
    source.write_bytes(b"# accidentally blanked - the symbols are gone\n")

    file_node = {"id": "FILE", "name": "🐍 fixture.py", "note": f"Path: {source}"}
    children = [
        {
            "id": f"S{i}",
            "name": f"ƒ symbol_{i}()",
            "note": "",
            "parent_id": "FILE",
            "priority": i,
        }
        for i in range(5)
    ]
    nodes = [file_node, *children]
    observed: dict = {"deletes": [], "creates": [], "updates": [], "moves": []}

    async def fake_export_nodes_impl(_client, node_id=None, **_kwargs):
        return {"nodes": [dict(n) for n in nodes]}

    async def fake_list_nodes(_self, request):
        parent = str(getattr(request, "parentId", "") or "")
        kids = [dict(n) for n in nodes if n.get("parent_id") == parent]
        return [SimpleNamespace(model_dump=lambda n=node: dict(n) ) for node in kids], None

    async def fake_delete(_self, node_id):
        observed["deletes"].append(str(node_id))
        return True

    async def fake_create(_self, req, _internal_call=False, **_kw):
        created_id = f"NEW-{len(observed['creates']) + 1}"
        observed["creates"].append(req.name)
        return SimpleNamespace(id=created_id)

    async def fake_update(_self, node_id, req):
        observed["updates"].append(str(node_id))

    async def fake_move(_self, node_id, parent, position="top"):
        observed["moves"].append((str(node_id), str(parent), position))

    monkeypatch.setattr(api_client_nexus, "export_nodes_impl", fake_export_nodes_impl)
    monkeypatch.setattr(
        WorkFlowyClientNexus, "_mark_nodes_export_dirty", lambda _self, _ids: None
    )
    monkeypatch.setattr(WorkFlowyClientNexus, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(WorkFlowyClientNexus, "delete_node", fake_delete)
    monkeypatch.setattr(WorkFlowyClientNexus, "create_node", fake_create)
    monkeypatch.setattr(WorkFlowyClientNexus, "update_node", fake_update)
    monkeypatch.setattr(WorkFlowyClientNexus, "move_node", fake_move)
    _install_governance(monkeypatch, reconcile_threshold=2)
    monkeypatch.setenv("CARTO_JOB_DIR", str(tmp_path))
    monkeypatch.delenv("CARTO_JOB_FILE", raising=False)

    client = object.__new__(WorkFlowyClientNexus)

    # Without force: the armed threshold aborts before the DELETE phase —
    # zero deletions of the 5 unmatched symbol roots.
    result = asyncio.run(client.refresh_file_node_beacons("FILE", dry_run=False))
    assert observed["deletes"] == []
    assert (result.get("nodes_deleted") or 0) == 0
    # The source file itself is untouched by the aborted run.
    assert source.read_bytes() == b"# accidentally blanked - the symbols are gone\n"

    # With force=True: the same plan proceeds and deletes exactly the 5 roots.
    result_forced = asyncio.run(
        client.refresh_file_node_beacons("FILE", dry_run=False, force=True)
    )
    assert sorted(observed["deletes"]) == ["S0", "S1", "S2", "S3", "S4"]
    assert (result_forced.get("nodes_deleted") or 0) == 5


# ---------------------------------------------------------------------------
# Config — the two GovernanceConfiguration thresholds
# ---------------------------------------------------------------------------


def test_governance_configuration_exposes_both_thresholds_closed_schema_intact():
    """Spec §9 test 7: defaults 10 / 100, camelCase aliases, ge=0, and the
    closed schema (extra="forbid") unchanged."""
    config = ServerConfig.model_validate({"WORKFLOWY_API_KEY": "test-key"})
    assert config.governance.carto_folder_file_delete_threshold == 10
    assert config.governance.carto_reconcile_delete_threshold == 100

    tuned = ServerConfig.model_validate(
        {
            "WORKFLOWY_API_KEY": "test-key",
            "governance": {
                "cartoFolderFileDeleteThreshold": 3,
                "cartoReconcileDeleteThreshold": 7,
            },
        }
    )
    assert tuned.governance.carto_folder_file_delete_threshold == 3
    assert tuned.governance.carto_reconcile_delete_threshold == 7

    with pytest.raises(ValidationError):
        ServerConfig.model_validate(
            {"WORKFLOWY_API_KEY": "test-key", "governance": {"inventedThreshold": 1}}
        )
    with pytest.raises(ValidationError):
        ServerConfig.model_validate(
            {
                "WORKFLOWY_API_KEY": "test-key",
                "governance": {"cartoFolderFileDeleteThreshold": -1},
            }
        )

    bare = GovernanceConfiguration()
    assert (
        bare.carto_folder_file_delete_threshold,
        bare.carto_reconcile_delete_threshold,
    ) == (10, 100)


# ---------------------------------------------------------------------------
# The force channel end-to-end (spec §9 test 8)
# ---------------------------------------------------------------------------


def test_map_refresh_impl_threads_force_into_extra_job_fields(monkeypatch):
    """_beacon_map_refresh_impl(force=True) reaches extra_job_fields — and
    the registry surface accepts/threads the new parameter.  Without force,
    extra_job_fields stays None (absent means a normal guarded run)."""
    cache = {
        "FILE": {
            "id": "FILE",
            "name": "server.py",
            "note": "Path: src/server.py\nLINE COUNT: 10",
        },
    }
    monkeypatch.setattr(
        server,
        "_resolve_beacon_graph_handle",
        lambda _handle: ("FILE", {"resolved_uuid": "FILE", "nodes_by_id": cache}),
    )
    launches: list = []

    async def launch(root_uuid, mode, extra_job_fields=None, **_kwargs):
        launches.append((root_uuid, mode, extra_job_fields))
        return {
            "success": True,
            "job_id": "J1",
            "pid": 42,
            "detached": True,
            "job_file": "job.json",
            "log_file": "job.log",
            "note": "detached",
        }

    monkeypatch.setattr(server, "_start_carto_refresh_job", launch)

    asyncio.run(server._beacon_map_refresh_impl("FILE", "file"))
    asyncio.run(server._beacon_map_refresh_impl("FILE", "file", force=True))
    assert launches == [("FILE", "file", None), ("FILE", "file", {"force": True})]

    # Through the public registry surface (schema + handler wiring).
    registry = server.build_beacon_registry()
    result = asyncio.run(
        dispatch_operation(
            registry,
            "map.refresh",
            {"node_handle": "FILE", "mode": "file", "force": True},
        )
    )
    assert result["success"] is True
    assert launches[-1] == ("FILE", "file", {"force": True})

    # §11.4: the schema/describe show force with never-the-default wording.
    spec = registry.resolve("map.refresh")
    force_schema = spec.params_schema["properties"]["force"]
    assert force_schema["type"] == "boolean"
    assert "NEVER the default" in force_schema["description"]
    assert "force" not in spec.params_schema["required"]
    assert "fail-closed" in spec.returns["description"]


def _seed_lineage_with_mapped_file(tmp_path: Path, monkeypatch, source_file: Path):
    """_seed_lineage (test_coherence_s03_prepared_boundary) with a 'file' node
    that carries a real Path: header — the PLAIN (non-prepared) launch path
    runs the footprint preflight, which resolves the root's source path from
    the snapshot cache."""
    import test_coherence_s03_prepared_boundary as pb
    from coherence_harness import s02_node
    from workflowy_mcp.coherence import (
        initialize_manifest_if_absent,
        manifest_with_updates,
        new_bootstrap_manifest,
        write_manifest,
    )
    from workflowy_mcp.coherence.snapshot_store import (
        new_snapshot_candidate_path,
        write_snapshot_candidate,
    )

    probe, jobs_root, snapshot_dir = pb._make_client(tmp_path, monkeypatch)
    paths = probe._coherence_paths()
    account = paths.account_root.name
    manifest = initialize_manifest_if_absent(
        paths, new_bootstrap_manifest(account, now=pb.NOW)
    )
    nodes = [
        s02_node("vault", name="vault"),
        s02_node("repo", name="repo", parent_id="vault", note=f"Root: {tmp_path}"),
        s02_node(
            "file", name="file.py", parent_id="repo", note=f"Path: {source_file}"
        ),
    ]
    candidate = write_snapshot_candidate(
        new_snapshot_candidate_path(snapshot_dir),
        {"nodes": nodes},
        account_scope_id=account,
        epoch_number=manifest.epoch_number,
        epoch_id=manifest.epoch_id,
        incorporated_through_seq=0,
        publication_kind="full_refresh",
    )
    write_manifest(
        paths,
        manifest_with_updates(
            manifest, base_snapshot_path=candidate, coherence_mode="transition"
        ),
        expected_epoch_id=manifest.epoch_id,
        expected_head_seq=0,
    )
    client, _jr, _sd = pb._make_client(tmp_path, monkeypatch)
    return client, paths, account, manifest, candidate, jobs_root


def test_start_carto_refresh_job_writes_force_into_the_job_json(tmp_path, monkeypatch):
    """The REAL server launch seam: extra_job_fields={"force": True} lands in
    the job JSON the daemon task reads; a normal launch carries no force
    key at all (old job files stay byte-compatible)."""
    from test_coherence_s03_prepared_boundary import (  # noqa: PLC0415
        _install_launch_mocks,
    )
    from workflowy_mcp.coherence.operation_cache import (  # noqa: PLC0415
        cache_operation_boundary,
    )

    source_file = tmp_path / "mapped_file.py"
    source_file.write_bytes(b"def f():\n    return 1\n")

    async def scenario():
        forced_dir = tmp_path / "forced"
        client, _paths, account, manifest, candidate, jobs_root = (
            _seed_lineage_with_mapped_file(forced_dir, monkeypatch, source_file)
        )
        calls = _install_launch_mocks(
            monkeypatch, client, account, manifest, candidate, jobs_root, forced_dir
        )
        # The PLAIN launch path derives its admission footprint from the
        # operation's pinned cache (production enters the boundary at the MCP
        # dispatch front / the WS handler decorator); mirror that here.
        async with cache_operation_boundary(client, "u5-force-launch"):
            forced = await server._start_carto_refresh_job(
                "file",
                "file",
                {"force": True},
                launcher="system",
            )
        assert isinstance(forced, dict) and forced.get("success") is True, forced
        forced_payload = json.loads(Path(forced["job_file"]).read_text(encoding="utf-8"))
        assert forced_payload["force"] is True
        task = server._carto_inprocess_tasks.get(forced["job_id"])
        if task is not None:
            await task
        assert calls and calls[-1]["force"] is True

        # Re-seed an isolated twin environment: the forced job's claim is
        # completed under the real runner and left its ordinary cooldown.
        normal_dir = tmp_path / "normal"
        client2, _paths2, account2, manifest2, candidate2, jobs_root2 = (
            _seed_lineage_with_mapped_file(normal_dir, monkeypatch, source_file)
        )
        calls2 = _install_launch_mocks(
            monkeypatch, client2, account2, manifest2, candidate2, jobs_root2, normal_dir
        )
        async with cache_operation_boundary(client2, "u5-normal-launch"):
            normal = await server._start_carto_refresh_job(
                "file",
                "file",
                launcher="system",
            )
        assert isinstance(normal, dict) and normal.get("success") is True, normal
        normal_payload = json.loads(Path(normal["job_file"]).read_text(encoding="utf-8"))
        assert "force" not in normal_payload
        task2 = server._carto_inprocess_tasks.get(normal["job_id"])
        if task2 is not None:
            await task2
        assert calls2 and calls2[-1]["force"] is False

    asyncio.run(scenario())


def _rewrite_job_file(job_file: Path, **fields) -> None:
    payload = json.loads(job_file.read_text(encoding="utf-8"))
    payload.update(fields)
    job_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")


async def _assert_runner_force(env, monkeypatch, *, mode, force):
    """Observe the actual refresh call while the full real daemon pipeline executes."""
    from j_counterfactual import install_j_counterfactual
    install_j_counterfactual(monkeypatch)
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    method = "refresh_folder_cartographer_sync" if mode == "folder" else "refresh_file_node_beacons"
    original = getattr(env.client, method)
    calls = []

    async def observed(**kwargs):
        calls.append(kwargs["force"])
        return await original(**kwargs)

    monkeypatch.setattr(env.client, method, observed)
    async with cache_operation_boundary(env.client, "cut2b-force-front"):
        ack = await server._start_carto_refresh_job(env.ids[mode], mode,
            extra_job_fields={"force": True} if force else None)
    assert ack["success"], ack
    await env.tasks[ack["job_id"]]
    assert calls == [force]
    # J-2 retains the local failed row without failing useful folder work.
    expected = {"completed_with_warnings"} if mode == "folder" else {"completed", "completed_with_warnings"}
    assert env.controls[ack["job_id"]].terminal_result["status"] in expected
    assert env.probe.sends and not env.probe.violations


@pytest.mark.asyncio
async def test_weave_worker_file_branch_passes_job_force_into_refresh(real_job_environment, monkeypatch):
    """Historical worker name: real daemon FILE runner forwards explicit force=True."""
    await _assert_runner_force(real_job_environment, monkeypatch, mode="file", force=True)


@pytest.mark.asyncio
async def test_weave_worker_file_branch_absent_force_is_false(real_job_environment, monkeypatch):
    """Historical worker name: absent force stays False through the real daemon FILE runner."""
    await _assert_runner_force(real_job_environment, monkeypatch, mode="file", force=False)


@pytest.mark.asyncio
async def test_weave_worker_folder_branch_passes_job_force_into_refresh(real_job_environment, monkeypatch):
    """Historical worker name: real daemon FOLDER runner forwards explicit force=True."""
    await _assert_runner_force(real_job_environment, monkeypatch, mode="folder", force=True)


def test_ws_refresh_handlers_forward_confirmed_force_only(monkeypatch):
    """The F12+1 WS handlers: a payload carrying force:true (only ever sent
    by the popup's confirmed override row) becomes
    extra_job_fields={"force": True}; an ordinary payload forwards None —
    the guards stay armed for every legacy extension."""
    from test_carto_safety_phase0 import (  # noqa: PLC0415
        _Socket,
        _install_handler_fakes,
    )

    class Client:
        def _get_nodes_export_cache_nodes(self):
            return []

    launches = _install_handler_fakes(monkeypatch, Client())

    file_socket = _Socket()
    asyncio.run(
        server._handle_refresh_file_node(
            {"node_id": "FILE", "node_note": "Path: file.py", "force": True},
            file_socket,
        )
    )
    folder_socket = _Socket()
    asyncio.run(
        server._handle_refresh_folder_node(
            {"node_id": "FOLDER", "force": True}, folder_socket
        )
    )
    plain_socket = _Socket()
    asyncio.run(
        server._handle_refresh_file_node(
            {"node_id": "PLAIN", "node_note": "Path: plain.py"}, plain_socket
        )
    )
    plain_folder_socket = _Socket()
    asyncio.run(
        server._handle_refresh_folder_node({"node_id": "PFOLDER"}, plain_folder_socket)
    )

    assert [(row["root_uuid"], row["mode"]) for row in launches] == [
        ("FILE", "file"),
        ("FOLDER", "folder"),
        ("PLAIN", "file"),
        ("PFOLDER", "folder"),
    ]
    assert launches[0]["extra_job_fields"] == {"force": True}
    assert launches[1]["extra_job_fields"] == {"force": True}
    assert launches[2]["extra_job_fields"] is None
    assert launches[3]["extra_job_fields"] is None


# ---------------------------------------------------------------------------
# Negative postconditions (spec §6) — source-level pins
# ---------------------------------------------------------------------------


def test_negative_postconditions_grep_shape():
    """§6's four negative postconditions, as source-shape pins:
    1. the log-and-continue phrase is gone;
    2. the live reconcile call passes the threshold;
    4. no production signature defaults force to True."""
    repo = Path(__file__).resolve().parents[1]
    client_src = (repo / "src/workflowy_mcp/client/api_client_nexus.py").read_text(
        encoding="utf-8"
    )

    # (1) The log-and-continue is gone.
    assert "ignoring disk sync" not in client_src

    # (2) The one live reconcile_tree call site passes max_delete_threshold.
    call_index = client_src.index("reconcile_result = await _reconcile_tree_for_f12(")
    call_end = client_src.index(")", client_src.index("cancel_check_interval=10", call_index))
    call_text = client_src[call_index:call_end]
    assert "max_delete_threshold=" in call_text
    assert "force=force" in call_text

    # (4) force is never defaulted True in any production signature.
    assert_worker_absent(repo / "src/workflowy_mcp/weave_worker.py")
    for rel in (
        "src/workflowy_mcp/client/api_client_nexus.py",
        "src/workflowy_mcp/server.py",
        "src/workflowy_mcp/carto_job_governance.py",
        "src/workflowy_mcp/client/workflowy_move_reconcile.py",
        "src/workflowy_mcp/client/carto_reconcile_cpu.py",
        "src/workflowy_mcp/carto_job_runtime.py",
    ):
        text = (repo / rel).read_text(encoding="utf-8")
        assert "force: bool = True" not in text, rel
