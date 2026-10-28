"""PHASE 0.5 Step 3 — RenderPolicy value object (introduced UNWIRED).

This is the ledger's Step 3 (ap:EXECLDG): introduce the ``RenderPolicy`` carrier
that Step 4 (rebase relocation) and Steps 6/7 (composer + full/UUID serializer
body-suppression) will consume, and lock the ONE thing that is easy to get
wrong — the KEYING DISCIPLINE:

  * BREADCRUMB body-suppression is OCCURRENCE / index-path keyed (a RenderKey =
    tuple of child indices), NOT a uuid-set (v3.3 #1 trap: under a timestamp
    sort the same uuid re-emits and only SOME occurrences are focus breadcrumbs,
    so a uuid-set would suppress the body at EVERY occurrence — symptom #5 in
    full mode).
  * DEPRECATED body-suppression is UUID keyed (ap:DEPBOD — a per-node property,
    correctly suppressed at every occurrence).
  * The two causes are DISTINGUISHABLE, and ``emit_body_false_for`` lets the
    full/UUID serializer consult ONLY the breadcrumb cause
    (``include_deprecated=False``) so DEPBOD stays Markdown-only in Phase 0.5
    (v3.1 #2 / v3.2(a)).
  * The per-file/heading REBASE baseline is keyed by the same RenderKey
    (index_path), covering BOTH file-node and bare-MD-heading roots (v3.3 #2).

UNWIRED: nothing populates or reads RenderPolicy in production yet; these are
pure construction + accessor semantics tests, matching the no-I/O harness the
other cache_render_spec tests use.
"""
from __future__ import annotations

from workflowy_mcp.cache_render_spec import (
    BODY_SUPPRESSION_CAUSES,
    RenderPolicy,
)


# ---------------------------------------------------------------------------
# Construction / defaults
# ---------------------------------------------------------------------------

def test_default_policy_is_empty_and_frozen():
    p = RenderPolicy()
    assert p.rebase_by_render_key == {}
    assert p.breadcrumb_emit_body_false == frozenset()
    assert p.deprecated_emit_body_false == frozenset()
    # U6 (ap:E03BN3): the third cause's set defaults empty — every
    # non-chronological render keeps byte-identical behavior.
    assert p.chronological_ancestor_emit_body_false == frozenset()
    # Frozen dataclass: field assignment must raise.
    import dataclasses
    try:
        p.rebase_by_render_key = {(0,): 1}  # type: ignore[misc]
        raised = False
    except dataclasses.FrozenInstanceError:
        raised = True
    assert raised, "RenderPolicy must be immutable (frozen)"


def test_default_suppression_sets_do_not_alias():
    """Two independently-constructed default policies must not share a set."""
    a = RenderPolicy()
    b = RenderPolicy()
    assert a.breadcrumb_emit_body_false is not b.breadcrumb_emit_body_false or (
        a.breadcrumb_emit_body_false == frozenset()
    )
    # (frozenset() is a safe immutable default; the real guard is that mutating
    # one policy is impossible at all — covered by the frozen test above.)


def test_cause_vocabulary_is_the_expected_causes():
    # U6 (ap:E03BN3) added the THIRD cause: ``chronological_ancestor`` —
    # occurrence/index-path keyed like ``breadcrumb`` (never uuid keyed) and
    # consumed by BOTH renderers through the same render_key branch; the
    # uuid-keyed ``deprecated`` cause stays Markdown-only.
    # [ap:I506WY] unit 5g added the FOURTH cause: ``lens_scaffold_ancestor`` —
    # occurrence-keyed too (the scaffolding above a lens's input roots,
    # heading-only even in document order), same render_key branch.
    assert BODY_SUPPRESSION_CAUSES == (
        "breadcrumb",
        "deprecated",
        "chronological_ancestor",
        "lens_scaffold_ancestor",
    )


# ---------------------------------------------------------------------------
# REBASE baseline — keyed by RenderKey (index_path tuple)
# ---------------------------------------------------------------------------

def test_rebase_base_for_returns_stored_value_and_none_otherwise():
    p = RenderPolicy(rebase_by_render_key={(0,): 1, (0, 2): 3})
    assert p.rebase_base_for((0,)) == 1
    assert p.rebase_base_for((0, 2)) == 3
    # An unknown key -> None (composer falls back to its own default).
    assert p.rebase_base_for((1,)) is None


def test_rebase_base_for_coerces_list_key_to_tuple():
    """A caller passing an index_path as a list still matches the tuple key."""
    p = RenderPolicy(rebase_by_render_key={(0, 1): 2})
    assert p.rebase_base_for([0, 1]) == 2  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# BODY SUPPRESSION — the load-bearing KEYING DISCIPLINE (v3.3 #1)
# ---------------------------------------------------------------------------

def test_breadcrumb_is_occurrence_keyed_not_uuid_keyed():
    """The breadcrumb cause matches an INDEX-PATH occurrence, never a uuid.

    This is the v3.3 #1 trap made executable: a policy that suppresses the
    breadcrumb occurrence (0, 0) must NOT suppress a DIFFERENT occurrence
    (0, 1) of the same node, even though (in a real sort) both share a uuid.
    """
    p = RenderPolicy(breadcrumb_emit_body_false=frozenset({(0, 0)}))
    # The exact breadcrumb occurrence is suppressed ...
    assert p.emit_body_false_for(render_key=(0, 0)) is True
    # ... a different occurrence of the "same" node is NOT.
    assert p.emit_body_false_for(render_key=(0, 1)) is False
    # A uuid never triggers the breadcrumb cause (it is index-path keyed).
    assert p.emit_body_false_for(uuid="whatever") is False


def test_deprecated_is_uuid_keyed():
    p = RenderPolicy(deprecated_emit_body_false=frozenset({"dep-uuid"}))
    assert p.emit_body_false_for(uuid="dep-uuid") is True
    assert p.emit_body_false_for(uuid="other-uuid") is False
    # A render_key never triggers the deprecated cause.
    assert p.emit_body_false_for(render_key=(0, 0)) is False


def test_full_uuid_serializer_ignores_deprecated_cause():
    """DEPBOD stays Markdown-only in Phase 0.5 (v3.1 #2 / v3.2(a)).

    The full/UUID serializer consults emit_body_false_for with
    include_deprecated=False, so a surviving --deprecated node is NOT
    body-suppressed in full mode — only the Markdown composer honors it.
    """
    p = RenderPolicy(deprecated_emit_body_false=frozenset({"dep-uuid"}))
    # Markdown composer (default include_deprecated=True) suppresses it ...
    assert p.emit_body_false_for(uuid="dep-uuid") is True
    # ... full/UUID serializer (include_deprecated=False) does NOT.
    assert p.emit_body_false_for(uuid="dep-uuid", include_deprecated=False) is False


def test_both_causes_compose_and_are_distinguishable():
    """A policy can carry both causes at once; each is matched by its own key.

    Proves the two causes are DISTINGUISHABLE (kept in separate sets), which is
    exactly what lets the full/UUID serializer filter to the breadcrumb cause
    while the Markdown composer honors both.
    """
    p = RenderPolicy(
        breadcrumb_emit_body_false=frozenset({(0, 0), (0, 1, 0)}),
        deprecated_emit_body_false=frozenset({"dep-1"}),
    )
    # breadcrumb occurrences
    assert p.emit_body_false_for(render_key=(0, 0)) is True
    assert p.emit_body_false_for(render_key=(0, 1, 0)) is True
    assert p.emit_body_false_for(render_key=(0, 2)) is False
    # deprecated uuid
    assert p.emit_body_false_for(uuid="dep-1") is True
    assert p.emit_body_false_for(uuid="dep-2") is False
    # A node that is BOTH a breadcrumb occurrence AND deprecated: suppressed
    # under the Markdown composer via EITHER cause ...
    assert p.emit_body_false_for(render_key=(0, 0), uuid="dep-1") is True
    # ... and under the full/UUID serializer STILL suppressed, but ONLY because
    # of the breadcrumb cause (deprecated excluded).
    assert (
        p.emit_body_false_for(render_key=(0, 0), uuid="dep-1", include_deprecated=False)
        is True
    )
    # A node that is ONLY deprecated (not a breadcrumb): suppressed for Markdown,
    # NOT for full/UUID.
    assert p.emit_body_false_for(render_key=(0, 9), uuid="dep-1") is True
    assert (
        p.emit_body_false_for(render_key=(0, 9), uuid="dep-1", include_deprecated=False)
        is False
    )


def test_emit_body_false_with_no_args_is_false():
    """Neither key supplied -> no suppression (the ordinary body-emitting node)."""
    p = RenderPolicy(
        breadcrumb_emit_body_false=frozenset({(0, 0)}),
        deprecated_emit_body_false=frozenset({"dep-1"}),
    )
    assert p.emit_body_false_for() is False


def test_breadcrumb_render_key_list_coerces_to_tuple():
    """A render_key passed as a list matches the tuple-keyed set."""
    p = RenderPolicy(breadcrumb_emit_body_false=frozenset({(0, 0)}))
    assert p.emit_body_false_for(render_key=[0, 0]) is True  # type: ignore[arg-type]
