"""CUT-1: live held-key admission and fixed BASE compatibility witnesses."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_cut_2a_publication import _publication_records_cleanup
from mutation_lease_harness import (
    ACCOUNT_A, append_tx, async_test, cache_of, effects_create,
    make_lease_env, node,
)

from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp import mutation_locks as locks
from n05_region_harness import node_uuid
from workflowy_mcp.coherence.cache_view import CacheView
from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
from workflowy_mcp.coherence.admission import (
    barrier_refusal_extra, require_coherence_mutation_admission,
)
from workflowy_mcp.coherence.barrier import (
    hard_stop_state, raise_global_dirty_barrier, read_coherence_boundary_status,
)
from workflowy_mcp.coherence.errors import CoherenceBarrierActiveError
from workflowy_mcp.coherence.read_advisory import (
    apply_coherence_advisory_to_text, apply_coherence_read_advisory,
)

_CACHE = cache_of(
    ("vault", None), ("repo", "vault"), ("file", "repo"),
    ("f1", "file"), ("g", "vault"), ("p", "g"),
)


def _u(label):
    if label == "a_missing":
        return "00000000-0000-0000-0000-000000000001"
    if label == "z_missing":
        return "ffffffff-ffff-ffff-ffff-ffffffffffff"
    return node_uuid(label)


_REGION_CACHE = cache_of(
    (_u("vault"), None), (_u("repo"), _u("vault")), (_u("file"), _u("repo")),
    (_u("f1"), _u("file")), (_u("g"), _u("vault")), (_u("p"), _u("g")),
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Every test uses an isolated registry; admissions may outlive a case."""
    monkeypatch.setattr(locks, "_LOCKS", locks._RegionRegistry())
    yield


def _region_env(root, *, extra=()):
    env = make_lease_env(root, ACCOUNT_A, nodes=[*_REGION_CACHE.values(), *extra])
    env.client._write_governor = SimpleNamespace(paths=SimpleNamespace(jobs_root=env.jobs_root, account_scope_id=env.account))
    return env


@asynccontextmanager
async def _hold(env, *keys):
    from workflowy_mcp.mutation_locks import mutation_node_locks
    async with cache_operation_boundary(env.client, "held-consult-fixture"):
        async with mutation_node_locks(*keys, client=env.client):
            yield


def _admit_kwargs(env, root_uuid="g", **overrides):
    footprint = gov.compute_footprint(None, root_uuid, _REGION_CACHE if root_uuid in _REGION_CACHE else _CACHE, kind="file")
    payload = {"id": "job-candidate", "type": "CARTO_REFRESH"}
    kwargs = {
        "job_file": env.jobs_root / "job-candidate.json",
        "job_payload": payload,
        "job_type": "CARTO_REFRESH",
        "dedupe_key": "b" * 64,
        "account_scope_id": env.account,
        "footprint": footprint,
        "launcher": "agent",
        "parent_job_id": None,
        "prepared": False,
        "jobs_root": env.jobs_root,
    }
    kwargs.update(overrides)
    return kwargs


_CLAIM_FIELDS = (
    "record_kind", "jobs_root", "account_scope_id", "kind", "path", "root_uuid", "ancestor_chain",
    "claim_id", "job_id", "job_file", "job_type", "mode", "dedupe_key", "launcher",
    "parent_job_id", "log_file", "freshness_disposition", "finalized",
)
_RETIRED_CLAIM_FIELDS = frozenset({
    "pid", "launcher_pid", "pid_create_time", "pid_start_ticks", "pid_boot_id",
    "handoff_state", "spawn_started_at", "execution_owner", "launched_at",
})
_COOLDOWN_FIELDS = (
    "account_scope_id", "kind", "path", "root_uuid", "ancestor_chain",
    "dedupe_key", "job_id", "last_terminal_at",
)


def _claim_view(claim):
    """The exact §6.2.2 admission metadata, never an implementation snapshot."""
    assert set(claim) == set(_CLAIM_FIELDS), set(claim) ^ set(_CLAIM_FIELDS)
    assert not (set(claim) & _RETIRED_CLAIM_FIELDS), set(claim) & _RETIRED_CLAIM_FIELDS
    projected = {key: claim[key] for key in _CLAIM_FIELDS}
    projected["ancestor_chain"] = list(projected["ancestor_chain"])
    return projected


def _cooldown_view(record):
    assert set(record) == set(_COOLDOWN_FIELDS), set(record) ^ set(_COOLDOWN_FIELDS)
    projected = {key: record[key] for key in _COOLDOWN_FIELDS}
    projected["ancestor_chain"] = list(projected["ancestor_chain"])
    return projected


def _mask_incidental(value, env):
    """Mask generated identities/time and the isolated root, never wire semantics."""
    if isinstance(value, dict):
        return {
            key: (
                "<generated>" if item is not None and key in {
                    "claim_id", "epoch_id", "current_epoch_id", "basis_epoch_id",
                    "pid", "launcher_pid", "pid_create_time", "pid_start_ticks",
                    "pid_boot_id", "launched_at", "spawn_started_at", "last_terminal_at",
                } else _mask_incidental(item, env)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_mask_incidental(item, env) for item in value]
    if isinstance(value, str):
        return value.replace(str(env.jobs_root.parent), "<root>")
    return value


async def _boundary_observations(root):
    env = make_lease_env(root, ACCOUNT_A)
    result = {}
    for dirty in (False, True):
        if dirty:
            raise_global_dirty_barrier(
                env.paths, reason_code="ambiguous_commit_outcome", transaction_id="tx-1"
            )
        status = await hard_stop_state(env.paths)
        captured = read_coherence_boundary_status(env.paths)
        retained = asdict(status)
        retained.pop("hard_dirty_equivalent", None)
        retained.pop("blocking_artifacts", None)
        assert captured == status
        observations = {
            "status": retained,
            "text": apply_coherence_advisory_to_text("payload text", status=status),
            "structured": apply_coherence_read_advisory({"preview_markdown": "# Doc"}, status=status),
        }
        try:
            await require_coherence_mutation_admission(env.paths, operation="test.mutate")
        except CoherenceBarrierActiveError as exc:
            observations["refusal"] = {"message": str(exc), "extra": barrier_refusal_extra(status)}
        else:
            observations["refusal"] = None
        result["dirty" if dirty else "clean"] = _mask_incidental(observations, env)
    return result


def _no_key_observations(root):
    result = {}
    for branch in ("success", "dedupe", "active", "cooldown", "stale"):
        env = make_lease_env(root / branch, ACCOUNT_A, nodes=_CACHE.values())
        kwargs = _admit_kwargs(env, root_uuid="file", now_epoch=1700000000.0)
        if branch in {"dedupe", "active"}:
            existing = _admit_kwargs(
                env, root_uuid="file", now_epoch=1700000000.0,
                job_file=env.jobs_root / "job-x.json",
                job_payload={"id": "job-x", "type": "CARTO_REFRESH"},
                dedupe_key=("b" if branch == "dedupe" else "c") * 64,
            )
            assert gov.admit_carto_job_locked(**existing)["admitted"] is True
        elif branch == "cooldown":
            existing = _admit_kwargs(
                env, root_uuid="file", now_epoch=1700000000.0,
                job_file=env.jobs_root / "job-x.json",
                job_payload={"id": "job-x", "type": "CARTO_REFRESH"},
                dedupe_key="c" * 64,
            )
            admitted = gov.admit_carto_job_locked(**existing)
            assert asyncio.run(
                gov.finalize_claim_async(
                    admitted["claim"]["claim_id"],
                    env.jobs_root,
                    now_epoch=1700000000.0,
                )
            ) is True
            assert locks._governance_holds(env.jobs_root) == ()
        elif branch == "stale":
            append_tx(env, "1" * 32, effects_create(node("T1")))
            kwargs.update(footprint_basis_sequence=0, footprint_basis_epoch_id=env.manifest.epoch_id)
        try:
            value = gov.admit_carto_job_locked(**kwargs)
        except gov.AdmissionRefusal as exc:
            value = exc.to_result()
        if isinstance(value.get("claim"), dict):
            value["claim"] = _claim_view(value["claim"])
        conflict = value.get("conflict")
        if isinstance(conflict, dict):
            if value.get("error_kind") == "cooldown_active":
                value["conflict"] = _cooldown_view(conflict)
            elif "record_kind" in conflict:
                value["conflict"] = _claim_view(conflict)
        result[branch] = _mask_incidental(value, env)
    return result


# Frozen from real BASE 1c0d1d5c before production edits. Only generated
# identities/timestamps/root prefixes were masked; see _mask_incidental.
_BASE_FOOTPRINT = {"kind": "file", "path": None, "root_uuid": "file", "ancestor_chain": ["file", "repo", "vault"]}
_BASE_CLAIM = {
    "record_kind": "carto", "jobs_root": "<root>/cartographer_jobs", "account_scope_id": "a" * 32, **_BASE_FOOTPRINT,
    "claim_id": "<generated>", "job_id": "job-candidate",
    "job_file": "<root>/cartographer_jobs/job-candidate.json",
    "job_type": "CARTO_REFRESH", "mode": "", "dedupe_key": "b" * 64,
    "launcher": "agent", "parent_job_id": None, "log_file": None,
    "freshness_disposition": None, "finalized": False,
}
_BASE_ACTIVE_CONFLICT = {
    **_BASE_CLAIM, "job_id": "job-x", "dedupe_key": "c" * 64,
    "job_file": "<root>/cartographer_jobs/job-x.json",
}
_BASE_SUCCESS = {
    "success": True, "admitted": True, "deduplicated": False, "claim": _BASE_CLAIM,
    "claim_path": None,
    "job_file": "<root>/cartographer_jobs/job-candidate.json", "continuation_parent": None,
    "job_payload": {
        "id": "job-candidate", "type": "CARTO_REFRESH", "claim_id": "<generated>",
        "dedupe_key": "b" * 64, "account_scope_id": "a" * 32, "footprint": _BASE_FOOTPRINT,
        "launcher": "agent", "parent_job_id": None, "status": "preparing", "handoff_state": "preparing",
        "execution_model": "daemon_task", "execution_owner": "launcher", "detached": False, "pid": "<generated>",
    },
}
_BASE_ADMISSION = {
    "success": _BASE_SUCCESS,
    "active": {
        # N07 CF12 / [ap:BKP1Q5]: other branches and full equality stay frozen.
        "success": False, "error_kind": "overlap_conflict", "retry_after": 60.0,
        "error": "Active owner 'job-x' overlaps 'file'. Retry after about 60s; check system.job.status because the job may still be running.",
        "conflict": _BASE_ACTIVE_CONFLICT,
    },
    "cooldown": {
        "success": False, "error_kind": "cooldown_active", "retry_after": 60.0,
        "error": "The target overlaps a recently-terminal CARTO region; retry after cooldown. For several node beacon edits, use F12+2 Bulk Apply.",
        "conflict": {**_BASE_FOOTPRINT, "account_scope_id": "a" * 32, "dedupe_key": "c" * 64,
                     "job_id": "job-x", "last_terminal_at": "<generated>"},
    },
    "dedupe": {
        "success": True, "job_id": "job-x", "pid": "<generated>", "detached": False,
        "deduplicated": True, "root_uuid": "file",
        "job_file": "<root>/cartographer_jobs/job-x.json", "log_file": None,
        "freshness_disposition": None, "note": "Existing active governed job reused; no second preparation ran.",
    },
    "stale": {
        "success": False, "error_kind": "head_basis_stale", "retry_after": 0.0,
        "error": "The manifest journal head advanced past the CARTO footprint's basis view; adopt the head and re-derive the footprint before retrying (S05 A1c head-currency).",
        "conflict": {"basis_epoch_id": "<generated>", "basis_sequence": 0,
                     "current_epoch_id": "<generated>", "current_head": 1},
    },
}
_BASE_WARNING = "⚠️ COHERENCE FAILURE — advisory only; NOT part of the returned content. This cache-backed result may be stale; Workflowy mutations are blocked until a full system.cache.refresh succeeds."
_BASE_STATUS = {
    "epoch_id": "<generated>", "journal_head_seq": 0, "hard_dirty": False,
    "hard_generation": 0, "hard_reason_code": None, "hard_transaction_id": None,
    "external_dirty": False, "external_generation": 0, "external_mutation_kind": None,
}
_BASE_BOUNDARY = {
    "clean": {"status": _BASE_STATUS, "text": "payload text", "structured": {"preview_markdown": "# Doc"}, "refusal": None},
    "dirty": {
        "status": {**_BASE_STATUS, "hard_dirty": True, "hard_generation": 1,
                   "hard_reason_code": "ambiguous_commit_outcome", "hard_transaction_id": "tx-1"},
        "text": _BASE_WARNING + "\n\npayload text",
        "structured": {
            "preview_markdown": _BASE_WARNING + "\n\n# Doc",
            "coherence_advisory": {"status": "hard_dirty", "refresh_required": True, "dirty_generation": 1,
                                   "message": _BASE_WARNING, "reason_code": "ambiguous_commit_outcome", "transaction_id": "tx-1"},
        },
        "refusal": {
            "message": "test.mutate: refused before any side effect — the global coherence dirty barrier is active (generation 1, reason ambiguous_commit_outcome); run a full system.cache.refresh to recover [recovery=refresh]",
            "extra": {"refresh_required": True, "hard_dirty": True, "dirty_generation": 1,
                      "recovery": {"family": "system", "verb": "cache.refresh", "params": {}},
                      "reason_code": "ambiguous_commit_outcome", "transaction_id": "tx-1"},
        },
    },
}


def test_t4_no_keys_matches_fixed_base(tmp_path):
    assert _no_key_observations(tmp_path) == _BASE_ADMISSION


@async_test
async def test_clean_machine_is_byte_identical(tmp_path):
    assert await _boundary_observations(tmp_path) == _BASE_BOUNDARY


@async_test
async def test_boundary_status_retired_fields_are_absent(tmp_path):
    """N3 deletion fence, separate from the BASE-passing wire control."""
    status = await hard_stop_state(make_lease_env(tmp_path / "absence", ACCOUNT_A).paths)
    assert not hasattr(status, "hard_dirty_equivalent")
    assert not hasattr(status, "blocking_artifacts")


async def _assert_holder_refuses(root, key="file", candidate="file"):
    from workflowy_mcp.mutation_locks import mutation_node_locks

    env = _region_env(root)
    key, candidate = _u(key), _u(candidate)
    async with _hold(env, key):
        with pytest.raises(gov.AdmissionRefusal) as info:
            gov.admit_carto_job_locked(**_admit_kwargs(env, candidate, live_mutation_lease_view=_REGION_CACHE))
        assert info.value.to_result() == {
            "success": False, "error_kind": "overlap_conflict",
            "error": f"Active mutation lock '{key[:8]}' overlaps CARTO candidate '{candidate}'; retry shortly (refuse, never queue).",
            "retry_after": 1.0,
            "conflict": {"owner_kind": "mutation_lock", "node_id": key,
                         "region": {"kind": "exact_node", "node_uuid": key,
                                     "ancestor_chain": [key, _u("repo"), _u("vault")] if key == _u("file") else [_u("f1"), _u("file"), _u("repo"), _u("vault")]}},
        }
        assert not (env.jobs_root / "job-candidate.json").exists()
        assert locks._governance_holds(env.jobs_root) == ()


@async_test
async def test_t1_same_task_root_holder_refuses(tmp_path, monkeypatch):
    real_hold = _hold
    @asynccontextmanager
    async def populated_hold(env, *keys):
        assert env.client._load_nodes_export_cache_from_disk()  # explicit isolated fixture warm start
        async with real_hold(env, *keys):
            yield
    monkeypatch.setitem(globals(), "_hold", populated_hold)
    await _assert_holder_refuses(tmp_path)


@async_test
async def test_t2_descendant_holder_refuses(tmp_path, monkeypatch):
    real_hold = _hold
    @asynccontextmanager
    async def populated_hold(env, *keys):
        assert env.client._load_nodes_export_cache_from_disk()  # explicit isolated fixture warm start
        async with real_hold(env, *keys):
            yield
    monkeypatch.setitem(globals(), "_hold", populated_hold)
    await _assert_holder_refuses(tmp_path, "f1")


@async_test
async def test_t3_unrelated_holder_admits(tmp_path):
    from workflowy_mcp.mutation_locks import mutation_node_locks

    env = _region_env(tmp_path)
    assert env.client._load_nodes_export_cache_from_disk()  # explicit isolated fixture warm start
    async with _hold(env, _u("p")):
        result = gov.admit_carto_job_locked(**_admit_kwargs(env, _u("file"), live_mutation_lease_view=_REGION_CACHE))
    assert result["success"] is True
    assert result["claim_path"] is None
    holds = locks._governance_holds(env.jobs_root)
    assert len(holds) == 1
    assert holds[0]["record_kind"] == "carto"
    assert holds[0]["root_uuid"] == _u("file")


@async_test
async def test_t5_no_view_with_holder_refuses(tmp_path):
    from workflowy_mcp.mutation_locks import mutation_node_locks

    env = _region_env(tmp_path)
    assert env.client._load_nodes_export_cache_from_disk()  # explicit isolated fixture warm start
    async with _hold(env, _u("file")):
        with pytest.raises(gov.AdmissionRefusal) as info:
            gov.admit_carto_job_locked(**_admit_kwargs(env))
    assert info.value.to_result() == {
        "success": False, "error_kind": "governance_state_unclassifiable",
        "error": "Held mutation-lock keys exist but no current adopted view was supplied for chain rehydration; refusing to admit on unproved topology. Retry with a current admission view.",
    }
    assert not (env.jobs_root / "job-candidate.json").exists()
    assert locks._governance_holds(env.jobs_root) == ()


def test_empty_registry_without_view_admits(tmp_path):
    """T5's empty/None preservation control has its own BASE answer."""
    env = make_lease_env(tmp_path, ACCOUNT_A, nodes=_CACHE.values())
    assert gov.admit_carto_job_locked(**_admit_kwargs(env))["success"] is True


@pytest.mark.parametrize("bad", ["absent", "parent", "cycle", "sorted"])
@async_test
async def test_t6_unplaceable_keys_build_before_overlap_scan(tmp_path, bad):
    from workflowy_mcp.mutation_locks import mutation_node_locks

    # Acquire from a valid view, then publish the W8 fault. The typed plane
    # correctly refuses to acquire a key which was already unplaceable.
    env = _region_env(tmp_path, extra=[{"id": _u(k), "parent_id": _u("vault")} for k in ("a_missing", "z_missing")])
    assert env.client._load_nodes_export_cache_from_disk()  # explicit isolated fixture warm start
    view = dict(_REGION_CACHE)
    if bad == "parent":
        view[_u("z_missing")] = {"id": _u("z_missing"), "parent_id": _u("absent")}
    elif bad == "cycle":
        view[_u("z_missing")] = {"id": _u("z_missing"), "parent_id": _u("z_missing")}
    keys = [_u("file"), _u("z_missing")]
    expected_key = _u("z_missing")
    if bad == "sorted":
        keys += [_u("a_missing")]
        expected_key = _u("a_missing")
    async with _hold(env, *keys):
        old = env.publisher.capture()
        env.publisher.publish(CacheView.build(epoch_number=old.epoch_number, epoch_id=old.epoch_id, adopted_sequence=old.adopted_sequence, nodes_export={"nodes": list(view.values())}, account_scope_id=env.account))
        with pytest.raises(gov.AdmissionRefusal) as info:
            gov.admit_carto_job_locked(**_admit_kwargs(env, _u("file"), live_mutation_lease_view=view))
    assert info.value.to_result() == {
        "success": False, "error_kind": "overlap_conflict", "retry_after": 1.0,
        "error": f"Held mutation lock '{expected_key[:8]}' cannot be placed in the current admission view for CARTO candidate '{_u('file')}'; retry shortly (refuse, never queue).",
        "conflict": {"owner_kind": "mutation_lock", "node_id": expected_key, "region": None,
                     "reason": "held_key_not_in_admission_view"},
    }
    assert not (env.jobs_root / "job-candidate.json").exists()
    assert locks._governance_holds(env.jobs_root) == ()


@async_test
async def test_t7_continuation_parent_does_not_exempt_holder(tmp_path):
    from workflowy_mcp.mutation_locks import mutation_node_locks

    env = _region_env(tmp_path)
    assert env.client._load_nodes_export_cache_from_disk()  # explicit isolated fixture warm start
    # parent_job_id is diagnostic metadata, not an ownership exemption; no
    # phase-8 co-claim fixture and no persisted claim exists.
    async with _hold(env, _u("file")):
        with pytest.raises(gov.AdmissionRefusal) as info:
            gov.admit_carto_job_locked(**_admit_kwargs(
                env, _u("file"), parent_job_id="parent-job", live_mutation_lease_view=_REGION_CACHE,
            ))
    assert info.value.details["conflict"]["owner_kind"] == "mutation_lock"
    assert info.value.details["retry_after"] == 1.0
    assert not (env.jobs_root / "job-candidate.json").exists()
    assert locks._governance_holds(env.jobs_root) == ()


async def _assert_two_task_refusal_then_retry(root):
    from workflowy_mcp.mutation_locks import mutation_node_locks

    env = _region_env(root)
    held, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with _hold(env, _u("f1")):
            held.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await held.wait()
    try:
        with pytest.raises(gov.AdmissionRefusal) as info:
            await gov.admit_carto_job(**_admit_kwargs(env, _u("repo"), live_mutation_lease_view=_REGION_CACHE))
        assert info.value.kind == "overlap_conflict"
        assert info.value.details["retry_after"] == 1.0
        assert info.value.details["conflict"] == {
            "owner_kind": "mutation_lock", "node_id": _u("f1"),
            "region": {"kind": "exact_node", "node_uuid": _u("f1"), "ancestor_chain": [_u("f1"), _u("file"), _u("repo"), _u("vault")]},
        }
        assert not (env.jobs_root / "job-candidate.json").exists()
        assert locks._governance_holds(env.jobs_root) == ()
    finally:
        release.set()
        await task
    assert (await gov.admit_carto_job(**_admit_kwargs(env, _u("repo"), live_mutation_lease_view=_REGION_CACHE)))["success"] is True
    assert len(locks._governance_holds(env.jobs_root)) == 1


@async_test
async def test_t8_two_tasks_release_then_retry(tmp_path, monkeypatch):
    real_hold = _hold
    @asynccontextmanager
    async def populated_hold(env, *keys):
        assert env.client._load_nodes_export_cache_from_disk()  # explicit isolated fixture warm start
        async with real_hold(env, *keys):
            yield
    monkeypatch.setitem(globals(), "_hold", populated_hold)
    await _assert_two_task_refusal_then_retry(tmp_path)


@async_test
async def test_t9_empty_read_neutralization_rejects_t1_and_t8(tmp_path, monkeypatch):
    from workflowy_mcp import mutation_locks

    real_hold = _hold
    @asynccontextmanager
    async def populated_hold(env, *keys):
        assert env.client._load_nodes_export_cache_from_disk()  # explicit isolated fixture warm start
        async with real_hold(env, *keys):
            yield
    monkeypatch.setitem(globals(), "_hold", populated_hold)
    for label, witness in (("t1", _assert_holder_refuses), ("t8", _assert_two_task_refusal_then_retry)):
        with monkeypatch.context() as patch:
            patch.setattr(mutation_locks, "registry_held_regions", lambda: mutation_locks.RegistryMutationRegions((), None, True))
            with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
                await witness(tmp_path / (label + "-neutralized"))
        await witness(tmp_path / (label + "-restored"))


@async_test
async def test_t10_base_shaped_files_and_inert_residue(tmp_path):
    from coherence_harness import s04_write_base_snapshot
    from workflowy_mcp.coherence import read_manifest, run_full_refresh
    from workflowy_mcp.coherence.schema import CoherenceManifest

    env = make_lease_env(tmp_path, ACCOUNT_A, nodes=_CACHE.values())
    # BASE-shaped raw legacy input; the retired key is never emitted again.
    manifest_payload = json.loads(env.paths.manifest_path.read_text())
    legacy_base = s04_write_base_snapshot(
        env.paths, env.snapshot_dir, list(_CACHE.values()), epoch_number=737, epoch_id="7" * 32,
    )
    manifest_payload.update(
        epoch_number=737, epoch_id="7" * 32, previous_epoch_id="6" * 32,
        base_snapshot_path=str(legacy_base), coherence_mode="transition",
    )
    manifest_payload["covered_barrier_required_lease_ids"] = []
    env.paths.manifest_path.write_text(json.dumps(manifest_payload))
    assert CoherenceManifest.from_dict(manifest_payload) == read_manifest(env.paths)
    assert "covered_barrier_required_lease_ids" not in CoherenceManifest.from_dict(manifest_payload).to_dict()

    async def fetch():
        return {"nodes": list(_CACHE.values())}

    outcome = await run_full_refresh(paths=env.paths, fetch=fetch, settle_seconds=0, snapshot_dir=env.snapshot_dir)
    assert outcome.status == "published"
    assert "covered_barrier_required_lease_ids" not in json.loads(env.paths.manifest_path.read_text())
    assert json.loads(env.paths.manifest_path.read_text())["schema_version"] == 1


@async_test
async def test_t10_nonempty_parser_and_compat_carry_are_preserved(tmp_path):
    """Nonempty legacy receipt is discarded; compat writes omit it."""
    from workflowy_mcp.coherence import read_manifest, run_compat_patch
    from workflowy_mcp.coherence.schema import CoherenceManifest

    env = make_lease_env(tmp_path, ACCOUNT_A, nodes=_CACHE.values())
    old = read_manifest(env.paths)
    payload = old.to_dict()
    payload["covered_barrier_required_lease_ids"] = ["1" * 32]
    env.paths.manifest_path.write_text(json.dumps(payload))
    assert "covered_barrier_required_lease_ids" not in CoherenceManifest.from_dict(payload).to_dict()
    await run_compat_patch(paths=env.paths, patches=[{"id": "file", "name": "renamed"}], snapshot_dir=env.snapshot_dir)
    assert "covered_barrier_required_lease_ids" not in json.loads(env.paths.manifest_path.read_text())


@async_test
async def test_p7_full_refresh_carries_nonempty_inert_field(tmp_path):
    """Historical name: full refresh omits arbitrary obsolete input."""
    from workflowy_mcp.coherence import read_manifest, run_full_refresh

    env = make_lease_env(tmp_path, ACCOUNT_A, nodes=_CACHE.values())
    old = read_manifest(env.paths)
    payload = old.to_dict()
    payload["covered_barrier_required_lease_ids"] = ["1" * 32]
    env.paths.manifest_path.write_text(json.dumps(payload))

    async def fetch():
        return {"nodes": list(_CACHE.values())}

    await run_full_refresh(paths=env.paths, fetch=fetch, settle_seconds=0, snapshot_dir=env.snapshot_dir)
    assert "covered_barrier_required_lease_ids" not in json.loads(env.paths.manifest_path.read_text())


@async_test
async def test_carto_head_gate_refuses_stale_basis(tmp_path):
    env = make_lease_env(tmp_path, ACCOUNT_A, nodes=_CACHE.values())
    append_tx(env, "1" * 32, effects_create(node("T1")))
    with pytest.raises(gov.AdmissionRefusal) as exc_info:
        gov.admit_carto_job_locked(**_admit_kwargs(
            env, root_uuid="g", footprint_basis_sequence=0,
            footprint_basis_epoch_id=env.manifest.epoch_id, live_mutation_lease_view=_CACHE,
        ))
    assert exc_info.value.kind == "head_basis_stale"
    assert exc_info.value.details["conflict"]["current_head"] == 1


@async_test
async def test_carto_head_gate_epoch_change_is_stale(tmp_path):
    env = make_lease_env(tmp_path, ACCOUNT_A, nodes=_CACHE.values())
    from workflowy_mcp.coherence.schema import manifest_with_updates, new_epoch_id, write_manifest

    write_manifest(env.paths, manifest_with_updates(
        env.manifest, epoch_number=1, epoch_id=new_epoch_id(), previous_epoch_id=env.manifest.epoch_id,
    ), expected_epoch_id=env.manifest.epoch_id, expected_head_seq=0)
    with pytest.raises(gov.AdmissionRefusal) as exc_info:
        gov.admit_carto_job_locked(**_admit_kwargs(
            env, root_uuid="g", footprint_basis_sequence=0,
            footprint_basis_epoch_id=env.manifest.epoch_id, live_mutation_lease_view=_CACHE,
        ))
    assert exc_info.value.kind == "head_basis_stale"


@async_test
async def test_adopt_fn_revalidates_and_admits(tmp_path):
    """Stale basis → adopt → re-derive the footprint from the fresh view."""
    env = make_lease_env(tmp_path, ACCOUNT_A, nodes=_CACHE.values())
    append_tx(env, "2" * 32, effects_create(node("T1")))

    async def adopt_fn():
        assert env.client._load_nodes_export_cache_from_disk()
        view = env.publisher.capture()
        return {"view": dict(view.nodes_by_id), "basis_sequence": view.adopted_sequence,
                "basis_epoch_id": view.epoch_id}

    admission = await gov.admit_carto_job(**_admit_kwargs(
        env, root_uuid="g", footprint_basis_sequence=0, footprint_basis_epoch_id=env.manifest.epoch_id,
        live_mutation_lease_view=_CACHE, adopt_fn=adopt_fn,
    ))
    assert admission["success"] is True
    assert admission["claim"]["root_uuid"] == "g"


@async_test
async def test_adopt_fn_second_stale_surfaces_refusal(tmp_path):
    env = make_lease_env(tmp_path, ACCOUNT_A, nodes=_CACHE.values())
    append_tx(env, "3" * 32, effects_create(node("T1")))

    async def adopt_fn():
        return {"view": dict(_CACHE), "basis_sequence": 0, "basis_epoch_id": env.manifest.epoch_id}

    with pytest.raises(gov.AdmissionRefusal) as exc_info:
        await gov.admit_carto_job(**_admit_kwargs(
            env, root_uuid="g", footprint_basis_sequence=0, footprint_basis_epoch_id=env.manifest.epoch_id,
            live_mutation_lease_view=_CACHE, adopt_fn=adopt_fn,
        ))
    assert exc_info.value.kind == "head_basis_stale"


@async_test
async def test_head_gate_inert_without_basis_or_current(tmp_path):
    env = make_lease_env(tmp_path, ACCOUNT_A, nodes=_CACHE.values())
    append_tx(env, "4" * 32, effects_create(node("T1")))
    admission = gov.admit_carto_job_locked(**_admit_kwargs(env, root_uuid="g"))
    assert admission["success"] is True
    admission2 = gov.admit_carto_job_locked(**_admit_kwargs(
        env, root_uuid="f1", dedupe_key="a" * 64,
        job_payload={"id": "job-candidate-2", "type": "CARTO_REFRESH"},
        job_file=env.jobs_root / "job-candidate-2.json", footprint_basis_sequence=1,
        footprint_basis_epoch_id=env.manifest.epoch_id, live_mutation_lease_view=_CACHE,
    ))
    assert admission2["success"] is True


def test_t11_retired_plane_and_consult_fence_sensitivity():
    from test_mutation_leases_s05_fences import (
        _held_consult_offenders, _retired_offenders_in, _source_pairs,
    )

    for shape in (
        "class MutationLeaseSet: pass",
        "from workflowy_mcp.mutation_lease_governance import acquire_mutation_lease_set_async as acquire\nacquire()",
        "from workflowy_mcp import mutation_lease_governance as substrate\nsubstrate.mark_mutation_sent(None)",
        "def bad():\n    mark_mutation_verified(None)",
    ):
        assert _retired_offenders_in([("workflowy_mcp/rogue.py", shape)])
    actor = 'code = "from workflowy_mcp.mutation_lease_governance import mark_mutation_sent as send\\nsend(None)"\nlaunch_actor(code)'
    assert _retired_offenders_in([("tests/rogue.py", actor)], tests=True)
    multiplied_actor = '''account = "a" * 32
common = f"ACCOUNT = {account!r}\\n"
code = common + "from workflowy_mcp.mutation_lease_governance import mark_mutation_sent as send\\nsend(None)"
launch_actor(code)
'''
    assert _retired_offenders_in([("tests/rogue.py", multiplied_actor)], tests=True)
    popen_actor = '''code = "from workflowy_mcp.mutation_lease_governance import mark_mutation_sent as send\\nsend(None)"
subprocess.Popen([sys.executable, "-c", code, str(root)])
'''
    assert _retired_offenders_in([("tests/rogue.py", popen_actor)], tests=True)
    assert not _retired_offenders_in([("tests/fence.py", 'prohibited = "mark_mutation_sent(None)"')], tests=True)
    assert _retired_offenders_in([("workflowy_mcp/rogue.py", "_HEX_32_RE = None")])
    assert not _retired_offenders_in([("workflowy_mcp/carto_job_governance.py", "_HEX_32_RE = None")])
    lock_def = ("workflowy_mcp/mutation_locks.py", "def registry_held_keys(): return frozenset()\ndef registry_held_regions(): return ()")
    prefix = "from .mutation_locks import registry_held_regions\n"
    valid = prefix + "def admit_carto_job_locked():\n    held = registry_held_regions()\n    return held\n"
    assert _held_consult_offenders([lock_def, ("workflowy_mcp/carto_job_governance.py", valid)]) == []
    for shape in (
        prefix + "def admit_carto_job_locked(): return None",
        valid + "\ndef elsewhere(): return registry_held_regions()",
        valid.replace("admit_carto_job_locked", "elsewhere"),
        prefix + "async def admit_carto_job_locked(): return await asyncio.to_thread(registry_held_regions)",
        prefix + "def admit_carto_job_locked(): return asyncio.to_thread(lambda: registry_held_regions())",
    ):
        assert _held_consult_offenders([lock_def, ("workflowy_mcp/carto_job_governance.py", shape)])
    # JZO30P / N07 CF37: the second READ is only the named synchronous typed
    # drain observer, registered once by lifespan; never a second admission gate.
    observer_source = "from .mutation_locks import registry_held_regions\n" + '''
def _node_locks_drain_busy() -> bool:
    return bool(registry_held_regions().regions)

async def lifespan(app):
    controller.register_drain_probe("node_locks", _node_locks_drain_busy)
'''
    registered = [lock_def, ("workflowy_mcp/carto_job_governance.py", valid),
                  ("workflowy_mcp/server.py", observer_source)]
    assert _held_consult_offenders(registered) == []
    extra_callers = (
        "def elsewhere(): return registry_held_keys()",
        "from .mutation_locks import registry_held_keys as read_held\ndef elsewhere(): return read_held()",
        "from . import mutation_locks as locks\ndef elsewhere(): return locks.registry_held_keys()",
        'from . import mutation_locks as locks\nread_held = getattr(locks, "registry_held_keys")\ndef elsewhere(): return read_held()',
        'from . import mutation_locks as locks\nlookup = getattr\nread_held = lookup(locks, "registry_held_keys")\ndef elsewhere(): return read_held()',
        'import importlib\nlocks = importlib.import_module("workflowy_mcp.mutation_locks")\ndef elsewhere(): return getattr(locks, "registry_held_keys")()',
        'from . import mutation_locks as locks\ndef elsewhere(field): return getattr(locks, field)()',
        'def elsewhere(): return getattr(__import__("workflowy_mcp.mutation_locks", fromlist=["registry_held_keys"]), "registry_held_keys")()',
        "def elsewhere(): return asyncio.to_thread(registry_held_keys)",
        "async def elsewhere(): return await registry_held_keys()",
        "def decide_admission(): return _node_locks_drain_busy()",
        "from .server import _node_locks_drain_busy as busy\ndef decide_admission(): return busy()",
    )
    for extra in extra_callers:
        assert _held_consult_offenders([*registered[:2], ("workflowy_mcp/server.py", observer_source + "\n" + extra)])
        typed = extra.replace("registry_held_keys", "registry_held_regions")
        assert _held_consult_offenders([*registered[:2], ("workflowy_mcp/server.py", observer_source + "\n" + typed)])
    for changed in (
        observer_source.replace('return bool(registry_held_regions().regions)', 'return False'),
        observer_source.replace('return bool(registry_held_regions().regions)', 'region = mutation_node_locks("node")\n    return bool(registry_held_regions().regions)'),
        observer_source.replace('def _node_locks_drain_busy()', 'async def _node_locks_drain_busy()').replace('registry_held_regions().regions)', 'await registry_held_regions().regions)'),
        observer_source + '\ndef _node_locks_drain_busy():\n    return bool(registry_held_regions().regions)\n',
        observer_source.replace('registry_held_regions', 'registry_held_keys').replace('().regions', '()'),
        observer_source.replace('controller.register_drain_probe("node_locks", _node_locks_drain_busy)', 'pass'),
        observer_source.replace('controller.register_drain_probe("node_locks", _node_locks_drain_busy)', 'controller.register_drain_probe("node_locks", _node_locks_drain_busy)\n    controller.register_drain_probe("node_locks", _node_locks_drain_busy)'),
    ):
        assert _held_consult_offenders([*registered[:2], ("workflowy_mcp/server.py", changed)])
    second_admission = valid.replace("    return held", "    second = registry_held_regions()\n    return held")
    assert _held_consult_offenders([lock_def, ("workflowy_mcp/carto_job_governance.py", second_admission), registered[2]])
    assert _retired_offenders_in(_source_pairs()) == []
    assert _held_consult_offenders(_source_pairs()) == []
