"""Both N05 CARTO directions and observation costs, all files under tmp_path.

No stage2/lease harness, external daemon, credentials or liveness oracle.
"""

from __future__ import annotations

import json
from pathlib import Path
import asyncio
from contextlib import asynccontextmanager

import pytest

from coherence_harness import ACCOUNT_A
from n05_region_harness import graph_cache, node_uuid, region_client, model_chain
from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp import mutation_locks as locks
from workflowy_mcp import mutation_lease_governance as regions
from workflowy_mcp.carto_job_runtime import CartoJobContext, CartoJobControl, carto_job_scope
from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, create_isolated_context_task


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Each test builds the one registry over isolated roots; no leak across."""
    monkeypatch.setattr(locks, "_LOCKS", locks._RegionRegistry())
    if __import__("os").environ.get("CUT2B_NEUTRALIZE_STATUS_RELEASE"):
        actual = locks._governance_holds
        def terminal_releases(root=None):
            holds = actual(root)
            if root is not None:
                job = Path(root) / "candidate.json"
                if job.exists() and json.loads(job.read_text(encoding="utf-8")).get("status") == "completed":
                    return ()
            return holds
        monkeypatch.setattr(locks, "_governance_holds", terminal_releases)
    yield


def _admission(root, claim_root, view):
    return {
        "job_file": root / "candidate.json", "job_payload": {"id": "candidate", "type": "CARTO_REFRESH"},
        "job_type": "CARTO_REFRESH", "dedupe_key": "c" * 64,
        "account_scope_id": ACCOUNT_A,
        "footprint": gov.compute_footprint(None, node_uuid(claim_root), view, kind="file"),
        "launcher": "agent", "parent_job_id": None, "prepared": False,
        "jobs_root": root, "live_mutation_lease_view": view,
    }


def _region(kind, view):
    if kind == "ap_core":
        return regions.build_region_ap_core("ABC123")
    if kind == "special":
        return regions.build_region_child_set(regions.ReservedMutationParent.INBOX, cache_by_id=view)
    return {
        "exact_node": regions.build_region_exact_node,
        "child_set": regions.build_region_child_set,
        "subtree": regions.build_region_subtree,
    }[kind](node_uuid("p"), cache_by_id=view)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exact_node", "child_set", "subtree", "ap_core", "special"])
@pytest.mark.parametrize("claim_root", ["vault", "a", "p", "x", "g"])
async def test_forward_consult_typed_region_matrix(tmp_path, kind, claim_root):
    client = region_client(tmp_path)
    root = client._write_governor.paths.jobs_root
    view = graph_cache()
    expected = (claim_root in model_chain("p") or kind == "subtree" and "p" in model_chain(claim_root)) if kind not in ("ap_core", "special") else False
    async with cache_operation_boundary(client, "forward"):
        async with locks.mutation_region_locks(client=client, operation_label="forward", region_factory=lambda snapshot: (_region(kind, snapshot.nodes_by_id),)):
            if expected:
                try:
                    gov.admit_carto_job_locked(**_admission(root, claim_root, view))
                except gov.AdmissionRefusal as refusal:
                    assert refusal.kind == "overlap_conflict"
                    assert refusal.details["conflict"]["node_id"] == node_uuid("p")
                    assert refusal.details["conflict"]["region"]["kind"] == kind
                else:
                    raise AssertionError("typed held region was ignored by launch")
                assert not (root / "candidate.json").exists()
            else:
                assert gov.admit_carto_job_locked(**_admission(root, claim_root, view))["admitted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exact_node", "child_set", "subtree", "ap_core", "special"])
async def test_reverse_task_hold_matrix(tmp_path, kind, monkeypatch):
    client = region_client(tmp_path)
    monkeypatch.setattr(locks, "_long_lived_retry_after_seconds", lambda: 60.0)
    sent = []
    ready, release = asyncio.Event(), asyncio.Event()
    owner = []
    async def hold():
        async with cache_operation_boundary(client, "carto:reverse-job") as snapshot:
            owner.append(snapshot.operation_id)
            async with _carto_task_hold(client):
                ready.set()
                await release.wait()
    task = create_isolated_context_task(hold())
    try:
        await asyncio.wait_for(ready.wait(), 10)
        async with cache_operation_boundary(client, "reverse"):
            async def enter():
                async with locks.mutation_region_locks(client=client, operation_label="reverse", region_factory=lambda snapshot: (_region(kind, snapshot.nodes_by_id),)):
                    sent.append(True)
            if kind in ("ap_core", "special"):
                await enter()
                assert sent == [True]
            else:
                try:
                    await enter()
                except locks.MutationSerializationConflictError as refusal:
                    assert refusal.reason == "long_lived_holder"
                    assert refusal.holder_id == owner[0]  # opaque operation id, not claim/job id
                    assert refusal.retry_after == 60
                else:
                    raise AssertionError("interactive typed job hold was ignored")
                assert sent == []
                assert locks._LOCKS.waiters == []
    finally:
        release.set()
        await task


@asynccontextmanager
async def _carto_task_hold(client):
    async with locks.mutation_region_locks(
        client=client, operation_label="carto:reverse-job",
        region_factory=lambda s: (regions.build_region_subtree(node_uuid("a"), cache_by_id=s.nodes_by_id),),
        hold_mode="long_lived", expected_duration_seconds=None,
    ):
        yield


@pytest.mark.asyncio
async def test_edit_first_blocks_daemon_claim_between_probe_and_send(tmp_path, monkeypatch):
    from test_mutation_regions_n05_fences import real_region_probe
    from test_mutation_serialization import dispatch
    from workflowy_mcp import server
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    at_send, release = asyncio.Event(), asyncio.Event()
    async def paused(request, held, required):
        at_send.set()
        await release.wait()
    probe.before_send = paused
    async def edit():
        return await dispatch(server.build_node_registry(), "edit", {"node_handle": ids["source"], "name": "Owned through HTTP"})
    task = create_isolated_context_task(edit())
    try:
        await asyncio.wait_for(at_send.wait(), 10)
        root = client._write_governor.paths.jobs_root
        view = client._cache_view_publisher.capture().nodes_by_id
        args = _admission(root, "a", graph_cache())
        args.update(footprint=gov.compute_footprint(None, ids["origin"], view, kind="file"), live_mutation_lease_view=view)
        for attempt in range(2):
            with pytest.raises(gov.AdmissionRefusal) as info:
                gov.admit_carto_job_locked(**args)
            assert info.value.kind == "overlap_conflict"
            assert not (root / "candidate.json").exists()
            assert locks._governance_holds(root) == ()
        release.set()
        assert (await task)["success"] is True and not probe.violations
        assert gov.admit_carto_job_locked(**args)["admitted"]  # only after release
    finally:
        release.set()
        await task
        await client.close()


@pytest.mark.asyncio
async def test_task_grant_precedes_first_effect_and_blocks_next_mutation(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    ready, release = asyncio.Event(), asyncio.Event()
    async def job():
        async with cache_operation_boundary(client, "carto:ordered"):
            async with _carto_task_hold(client):
                assert locks.held_mutation_lock_regions(), "job effect preceded typed grant"
                ready.set()  # first possible effect occurs only after the grant
                await release.wait()
    task = create_isolated_context_task(job())
    try:
        # Join a failed pre-effect assertion rather than hiding it in a wait timeout.
        ready_task = asyncio.create_task(ready.wait())
        await asyncio.wait({task, ready_task}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            ready_task.cancel()
            await asyncio.gather(ready_task, return_exceptions=True)
            await task
        await ready_task
        async with cache_operation_boundary(client, "after-launch"):
            entered = False
            try:
                async with locks.mutation_region_locks(client=client, operation_label="after-launch", region_factory=lambda snapshot: (_region("exact_node", snapshot.nodes_by_id),)):
                    entered = True
            except locks.MutationSerializationConflictError as refusal:
                assert refusal.reason == "long_lived_holder"
            assert not entered, "job effect preceded typed grant"
    finally:
        release.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exact_node", "child_set", "subtree"])
@pytest.mark.parametrize("mutation", ["registry", "predicate"])
async def test_reverse_task_hold_neutralizations(tmp_path, monkeypatch, kind, mutation):
    with monkeypatch.context() as patch:
        if mutation == "registry":
            patch.setattr(locks, "_scan_region_blockers", lambda *args, **kwargs: ())
        else:
            patch.setattr(locks, "regions_intersect", lambda *args, **kwargs: False)
        with pytest.raises(AssertionError, match="interactive typed job hold was ignored"):
            await test_reverse_task_hold_matrix(tmp_path / "missing", kind, patch)
    await test_reverse_task_hold_matrix(tmp_path / "restored", kind, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exact_node", "child_set", "subtree"])
async def test_forward_typed_reader_neutralization(tmp_path, monkeypatch, kind):
    with monkeypatch.context() as patch:
        patch.setattr(locks, "registry_held_regions", lambda: locks.RegistryMutationRegions((), None, True))
        with pytest.raises(AssertionError, match="typed held region was ignored by launch"):
            await test_forward_consult_typed_region_matrix(tmp_path / "missing", kind, "a")
    await test_forward_consult_typed_region_matrix(tmp_path / "restored", kind, "a")


@pytest.mark.asyncio
async def test_grant_before_first_effect_neutralization(tmp_path, monkeypatch):
    import sys
    @asynccontextmanager
    async def missing_grant(client):
        yield
    with monkeypatch.context() as patch:
        patch.setattr(sys.modules[__name__], "_carto_task_hold", missing_grant)
        with pytest.raises(AssertionError, match="job effect preceded typed grant"):
            await test_task_grant_precedes_first_effect_and_blocks_next_mutation(tmp_path / "missing", patch)
    await test_task_grant_precedes_first_effect_and_blocks_next_mutation(tmp_path / "restored", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["child_set", "subtree"])
async def test_forward_w8_checks_all_real_keys_before_positive_overlap(tmp_path, monkeypatch, kind):
    from dataclasses import replace
    client = region_client(tmp_path)
    publisher = client._cache_view_publisher
    async with cache_operation_boundary(client, "all-held-w8"):
        async with locks.mutation_region_locks(client=client, operation_label="all-held-w8",
                region_factory=lambda s: (_region("exact_node", s.nodes_by_id),
                    (regions.build_region_child_set if kind == "child_set" else regions.build_region_subtree)(node_uuid("q"), cache_by_id=s.nodes_by_id))):
            view = publisher.capture()
            current = {key: value for key, value in view.nodes_by_id.items() if key != node_uuid("q")}
            # No holder can be acquired malformed: only the newer proof view
            # loses this held key, after both regions were validly acquired.
            publisher.publish(type(view).build(epoch_number=view.epoch_number, epoch_id=view.epoch_id,
                adopted_sequence=view.adopted_sequence, nodes_export={"nodes": list(current.values())}))
            with pytest.raises(gov.AdmissionRefusal) as info:
                gov.admit_carto_job_locked(**_admission(client._write_governor.paths.jobs_root, "a", graph_cache()))
            conflict = info.value.details["conflict"]
            assert conflict["reason"] == "held_key_not_in_admission_view"
            assert conflict["node_id"] == node_uuid("q") and conflict["region"] is None


@pytest.mark.asyncio
async def test_claim_terminal_status_is_not_release(tmp_path):
    """STATUS/EXIT: presentation never releases; the grant's finally does."""
    client = region_client(tmp_path)
    root = client._write_governor.paths.jobs_root
    view = graph_cache()
    args = _admission(root, "a", view)
    admitted = gov.admit_carto_job_locked(**args)
    claim_id = admitted["claim"]["claim_id"]
    job_file = Path(admitted["job_file"])
    holds = locks._governance_holds(root)
    assert [hold["claim_id"] for hold in holds] == [claim_id]
    assert holds[0]["finalized"] is False

    terminal = json.loads(job_file.read_text(encoding="utf-8"))
    terminal["status"] = "completed"
    job_file.write_text(json.dumps(terminal), encoding="utf-8")
    assert [hold["claim_id"] for hold in locks._governance_holds(root)] == [claim_id], \
        "terminal job status released its live claim"

    context = CartoJobContext(job_file, job_file.parent, None, CartoJobControl(), claim_id=claim_id)
    async with cache_operation_boundary(client, "carto:terminal-probe"):
        with carto_job_scope(context):
            async with locks.mutation_region_locks(
                client=client, operation_label="carto:terminal-probe",
                region_factory=lambda s: (
                    regions.build_region_subtree(node_uuid("a"), cache_by_id=s.nodes_by_id),
                ),
                hold_mode="long_lived", expected_duration_seconds=None,
            ) as grant:
                entries = [hold for hold in locks._LOCKS.holders.values()
                           if hold.governance is not None and hold.governance.claim_id == claim_id]
                assert len(entries) == 1, "a job entry must carry its file metadata and grant"
                assert entries[0].token is grant.token
                assert grant.is_active
                assert locks._governance_holds(root)[0]["finalized"] is False
                assert await gov.finalize_claim_async(
                    claim_id, root, now_epoch=1700000000.0,
                ) is True
                assert locks._governance_holds(root)[0]["finalized"] is True
                assert grant.is_active, "EXIT released before the grant's finally"
        assert locks._governance_holds(root) == (), "grant finally did not release the finalized reservation"

    after = _admission(root, "a", view)
    after.update(
        job_file=root / "after.json",
        job_payload={"id": "after", "type": "CARTO_REFRESH"},
        dedupe_key="d" * 64,
        now_epoch=1700000000.0,
    )
    with pytest.raises(gov.AdmissionRefusal) as info:
        gov.admit_carto_job_locked(**after)
    assert info.value.kind == "cooldown_active"


def test_terminal_and_cost_observer_neutralizations(tmp_path, monkeypatch):
    """A mutant that treats terminal status as release must fail the witness."""
    original = locks._governance_holds
    def terminal_releases(root):
        holds = original(root)
        job_file = Path(root) / "candidate.json"
        try:
            status = json.loads(job_file.read_text(encoding="utf-8")).get("status")
        except Exception:
            status = None
        return () if status == "completed" else holds
    with monkeypatch.context() as patch:
        patch.setattr(locks, "_governance_holds", terminal_releases)
        with pytest.raises(AssertionError, match="terminal job status released its live claim"):
            asyncio.run(test_claim_terminal_status_is_not_release(tmp_path / "missing"))
    asyncio.run(test_claim_terminal_status_is_not_release(tmp_path / "restored"))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exact_node", "child_set", "subtree"])
async def test_forward_continuation_coclaim_never_exempts_typed_holder(tmp_path, monkeypatch, kind):
    """parent_job_id is diagnostic metadata, never a region-owner bypass.

    The current interactive typed holder still refuses the candidate; the
    empty-reader mutant below must fail the same assertion.
    """
    client = region_client(tmp_path)
    root = client._write_governor.paths.jobs_root
    args = {**_admission(root, "a", graph_cache()), "parent_job_id": "parent-job"}
    async with cache_operation_boundary(client, "typed-continuation"):
        async with locks.mutation_region_locks(client=client, operation_label="typed-continuation",
                region_factory=lambda s: (_region(kind, s.nodes_by_id),)):
            try:
                gov.admit_carto_job_locked(**args)
            except gov.AdmissionRefusal as exc:
                assert exc.kind == "overlap_conflict"
                assert exc.details["conflict"]["owner_kind"] == "mutation_lock"
                assert exc.details["conflict"]["region"]["kind"] == kind
            else:
                raise AssertionError("continuation co-claim bypassed the typed holder")
            assert not args["job_file"].exists()
            assert locks._governance_holds(root) == ()
    # The diagnostic parent id survives on a legitimate admission; it confers
    # no ownership and creates no continuation parent.
    admitted = gov.admit_carto_job_locked(**args)
    assert admitted["admitted"] is True
    assert admitted["continuation_parent"] is None
    assert admitted["claim"]["parent_job_id"] == "parent-job"
    assert admitted["job_payload"]["parent_job_id"] == "parent-job"
    assert len(locks._governance_holds(root)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exact_node", "child_set", "subtree"])
async def test_forward_continuation_typed_consult_neutralization(tmp_path, monkeypatch, kind):
    with monkeypatch.context() as patch:
        patch.setattr(locks, "registry_held_regions", lambda: locks.RegistryMutationRegions((), None, True))
        with pytest.raises(AssertionError, match="continuation co-claim bypassed the typed holder"):
            await test_forward_continuation_coclaim_never_exempts_typed_holder(tmp_path / "missing", patch, kind)
    await test_forward_continuation_coclaim_never_exempts_typed_holder(tmp_path / "restored", monkeypatch, kind)


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


def _decision_view(value):
    """Project a decision payload onto the §6.2 contract fields only.

    Retired claim-path/process fields are absent by construction; a stray
    implementation field never freezes into the oracle, and no decision field
    is dropped.
    """
    claim = value.get("claim")
    if isinstance(claim, dict):
        assert set(claim) == set(_CLAIM_FIELDS), set(claim) ^ set(_CLAIM_FIELDS)
        assert not (set(claim) & _RETIRED_CLAIM_FIELDS), set(claim) & _RETIRED_CLAIM_FIELDS
        value = {**value, "claim": {key: claim[key] for key in _CLAIM_FIELDS}}
        value["claim"]["ancestor_chain"] = list(value["claim"]["ancestor_chain"])
    conflict = value.get("conflict")
    if isinstance(conflict, dict):
        fields = (_COOLDOWN_FIELDS if value.get("error_kind") == "cooldown_active"
                  else _CLAIM_FIELDS if "record_kind" in conflict else None)
        if fields is not None:
            assert set(conflict) == set(fields), set(conflict) ^ set(fields)
            value = {**value, "conflict": {key: conflict[key] for key in fields}}
            value["conflict"]["ancestor_chain"] = list(value["conflict"]["ancestor_chain"])
    return value


def _forward_no_holder_observations(root, *, authority=gov):
    """Complete decision/shape observations; only generated IDs/time are masked.

    The independent oracle is written from §6.2's admission contract: the
    registry reservation is diagnostic metadata, claim_path is inert None, and
    no claim file or owner/PID proof exists to freeze.
    """
    from coherence_harness import make_coherence_root
    from workflowy_mcp.coherence.schema import read_manifest, manifest_with_updates, write_manifest
    def mask(value, branch_root):
        if isinstance(value, dict):
            generated = {"claim_id", "epoch_id", "basis_epoch_id", "current_epoch_id", "pid",
                         "launcher_pid", "pid_create_time", "pid_start_ticks", "pid_boot_id",
                         "launched_at", "spawn_started_at", "last_terminal_at"}
            return {key: "<generated>" if key in generated and item is not None else mask(item, branch_root)
                    for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [mask(item, branch_root) for item in value]
        return value.replace(str(branch_root), "<root>") if isinstance(value, str) else value
    observed = {}
    for branch in ("success", "dedupe", "active", "cooldown", "stale", "epoch"):
        branch_root = root / branch
        paths = make_coherence_root(branch_root)
        jobs = branch_root / "cartographer_jobs"
        args = {**_admission(jobs, "a", graph_cache()), "now_epoch": 1700000000.}
        if branch in {"dedupe", "active", "cooldown"}:
            existing = {
                **_admission(jobs, "a", graph_cache()), "now_epoch": 1700000000.,
                "job_file": jobs / "existing.json",
                "job_payload": {"id": "existing", "type": "CARTO_REFRESH"},
                "dedupe_key": (args["dedupe_key"] if branch == "dedupe" else "b" * 64),
            }
            admitted = authority.admit_carto_job_locked(**existing)
            assert admitted["admitted"] is True
            if branch == "cooldown":
                ids = [hold["claim_id"] for hold in locks._governance_holds(jobs)]
                assert len(ids) == 1
                assert asyncio.run(
                    authority.finalize_claim_async(ids[0], jobs, now_epoch=1700000000.)
                ) is True
                assert locks._governance_holds(jobs) == ()
        elif branch in {"stale", "epoch"}:
            manifest = read_manifest(paths)
            changed = manifest_with_updates(manifest, journal_head_seq=1) if branch == "stale" else manifest_with_updates(
                manifest, epoch_number=1, epoch_id="2" * 32, previous_epoch_id=manifest.epoch_id)
            write_manifest(paths, changed, expected_epoch_id=manifest.epoch_id, expected_head_seq=0)
            args.update(footprint_basis_sequence=0, footprint_basis_epoch_id=manifest.epoch_id)
        try:
            value = authority.admit_carto_job_locked(**args)
        except authority.AdmissionRefusal as exc:
            value = exc.to_result()
        observed[branch] = mask(_decision_view(value), branch_root)
    return observed


# Independent §6.2 contract literal (not a snapshot of implementation output).
# Small shared literal shapes compress the decision matrix without dropping a
# single decision field. UUIDs are fixture literals.
_BASE_FOOTPRINT = {"kind": "file", "path": None,
    "root_uuid": "4f908749-15f9-5602-ba99-d6b39f4a7c56",
    "ancestor_chain": ["4f908749-15f9-5602-ba99-d6b39f4a7c56", "57b938c4-0cb7-51fc-93ca-fcf8503c6382"]}
_BASE_CLAIM = {**_BASE_FOOTPRINT, "record_kind": "carto", "jobs_root": "<root>/cartographer_jobs", "claim_id": "<generated>", "job_id": "candidate",
    "job_type": "CARTO_REFRESH", "mode": "", "dedupe_key": "c" * 64, "account_scope_id": "a" * 32,
    "launcher": "agent", "parent_job_id": None,
    "job_file": "<root>/cartographer_jobs/candidate.json", "log_file": None,
    "freshness_disposition": None, "finalized": False}
_BASE_ACTIVE_CONFLICT = {**_BASE_CLAIM, "job_id": "existing", "dedupe_key": "b" * 64,
    "job_file": "<root>/cartographer_jobs/existing.json"}
_BASE_STALE = {"success": False, "error_kind": "head_basis_stale", "retry_after": 0.,
    "error": "The manifest journal head advanced past the CARTO footprint's basis view; adopt the head and re-derive the footprint before retrying (S05 A1c head-currency).",
    "conflict": {"current_epoch_id": "<generated>", "current_head": 1, "basis_epoch_id": "<generated>", "basis_sequence": 0}}
_NO_HOLDER_BASE = {
    "success": {"success": True, "admitted": True, "deduplicated": False, "claim": _BASE_CLAIM,
        "claim_path": None,
        "job_file": "<root>/cartographer_jobs/candidate.json", "continuation_parent": None,
        "job_payload": {"id": "candidate", "type": "CARTO_REFRESH", "claim_id": "<generated>", "dedupe_key": "c" * 64,
            "account_scope_id": "a" * 32, "footprint": _BASE_FOOTPRINT, "launcher": "agent", "parent_job_id": None,
            "status": "preparing", "handoff_state": "preparing", "execution_model": "daemon_task",
            "execution_owner": "launcher", "detached": False, "pid": "<generated>"}},
    "dedupe": {"success": True, "job_id": "existing", "pid": "<generated>", "detached": False, "deduplicated": True,
        "root_uuid": "4f908749-15f9-5602-ba99-d6b39f4a7c56", "job_file": "<root>/cartographer_jobs/existing.json",
        "log_file": None, "freshness_disposition": None,
        "note": "Existing active governed job reused; no second preparation ran."},
    # N07 CF12 / [ap:BKP1Q5]: only this prescribed additive reply changed.
    "active": {"success": False, "error_kind": "overlap_conflict", "retry_after": 60.0,
        "error": "Active owner 'existing' overlaps '4f908749-15f9-5602-ba99-d6b39f4a7c56'. Retry after about 60s; check system.job.status because the job may still be running.",
        "conflict": _BASE_ACTIVE_CONFLICT},
    "cooldown": {"success": False, "error_kind": "cooldown_active", "retry_after": 60.,
        "error": "The target overlaps a recently-terminal CARTO region; retry after cooldown. For several node beacon edits, use F12+2 Bulk Apply.",
        "conflict": {**_BASE_FOOTPRINT, "account_scope_id": "a" * 32, "dedupe_key": "b" * 64,
                     "last_terminal_at": "<generated>", "job_id": "existing"}},
    "stale": _BASE_STALE,
    "epoch": {**_BASE_STALE, "conflict": {**_BASE_STALE["conflict"], "current_head": 0}},
}


def test_forward_consult_no_holder_compatibility(tmp_path):
    assert not locks.registry_held_keys()
    assert _forward_no_holder_observations(tmp_path) == _NO_HOLDER_BASE


def test_forward_no_holder_decision_field_snapshot_sensitivity(tmp_path, monkeypatch):
    """A compatibility control deliberately passes BASE; stray decisions don't."""
    actual = gov.admit_carto_job_locked
    with monkeypatch.context() as patch:
        def changed(**kwargs):
            result = actual(**kwargs)
            return {**result, "deduplicated": True} if result.get("admitted") else result
        patch.setattr(gov, "admit_carto_job_locked", changed)
        with pytest.raises(AssertionError):
            test_forward_consult_no_holder_compatibility(tmp_path / "changed")
    test_forward_consult_no_holder_compatibility(tmp_path / "restored")
