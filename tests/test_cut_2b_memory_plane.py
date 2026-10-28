"""CUT-2B §9.5 in-process memory-plane and short-lock witnesses.

Every row drives the real N05/N07 plane: one ``_LOCKS.holders`` entry from the
admission reservation, through the same-entry typed grant, to exact release;
real Q3 acquisitions; the real cooldown history; and the real daemon runner.
The only test instruments are seam wrappers that park real work, an isolated
root, and the test-only ``CUT2B_NEUTRALIZE_*`` counterfactuals.  No production
flag, second ownership table, fake admission, process control or killed task
is introduced here.

W12 all-nonterminal startup marking and the startup directory guard belong to
2B-5 and are deliberately not duplicated in this module.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from types import MappingProxyType

import pytest

from n07_job_harness import (  # noqa: F401 - fixtures are registered by import
    TEST_WAIT_SECONDS,
    launch_real_job,
    real_job_environment,
    reject_raw_job_cancel,
    scrubbed_runtime,
    wait_job_gate,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "workflowy_mcp"
ACCOUNT = "a" * 32
WAIT = TEST_WAIT_SECONDS

#: Names that must never run or suspend inside one short admission span.
_FORBIDDEN_UNDER_SHORT_LOCK = frozenset({
    "run_carto_cpu", "to_thread", "run_in_executor", "sleep", "send",
    "send_json", "send_workflowy_write", "post", "get", "request",
})


def _enabled(name: str) -> bool:
    return bool(os.environ.get(name))


@pytest.fixture
def isolated_registry(monkeypatch):
    """One fresh registry for a unit-level witness (isolated root state only)."""
    from workflowy_mcp import mutation_locks as locks

    monkeypatch.setattr(locks, "_LOCKS", locks._RegionRegistry())
    return locks


@pytest.fixture
def isolated_server_memory(monkeypatch):
    """The server's loop state is process-global; a unit witness owns empty sets."""
    from workflowy_mcp import server

    for name, value in (
        ("_carto_inprocess_tasks", {}),
        ("_carto_job_controls", {}),
        ("_carto_operation_jobs", {}),
        ("_carto_final_obligations", {}),
        ("_carto_job_states", {}),
        ("_inprocess_restart_stops", {}),
        ("_carto_startup_failed_jobs", MappingProxyType({})),
        ("_carto_active_cache_jobs", set()),
        ("_carto_pending_terminal_cache_jobs", set()),
    ):
        monkeypatch.setattr(server, name, value)
    return server


def _file_target(tmp_path: Path, name: str, root_uuid: str, *, parent: str | None = None) -> dict:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# memory-plane fixture\n", encoding="utf-8")
    chain = [root_uuid] if parent is None else [parent, root_uuid]
    return {"kind": "file", "path": str(path), "root_uuid": root_uuid, "ancestor_chain": chain}


def _target_from_projection(projection) -> dict:
    """The registry projection is immutable; a target needs its validated list."""
    return {
        "kind": projection["kind"],
        "path": projection["path"],
        "root_uuid": projection["root_uuid"],
        "ancestor_chain": list(projection.get("ancestor_chain") or ()),
    }


def _carto_holds(registry, jobs: Path) -> tuple:
    return tuple(
        hold for hold in registry._governance_holds(jobs)
        if hold.get("record_kind") == "carto"
    )


def _job_json(jobs: Path, job_id: str) -> Path:
    matches = [path for path in Path(jobs).glob("*.json") if path.stem.endswith(job_id)]
    assert len(matches) == 1, (job_id, matches)
    return matches[0]


def _job_payload(jobs: Path, job_id: str) -> dict:
    return json.loads(_job_json(jobs, job_id).read_text(encoding="utf-8"))


async def _launch_new_job(env, family: str):
    """Launch one real job and return (ack, its exact job id)."""
    before = set(env.tasks)
    ack = await launch_real_job(env, family)
    job_id = ack.get("job_id")
    if job_id is None:
        created = set(env.tasks) - before
        assert len(created) == 1, (family, ack, created)
        job_id = created.pop()
    assert job_id in env.tasks, (family, ack, set(env.tasks))
    return ack, job_id


async def _join(env, job_id: str) -> dict:
    await asyncio.wait_for(env.tasks[job_id], WAIT)
    return env.controls[job_id].terminal_result or {}


def _install_footprint_release(monkeypatch, gov, locks):
    """Probe: release resolves by footprint only, ignoring the acquisition label."""

    async def release_by_footprint(leases, *, jobs_root=None):
        for receipt in leases:
            for token, hold in tuple(locks._LOCKS.holders.items()):
                metadata = hold.governance
                if (
                    metadata is not None
                    and metadata.record_kind == "q3"
                    and metadata.footprint.get("path") == receipt.get("path")
                    and metadata.footprint.get("root_uuid") == receipt.get("root_uuid")
                ):
                    locks._release_region_hold(token, None)
        return []

    monkeypatch.setattr(gov, "release_q3_leases_async", release_by_footprint)


# --------------------------------------------------------------------------- #
# Row 1 - one reservation entry becomes the one running grant
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reservation_and_running_grant_are_the_same_registry_entry(
    real_job_environment, monkeypatch, request
):
    from workflowy_mcp import carto_job_governance as gov
    from workflowy_mcp import mutation_locks as locks
    from workflowy_mcp.mutation_lease_governance import MutationRegionError

    env = real_job_environment
    server, jobs = env.server, env.jobs

    if _enabled("CUT2B_NEUTRALIZE_ADMISSION_GAP"):
        # Probe: admission publishes the job but omits the pre-grant file
        # reservation, so the admission-to-B0 gap owns nothing.
        real_admit = gov.admit_carto_job

        async def omit_reservation(**kwargs):
            result = await real_admit(**kwargs)
            claim_id = str(result["claim"]["claim_id"])
            for token, hold in tuple(locks._LOCKS.holders.items()):
                metadata = hold.governance
                if (metadata is not None and metadata.record_kind == "carto"
                        and metadata.claim_id == claim_id):
                    del locks._LOCKS.holders[token]
            return result

        monkeypatch.setattr(gov, "admit_carto_job", omit_reservation)

    before_b0, release_b0 = asyncio.Event(), asyncio.Event()
    before_grant, release_grant = asyncio.Event(), asyncio.Event()
    during_body, release_body = asyncio.Event(), asyncio.Event()
    armed = {"body": True}
    duplicates = []
    def release_test_gates():
        release_b0.set()
        release_grant.set()
        release_body.set()
        for duplicate in duplicates:
            if locks._LOCKS.holders.get(duplicate.token) is duplicate:
                del locks._LOCKS.holders[duplicate.token]
    request.addfinalizer(release_test_gates)

    selection = server._carto_select_snapshot
    selection_calls = {"count": 0}

    async def gated_selection(**kwargs):
        selection_calls["count"] += 1
        before_b0.set()
        await release_b0.wait()
        return await selection(**kwargs)

    monkeypatch.setattr(server, "_carto_select_snapshot", gated_selection)

    door = server.mutation_region_locks
    grants = []

    @asynccontextmanager
    async def gated_door(*args, **kwargs):
        before_grant.set()
        await release_grant.wait()
        async with door(*args, **kwargs) as grant:
            grants.append(grant)
            yield grant

    @asynccontextmanager
    async def duplicated_door(*args, **kwargs):
        # Probe: install a separate "running" ownership entry beside the
        # reservation instead of binding the same one.
        before_grant.set()
        await release_grant.wait()
        async with door(*args, **kwargs) as grant:
            grants.append(grant)
            reservation = grant._hold
            if reservation is not None:
                duplicate = locks._RegionHold(
                    object(), reservation.state, reservation.regions, reservation.client,
                    reservation.hold_mode, reservation.expected_release_at,
                )
                duplicate.governance = reservation.governance
                locks._LOCKS.holders[duplicate.token] = duplicate
                duplicates.append(duplicate)
            yield grant

    monkeypatch.setattr(
        server, "mutation_region_locks",
        duplicated_door if _enabled("CUT2B_NEUTRALIZE_SECOND_OWNERSHIP_TABLE") else gated_door,
    )

    real_body = env.client.refresh_file_node_beacons

    async def gated_body(*args, **kwargs):
        if armed["body"]:
            during_body.set()
            await release_body.wait()
        return await real_body(*args, **kwargs)

    monkeypatch.setattr(env.client, "refresh_file_node_beacons", gated_body)
    real_regions = server._carto_job_regions

    async def assert_reservation_blocks_everything(reservation) -> None:
        before = set(env.tasks)
        reply = await launch_real_job(env, "file")
        assert reply.get("deduplicated") is True, reply
        assert set(env.tasks) == before
        with pytest.raises(gov.GovernanceConflictError):
            await gov.acquire_q3_leases_async(
                [_target_from_projection(reservation)],
                account_scope_id=reservation["account_scope_id"], jobs_root=jobs,
            )
        with pytest.raises(gov.AdmissionRefusal) as contended:
            await gov.admit_carto_job(
                job_file=jobs / "contending-overlap.json",
                job_payload={"id": "contending-overlap"},
                job_type="CARTO_REFRESH",
                dedupe_key="d" * 64,
                account_scope_id=reservation["account_scope_id"],
                footprint=_target_from_projection(reservation),
                launcher="system",
                parent_job_id=None,
                prepared=False,
                jobs_root=jobs,
            )
        assert contended.value.kind == "overlap_conflict"
        # CARTO-blocked admission keeps its existing positive retry advice.
        advice = contended.value.details.get("retry_after")
        assert advice is not None and advice > 0
        assert len(_carto_holds(locks, jobs)) == 1

    # Pause 1: admitted + scheduled, waiting for its B0 base snapshot.
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    initial = gov.compute_footprint(
        str(env.repo / "source.py"), env.ids["file"],
        {node["id"]: node for node in env.initial_nodes}, kind="file")
    async def prepared(payload, selected):
        return {"prepared": True}
    async with cache_operation_boundary(env.client, "cut2b:prepared-reservation"):
        ack = await server._start_carto_refresh_job(
            env.ids["file"], "file", preparation=prepared, initial_footprint=initial)
    job_id = ack["job_id"]
    assert ack["success"] is True and ack["deduplicated"] is False
    holds = _carto_holds(locks, jobs)
    assert len(holds) == 1, holds
    await wait_job_gate(env, before_b0, job_id=job_id)
    reservation = holds[0]
    assert reservation["finalized"] is False
    claim_id, job_file = reservation["claim_id"], reservation["job_file"]
    entry = locks._require_carto_hold(job_file, claim_id)
    assert entry.state is None and entry.regions == ()
    assert locks._LOCKS.holders[entry.token] is entry
    await assert_reservation_blocks_everything(reservation)

    # Pause 2: after B0, before the typed grant.  Still one unbound entry.
    release_b0.set()
    await wait_job_gate(env, before_grant, job_id=job_id)
    assert len(_carto_holds(locks, jobs)) == 1
    assert locks._require_carto_hold(job_file, claim_id) is entry
    assert entry.state is None and entry.regions == ()
    await assert_reservation_blocks_everything(reservation)

    # Pause 3: during execution - the same object/token now owns the grant.
    release_grant.set()
    await wait_job_gate(env, during_body, job_id=job_id)
    assert len(grants) == 1
    bound = locks._require_carto_hold(job_file, claim_id)
    assert bound is entry
    assert grants[0].token is entry.token
    assert entry.state is not None and entry.regions
    assert len(_carto_holds(locks, jobs)) == 1
    await assert_reservation_blocks_everything(reservation)

    release_body.set()
    await _join(env, job_id)
    assert tuple(locks._governance_holds(jobs)) == ()
    assert locks._governance_cooldowns(jobs, now_epoch=0.0, cooldown_seconds=1e9)

    # A failed graph acquisition still exits through the existing finalizer.
    armed["body"] = False
    locks._LOCKS.cooldowns.clear()

    def unresolvable_regions(spec, proof):
        raise MutationRegionError("fixture region cannot be placed", reason="unresolvable_region")

    monkeypatch.setattr(server, "_carto_job_regions", unresolvable_regions)
    _failed_ack, failed_id = await _launch_new_job(env, "file")
    await _join(env, failed_id)
    assert tuple(locks._governance_holds(jobs)) == ()
    assert _job_payload(jobs, failed_id)["status"] == "failed"

    # An empty bulk never receives a graph grant; the reservation is removed.
    monkeypatch.setattr(server, "_carto_job_regions", real_regions)
    locks._LOCKS.cooldowns.clear()
    monkeypatch.setattr(
        server, "_collect_bulk_visible_apply_groups",
        lambda *args, **kwargs: ({}, (), 0),
    )
    _bulk_ack, bulk_id = await _launch_new_job(env, "bulk")
    await _join(env, bulk_id)
    assert tuple(locks._governance_holds(jobs)) == ()
    assert _job_payload(jobs, bulk_id)["status"] == "completed"


# --------------------------------------------------------------------------- #
# Row 2 - Q3 all-or-none and exact, idempotent release
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_q3_group_is_all_or_none_and_exact_release_is_idempotent(
    tmp_path, monkeypatch, isolated_registry
):
    from workflowy_mcp import carto_job_governance as gov
    from workflowy_mcp import mutation_locks as locks

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    free = _file_target(tmp_path, "free.py", "root-free")
    blocked = _file_target(tmp_path, "blocked.py", "root-blocked")
    extra = _file_target(tmp_path, "extra.py", "root-extra")

    async def scenario() -> None:
        holder = await gov.acquire_q3_leases_async(
            [blocked], account_scope_id=ACCOUNT, jobs_root=jobs
        )
        assert len(holder) == 1 and holder[0]["lease_id"]
        assert not (jobs / "q3").exists()
        observable = tuple(locks._governance_holds(jobs))
        assert [hold["path"] for hold in observable] == [blocked["path"]]
        disk_before = sorted(str(path) for path in jobs.rglob("*"))

        if _enabled("CUT2B_NEUTRALIZE_Q3_ALL_OR_NONE"):
            # Probe: insert the first target before the final target validates,
            # destroying the group's all-or-none property.
            await gov.acquire_q3_leases_async(
                [free], account_scope_id=ACCOUNT, jobs_root=jobs
            )

        with pytest.raises(gov.GovernanceConflictError) as conflict:
            await gov.acquire_q3_leases_async(
                [free, blocked], account_scope_id=ACCOUNT, jobs_root=jobs
            )
        assert "overlaps active owner" in str(conflict.value)
        assert tuple(locks._governance_holds(jobs)) == observable
        assert sorted(str(path) for path in jobs.rglob("*")) == disk_before

        with pytest.raises(gov.Q3LeaseAcquisitionError):
            await gov.acquire_q3_leases_async(
                [extra, extra], account_scope_id=ACCOUNT, jobs_root=jobs
            )
        assert tuple(locks._governance_holds(jobs)) == observable

        assert await gov.release_q3_leases_async(holder, jobs_root=jobs) == []
        assert tuple(locks._governance_holds(jobs)) == ()

        group = await gov.acquire_q3_leases_async(
            [free, extra], account_scope_id=ACCOUNT, jobs_root=jobs
        )
        assert len(group) == 2
        assert len({receipt["lease_id"] for receipt in group}) == 1
        assert {receipt["path"] for receipt in group} == {free["path"], extra["path"]}
        assert await gov.release_q3_leases_async(group, jobs_root=jobs) == []
        assert tuple(locks._governance_holds(jobs)) == ()
        # A second release of the same receipts is an inert no-op.
        assert await gov.release_q3_leases_async(group, jobs_root=jobs) == []
        assert tuple(locks._governance_holds(jobs)) == ()

        newer = await gov.acquire_q3_leases_async(
            [extra], account_scope_id=ACCOUNT, jobs_root=jobs
        )
        newer_lease = newer[0]["lease_id"]
        if _enabled("CUT2B_NEUTRALIZE_Q3_RELEASE_IDENTITY"):
            # Probe: release resolves by footprint, so the stale receipt removes
            # the newer acquisition with the same footprint.
            _install_footprint_release(monkeypatch, gov, locks)
        assert await gov.release_q3_leases_async(group, jobs_root=jobs) == []
        surviving = tuple(locks._governance_holds(jobs))
        assert [hold["lease_id"] for hold in surviving] == [newer_lease]
        assert await gov.release_q3_leases_async(newer, jobs_root=jobs) == []
        assert tuple(locks._governance_holds(jobs)) == ()

        # Empty input stays a no-behaviour call.
        assert await gov.acquire_q3_leases_async([], account_scope_id=ACCOUNT, jobs_root=jobs) == []
        assert await gov.release_q3_leases_async([], jobs_root=jobs) == []

    await scenario()


# --------------------------------------------------------------------------- #
# Row 3 - the three symmetric refusal directions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("direction", ["carto_to_q3", "q3_to_carto", "q3_to_q3"])
@pytest.mark.asyncio
async def test_carto_q3_and_q3_q3_refuse_symmetrically(
    direction, tmp_path, monkeypatch, isolated_registry
):
    from workflowy_mcp import carto_job_governance as gov
    from workflowy_mcp import mutation_locks as locks

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    shared = _file_target(tmp_path, "shared.py", "root-holder")
    same_path_other_root = {
        "kind": "file", "path": shared["path"],
        "root_uuid": "root-other", "ancestor_chain": ["root-other"],
    }
    pathless_parent = {"kind": "folder", "path": None, "root_uuid": "vault",
                       "ancestor_chain": ["vault"]}
    pathless_child = {"kind": "folder", "path": None, "root_uuid": "child",
                      "ancestor_chain": ["vault", "child"]}
    disjoint = _file_target(tmp_path, "disjoint.py", "root-disjoint")

    async def scenario() -> None:
        if _enabled("CUT2B_NEUTRALIZE_Q3_SYMMETRY"):
            # Probe: drop the one symmetric overlap predicate for this direction.
            monkeypatch.setattr(gov, "footprints_overlap", lambda _left, _right: False)

        async def install_holder(footprint, label):
            if direction == "carto_to_q3":
                admitted = await gov.admit_carto_job(
                    job_file=jobs / f"{label}.json",
                    job_payload={"id": f"{label}-holder"},
                    job_type="CARTO_REFRESH",
                    dedupe_key="a" * 64,
                    account_scope_id=ACCOUNT,
                    footprint=footprint,
                    launcher="system",
                    parent_job_id=None,
                    prepared=False,
                    jobs_root=jobs,
                )
                assert admitted["claim_path"] is None
                return "carto", admitted["claim"]["claim_id"]
            return "q3", await gov.acquire_q3_leases_async(
                [footprint], account_scope_id=ACCOUNT, jobs_root=jobs
            )

        async def drop_holder(kind, value):
            if kind == "carto":
                assert await gov.finalize_claim_async(value, jobs) is True
            else:
                assert await gov.release_q3_leases_async(value, jobs_root=jobs) == []

        async def contend(footprint, label):
            if direction == "q3_to_carto":
                with pytest.raises(gov.AdmissionRefusal) as caught:
                    await gov.admit_carto_job(
                        job_file=jobs / f"{label}-contender.json",
                        job_payload={"id": f"{label}-contender"},
                        job_type="CARTO_REFRESH",
                        dedupe_key="b" * 64,
                        account_scope_id=ACCOUNT,
                        footprint=footprint,
                        launcher="system",
                        parent_job_id=None,
                        prepared=False,
                        jobs_root=jobs,
                    )
                assert caught.value.kind == "overlap_conflict"
                assert "overlaps" in str(caught.value)
                # Q3 holds carry no job-duration advice.
                assert caught.value.details.get("retry_after") is None
                return
            with pytest.raises(gov.GovernanceConflictError) as caught:
                await gov.acquire_q3_leases_async(
                    [footprint], account_scope_id=ACCOUNT, jobs_root=jobs
                )
            assert "overlaps active owner" in str(caught.value)

        for holder_footprint, contender_footprint, label in (
            (shared, same_path_other_root, f"{direction}-same-path"),
            (pathless_parent, pathless_child, f"{direction}-pathless"),
        ):
            kind, value = await install_holder(holder_footprint, label)
            await contend(contender_footprint, label)
            await drop_holder(kind, value)
            # No cooldown follows a Q3 release; a released Q3 hold lets the
            # immediate CARTO admission through, and vice versa.
            if direction == "q3_to_carto":
                admitted = await gov.admit_carto_job(
                    job_file=jobs / f"{label}-after.json",
                    job_payload={"id": f"{label}-after"},
                    job_type="CARTO_REFRESH",
                    dedupe_key="c" * 64,
                    account_scope_id=ACCOUNT,
                    footprint=contender_footprint,
                    launcher="system",
                    parent_job_id=None,
                    prepared=False,
                    jobs_root=jobs,
                )
                assert admitted["admitted"] is True
                assert await gov.finalize_claim_async(admitted["claim"]["claim_id"], jobs) is True
            else:
                receipts = await gov.acquire_q3_leases_async(
                    [contender_footprint], account_scope_id=ACCOUNT, jobs_root=jobs
                )
                assert await gov.release_q3_leases_async(receipts, jobs_root=jobs) == []

        # Disjoint control: a different path is admitted while nothing is held.
        receipts = await gov.acquire_q3_leases_async(
            [disjoint], account_scope_id=ACCOUNT, jobs_root=jobs
        )
        assert len(receipts) == 1
        assert await gov.release_q3_leases_async(receipts, jobs_root=jobs) == []
        assert tuple(locks._governance_holds(jobs)) == ()

    await scenario()


# --------------------------------------------------------------------------- #
# Row 4 - cooldown max-merge, configured value, history independence
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cooldown_max_merge_config_and_history_independence(
    tmp_path, monkeypatch, isolated_registry, isolated_server_memory
):
    from workflowy_mcp import carto_job_governance as gov
    from workflowy_mcp import mutation_locks as locks
    from workflowy_mcp import server

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    footprint = _file_target(tmp_path, "cooldown.py", "root-cooldown")

    if _enabled("CUT2B_NEUTRALIZE_COOLDOWN"):
        monkeypatch.setattr(locks, "_governance_cooldowns", lambda *args, **kwargs: ())

    if _enabled("CUT2B_NEUTRALIZE_COOLDOWN_MAX"):
        real_finalize = locks._finalize_carto_hold

        def last_write_wins(claim_id, jobs_root, **kwargs):
            real_finalize(claim_id, jobs_root, **kwargs)
            # Probe: overwrite instead of max-merging the terminal timestamp.
            for key in list(locks._LOCKS.cooldowns):
                record = dict(locks._LOCKS.cooldowns[key])
                record["last_terminal_at"] = gov.utc_now_iso(kwargs.get("now_epoch"))
                locks._LOCKS.cooldowns[key] = record
            return True

        monkeypatch.setattr(locks, "_finalize_carto_hold", last_write_wins)

    async def admit(dedupe: str, label: str):
        return await gov.admit_carto_job(
            job_file=jobs / f"{label}.json",
            job_payload={"id": label, "type": "CARTO_REFRESH"},
            job_type="CARTO_REFRESH",
            dedupe_key=dedupe,
            account_scope_id=ACCOUNT,
            footprint=footprint,
            launcher="system",
            parent_job_id=None,
            prepared=False,
            jobs_root=jobs,
            cooldown_seconds=0.0,
        )

    async def cooldown_records(now_epoch: float) -> tuple:
        return tuple(locks._governance_cooldowns(
            jobs, now_epoch=now_epoch, cooldown_seconds=1e9
        ))

    assert gov.DEFAULT_COOLDOWN_SECONDS == 60.0

    # Public path: one admitted job finalizes into the non-owning history.
    first = await admit("1" * 64, "cooldown-first")
    assert await gov.finalize_claim_async(first["claim"]["claim_id"], jobs, now_epoch=1000.0) is True
    assert [record["last_terminal_at"] for record in await cooldown_records(1000.0)] == [
        gov.utc_now_iso(1000.0)
    ]

    # Explicit nondefault 137 s: still cooling at 1136/1135, released at 1137.
    gate = await gov.negative_exact_intent_gate(
        dedupe_key="1" * 64, jobs_root=jobs, now_epoch=1136.0, cooldown_seconds=137.0
    )
    assert gate["error_kind"] == "cooldown_active"
    assert gate["retry_after"] == pytest.approx(1.0)
    assert (await gov.negative_exact_intent_gate(
        dedupe_key="1" * 64, jobs_root=jobs, now_epoch=1135.0, cooldown_seconds=137.0
    ))["retry_after"] == pytest.approx(2.0)

    # Configured value is read, never hard-coded: 137 s with no override.
    from workflowy_mcp.models.config import ServerConfig
    configured = ServerConfig.model_validate({"WORKFLOWY_API_KEY": "isolated-test-key"})
    configured.admission = configured.admission.model_copy(update={"cooldown_seconds": 137.0})
    monkeypatch.setattr(gov, "get_server_config", lambda: configured)
    if _enabled("CUT2B_NEUTRALIZE_COOLDOWN_CONFIG"):
        monkeypatch.setattr(gov, "_configured_cooldown_seconds", lambda: 60.0)
    configured_gate = await gov.negative_exact_intent_gate(
        dedupe_key="1" * 64, jobs_root=jobs, now_epoch=1030.0
    )
    assert configured_gate["error_kind"] == "cooldown_active"
    assert configured_gate["retry_after"] == pytest.approx(107.0)
    assert await gov.negative_exact_intent_gate(
        dedupe_key="1" * 64, jobs_root=jobs, now_epoch=1137.0, cooldown_seconds=137.0
    ) is None

    # Zero cooldown admits the identical intent immediately.
    second = await admit("1" * 64, "cooldown-zero")
    assert second["admitted"] is True
    assert await gov.finalize_claim_async(second["claim"]["claim_id"], jobs, now_epoch=1200.0) is True

    # `_finalize_carto_hold` is the sole insertion/max-merge authority; a
    # registry-level sequence proves the terminal-time merge itself (public
    # admission serializes same-footprint jobs through the cooldown).
    locks._LOCKS.cooldowns.clear()
    observed = []
    reservation = None
    for job_id, terminal_at, dedupe in (
        ("max-a", 1000.0, "a" * 64),
        ("max-b", 900.0, "b" * 64),
        ("max-c", 1200.0, "c" * 64),
        ("max-d", 1200.0, "d" * 64),
    ):
        reservation = locks._reserve_carto_hold(
            jobs_root=jobs,
            account_scope_id=ACCOUNT,
            footprint=footprint,
            job_id=job_id,
            job_file=jobs / f"{job_id}.json",
            job_type="CARTO_REFRESH",
            mode="file",
            dedupe_key=dedupe,
            launcher="system",
        )
        assert await gov.finalize_claim_async(
            reservation["claim_id"], jobs, now_epoch=terminal_at
        ) is True
        assert tuple(locks._governance_holds(jobs)) == ()
        observed.append([
            record["last_terminal_at"] for record in await cooldown_records(terminal_at)
        ])
    assert observed == [
        [gov.utc_now_iso(1000.0)],
        [gov.utc_now_iso(1000.0)],  # earlier terminal timestamp cannot shorten
        [gov.utc_now_iso(1200.0)],  # later terminal timestamp replaces
        [gov.utc_now_iso(1200.0)],  # equal terminal timestamp stays stable
    ]
    # Idempotent finalize and an absent id never extend or create a record.
    assert await gov.finalize_claim_async(reservation["claim_id"], jobs, now_epoch=2000.0) is True
    assert await gov.finalize_claim_async("f" * 32, jobs, now_epoch=2000.0) is True
    assert [record["last_terminal_at"] for record in await cooldown_records(1300.0)] == [
        gov.utc_now_iso(1200.0)
    ]

    # Clearing terminal job history never clears the memory cooldown.
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(jobs))
    for path in jobs.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "completed"
        gov.atomic_write_json(path, payload)
    cleared = await server._clear_carto_job_history()
    assert cleared["deleted_jobs"] >= 1
    assert not list(jobs.glob("*.json"))
    assert await cooldown_records(1200.0)
    assert await gov.negative_exact_intent_gate(
        dedupe_key="d" * 64, jobs_root=jobs, now_epoch=1210.0, cooldown_seconds=137.0
    ) is not None

    # Fresh memory drops it: cooldown is non-owning history, never durable.
    monkeypatch.setattr(locks, "_LOCKS", locks._RegionRegistry())
    assert await gov.negative_exact_intent_gate(
        dedupe_key="d" * 64, jobs_root=jobs, now_epoch=1210.0, cooldown_seconds=137.0
    ) is None


# --------------------------------------------------------------------------- #
# Row 5 - cancel/status failure finalize before exact release
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cancel_and_status_failure_finalize_before_exact_release(
    real_job_environment, monkeypatch
):
    from workflowy_mcp import config, mutation_locks as locks

    env = real_job_environment
    server, jobs = env.server, env.jobs

    real_body = env.client.refresh_file_node_beacons
    base_cancel_finalizer = server.finalize_cancelled_job_claim_async
    refused_terminal_statuses = []

    async def run_parked_job(*, fail_terminal_write: bool) -> tuple:
        entered, release = asyncio.Event(), asyncio.Event()
        probe_kill = _enabled("CUT2B_NEUTRALIZE_KILL_PATH")

        async def parked_body(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real_body(*args, **kwargs)

        monkeypatch.setattr(env.client, "refresh_file_node_beacons", parked_body)
        locks._LOCKS.cooldowns.clear()
        if fail_terminal_write:
            real_merge = config.merge_write_carto_job_status

            def failing_merge(job_file, payload, **kwargs):
                if str(payload.get("status") or "").lower() in {
                    "completed", "completed_with_warnings", "failed", "cancelled",
                }:
                    refused_terminal_statuses.append(payload["status"])
                    return False
                return real_merge(job_file, payload, **kwargs)

            # G4's registered writer imports this same funnel from config;
            # retain the server alias injection for the no-writer path too.
            monkeypatch.setattr(config, "merge_write_carto_job_status", failing_merge)
            monkeypatch.setattr(server, "merge_write_carto_job_status", failing_merge)

        with ExitStack() as traps:
            if probe_kill:
                # Probe: the real job task is created here, long before the
                # cancel call the old trap wrapped. Enter the trap first, so
                # the *task method itself* is the oracle; otherwise a plain
                # asyncio.Task made before the trap would accept cancel().
                traps.enter_context(reject_raw_job_cancel(env))
            _ack, job_id = await _launch_new_job(env, "file")
            await wait_job_gate(env, entered, job_id=job_id)
            reservation = _carto_holds(locks, jobs)[0]
            job_file, claim_id = reservation["job_file"], reservation["claim_id"]
            entry = locks._require_carto_hold(job_file, claim_id)
            assert entry.state is not None

            seen: list[tuple[str, int, bool]] = []
            cancel_finalizer = base_cancel_finalizer
            leak = _enabled("CUT2B_NEUTRALIZE_STATUS_RELEASE") and bool(fail_terminal_write)

            async def observed_cancel_finalizer(*args, **kwargs):
                if _enabled("CUT2B_NEUTRALIZE_FINALIZE_RELEASE_ORDER"):
                    # Probe: exact release happens before finalization.
                    locks._LOCKS.holders.clear()
                alive = True
                try:
                    locks._require_carto_hold(job_file, claim_id)
                except Exception:
                    alive = False
                seen.append(("enter", len(_carto_holds(locks, jobs)), alive))
                if leak:
                    # Probe: a failed terminal write skips the one memory
                    # finalization (no finalized flag, no cooldown publication).
                    return "cancelled"
                result = await cancel_finalizer(*args, **kwargs)
                seen.append(("exit", len(_carto_holds(locks, jobs)),
                             entry.governance.finalized))
                return result

            monkeypatch.setattr(server, "finalize_cancelled_job_claim_async",
                                observed_cancel_finalizer)

            if not probe_kill:
                traps.enter_context(reject_raw_job_cancel(env))
            cancel_task = asyncio.create_task(server._system_job_cancel_impl(job_id))
            await asyncio.sleep(0.1)
            release.set()
            result = await asyncio.wait_for(cancel_task, WAIT)
            assert result["outcome"] == "cooperative"
            assert result["success"] is True
            terminal = await _join(env, job_id)
        return result, terminal, job_id, seen, entry

    result, terminal, job_id, seen, entry = await run_parked_job(fail_terminal_write=False)
    kinds = [event["kind"] for event in env.events]
    assert "finalizer-enter" in kinds and "grant-before-release" in kinds
    assert kinds.index("finalizer-enter") < kinds.index("grant-before-release")
    # Finalization runs under the exact, not-yet-released grant and completes
    # before the outer finally releases it: one enter, one exit, entry finalized.
    assert seen[0] == ("enter", 1, True), seen
    assert [event[0] for event in seen] == ["enter", "exit"], seen
    assert seen[-1] == ("exit", 1, True), seen
    assert entry.governance.finalized is True
    assert terminal["status"] == "cancelled"
    assert tuple(locks._governance_holds(jobs)) == ()

    if _enabled("CUT2B_NEUTRALIZE_KILL_PATH"):
        # Probe: a process-control escalation would call the real task method;
        # the fixture trap must reject it. The job task was created under the
        # same trap above, so this is the real ``Task.cancel`` override.
        with reject_raw_job_cancel(env):
            env.tasks[job_id].cancel()

    # Repeat with the terminal status writer failing: the primary failure is
    # retained, memory finalization still runs, and no registry entry leaks.
    result, terminal, job_id, seen, entry = await run_parked_job(fail_terminal_write=True)
    assert result["outcome"] == "cooperative"
    assert refused_terminal_statuses == ["cancelled"], "terminal-write failure injection missed the real writer"
    assert terminal["status"] == "failed"
    assert "status_write_error" in terminal
    assert [event[0] for event in seen] == ["enter", "exit"], seen
    assert seen[-1] == ("exit", 1, True), seen
    assert entry.governance.finalized is True
    assert locks._governance_cooldowns(jobs, now_epoch=0.0, cooldown_seconds=1e9)
    assert tuple(locks._governance_holds(jobs)) == ()


# --------------------------------------------------------------------------- #
# Row 6 - short locks: wait, cancel, order, no work under the span
# --------------------------------------------------------------------------- #
def _call_name(node) -> str | None:
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _short_lock_offenders(sources: dict[str, str]) -> tuple[list[str], int]:
    offenders: list[str] = []
    spans = 0
    for name, source in sources.items():
        tree = ast.parse(source)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncWith):
                continue
            if "admission_lock" not in {_call_name(item.context_expr) for item in node.items}:
                continue
            spans += 1
            owner = "<module>"
            cursor = node
            while cursor in parents:
                cursor = parents[cursor]
                if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = cursor.name
                    break
            for statement in node.body:
                for child in ast.walk(statement):
                    if isinstance(child, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                        offenders.append(f"await in admission span {name}:{owner}")
                    if isinstance(child, ast.Call) and _call_name(child.func) in _FORBIDDEN_UNDER_SHORT_LOCK:
                        offenders.append(
                            f"forbidden work in admission span {name}:{owner}:{_call_name(child.func)}"
                        )
            cursor = node
            while cursor in parents:
                cursor = parents[cursor]
                if isinstance(cursor, (ast.With, ast.AsyncWith)) and any(
                    "publisher" in ast.unparse(item.context_expr)
                    or "coherence_lock" in ast.unparse(item.context_expr)
                    for item in cursor.items
                ):
                    offenders.append(f"reverse short-lock order {name}:{owner}")
    return offenders, spans


@pytest.mark.asyncio
async def test_short_locks_wait_cancel_order_and_no_io_under_hold(
    tmp_path, monkeypatch, isolated_registry
):
    from workflowy_mcp import carto_job_governance as gov

    jobs = tmp_path / "jobs"
    target = _file_target(tmp_path, "short-lock.py", "root-short")
    normalized = os.path.normcase(os.path.abspath(os.fspath(gov.get_jobs_root(jobs))))

    async def scenario() -> None:
        # One in-process lock per normalized jobs root; no lock file or path.
        assert isinstance(gov._ADMISSION_LOCKS, dict)
        assert gov.DEFAULT_MUTEX_WAIT_SECONDS == 10.0
        assert gov.DEFAULT_MUTEX_POLL_SECONDS == 0.05
        assert not hasattr(gov.governance_paths(jobs), "admission_mutex")
        assert gov.governance_paths(jobs).jobs_root == gov.get_jobs_root(jobs)

        holder_entered = asyncio.Event()
        holder_release = asyncio.Event()

        async def holder():
            async with gov.admission_lock(jobs):
                holder_entered.set()
                await holder_release.wait()

        held = asyncio.create_task(holder())
        await holder_entered.wait()
        admission_lock = gov._ADMISSION_LOCKS.get(normalized)
        assert isinstance(admission_lock, asyncio.Lock)
        # One lock per normalized absolute jobs root, whatever spelling is used.
        assert gov._ADMISSION_LOCKS.get(
            os.path.normcase(os.path.abspath(os.fspath(jobs / ".")))
        ) is admission_lock

        # Zero timeout permits exactly the immediate attempt, then refuses.
        with pytest.raises(gov.AdmissionBusyTimeoutError):
            async with gov.admission_lock(jobs, timeout_seconds=0.0):
                pytest.fail("a zero-timeout acquisition entered a held admission lock")

        # A real contender waits without entering the transaction, and the
        # daemon loop keeps ticking while it waits.
        ticks = 0
        transaction_entered = asyncio.Event()
        stop_ticker = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not stop_ticker.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        async def contender():
            async with gov.admission_lock(jobs):
                transaction_entered.set()
                # The locked body runs synchronously under the same span, like
                # the real async fronts; no nested lock acquisition.
                return gov.negative_exact_intent_gate_locked(
                    dedupe_key="a" * 64, jobs_root=jobs
                )

        ticker_task = asyncio.create_task(ticker())
        contender_task = asyncio.create_task(contender())
        await asyncio.sleep(0.12)
        assert ticks >= 5
        assert not transaction_entered.is_set()
        assert tuple(isolated_registry._governance_holds(jobs)) == ()
        holder_release.set()
        await asyncio.wait_for(contender_task, WAIT)
        stop_ticker.set()
        await ticker_task
        await held
        assert transaction_entered.is_set()
        assert tuple(isolated_registry._governance_holds(jobs)) == ()

        # Cancellation stops the wait with no late body.
        flag = {"cancel": False}
        late_body = asyncio.Event()

        async def cancellable():
            kwargs = {"cancel_check": None if _enabled("CUT2B_NEUTRALIZE_CANCEL_WAIT")
                      else lambda: flag["cancel"]}
            async with gov.admission_lock(jobs, **kwargs):
                late_body.set()

        blocker_entered = asyncio.Event()
        blocker_release = asyncio.Event()

        async def blocker():
            async with gov.admission_lock(jobs):
                blocker_entered.set()
                await blocker_release.wait()

        blocker_task = asyncio.create_task(blocker())
        await blocker_entered.wait()
        cancelled = asyncio.create_task(cancellable())
        await asyncio.sleep(0.08)
        flag["cancel"] = True
        blocker_release.set()
        await blocker_task
        if _enabled("CUT2B_NEUTRALIZE_CANCEL_WAIT"):
            # Probe: the ignored cancel request lets the late body run.
            await asyncio.wait_for(cancelled, WAIT)
        else:
            with pytest.raises(gov.MutexWaitCancelledError):
                await cancelled
        assert not late_body.is_set()

        # No suspension may occur while the short span is held: a sampling
        # task can never observe the lock held across a yield.
        observed_while_held: list[int] = []
        stop_sampler = asyncio.Event()

        async def sampler():
            while not stop_sampler.is_set():
                if admission_lock.locked():
                    observed_while_held.append(1)
                await asyncio.sleep(0)

        sampler_task = asyncio.create_task(sampler())
        if _enabled("CUT2B_NEUTRALIZE_AWAIT_IN_LOCK"):
            # Probe: hold the span across a suspension.
            real_acquire = gov.acquire_q3_leases_async

            async def awaiting_span(targets, **kwargs):
                async with gov.admission_lock(kwargs.get("jobs_root")):
                    await asyncio.sleep(0.01)
                return await real_acquire(targets, **kwargs)

            monkeypatch.setattr(gov, "acquire_q3_leases_async", awaiting_span)
        leases = await gov.acquire_q3_leases_async(
            [target], account_scope_id=ACCOUNT, jobs_root=jobs
        )
        stop_sampler.set()
        await sampler_task
        assert observed_while_held == []
        assert await gov.release_q3_leases_async(leases, jobs_root=jobs) == []

    await scenario()

    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in (PACKAGE / "coherence").glob("*.py")
    }
    for name in ("carto_job_governance.py", "server.py", "mutation_locks.py", "config.py"):
        sources[name] = (PACKAGE / name).read_text(encoding="utf-8")
    offenders, spans = _short_lock_offenders(sources)
    assert spans >= 3, "the admission-span inventory went missing"
    assert offenders == []

    mutants = (
        "async def mutant(paths):\n"
        "    async with admission_lock(paths):\n"
        "        await io()\n",
        "async def mutant(paths, client):\n"
        "    async with admission_lock(paths):\n"
        "        await run_carto_cpu('x', f)\n",
        "async def mutant(paths, publisher):\n"
        "    with publisher._mutex:\n"
        "        async with admission_lock(paths):\n"
        "            pass\n",
        "async def mutant(paths):\n"
        "    async with coherence_lock_async(paths):\n"
        "        async with admission_lock(paths):\n"
        "            pass\n",
    )
    per_mutant = [
        _short_lock_offenders({"<mutant>": mutant})[0] for mutant in mutants
    ]
    if _enabled("CUT2B_NEUTRALIZE_LOCK_ORDER"):
        per_mutant = [[] for _ in mutants]
    assert all(per_mutant), per_mutant


# --------------------------------------------------------------------------- #
# Row 7 - CF-43 per-job artifact directory owned by existing GC
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_job_artifacts_are_per_job_and_existing_gc_owns_them(
    real_job_environment, monkeypatch, request
):
    from workflowy_mcp.carto_job_runtime import CartoJobContext as RealJobContext

    env = real_job_environment
    server, jobs = env.server, env.jobs

    if _enabled("CUT2B_NEUTRALIZE_JOB_DIRECTORY"):
        # Probe: every job keeps the shared jobs root as its artifact directory.
        class SharedRootContext(RealJobContext):
            def __init__(self, job_file, job_dir, log_file, job_control, claim_id="", **kwargs):
                super().__init__(job_file, Path(job_file).parent, log_file, job_control,
                                 claim_id=claim_id, **kwargs)

        monkeypatch.setattr(server, "CartoJobContext", SharedRootContext)

    after_peer_body, release_peer = asyncio.Event(), asyncio.Event()
    request.addfinalizer(release_peer.set)
    real_body = env.client.refresh_file_node_beacons

    async def gated_body(*args, **kwargs):
        result = await real_body(*args, **kwargs)
        if str(kwargs.get("file_node_id")) == env.ids["outside-file"]:
            after_peer_body.set()
            await release_peer.wait()
        return result

    monkeypatch.setattr(env.client, "refresh_file_node_beacons", gated_body)

    _ack_a, job_a = await _launch_new_job(env, "file")
    await _join(env, job_a)
    _ack_b, job_b = await _launch_new_job(env, "peer")
    await wait_job_gate(env, after_peer_body, job_id=job_b)

    json_a, json_b = _job_json(jobs, job_a), _job_json(jobs, job_b)
    dir_a, dir_b = json_a.with_suffix(""), json_b.with_suffix("")
    assert dir_a != dir_b
    assert dir_a.is_dir() and dir_b.is_dir()
    # No per-file artifact leaked into the shared jobs root.
    assert not (jobs / "file_refresh").exists()
    artifacts_a = sorted(path.name for path in (dir_a / "file_refresh").glob("*"))
    artifacts_b = sorted(path.name for path in (dir_b / "file_refresh").glob("*"))
    assert any(name.endswith(".json") for name in artifacts_a), artifacts_a
    assert any(name.endswith(".json") for name in artifacts_b), artifacts_b
    assert any(name.endswith(".reconcile_debug.log") or name == "reconcile_debug.log"
               for name in artifacts_a), artifacts_a
    assert not set(artifacts_a) & (set(artifacts_b) - {"reconcile_debug.log"})

    # The completed job's final-refresh obligation no longer protects it; the
    # live job is protected by its task and its admitted hold.
    server._carto_final_obligations.pop(job_a, None)
    cleared = await server._clear_carto_job_history()
    assert cleared["deleted_jobs"] >= 1
    assert not json_a.exists() and not dir_a.exists()
    assert json_b.exists() and dir_b.is_dir()
    assert (dir_b / "file_refresh").is_dir()

    release_peer.set()
    await _join(env, job_b)
    from workflowy_mcp import mutation_locks as locks

    assert tuple(locks._governance_holds(jobs)) == ()


# --------------------------------------------------------------------------- #
# Row 8 - CF-38 additive pending marker on the real MCP and WS replies
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cancel_pending_wire_keeps_legacy_outcome_and_never_claims_stop(
    real_job_environment, monkeypatch, request
):
    env = real_job_environment
    server = env.server

    if _enabled("CUT2B_NEUTRALIZE_PENDING_MARKER"):
        # Probe: the server drops the additive field.
        real_cancel = server.cancel_carto_job

        async def without_marker(*args, **kwargs):
            result = await real_cancel(*args, **kwargs)
            result.pop("cancellation_pending", None)
            return result

        monkeypatch.setattr(server, "cancel_carto_job", without_marker)

    entered, release = asyncio.Event(), asyncio.Event()
    # A probe failure must not leave the parked body running into fixture
    # teardown; release it either way.
    request.addfinalizer(release.set)
    real_body = env.client.refresh_file_node_beacons

    async def parked_body(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real_body(*args, **kwargs)

    monkeypatch.setattr(env.client, "refresh_file_node_beacons", parked_body)
    _ack, job_id = await _launch_new_job(env, "file")
    await wait_job_gate(env, entered, job_id=job_id)

    with reject_raw_job_cancel(env):
        mcp_reply = await asyncio.wait_for(server._system_job_cancel_impl(job_id), WAIT * 2)
    assert mcp_reply["success"] is True
    assert mcp_reply["outcome"] == "kill_refused_in_process_job"
    assert mcp_reply["status"] == "cancelling"
    assert mcp_reply["cancellation_requested"] is True
    assert mcp_reply.get("cancellation_pending") is True
    assert "No process was killed" in mcp_reply["message"]
    assert env.tasks[job_id].done() is False

    frames = []

    class FakeWebSocket:
        async def send(self, text):
            frames.append(json.loads(text))

    await asyncio.wait_for(
        server._handle_carto_cancel_job({"job_id": job_id}, FakeWebSocket()), WAIT * 2
    )
    assert len(frames) == 1
    frame = frames[0]
    assert frame["action"] == "carto_cancel_job_result"
    for key, value in mcp_reply.items():
        assert frame[key] == value
    assert frame.get("cancellation_pending") is True

    # Release the parked body: the real task stops only then, and the terminal
    # answer carries no pending marker.
    release.set()
    await _join(env, job_id)
    after = await asyncio.wait_for(server._system_job_cancel_impl(job_id), WAIT)
    assert after["outcome"] == "already_terminal"
    assert "cancellation_pending" not in after
