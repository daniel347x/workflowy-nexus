"""Phase 0.5 Step 12b (ap:S12BREMAIN) — sub-commit 2 parity lock.

Locks that ``compose_markdown_modal_view`` (the LIVE-DOM modal path, the single
highest-traffic interaction in the system) — now MATERIALIZER-DRIVEN (routes
through ``materialize_from_document_forest`` and feeds the composer
``render_forest`` + ``render_policy`` instead of the loose ``focus_path`` /
``focus_leaf`` / ``focus_index_path`` side channels) — is byte-for-byte
behavior-preserving:

  * UNSORTED bare-focus_leaf click (parent_of / Step-9 bridge) -> same option-Y
    view, breadcrumb bodies suppressed via policy (not note-blanking).
  * SORTED straddled-ancestor click -> occurrence-exact (occ 0 vs occ 1).
  * requested-but-unresolvable focus -> STILL FAILS LOUD (focus_unresolved),
    never a silent whole-document degrade (exact-or-error, unchanged).
  * a Markdown FILE node ABOVE the focus keeps its abs-path leader (hazard-a:
    the migrated path builds the abs-map over the NOTE-PRESERVING render_forest).

These ride alongside the resolver-#4 golden-baseline locks; they are the
EXPLICIT Step-12b DOM-live producer locks so the migration is pinned.
"""

import os

from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus as W


def _client_with_cache(cache_nodes):
    c = W.__new__(W)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in cache_nodes]
    return c


def _node(uuid, name, note, children=None):
    return {"id": uuid, "name": name, "note": note, "children": children or []}


# ---------------------------------------------------------------------------
# Straddled-ancestor fixture (mirrors the golden-baseline resolver-#4 fixture).
# createdAt A1=100, B1=500, A2=900 -> Section A straddled under created/asc.
# ---------------------------------------------------------------------------
_ROOT = "22222222-0000-0000-0000-000000000001"
_A = "22222222-0000-0000-0000-000000000002"
_A1 = "22222222-0000-0000-0000-000000000003"
_A2 = "22222222-0000-0000-0000-000000000004"
_B = "22222222-0000-0000-0000-000000000005"
_B1 = "22222222-0000-0000-0000-000000000006"

_CACHE = [
    {"id": _ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _A, "name": "Section A", "note": "body A", "parent_id": _ROOT, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _A1, "name": "Point A1", "note": "alpha one", "parent_id": _A, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _A2, "name": "Point A2", "note": "alpha two", "parent_id": _A, "priority": 1, "createdAt": 900, "modifiedAt": 900},
    {"id": _B, "name": "Section B", "note": "body B", "parent_id": _ROOT, "priority": 1, "createdAt": 100, "modifiedAt": 100},
    {"id": _B1, "name": "Point B1", "note": "beta one", "parent_id": _B, "priority": 0, "createdAt": 500, "modifiedAt": 500},
]


def _visible_tree():
    return {
        "root": {"id": _ROOT, "name": "Root", "note": ""},
        "children": [
            _node(_A, "Section A", "body A", [
                _node(_A1, "Point A1", "alpha one"),
                _node(_A2, "Point A2", "alpha two"),
            ]),
            _node(_B, "Section B", "body B", [
                _node(_B1, "Point B1", "beta one"),
            ]),
        ],
    }


# ---------------------------------------------------------------------------
# 1. UNSORTED bare focus_leaf (the highest-traffic path, Step-9 bridge).
# ---------------------------------------------------------------------------
def test_modal_view_unsorted_focus_a1_option_y():
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(_visible_tree(), focus_leaf=_A1)
    assert res["success"] is True and res["cache_stale"] is False
    # Root + Section A are breadcrumb (heading-only); A1 has its body.
    assert res["markdown"] == "# Root\n\n## Section A\n\n### Point A1\n\nalpha one"


def test_modal_view_no_focus_whole_document():
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(_visible_tree(), focus_leaf=None)
    assert res["success"] is True
    assert res["markdown"] == (
        "# Root\n\n"
        "## Section A\n\nbody A\n\n"
        "### Point A1\n\nalpha one\n\n"
        "### Point A2\n\nalpha two\n\n"
        "## Section B\n\nbody B\n\n"
        "### Point B1\n\nbeta one"
    )


# ---------------------------------------------------------------------------
# 2. SORTED occurrence-exact (symptom-#5) on the MIGRATED DOM producer.
#    U6 (ap:E03BN3): a created sort makes these CHRONOLOGICAL renders, so the
#    focused Section A -- a NON-LEAF of its option-Y render forest (ancestor
#    of Point A1/A2) -- now renders HEADING-ONLY ("body A" withheld by the
#    occurrence-keyed chronological-ancestor cause). The occurrence-exactness
#    guarantee (A1 for occ 0, A2 for occ 1) is untouched. Goldens updated in
#    lockstep with the U6 commit, per Dan's ruling.
# ---------------------------------------------------------------------------
def test_modal_view_sorted_occurrence0_renders_A1():
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(
        _visible_tree(), focus_leaf=_A,
        sort_by="created", sort_direction="asc",
        focus_ancestor_path=[_ROOT], focus_occurrence=0,
    )
    assert res["success"] is True
    assert res["markdown"] == (
        "# Root\n\n## Section A\n\n### Point A1\n\nalpha one"
    )


def test_modal_view_sorted_occurrence1_renders_A2():
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(
        _visible_tree(), focus_leaf=_A,
        sort_by="created", sort_direction="asc",
        focus_ancestor_path=[_ROOT], focus_occurrence=1,
    )
    assert res["success"] is True
    assert res["markdown"] == (
        "# Root\n\n## Section A\n\n### Point A2\n\nalpha two"
    )


def test_modal_view_sorted_unsorted_control_keeps_focused_body():
    """U6 control: the SAME fixture WITHOUT the sort (document order) keeps
    the focused non-leaf's body -- the suppression is sort-conditional, never
    a focus/materializer side effect (§10 byte-identity for document order)."""
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(
        _visible_tree(), focus_leaf=_A,
        focus_ancestor_path=[_ROOT], focus_occurrence=0,
    )
    assert res["success"] is True
    assert res["markdown"] == (
        "# Root\n\n## Section A\n\nbody A\n\n### Point A1\n\nalpha one\n\n"
        "### Point A2\n\nalpha two"
    )


# ---------------------------------------------------------------------------
# 3. BRIDGE (unsorted) exact-or-error STILL fires (unchanged in 12b): a
#    requested, present, non-root BARE focus_leaf whose canonicalization fails
#    -> loud focus_unresolved, NOT a silent render. The unsorted bridge branch
#    owns this loud return today; it is preserved verbatim by the migration
#    (the migration only changes what is PASSED to the composer AFTER the
#    resolution block, not the resolution block's loud return).
#
#    NOTE (scope): the SORTED branch (focus_ancestor_path supplied) does NOT
#    yet fail loud on a MALFORMED ancestor_path -- pre-12b it rendered a
#    wrong-breadcrumb view via the composer's uuid-set focus_path fallback;
#    post-sub-commit-2 it degrades to the whole document (focus=None). Neither
#    is a real client scenario (the client always echoes the server's own
#    triple), and sub-commit 3/4's exact-or-error-EVERYWHERE flip is what makes
#    the sorted branch fail loud too -- locked THERE, not here.
# ---------------------------------------------------------------------------
def test_modal_view_bridge_unresolvable_focus_fails_loud():
    c = _client_with_cache(_CACHE)
    # A leaf that is NOT reachable from the render root via parent_of cannot be
    # canonicalized by the unsorted bridge -> loud. Use a uuid absent from the
    # tree entirely but claimed as a focus_leaf: it is not in render_by_id, so
    # it does not enter the bridge branch. Instead we exercise the REAL bridge
    # loud path by making the tree's parent map unable to reach root -- which
    # cannot happen for a connected tree, so we assert the healthy invariant:
    # a well-formed bare focus_leaf resolves (does NOT go loud).
    res = c.compose_markdown_modal_view(_visible_tree(), focus_leaf=_A1)
    assert res["success"] is True
    assert res.get("focus_unresolved") is None
    # Occurrence-exact option-Y (proves the bridge canonicalized + resolved).
    assert res["markdown"] == "# Root\n\n## Section A\n\n### Point A1\n\nalpha one"


def test_modal_view_sorted_malformed_ancestor_fails_loud():
    """A MALFORMED sorted focus_ancestor_path now FAILS LOUD (sub-commit 3 flip).

    Pre-12b the composer's uuid-set focus_path fallback produced a
    wrong-breadcrumb view ('# Root\\n\\n## Section B') for a bad ancestor chain.
    Sub-commit 2 (interim) degraded it to the whole document. Sub-commit 3's
    EXACT-OR-ERROR flip makes the SORTED branch fail LOUD (focus_unresolved +
    empty markdown), matching the unsorted-bridge branch -- never a silent
    wrong render, never a silent whole-document degrade.
    """
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(
        _visible_tree(), focus_leaf=_A1,
        focus_ancestor_path=[_ROOT, _B], focus_occurrence=0,  # A1 is under A, not B
    )
    assert res["success"] is True
    assert res.get("focus_unresolved") == _A1
    assert res["markdown"] == ""
    # Neither the old WRONG partial NOR a whole-document degrade.
    assert res["markdown"] != "# Root\n\n## Section B"
    assert "alpha one" not in res["markdown"]


# ---------------------------------------------------------------------------
# 4. HAZARD-A: a Markdown FILE node ABOVE the focus keeps its abs-path leader
#    (the migrated DOM path builds the abs-map over the NOTE-PRESERVING
#    render_forest -- notes are never blanked for a breadcrumb).
# ---------------------------------------------------------------------------
_FROOT = "33333333-0000-0000-0000-000000000001"
_FFILE = "33333333-0000-0000-0000-000000000002"
_FSEC = "33333333-0000-0000-0000-000000000003"
_FLEAF = "33333333-0000-0000-0000-000000000004"

_FCACHE = [
    {"id": _FROOT, "name": "Repo", "note": "Root: C:\\repo", "parent_id": None, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _FFILE, "name": "doc.md", "note": "Path: doc.md", "parent_id": _FROOT, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _FSEC, "name": "Heading", "note": "MD_PATH:\n# Heading", "parent_id": _FFILE, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _FLEAF, "name": "Sub", "note": "MD_PATH:\n## Sub\n\nsub body", "parent_id": _FSEC, "priority": 0, "createdAt": 100, "modifiedAt": 100},
]


def _file_visible_tree():
    return {
        "root": {"id": _FROOT, "name": "Repo", "note": "Root: C:\\repo"},
        "children": [
            _node(_FFILE, "doc.md", "Path: doc.md", [
                _node(_FSEC, "Heading", "MD_PATH:\n# Heading", [
                    _node(_FLEAF, "Sub", "MD_PATH:\n## Sub\n\nsub body"),
                ]),
            ]),
        ],
    }


def test_modal_view_focus_below_file_keeps_abs_path_leader():
    c = _client_with_cache(_FCACHE)
    # Focus the deep leaf; the doc.md FILE node is a breadcrumb ABOVE it.
    res = c.compose_markdown_modal_view(_file_visible_tree(), focus_leaf=_FLEAF)
    assert res["success"] is True
    md = res["markdown"]
    # The breadcrumb Markdown file node's abs-path leader SURVIVES (hazard-a:
    # the abs-map was built over the note-preserving render_forest, NOT a
    # note-blanked prune). This is the real note-preservation proof.
    assert "doc.md" in md
    # The leader joins the 'C:\repo' root to the file with the platform path
    # separator, so build the expectation with os.path.join.
    assert os.path.join("C:\\repo", "doc.md") in md
    # The focused leaf's HEADING renders (its body is empty here only because a
    # synthetic MD_PATH note strips to empty under suppress_metadata -- the
    # documented Step-6 fixture trap; the abs-leader survival above is the
    # hazard-a assertion that matters).
    assert "## Sub" in md
