"""PHASE 0.5 GOLDEN BASELINE — freeze CURRENT focus-render behavior byte-for-byte.

WHY THIS FILE EXISTS
--------------------
This is Step 1 of the v3.4 Execution Ledger (ap:EXECLDG) for the BATON v3
Phase-0.5 refactor (ap:OBXJ6Z + addenda ap:DEPBODFULL / ap:FOCUSBRIDGE /
ap:V33GRD). Phase 0.5 consolidates FOUR duplicated focus-render resolvers onto
ONE server-internal materializer. The refactor is claimed to be
"behavior-preserving"; these golden literals are how that claim is PROVEN
rather than assumed.

The rule (Dan, ledger §11 + Step-1 spec): capture the CURRENT byte-for-byte
output across the whole §8 matrix, for ALL FOUR focus resolvers, exercised as
PUBLIC calls. This file MUST pass on the pre-refactor tree (it merely enshrines
the status quo). Later commits that DELIBERATELY change behavior — there are
exactly two signed-off changes in Phase 0.5 — must change a lock HERE, visibly,
with Dan's sign-off, NEVER silently.

THE FOUR RESOLVERS LOCKED (ledger Step 1 / §5-Correction#1):
  1. In-composer Markdown pruning: ``_build_markdown_preview_from_tree``
     (focus_path / focus_leaf / focus_index_path).
  2. Full/UUID pre-pruner: ``_prune_forest_to_focus_index_path`` feeding
     ``_annotate_preview_ids_and_build_tree`` (the real full-mode pipeline,
     with ``_build_abs_path_map_for_preview`` in front).
  3. Slice-row path: ``_render_slice_row_view`` (occurrence-exact triple).
  4. Live-DOM modal: ``compose_markdown_modal_view`` — BOTH its legacy
     ``parent_of`` unsorted path (the single highest-traffic interaction) AND
     its occurrence-exact sorted path.

⚠️⚠️⚠️ TWO DELIBERATE, PRE-FIX "KNOWN-BAD" LOCKS IN THIS FILE ⚠️⚠️⚠️
These assertions pin behavior that Phase 0.5 will DELIBERATELY FIX. They PASS
now (they faithfully record today's — flawed — behavior). When the fix lands
they will BREAK on purpose; the implementer then updates the literal to the
corrected behavior WITH DAN'S SIGN-OFF. They are marked inline with a big
``# ⚠️ PRE-FIX (hazard-a) — Step 7 WILL FLIP THIS`` banner so the change is
never mistaken for an accidental regression:
  * HAZARD-A (v3.1 / ap:DEPBODFULL §①): in FULL/UUID FOCUSED mode the
    breadcrumb file node's abs-path leader is currently LOST, because
    ``_prune_forest_to_focus_index_path`` blanks the note BEFORE
    ``_build_abs_path_map_for_preview`` reads it. Step 7 fixes this (abs-path
    map computed over the note-preserving render_forest) and the lock flips to
    "leader PRESENT".
Everything else in this file is expected to remain byte-for-byte stable across
the entire refactor and is a pure regression guard.

Same pure, no-I/O harness the other modal tests use: import
``WorkFlowyClientNexus`` (aliased ``W``), build small node dicts, and for the
live-DOM modal path instantiate via ``__new__`` with a stubbed
``_get_nodes_export_cache_nodes``.
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus as W,
)


# ---------------------------------------------------------------------------
# Tiny builders + a stubbed-cache client factory (mirrors the other modal tests)
# ---------------------------------------------------------------------------
def _node(nid, name, note="", children=None):
    return {"id": nid, "name": name, "note": note, "children": children or []}


def _client_with_cache(cache_nodes):
    """A WorkFlowyClientNexus whose /nodes-export cache is the given flat list.

    Instantiated via __new__ (no real client init) with the ONE cache reader
    stubbed — exactly the no-I/O pattern test_phase0_row_identity_baseline.py
    uses for the live-DOM modal paths.
    """
    c = W.__new__(W)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in cache_nodes]
    return c


def _focused_md(forest, uuid, ancestor_path, occurrence, *, suppress_metadata=True):
    """Reproduce a focused MARKDOWN render through the PRODUCTION surface.

    Phase 0.5 Step 12b sub-commit 3b repoint: the composer
    (_build_markdown_preview_from_tree) is now FOCUS-AGNOSTIC -- its
    focus_path/focus_leaf/focus_index_path params + in-walk option-Y pruning were
    deleted. A focused option-Y render is produced by the ONE shared materializer
    (materialize_from_document_forest), which hands the composer the already-
    pruned NOTE-PRESERVING render_forest + a render-forest-keyed RenderPolicy --
    exactly what _render_slice_row_view / compose_markdown_modal_view / the cache
    tail now do. Output is byte-identical to the old direct-composer focus call
    (that IS the parity thesis). ``ancestor_path`` is the root-first chain WITHOUT
    the leaf; ``occurrence`` disambiguates a repeated (uuid, ancestor_path).
    """
    mrd = W.materialize_from_document_forest(
        forest,
        focus={
            "mode": "row",
            "uuid": uuid,
            "ancestor_path": list(ancestor_path),
            "occurrence": occurrence,
        },
    )
    return W._build_markdown_preview_from_tree(
        mrd.render_forest,
        suppress_metadata=suppress_metadata,
        render_policy=mrd.policy_by_occurrence,
    )


# ===========================================================================
# FIXTURE 1 — a plain/structural subtree (no Cartographer files).
#   root
#     a (Alpha)
#       a1 (Alpha-1)
#       a2 (Alpha-2)
#     b (Beta)
# ===========================================================================
def _plain_forest():
    return [
        _node("root", "Root", "root body", [
            _node("a", "Alpha", "alpha body", [
                _node("a1", "Alpha-1", "a1 body"),
                _node("a2", "Alpha-2", "a2 body"),
            ]),
            _node("b", "Beta", "beta body"),
        ]),
    ]


# --- Resolver #1: in-composer Markdown pruning -----------------------------

def test_r1_markdown_unfocused_plain():
    md = W._build_markdown_preview_from_tree(_plain_forest(), suppress_metadata=True)
    assert md == (
        "# Root\n\nroot body\n\n"
        "## Alpha\n\nalpha body\n\n"
        "### Alpha-1\n\na1 body\n\n"
        "### Alpha-2\n\na2 body\n\n"
        "## Beta\n\nbeta body"
    )


def test_r1_markdown_focus_leaf_a1_breadcrumb_headingonly():
    """Focus a leaf: root + Alpha render HEADING-ONLY (bodies suppressed),

    a1 renders WITH its body. Occurrence-exact via focus_index_path.
    """
    forest = _plain_forest()
    # Occurrence-exact index path is still asserted (the materializer resolves
    # the same triple internally); the render now goes through the production
    # materializer surface (see _focused_md).
    ip = W._resolve_focus_index_path(
        forest, uuid="a1", ancestor_path=["root", "a"], occurrence=0
    )
    assert ip == [0, 0, 0]
    md = _focused_md(forest, "a1", ["root", "a"], 0)
    assert md == "# Root\n\n## Alpha\n\n### Alpha-1\n\na1 body"


def test_r1_markdown_focus_root_row_whole_document():
    """Focusing the render ROOT (falsy/self focus) renders the WHOLE document."""
    forest = _plain_forest()
    # A ROOT-ROW focus (top-level node, empty ancestor_path) is the
    # whole-rooted-document case: the materializer does NOT prune, so the render
    # equals the unfocused whole document (see _focused_md / the materializer
    # FOCUS CONTRACT).
    md_root = _focused_md(forest, "root", [], 0)
    md_unfocused = W._build_markdown_preview_from_tree(
        forest, suppress_metadata=True
    )
    assert md_root == md_unfocused


# --- Resolver #2: full/UUID pre-pruner + serializer (the real pipeline) ----

def _full_mode_lines(forest, focus=None, headings_only=False):
    """Reproduce the real full/UUID GLIMPSE pipeline for a (possibly focused)

    forest. Phase 0.5 Step 12a repoint: the focused option-Y render_forest now
    comes from the SHARED materializer core (materialize_from_document_forest),
    the production surface -- NOT the deleted standalone helper
    _prune_forest_to_focus_index_path. ``focus`` is the canonical row-focus
    dict ({mode,uuid,ancestor_path,occurrence}) or None (whole document).
    Then the abs-path map is built over the (note-preserving) render_forest and
    the preview-tree serializer runs -- exactly the production full/UUID path,
    including the render-forest-keyed body-suppression policy.
    """
    mrd = W.materialize_from_document_forest(forest, focus=focus)
    tree = mrd.render_forest
    abs_map = W._build_abs_path_map_for_preview(tree)
    return W._annotate_preview_ids_and_build_tree(
        tree, "P", None, abs_map, headings_only,
        mrd.policy_by_occurrence,
    )


def test_r2_full_unfocused_plain():
    lines = _full_mode_lines(_plain_forest())
    assert lines == [
        "[root] [P-1    ] \u29bf Root [root body]",
        "[a] [P-1.1  ]     \u29bf Alpha [alpha body]",
        "[a1] [P-1.1.1]         \u2022 Alpha-1 [a1 body]",
        "[a2] [P-1.1.2]         \u2022 Alpha-2 [a2 body]",
        "[b] [P-1.2  ]     \u2022 Beta [beta body]",
    ]


def test_r2_full_focus_a1_breadcrumb_notes_absent_today():
    """Focused full/UUID mode: breadcrumb nodes (root, Alpha) carry NO [note]

    suffix today. That suppression is a SIDE EFFECT of
    ``_prune_forest_to_focus_index_path`` blanking the note before the
    serializer runs — which is exactly the v3.1 (ap:DEPBODFULL §①) crack the
    refactor will make policy-driven. Locking the CURRENT (note-absent)
    breadcrumb output; the focused node a1 keeps its [a1 body].

    NOTE: This particular lock (breadcrumb [note] absent) is EXPECTED to stay
    green even after Step 7 — Step 7 keeps breadcrumb bodies suppressed, it
    just changes the MECHANISM (occurrence-keyed policy instead of
    note-blanking). What Step 7 flips is the abs-leader hazard-a lock below,
    not this one.
    """
    forest = _plain_forest()
    focus = {"mode": "row", "uuid": "a1", "ancestor_path": ["root", "a"], "occurrence": 0}
    lines = _full_mode_lines(forest, focus=focus)
    assert lines == [
        "[root] [P-1    ] \u29bf Root",
        "[a] [P-1.1  ]     \u29bf Alpha",
        "[a1] [P-1.1.1]         \u2022 Alpha-1 [a1 body]",
    ]


def test_r2_full_focus_a1_headings_only():
    forest = _plain_forest()
    focus = {"mode": "row", "uuid": "a1", "ancestor_path": ["root", "a"], "occurrence": 0}
    lines = _full_mode_lines(forest, focus=focus, headings_only=True)
    # No cache count operand in this pure fixture; unknown is not zero.
    assert lines == [
        "[root] [P-1    ] \u29bf Root ▸?",
        "[a] [P-1.1  ]     \u29bf Alpha ▸?",
        "[a1] [P-1.1.1]         \u2022 Alpha-1 ▸?",
    ]


def test_r2_materializer_focus_does_not_mutate_input():
    """The full-mode focus path (materializer core) is pure -- input untouched,
    notes NEVER blanked (v3.1 hazard-a: body suppression is render policy, not
    a tree mutation). Repointed in Step 12a from the deleted pre-pruner."""
    forest = _plain_forest()
    focus = {"mode": "row", "uuid": "a1", "ancestor_path": ["root", "a"], "occurrence": 0}
    W.materialize_from_document_forest(forest, focus=focus)
    # Original breadcrumb notes intact (the materializer shadow-copies).
    assert forest[0]["note"] == "root body"
    assert forest[0]["children"][0]["note"] == "alpha body"


# ===========================================================================
# FIXTURE 2 — a Cartographer MARKDOWN FILE above the focus (HAZARD-A).
#   doc.md          (note: "Root: E:\\tmp\\doc.md")   <- FILE node, H1 + leader
#     Intro         (MD_PATH -> "# Intro")
#       Details     (MD_PATH -> "## Details", body)   <- focus target
# ===========================================================================
def _md_file_forest():
    return [
        _node("fileroot", "doc.md", "Root: E:\\tmp\\doc.md", [
            _node("h1", "Intro", "MD_PATH: doc.md#Intro\n\n# Intro", [
                _node(
                    "h2", "Details",
                    "MD_PATH: doc.md#Intro#Details\n\n## Details\n\ndetails body",
                ),
            ]),
        ]),
    ]


def test_hazard_a_markdown_unfocused_leader_present():
    md = W._build_markdown_preview_from_tree(_md_file_forest(), suppress_metadata=True)
    assert md == (
        "# doc.md\n\n**E:\\tmp\\doc.md**\n\nRoot: E:\\tmp\\doc.md\n\n"
        "# Intro\n\n## Details\n\n---"
    )


def test_hazard_a_markdown_focus_h2_leader_survives():
    """MARKDOWN mode focused: the breadcrumb FILE node keeps its H1 marker AND

    its abs-path leader (Markdown reads the note in-walk before pruning drops
    it). This is CORRECT today and must stay stable across the refactor.
    """
    forest = _md_file_forest()
    ip = W._resolve_focus_index_path(
        forest, uuid="h2", ancestor_path=["fileroot", "h1"], occurrence=0
    )
    assert ip == [0, 0, 0]
    # Repointed (3b) to the production materializer surface; the abs-path leader
    # survives because the materializer's render_forest preserves every note
    # (v3.1 hazard-a) -- byte-identical to the old direct-composer focus call.
    md = _focused_md(forest, "h2", ["fileroot", "h1"], 0)
    assert md == "# doc.md\n\n**E:\\tmp\\doc.md**\n\n# Intro\n\n## Details\n\n---"


# ==========================================================================
# RETIRED in Phase 0.5 Step 12a: test_hazard_a_full_focus_h2_leader_LOST_pre_fix
#
# That test pinned the LEGACY HELPER _prune_forest_to_focus_index_path's
# note-blanking behavior (the empty-abs-map / lost-leader v3.1 hazard-a crack),
# explicitly "to be removed when Step 12 deletes the helper." Step 12a deleted
# the helper (it was dead code once Steps 6/7/8 routed full-mode focus through
# the materializer), so this characterization test is removed with it. The
# PRODUCTION hazard-a fix (abs-leader SURVIVES over the note-preserving
# render_forest in full/UUID focused mode) remains locked by
# tests/test_phase05_step7_full_uuid_policy_aware.py::
#   test_L1_full_focus_breadcrumb_abs_leader_PRESENT_and_note_suppressed
# (and the DOM-capture twin in test_phase05_step8_*). Nothing lost.
# ==========================================================================


# ===========================================================================
# FIXTURE 3 — SOURCE-FILE regime (root_in_non_md_file seed).
# A '#'-comment note body must be FENCE-protected (never promoted to a heading)
# when the render root is seeded as living inside a non-Markdown source file.
# ===========================================================================
def _source_regime_forest():
    # A plain node whose NOTE body contains a '#'-comment line that markdown-it
    # would read as a heading if not fenced. With root_in_non_md_file=True the
    # walk hazard-fences it.
    return [
        _node("fn", "some_function", "# this is a python comment, not a heading\ncode line"),
    ]


def test_source_regime_seed_fences_hash_comment():
    """root_in_non_md_file=True: a '#'-comment note body is fence-guarded, not

    promoted to an H1 markdown-content block. Locks the source-file regime seed.
    """
    md = W._build_markdown_preview_from_tree(
        _source_regime_forest(), suppress_metadata=True, root_in_non_md_file=True
    )
    # The node renders as a structural heading; the '#'-comment line is
    # FENCE-protected (```text fence) so it cannot masquerade as a Markdown
    # heading. NOTE the exact current shape: only the heading-hazard line is
    # wrapped in the fence; the following plain 'code line' is emitted AFTER
    # the fence. This is the byte-for-byte current behavior of
    # _fence_structural_markdown_note_hazards_for_preview.
    assert md == (
        "# some_function\n\n"
        "```text\n"
        "# this is a python comment, not a heading\n"
        "```\n"
        "code line"
    )


def test_source_regime_seed_default_false_promotes_hash_comment():
    """Contrast lock: WITHOUT the seed (default False) the SAME '#'-comment body

    is treated as Markdown content and embedded as a heading. Pins the
    DIFFERENCE the seed makes (so a regression that ignores the seed is caught).
    """
    md = W._build_markdown_preview_from_tree(
        _source_regime_forest(), suppress_metadata=True, root_in_non_md_file=False
    )
    # Different from the fenced form above — the body is NOT fenced here.
    assert md != (
        "# some_function\n\n"
        "```\n"
        "# this is a python comment, not a heading\n"
        "code line\n"
        "```"
    )
    # It contains the comment text (however embedded) and no code fence.
    assert "this is a python comment" in md
    assert "```" not in md


# ===========================================================================
# FIXTURE 4 — SLICE-ROW resolver (#3): occurrence-exact triple over a
# projected slice tree. Reuses the plain forest's single root as the slice
# render tree.
# ===========================================================================
def test_r3_slice_row_focus_leaf_a1():
    """_render_slice_row_view on the plain tree, focusing a1 by its triple."""
    tree = _plain_forest()[0]
    md = W._render_slice_row_view(
        tree, uuid="a1", ancestor_path=["root", "a"], occurrence=0
    )
    assert md == "# Root\n\n## Alpha\n\n### Alpha-1\n\na1 body"


def test_r3_slice_row_root_renders_whole_document():
    tree = _plain_forest()[0]
    md = W._render_slice_row_view(tree, uuid="root", ancestor_path=[], occurrence=0)
    assert md == W._build_markdown_preview_from_tree([tree], suppress_metadata=True)


# ===========================================================================
# FIXTURE 5 — the STRADDLED-ANCESTOR timestamp-sort tree (occurrence-exact).
# createdAt A1=100, B1=500, A2=900 -> leaf sort A1, B1, A2 straddles Section A,
# so the trie-merge re-emits A as occurrence 0 and occurrence 1.
# ===========================================================================
_S_ROOT = "22222222-0000-0000-0000-000000000001"
_S_A = "22222222-0000-0000-0000-000000000002"
_S_A1 = "22222222-0000-0000-0000-000000000003"
_S_A2 = "22222222-0000-0000-0000-000000000004"
_S_B = "22222222-0000-0000-0000-000000000005"
_S_B1 = "22222222-0000-0000-0000-000000000006"

_S_CACHE = [
    {"id": _S_ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _S_A, "name": "Section A", "note": "body A", "parent_id": _S_ROOT, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _S_A1, "name": "Point A1", "note": "alpha one", "parent_id": _S_A, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _S_A2, "name": "Point A2", "note": "alpha two", "parent_id": _S_A, "priority": 1, "createdAt": 900, "modifiedAt": 900},
    {"id": _S_B, "name": "Section B", "note": "body B", "parent_id": _S_ROOT, "priority": 1, "createdAt": 100, "modifiedAt": 100},
    {"id": _S_B1, "name": "Point B1", "note": "beta one", "parent_id": _S_B, "priority": 0, "createdAt": 500, "modifiedAt": 500},
]


def _straddle_visible_tree():
    return {
        "root": {"id": _S_ROOT, "name": "Root", "note": ""},
        "children": [
            _node(_S_A, "Section A", "body A", [
                _node(_S_A1, "Point A1", "alpha one"),
                _node(_S_A2, "Point A2", "alpha two"),
            ]),
            _node(_S_B, "Section B", "body B", [
                _node(_S_B1, "Point B1", "beta one"),
            ]),
        ],
    }


# --- Resolver #4: live-DOM modal, UNSORTED parent_of path (highest traffic) ---

def test_r4_modal_view_unsorted_parent_of_focus_a1():
    """The single most common interaction: an UNSORTED live-DOM modal row click,

    served by compose_markdown_modal_view's legacy parent_of chain walk
    (focus_ancestor_path=None). Locks the option-Y result via the PUBLIC call.
    """
    c = _client_with_cache(_S_CACHE)
    res = c.compose_markdown_modal_view(_straddle_visible_tree(), focus_leaf=_S_A1)
    assert res["success"] is True
    assert res["cache_stale"] is False
    # Section A is a BREADCRUMB (above the focused leaf A1) -> heading-only,
    # its body 'body A' is suppressed. A1 renders with its body.
    assert res["markdown"] == (
        "# Root\n\n## Section A\n\n### Point A1\n\nalpha one"
    )


def test_r4_modal_view_unsorted_no_focus_whole_document():
    c = _client_with_cache(_S_CACHE)
    res = c.compose_markdown_modal_view(_straddle_visible_tree(), focus_leaf=None)
    assert res["success"] is True
    assert res["markdown"] == (
        "# Root\n\n"
        "## Section A\n\nbody A\n\n"
        "### Point A1\n\nalpha one\n\n"
        "### Point A2\n\nalpha two\n\n"
        "## Section B\n\nbody B\n\n"
        "### Point B1\n\nbeta one"
    )


# --- Resolver #4: live-DOM modal, SORTED occurrence-exact path -------------

def test_r4_modal_view_sorted_focus_A_occurrence0_renders_A1():
    """Under a `created` sort, Section A is straddled and re-emitted. Focusing

    OCCURRENCE 0 must render the first A subtree (Point A1) — the symptom-#5
    occurrence-exact guarantee, via the PUBLIC modal-view call.

    U6 (ap:E03BN3): the created sort makes this a CHRONOLOGICAL render, so
    the focused Section A — a NON-LEAF of its option-Y render forest —
    renders heading-only ("body A" withheld by the occurrence-keyed
    chronological-ancestor cause). Golden updated in lockstep with the U6
    commit; occurrence-exactness (A1, never A2) is the preserved guarantee.
    """
    c = _client_with_cache(_S_CACHE)
    res = c.compose_markdown_modal_view(
        _straddle_visible_tree(), focus_leaf=_S_A,
        sort_by="created", sort_direction="asc",
        focus_ancestor_path=[_S_ROOT], focus_occurrence=0,
    )
    assert res["success"] is True
    assert res["markdown"] == (
        "# Root\n\n## Section A\n\n### Point A1\n\nalpha one"
    )


def test_r4_modal_view_sorted_focus_A_occurrence1_renders_A2():
    """OCCURRENCE 1 must render the SECOND A subtree (Point A2), NOT leak to the

    first. This is the exact behavior Step 12 must preserve after deleting the
    legacy resolvers. U6 (ap:E03BN3): "body A" is withheld (the focused
    Section A is a non-leaf of a CHRONOLOGICAL render's final forest); the
    occurrence-exact A2 subtree is the preserved guarantee.
    """
    c = _client_with_cache(_S_CACHE)
    res = c.compose_markdown_modal_view(
        _straddle_visible_tree(), focus_leaf=_S_A,
        sort_by="created", sort_direction="asc",
        focus_ancestor_path=[_S_ROOT], focus_occurrence=1,
    )
    assert res["success"] is True
    assert res["markdown"] == (
        "# Root\n\n## Section A\n\n### Point A2\n\nalpha two"
    )


def test_straddle_enumerator_reemits_A_as_two_occurrences():
    """Underpinning lock: the identity authority really does re-emit Section A

    twice under the created sort (otherwise the occurrence tests above would be
    vacuous).
    """
    c = _client_with_cache(_S_CACHE)
    cache = {n["id"]: dict(n) for n in _S_CACHE}
    reordered = c._reorder_tree_by_timestamp(
        [W._normalize_modal_visible_tree(_straddle_visible_tree())],
        sort_by="created", direction="asc", cache_by_id=cache,
    )
    recs = W._iter_markdown_focus_rows(reordered)
    a_occs = sorted(r["occurrence"] for r in recs if r["uuid"] == _S_A)
    assert a_occs == [0, 1]


# ===========================================================================
# FIXTURE 6 — empty-id SENTINEL ('Home') routing (transparent identity).
# ===========================================================================
def test_sentinel_transparent_focus_routes_through_index_slot():
    """A leading empty-id 'Home' sentinel consumes an index slot but never a

    path uuid; occurrence-exact focus still lands on the target. Locks the
    sentinel-routing behavior all resolvers rely on.
    """
    stree = _node("", "Home", "", [
        _node("R", "R", "", [
            _node("X", "X-first", "", [_node("LX1", "batch one", "leaf1")]),
            _node("X", "X-second", "", [_node("LX3", "batch three", "leaf3")]),
        ]),
    ])
    recs = W._iter_markdown_focus_rows([stree])
    x_rows = [r for r in recs if r["uuid"] == "X"]
    assert [r["ancestor_path"] for r in x_rows] == [["R"], ["R"]]

    ip1 = W._resolve_focus_index_path(
        [stree], uuid="X", ancestor_path=["R"], occurrence=1
    )
    assert ip1 is not None
    assert W._node_uuid_at_index_path([stree], ip1) == "X"
    # Repointed (3b) to the production materializer surface (see _focused_md):
    # the empty-id sentinel index-slot hop is handled inside the materializer's
    # occurrence-exact resolution.
    md = _focused_md([stree], "X", ["R"], 1)
    assert "batch three" in md
    assert "batch one" not in md
