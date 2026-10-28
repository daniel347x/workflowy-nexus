"""N03 G3: independent fake account, real sender/governor/region/cache/barrier.

Wire simulation is P0-prime s1/s3/s4/s6/s7 (2026-10-23): v1 stores one
name decode, LLM stores n directly, notes unchanged, listings not wire-sorted.
No test connects to an API or invokes the live probe harness.
"""
from __future__ import annotations

import asyncio
import copy
import html
import inspect
import json
import os
import textwrap
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import pytest

from test_carto_write_governor_phase_b import _real_governed_client
from test_n03_create_transport import PARENT, PREDECESSOR, CREATED
from workflowy_mcp.client import api_client_core as core
from workflowy_mcp.client.create_transport import PlacementIntent, SendStage
from workflowy_mcp.client.insert_capability import InsertCapabilityController, InsertCapabilityState
from workflowy_mcp.coherence import transition_create as tc
from workflowy_mcp.coherence.barrier import hard_stop_state, raise_global_dirty_barrier_async
from workflowy_mcp.coherence.cache_view import CacheView, CacheViewError
from workflowy_mcp.coherence.errors import CoherenceBarrierActiveError
from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, create_isolated_context_task
from workflowy_mcp.coherence.schema import read_external_dirt, initialize_manifest_if_absent, new_bootstrap_manifest
from workflowy_mcp.models import AuthenticationError, NodeCreateRequest, NodeListRequest, OperationCancelledError, RateLimitError
from workflowy_mcp.mutation_lease_governance import build_region_child_set, build_region_subtree
from workflowy_mcp.mutation_locks import mutation_region_locks

OTHER = "c02cdc51-0000-4000-8000-000000000002"
GUI = "c02cdc51-0000-4000-8000-000000000003"
TAIL = "c02cdc51-0000-4000-8000-000000000004"


@pytest.fixture(autouse=True)
def mutants(monkeypatch):
    mutant = os.getenv("N03_G3_MUTANT")
    if not mutant:
        return
    owner, name, namespace = tc, "classify_create_run", vars(tc)
    changes = {
        "first_candidate": ('any(len(matches) != 1 for matches in matching)', 'any(not matches for matches in matching)'),
        "missing_note": ('note = row.get("note")', 'note = row["note"]'),
        "empty_note": ('_decode_text(note, profile, field="note") or ""', '_decode_text(note, profile, field="note")'),
        "wire_order": ('owned.sort(key=lambda row: row["priority"])', 'pass'),
        "dirt_generation": ('generation=previous.generation + 1', 'generation=previous.generation'),
        "listing_evidence": ('if not evidence.get("recognized") or baseline.parent_uuid != parent_id:', 'if baseline.parent_uuid != parent_id:'),
        "gui_adjacency": ('not any(i in old_ids for i in order[order.index(predecessor) + 1:index])', 'not order[order.index(predecessor) + 1:index]'),
        "a1": ('original_auth = response.response', 'await self.client._handle_response(response.response)'),
        "post_slot": ('await require_coherence_mutation_admission(self.paths, operation="create")', 'pass'),
        "stage_never": ('_send_stage.dispatch_started = True', 'pass'),
        "stage_early": ('await governor.recheck_before_send(ticket, cancel_check=cancel_check)', 'if _send_stage is not None:\n    _send_stage.dispatch_started = True\nawait governor.recheck_before_send(ticket, cancel_check=cancel_check)'),
        "v1_unknown": ('if transport == "llm" and not stage.dispatch_started:', 'if transport != "llm" or not stage.dispatch_started:'),
        "region_identity": ('description.get("kind") != "child_set" or description.get(parent_field) != parent', 'False'),
        "snapshot_only_baseline": ('and parent_clean_now', ''),
    }
    if mutant == "region_coverage":
        monkeypatch.setattr(tc, "_regions_cover_request", lambda *a, **kw: True)
        return
    if mutant == "serialization":
        from workflowy_mcp import mutation_locks
        monkeypatch.setattr(mutation_locks, "_scan_region_blockers", lambda *a, **kw: ())
        return
    if mutant == "cancel_cleanup":
        original = tc.TransitionCreateTransaction._joined_verification
        async def skip(self, *args, cancelled=None):
            if cancelled is not None:
                return tc.AttemptClassification(tc.CreateClassification.CONCLUSIVELY_ABSENT, args[0]), cancelled
            return await original(self, *args, cancelled=cancelled)
        monkeypatch.setattr(tc.TransitionCreateTransaction, "_joined_verification", skip)
        return
    if mutant in ("stage_never", "stage_early"):
        owner, name, namespace = core.WorkFlowyClientCore, "_send_workflowy_write", vars(core)
    elif mutant in ("a1", "post_slot", "v1_unknown"):
        owner = tc.TransitionCreateTransaction
        name = "_attempt" if mutant in ("a1", "v1_unknown") else "_preflight"
    elif mutant == "region_identity":
        owner, name = tc.TransitionCreateTransaction, "_require_hold"
    elif mutant == "snapshot_only_baseline":
        owner, name = tc.TransitionCreateTransaction, "_baseline"
    elif mutant in ("wire_order", "dirt_generation", "empty_note"):
        name = {"wire_order": "_order", "dirt_generation": "record_external_additions", "empty_note": "_content"}[mutant]
    elif mutant in ("no_raise", "no_recovery", "no_dirty", "no_hold"):
        if mutant == "no_recovery":
            async def noop(): pass
            monkeypatch.setattr(tc, "_request_recovery", noop)
        elif mutant == "no_raise":
            from types import SimpleNamespace
            async def noop(*args, **kwargs):
                return SimpleNamespace(state=SimpleNamespace(generation=0))
            monkeypatch.setattr(tc, "raise_global_dirty_barrier_async", noop)
        else:
            monkeypatch.setattr(tc.TransitionCreateTransaction, "_dirty" if mutant == "no_dirty" else "_require_hold", lambda self: None)
        return
    source = textwrap.dedent(inspect.getsource(getattr(owner, name)))
    old, new = changes[mutant]
    if mutant == "stage_early":
        new = 'if _send_stage is not None:\n    ' + '_send_stage.dispatch_started = True\n' + old
        # Indent injected statements within the method, not at module level.
        new = new.replace('\n', '\n    ')
    assert source.count(old) == 1
    local = dict(namespace)
    exec(compile(source.replace(old, new, 1), "<n03-g3-mutant>", "exec"), local)
    monkeypatch.setattr(owner, name, local[name])


def row(identifier, name, priority, *, parent=PARENT, note=None):
    return dict(id=identifier, name=name, priority=priority, parent_id=parent, note=note)


class Account:
    """Literal ordered server state, never calls the production classifier."""
    def __init__(self, mode="ok", *, empty=False, omitted_note=False):
        self.rows = [] if empty else [row(PREDECESSOR, "left", 100), row(TAIL, "right", 200)]
        self.mode, self.omitted_note = mode, omitted_note
        self.calls, self.llm_count, self.v1_count, self.list_count = [], 0, 0, 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.ids = []

    async def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        if request.method == "GET":
            self.list_count += 1
            if self.mode == "listing_auth":
                return httpx.Response(401, json={})
            if self.mode == "malformed":
                return httpx.Response(200, json={"surprise": []})
            if self.mode == "list_cancel" and self.list_count == 1:
                self.entered.set()
                await self.release.wait()
            return httpx.Response(200, json={"nodes": list(reversed(copy.deepcopy(self.rows)))})
        body = json.loads(request.content)
        llm = request.url.path.endswith("/api/llm/doc/edit")
        if llm:
            self.llm_count += 1
            if isinstance(self.mode, int):
                return httpx.Response(self.mode, json={"message": "endpoint unavailable"})
            if self.mode == "listing_auth":
                return httpx.Response(401, json={})
            if self.mode == "wfb":
                return httpx.Response(403, text="WFB(3)")
            if self.mode == "rate":
                return httpx.Response(429, headers={"Retry-After": "3"}, json={})
            if self.mode == "read_required":
                return httpx.Response(400, json={"code": "read_required"})
            if self.mode == "op_error":
                return httpx.Response(200, json={"results": [{"op": "insert", "status": "error", "error": "not_found"}]})
            if self.mode in ("absent", "cancel_absent"):
                if self.mode.startswith("cancel"):
                    self.entered.set()
                    await self.release.wait()
                raise httpx.ReadTimeout("lost acknowledgement")
            items = body["operations"][0]["items"]
            predecessor = body["operations"][0]["after"]
            index = next(i for i, r in enumerate(self.rows) if r["id"] == predecessor) + 1
        else:
            self.v1_count += 1
            name, note = html.unescape(body["name"]), body.get("note")
            items = [{"n": name, "d": note}]
            index = 0 if body.get("position") == "top" else len(self.rows)
        for offset, item in enumerate(items):
            name, note = item["n"], item.get("d", "")
            # Preserve the two singleton fixture identities; additional vector
            # members must not collide with the reserved GUI/TAIL identities.
            identifier = (CREATED if not self.ids else OTHER if len(self.ids) == 1 else
                          str(UUID(int=UUID(CREATED).int + 0x10000 * len(self.ids))))
            self.ids.append(identifier)
            self.rows.insert(index + offset, row(identifier, name, 0, note=note))
        if llm and self.mode in ("two", "gui", "wrong"):
            if self.mode == "wrong":
                self.rows[index]["name"] = "unexpected stored bytes"
            else:
                self.rows.insert(index + 1, row(GUI, name if self.mode == "two" else "GUI distinct", 0, note=note))
        # Exhaustion model: renumber old rows too, without modifying timestamps.
        for i, r in enumerate(self.rows):
            r["priority"] = 100 * (i + 1)
        if self.omitted_note:
            for r in self.rows:
                if not r.get("note"):
                    r.pop("note", None)
        if llm and self.mode in ("cancel_committed", "cancel_ambiguous"):
            if self.mode == "cancel_ambiguous":
                self.rows[index]["name"] = "unknown content"
            self.entered.set()
            await self.release.wait()
        if llm and self.mode == "timeout":
            raise httpx.ReadTimeout("ack lost after commit")
        if not llm:
            return httpx.Response(200, json={"item_id": identifier})
        if self.mode == "schema":
            return httpx.Response(200, json={"new_schema": True})
        return httpx.Response(200, json={"results": [{"op": "insert", "status": "ok", "created": len(items), "message": "additive"}]})


def setup_client(tmp_path, account, *, forced=False, dirty=False):
    client, governor, clock = _real_governed_client(tmp_path, account)
    initialize_manifest_if_absent(client._coherence_paths(), new_bootstrap_manifest(governor.paths.account_scope_id))
    view = client._cache_view_publisher.capture()
    client._cache_view_publisher.publish(CacheView.build(
        epoch_number=0, epoch_id="f" * 32, adopted_sequence=0,
        nodes_export={"nodes": [row(PARENT, "parent", 100, parent=None), *copy.deepcopy(account.rows)]},
        account_scope_id=view.account_scope_id,
    ))
    client._insert_capability = InsertCapabilityController(governor.paths.account_scope_id, clock=lambda: clock.value, v1_only=lambda: forced)
    if dirty:
        client._mark_nodes_export_dirty([PARENT])
    sleeps = []
    async def sleep(seconds, operation):
        client._raise_if_operation_cancelled(operation)
        sleeps.append((seconds, operation))
        clock.advance(seconds)
        await asyncio.sleep(0)
        client._raise_if_operation_cancelled(operation)
    client._sleep_with_operation_cancel = sleep
    return client, governor, clock, sleeps


@asynccontextmanager
async def transaction(client):
    async with cache_operation_boundary(client, "n03-test"):
        async with mutation_region_locks(client=client, operation_label="n03-test", region_factory=lambda s: (
            build_region_child_set(PARENT, cache_by_id=s.nodes_by_id),
        )) as grant:
            yield tc.TransitionCreateTransaction(client, PARENT, grant, parent_region=grant.regions[0])


@pytest.fixture
def recovery(monkeypatch):
    calls = []
    async def scheduled(): calls.append("fresh recovery scheduled")
    monkeypatch.setattr(tc, "_request_recovery", scheduled)
    return calls


@pytest.mark.parametrize("mode", ["ok", "timeout", "schema", "absent", "read_required", "op_error", 401, 403, 404, 405, 410, 501, 307, 308])
def test_verified_one_attempt_or_absent_then_v1(tmp_path, recovery, mode):
    async def run():
        account = Account(mode)
        client, _, _, sleeps = setup_client(tmp_path, account)
        async with client._client:
            async with transaction(client) as tx:
                result = await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours", note=""), PlacementIntent.after(PREDECESSOR))
                assert result.classification == tc.CreateClassification.COMMITTED
                assert account.llm_count == 1
                fallback = mode not in ("ok", "timeout", "schema")
                assert account.v1_count == int(fallback)
                assert result.reorder_required == fallback
                assert result.created_ids == (CREATED,)
                assert result.order_witness.ordered_child_ids == ((PREDECESSOR, TAIL, CREATED) if fallback else (PREDECESSOR, CREATED, TAIL))
                assert account.list_count == 1 + int(fallback)
                assert len([s for s, _ in sleeps if s == 1.0]) >= account.list_count
                assert PARENT in client._capture_view_and_dirty()[1]
                cached = client._capture_view_and_dirty()[0].nodes_by_id
                assert CREATED in cached
                cache_rows = client._capture_view_and_dirty()[0].nodes_export["nodes"]
                assert sum(r["id"] == CREATED for r in cache_rows) == 1
                assert await tx.conclude() == result.order_witness
            state = client._insert_capability.snapshot().state
            assert state == (InsertCapabilityState.DEGRADED_UNTIL if mode == "schema" or isinstance(mode, int) else
                             InsertCapabilityState.UNKNOWN if fallback else InsertCapabilityState.AVAILABLE)
            assert not recovery
    asyncio.run(run())


@pytest.mark.parametrize("forced", [False, True])
def test_a2_omitted_note_commits_on_both_transports(tmp_path, forced):
    async def run():
        account = Account(omitted_note=True)
        client, _, _, _ = setup_client(tmp_path, account, forced=forced)
        async with client._client, transaction(client) as tx:
            result = await tx.create(NodeCreateRequest(parent_id=PARENT, name="empty", note=""), PlacementIntent.after(PREDECESSOR))
            assert result.created_ids == (CREATED,)
            assert result.transport == ("v1" if forced else "llm")
            assert len(account.ids) == 1
    asyncio.run(run())


def test_a1_listing_auth_propagates_original_without_barrier(tmp_path, recovery):
    async def run():
        account = Account("listing_auth")
        client, _, _, _ = setup_client(tmp_path, account)
        async with client._client, transaction(client) as tx:
            with pytest.raises(AuthenticationError):
                await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
            assert account.llm_count == account.list_count == 1
            assert not account.v1_count and not recovery
            assert not (await hard_stop_state(client._coherence_paths())).hard_dirty
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["two", "wrong", "malformed"])
def test_ambiguity_raises_refuses_and_requests_healing(tmp_path, recovery, mode):
    async def run():
        account = Account(mode)
        client, _, _, _ = setup_client(tmp_path, account)
        async with client._client:
            async with transaction(client) as tx:
                with pytest.raises(CoherenceBarrierActiveError, match="outcome is unknown.*no fallback.*system.cache.refresh") as caught:
                    await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
                assert caught.value.reason_code == "ambiguous_commit_outcome"
                assert (await hard_stop_state(client._coherence_paths())).hard_dirty
                assert caught.value.dirty_generation == 1
                assert recovery == ["fresh recovery scheduled"]
                assert account.llm_count == 1 and not account.v1_count
                with pytest.raises(CoherenceBarrierActiveError):
                    await tx.create(NodeCreateRequest(parent_id=PARENT, name="later"), PlacementIntent.after(PREDECESSOR))
                assert account.llm_count == 1
    asyncio.run(run())


def test_gui_distinct_commits_renumber_is_not_dirt(tmp_path):
    async def run():
        for mode in ("ok", "gui"):
            account = Account(mode)
            client, _, _, _ = setup_client(tmp_path / mode, account)
            async with client._client, transaction(client) as tx:
                result = await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
                assert result.created_ids == (CREATED,)
                dirt = read_external_dirt(client._coherence_paths())
                assert dirt.active == (mode == "gui")
                assert dirt.generation == int(mode == "gui")
                assert result.order_witness.priority_by_id[TAIL] != 200
                assert GUI in result.order_witness.ordered_child_ids if mode == "gui" else GUI not in result.order_witness.ordered_child_ids
                assert not (await hard_stop_state(client._coherence_paths())).hard_dirty
    asyncio.run(run())


@pytest.mark.parametrize("where", ["recheck", "cancel_committed", "cancel_absent", "cancel_ambiguous", "list_cancel"])
@pytest.mark.parametrize("cooperative", [False, True])
def test_cancellation_boundary_and_join_under_hold(tmp_path, monkeypatch, recovery, where, cooperative):
    async def run():
        account = Account("malformed" if where == "recheck" else where)
        client, governor, _, _ = setup_client(tmp_path, account)
        cancelled = [False]
        rechecked = asyncio.Event()
        actual_preflight = tc.TransitionCreateTransaction._preflight
        hooks = []
        async def preflight(self):
            await actual_preflight(self)
            hooks.append("checked")
            if len(hooks) == 2:
                # Entry admission is first; the sole after-ticket callback is
                # second. The unchanged REAL recheck now blocks on its mutex.
                await governor._schedule.lock.acquire()
                rechecked.set()
        if where == "recheck":
            monkeypatch.setattr(tc.TransitionCreateTransaction, "_preflight", preflight)
        async def work():
            with core.operation_cancel_scope(lambda: cancelled[0]):
                async with transaction(client) as tx:
                    try:
                        await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
                    except (asyncio.CancelledError, OperationCancelledError, CoherenceBarrierActiveError):
                        assert tx.held_region.is_active
                        raise
        async with client._client:
            task = asyncio.create_task(work())
            await asyncio.wait_for((rechecked if where == "recheck" else account.entered).wait(), 5)
            if cooperative:
                cancelled[0] = True
                if where == "recheck":
                    governor._schedule.lock.release()
                account.release.set()
            else:
                task.cancel()
                account.release.set()
            expected = CoherenceBarrierActiveError if where == "cancel_ambiguous" else (OperationCancelledError if cooperative else asyncio.CancelledError)
            try:
                with pytest.raises(expected):
                    await task
            finally:
                if where == "recheck" and governor._schedule.lock.locked():
                    governor._schedule.lock.release()
            assert account.v1_count == 0
            if where == "recheck":
                assert account.llm_count == account.list_count == 0
                assert not recovery
            else:
                assert account.llm_count == 1 and account.list_count >= 1
                if where in ("cancel_committed", "list_cancel"):
                    assert CREATED in client._capture_view_and_dirty()[0].nodes_by_id
                    assert PARENT in client._capture_view_and_dirty()[1]
                assert bool(recovery) == (where == "cancel_ambiguous")
    asyncio.run(run())


def test_post_slot_barrier_blocks_llm_http(tmp_path, monkeypatch):
    async def run():
        account = Account()
        client, governor, _, _ = setup_client(tmp_path, account)
        waiting, release = asyncio.Event(), asyncio.Event()
        acquire = governor.acquire_write_slot
        async def delayed(*args, **kwargs):
            ticket = await acquire(*args, **kwargs)
            waiting.set()
            await release.wait()
            return ticket
        monkeypatch.setattr(governor, "acquire_write_slot", delayed)
        async def work():
            async with transaction(client) as tx:
                return await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
        async with client._client:
            task = asyncio.create_task(work())
            await asyncio.wait_for(waiting.wait(), 5)
            await raise_global_dirty_barrier_async(client._coherence_paths(), reason_code="ambiguous_commit_outcome", transaction_id="other-task")
            release.set()
            with pytest.raises(CoherenceBarrierActiveError):
                await task
            assert account.llm_count == account.v1_count == account.list_count == 0
            assert client._insert_capability.snapshot().state == InsertCapabilityState.UNKNOWN
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["wfb", "rate"])
def test_shared_lane_errors_have_no_listing_fallback_or_capability_change(tmp_path, mode):
    from workflowy_mcp.models.errors import AccountWriteBlockedError
    async def run():
        account = Account(mode)
        client, governor, clock, _ = setup_client(tmp_path, account)
        async with client._client, transaction(client) as tx:
            with pytest.raises(AccountWriteBlockedError if mode == "wfb" else RateLimitError):
                await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
            assert account.llm_count == 1 and account.v1_count == account.list_count == 0
            assert client._insert_capability.snapshot().state == InsertCapabilityState.UNKNOWN
            if mode == "rate":
                assert (await governor.snapshot())["governor"]["next_slot_at"] >= clock.value + 3
    asyncio.run(run())


@pytest.mark.parametrize("damage", ["id", "priority", "name", "duplicate", "tie", "wrong_parent", "placement"])
def test_raw_evidence_never_manufactures_identity(damage):
    baseline = tc._order(PARENT, [row(PREDECESSOR, "left", 100), row(TAIL, "right", 300)])
    addition = row(CREATED, "ours", 200)
    rows = [*baseline.rows, addition]
    if damage in ("id", "priority", "name"):
        addition.pop(damage)
    elif damage == "duplicate": rows.append(dict(addition))
    elif damage == "tie": addition["priority"] = 100
    elif damage == "wrong_parent": addition["parent_id"] = OTHER
    else: addition["priority"] = 400
    verdict = tc.classify_create_attempt(PARENT, baseline, {"recognized": True, "rows": rows}, "ours", None, PlacementIntent.after(PREDECESSOR))
    assert verdict.classification == tc.CreateClassification.AMBIGUOUS


def test_empty_baseline_malformed_envelope_and_legacy_default(tmp_path):
    async def run():
        account = Account("malformed", empty=True)
        client, _, _, _ = setup_client(tmp_path, account)
        async with client._client:
            nodes, total = await client.list_nodes(NodeListRequest(parentId=PARENT))
            assert nodes == [] and total == 0
            evidence = {}
            await client.list_nodes(NodeListRequest(parentId=PARENT), _listing_evidence=evidence)
            assert not evidence["recognized"]
            verdict = tc.classify_create_attempt(PARENT, tc._order(PARENT, []), evidence, "ours", None, PlacementIntent.bottom())
            assert verdict.classification == tc.CreateClassification.AMBIGUOUS
    asyncio.run(run())


def test_foreign_and_inactive_hold_cannot_supply_baseline(tmp_path):
    async def run():
        account = Account()
        client, _, _, _ = setup_client(tmp_path, account)
        async with client._client:
            async with transaction(client) as tx:
                grant = tx.held_region
            with pytest.raises(CacheViewError, match="active"):
                await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
            assert not account.calls and not grant.is_active
    asyncio.run(run())


@pytest.mark.parametrize("case", ["child_set", "subtree", "wrong_region", "wrong_grant"])
def test_caller_supplied_parent_region_requires_identity_and_coverage(tmp_path, case):
    """Caller constructs the region; C still verifies its identity and real coverage."""
    async def run():
        account = Account()
        client, _, _, _ = setup_client(tmp_path, account)
        def regions(snapshot):
            parent = TAIL if case == "wrong_grant" else PARENT
            builder = build_region_subtree if case in ("subtree", "wrong_region") else build_region_child_set
            return (builder(parent, cache_by_id=snapshot.nodes_by_id),)
        async with client._client, cache_operation_boundary(client, "supplied-region"):
            async with mutation_region_locks(client=client, operation_label="supplied-region", region_factory=regions) as grant:
                requested_parent = TAIL if case == "wrong_region" else PARENT
                requested = build_region_child_set(requested_parent, cache_by_id=grant.snapshot.nodes_by_id)
                tx = tc.TransitionCreateTransaction(client, PARENT, grant, parent_region=requested)
                request = NodeCreateRequest(parent_id=PARENT, name="ours")
                if case.startswith("wrong"):
                    with pytest.raises(CacheViewError, match="child set"):
                        await tx.create(request, PlacementIntent.after(PREDECESSOR))
                    assert not account.calls
                else:
                    result = await tx.create(request, PlacementIntent.after(PREDECESSOR))
                    assert result.created_ids == (CREATED,)
                    assert grant.is_active
                    assert await tx.conclude() == result.order_witness
    asyncio.run(run())


def test_dirty_baseline_and_patch_failure_preserve_verified_result(tmp_path, monkeypatch):
    async def run():
        account = Account()
        client, _, _, _ = setup_client(tmp_path, account, dirty=True)
        def failed(*args): raise RuntimeError("publish unavailable")
        monkeypatch.setattr(client, "_apply_create_to_nodes_export_cache", failed)
        async with client._client, transaction(client) as tx:
            result = await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
            assert result.created_ids == (CREATED,)
            assert account.list_count == 2 and account.llm_count == 1
            assert PARENT in client._capture_view_and_dirty()[1]
    asyncio.run(run())


def test_real_coalescer_old_cut_survives_then_fresh_recovery_heals(tmp_path, monkeypatch):
    from workflowy_mcp import server
    from workflowy_mcp.coherence.refresh_authority import run_full_refresh
    from coherence_harness import RecordedSleep, s02_make_export
    async def run():
        account = Account("two")
        client, _, _, _ = setup_client(tmp_path, account)
        fetching, release = asyncio.Event(), asyncio.Event()
        outcomes, demands = [], []
        async def refresh(*, refresh_kind="system", require_new_fetch=False):
            demands.append((refresh_kind, require_new_fetch))
            async def fetch():
                if len(demands) == 1:
                    fetching.set()
                    await release.wait()
                return s02_make_export([row(PARENT, "parent", 100, parent=None), *account.rows])
            outcome = await run_full_refresh(paths=client._coherence_paths(), fetch=fetch,
                refresh_kind=refresh_kind, require_new_fetch=require_new_fetch, settle_seconds=0,
                snapshot_dir=tmp_path / "snapshots", sleep=RecordedSleep())
            outcomes.append(outcome)
            return {"success": True}
        async def noop(*args, **kwargs): pass
        monkeypatch.setattr(client, "refresh_nodes_export_cache", refresh)
        monkeypatch.setattr(server, "get_client", lambda: client)
        monkeypatch.setattr(server, "_emit_nodes_export_cache_status", noop)
        monkeypatch.setattr(server, "_send_ws_json_best_effort", noop)
        monkeypatch.setattr(server, "_nodes_export_refresh_lock", asyncio.Lock())
        monkeypatch.setattr(server, "_nodes_export_refresh_idle_event", asyncio.Event())
        for name, value in {"_nodes_export_refresh_task": None, "_nodes_export_refresh_state": "idle",
                "_nodes_export_refresh_rerun_requested": False, "_nodes_export_refresh_pending_reasons": [],
                "_nodes_export_refresh_pending_kinds": [], "_nodes_export_refresh_pending_new_fetch": False,
                "_nodes_export_refresh_pending_final_batches": {}}.items():
            monkeypatch.setattr(server, name, value)
        async with client._client:
            await server._request_nodes_export_cache_refresh_async("old cut", source="n03-test")
            worker = server._nodes_export_refresh_task
            await asyncio.wait_for(fetching.wait(), 5)
            try:
                async with transaction(client) as tx:
                    with pytest.raises(CoherenceBarrierActiveError):
                        await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
                    status = await hard_stop_state(client._coherence_paths())
                    assert status.hard_dirty
                    from workflowy_mcp.coherence.read_advisory import coherence_advisory_payload
                    advisory = coherence_advisory_payload(status)["coherence_advisory"]
                    assert "system.cache.refresh" in advisory["message"]
                    assert advisory["reason_code"] == "ambiguous_commit_outcome"
                    assert server._nodes_export_refresh_rerun_requested
                    assert server._nodes_export_refresh_pending_new_fetch
            finally:
                release.set()
                await worker
            assert len(outcomes) == 2
            assert not outcomes[0].hard_barrier_cleared
            assert outcomes[1].hard_barrier_cleared
            assert demands == [("system", False), ("recovery", True)]
            assert not (await hard_stop_state(client._coherence_paths())).hard_dirty
    asyncio.run(run())


def test_identical_daemon_creates_serialize_on_real_parent_region(tmp_path):
    async def run():
        account = Account("cancel_committed")
        client, _, _, _ = setup_client(tmp_path, account)
        async def work():
            async with transaction(client) as tx:
                result = await tx.create(NodeCreateRequest(parent_id=PARENT, name="same"), PlacementIntent.after(PREDECESSOR))
                await tx.conclude()
                return result
        async with client._client:
            first = create_isolated_context_task(work())
            await asyncio.wait_for(account.entered.wait(), 5)
            second = create_isolated_context_task(work())
            await asyncio.sleep(0)
            assert account.llm_count == 1
            account.release.set()
            results = await asyncio.gather(first, second)
            assert {r.created_ids[0] for r in results} == {CREATED, OTHER}
            assert account.llm_count + account.v1_count == 2
            assert len(account.ids) == len(set(account.ids)) == 2
    asyncio.run(run())


@pytest.mark.parametrize("parent", [None, "inbox"])
def test_special_parent_uses_v1_without_capability_probe(tmp_path, parent):
    from workflowy_mcp.mutation_lease_governance import ReservedMutationParent
    async def run():
        rows, sends = [], []
        async def server(request):
            if request.method == "GET": return httpx.Response(200, json=rows)
            sends.append(request.url.path)
            body = json.loads(request.content)
            rows.append(row(CREATED, html.unescape(body["name"]), 100, parent=parent))
            return httpx.Response(200, json={"item_id": CREATED})
        client, _, _, _ = setup_client(tmp_path, server_account := Account())
        await client._client.aclose()
        client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(server))
        reserved = ReservedMutationParent.ROOT if parent is None else ReservedMutationParent.INBOX
        async with client._client, cache_operation_boundary(client, "special-parent"):
            async with mutation_region_locks(client=client, operation_label="special-parent", region_factory=lambda s: (build_region_child_set(reserved, cache_by_id=s.nodes_by_id),)) as grant:
                tx = tc.TransitionCreateTransaction(client, parent, grant, parent_region=grant.regions[0])
                result = await tx.create(NodeCreateRequest(parent_id=parent, name="special"), PlacementIntent.bottom())
                assert result.transport == "v1" and result.created_ids == (CREATED,)
                assert client._insert_capability.snapshot().state == InsertCapabilityState.UNKNOWN
                assert sends and not any("llm" in path for path in sends)
    asyncio.run(run())


def test_unpopulated_cache_after_hold_keeps_verified_uuid_and_order(tmp_path):
    async def run():
        account = Account()
        client, _, _, _ = setup_client(tmp_path, account)
        async with client._client, transaction(client) as tx:
            from workflowy_mcp.coherence.cache_view import unpopulated_cache_view
            client._cache_view_publisher.publish(unpopulated_cache_view())
            result = await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
            assert result.created_ids == (CREATED,)
            assert result.order_witness.ordered_child_ids == (PREDECESSOR, CREATED, TAIL)
            assert account.llm_count == 1 and account.v1_count == 0
    asyncio.run(run())


@pytest.mark.parametrize("raw", [False, True])
def test_cancel_before_send_has_no_listing_dirt_or_barrier(tmp_path, raw):
    async def run():
        account = Account()
        client, _, _, _ = setup_client(tmp_path, account)
        async def work():
            with core.operation_cancel_scope(lambda: True):
                async with transaction(client) as tx:
                    await tx.create(NodeCreateRequest(parent_id=PARENT, name="never"), PlacementIntent.after(PREDECESSOR))
        async with client._client:
            if raw:
                task = asyncio.create_task(work())
                task.cancel()
                with pytest.raises(asyncio.CancelledError): await task
            else:
                with pytest.raises(OperationCancelledError): await work()
            assert not account.calls
            assert not client._capture_view_and_dirty()[1]
            assert not (await hard_stop_state(client._coherence_paths())).hard_dirty
    asyncio.run(run())


def test_empty_parent_malformed_post_listing_raises_after_one_v1(tmp_path, recovery):
    async def run():
        account = Account("malformed", empty=True)
        client, _, _, _ = setup_client(tmp_path, account)
        async with client._client, transaction(client) as tx:
            with pytest.raises(CoherenceBarrierActiveError):
                await tx.create(NodeCreateRequest(parent_id=PARENT, name="uncertain"), PlacementIntent.bottom())
            assert account.v1_count == account.list_count == 1 and not account.llm_count
            assert recovery == ["fresh recovery scheduled"]
    asyncio.run(run())


def test_gui_dirt_after_old_cut_survives_until_fresh_fetch(tmp_path):
    from coherence_harness import RecordedSleep, s02_make_export
    from workflowy_mcp.coherence.refresh_authority import run_full_refresh
    async def run():
        account = Account("gui")
        client, _, _, _ = setup_client(tmp_path, account)
        started, release = asyncio.Event(), asyncio.Event()
        async def fetch():
            started.set()
            await release.wait()
            return s02_make_export([row(PARENT, "parent", 100, parent=None), *account.rows])
        args = dict(paths=client._coherence_paths(), fetch=fetch, settle_seconds=0,
                    snapshot_dir=tmp_path / "snapshots", sleep=RecordedSleep())
        async with client._client:
            old = asyncio.create_task(run_full_refresh(**args))
            await asyncio.wait_for(started.wait(), 5)
            try:
                async with transaction(client) as tx:
                    await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
            finally:
                release.set()
                await old
            dirt = read_external_dirt(client._coherence_paths())
            assert dirt.active and dirt.generation == 1
            await run_full_refresh(**args, require_new_fetch=True)
            assert not read_external_dirt(client._coherence_paths()).active
    asyncio.run(run())


class PlanAccount(Account):
    """Independent v1 reference completion, not a production reorder planner."""
    def __init__(self):
        super().__init__()
        self.wires, self.moves = [], []

    async def __call__(self, request):
        if request.method == "POST":
            self.wires.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/move"):
            self.calls.append((request.method, request.url.path))
            identifier = request.url.path.split("/")[-2]
            body = json.loads(request.content)
            record = next(row for row in self.rows if row["id"] == identifier)
            self.rows.remove(record)
            self.rows.insert(0 if body["position"] == "top" else len(self.rows), record)
            for i, record in enumerate(self.rows): record["priority"] = 100 * (i + 1)
            self.moves.append(identifier)
            return httpx.Response(200, json={"status": "ok"})
        return await super().__call__(request)


async def complete_parent_plan(client, account, profile, semantic, note):
    """Test-only driver pays v1 order debt through the unchanged move API."""
    results, predecessor, debt = [], PREDECESSOR, False
    async with transaction(client) as tx:
        for index, (name, description) in enumerate(((semantic, note), ("second", None), ("third", None))):
            if profile == "C" and index == 1: account.mode = 404
            result = await tx.create(NodeCreateRequest(parent_id=PARENT, name=name, note=description), PlacementIntent.after(predecessor))
            results.append(result)
            predecessor = result.created_ids[0]
            debt |= result.reorder_required
        desired = (PREDECESSOR, *(r.created_ids[0] for r in results), TAIL)
        if debt:
            for identifier in reversed(desired):
                assert await client.move_node(identifier, PARENT, "top")
        witness = await tx.conclude(relist=debt)
        assert witness.ordered_child_ids == desired
        assert len(set(account.ids)) == len(account.ids) == 3
        assert account.ids[0] == results[0].created_ids[0]
        return tuple((row["name"], row.get("note") or "") for row in witness.rows)


from test_n03_create_transport import P2, NOTE, NOTE_WIRE
from test_n03_insert_capability import controller_mutants


@pytest.mark.parametrize("semantic,v1_wire,stored", P2)
@pytest.mark.parametrize("note,note_wire", [(None, None), ("", ""), (NOTE, NOTE_WIRE), ("?x=1&param=2", "?x=1&param=2")])
def test_profiles_a_b_c_final_order_and_independent_stored_bytes(tmp_path, semantic, v1_wire, stored, note, note_wire):
    async def run():
        signatures = []
        for profile in ("A", "B", "C"):
            account = PlanAccount()
            client, _, _, _ = setup_client(tmp_path / profile, account, forced=profile == "A")
            async with client._client:
                signatures.append(await complete_parent_plan(client, account, profile, semantic, note))
            first_path, first_body = account.wires[0]
            if profile == "A":
                assert first_body["name"] == v1_wire
                assert ("note" in first_body) == (note is not None)
                if note is not None: assert first_body["note"] == note_wire
            else:
                assert first_body["operations"][0]["items"] == [{"n": stored, **({"d": note_wire} if note else {})}]
            assert account.llm_count == {"A": 0, "B": 3, "C": 2}[profile]
            assert account.v1_count == {"A": 3, "B": 0, "C": 2}[profile]
            assert len(account.moves) == (0 if profile == "B" else 5)
        expected = (("left", ""), (stored, note_wire or ""), ("second", ""), ("third", ""), ("right", ""))
        assert signatures == [expected] * 3
    asyncio.run(run())


def test_nonmatching_gui_addition_may_intervene_before_unique_candidate():
    baseline = tc._order(PARENT, [row(PREDECESSOR, "left", 100), row(TAIL, "right", 400)])
    evidence = {"recognized": True, "rows": [row(TAIL, "right", 400), row(CREATED, "ours", 300), row(GUI, "distinct GUI", 200), row(PREDECESSOR, "left", 100)]}
    verdict = tc.classify_create_attempt(PARENT, baseline, evidence, "ours", None, PlacementIntent.after(PREDECESSOR))
    assert verdict.classification == tc.CreateClassification.COMMITTED
    assert verdict.created_ids == (CREATED,) and verdict.external_ids == (GUI,)


@pytest.mark.parametrize("variant", ["absent", "ambiguous"])
def test_schema_drift_listing_decides_absent_or_ambiguous(tmp_path, recovery, variant):
    class DriftAccount(Account):
        async def __call__(self, request):
            if request.url.path.endswith("/api/llm/doc/edit"):
                if variant == "absent":
                    self.calls.append((request.method, request.url.path));self.llm_count += 1
                else:
                    self.mode = "two"
                    await super().__call__(request)
                return httpx.Response(200, json={"required_schema_replaced": True})
            return await super().__call__(request)
    async def run():
        account = DriftAccount()
        client, _, _, _ = setup_client(tmp_path, account)
        async with client._client, transaction(client) as tx:
            request = NodeCreateRequest(parent_id=PARENT, name="ours")
            if variant == "ambiguous":
                with pytest.raises(CoherenceBarrierActiveError): await tx.create(request, PlacementIntent.after(PREDECESSOR))
                assert not account.v1_count and recovery
            else:
                result = await tx.create(request, PlacementIntent.after(PREDECESSOR))
                assert result.transport == "v1" and account.v1_count == 1
            assert account.llm_count == 1
            assert client._insert_capability.snapshot().state == InsertCapabilityState.DEGRADED_UNTIL
    asyncio.run(run())


def test_shared_breaker_refusal_then_same_governor_half_open_recovers(tmp_path):
    from workflowy_mcp.models.errors import AccountWriteBlockedError
    async def run():
        account = Account("wfb")
        client, governor, clock, _ = setup_client(tmp_path, account)
        async with client._client:
            for _ in range(2):
                async with transaction(client) as tx:
                    with pytest.raises(AccountWriteBlockedError):
                        await tx.create(NodeCreateRequest(parent_id=PARENT, name="recover"), PlacementIntent.after(PREDECESSOR))
            assert account.llm_count == 1 and account.list_count == account.v1_count == 0
            assert client._insert_capability.snapshot().state == InsertCapabilityState.UNKNOWN
            clock.advance(10)
            account.mode = "ok"
            async with transaction(client) as tx:
                result = await tx.create(NodeCreateRequest(parent_id=PARENT, name="recover"), PlacementIntent.after(PREDECESSOR))
                assert result.created_ids == (CREATED,)
            assert account.llm_count == 2 and account.v1_count == 0
            assert (await governor.snapshot())["breaker"]["state"] == "closed"
            assert client._insert_capability.snapshot().state == InsertCapabilityState.AVAILABLE
    asyncio.run(run())


def test_governor_deferral_before_send_is_not_transport_unavailability(tmp_path):
    from workflowy_mcp.models.errors import WriteGovernorDeferredError
    async def run():
        account = Account()
        client, governor, _, _ = setup_client(tmp_path, account)
        await governor.defer_global_writes_until(100000, operation="test-floor")
        async with client._client, transaction(client) as tx:
            with pytest.raises(WriteGovernorDeferredError):
                await tx.create(NodeCreateRequest(parent_id=PARENT, name="never"), PlacementIntent.after(PREDECESSOR))
            assert not account.calls
            assert client._insert_capability.snapshot().state == InsertCapabilityState.UNKNOWN
    asyncio.run(run())


def test_busy_probe_other_parent_finishes_v1_on_same_client(tmp_path):
    async def run():
        pred2, tail2, created2 = (str(UUID(int=UUID(PREDECESSOR).int + 1)), str(UUID(int=UUID(TAIL).int + 1)), str(UUID(int=UUID(CREATED).int + 100)))
        records = {PARENT: [row(PREDECESSOR, "left", 100), row(TAIL, "right", 200)],
                   OTHER: [row(pred2, "left2", 100, parent=OTHER), row(tail2, "right2", 200, parent=OTHER)]}
        ready, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def server(request):
            calls.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(200, json=records[request.url.params["parent_id"]])
            body = json.loads(request.content)
            llm = request.url.path.endswith("/api/llm/doc/edit")
            parent = (PARENT if body["root"] == UUID(PARENT).hex[-12:] else OTHER) if llm else body["parent_id"]
            identifier = CREATED if parent == PARENT else created2
            if llm:
                operation = body["operations"][0]
                name = operation["items"][0]["n"]
                index = next(i for i, r in enumerate(records[parent]) if r["id"] == operation["after"]) + 1
            else:
                name, index = html.unescape(body["name"]), len(records[parent])
            records[parent].insert(index, row(identifier, name, 0, parent=parent))
            for i, record in enumerate(records[parent]): record["priority"] = 100 * (i + 1)
            if parent == PARENT:
                ready.set();await release.wait()
            return httpx.Response(200, json={"results": [{"op": "insert", "status": "ok", "created": 1}]} if llm else {"item_id": identifier})
        client, _, _, _ = setup_client(tmp_path, Account())
        before = client._cache_view_publisher.capture()
        client._cache_view_publisher.publish(CacheView.build(epoch_number=0, epoch_id=before.epoch_id, adopted_sequence=0,
            nodes_export={"nodes": [row(PARENT, "parent", 100, parent=None), row(OTHER, "parent2", 200, parent=None), *records[PARENT], *records[OTHER]]}))
        await client._client.aclose()
        client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(server))
        async def create(parent, predecessor, name):
            async with cache_operation_boundary(client, name):
                async with mutation_region_locks(client=client, operation_label=name, region_factory=lambda s: (build_region_child_set(parent, cache_by_id=s.nodes_by_id),)) as grant:
                    tx = tc.TransitionCreateTransaction(client, parent, grant, parent_region=grant.regions[0])
                    return await tx.create(NodeCreateRequest(parent_id=parent, name=name), PlacementIntent.after(predecessor))
        async with client._client:
            first = create_isolated_context_task(create(PARENT, PREDECESSOR, "first"))
            await asyncio.wait_for(ready.wait(), 5)
            try:
                second = await asyncio.wait_for(create_isolated_context_task(create(OTHER, pred2, "second")), 5)
                assert second.transport == "v1" and second.created_ids == (created2,)
                assert client._insert_capability.snapshot().state == InsertCapabilityState.HALF_OPEN_PROBE
            finally:
                release.set()
                result = await first
            assert result.transport == "llm" and result.created_ids == (CREATED,)
            assert client._insert_capability.snapshot().state == InsertCapabilityState.AVAILABLE
            assert sum(path.endswith("/api/llm/doc/edit") for method, path in calls if method == "POST") == 1
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["network", "timeout"])
def test_final_v1_unknown_outcome_still_takes_mandatory_listing(tmp_path, monkeypatch, failure):
    """V3 applies after the legacy invocation, not between its hidden retries."""
    from workflowy_mcp.models.errors import NetworkError, TimeoutError as WorkflowyTimeoutError
    class LostV1Reply(Account):
        async def __call__(self, request):
            response = await super().__call__(request)
            if request.method == "POST" and request.url.path.endswith("/nodes/"):
                if failure == "timeout": raise httpx.ReadTimeout("reply lost after stored create")
                return httpx.Response(200, json={"reply_without_item_id": True})
            return response
    async def run():
        account = LostV1Reply()
        client, _, _, _ = setup_client(tmp_path, account, forced=True)
        original = client.create_node
        async def one_legacy_attempt(request, **kwargs):
            # Test-process injection only: isolate the FINAL error of the real
            # legacy invocation without manufacturing ten inherited duplicates.
            return await original(request, max_retries=1, **kwargs)
        monkeypatch.setattr(client, "create_node", one_legacy_attempt)
        async with client._client, transaction(client) as tx:
            try:
                await tx.create(NodeCreateRequest(parent_id=PARENT, name="stored"), PlacementIntent.after(PREDECESSOR))
            except (NetworkError, WorkflowyTimeoutError):
                pass
            assert any(record["id"] == CREATED for record in account.rows)
            assert account.v1_count == 1 and account.llm_count == 0
            assert account.list_count == 1, "possibly-committed final v1 error skipped V3's mandatory post-listing"
    asyncio.run(run())


class V1FinalFaultAccount(Account):
    """Independent final-v1 outcome model; one invocation's last send only."""
    def __init__(self, fault, outcome):
        super().__init__()
        self.fault, self.outcome = fault, outcome

    async def __call__(self, request):
        if request.method == "GET" and self.outcome == "unreliable" and self.v1_count:
            self.calls.append((request.method, request.url.path));self.list_count += 1
            return httpx.Response(200, json={"unrecognized": []})
        if request.method == "POST" and request.url.path.endswith("/nodes/"):
            if isinstance(self.fault, int):
                self.calls.append((request.method, request.url.path));self.v1_count += 1
                return httpx.Response(self.fault, json={"error": "definitive refusal"}, headers={"Retry-After": "1"})
            if self.outcome == "absent":
                self.calls.append((request.method, request.url.path));self.v1_count += 1
            else:
                await super().__call__(request)
                if self.outcome == "two":
                    self.rows.append(row(GUI, self.rows[-1]["name"], 400, note=self.rows[-1].get("note")))
            if self.fault == "timeout": raise httpx.ReadTimeout("last v1 reply lost")
            if self.fault == "transport": raise httpx.ReadError("last v1 connection lost")
            if self.fault == "json": return httpx.Response(200, content=b"not-json")
            if self.fault == "array": return httpx.Response(200, json=[])
            if self.fault == "null": return httpx.Response(200, content=b"null")
            if self.fault == "empty_id": return httpx.Response(200, json={"item_id": {}})
            if self.fault == "utf8": return httpx.Response(200, content=b"\xff")
            return httpx.Response(200, json={"accepted_without_item_id": True})
        return await super().__call__(request)


def one_final_v1_attempt(monkeypatch, client):
    """Test-only retry isolation preserves the real client and original error."""
    original, errors = client.create_node, []
    async def invoke(request, **kwargs):
        try:
            return await original(request, max_retries=1, **kwargs)
        except Exception as exc:
            errors.append(exc)
            raise
    monkeypatch.setattr(client, "create_node", invoke)
    return errors


@pytest.mark.parametrize("fault", ["network", "timeout", "transport", "json", "array", "null", "empty_id", "utf8"])
@pytest.mark.parametrize("outcome", ["committed", "absent", "two", "unreliable"])
def test_final_v1_error_uses_frozen_outcomes_without_another_invocation(tmp_path, monkeypatch, recovery, fault, outcome):
    from workflowy_mcp.models.errors import NetworkError, TimeoutError as WorkflowyTimeoutError
    async def run():
        account = V1FinalFaultAccount(fault, outcome)
        client, _, _, _ = setup_client(tmp_path, account, forced=True)
        errors = one_final_v1_attempt(monkeypatch, client)
        async with client._client, transaction(client) as tx:
            request, placement = NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR)
            if outcome == "committed":
                result = await tx.create(request, placement)
                assert result.transport == "v1" and result.created_ids == (CREATED,)
                assert result.order_witness.ordered_child_ids == (PREDECESSOR, TAIL, CREATED)
                assert result.reorder_required
                view, dirty = client._capture_view_and_dirty()
                assert CREATED in view.nodes_by_id and PARENT in dirty
                assert sum(r["id"] == CREATED for r in view.nodes_export["nodes"]) == 1
            elif outcome == "absent":
                with pytest.raises(Exception) as caught: await tx.create(request, placement)
                assert caught.value is errors[0], "absence replaced the original legacy error"
            else:
                with pytest.raises(CoherenceBarrierActiveError): await tx.create(request, placement)
                assert recovery == ["fresh recovery scheduled"]
                assert (await hard_stop_state(client._coherence_paths())).hard_dirty
            assert len(errors) == 1
            assert account.v1_count == account.list_count == 1 and account.llm_count == 0
            if outcome in ("absent", "committed"):
                assert not recovery and not (await hard_stop_state(client._coherence_paths())).hard_dirty
    asyncio.run(run())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
def test_definitive_v1_4xx_preserves_original_error_without_listing(tmp_path, monkeypatch, recovery, status):
    from workflowy_mcp.models.errors import WorkFlowyError
    async def run():
        account = V1FinalFaultAccount(status, "absent")
        client, _, _, _ = setup_client(tmp_path, account, forced=True)
        errors = one_final_v1_attempt(monkeypatch, client)
        async with client._client, transaction(client) as tx:
            with pytest.raises(WorkFlowyError) as caught:
                await tx.create(NodeCreateRequest(parent_id=PARENT, name="refused"), PlacementIntent.after(PREDECESSOR))
            assert caught.value is errors[0]
            assert account.v1_count == 1 and account.llm_count == account.list_count == 0
            assert not recovery and not (await hard_stop_state(client._coherence_paths())).hard_dirty
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [("name", " "), ("note", 42)])
def test_definitive_legacy_input_validation_network_error_is_not_unknown(tmp_path, monkeypatch, recovery, field, value):
    from workflowy_mcp.models.errors import NetworkError
    async def run():
        account = Account()
        client, _, _, _ = setup_client(tmp_path, account, forced=True)
        errors = one_final_v1_attempt(monkeypatch, client)
        request = NodeCreateRequest(parent_id=PARENT, name="valid")
        setattr(request, field, value)  # Isolate the legacy pure-validation refusal.
        async with client._client, transaction(client) as tx:
            with pytest.raises(NetworkError) as caught: await tx.create(request, PlacementIntent.after(PREDECESSOR))
            assert caught.value is errors[0]
            assert not account.calls and not recovery
            assert not (await hard_stop_state(client._coherence_paths())).hard_dirty
    asyncio.run(run())


@pytest.mark.parametrize("wrong_id", [OTHER, {"unusable": True}])
def test_acknowledged_v1_id_cannot_override_contrary_listing(tmp_path, recovery, wrong_id):
    class WrongId(Account):
        async def __call__(self, request):
            response = await super().__call__(request)
            if request.method == "POST" and request.url.path.endswith("/nodes/"):
                return httpx.Response(200, json={"item_id": wrong_id})
            return response
    async def run():
        account = WrongId()
        client, _, _, _ = setup_client(tmp_path, account, forced=True)
        async with client._client, transaction(client) as tx:
            with pytest.raises(CoherenceBarrierActiveError):
                await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
            assert account.v1_count == account.list_count == 1 and not account.llm_count
            assert recovery == ["fresh recovery scheduled"]
    asyncio.run(run())


async def _assert_sequential_parent_baseline(tmp_path, monkeypatch, recovery, *, second_mode,
                                           identical=False, first_kind="llm", same_transaction=False):
    """Port of adjudicator F1: two real creates share one operation and N05 grant."""
    account, phase = Account(), {"first": True}
    async def server(request):
        llm = request.url.path.endswith("/api/llm/doc/edit")
        if not phase["first"] and request.method == "POST":
            if second_mode in ("transport_absent", "schema_absent", "v1_unknown_absent") and (llm or second_mode == "v1_unknown_absent"):
                account.calls.append((request.method, request.url.path))
                if llm:
                    account.llm_count += 1
                else:
                    account.v1_count += 1
                if second_mode == "transport_absent":
                    raise httpx.ConnectError("no insert landed")
                return httpx.Response(200, json={"accepted_without_identity": True})
        response = await account(request)
        if phase["first"] and first_kind == "v1_unknown" and request.method == "POST":
            return httpx.Response(200, json={"accepted_without_item_id": True})
        return response
    client, _, _, _ = setup_client(tmp_path, account)
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(server))
    errors = one_final_v1_attempt(monkeypatch, client)
    patch = client._apply_create_to_nodes_export_cache
    async with client._client, transaction(client) as first:
        if first_kind == "patch_failed":
            def failed(*args): raise RuntimeError("eager publication failed")
            monkeypatch.setattr(client, "_apply_create_to_nodes_export_cache", failed)
        elif first_kind == "unpopulated":
            from workflowy_mcp.coherence.cache_view import unpopulated_cache_view
            client._cache_view_publisher.publish(unpopulated_cache_view())
        placement = PlacementIntent.bottom() if first_kind.startswith("v1") else PlacementIntent.after(PREDECESSOR)
        first_result = await first.create(NodeCreateRequest(parent_id=PARENT, name="first"), placement)
        assert first_result.created_ids == (CREATED,)
        assert first_result.transport == ("v1" if first_kind.startswith("v1") else "llm")
        if not same_transaction:
            await first.conclude()
        monkeypatch.setattr(client, "_apply_create_to_nodes_export_cache", patch)
        before_dirt = read_external_dirt(client._coherence_paths(), required=False)
        lists_before, v1_before, llm_before = account.list_count, account.v1_count, account.llm_count
        phase["first"] = False
        account.mode = second_mode if second_mode not in ("transport_absent", "schema_absent", "v1_unknown_absent") else "ok"
        second = first if same_transaction else tc.TransitionCreateTransaction(
            client, PARENT, first.held_region, parent_region=first.parent_region)
        request = NodeCreateRequest(parent_id=PARENT, name="first" if identical else "second")
        placement = PlacementIntent.bottom() if second_mode == "v1_unknown_absent" else PlacementIntent.after(PREDECESSOR if identical else TAIL)
        if second_mode == "v1_unknown_absent":
            with pytest.raises(core.NetworkError) as caught:
                await second.create(request, placement)
            assert caught.value is errors[-1]
            assert account.ids == [CREATED]
        else:
            result = await second.create(request, placement)
            assert result.classification == tc.CreateClassification.COMMITTED
            assert result.created_ids == (OTHER,) and result.created_ids != first_result.created_ids
            assert result.transport == ("llm" if second_mode == "ok" else "v1")
            assert result.reorder_required == (second_mode != "ok")
            assert account.ids == [CREATED, OTHER]
            assert await second.conclude() == result.order_witness
        after_dirt = read_external_dirt(client._coherence_paths(), required=False)
        assert after_dirt.generation == before_dirt.generation and not after_dirt.active
        assert account.llm_count - llm_before == int(second_mode != "v1_unknown_absent")
        assert account.v1_count - v1_before == int(second_mode != "ok")
        post_lists = 1 if second_mode in ("ok", "v1_unknown_absent") else 2
        assert account.list_count - lists_before == post_lists + int(not same_transaction)
        assert not recovery and not (await hard_stop_state(client._coherence_paths())).hard_dirty
        assert first.held_region.is_active


@pytest.mark.parametrize("identical", [False, True])
@pytest.mark.parametrize("second_mode", [404, 405, 410, 501, 307, 308, "op_error", "read_required", "absent", "transport_absent", "schema_absent", "ok", "v1_unknown_absent"])
def test_later_transaction_under_same_grant_uses_current_parent_baseline(tmp_path, monkeypatch, recovery, identical, second_mode):
    """WO-1 T-a/b/c: earlier verified daemon IDs are never new identity or GUI dirt."""
    asyncio.run(_assert_sequential_parent_baseline(tmp_path, monkeypatch, recovery,
                                                 second_mode=second_mode, identical=identical))


@pytest.mark.parametrize("first_kind", ["v1", "v1_unknown", "patch_failed", "unpopulated"])
@pytest.mark.parametrize("second_mode", ["ok", "absent"])
def test_later_transaction_after_verified_commit_publication_variants(tmp_path, monkeypatch, recovery, first_kind, second_mode):
    """WO-1 X1/C21: both transports and failed/unpopulated patches obey V6."""
    asyncio.run(_assert_sequential_parent_baseline(tmp_path, monkeypatch, recovery,
                                                 first_kind=first_kind, second_mode=second_mode))


@pytest.mark.parametrize("second_mode", ["ok", 404])
def test_same_transaction_reuses_post_listing_without_another_pre_listing(tmp_path, monkeypatch, recovery, second_mode):
    """WO-1 T-d: V2 still costs one post-listing per accepted/unknown attempt."""
    asyncio.run(_assert_sequential_parent_baseline(tmp_path, monkeypatch, recovery,
                                                 second_mode=second_mode, same_transaction=True))
