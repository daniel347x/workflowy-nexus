"""U6 (ap:E03BN3, contract [ap:UXYCID]) — chronological renders never display
ancestor notes, plus the ``ancestor_notes`` teaching flag (default False).

Dan's binding ruling: *if it is a chronological sort of any form — ``recent``,
the curated tail, an explicit ``sort_by`` created **or** modified, ascending
**or** descending — then automatically do not display ancestor notes. Just
don't.*

Mechanism locks (the spec's §2 governing invariants):

  1. ONE GATE — the suppression is consulted only through
     ``RenderPolicy.emit_body_false_for`` at the existing body-emit gates; no
     second suppression path and no note-blanking tree mutation.
  2. OCCURRENCE/index-path keying, NEVER uuid keying — a sorted render
     re-emits the same ancestor uuid at multiple index paths; a uuid-keyed
     implementation is a real bug (test 4 pins it).
  3. Ancestor = NON-LEAF of the FINAL render forest — the root's own note
     included when the root is a non-leaf (the ~25 KB "ghastly" case).
  4. Membership, ordering and leaf identity untouched; document-order output
     byte-identical (tests 1/9).
  5. No created/modified or asc/desc distinction (tests 1/2).
  6. CF-13 = Option A (Dan's ruling 2026-09-22, relayed Manager-Session-033;
     supersedes U6 §2 invariant 6): an item-count tail (``'last N'``) alone
     IS chronological for note suppression — ancestor notes withheld — while
     its excerpt stays in DOCUMENT order (tests 3/10). A pure document-order
     render (no sort, no since) keeps ancestor notes (tests 1/9).

Patterns after ``tests/test_deprecated_body_suppression.py`` and
``tests/test_render_policy_phase05_step3.py``: forests and policies are built
directly; NO live account, no WebSocket, no I/O.

Producer-side signal chain under test (unit level): each producer computes
``render_is_chronological(resolved_sort_by, since_spec)`` and folds any
``RenderProjectionSpec.ancestor_notes`` opt-out into the ONE boolean the
materializer receives; the materializer — the single computation site — turns
``chronological=True`` into the occurrence-keyed
``chronological_ancestor_emit_body_false`` cause set over the FINAL
render_forest (strict prefixes of ``collect_leaf_branches``), consumed by BOTH
renderers through the one ``emit_body_false_for`` render_key branch.
"""

import copy
import os
import sys

import pytest  # noqa: F401  (kept for pattern parity / future parametrize)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus as W,
)
from workflowy_mcp.cache_render_spec import (  # noqa: E402
    BODY_SUPPRESSION_CAUSES,
    RenderPolicy,
    RenderProjectionSpec,
)
from workflowy_mcp.glimpse_since_policy import (  # noqa: E402
    SinceSpec,
    reorder_tree_by_timestamp,
    since_from_edge,
)


def render_is_chronological(sort_by, since_spec):
    """Test-local import of the U6 predicate.

    Deliberately NOT a module-level import: against the PRE-UNIT tree the
    symbol does not exist, and a top-level import would fail COLLECTION of
    the whole file (one coarse ImportError). Importing inside this shim makes
    every dependent test fail INDIVIDUALLY and BY NAME on the pre-fix tree —
    the §11.2 demonstration ("would this test fail against pre-fix code?")
    recorded per test instead of per file.
    """
    from workflowy_mcp.glimpse_since_policy import (
        render_is_chronological as _impl,
    )
    return _impl(sort_by, since_spec)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _node(nid, name, note="", children=None):
    """A nested render-forest node (the shape the materializer consumes)."""
    return {"id": nid, "name": name, "note": note, "children": children or []}


def _cache(**created_by_id):
    """/nodes-export-style cache map: {uuid: {id, createdAt, modifiedAt}}."""
    out = {}
    for nid, ts in created_by_id.items():
        created, modified = ts if isinstance(ts, tuple) else (ts, ts)
        out[nid] = {"id": nid, "createdAt": created, "modifiedAt": modified}
    return out


def _rich_forest():
    """Root with a BIG note + an intermediate ancestor + two noted leaves.

    R (BIG-ROOT-NOTE)
    ├── M (MID-NOTE)
    │   └── L1 (LEAF-ONE-BODY)
    └── L2 (LEAF-TWO-BODY)
    """
    return [
        _node(
            "R",
            "Rich root",
            note="BIG-ROOT-NOTE\n\nThe whole 25 KB framing document lives here.",
            children=[
                _node(
                    "M",
                    "Middle ancestor",
                    note="MID-NOTE",
                    children=[_node("L1", "Leaf one", note="LEAF-ONE-BODY")],
                ),
                _node("L2", "Leaf two", note="LEAF-TWO-BODY"),
            ],
        )
    ]


_RICH_CACHE = _cache(R=(10, 10), M=(20, 50), L1=(100, 300), L2=(200, 200))


def _sorted_forest(sort_by="created", direction="asc"):
    """The FINAL forest a chronological producer hands the materializer:
    the pure shared sort over the rich forest (inputs never mutated)."""
    out = reorder_tree_by_timestamp(
        _rich_forest(), sort_by=sort_by, direction=direction,
        cache_by_id=dict(_RICH_CACHE),
    )
    assert out is not None
    return out


def _materialize(forest, **kwargs):
    return W.materialize_from_document_forest(forest, **kwargs)


def _compose(mrd, **kwargs):
    return W._build_markdown_preview_from_tree(
        mrd.render_forest,
        suppress_metadata=True,
        render_policy=mrd.policy_by_occurrence,
        **kwargs,
    )


def _serialize(mrd, headings_only=False):
    return W._annotate_preview_ids_and_build_tree(
        mrd.render_forest,
        "WG",
        headings_only=headings_only,
        render_policy=mrd.policy_by_occurrence,
    )


def _heading_lines(md):
    return [ln for ln in md.splitlines() if ln.startswith("#")]


# ===========================================================================
# TEST 1 (§9.1) — sorted render (created, asc): root + intermediate ancestors
# heading-only; leaf notes print; heading sequence IDENTICAL to the
# document-order render of the same forest.  (Fails pre-fix: notes print.)
# ===========================================================================

def test_sorted_created_asc_ancestors_heading_only_leaves_print():
    forest = _sorted_forest("created", "asc")
    mrd = _materialize(copy.deepcopy(forest), focus=None, chronological=True)
    md = _compose(mrd)

    # Ancestor HEADINGS still render (you see where things live) ...
    assert "Rich root" in md
    assert "Middle ancestor" in md
    # ... ancestor NOTE BODIES are withheld — the root's own note included
    # (the 25 KB ghastly case; suppressing it is the point).
    assert "BIG-ROOT-NOTE" not in md
    assert "MID-NOTE" not in md
    # LEAF notes print.
    assert "LEAF-ONE-BODY" in md
    assert "LEAF-TWO-BODY" in md

    # The cause set is the strict prefixes of the final forest's leaf
    # branches, occurrence-keyed: R at (0,) and M at (0,0). Leaves absent.
    assert mrd.policy_by_occurrence.chronological_ancestor_emit_body_false == frozenset(
        {(0,), (0, 0)}
    )


def test_sorted_render_heading_sequence_identical_to_document_order():
    """Member set + ordering untouched: ONLY bodies differ (§2 invariant 4)."""
    forest = _sorted_forest("created", "asc")
    md_chrono = _compose(_materialize(copy.deepcopy(forest), focus=None, chronological=True))
    md_doc = _compose(_materialize(copy.deepcopy(forest), focus=None, chronological=False))
    assert _heading_lines(md_chrono) == _heading_lines(md_doc)

    # full/UUID mode, headings_only harvest: byte-identical either way
    # (headings_only is inert — already title-only, §7 fault matrix).
    ser_chrono = _serialize(
        _materialize(copy.deepcopy(forest), focus=None, chronological=True),
        headings_only=True,
    )
    ser_doc = _serialize(
        _materialize(copy.deepcopy(forest), focus=None, chronological=False),
        headings_only=True,
    )
    assert ser_chrono == ser_doc


# ===========================================================================
# TEST 2 (§9.2) — modified, desc: identical suppression (no base/direction
# distinction — Dan's ruling, §2 invariant 5).
# ===========================================================================

def test_sorted_modified_desc_identical_suppression():
    forest = _sorted_forest("modified", "desc")
    mrd = _materialize(copy.deepcopy(forest), focus=None, chronological=True)
    md = _compose(mrd)
    assert "BIG-ROOT-NOTE" not in md
    assert "MID-NOTE" not in md
    assert "LEAF-ONE-BODY" in md
    assert "LEAF-TWO-BODY" in md
    assert "Rich root" in md and "Middle ancestor" in md


# ===========================================================================
# TEST 3 (§9.3 as flipped by CF-13 = Option A, Dan's ruling 2026-09-22) —
# since forms: window ⇒ suppress · curated ⇒ suppress · 'last 2' alone ⇒
# SUPPRESS (ancestor notes withheld; the excerpt's order untouched) ·
# 'last 2' with a sort ⇒ suppress.
# ===========================================================================

def test_since_window_signal_suppresses():
    spec = since_from_edge("1w")
    assert spec is not None and spec.form == "window"
    assert render_is_chronological(None, spec) is True
    forest = _sorted_forest()  # producers hand the post-since forest in
    md = _compose(_materialize(copy.deepcopy(forest), focus=None, chronological=True))
    assert "BIG-ROOT-NOTE" not in md and "LEAF-ONE-BODY" in md


def test_since_curated_signal_suppresses():
    spec = since_from_edge("curated")
    assert spec is not None and spec.form == "curated"
    assert render_is_chronological(None, spec) is True


def test_since_item_count_alone_suppresses_ancestor_notes():
    spec = since_from_edge("last 2")
    assert spec is not None and spec.form == "item_count"
    # CF-13 = Option A (Dan's ruling 2026-09-22, relayed Manager-Session-033;
    # supersedes U6 §2 invariant 6): a bare item-count tail IS chronological
    # for ancestor-note suppression. The excerpt's document order is
    # PRESERVED — the ruling flips what PRINTS, never the ordering.
    assert render_is_chronological(None, spec) is True
    forest = _sorted_forest()
    md = _compose(_materialize(copy.deepcopy(forest), focus=None, chronological=True))
    # Ancestor notes are withheld; leaf notes print.
    assert "BIG-ROOT-NOTE" not in md and "MID-NOTE" not in md
    assert "LEAF-ONE-BODY" in md and "LEAF-TWO-BODY" in md


def test_since_item_count_with_sort_suppresses():
    spec = since_from_edge("last 2")
    assert render_is_chronological("created", spec) is True


# ===========================================================================
# TEST 4 (§9.4) — OCCURRENCE keying, never uuid keying: one uuid at two
# index paths, ancestor at one occurrence and LEAF at the other → suppressed
# exactly at the ancestor occurrence; the leaf occurrence's note PRINTS.
# A uuid-keyed implementation fails this.
# ===========================================================================

def _dup_uuid_forest():
    """[ P(P-NOTE) ─ A(A-ANC-BODY) ─ X(X-NOTE) ,  A(A-LEAF-BODY) ]

    uuid 'A' occurs at index_path (0,0) as a NON-LEAF and at (1,) as a LEAF.
    """
    return [
        _node(
            "P", "Parent", note="P-NOTE",
            children=[
                _node(
                    "A", "Dup ancestor", note="A-ANC-BODY",
                    children=[_node("X", "Deep leaf", note="X-NOTE")],
                ),
            ],
        ),
        _node("A", "Dup leaf", note="A-LEAF-BODY"),
    ]


def test_occurrence_keying_ancestor_suppressed_leaf_occurrence_prints():
    forest = _dup_uuid_forest()
    mrd = _materialize(copy.deepcopy(forest), focus=None, chronological=True)

    # The cause set is keyed by INDEX PATH, not uuid: (0,) = P, (0,0) = the
    # ancestor occurrence of A. The leaf occurrence (1,) is ABSENT — a
    # uuid-keyed set would have suppressed it too.
    assert mrd.policy_by_occurrence.chronological_ancestor_emit_body_false == frozenset(
        {(0,), (0, 0)}
    )

    md = _compose(mrd)
    assert "P-NOTE" not in md
    assert "A-ANC-BODY" not in md       # ancestor occurrence: withheld
    assert "A-LEAF-BODY" in md          # leaf occurrence: PRINTS
    assert "X-NOTE" in md               # deeper leaf prints
    assert "Dup ancestor" in md and "Dup leaf" in md  # both headings render

    # Full/UUID serializer honors the SAME occurrence discipline.
    joined = "\n".join(_serialize(mrd))
    assert "A-ANC-BODY" not in joined
    assert "A-LEAF-BODY" in joined
    assert "P-NOTE" not in joined
    assert "X-NOTE" in joined


# ===========================================================================
# TEST 5 (§9.5) — root as SOLE LEAF of the final forest → root note PRINTS.
# ===========================================================================

def test_root_sole_leaf_note_prints():
    forest = [_node("SOLO", "Solo root", note="SOLO-ROOT-NOTE")]
    mrd = _materialize(copy.deepcopy(forest), focus=None, chronological=True)
    assert mrd.policy_by_occurrence.chronological_ancestor_emit_body_false == frozenset()
    md = _compose(mrd)
    assert "SOLO-ROOT-NOTE" in md


# ===========================================================================
# TEST 6 (§9.6) — ancestor_notes: true → old behavior byte-restored (the
# producer folds the flag into the boolean; the materializer never sees the
# spec — §6's settled computation site).
# ===========================================================================

def test_ancestor_notes_true_restores_old_behavior_byte_for_byte():
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="R", sort_by="created", ancestor_notes=True,
    )
    assert spec.ancestor_notes is True
    # THE producer fold, exactly as the call sites implement it:
    chronological = render_is_chronological(spec.sort_by_internal(), spec.since) \
        and not bool(spec.ancestor_notes)
    assert chronological is False

    forest = _sorted_forest()
    md_flag = _compose(
        _materialize(copy.deepcopy(forest), focus=None, chronological=chronological)
    )
    md_baseline = _compose(_materialize(copy.deepcopy(forest), focus=None))
    assert md_flag == md_baseline
    assert "BIG-ROOT-NOTE" in md_flag and "MID-NOTE" in md_flag


# ===========================================================================
# TEST 7 (§9.7) — full/UUID mode: inline [note] withheld at marked
# occurrences, present at leaves.
# ===========================================================================

def test_full_uuid_mode_withholds_inline_note_at_ancestors():
    forest = _sorted_forest()
    mrd = _materialize(copy.deepcopy(forest), focus=None, chronological=True)
    lines = _serialize(mrd)
    joined = "\n".join(lines)
    assert "BIG-ROOT-NOTE" not in joined
    assert "MID-NOTE" not in joined
    assert "LEAF-ONE-BODY" in joined
    assert "LEAF-TWO-BODY" in joined
    # Headings (names) all still render.
    assert "Rich root" in joined and "Middle ancestor" in joined

    # Control: without the signal the inline notes are all present.
    control = "\n".join(
        _serialize(_materialize(copy.deepcopy(forest), focus=None, chronological=False))
    )
    assert "BIG-ROOT-NOTE" in control and "MID-NOTE" in control


# ===========================================================================
# TEST 8 (§9.8) — focused render: breadcrumb ancestor AND chronological
# ancestor both heading-only; the focused LEAF's note prints.
# ===========================================================================

def test_focused_render_breadcrumb_and_chronological_compose():
    forest = _sorted_forest()
    focus = {
        "mode": "row",
        "uuid": "L1",
        "ancestor_path": ["R", "M"],
        "occurrence": 0,
    }
    mrd = _materialize(copy.deepcopy(forest), focus=focus, chronological=True)
    policy = mrd.policy_by_occurrence

    # The breadcrumb cause (focus spine) and the chronological cause BOTH
    # mark the ancestors; they compose as a union at the one consult.
    assert policy.breadcrumb_emit_body_false  # non-empty spine
    assert (0,) in policy.chronological_ancestor_emit_body_false  # root
    assert (0, 0) in policy.chronological_ancestor_emit_body_false  # mid

    md = _compose(mrd)
    assert "Rich root" in md and "Middle ancestor" in md  # headings render
    assert "BIG-ROOT-NOTE" not in md
    assert "MID-NOTE" not in md
    # The focused leaf IS a leaf of the final render forest → its note prints.
    assert "LEAF-ONE-BODY" in md
    # The pruned sibling never renders (option-Y shape, unchanged).
    assert "LEAF-TWO-BODY" not in md


# ===========================================================================
# TEST 9 (§9.9) — GOLDEN: document-order output byte-identical (rule inert).
# ===========================================================================

def test_document_order_output_byte_identical_to_direct_composer():
    """chronological defaults False; the materialized document-order render
    equals the legacy DIRECT composer call byte-for-byte (the composer's
    documented unfocused FALLBACK/PARITY guarantee), and the new cause set is
    empty — the rule is inert on every non-chronological render (§10)."""
    forest = _rich_forest()
    mrd_default = _materialize(copy.deepcopy(forest), focus=None)
    mrd_explicit = _materialize(copy.deepcopy(forest), focus=None, chronological=False)
    assert mrd_default.policy_by_occurrence.chronological_ancestor_emit_body_false == frozenset()
    assert mrd_explicit.policy_by_occurrence.chronological_ancestor_emit_body_false == frozenset()
    md_default = _compose(mrd_default)
    md_explicit = _compose(mrd_explicit)
    assert md_default == md_explicit

    # The pre-unit full-document path composed the forest DIRECTLY (no
    # materializer, no policy). Byte-identity against it locks the golden.
    md_direct = W._build_markdown_preview_from_tree(
        copy.deepcopy(forest), suppress_metadata=True,
    )
    assert md_default == md_direct
    assert "BIG-ROOT-NOTE" in md_default and "MID-NOTE" in md_default

    # Serializer golden too (full/UUID document order).
    ser_direct = W._annotate_preview_ids_and_build_tree(
        copy.deepcopy(forest), "WG",
    )
    assert _serialize(mrd_default) == ser_direct


# ===========================================================================
# TEST 10 (§9.10) — render_is_chronological truth table.
# ===========================================================================

def test_render_is_chronological_truth_table():
    window = since_from_edge("1w")
    curated = since_from_edge("curated")
    item_count = since_from_edge("last 2")
    window_desc_modified = SinceSpec(
        form="window", window_seconds=604800, axis="modified", direction="desc",
    )

    # Presentation sort — either basis. Direction is not an input (asc/desc
    # both arrive as the same resolved sort_by at every call site).
    assert render_is_chronological("created", None) is True
    assert render_is_chronological("modified", None) is True
    # Resolved document order — BOTH spellings: internal None (unresolved /
    # natural order) and the wire sentinel 'none' (explicit document order).
    # Call sites resolve slice-group default-sort inheritance BEFORE
    # consulting (_resolve_slice_group_effective_sort / sort_by_internal), so
    # an inherited chronological default arrives here as 'created'/'modified'.
    assert render_is_chronological(None, None) is False
    assert render_is_chronological("none", None) is False
    # Anything unrecognized is not a sort.
    assert render_is_chronological("bogus", None) is False
    assert render_is_chronological("", None) is False

    # Date-form since — chronological even unsorted.
    assert render_is_chronological(None, window) is True
    assert render_is_chronological("none", window) is True
    assert render_is_chronological(None, curated) is True
    # since axis/direction are inert for the SIGNAL (window on the modified
    # axis, descending, is still a date-form window).
    assert render_is_chronological(None, window_desc_modified) is True

    # Item-count tail alone — CHRONOLOGICAL (CF-13 = Option A, Dan's ruling
    # 2026-09-22; supersedes U6 §9 test-10's item-count rows). The excerpt's
    # document order is untouched — this classifies note presentation only.
    assert render_is_chronological(None, item_count) is True
    assert render_is_chronological("none", item_count) is True
    # ... and combined with a sort, still chronological.
    assert render_is_chronological("created", item_count) is True
    assert render_is_chronological("modified", item_count) is True

    # A raw token string must never reach here (interior code carries
    # SinceSpec|None only — test_since_object_only pins the object rule); if
    # one leaks, it is inert rather than accidentally chronological.
    assert render_is_chronological(None, "1w") is False  # type: ignore[arg-type]


# ===========================================================================
# Policy object + spec-field locks (the flag is a SPEC FIELD, never a since
# token; the echo stays byte-identical; the codec round-trips).
# ===========================================================================

def test_cause_vocabulary_names_the_third_cause():
    # [ap:I506WY] unit 5g added the FOURTH cause (``lens_scaffold_ancestor``).
    assert BODY_SUPPRESSION_CAUSES == (
        "breadcrumb", "deprecated", "chronological_ancestor",
        "lens_scaffold_ancestor",
    )


def test_emit_body_false_for_render_key_branch_tests_both_occurrence_causes():
    policy = RenderPolicy(
        chronological_ancestor_emit_body_false=frozenset({(0,), (1, 2)}),
    )
    # The occurrence branch fires for the chronological cause — with
    # include_deprecated either way (the flag governs ONLY the uuid-keyed
    # deprecated cause; both renderers' existing consults inherit the new
    # cause with NO gate changes).
    assert policy.emit_body_false_for(render_key=(0,), uuid=None, include_deprecated=False) is True
    assert policy.emit_body_false_for(render_key=(1, 2), uuid="X", include_deprecated=True) is True
    assert policy.emit_body_false_for(render_key=[1, 2], uuid=None, include_deprecated=False) is True
    # Unmarked occurrences are untouched.
    assert policy.emit_body_false_for(render_key=(1,), uuid=None, include_deprecated=False) is False
    # The uuid consult NEVER reads the occurrence set (no uuid keying leak).
    assert policy.emit_body_false_for(render_key=None, uuid="R", include_deprecated=True) is False
    # Empty policy (every non-chronological render) → nothing fires.
    empty = RenderPolicy()
    assert empty.chronological_ancestor_emit_body_false == frozenset()
    assert empty.emit_body_false_for(render_key=(0,), uuid="R", include_deprecated=True) is False


def test_ancestor_notes_spec_field_default_codec_and_echo():
    # Default false everywhere.
    assert RenderProjectionSpec().ancestor_notes is False
    assert RenderProjectionSpec.normalize_from_glimpse_args().ancestor_notes is False
    # Explicit true, coerced bool.
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="R", ancestor_notes=True,
    )
    assert spec.ancestor_notes is True

    # Wire blob round-trip (frozen/scoped re-projections keep the flag).
    blob = spec.to_render_spec_blob()
    assert blob["ancestor_notes"] is True
    assert RenderProjectionSpec.from_render_spec_blob(blob).ancestor_notes is True
    # OLD blobs without the key deserialize unchanged → default False
    # (frozen modals re-project safely, §10).
    legacy_blob = {k: v for k, v in blob.items() if k != "ancestor_notes"}
    assert RenderProjectionSpec.from_render_spec_blob(legacy_blob).ancestor_notes is False

    # The modal-frame normalizer reads the blob's flag.
    frame_spec = RenderProjectionSpec.normalize_from_modal_frame(
        {"render_spec": dict(blob)}
    )
    assert frame_spec.ancestor_notes is True

    # Agent view overrides CARRY the flag (never silently re-decide it).
    assert spec.with_view_overrides(headings_only=True).ancestor_notes is True

    # The render_scope echo is UNCHANGED (§10): the flag is a spec/blob field,
    # never an echo field, never a `since` token.
    echo = spec.to_render_scope_fields()
    assert "ancestor_notes" not in echo
    assert "ancestor_notes" not in str(echo.get("since"))
    # The since object itself never grows the flag (it is a SPEC field;
    # test_since_object_only keeps pinning that `since` stays an object).
    assert not hasattr(SinceSpec(form="window"), "ancestor_notes")


def test_materializer_stays_pure_and_handles_empty_forest():
    """The new walk adds no mutation and no kill point (§7): shadow dicts
    only, and an empty forest (an empty-window producer short-circuits before
    materializing, but the core never crashes) yields an empty cause set."""
    forest = _rich_forest()
    before = copy.deepcopy(forest)
    _materialize(forest, focus=None, chronological=True)
    assert forest == before  # input forest never mutated

    empty = _materialize([], focus=None, chronological=True)
    assert empty.render_forest == []
    assert empty.policy_by_occurrence.chronological_ancestor_emit_body_false == frozenset()


def test_headings_only_is_inert_under_the_rule():
    """headings_only renders are already title-only; the rule changes
    nothing there (§7 fault matrix)."""
    forest = _sorted_forest()
    md_ho_chrono = _compose(
        _materialize(copy.deepcopy(forest), focus=None, chronological=True),
        headings_only=True,
    )
    md_ho_plain = _compose(
        _materialize(copy.deepcopy(forest), focus=None, chronological=False),
        headings_only=True,
    )
    assert md_ho_chrono == md_ho_plain
    assert "BIG-ROOT-NOTE" not in md_ho_chrono
