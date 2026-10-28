"""N05 algebra witnesses; registry/seam witnesses follow in this module.

Expected topology and matrix are literal/model data, never production answers.
"""

from __future__ import annotations

from enum import Enum
from types import SimpleNamespace
import asyncio
from contextlib import asynccontextmanager

import pytest

from n05_region_harness import LookupOnlyMap, graph_cache, model_chain, node_uuid, region_client
from workflowy_mcp import mutation_lease_governance as regions
from workflowy_mcp import mutation_locks as locks
from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, create_isolated_context_task


@pytest.fixture(autouse=True)
def _idle_registry(monkeypatch):
    assert not locks._LOCKS.holders and not locks._LOCKS.waiters
    monkeypatch.setattr(locks, "_LOCKS", locks._RegionRegistry())
    monkeypatch.setattr(locks, "_governor_interval_seconds", lambda: 1.0)
    yield
    assert not locks._LOCKS.holders and not locks._LOCKS.waiters


def _factory(*requests):
    builders = {"exact_node": regions.build_region_exact_node, "child_set": regions.build_region_child_set, "subtree": regions.build_region_subtree}
    return lambda snapshot: tuple(builders[kind](node_uuid(label), cache_by_id=snapshot.nodes_by_id) for kind, label in requests)


@asynccontextmanager
async def _window(client, label, requests, **kwargs):
    async with cache_operation_boundary(client, label):
        async with locks.mutation_region_locks(client=client, operation_label=label, region_factory=_factory(*requests), **kwargs) as grant:
            yield grant


def _must_refuse(call, marker="expected typed region refusal"):
    try:
        call()
    except regions.MutationRegionError:
        return
    raise AssertionError(marker)


@pytest.mark.parametrize("invalid", ["", " ", "p", "root", "inbox", "[ap:ABC123]", "12345678", "{00000000-0000-0000-0000-000000000000}", "urn:uuid:00000000-0000-0000-0000-000000000000", None, 123])
def test_region_uuid_grammar_and_aliases(invalid):
    cache = graph_cache()
    _must_refuse(lambda: regions.build_region_exact_node(invalid, cache_by_id=cache))
    key = node_uuid("p")
    expected = regions.build_region_exact_node(key, cache_by_id=cache)
    for spelling in (key.upper(), key.replace("-", ""), key.replace("-", "").upper(), f" {key} "):
        assert regions.build_region_exact_node(spelling, cache_by_id=cache) == expected
        direct = regions.MutationRegion(kind="exact_node", node_uuid=spelling, ancestor_chain=(spelling,))
        assert direct.key == key
    # Every supported cache spelling is reachable by canonical keyed lookups.
    for transform in (str.upper, lambda s: s.replace("-", ""), lambda s: s.replace("-", "").upper()):
        alternate = LookupOnlyMap({transform(k): v for k, v in cache.items()})
        assert regions.build_region_exact_node(key, cache_by_id=alternate) == expected
        # compute_footprint checks root membership before its ancestor walk.
        assert alternate.lookups <= 4 * (len(expected.ancestor_chain) + 1)
    conflicting = {**cache, key.upper(): {**cache[key], "parent_id": node_uuid("g")}}
    _must_refuse(lambda: regions.build_region_exact_node(key, cache_by_id=conflicting))


def test_uuid_validation_sensitivity(monkeypatch):
    def witness():
        _must_refuse(lambda: regions._validate_uuid_text("symbol", "node_uuid"), "UUID grammar was bypassed")
    witness()
    with monkeypatch.context() as patch:
        patch.setattr(regions, "_validate_uuid_text", lambda value, label: str(value))
        with pytest.raises(AssertionError, match="UUID grammar was bypassed"):
            witness()
    witness()


def test_reserved_parent_roundtrip_and_order():
    root = regions.build_region_child_set(regions.ReservedMutationParent.ROOT, cache_by_id={})
    inbox = regions.build_region_child_set(regions.ReservedMutationParent.INBOX, cache_by_id={})
    graph = regions.build_region_child_set(node_uuid("p"), cache_by_id=graph_cache())
    assert not isinstance(root.key, str)
    assert root.ancestor_chain == inbox.ancestor_chain == ()
    assert root.to_dict() == {"kind": "child_set", "parent_special": "root"}
    assert inbox.to_dict() == {"kind": "child_set", "parent_special": "inbox"}
    assert regions.MutationRegion.from_dict(root.to_dict()) == root
    assert regions.MutationRegion.from_dict(inbox.to_dict()) == inbox
    assert regions.normalize_mutation_regions([graph, inbox, root]) == (root, inbox, graph)
    assert [regions.region_display_key(r) for r in (root, inbox, graph)] == ["<root>", "<inbox>", node_uuid("p")[:8]]
    for payload in (
        {"kind": "child_set", "parent_special": "other"},
        {"kind": "child_set", "parent_special": "root", "parent_uuid": node_uuid("p")},
        {"kind": "child_set", "parent_special": "root", "ancestor_chain": []},
        {"kind": "exact_node", "parent_special": "root"},
        {"kind": "subtree", "parent_special": "root"},
    ):
        _must_refuse(lambda: regions.MutationRegion.from_dict(payload))
    for raw in ("root", "inbox"):
        _must_refuse(lambda: regions.build_region_child_set(raw, cache_by_id={}))
    for builder in (regions.build_region_exact_node, regions.build_region_subtree):
        _must_refuse(lambda: builder(regions.ReservedMutationParent.ROOT, cache_by_id={}))


def test_special_identity_sensitivity(monkeypatch):
    def witness():
        region = regions.build_region_child_set(regions.ReservedMutationParent.ROOT, cache_by_id={})
        assert not isinstance(region.key, str), "special identity became a raw string"
    witness()
    class StringParent(str, Enum):
        ROOT = "root"
        INBOX = "inbox"
    with monkeypatch.context() as patch:
        patch.setattr(regions, "ReservedMutationParent", StringParent)
        with pytest.raises(AssertionError, match="special identity became a raw string"):
            witness()
    witness()


def _declared(kind, label, *, missing=False):
    if kind == "ap_core":
        return regions.build_region_ap_core("AAA111" if label == "p" else "BBB222")
    if kind in ("root", "inbox"):
        return regions.build_region_child_set(regions.ReservedMutationParent(kind), cache_by_id={})
    key = node_uuid(label)
    if missing:
        field = {"exact_node": "node_uuid", "child_set": "parent_uuid", "subtree": "root_uuid"}[kind]
        return regions.MutationRegion(kind=kind, **{field: key}, ancestor_chain=(key,))
    return {
        "exact_node": regions.build_region_exact_node,
        "child_set": regions.build_region_child_set,
        "subtree": regions.build_region_subtree,
    }[kind](key, cache_by_id=graph_cache())


def _expected(a, b, left, right, unknown):
    # Independent ordered-kind matrix. No region object or cache is queried.
    if "ap_core" in (a, b):
        return a == b and left == right
    if a in ("root", "inbox") or b in ("root", "inbox"):
        return a == b
    if a == b == "exact_node" or a == b == "child_set":
        return left == right
    if {a, b} == {"exact_node", "child_set"}:
        return False
    if unknown:
        return "error"
    if a == b == "subtree":
        return left in model_chain(right) or right in model_chain(left)
    return left in model_chain(right) if a == "subtree" else right in model_chain(left)


@pytest.mark.parametrize("a", ["exact_node", "child_set", "subtree", "ap_core", "root", "inbox"])
@pytest.mark.parametrize("b", ["exact_node", "child_set", "subtree", "ap_core", "root", "inbox"])
@pytest.mark.parametrize("left,right,unknown", [("p", "p", False), ("p", "q", False), ("a", "p", False), ("p", "a", False), ("p", "missing", True)])
def test_region_pair_matrix_total(a, b, left, right, unknown):
    r1, r2 = _declared(a, left), _declared(b, right, missing=unknown)
    expected = _expected(a, b, left, right, unknown)
    for first, second in ((r1, r2), (r2, r1)):
        if expected == "error":
            _must_refuse(lambda: regions.regions_intersect(first, second, cache_by_id=graph_cache()))
        else:
            assert regions.regions_intersect(first, second, cache_by_id=graph_cache()) is expected


@pytest.mark.parametrize("known", ["exact_node", "child_set", "subtree", "ap_core", "root", "inbox"])
def test_unknown_kind_raises_before_disjoint_shortcuts(known):
    forged = SimpleNamespace(kind="future_kind", key=node_uuid("p"))
    other = _declared(known, "p")
    for a, b in ((forged, other), (other, forged)):
        _must_refuse(lambda: regions.regions_intersect(a, b, cache_by_id=graph_cache()), "unknown pair was allowed")


@pytest.mark.parametrize("known", ["subtree", "ap_core"])
def test_unknown_pair_sensitivity(monkeypatch, known):
    def witness():
        forged = SimpleNamespace(kind="future_kind", key=node_uuid("p"))
        _must_refuse(lambda: regions.regions_intersect(forged, _declared(known, "p"), cache_by_id=graph_cache()), "unknown pair was allowed")
    witness()
    original = regions.regions_intersect
    with monkeypatch.context() as patch:
        patch.setattr(regions, "regions_intersect", lambda a, b, **kw: False if "future_kind" in (a.kind, b.kind) else original(a, b, **kw))
        with pytest.raises(AssertionError, match="unknown pair was allowed"):
            witness()
    witness()


@pytest.mark.parametrize("mechanism", ["initial_guard", "final_tail"])
def test_unknown_pair_guard_and_tail_independent_neutralizations(monkeypatch, mechanism):
    """Independently reach the guard and the defensive unsupported-pair tail."""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(regions.regions_intersect)))
    definition = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    if mechanism == "initial_guard":
        guard = next(n for n in definition.body if isinstance(n, ast.If))
        definition.body.remove(guard)
        other = _declared("ap_core", "p")
    else:
        # Give the union validator a future kind so the independent tail is
        # reachable. Without this staging the front guard masks a dead tail.
        monkeypatch.setattr(regions, "_REGION_KINDS", regions._REGION_KINDS | {"future_kind"})
        assert isinstance(definition.body[-1], ast.Raise)
        definition.body[-1] = ast.Return(value=ast.Constant(False))
        other = _declared("exact_node", "p")
    forged = SimpleNamespace(kind="future_kind", key=node_uuid("p"))
    def witness(predicate):
        _must_refuse(lambda: predicate(forged, other, cache_by_id=graph_cache()), "unknown pair was allowed")
    witness(regions.regions_intersect)
    namespace = dict(regions.__dict__)
    exec(compile(ast.fix_missing_locations(tree), "<n05-test-only-unknown-pair>", "exec"), namespace)
    with pytest.raises(AssertionError, match="unknown pair was allowed"):
        witness(namespace["regions_intersect"])
    witness(regions.regions_intersect)


def test_canonical_dedup_and_conflicting_duplicates():
    cache = graph_cache()
    exact = regions.build_region_exact_node(node_uuid("p"), cache_by_id=cache)
    child = regions.build_region_child_set(node_uuid("a"), cache_by_id=cache)
    subtree = regions.build_region_subtree(node_uuid("p"), cache_by_id=cache)
    ap = regions.build_region_ap_core("[ap:abc123]")
    normalized = regions.normalize_mutation_regions([subtree, child, exact, exact, ap])
    assert normalized == (ap, child, exact, subtree)
    assert regions.normalize_mutation_regions(normalized) == normalized
    stale = regions.MutationRegion(kind="exact_node", node_uuid=node_uuid("p"), ancestor_chain=(node_uuid("p"), node_uuid("g"), node_uuid("vault")))
    _must_refuse(lambda: regions.normalize_mutation_regions([exact, stale]))
    _must_refuse(lambda: regions.normalize_mutation_regions([]))
    for chain in ((node_uuid("a"),), (node_uuid("p"), node_uuid("p")), (node_uuid("p"), "symbol")):
        _must_refuse(lambda: regions.MutationRegion(kind="exact_node", node_uuid=node_uuid("p"), ancestor_chain=chain))
    _must_refuse(lambda: regions.MutationRegion(kind="ap_core", core="ABC123", ancestor_chain=[]))


def test_current_containment_validates_both_subtrees():
    cache = graph_cache()
    present = _declared("subtree", "p")
    missing = _declared("subtree", "missing", missing=True)
    # First direction would match using the other side's stored diagnostic
    # chain. Both live keys still require a complete proof, in either order.
    for a, b in ((present, missing), (missing, present)):
        _must_refuse(lambda: regions.regions_intersect(a, b, cache_by_id=cache))
    cyclic = {**cache, node_uuid("a"): {"id": node_uuid("a"), "parent_id": node_uuid("p")}}
    _must_refuse(lambda: regions.build_region_subtree(node_uuid("p"), cache_by_id=cyclic))


def test_inbox_terminal_allows_equality_not_unknown_containment():
    cache = graph_cache()
    cache[node_uuid("p")] = {**cache[node_uuid("p")], "parent_id": "inbox"}
    exact = regions.build_region_exact_node(node_uuid("x"), cache_by_id=cache)
    assert exact.ancestor_chain == (node_uuid("x"), node_uuid("p"))
    assert regions.regions_intersect(exact, exact, cache_by_id=cache)
    assert regions.mutation_parent_identity(node_uuid("p"), cache_by_id=cache) is regions.ReservedMutationParent.INBOX
    subtree = regions.build_region_subtree(node_uuid("g"), cache_by_id=cache)
    _must_refuse(lambda: regions.regions_intersect(exact, subtree, cache_by_id=cache))
    cache[node_uuid("p")] = {**cache[node_uuid("p")], "parent_id": node_uuid("a")}
    assert not regions.regions_intersect(exact, subtree, cache_by_id=cache)
    assert regions.mutation_parent_identity(node_uuid("p"), cache_by_id=cache) == node_uuid("a")
    assert regions.mutation_parent_identity(node_uuid("vault"), cache_by_id=cache) is regions.ReservedMutationParent.ROOT
    for nodes in ({}, {node_uuid("p"): {"id": node_uuid("p")}}, {node_uuid("p"): {"parent_id": "arbitrary"}}):
        _must_refuse(lambda: regions.mutation_parent_identity(node_uuid("p"), cache_by_id=nodes))


def test_carto_non_graph_requests_do_not_need_claim_topology():
    for region in (_declared("root", "p"), _declared("inbox", "p"), _declared("ap_core", "p")):
        assert regions.mutation_regions_intersect_carto_claim([region], {}, cache_by_id={}) is None
    cache = graph_cache()
    claim = {"root_uuid": node_uuid("a")}
    exact = _declared("exact_node", "p")
    missing = _declared("exact_node", "missing", missing=True)
    _must_refuse(lambda: regions.mutation_regions_intersect_carto_claim([exact, missing], claim, cache_by_id=cache))


@pytest.mark.parametrize("kind", ["exact_node", "child_set", "subtree"])
@pytest.mark.parametrize("spelling", [str.upper, lambda s: s.replace("-", ""), lambda s: s.replace("-", "").upper()])
def test_carto_claim_root_aliases_cannot_hide_overlap(kind, spelling):
    region = _declared(kind, "p")
    claim = {"root_uuid": spelling(node_uuid("a"))}
    assert regions.mutation_regions_intersect_carto_claim([region], claim, cache_by_id=graph_cache()) is region


@pytest.mark.asyncio
async def test_uncontended_admission_has_no_yield(tmp_path, monkeypatch):
    from dataclasses import replace
    client = region_client(tmp_path)
    publisher = client._cache_view_publisher
    before = publisher.capture()
    publisher.publish(replace(before, nodes_by_id=LookupOnlyMap(before.nodes_by_id),
                              nodes_export=LookupOnlyMap(before.nodes_export)))
    from workflowy_mcp import carto_job_governance as gov
    async with cache_operation_boundary(client, "uncontended"):
        def forbidden(*args, **kwargs):
            raise AssertionError("uncontended admission adopted or enumerated claims")
        monkeypatch.setattr(gov, "_read_json", forbidden)
        monkeypatch.setattr(gov.Path, "glob", forbidden)
        resumed = []
        asyncio.get_running_loop().call_soon(resumed.append, True)
        async with locks.mutation_region_locks(client=client, operation_label="any unknown operation label", region_factory=_factory(("exact_node", "p"))) as grant:
            assert resumed == [], "uncontended admission yielded"
            assert grant.waited_seconds == 0 and grant.waited_on == []
            assert grant.is_active
            assert locks.MUTATION_LOCK_STATE.get().serialization() is None
        assert not grant.is_active


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["yield", "cache_scan"])
async def test_uncontended_no_yield_or_scan_sensitivity(tmp_path, monkeypatch, mutation):
    acquire = locks._acquire_region_set
    async def wrong(requested, **kwargs):
        if mutation == "yield":
            await asyncio.sleep(0)
        else:
            tuple(kwargs["snapshot"].nodes_by_id)
        return await acquire(requested, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(locks, "_acquire_region_set", wrong)
        with pytest.raises(AssertionError, match=("uncontended admission yielded" if mutation == "yield" else "region admission iterated the cache")):
            await test_uncontended_admission_has_no_yield(tmp_path / "missing", patch)
    await test_uncontended_admission_has_no_yield(tmp_path / "restored", monkeypatch)


@pytest.mark.asyncio
async def test_exact_node_child_set_commute_permanent(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    release, first, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
    decided = asyncio.Event()
    wait = locks._wait_for_region_change
    async def observe(waiter, **kwargs):
        decided.set()
        return await wait(waiter, **kwargs)
    monkeypatch.setattr(locks, "_wait_for_region_change", observe)
    async def actor(kind, entered):
        async with _window(client, kind, ((kind, "p"),)):
            entered.set()
            decided.set()
            await release.wait()
    tasks = [create_isolated_context_task(actor("exact_node", first))]
    try:
        await first.wait()
        decided.clear()
        tasks.append(create_isolated_context_task(actor("child_set", second)))
        await decided.wait()
        assert second.is_set(), "content and child-set did not commute"
    finally:
        release.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["overbroad_intersection", "global_ownership"])
async def test_commute_and_no_global_lock_sensitivity(tmp_path, monkeypatch, mutation):
    intersect, acquire = locks.regions_intersect, locks._acquire_region_set
    with monkeypatch.context() as patch:
        if mutation == "overbroad_intersection":
            patch.setattr(locks, "regions_intersect", lambda a, b, **kw:
                True if {a.kind, b.kind} == {"exact_node", "child_set"} else intersect(a, b, **kw))
        else:
            async def global_request(requested, **kwargs):
                # One physical region for every task is the forbidden global
                # ownership geometry; no asyncio.Lock or second registry.
                return await acquire((regions.build_region_exact_node(node_uuid("p"), cache_by_id=kwargs["snapshot"].nodes_by_id),), **kwargs)
            patch.setattr(locks, "_acquire_region_set", global_request)
        with pytest.raises(AssertionError, match="content and child-set did not commute"):
            await test_exact_node_child_set_commute_permanent(tmp_path / "missing", patch)
    await test_exact_node_child_set_commute_permanent(tmp_path / "restored", monkeypatch)


@pytest.mark.asyncio
async def test_all_or_none_last_region_conflict(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    registered = asyncio.Event()
    original = locks._wait_for_region_change
    async def observed(waiter, **kwargs):
        registered.set()
        return await original(waiter, **kwargs)
    monkeypatch.setattr(locks, "_wait_for_region_change", observed)
    async with _window(client, "holder", (("exact_node", "p"),)):
        async def contender():
            async with _window(client, "contender", (("exact_node", "a"), ("exact_node", "g"), ("exact_node", "p"))):
                raise AssertionError("contender entered before the blocked last region")
        task = create_isolated_context_task(contender())
        try:
            await registered.wait()
            assert locks.registry_held_keys() == {node_uuid("p")}, "partial ownership appeared while waiting"
            observed = locks.registry_held_regions()
            assert [(r.kind, r.key) for r in observed.regions] == [("exact_node", node_uuid("p"))]
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert locks.registry_held_keys() == {node_uuid("p")}
        assert locks._LOCKS.waiters == []


@pytest.mark.asyncio
async def test_release_notify_no_lost_wakeup(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    registered, resume, release, entered = (asyncio.Event() for _ in range(4))
    original = locks._wait_for_region_change
    async def pause(waiter, **kwargs):
        registered.set()
        await resume.wait()
        assert waiter.event.is_set(), "registered waiter lost the release notification"
        return await original(waiter, **kwargs)
    monkeypatch.setattr(locks, "_wait_for_region_change", pause)
    async def holder():
        async with _window(client, "holder", (("child_set", "p"),)):
            entered.set()
            await release.wait()
    task = create_isolated_context_task(holder())
    await entered.wait()
    async def waiter():
        async with _window(client, "waiter", (("child_set", "p"),)):
            return True
    waiting = create_isolated_context_task(waiter())
    await registered.wait()
    release.set()
    await task
    resume.set()
    assert await asyncio.wait_for(waiting, 2)


@pytest.mark.asyncio
async def test_cancel_waiting_and_grant_handoff(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    for after_notification in (False, True):
        registered, resume = asyncio.Event(), asyncio.Event()
        original = locks._wait_for_region_change
        async def pause(waiter, **kwargs):
            registered.set()
            await resume.wait()
            return await original(waiter, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(locks, "_wait_for_region_change", pause)
            async with _window(client, "holder", (("exact_node", "p"),)) as holder:
                async def contender():
                    async with _window(client, "cancelled", (("exact_node", "p"),)):
                        raise AssertionError("cancelled request entered its body")
                task = create_isolated_context_task(contender())
                await registered.wait()
                if after_notification:
                    locks._LOCKS.waiters[0].event.set()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert holder.is_active and locks.registry_held_keys() == {node_uuid("p")}
                assert locks._LOCKS.waiters == [], "cancelled waiter metadata survived"


@pytest.mark.asyncio
async def test_cancel_holder_and_exact_token_release(tmp_path):
    client = region_client(tmp_path)
    entered, never = asyncio.Event(), asyncio.Event()
    old = []
    async def actor():
        async with _window(client, "cancelled-holder", (("exact_node", "p"),)) as grant:
            old.append((grant.token, locks.MUTATION_LOCK_STATE.get()))
            entered.set()
            await never.wait()
    task = create_isolated_context_task(actor())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert locks.registry_held_regions().regions == (), "cancelled holder token survived"
    async with _window(client, "replacement-holder", (("exact_node", "p"),)) as grant:
        own = locks.MUTATION_LOCK_STATE.get()
        locks._release_region_hold(*old[0])
        locks._release_region_hold(grant.token, locks._TaskLockState("foreign", own.owner, client))
        assert grant.is_active, "foreign release removed the replacement token"
        locks._release_region_hold(grant.token, own)
        locks._release_region_hold(grant.token, own)
        assert not grant.is_active


@pytest.mark.asyncio
async def test_reentrant_subset_releases_only_inner_additions(tmp_path):
    client = region_client(tmp_path)
    async with _window(client, "outer", (("child_set", "a"), ("exact_node", "p"))) as outer:
        async with locks.mutation_node_locks(node_uuid("p"), client=client) as inner:
            assert inner.token is None, "covered request acquired another token"
            assert locks.registry_held_keys() == {node_uuid("p"), node_uuid("a")}
            with pytest.raises(AttributeError):
                inner.token = object()
        assert outer.is_active and not inner.is_active, "inner exit released the outer token"
        async with locks.mutation_region_locks(client=client, operation_label="fresh extension", region_factory=_factory(("exact_node", "x"))) as fresh:
            assert fresh.token is not None and fresh.token is not outer.token
            assert node_uuid("x") in locks.registry_held_keys()
        assert node_uuid("x") not in locks.registry_held_keys() and outer.is_active


@pytest.mark.asyncio
async def test_long_lived_holder_refuses_immediately(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    monkeypatch.setattr(locks, "_long_lived_retry_after_seconds", lambda: 60.0)
    async def no_wait(*args, **kwargs):
        raise AssertionError("declared long-lived holder was waited on")
    monkeypatch.setattr(locks, "_wait_for_region_change", no_wait)
    async def contender():
        async with _window(client, "unknown-label-is-allowed", (("exact_node", "p"),)):
            raise AssertionError("long-lived conflict entered its body")
    for estimate in (None, 120.0, -1.0, float("nan")):
        async with _window(client, "long-lived-holder", (("exact_node", "p"),), hold_mode="long_lived", expected_duration_seconds=estimate) as holder:
            with pytest.raises(locks.MutationSerializationConflictError) as info:
                await create_isolated_context_task(contender())
            assert info.value.holder_kind == "long_lived"
            assert info.value.reason == "long_lived_holder"
            assert info.value.retry_after > 0 and holder.is_active
            assert locks._LOCKS.waiters == []
            if estimate == 120:
                assert 110 < info.value.retry_after <= 120
            else:
                assert info.value.retry_after == 60


@pytest.mark.asyncio
async def test_single_deadline_timeout_fields(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    monkeypatch.setattr(locks, "_acquire_timeout_seconds", lambda: 0.01)
    async with _window(client, "holder", (("exact_node", "p"),)):
        async def contender():
            async with _window(client, "waiter", (("exact_node", "p"),)):
                raise AssertionError("timed-out request entered its body")
        with pytest.raises(locks.MutationSerializationTimeoutError) as info:
            await create_isolated_context_task(contender())
        assert info.value.code == "mutation_serialization_timeout"
        assert info.value.timeout_seconds == 0.01 and info.value.retry_after == 2.0
        assert info.value.contended_keys == [node_uuid("p")[:8]]
        assert locks._LOCKS.waiters == []


async def _deadline_witness(tmp_path, monkeypatch, *, reset_on_replacement=False):
    from n05_region_harness import AdmissionClock
    from test_mutation_regions_n05_revalidation import _publish_parent
    client, clock = region_client(tmp_path), AdmissionClock()
    # Only locks' monotonic reads change. The actual asyncio loop, Event and
    # wait_for run normally; no loop timer heap is moved into a fake future.
    class ClockAsyncio:
        def get_running_loop(self):
            return clock
        def __getattr__(self, key):
            return getattr(asyncio, key)
    monkeypatch.setattr(locks, "asyncio", ClockAsyncio())
    monkeypatch.setattr(locks, "_acquire_timeout_seconds", lambda: 60.)
    release_first, release_second = asyncio.Event(), asyncio.Event()
    entered_first, entered_second = asyncio.Event(), asyncio.Event()
    async def holder(label, requests, entered, release):
        async with _window(client, label, requests):
            entered.set()
            await release.wait()
    first = create_isolated_context_task(holder("deadline-holder", (("exact_node", "p"),), entered_first, release_first))
    second = create_isolated_context_task(holder("replacement-holder", (("child_set", "g"),), entered_second, release_second))
    await entered_first.wait()
    await entered_second.wait()
    raw, wait = locks._acquire_region_set, locks._wait_for_region_change
    attempts, waits, remaining = [], [], []
    async def acquire(requested, **kwargs):
        attempts.append(kwargs["deadline"])
        if reset_on_replacement and len(attempts) == 2:
            kwargs["deadline"] = clock.time() + kwargs["timeout_seconds"]
        result = await raw(requested, **kwargs)
        if len(attempts) == 1:
            clock.advance(10)
            _publish_parent(client, "p", "g")
        return result
    async def notified(waiter, **kwargs):
        waits.append(kwargs["deadline"])
        assert kwargs["deadline"] == 160., "replacement reset the original deadline"
        if len(waits) <= 3:
            clock.advance((20, 15, 5)[len(waits) - 1])
            if len(waits) == 3:
                release_first.set()
                await first
            waiter.event.set()  # several actual registered-event notifications
        else:
            clock.advance(11)  # 61 seconds across the initial and replacement
        remaining.append(kwargs["deadline"] - clock.time())
        return await wait(waiter, **kwargs)
    monkeypatch.setattr(locks, "_acquire_region_set", acquire)
    monkeypatch.setattr(locks, "_wait_for_region_change", notified)
    def footprint(snapshot):
        cache = snapshot.nodes_by_id
        return (regions.build_region_exact_node(node_uuid("p"), cache_by_id=cache),
                regions.build_region_child_set(regions.mutation_parent_identity(node_uuid("p"), cache_by_id=cache), cache_by_id=cache))
    try:
        async with cache_operation_boundary(client, "deadline-contender"):
            with pytest.raises(locks.MutationSerializationTimeoutError) as info:
                async with locks.mutation_region_locks(client=client, operation_label="deadline-contender", region_factory=footprint):
                    raise AssertionError("deadline-expired request sent")
        assert attempts == [160., 160.]
        assert waits == [160.] * 4 and remaining == [40., 25., 20., -1.]
        assert info.value.code == "mutation_serialization_timeout"
        assert info.value.timeout_seconds == 60. and info.value.retry_after == 2.
        assert info.value.contended_keys == [node_uuid("g")[:8]]
        assert not locks._LOCKS.waiters
        assert locks.registry_held_keys() == {node_uuid("g")}  # no partial A/P hold
    finally:
        release_first.set()
        release_second.set()
        await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_single_deadline_covers_waits_and_reacquisition(tmp_path, monkeypatch):
    await _deadline_witness(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_single_deadline_carry_neutralization(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        with pytest.raises(AssertionError, match="replacement reset the original deadline"):
            await _deadline_witness(tmp_path / "missing", patch, reset_on_replacement=True)
    await _deadline_witness(tmp_path / "restored", monkeypatch)


@pytest.mark.asyncio
async def test_diagnostic_readers_are_not_ownership_authorities(tmp_path):
    client = region_client(tmp_path)
    ap = regions.build_region_ap_core("ABC123")
    special = regions.build_region_child_set(regions.ReservedMutationParent.INBOX, cache_by_id={})
    async with cache_operation_boundary(client, "typed-only"):
        async with locks.mutation_region_locks(client=client, operation_label="typed-only", region_factory=lambda s: (ap, special)):
            observed = locks.registry_held_regions()
            assert observed.regions == locks.held_mutation_lock_regions() == (ap, special)
            assert observed.view is None and observed.authority_consistent
            assert locks.registry_held_keys() == locks.held_mutation_lock_keys() == frozenset()
            with pytest.raises(AttributeError):
                observed.regions = ()


async def _explicit_core_witness(tmp_path, monkeypatch, case, *, omit_core=False):
    from test_mutation_regions_n05_fences import real_region_probe
    from test_mutation_serialization import dispatch
    from workflowy_mcp import server
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    events, calls, sent_holds = [], [], []
    reserve, finish, plane = server._anchor_reserve_ap_core, server._anchor_finish_ap_reservation, server.mutation_region_locks
    async def reserved(requested, **kwargs):
        events.append("reserve")
        before = tuple((r.kind, r.key) for r in locks.held_mutation_lock_regions())
        core = await reserve(requested, **kwargs)
        assert core in server._ANCHOR_AP_ID_RESERVED_CORES
        assert not server._ANCHOR_AP_ID_ALLOCATION_LOCK.locked()
        calls.append((requested, core, before))
        events.append("reserved")
        return core
    async def finished(core, **kwargs):
        events.append("finish")
        assert core in server._ANCHOR_AP_ID_RESERVED_CORES
        await finish(core, **kwargs)
        assert core not in server._ANCHOR_AP_ID_RESERVED_CORES
        assert kwargs == {"committed": True}
        assert core in server._ANCHOR_AP_ID_ALLOCATED_CORES
        events.append("finished")
    @asynccontextmanager
    async def observed(*, region_factory, **kwargs):
        def factory(snapshot):
            requested = tuple(region_factory(snapshot))
            return tuple(r for r in requested if r.kind != "ap_core") if omit_core else requested
        async with plane(region_factory=factory, **kwargs) as grant:
            events.append("enter:" + kwargs["operation_label"])
            yield grant
        events.append("exit:" + kwargs["operation_label"])
    async def at_send(request, held, required):
        sent_holds.append(held)
        events.append("send")
    monkeypatch.setattr(server, "_anchor_reserve_ap_core", reserved)
    monkeypatch.setattr(server, "_anchor_finish_ap_reservation", finished)
    monkeypatch.setattr(server, "mutation_region_locks", observed)
    probe.before_send = at_send
    try:
        if case.startswith("capture"):
            result = await dispatch(server.build_anchor_registry(), "point.capture", {
                "parent_handle": ids["dest"], "name": "Core capture", "content": "body",
                "ap_id": "NEW801" if case.endswith("explicit") else "auto",
            })
        elif case.startswith("assign"):
            result = await dispatch(server.build_anchor_registry(), "point.assign_id", {
                "node_handle": ids["plain"], "ap_id": "NEW801" if case.endswith("explicit") else "auto",
            })
        elif case == "etch_literal":
            result = await dispatch(server.build_node_registry(), "etch", {
                "parent_handle": ids["dest"], "nodes": [{"name": "[ap:NEW801] Literal"}],
            })
        else:
            result = await dispatch(server.build_anchor_registry(), "point.promote", {
                "handles": [ids["source"]], "destination": ids["dest"], "mode": "derivative",
            })
        assert result["success"] is True and sent_holds, result
        if case.endswith("explicit") or case == "etch_literal":
            assert all(("ap_core", "NEW801") in held for held in sent_holds), "explicit AP core was not held at send"
        else:
            assert all(not any(kind == "ap_core" for kind, key in held) for held in sent_holds), "generated core was misclassified as explicit"
        if case == "etch_literal":
            assert calls == [] and not any(event.startswith("reserve") for event in events)
        else:
            assert len(calls) == 1 and events.count("finish") == 1  # no second reservation consumer
            assert calls[0][0] == ("NEW801" if case.endswith("explicit") else "auto")
            assert events.index("reserved") < events.index("send") < events.index("finish")
            if case.startswith("capture"):
                assert calls[0][2] == ()
                assert events.index("reserved") < events.index("enter:anchor.point.capture")
                assert events.index("exit:anchor.point.capture") < events.index("finish")
            elif case.startswith("assign"):
                assert events.index("enter:anchor.point.assign_id") < events.index("reserve")
                assert events.index("finished") < events.index("exit:anchor.point.assign_id")
            else:
                assert events.index("enter:anchor.point.promote:item") < events.index("reserve")
                assert events.index("finished") < events.index("exit:anchor.point.promote:item")
        assert probe.violations == [] and not server._ANCHOR_AP_ID_RESERVED_CORES
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["capture_explicit", "capture_auto", "assign_explicit", "assign_auto", "etch_literal", "derivative_generated"])
async def test_explicit_core_hold_auto_allocator_unchanged(tmp_path, monkeypatch, case):
    await _explicit_core_witness(tmp_path, monkeypatch, case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["capture_explicit", "assign_explicit", "etch_literal"])
async def test_explicit_core_inclusion_neutralization(tmp_path, monkeypatch, case):
    with monkeypatch.context() as patch:
        with pytest.raises(AssertionError, match="explicit AP core was not held at send"):
            await _explicit_core_witness(tmp_path / "missing", patch, case, omit_core=True)
    await _explicit_core_witness(tmp_path / "restored", monkeypatch, case)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["malformed_declaration", "unresolved_symbol", "missing_move_parent", "missing_capture_parent", "missing_ancestor", "cycle", "unstable_topology"])
async def test_region_errors_are_pre_send_retryable(tmp_path, monkeypatch, fault):
    from test_mutation_regions_n05_fences import real_region_probe
    from test_mutation_serialization import dispatch
    from n05_region_harness import publish_fixture_nodes
    from workflowy_mcp import server
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    absent = node_uuid("newer-than-cache")
    saved_rows = [dict(row) for row in probe.nodes.values()]
    if fault == "missing_move_parent":
        # A usable symbol-resolution substrate, not an empty-source-map
        # infrastructure failure: the REAL universal resolver must complete
        # its candidates-only symbol leg with zero matches before this test
        # can exercise N05's typed handling of a genuine full-UUID miss.
        mapped_root = node_uuid("remedy-source-map-root")
        mapped = [
            {"id": mapped_root, "name": "Source Map: fixture #nexus--map", "note": "Root: /n05-fixture", "parent_id": None},
            {"id": node_uuid("remedy-source-map-file"), "name": "fixture.py", "note": "Path: fixture.py\nLINE COUNT: 1", "parent_id": mapped_root},
        ]
        saved_rows.extend(mapped)
        probe.nodes.update({row["id"]: dict(row) for row in mapped})
        publish_fixture_nodes(client, saved_rows)
    registry, verb = server.build_node_registry(), "move"
    params = {"node_handle": ids["source"], "parent_handle": ids["dest"], "position": "bottom"}
    expected_reason = "unresolvable_region"
    try:
        with monkeypatch.context() as patch:
            if fault in {"missing_capture_parent", "unresolved_symbol"}:
                registry, verb = server.build_anchor_registry(), "point.capture"
                params = {"parent_handle": absent if fault == "missing_capture_parent" else "not-a-node-symbol",
                          "name": "Retry me", "content": "body", "ap_id": None}
            if fault == "unresolved_symbol":
                resolver = server._resolve_generic_node_handle
                async def missing(handle, **kwargs):
                    if handle == "not-a-node-symbol":
                        raise RuntimeError("fixture symbol cannot be placed")
                    return await resolver(handle, **kwargs)
                patch.setattr(server, "_resolve_generic_node_handle", missing)
                expected_reason = "malformed_region"
            elif fault == "missing_move_parent":
                params["parent_handle"] = absent
                # This mutator resolver also tries its existing symbol leg on
                # a UUID miss. Supply the successful browser-config read so
                # the subject is a genuine resolver miss, not no-tab I/O.
                from unittest.mock import AsyncMock
                patch.setattr(server, "_beacon_symbol_suppress_tags", AsyncMock(return_value=[]))
            elif fault in {"missing_ancestor", "cycle"}:
                rows = [dict(row) for row in saved_rows]
                origin = next(row for row in rows if row["id"] == ids["origin"])
                origin["parent_id"] = absent if fault == "missing_ancestor" else ids["source"]
                publish_fixture_nodes(client, rows)
            elif fault == "malformed_declaration":
                expected_reason = "malformed_region"
                plane = server.mutation_region_locks
                @asynccontextmanager
                async def malformed(*, region_factory, **kwargs):
                    async with plane(region_factory=lambda s: (regions.build_region_exact_node("arbitrary", cache_by_id=s.nodes_by_id),), **kwargs) as grant:
                        yield grant
                patch.setattr(server, "mutation_region_locks", malformed)
            elif fault == "unstable_topology":
                expected_reason = "unstable_topology"
                raw, attempts = locks._acquire_region_set, []
                async def changing(requested, **kwargs):
                    result = await raw(requested, **kwargs)
                    attempts.append(True)
                    rows = [dict(row) for row in saved_rows]
                    next(row for row in rows if row["id"] == ids["source"])["parent_id"] = ids["root"] if len(attempts) == 1 else ids["origin"]
                    publish_fixture_nodes(client, rows)
                    return result
                patch.setattr(locks, "_acquire_region_set", changing)
            refused = await dispatch(registry, verb, params)
            assert refused["success"] is False, "unproved region entered its write window"
            assert refused["error"]["code"] == "mutation_region_error", refused
            extra, message = refused["error"]["extra"], refused["error"]["message"]
            assert extra["reason"] == expected_reason and extra["retry_after"] > 0
            assert "no write was attempted" in message and "silently overwriting another" in message
            remedy = {"malformed_region": "Resolve non-special handles to full UUIDs",
                      "unresolvable_region": "Run system.cache.refresh", "unstable_topology": "Re-read the target"}[expected_reason]
            assert remedy in message
            assert probe.sends == [] and probe.reads == []
            assert not locks._LOCKS.holders and not locks._LOCKS.waiters
            if fault == "unstable_topology":
                assert len(attempts) == 2
        # Declaration fix or ONE fixture refresh, not a barrier/daemon restart.
        publish_fixture_nodes(client, saved_rows)
        retried = await dispatch(server.build_node_registry(), "move", {
            "node_handle": ids["source"], "parent_handle": ids["dest"], "position": "bottom"})
        assert retried["success"] is True and len(probe.sends) == 1, retried
        assert probe.violations == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_capture_containment_both_orders(tmp_path, monkeypatch):
    client = region_client(tmp_path)
    for first, second in ((("subtree", "a"), ("child_set", "p")), (("child_set", "p"), ("subtree", "a"))):
        registered = asyncio.Event()
        original = locks._wait_for_region_change
        async def observed(waiter, **kwargs):
            registered.set()
            return await original(waiter, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(locks, "_wait_for_region_change", observed)
            async with _window(client, "first", (first,)):
                async def contender():
                    async with _window(client, "second", (second,)):
                        registered.set()
                        return True
                task = create_isolated_context_task(contender())
                await registered.wait()
                assert not task.done(), "destructive containment admitted concurrent ownership"
                # An unrelated branch still progresses through the same plane.
                async def unrelated():
                    async with _window(client, "unrelated", (("exact_node", "q"),)):
                        return True
                assert await create_isolated_context_task(unrelated())
            assert await asyncio.wait_for(task, 2)


def _compiled_lock_mutation(name, old, new):
    """A scoped TEST-process code mutation, never a production bypass flag."""
    import inspect
    source = inspect.getsource(getattr(locks, name))
    assert old in source, (name, old)
    namespace = {}
    exec(compile(source.replace(old, new, 1), f"<n05-test-{name}-mutation>", "exec"), locks.__dict__, namespace)
    return namespace[name]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["partial", "notify", "cancel_waiter", "cancel_holder", "foreign_release", "coverage", "inner_release", "long_mode", "containment"])
async def test_registry_bookkeeping_neutralizations(tmp_path, monkeypatch, mutation):
    release = locks._release_region_hold
    witnesses = {
        "partial": (test_all_or_none_last_region_conflict, "partial ownership appeared while waiting"),
        "notify": (test_release_notify_no_lost_wakeup, "registered waiter lost the release notification"),
        "cancel_waiter": (test_cancel_waiting_and_grant_handoff, "cancelled waiter metadata survived"),
        "cancel_holder": (test_cancel_holder_and_exact_token_release, "cancelled holder token survived"),
        "foreign_release": (test_cancel_holder_and_exact_token_release, "foreign release removed the replacement token"),
        "coverage": (test_reentrant_subset_releases_only_inner_additions, "covered request acquired another token"),
        "inner_release": (test_reentrant_subset_releases_only_inner_additions, "inner exit released the outer token"),
        "long_mode": (test_long_lived_holder_refuses_immediately, "declared long-lived holder was waited on"),
        "containment": (test_delete_capture_containment_both_orders, "destructive containment admitted concurrent ownership"),
    }
    witness, marker = witnesses[mutation]
    async def run(path, patch):
        # Two witnesses do not need monkeypatch; keep their actual public API.
        if mutation in {"cancel_holder", "foreign_release", "coverage", "inner_release"}:
            await witness(path)
        else:
            await witness(path, patch)
    with monkeypatch.context() as patch:
        if mutation == "partial":
            patch.setattr(locks, "_acquire_region_set", _compiled_lock_mutation("_acquire_region_set",
                "waiter.blockers = tuple(sorted(blockers))",
                "waiter.blockers = tuple(sorted(blockers))\n"
                "            if not state.holds:\n"
                "                illegal = next(r for r in regions if region_display_key(r) not in blockers)\n"
                "                hold = _RegionHold(token=object(), state=state, regions=(illegal,), client=state.client, hold_mode=hold_mode, expected_release_at=expected_release_at)\n"
                "                _LOCKS.holders[hold.token] = hold\n"
                "                state.holds.append(hold)"))
        elif mutation == "notify":
            patch.setattr(locks, "_wake_region_waiters", lambda: None)
        elif mutation == "cancel_waiter":
            patch.setattr(locks, "_acquire_region_set", _compiled_lock_mutation("_acquire_region_set",
                "_LOCKS.waiters.remove(waiter)", "pass  # omitted waiter-finally cleanup"))
        elif mutation == "cancel_holder":
            patch.setattr(locks, "_release_region_hold", lambda token, state: None)
        elif mutation == "foreign_release":
            def release_foreign(token, state):
                hold = locks._LOCKS.holders.get(token)
                release(token, hold.state if hold is not None else state)
            patch.setattr(locks, "_release_region_hold", release_foreign)
        elif mutation == "coverage":
            patch.setattr(locks, "_regions_cover_request", lambda *args, **kwargs: False)
        elif mutation == "inner_release":
            delegate = locks.mutation_node_locks
            @asynccontextmanager
            async def wrong_inner(*args, **kwargs):
                async with delegate(*args, **kwargs) as grant:
                    yield grant
                state = locks.MUTATION_LOCK_STATE.get()
                if state.holds:
                    release(state.holds[0].token, state)
            patch.setattr(locks, "mutation_node_locks", wrong_inner)
        elif mutation == "long_mode":
            patch.setattr(locks, "_scan_region_blockers", _compiled_lock_mutation("_scan_region_blockers",
                'if hold.hold_mode == "long_lived":', "if False:"))
        else:
            intersect = locks.regions_intersect
            patch.setattr(locks, "regions_intersect", lambda a, b, **kw:
                False if "subtree" in (a.kind, b.kind) else intersect(a, b, **kw))
        try:
            with pytest.raises(AssertionError, match=marker):
                await run(tmp_path / "missing", patch)
        finally:
            # Actors have joined before the specific assertion. Remove only
            # leaked TEST-mutant metadata before restored proof, using the
            # unmodified exact-token release, not a compatibility lock table.
            for hold in tuple(locks._LOCKS.holders.values()):
                release(hold.token, hold.state)
            locks._LOCKS.waiters.clear()
    await run(tmp_path / "restored", monkeypatch)


def test_region_config_budget_and_advice(monkeypatch):
    from workflowy_mcp.models.config import ServerConfig
    from workflowy_mcp import config
    for env in ("MUTATION_LOCK_ACQUIRE_TIMEOUT_SECONDS", "MUTATION_LOCK_LONG_LIVED_RETRY_AFTER_SECONDS"):
        monkeypatch.delenv(env, raising=False)
    defaults = ServerConfig(_env_file=None, WORKFLOWY_API_KEY="n05-fake-key")
    assert defaults.mutation_lock_acquire_timeout_seconds == 60
    assert defaults.mutation_lock_long_lived_retry_after_seconds == 60
    for key in ("mutationLockAcquireTimeoutSeconds", "mutationLockLongLivedRetryAfterSeconds"):
        with pytest.raises(ValueError):
            ServerConfig(_env_file=None, WORKFLOWY_API_KEY="n05-fake-key", **{key: 0})
    monkeypatch.setenv("MUTATION_LOCK_ACQUIRE_TIMEOUT_SECONDS", "3600")
    monkeypatch.setenv("MUTATION_LOCK_LONG_LIVED_RETRY_AFTER_SECONDS", "120")
    configured = ServerConfig(_env_file=None, WORKFLOWY_API_KEY="n05-fake-key")
    assert configured.mutation_lock_acquire_timeout_seconds == 3600
    assert configured.mutation_lock_long_lived_retry_after_seconds == 120
    monkeypatch.setattr(config, "get_server_config", lambda: configured)
    assert locks._acquire_timeout_seconds() == 3600
    assert locks._long_lived_retry_after_seconds() == 120


async def _fifo_witness(tmp_path, monkeypatch, *, ignore_predecessors=False):
    client = region_client(tmp_path)
    if ignore_predecessors:
        class InvisibleTickets(list):
            def __iter__(self):
                return iter(())
        # Test-only mutation of pending metadata visibility; actual holders stay.
        locks._LOCKS.waiters = InvisibleTickets()
    registered, later_decided, first_entered = (asyncio.Event() for _ in range(3))
    release_first = asyncio.Event()
    order = []
    wait = locks._wait_for_region_change
    async def observe(waiter, **kwargs):
        if waiter.snapshot.label == "fifo-first":
            registered.set()
        if waiter.snapshot.label == "fifo-later":
            later_decided.set()
        return await wait(waiter, **kwargs)
    monkeypatch.setattr(locks, "_wait_for_region_change", observe)
    tasks = []
    async def first():
        async with _window(client, "fifo-first", (("child_set", "p"), ("child_set", "g"))):
            order.append("first")
            first_entered.set()
            await release_first.wait()
    async def later(label):
        async with _window(client, label, (("child_set", "g"),)):
            order.append(label)
            later_decided.set()
    try:
        async with _window(client, "actual-holder", (("child_set", "p"),)):
            tasks.append(create_isolated_context_task(first()))
            await asyncio.wait_for(registered.wait(), 2)
            tasks.append(create_isolated_context_task(later("fifo-later")))
            await asyncio.wait_for(later_decided.wait(), 2)
            assert order == [], "later conflicting ticket barged"
            async def independent():
                async with _window(client, "disjoint", (("child_set", "a"),)):
                    return True
            assert await create_isolated_context_task(independent()), "global head-of-line blocked disjoint work"
            tasks.append(create_isolated_context_task(later("fifo-last")))
        await asyncio.wait_for(first_entered.wait(), 2)
        assert order == ["first"]
        release_first.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert order == ["first", "fifo-later", "fifo-last"]
    finally:
        release_first.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_conflict_fifo_without_global_head_of_line(tmp_path, monkeypatch):
    await _fifo_witness(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_conflict_fifo_predecessor_neutralization(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        with pytest.raises(AssertionError, match="later conflicting ticket barged"):
            await _fifo_witness(tmp_path / "missing", patch, ignore_predecessors=True)
    assert not locks._LOCKS.holders and not locks._LOCKS.waiters
    locks._LOCKS.waiters = []
    await _fifo_witness(tmp_path / "restored", monkeypatch)


async def _captures_witness(tmp_path, monkeypatch, *, disable_child_overlap=False, global_child_lock=False):
    import json
    from test_mutation_regions_n05_fences import real_region_probe
    from test_mutation_serialization import dispatch
    from workflowy_mcp import server
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    first_sent, release_first, second_decided = (asyncio.Event() for _ in range(3))
    second_sent = asyncio.Event()
    independent_decided, independent_sent = asyncio.Event(), asyncio.Event()
    real_wait = locks._wait_for_region_change
    real_intersect = locks.regions_intersect
    async def observe_wait(waiter, **kwargs):
        second_decided.set()
        if any(r.kind == "child_set" and r.key == ids["origin"] for r in waiter.regions):
            independent_decided.set()
        return await real_wait(waiter, **kwargs)
    monkeypatch.setattr(locks, "_wait_for_region_change", observe_wait)
    if disable_child_overlap:
        monkeypatch.setattr(locks, "regions_intersect", lambda a, b, **kwargs:
            False if a.kind == b.kind == "child_set" else real_intersect(a, b, **kwargs))
    if global_child_lock:
        monkeypatch.setattr(locks, "regions_intersect", lambda a, b, **kwargs:
            True if a.kind == b.kind == "child_set" else real_intersect(a, b, **kwargs))
    async def at_send(request, held, required):
        if request.method == "POST" and request.url.path.rstrip("/").endswith("nodes"):
            name = json.loads(request.content)["name"]
            if name == "One":
                first_sent.set()
                await release_first.wait()
            elif name == "Two":
                second_sent.set()
                second_decided.set()
            elif name == "Independent":
                independent_sent.set()
                independent_decided.set()
    probe.before_send = at_send
    async def capture(name, parent):
        return await dispatch(server.build_anchor_registry(), "point.capture", {
            "parent_handle": parent, "name": name, "content": "intentional", "ap_id": None,
        })
    tasks = []
    try:
        tasks.append(create_isolated_context_task(capture("One", ids["dest"])))
        await asyncio.wait_for(first_sent.wait(), 10)
        tasks.append(create_isolated_context_task(capture("Two", ids["dest"])))
        await asyncio.wait_for(second_decided.wait(), 10)
        assert not second_sent.is_set(), "same-parent capture entered concurrently"
        separate = create_isolated_context_task(capture("Independent", ids["origin"]))
        tasks.append(separate)
        await asyncio.wait_for(independent_decided.wait(), 10)
        assert independent_sent.is_set(), "unrelated capture was obstructed by global ownership"
        independent = await separate
        assert independent["success"] is True and not release_first.is_set()
        release_first.set()
        results = await asyncio.gather(*tasks)
        assert all(result["success"] is True for result in results), results
        assert len(probe.created) == 3 and len(probe.sends) == 3  # no dedupe
        assert probe.violations == []
    finally:
        release_first.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_two_captures_same_parent_and_independent_parents(tmp_path, monkeypatch):
    await _captures_witness(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_same_parent_capture_overlap_neutralization(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        with pytest.raises(AssertionError, match="same-parent capture entered concurrently"):
            await _captures_witness(tmp_path / "missing", patch, disable_child_overlap=True)
    await _captures_witness(tmp_path / "restored", monkeypatch)


@pytest.mark.asyncio
async def test_independent_parent_capture_global_lock_neutralization(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        with pytest.raises(AssertionError, match="unrelated capture was obstructed by global ownership"):
            await _captures_witness(tmp_path / "global", patch, global_child_lock=True)
    await _captures_witness(tmp_path / "restored", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", [None, "", "InBoX"])
async def test_special_destination_compatibility_and_alias_limit(tmp_path, monkeypatch, destination):
    from test_mutation_regions_n05_fences import real_region_probe
    from test_mutation_serialization import dispatch
    from workflowy_mcp import server
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    try:
        result = await dispatch(server.build_node_registry(), "move", {
            "node_handle": ids["source"], "parent_handle": destination, "position": "bottom",
        })
        assert result["success"] is True and "serialization" not in result, result
        expected_wire = "inbox" if destination else destination  # root None/blank stay byte-identical
        assert probe.nodes[ids["source"]]["parent_id"] == expected_wire
        edited = await dispatch(server.build_node_registry(), "edit", {
            "node_handle": ids["source"], "name": "Still editable",
        })
        assert edited["success"] is True, edited
        moved_out = await dispatch(server.build_node_registry(), "move", {
            "node_handle": ids["source"], "parent_handle": ids["dest"], "position": "bottom",
        })
        assert moved_out["success"] is True and probe.violations == [], moved_out
        async with cache_operation_boundary(client, "explicit-test-alias"):
            snapshot = current_operation_cache()
            unknown = server._mutation_parent_regions(regions.ReservedMutationParent.INBOX, snapshot=snapshot)
            known = server._mutation_parent_regions(regions.ReservedMutationParent.INBOX, snapshot=snapshot, known_inbox_uuid=ids["dest"])
            real = regions.build_region_child_set(ids["dest"], cache_by_id=snapshot.nodes_by_id)
            assert all(not regions.regions_intersect(r, real, cache_by_id=snapshot.nodes_by_id) for r in unknown)  # CF-23's ruled non-intersection
            assert any(regions.regions_intersect(r, real, cache_by_id=snapshot.nodes_by_id) for r in known)
            assert {r.key for r in known} == {regions.ReservedMutationParent.INBOX, ids["dest"]}
    finally:
        await client.close()


@pytest.mark.timing
@pytest.mark.asyncio
async def test_n05_acquisition_costs(tmp_path, monkeypatch, capsys):
    """§9.6 real-clock Cartesian workload; thresholds are TEST assertions only.

    Existing boundary acquisition is measured, not cache construction/adoption
    or fake HTTP. Reuse one deep-frozen account view per size, but give every
    cell its own publisher, lookup counters, holders and root. One-region edit
    and three-region MOVE use depth64 keys;
    holders cycle E/C/S on 100 disjoint depth64 branches. The 0/12/100
    claim-count dimension is INERT legacy on-disk fixture bytes written by
    this test itself: the deleted memo observer is gone, so those artifacts
    are never read and must add no admission work. All 36 warm cells run
    before asserting performance, so a slow case is not silently removed.
    """
    import json
    import math
    import statistics
    import time
    from dataclasses import replace
    from pathlib import Path
    from coherence_harness import ACCOUNT_A, make_boundary_capable
    from n05_region_harness import AdmissionCounters, large_region_tree
    from workflowy_mcp import carto_job_governance as gov

    def summary(samples):
        ordered = sorted(samples)
        return {"median_ms": round(statistics.median(ordered), 6),
                "p95_ms": round(ordered[math.ceil(.95 * len(ordered)) - 1], 6),
                "max_ms": round(ordered[-1], 6)}

    report = []
    lookups_by_cell = {}
    for size in (38_700, 100_000):
        rows, branches = large_region_tree(size)
        seed_client = make_boundary_capable(SimpleNamespace(), nodes=rows)
        pristine_view = seed_client._cache_view_publisher.capture()
        for count_regions in (1, 3):
            for count_holders in (0, 12, 100):
                for count_claims in (0, 12, 100):
                    root = tmp_path / f"{size}-{count_regions}-{count_holders}-{count_claims}"
                    client = SimpleNamespace(api_key="n05-cost-fake-key",
                        _write_governor=SimpleNamespace(paths=SimpleNamespace(jobs_root=root, account_scope_id=ACCOUNT_A)))
                    make_boundary_capable(client)
                    publisher = client._cache_view_publisher
                    publisher.publish(pristine_view)
                    view = publisher.capture()
                    counted_map = LookupOnlyMap(view.nodes_by_id)
                    # The frozen original records stay frozen. Only the read
                    # access facade changes; whole-map iteration/copy raises.
                    publisher.publish(replace(view, nodes_by_id=counted_map,
                        nodes_export=LookupOnlyMap(view.nodes_export)))

                    # Inert legacy claim artifacts: byte-shaped like the retired
                    # on-disk artifact, written by the test itself, never by a
                    # retired production builder, and never read after CUT-2B.
                    claims_dir = root / ".refresh_locks"
                    claims_dir.mkdir(parents=True, exist_ok=True)
                    for index in range(count_claims):
                        branch = branches[102 + index]
                        (claims_dir / f"{index:064x}.cost-job-{index}.lock.json").write_text(
                            json.dumps({
                                "claim_id": f"{index:032x}",
                                "job_id": f"cost-job-{index}",
                                "job_type": "CARTO_REFRESH",
                                "dedupe_key": f"{index:064x}",
                                "account_scope_id": ACCOUNT_A,
                                "kind": "file",
                                "path": None,
                                "root_uuid": branch[-1],
                                "ancestor_chain": list(reversed(branch)),
                                "launcher": "agent",
                                "parent_job_id": None,
                                "handoff_state": "running",
                                "execution_owner": "launcher",
                            }, sort_keys=True),
                            encoding="utf-8",
                        )

                    release = asyncio.Event()
                    holders = []
                    builders = (regions.build_region_exact_node, regions.build_region_child_set, regions.build_region_subtree)
                    async def holder(index, entered):
                        async with cache_operation_boundary(client, f"cost-holder-{index}"):
                            async with locks.mutation_region_locks(client=client, operation_label="cost-holder",
                                    region_factory=lambda s: (builders[index % 3](branches[2 + index][-1], cache_by_id=s.nodes_by_id),)):
                                entered.set()
                                await release.wait()
                    for index in range(count_holders):
                        entered = asyncio.Event()
                        holders.append(create_isolated_context_task(holder(index, entered)))
                        await entered.wait()

                    def request(snapshot):
                        cache = snapshot.nodes_by_id
                        result = (regions.build_region_exact_node(branches[0][-1], cache_by_id=cache),)
                        if count_regions == 3:
                            result += (regions.build_region_child_set(branches[0][-2], cache_by_id=cache),
                                       regions.build_region_child_set(branches[1][-1], cache_by_id=cache))
                        return result

                    counters = AdmissionCounters()
                    try:
                        with monkeypatch.context() as patch:
                            patch.setattr(publisher, "capture", counters.observe("captures", publisher.capture))
                            patch.setattr(Path, "glob", counters.forbidden("scans"))
                            patch.setattr(gov, "_read_json", counters.forbidden("json_opens"))
                            patch.setattr(gov, "acquire_os_lock", counters.forbidden("mutex"))
                            counters.reset()
                            samples, gaps, measured_counts, measured_lookups = [], [], [], []
                            for sample in range(201):
                                async with cache_operation_boundary(client, f"cost-edit-{sample}"):
                                    counters.reset()
                                    counted_map.lookups = 0
                                    beat = []
                                    start = time.perf_counter()
                                    asyncio.get_running_loop().call_soon(lambda: beat.append(time.perf_counter()))
                                    async with locks.mutation_region_locks(client=client, operation_label="cost-edit", region_factory=request) as grant:
                                        elapsed = (time.perf_counter() - start) * 1000
                                        assert not beat, "uncontended cost acquisition yielded"
                                        assert grant.waited_on == []
                                        observations = dict(counters.values)
                                        lookups = counted_map.lookups
                                    await asyncio.sleep(0)
                                    if sample:  # exactly one warm-up, 200 reported admissions
                                        samples.append(elapsed)
                                        gaps.append((beat[0] - start) * 1000)
                                        measured_counts.append(observations)
                                        measured_lookups.append(lookups)
                            assert len(samples) == 200
                            # CF33 §6.8 L282/L286 (conductor 7PDGNM): initial
                            # refresh_proof, first raw pass, post-grant check.
                            # N07 §6.9 / CUT-2B: no interactive claim observation,
                            # file scan, JSON read or OS lock enters admission.
                            assert all(c["captures"] == 3 and c["scans"] == 0
                                       and c["json_opens"] == 0 and c["mutex"] == 0
                                       for c in measured_counts)
                            assert len(set(measured_lookups)) == 1
                            cell_key = (count_regions, count_holders, count_claims)
                            # Equal geometry at the two account sizes must do
                            # identical keyed work, not cache-size work.
                            if cell_key in lookups_by_cell:
                                assert measured_lookups[0] == lookups_by_cell[cell_key]
                            lookups_by_cell[cell_key] = measured_lookups[0]
                            row = {"size": size, "regions": count_regions, "holders": count_holders,
                                "claims": count_claims, "admissions": 200, **summary(samples),
                                "lookup_per_admission": measured_lookups[0],
                                "counters_per_admission": measured_counts[0],
                                "heartbeat": summary(gaps),
                                "over_250_ms": [{"sample": n, "admission_ms": round(a, 6),
                                    "heartbeat_ms": round(b, 6), "attribution": "synchronous depth64 region proof plus event-loop return; no HTTP or adoption"}
                                    for n, (a, b) in enumerate(zip(samples, gaps)) if max(a, b) > 250]}
                            report.append(row)
                            with capsys.disabled():
                                print("N05_COST " + json.dumps(row, sort_keys=True), flush=True)
                    finally:
                        release.set()
                        await asyncio.gather(*holders)
        del rows
    slow = [r for r in report if r["p95_ms"] > 250]
    assert not slow, "§9.6 warm p95 acceptance failed: " + json.dumps(slow, sort_keys=True)


@pytest.mark.asyncio
async def test_inbox_unknown_claim_containment_is_one_refresh_remedy_cf24(tmp_path, monkeypatch):
    from test_mutation_regions_n05_fences import real_region_probe
    from n05_region_harness import publish_fixture_nodes
    from test_mutation_serialization import dispatch
    from workflowy_mcp import server, carto_job_governance as gov
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    ready, release = asyncio.Event(), asyncio.Event()
    task = None
    try:
        moved = await dispatch(server.build_node_registry(), "move", {
            "node_handle": ids["source"], "parent_handle": "inbox", "position": "bottom",
        })
        assert moved["success"] is True
        view = client._cache_view_publisher.capture()
        async def hold():
            async with cache_operation_boundary(client, "carto:cf24-job"):
                async with locks.mutation_region_locks(
                    client=client, operation_label="carto:cf24-job",
                    region_factory=lambda s: (regions.build_region_subtree(ids["root"], cache_by_id=s.nodes_by_id),),
                    hold_mode="long_lived", expected_duration_seconds=None,
                ):
                    ready.set()
                    await release.wait()
        task = create_isolated_context_task(hold())
        await asyncio.wait_for(ready.wait(), 10)
        before = len(probe.sends)
        refused = await dispatch(server.build_node_registry(), "edit", {"node_handle": ids["source"], "name": "After Inbox"})
        assert refused["error"]["code"] == "mutation_region_error"
        assert refused["error"]["extra"]["reason"] == "unresolvable_region"
        assert "system.cache.refresh" in refused["error"]["message"] and len(probe.sends) == before
        # A refresh supplies the missing authoritative real Inbox ancestry.
        # It is outside this CARTO root, not a title-guessed alias in production.
        inbox = node_uuid("cf24-real-inbox")
        probe.nodes[inbox] = {"id": inbox, "name": "Inbox", "parent_id": None}
        probe.nodes[ids["source"]]["parent_id"] = inbox
        publish_fixture_nodes(client, probe.nodes.values())
        retried = await dispatch(server.build_node_registry(), "edit", {"node_handle": ids["source"], "name": "After Inbox"})
        assert retried["success"] is True and len(probe.sends) == before + 1, retried
        assert probe.violations == []
    finally:
        release.set()
        if task is not None:
            await task
        await client.close()
