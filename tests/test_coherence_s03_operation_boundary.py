"""S03-A seal — operation-boundary authority tests (items 4, 8)."""

from __future__ import annotations

import asyncio
from functools import wraps
from pathlib import Path

import pytest
from coherence_harness import NOW, s02_node

from workflowy_mcp.coherence import (
    initialize_manifest_if_absent,
    is_unpopulated_view,
    manifest_with_updates,
    new_bootstrap_manifest,
    write_manifest,
)
from workflowy_mcp.coherence.errors import CoherenceStateMissingError
from workflowy_mcp.coherence.operation_cache import (
    cache_operation_boundary,
    capture_operation_cache,
    current_operation_cache,
)
from workflowy_mcp.coherence.snapshot_store import (
    new_snapshot_candidate_path,
    write_snapshot_candidate,
)


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


BASE_NODES = [s02_node("A", name="alpha"), s02_node("B", name="beta", parent_id="A")]


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


def _make_lineage_client(tmp_path: Path, monkeypatch):
    """A client whose manifest references a real base snapshot (populated)."""

    probe, _jr, snapshot_dir = _make_client(tmp_path, monkeypatch)
    paths = probe._coherence_paths()
    manifest = initialize_manifest_if_absent(
        paths, new_bootstrap_manifest(paths.account_root.name, now=NOW)
    )
    candidate = write_snapshot_candidate(
        new_snapshot_candidate_path(snapshot_dir),
        {"nodes": BASE_NODES},
        account_scope_id=paths.account_root.name,
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
    client, _jr2, _sd2 = _make_client(tmp_path, monkeypatch)
    return client, paths


def _rename_transform(node_id: str, new_name: str):
    def _transform(working):
        for index, node in enumerate(working["nodes"]):
            if node.get("id") == node_id:
                patched = dict(node)
                patched["name"] = new_name
                working["nodes"][index] = patched
                return
        raise AssertionError(f"target {node_id} missing from working payload")

    return _transform


# ---------------------------------------------------------------------------
# S03-B1 — welded view+dirty capture (B1.1) and owner-bound snapshots (B1.2)
# ---------------------------------------------------------------------------


@async_test
async def test_boundary_welds_view_and_dirty_atomically(tmp_path, monkeypatch):
    """B1.1: the pinned snapshot carries the dirty bookkeeping welded to the
    SAME local transition as its view — never a half-transitioned pair."""
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    client._mark_nodes_export_dirty(["A", "Z"])
    async with cache_operation_boundary(client, "op") as snapshot:
        assert snapshot.dirty_ids == frozenset({"A", "Z"})
        assert snapshot.view.nodes_by_id["A"]["name"] == "alpha"


@async_test
async def test_cow_advance_rewelds_dirty_on_success(tmp_path, monkeypatch):
    """B1.1 + §6.3: a successful COW publication advances the task snapshot
    with BOTH the new content and the post-commit dirty state."""
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    client._mark_nodes_export_dirty(["A"])
    async with cache_operation_boundary(client, "op") as snapshot:
        assert snapshot.dirty_ids == frozenset({"A"})
        client.publish_local_cache_view(
            _rename_transform("A", "alpha-renamed"),
            reason="test rename",
            commit=lambda: client._nodes_export_dirty_ids.discard("A"),
        )
        advanced = current_operation_cache()
        assert advanced.view.nodes_by_id["A"]["name"] == "alpha-renamed"
        # The dirty discard committed atomically with the swap is welded in.
        assert advanced.dirty_ids == frozenset()
        assert advanced.operation_id == snapshot.operation_id
        assert advanced.view.adopted_sequence == snapshot.view.adopted_sequence


@async_test
async def test_nested_boundary_reuses_only_same_authority(tmp_path, monkeypatch):
    """B1.2: a nested boundary naming a DIFFERENT client never borrows the
    outer snapshot's generation — it captures its own authority's view, and
    the outer snapshot is restored on exit."""
    client_a, _paths_a = _make_lineage_client(tmp_path / "a", monkeypatch)
    client_b, _paths_b = _make_lineage_client(tmp_path / "b", monkeypatch)
    async with cache_operation_boundary(client_a, "outer") as outer:
        async with cache_operation_boundary(client_b, "inner") as inner:
            assert inner is not outer
            assert inner.owner is not outer.owner
            assert inner.owner is client_b._cache_view_publisher
            assert current_operation_cache() is inner
        # Outer snapshot restored after the nested boundary exits.
        assert current_operation_cache() is outer


@async_test
async def test_nested_boundary_same_authority_reuses_snapshot(tmp_path, monkeypatch):
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    captures = {"count": 0}
    original = client._capture_view_and_dirty

    def counting():
        captures["count"] += 1
        return original()

    monkeypatch.setattr(client, "_capture_view_and_dirty", counting)
    async with cache_operation_boundary(client, "outer") as outer:
        async with cache_operation_boundary(client, "inner") as inner:
            assert inner is outer
    assert captures["count"] == 1  # Nested reuse needs only the one welded capture.


@async_test
async def test_accessor_rejects_foreign_owner_snapshot(tmp_path, monkeypatch):
    """B1.2 (accessor side): a snapshot pinned by authority A is never served
    to client B's accessor — the owner check fails loudly."""
    from workflowy_mcp.coherence.errors import CacheViewError

    client_a, _paths_a = _make_lineage_client(tmp_path / "a", monkeypatch)
    client_b, _paths_b = _make_lineage_client(tmp_path / "b", monkeypatch)
    async with cache_operation_boundary(client_a, "outer"):
        assert client_a._get_nodes_export_cache_nodes()  # same authority: fine
        with pytest.raises(CacheViewError, match="no operation cache snapshot"):
            client_b._get_nodes_export_cache_nodes()


@async_test
async def test_cache_free_operation_performs_zero_boundary_work(tmp_path, monkeypatch):
    """§7.2: a cache-free public operation (cache_backed=False) never invokes
    the boundary factory — zero coherence work."""
    from workflowy_mcp import server
    from workflowy_mcp.mcp_api_registry import dispatch_operation

    calls: list[str] = []

    def counting_boundary(canonical_name: str):
        calls.append(canonical_name)
        return cache_operation_boundary(getattr(server, "_client", None), canonical_name)

    result = await dispatch_operation(
        server.SYSTEM_REGISTRY,
        "ping",
        {},
        error_logger=None,
        operation_boundary=counting_boundary,
    )
    assert result.get("ok") is True
    assert calls == []


@async_test
async def test_dispatch_hook_wraps_only_cache_backed_invocation(tmp_path, monkeypatch):
    """§7.1: the dispatch funnel runs a cache_backed handler's INVOCATION
    inside the boundary — and never wraps describe/unknown/invalid paths."""
    from workflowy_mcp.mcp_api_registry import (
        OperationRegistry,
        OperationSpec,
        dispatch_operation,
    )

    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    seen: dict[str, object] = {}

    async def handler(_params):
        snapshot = current_operation_cache(required=False)
        seen["snapshot_active"] = snapshot is not None
        seen["label"] = snapshot.label if snapshot else None
        return {"ok": True}

    registry = OperationRegistry("demo", "demo family")
    registry.register(
        OperationSpec(
            family="demo",
            verb="run",
            summary="Run the demo operation.",
            side_effects=("pure_read",),
            async_mode="never",
            params_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            returns={"type": "object"},
            examples=(),
            related=(),
            handler=handler,
            cache_backed=True,
        )
    )
    boundary_calls: list[str] = []

    def factory(canonical_name: str):
        boundary_calls.append(canonical_name)
        return cache_operation_boundary(client, canonical_name)

    result = await dispatch_operation(
        registry, "run", {}, operation_boundary=factory
    )
    assert result == {"ok": True}
    assert seen == {"snapshot_active": True, "label": "demo.run"}
    assert boundary_calls == ["demo.run"]

    # describe: no adoption.
    await dispatch_operation(registry, "describe", {}, operation_boundary=factory)
    # unknown verb: no adoption.
    await dispatch_operation(registry, "nope", {}, operation_boundary=factory)
    assert boundary_calls == ["demo.run"]


@async_test
async def test_capture_rejects_unpopulated_sentinel(tmp_path, monkeypatch):
    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    with pytest.raises(CoherenceStateMissingError, match="cache refresh"):
        await capture_operation_cache(client, "test-op")
    # No ContextVar was set by the refusal.
    assert current_operation_cache(required=False) is None


@async_test
async def test_boundary_does_not_yield_on_sentinel(tmp_path, monkeypatch):
    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    entered = {"flag": False}
    with pytest.raises(CoherenceStateMissingError):
        async with cache_operation_boundary(client, "test-op"):
            entered["flag"] = True
    assert entered["flag"] is False
    assert current_operation_cache(required=False) is None


@async_test
async def test_capture_succeeds_once_base_published(tmp_path, monkeypatch):
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)

    async with cache_operation_boundary(client, "op") as snapshot:
        assert snapshot.nodes_by_id["A"]["name"] == "alpha"
        assert snapshot.nodes_export is snapshot.view.nodes_export
        # Nested boundary reuses the SAME snapshot (zero adoption).
        async with cache_operation_boundary(client, "nested") as nested:
            assert nested is snapshot
    # Context resets after the boundary.
    assert current_operation_cache(required=False) is None


# ---------------------------------------------------------------------------
# Seal item 4 — cumulative same-generation COW semantics (real tasks)
# ---------------------------------------------------------------------------


@async_test
async def test_cumulative_cow_task_semantics(tmp_path, monkeypatch):
    """Deterministic W1 sequence (seal-2 item 3):

    1. T1 enters its boundary and captures S.
    2. T1 spawns a sibling while its ContextVar carries S.
    3. The sibling WAITS for T2's publication before reading.
    4. T2 (independent task) publishes B.name = "beta-t2".
    5. The sibling then reads B from its inherited (copied) snapshot and
       must still see "beta" — it retains S AFTER T2's publication.
    6. T1 itself still sees S (B == "beta") before publishing.
    7. T1 publishes its own A change and advances to the cumulative
       successor containing BOTH writes (A from T1, B from T2).
    8. A child spawned after T1's publication inherits the advanced
       snapshot.

    No assertion that one task's ContextVar write retroactively rewrites an
    already-spawned sibling (W1-prohibited expectation).
    """

    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    t1_ready = asyncio.Event()
    t2_done = asyncio.Event()
    reads: dict[str, str] = {}

    async def t1() -> None:
        async with cache_operation_boundary(client, "t1") as snap_s:
            s_view = snap_s.view

            async def sibling() -> None:
                # Deterministic: read ONLY after T2's publication landed.
                await t2_done.wait()
                reads["sibling_B"] = current_operation_cache().nodes_by_id["B"]["name"]

            sibling_task = asyncio.create_task(sibling())
            t1_ready.set()
            await t2_done.wait()
            await sibling_task

            # T1 never passively saw T2's publication: still pinned to S.
            assert current_operation_cache().view is s_view
            assert current_operation_cache().nodes_by_id["B"]["name"] == "beta"

            # T1 publishes its own transform: rebases onto B and advances.
            client.publish_local_cache_view(
                _rename_transform("A", "alpha-t1"), reason="t1-patch"
            )
            advanced = current_operation_cache()
            assert advanced is not None
            assert advanced.view is not s_view
            assert advanced.operation_id == snap_s.operation_id
            assert advanced.view.epoch_id == s_view.epoch_id
            assert advanced.view.adopted_sequence == s_view.adopted_sequence
            # B′ contains BOTH writes.
            assert advanced.nodes_by_id["A"]["name"] == "alpha-t1"
            assert advanced.nodes_by_id["B"]["name"] == "beta-t2"

            async def child() -> None:
                reads["child_A"] = current_operation_cache().nodes_by_id["A"]["name"]
                reads["child_B"] = current_operation_cache().nodes_by_id["B"]["name"]

            # A child spawned AFTER the publication inherits the advanced snapshot.
            await asyncio.create_task(child())
            assert reads["child_A"] == "alpha-t1"
            assert reads["child_B"] == "beta-t2"

    async def t2() -> None:
        await t1_ready.wait()
        async with cache_operation_boundary(client, "t2"):
            client.publish_local_cache_view(
                _rename_transform("B", "beta-t2"), reason="t2-patch"
            )
        t2_done.set()

    await asyncio.gather(t1(), t2())

    # The pre-publication sibling legitimately retained S (W1: no rewrite),
    # read AFTER T2's publication, and still sees S's B.
    assert reads["sibling_B"] == "beta"


# ---------------------------------------------------------------------------
# Chain-3 W3 (§10) — exact §14.3 boundary reset semantics on every exit path
# ---------------------------------------------------------------------------


@async_test
async def test_boundary_context_resets_on_normal_exit(tmp_path, monkeypatch):
    """§14.3: a normal boundary exit leaves no task-local operation
    snapshot behind."""
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    async with cache_operation_boundary(client, "op"):
        assert current_operation_cache(required=False) is not None
    assert current_operation_cache(required=False) is None


@async_test
async def test_boundary_context_resets_on_ordinary_exception(tmp_path, monkeypatch):
    """§14.3: an ordinary exception inside the boundary still resets the
    task-local operation snapshot (the captured view is never altered)."""
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="boom"):
        async with cache_operation_boundary(client, "op"):
            assert current_operation_cache(required=False) is not None
            raise RuntimeError("boom")
    assert current_operation_cache(required=False) is None


@async_test
async def test_boundary_context_resets_on_cancellation(tmp_path, monkeypatch):
    """§14.3: asyncio.CancelledError inside the boundary resets the
    task-local operation snapshot exactly like any other exit."""
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        async with cache_operation_boundary(client, "op"):
            assert current_operation_cache(required=False) is not None
            raise asyncio.CancelledError()
    assert current_operation_cache(required=False) is None


# ---------------------------------------------------------------------------
# Chain-3 W3 (§11) — WS boundary execution shapes
# ---------------------------------------------------------------------------


@async_test
async def test_ws_inline_handler_boundary_adopts_once_and_resets(tmp_path, monkeypatch):
    """§11.1: an inline-awaited ``@_ws_cache_boundary`` handler runs its
    whole body inside exactly ONE operation boundary (zero adoption, snapshot
    active inside), then resets its context."""
    from workflowy_mcp import server

    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "get_client", lambda: client)
    captures = {"count": 0}
    real_capture = client._capture_view_and_dirty

    def _counting():
        captures["count"] += 1
        return real_capture()

    monkeypatch.setattr(client, "_capture_view_and_dirty", _counting)

    seen: dict[str, object] = {}

    @server._ws_cache_boundary("ws:test-inline")
    async def handler():
        snapshot = current_operation_cache(required=False)
        seen["active"] = snapshot is not None
        seen["label"] = snapshot.label if snapshot else None
        return {"ok": True}

    assert await handler() == {"ok": True}
    assert seen == {"active": True, "label": "ws:test-inline"}
    assert captures["count"] == 1
    assert current_operation_cache(required=False) is None


@async_test
async def test_ws_task_dispatched_handler_boundary_isolated(tmp_path, monkeypatch):
    """§11.2: a task-dispatched ``@_ws_cache_boundary`` handler spawned
    through ``create_isolated_context_task`` does NOT inherit the caller's
    snapshot — it captures its own generation without adoption,
    runs with its snapshot active, and resets; the caller's snapshot is
    untouched."""
    from workflowy_mcp import server
    from workflowy_mcp.coherence.operation_cache import create_isolated_context_task

    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "get_client", lambda: client)
    captures = {"count": 0}
    real_capture = client._capture_view_and_dirty

    def _counting():
        captures["count"] += 1
        return real_capture()

    monkeypatch.setattr(client, "_capture_view_and_dirty", _counting)

    seen: dict[str, object] = {}

    @server._ws_cache_boundary("ws:test-dispatch")
    async def handler():
        snapshot = current_operation_cache(required=False)
        seen["active"] = snapshot is not None
        seen["snapshot"] = snapshot
        return {"ok": True}

    async with cache_operation_boundary(client, "outer") as outer:
        assert await create_isolated_context_task(handler()) == {"ok": True}
        # The caller's own snapshot is intact after the dispatched handler ran.
        assert current_operation_cache() is outer
        # The dispatched handler did NOT inherit the caller's snapshot: it
        # captured its OWN OperationCacheSnapshot (distinct operation
        # identity) via its own capture.  (The immutable view object may
        # legitimately be SHARED when the head has not advanced — the
        # already_current fast path returns the publisher's current view;
        # non-inheritance is a snapshot-level property, not a view-object
        # identity property.)
        assert seen["active"] is True
        assert seen["snapshot"] is not None
        assert seen["snapshot"] is not outer
        assert seen["snapshot"].operation_id != outer.operation_id
        assert seen["snapshot"].view.adopted_sequence == outer.view.adopted_sequence
    assert captures["count"] == 2  # Outer and isolated handler each weld their own view.
    assert current_operation_cache(required=False) is None
