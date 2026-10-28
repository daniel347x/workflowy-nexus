"""Phase 1 multi-select — cross-file file-boundary presentation (ap:MSFBP).

The ONE remaining Phase-1 multi-select defect: when a covering render
(``focus={mode:'rows'}``) selects leaves that live in TWO DIFFERENT
Cartographer files sharing a common ancestor, with plain STRUCTURAL nodes
between the shared ancestor and each file, the composer's SINGLE file-boundary
regime (``standalone_blocks``) either:

  (1) RE-EMITS the shared root->ancestor spine once per file branch (the
      literal duplicate-spine bug, when there is intervening structure), and/or
  (2) resets EACH file to H1 and separates them with ``---`` rules, reading as
      two standalone documents rather than one connected subtree.

The architected fix (ap:MSFBP3) adds an explicit file-boundary presentation
POLICY on ``RenderPolicy`` (``file_boundary_mode``):

  * ``standalone_blocks`` (DEFAULT) — today's behavior for ALL ordinary
    GLIMPSE / slice / single-row-focus / DOM paths. MUST stay byte-for-byte
    identical (golden-locked elsewhere).
  * ``connected_subtree`` (NEW) — only ``focus={mode:'rows'}`` covering
    renders. Shared ancestors render ONCE; each file node renders at its
    NATURAL STRUCTURAL level (not H1); file contents rebase UNDER the file
    node; abs-path leader still emitted; NO post-file breadcrumb delimiter;
    NO root-level ``---`` breaks.

The materializer sets ``connected_subtree`` when the canonical focus mode ==
``rows``; every other path keeps ``standalone_blocks``.

THIS FILE (Step A) is written FAILING-RED FIRST: it asserts the DESIRED
connected-subtree behavior before the composer/materializer changes land, to
prove the bug exists and the tests bite. Steps B–E make it green while keeping
the whole existing suite untouched.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus as W,
)
from workflowy_mcp import server as srv  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers (mirror test_phase1_multiselect_materializer.py idioms)
# ---------------------------------------------------------------------------
def _node(nid, name, note="", children=None):
    return {"id": nid, "name": name, "note": note, "children": children or []}


def _r(uuid, ancestor_path, occurrence=0):
    return {"uuid": uuid, "ancestor_path": list(ancestor_path), "occurrence": occurrence}


def _rows(*row_triples):
    return {"mode": "rows", "rows": [dict(r) for r in row_triples]}


def _row(uuid, ancestor_path, occurrence=0):
    return {
        "mode": "row",
        "uuid": uuid,
        "ancestor_path": list(ancestor_path),
        "occurrence": occurrence,
    }


def _mdfile(nid, name, abs_path, children=None):
    """A Markdown Cartographer FILE node (note carries a Path: header)."""
    return _node(nid, name, f"Path: {abs_path}", children or [])


def _mdheading(nid, name, hashes, section="", children=None):
    """A Markdown heading node in the REAL Cartographer format.

    The note's FIRST line is EXACTLY ``MD_PATH:`` (a bare marker); the hash-run
    that declares the heading's md-depth is on the NEXT line. The WHOLE note is
    Cartographer metadata (it strips to empty under ``suppress_metadata=True``),
    so a Markdown heading node has NO separately-rendered note body -- section
    prose lives in CHILD nodes. ``section`` (optional) adds one such child so a
    test can assert the selected heading's full subtree renders.
    """
    note = f"MD_PATH:\n{hashes} {name}"
    kids = list(children or [])
    if section:
        kids = [_node(nid + "_sec", section, "")] + kids
    return _node(nid, name, note, kids)


def _count_file_marker_headings(md, filename):
    """Count the file-MARKER HEADING lines for ``filename``.

    A bare substring count would also match the bold abs-path leader line
    (``**/repo/fileA.md**``) whenever one is emitted, and whether the leader is
    emitted for these fixtures is OS/filesystem-dependent; the invariant under
    test is that the file NODE renders once, so count heading lines only.
    """
    return sum(1 for ln in md.splitlines() if ln.startswith("#") and filename in ln)


def _render(mrd):
    """Compose the materialized render_forest exactly as production does."""
    return W._build_markdown_preview_from_tree(
        mrd.render_forest,
        suppress_metadata=True,
        render_policy=mrd.policy_by_occurrence,
    )


def _count_lines_matching(md, needle):
    return sum(1 for ln in md.splitlines() if needle in ln)


# ---------------------------------------------------------------------------
# The ap:MSFBP2 topology — two files under a shared ancestor, WITH intervening
# plain structural nodes ('Proj X' / 'Proj Y') between the shared ancestor and
# each file. This is the exact shape that triggers BOTH the literal duplicate
# spine AND the H1-reset flattening in standalone mode.
#
#   Root
#    └─ CONVERSATIONAL R&D        (shared ancestor)
#        ├─ Proj X                (plain intervening structure)
#        │   └─ File A  (fileA.md)
#        │       └─ Heading A1  (## selected)
#        └─ Proj Y                (plain intervening structure)
#            └─ File B  (fileB.md)
#                └─ Heading B1  (## selected)
# ---------------------------------------------------------------------------
def _crossfile_forest():
    return [
        _node("root", "Workflowy Root", "", [
            _node("cr", "CONVERSATIONAL R&D", "", [
                _node("projx", "Proj X", "", [
                    _mdfile("fileA", "fileA.md", "/repo/fileA.md", [
                        _mdheading("a1", "Heading A1", "##", section="Section A1 content"),
                    ]),
                ]),
                _node("projy", "Proj Y", "", [
                    _mdfile("fileB", "fileB.md", "/repo/fileB.md", [
                        _mdheading("b1", "Heading B1", "##", section="Section B1 content"),
                    ]),
                ]),
            ]),
        ]),
    ]


def _crossfile_rows_focus():
    return _rows(
        _r("a1", ["root", "cr", "projx", "fileA"], 0),
        _r("b1", ["root", "cr", "projy", "fileB"], 0),
    )


# ===========================================================================
# A1) SHARED SPINE RENDERS EXACTLY ONCE (kills the literal duplicate)
# ===========================================================================
def test_crossfile_shared_spine_rendered_once():
    mrd = W.materialize_from_document_forest(
        _crossfile_forest(), focus=_crossfile_rows_focus()
    )
    md = _render(mrd)
    # Each shared-spine name appears EXACTLY ONCE.
    assert _count_lines_matching(md, "Workflowy Root") == 1, md
    assert _count_lines_matching(md, "CONVERSATIONAL R&D") == 1, md
    # Each fork appears exactly once.
    assert _count_lines_matching(md, "Proj X") == 1, md
    assert _count_lines_matching(md, "Proj Y") == 1, md


# ===========================================================================
# A2) FILES NEST AS DESCENDANTS (not standalone root-level H1 blocks)
# ===========================================================================
def test_crossfile_files_nest_below_shared_ancestor():
    mrd = W.materialize_from_document_forest(
        _crossfile_forest(), focus=_crossfile_rows_focus()
    )
    md = _render(mrd)
    lines = [ln for ln in md.splitlines() if ln.startswith("#")]
    # 'Workflowy Root' is the shallowest heading; it is H1. Everything below
    # (CONVERSATIONAL R&D, Proj X/Y, the files, the file headings) must be at
    # a DEEPER level than root -- i.e. root is the ONLY H1 line.
    h1_lines = [ln for ln in lines if ln.startswith("# ") and not ln.startswith("## ")]
    assert len(h1_lines) == 1, f"expected exactly one H1 (root); got {h1_lines}\n{md}"
    assert "Workflowy Root" in h1_lines[0], md
    # The file marker lines are present but NOT at H1.
    file_a_lines = [ln for ln in lines if "fileA.md" in ln]
    file_b_lines = [ln for ln in lines if "fileB.md" in ln]
    assert file_a_lines, md
    assert file_b_lines, md
    for ln in file_a_lines + file_b_lines:
        assert not (ln.startswith("# ") and not ln.startswith("## ")), (
            f"file node should NOT render at H1 in connected mode: {ln}\n{md}"
        )


# ===========================================================================
# A3) FILE MARKERS PRESENT (both files rendered). The bold ABS-PATH LEADER is
#     resolved by resolve_cartographer_path_from_node, which needs the real
#     Root:/Path: ancestry chain; that is exercised end-to-end via the cache
#     path (Step E / the real glimpse path), not this hand-built forest. Here
#     we simply confirm BOTH file nodes render (once each) in the covering
#     render -- the leader-emission code is regime-independent (identical in
#     both standalone + connected), so this build never changes it.
# ===========================================================================
def test_crossfile_both_file_markers_present():
    mrd = W.materialize_from_document_forest(
        _crossfile_forest(), focus=_crossfile_rows_focus()
    )
    md = _render(mrd)
    assert _count_file_marker_headings(md, "fileA.md") == 1, md
    assert _count_file_marker_headings(md, "fileB.md") == 1, md


# ===========================================================================
# A4) NO '---' SEPARATORS IN CONNECTED MODE (the hierarchy IS the separator)
# ===========================================================================
def test_crossfile_no_horizontal_rules_in_connected_mode():
    mrd = W.materialize_from_document_forest(
        _crossfile_forest(), focus=_crossfile_rows_focus()
    )
    md = _render(mrd)
    hr_lines = [ln for ln in md.splitlines() if ln.strip() == "---"]
    assert hr_lines == [], f"connected mode must emit no '---' rules; got {len(hr_lines)}\n{md}"


# ===========================================================================
# A5) SELECTED CONTENT PRESENT; OFF-PATH SIBLINGS PRUNED
# ===========================================================================
def test_crossfile_selected_headings_and_subtrees_present():
    mrd = W.materialize_from_document_forest(
        _crossfile_forest(), focus=_crossfile_rows_focus()
    )
    md = _render(mrd)
    # The selected heading nodes render...
    assert "Heading A1" in md, md
    assert "Heading B1" in md, md
    # ...WITH their full subtrees (the section-content children), proving the
    # selected node keeps its whole subtree in the covering render.
    assert "Section A1 content" in md, md
    assert "Section B1 content" in md, md


# ===========================================================================
# A6) REGRESSION ANCHOR — SAME-FILE multi-select stays SEMANTICALLY correct
#     under connected_subtree (shared spine once, both leaves covered, off-path
#     sibling pruned). NOT asserted byte-identical to today (multi-select is
#     hours old; connected_subtree is the intended regime for all mode='rows').
# ===========================================================================
def _samefile_forest():
    #   Root
    #    └─ CONVERSATIONAL R&D
    #        └─ File A (fileA.md)
    #            ├─ Heading A1  (## selected)
    #            ├─ Heading A2  (## off-path -> pruned)
    #            └─ Heading A3  (## selected)
    return [
        _node("root", "Workflowy Root", "", [
            _node("cr", "CONVERSATIONAL R&D", "", [
                _mdfile("fileA", "fileA.md", "/repo/fileA.md", [
                    _mdheading("a1", "Heading A1", "##", section="Section A1 content"),
                    _mdheading("a2", "Heading A2", "##", section="Section A2 content"),
                    _mdheading("a3", "Heading A3", "##", section="Section A3 content"),
                ]),
            ]),
        ]),
    ]


def test_samefile_multiselect_semantics_under_connected():
    mrd = W.materialize_from_document_forest(
        _samefile_forest(),
        focus=_rows(
            _r("a1", ["root", "cr", "fileA"], 0),
            _r("a3", ["root", "cr", "fileA"], 0),
        ),
    )
    md = _render(mrd)
    # Shared spine (root, CR, fileA) each rendered once.
    assert _count_lines_matching(md, "Workflowy Root") == 1, md
    assert _count_lines_matching(md, "CONVERSATIONAL R&D") == 1, md
    assert _count_file_marker_headings(md, "fileA.md") == 1, md
    # Both selected headings present; off-path sibling pruned.
    assert "Heading A1" in md, md
    assert "Heading A3" in md, md
    assert "Heading A2" not in md, md
    # No '---' in connected mode.
    assert [ln for ln in md.splitlines() if ln.strip() == "---"] == [], md


# ===========================================================================
# A7) SINGLE-ROW FOCUS STAYS STANDALONE (untouched). A mode='row' focus on a
#     file's heading keeps today's standalone regime: the file node resets to
#     H1 and the closing '---' terminator is emitted.
# ===========================================================================
def test_single_row_focus_stays_standalone():
    mrd = W.materialize_from_document_forest(
        _samefile_forest(), focus=_row("a1", ["root", "cr", "fileA"], 0)
    )
    md = _render(mrd)
    # Standalone regime: the file node renders at H1 (the '# fileA.md' marker),
    # and a trailing '---' terminator is present.
    file_marker = [ln for ln in md.splitlines() if "fileA.md" in ln and ln.startswith("#")]
    assert file_marker, md
    assert any(ln.startswith("# ") and not ln.startswith("## ") for ln in file_marker), (
        f"single-row (standalone) must render the file node at H1: {file_marker}\n{md}"
    )
    assert any(ln.strip() == "---" for ln in md.splitlines()), (
        f"single-row (standalone) must keep the '---' terminator\n{md}"
    )


# ===========================================================================
# A8) SOURCE-FILE connected variant (ap:MSFBP4 Step E #4 -- the top hazard).
#     Two selected AST/source leaves across TWO SOURCE (.py) files under a
#     shared structural ancestor. In connected mode: (1) each source file node
#     nests at its natural structural level (not H1); (2) its AST descendant
#     nests UNDER the file node (via the enclosing structural anchor, since
#     connected mode pushes no child-depth anchor); (3) source '#'-comment
#     note bodies STILL fence as ```text (never mis-promoted to headings);
#     (4) shared spine once; (5) no '---' breaks.
# ===========================================================================
def _pyfile(nid, name, abs_path, children=None):
    """A SOURCE (.py) Cartographer FILE node (note is a Path: header)."""
    return _node(nid, name, f"Path: {abs_path}", children or [])


def _astnode(nid, name, comment_note, children=None):
    """An AST/structural node whose note is a source '#'-comment (a hazard:
    a bare '#' line the markdown detector could mis-read as a heading)."""
    return _node(nid, name, comment_note, children or [])


def _crossfile_source_forest():
    #   Root
    #    └─ ENGINE                       (shared ancestor)
    #        ├─ pkg_a                     (plain intervening structure)
    #        │   └─ a.py  (source file)
    #        │       └─ func_a  (# a comment  -> selected)
    #        └─ pkg_b
    #            └─ b.py  (source file)
    #                └─ func_b  (## b comment -> selected)
    return [
        _node("root", "Workflowy Root", "", [
            _node("engine", "ENGINE", "", [
                _node("pkga", "pkg_a", "", [
                    _pyfile("aPy", "a.py", "/repo/a.py", [
                        _astnode("funcA", "func_a", "# a comment\nreturn 1"),
                    ]),
                ]),
                _node("pkgb", "pkg_b", "", [
                    _pyfile("bPy", "b.py", "/repo/b.py", [
                        _astnode("funcB", "func_b", "## b comment\nreturn 2"),
                    ]),
                ]),
            ]),
        ]),
    ]


def test_crossfile_source_files_nest_and_fence():
    mrd = W.materialize_from_document_forest(
        _crossfile_source_forest(),
        focus=_rows(
            _r("funcA", ["root", "engine", "pkga", "aPy"], 0),
            _r("funcB", ["root", "engine", "pkgb", "bPy"], 0),
        ),
    )
    md = _render(mrd)
    # Shared spine once.
    assert _count_lines_matching(md, "Workflowy Root") == 1, md
    assert _count_lines_matching(md, "ENGINE") == 1, md
    # Both source files render, once each.
    assert _count_file_marker_headings(md, "a.py") == 1, md
    assert _count_file_marker_headings(md, "b.py") == 1, md
    # File nodes NOT at H1 (only the shared root is H1). Count REAL heading
    # lines only -- a '#'-comment line INSIDE a ```text fence (e.g. the fenced
    # source comment 'return 1'/'# a comment') is content, not a heading, so
    # we must exclude fenced regions before counting H1s.
    h1_lines = []
    in_fence = False
    for ln in md.splitlines():
        if ln.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if ln.startswith("# ") and not ln.startswith("## "):
            h1_lines.append(ln)
    assert len(h1_lines) == 1 and "Workflowy Root" in h1_lines[0], md
    # The AST function nodes render (nested under their files).
    assert "func_a" in md, md
    assert "func_b" in md, md
    # CRITICAL: source '#'-comment note bodies are FENCED, never mis-promoted
    # to Markdown headings. The connected-mode file branch must preserve the
    # child_in_non_md_file threading exactly as standalone does.
    assert "```text\n# a comment" in md, md
    assert "```text\n## b comment" in md, md
    # No '---' breaks in connected mode.
    assert [ln for ln in md.splitlines() if ln.strip() == "---"] == [], md


# ===========================================================================
# A9) REBASE-BASELINE hazard (ap:MSFBP5 hazard-b). A Markdown file whose
#     SHALLOWEST heading starts DEEP (H3), selected in a cross-file connected
#     render, must preserve its INTERNAL relative heading structure beneath
#     the (non-H1) file node -- the file's shallowest heading rebases to
#     file_node_level + 1, and a deeper sibling heading keeps its relative
#     offset.
# ===========================================================================
def _deep_heading_forest():
    #   Root
    #    └─ DOCS                          (shared ancestor)
    #        ├─ Proj X
    #        │   └─ deep.md (file)
    #        │       └─ H3 heading  (### shallowest in file)
    #        │           └─ H5 heading (##### deeper -> relative +2)  [selected]
    #        └─ Proj Y
    #            └─ other.md (file)
    #                └─ H2 heading  (## selected)
    return [
        _node("root", "Workflowy Root", "", [
            _node("docs", "DOCS", "", [
                _node("projx", "Proj X", "", [
                    _mdfile("deep", "deep.md", "/repo/deep.md", [
                        _mdheading("h3", "Deep H3", "###", children=[
                            _mdheading("h5", "Deep H5", "#####",
                                       section="H5 section"),
                        ]),
                    ]),
                ]),
                _node("projy", "Proj Y", "", [
                    _mdfile("other", "other.md", "/repo/other.md", [
                        _mdheading("o2", "Other H2", "##", section="O2 section"),
                    ]),
                ]),
            ]),
        ]),
    ]


# ===========================================================================
# A10) END-TO-END through the REAL cache tail (the glimpse / render_spec=
#      'modal' path). _glimpse_root_uuid_from_cache with a mode='rows' focus
#      projects from the cache, materializes (which stamps connected_subtree),
#      and composes -- exactly what an agent render_spec='modal' read of a
#      multi-selected dock does. This ALSO exercises the abs-path leader
#      resolution (which needs the real Root:/Path: ancestry chain the cache
#      supplies), closing the leader coverage the hand-built unit fixtures
#      cannot. Two Markdown files under a shared Root: folder with intervening
#      plain structure; one heading selected in each.
# ===========================================================================
def _client(cache_nodes):
    c = W.__new__(W)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in cache_nodes]
    return c


def _run_root_glimpse(monkeypatch, cache_nodes, root_uuid, *, focus=None):
    client = _client(cache_nodes)
    monkeypatch.setattr(srv, "get_client", lambda: client)
    import asyncio
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


# A cross-file cache: a Root: FOLDER 'repo' with two Markdown FILE nodes under
# intervening plain 'pkg' nodes. Note the real MD_PATH note format (first line
# EXACTLY 'MD_PATH:', hash-run below).
_CROSSFILE_CACHE = [
    {"id": "repo", "name": "repo", "note": "Root: E:\\repo", "parent_id": None, "priority": 0},
    {"id": "pkgx", "name": "Proj X", "note": "", "parent_id": "repo", "priority": 0},
    {"id": "fA", "name": "a.md", "note": "Path: a.md", "parent_id": "pkgx", "priority": 0},
    {"id": "hA", "name": "Heading A", "note": "MD_PATH:\n## Heading A", "parent_id": "fA", "priority": 0},
    {"id": "pkgy", "name": "Proj Y", "note": "", "parent_id": "repo", "priority": 1},
    {"id": "fB", "name": "b.md", "note": "Path: b.md", "parent_id": "pkgy", "priority": 0},
    {"id": "hB", "name": "Heading B", "note": "MD_PATH:\n## Heading B", "parent_id": "fB", "priority": 0},
]


def test_e2e_crossfile_connected_render(monkeypatch):
    res = _run_root_glimpse(
        monkeypatch, _CROSSFILE_CACHE, "repo",
        focus=_rows(
            _r("hA", ["repo", "pkgx", "fA"], 0),
            _r("hB", ["repo", "pkgy", "fB"], 0),
        ),
    )
    assert res["success"] is True
    md = res["preview_markdown"]
    # Connected subtree: shared spine ('repo' Root folder) once, files nested,
    # both selected headings present, NO '---' breaks. Count the file-MARKER
    # HEADING lines specifically ('### a.md') -- 'a.md' also appears in the
    # abs-path leader line ('**E:\repo\a.md**'), so a bare substring count
    # would double.
    assert _count_lines_matching(md, "repo") >= 1, md
    assert sum(1 for ln in md.splitlines()
               if ln.startswith("#") and "a.md" in ln) == 1, md
    assert sum(1 for ln in md.splitlines()
               if ln.startswith("#") and "b.md" in ln) == 1, md
    assert "Heading A" in md, md
    assert "Heading B" in md, md
    assert [ln for ln in md.splitlines() if ln.strip() == "---"] == [], md
    # The abs-path leader IS resolved end-to-end via the Root:/Path: chain.
    # The separator between the resolved root and the file is the platform
    # path separator, so build the expectation with os.path.join.
    assert os.path.join("E:\\repo", "a.md") in md, md
    assert os.path.join("E:\\repo", "b.md") in md, md
    # Files nest below the shared folder (only the folder root is H1; count
    # real heading lines, fence-aware for safety).
    h1_lines = []
    in_fence = False
    for ln in md.splitlines():
        if ln.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and ln.startswith("# ") and not ln.startswith("## "):
            h1_lines.append(ln)
    assert len(h1_lines) == 1, md


def test_crossfile_deep_starting_heading_rebases_relative():
    mrd = W.materialize_from_document_forest(
        _deep_heading_forest(),
        focus=_rows(
            _r("h5", ["root", "docs", "projx", "deep", "h3"], 0),
            _r("o2", ["root", "docs", "projy", "other"], 0),
        ),
    )
    md = _render(mrd)
    lines = [ln for ln in md.splitlines() if ln.startswith("#")]

    def level_of(needle):
        for ln in lines:
            if needle in ln:
                return len(ln) - len(ln.lstrip("#"))
        return None

    # Structural spine: Root H1, DOCS H2, Proj X/Y H3, files H4.
    assert level_of("Workflowy Root") == 1, md
    assert level_of("DOCS") == 2, md
    assert level_of("deep.md") == 4, md
    assert level_of("other.md") == 4, md
    # deep.md is H4 -> its SHALLOWEST heading (the on-disk H3) rebases to H5
    # (file_node_level + 1), and its deeper on-disk H5 keeps the +2 relative
    # offset -> H7 (model), render-clamped to H6.
    assert level_of("Deep H3") == 5, md
    # The relative offset from H3->H5 on disk is +2; H5 render level = 5+2=7
    # model, clamped to 6.
    assert level_of("Deep H5") == 6, md
    # other.md is H4 -> its shallowest on-disk H2 rebases to H5 as well.
    assert level_of("Other H2") == 5, md
    # Shared spine once; no '---'.
    assert _count_lines_matching(md, "DOCS") == 1, md
    assert [ln for ln in md.splitlines() if ln.strip() == "---"] == [], md
