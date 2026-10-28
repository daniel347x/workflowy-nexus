"""N05 current-proof/stable-execution witnesses and sensitive A1b task races."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

import pytest

from n07_job_harness import scrubbed_runtime
from n05_region_harness import LookupOnlyMap, graph_cache, node_uuid, region_client
from workflowy_mcp.coherence import operation_cache as operations
from workflowy_mcp.coherence.cache_view import CacheView
from workflowy_mcp.coherence.errors import CacheViewError
from workflowy_mcp import mutation_locks as locks
from workflowy_mcp import mutation_lease_governance as regions


def _regions(*requests):
    builders = {"exact_node": regions.build_region_exact_node, "child_set": regions.build_region_child_set, "subtree": regions.build_region_subtree}
    return lambda snapshot: tuple(builders[kind](node_uuid(label), cache_by_id=snapshot.nodes_by_id) for kind, label in requests)


def _publish_parent(client, label, parent):
    publisher = client._cache_view_publisher
    before = publisher.capture()
    nodes = {**before.nodes_by_id}
    nodes[node_uuid(label)] = {**nodes[node_uuid(label)], "parent_id": node_uuid(parent)}
    after = CacheView.build(
        epoch_number=before.epoch_number, epoch_id=before.epoch_id,
        adopted_sequence=before.adopted_sequence,
        nodes_export={"nodes": list(nodes.values())},
        account_scope_id=before.account_scope_id,
    )
    publisher.publish(after)
    return after


@pytest.mark.asyncio
async def test_w4_establish_reuses_boundary_without_yield_or_adoption(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    async with operations.cache_operation_boundary(client, "w4") as boundary:
        def forbidden(*args, **kwargs):
            raise AssertionError("admission adopted or ran an unnecessary cancellation check")
        resumed = []
        asyncio.get_running_loop().call_soon(resumed.append, True)
        assert await operations.establish_admission_snapshot(client, "different-label", cancel_check=forbidden) is boundary
        assert resumed == []
    with pytest.raises(CacheViewError, match="same-authority"):
        await operations.establish_admission_snapshot(client, "no-boundary")


@pytest.mark.asyncio
async def test_w4_proof_only_capture_preserves_execution_and_boundary_context(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    status, archive = object(), object()
    client._coherence_boundary_status = lambda: status
    async with operations.cache_operation_boundary(client, "w4") as boundary:
        with operations.archive_operation_scope(archive):
            basis = operations.current_operation_cache()
            latest = _publish_parent(client, "p", "g")
            def forbidden(*args, **kwargs):
                raise AssertionError("proof snapshot adopted or reread boundary status")
            monkeypatch.setattr(client, "_coherence_boundary_status", forbidden)
            proof = await operations.rebase_during_admission(client, "proof", install=False)
            assert proof.view is latest and proof.view is not boundary.view
            assert (proof.operation_id, proof.label, proof.owner) == (basis.operation_id, basis.label, basis.owner)
            assert proof.archive is archive and proof.boundary_status is status
            assert proof.nodes_by_id is latest.nodes_by_id
            assert proof.nodes_export is latest.nodes_export
            assert operations.current_operation_cache() is basis


@pytest.mark.asyncio
async def test_w4_exact_proof_install_never_recaptures(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    async with operations.cache_operation_boundary(client, "w4"):
        first = _publish_parent(client, "p", "g")
        proof = await operations.rebase_during_admission(client, "proof", install=False)
        _publish_parent(client, "p", "a")
        def forbidden(*args, **kwargs):
            raise AssertionError("validated proof was recaptured")
        monkeypatch.setattr(client, "_capture_view_and_dirty", forbidden)
        installed = await operations.rebase_during_admission(client, "install", snapshot=proof, install=True)
        assert installed is proof
        assert operations.current_operation_cache() is proof
        assert proof.view is first


@pytest.mark.asyncio
async def test_w4_cancellation_check_precedes_current_capture(tmp_path):
    client = region_client(tmp_path)
    latest = None
    async with operations.cache_operation_boundary(client, "w4") as basis:
        async def yielding_check():
            nonlocal latest
            await asyncio.sleep(0)
            latest = _publish_parent(client, "p", "g")
        proof = await operations.rebase_during_admission(client, "proof", install=False, cancel_check=yielding_check)
        assert proof.view is latest
        assert operations.current_operation_cache() is basis


@pytest.mark.asyncio
async def test_w4_foreign_proof_and_authority_never_install(tmp_path):
    client, foreign = region_client(tmp_path / "own"), region_client(tmp_path / "other")
    async with operations.cache_operation_boundary(client, "w4") as basis:
        for bad in (replace(basis, operation_id="foreign-operation"), replace(basis, owner=object())):
            with pytest.raises(CacheViewError, match="not a proof"):
                await operations.rebase_during_admission(client, "bad", snapshot=bad)
            assert operations.current_operation_cache() is basis
        with pytest.raises(CacheViewError):
            await operations.establish_admission_snapshot(foreign, "bad")
        with pytest.raises(CacheViewError):
            operations._capture_current_admission_snapshot(foreign, basis)


@pytest.mark.asyncio
async def test_w4_capture_never_iterates_the_cache(tmp_path):
    client = region_client(tmp_path)
    async with operations.cache_operation_boundary(client, "w4") as basis:
        publisher = client._cache_view_publisher
        counted = LookupOnlyMap(publisher.capture().nodes_by_id)
        latest = replace(publisher.capture(), nodes_by_id=counted)
        publisher.publish(latest)
        proof = await operations.rebase_during_admission(client, "proof", install=False)
        assert proof.nodes_by_id is counted
        assert counted.lookups == 0
        assert operations.current_operation_cache() is basis
        del client._capture_view_and_dirty
        fallback = operations._capture_current_admission_snapshot(client, basis)
        assert fallback.view is latest and fallback.dirty_ids == frozenset()


@pytest.mark.asyncio
async def test_w4_no_repin_sensitivity(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    original = operations.rebase_during_admission
    async with operations.cache_operation_boundary(client, "w4") as basis:
        _publish_parent(client, "p", "g")
        async def witness():
            await operations.rebase_during_admission(client, "proof", install=False)
            assert operations.current_operation_cache() is basis, "proof-only admission repinned execution"
        await witness()
        async def wrong_install(*args, **kwargs):
            kwargs["install"] = True
            return await original(*args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(operations, "rebase_during_admission", wrong_install)
            # The reset belongs to this test's simulated bad implementation.
            token = operations._OPERATION_CACHE_SNAPSHOT.set(basis)
            try:
                with pytest.raises(AssertionError, match="proof-only admission repinned execution"):
                    await witness()
            finally:
                operations._OPERATION_CACHE_SNAPSHOT.reset(token)
        await witness()


@pytest.mark.asyncio
async def test_a1b_holder_waiter_mover(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    capture_entered, mover_entered, delete_waiting, rescanned_capture = (asyncio.Event() for _ in range(4))
    capture_release, mover_publish, delete_entered = (asyncio.Event() for _ in range(3))
    decided = asyncio.Event()
    scan = locks._scan_region_blockers
    wait = locks._wait_for_region_change
    def observe(regions_arg, **kwargs):
        result = scan(regions_arg, **kwargs)
        if any(r.kind == "subtree" and r.key == node_uuid("g") for r in regions_arg):
            if kwargs["snapshot"].nodes_by_id[node_uuid("p")]["parent_id"] == node_uuid("g"):
                capture_is_live = any(any(r.kind == "child_set" and r.key == node_uuid("p") for r in h.regions) for h in locks._LOCKS.holders.values())
                if result and capture_is_live:
                    rescanned_capture.set()
                    decided.set()
        return result
    async def waiting(waiter, **kwargs):
        if waiter.state.operation_id == delete_operation[0]:
            delete_waiting.set()
        return await wait(waiter, **kwargs)
    monkeypatch.setattr(locks, "_scan_region_blockers", observe)
    monkeypatch.setattr(locks, "_wait_for_region_change", waiting)
    delete_operation = []
    async def capture():
        async with operations.cache_operation_boundary(client, "capture"):
            async with locks.mutation_region_locks(client=client, operation_label="capture", region_factory=_regions(("child_set", "p"))):
                capture_entered.set()
                await capture_release.wait()
    async def mover():
        async with operations.cache_operation_boundary(client, "mover"):
            async with locks.mutation_region_locks(client=client, operation_label="mover", region_factory=_regions(("exact_node", "p"), ("child_set", "a"), ("child_set", "g"))):
                mover_entered.set()
                await mover_publish.wait()
                _publish_parent(client, "p", "g")
    async def delete():
        async with operations.cache_operation_boundary(client, "delete") as boundary:
            delete_operation.append(boundary.operation_id)
            async with locks.mutation_region_locks(client=client, operation_label="delete", region_factory=_regions(("subtree", "g"), ("child_set", "vault"))):
                delete_entered.set()
                decided.set()
    c = operations.create_isolated_context_task(capture())
    await capture_entered.wait()
    m = operations.create_isolated_context_task(mover())
    await mover_entered.wait()  # MOVE and capture intentionally coexist
    d = operations.create_isolated_context_task(delete())
    try:
        await asyncio.wait_for(delete_waiting.wait(), 2)
        mover_publish.set()
        await m
        await asyncio.wait_for(decided.wait(), 2)
        assert not delete_entered.is_set(), "DELETE missed the moved live capture"
        assert rescanned_capture.is_set()
        capture_release.set()
        await c
        await asyncio.wait_for(d, 2)
        assert delete_entered.is_set()
    finally:
        capture_release.set()
        mover_publish.set()
        for task in (c, m, d):
            if not task.done():
                task.cancel()
        await asyncio.gather(c, m, d, return_exceptions=True)
    assert not locks._LOCKS.holders and not locks._LOCKS.waiters


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_first", [True, False])
async def test_a1b_ordered_delete_and_mover_exclude(tmp_path, monkeypatch, delete_first):
    client = region_client(tmp_path)
    first_entered, second_entered, registered, release = (asyncio.Event() for _ in range(4))
    original = locks._wait_for_region_change
    async def waiting(waiter, **kwargs):
        registered.set()
        return await original(waiter, **kwargs)
    monkeypatch.setattr(locks, "_wait_for_region_change", waiting)
    async def actor(is_delete, entered, wait_release):
        requested = (("subtree", "g"), ("child_set", "vault")) if is_delete else (("exact_node", "p"), ("child_set", "a"), ("child_set", "g"))
        async with operations.cache_operation_boundary(client, str(is_delete)):
            async with locks.mutation_region_locks(client=client, operation_label=str(is_delete), region_factory=_regions(*requested)):
                entered.set()
                if wait_release:
                    await release.wait()
    first = operations.create_isolated_context_task(actor(delete_first, first_entered, True))
    await first_entered.wait()
    second = operations.create_isolated_context_task(actor(not delete_first, second_entered, False))
    try:
        await asyncio.wait_for(registered.wait(), 2)
        assert not second_entered.is_set()
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert second_entered.is_set()
    finally:
        release.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("second_change", [False, True])
async def test_changed_footprint_reacquires_once(tmp_path, monkeypatch, second_change):
    client = region_client(tmp_path)
    original = locks._acquire_region_set
    attempts = []
    async def advanced(requested, **kwargs):
        result = await original(requested, **kwargs)
        attempts.append((tuple(r.key for r in requested), kwargs["deadline"]))
        if len(attempts) == 1:
            _publish_parent(client, "p", "g")
        elif second_change:
            _publish_parent(client, "p", "vault")
        return result
    monkeypatch.setattr(locks, "_acquire_region_set", advanced)
    def factory(snapshot):
        return (
            regions.build_region_exact_node(node_uuid("p"), cache_by_id=snapshot.nodes_by_id),
            regions.build_region_child_set(regions.mutation_parent_identity(node_uuid("p"), cache_by_id=snapshot.nodes_by_id), cache_by_id=snapshot.nodes_by_id),
        )
    async with operations.cache_operation_boundary(client, "changed-footprint"):
        async def witness():
            async with locks.mutation_region_locks(client=client, operation_label="changed-footprint", region_factory=factory):
                assert not second_change
                assert node_uuid("a") not in locks.registry_held_keys()
                assert locks.registry_held_keys() == {node_uuid("p"), node_uuid("g")}
        if second_change:
            with pytest.raises(regions.MutationRegionError) as info:
                await witness()
            assert info.value.reason == "unstable_topology"
        else:
            await witness()
    assert len(attempts) == 2 and attempts[0][1] == attempts[1][1]
    assert not locks._LOCKS.holders and not locks._LOCKS.waiters


@pytest.mark.asyncio
async def test_first_admission_installs_then_execution_stays_pinned(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    original = locks._acquire_region_set
    calls = []
    async def advanced(requested, **kwargs):
        result = await original(requested, **kwargs)
        if not calls:
            calls.append(_publish_parent(client, "q", "a"))
        return result
    monkeypatch.setattr(locks, "_acquire_region_set", advanced)
    async with operations.cache_operation_boundary(client, "first-admission") as old:
        async with locks.mutation_region_locks(client=client, operation_label="first", region_factory=_regions(("exact_node", "p"))) as outer:
            finalized = operations.current_operation_cache()
            assert finalized.view is calls[0] and finalized.view is not old.view
            later = _publish_parent(client, "q", "g")
            async with locks.mutation_region_locks(client=client, operation_label="nested", region_factory=_regions(("exact_node", "p"))) as inner:
                assert inner.token is None and inner.snapshot.view is later
                assert operations.current_operation_cache() is finalized
            assert outer.is_active


@pytest.mark.asyncio
@pytest.mark.parametrize("neutralization", ["view_identity", "stored_chains", "former_blocker_only", "equal_candidate_keys"])
async def test_a1b_neutralizations(tmp_path, monkeypatch, neutralization):
    """Each specific lost-overlap mutation reaches D's body, not a timeout."""
    real_client = region_client
    real_intersect = locks.regions_intersect
    real_scan = locks._scan_region_blockers

    with monkeypatch.context() as patch:
        if neutralization == "view_identity":
            def with_stale_capture(path):
                client = real_client(path)
                publisher = client._cache_view_publisher
                capture = publisher.capture
                old = capture()
                def capture_without_advance():
                    snapshot = operations.current_operation_cache(required=False)
                    return old if snapshot is not None and snapshot.label == "delete" else capture()
                patch.setattr(publisher, "capture", capture_without_advance)
                return client
            patch.setattr(sys.modules[__name__], "region_client", with_stale_capture)
        elif neutralization == "stored_chains":
            def stored(a, b, *, cache_by_id=None, _chain_memo=None):
                if a.kind == "subtree" and b.kind in {"exact_node", "child_set", "subtree"}:
                    return a.key in b.ancestor_chain or (b.kind == "subtree" and b.key in a.ancestor_chain)
                if b.kind == "subtree" and a.kind in {"exact_node", "child_set"}:
                    return b.key in a.ancestor_chain
                return real_intersect(a, b, cache_by_id=cache_by_id, _chain_memo=_chain_memo)
            patch.setattr(locks, "regions_intersect", stored)
        else:
            def incomplete(requested, **kwargs):
                is_delete = kwargs["snapshot"].label == "delete"
                fresh = kwargs["snapshot"].nodes_by_id[node_uuid("p")]["parent_id"] == node_uuid("g")
                if is_delete and fresh:
                    if neutralization == "equal_candidate_keys":
                        # CF33 moves the first fresh proof scan before grant.
                        # Omit it as well as the old post-grant rescan when D's
                        # kind/key tuple stayed equal (GLYSXW / LE1PTI).
                        return ()
                    if neutralization == "former_blocker_only":
                        before = locks._LOCKS.holders
                        # The former MOVE blocker is gone; omit the still-live C.
                        locks._LOCKS.holders = {token: hold for token, hold in before.items()
                            if any(r.kind == "exact_node" and r.key == node_uuid("p") for r in hold.regions)}
                        try:
                            return real_scan(requested, **kwargs)
                        finally:
                            locks._LOCKS.holders = before
                return real_scan(requested, **kwargs)
            patch.setattr(locks, "_scan_region_blockers", incomplete)
        with pytest.raises(AssertionError, match="DELETE missed the moved live capture"):
            await test_a1b_holder_waiter_mover(tmp_path / "missing", patch)
    await test_a1b_holder_waiter_mover(tmp_path / "restored", monkeypatch)


@pytest.mark.asyncio
async def test_same_marker_publication_revalidates_all_holders(tmp_path, monkeypatch):
    publish = _publish_parent
    seen = []
    def same_marker(client, label, parent):
        before = client._cache_view_publisher.capture()
        after = publish(client, label, parent)
        assert after is not before
        assert (after.epoch_number, after.epoch_id, after.adopted_sequence) == (before.epoch_number, before.epoch_id, before.adopted_sequence)
        seen.append(after)
        return after
    monkeypatch.setattr(sys.modules[__name__], "_publish_parent", same_marker)
    await test_a1b_holder_waiter_mover(tmp_path, monkeypatch)
    assert len(seen) == 1


async def _fresh_tag_witness(tmp_path, monkeypatch, *, transitive):
    from test_mutation_regions_n05_fences import real_region_probe
    from test_mutation_serialization import dispatch
    from workflowy_mcp import server
    from workflowy_mcp.models.requests import NodeCreateRequest
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    fresh_known, allow_tag, tag_waiting, tag_finished, tag_decided = (asyncio.Event() for _ in range(5))
    other_ready, release_other, editor_ready, release_editor = (asyncio.Event() for _ in range(4))
    tickets = {"fresh-w1": asyncio.Event(), "fresh-w2": asyncio.Event()}
    fresh, other, ticket_operations = [], [], []
    add_tag = client.workflowy_add_tag

    async def pause_before_tag(node_id, *args, **kwargs):
        fresh.append(node_id)
        assert node_id in client._cache_view_publisher.capture().nodes_by_id
        fresh_known.set()
        await allow_tag.wait()
        result = await add_tag(node_id, *args, **kwargs)
        tag_finished.set()
        tag_decided.set()
        return result

    monkeypatch.setattr(client, "workflowy_add_tag", pause_before_tag)
    wait = locks._wait_for_region_change

    async def observed(waiter, **kwargs):
        if waiter.snapshot.label in tickets:
            tickets[waiter.snapshot.label].set()
        if fresh and len(waiter.regions) == 1 and waiter.regions[0].kind == "exact_node" and waiter.regions[0].key == fresh[0]:
            tag_waiting.set()
            if release_editor.is_set():
                tag_decided.set()  # A wrong fairness policy queued the tag again.
        return await wait(waiter, **kwargs)

    monkeypatch.setattr(locks, "_wait_for_region_change", observed)

    async def creator():
        return await dispatch(server.build_anchor_registry(), "point.capture", {
            "parent_handle": ids["dest"], "name": "Fresh", "content": "body", "tags": ["new"], "ap_id": None,
        })

    async def other_creator():
        async with operations.cache_operation_boundary(client, "other-creator"):
            async with locks.mutation_region_locks(client=client, operation_label="other-creator",
                    region_factory=lambda s: (regions.build_region_child_set(ids["origin"], cache_by_id=s.nodes_by_id),)):
                made = await client.create_node(NodeCreateRequest(name="M", parent_id=ids["origin"]), _internal_call=True)
                other.append(made.id)
                other_ready.set()
                await release_other.wait()

    async def editor():
        async with operations.cache_operation_boundary(client, "exact-only-editor"):
            async with locks.mutation_node_locks(fresh[0], client=client):
                editor_ready.set()
                await release_editor.wait()

    async def ticket(label, requests):
        async with operations.cache_operation_boundary(client, label) as snapshot:
            ticket_operations.append(snapshot.operation_id)
            async with locks.mutation_region_locks(client=client, operation_label=label,
                    region_factory=lambda s: tuple((regions.build_region_child_set if kind == "child_set" else regions.build_region_exact_node)(key, cache_by_id=s.nodes_by_id) for kind, key in requests)):
                return True

    tasks = []
    try:
        a = operations.create_isolated_context_task(creator())
        tasks.append(a)
        await asyncio.wait_for(fresh_known.wait(), 10)
        if transitive:
            o = operations.create_isolated_context_task(other_creator())
            tasks.append(o)
            await asyncio.wait_for(other_ready.wait(), 10)
        x = operations.create_isolated_context_task(editor())
        tasks.append(x)
        await asyncio.wait_for(editor_ready.wait(), 2)
        # W1 depends on A's parent hold. In the transitive case W2 does not
        # intersect that parent, but depends on W1's earlier E(M) ticket.
        first_requests = (("child_set", ids["dest"]), ("exact_node", other[0] if transitive else fresh[0]))
        w1 = operations.create_isolated_context_task(ticket("fresh-w1", first_requests))
        tasks.append(w1)
        await asyncio.wait_for(tickets["fresh-w1"].wait(), 2)
        if transitive:
            w2 = operations.create_isolated_context_task(ticket("fresh-w2", (("exact_node", other[0]), ("exact_node", fresh[0]))))
            tasks.append(w2)
            await asyncio.wait_for(tickets["fresh-w2"].wait(), 2)
        assert not any(hold.state.operation_id in ticket_operations for hold in locks._LOCKS.holders.values())
        allow_tag.set()
        await asyncio.wait_for(tag_waiting.wait(), 2)
        assert not tag_finished.is_set()  # The exception never ignores X's actual hold.
        release_editor.set()
        await x
        await asyncio.wait_for(tag_decided.wait(), 10)
        assert tag_finished.is_set(), "pending ticket obstructed the fresh tag"
        result = await asyncio.wait_for(a, 10)
        assert result["success"] is True, result
        assert await asyncio.wait_for(w1, 2)
        if transitive:
            assert await asyncio.wait_for(w2, 2)
            release_other.set()
            await o
        assert probe.violations == []
        assert not locks._LOCKS.holders and not locks._LOCKS.waiters
    finally:
        allow_tag.set()
        release_editor.set()
        release_other.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transitive", [False, True])
async def test_nested_fresh_tag_wait_has_acyclic_geometry(tmp_path, monkeypatch, transitive):
    await _fresh_tag_witness(tmp_path, monkeypatch, transitive=transitive)


@pytest.mark.asyncio
@pytest.mark.parametrize("transitive", [False, True])
async def test_nested_fresh_tag_pending_precedence_neutralization(tmp_path, monkeypatch, transitive):
    import inspect
    source = inspect.getsource(locks._acquire_region_set)
    assert "if additions and not state.holds:" in source
    mutant = source.replace("if additions and not state.holds:", "if additions:")
    namespace = {}
    exec(compile(mutant, "<n05-test-fairness-neutralization>", "exec"), locks.__dict__, namespace)
    with monkeypatch.context() as patch:
        patch.setattr(locks, "_acquire_region_set", namespace["_acquire_region_set"])
        with pytest.raises(AssertionError, match="pending ticket obstructed the fresh tag"):
            await _fresh_tag_witness(tmp_path / "missing", patch, transitive=transitive)
    await _fresh_tag_witness(tmp_path / "restored", monkeypatch, transitive=transitive)


@pytest.mark.asyncio
async def test_second_unrelated_advance_is_not_a_refusal(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    acquire = locks._acquire_region_set
    attempts = []
    async def advance(requested, **kwargs):
        result = await acquire(requested, **kwargs)
        attempts.append(kwargs["deadline"])
        if len(attempts) == 1:
            _publish_parent(client, "p", "g")
        elif len(attempts) == 2:
            _publish_parent(client, "q", "a")  # unrelated to P's new former parent
        return result
    monkeypatch.setattr(locks, "_acquire_region_set", advance)
    def footprint(snapshot):
        return (regions.build_region_exact_node(node_uuid("p"), cache_by_id=snapshot.nodes_by_id),
                regions.build_region_child_set(regions.mutation_parent_identity(node_uuid("p"), cache_by_id=snapshot.nodes_by_id), cache_by_id=snapshot.nodes_by_id))
    async with operations.cache_operation_boundary(client, "unrelated-second"):
        async with locks.mutation_region_locks(client=client, operation_label="unrelated-second", region_factory=footprint) as grant:
            assert {r.key for r in grant.regions} == {node_uuid("p"), node_uuid("g")}
            assert grant.snapshot.view is client._cache_view_publisher.capture()
    assert len(attempts) == 2 and attempts[0] == attempts[1]


async def _spawn_state_witness(tmp_path):
    client = region_client(tmp_path)
    async with operations.cache_operation_boundary(client, "outer-owner"):
        async with locks.mutation_region_locks(client=client, operation_label="outer-owner", region_factory=_regions(("exact_node", "p"))) as outer:
            owner = locks.MUTATION_LOCK_STATE.get()
            execution = operations.current_operation_cache()
            async def same_operation():
                assert operations.current_operation_cache() is execution
                assert locks.MUTATION_LOCK_STATE.get() is owner
                async with locks.mutation_node_locks(node_uuid("p"), client=client) as inner:
                    assert inner.token is None
            await asyncio.create_task(same_operation())  # joined before outer release
            async def independent():
                assert operations.current_operation_cache(required=False) is None, "new operation inherited the execution snapshot"
                assert locks.MUTATION_LOCK_STATE.get() is None, "new operation inherited ownership"
                async with operations.cache_operation_boundary(client, "independent"):
                    async with locks.mutation_node_locks(node_uuid("q"), client=client):
                        assert locks.MUTATION_LOCK_STATE.get() is not owner
                        assert locks.held_mutation_lock_keys() == {node_uuid("q")}
            await operations.create_isolated_context_task(independent())
            assert outer.is_active and operations.current_operation_cache() is execution
            assert locks.held_mutation_lock_keys() == {node_uuid("p")}
    async with operations.cache_operation_boundary(client, "later-operation") as later:
        async with locks.mutation_node_locks(node_uuid("p"), client=client):
            fresh = locks.MUTATION_LOCK_STATE.get()
            assert fresh is not owner and fresh.operation_id == later.operation_id
            assert fresh.waits == []


@pytest.mark.asyncio
async def test_new_operation_spawn_clears_both_states(tmp_path):
    await _spawn_state_witness(tmp_path)


@pytest.mark.asyncio
async def test_new_operation_spawn_context_neutralization(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(operations, "create_isolated_context_task", asyncio.create_task)
        with pytest.raises(AssertionError, match="new operation inherited the execution snapshot"):
            await _spawn_state_witness(tmp_path / "missing")
    await _spawn_state_witness(tmp_path / "restored")


@pytest.mark.asyncio
async def test_a1b_delete_first_blocks_mover(tmp_path, monkeypatch):
    await test_a1b_ordered_delete_and_mover_exclude(tmp_path, monkeypatch, True)


@pytest.mark.asyncio
async def test_a1b_mover_inflight_blocks_delete(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    entered, release, first_scan, second_scan, delete_entered = (asyncio.Event() for _ in range(5))
    waits = []
    wait = locks._wait_for_region_change
    async def observed(waiter, **kwargs):
        waits.append(waiter)
        (first_scan if len(waits) == 1 else second_scan).set()
        return await wait(waiter, **kwargs)
    monkeypatch.setattr(locks, "_wait_for_region_change", observed)
    async def mover():
        async with operations.cache_operation_boundary(client, "live-mover"):
            async with locks.mutation_region_locks(client=client, operation_label="live-mover", region_factory=_regions(("exact_node", "p"), ("child_set", "a"), ("child_set", "g"))):
                entered.set()
                await release.wait()
                _publish_parent(client, "p", "g")
    async def delete():
        async with operations.cache_operation_boundary(client, "delete"):
            async with locks.mutation_region_locks(client=client, operation_label="delete", region_factory=_regions(("subtree", "g"), ("child_set", "vault"))):
                delete_entered.set()
    m = operations.create_isolated_context_task(mover())
    await entered.wait()
    d = operations.create_isolated_context_task(delete())
    try:
        await asyncio.wait_for(first_scan.wait(), 2)
        assert not delete_entered.is_set()
        _publish_parent(client, "q", "a")
        locks._wake_region_waiters()
        await asyncio.wait_for(second_scan.wait(), 2)
        assert not delete_entered.is_set()  # no capture holder needed in this ordering
        release.set()
        await asyncio.wait_for(asyncio.gather(m, d), 2)
        assert delete_entered.is_set()
    finally:
        release.set()
        for task in (m, d):
            if not task.done():
                task.cancel()
        await asyncio.gather(m, d, return_exceptions=True)


async def _batch_proof_witness(tmp_path, monkeypatch, case, *, stale_parent=False):
    from contextlib import asynccontextmanager
    from test_mutation_regions_n05_fences import real_region_probe
    from test_mutation_serialization import dispatch
    from workflowy_mcp import server
    from workflowy_mcp.client import api_client_core
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    plane = locks.mutation_region_locks
    item_label = {"tag.batch": "tag.add", "touch": "anchor.point.touch:item",
                  "promote": "anchor.point.promote:item"}[case]
    items, inner_moves, generations, own_after_first = [], [], [], []
    latest, violations = [], []
    def check(condition, marker):
        if not condition:
            violations.append(marker)
    @asynccontextmanager
    async def observe(*, region_factory, **kwargs):
        is_item = kwargs["operation_label"] == item_label
        def factory(snapshot):
            request = tuple(region_factory(snapshot))
            if stale_parent and is_item and len(items) == 1:
                request = tuple(regions.build_region_child_set(ids["origin"], cache_by_id=snapshot.nodes_by_id)
                    if r.kind == "child_set" and r.key == ids["root"] else r for r in request)
            return request
        async with plane(region_factory=factory, **kwargs) as grant:
            execution = operations.current_operation_cache()
            if is_item:
                items.append(grant)
                assert grant.hold_mode == "short"
                identity = {(r.kind, r.key) for r in grant.regions}
                target = ids["source"] if len(items) == 1 else ids["plain"]
                expected = {("exact_node", target)}
                if case == "promote":
                    expected |= {("child_set", ids["origin"] if len(items) == 1 else ids["root"]), ("child_set", ids["dest"])}
                check(identity == expected, "later item's ownership used old topology")
                generations.append((execution.view.epoch_number, execution.view.epoch_id, execution.view.adopted_sequence))
                if len(items) == 2:
                    check(grant.snapshot.view is latest[0], "later item did not use publisher-current proof")
                    check(generations[1] == generations[0], "later admission repinned the batch plan")
                    check(execution.nodes_by_id[ids["plain"]]["parent_id"] == ids["origin"], "proof imported foreign parent into stable plan")
                    check(execution.nodes_by_id[ids["source"]] == own_after_first[0], "batch lost its own earlier write")
            if kwargs["operation_label"] == "node.move" and len(items) == 2:
                inner_moves.append(grant)
                check(grant.token is None, "inner MOVE derived a stale former parent")
                check(("child_set", ids["root"]) in {(r.kind, r.key) for r in grant.regions}, "inner MOVE omitted the proof-current former parent")
            yield grant
        if is_item and len(items) == 1:
            current = operations.current_operation_cache()
            own_after_first.append(current.nodes_by_id[ids["source"]])
            assert current.nodes_by_id[ids["source"]] != initial_source
            before = client._cache_view_publisher.capture()
            probe.nodes[ids["plain"]]["parent_id"] = ids["root"]
            probe.nodes[ids["plain"]]["name"] = "Foreign generation content #old"
            # A NEW generation, not equal-marker COW: later own writes may
            # overlay, but must not import this unrelated content/parentage.
            advanced = CacheView.build(epoch_number=before.epoch_number, epoch_id=before.epoch_id,
                adopted_sequence=before.adopted_sequence + 1,
                nodes_export={"nodes": list(probe.nodes.values())}, account_scope_id=before.account_scope_id)
            client._cache_view_publisher.publish(advanced)
            latest.append(advanced)
            assert operations.current_operation_cache() is current
    initial_source = client._cache_view_publisher.capture().nodes_by_id[ids["source"]]
    monkeypatch.setattr(server, "mutation_region_locks", observe)
    monkeypatch.setattr(api_client_core, "mutation_region_locks", observe)
    try:
        if case == "tag.batch":
            result = await dispatch(server.build_tag_registry(), "batch", {"operations": [
                {"handle": ids[name], "operation": "add", "tags": ["new"]} for name in ("source", "plain")]})
        else:
            result = await dispatch(server.build_anchor_registry(), "point." + case, {
                "handles": [ids["plain"], ids["source"]] if case == "touch" else [ids["source"], ids["plain"]],
                **({"destination": ids["dest"]} if case == "promote" else {})})
        assert not violations, violations[0] if violations else ""
        assert result["success"] is True, result
        assert len(items) == 2, (case, [g.regions for g in items])
        assert not any(g.is_active for g in items) and not probe.violations
        if case == "promote":
            assert len(inner_moves) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["tag.batch", "touch", "promote"])
async def test_batch_items_use_current_proof_without_repinning_plan(tmp_path, monkeypatch, case):
    await _batch_proof_witness(tmp_path, monkeypatch, case)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["stale_parent", "repin_plan"])
async def test_batch_proof_and_execution_neutralizations(tmp_path, monkeypatch, mutation):
    with monkeypatch.context() as patch:
        if mutation == "repin_plan":
            rebase = locks.rebase_during_admission
            async def repin(*args, **kwargs):
                if kwargs.get("install") is False:
                    kwargs["install"] = True
                return await rebase(*args, **kwargs)
            patch.setattr(locks, "rebase_during_admission", repin)
        marker = "later item's ownership used old topology" if mutation == "stale_parent" else "later admission repinned the batch plan"
        with pytest.raises(AssertionError, match=marker):
            await _batch_proof_witness(tmp_path / "missing", patch, "promote", stale_parent=mutation == "stale_parent")
    await _batch_proof_witness(tmp_path / "restored", monkeypatch, "promote")


@pytest.mark.asyncio
async def test_injected_current_node_nested_core_does_not_refetch_or_repin(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from test_mutation_regions_n05_fences import real_region_probe
    from test_mutation_serialization import dispatch
    from workflowy_mcp import server
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    read, plane = server._node_read_impl, server.mutation_region_locks
    initial, latest, inner, pre_send_reads, violations = [], [], [], [], []
    async def at_send(request, held, required):
        pre_send_reads.append(len([path for path, _held in probe.reads if path.endswith(ids["plain"])]))
    probe.before_send = at_send
    async def authoritative_read(*args, **kwargs):
        node = await read(*args, **kwargs)
        initial.append(operations.current_operation_cache())
        before = client._cache_view_publisher.capture()
        rows = [dict(row) for row in before.nodes_export["nodes"]]
        next(row for row in rows if row["id"] == ids["source"])["name"] = "Unrelated newer content"
        after = CacheView.build(epoch_number=before.epoch_number, epoch_id=before.epoch_id,
            adopted_sequence=before.adopted_sequence + 1,
            nodes_export={"nodes": rows}, account_scope_id=before.account_scope_id)
        client._cache_view_publisher.publish(after)
        latest.append(after)
        return node
    @asynccontextmanager
    async def observe(**kwargs):
        async with plane(**kwargs) as grant:
            if kwargs["operation_label"] == "node.edit":
                inner.append(grant)
                assert grant.token is None and grant.snapshot.view is latest[0]
                if operations.current_operation_cache() is not initial[0]:
                    violations.append("injected-node core repinned execution")
            yield grant
            if kwargs["operation_label"] == "anchor.point.assign_id":
                after = operations.current_operation_cache()
                assert after.view.adopted_sequence == initial[0].view.adopted_sequence
                assert after.nodes_by_id[ids["plain"]]["name"].startswith("[ap:NOD901]")
                assert after.nodes_by_id[ids["source"]]["name"] == "Source #old"
    monkeypatch.setattr(server, "_node_read_impl", authoritative_read)
    monkeypatch.setattr(server, "mutation_region_locks", observe)
    try:
        result = await dispatch(server.build_anchor_registry(), "point.assign_id", {"node_handle": ids["plain"], "ap_id": "NOD901"})
        assert not violations, violations[0] if violations else ""
        assert result["success"] is True, result
        assert len(initial) == len(inner) == 1
        assert pre_send_reads == [2], "injected-node core refetched the live node"
        # One original basis + N02's final-after-ticket read + existing readback;
        # neither verification is a second RMW basis or an execution repin.
        assert len([path for path, held in probe.reads if path.endswith(ids["plain"])]) == 3
        assert len(probe.sends) == 1 and not probe.violations
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_injected_node_no_refetch_neutralization(tmp_path, monkeypatch):
    from workflowy_mcp import server
    edit = server._node_edit_core
    async def drop_injection(*args, **kwargs):
        kwargs.pop("_current_node", None)
        return await edit(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(server, "_node_edit_core", drop_injection)
        with pytest.raises(AssertionError, match="injected-node core refetched the live node"):
            await test_injected_current_node_nested_core_does_not_refetch_or_repin(tmp_path / "missing", patch)
    await test_injected_current_node_nested_core_does_not_refetch_or_repin(tmp_path / "restored", monkeypatch)


async def _decision_memo_witness(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    entered_holder, release, decided, entered_second = (asyncio.Event() for _ in range(4))
    async def holder():
        async with operations.cache_operation_boundary(client, "memo-holder"):
            async with locks.mutation_region_locks(client=client, operation_label="memo-holder", region_factory=_regions(("subtree", "g"))):
                entered_holder.set()
                await release.wait()
    task = operations.create_isolated_context_task(holder())
    await entered_holder.wait()
    wait = locks._wait_for_region_change
    async def observe(waiter, **kwargs):
        decided.set()
        return await wait(waiter, **kwargs)
    monkeypatch.setattr(locks, "_wait_for_region_change", observe)
    async def second():
        async with operations.cache_operation_boundary(client, "memo-second-decision"):
            async with locks.mutation_region_locks(client=client, operation_label="memo-second-decision", region_factory=_regions(("exact_node", "p"))):
                entered_second.set()
                decided.set()
    contender = None
    try:
        # This scan proves P outside G, populating its own local memo only.
        async with operations.cache_operation_boundary(client, "memo-first-decision"):
            async with locks.mutation_region_locks(client=client, operation_label="memo-first-decision", region_factory=_regions(("exact_node", "p"))):
                pass
        _publish_parent(client, "p", "g")  # same-marker COW, different view
        contender = operations.create_isolated_context_task(second())
        await asyncio.wait_for(decided.wait(), 2)
        assert not entered_second.is_set(), "decision-local memo leaked stale ancestry"
        release.set()
        await asyncio.wait_for(asyncio.gather(task, contender), 2)
        assert entered_second.is_set()
    finally:
        release.set()
        if contender is not None and not contender.done():
            contender.cancel()
        await asyncio.gather(*([task, contender] if contender is not None else [task]), return_exceptions=True)


@pytest.mark.asyncio
async def test_decision_local_chain_memo_never_survives_publication(tmp_path, monkeypatch):
    await _decision_memo_witness(tmp_path, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("geometry", ["new_decision", "advanced_view_rescan"])
async def test_cross_decision_chain_memo_leak_neutralization(tmp_path, monkeypatch, geometry):
    chain, leaked = regions._chain_for, {}
    def wrong(key, cache_by_id, *, _chain_memo=None):
        if key not in leaked:
            leaked[key] = chain(key, cache_by_id, _chain_memo=_chain_memo)
        return leaked[key]
    witness = _decision_memo_witness if geometry == "new_decision" else test_a1b_holder_waiter_mover
    marker = "decision-local memo leaked stale ancestry" if geometry == "new_decision" else "DELETE missed the moved live capture"
    with monkeypatch.context() as patch:
        patch.setattr(regions, "_chain_for", wrong)
        with pytest.raises(AssertionError, match=marker):
            await witness(tmp_path / "missing", patch)
    await witness(tmp_path / "restored", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("advance_at", ["before_derivation", "raw_decision"])
@pytest.mark.parametrize("unplaceable_current", [False, True])
async def test_pregrant_current_proof_places_fresh_unrelated_holder(
    scrubbed_runtime, monkeypatch, advance_at, unplaceable_current,
):
    """R01: stale S0 is repaired by a proof capture, not a full cache refresh."""
    client = region_client(scrubbed_runtime)
    ready, release = asyncio.Event(), asyncio.Event()
    holder_task, current_views, factory_views = [], [], []
    establish, acquire = locks.establish_admission_snapshot, locks._acquire_region_set

    async def holder():
        async with operations.cache_operation_boundary(client, "r01-fresh-holder"):
            async with locks.mutation_region_locks(
                client=client, operation_label="r01-fresh-holder",
                region_factory=_regions(("subtree", "fresh")),
            ):
                ready.set()
                await release.wait()

    async def advance():
        before = client._cache_view_publisher.capture()
        rows = [*before.nodes_export["nodes"], *graph_cache({
            "fresh": "vault", "fresh-child": "fresh",
        }).values()]
        current_views.append(CacheView.build(
            epoch_number=before.epoch_number, epoch_id=before.epoch_id,
            adopted_sequence=before.adopted_sequence,
            nodes_export={"nodes": rows}, account_scope_id=before.account_scope_id,
        ))
        client._cache_view_publisher.publish(current_views[-1])
        holder_task.append(operations.create_isolated_context_task(holder()))
        await asyncio.wait_for(ready.wait(), 2)
        if unplaceable_current:
            # A genuinely missing CURRENT holder still keeps N05's remedy.
            latest = client._cache_view_publisher.capture()
            missing = CacheView.build(
                epoch_number=latest.epoch_number, epoch_id=latest.epoch_id,
                adopted_sequence=latest.adopted_sequence,
                nodes_export={"nodes": before.nodes_export["nodes"]},
                account_scope_id=latest.account_scope_id,
            )
            client._cache_view_publisher.publish(missing)
            current_views.append(missing)

    async def advanced_establish(*args, **kwargs):
        proof = await establish(*args, **kwargs)
        if args[1] == "r01-request" and advance_at == "before_derivation":
            await advance()
        return proof

    async def advanced_acquire(requested, **kwargs):
        if kwargs["snapshot"].label == "r01-request" and advance_at == "raw_decision":
            await advance()
        result = await acquire(requested, **kwargs)
        if kwargs["snapshot"].label == "r01-request":
            assert result.snapshot.view is current_views[-1], "raw acquire returned its stale input proof"
            assert result.regions == result.hold.regions
        return result

    monkeypatch.setattr(locks, "establish_admission_snapshot", advanced_establish)
    monkeypatch.setattr(locks, "_acquire_region_set", advanced_acquire)
    def factory(snapshot):
        factory_views.append(snapshot.view)
        return _regions(("subtree", "p"))(snapshot)
    try:
        async with operations.cache_operation_boundary(client, "r01-request") as s0:
            assert node_uuid("fresh") not in s0.nodes_by_id
            async def request():
                async with locks.mutation_region_locks(
                    client=client, operation_label="r01-request", region_factory=factory,
                ) as grant:
                    assert grant.snapshot.view is current_views[-1]
                    assert grant.snapshot.operation_id == s0.operation_id
                    assert node_uuid("fresh-child") in grant.snapshot.nodes_by_id
            if unplaceable_current:
                with pytest.raises(regions.MutationRegionError) as info:
                    await request()
                assert info.value.reason == "unresolvable_region"
                assert "system.cache.refresh" in str(info.value)
            else:
                await request()
            if advance_at == "before_derivation":
                assert factory_views[0] is current_views[-1], "factory derived from stale S0"
    finally:
        release.set()
        await asyncio.gather(*holder_task)
    assert not locks._LOCKS.holders and not locks._LOCKS.waiters


@pytest.mark.asyncio
@pytest.mark.parametrize("move_in_first", [False, True])
async def test_woken_waiter_reproves_moved_out_blocker_with_original_deadline(
    scrubbed_runtime, monkeypatch, move_in_first,
):
    """R02: wakes refresh topology, never the pending request's ticket/budget."""
    client = region_client(scrubbed_runtime)
    _publish_parent(client, "p", "a" if move_in_first else "g")
    release = asyncio.Event()
    ready = {name: asyncio.Event() for name in (("p", "q") if move_in_first else ("p",))}
    first_wait, second_wait, granted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observations = []
    wait = locks._wait_for_region_change
    async def observe(waiter, **kwargs):
        observations.append((waiter, waiter.ticket, waiter.event, waiter.deadline,
                             kwargs["deadline"], waiter.snapshot.view, tuple(waiter.waits)))
        (first_wait if len(observations) == 1 else second_wait).set()
        return await wait(waiter, **kwargs)
    monkeypatch.setattr(locks, "_wait_for_region_change", observe)
    async def holder(name):
        async with operations.cache_operation_boundary(client, "r02-holder-" + name):
            async with locks.mutation_region_locks(
                client=client, operation_label="r02-holder-" + name,
                region_factory=_regions(("child_set", name)),
            ):
                ready[name].set()
                await release.wait()
    async def request():
        async with operations.cache_operation_boundary(client, "r02-waiter"):
            async with locks.mutation_region_locks(
                client=client, operation_label="r02-waiter", region_factory=_regions(("subtree", "g")),
            ) as grant:
                assert grant.snapshot.view is client._cache_view_publisher.capture()
                assert grant.waited_on and grant.waited_seconds >= 0
                granted.set()
    holders = [operations.create_isolated_context_task(holder(name)) for name in ready]
    contender = None
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ready.values())), 2)
        contender = operations.create_isolated_context_task(request())
        await asyncio.wait_for(first_wait.wait(), 2)
        assert not granted.is_set()
        if move_in_first:
            _publish_parent(client, "q", "a")  # The former blocker moves OUT.
            latest = _publish_parent(client, "p", "g")  # Another holder moves IN.
            locks._wake_region_waiters()
            await asyncio.wait_for(second_wait.wait(), 2)
            assert not granted.is_set(), "wake omitted the moved-in actual holder"
            assert observations[-1][5] is latest
            original, newer = observations[0], observations[-1]
            assert newer[:5] == original[:5], "wake replaced its ticket Event or original deadline"
            assert newer[6], "wake discarded accumulated wait accounting"
        _publish_parent(client, "p", "a")
        locks._wake_region_waiters()
        done, _ = await asyncio.wait({contender}, timeout=2)
        assert contender in done, "waiter did not promptly grant after its blocker moved out"
        await contender
        assert granted.is_set() and not release.is_set(), "grant waited for a disjoint holder's release"
    finally:
        release.set()
        if contender is not None and not contender.done():
            contender.cancel()  # Test cleanup only, not a production job front.
        await asyncio.gather(*holders, *([contender] if contender is not None else []), return_exceptions=True)
    assert not locks._LOCKS.holders and not locks._LOCKS.waiters
