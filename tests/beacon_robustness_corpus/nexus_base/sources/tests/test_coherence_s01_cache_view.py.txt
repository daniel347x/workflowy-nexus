"""S01 §13.5 — CacheView and copy-on-write publication tests."""

from __future__ import annotations

import os
import threading

import pytest

from workflowy_mcp.coherence import (
    ApplicationMode,
    CacheView,
    CacheViewPublisher,
    SemanticEffect,
    apply_journal_transaction,
)
from workflowy_mcp.coherence.errors import (
    CacheViewError,
    CoherenceAccountMismatchError,
    EffectApplicationError,
    JournalIntegrityError,
)
from workflowy_mcp.coherence.schema import JournalProducer, JournalTransaction

NOW = "2026-11-01T00:00:00+00:00"
EPOCH = "e" * 32


def _payload() -> dict:
    return {
        "nodes": [
            {"id": "A", "name": "alpha", "priority": 100},
            {"id": "B", "name": "beta", "priority": 200, "note": "b"},
        ],
        "_meta": 1,
    }


def _view(payload=None, seq: int = 0) -> CacheView:
    return CacheView.build(
        epoch_number=1, epoch_id=EPOCH, adopted_sequence=seq, nodes_export=payload or _payload()
    )


def _tx(sequence: int, effects, *, account: str = "a" * 32) -> JournalTransaction:
    return JournalTransaction(
        schema_version=1,
        account_scope_id=account,
        sequence=sequence,
        previous_sequence=sequence - 1,
        transaction_id=f"tx{sequence:030d}",
        published_epoch_id=EPOCH,
        operation_kind="test",
        committed_at=NOW,
        producer=JournalProducer(
            pid=os.getpid(), pid_create_time=1.0, owner_kind="process", ownership_kind="none"
        ),
        verification=(),
        effects=tuple(effects),
    )


def test_index_and_payload_share_owned_identity_within_view():
    view = _view()
    node_from_index = view.nodes_by_id["A"]
    node_from_list = next(n for n in view.nodes_export["nodes"] if n["id"] == "A")
    assert node_from_index is node_from_list


def test_caller_mutation_after_build_cannot_affect_view():
    payload = _payload()
    view = _view(payload)
    payload["nodes"][0]["name"] = "MUTATED"
    payload["nodes"].append({"id": "X"})
    payload["_meta"] = 999
    assert view.nodes_by_id["A"]["name"] == "alpha"
    assert "X" not in view.nodes_by_id
    assert view.nodes_export["_meta"] == 1


def test_no_alias_between_views():
    view1 = _view()
    tx = _tx(1, [SemanticEffect.node_fields("A", ["name"], {"name": "v2"})])
    view2 = apply_journal_transaction(view1, tx, expected_sequence=1)
    assert view2.nodes_by_id["A"] is not view1.nodes_by_id["A"]
    assert view1.nodes_by_id["A"]["name"] == "alpha"


def test_cow_transform_leaves_captured_old_view_unchanged():
    publisher = CacheViewPublisher(_view())
    captured = publisher.capture()

    def transform(view: CacheView) -> CacheView:
        tx = _tx(1, [SemanticEffect.node_fields("A", ["name"], {"name": "v2"})])
        return apply_journal_transaction(view, tx, expected_sequence=1)

    published = publisher.build_and_publish(transform, expected_current=captured)
    assert published.nodes_by_id["A"]["name"] == "v2"
    assert captured.nodes_by_id["A"]["name"] == "alpha"
    assert publisher.capture() is published


def test_transform_returning_input_rejected():
    publisher = CacheViewPublisher(_view())
    with pytest.raises(CacheViewError):
        publisher.build_and_publish(lambda view: view)


def test_publisher_readers_see_whole_generation_a_or_b():
    publisher = CacheViewPublisher(_view())
    errors: list[str] = []
    stop = threading.Event()
    view_a = _view()
    view_b = CacheView.build(
        epoch_number=1,
        epoch_id=EPOCH,
        adopted_sequence=1,
        nodes_export={
            "nodes": [
                {"id": "A", "name": "v2", "priority": 100},
                {"id": "B", "name": "beta", "priority": 200, "note": "b2"},
            ],
            "_meta": 1,
        },
    )

    def reader() -> None:
        while not stop.is_set():
            view = publisher.capture()
            a_name = view.nodes_by_id["A"]["name"]
            b_note = view.nodes_by_id["B"].get("note")
            # generation A: alpha/b ; generation B: v2/b2 — never mixed
            if (a_name, b_note) not in (("alpha", "b"), ("v2", "b2")):
                errors.append(f"mixed generation: {a_name}/{b_note}")

    def writer() -> None:
        for _ in range(50):
            publisher.publish(view_b)
            publisher.publish(view_a)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    writer()
    stop.set()
    thread.join(timeout=5)
    assert not errors


def test_stale_expected_current_publication_fails():
    publisher = CacheViewPublisher(_view())
    captured = publisher.capture()
    publisher.publish(_view(_payload(), seq=2))
    with pytest.raises(CacheViewError):
        publisher.publish(_view(_payload(), seq=3), expected_current=captured)
    with pytest.raises(CacheViewError):
        publisher.build_and_publish(lambda v: _view(_payload(), seq=3), expected_current=captured)


def test_duplicate_and_missing_ids_fail_before_publication():
    with pytest.raises(CacheViewError):
        _view({"nodes": [{"id": "A"}, {"id": "A"}]})
    with pytest.raises(CacheViewError):
        _view({"nodes": [{"name": "no id"}]})
    with pytest.raises(CacheViewError):
        _view({"no_nodes": []})
    with pytest.raises(CacheViewError):
        CacheView.build(epoch_number=1, epoch_id=EPOCH, adopted_sequence=0, nodes_export={"nodes": [{"id": " "}]})


def test_journal_apply_advances_sequence_only_after_full_success():
    view = _view()
    good = _tx(1, [SemanticEffect.node_fields("A", ["name"], {"name": "v2"})])
    view1 = apply_journal_transaction(view, good, expected_sequence=1)
    assert view1.adopted_sequence == 1
    bad = _tx(2, [SemanticEffect.node_fields("GHOST", ["name"], {"name": "x"})])
    with pytest.raises(EffectApplicationError):
        apply_journal_transaction(view1, bad, expected_sequence=2, mode=ApplicationMode.PUBLICATION_STRICT)
    assert view1.adopted_sequence == 1  # unchanged


# ---------------------------------------------------------------------------
# Audit-gate additions (A2, A5, A6)
# ---------------------------------------------------------------------------


def test_cache_view_gap_and_backward_replay_rejected():
    view = _view()  # adopted 0
    tx2 = _tx(2, [SemanticEffect.node_fields("A", ["name"], {"name": "two"})])
    import pytest as _pytest

    with _pytest.raises(JournalIntegrityError):
        apply_journal_transaction(view, tx2, expected_sequence=2)
    view1 = apply_journal_transaction(view, _tx(1, [SemanticEffect.node_fields("A", ["name"], {"name": "one"})]), expected_sequence=1)
    assert view1.adopted_sequence == 1
    with _pytest.raises(JournalIntegrityError):  # gap past adopted
        apply_journal_transaction(view1, _tx(3, [SemanticEffect.node_fields("A", ["name"], {"name": "three"})]), expected_sequence=3)


def test_cache_view_backward_replay_rejected_and_duplicate_noop():
    view = _view()
    tx1 = _tx(1, [SemanticEffect.node_fields("A", ["name"], {"name": "one"})])
    tx2 = _tx(2, [SemanticEffect.node_fields("A", ["name"], {"name": "two"})])
    view2 = apply_journal_transaction(
        apply_journal_transaction(view, tx1, expected_sequence=1),
        tx2,
        expected_sequence=2,
    )
    assert view2.adopted_sequence == 2 and view2.nodes_by_id["A"]["name"] == "two"
    import pytest as _pytest

    with _pytest.raises(JournalIntegrityError):  # backward replay
        apply_journal_transaction(view2, tx1, expected_sequence=1)
    # duplicate at the already-adopted sequence: harmless no-op (same view)
    assert apply_journal_transaction(view2, tx2, expected_sequence=2) is view2


def test_nested_top_level_metadata_owned():
    payload = _payload()
    payload["_nested"] = {"deep": [1, 2, 3]}
    view = _view(payload)
    payload["_nested"]["deep"].append(999)
    payload["_nested"]["new"] = True
    assert view.nodes_export["_nested"] == {"deep": [1, 2, 3]}


def test_slow_transform_does_not_block_capture():
    publisher = CacheViewPublisher(_view())
    entered = threading.Event()
    finish = threading.Event()
    done: list = []

    def slow_transform(view: CacheView) -> CacheView:
        entered.set()
        finish.wait(timeout=5)
        return _view(_payload(), seq=1)

    thread = threading.Thread(
        target=lambda: done.append(publisher.build_and_publish(slow_transform)),
        daemon=True,
    )
    thread.start()
    assert entered.wait(timeout=5)
    # capture proceeds while the transform is still running (no lock held)
    captured = publisher.capture()
    assert captured.adopted_sequence == 0
    finish.set()
    thread.join(timeout=5)
    assert done and done[0].adopted_sequence == 1
    assert publisher.capture().adopted_sequence == 1


def test_cache_view_account_binding_at_replay():
    view = CacheView.build(
        epoch_number=1, epoch_id=EPOCH, adopted_sequence=0,
        nodes_export=_payload(), account_scope_id="a" * 32,
    )
    foreign = _tx(1, [SemanticEffect.tombstone(["A"])], account="b" * 32)
    import pytest as _pytest

    with _pytest.raises(CoherenceAccountMismatchError):
        apply_journal_transaction(view, foreign, expected_sequence=1)
    # and the mapping path honors expected_account
    with _pytest.raises(CoherenceAccountMismatchError):
        apply_journal_transaction(
            _payload(), foreign, expected_sequence=1, expected_account="a" * 32
        )


def test_fresh_view_duplicate_noop_post_epoch_shape():
    """F-4 (Axiom-2 keystone): a view freshly REBUILT at adopted_sequence == N
    from a post-epoch-cut-shaped payload (no prior in-memory lineage) treats
    the committed transaction with sequence == N as a harmless no-op under
    the immutable-journal identity rule."""
    fresh_payload = {
        "nodes": [
            {"id": "A", "name": "one", "priority": 100},
            {"id": "B", "name": "beta", "priority": 200},
        ]
    }
    view = CacheView.build(
        epoch_number=2, epoch_id="f" * 32, adopted_sequence=1,
        nodes_export=fresh_payload, account_scope_id="a" * 32,
    )
    committed = _tx(1, [SemanticEffect.node_fields("A", ["name"], {"name": "one"})])
    assert apply_journal_transaction(view, committed, expected_sequence=1) is view
