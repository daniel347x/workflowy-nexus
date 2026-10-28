"""N01 allocation/semantic evidence; baseline oracle is a frozen git blob."""
from __future__ import annotations

import copy
import hashlib
import ast
import asyncio
import dataclasses
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

import n01_reference as reference
from n01_copy_fixture import canonical_bytes, effect_tail
from workflowy_mcp.coherence.effects import (
    ApplicationMode, EffectWorkingCopy, SemanticEffect, apply_semantic_effects,
    apply_journal_transaction,
)
from workflowy_mcp.coherence.cache_view import CacheView, CacheViewPublisher, prepare_cow_payload
from workflowy_mcp.coherence.errors import CacheViewError, EffectApplicationError, JournalIntegrityError, CoherenceAccountMismatchError
from workflowy_mcp.coherence import effects as engine
from workflowy_mcp.coherence.snapshot_store import materialize_payload_with_records


@pytest.mark.parametrize("which", ["EFFECTS", "TRANSITION_EFFECTS"])
def test_reference_literal_is_the_recorded_git_blob(which):
    source = getattr(reference, f"{which}_SOURCE").encode("utf-8")
    actual = hashlib.sha1(f"blob {len(source)}\0".encode() + source).hexdigest()
    assert actual == getattr(reference, f"{which}_BLOB")


def _outcome(reducer, model, mode, payload, effects):
    diagnostics = []
    converted = [model(effect.effect_type, effect.payload) for effect in effects]
    try:
        result = reducer(payload, converted, mode=mode, diagnostics=diagnostics)
    except Exception as exc:
        return ("error", type(exc).__name__, str(exc), vars(exc), diagnostics)
    return ("success", result, diagnostics)


def _cases():
    a = {"id": "a", "parent_id": "p", "priority": 100, "name": "before",
         "data": {"nested": [None, {"deep": [1, 2]}]}, "future": {"x": [3]}}
    b = {"id": "b", "parent_id": "p", "priority": 200, "note": None}
    base = {"nodes": [a, b], "metadata": {"nested": [None, {"a": "📌"}]}}
    field = SemanticEffect.node_fields
    order = SemanticEffect.child_order
    create = SemanticEffect.node_create
    delete = SemanticEffect.tombstone
    parent = SemanticEffect.node_parentage
    return [
        (base, [field("a", ["name", "data"], {"name": "after <x>", "data": {"a": [1]}})]),
        (base, [parent("a", None)]),
        (base, [delete(["a", "absent"])]),
        (base, [create({"id": "c", "name": "new", "data": {"nested": [None]}})]),
        (base, [create({"id": "a", "name": "replace"})]),
        (base, [field("absent", ["note"], {"note": None})]),
        (base, [order("p", ["b", "a"])]),
        (base, [order("p", ["a", "b"], priority_by_id={"a": 10, "b": 20})]),
        (base, [order("p", ["a", "b"], priority_by_id={"a": 20, "b": 10})]),
        (base, [order("p", ["a", "missing"])]),
        (base, [field("a", ["name"], {"name": "owned"}), order(
            "p", ["a", "b"], canonical_records={"a": {"id": "a", "name": "owned"}})]),
        (base, [create({"id": "c", "name": "new"}), order(
            "p", ["b", "a", "c"], canonical_records={"c": {"id": "c", "name": "new"}})]),
        (base, [field("a", ["name"], {"name": "x"}),
                field("a", ["name"], {"name": "y"})]),
        (base, [field("a", ["note"], {"note": "earlier"}), delete(["a"])]),
        ({"nodes": [{"id": "a", "name": "first"}, {"id": "a", "name": "last"}]},
         [field("a", ["note"], {"note": "last only"})]),
        ({"nodes": [{"id": "a"}, {"id": "a"}, {"id": "b"}]}, [delete(["a"])]),
        ({"nodes": [{"name": "idless", "parent_id": "p"}, b]}, [order("p", ["b"])]),
        ({"nodes": [{"id": 1, "parent_id": "p"}, {"id": "1", "parent_id": "p"}]},
         [field("1", ["name"], {"name": "string lookup"})]),
        ({"nodes": [{"id": True}, {"id": 1}, {"id": 1.0}]}, [delete(["1"])]),
        ({"nodes": [{"id": "a", "parent_id": ["not", "hashable"]}, b]},
         [order("p", ["b"])]),
    ]


@pytest.mark.parametrize("case", range(20))
@pytest.mark.parametrize("mode", list(ApplicationMode))
def test_baseline_differential_matrix(case, mode):
    payload, effects = _cases()[case]
    before = canonical_bytes(payload)
    old = _outcome(reference.apply_semantic_effects, reference.SemanticEffect,
                   reference.ApplicationMode(mode.value), copy.deepcopy(payload), effects)
    new = _outcome(apply_semantic_effects, SemanticEffect, mode, payload, effects)
    assert canonical_bytes(new) == canonical_bytes(old)
    assert canonical_bytes(payload) == before


def test_tail_recipe_has_the_prescribed_five_record_pattern():
    tail = effect_tail()
    assert len(tail) == 500
    assert [effects[0].effect_type for effects in tail[:5]] == [
        "node_fields", "node_parentage", "child_order", "node_create", "tombstone"]
    for cycle in range(100):
        created = tail[cycle * 5 + 3][0].payload["node"]["id"]
        assert tail[cycle * 5 + 4][0].payload["node_ids"] == [created]
    assert len(tail[2][0].payload["ordered_child_ids"]) == 111


def _view(payload=None, **kwargs):
    return CacheView.build(epoch_number=1, epoch_id="e" * 32, adopted_sequence=0,
                           nodes_export=payload or {"nodes": [{"id": "a"}, {"id": "b", "data": {"x": [1]}}]}, **kwargs)


def test_borrowed_and_transferred_construction_are_distinct(monkeypatch):
    from workflowy_mcp.coherence import cache_view as module
    payload = {"nodes": [{"id": 123, "data": {"x": [1]}}], "metadata": {"x": [2]}}
    borrowed = _view(payload)
    payload["nodes"][0]["data"]["x"].append(9)
    payload["metadata"]["x"].append(9)
    assert borrowed.nodes_by_id["123"]["data"]["x"] == [1]
    assert borrowed.nodes_export["metadata"]["x"] == [2]
    # Required production-shape test only; never used to make sharing work.
    monkeypatch.setattr(module, "_TEST_DEEP_FREEZE", False)
    owned = {"nodes": [{"id": 1, "data": {"x": [1]}}], "meta": {"a": [1]}}
    transferred = _view(owned, ownership="transferred")
    assert transferred.nodes_export is owned
    assert transferred.nodes_export["nodes"] is owned["nodes"]
    assert transferred.nodes_by_id["1"] is owned["nodes"][0]
    assert owned["nodes"][0]["id"] == "1"
    with pytest.raises(CacheViewError):
        _view(borrowed.nodes_export, ownership="transferred")
    with pytest.raises(CacheViewError):
        _view(owned, ownership="guess")


def test_prepare_detaches_retained_fragments_and_preserves_unchanged_nodes():
    before = _view({"nodes": [{"id": "a"}, {"id": "b"}], "meta": {"nested": [1]}})
    retained = {"id": "a", "data": {"caller": [1]}}
    metadata = {"caller": [2]}
    candidate = {"nodes": [retained, before.nodes_by_id["b"]], "meta": before.nodes_export["meta"], "new": metadata}
    prepared = prepare_cow_payload(before, candidate)
    assert prepared["meta"] is before.nodes_export["meta"]
    assert prepared["nodes"][1] is before.nodes_by_id["b"]
    after = _view(prepared, ownership="transferred")
    retained["data"]["caller"].append(9)
    metadata["caller"].append(9)
    candidate["nodes"].clear()
    assert after.nodes_by_id["a"]["data"]["caller"] == [1]
    assert after.nodes_export["new"]["caller"] == [2]
    assert after.nodes_by_id["b"] is before.nodes_by_id["b"]


@pytest.mark.parametrize("case", range(20))
@pytest.mark.parametrize("mode", list(ApplicationMode))
def test_working_copy_matches_frozen_matrix_and_is_atomic(case, mode):
    payload, effects = _cases()[case]
    workspace = EffectWorkingCopy.from_payload(payload, ownership="borrowed")
    old = _outcome(reference.apply_semantic_effects, reference.SemanticEffect,
                   reference.ApplicationMode(mode.value), copy.deepcopy(payload), effects)
    before = canonical_bytes(workspace._state.payload)
    if old[0] == "error":
        diagnostics = []
        with pytest.raises(Exception) as caught:
            apply_semantic_effects(workspace, effects, mode=mode, diagnostics=diagnostics, input_mode="working_copy")
        exc = caught.value
        assert canonical_bytes(("error", type(exc).__name__, str(exc), vars(exc), diagnostics)) == canonical_bytes(old)
        assert canonical_bytes(workspace._state.payload) == before
    else:
        diagnostics = []
        assert apply_semantic_effects(workspace, effects, mode=mode, diagnostics=diagnostics, input_mode="working_copy") is workspace
        assert canonical_bytes(("success", workspace.finish(), diagnostics)) == canonical_bytes(old)


def test_late_application_failure_preserves_indexes_slots_and_diagnostics():
    payload = {"nodes": [{"id": "a", "name": "before"}, {"id": "b"}]}
    workspace = EffectWorkingCopy.from_payload(payload, ownership="borrowed")
    state = workspace._state
    before = canonical_bytes((state.slots, state.lookup, {k: sorted(v) for k, v in state.groups.items()}, {k: sorted(v) for k, v in state.parents.items()}))
    diagnostics = []
    with pytest.raises(EffectApplicationError, match="already-existing"):
        apply_semantic_effects(workspace, [SemanticEffect.tombstone(["absent"]),
            SemanticEffect.node_fields("a", ["name"], {"name": "staged"}), SemanticEffect.node_create({"id": "b"})],
            mode=ApplicationMode.PUBLICATION_STRICT, input_mode="working_copy", diagnostics=diagnostics)
    assert diagnostics[0]["kind"] == "absent_target"
    assert canonical_bytes((state.slots, state.lookup, {k: sorted(v) for k, v in state.groups.items()}, {k: sorted(v) for k, v in state.parents.items()})) == before
    apply_semantic_effects(workspace, [SemanticEffect.node_fields("a", ["name"], {"name": "success"})], mode=ApplicationMode.PUBLICATION_STRICT, input_mode="working_copy")
    assert workspace.finish()["nodes"][0]["name"] == "success"
    with pytest.raises(EffectApplicationError):
        workspace.finish()


def test_cow_copies_only_touched_nodes_once_per_transaction(monkeypatch):
    payload = {"nodes": [{"id": str(i), "data": {"nested": [i]}} for i in range(50)]}
    copied = []
    real = copy.deepcopy
    def spy(value, *args, **kwargs):
        if isinstance(value, dict) and "id" in value:
            copied.append(value["id"])
        return real(value, *args, **kwargs)
    monkeypatch.setattr(engine.copy, "deepcopy", spy)
    result = apply_semantic_effects(payload, [SemanticEffect.node_fields("1", ["name"], {"name": "x"}), SemanticEffect.node_fields("1", ["note"], {"note": "y"})], mode=ApplicationMode.PUBLICATION_STRICT)
    assert copied == ["1"]
    assert result["nodes"][1] is not payload["nodes"][1]
    assert all(after is before for i, (before, after) in enumerate(zip(payload["nodes"], result["nodes"])) if i != 1)


@pytest.mark.parametrize("count", [0, 1, 200, 500])
def test_s2_one_acquisition_index_and_finish(count, monkeypatch):
    from_payload = EffectWorkingCopy.from_payload.__func__
    init = engine._EffectState.__init__
    finish = engine._EffectState.finish
    real_copy = copy.deepcopy
    counts = {"acquire": 0, "index": 0, "finish": 0, "full_copy": 0}
    def acquire(cls, payload, **kwargs):
        counts["acquire"] += 1
        return from_payload(cls, payload, **kwargs)
    def index(self, payload):
        counts["index"] += 1
        return init(self, payload)
    def done(self):
        counts["finish"] += 1
        return finish(self)
    def clone(value, *args, **kwargs):
        if isinstance(value, dict) and isinstance(value.get("nodes"), list):
            counts["full_copy"] += 1
        return real_copy(value, *args, **kwargs)
    monkeypatch.setattr(EffectWorkingCopy, "from_payload", classmethod(acquire))
    monkeypatch.setattr(engine._EffectState, "__init__", index)
    monkeypatch.setattr(engine._EffectState, "finish", done)
    monkeypatch.setattr(engine.copy, "deepcopy", clone)
    payload = {"nodes": [{"id": "a", "data": {"x": [1]}}]}
    records = [SimpleNamespace(sequence=i+1, account_scope_id="a"*32,
               effects=[SemanticEffect.node_fields("a", ["note"], {"note": str(i)})]) for i in range(count)]
    result = materialize_payload_with_records(payload, records, start_sequence=1, account_scope_id="a"*32)
    assert counts == {"acquire": 1, "index": 1, "finish": 1, "full_copy": 1}
    assert result["nodes"][0] is not payload["nodes"][0]


def test_child_order_visits_only_parent_members_after_initialization(monkeypatch):
    visits = []
    class Counted(dict):
        def get(self, key, *args):
            visits.append(self["id"])
            return super().get(key, *args)
    payload = {"nodes": [Counted(id=str(i), parent_id="p" if i < 111 else "other") for i in range(1000)]}
    effect = SemanticEffect.child_order("p", [str(i) for i in range(111)])
    totals = []
    for count in (1, 500):
        workspace = EffectWorkingCopy.from_payload(payload, ownership="borrowed")
        visits.clear()
        apply_semantic_effects(workspace, [effect], mode=ApplicationMode.REPLAY_AUTHORITATIVE, input_mode="working_copy")
        first = list(visits)
        for _ in range(count - 1):
            apply_semantic_effects(workspace, [effect], mode=ApplicationMode.REPLAY_AUTHORITATIVE, input_mode="working_copy")
        assert all(int(node_id) < 111 for node_id in visits)
        # COW replacement may turn counted dicts into plain dicts; the crucial
        # rejecting witness is that no unrelated account member is consulted.
        assert all(int(node_id) < 111 for node_id in first)
        totals.append(len(visits))
    assert totals[0] == totals[1]


def test_journal_selectors_sequence_and_consumption():
    payload = {"nodes": [{"id": "a"}]}
    record = SimpleNamespace(sequence=1, account_scope_id="a"*32, effects=[SemanticEffect.node_fields("a", ["name"], {"name": "x"})])
    workspace = EffectWorkingCopy.from_payload(payload, ownership="borrowed")
    with pytest.raises(EffectApplicationError):
        apply_journal_transaction(workspace, record, expected_sequence=1)
    with pytest.raises(JournalIntegrityError):
        apply_journal_transaction(workspace, record, expected_sequence=2, input_mode="working_copy")
    with pytest.raises(CoherenceAccountMismatchError):
        apply_journal_transaction(workspace, record, expected_sequence=1, expected_account="b"*32, input_mode="working_copy")
    assert apply_journal_transaction(workspace, record, expected_sequence=1, expected_account="a"*32, input_mode="working_copy") is workspace
    workspace.finish()
    with pytest.raises(EffectApplicationError):
        apply_journal_transaction(workspace, record, expected_sequence=1, input_mode="working_copy")
    view = _view(payload, account_scope_id="a"*32)
    successor = apply_journal_transaction(view, record, expected_sequence=1)
    assert successor.adopted_sequence == 1
    assert apply_journal_transaction(successor, record, expected_sequence=1) is successor


def test_production_transfer_and_single_engine_ast_fence():
    import os
    root = Path(__file__).resolve().parents[1] / "src" / "workflowy_mcp"
    builders, callers = [], []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if os.environ.get("CUT2B_REINTRODUCE_ADOPTION_BUILDER") and path.name == "cache_view.py":
            source += '\ndef retired_adopter():\n    return CacheView.build(ownership="transferred")\n'
        if os.environ.get("CUT2B_REINTRODUCE_SNAPSHOT_SELECTOR") and path.name == "server.py":
            source += '\ndef _select_manifest_linked_snapshot(client):\n    return client._publish_lineage_view(payload, ownership="transferred")\n'
        tree = ast.parse(source)
        assert not any(isinstance(n, (ast.Import, ast.ImportFrom)) and "n01_reference" in ast.unparse(n) for n in ast.walk(tree))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if ast.unparse(node.func) == "CacheView.build":
                    builders.append((path, node.lineno))
                    assert any(k.arg == "ownership" for k in node.keywords), (path, node.lineno)
                if node.func.attr == "_publish_lineage_view":
                    callers.append((path, node.lineno))
                    assert any(k.arg == "ownership" and isinstance(k.value, ast.Constant) and k.value.value == "transferred" for k in node.keywords)
    # Two engine-only builders retired; the sentinel's one build moved intact.
    assert len(builders) == 6
    assert len(callers) == 3
    source = ast.parse((root / "coherence" / "snapshot_store.py").read_text())
    s2 = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "materialize_payload_with_records")
    loops = [n for n in ast.walk(s2) if isinstance(n, ast.For)]
    assert len(loops) == 1
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("from_payload", "finish") for n in ast.walk(loops[0]))
    tree = ast.parse((root / "coherence" / "effects.py").read_text())
    for name in ("_apply_effect_transaction", "_validate_transaction_effects"):
        assert sum(isinstance(n, ast.FunctionDef) and n.name == name for n in ast.walk(tree)) == 1


def test_bridge_retry_shares_unchanged_nodes_and_commits_only_the_winner(monkeypatch):
    from workflowy_mcp.coherence.operation_cache import publish_local_cache_view
    before = _view({"nodes": [{"id": "a", "name": "old"}, {"id": "b", "name": "old"}, {"id": "c", "data": {"x": [1]}}]})
    publisher = CacheViewPublisher(before)
    original = publisher.publish_with_commit
    attempts, commits, samples = [], [], []
    def race(candidate, **kwargs):
        attempts.append(candidate)
        if len(attempts) == 1:
            rival = dict(before.nodes_export)
            rival["nodes"] = [before.nodes_by_id["a"], dict(before.nodes_by_id["b"], name="rival"), before.nodes_by_id["c"]]
            winner = _view(prepare_cow_payload(before, rival), ownership="transferred")
            original(winner, expected_current=before)
        return original(candidate, **kwargs)
    monkeypatch.setattr(publisher, "publish_with_commit", race)
    def transform(working):
        working["nodes"] = [dict(n, name="own") if n["id"] == "a" else n for n in working["nodes"]]
    result = publish_local_cache_view(publisher, transform, reason="n01 test", commit=lambda effects: commits.append(effects), snapshot_dirty_ids=lambda: samples.append("sample") or frozenset({"a"}))
    assert result.nodes_by_id["a"]["name"] == "own"
    assert result.nodes_by_id["b"]["name"] == "rival"
    assert len(attempts) == 2
    assert len(commits) == len(samples) == 1
    assert all(view.nodes_by_id["c"] is before.nodes_by_id["c"] for view in attempts)


def test_transition_lazy_comparison_and_create_fragments_are_owned(monkeypatch):
    from workflowy_mcp.coherence import transition_effects as transition
    before = _view()
    retained = {"id": "c", "data": {"nested": [1]}}
    effects = transition.derive_transition_effects(before.nodes_export["nodes"], [*before.nodes_export["nodes"], retained])
    retained["data"]["nested"].append(9)
    assert effects["c"].record["data"]["nested"] == [1]
    counts = []
    canonical = transition._canonical
    monkeypatch.setattr(transition, "_canonical", lambda node: counts.append(node["id"]) or canonical(node))
    merged = transition.merge_offer_with_transition_effects(before, effects, publication_kind="checkpoint")
    assert counts == []
    assert merged.rebased.nodes_by_id["b"] is before.nodes_by_id["b"]
    assert merged.rebased.nodes_by_id["c"]["data"]["nested"] == [1]


def test_newer_lineage_overlay_shares_the_pinned_generation(tmp_path, monkeypatch):
    from test_coherence_s03_transition_cow import _make_lineage_client
    from workflowy_mcp.coherence import operation_cache as boundary
    nodes = [{"id": "a", "name": "old-a"}, {"id": "b", "name": "old-b"},
             {"id": "c", "data": {"x": [1]}}]
    client, paths = _make_lineage_client(tmp_path, monkeypatch, nodes=nodes)
    async def exercise():
        async with boundary.cache_operation_boundary(client, "n01-overlay-test"):
            pinned = boundary._OPERATION_CACHE_SNAPSHOT.get()
            ready, release = asyncio.Event(), asyncio.Event()
            async def sibling():
                ready.set()
                await release.wait()
                assert boundary._OPERATION_CACHE_SNAPSHOT.get() is pinned
            task = asyncio.create_task(sibling())
            await ready.wait()
            client._publish_lineage_view({"nodes": [dict(n, name="new-b") if n["id"] == "b" else n for n in nodes]},
                epoch_number=1, epoch_id="f"*32, adopted_sequence=0, snapshot_timestamp=None)
            def patch(working):
                working["nodes"] = [dict(n, name="own-a") if n["id"] == "a" else n for n in working["nodes"]]
            published = client.publish_local_cache_view(patch, reason="overlay test")
            own = boundary._OPERATION_CACHE_SNAPSHOT.get()
            assert own.view.epoch_number == pinned.view.epoch_number == 0
            assert own.operation_id == pinned.operation_id
            assert own.owner is pinned.owner
            assert own.view.nodes_by_id["a"]["name"] == "own-a"
            assert own.view.nodes_by_id["b"]["name"] == "old-b"
            assert own.view.nodes_by_id["b"] is pinned.view.nodes_by_id["b"]
            assert own.view.nodes_by_id["c"] is pinned.view.nodes_by_id["c"]
            assert published.nodes_by_id["b"]["name"] == "new-b"
            release.set()
            await task
        assert boundary._OPERATION_CACHE_SNAPSHOT.get() is None
    asyncio.run(exercise())


def test_racing_lineage_offer_keeps_local_effect_and_lazy_unrelated_identity(tmp_path, monkeypatch):
    from test_coherence_s03_transition_cow import _make_client
    client, _, _ = _make_client(tmp_path, monkeypatch)
    client._publish_lineage_view({"nodes": [{"id": "a", "name": "old"}, {"id": "b"}]}, epoch_number=1, epoch_id="e"*32, adopted_sequence=0, snapshot_timestamp=None)
    client.publish_local_cache_view(lambda work: work.update(nodes=[dict(n, name="own") if n["id"] == "a" else n for n in work["nodes"]]), reason="race local effect")
    result = client._publish_lineage_view({"nodes": [{"id": "a", "name": "old"}, {"id": "b", "note": "server"}]}, epoch_number=2, epoch_id="f"*32, adopted_sequence=0, snapshot_timestamp=None, publication_kind="full_refresh")
    assert result.nodes_by_id["a"]["name"] == "own"
    assert result.nodes_by_id["b"]["note"] == "server"
    assert "a" in client._nodes_export_dirty_ids


@pytest.mark.parametrize("count", [200, 500])
@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_s2_bad_record_returns_no_partial_payload(count, position, monkeypatch):
    payload = {"nodes": [{"id": "a", "name": "before"}], "meta": {"nested": [1]}}
    before = canonical_bytes(payload)
    records = [SimpleNamespace(sequence=i+1, account_scope_id="a"*32,
        effects=[SemanticEffect.node_fields("a", ["name"], {"name": str(i)})]) for i in range(count)]
    bad = {"first": 0, "middle": count//2, "last": count-1}[position]
    records[bad].sequence += 1
    finished = []
    original = EffectWorkingCopy.finish
    monkeypatch.setattr(EffectWorkingCopy, "finish", lambda self: finished.append(True) or original(self))
    with pytest.raises(JournalIntegrityError):
        materialize_payload_with_records(payload, records, start_sequence=1, account_scope_id="a"*32)
    assert not finished
    assert canonical_bytes(payload) == before


def test_transferred_workspace_acquisition_does_not_copy(monkeypatch):
    payload = {"nodes": [{"id": "a", "data": {"x": [1]}}], "meta": {"x": [2]}}
    def forbidden_copy(*args, **kwargs):
        raise AssertionError("transferred acquisition copied payload")
    monkeypatch.setattr(engine.copy, "deepcopy", forbidden_copy)
    workspace = EffectWorkingCopy.from_payload(payload, ownership="transferred")
    assert workspace.finish() is payload


def test_raw_mapping_node_keeps_baseline_plain_json_output():
    raw = {"id": "a", "note": "before", "data": {"x": [1]}}
    payload = {"nodes": [MappingProxyType(raw), {"id": "b"}]}
    effects = [SemanticEffect.node_fields("b", ["name"], {"name": "changed"})]
    old = _outcome(reference.apply_semantic_effects, reference.SemanticEffect,
        reference.ApplicationMode.REPLAY_AUTHORITATIVE, payload, effects)
    new = _outcome(apply_semantic_effects, SemanticEffect,
        ApplicationMode.REPLAY_AUTHORITATIVE, payload, effects)
    assert canonical_bytes(new) == canonical_bytes(old)
    assert type(new[1]["nodes"][0]) is dict
    assert raw == {"id": "a", "note": "before", "data": {"x": [1]}}


def test_parent_membership_updates_do_not_scan_unrelated_slots():
    comparisons = []
    class CountedSlot(int):
        __hash__ = int.__hash__
        def __eq__(self, other):
            comparisons.append(int(self))
            return int.__eq__(self, other)
    workspace = EffectWorkingCopy.from_payload({"nodes": [
        {"id": str(i), "parent_id": "wide"} for i in range(1000)]}, ownership="borrowed")
    bucket = workspace._state.parents["wide"]
    wrapped = [CountedSlot(slot) for slot in bucket]
    workspace._state.parents["wide"] = ({slot: None for slot in wrapped}
        if isinstance(bucket, dict) else type(bucket)(wrapped))
    apply_semantic_effects(workspace, [SemanticEffect.node_fields("999", ["note"], {"note": "field-only"})],
        mode=ApplicationMode.PUBLICATION_STRICT, input_mode="working_copy")
    assert len(comparisons) <= 2, "field-only commit scanned unrelated parent memberships"
    apply_semantic_effects(workspace, [SemanticEffect.node_parentage("999", "other")],
        mode=ApplicationMode.PUBLICATION_STRICT, input_mode="working_copy")
    assert len(comparisons) <= 4, "single reparent scanned unrelated parent memberships"
    assert workspace.finish()["nodes"][-1]["parent_id"] == "other"


def _plain(value):
    if dataclasses.is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(item) for item in value)
    return value


@pytest.mark.parametrize("kind", ["full_refresh", "checkpoint", "compat_patch", "job_basis"])
@pytest.mark.parametrize("scenario", range(8))
def test_transition_derivation_composition_and_merge_match_frozen_oracle(kind, scenario):
    from workflowy_mcp.coherence import transition_effects as transition
    before = [{"id": "a", "name": "old", "note": None},
              {"id": "b", "parent_id": "a", "name": "child"}, {"id": "c", "name": "other"}]
    after = copy.deepcopy(before)
    if scenario < 4:
        after[0]["name"] = "middle"
    elif scenario < 6:
        after.append({"id": "d", "name": "created", "data": {"nested": [None, 1]}})
    else:
        after = [before[2]]
    later = copy.deepcopy(after)
    if scenario < 4:
        later[0]["name"] = "latest"
    elif scenario < 6:
        later[-1]["note"] = "post-create"
    offered = copy.deepcopy(before)
    if scenario == 0:
        offered[0]["name"] = "old"
    elif scenario == 1:
        offered[0]["name"] = "middle"
    elif scenario == 2:
        offered[0]["name"] = "latest"
    elif scenario == 3:
        offered[0]["name"] = "conflict"
    elif scenario == 5:
        offered.append(copy.deepcopy(after[-1]))
    elif scenario >= 6:
        offered[1]["parent_id"] = "c"  # an escaped descendant survives
        offered.append({"id": "unknown", "parent_id": "a"})
    outcomes = []
    for module in (reference, transition):
        effects = module.derive_transition_effects(before, after)
        for node_id, new in module.derive_transition_effects(after, later).items():
            effects[node_id] = module.compose_transition_effect(effects.get(node_id), new)
        first = module.merge_offer_with_transition_effects(_view({"nodes": copy.deepcopy(offered)}), effects, publication_kind=kind)
        second = module.merge_offer_with_transition_effects(_view({"nodes": copy.deepcopy(offered)}), first.surviving_effects, publication_kind=kind)
        outcomes.append(_plain((effects, first, second)))
    assert canonical_bytes(outcomes[0]) == canonical_bytes(outcomes[1])
