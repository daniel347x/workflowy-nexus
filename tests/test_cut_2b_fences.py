"""CUT-2B physical subtraction and independent preservation witnesses.

Pins are measured from 7afe1f4, never regenerated from the implementation.
Source-copy mutants are inspected, not imported or executed.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


def test_fault_image_is_captured_before_unwind_cleanup(tmp_path):
    """§6.5: restored temps/base bytes and memory come from the death image."""
    from coherence_harness import (
        ACCOUNT_A, InjectedFault, SeamCapture, exit_ops, make_coherence_root,
        reset_fixture_memory, restore_successor, seed_publication_record,
        snapshot_fault_image, s02_node, s04_install_base_manifest,
        s04_producer, s04_write_base_snapshot,
    )
    from workflowy_mcp.coherence import (
        SemanticEffect, append_journal_transaction, read_manifest,
    )
    from workflowy_mcp.coherence import refresh_authority as authority
    root = tmp_path / "isolated-death-image"
    paths = make_coherence_root(root, ACCOUNT_A)
    base = Path(s04_write_base_snapshot(
        paths, root / "snapshots", [s02_node("base")],
        epoch_number=0, epoch_id="b" * 32))
    s04_install_base_manifest(paths, base_snapshot_path=str(base), epoch_id="b" * 32)
    seed_publication_record(paths)
    capture = SeamCapture(root)
    with pytest.raises(InjectedFault):
        append_journal_transaction(
            paths, operation_kind="test", effects=[SemanticEffect.tombstone(["x"])],
            producer=s04_producer(), fs_ops=exit_ops("after_record_file_fsync", capture=capture))
    captured = capture.image
    assert any(name.endswith(".tmp") for name in captured.files)
    assert not any(name.endswith(".tmp") for name in snapshot_fault_image(root).files)
    selected = capture.restart_image()
    assert any(name.endswith(".tmp") for name in selected.files), "restart used post-unwind cleanup instead of the pre-raise image"
    # A restored manifest's absolute base pointer must not reach abandoned
    # post-unwind bytes in another fixture root.
    original_base = captured.files[base.relative_to(root).as_posix()]
    base.write_bytes(b"post-unwind poison; not a captured snapshot")
    reset_fixture_memory(root, accounts=(ACCOUNT_A,))
    destination = tmp_path / "wrong-successor-root" if os.getenv("CUT2B_NEUTRALIZE_RESTORE_ORIGIN") else root
    restored = restore_successor(selected, destination, ACCOUNT_A)
    try:
        assert Path(read_manifest(restored).base_snapshot_path).read_bytes() == original_base, "successor base pointer read post-unwind bytes outside the restored image"
        assert snapshot_fault_image(destination).files == captured.files
        assert authority._read_publication_record(restored) is None, "successor inherited old publication ownership"
    finally:
        reset_fixture_memory(root, accounts=(ACCOUNT_A,))
        reset_fixture_memory(destination, accounts=(ACCOUNT_A,))

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "src/workflowy_mcp/"
ALIASES = {
    "srv": PACKAGE + "server.py",
    "acn": PACKAGE + "client/api_client_nexus.py",
    "mgp": PACKAGE + "markdown_generate_pipeline.py",
    "cjg": PACKAGE + "carto_job_governance.py",
    "ww": PACKAGE + "weave_worker.py",
}
# N04 G1 ([ap:TIGASU] §14.1): four of these bodies — srv's bulk executor, both
# acn refresh bodies and mgp's inline phase 8 — are re-measured by the N04
# gate's regeneration script after the production edit; the fifth changed body
# (mgp.run_markdown_generate_pipeline) is manifest-only and has no entry here.
# JR2 G4 C1 re-measures one more after the production edit: J's terminal
# ingress recovers a registered writer's summary itself, so its joined-live-tree
# body is pinned here. JR2 G4 C2 re-measures the three acn bodies whose /2
# preparation changed (both refresh bodies and the source-file carrier); the
# remaining bodies and J_LAUNCHER_FULL keep their earlier measured bytes.
# JR2 Leg 7 re-measures exactly the three bodies the C3/Q6 working bytes moved
# (both acn refresh bodies and srv's terminal payload); all other J bodies and
# the launcher whole bytes stay at their earlier measured values.
J_PINS = {
    "srv._handle_refresh_file_node": "298396170cd2f0ccedb81b69f47ff07367fd70cca98a7358e071dfdd884a05ad",
    "srv._start_carto_refresh_job": "6163cc48f12e314a4994f19dfa21dc3aba25ee7cf3fe924507520e505776f308",
    "srv._start_carto_bulk_visible_apply_job": "69cc0e0ba04d532a70b6c524bd75587c35e8e84c3c54bfaab53fc22757e355f1",
    "srv._run_carto_bulk_visible_apply_job": "b99902a806e54afb919d771189406e5c649b907f44b9657a5713b2fde96952ed",
    "srv._carto_task_terminal_payload": "b83dbaeed124aeb28213f966d5b7dbcd7896943f8a35a20c51692f8f0c4c13fd",
    "acn.WorkFlowyClientNexus.refresh_file_node_beacons": "ec1ad981e4c134669a4160f5baa22a3c29a9e4b92c8e84b3cb9b525e7493b293",
    "acn.WorkFlowyClientNexus.update_beacon_from_node": "771a92a48c25ae5e17161162bff92de82fde3cb827c04bce8c76fcc613c5e934",
    "acn._apply_beacon_from_node_data": "81bf3f9504b52481caf696a8aedc332e240000aea5342fee0604af349cbbb901",
    "acn._prepare_carto_source_file": "8390984d91c743b69c37cc10ec032d9d73fd732b6bf48c1049954b55fac472ed",
    "acn._plan_carto_file_refresh": "422712ed181f6faef0897840764ea9883b7249086b904e35fbd8dcc9f0588d03",
    "acn.WorkFlowyClientNexus.refresh_folder_cartographer_sync": "025e3871f5134e8a31a2c12dd8546a8e2978f71098da8803e03fd46283c9201e",
    "mgp._write_generated_markdown": "927f756d7108bfdd3790bdf07abd73265091672af371882f62b6ba17c9789455",
    "mgp._run_phase8_refresh_inline": "cacc83da93917cd07c5d96a1b417dbff9a2dbfcb8aa2a6b940e4f592504feb61",
    "cjg.launch_carto_refresh_job": "782e1ba0832e4417d872e35458fb6b7efe3912e30f5c7595ab1f34a42817db98",
    "srv._bulk_apply_per_file_inner_loop": "ba91aaae42681ce65e0c96af2ceb33e5feee25eda927d5538b14ece6b5ca4779",
    "srv.update_beacon_from_node": "0b71c7249607e6157009524f23467b8da47e0bb8873c1254904d347dacc6136f",
    "acn._detect_duplicate_source_beacon_ids": "0117b74115bea889b1f212c4b81a368bad45b1098fad7dcf0ad26c8c38f0645f",
}
J_DELETED = {"ww.main", "mgp._launch_phase8_refresh_via_authority"}
J_CHANGED = {
    "mgp.run_markdown_generate_pipeline",  # CF-44 retained; J-4 one-publication join.
    "mgp._write_generated_markdown", "mgp._run_phase8_refresh_inline",
    "acn.refresh_file_node_beacons",
    "acn._prepare_carto_source_file",
    "acn._plan_carto_file_refresh",
    "acn.refresh_folder_cartographer_sync",
    "acn.update_beacon_from_node", "acn._apply_beacon_from_node_data",
    "srv._handle_refresh_file_node", "srv._carto_task_terminal_payload",
    "srv._run_carto_bulk_visible_apply_job", "srv._start_carto_bulk_visible_apply_job",
    "srv._bulk_apply_per_file_inner_loop",
}
J_LAUNCHER_FULL = "e1eae5325392596d22991c9fd088365ee07e4ace5b27d7cb888f40551cb083f3"

# Measured by git-show 7afe1f4 + exactly the six §6.2.1 wrapper dispositions.
# Never generated from the implementing tree. Retirement pins its unchanged
# exact-path/below-floor deletion tail; its new nonwaiting prefix is runtime tested.
COHERENCE_BASE_PINS = {
    "coherence/schema.py::initialize_manifest_if_absent": "47d84130ef531c941068b3f11a914bfe9276f904e10090b4156607e8df1b396b",
    "coherence/barrier.py::raise_global_dirty_barrier": "0b14e20d1f83944c7711d9e6197898c4f0533eae64343724921ae019118821cc",
    "coherence/barrier.py::_seal_recovery_or_barrier": "3c69408234f37b97e348517a6bfa08e03176461407a65f346cc7fbc233468961",
    "coherence/journal_store.py::append_journal_transaction": "226355742bac619e887641859af8bf1af767dc3fc53115d9c248a19356d457d0",
    "coherence/journal_store.py::retire_incorporated_journal_records": "c1402773b76820c25daed6998b8981056748cb37f6fa79fc6c0fdad15dfe73b7",
    "coherence/checkpoint.py::request_checkpoint_if_due": "1683d679d838e4947bf097c422f539709b4515111d0f525698dfdf81283637e2",
}


def _coherence_base_fingerprint(source, name):
    node = copy.deepcopy(qualified_definitions(source)[name])
    if name == "retire_incorporated_journal_records":
        index = next(i for i, stmt in enumerate(node.body) if isinstance(stmt, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "retired" for t in stmt.targets))
        node.body = node.body[index:]
    for part in ast.walk(node):
        if isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and part.body:
            first = part.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                part.body.pop(0)
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


@pytest.mark.parametrize("key", COHERENCE_BASE_PINS)
def test_cut2b_coherence_transactions_preserve_base(key):
    path, name = key.split("::")
    source = (ROOT / PACKAGE / path).read_text(encoding="utf-8")
    assert _coherence_base_fingerprint(source, name) == COHERENCE_BASE_PINS[key], key


@pytest.mark.parametrize("key", COHERENCE_BASE_PINS)
def test_cut2b_coherence_base_pins_reject_mutants(key):
    path, name = key.split("::")
    source = (ROOT / PACKAGE / path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    target.body.append(ast.parse("_unregistered_computation()", mode="exec").body[0])
    fingerprint = _coherence_base_fingerprint(ast.unparse(tree), name)
    if os.environ.get("CUT2B_NEUTRALIZE_COHERENCE_PINS"):
        fingerprint = COHERENCE_BASE_PINS[key]
    assert fingerprint != COHERENCE_BASE_PINS[key], f"coherence BASE pin missed {key}"


def _coherence_plane_offenders(sources):
    offenders = []
    for path, source in sources.items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"coherence_lock", "_ensure_mutex_seed"}:
                offenders.append(f"retired coherence definition {path}:{node.name}")
            if isinstance(node, (ast.Name, ast.Attribute)) and (getattr(node, "id", None) or getattr(node, "attr", None)) == "coherence_mutex":
                offenders.append(f"retired coherence path {path}")
            if isinstance(node, ast.Constant) and node.value == "coherence.mutex":
                offenders.append(f"retired coherence path {path}")
            if isinstance(node, (ast.ImportFrom, ast.Import)) and any(a.name == "coherence_lock" for a in node.names):
                offenders.append(f"retired coherence import {path}")
    return offenders


def test_cut2b_coherence_plane_is_physically_absent():
    sources = {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8")
               for p in (ROOT / PACKAGE / "coherence").glob("*.py")}
    assert _coherence_plane_offenders(sources) == []


@pytest.mark.parametrize("injection,offender", [
    ("def coherence_lock(paths):\n    pass\n", "retired coherence definition"),
    ("def _ensure_mutex_seed(paths):\n    pass\n", "retired coherence definition"),
    ("_path = 'coherence.mutex'\n", "retired coherence path"),
    ("_path = paths.coherence_mutex\n", "retired coherence path"),
    ("from .paths import coherence_lock\n", "retired coherence import"),
])
def test_cut2b_coherence_absence_tripwire_rejects_reintroduction(injection, offender):
    found = _coherence_plane_offenders({"source-copy.py": injection})
    if os.environ.get("CUT2B_NEUTRALIZE_COHERENCE_ABSENCE"):
        found = []
    assert any(offender in row for row in found), (offender, found)


def _coherence_scope_offenders(sources, *, inventory=True):
    offenders, sites = [], {}
    forbidden = {"_send_workflowy_write", "run_carto_cpu", "to_thread", "run_in_executor", "sleep", "send", "send_json"}
    for path, source in sources.items():
        tree = ast.parse(source)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncWith) or not any(
                isinstance(item.context_expr, ast.Call) and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "coherence_lock_async" for item in node.items
            ):
                continue
            cursor, owner = node, "<module>"
            while cursor in parents:
                cursor = parents[cursor]
                if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = cursor.name
                    break
            key = path + "::" + owner
            sites[key] = sites.get(key, 0) + 1
            for statement in node.body:
                for child in ast.walk(statement):
                    if isinstance(child, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                        offenders.append(f"await in coherence span {key}")
                    if isinstance(child, ast.Call):
                        leaf = getattr(child.func, "id", None) or getattr(child.func, "attr", None)
                        if leaf in forbidden:
                            offenders.append(f"forbidden work in coherence span {key}:{leaf}")
            cursor = node
            while cursor in parents:
                cursor = parents[cursor]
                if isinstance(cursor, (ast.With, ast.AsyncWith)) and any(
                    "publisher" in ast.unparse(item.context_expr) or "_mutex" in ast.unparse(item.context_expr)
                    for item in cursor.items
                ):
                    offenders.append(f"reverse coherence/publisher order {key}")
    expected = {
        "barrier.py::hard_stop_state": 1, "barrier.py::raise_global_dirty_barrier_async": 1,
        "checkpoint.py::_acquire_checkpoint_claim": 1, "checkpoint.py::run_checkpoint_rotation": 3,
        "refresh_authority.py::_write_claim_status": 1, "refresh_authority.py::_acquire_or_join": 1,
        "refresh_authority.py::_own_full_refresh": 1, "refresh_authority.py::run_compat_patch": 1,
        "epoch_publisher.py::publish_epoch": 1,
        "transition_create.py::record_external_additions": 1,
    }
    if inventory and sites != expected and not os.environ.get("CUT2B_NEUTRALIZE_LOCK_INVENTORY"):
        offenders.append(f"coherence site inventory: {sites}")
    return offenders


def test_cut2b_short_lock_order_and_scope():
    """Coherence subpiece; admission closure joins with this gate (§9.9.3)."""
    sources = {p.name: p.read_text(encoding="utf-8") for p in (ROOT / PACKAGE / "coherence").glob("*.py")}
    assert _coherence_scope_offenders(sources) == []
    assert _admission_scope_offenders(_production_sources()) == []
    await_copy = {
        "src/workflowy_mcp/injected.py":
            "async def admit_carto_job(paths):\n"
            "    async with admission_lock(paths):\n"
            "        await io()\n"
    }
    assert any(
        "await in admission span" in row
        for row in _admission_scope_offenders(await_copy, inventory=False)
    ), "admission await probe missed"
    io_copy = {
        "src/workflowy_mcp/injected.py":
            "async def admit_carto_job(paths):\n"
            "    async with admission_lock(paths):\n"
            "        run_carto_cpu('work', callback)\n"
    }
    assert any(
        "forbidden work in admission span" in row
        for row in _admission_scope_offenders(io_copy, inventory=False)
    ), "admission IO probe missed"


def test_cut2b_admission_lock_site_inventory_rejects_drift():
    """The three async admission fronts are a closed, exact lock-site inventory."""
    source = "async def injected(paths):\n    async with admission_lock(paths):\n        pass\n"
    found = _admission_scope_offenders({"src/workflowy_mcp/injected.py": source})
    assert any("admission lock site inventory" in row for row in found), found


@pytest.mark.parametrize("body,offender", [
    ("async with coherence_lock_async(paths):\n        await io()", "await in coherence span"),
    ("async with coherence_lock_async(paths):\n        run_carto_cpu('work', callback)", "forbidden work in coherence span"),
    ("with publisher._mutex:\n        async with coherence_lock_async(paths):\n            pass", "reverse coherence/publisher order"),
])
def test_cut2b_coherence_scope_rejects_await_io_and_reverse_order(body, offender):
    source = "async def injected(paths, publisher):\n    " + body + "\n"
    found = _coherence_scope_offenders({"source-copy.py": source}, inventory=False)
    if os.environ.get("CUT2B_NEUTRALIZE_LOCK_SCOPE"):
        found = []
    assert any(offender in row for row in found), (offender, found)


def test_cut2b_coherence_scope_inventory_rejects_site_drift():
    """Every admitted coherence span is in the closed inventory, none invented."""
    source = "async def injected(paths):\n    async with coherence_lock_async(paths):\n        pass\n"
    found = _coherence_scope_offenders({"source-copy.py": source})
    assert any("coherence site inventory" in row for row in found), found


# ---------------------------------------------------------------------------
# 2B-2 executable negatives: the one memory ownership authority, its exact
# file-only entries, and the retired persisted-file plane (§9.9.3).
# These are staged for the 2B-2 subset: the whole-unit negative postconditions
# (governor, owner family, snapshot proof, harness) stay open until 2B-3/2B-5.
# ---------------------------------------------------------------------------
PRODUCTION_ROOT = ROOT / "src"
CORPORA = ("tests/beacon_robustness_corpus", "tests/beacon_seams_corpus")

# File-qualified retired identities, each at its former file (§9.9.3). A
# definition, import, attribute or executable reference is a violation.
CUT2B_RETIRED_2B2 = {
    "src/workflowy_mcp/carto_job_governance.py": frozenset({
        "CleanupAttemptResult", "_PostClaimAdmissionFailure", "SweepResult",
        "validate_claim", "validate_cancellation_sentinel", "validate_q3_lease",
        "_artifact_key_for_record", "build_claim", "claim_path_for", "write_claim", "read_claim",
        "tombstone_path_for", "write_tombstone_max_merge", "read_tombstone",
        "cancellation_sentinel_path", "create_cancellation_sentinel",
        "read_cancellation_sentinel", "delete_cancellation_sentinel",
        "build_q3_lease", "q3_lease_path_for", "write_q3_lease", "read_q3_lease",
        "_InteractiveClaimSnapshot", "_interactive_claim_paths", "_claim_directory_stamp",
        "_project_interactive_claim", "_warn_interactive_claim_problems",
        "read_interactive_carto_claims", "invalidate_interactive_carto_claims",
        "publish_interactive_carto_claims",
        "_find_claim_files", "_find_lease_files", "_find_tombstone_files", "_read_carto_jobs",
        "_job_by_id", "_finalize_claim_locked_outcome", "finalize_claim_locked",
        "finalize_claim", "_claim_spawn_grace_active", "_persist_reaped_status",
        "_legacy_job_age", "sweep_region_governance_locked", "sweep_region_governance",
        "_claim_candidates_for_job", "inspect_carto_job_liveness", "carto_cancel_requested",
        "finalize_cancelled_job_claim", "_exact_job_file_in_cmdline",
        "cleanup_carto_auxiliary_artifacts_locked", "cleanup_carto_auxiliary_artifacts",
        "acquire_q3_leases", "acquire_q3_leases_locked", "_release_q3_leases_locked_outcomes",
        "_release_q3_leases_impl", "release_q3_leases", "release_q3_leases_locked",
        "pending_cleanup_obligations", "_clear_cleanup_obligation",
        "_cleanup_obligation_supervisor", "_cleanup_supervisor_done",
        "_schedule_cleanup_obligation", "_queue_q3_release_obligations",
        "_queue_claim_finalize_obligation", "_safe_claim_id", "retry_cleanup_obligations_async",
        "_find_job_file_by_id", "_raise_for_sweep_blockers", "_validated_continuation_parent",
        "_merge_job_identity", "_persist_governance_cleanup_failure",
        "os_lock", "async_os_lock", "acquire_os_lock_async",
    }),
    "src/workflowy_mcp/server.py": frozenset({
        "_read_carto_heartbeat_diagnostic", "_sweep_carto_governance",
        "_carto_account_scope_and_env",
    }),
    "src/workflowy_mcp/config.py": frozenset({"_carto_cancel_sentinel_exists"}),
    "src/workflowy_mcp/coherence/paths.py": frozenset({
        "coherence_lock", "_ensure_mutex_seed", "coherence_mutex",
    }),
    "src/workflowy_mcp/client/api_client_core.py": frozenset({"_adopt_coherence_head"}),
    "src/workflowy_mcp/coherence/operation_cache.py": frozenset({
        "pin_worker_operation_cache", "_adopt_phase8",
    }),
    "src/workflowy_mcp/markdown_generate_pipeline.py": frozenset({
        "_launch_phase8_refresh_via_authority",
    }),
}
DELETED_MODULE_FILES = (
    "src/workflowy_mcp/weave_worker.py",
    "src/workflowy_mcp/coherence/adoption.py",
)
SNAPSHOT_RETIRED = {
    "src/workflowy_mcp/carto_job_governance.py": frozenset({"load_usable_nodes_export_snapshot"}),
    "src/workflowy_mcp/server.py": frozenset({"_select_manifest_linked_snapshot"}),
    "src/workflowy_mcp/coherence/snapshot_store.py": frozenset({
        "CandidateSnapshotProof", "_stat_identity", "read_candidate_snapshot_proof",
        "verify_candidate_snapshot_identity", "_is_terminal_job_status",
    }),
}
for _snapshot_file, _snapshot_names in SNAPSHOT_RETIRED.items():
    CUT2B_RETIRED_2B2[_snapshot_file] = CUT2B_RETIRED_2B2.get(_snapshot_file, frozenset()) | _snapshot_names
# §6.6/§9.9.3 literal census; the scheduler's file lane is retired too.
RETIRED_ARTIFACT_LITERALS = (
    "coherence.mutex", "admission.mutex", ".refresh_locks", ".q3_leases",
    ".refresh_tombstones", ".cancel_requests", ".heartbeats", ".refresh_claims",
    "refresh_publication", ".mutation_leases",
    "write_slot.mutex",
)
# ---------------------------------------------------------------------------
# 2B-5 whole-unit closure (§6.5/§6.6/§14 A–B): the complete file-qualified
# delete roster.  Every value is a BASE (`git show 7afe1f4:<path>`)
# definition/assignment line, so the expected set is measured from BASE, never
# inferred from the implementing tree.  The digest pins the exact
# (file, name, line) roster: dropping a delete row from the assertion changes
# it, which keeps an omitted deletion from quietly disappearing.
# ---------------------------------------------------------------------------
CUT2B_BASE_REV = "7afe1f4"

CUT2B_RETIRED_BASE = {
    "src/workflowy_mcp/carto_job_governance.py": {
        "DEFAULT_START_GRACE_SECONDS": 46, "DEFAULT_SPAWN_GRACE_SECONDS": 47,
        "DEFAULT_ORPHAN_GRACE_SECONDS": 48, "CLAIM_REQUIRED_FIELDS": 133,
        "CANCELLATION_SENTINEL_REQUIRED_FIELDS": 167, "Q3_LEASE_REQUIRED_FIELDS": 168,
        "CleanupAttemptResult": 256, "SpawnRollbackResult": 270,
        "_PostClaimAdmissionFailure": 319, "SweepResult": 378,
        "acquire_os_lock_async": 566, "os_lock": 614, "async_os_lock": 636, "validate_claim": 959,
        "validate_cancellation_sentinel": 1025, "validate_q3_lease": 1036,
        "_artifact_key_for_record": 1245, "build_claim": 1250, "claim_path_for": 1292,
        "write_claim": 1300, "read_claim": 1313, "_InteractiveClaimSnapshot": 1318,
        "_INTERACTIVE_CLAIM_CACHE": 1325, "_interactive_claim_paths": 1328,
        "_claim_directory_stamp": 1335, "_project_interactive_claim": 1343,
        "_warn_interactive_claim_problems": 1367, "read_interactive_carto_claims": 1384,
        "invalidate_interactive_carto_claims": 1425, "publish_interactive_carto_claims": 1430,
        "tombstone_path_for": 1481, "write_tombstone_max_merge": 1490, "read_tombstone": 1507,
        "cancellation_sentinel_path": 1511, "create_cancellation_sentinel": 1518,
        "read_cancellation_sentinel": 1533, "delete_cancellation_sentinel": 1542,
        "build_q3_lease": 1548, "q3_lease_path_for": 1573, "write_q3_lease": 1582,
        "read_q3_lease": 1594, "ProcessIdentityError": 1598, "ProcessIdentity": 1603,
        "_linux_process_identity": 1622, "_windows_process_identity": 1668,
        "read_process_identity": 1693, "current_process_identity": 1714,
        "StoredOwnerIdentity": 1724, "classify_owner_liveness": 1755,
        "_stored_identity_matches_current": 1809, "_caller_identity_or_defaults": 1838,
        "classify_legacy_pid_liveness": 1869, "_find_claim_files": 1905, "_find_lease_files": 1911,
        "_find_tombstone_files": 1917, "_read_carto_jobs": 1923, "_job_by_id": 1937,
        "_finalize_claim_locked_outcome": 1952, "finalize_claim_locked": 2068,
        "finalize_claim": 2092, "_claim_spawn_grace_active": 2126, "_persist_reaped_status": 2135,
        "_legacy_job_age": 2163, "sweep_region_governance_locked": 2178,
        "sweep_region_governance": 2326, "_claim_candidates_for_job": 2347,
        "inspect_carto_job_liveness": 2368, "carto_cancel_requested": 2461,
        "finalize_cancelled_job_claim": 2481, "_exact_job_file_in_cmdline": 2562,
        "cleanup_carto_auxiliary_artifacts_locked": 2804,
        "cleanup_carto_auxiliary_artifacts": 2856, "acquire_q3_leases": 2998,
        "acquire_q3_leases_locked": 3007, "_release_q3_leases_locked_outcomes": 3072,
        "_release_q3_leases_impl": 3115,
        "_CLEANUP_OBLIGATIONS": 3151, "_CLEANUP_TASKS": 3152, "_CLEANUP_WAKE_EVENTS": 3153,
        "DEFAULT_CLEANUP_RETRY_ATTEMPTS": 3154, "DEFAULT_CLEANUP_RETRY_DELAY_SECONDS": 3155,
        "DEFAULT_CLEANUP_LATER_DELAY_SECONDS": 3156, "DEFAULT_CLEANUP_BACKOFF_MAX_SECONDS": 3157,
        "pending_cleanup_obligations": 3160,
        "_clear_cleanup_obligation": 3166, "_cleanup_obligation_supervisor": 3173,
        "_cleanup_supervisor_done": 3209, "_schedule_cleanup_obligation": 3233,
        "release_q3_leases": 3266, "release_q3_leases_locked": 3275,
        "_queue_q3_release_obligations": 3284, "_queue_claim_finalize_obligation": 3308,
        "_safe_claim_id": 3565, "retry_cleanup_obligations_async": 3572,
        "_find_job_file_by_id": 3652, "_raise_for_sweep_blockers": 3692,
        "_validated_continuation_parent": 3777, "load_usable_nodes_export_snapshot": 4197,
        "_merge_job_identity": 4240, "_persist_governance_cleanup_failure": 4268,
        "_transition_to_spawning": 4288, "_find_spawn_claim_locked": 4324,
        "_detached_worker_owns_claim": 4342, "_record_spawned_worker": 4346,
        "_rollback_spawn_failure_if_launcher_owned": 4400, "_spawn_governed_worker": 4499,
        "adopt_carto_worker_claim": 4873,
    },
    "src/workflowy_mcp/client/api_client_core.py": {
        "_adopt_coherence_head": 741,
    },
    "src/workflowy_mcp/coherence/operation_cache.py": {
        "pin_worker_operation_cache": 418,
    },
    "src/workflowy_mcp/coherence/paths.py": {
        "_MUTEX_NAME": 40, "coherence_mutex": 61, "coherence_lock": 215,
    },
    "src/workflowy_mcp/coherence/schema.py": {
        "_ensure_mutex_seed": 894,
    },
    "src/workflowy_mcp/coherence/snapshot_store.py": {
        "CandidateSnapshotProof": 341, "_stat_identity": 362, "read_candidate_snapshot_proof": 373,
        "verify_candidate_snapshot_identity": 415, "_is_terminal_job_status": 632,
    },
    "src/workflowy_mcp/config.py": {
        "_carto_cancel_sentinel_exists": 222,
    },
    "src/workflowy_mcp/markdown_generate_pipeline.py": {
        "_launch_phase8_refresh_via_authority": 812, "_adopt_phase8": 872,
    },
    "src/workflowy_mcp/server.py": {
        "CARTO_HEARTBEAT_STALE_SECONDS": 1283,
        "_read_carto_heartbeat_diagnostic": 1292, "_sweep_carto_governance": 2329,
        "_select_manifest_linked_snapshot": 10848, "_carto_account_scope_and_env": 10994,
    },
    "tests/mutation_lease_harness.py": {
        "live_identity": 177,
    },
}

# Whole modules the unit deletes; their top-level definitions retire with the
# file and may never reappear at that former path.
RETIRED_MODULE_BASE = {
    "src/workflowy_mcp/coherence/adoption.py": {
        "UNPOPULATED_EPOCH_ID": 77,
        "unpopulated_cache_view": 80, "is_unpopulated_view": 93, "CacheAdoptionResult": 111,
        "_LocalEpochAdvanced": 135, "_markers": 145, "_result": 149,
        "_check_local_manifest_compatibility": 172, "_has_above_head_records": 222,
        "_cas_publish": 244, "_replay_tail_onto": 289, "_publish_tail_replay": 311,
        "_materialize_reload": 387, "_publish_epoch_view": 398, "_reload_from_manifest": 457,
        "adopt_manifest_head": 513, "_adopt_once": 546, "_manifest_advanced_past": 708,
        "_reload_with_rotation_restart": 718, "_recover_from_base": 753,
    },
    "src/workflowy_mcp/weave_worker.py": {
        "_merge_write_carto_job_status": 37, "_carto_relpath_memo": 68,
        "HEARTBEAT_INTERVAL_SECONDS": 70, "HEARTBEAT_JOIN_TIMEOUT_SECONDS": 71,
        "log_worker": 57, "CartoHeartbeatController": 80,
        "_start_carto_heartbeat_after_adoption": 162,
        "_resolve_carto_file_relpath_for_progress": 219, "main": 280,
    },
    "tests/stage2_harness.py": {
        "REPO": 35, "TESTS_DIR": 36,
        "launch_actor": 50, "run_actor": 56, "pid_marker_code": 76, "read_pid": 82,
        "collect_distinct_pids": 98, "write_result": 124, "read_result": 129, "finish_actor": 140,
    },
}

CUT2B_RETIRED = {
    path: frozenset(row)
    for path, row in {**CUT2B_RETIRED_BASE, **RETIRED_MODULE_BASE}.items()
}
OWNER_IDENTITIES = frozenset({
    "ProcessIdentityError", "ProcessIdentity", "_linux_process_identity",
    "_windows_process_identity", "read_process_identity", "current_process_identity",
    "StoredOwnerIdentity", "classify_owner_liveness", "_stored_identity_matches_current",
    "_caller_identity_or_defaults", "classify_legacy_pid_liveness",
})
RETIRED_MODULE_IMPORTS = (
    r"\bimport\s+(?:workflowy_mcp\.)?weave_worker\b",
    r"\bfrom\s+(?:workflowy_mcp\.)?weave_worker\b",
    r"\bimport\s+(?:workflowy_mcp\.coherence\.)?adoption\b",
    r"\bfrom\s+(?:workflowy_mcp\.coherence\.)?adoption\s+import\b",
    r"\bfrom\s+workflowy_mcp\.coherence\s+import\s+adoption\b",
    r"\bimport\s+stage2_harness\b",
    r"\bfrom\s+stage2_harness\s+import\b",
)
RETIRED_PROCESS_HARNESS_SEAMS = ("exit_ops", "s02_exit_ops")
CUT2B_RETIRED_DIGEST = "df03ecf177cc965d1789f4bf88b7ec8ff77bef2d1ef445060562cf54534f17cd"

RETAINED_DURABLE_WRITERS = {
    "src/workflowy_mcp/carto_job_governance.py": {"atomic_write_text", "atomic_write_json"},
    "src/workflowy_mcp/config.py": {"merge_write_carto_job_status"},
}
RETAINED_ARTIFACT_NAMES = {
    "src/workflowy_mcp/carto_write_governor.py": {"write_breaker.json", "write_governor.json"},
}
# The one registry's containers may be written only by these private helpers.
REGISTRY_AUTHORITY_NAMES = frozenset({
    "_LOCKS", "_governance_holds", "_reserve_carto_hold", "_acquire_q3_holds",
    "_release_q3_holds", "_finalize_carto_hold", "_carto_hold_protection",
    "_require_carto_hold", "_reset_carto_region_binding", "_governance_cooldowns",
    "registry_held_regions", "registry_held_keys",
})
OFFLOAD_HEADS = frozenset({"to_thread", "run_in_executor", "submit", "Thread", "run_carto_cpu"})
REGION_BINDING_ALLOWED = frozenset({
    ("workflowy_mcp.mutation_locks", "_RegionHold"),
    ("workflowy_mcp.mutation_locks", "_acquire_region_set"),
    ("workflowy_mcp.mutation_locks", "mutation_region_locks"),
    ("workflowy_mcp.mutation_locks", "_reset_carto_region_binding"),
})
HOLD_APPEND_ALLOWED = frozenset({
    ("workflowy_mcp.mutation_locks", "_acquire_region_set"),
    ("workflowy_mcp.mutation_locks", "mutation_region_locks"),
})


def _production_sources():
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((PRODUCTION_ROOT / "workflowy_mcp").rglob("*.py"))
    }


def _retired_identity_offenders(sources, roster=None):
    """§9.9.3: definitions at the former file, imports/aliases/calls anywhere.

    A name's *definition* is file-qualified (that is the retired identity);
    any import, alias, ``__all__`` export, dynamic ``getattr`` or executable
    reference of a retired name anywhere in production is a reintroduction.
    """
    if os.environ.get("CUT2B_NEUTRALIZE_RETIRED_IDENTITIES"):
        return []  # Test-only fence sensitivity.
    roster = CUT2B_RETIRED_2B2 if roster is None else roster
    # Whole-module identities are file-qualified: `main` and the moved
    # sentinel functions also legitimately exist elsewhere. Their import/call
    # origins are closed by _retired_module_reference_offenders, not a global
    # bare-name prohibition.
    names = set().union(*(names for path, names in roster.items()
                          if path not in RETIRED_MODULE_BASE)) if roster else set()
    offenders = []
    for relative, source in sorted(sources.items()):
        tree = ast.parse(source, filename=relative)
        former = roster.get(relative, frozenset())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in former:
                    offenders.append((relative, node.name, "retired definition"))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    leaf = alias.name.rsplit(".", 1)[-1]
                    if leaf in names or (alias.asname and alias.asname in names):
                        offenders.append((relative, leaf, "retired import"))
            elif isinstance(node, ast.Name) and node.id in names:
                offenders.append((relative, node.id, "retired reference"))
            elif isinstance(node, ast.Attribute) and node.attr in names:
                offenders.append((relative, node.attr, "retired reference"))
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            ):
                for element in ast.walk(node.value):
                    if isinstance(element, ast.Constant) and element.value in names:
                        offenders.append((relative, element.value, "retired export"))
            elif isinstance(node, ast.Call):
                leaf = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if leaf in {"getattr", "hasattr", "setattr"}:
                    for argument in node.args[1:]:
                        if isinstance(argument, ast.Constant) and argument.value in names:
                            offenders.append((relative, argument.value, "retired dynamic reference"))
    return offenders


def _retired_module_offenders(sources=None):
    """A deleted module file must be absent, and its definitions may not return."""
    if os.environ.get("CUT2B_NEUTRALIZE_RETIRED_MODULES"):
        return []  # Test-only fence sensitivity.
    sources = {} if sources is None else sources
    offenders = []
    for relative, definitions in RETIRED_MODULE_BASE.items():
        if relative not in sources and not (ROOT / relative).exists():
            continue
        offenders.append((relative, "retired module"))
        text = sources.get(relative)
        if text is None:
            path = ROOT / relative
            text = path.read_text(encoding="utf-8") if path.exists() else ""
        for node in ast.walk(ast.parse(text, filename=relative)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in definitions:
                offenders.append((relative, node.name, "retired definition"))
    return offenders


def _retired_module_reference_offenders(sources):
    """Deleted module origins remain forbidden through aliases/dynamic imports."""
    if os.getenv("CUT2B_NEUTRALIZE_RETIRED_MODULE_REFERENCES"):
        return []
    offenders = []
    for relative, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            modules = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
                modules.extend((node.module or "") + "." + alias.name for alias in node.names)
            elif isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id == "__import__")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
            ):
                modules.extend(arg.value for arg in node.args[:1]
                               if isinstance(arg, ast.Constant) and isinstance(arg.value, str))
            for module in modules:
                normalized = module.lstrip(".")
                retired = normalized in {
                    "weave_worker", "workflowy_mcp.weave_worker", "stage2_harness",
                    "coherence.adoption", "workflowy_mcp.coherence.adoption",
                }
                if normalized == "adoption" and isinstance(node, ast.ImportFrom):
                    retired = bool(node.level and "/coherence/" in relative)
                if retired:
                    offenders.append((relative, module, "retired module import"))
    return offenders


@pytest.mark.parametrize("source", [
    "from .coherence.adoption import adopt_manifest_head as engine\nengine(client)\n",
    "from .weave_worker import main as actor\nactor()\n",
    "import importlib\nactor = importlib.import_module('workflowy_mcp.weave_worker')\n",
    "from .coherence import adoption as old\nold.adopt_manifest_head(client)\n",
])
def test_cut2b_retired_module_imports_and_aliases_are_rejected(source):
    found = _retired_module_reference_offenders({PACKAGE + "server.py": source})
    assert any(row[-1] == "retired module import" for row in found), found


def _enclosing_function(node, parents):
    cursor = node
    while cursor in parents:
        cursor = parents[cursor]
        if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cursor.name
    return "<module>"


def _native_owner_offenders(sources):
    """§6.5/§6.6: no psutil identity plane and no CARTO process spawner."""
    if os.environ.get("CUT2B_NEUTRALIZE_NATIVE_OWNER"):
        return []  # Test-only fence sensitivity.
    offenders = []
    for relative, source in sorted(sources.items()):
        tree = ast.parse(source, filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if any((alias.name or "").split(".")[0] == "psutil" for alias in node.names):
                    offenders.append((relative, "psutil import"))
            elif isinstance(node, (ast.Name, ast.Attribute)):
                leaf = getattr(node, "id", None) or getattr(node, "attr", None)
                if leaf == "psutil":
                    offenders.append((relative, "psutil reference"))
    governance = "src/workflowy_mcp/carto_job_governance.py"
    source = sources.get(governance)
    if source is not None:
        tree = ast.parse(source, filename=governance)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                (alias.name or "").split(".")[0] == "subprocess" for alias in node.names
            ):
                offenders.append((governance, "subprocess import"))
            elif isinstance(node, ast.Call):
                leaf = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if leaf in {"Popen", "spawn", "posix_spawn", "fork"}:
                    offenders.append((governance, f"CARTO spawner {leaf}"))
    return offenders


def _os_primitive_offenders(sources):
    """§6.6: `msvcrt`/`fcntl` live only in the kept OS-lock authority."""
    if os.environ.get("CUT2B_NEUTRALIZE_OS_PRIMITIVES"):
        return []  # Test-only fence sensitivity.
    authority = "src/workflowy_mcp/carto_job_governance.py"
    allowed_scopes = {"_try_lock_handle", "_unlock_handle"}
    offenders = []
    for relative, source in sorted(sources.items()):
        tree = ast.parse(source, filename=relative)
        if relative != authority:
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    if {alias.name.split(".")[0] for alias in node.names} & {"msvcrt", "fcntl"}:
                        offenders.append((relative, node.lineno, "foreign OS primitive import"))
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                        and node.value.id in {"msvcrt", "fcntl"}:
                    offenders.append((relative, node.lineno, "foreign OS primitive call"))
            continue
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and {
                alias.name.split(".")[0] for alias in node.names
            } & {"msvcrt", "fcntl"}:
                if _enclosing_function(node, parents) not in allowed_scopes:
                    offenders.append((relative, node.lineno, "OS primitive outside the kept authority"))
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id in {"msvcrt", "fcntl"}:
                if _enclosing_function(node, parents) not in allowed_scopes:
                    offenders.append((relative, node.lineno, "OS primitive outside the kept authority"))
    return offenders


def _sole_os_acquirer_offenders(sources):
    """§6.6: the one production OS-lock acquirer is the singleton guard."""
    if os.environ.get("CUT2B_NEUTRALIZE_OS_CALLERS"):
        return []  # Test-only fence sensitivity.
    sites = []
    for relative, source in sorted(sources.items()):
        if not relative.startswith("src/"):
            continue
        module = relative[len("src/"):-3].replace("/", ".")
        tree = ast.parse(source, filename=relative)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                leaf = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if leaf == "acquire_os_lock":
                    sites.append((module, _enclosing_function(node, parents)))
    if sites != [("workflowy_mcp.server_bootstrap", "acquire_single_writer_guard")]:
        return [("os-lock acquirers", tuple(sites))]
    return []


def _retired_inventory_drift(inventory=None, modules=None):
    """The expected delete roster is the BASE-measured literal, digest-pinned."""
    if os.environ.get("CUT2B_NEUTRALIZE_RETIRED_INVENTORY"):
        return []  # Test-only fence sensitivity.
    inventory = CUT2B_RETIRED_BASE if inventory is None else inventory
    modules = RETIRED_MODULE_BASE if modules is None else modules
    triples = [(path, name, line) for path, row in inventory.items() for name, line in row.items()]
    triples += [("module:" + path, name, line) for path, row in modules.items() for name, line in row.items()]
    digest = hashlib.sha256(json.dumps(sorted(triples), separators=(",", ":")).encode("utf-8")).hexdigest()
    if digest != CUT2B_RETIRED_DIGEST:
        return [("retired inventory", "digest", digest)]
    return []


def _executable_test_sources():
    """Every executable test; both frozen corpora are excluded by path parts."""
    sources = {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "tests").rglob("*.py"))
        if not any(path.relative_to(ROOT).as_posix().startswith(corpus) for corpus in CORPORA)
    }
    assert not any(relative.startswith(CORPORA) for relative in sources), "corpus entered census"
    return sources


def _actor_payloads(test_sources):
    from test_mutation_leases_s05_fences import _actor_sources

    for relative, text in sorted(test_sources.items()):
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - an unparseable test is not an actor
            continue
        exempt = _cf16_witness_lines(tree)
        for line, payload in _actor_sources(tree, with_origins=True):
            if line in exempt:
                continue
            yield relative, payload


def _retired_actor_offenders(test_sources=None, roster=None):
    """Executable `-c` payloads in tests may not name a retired identity."""
    if os.environ.get("CUT2B_NEUTRALIZE_RETIRED_ACTORS"):
        return []  # Test-only fence sensitivity.
    roster = CUT2B_RETIRED_2B2 if roster is None else roster
    names = set().union(*(names for path, names in roster.items()
                          if path not in RETIRED_MODULE_BASE)) if roster else set()
    test_sources = _executable_test_sources() if test_sources is None else test_sources
    offenders = []
    for relative, payload in _actor_payloads(test_sources):
        offenders += _retired_module_reference_offenders({relative: payload})
        for name in names:
            if re.search(rf"\b{re.escape(name)}\b", payload):
                offenders.append((relative, name, "retired actor"))
    return offenders


def _process_harness_actor_offenders(test_sources=None):
    """§6.5: no executable actor runs the retired process/fault harness."""
    if os.environ.get("CUT2B_NEUTRALIZE_PROCESS_HARNESS"):
        return []  # Test-only fence sensitivity.
    test_sources = _executable_test_sources() if test_sources is None else test_sources
    offenders = []
    for relative, payload in _actor_payloads(test_sources):
        for pattern in RETIRED_MODULE_IMPORTS:
            if re.search(pattern, payload):
                offenders.append((relative, pattern, "retired module actor"))
        for seam in RETIRED_PROCESS_HARNESS_SEAMS:
            if re.search(rf"\b{re.escape(seam)}\b", payload):
                offenders.append((relative, seam, "process harness actor"))
    return offenders


def _cf16_witness_lines(tree):
    """The CF-16 witness's own retired-name payload is named test data."""
    lines = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "test_static_actor_sink_scans_replace_assembled_payloads"
        ):
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
                if name in {"launch_actor", "run_actor", "launch_subprocess", "Popen", "run",
                            "check_call", "check_output"}:
                    lines.add(call.lineno)
    return lines


def _snapshot_offenders(sources):
    """§6.4: physical proof/selector absence, one parse, no job GC edge."""
    if os.environ.get("CUT2B_NEUTRALIZE_SNAPSHOT_FENCE"):
        return []
    retired = set().union(*SNAPSHOT_RETIRED.values())
    offenders = []
    for relative, source in sources.items():
        tree = ast.parse(source)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in retired:
                offenders.append(f"retired snapshot identity {relative}:{node.name}")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name.rsplit('.', 1)[-1] in retired:
                        offenders.append(f"retired snapshot identity {relative}:{alias.name}")
            if isinstance(node, (ast.Name, ast.Attribute)):
                name = getattr(node, "id", None) or getattr(node, "attr", None)
                if name in retired:
                    offenders.append(f"retired snapshot identity {relative}:{name}")
            if isinstance(node, ast.Call) and any(
                kw.arg == "publication_kind" and isinstance(kw.value, ast.Constant) and kw.value.value == "job_basis"
                for kw in node.keywords
            ):
                offenders.append(f"job-basis producer {relative}:{node.lineno}")
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if relative.endswith("coherence/epoch_publisher.py") and function.name == "publish_epoch":
                reads = []
                for node in ast.walk(function):
                    if not isinstance(node, ast.Call):
                        continue
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name not in {"read_snapshot_envelope", "open", "stat", "read_bytes", "read_text"}:
                        continue
                    reads.append(node)
                    cursor = node
                    while cursor in parents:
                        cursor = parents[cursor]
                        if isinstance(cursor, ast.AsyncWith):
                            offenders.append(f"in-lock candidate read/stat {relative}:{node.lineno}")
                            break
                if len(reads) != 1 or (getattr(reads[0].func, "id", None) != "read_snapshot_envelope"):
                    offenders.append(f"single candidate parse {relative}: {len(reads)}")
            if relative.endswith("coherence/snapshot_store.py") and function.name == "collect_protected_snapshot_paths":
                for node in ast.walk(function):
                    if isinstance(node, ast.Call):
                        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                        if name in {"glob", "rglob", "_read_job_payload", "_is_terminal_job_status"}:
                            offenders.append(f"job scan {relative}:{node.lineno}")
                    if isinstance(node, ast.Constant) and node.value in {"snapshot_path", "status"}:
                        offenders.append(f"job scan {relative}:{node.lineno}")
    return offenders


def test_cut2b_snapshot_negative_postconditions():
    assert _snapshot_offenders(_production_sources()) == []


@pytest.mark.parametrize("relative,name", [(path, name) for path, names in SNAPSHOT_RETIRED.items() for name in sorted(names)])
def test_cut2b_snapshot_deleted_identity_tripwire(relative, name):
    found = _snapshot_offenders({relative: f"def {name}():\n    pass\n"})
    assert any("retired snapshot identity" in row and name in row for row in found), (name, found)


@pytest.mark.parametrize("relative,source,offender", [
    ("src/workflowy_mcp/coherence/epoch_publisher.py", "async def publish_epoch(paths):\n    envelope = read_snapshot_envelope(candidate)\n    async with coherence_lock_async(paths):\n        again = read_snapshot_envelope(candidate)\n", "in-lock candidate read/stat"),
    ("src/workflowy_mcp/coherence/epoch_publisher.py", "async def publish_epoch(paths):\n    pass\n", "single candidate parse"),
    ("src/workflowy_mcp/coherence/epoch_publisher.py", "from .snapshot_store import read_candidate_snapshot_proof as parse\n", "retired snapshot identity"),
    ("src/workflowy_mcp/server.py", "write_snapshot_candidate(path, payload, publication_kind='job_basis')\n", "job-basis producer"),
    ("src/workflowy_mcp/coherence/snapshot_store.py", "def collect_protected_snapshot_paths(root):\n    return root.glob('*.json')\n", "job scan"),
])
def test_cut2b_snapshot_reintroduction_mutations_are_rejected(relative, source, offender):
    found = _snapshot_offenders({relative: source})
    assert any(offender in row for row in found), (offender, found)


def test_cut2b_2b2_negative_postconditions():
    """Staged 2B-2 subset of §6.6's negatives (whole-unit final stays open)."""
    sources = _production_sources()
    assert _retired_identity_offenders(sources) == []
    assert _retired_actor_offenders() == []
    for relative in DELETED_MODULE_FILES:
        assert not (ROOT / relative).exists(), f"retired module is present: {relative}"


CUT2B_REINTRODUCTION_ROWS = [
    (relative, name, "definition")
    for relative, names in sorted(CUT2B_RETIRED.items())
    for name in sorted(names)
] + [
    ("src/workflowy_mcp/client/api_client_core.py", name, "owner-import")
    for name in sorted(OWNER_IDENTITIES)
] + [
    ("src/workflowy_mcp/server.py", "acquire_os_lock", "os-lock-outside-guard"),
    ("tests/injected_process_actor.py", "stage2_harness", "process-harness-actor"),
    ("tests/injected_process_actor.py", "exit_ops", "process-harness-actor"),
]


@pytest.mark.parametrize("relative,name,kind", CUT2B_REINTRODUCTION_ROWS)
def test_cut2b_reintroduction_mutations_are_rejected(relative, name, kind):
    if kind == "definition":
        if name == "coherence_mutex":
            injection = "\n\ndef injected(paths):\n    return paths.coherence_mutex\n"
        else:
            injection = f"\n\ndef {name}():\n    pass\n"
        found = _retired_identity_offenders({relative: injection}, CUT2B_RETIRED)
        knob = "CUT2B_NEUTRALIZE_RETIRED_IDENTITIES"
    elif kind == "owner-import":
        injection = f"from .carto_job_governance import {name} as alive\n"
        found = _retired_identity_offenders({relative: injection}, CUT2B_RETIRED)
        knob = "CUT2B_NEUTRALIZE_RETIRED_IDENTITIES"
    elif kind == "os-lock-outside-guard":
        injection = "def foreign_writer(path):\n    return acquire_os_lock(path, timeout_seconds=0)\n"
        found = _os_lock_caller_offenders({relative: injection})
        knob = "CUT2B_NEUTRALIZE_OS_CALLERS"
    else:
        payload = "import stage2_harness\n" if name == "stage2_harness" else "from coherence_harness import exit_ops\n"
        injection = f"PAYLOAD = {payload!r}\nsubprocess.Popen([sys.executable, '-c', PAYLOAD])\n"
        found = _process_harness_actor_offenders({"tests/injected_process_actor.py": injection})
        knob = "CUT2B_NEUTRALIZE_PROCESS_HARNESS"
    if os.environ.get(knob):
        found = []  # Test-only fence sensitivity.
    assert any(name in str(row) for row in found), (relative, name, kind, found)


def test_cut2b_retired_actor_sink_rejects_source_copy():
    """An executable -c payload naming a retired identity is a real reintroduction."""
    source = (
        'PAYLOAD = "from workflowy_mcp.carto_job_governance import build_claim\\n"\n'
        'subprocess.Popen([sys.executable, "-c", PAYLOAD])\n'
    )
    found = _retired_actor_offenders({"tests/injected_actor_test.py": source})
    assert any("build_claim" in str(row) for row in found), found


def _offload_registry_offenders(sources):
    """§14 D: no registry authority or writable registry inside a data worker."""
    if os.environ.get("CUT2B_NEUTRALIZE_REGISTRY_OFFLOAD"):
        return []  # Test-only fence sensitivity.
    offenders = []
    for relative, source in sorted(sources.items()):
        if not relative.startswith("src/"):
            continue
        tree = ast.parse(source, filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            leaf = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if leaf not in OFFLOAD_HEADS:
                continue
            argument_names = {
                child.id for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
                for child in ast.walk(argument) if isinstance(child, ast.Name)
            } | {
                child.attr for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
                for child in ast.walk(argument) if isinstance(child, ast.Attribute)
            }
            hit = argument_names & REGISTRY_AUTHORITY_NAMES
            if hit:
                offenders.append((relative, node.lineno, sorted(hit)))
    return offenders


def _hold_constructor_offenders(sources):
    """§6.2.2: reservation/Q3 holds are created with an empty graph."""
    if os.environ.get("CUT2B_NEUTRALIZE_FILE_ONLY_GRAPH"):
        return []  # Test-only fence sensitivity.
    source = sources.get("src/workflowy_mcp/mutation_locks.py")
    if source is None:
        return []
    offenders = []

    def visit(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = (*scope, node.name)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_RegionHold"
        ):
            regions = node.args[2] if len(node.args) > 2 else next(
                (keyword.value for keyword in node.keywords if keyword.arg == "regions"), None
            )
            empty = isinstance(regions, (ast.Tuple, ast.List)) and not regions.elts
            file_only = ".".join(scope) in {"_reserve_carto_hold", "_acquire_q3_holds"}
            if file_only and not empty:
                offenders.append(("file-only hold with graph regions", ".".join(scope)))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(ast.parse(source), ())
    return offenders


def _region_binding_offenders(sources):
    """§6.2.2/§14 C: one entry is rebound only by the existing door."""
    if os.environ.get("CUT2B_NEUTRALIZE_GRAPH_BINDING"):
        return []  # Test-only fence sensitivity.
    offenders = []
    for relative, source in sorted(sources.items()):
        if not relative.startswith("src/"):
            continue
        tree = ast.parse(source, filename=relative)
        module = relative[len("src/"):-3].replace("/", ".")

        def visit(node, scope):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope = (*scope, node.name)
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                targets = []
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "regions":
                    if (module, ".".join(scope)) not in REGION_BINDING_ALLOWED:
                        offenders.append((relative, ".".join(scope), "graph binding outside the door"))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "holds"
                and (module, ".".join(scope)) not in HOLD_APPEND_ALLOWED
            ):
                offenders.append((relative, ".".join(scope), "hold append outside acquisition"))
            for child in ast.iter_child_nodes(node):
                visit(child, scope)

        visit(tree, ())
    return offenders


REGISTRY_TYPE_NAMES = frozenset({
    "_RegionHold", "_GovernanceHold", "_RegionRegistry", "_TaskLockState",
    "_RegionWaiter", "_LOCKS",
})


def _foreign_registry_type_offenders(sources):
    """§6.2.2: the registry entry/table types never escape mutation_locks."""
    if os.environ.get("CUT2B_NEUTRALIZE_REGISTRY_TYPES"):
        return []  # Test-only fence sensitivity.
    offenders = []
    for relative, source in sorted(sources.items()):
        if not relative.startswith("src/") or relative.endswith("mutation_locks.py"):
            continue
        for node in ast.walk(ast.parse(source, filename=relative)):
            if isinstance(node, ast.Name) and node.id in REGISTRY_TYPE_NAMES:
                offenders.append((relative, node.lineno, node.id))
            elif isinstance(node, ast.Attribute) and node.attr in REGISTRY_TYPE_NAMES:
                offenders.append((relative, node.lineno, node.attr))
            elif isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                (alias.asname or alias.name.rsplit(".", 1)[-1]) in REGISTRY_TYPE_NAMES
                for alias in node.names
            ):
                offenders.append((relative, "retired import"))
    return offenders


def _single_registry_offenders(sources):
    """§6.2.2/§14 C: exactly one in-process registry, with graph-only projection."""
    if os.environ.get("CUT2B_NEUTRALIZE_SINGLE_REGISTRY"):
        return []  # Test-only fence sensitivity.

    source = sources.get("src/workflowy_mcp/mutation_locks.py")
    if source is None:
        return [("mutation_locks.py", "missing registry module")]
    tree = ast.parse(source)
    offenders = []
    registry_instances = [node for node in ast.walk(tree)
                          if isinstance(node, ast.Call)
                          and isinstance(node.func, ast.Name)
                          and node.func.id == "_RegionRegistry"]
    if len(registry_instances) != 1:
        offenders.append(("mutation_locks.py", "registry instance count", len(registry_instances)))
    declarations = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_LOCKS" for target in node.targets)
    ]
    if len(declarations) != 1:
        offenders.append(("mutation_locks.py", "registry declaration count", len(declarations)))
    elif not (
        isinstance(declarations[0].value, ast.Call)
        and getattr(declarations[0].value.func, "id", None) == "_RegionRegistry"
    ):
        offenders.append(("mutation_locks.py", "registry type"))
    projection = next(
        (
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "registry_held_regions"
        ),
        None,
    )
    text = ast.unparse(projection) if projection is not None else ""
    for required in (
        "_LOCKS.holders.values()",
        "_real_graph_region",
        "for hold in _LOCKS.holders.values() for r in hold.regions",
    ):
        if required not in text:
            offenders.append(("registry_held_regions", required))
    return offenders


@pytest.mark.parametrize("mutation", ["extra-table", "fabricated-regions"])
def test_cut2b_single_registry_declaration_rejects_extra_table_and_fabricated_regions(mutation):
    path = "src/workflowy_mcp/mutation_locks.py"
    source = _production_sources()[path]
    if mutation == "extra-table":
        # Keep the actual graph projection intact: it must be the second table
        # alone, not an unrelated broken projection, that rejects this mutant.
        source += "\n_SECOND = _RegionRegistry()\n"
        diagnostic = "registry instance count"
    else:
        node = qualified_definitions(source)["registry_held_regions"]
        lines = source.splitlines(keepends=True)
        source = "".join(lines[:node.lineno - 1]) + "def registry_held_regions():\n    return []\n" + "".join(lines[node.end_lineno:])
        diagnostic = "_LOCKS.holders.values()"
    found = _single_registry_offenders({path: source})
    assert any(item[1] == diagnostic for item in found), ("single-registry declaration probe missed", mutation, found)


def test_cut2b_one_memory_ownership_authority_and_loop_callers():
    from test_carto_finalization_caller_closure import (
        REGISTRY_WRITE_CLOSURE, _registry_writes,
    )

    sources = _production_sources()
    locks = sources["src/workflowy_mcp/mutation_locks.py"]
    assert set(_registry_writes(sources)) == set(REGISTRY_WRITE_CLOSURE), (
        "registry write closure drifted", sorted(set(_registry_writes(sources)) ^ set(REGISTRY_WRITE_CLOSURE))
    )
    assert all(module == "workflowy_mcp.mutation_locks" for module, _name in _registry_writes(sources))
    assert _offload_registry_offenders(sources) == []
    assert _foreign_registry_type_offenders(sources) == []
    assert _hold_constructor_offenders(sources) == []
    assert _region_binding_offenders(sources) == []
    assert _single_registry_offenders(sources) == []
    # Independent source-copy sensitivity for each closed registry rule.
    assert _offload_registry_offenders({
        "src/workflowy_mcp/injected.py":
            "async def leaked():\n"
            "    await asyncio.to_thread(run_carto_cpu, _reserve_carto_hold)\n"
    }), "registry offload probe missed"
    assert _foreign_registry_type_offenders({
        "src/workflowy_mcp/injected.py": "def leaked():\n    return _RegionHold\n"
    }), "foreign registry type probe missed"
    assert _hold_constructor_offenders({
        "src/workflowy_mcp/mutation_locks.py":
            "def _reserve_carto_hold(token):\n"
            "    return _RegionHold(token, None, [region])\n"
    }), "file-only hold probe missed"
    assert _region_binding_offenders({
        "src/workflowy_mcp/injected.py":
            "def leaked(hold, regions):\n    hold.regions = regions\n"
    }), "graph binding probe missed"


def _retired_literal_offenders(sources):
    if os.environ.get("CUT2B_NEUTRALIZE_RETIRED_LITERALS"):
        return []  # Test-only fence sensitivity.
    offenders = []
    for relative, source in sorted(sources.items()):
        if not relative.startswith("src/"):
            continue
        for node in ast.walk(ast.parse(source, filename=relative)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for literal in RETIRED_ARTIFACT_LITERALS:
                    if literal in node.value:
                        offenders.append((relative, node.lineno, literal))
    return offenders


def _os_lock_caller_offenders(sources):
    """Close direct calls and authority-value aliases/callbacks to the guard."""
    from test_cut_2a_fences import _aliases, _resolve, _walk
    if os.environ.get("CUT2B_NEUTRALIZE_OS_CALLERS"):
        return []  # Test-only fence sensitivity.
    allowed = {
        ("workflowy_mcp.server_bootstrap", "acquire_single_writer_guard"),
    }
    offenders = []
    for relative, source in sorted(sources.items()):
        if not relative.startswith("src/"):
            continue
        module = relative[len("src/"):-3].replace("/", ".")
        tree = ast.parse(source, filename=relative)

        def visit(node, scope):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope = (*scope, node.name)
            if isinstance(node, ast.Call):
                leaf = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if leaf in {"acquire_os_lock", "acquire_os_lock_async"} and (
                    module, ".".join(scope)
                ) not in allowed:
                    offenders.append((relative, node.lineno, leaf))
            for child in ast.iter_child_nodes(node):
                visit(child, scope)

        visit(tree, ())
        rows = list(_walk(tree))
        bindings = _aliases(rows)
        for node, owner in rows:
            value = node.func if isinstance(node, ast.Call) else node
            is_reference = (
                isinstance(value, (ast.Name, ast.Attribute)) and isinstance(value.ctx, ast.Load)
            ) or (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                  and value.func.id == "getattr")
            if is_reference:
                leaf = _resolve(value, owner, bindings).rsplit(".", 1)[-1]
                if leaf in {"acquire_os_lock", "acquire_os_lock_async"} and (module, owner) not in allowed:
                    offenders.append((relative, node.lineno, leaf, "OS authority alias/callback escape"))
    return offenders


@pytest.mark.parametrize("source", [
    "from .carto_job_governance import acquire_os_lock as lane\ndef rogue(path):\n    return lane(path)\n",
    "from . import carto_job_governance as gov\ndef rogue(path):\n    lane = gov.acquire_os_lock\n    factory = lane\n    return factory(path)\n",
    "from . import carto_job_governance as gov\ndef rogue(path):\n    callback = getattr(gov, 'acquire_os_lock')\n    return forward(callback, path)\n",
    "from . import carto_job_governance as gov\ndef rogue(path):\n    callbacks = {'lane': gov.acquire_os_lock}\n    return forward(callbacks['lane'], path)\n",
])
def test_cut2b_os_lock_alias_and_callback_reintroduction_rejected(source):
    """The sole kept OS authority cannot escape through aliases/callback values."""
    found = _os_lock_caller_offenders({PACKAGE + "server.py": source})
    assert any("acquire_os_lock" in str(row) for row in found), found


def test_cut2b_persisted_file_writers_are_closed():
    sources = _production_sources()
    assert _retired_literal_offenders(sources) == []
    assert _os_lock_caller_offenders(sources) == []
    # Independent source-copy sensitivity for the two positive tripwires.
    assert _retired_literal_offenders(
        {"src/workflowy_mcp/injected.py": "PATH = '.q3_leases/held.json'\n"}
    ), "retired-artifact literal probe missed"
    assert _os_lock_caller_offenders(
        {"src/workflowy_mcp/injected.py": "def own(path):\n    acquire_os_lock(path, timeout_seconds=0)\n"}
    ), "foreign OS-lock acquisition probe missed"
    for relative, names in RETAINED_DURABLE_WRITERS.items():
        defined = {
            node.name for node in ast.walk(ast.parse(sources[relative]))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert names <= defined, (relative, sorted(names - defined))
    for relative, literals in RETAINED_ARTIFACT_NAMES.items():
        text = sources[relative]
        for literal in literals:
            assert literal in text, (relative, literal)
    from test_cut_2a_fences import _definitions

    governance_paths = _definitions(sources["src/workflowy_mcp/carto_job_governance.py"])["GovernancePaths"][0]
    fields = {
        member.target.id for member in governance_paths.body
        if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name)
    }
    assert fields == {"jobs_root"}, sorted(fields)


# ---------------------------------------------------------------------------
# 2B-5 whole-unit negatives (§6.5/§6.6/§9.9.3): the final, closed subtraction.
# ---------------------------------------------------------------------------
def _whole_unit_negative_offenders(sources=None, *, tests=True):
    if os.environ.get("CUT2B_NEUTRALIZE_FINAL_NEGATIVES"):
        return []  # Test-only fence sensitivity.
    sources = _production_sources() if sources is None else sources
    offenders = []
    offenders += _retired_identity_offenders(sources, CUT2B_RETIRED)
    offenders += _retired_module_offenders(sources)
    offenders += _retired_module_reference_offenders(sources)
    offenders += _native_owner_offenders(sources)
    offenders += _os_lock_caller_offenders(sources)
    offenders += _sole_os_acquirer_offenders(sources)
    offenders += _os_primitive_offenders(sources)
    offenders += _retired_literal_offenders(sources)
    offenders += _retired_inventory_drift()
    if tests:
        executable = _executable_test_sources()
        fixtures = {path: executable[path] for path in CUT2B_RETIRED
                    if path.startswith("tests/") and path in executable}
        offenders += _retired_identity_offenders(fixtures, CUT2B_RETIRED)
        offenders += _retired_actor_offenders(executable, roster=CUT2B_RETIRED)
        offenders += _process_harness_actor_offenders(executable)
    return offenders


def test_cut2b_final_negative_postconditions():
    """§9.9.3: the complete §6/§14 delete closure from the BASE-measured roster.

    The expected set is CUT2B_RETIRED/CUT2B_RETIRED_BASE: file-qualified names
    measured from BASE 7afe1f4 and digest-pinned, never inferred from this tree,
    so an omitted deletion cannot disappear from the assertion.  Definitions,
    imports, aliases and executable references are checked in production; actor
    programs in executable tests exclude both frozen corpora; deleted files must
    be physically absent, not merely unimported.
    """
    assert CUT2B_BASE_REV == "7afe1f4"
    assert len(CUT2B_RETIRED) == 13 and len(RETIRED_MODULE_BASE) == 3
    assert _retired_inventory_drift() == []
    assert set().union(*CUT2B_RETIRED_2B2.values()) <= set().union(*CUT2B_RETIRED.values())
    assert _whole_unit_negative_offenders() == []


def test_cut2b_final_negative_sensitivity():
    """A reintroduced owner identity in a source copy is caught by the closure."""
    injected = {
        "src/workflowy_mcp/carto_job_governance.py":
            "def classify_owner_liveness(identity):\n    return True\n"
    }
    found = _whole_unit_negative_offenders(injected, tests=False)
    if os.environ.get("CUT2B_NEUTRALIZE_FINAL_NEGATIVES"):
        found = []
    assert any("classify_owner_liveness" in str(row) for row in found), found


def test_cut2b_retired_inventory_rejects_omitted_delete_row():
    """Dropping a row from the BASE roster changes the pinned digest."""
    shrunken = copy.deepcopy(CUT2B_RETIRED_BASE)
    shrunken["src/workflowy_mcp/carto_job_governance.py"].pop("classify_legacy_pid_liveness")
    found = _retired_inventory_drift(shrunken)
    if os.environ.get("CUT2B_NEUTRALIZE_RETIRED_INVENTORY"):
        found = []
    assert any(row[1] == "digest" for row in found), found


def test_cut2b_retired_module_tripwire_rejects_source_copy():
    found = _retired_module_offenders(
        {"src/workflowy_mcp/weave_worker.py": "async def main():\n    pass\n"}
    )
    if os.environ.get("CUT2B_NEUTRALIZE_RETIRED_MODULES"):
        found = []
    assert any("weave_worker.py" in str(row) and "retired module" in str(row) for row in found), found


def test_cut2b_native_owner_tripwire_rejects_psutil_and_spawner():
    found = _native_owner_offenders({
        "src/workflowy_mcp/carto_job_governance.py":
            "import psutil\n\n"
            "def classify(pid):\n    return psutil.Process(pid)\n"
            "\n\nasync def launch(spec):\n    subprocess.Popen(spec.argv)\n"
    })
    if os.environ.get("CUT2B_NEUTRALIZE_NATIVE_OWNER"):
        found = []
    assert any("psutil" in str(row) for row in found), found
    assert any("Popen" in str(row) for row in found), found


def test_cut2b_os_primitive_tripwire_rejects_foreign_takeover():
    found = _os_primitive_offenders({
        "src/workflowy_mcp/carto_job_governance.py":
            "def _try_lock_handle(handle):\n"
            "    import msvcrt\n"
            "    return msvcrt.locking(handle, 1, 1)\n",
        "src/workflowy_mcp/server.py":
            "def another_writer(handle):\n"
            "    import fcntl\n"
            "    return fcntl.flock(handle, 1)\n",
    })
    if os.environ.get("CUT2B_NEUTRALIZE_OS_PRIMITIVES"):
        found = []
    assert any("server.py" in str(row) for row in found), found
    sole = _sole_os_acquirer_offenders({
        "src/workflowy_mcp/server.py":
            "def another_writer(path):\n    return acquire_os_lock(path, timeout_seconds=0)\n"
    })
    if os.environ.get("CUT2B_NEUTRALIZE_OS_CALLERS"):
        sole = []
    assert sole, "sole OS acquirer probe missed"


def _admission_scope_offenders(sources, *, inventory=True):
    """§6.2.1: an admission span is synchronous loop work, never IO or a wait."""
    if os.environ.get("CUT2B_NEUTRALIZE_ADMISSION_SCOPE"):
        return []  # Test-only fence sensitivity.
    offenders, sites = [], {}
    forbidden = {"_send_workflowy_write", "run_carto_cpu", "to_thread", "run_in_executor",
                 "sleep", "send", "send_json", "coherence_lock_async", "admission_lock"}
    for relative, source in sorted(sources.items()):
        tree = ast.parse(source, filename=relative)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncWith) or not any(
                isinstance(item.context_expr, ast.Call)
                and ((isinstance(item.context_expr.func, ast.Name) and item.context_expr.func.id == "admission_lock")
                     or (isinstance(item.context_expr.func, ast.Attribute) and item.context_expr.func.attr == "admission_lock"))
                for item in node.items
            ):
                continue
            owner, cursor = "<module>", node
            while cursor in parents:
                cursor = parents[cursor]
                if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = cursor.name
                    break
            key = relative + "::" + owner
            sites[key] = sites.get(key, 0) + 1
            for statement in node.body:
                for child in ast.walk(statement):
                    if isinstance(child, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                        offenders.append(f"await in admission span {key}")
                    if isinstance(child, ast.Call):
                        leaf = getattr(child.func, "id", None) or getattr(child.func, "attr", None)
                        if leaf in forbidden:
                            offenders.append(f"forbidden work in admission span {key}:{leaf}")
    if inventory:
        # §6.2.1/§14.2: exactly the three async fronts take the short admission
        # lock; no other function may join it, and none may be missed.
        expected = {
            "src/workflowy_mcp/carto_job_governance.py::acquire_q3_leases_async": 1,
            "src/workflowy_mcp/carto_job_governance.py::negative_exact_intent_gate": 1,
            "src/workflowy_mcp/carto_job_governance.py::admit_carto_job": 1,
        }
        if sites != expected and not os.environ.get("CUT2B_NEUTRALIZE_ADMISSION_INVENTORY"):
            offenders.append(f"admission lock site inventory: {sites}")
    return offenders


def assert_worker_absent(path):
    """Shared exact absence tripwire for the normalized S06/N06/force inventories."""
    assert not path.exists(), f"retired worker file is present: {path}"


def test_cut2b_worker_absence_tripwire_rejects_source_copy(tmp_path):
    import inspect
    path = tmp_path / "weave_worker.py"
    path.write_text("async def main():\n    pass\n", encoding="utf-8")
    source = inspect.getsource(assert_worker_absent)
    if os.environ.get("CUT2B_NEUTRALIZE_WORKER_TRIPWIRE"):
        source = source.replace("assert not path.exists()", "assert True")
    namespace = {}
    exec(source, namespace)
    with pytest.raises(AssertionError, match="retired worker file"):
        namespace["assert_worker_absent"](path)


def qualified_definitions(source):
    """Resolve definitions with enclosing class/function identity, not bare names."""
    result = {}

    def visit(node, scope=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{scope}.{child.name}" if scope else child.name
                result[name] = child
                visit(child, name)
            else:
                visit(child, scope)

    visit(ast.parse(source))
    return result


def definition_record(source, qualname, *, definitions=None):
    """The same J line/body/AST hashes, optionally using this call's parsed map."""
    node = (qualified_definitions(source) if definitions is None else definitions)[qualname]
    lines = source.splitlines(keepends=True)
    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    return {
        "present": True, "qualname": qualname, "range": [node.lineno, node.end_lineno],
        "body_sha256": digest("".join(lines[node.body[0].lineno - 1:node.end_lineno])),
        "sha256": digest("".join(lines[node.lineno - 1:node.end_lineno])),
        "ast_sha256": digest(ast.dump(node, include_attributes=False)),
    }


def j_sources():
    return {path: (ROOT / path).read_text(encoding="utf-8")
            for path in ALIASES.values() if (ROOT / path).exists()}


def verify_j_pins(sources):
    """Check every independent BASE pin, parsing each source once per call."""
    if os.environ.get("CUT2B_NEUTRALIZE_J_PINS"):
        return  # Test-only fence sensitivity; never product source execution.
    assert len(J_PINS) == 14 + 3
    definitions = {}
    for key, expected in J_PINS.items():
        alias, qualname = key.split(".", 1)
        path = ALIASES[alias]
        if path not in definitions:
            definitions[path] = qualified_definitions(sources[path])
        actual = definition_record(sources[path], qualname, definitions=definitions[path])
        assert actual["body_sha256"] == expected, f"J independent body pin: {key}"
        if key == "cjg.launch_carto_refresh_job":
            assert actual["sha256"] == J_LAUNCHER_FULL, f"J launcher full-definition pin: {key}"


def verify_j_register(sources, manifest):
    """Two absences, frozen history and exact symbol-located J content pins.

    Definition ranges are evidence coordinates, never identity: an unrelated
    insertion must not invalidate unchanged full-source/body/AST hashes.
    Parse each file once per call, keeping source-copy mutants independent.
    """
    if os.environ.get("CUT2B_NEUTRALIZE_J_REGISTER"):
        return  # Test-only fence sensitivity; never a production exception policy.
    assert len(manifest["protected"]) == 17
    assert ALIASES["ww"] not in sources, "retired worker file is present"
    pipeline = qualified_definitions(sources[ALIASES["mgp"]])
    definitions = {ALIASES["mgp"]: pipeline}
    assert "_launch_phase8_refresh_via_authority" not in pipeline, "retired phase8 launcher is present"
    for row in manifest["protected"]:
        key, current = row["key"], row["versions"]["merged"]
        if key in J_DELETED:
            assert current == {"present": False}, key
            continue
        assert current["present"], key
        path = row["file"]
        if path not in definitions:
            definitions[path] = qualified_definitions(sources[path])
        actual = definition_record(sources[path], current["qualname"], definitions=definitions[path])
        for field in ("qualname", "body_sha256", "sha256", "ast_sha256"):
            assert current[field] == actual[field], (key, field)
        if key in J_CHANGED:
            assert current["body_sha256"] != row["versions"]["main"]["body_sha256"], key
        else:
            assert current["body_sha256"] == row["versions"]["main"]["body_sha256"], f"fourth J exception: {key}"
    assert {key for key in manifest["named_dispositions"] if key.startswith("cut2b:")} == {
        "cut2b:ww.main", "cut2b:mgp._launch_phase8_refresh_via_authority",
        "cut2b:mgp.run_markdown_generate_pipeline",
    }, "fourth J disposition"


@pytest.mark.asyncio
async def test_unpopulated_sentinel_moves_without_adoption_import(tmp_path, monkeypatch):
    """The real fresh client refuses capture until local publication; imports stay cache-only."""
    from pydantic import SecretStr
    from workflowy_mcp import carto_job_governance as governance
    from workflowy_mcp.client import api_client_core as core
    from workflowy_mcp.coherence import cache_view
    from workflowy_mcp.coherence.errors import CoherenceStateMissingError
    from workflowy_mcp.coherence.operation_cache import capture_operation_cache
    from workflowy_mcp.models import APIConfiguration

    monkeypatch.setattr(governance, "get_cartographer_jobs_dir", lambda: tmp_path / "jobs")
    monkeypatch.setattr(core, "get_runtime_subdir", lambda *parts: tmp_path.joinpath(*parts))
    if os.environ.get("CUT2B_NEUTRALIZE_SENTINEL_MOVE"):
        monkeypatch.setattr(cache_view, "UNPOPULATED_EPOCH_ID", "f" * 32)
    client = core.WorkFlowyClientCore(APIConfiguration(
        api_key=SecretStr("cut2b-isolated-fixture"), base_url="https://cut2b.invalid/api/v1"))
    view = client._cache_view_publisher.capture()
    assert (view.epoch_number, view.epoch_id, view.adopted_sequence) == (0, "0" * 32, 0)
    assert view.nodes_export == {"nodes": []} and view.account_scope_id == client._safe_account_scope_id()
    assert cache_view.is_unpopulated_view(view)
    with pytest.raises(CoherenceStateMissingError, match="cache refresh"):
        await capture_operation_cache(client, "cut2b-empty")
    published = client._publish_lineage_view(
        {"nodes": [{"id": "cut2b", "name": "populated"}]}, epoch_number=1,
        epoch_id="e" * 32, adopted_sequence=0, snapshot_timestamp=None)
    assert (await capture_operation_cache(client, "cut2b-populated")).view is published
    assert not (ROOT / PACKAGE / "coherence/adoption.py").exists()
    tree = ast.parse((ROOT / PACKAGE / "coherence/cache_view.py").read_text())
    for name in ("unpopulated_cache_view", "is_unpopulated_view"):
        assert len([node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name]) == 1
    assert len([node for node in ast.walk(tree) if isinstance(node, ast.Name) and
                isinstance(node.ctx, ast.Store) and node.id == "UNPOPULATED_EPOCH_ID"]) == 1
    environment = os.environ.copy()
    for key in ("WORKFLOWY_API_KEY", "WORKFLOWY_NEXUS_CONFIG", "WORKFLOWY_BROKER_TOKEN_FILE",
                "WORKFLOWY_MCP_BEARER", "WORKFLOWY_MCP_HTTP_DIAL_HOST"):
        environment.pop(key, None)
    environment["PYTHONPATH"] = str(ROOT / "src")
    script = "import sys; import workflowy_mcp.coherence; assert 'workflowy_mcp.server' not in sys.modules; assert 'workflowy_mcp.coherence.adoption' not in sys.modules"
    child = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=environment,
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr


def test_cut2b_j_unchanged_bodies_and_seams():
    verify_j_pins(j_sources())


def test_cut2b_j_content_pins_ignore_unrelated_line_insertions():
    """Independent pins and the joined register both locate the same definitions."""
    sources = j_sources()
    sources[ALIASES["srv"]] = "# Unrelated insertion above the J definitions.\n\n" + sources[ALIASES["srv"]]
    manifest = json.loads((ROOT / "tests/beacon_robustness_u09_manifest.json").read_text())
    verify_j_pins(sources)
    verify_j_register(sources, manifest)


@pytest.mark.parametrize("key", tuple(J_PINS))
def test_cut2b_j_fence_rejects_untouched_body_and_seam_mutations(key):
    sources = j_sources()
    manifest = json.loads((ROOT / "tests/beacon_robustness_u09_manifest.json").read_text())
    alias, qualname = key.split(".", 1)
    path = ALIASES[alias]
    source = sources[path]
    node = qualified_definitions(source)[qualname]
    lines = source.splitlines(keepends=True)
    indent = " " * node.body[0].col_offset
    lines.insert(node.body[0].lineno - 1, indent + "pass  # cut2b body mutant\n")
    sources[path] = "".join(lines)
    for row in manifest["protected"]:
        if row["file"] == path and row["versions"]["merged"].get("qualname") == qualname:
            row["versions"]["merged"].update(definition_record(sources[path], qualname))
    with pytest.raises(AssertionError, match=key.replace(".", r"\.")):
        verify_j_pins(sources)


@pytest.mark.parametrize("mutant", ["fourth_exception", "fourth_body", "worker", "phase8"])
def test_cut2b_j_register_rejects_extra_exception_and_retired_definition(mutant):
    sources = j_sources()
    manifest = copy.deepcopy(json.loads((ROOT / "tests/beacon_robustness_u09_manifest.json").read_text()))
    if mutant == "worker":
        sources[ALIASES["ww"]] = "async def main():\n    pass\n"
        expected = "retired worker file"
    elif mutant == "phase8":
        sources[ALIASES["mgp"]] += "\nasync def _launch_phase8_refresh_via_authority():\n    pass\n"
        expected = "retired phase8 launcher"
    elif mutant == "fourth_body":
        row = next(row for row in manifest["protected"] if row["key"] == "cjg.launch_carto_refresh_job")
        path, qualified = row["file"], row["versions"]["merged"]["qualname"]
        node = qualified_definitions(sources[path])[qualified]
        lines = sources[path].splitlines(keepends=True)
        lines.insert(node.body[0].lineno - 1, " " * node.body[0].col_offset + "pass\n")
        sources[path] = "".join(lines)
        row["versions"]["merged"].update(definition_record(sources[path], qualified))
        expected = "fourth J exception"
    else:
        manifest["named_dispositions"]["cut2b:srv._handle_refresh_file_node"] = "unauthorized fourth exception"
        expected = "fourth J disposition"
    with pytest.raises(AssertionError, match=expected):
        verify_j_register(sources, manifest)


# ---------------------------------------------------------------------------
# §10.2 independent computation preservation: 17 kept bodies hashed from BASE
# 7afe1f4 (continuation 2), with only the first string-expression docstring
# removed recursively and `ast.dump(include_attributes=False)`; comments and
# line shifts are irrelevant.  These values are not read from the mutable
# manifest or regenerated from the implementing tree.
# ---------------------------------------------------------------------------
KEPT_COMPUTATION_PINS = {
    "coherence/refresh_authority.py::_cut_covers":
        "f5606914cea444c2929a5056aadee403ea20f5bb7fcaf50e9bee3461ec457949",
    "coherence/refresh_authority.py::RefreshOutcome":
        "680ae8f5fcfe9f63c4e8f2ad2ca3f77927be1dced345e70481c8a726bce9dbfd",
    "coherence/transition_effects.py::TransitionEffect":
        "4611fecf53f8564086b14aa28b6c2868dce2736c1f04cd30344cf0bb3e434216",
    "coherence/transition_effects.py::TransitionEffectPolicy":
        "42a82308d8a23e2755bf70783ba433a60a951c8faa4772fc1e20204c0874b2ad",
    "coherence/transition_effects.py::derive_transition_effects":
        "64099ff46198174aa06052f666876329b0cfc8f22f26d050cc406ef073226b2a",
    "coherence/transition_effects.py::compose_transition_effect":
        "8ff1247ea4d51f1999cc45da39903613d0b6dbb88e72fc2b4146e532b30bb27d",
    "coherence/transition_effects.py::merge_offer_with_transition_effects":
        "5b361172c33783ccc82cf92020e3f721e4a14cbf9c11dc2bccfcc7c515035b7a",
    "coherence/operation_cache.py::publish_local_cache_view":
        "11f213dc7d22d7f4edfa10b9f5d58707313f7ec3c9c46c1cbeeac1ddf2d5a19c",
    "coherence/operation_cache.py::capture_operation_cache":
        "57fe59e899919c72a312a479c2375cc6c0d81644fa661579ea203ec415b16a91",
    "client/api_client_core.py::WorkFlowyClientCore._publish_lineage_view":
        "5677c6a8faa3457ad1a9972ca252736ae29cd2427f3a64e4f738c8e8011863d3",
    "client/api_client_core.py::WorkFlowyClientCore._load_nodes_export_cache_from_disk":
        "afd922e95e68f0f1b9a267f2093e4cc8a165d0c26057b5e7553104cd36384860",
    "mutation_lease_governance.py::MutationRegion":
        "7b7060f8559198e8cdcfaf5e7ada3308c1f648f64861d8af260ee0ff877fd8b5",
    "mutation_lease_governance.py::regions_intersect":
        "a3f49dd811e9baca37e02f5eb9b468d0089c7237308ef46ee693589a28e45052",
    "mutation_lease_governance.py::mutation_regions_intersect_carto_claim":
        "ad4715f9008e11ad4cb9d50367fd9c65518835e76a5bf3a66a0a9d185f09edc6",
    "server.py::_dispatch_ws_work":
        "ebe00b58727752470c817315e189f675135967ffecd336ff2e8b811fb5068f22",
    "server.py::_carto_job_regions":
        "1f92e32652a6c0d17149fc3ecfc464b215a9f799419c9122995f018c24eeb1be",
    "server.py::_carto_job_incarnation":
        "3e31f67fcb14d47fb11b8da0a27d9529149541cb92b2ed83329dd808dca837eb",
}
# 2026-10-26 The Rail ([ap:Q7K9VR], merged at 82fc0f3) adds exactly one
# statement to a kept computation: the nested `dispatch()` of
# `_dispatch_ws_work` gains the `resolve_node_titles` branch that spawns
# `_handle_resolve_node_titles`. The BASE pin above is not re-blessed: the hash
# is taken with exactly that statement removed, and the statement itself is
# pinned by the SHA-256 of its `ast.dump(include_attributes=False)`, so a
# changed or missing branch, or any other edit to the dispatcher, still fails.
# 2026-10-27 JR2 (WaveSpec §3.1) adds exactly two more, both removable without
# touching the legacy computation: the protocol-2 poll INGRESS (`if action ==
# 'carto_list_jobs' and 'since' in data:`) and the one-job Details sibling
# (`if action == 'carto_job_detail':`). The deliberate ingress split leaves the
# ORIGINAL `if action == 'carto_list_jobs': _carto_list_jobs_poll(websocket)`
# statement exactly as at BASE, so the historical pin above is unchanged; with
# all three statements removed the docstring-free AST still hashes to it.
KEPT_COMPUTATION_ADDED_STATEMENTS = {
    "server.py::_dispatch_ws_work": (
        "20c8d3ff82fc743498ef327e50d6c32a0f4b09198df2d8bbccb09d5cb735baf9",
        "6615927caf177ae3db82c7e56e69cd2a56bfa70c0e5e537329a873fe4582999c",
        "9a9dc5749e52a42f81201c894b516682605d7be6ca599f331ad83ee7a896ef5c",
    ),
}


def _docstring_free_ast_hash(source, qualname, added=()):
    node = copy.deepcopy(qualified_definitions(source)[qualname])
    remaining = list(added)
    for part in ast.walk(node):
        for field in ("body", "orelse", "finalbody"):
            statements = getattr(part, field, None)
            if not isinstance(statements, list):
                continue
            for statement in list(statements):
                digest = hashlib.sha256(
                    ast.dump(statement, include_attributes=False).encode("utf-8")).hexdigest()
                if digest in remaining:
                    remaining.remove(digest)
                    statements.remove(statement)
    if remaining:
        return "missing added statement: " + ", ".join(remaining)
    for part in ast.walk(node):
        if isinstance(part, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and part.body:
            first = part.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                part.body.pop(0)
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode("utf-8")).hexdigest()


def _kept_computation_sources():
    return {
        path: (ROOT / PACKAGE / path).read_text(encoding="utf-8")
        for path in sorted({key.split("::", 1)[0] for key in KEPT_COMPUTATION_PINS})
    }


def _kept_computation_hash(source, key):
    return _docstring_free_ast_hash(
        source, key.split("::", 1)[1], KEPT_COMPUTATION_ADDED_STATEMENTS.get(key, ()))


def _kept_computation_mismatches(sources):
    return {
        key: _kept_computation_hash(sources[key.split("::", 1)[0]], key)
        for key in KEPT_COMPUTATION_PINS
        if _kept_computation_hash(sources[key.split("::", 1)[0]], key)
        != KEPT_COMPUTATION_PINS[key]
    }


def test_cut2b_kept_computations_match_base():
    sources = _kept_computation_sources()
    assert len(KEPT_COMPUTATION_PINS) == 17
    mismatches = _kept_computation_mismatches(sources)
    if os.environ.get("CUT2B_NEUTRALIZE_COMPUTATION_PINS"):
        mismatches = {}  # Test-only fence sensitivity.
    assert mismatches == {}


@pytest.mark.parametrize("key", tuple(KEPT_COMPUTATION_PINS))
def test_cut2b_kept_computation_fence_rejects_mutants(key):
    path, qualname = key.split("::", 1)
    source = (ROOT / PACKAGE / path).read_text(encoding="utf-8")
    node = qualified_definitions(source)[qualname]
    lines = source.splitlines(keepends=True)
    lines.insert(node.body[0].lineno - 1, " " * node.body[0].col_offset + "pass  # cut2b body mutant\n")
    actual = _kept_computation_hash("".join(lines), key)
    if os.environ.get("CUT2B_NEUTRALIZE_COMPUTATION_PINS"):
        actual = KEPT_COMPUTATION_PINS[key]
    assert actual != KEPT_COMPUTATION_PINS[key], f"computation pin missed {key}"
