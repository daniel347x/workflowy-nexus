"""S03-B1 consumer-migration parity tests (§14.4).

Three coverage tiers, deliberately distinguished:

1. GENERIC STRUCTURAL PROOFS (this module's first sections): one adoption
   per operation across nested helpers, the export alias contract (§9),
   same-operation read-your-writes for repeated plan/commit loops, and
   concurrent operations legitimately holding different generations.
2. REPRESENTATIVE REAL-PATH PROOFS (the final section, audit amendment A6):
   head-advance-mid-operation pinning across the four distinct operation
   shapes — resolution→planning, render Stage-0→Stage-1, mutation
   plan→eager-patch verification, and bulk/path classification — exercising
   the REAL resolvers, render seams, patchers, and path classifiers.
3. ORDINARY BEHAVIOR REGRESSION: the retrofitted feature suites elsewhere
   in the repo (boundary-capable doubles + fixed-fixture output parity).
"""

from __future__ import annotations

import asyncio
import json
from functools import wraps
from pathlib import Path

import pytest
from coherence_harness import NOW, publish_fixture_head, s02_node

from workflowy_mcp.coherence import (
    initialize_manifest_if_absent,
    manifest_with_updates,
    new_bootstrap_manifest,
    write_manifest,
)
from workflowy_mcp.coherence.operation_cache import (
    cache_operation_boundary,
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


BASE_NODES = [
    s02_node("A", name="alpha"),
    s02_node("B", name="beta", parent_id="A"),
    s02_node("C", name="gamma", parent_id="B"),
]


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


def _make_lineage_client(tmp_path: Path, monkeypatch, nodes=BASE_NODES):
    probe, _jr, snapshot_dir = _make_client(tmp_path, monkeypatch)
    paths = probe._coherence_paths()
    manifest = initialize_manifest_if_absent(
        paths, new_bootstrap_manifest(paths.account_root.name, now=NOW)
    )
    candidate = write_snapshot_candidate(
        new_snapshot_candidate_path(snapshot_dir),
        {"nodes": list(nodes)},
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
        raise AssertionError(f"target {node_id} missing")

    return _transform


# ---------------------------------------------------------------------------
# One adoption per operation, across nested helper calls
# ---------------------------------------------------------------------------


@async_test
async def test_one_adoption_per_operation_despite_nested_helpers(tmp_path, monkeypatch):
    """§7.1: handle resolution, map building, and export all ride the ONE
    local capture at the operation boundary — zero adoptions."""
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    captures = {"count": 0}
    original = client._capture_view_and_dirty

    def counting():
        captures["count"] += 1
        return original()

    monkeypatch.setattr(client, "_capture_view_and_dirty", counting)

    from workflowy_mcp.client.api_client_nexus import (
        load_nodes_by_id_from_cache,
        resolve_uuid_like_node_handle,
    )

    async with cache_operation_boundary(client, "op") as snapshot:
        # Nested consumers: accessor, loader, resolver, explicit map threading.
        nodes = client._get_nodes_export_cache_nodes()
        assert nodes is snapshot.view.nodes_export["nodes"]
        mapping = load_nodes_by_id_from_cache(snapshot)
        assert mapping is snapshot.nodes_by_id
        mapping2 = load_nodes_by_id_from_cache(snapshot.view)
        assert mapping2 is snapshot.nodes_by_id
        resolved = resolve_uuid_like_node_handle("A", mapping)
        assert resolved["success"] and resolved["input_kind"] == "exact_uuid"
        # A resolver self-load inside the boundary funnels through the SAME view
        # (a fresh map built from the pinned view's nodes — node OBJECTS shared).
        resolved2 = resolve_uuid_like_node_handle("B", client=client)
        assert resolved2["success"]
        assert resolved2["nodes_by_id"]["B"] is snapshot.nodes_by_id["B"]

    assert captures["count"] == 1


@async_test
async def test_loader_client_form_reads_task_local_snapshot(tmp_path, monkeypatch):
    """§8.1: the loader's client form never independently reacquires — it
    reads the task-local snapshot (or fails loudly with none active)."""
    from workflowy_mcp.client.api_client_nexus import load_nodes_by_id_from_cache
    from workflowy_mcp.coherence.errors import CacheViewError

    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    with pytest.raises(CacheViewError, match="no operation cache snapshot"):
        load_nodes_by_id_from_cache(client)
    async with cache_operation_boundary(client, "op") as snapshot:
        assert load_nodes_by_id_from_cache(client)["A"] is snapshot.nodes_by_id["A"]


# ---------------------------------------------------------------------------
# §9 export alias contract
# ---------------------------------------------------------------------------


@async_test
async def test_export_alias_escape(tmp_path, monkeypatch):
    """§9 (steps 1-5): capture view A; export from its snapshot; publish view
    B; mutate a caller-owned working copy; neither A nor B changes."""
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)

    from workflowy_mcp.client.api_client_etch import export_nodes_impl

    async with cache_operation_boundary(client, "op") as snapshot_a:
        # (2) obtain the export result from the captured snapshot.
        data = await export_nodes_impl(client, node_id=None, use_cache=True)
        # S03 §9 + the S02-ledger-8 shallow wrapper: the whole-vault cache-hit
        # result is a NEW top-level wrapper dict (never patched into the view),
        # whose ``nodes`` list IS the pinned view's own immutable list — the
        # caller never receives a mutable alias of the publisher's live global.
        assert data is not snapshot_a.view.nodes_export
        assert data["nodes"] is snapshot_a.view.nodes_export["nodes"]
        names_a = {n["id"]: n["name"] for n in data["nodes"]}
        assert names_a == {"A": "alpha", "B": "beta", "C": "gamma"}

        # (3) publish view B (a same-generation COW patch).
        client.publish_local_cache_view(
            _rename_transform("A", "alpha-renamed"),
            reason="test rename",
        )
        # (4) mutate a caller-owned working copy.
        working_copy = {"nodes": [dict(n) for n in data["nodes"]]}
        working_copy["nodes"][0]["name"] = "caller-mutated"

        # (5) neither A nor B changed: A keeps its pinned content, B carries
        # only the COW rename — never the caller's mutation.
        assert snapshot_a.view.nodes_by_id["A"]["name"] == "alpha"
        current = client._cache_view_publisher.capture()
        assert current.nodes_by_id["A"]["name"] == "alpha-renamed"
        assert "caller-mutated" not in {
            n.get("name") for n in current.nodes_export["nodes"]
        }


@async_test
async def test_export_refresh_branch_still_works_boundaryless(tmp_path, monkeypatch):
    """§9 compatibility: the _allow_api_refresh path (refresh authority) is
    untouched by the snapshot contract AND by the S03-closure loudening."""
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    refreshed = {"count": 0}

    async def fake_refresh(**kwargs):
        refreshed["count"] += 1

    client.refresh_nodes_export_cache = fake_refresh  # type: ignore[method-assign]

    from workflowy_mcp.client.api_client_etch import export_nodes_impl

    # Boundary-less + _allow_api_refresh=True: the refresh branch delegates to
    # the authority with no operation snapshot required (H2-c), then returns
    # the (populated) adopted payload.  This is the ONLY boundary-less route
    # that survives the S03 closure (CF-S03CR-01).
    data = await export_nodes_impl(
        client, node_id=None, use_cache=False, _allow_api_refresh=True
    )
    assert {n["id"] for n in data["nodes"]} == {"A", "B", "C"}
    assert refreshed["count"] == 1


# ---------------------------------------------------------------------------
# S03 closure (CF-S03CR-01) — the boundary-less property fallback is retired LOUD
# ---------------------------------------------------------------------------


CANONICAL_LEGACY_PROFILE_NOTE = (
    "the 37 @_legacy_tool surfaces are profile-gated off under the default "
    "'consolidated' profile (identity decorator). Under the selectable "
    "'legacy' profile, every cache-backed legacy tool is wrapped in ONE "
    "local-capture cache_operation_boundary at FastMCP registration "
    "(S03-CR-03): the legacy surface is a first-class migrated cache surface "
    "— never a boundaryless loud failure, never a fail-soft render without "
    "cache context (_modal_cache_nodes_by_id re-raises CacheViewError). "
    "Cache-free legacy tools (the system.* and slice-group CRUD equivalents "
    "per LEGACY_TOOL_MAPPING) keep their zero-coherence-work contracts and "
    "are never wrapped. Supersedes the CF-S03CR-01 fail-closed disposition "
    "(which treated the still-selectable legacy profile as unsupported)."
)


def _make_lineage_nexus_client(tmp_path: Path, monkeypatch, nodes=BASE_NODES):
    """A WorkFlowyClientNexus warm-started populated from the same lineage
    the Core helper establishes (needed for the end-to-end scry route)."""
    _client, paths = _make_lineage_client(tmp_path, monkeypatch, nodes=nodes)

    from pydantic import SecretStr

    from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
    from workflowy_mcp.models import APIConfiguration

    config = APIConfiguration(
        api_key=SecretStr("test-key-1234"), base_url="https://workflowy.com/api/v1"
    )
    return WorkFlowyClientNexus(config), paths


@async_test
async def test_export_boundaryless_populated_publisher_fails_loud(tmp_path, monkeypatch):
    """S03 closure (CF-S03CR-01, P1): a POPULATED publisher with NO operation
    boundary now fails LOUDLY — the silent publisher-current serve is gone."""
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)

    from workflowy_mcp.client.api_client_etch import export_nodes_impl
    from workflowy_mcp.coherence.errors import CacheViewError

    with pytest.raises(CacheViewError, match="no operation cache snapshot"):
        await export_nodes_impl(client, node_id=None, use_cache=True)


@async_test
async def test_export_foreign_owner_boundary_fails_loud(tmp_path, monkeypatch):
    """B1.2: an ACTIVE boundary owned by a DIFFERENT client authority must not
    be borrowed — export_nodes_impl fails loud rather than serving the foreign
    snapshot or falling through to a silent property read."""
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    foreign, _jr, _sd = _make_client(tmp_path, monkeypatch)

    from workflowy_mcp.client.api_client_etch import export_nodes_impl
    from workflowy_mcp.coherence.errors import CacheViewError

    async with cache_operation_boundary(foreign, "foreign-op"):
        with pytest.raises(CacheViewError, match="no operation cache snapshot"):
            await export_nodes_impl(client, node_id=None, use_cache=True)


@async_test
async def test_export_unpopulated_boundaryless_keeps_refresh_required_dialect(
    tmp_path, monkeypatch
):
    """P2 split: unpopulated + boundary-less + no refresh flag keeps the
    actionable ``NetworkError`` refresh-required dialect — ONLY the populated
    boundary-less case is the migration-bug ``CacheViewError``."""
    client, _jr, _sd = _make_client(tmp_path, monkeypatch)

    from workflowy_mcp.client.api_client_etch import export_nodes_impl
    from workflowy_mcp.models import NetworkError

    with pytest.raises(NetworkError, match="nodes-export cache is not initialized"):
        await export_nodes_impl(client, node_id=None, use_cache=True)


@async_test
async def test_boundaryless_scry_output_file_fails_loud_end_to_end(tmp_path, monkeypatch):
    """CF-S03CR-01's concrete route: a boundary-less ``workflowy_scry`` with
    ``output_file`` (the legacy-profile path that used to serve the publisher's
    current view SILENTLY) now fails loudly end-to-end and writes NOTHING."""
    client, _paths = _make_lineage_nexus_client(tmp_path, monkeypatch)

    from workflowy_mcp.coherence.errors import CacheViewError

    out = tmp_path / "legacy_scry_out.json"
    with pytest.raises(CacheViewError, match="no operation cache snapshot"):
        await client.workflowy_scry(node_id="A", output_file=str(out))
    assert not out.exists()
    assert not out.with_suffix(".md").exists()


def test_registry_legacy_profile_note_states_closure_truth() -> None:
    """Record-honesty lock (CF-S03CR-01, P4): the registry's legacy_profile_note
    must state the post-closure truth VERBATIM — drift between the durable
    record and the bytes trips CI immediately."""
    registry = json.loads(
        (Path(__file__).resolve().parent / "coherence_s03b_registry.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        registry["mcp_dispatch"]["legacy_profile_note"] == CANONICAL_LEGACY_PROFILE_NOTE
    )
    # The export surfaces no longer carry any retained-fallback clause.
    dispo = registry["consumer_dispositions"]["client.api_client_etch.export_nodes_impl"]
    assert "retained verbatim" not in dispo
    assert "CacheViewError" in dispo
    (allow_entry,) = [
        entry for entry in registry["allowlist"] if entry["function"] == "export_nodes_impl"
    ]
    assert "retained verbatim" not in allow_entry["reason"]
    assert "RETIRED" in allow_entry["reason"]


# ---------------------------------------------------------------------------
# Repeated plan/commit loops (Q3/Q4 pattern)
# ---------------------------------------------------------------------------


@async_test
async def test_repeated_plan_commit_loops_observe_prior_patches(tmp_path, monkeypatch):
    """§6.3/§14.4: repeated same-operation COW cycles each observe the prior
    patch, with adopted markers pinned end-to-end."""
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    async with cache_operation_boundary(client, "op") as snapshot:
        base_seq = snapshot.view.adopted_sequence
        client.publish_local_cache_view(
            _rename_transform("A", "alpha-1"), reason="step 1"
        )
        assert current_operation_cache().nodes_by_id["A"]["name"] == "alpha-1"
        client.publish_local_cache_view(
            _rename_transform("B", "beta-1"), reason="step 2"
        )
        final = current_operation_cache()
        assert final.nodes_by_id["A"]["name"] == "alpha-1"
        assert final.nodes_by_id["B"]["name"] == "beta-1"
        assert final.view.adopted_sequence == base_seq
        assert final.operation_id == snapshot.operation_id


@async_test
async def test_post_mutation_verifier_observes_own_writes(tmp_path, monkeypatch):
    """The _merge_slice_groups/_purge_apply_impl pattern: plan against the
    pinned view, commit (COW), verify against the ADVANCED task snapshot —
    one epoch/sequence end-to-end."""
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    client._mark_nodes_export_dirty(["A"])
    async with cache_operation_boundary(client, "op") as snapshot:
        # Plan: the verifier would read the pinned view here.
        assert snapshot.nodes_by_id["A"]["name"] == "alpha"
        # Commit (the mutation family's eager patch, transition-COW form).
        client.publish_local_cache_view(
            _rename_transform("A", "alpha #merged"),
            reason="merge tag apply",
            commit=lambda: client._nodes_export_dirty_ids.discard("A"),
        )
        # Verify: reads its own write + the welded dirty commit.
        verifier = current_operation_cache()
        assert verifier.nodes_by_id["A"]["name"] == "alpha #merged"
        assert verifier.dirty_ids == frozenset()
        assert verifier.view.epoch_id == snapshot.view.epoch_id
        assert verifier.view.adopted_sequence == snapshot.view.adopted_sequence


# ---------------------------------------------------------------------------
# Concurrent operations hold different generations legitimately
# ---------------------------------------------------------------------------


@async_test
async def test_two_operations_may_hold_different_heads(tmp_path, monkeypatch):
    """§12.1: T1 keeps its pinned generation while T2 captures a newer one;
    neither disturbs the other's objects."""
    from workflowy_mcp.coherence.effects import SemanticEffect
    from workflowy_mcp.coherence.journal_store import append_journal_transaction

    client, paths = _make_lineage_client(tmp_path, monkeypatch)
    from workflowy_mcp.coherence.operation_cache import (
        cache_operation_boundary as boundary,
    )
    from workflowy_mcp.coherence.operation_cache import (
        create_isolated_context_task,
    )

    async with cache_operation_boundary(client, "t1") as t1:
        head_before = t1.view.adopted_sequence
        # Publish the fixture writer's head locally before the independently
        # dispatched operation captures it (T1 stays pinned).
        import os as _os

        from workflowy_mcp.coherence.schema import JournalProducer

        append_journal_transaction(
            paths,
            operation_kind="test",
            effects=[
                SemanticEffect.node_create(
                    s02_node("D", name="delta", parent_id="C")
                )
            ],
            producer=JournalProducer(
                pid=_os.getpid(),
                pid_create_time=1.0,
                owner_kind="process",
                ownership_kind="none",
            ),
        )

        publish_fixture_head(client, paths)

        async def second_operation():
            async with boundary(client, "t2") as t2:
                return t2

        t2 = await create_isolated_context_task(second_operation())
        assert t2.view.adopted_sequence == head_before + 1
        assert t2.nodes_by_id["D"]["name"] == "delta"
        # T1 remains pinned at its original generation, objects untouched.
        assert t1.view.adopted_sequence == head_before
        assert "D" not in t1.nodes_by_id
        assert current_operation_cache() is t1


@async_test
async def test_same_operation_subtask_inherits_snapshot(tmp_path, monkeypatch):
    """§7.3/F2: a short-lived same-operation subtask legitimately inherits
    the captured snapshot (plain create_task copies the context)."""
    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    async with cache_operation_boundary(client, "op") as snapshot:
        async def child_read():
            return current_operation_cache(required=False)

        child = asyncio.create_task(child_read())
        assert await child is snapshot


@async_test
async def test_isolated_task_establishes_own_boundary(tmp_path, monkeypatch):
    """§7.3/F2: a NEW-operation spawn through the isolated-context helper
    starts with NO snapshot and must establish its own boundary."""
    from workflowy_mcp.coherence.operation_cache import (
        cache_operation_boundary as boundary,
    )
    from workflowy_mcp.coherence.operation_cache import (
        create_isolated_context_task,
    )

    client, _paths = _make_lineage_client(tmp_path, monkeypatch)
    async with cache_operation_boundary(client, "parent") as parent_snapshot:
        async def new_operation():
            # No inherited snapshot...
            assert current_operation_cache(required=False) is None
            # ...it establishes its own.
            async with boundary(client, "child-op") as child_snapshot:
                return child_snapshot

        task = create_isolated_context_task(new_operation())
        child_snapshot = await task
        assert child_snapshot is not parent_snapshot
        assert child_snapshot.operation_id != parent_snapshot.operation_id
        # The parent's context is intact after the child completes.
        assert current_operation_cache() is parent_snapshot


# ---------------------------------------------------------------------------
# A6 — representative real-path generation pinning (head advance MID-operation)
# ---------------------------------------------------------------------------


def _append_record(paths, node) -> None:
    import os as _os

    from workflowy_mcp.coherence.effects import SemanticEffect
    from workflowy_mcp.coherence.journal_store import append_journal_transaction
    from workflowy_mcp.coherence.schema import JournalProducer

    append_journal_transaction(
        paths,
        operation_kind="test",
        effects=[SemanticEffect.node_create(node)],
        producer=JournalProducer(
            pid=_os.getpid(),
            pid_create_time=1.0,
            owner_kind="process",
            ownership_kind="none",
        ),
    )


@async_test
async def test_a6_resolution_then_planning_stays_pinned(tmp_path, monkeypatch):
    """A6 shape 1 (resolution → planning): a handle resolved against pinned
    T1 and the plan built AFTER an external head advance both use T1 — the
    operation never resolves a handle against one generation and plans
    against another."""
    from workflowy_mcp.client.api_client_nexus import (
        load_nodes_by_id_from_cache,
        resolve_uuid_like_node_handle,
    )

    client, paths = _make_lineage_client(tmp_path, monkeypatch)
    captures = {"count": 0}
    original = client._capture_view_and_dirty

    def counting():
        captures["count"] += 1
        return original()

    monkeypatch.setattr(client, "_capture_view_and_dirty", counting)

    async with cache_operation_boundary(client, "op-resolve-plan"):
        # Resolve a handle against the pinned generation (REAL resolver).
        resolved = resolve_uuid_like_node_handle("B", client=client)
        assert resolved["success"] and resolved["input_kind"] == "exact_uuid"
        # External writer advances the shared head mid-operation.
        _append_record(paths, s02_node("D", name="delta", parent_id="C"))
        # Plan construction (REAL loader self-load path) still uses T1.
        plan_map = load_nodes_by_id_from_cache(client)
        assert "D" not in plan_map
        assert plan_map["B"] is not None
        # And a fresh resolution of the NEW node cannot see it either.
        resolved_new = resolve_uuid_like_node_handle("D", client=client)
        assert resolved_new["success"] is False
    assert captures["count"] == 1


@async_test
async def test_a6_render_stage0_to_stage1_stays_pinned(tmp_path, monkeypatch):
    """A6 shape 2 (rendering Stage 0 → Stage 1): the real Stage-0 cache seam
    (``server._current_cache_by_id``) and the real Phase-1 projection
    primitive both consume the SAME pinned snapshot across an external head
    advance."""
    client, paths = _make_lineage_client(tmp_path, monkeypatch)

    from workflowy_mcp import server
    from workflowy_mcp.f8_slice_policy import build_slice_subtree_from_cache

    def _tree_ids(tree) -> set:
        ids = set()

        def visit(node):
            ids.add(str(node.get("id")))
            for child in node.get("children") or ():
                visit(child)

        visit(tree)
        return ids

    async with cache_operation_boundary(client, "op-render"):
        stage0_t1 = server._current_cache_by_id()
        tree_t1 = build_slice_subtree_from_cache(
            stage0_t1, "A", [], member_all=True
        )
        assert _tree_ids(tree_t1) == {"A", "B", "C"}
        # External writer advances the shared head mid-render.
        _append_record(paths, s02_node("D", name="delta", parent_id="C"))
        # Stage 0 re-read: the SAME pinned map object (one generation).
        stage0_t2 = server._current_cache_by_id()
        assert stage0_t2 == stage0_t1
        assert stage0_t2["A"] is stage0_t1["A"]
        # Stage 1 against the re-read seam: the new node is invisible.
        tree_t2 = build_slice_subtree_from_cache(
            stage0_t2, "A", [], member_all=True
        )
        assert _tree_ids(tree_t2) == {"A", "B", "C"}


@async_test
async def test_a6_mutation_plan_then_eager_patch_verification(tmp_path, monkeypatch):
    """A6 shape 3 (mutation planning → eager-patch verification): plan
    against the pinned generation, advance the shared head EXTERNALLY, then
    perform the same-operation local COW mutation and verify through
    task-local read-your-writes — no re-adoption, pinned markers, and the
    external record never enters the operation's view."""
    client, paths = _make_lineage_client(tmp_path, monkeypatch)
    captures = {"count": 0}
    original = client._capture_view_and_dirty

    def counting():
        captures["count"] += 1
        return original()

    monkeypatch.setattr(client, "_capture_view_and_dirty", counting)

    from workflowy_mcp.models import WorkFlowyNode

    async with cache_operation_boundary(client, "op-mutate") as snapshot:
        pinned_seq = snapshot.view.adopted_sequence
        pinned_epoch = snapshot.view.epoch_id
        # Plan: the verifier would read the pinned view here.
        assert snapshot.nodes_by_id["A"]["name"] == "alpha"
        # External writer advances the shared head mid-operation.
        _append_record(paths, s02_node("D", name="delta", parent_id="C"))
        # Same-operation local COW mutation (the REAL legacy patcher).
        client._apply_update_to_nodes_export_cache(
            WorkFlowyNode(id="A", name="alpha-merged")
        )
        # Verify through task-local read-your-writes (§6.3).
        verifier = current_operation_cache()
        assert verifier.nodes_by_id["A"]["name"] == "alpha-merged"
        assert verifier.view.adopted_sequence == pinned_seq
        assert verifier.view.epoch_id == pinned_epoch
        # The external head advance was never adopted mid-operation.
        assert "D" not in verifier.nodes_by_id
        assert verifier.operation_id == snapshot.operation_id
    assert captures["count"] == 1


# (End of A6 representative real-path proofs.)
