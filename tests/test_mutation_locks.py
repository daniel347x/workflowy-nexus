"""U01 helper unit tests — mutation_locks.py (spec [ap:XKQQUO] §9 tests 1–7).

Pure in-process asyncio against the real module: registry, idle GC, sorted
all-or-none region acquisition, held-set re-entrancy, the bounded-wait typed refusal
(+ its make_error envelope and the domain_error_codes declaration), both
cancellation kill points, and the two diagnostic readers under concurrency.
Plus the config knob (§5) and the §6.7 finalizer branch (unit assertion).
No Workflowy, no network, no real client.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from workflowy_mcp import mutation_locks as ml
from coherence_harness import make_boundary_capable
from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
from workflowy_mcp.mutation_locks import (
    MUTATION_LOCK_STATE,
    MutationSerializationTimeoutError,
    held_mutation_lock_keys,
    mutation_node_locks,
    registry_held_keys,
)

A = "aaaaaaaa-1111-4111-8111-111111111111"
B = "bbbbbbbb-2222-4222-8222-222222222222"
C = "cccccccc-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Every test starts/ends with an empty registry and no task state.

    Each test body runs under its own ``asyncio.run`` so no lock object ever
    crosses event loops and no ContextVar state leaks between tests.
    """
    monkeypatch.setattr(ml, "_LOCKS", ml._RegionRegistry())
    token = MUTATION_LOCK_STATE.set(None)
    yield
    MUTATION_LOCK_STATE.reset(token)
    assert not ml._LOCKS.holders and not ml._LOCKS.waiters


@pytest.fixture
def lock_client(tmp_path):
    """The delegate now requires a real publisher boundary and private claims."""
    client = SimpleNamespace(
        api_key="u01-fake-key-never-connect",
        _write_governor=SimpleNamespace(paths=SimpleNamespace(
            jobs_root=tmp_path / "u01-jobs", account_scope_id="a" * 32,
        )),
    )
    return make_boundary_capable(client, nodes=[
        {"id": key, "name": key[:8], "parent_id": None} for key in (A, B, C)
    ])


@asynccontextmanager
async def _locks(client, *keys):
    async with cache_operation_boundary(client, "u01-delegate"):
        async with mutation_node_locks(*keys, client=client) as grant:
            yield grant


# ---------------------------------------------------------------------------
# Test 1 — uncontended acquire/release; a grant releases only its own keys
# ---------------------------------------------------------------------------


def test_uncontended_acquire_release_and_grant_scoping(lock_client):
    async def main():
        async with _locks(lock_client, A) as grant:
            assert grant.waited_seconds == 0.0
            assert grant.waited_on == []
            assert registry_held_keys() == frozenset({A})
            assert held_mutation_lock_keys() == frozenset({A})
            # nested acquisition: A is a held no-op, B is new
            async with _locks(lock_client, A, B) as inner:
                assert registry_held_keys() == frozenset({A, B})
                assert inner.waited_seconds == 0.0
            # the inner grant released ONLY its own key (B); A survives
            assert registry_held_keys() == frozenset({A})
            assert held_mutation_lock_keys() == frozenset({A})
        assert registry_held_keys() == frozenset()
        assert held_mutation_lock_keys() == frozenset()
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters
        state = MUTATION_LOCK_STATE.get()
        assert state is not None and state.waits == []  # never contended

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Test 2 — registry GC; a waiter racing release is never stranded
# ---------------------------------------------------------------------------


def test_idle_entries_garbage_collected(lock_client):
    async def main():
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters
        async with _locks(lock_client, A, B):
            assert registry_held_keys() == {A, B}
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters
        # a fresh acquisition after GC works on a brand-new entry
        async with _locks(lock_client, A):
            assert A in registry_held_keys()
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters

    asyncio.run(main())


def test_waiter_racing_release_is_never_stranded(lock_client):
    """Forced interleaving: the holder releases while a waiter is registered.

    The waiter's reference is counted synchronously BEFORE it awaits, so the
    release-then-delete check can never strand it on a deleted lock (spec
    §6.4); the waiter always ends up holding the key.
    """

    async def main():
        release = asyncio.Event()
        progress = []

        async def holder():
            async with _locks(lock_client, A):
                release.set()
                await asyncio.sleep(0.05)
            progress.append("holder-released")

        async def waiter():
            await release.wait()
            await asyncio.sleep(0)  # ensure the holder is inside its hold
            assert A in registry_held_keys()
            async with _locks(lock_client, A) as grant:
                # The registered wait survives the old holder's removal.
                assert A in registry_held_keys()
                assert grant.waited_seconds > 0
                progress.append("waiter-acquired")

        await asyncio.gather(holder(), waiter())
        assert progress == ["holder-released", "waiter-acquired"]
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters

    asyncio.run(main())


def test_registered_waiter_blocks_idle_gc_between_release_and_wake(lock_client):
    """Release removes the hold, never the live waiter's registration (N05 §9.4)."""

    async def main():
        release = asyncio.Event()

        async def holder():
            async with _locks(lock_client, A):
                await release.wait()

        async def waiter():
            async with _locks(lock_client, A):
                pass

        th = asyncio.create_task(holder())
        await asyncio.sleep(0.01)
        tw = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)
        assert len(ml._LOCKS.waiters) == 1 and A in registry_held_keys()
        release.set()  # holder releases; the waiter is granted but not resumed
        await asyncio.sleep(0)  # one tick: the release is processed
        assert A in registry_held_keys() or any(
            region.key == A for waiter in ml._LOCKS.waiters for region in waiter.regions
        )  # an actual holder OR the registered waiter survives; no scalar tombstone
        await th
        await tw
        for _ in range(3):
            await asyncio.sleep(0)
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Test 3 — multi-key acquisition is sorted and deduped (observable order)
# ---------------------------------------------------------------------------


def test_multi_key_acquisition_sorted_and_deduped(lock_client):
    async def main():
        from workflowy_mcp.coherence.operation_cache import create_isolated_context_task

        assert not ml._LOCKS.holders and not ml._LOCKS.waiters
        async with _locks(lock_client, C, A, C, B) as grant:
            # The one complete hold has canonical sorted/deduped region order.
            assert [region.key for region in grant.regions] == [A, B, C]
            assert grant.waited_seconds == 0.0 and grant.waited_on == []
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters

        # N05 §9.4: a blocked complete-set request holds no partial A or C.
        # (Both tasks spawn ISOLATED: main acquired earlier in this test, and
        # a plain create_task would share main's held set — §6.5 same-op
        # inheritance, which these two separate operations must not have.)
        release = asyncio.Event()

        async def hold_b():
            async with _locks(lock_client, B):
                await release.wait()

        th = create_isolated_context_task(hold_b())
        await asyncio.sleep(0.01)

        async def acquire_all():
            async with _locks(lock_client, C, A, B):
                return "done"

        t = create_isolated_context_task(acquire_all())
        await asyncio.sleep(0.02)
        held = registry_held_keys()
        assert A not in held and B in held
        assert C not in held  # no subset of the blocked request is installed
        release.set()
        assert await asyncio.wait_for(t, 5) == "done"
        await th
        for _ in range(3):
            await asyncio.sleep(0)
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Test 4 — held-set re-entrancy: nested acquisition is a no-op grant
# ---------------------------------------------------------------------------


def test_nested_acquisition_of_held_keys_is_a_noop_grant(lock_client):
    async def main():
        from workflowy_mcp.coherence.operation_cache import create_isolated_context_task

        async with _locks(lock_client, A, B):
            # a queued external contender (a NEW operation — spawned through
            # create_isolated_context_task so it does NOT share this task's
            # held set) makes a non-reentrant nested acquisition deadlock —
            # the no-op grant never touches the queue
            queued = asyncio.Event()

            async def contender():
                queued.set()
                async with _locks(lock_client, A):
                    pass

            tc = create_isolated_context_task(contender())
            await queued.wait()
            await asyncio.sleep(0.01)  # the contender is now waiting on A
            assert len(ml._LOCKS.waiters) == 1
            async with _locks(lock_client, A, B) as inner:
                assert inner.waited_seconds == 0.0
                assert inner.waited_on == []
                assert inner.token is None  # released NOTHING on exit
            assert registry_held_keys() == frozenset({A, B})
            # the contender is still queued — the no-op changed nothing
            assert len(ml._LOCKS.waiters) == 1
        # let the contender finish once we release
        await asyncio.wait_for(tc, 5)
        for _ in range(3):
            await asyncio.sleep(0)
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters

    asyncio.run(main())


def test_same_operation_subtask_shares_the_held_set(lock_client):
    """Plain create_task copies the context by reference (§6.5): a subtask of
    the SAME operation inherits the held set and no-ops on those keys."""

    async def main():
        async with _locks(lock_client, A):
            result = {}

            async def subtask():
                result["held"] = held_mutation_lock_keys()
                async with _locks(lock_client, A) as grant:
                    result["noop"] = grant.token is None

            await asyncio.create_task(subtask())
            assert result["held"] == frozenset({A})
            assert result["noop"] is True
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Test 5 — timeout: typed error, envelope, declaration
# ---------------------------------------------------------------------------


def test_timeout_typed_refusal(monkeypatch, lock_client):
    monkeypatch.setattr(ml, "_acquire_timeout_seconds", lambda: 0.05)
    monkeypatch.setattr(ml, "_governor_interval_seconds", lambda: 1.5)

    async def main():
        release = asyncio.Event()

        async def holder():
            async with _locks(lock_client, A):
                await release.wait()

        th = asyncio.create_task(holder())
        await asyncio.sleep(0.01)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(MutationSerializationTimeoutError) as ei:
            async with _locks(lock_client, A):
                raise AssertionError("must never be granted")
        dt = loop.time() - t0
        exc = ei.value
        assert exc.code == "mutation_serialization_timeout"
        assert exc.contended_keys == [A[:8]]
        assert exc.retry_after == 3.0  # 2 × governor interval
        assert exc.timeout_seconds == 0.05
        # the message names the budget and the contended prefix
        assert "0.05" in str(exc) and A[:8] in str(exc)
        assert dt < 1.0  # refused at the knob — never queued (I3)
        # registry consistent: the holder still holds; nothing phantom
        assert registry_held_keys() == frozenset({A})
        assert ml._LOCKS.waiters == []
        release.set()
        await th
        for _ in range(3):
            await asyncio.sleep(0)
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters
        return exc

    exc = asyncio.run(main())

    # the surfaced envelope: the mapper's make_error shape (§5/A4)
    from workflowy_mcp import server

    registry = server.build_node_registry()
    spec = registry.resolve("edit")
    env = server._mcp_exception_mapper(spec, exc)
    assert env["success"] is False
    assert env["operation"] == "node.edit"
    assert env["error"]["code"] == "mutation_serialization_timeout"
    assert env["error"]["extra"]["retry_after"] == 3.0
    assert env["error"]["extra"]["contended_keys"] == [A[:8]]
    assert env["error"]["extra"]["timeout_seconds"] == 0.05
    assert env["error"]["describe_hint"] == {"tool": "node", "verb": "describe", "of": "edit"}
    assert env["error"]["message"] == str(exc)

    # the mechanical declaration check: make_error fail-closes on an
    # UNDECLARED code — a verb without the declaration cannot even build the
    # envelope (OperationSpecError), which is exactly the §9-test-5 check.
    from workflowy_mcp.mcp_api_registry import OperationSpecError, make_error

    assert "mutation_serialization_timeout" in spec.domain_error_codes
    read_spec = registry.resolve("read")  # a pure-read verb does NOT declare it
    with pytest.raises(OperationSpecError):
        make_error(
            family="node",
            operation="node.read",
            code="mutation_serialization_timeout",
            message="x",
            operation_spec=read_spec,
        )


def test_timeout_budget_bounds_the_whole_multi_key_acquisition(monkeypatch, lock_client):
    """P4: no call waits longer than the knob — the deadline spans every key
    of ONE acquisition, not each key separately."""
    monkeypatch.setattr(ml, "_acquire_timeout_seconds", lambda: 0.1)

    async def main():
        release = asyncio.Event()

        async def holder():
            async with _locks(lock_client, C):
                await release.wait()

        th = asyncio.create_task(holder())
        await asyncio.sleep(0.01)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(MutationSerializationTimeoutError) as ei:
            # A/B remain unowned; the complete request's budget is spent at C.
            async with _locks(lock_client, A, B, C):
                raise AssertionError("must never be granted")
        dt = loop.time() - t0
        assert dt < 1.0
        assert ei.value.contended_keys == [C[:8]]
        # No partial A/B acquisition was ever installed — nothing leaked.
        assert registry_held_keys() == frozenset({C})
        release.set()
        await th
        for _ in range(3):
            await asyncio.sleep(0)
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters

    asyncio.run(main())


def test_every_affected_verb_declares_the_timeout_code():
    """N05 §9.4: all three plane codes, on exactly the affected mutation verbs."""
    from workflowy_mcp import server

    expected = {
        "node": {"edit", "update_emoji", "prepend_emoji", "append_emoji",
                 "append_note", "prepend_note", "complete",
                 "uncomplete", "delete", "move", "etch"},
        "tag": {"add", "remove", "rename", "batch", "purge.apply"},
        "anchor": {"dock.create", "dock.update", "dock.promote", "dock.demote",
                   "point.capture", "point.extract", "point.assign_id",
                   "point.remove_id", "point.touch", "point.promote"},
        "slice_group": {"merge.persist", "purge.apply"},
    }
    registries = {
        "node": server.build_node_registry(),
        "tag": server.build_tag_registry(),
        "anchor": server.build_anchor_registry(),
        "slice_group": server.build_slice_group_registry(),
    }
    for family, verbs in expected.items():
        for verb in verbs:
            spec = registries[family].resolve(verb)
            assert {"mutation_serialization_timeout", "mutation_serialization_conflict",
                    "mutation_region_error"} <= set(spec.domain_error_codes), (
                f"{family}.{verb} is missing the declaration"
            )
    for family, verb in (
        ("node", "read"), ("node", "search"), ("node", "export"),
        ("tag", "purge.preview"),
        ("slice_group", "purge.preview"), ("slice_group", "merge.render"),
        ("slice_group", "create"), ("slice_group", "update"), ("slice_group", "delete"),
    ):
        spec = registries[family].resolve(verb)
        assert not {"mutation_serialization_timeout", "mutation_serialization_conflict",
                    "mutation_region_error"}.intersection(spec.domain_error_codes), (
            f"{family}.{verb} must NOT declare the code"
        )


# ---------------------------------------------------------------------------
def test_n02_exact_sixteen_declarations_and_describe_only_disclosure():
    """Add to, never replace, the existing plane-code census above."""
    from workflowy_mcp import server
    from workflowy_mcp.mutation_conflicts import ConcurrentEditDetectedError
    expected = {
        "node": {"edit", "update_emoji", "prepend_emoji", "append_emoji", "append_note", "prepend_note"},
        "tag": {"add", "remove", "rename", "batch"},
        "anchor": {"dock.update", "point.assign_id", "point.remove_id", "point.touch", "point.extract", "point.promote"},
    }
    count = 0
    for registry in (server.build_node_registry(), server.build_tag_registry(), server.build_anchor_registry()):
        for spec in registry.operations():
            present = ConcurrentEditDetectedError.code in spec.domain_error_codes
            assert present == (spec.verb in expected[spec.family]), spec.canonical_name
            if present:
                count += 1
                assert ConcurrentEditDetectedError.code not in spec.summary
                for word in ("re-read", "wire", "last-writer-wins", "compare-and-swap"):
                    assert word in spec.describe_suffix, (spec.canonical_name, word)
    assert count == 16


# Test 6 — cancellation at both kill points
# ---------------------------------------------------------------------------


def test_cancellation_mid_wait_propagates_unregisters_and_leaves_registry_clean(lock_client):
    async def main():
        release = asyncio.Event()

        async def holder():
            async with _locks(lock_client, A):
                await release.wait()

        th = asyncio.create_task(holder())
        await asyncio.sleep(0.01)

        acquired = asyncio.Event()

        async def waiter():
            async with _locks(lock_client, A):
                acquired.set()

        tw = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)
        assert not acquired.is_set()
        assert len(ml._LOCKS.waiters) == 1  # registered before awaiting
        tw.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tw
        for _ in range(3):
            await asyncio.sleep(0)
        assert ml._LOCKS.waiters == []  # unregistered by the cancellation
        assert len(ml._LOCKS.holders) == 1  # holder intact
        assert registry_held_keys() == frozenset({A})
        # the holder releases cleanly and the key is immediately reusable
        release.set()
        await th
        for _ in range(3):
            await asyncio.sleep(0)
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters
        async with _locks(lock_client, A):
            assert registry_held_keys() == frozenset({A})
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters

    asyncio.run(main())


def test_cancellation_mid_hold_releases_every_key(lock_client):
    async def main():
        started = asyncio.Event()

        async def hold():
            async with _locks(lock_client, A, B):
                started.set()
                await asyncio.sleep(30)

        t = asyncio.create_task(hold())
        await started.wait()
        assert registry_held_keys() == frozenset({A, B})
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert registry_held_keys() == frozenset()
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters
        # both keys are immediately reusable by a fresh task
        async def reuse():
            async with _locks(lock_client, A, B):
                assert registry_held_keys() == frozenset({A, B})

        await asyncio.create_task(reuse())
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Test 7 — the diagnostic readers reflect reality under concurrency
# ---------------------------------------------------------------------------


def test_diagnostic_readers_reflect_reality_under_concurrency(lock_client):
    async def main():
        t1_in = asyncio.Event()
        proceed = asyncio.Event()
        t1_recorded = asyncio.Event()
        seen: dict[str, object] = {}

        async def first():
            async with _locks(lock_client, A):
                seen["t1_held"] = held_mutation_lock_keys()
                t1_in.set()
                await proceed.wait()
                seen["t1_registry"] = registry_held_keys()
                t1_recorded.set()

        async def second():
            await t1_in.wait()
            async with _locks(lock_client, B):
                seen["t2_held"] = held_mutation_lock_keys()
                seen["t2_registry"] = registry_held_keys()
                proceed.set()
                # hold B until first() has taken its registry snapshot
                await t1_recorded.wait()

        await asyncio.gather(first(), second())
        # held_* is per-task: each sees ONLY its own keys …
        assert seen["t1_held"] == frozenset({A})
        assert seen["t2_held"] == frozenset({B})
        # … while registry_* shows EVERY task's holds (the §6.9 exposure point)
        assert seen["t2_registry"] == frozenset({A, B})
        assert seen["t1_registry"] == frozenset({A, B})
        assert not ml._LOCKS.holders and not ml._LOCKS.waiters

    asyncio.run(main())


# ---------------------------------------------------------------------------
# The config knob (§5/§6.6) and the module's runtime readers
# ---------------------------------------------------------------------------


def test_config_knob_default_validation_and_env_reachability(monkeypatch):
    from workflowy_mcp.models.config import ServerConfig

    field = ServerConfig.model_fields["mutation_lock_acquire_timeout_seconds"]
    assert float(field.default) == 60.0
    assert ml._DEFAULT_ACQUIRE_TIMEOUT_SECONDS == 60.0
    # gt=0 is enforced
    with pytest.raises(Exception):
        ServerConfig.model_validate(
            {"WORKFLOWY_API_KEY": "k", "mutationLockAcquireTimeoutSeconds": 0}
        )
    # env-over-JSON reachability (the loader's own mechanism; field-name env)
    monkeypatch.setenv("MUTATION_LOCK_ACQUIRE_TIMEOUT_SECONDS", "7.5")
    cfg = ServerConfig.model_validate({"WORKFLOWY_API_KEY": "k"})
    assert cfg.mutation_lock_acquire_timeout_seconds == 7.5
    # the JSON config key (camelCase alias)
    monkeypatch.delenv("MUTATION_LOCK_ACQUIRE_TIMEOUT_SECONDS")
    cfg = ServerConfig.model_validate(
        {"WORKFLOWY_API_KEY": "k", "mutationLockAcquireTimeoutSeconds": 3.5}
    )
    assert cfg.mutation_lock_acquire_timeout_seconds == 3.5


def test_runtime_knob_reader_follows_config(monkeypatch):
    from workflowy_mcp import config as config_module

    fake_cfg = SimpleNamespace(
        mutation_lock_acquire_timeout_seconds=12.5,
        write_governor=SimpleNamespace(interval_seconds=2.0),
    )
    monkeypatch.setattr(config_module, "get_server_config", lambda **_kw: fake_cfg)
    assert ml._acquire_timeout_seconds() == 12.5
    assert ml._governor_interval_seconds() == 2.0

    def _boom(**_kw):
        raise RuntimeError("no config in this context")

    monkeypatch.setattr(config_module, "get_server_config", _boom)
    # a lock helper must never die on config: model default + governor default
    assert ml._acquire_timeout_seconds() == 60.0
    from workflowy_mcp.carto_write_governor import DEFAULT_WRITE_INTERVAL_SECONDS

    assert ml._governor_interval_seconds() == float(DEFAULT_WRITE_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# The §6.7 finalizer branch (unit assertion; DoD 5)
# ---------------------------------------------------------------------------


def test_finalizer_serialization_field_present_iff_contended(lock_client):
    from workflowy_mcp import server

    mutation_spec = SimpleNamespace(
        family="node", cache_backed=True,
        side_effects=("workflowy_mutation",), canonical_name="node.edit",
    )

    async def main():
        # uncontended: the SAME object passes through, no field (§10)
        result = {"success": True, "operation": "node.edit"}
        out = server._mcp_operation_result_finalizer(mutation_spec, result)
        assert out is result and "serialization" not in out

        # contended: the dict result gains the top-level field
        async def holder():
            async with _locks(lock_client, A):
                await asyncio.sleep(0.05)

        th = asyncio.create_task(holder())
        await asyncio.sleep(0.01)
        async with _locks(lock_client, A):
            pass  # waits ~0.04s → one wait event on this task's record
        out = server._mcp_operation_result_finalizer(mutation_spec, {"success": True})
        ser = out["serialization"]
        assert ser["waited_on"] == [A[:8]]
        assert ser["waited_seconds"] > 0
        await th

        # string results (node.edit's note-edit report) are left byte-identical
        # by the finalizer — that path carries the field in its JSON header
        report = '{"success": true}\n'
        assert server._mcp_operation_result_finalizer(mutation_spec, report) == report

        # an existing key is never clobbered
        carried = {"success": True, "serialization": {"waited_seconds": 1.0, "waited_on": ["zzzzzzzz"]}}
        out = server._mcp_operation_result_finalizer(mutation_spec, carried)
        assert out["serialization"]["waited_on"] == ["zzzzzzzz"]

    asyncio.run(main())
