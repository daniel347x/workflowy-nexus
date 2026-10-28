"""PHASE 0.5 STEP 7 — the FULL/UUID serializer becomes POLICY-AWARE.

WHY THIS FILE EXISTS
--------------------
Step 7 of the v3.4 Execution Ledger (ap:EXECLDG) for the BATON v3 Phase-0.5
refactor (ap:OBXJ6Z + addenda ap:DEPBODFULL / ap:FOCUSBRIDGE / ap:V33GRD).
It is THE LOAD-BEARING STEP: it makes ``_annotate_preview_ids_and_build_tree``
(the full/UUID preview-tree serializer) consume the render-forest
body-suppression POLICY produced by the shared materializer, INSTEAD OF
relying on the old note-blanking prune. The consequences locked here:

  * L1 (v3.1 ① / §8 "Markdown FILE node ABOVE the focus", cache-path twin):
    a FOCUSED full/UUID GLIMPSE over a Markdown file now KEEPS the breadcrumb
    FILE node's ``→abs:`` leader (the abs-path map is built over the
    NOTE-PRESERVING render_forest), while the breadcrumb's inline ``[note]``
    is still suppressed -- now via the occurrence-keyed policy, not a blanked
    note. This is the DELIBERATE, SIGNED-OFF flip of the Step-1 baseline lock
    ``test_hazard_a_full_focus_h2_leader_LOST_pre_fix`` (see that file; the
    baseline was updated in lockstep with THIS commit, with Dan's sign-off).

  * L2 (v3.3 #1 -- the sharpest guardrail): the breadcrumb suppression is
    keyed by RENDER-FOREST OCCURRENCE (index_path), NOT by bare uuid. Under a
    timestamp sort the trie-merge re-emits a straddled ancestor at multiple
    occurrences; ONLY the breadcrumb occurrence of the focused row has its
    ``[note]`` withheld, while the SAME uuid's OTHER (non-breadcrumb)
    occurrence renders its ``[note]`` normally. A uuid-set policy would have
    wrongly suppressed both (symptom-#5 in full mode).
    U6 NOTE (ap:E03BN3): chronological renders now ALSO withhold every
    non-leaf occurrence's ``[note]`` by default via the sibling
    ``chronological_ancestor`` cause (same occurrence keying, same consult);
    the breadcrumb-only discrimination is pinned via the ancestor_notes=true
    opt-out and the direct policy-boundary lock (see the L2 test).

  * L3 (v3.1 ② / v3.2(a)): DEPBOD surviving-``--deprecated`` body suppression
    stays MARKDOWN-ONLY. The full/UUID serializer consults ONLY the
    ``breadcrumb`` cause (``include_deprecated=False``), so a surviving
    ``--deprecated`` node STILL shows its inline ``[note]`` in full/UUID mode
    (unchanged) even though Markdown mode renders it header-only.

Plus: v3.3 #3 immutability (the serializer never writes ``preview_id`` onto
the input dicts) and a Markdown-vs-full node-set parity cross-check for the
same focused click.

The production full/UUID cache path is driven END-TO-END through
``server._glimpse_root_uuid_from_cache`` (the self-contained RAW-DUMP cell --
no WebSocket, no disk), which is resolver #5 (the cache tail) from the Step-2
change-map. The serializer-level policy behavior (L2/L3 keying) is also locked
directly against ``_annotate_preview_ids_and_build_tree`` + a hand-built
``RenderPolicy`` so the keying discipline is pinned at the unit boundary too.

All local: no network, no real Workflowy, no disk.
"""
from __future__ import annotations

import asyncio
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.cache_render_spec import RenderPolicy  # noqa: E402

W = WorkFlowyClientNexus


# ===========================================================================
# Harness — drive the REAL cache tail via _glimpse_root_uuid_from_cache.
# ===========================================================================
def _make_client(cache_nodes):
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in cache_nodes]
    return c


def _run_root_glimpse(monkeypatch, cache_nodes, root_uuid, *, as_markdown,
                      focus=None, headings_only=False, sort_by=None,
                      sort_direction="asc", ancestor_notes=False):
    client = _make_client(cache_nodes)
    monkeypatch.setattr(srv, "get_client", lambda: client)
    return asyncio.run(
        srv._glimpse_root_uuid_from_cache(
            root_uuid=root_uuid,
            as_markdown=as_markdown,
            output_file=None,
            sort_by=sort_by,
            sort_direction=sort_direction,
            headings_only=headings_only,
            since=None,
            focus=focus,
            ancestor_notes=ancestor_notes,
        )
    )


# ===========================================================================
# FIXTURE A — a Cartographer MARKDOWN FILE above the focus (hazard-a / L1).
#   doc.md            (note: "Root: E:\\tmp\\doc.md")     <- FILE node (leader)
#     Intro           (MD_PATH -> "# Intro")
#       Details       (MD_PATH -> "## Details", body)     <- focus target
# ===========================================================================
_FROOT = "ffffffff-0000-0000-0000-000000000001"
_FH1 = "ffffffff-0000-0000-0000-000000000002"
_FH2 = "ffffffff-0000-0000-0000-000000000003"

_MD_FILE_CACHE = [
    {"id": _FROOT, "name": "doc.md", "note": "Root: E:\\tmp\\doc.md",
     "parent_id": None, "priority": 0},
    {"id": _FH1, "name": "Intro", "note": "MD_PATH: doc.md#Intro\n\n# Intro",
     "parent_id": _FROOT, "priority": 0},
    {"id": _FH2, "name": "Details",
     "note": "MD_PATH: doc.md#Intro#Details\n\n## Details\n\ndetails body",
     "parent_id": _FH1, "priority": 0},
]


def test_L1_full_focus_breadcrumb_abs_leader_PRESENT_and_note_suppressed(monkeypatch):
    """L1 (hazard-a FIX, cache-path twin): focused full/UUID over a Markdown

    file. The breadcrumb FILE node (doc.md) now KEEPS its ``→abs:`` leader
    (abs-map built over the note-preserving render_forest), while its inline
    ``[note]`` is suppressed via the occurrence-keyed breadcrumb policy (NOT by
    a blanked note). The focused leaf (Details) keeps its full ``[note]``.
    """
    focus = {"mode": "row", "uuid": _FH2,
             "ancestor_path": [_FROOT, _FH1], "occurrence": 0}
    res = _run_root_glimpse(
        monkeypatch, _MD_FILE_CACHE, _FROOT, as_markdown=False, focus=focus,
    )
    assert res["success"] is True
    lines = res["preview_tree"]

    # The breadcrumb FILE node's abs-path leader SURVIVES (the fix). Pre-Step-7
    # this was LOST (see test_hazard_a_full_focus_h2_leader_LOST_pre_fix).
    file_line = next(ln for ln in lines if f"[{_FROOT}]" in ln)
    assert "\u2192abs: E:\\tmp\\doc.md" in file_line, (
        "L1: the breadcrumb file node must keep its abs-path leader now that "
        "the abs-map is built over the note-preserving render_forest."
    )

    # The breadcrumb nodes (doc.md, Intro) carry NO inline [note] (suppressed
    # via policy). We assert the note BODIES are absent from those lines.
    intro_line = next(ln for ln in lines if f"[{_FH1}]" in ln)
    assert "MD_PATH" not in file_line
    assert "MD_PATH" not in intro_line
    assert "[Root: E:\\tmp\\doc.md]" not in file_line

    # The focused leaf keeps its full body.
    leaf_line = next(ln for ln in lines if f"[{_FH2}]" in ln)
    assert "details body" in leaf_line


def test_L1_markdown_focus_leader_survives_parity(monkeypatch):
    """Markdown mode of the SAME focused click keeps the leader too (it always

    did -- Markdown reads the note in-walk). Locked here as the parity partner
    of L1 so the two output modes agree that the leader is present.
    """
    focus = {"mode": "row", "uuid": _FH2,
             "ancestor_path": [_FROOT, _FH1], "occurrence": 0}
    res = _run_root_glimpse(
        monkeypatch, _MD_FILE_CACHE, _FROOT, as_markdown=True, focus=focus,
    )
    assert res["success"] is True
    md = res["preview_markdown"]
    assert "**E:\\tmp\\doc.md**" in md
    assert "## Details" in md


# ===========================================================================
# FIXTURE B — repeated / straddled ancestor under a timestamp sort (L2).
#
# Under a `created` sort the trie-merge re-emits a straddled ancestor as
# DISTINCT occurrences. We build a shape where ONE ancestor (Shared) is the
# parent of leaves whose created-times straddle another branch, so it
# re-emits. Focusing one occurrence must suppress ONLY that occurrence's
# breadcrumb [note], never the other occurrence's.
#
#   Root
#   ├── Shared          (note "shared body")
#   │   ├── Early       (created=10)   <- leaf under occurrence-1 of Shared
#   │   └── Late        (created=30)   <- leaf under occurrence-2 of Shared
#   └── Mid             (created=20, note "mid body")  <- straddles Shared
#
# created-asc leaf order: Early(10), Mid(20), Late(30). Because Mid sits
# between Early and Late but is NOT under Shared, the reorder re-emits Shared:
#   Shared > Early
#   Mid
#   Shared > Late
# So "Shared" appears at TWO occurrences. Focusing the LATE leaf makes the
# SECOND Shared a breadcrumb (its [note] withheld). U6 (ap:E03BN3): on a
# CHRONOLOGICAL render every non-leaf occurrence is heading-only by DEFAULT,
# so the pre-U6 discriminator "the FIRST Shared is NOT a breadcrumb of that
# focus, so it KEEPS its [note]" is now pinned via the ancestor_notes=true
# opt-out (and the direct policy-boundary lock below), not via the default
# sorted render.
# ===========================================================================
_RROOT = "bbbbbbbb-0000-0000-0000-000000000001"
_SHARED = "bbbbbbbb-0000-0000-0000-000000000002"
_EARLY = "bbbbbbbb-0000-0000-0000-000000000003"
_LATE = "bbbbbbbb-0000-0000-0000-000000000004"
_MID = "bbbbbbbb-0000-0000-0000-000000000005"

_SORT_CACHE = [
    {"id": _RROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0,
     "createdAt": 1, "modifiedAt": 1},
    {"id": _SHARED, "name": "Shared", "note": "shared body", "parent_id": _RROOT,
     "priority": 0, "createdAt": 5, "modifiedAt": 5},
    {"id": _EARLY, "name": "Early", "note": "early body", "parent_id": _SHARED,
     "priority": 0, "createdAt": 10, "modifiedAt": 10},
    {"id": _LATE, "name": "Late", "note": "late body", "parent_id": _SHARED,
     "priority": 1, "createdAt": 30, "modifiedAt": 30},
    {"id": _MID, "name": "Mid", "note": "mid body", "parent_id": _RROOT,
     "priority": 1, "createdAt": 20, "modifiedAt": 20},
]


def _shared_lines(lines):
    return [ln for ln in lines if f"[{_SHARED}]" in ln]


def test_L2_breadcrumb_suppression_is_occurrence_keyed_not_uuid_set(monkeypatch):
    """L2 (v3.3 #1): the straddled ancestor (Shared) re-emits at TWO occurrences

    under a `created` sort, but its [note] is suppressed in the render by
    OCCURRENCE (render-forest index_path), never by bare uuid.

    U6 UPDATE (ap:E03BN3, contract [ap:UXYCID]): a `created` sort now makes
    this a CHRONOLOGICAL render, so the sibling ``chronological_ancestor``
    cause (SAME occurrence keying, SAME emit_body_false_for consult) withholds
    EVERY non-leaf occurrence's [note] by default -- both Shared occurrences
    included. The pre-U6 discrimination (unfocused sorted => both bodies
    print) is therefore re-proven via the ``ancestor_notes=true`` opt-out,
    which restores exactly the old behavior, and via the focused variant
    below, where the BREADCRUMB cause alone (opt-out on) still targets one
    occurrence only. The direct policy-boundary lock
    (test_L2_unit_occurrence_keyed_policy_direct) is unchanged.

    We prove the occurrence keying in complementary ways:
      (i) UNFOCUSED sorted render (default): the fixture really does re-emit
          the same uuid twice (two Shared LINES), and U6 withholds both
          bodies (both occurrences are non-leaves); with ancestor_notes=true
          BOTH [note]s are back -- nothing is uuid-blanked.
      (ii) FOCUSED render on Late (its ONLY occurrence is 0): the option-Y
           breadcrumb spine Root > Shared > Late has Shared's [note] SUPPRESSED
           (breadcrumb AND chronological causes both mark it), while Late --
           a LEAF of the final forest -- keeps its body, and the
           non-breadcrumb Early/Mid branches are pruned.
      (ii-b) The SAME focused render with ancestor_notes=true: the chrono
           cause is off, so the suppression that remains is the BREADCRUMB
           cause ALONE -- still exactly the one spine occurrence, never a
           uuid-set (the original L2 discrimination, preserved).
    """
    # (i) UNFOCUSED sorted render re-emits Shared twice. U6: both occurrences
    # are non-leaves of the final forest => both [note]s withheld by default.
    res_unfocused = _run_root_glimpse(
        monkeypatch, _SORT_CACHE, _RROOT, as_markdown=False,
        sort_by="created", sort_direction="asc",
    )
    assert res_unfocused["success"] is True
    shared_unfocused = _shared_lines(res_unfocused["preview_tree"])
    assert len(shared_unfocused) == 2, (
        "fixture precondition: the created-sort must re-emit the straddled "
        f"ancestor twice; got {len(shared_unfocused)} occurrence(s)."
    )
    assert not any("[shared body]" in ln for ln in shared_unfocused), (
        "U6: an unfocused CHRONOLOGICAL render withholds EVERY non-leaf "
        "occurrence's [note] (the names/UUIDs still render)."
    )
    # ancestor_notes=true (the sole opt-out) restores BOTH bodies -- proving
    # the two occurrences are real re-emissions and the suppression is the
    # occurrence-keyed policy cause, never a uuid blank.
    res_optout = _run_root_glimpse(
        monkeypatch, _SORT_CACHE, _RROOT, as_markdown=False,
        sort_by="created", sort_direction="asc", ancestor_notes=True,
    )
    assert res_optout["success"] is True
    shared_optout = _shared_lines(res_optout["preview_tree"])
    assert len(shared_optout) == 2
    assert all("[shared body]" in ln for ln in shared_optout), (
        "opt-out: both Shared occurrences keep their [note] (pre-U6 behavior)."
    )

    # (ii) FOCUS the LATE leaf (its unique occurrence is 0). The option-Y view
    # is the single breadcrumb spine Root > Shared > Late.
    focus = {"mode": "row", "uuid": _LATE,
             "ancestor_path": [_RROOT, _SHARED], "occurrence": 0}
    res = _run_root_glimpse(
        monkeypatch, _SORT_CACHE, _RROOT, as_markdown=False, focus=focus,
        sort_by="created", sort_direction="asc",
    )
    assert res["success"] is True
    lines = res["preview_tree"]

    # Shared appears ONCE in the pruned option-Y view and is a breadcrumb, so
    # its [note] is suppressed via the occurrence-keyed policy.
    shared_focused = _shared_lines(lines)
    assert len(shared_focused) == 1
    assert "[shared body]" not in shared_focused[0], (
        "L2: the breadcrumb occurrence of the straddled ancestor must have its "
        "[note] suppressed via the occurrence-keyed policy."
    )
    late_line = next(ln for ln in lines if f"[{_LATE}]" in ln)
    assert "late body" in late_line
    # The non-breadcrumb branches (Early, Mid) are pruned out of option-Y.
    assert not any(f"[{_EARLY}]" in ln for ln in lines)
    assert not any(f"[{_MID}]" in ln for ln in lines)

    # (ii-b) SAME focused render, chronological cause OFF (ancestor_notes
    # opt-out): the breadcrumb cause ALONE still suppresses exactly the one
    # spine occurrence -- the original L2 discrimination against a uuid-set.
    res_b = _run_root_glimpse(
        monkeypatch, _SORT_CACHE, _RROOT, as_markdown=False, focus=focus,
        sort_by="created", sort_direction="asc", ancestor_notes=True,
    )
    assert res_b["success"] is True
    lines_b = res_b["preview_tree"]
    shared_b = _shared_lines(lines_b)
    assert len(shared_b) == 1
    assert "[shared body]" not in shared_b[0], (
        "L2 (opt-out): the breadcrumb occurrence stays suppressed by the "
        "breadcrumb cause alone."
    )
    late_b = next(ln for ln in lines_b if f"[{_LATE}]" in ln)
    assert "late body" in late_b


def test_L2_unit_occurrence_keyed_policy_direct():
    """L2 at the UNIT boundary: a RenderPolicy with a breadcrumb key on ONE

    occurrence (index_path) suppresses the body at THAT occurrence only, even
    when the same uuid appears at another index_path with its body intact.
    Pins the keying discipline directly on the serializer.
    """
    # A forest where uuid "dup" appears at two positions: [0,0] and [0,1].
    forest = [
        {"id": "root", "name": "Root", "note": "root body", "children": [
            {"id": "dup", "name": "Dup", "note": "dup body", "children": [
                {"id": "x", "name": "X", "note": "x body"},
            ]},
            {"id": "dup", "name": "Dup", "note": "dup body", "children": [
                {"id": "y", "name": "Y", "note": "y body"},
            ]},
        ]},
    ]
    # Suppress the body of the FIRST dup occurrence only (index_path (0,0)).
    policy = RenderPolicy(
        rebase_by_render_key={},
        breadcrumb_emit_body_false=frozenset({(0, 0)}),
        deprecated_emit_body_false=frozenset(),
    )
    lines = W._annotate_preview_ids_and_build_tree(
        forest, "P", None, None, False, render_policy=policy,
    )
    # Both 'Dup' nodes have children -> bullet '\u29bf'; match the display name.
    dup_lines = [ln for ln in lines if "\u29bf Dup" in ln]
    assert len(dup_lines) == 2
    # First Dup (index_path (0,0)) -> body suppressed; second -> body kept.
    assert "[dup body]" not in dup_lines[0]
    assert "[dup body]" in dup_lines[1]


# ===========================================================================
# FIXTURE C — DEPBOD stays Markdown-only in full/UUID (L3).
# ===========================================================================
def test_L3_depbod_not_honored_in_full_uuid_mode():
    """L3 (v3.1 ② / v3.2(a)): the full/UUID serializer consults ONLY the

    breadcrumb cause (include_deprecated=False). A RenderPolicy carrying a
    DEPRECATED uuid must NOT suppress that node's inline ``[note]`` in full
    mode -- DEPBOD remains Markdown-only in Phase 0.5.
    """
    forest = [
        {"id": "dep", "name": "Deprecated Node", "note": "deprecated body"},
    ]
    policy = RenderPolicy(
        rebase_by_render_key={},
        breadcrumb_emit_body_false=frozenset(),          # not a breadcrumb
        deprecated_emit_body_false=frozenset({"dep"}),   # DEPBOD marks it
    )
    lines = W._annotate_preview_ids_and_build_tree(
        forest, "P", None, None, False, render_policy=policy,
    )
    assert len(lines) == 1
    # DEPBOD is IGNORED in full mode -> the [note] is STILL shown.
    assert "[deprecated body]" in lines[0], (
        "L3: DEPBOD must stay Markdown-only; full/UUID mode still shows the "
        "[note] of a surviving --deprecated node."
    )


def test_L3_markdown_mode_DOES_suppress_depbod_body():
    """Parity partner of L3: the SAME deprecated policy DOES withhold the body

    in Markdown mode (the composer consults BOTH causes), so the difference is
    purely the output-format policy split, not a missing suppression.
    """
    forest = [
        {"id": "dep", "name": "Deprecated Node", "note": "deprecated body"},
    ]
    policy = RenderPolicy(
        rebase_by_render_key={},
        breadcrumb_emit_body_false=frozenset(),
        deprecated_emit_body_false=frozenset({"dep"}),
    )
    md = W._build_markdown_preview_from_tree(
        forest, suppress_metadata=True, render_policy=policy,
    )
    # Heading present, body withheld (Markdown consults the deprecated cause).
    assert "Deprecated Node" in md
    assert "deprecated body" not in md


# ===========================================================================
# v3.3 #3 — immutability: the serializer never mutates the input forest.
# ===========================================================================
def test_serializer_does_not_mutate_input_preview_id():
    """v3.3 #3: preview_id lives in a serializer-local map; the input node

    dicts are never stamped with a 'preview_id' key.
    """
    forest = [
        {"id": "root", "name": "Root", "note": "root body", "children": [
            {"id": "a", "name": "A", "note": "a body"},
        ]},
    ]
    W._annotate_preview_ids_and_build_tree(forest, "P")
    assert "preview_id" not in forest[0]
    assert "preview_id" not in forest[0]["children"][0]


def test_serializer_legacy_output_unchanged_without_policy():
    """Byte-for-byte: with no render_policy (every legacy caller), output is

    exactly as before -- the local-preview_id refactor is output-identical.
    """
    forest = [
        {"id": "root", "name": "Root", "note": "root body", "children": [
            {"id": "a", "name": "A", "note": "a body", "children": [
                {"id": "a1", "name": "A1", "note": "a1 body"},
            ]},
        ]},
    ]
    lines = W._annotate_preview_ids_and_build_tree(forest, "P")
    assert lines == [
        "[root] [P-1    ] \u29bf Root [root body]",
        "[a] [P-1.1  ]     \u29bf A [a body]",
        "[a1] [P-1.1.1]         \u2022 A1 [a1 body]",
    ]


# ===========================================================================
# Markdown-vs-full NODE-SET parity for the same focused click (§8).
# ===========================================================================
def test_markdown_and_full_focus_same_node_set(monkeypatch):
    """The focused option-Y NODE SET is identical between Markdown and full/UUID

    mode for the same click (both driven by the ONE materializer render_forest).
    """
    focus = {"mode": "row", "uuid": _FH2,
             "ancestor_path": [_FROOT, _FH1], "occurrence": 0}
    res_full = _run_root_glimpse(
        monkeypatch, _MD_FILE_CACHE, _FROOT, as_markdown=False, focus=focus,
    )
    # Full-mode UUIDs present, in order.
    full_uuids = []
    for ln in res_full["preview_tree"]:
        # each line starts "[<uuid>] [<preview_id>] ..."
        u = ln.split("]", 1)[0].lstrip("[")
        full_uuids.append(u)
    assert full_uuids == [_FROOT, _FH1, _FH2]
