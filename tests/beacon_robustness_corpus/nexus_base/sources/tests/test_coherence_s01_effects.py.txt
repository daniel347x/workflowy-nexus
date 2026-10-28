"""S01 §13.3 — semantic effect schema and pure reducer tests."""

from __future__ import annotations

import copy
import os

import pytest

from workflowy_mcp.coherence import (
    ApplicationMode,
    SemanticEffect,
    apply_journal_transaction,
    apply_semantic_effects,
    effect_from_dict,
    validate_transaction_effects,
)
from workflowy_mcp.coherence.effects import (
    synthetic_priority_ranks,
)
from workflowy_mcp.coherence.errors import (
    CoherenceSchemaError,
    EffectApplicationError,
    EffectValidationError,
    JournalIntegrityError,
    UnsupportedCoherenceSchemaError,
)
from workflowy_mcp.coherence.schema import JournalProducer, JournalTransaction

STRICT = ApplicationMode.PUBLICATION_STRICT
REPLAY = ApplicationMode.REPLAY_AUTHORITATIVE

NOW = "2026-11-01T00:00:00+00:00"


def _cache() -> dict:
    return {
        "nodes": [
            {"id": "P", "name": "parent", "priority": 1},
            {"id": "A", "name": "alpha", "note": "a-note", "parent_id": "P", "priority": 100},
            {"id": "B", "name": "beta", "parent_id": "P", "priority": 200},
        ]
    }


def _by_id(payload: dict) -> dict:
    return {n["id"]: n for n in payload["nodes"]}


def _tx(sequence: int, effects, *, account: str = "a" * 32, epoch: str = "e" * 32) -> JournalTransaction:
    return JournalTransaction(
        schema_version=1,
        account_scope_id=account,
        sequence=sequence,
        previous_sequence=sequence - 1,
        transaction_id=f"tx{sequence:030d}",
        published_epoch_id=epoch,
        operation_kind="test",
        committed_at=NOW,
        producer=JournalProducer(
            pid=os.getpid(), pid_create_time=1.0, owner_kind="process", ownership_kind="none"
        ),
        verification=(),
        effects=tuple(effects),
    )


# ---------------------------------------------------------------------------
# Envelope strictness — one per effect type + forbidden mask cases
# ---------------------------------------------------------------------------


def test_effect_roundtrips_all_types():
    effects = [
        SemanticEffect.node_create({"id": "n", "name": "x", "future": [1]}),
        SemanticEffect.node_fields("n", ["name"], {"name": "y"}),
        SemanticEffect.node_parentage("n", "p"),
        SemanticEffect.node_parentage("n", None),
        SemanticEffect.child_order("p", ["a", "b"], priority_by_id={"a": 5, "b": 5}),
        SemanticEffect.tombstone(["a", "b"]),
    ]
    for effect in effects:
        decoded = effect_from_dict(effect.to_dict())
        assert decoded == effect


def test_envelope_unknown_type_version_keys_fail():
    doc = SemanticEffect.tombstone(["a"]).to_dict()
    doc["effect_type"] = "nope"
    with pytest.raises(CoherenceSchemaError):
        effect_from_dict(doc)
    doc = SemanticEffect.tombstone(["a"]).to_dict()
    doc["effect_version"] = 2
    with pytest.raises(UnsupportedCoherenceSchemaError):
        effect_from_dict(doc)
    doc = SemanticEffect.tombstone(["a"]).to_dict()
    del doc["effect_version"]
    with pytest.raises(CoherenceSchemaError):
        effect_from_dict(doc)
    doc = SemanticEffect.tombstone(["a"]).to_dict()
    doc["payload"]["extra"] = 1
    with pytest.raises(EffectValidationError):
        effect_from_dict(doc)


def test_forbidden_field_mask_cases():
    for bad in ("id", "parent_id", "priority"):
        with pytest.raises(EffectValidationError):
            SemanticEffect.node_fields("n", [bad], {bad: 1})
    with pytest.raises(EffectValidationError):  # unapproved nonstructural field
        SemanticEffect.node_fields("n", ["secret_field"], {"secret_field": 1})
    with pytest.raises(EffectValidationError):  # values/mask mismatch
        SemanticEffect.node_fields("n", ["name"], {"name": "x", "note": "y"})
    with pytest.raises(EffectValidationError):  # empty mask
        SemanticEffect.node_fields("n", [], {})
    with pytest.raises(EffectValidationError):  # duplicate mask entries
        SemanticEffect.node_fields("n", ["name", "name"], {"name": "x"})


def test_child_order_payload_strictness():
    with pytest.raises(EffectValidationError):  # duplicate ids
        SemanticEffect.child_order("p", ["a", "a"])
    with pytest.raises(EffectValidationError):  # priority for unlisted id
        SemanticEffect.child_order("p", ["a"], priority_by_id={"b": 1})
    with pytest.raises(EffectValidationError):  # canonical record id mismatch
        SemanticEffect.child_order(
            "p", ["a"], canonical_records={"a": {"id": "b"}}
        )
    with pytest.raises(EffectValidationError):  # canonical record not listed
        SemanticEffect.child_order(
            "p", ["a"], canonical_records={"z": {"id": "z"}}
        )
    with pytest.raises(EffectValidationError):  # blank parent
        SemanticEffect.child_order(" ", ["a"])


def test_child_order_canonical_records_require_same_transaction_authority():
    cache = _cache()
    records = {"C": {"id": "C", "name": "gamma"}}
    order = SemanticEffect.child_order("P", ["A", "B", "C"], canonical_records=records)
    # Without a same-transaction create/fields effect covering C -> reject.
    with pytest.raises(EffectValidationError):
        validate_transaction_effects([order], _by_id(cache), mode=STRICT)
    create = SemanticEffect.node_create({"id": "C", "name": "gamma"})
    validate_transaction_effects([create, order], _by_id(cache), mode=STRICT)
    # Content-verified (node_fields) authorization: the canonical record is
    # cargo, NOT supply — a pre-existing id must still exist in the cache.
    fields = SemanticEffect.node_fields("C", ["name"], {"name": "gamma"})
    with pytest.raises(EffectValidationError):  # C absent from cache AND not created
        validate_transaction_effects([fields, order], _by_id(cache), mode=STRICT)
    cache_with_c = _cache()
    cache_with_c["nodes"].append({"id": "C", "name": "gamma-old", "parent_id": "P", "priority": 300})
    validate_transaction_effects([fields, order], _by_id(cache_with_c), mode=STRICT)


def test_canonical_record_cannot_overwrite_unmasked_content():
    """A3: authorization via a node_fields effect covers ONLY the masked
    fields — a canonical record carrying unverified content fields must be
    rejected before anything is applied."""
    cache = _cache()
    records = {
        "A": {
            "id": "A",
            "name": "record-name",
            "note": "UNVERIFIED-OVERWRITE",
            "completedAt": 123,
        }
    }
    order = SemanticEffect.child_order("P", ["A", "B"], canonical_records=records)
    fields = SemanticEffect.node_fields("A", ["name"], {"name": "record-name"})
    with pytest.raises(EffectValidationError):
        apply_semantic_effects(cache, [fields, order], mode=STRICT)
    with pytest.raises(EffectValidationError):
        apply_semantic_effects(cache, [fields, order], mode=REPLAY)
    # A record whose content exactly matches the mask is accepted and is
    # order-independent (cargo only; node_fields owns the content write).
    good_records = {"A": {"id": "A", "name": "record-name"}}
    good_order = SemanticEffect.child_order("P", ["A", "B"], canonical_records=good_records)
    for effects in ([fields, good_order], [good_order, fields]):
        out = apply_semantic_effects(cache, effects, mode=STRICT)
        nodes = _by_id(out)
        assert nodes["A"]["name"] == "record-name"
        assert nodes["A"]["note"] == "a-note"  # pre-existing content preserved
        assert "completedAt" not in nodes["A"]


def test_canonical_record_disagreeing_with_content_effect_rejected():
    cache = _cache()
    records = {"A": {"id": "A", "name": "DIFFERENT"}}
    order = SemanticEffect.child_order("P", ["A", "B"], canonical_records=records)
    fields = SemanticEffect.node_fields("A", ["name"], {"name": "record-name"})
    with pytest.raises(EffectValidationError):
        apply_semantic_effects(cache, [fields, order], mode=STRICT)


# ---------------------------------------------------------------------------
# Application — field authority
# ---------------------------------------------------------------------------


def test_node_create_full_authority_and_strict_existing_rejected():
    cache = _cache()
    effect = SemanticEffect.node_create(
        {"id": "C", "name": "gamma", "note": "n", "parent_id": "P", "priority": 300, "future": 1}
    )
    out = apply_semantic_effects(cache, [effect], mode=STRICT)
    node = _by_id(out)["C"]
    assert node["note"] == "n" and node["future"] == 1
    with pytest.raises(EffectApplicationError):
        apply_semantic_effects(cache, [SemanticEffect.node_create({"id": "A"})], mode=STRICT)
    # replay upserts (idempotent)
    out2 = apply_semantic_effects(out, [effect], mode=REPLAY)
    assert _by_id(out2)["C"]["name"] == "gamma"


def test_node_fields_content_only_never_structure():
    cache = _cache()
    effect = SemanticEffect.node_fields(
        "A", ["name", "note", "completedAt"], {"name": "new", "note": None, "completedAt": 42}
    )
    out = apply_semantic_effects(cache, [effect], mode=STRICT)
    node = _by_id(out)["A"]
    assert node["name"] == "new" and node["note"] is None and node["completedAt"] == 42
    assert node["parent_id"] == "P" and node["priority"] == 100  # structure untouched


def test_node_parentage_only_parent():
    cache = _cache()
    effect = SemanticEffect.node_parentage("A", "Q")
    out = apply_semantic_effects(cache, [effect], mode=STRICT)
    node = _by_id(out)["A"]
    assert node["parent_id"] == "Q" and node["priority"] == 100 and node["name"] == "alpha"
    # explicit null/root supported
    out = apply_semantic_effects(cache, [SemanticEffect.node_parentage("A", None)], mode=STRICT)
    assert _by_id(out)["A"]["parent_id"] is None


def test_child_order_never_overwrites_sibling_content():
    cache = _cache()
    order = SemanticEffect.child_order("P", ["B", "A"])
    out = apply_semantic_effects(cache, [order], mode=STRICT)
    nodes = _by_id(out)
    assert nodes["A"]["name"] == "alpha" and nodes["A"]["note"] == "a-note"
    assert nodes["B"]["name"] == "beta"
    # order flipped via synthetic ranks (server priorities 100<200 would NOT reproduce B,A)
    kids = sorted((n for n in out["nodes"] if n.get("parent_id") == "P"), key=lambda n: n["priority"])
    assert [k["id"] for k in kids] == ["B", "A"]


def test_child_order_preserves_valid_server_priorities():
    cache = _cache()
    order = SemanticEffect.child_order("P", ["B", "A"], priority_by_id={"A": 200, "B": 100})
    out = apply_semantic_effects(cache, [order], mode=STRICT)
    nodes = _by_id(out)
    assert nodes["B"]["priority"] == 100 and nodes["A"]["priority"] == 200


@pytest.mark.parametrize(
    "priority_by_id",
    [
        None,  # missing
        {"A": 100},  # partial
        {"A": 100, "B": True},  # bool
        {"A": 100, "B": "x"},  # non-integer
        {"A": 200, "B": 100},  # order-inconsistent (cannot reproduce A,B)
        {"A": 100, "B": 100},  # duplicate (cannot impose strict order)
    ],
)
def test_child_order_synthetic_rank_fallback(priority_by_id):
    cache = _cache()
    order = SemanticEffect.child_order("P", ["A", "B"], priority_by_id=priority_by_id)
    out = apply_semantic_effects(cache, [order], mode=STRICT)
    nodes = _by_id(out)
    assert nodes["A"]["priority"] == 100 and nodes["B"]["priority"] == 200  # synthetic
    kids = sorted((n for n in out["nodes"] if n.get("parent_id") == "P"), key=lambda n: n["priority"])
    assert [k["id"] for k in kids] == ["A", "B"]


def test_duplicate_priorities_trigger_synthetic_with_contrary_source_order():
    """The contractually required A4 case: cache STORAGE order [B, A],
    declared order [A, B], duplicate priorities {A: 100, B: 100}.  Valid
    server priorities must impose an unambiguous strict ordering —
    duplicates trigger all-sibling synthetic ranks, and priority-sort then
    reproduces the declared [A, B] rather than the storage-tie-broken [B, A]."""
    cache = {
        "nodes": [
            {"id": "P", "name": "parent", "priority": 1},
            {"id": "B", "name": "beta", "parent_id": "P", "priority": 200},
            {"id": "A", "name": "alpha", "parent_id": "P", "priority": 100},
        ]
    }
    order = SemanticEffect.child_order("P", ["A", "B"], priority_by_id={"A": 100, "B": 100})
    out = apply_semantic_effects(cache, [order], mode=STRICT)
    nodes = _by_id(out)
    assert nodes["A"]["priority"] == 100 and nodes["B"]["priority"] == 200  # synthetic
    kids = sorted((n for n in out["nodes"] if n.get("parent_id") == "P"), key=lambda n: n["priority"])
    assert [k["id"] for k in kids] == ["A", "B"]  # declared order, not storage order


def test_synthetic_rank_helper_frozen_contract():
    assert synthetic_priority_ranks(3) == [100, 200, 300]


def test_unexplained_membership_disappearance_fails_strict():
    cache = _cache()
    cache["nodes"].append({"id": "G", "name": "gui-added", "parent_id": "P", "priority": 300})
    order = SemanticEffect.child_order("P", ["A", "B"])  # omits G
    with pytest.raises(EffectValidationError):
        apply_semantic_effects(cache, [order], mode=STRICT)


def test_explained_membership_change_succeeds():
    cache = _cache()
    cache["nodes"].append({"id": "G", "name": "moving", "parent_id": "P", "priority": 300})
    order = SemanticEffect.child_order("P", ["A", "B"])
    parentage = SemanticEffect.node_parentage("G", "Q")
    out = apply_semantic_effects(cache, [parentage, order], mode=STRICT)
    assert _by_id(out)["G"]["parent_id"] == "Q"
    tombstone = SemanticEffect.tombstone(["G"])
    out = apply_semantic_effects(cache, [tombstone, order], mode=STRICT)
    assert "G" not in _by_id(out)


def test_tombstone_explicit_closure_only():
    cache = _cache()
    cache["nodes"].append({"id": "G", "parent_id": "A", "priority": 1})  # grandchild
    out = apply_semantic_effects(cache, [SemanticEffect.tombstone(["A"])], mode=STRICT)
    nodes = _by_id(out)
    assert "A" not in nodes and "G" in nodes  # replay never guesses subtree closure


def test_all_or_none_on_late_failure():
    cache = _cache()
    snapshot = copy.deepcopy(cache)
    effects = [
        SemanticEffect.node_fields("A", ["name"], {"name": "changed"}),
        SemanticEffect.node_fields("MISSING", ["name"], {"name": "x"}),
    ]
    with pytest.raises(EffectApplicationError):
        apply_semantic_effects(cache, effects, mode=STRICT)
    assert cache == snapshot  # input never mutated


def test_inputs_never_mutated():
    cache = _cache()
    snapshot = copy.deepcopy(cache)
    order = SemanticEffect.child_order("P", ["B", "A"])
    apply_semantic_effects(cache, [order], mode=STRICT)
    assert cache == snapshot


# ---------------------------------------------------------------------------
# Application modes (F1)
# ---------------------------------------------------------------------------


def test_replay_tolerance_listed_sibling_absent():
    cache = _cache()
    order = SemanticEffect.child_order(
        "P", ["A", "B", "GHOST"], priority_by_id={"A": 1, "B": 2, "GHOST": 3}
    )
    diagnostics: list[dict] = []
    out = apply_semantic_effects(cache, [order], mode=REPLAY, diagnostics=diagnostics)
    assert any(d["kind"] == "listed_sibling_absent" for d in diagnostics)
    kids = sorted((n for n in out["nodes"] if n.get("parent_id") == "P"), key=lambda n: n["priority"])
    assert [k["id"] for k in kids] == ["A", "B"]


def test_replay_unlisted_gui_child_removed_from_membership():
    cache = _cache()
    cache["nodes"].append({"id": "G", "name": "gui", "parent_id": "P", "priority": 300})
    order = SemanticEffect.child_order("P", ["A", "B"])
    diagnostics: list[dict] = []
    out = apply_semantic_effects(cache, [order], mode=REPLAY, diagnostics=diagnostics)
    assert _by_id(out)["G"]["parent_id"] is None  # documented brief regression
    assert any(d["kind"] == "membership_removed" for d in diagnostics)


def test_replay_content_effect_on_absent_node_is_diagnostic_noop():
    cache = _cache()
    diagnostics: list[dict] = []
    out = apply_semantic_effects(
        cache,
        [
            SemanticEffect.node_fields("GHOST", ["name"], {"name": "x"}),
            SemanticEffect.node_parentage("GHOST", "P"),
            SemanticEffect.tombstone(["GHOST"]),
        ],
        mode=REPLAY,
        diagnostics=diagnostics,
    )
    assert out["nodes"] == cache["nodes"]
    assert len(diagnostics) == 3


def test_publication_strict_rejects_same_three_scenarios():
    cache = _cache()
    with pytest.raises(EffectValidationError):  # listed sibling absent
        apply_semantic_effects(
            cache, [SemanticEffect.child_order("P", ["A", "B", "GHOST"])], mode=STRICT
        )
    cache2 = _cache()
    cache2["nodes"].append({"id": "G", "parent_id": "P", "priority": 300})
    with pytest.raises(EffectValidationError):  # unexplained membership removal
        apply_semantic_effects(cache2, [SemanticEffect.child_order("P", ["A", "B"])], mode=STRICT)
    with pytest.raises(EffectApplicationError):  # content on absent node
        apply_semantic_effects(
            cache, [SemanticEffect.node_fields("GHOST", ["name"], {"name": "x"})], mode=STRICT
        )


def test_mode_tolerance_never_excuses_invalid_records():
    cache = _cache()
    with pytest.raises(EffectValidationError):  # field-authority violation
        apply_semantic_effects(
            cache,
            [SemanticEffect.node_fields("A", ["name"], {"name": "x", "note": "y"})],
            mode=REPLAY,
        )
    bad_tx = _tx(5, [SemanticEffect.tombstone(["A"])])
    with pytest.raises(JournalIntegrityError):  # sequence reversal, both modes
        apply_journal_transaction(cache, bad_tx, expected_sequence=6, mode=STRICT)


# ---------------------------------------------------------------------------
# Journal transaction application — sequence discipline and idempotence
# ---------------------------------------------------------------------------


def test_exact_duplicate_replay_idempotent():
    cache = _cache()
    tx = _tx(1, [SemanticEffect.node_fields("A", ["name"], {"name": "v1"})])
    once = apply_journal_transaction(cache, tx, expected_sequence=1, mode=REPLAY)
    twice = apply_journal_transaction(once, tx, expected_sequence=1, mode=REPLAY)
    assert _by_id(once) == _by_id(twice)


def test_older_distinct_after_newer_rejected():
    cache = _cache()
    older = _tx(1, [SemanticEffect.node_fields("A", ["name"], {"name": "old"})])
    newer = _tx(2, [SemanticEffect.node_fields("A", ["name"], {"name": "new"})])
    state = apply_journal_transaction(cache, newer, expected_sequence=2, mode=REPLAY)
    with pytest.raises(JournalIntegrityError):
        apply_journal_transaction(state, older, expected_sequence=3, mode=REPLAY)
    with pytest.raises(JournalIntegrityError):  # gap
        apply_journal_transaction(cache, newer, expected_sequence=1, mode=REPLAY)


def test_apply_journal_transaction_on_cache_view_advances_after_success():
    from workflowy_mcp.coherence import CacheView

    cache = _cache()
    view = CacheView.build(epoch_number=1, epoch_id="e" * 32, adopted_sequence=0, nodes_export=cache)
    good = _tx(1, [SemanticEffect.node_fields("A", ["name"], {"name": "v1"})])
    view1 = apply_journal_transaction(view, good, expected_sequence=1)
    assert view1.adopted_sequence == 1
    assert view1.nodes_by_id["A"]["name"] == "v1"
    assert view.adopted_sequence == 0 and view.nodes_by_id["A"]["name"] == "alpha"
    # failing transaction leaves adopted sequence unmoved
    bad = _tx(2, [SemanticEffect.node_fields("GHOST", ["name"], {"name": "x"})])
    with pytest.raises(EffectApplicationError):
        apply_journal_transaction(view1, bad, expected_sequence=2, mode=STRICT)
    assert view1.adopted_sequence == 1


# ---------------------------------------------------------------------------
# The sealed Axiom-7 commutativity lock
# ---------------------------------------------------------------------------


def test_update_vs_sibling_create_commutativity_matrix():
    cache = _cache()
    update = _tx(
        1,
        [
            SemanticEffect.node_fields(
                "A", ["name", "note", "completedAt"], {"name": "A2", "note": "n2", "completedAt": 9}
            )
        ],
    )
    create = _tx(
        2,
        [
            SemanticEffect.node_create({"id": "C", "name": "gamma", "parent_id": "P", "priority": 300}),
            SemanticEffect.child_order(
                "P",
                ["A", "B", "C"],
                priority_by_id={"A": 100, "B": 200, "C": 300},
                canonical_records={"C": {"id": "C", "name": "gamma", "parent_id": "P", "priority": 300}},
            ),
        ],
    )
    for first, second in ((update, create), (create, update)):
        diagnostics: list[dict] = []
        intermediate = apply_journal_transaction(
            cache, first, expected_sequence=first.sequence, mode=REPLAY, diagnostics=diagnostics
        )
        final = apply_journal_transaction(
            intermediate, second, expected_sequence=second.sequence, mode=REPLAY, diagnostics=diagnostics
        )
        nodes = _by_id(final)
        # A's content comes from U; membership/order comes from C.
        assert nodes["A"]["name"] == "A2" and nodes["A"]["note"] == "n2"
        assert nodes["A"]["completedAt"] == 9
        kids = sorted(
            (n for n in final["nodes"] if n.get("parent_id") == "P"),
            key=lambda n: n["priority"],
        )
        assert [k["id"] for k in kids] == ["A", "B", "C"]
    # both orders converge to identical semantic state
    state_uc = apply_journal_transaction(
        apply_journal_transaction(cache, update, expected_sequence=1, mode=REPLAY),
        create,
        expected_sequence=2,
        mode=REPLAY,
    )
    state_cu = apply_journal_transaction(
        apply_journal_transaction(cache, create, expected_sequence=2, mode=REPLAY),
        update,
        expected_sequence=1,
        mode=REPLAY,
    )
    assert _by_id(state_uc) == _by_id(state_cu)


# ---------------------------------------------------------------------------
# Audit residual R1 — created-node canonical-record agreement + ordering
# ---------------------------------------------------------------------------


def test_created_record_must_agree_with_node_create():
    cache = _cache()
    create = SemanticEffect.node_create({"id": "C", "name": "create-name", "parent_id": "P"})
    conflict_order = SemanticEffect.child_order(
        "P", ["A", "B", "C"],
        canonical_records={"C": {"id": "C", "name": "DIFFERENT", "parent_id": "P"}},
    )
    for effects in ([create, conflict_order], [conflict_order, create]):
        with pytest.raises(EffectValidationError):
            apply_semantic_effects(cache, effects, mode=STRICT)
        with pytest.raises(EffectValidationError):
            apply_semantic_effects(cache, effects, mode=REPLAY)


def test_created_record_agreement_converges_both_modes():
    cache = _cache()
    create = SemanticEffect.node_create({"id": "C", "name": "create-name", "parent_id": "P"})
    agree_order = SemanticEffect.child_order(
        "P", ["A", "B", "C"],
        priority_by_id={"A": 100, "B": 200, "C": 300},
        canonical_records={"C": {"id": "C", "name": "create-name", "parent_id": "P", "priority": 300}},
    )
    for mode in (STRICT, REPLAY):
        out = apply_semantic_effects(cache, [create, agree_order], mode=mode)
        nodes = _by_id(out)
        assert nodes["C"]["name"] == "create-name" and nodes["C"]["priority"] == 300


def test_order_before_create_is_noncanonical_and_rejected():
    cache = _cache()
    create = SemanticEffect.node_create({"id": "C", "name": "create-name", "parent_id": "P"})
    agree_order = SemanticEffect.child_order(
        "P", ["A", "B", "C"],
        canonical_records={"C": {"id": "C", "name": "create-name", "parent_id": "P"}},
    )
    for mode in (STRICT, REPLAY):
        with pytest.raises(EffectValidationError):
            apply_semantic_effects(cache, [agree_order, create], mode=mode)


def test_contradictory_transaction_combinations_rejected():
    cache = _cache()
    create = SemanticEffect.node_create({"id": "C", "name": "x"})
    with pytest.raises(EffectValidationError):  # create + tombstone same id
        validate_transaction_effects([create, SemanticEffect.tombstone(["C"])], _by_id(cache), mode=STRICT)
    with pytest.raises(EffectValidationError):  # node_fields on tombstoned
        validate_transaction_effects(
            [SemanticEffect.tombstone(["A"]), SemanticEffect.node_fields("A", ["name"], {"name": "x"})],
            _by_id(cache), mode=STRICT,
        )
    with pytest.raises(EffectValidationError):  # node_parentage on tombstoned
        validate_transaction_effects(
            [SemanticEffect.tombstone(["A"]), SemanticEffect.node_parentage("A", "Q")],
            _by_id(cache), mode=STRICT,
        )


# ---------------------------------------------------------------------------
# Audit residual — structural inter-effect conflict matrix
# ---------------------------------------------------------------------------


def test_conflicting_node_parentage_values_rejected():
    cache = _cache()
    effects = [
        SemanticEffect.node_parentage("A", "Q"),
        SemanticEffect.node_parentage("A", "P"),
    ]
    for mode in (STRICT, REPLAY):
        with pytest.raises(EffectValidationError):
            validate_transaction_effects(effects, _by_id(cache), mode=mode)
    # identical duplicate is harmless
    validate_transaction_effects(
        [SemanticEffect.node_parentage("A", "Q"), SemanticEffect.node_parentage("A", "Q")],
        _by_id(cache), mode=STRICT,
    )


def test_conflicting_child_order_declarations_rejected():
    cache = _cache()
    effects = [
        SemanticEffect.child_order("P", ["A", "B"]),
        SemanticEffect.child_order("P", ["B", "A"]),
    ]
    for mode in (STRICT, REPLAY):
        with pytest.raises(EffectValidationError):
            validate_transaction_effects(effects, _by_id(cache), mode=mode)
    # identical duplicate is harmless
    validate_transaction_effects(
        [SemanticEffect.child_order("P", ["A", "B"]), SemanticEffect.child_order("P", ["A", "B"])],
        _by_id(cache), mode=STRICT,
    )


def test_tombstoned_id_listed_in_child_order_rejected_both_orders():
    cache = _cache()
    order = SemanticEffect.child_order("P", ["A", "B"])
    tombstone = SemanticEffect.tombstone(["A"])
    for effects in ([tombstone, order], [order, tombstone]):
        for mode in (STRICT, REPLAY):
            with pytest.raises(EffectValidationError):
                validate_transaction_effects(effects, _by_id(cache), mode=mode)


def test_parentage_and_child_order_must_agree():
    cache = _cache()
    # node_parentage A->Q but child_order lists A under P
    with pytest.raises(EffectValidationError):
        validate_transaction_effects(
            [SemanticEffect.node_parentage("A", "Q"), SemanticEffect.child_order("P", ["A", "B"])],
            _by_id(cache), mode=STRICT,
        )
    # node_parentage A->P but the child_order for P omits A
    with pytest.raises(EffectValidationError):
        validate_transaction_effects(
            [SemanticEffect.node_parentage("A", "P"), SemanticEffect.child_order("P", ["B"])],
            _by_id(cache), mode=REPLAY,
        )
    # agreement is accepted: parentage A->Q with A listed under Q
    cache2 = _cache()
    cache2["nodes"].append({"id": "Q", "name": "q-parent", "priority": 2})
    validate_transaction_effects(
        [SemanticEffect.node_parentage("A", "Q"), SemanticEffect.child_order("Q", ["A"])],
        _by_id(cache2), mode=STRICT,
    )


def test_accepted_transaction_never_fails_from_static_contradiction():
    """A transaction accepted by strict validation against an authoritative
    cache must not later fail application from static inter-effect
    contradictions (membership changes here are all explained)."""
    cache = _cache()
    effects = [
        SemanticEffect.node_fields("A", ["name"], {"name": "renamed"}),
        SemanticEffect.child_order("P", ["B", "A"]),
    ]
    validate_transaction_effects(effects, _by_id(cache), mode=STRICT)
    out = apply_semantic_effects(cache, effects, mode=STRICT)  # must not raise
    assert _by_id(out)["A"]["name"] == "renamed"
