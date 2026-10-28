"""End-to-end lock for the Layer-4 snippet EXTRACTION method.

``WorkFlowyClientNexus.get_code_snippet_for_resolved_node`` is the back half
split out of ``beacon_get_code_snippet`` in Commit 2: given an ALREADY-RESOLVED
node (+ the nodes_by_id map used to resolve it), it walks ancestors to the
enclosing FILE node, resolves the on-disk path, and delegates to
``beacon_obtain_code_snippet`` for language-specific extraction. It performs NO
handle resolution.

``beacon_get_code_snippet`` was never characterized before this commit (no test
constructed a client + cache + mapped file), so the resolution-layer test suite
proves the split didn't disturb RESOLUTION but not EXTRACTION. This test closes
that gap with a REAL mapped file: this very repo module. It builds a faithful
minimal cache map -- a FILE node (note = ``Path: <abs>``) + an AST node (note =
``AST_QUALNAME: <fn>``, parent = the FILE node) -- and asserts L4 extracts the
target function's real source lines. That exercises the whole L4 path: ancestor
walk -> FILE Path: resolve -> the ``.py`` AST_QUALNAME branch -> the
beacon_obtain_code_snippet helper -> the returned snippet dict.

Run with plain asyncio.run (no pytest-asyncio dependency).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # repo root for beacon_obtain_code_snippet

from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402


# A real, stable, module-level function in a mapped repo file we can target by
# AST_QUALNAME. (It is the Layer-1 primitive added earlier this session.)
TARGET_FN = "resolve_uuid_like_node_handle"
MAPPED_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..",
                 "src", "workflowy_mcp", "client", "api_client_nexus.py")
)

FILE_UUID = "11111111-1111-1111-1111-111111111111"
AST_UUID = "22222222-2222-2222-2222-222222222222"


class _L4Client:
    """Minimal client exposing only what L4 touches: the bound L4 method.

    L4 walks nodes_by_id by parent_id and reads notes; it does not call the
    cache accessor or the API, so no other machinery is needed.
    """

    resolve_uuid_like_node_handle = None  # unused here
    get_code_snippet_for_resolved_node = WorkFlowyClientNexus.get_code_snippet_for_resolved_node


def _cache_map():
    file_node = {"id": FILE_UUID, "name": "api_client_nexus.py",
                 "note": f"Path: {MAPPED_FILE}"}
    ast_node = {"id": AST_UUID, "name": TARGET_FN,
                "note": f"AST_QUALNAME: {TARGET_FN}", "parent_id": FILE_UUID}
    return {FILE_UUID: file_node, AST_UUID: ast_node}


def test_l4_extracts_python_ast_qualname_snippet():
    client = _L4Client()
    nodes = _cache_map()
    ast_node = nodes[AST_UUID]
    result = asyncio.run(
        client.get_code_snippet_for_resolved_node(AST_UUID, ast_node, nodes, 3)
    )
    assert isinstance(result, dict)
    assert result["success"] is True
    assert result["file_path"] == MAPPED_FILE
    # the resolved snippet must actually contain the target function's def line
    assert f"def {TARGET_FN}(" in result["snippet"]
    # provenance: the original handle is echoed back
    assert result["beacon_node_id"] == AST_UUID
    assert result["start_line"] >= 1 and result["end_line"] >= result["start_line"]
    # U01 §9.1 item 23: the builder carries the WHOLE file it read, and the
    # snippet view numbers the def line with its TRUE file line number.
    with open(MAPPED_FILE, "r", encoding="utf-8") as handle:
        disk_lines = handle.read().splitlines()
    assert result["file_lines"] == disk_lines
    from workflowy_mcp.snippet_view import (
        normalize_snippet_view_params,
        render_snippet_view,
    )

    view = render_snippet_view(result, normalize_snippet_view_params({}))
    def_line_number = result["core_start_line"]
    assert disk_lines[def_line_number - 1].lstrip().startswith("def ")
    assert f"{def_line_number} | " in view


def test_l4_file_top_text_node_spans_the_whole_file(tmp_path):
    """U01 §9.1 item 24 — a text-like file node with no metadata reads file_top:
    core_end_line is len(lines) while start_line/end_line/snippet equal today's
    values for the same context (the anchored-search values when a needle
    matches, file_top's own window when it does not)."""
    file_path = tmp_path / "note.txt"
    file_path.write_text("\n".join(f"L{i}" for i in range(1, 51)) + "\n", encoding="utf-8")
    client = _L4Client()
    file_node = {"id": FILE_UUID, "name": "note.txt", "note": f"Path: {file_path}"}
    # A name whose search token matches NOTHING in the file -> file_top branch.
    text_node = {"id": AST_UUID, "name": "missing needle xyz", "parent_id": FILE_UUID}
    nodes = {FILE_UUID: file_node, AST_UUID: text_node}
    result = asyncio.run(
        client.get_code_snippet_for_resolved_node(AST_UUID, text_node, nodes, 2)
    )
    assert result["success"] is True
    assert result["resolution_strategy"] == "file_top"
    assert result["core_end_line"] == len(result["file_lines"]) == 50
    assert result["start_line"] == 1 and result["end_line"] == 4
    assert result["snippet"] == "\n".join(result["file_lines"][0:4])

    # The anchored-search branch keeps its discovered span; only core_end_line
    # widens to the whole file.
    anchored = {"id": AST_UUID, "name": "L25", "parent_id": FILE_UUID}
    result2 = asyncio.run(
        client.get_code_snippet_for_resolved_node(AST_UUID, anchored, nodes, 2)
    )
    assert result2["resolution_strategy"] == "file_anchor_search"
    # The anchored branch keeps its discovered core span unchanged.
    assert result2["core_end_line"] == 27
    assert result2["start_line"] == 23 and result2["end_line"] == 27
    assert result2["core_start_line"] == 23
    assert result2["snippet"] == "\n".join(
        result2["file_lines"][22:27]
    )


def test_l4_missing_file_ancestor_raises():
    # An AST node with NO enclosing FILE (Path:) ancestor must raise, exactly as
    # the pre-split code did (L4 owns the ancestor-walk failure).
    from workflowy_mcp.models.errors import NetworkError
    orphan = {"id": AST_UUID, "name": TARGET_FN,
              "note": f"AST_QUALNAME: {TARGET_FN}"}  # no parent_id / no FILE node
    client = _L4Client()
    nodes = {AST_UUID: orphan}
    raised = False
    try:
        asyncio.run(client.get_code_snippet_for_resolved_node(AST_UUID, orphan, nodes, 3))
    except NetworkError:
        raised = True
    assert raised, "expected NetworkError when no FILE ancestor is present"


def test_l4_file_node_that_names_itself_spans_the_whole_file(tmp_path):
    """[ap:803VZI] — a FILE node read as a file spans the WHOLE file even when
    the file names its own basename. Pre-fix, the FILE node's derived search
    token IS the basename, the anchored search found it (a module docstring on
    line 1 is typical), and with beacon.snippet's context=0 the span collapsed
    to that one line of an N-line file (observed live: 'lines 1-1 of 3545')."""
    file_path = tmp_path / "self_naming.py"
    file_path.write_text(
        '"""self_naming.py — names itself on line 1."""\n'
        + "\n".join(f"L{i}" for i in range(2, 51)) + "\n",
        encoding="utf-8",
    )
    client = _L4Client()
    file_node = {"id": FILE_UUID, "name": "self_naming.py",
                 "note": f"Path: {file_path}"}
    nodes = {FILE_UUID: file_node}
    result = asyncio.run(
        client.get_code_snippet_for_resolved_node(FILE_UUID, file_node, nodes, 0)
    )
    assert result["success"] is True
    assert result["resolution_strategy"] == "file_top"
    assert result["core_start_line"] == 1
    assert result["core_end_line"] == len(result["file_lines"]) == 50
    # The consolidated beacon.snippet view (context=0, default options) shows
    # the whole file under the guard, not the one self-naming line.
    from workflowy_mcp.snippet_view import normalize_snippet_view_params, render_snippet_view

    view = render_snippet_view(result, normalize_snippet_view_params({}))
    assert "lines 1-50 of 50" in view
    assert '"""self_naming.py' in view


def test_l4_file_node_whose_token_matches_basename_without_self_reference(tmp_path):
    """[ap:803VZI] second leg — even without a self-reference in the file, a
    resolved node whose derived token EQUALS the file's basename takes the
    whole-file file_top path (the anchored search would otherwise anchor on an
    arbitrary later occurrence, e.g. a mid-file comment naming the module)."""
    file_path = tmp_path / "late_mention.py"
    file_path.write_text(
        "\n".join(f"L{i}" for i in range(1, 30))
        + "\n# see late_mention.py for details\n"
        + "\n".join(f"M{i}" for i in range(31, 41)) + "\n",
        encoding="utf-8",
    )
    client = _L4Client()
    # A child note that happens to be named exactly like the file: token ==
    # basename -> whole-file path per the contract's OR condition.
    child = {"id": AST_UUID, "name": "late_mention.py", "parent_id": FILE_UUID}
    file_node = {"id": FILE_UUID, "name": "late_mention.py",
                 "note": f"Path: {file_path}"}
    nodes = {FILE_UUID: file_node, AST_UUID: child}
    result = asyncio.run(
        client.get_code_snippet_for_resolved_node(AST_UUID, child, nodes, 0)
    )
    assert result["success"] is True
    assert result["resolution_strategy"] == "file_top"
    assert result["core_start_line"] == 1
    assert result["core_end_line"] == len(result["file_lines"]) == 40


def test_l4_non_file_node_with_name_token_still_anchors(tmp_path):
    """[ap:803VZI] regression — a non-FILE node whose name token is an ordinary
    needle (not the basename) keeps the anchored file_anchor_search span, and a
    FILE node read is the only whole-file widening on the name side."""
    file_path = tmp_path / "note.txt"
    file_path.write_text("\n".join(f"L{i}" for i in range(1, 51)) + "\n", encoding="utf-8")
    client = _L4Client()
    file_node = {"id": FILE_UUID, "name": "note.txt", "note": f"Path: {file_path}"}
    anchored = {"id": AST_UUID, "name": "L25", "parent_id": FILE_UUID}
    nodes = {FILE_UUID: file_node, AST_UUID: anchored}
    result = asyncio.run(
        client.get_code_snippet_for_resolved_node(AST_UUID, anchored, nodes, 2)
    )
    assert result["success"] is True
    assert result["resolution_strategy"] == "file_anchor_search"
    # The anchored span keeps its discovered window (anchor 25, context 2).
    assert result["core_start_line"] == 23
    assert result["core_end_line"] == 27
