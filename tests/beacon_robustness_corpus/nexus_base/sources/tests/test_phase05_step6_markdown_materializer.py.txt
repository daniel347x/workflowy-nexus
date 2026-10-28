"""Phase 0.5 Step 6 (ap:EXECLDG) — wire the MARKDOWN cache-tail through the

shared materializer, POLICY-DRIVEN body suppression (B-full).

WHAT STEP 6 CHANGED
-------------------
``render_stage2_filter_sort``'s MARKDOWN branch no longer threads the loose
``focus_path`` / ``focus_leaf`` / ``focus_index_path`` / ``body_suppressed_ids``
side channels into the composer. Instead it:
  1. builds a ``MaterializedRenderDocument`` via
     ``materialize_from_document_forest`` (note-preserving option-Y
     ``render_forest`` + a render-forest-keyed ``RenderPolicy``), then
  2. renders ``render_forest`` with ``render_policy=`` and NO focus params.

Breadcrumb body suppression is now the policy's OCCURRENCE / index-path-keyed
``breadcrumb_emit_body_false`` set (v3.3 #1 — NOT a uuid-set); DEPBOD
surviving-``--deprecated`` suppression is the uuid-keyed
``deprecated_emit_body_false`` set. The Markdown composer consults BOTH via
``RenderPolicy.emit_body_false_for`` when a ``render_policy`` is supplied;
``render_policy=None`` (every legacy resolver #2/#3/#4 caller) is byte-for-byte
unchanged.

WHAT THESE TESTS LOCK
  * MATERIALIZER breadcrumb-set CONTRACT (GPT-5.5 / Opus guardrail): the set is
    EXACTLY the render nodes STRICTLY ABOVE the focused leaf (strict-prefix of
    the leaf's render index_path), in RENDER coords — leaf + subtree excluded.
  * end-to-end MARKDOWN cache-tail parity: unfocused whole-document render is
    unchanged; a focused render shows breadcrumb ancestors HEADING-ONLY (bodies
    suppressed via policy) and the focused leaf's body present.
  * the focused materialized render EQUALS the dock's per-row view
    (``_render_slice_row_view``) — the 'GLIMPSE what the modal shows' identity,
    proving the two focus mechanisms converge.
  * ⭐ HAZARD-B: an off-path SHALLOWER Markdown sibling under a file node is
    pruned by focus, yet the file's H1 rebase baseline is unchanged (values
    computed from the UNPRUNED document, re-keyed to render coords) — heading
    levels do NOT shift. This is the Step-6 STOP-on-divergence edge.
  * DEPBOD via policy: a surviving ``--deprecated`` node renders header-only in
    Markdown when its uuid is in the materialized policy's deprecated set.

No network, no real Workflowy, no disk (the raw-dump ``root_uuid`` GLIMPSE cell
needs NO WebSocket).
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
from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus as W,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _node(nid, name, note="", children=None):
    return {"id": nid, "name": name, "note": note, "children": children or []}


def _row(uuid, ancestor_path, occurrence=0):
    return {
        "mode": "row",
        "uuid": uuid,
        "ancestor_path": list(ancestor_path),
        "occurrence": occurrence,
    }


def _client(cache_nodes):
    c = W.__new__(W)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in cache_nodes]
    return c


def _run_root_glimpse(monkeypatch, cache_nodes, root_uuid, *, focus=None):
    client = _client(cache_nodes)
    monkeypatch.setattr(srv, "get_client", lambda: client)
    return asyncio.run(
        srv._glimpse_root_uuid_from_cache(
            root_uuid=root_uuid,
            as_markdown=True,
            output_file=None,
            sort_by=None,
            sort_direction="asc",
            headings_only=False,
            since=None,
            focus=focus,
        )
    )


# ===========================================================================
# 1) MATERIALIZER breadcrumb-set CONTRACT (the load-bearing guardrail)
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


def test_materializer_breadcrumb_set_is_strict_prefixes_only():
    """Focusing a1 (root/a/a1): the breadcrumb set is EXACTLY {root, a} in

    RENDER coords — the strict prefixes of the leaf's render index_path. The
    leaf itself (a1) and everything at/below it are NOT in the set.
    """
    forest = _plain_forest()
    mrd = W.materialize_from_document_forest(
        forest, focus=_row("a1", ["root", "a"], 0)
    )
    bc = mrd.policy_by_occurrence.breadcrumb_emit_body_false
    # render_forest is the single-rooted option-Y spine: root (0,) -> a (0,0)
    # -> a1 (0,0,0). Breadcrumbs = strict prefixes of (0,0,0) => (0,) and (0,0).
    assert bc == frozenset({(0,), (0, 0)})
    # The focused leaf's own render key is NOT suppressed.
    assert (0, 0, 0) not in bc
    # emit_body_false_for agrees at each key.
    p = mrd.policy_by_occurrence
    assert p.emit_body_false_for(render_key=(0,), include_deprecated=False) is True
    assert p.emit_body_false_for(render_key=(0, 0), include_deprecated=False) is True
    assert p.emit_body_false_for(render_key=(0, 0, 0), include_deprecated=False) is False


def test_materializer_no_focus_empty_breadcrumb_set():
    mrd = W.materialize_from_document_forest(_plain_forest(), focus=None)
    assert mrd.policy_by_occurrence.breadcrumb_emit_body_false == frozenset()


def test_materializer_root_row_focus_empty_breadcrumb_set():
    # A root-row focus renders the whole rooted document -> no breadcrumb.
    mrd = W.materialize_from_document_forest(
        _plain_forest(), focus=_row("root", [], 0)
    )
    assert mrd.policy_by_occurrence.breadcrumb_emit_body_false == frozenset()


def test_materializer_deprecated_set_passes_through_uuid_keyed():
    mrd = W.materialize_from_document_forest(
        _plain_forest(), focus=None, deprecated_ids={"a", "b"}
    )
    p = mrd.policy_by_occurrence
    assert p.deprecated_emit_body_false == frozenset({"a", "b"})
    # uuid-keyed, and gated by include_deprecated.
    assert p.emit_body_false_for(uuid="a", include_deprecated=True) is True
    assert p.emit_body_false_for(uuid="a", include_deprecated=False) is False
    assert p.emit_body_false_for(uuid="z", include_deprecated=True) is False


# ===========================================================================
# 2) END-TO-END Markdown cache-tail: unfocused parity + focused suppression
# ===========================================================================
_PLAIN_CACHE = [
    {"id": "root", "name": "Root", "note": "root body", "parent_id": None, "priority": 0},
    {"id": "a", "name": "Alpha", "note": "alpha body", "parent_id": "root", "priority": 0},
    {"id": "a1", "name": "Alpha-1", "note": "a1 body", "parent_id": "a", "priority": 0},
    {"id": "a2", "name": "Alpha-2", "note": "a2 body", "parent_id": "a", "priority": 1},
    {"id": "b", "name": "Beta", "note": "beta body", "parent_id": "root", "priority": 1},
]


def test_e2e_unfocused_whole_document_unchanged(monkeypatch):
    res = _run_root_glimpse(monkeypatch, _PLAIN_CACHE, "root", focus=None)
    assert res["success"] is True
    # Byte-for-byte the plain whole-document render (bodies present everywhere).
    assert res["preview_markdown"] == (
        "# Root\n\nroot body\n\n"
        "## Alpha\n\nalpha body\n\n"
        "### Alpha-1\n\na1 body\n\n"
        "### Alpha-2\n\na2 body\n\n"
        "## Beta\n\nbeta body"
    )


def test_e2e_focus_breadcrumb_bodies_suppressed_via_policy(monkeypatch):
    # Focus a1: root + Alpha render HEADING-ONLY (bodies suppressed via the
    # policy breadcrumb set, NOT focus_* params); a1 renders WITH its body.
    res = _run_root_glimpse(
        monkeypatch, _PLAIN_CACHE, "root", focus=_row("a1", ["root", "a"], 0)
    )
    assert res["success"] is True
    assert res["preview_markdown"] == "# Root\n\n## Alpha\n\n### Alpha-1\n\na1 body"


def test_e2e_focus_matches_dock_per_row_view(monkeypatch):
    # The materialized focused render == the dock's per-row view for the same
    # row/tree — the two focus mechanisms converge (identity lock).
    from workflowy_mcp.f8_slice_policy import build_slice_subtree_from_cache
    cache_by_id = {n["id"]: dict(n) for n in _PLAIN_CACHE}
    subtree = build_slice_subtree_from_cache(cache_by_id, "root", [], member_all=True)
    res = _run_root_glimpse(
        monkeypatch, _PLAIN_CACHE, "root", focus=_row("a1", ["root", "a"], 0)
    )
    dock = W._render_slice_row_view(
        subtree, "a1", ["root", "a"], occurrence=0, root_in_non_md_file=False
    )
    assert res["preview_markdown"] == dock


# ===========================================================================
# 3) ⭐ HAZARD-B — focus prunes an off-path SHALLOWER md sibling; the file's
#    H1 rebase baseline must NOT shift (values from the unpruned document).
# ===========================================================================
#   doc.md (Root: ...)                       <- file node, H1
#     Deep    (### =3)                        <- on-path
#       Deeper(##### =5)                       <- focus target
#     Shallow (## =2)  OFF-PATH, owns file min shallowest depth (2)
#
# Unfocused, the file rebases so the shallowest visible md-depth (Shallow=2)
# maps to H1 => Deep(3)->H2, Deeper(5)->H4, Shallow(2)->H1.
# Focusing Deeper prunes Shallow away. A NAIVE recompute over the pruned tree
# would take Deep(3) as the shallowest => Deep->H1, Deeper->H3 (SHIFTED). The
# materializer keeps the baseline=2 (from the unpruned document), so
# Deep stays H2 and Deeper stays H4 even after the prune.
# NOTE on the MD_PATH note format: _md_depth_from_note_for_preview requires the
# FIRST non-empty note line to be EXACTLY "MD_PATH:" (a bare marker), with the
# actual '#'-heading line(s) BELOW it. It then takes the LAST heading line's
# hash-count as the node's md-depth. (A first line like "MD_PATH: doc.md#Deep"
# does NOT match and yields depth=None -> the node renders STRUCTURALLY, which
# is a common fixture trap. Verified live against the parser.)
_HAZB_CACHE = [
    {"id": "fr", "name": "doc.md", "note": "Root: E:\\tmp\\doc.md", "parent_id": None, "priority": 0},
    {"id": "deep", "name": "Deep", "note": "MD_PATH:\n### Deep", "parent_id": "fr", "priority": 0},
    {"id": "deeper", "name": "Deeper", "note": "MD_PATH:\n##### Deeper\n\ndeeper body", "parent_id": "deep", "priority": 0},
    {"id": "shallow", "name": "Shallow", "note": "MD_PATH:\n## Shallow", "parent_id": "fr", "priority": 1},
]


def test_e2e_hazard_b_unfocused_baseline(monkeypatch):
    res = _run_root_glimpse(monkeypatch, _HAZB_CACHE, "fr", focus=None)
    md = res["preview_markdown"]
    # Shallowest md-depth in the whole file subtree is 2 (Shallow) => the file
    # rebases so depth-2 maps to H1: Deep(3)->H2 (##), Deeper(5)->H4 (####),
    # Shallow(2)->H1 (#). VERIFIED live.
    assert md == (
        "# doc.md\n\n**E:\\tmp\\doc.md**\n\nRoot: E:\\tmp\\doc.md\n\n"
        "## Deep\n\n#### Deeper\n\n# Shallow\n\n---"
    )


def test_e2e_hazard_b_focus_does_not_shift_headings(monkeypatch):
    # Focus Deeper -> Shallow (the off-path shallowest sibling) is pruned.
    # The file baseline MUST remain 2 (from the UNPRUNED document), so Deep
    # stays H2 (##) and Deeper stays H4 (####). A regression to a pruned
    # recompute would take Deep(3) as the shallowest => Deep->H1, Deeper->H3.
    res = _run_root_glimpse(
        monkeypatch, _HAZB_CACHE, "fr", focus=_row("deeper", ["fr", "deep"], 0)
    )
    md = res["preview_markdown"]
    # Exact byte-for-byte. The KEY hazard-b guarantee: Deep stays H2 (##) and
    # Deeper stays H4 (####) even though the off-path shallowest sibling
    # (Shallow, depth 2) was pruned by focus -- because the file's rebase
    # baseline (2) came from the UNPRUNED document, re-keyed to render coords.
    #
    # NOTE (verified live): under suppress_metadata=True a Cartographer MD_PATH
    # note ('MD_PATH:\n##### Deeper\n\ndeeper body') strips to EMPTY -- the whole
    # note IS metadata; a Markdown section's prose body is not modeled in these
    # synthetic nodes -- so NO node emits a body here. That is orthogonal to
    # the hazard-b levels this test locks. (The plain-forest E2E test above
    # exercises breadcrumb BODY suppression via non-MD_PATH notes.)
    assert md == (
        "# doc.md\n\n**E:\\tmp\\doc.md**\n\n"
        "## Deep\n\n#### Deeper\n\n---"
    )
    # Explicit anti-regression guards on the hazard-b shift.
    assert "Shallow" not in md            # off-path sibling pruned
    assert "#### Deeper" in md            # H4 preserved (baseline 2, not 3)
    assert "\n# Deep\n" not in md         # would be H1 under a pruned recompute
    assert "\n### Deeper" not in md       # would be H3 under a pruned recompute


# ===========================================================================
# 4) DEPBOD via policy — a surviving --deprecated node renders header-only in
#    Markdown when the materialized policy carries its uuid.
# ===========================================================================
def test_depbod_via_policy_markdown_header_only():
    # Build a materialized doc (no focus) marking 'a' deprecated, then render
    # via the composer with the policy. 'a' shows its heading, NOT its body.
    forest = _plain_forest()
    mrd = W.materialize_from_document_forest(
        forest, focus=None, deprecated_ids={"a"}
    )
    md = W._build_markdown_preview_from_tree(
        mrd.render_forest,
        suppress_metadata=True,
        render_policy=mrd.policy_by_occurrence,
    )
    # 'Alpha' heading present, 'alpha body' withheld; siblings' bodies present.
    assert "## Alpha" in md
    assert "alpha body" not in md
    assert "root body" in md
    assert "beta body" in md
    # a1/a2 (descendants of the deprecated node) still render normally — DEPBOD
    # is a per-NODE header-only suppression, not a subtree drop.
    assert "a1 body" in md
