"""S04 Remediation Chain 5 (work-order item 1) — the universal typed
barrier refusal survives through CARTO job status.

Prepared CARTO tasks and ``weave_worker`` persisted ``error_kind`` but
``_scan_carto_job_files`` dropped it (and no path persisted the standard
``refusal_extra()`` payload); the in-process bulk-apply runner persisted
only a generic error string.  ``system.job.status(job_id=...)`` therefore
could not machine-read ``coherence_barrier_active`` or the standard
recovery payload for these execution-side refusals.

Chain 5: all three refusal paths (prepared task, detached worker,
in-process bulk runner) persist the machine-readable
``error_kind="coherence_barrier_active"`` code AND the standard
``refusal_extra()`` payload, and ``_scan_carto_job_files`` forwards both
(normalized: verbatim, only when present and well-typed) so the status
view exposes them.  Zero-mutation behavior, exact memory-reservation
finalization, the closed terminal-status vocabulary, and clean job-status
shapes are unchanged.

Required-test mutation note (step 4a): the adversarial tests below were
verified to FAIL against the pre-fix code (production stashed to the
a77e8ea baseline) with their predicted shapes — see the Chain-5 result
anchor for the full evidence table.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from coherence_harness import (
    ACCOUNT_A,
    NOW,
    make_boundary_capable,
    make_coherence_root,
    run_subprocess,
    s02_node,
)

import workflowy_mcp.coherence.operation_cache as _opc
from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp import server
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

REPO = Path(__file__).resolve().parents[1]

_RAISE_BARRIER_DRIVER = r"""
import sys
sys.path.insert(0, sys.argv[3] + "/tests")
from pathlib import Path
from coherence_harness import NOW
from workflowy_mcp.coherence import (
    coherence_paths,
    initialize_manifest_if_absent,
    new_bootstrap_manifest,
    raise_global_dirty_barrier,
)

paths = coherence_paths(sys.argv[2], jobs_root=Path(sys.argv[1]))
initialize_manifest_if_absent(paths, new_bootstrap_manifest(sys.argv[2], now=NOW))
raise_global_dirty_barrier(
    paths,
    reason_code="ambiguous_commit_outcome",
    transaction_id=sys.argv[4],
    detail="raised by a second process",
)
print("barrier raised")
"""

_BULK_BARRIER_DRIVER = r"""
import sys
sys.path.insert(0, sys.argv[3] + "/tests")
from pathlib import Path
from coherence_harness import ACCOUNT_A, make_coherence_root, s04_raise_barrier

paths = make_coherence_root(Path(sys.argv[1]), ACCOUNT_A, bootstrap=False)
s04_raise_barrier(paths, transaction_id=sys.argv[4])
print("barrier raised")
"""


def async_test(function):
    import functools

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


class _WsStub:
    """Recording websocket stub for the WS handlers."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, text: str) -> None:
        self.messages.append(json.loads(text))


def _expected_recovery() -> dict:
    return {"family": "system", "verb": "cache.refresh", "params": {}}


# ---------------------------------------------------------------------------
# Shared prepared-task scaffolding (mirrors the Chain-2 fixture posture)
# ---------------------------------------------------------------------------


def _make_client(tmp_path: Path, monkeypatch):
    from pydantic import SecretStr

    from workflowy_mcp.client.api_client_core import WorkFlowyClientCore
    from workflowy_mcp.models import APIConfiguration

    jobs_root = tmp_path / "cartographer_jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "workflowy_mcp.carto_job_governance.get_cartographer_jobs_dir",
        lambda: jobs_root,
    )

    def _runtime_subdir(*parts):
        if parts == ("nodes_export_cache",):
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            return snapshot_dir
        return tmp_path.joinpath(*parts)

    monkeypatch.setattr(
        "workflowy_mcp.client.api_client_core.get_runtime_subdir", _runtime_subdir
    )
    config = APIConfiguration(
        api_key=SecretStr("test-key-1234"), base_url="https://workflowy.com/api/v1"
    )
    return WorkFlowyClientCore(config), jobs_root, snapshot_dir


def _seed_client(tmp_path: Path, monkeypatch, nodes: list[dict]):
    """A warm-started real client over a real manifest + base carrying the
    fixture nodes (mirrors the S03 prepared-boundary scaffolding)."""

    probe, jobs_root, snapshot_dir = _make_client(tmp_path, monkeypatch)
    paths = probe._coherence_paths()
    account = paths.account_root.name
    manifest = initialize_manifest_if_absent(
        paths, new_bootstrap_manifest(account, now=NOW)
    )
    candidate = write_snapshot_candidate(
        new_snapshot_candidate_path(snapshot_dir),
        {"nodes": [dict(n) for n in nodes]},
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
    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    return client, jobs_root, snapshot_dir, candidate, account, manifest


def _install_launch_mocks(
    monkeypatch,
    *,
    client,
    jobs_root,
    account,
    manifest,
    candidate,
    barrier_tx: str | None,
):
    """Route the REAL server launch seam; the freshness callback is the
    deterministic pause point where a SECOND PROCESS raises the barrier."""

    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_governance_jobs_root", lambda: str(jobs_root))
    from n07_job_harness import install_legacy_task_fixture
    fixture = install_legacy_task_fixture(monkeypatch, jobs=jobs_root, account=account, client=client)
    # The status view under test scans this directory.
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(jobs_root))

    async def _freshness(*, reason: str, source: str):
        if barrier_tx is not None:
            proc = run_subprocess(
                _RAISE_BARRIER_DRIVER,
                str(jobs_root),
                account,
                str(REPO),
                barrier_tx,
                timeout=30,
            )
            assert proc.returncode == 0, proc.stderr
        return {
            "snapshot_path": str(candidate),
            "freshness_disposition": "performed",
            "snapshot_lineage": {
                "account_scope_id": account,
                "epoch_number": manifest.epoch_number,
                "epoch_id": manifest.epoch_id,
                "incorporated_through_seq": 0,
            },
        }

    monkeypatch.setattr(server, "_carto_select_snapshot", _freshness)

    spawned = fixture.calls

    finalize_calls: list[str] = []
    real_finalize = gov.finalize_claim_async

    async def _finalize_spy(claim_id, finalize_jobs_root):
        finalize_calls.append(str(claim_id))
        return await real_finalize(claim_id, finalize_jobs_root)

    monkeypatch.setattr(gov, "finalize_claim_async", _finalize_spy)
    return {"spawned": spawned, "finalize_calls": finalize_calls}


async def _drain_prepared() -> None:
    pending = list(server._carto_inprocess_tasks.values())
    while pending:
        await asyncio.gather(*pending, return_exceptions=True)
        pending = list(server._carto_inprocess_tasks.values())


def _job_record(jobs_root: Path) -> dict:
    job_files = sorted(jobs_root.glob("*_carto-refresh-*.json"))
    assert len(job_files) == 1, f"expected exactly one job file, got {job_files}"
    return json.loads(job_files[0].read_text(encoding="utf-8"))


def _ast_fixture(tmp_path: Path) -> list[dict]:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    py_file = repo_dir / "mod.py"
    py_file.write_text("def foo():\n    pass\n", encoding="utf-8")
    return [
        s02_node("vault", name="Vault"),
        s02_node("file", name="mod.py", parent_id="vault", note=f"Path: {py_file}"),
        s02_node("ast", name="foo", parent_id="file", note=""),
    ]


def _ast_data() -> dict:
    return {
        "action": "refresh_file_node",
        "node_id": "ast",
        "node_note": "",
        "node_name": "foo",
    }


def _f12p4_fixture(tmp_path: Path) -> list[dict]:
    return [
        s02_node("vault", name="Vault"),
        s02_node("mdoc", name="My Doc", parent_id="vault", note="# My Doc\n\nbody text"),
    ]


def _f12p4_data(out_dir: Path) -> dict:
    return {
        "action": "materialize_markdown_file",
        "node_id": "mdoc",
        "node_note": "# My Doc\n\nbody text",
        "node_name": "My Doc",
        "default_markdown_folder": str(out_dir),
    }


def _install_f12p4_spies(monkeypatch, client) -> dict:
    calls: dict[str, list] = {"update_node": [], "patch": [], "dirty": []}

    async def _update_node_spy(**kwargs):
        calls["update_node"].append(kwargs)
        return {"success": True}

    async def _patch_spy(patches):
        calls["patch"].append(patches)
        return {"success": True}

    def _dirty_spy(ids):
        calls["dirty"].append(list(ids))

    monkeypatch.setattr(client, "update_node", _update_node_spy)
    monkeypatch.setattr(client, "patch_nodes_export_snapshot_on_disk", _patch_spy)
    monkeypatch.setattr(client, "_mark_nodes_export_dirty", _dirty_spy)
    return calls


def _assert_typed_status_view(status: dict, *, job_id: str, tx: str) -> None:
    """The universal typed contract machine-readable through job.status."""
    assert status["success"] is True
    assert status["job_id"] == job_id
    assert status["status"] == "failed"
    assert status["error_kind"] == "coherence_barrier_active"
    assert "coherence_barrier_active" in str(status["error"])
    extra = status["error_extra"]
    assert extra["refresh_required"] is True
    assert extra["hard_dirty"] is True
    assert extra["dirty_generation"] == 1
    assert extra["reason_code"] == "ambiguous_commit_outcome"
    assert extra["transaction_id"] == tx
    assert extra["recovery"] == _expected_recovery()


# ---------------------------------------------------------------------------
# Required test (a): prepared F12-on-AST / F12+4 refusals inspected through
# system.job.status.
# ---------------------------------------------------------------------------


@async_test
async def test_prepared_f12_on_ast_refusal_exposed_through_job_status(
    tmp_path, monkeypatch
):
    """Work-order item 1(a) — F12-on-AST: launch clean, a REAL second
    process raises the barrier inside the prepared task, resume; the
    refusal is machine-readable through system.job.status."""
    client, jobs_root, _sd, candidate, account, manifest = _seed_client(
        tmp_path, monkeypatch, _ast_fixture(tmp_path)
    )
    mocks = _install_launch_mocks(
        monkeypatch,
        client=client,
        jobs_root=jobs_root,
        account=account,
        manifest=manifest,
        candidate=candidate,
        barrier_tx="tx-prepared-recheck",
    )
    beacon_calls: list[dict] = []

    async def _beacon_spy(**kwargs):
        beacon_calls.append(kwargs)
        return {"success": True, "operation": "updated_beacon"}

    monkeypatch.setattr(client, "update_beacon_from_node", _beacon_spy, raising=False)
    ws = _WsStub()
    await server._handle_refresh_file_node(_ast_data(), ws)
    await _drain_prepared()

    # Zero preparation side effects; no worker spawn; exact-token finalization.
    assert beacon_calls == []
    assert mocks["spawned"] == []
    job = _job_record(jobs_root)
    assert mocks["finalize_calls"] == [str(job["claim_id"])]

    # The persisted typed payload (both the code AND the recovery payload).
    assert job["status"] == "failed"
    assert job["error_kind"] == "coherence_barrier_active"
    assert job["error_extra"]["transaction_id"] == "tx-prepared-recheck"
    assert job["error_extra"]["reason_code"] == "ambiguous_commit_outcome"
    assert job["error_extra"]["recovery"] == _expected_recovery()

    # Machine-readable through system.job.status(job_id=...).
    status = await server._system_job_status_impl(job_id=str(job["id"]))
    _assert_typed_status_view(status, job_id=str(job["id"]), tx="tx-prepared-recheck")


@async_test
async def test_prepared_f12p4_refusal_exposed_through_job_status(tmp_path, monkeypatch):
    """Work-order item 1(a) — F12+4 materialization: same ordering, same
    typed status contract (no file write, no update_node, no compat patch,
    no worker spawn)."""
    out_dir = tmp_path / "md_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    client, jobs_root, _sd, candidate, account, manifest = _seed_client(
        tmp_path, monkeypatch, _f12p4_fixture(tmp_path)
    )
    mocks = _install_launch_mocks(
        monkeypatch,
        client=client,
        jobs_root=jobs_root,
        account=account,
        manifest=manifest,
        candidate=candidate,
        barrier_tx="tx-prepared-recheck",
    )
    calls = _install_f12p4_spies(monkeypatch, client)
    ws = _WsStub()
    await server._handle_materialize_markdown_file(_f12p4_data(out_dir), ws)
    await _drain_prepared()

    assert list(out_dir.iterdir()) == []
    assert calls == {"update_node": [], "patch": [], "dirty": []}
    assert mocks["spawned"] == []
    job = _job_record(jobs_root)
    assert job["status"] == "failed"
    assert job["error_kind"] == "coherence_barrier_active"
    assert job["error_extra"]["transaction_id"] == "tx-prepared-recheck"
    assert mocks["finalize_calls"] == [str(job["claim_id"])]

    status = await server._system_job_status_impl(job_id=str(job["id"]))
    _assert_typed_status_view(status, job_id=str(job["id"]), tx="tx-prepared-recheck")


# ---------------------------------------------------------------------------
# Required test (c): in-process bulk apply queued clean, barrier raised
# before the runner recheck — zero operation calls and the typed
# status/recovery payload.
# ---------------------------------------------------------------------------


def _install_bulk_apply_scaffolding(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    paths = make_coherence_root(jobs, ACCOUNT_A)

    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(jobs))
    monkeypatch.setattr(server, "_governance_jobs_root", lambda: jobs)
    monkeypatch.setattr(server, "_carto_account_scope", lambda: ACCOUNT_A)
    monkeypatch.setattr(
        server, "_build_bulk_visible_root_from_payload", lambda _payload: {"id": "file"}
    )
    target = tmp_path / "repo" / "a.py"
    target.parent.mkdir(exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    footprint = gov.compute_footprint(
        target, "file", {"file": {"id": "file"}}, kind="file"
    )
    monkeypatch.setattr(
        server, "_carto_footprint_from_selection", lambda **_kwargs: footprint
    )

    async def _no_refresh():
        return None

    monkeypatch.setattr(server, "_refresh_carto_job_tracker_once", _no_refresh)
    from n07_job_harness import install_legacy_task_fixture
    install_legacy_task_fixture(monkeypatch, jobs=jobs, account=ACCOUNT_A)
    monkeypatch.setattr(server, "_collect_bulk_visible_apply_groups", lambda *args, **kwargs: (
        {"file": {"file_node": {"id": "file"}, "nodes": [{"id": "candidate"}]}}, [], 0,
    ))

    bulk_calls: list[tuple] = []

    async def _bulk_spy(*args, **kwargs):
        bulk_calls.append((args, kwargs))

    monkeypatch.setattr(server, "_run_carto_bulk_visible_apply_job", _bulk_spy)
    return {"jobs": jobs, "paths": paths, "bulk_calls": bulk_calls}


@async_test
async def test_bulk_apply_barrier_before_runner_recheck_zero_ops_typed_status(
    tmp_path, monkeypatch
):
    """Work-order item 1(c): queue clean, hold the independently dispatched
    runner, a REAL SUBPROCESS second actor raises the barrier, release —
    zero operation calls, typed status/recovery payload through
    system.job.status, exact-token claim finalization."""
    fx = _install_bulk_apply_scaffolding(tmp_path, monkeypatch)

    release = asyncio.Event()
    real_spawn = _opc.create_isolated_context_task

    def _holding_spawn(coro, *, name=None):
        async def _held():
            await release.wait()
            return await coro

        return real_spawn(_held(), name=name)

    _opc.create_isolated_context_task = _holding_spawn
    try:
        result = await server._start_carto_bulk_visible_apply_job(
            root_uuid="file",
            mode="file",
            visible_tree_payload={},
            selected_snapshot={"snapshot_path": str(tmp_path / "snapshot.json")},
        )
        assert result["success"] is True
        job_id = result["job_id"]
        task = server._carto_inprocess_tasks[job_id]

        # A REAL second actor raises the barrier against the shared
        # coherence root before the runner's internal recheck executes.
        completed = run_subprocess(
            _BULK_BARRIER_DRIVER,
            str(fx["jobs"]),
            "unused",
            str(REPO),
            "tx-bulk-recheck",
            timeout=60.0,
        )
        assert completed.returncode == 0, completed.stderr
        release.set()
        await task
    finally:
        _opc.create_isolated_context_task = real_spawn

    # Zero operation calls (the recheck fired before the bulk operation).
    assert fx["bulk_calls"] == []

    # The persisted typed payload on the job JSON.
    job = json.loads(Path(result["carto_job_file"]).read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert job["error_kind"] == "coherence_barrier_active"
    assert "coherence_barrier_active" in str(job["error"])
    extra = job["error_extra"]
    assert extra["refresh_required"] is True
    assert extra["hard_dirty"] is True
    assert extra["dirty_generation"] == 1
    assert extra["reason_code"] == "journal_publication_failed"
    assert extra["transaction_id"] == "tx-bulk-recheck"
    assert extra["recovery"] == _expected_recovery()

    # Exact-token memory finalization (reservation released; cooldown recorded).
    from workflowy_mcp import mutation_locks as locks
    assert locks._governance_holds(fx["jobs"]) == ()

    # Machine-readable through system.job.status(job_id=...).
    status = await server._system_job_status_impl(job_id=job_id)
    assert status["success"] is True
    assert status["status"] == "failed"
    assert status["error_kind"] == "coherence_barrier_active"
    assert status["error_extra"]["transaction_id"] == "tx-bulk-recheck"
    assert status["error_extra"]["reason_code"] == "journal_publication_failed"
    assert status["error_extra"]["recovery"] == _expected_recovery()


# ---------------------------------------------------------------------------
# Required test (d): clean controls retain the existing shapes.
# ---------------------------------------------------------------------------


def test_scan_forwards_typed_fields_only_when_present(tmp_path, monkeypatch):
    """Scanner normalization: a clean/completed job JSON yields a row with
    NO error_kind/error_extra keys (clean shapes unchanged); a typed
    refusal job JSON yields both, forwarded verbatim."""
    jobs = tmp_path / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    clean_job = {
        "id": "job-clean",
        "type": "CARTO_REFRESH",
        "status": "completed",
        "error": None,
        "created_at": NOW.isoformat() if hasattr(NOW, "isoformat") else None,
    }
    typed_job = {
        "id": "job-typed",
        "type": "CARTO_REFRESH",
        "status": "failed",
        "error": "coherence_barrier_active: refused",
        "error_kind": "coherence_barrier_active",
        "error_extra": {
            "refresh_required": True,
            "hard_dirty": True,
            "dirty_generation": 3,
            "reason_code": "journal_publication_failed",
            "transaction_id": "tx-typed",
            "recovery": _expected_recovery(),
        },
    }
    (jobs / "job-clean.json").write_text(json.dumps(clean_job), encoding="utf-8")
    (jobs / "job-typed.json").write_text(json.dumps(typed_job), encoding="utf-8")

    rows = {row["job_id"]: row for row in server._scan_carto_job_files(str(jobs))}
    assert set(rows) == {"job-clean", "job-typed"}
    # Clean shape: neither key is added.
    assert "error_kind" not in rows["job-clean"]
    assert "error_extra" not in rows["job-clean"]
    # Typed shape: both forwarded verbatim.
    assert rows["job-typed"]["error_kind"] == "coherence_barrier_active"
    assert rows["job-typed"]["error_extra"] == typed_job["error_extra"]

    # ...and through the status view itself.
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(jobs))
    clean_status = asyncio.run(server._system_job_status_impl(job_id="job-clean"))
    assert "error_kind" not in clean_status
    assert "error_extra" not in clean_status
    typed_status = asyncio.run(server._system_job_status_impl(job_id="job-typed"))
    assert typed_status["error_kind"] == "coherence_barrier_active"
    assert typed_status["error_extra"]["transaction_id"] == "tx-typed"


@async_test
async def test_bulk_apply_clean_run_keeps_clean_shapes(tmp_path, monkeypatch):
    """Clean control for the in-process runner: no barrier — the job
    completes through the ordinary path and neither the job JSON nor the
    status view gains the typed keys."""
    fx = _install_bulk_apply_scaffolding(tmp_path, monkeypatch)

    # The clean path runs the (spied) bulk operation to completion.
    async def _clean_bulk(job_file, root_node, mode, **kwargs):
        payload = json.loads(Path(job_file).read_text(encoding="utf-8"))
        payload["status"] = "completed"
        payload["error"] = None
        Path(job_file).write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(server, "_run_carto_bulk_visible_apply_job", _clean_bulk)

    result = await server._start_carto_bulk_visible_apply_job(
        root_uuid="file",
        mode="file",
        visible_tree_payload={},
        selected_snapshot={"snapshot_path": str(tmp_path / "snapshot.json")},
    )
    assert result["success"] is True
    task = server._carto_inprocess_tasks[result["job_id"]]
    await task

    job = json.loads(Path(result["carto_job_file"]).read_text(encoding="utf-8"))
    assert job["status"] == "completed"
    assert "error_kind" not in job
    assert "error_extra" not in job

    status = await server._system_job_status_impl(job_id=result["job_id"])
    assert status["status"] == "completed"
    assert "error_kind" not in status
    assert "error_extra" not in status

    # Exact-token memory finalization on the clean path too.
    from workflowy_mcp import mutation_locks as locks
    assert locks._governance_holds(fx["jobs"]) == ()
