"""U07 typed reads: own witnesses, degraded windows, fronts and read purity."""
import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import beacon_obtain_code_snippet as bocs
from workflowy_mcp import server as srv
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
from workflowy_mcp.beacon_snippet import format_beacon_snippet_result, snippet_metadata_projection
from workflowy_mcp.snippet_view import normalize_snippet_view_params, render_snippet_view


NODE = "11111111-1111-1111-1111-111111111111"
FILE = "22222222-2222-2222-2222-222222222222"


def _block(bid, kind="ast"):
    return ["# @beacon[", f"# id={bid},", f"# kind={kind},", "# slice_labels=read-test,",
            "# comment=Useful source,", "# ]"]


def _write(tmp_path, name, lines):
    path = tmp_path / name
    path.write_bytes(("\n".join(lines) + "\n").encode())
    return path


def _pure(path, operation):
    before = path.read_bytes(), path.stat().st_mtime_ns
    try:
        return operation()
    finally:
        assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def _clean(tmp_path):
    return _write(tmp_path, "safe.py", [*_block("safe@0", "span"), "value = 1",
                                        "# @beacon-close[", "# id=safe@0,", "# ]"])


def _read_dict(path, bid):
    result = bocs.get_snippet_data(str(path), bid, 0)
    return {"file_path": str(path), "file_lines": result[2], "core_start_line": result[3],
            "core_end_line": result[4], "beacon_line": result[5],
            "resolution_strategy": result[6]["resolution_strategy"], **snippet_metadata_projection(result[6])}


def test_duplicate_id_uses_current_anchor_not_a_stale_line(tmp_path):
    path = _write(tmp_path, "duplicates.py", [*_block("dup@0"), "def first():", "    return 1", "",
                                             *_block("dup@0"), "def second():", "    return 2", "",
                                             *_block("safe@0"), "def safe():", "    return 3"])
    with pytest.raises(bocs.BeaconSnippetError) as error:
        _pure(path, lambda: bocs.get_snippet_data(str(path), "dup@0", 0, target={"line": 17}))
    assert error.value.source_extraction["outcome"] == "ambiguous"
    assert {candidate["anchor_symbol"] for candidate in error.value.candidates} == {"first", "second"}
    assert len(error.value.candidates) == 2
    read = _pure(path, lambda: bocs.get_snippet_data(str(path), "dup@0", 0,
                                                   target={"ast_qualname": "second", "line": 1}))
    text = "\n".join(read[2][read[3] - 1:read[4]])
    assert "return 2" in text and "return 1" not in text
    assert "duplicate_id" in {finding["code"] for finding in read[6]["findings"]}
    assert read[6]["observed"]["file_sha256"] == bocs.read_source_snapshot(path).sha256
    safe = _pure(path, lambda: bocs.get_snippet_data(str(path), "safe@0", 0))
    assert "return 3" in "\n".join(safe[2][safe[3] - 1:safe[4]])


def test_parse_and_analysis_failures_keep_bounded_windows_and_safe_neighbour(tmp_path, monkeypatch):
    bad = _write(tmp_path, "broken.py", [*_block("broken@0"), "def broken():", "    return 1", "if ("])
    read = _pure(bad, lambda: _read_dict(bad, "broken@0"))
    assert read["resolution_strategy"] == "degraded"
    assert read["core_start_line"] <= 1 and read["core_end_line"] >= 6
    assert "mapper_parse_error" in {finding["code"] for finding in read["findings"]}
    assert len([row for row in read["findings"] if row["code"] == "mapper_parse_error"]) == 1
    node, nodes = _nodes(bad, "AST_QUALNAME: broken\nBEACON (AST)\nid: broken@0\nkind: ast")
    mapped = _pure(bad, lambda: asyncio.run(_L4().get_code_snippet_for_resolved_node(NODE, node, nodes, 0)))
    assert mapped["resolution_strategy"] == "degraded" and mapped["anchor_degradation"]
    assert mapped["location"]["anchor"] is None
    assert "mapper_parse_error" in {row["code"] for row in mapped["findings"]}
    with pytest.raises(bocs.BeaconSnippetError) as error:
        _pure(bad, lambda: bocs.get_snippet_for_ast_qualname(str(bad), "broken", 0))
    assert error.value.source_extraction["outcome"] == "outline_parse_failed"
    safe = _clean(tmp_path)
    assert _pure(safe, lambda: _read_dict(safe, "safe@0"))["resolution_strategy"] == "beacon"
    original = bocs._snippet_analysis_rows

    def fail_analysis(snapshot, *args, **kwargs):
        if snapshot.canonical_path == str(bad):
            raise RuntimeError("analysis injected failure")
        return original(snapshot, *args, **kwargs)

    monkeypatch.setattr(bocs, "_snippet_analysis_rows", fail_analysis)
    read = _pure(bad, lambda: _read_dict(bad, "broken@0"))
    assert read["analysis_error"]["message"] == "analysis injected failure"
    assert read["core_end_line"] >= 6
    assert _pure(safe, lambda: _read_dict(safe, "safe@0"))["findings"] == []


def test_decode_missing_and_vanished_id_are_typed_with_safe_neighbour(tmp_path):
    bad = tmp_path / "unreadable.py"
    bad.write_bytes(b"\xff\n")
    with pytest.raises(bocs.BeaconSnippetError) as error:
        _pure(bad, lambda: bocs.get_snippet_data(str(bad), "bad@0", 0))
    assert error.value.source_extraction["outcome"] == "file_unreadable"
    assert error.value.source_extraction["observed"]["unreadable_identity"]["undecodable_bytes_sha256"]
    missing = tmp_path / "absent.py"
    with pytest.raises(bocs.BeaconSnippetError) as error:
        bocs.get_snippet_data(str(missing), "gone@0", 0)
    assert error.value.source_extraction["file_path"] == str(missing)
    safe = _clean(tmp_path)
    with pytest.raises(bocs.BeaconSnippetError) as error:
        _pure(safe, lambda: bocs.get_snippet_data(str(safe), "gone@0", 0))
    assert error.value.source_extraction["beacon_id"] == "gone@0"
    assert _pure(safe, lambda: _read_dict(safe, "safe@0"))["findings"] == []


def test_near_close_findings_and_closed_geometry_in_both_formats(tmp_path):
    path = _write(tmp_path, "near.py", [*_block("near@0", "span"), "value = 1",
                                       "# @beacon-close[", "# id=near@00,", "# ]"])
    read = _pure(path, lambda: _read_dict(path, "near@0"))
    assert {"span_without_close", "orphan_close"} <= {row["code"] for row in read["findings"]}
    view = normalize_snippet_view_params({"max_lines": 3})
    text = format_beacon_snippet_result(read, view, renderer=render_snippet_view)
    assert "BEACON_FINDINGS_JSON" not in text and "⚠ Snippet degraded:" in text
    assert "Full findings and fix resolutions:" in text
    for finding in read["findings"]:
        if finding["severity"] != "info":
            assert f"finding_id {finding['finding_id']}" in text
    structured = format_beacon_snippet_result(read, view, renderer=render_snippet_view, output_format="json")
    assert structured["text"] == render_snippet_view(read, view)
    assert structured["findings"] == read["findings"]
    assert structured["location"]["span"]["pair_state"] == "open_only"
    assert structured["text"] != render_snippet_view(read, normalize_snippet_view_params({"max_lines": 0}))
    safe = _clean(tmp_path)
    clean = _pure(safe, lambda: _read_dict(safe, "safe@0"))
    assert format_beacon_snippet_result(clean, view, renderer=render_snippet_view) == render_snippet_view(clean, view)
    structured = format_beacon_snippet_result(clean, view, renderer=render_snippet_view, output_format="json")
    assert structured["location"]["span"]["close"]["pair_state"] == "matched"
    assert structured["location"]["span"]["close"]["line"] > clean["core_end_line"]


class _L4:
    get_code_snippet_for_resolved_node = WorkFlowyClientNexus.get_code_snippet_for_resolved_node


def _nodes(path, note):
    file = {"id": FILE, "name": path.name, "note": f"Path: {path}"}
    node = {"id": NODE, "name": "mapped", "note": note, "parent_id": FILE}
    return node, {FILE: file, NODE: node}


def test_client_span_text_cannot_shadow_header_id_or_kind(tmp_path):
    path = _write(tmp_path, "config.yaml", ["# @beacon[", "# id=cfg@0,", "# kind=span,",
                                             "# slice_labels=read-test,", "# comment=Config,", "# ]",
                                             "user:", "  id: admin", "# @beacon-close[", "# id=cfg@0,", "# ]"])
    node, nodes = _nodes(path, "BEACON (SPAN)\nid: cfg@0\nkind: span\n\nSPAN TEXT:\nid: admin\nkind: ast\nAST_QUALNAME: fake\nMD_PATH:\n# Fake\n---")
    read = _pure(path, lambda: asyncio.run(_L4().get_code_snippet_for_resolved_node(NODE, node, nodes, 0)))
    assert read["beacon_id"] == "cfg@0" and read["kind"] == "span"
    assert "id: admin" in read["snippet"]
    assert "location" in read and "observed" in read


def test_missing_header_id_does_not_veto_a_unique_current_ast_attachment(tmp_path):
    path = _write(tmp_path, "idless.py", ["# @beacon[", "# kind=ast,", "# slice_labels=read-test,",
                                         "# comment=Known attachment,", "# ]", "def known():", "    return 1"])
    node, nodes = _nodes(path, "AST_QUALNAME: known\nBEACON (AST)\nkind: ast")
    read = _pure(path, lambda: asyncio.run(_L4().get_code_snippet_for_resolved_node(NODE, node, nodes, 0)))
    assert read["core_start_line"] == 1 and read["core_end_line"] == 7
    assert "missing_id" in {row["code"] for row in read["findings"]}
    assert read["location"]["anchor"]["ast_qualname"] == "known"
    safe = _clean(tmp_path)
    assert _pure(safe, lambda: _read_dict(safe, "safe@0"))["findings"] == []


def test_client_markdown_beacon_keeps_own_block_and_heading_authority(tmp_path):
    path = _write(tmp_path, "mapped.md", ["# Parent", "", "<!--", "@beacon[", "id=md@0,", "kind=ast,",
                                         "slice_labels=read-test,", "comment=Doc,", "]", "-->", "",
                                         "## Child", "own body", "", "## Next", "safe"])
    node, nodes = _nodes(path, "MD_PATH:\n# Parent\n## Child\n---\nBEACON (MD AST)\nid: md@0\nkind: ast")
    read = _pure(path, lambda: asyncio.run(_L4().get_code_snippet_for_resolved_node(NODE, node, nodes, 0)))
    assert (read["core_start_line"], read["core_end_line"]) == (3, 14)
    assert "@beacon[" in read["snippet"] and "own body" in read["snippet"]
    node["note"] = "MD_PATH:\n# Parent\n## Next\n---"
    neighbour = _pure(path, lambda: asyncio.run(_L4().get_code_snippet_for_resolved_node(NODE, node, nodes, 0)))
    assert (neighbour["core_start_line"], neighbour["core_end_line"]) == (15, 16)


def test_client_duplicate_ast_twins_use_u03_signature_witness(tmp_path):
    path = _write(tmp_path, "twins.py", [*_block("dup@0"), "def same(first):", "    return 1", "",
                                        *_block("dup@0"), "def same(second):", "    return 2", "",
                                        *_block("safe@0"), "def safe():", "    return 3"])
    outline = bocs.nexus_map_codebase.parse_file_outline(str(path), snapshot=bocs.read_source_snapshot(path))
    twins = [node for node in bocs._iter_outline_nodes(outline) if node.get("ast_qualname") == "same"]
    assert len(twins) == 2
    nodes = {FILE: {"id": FILE, "name": path.name, "note": f"Path: {path}"}}
    for i, node in enumerate(twins):
        handle = NODE if i == 1 else "33333333-3333-3333-3333-333333333333"
        nodes[handle] = {**node, "id": handle, "parent_id": FILE, "priority": i,
                         "orig_lineno_start_unused": 999}
    read = _pure(path, lambda: asyncio.run(_L4().get_code_snippet_for_resolved_node(NODE, nodes[NODE], nodes, 0)))
    assert "return 2" in read["snippet"] and "return 1" not in read["snippet"]
    assert read["location"]["anchor"]["outline_line"] != 999
    assert "duplicate_id" in {row["code"] for row in read["findings"]}
    safe = _pure(path, lambda: _read_dict(path, "safe@0"))
    assert "return 3" in "\n".join(safe["file_lines"][safe["core_start_line"] - 1:safe["core_end_line"]])


def test_vue_ast_prefix_ids_read_their_own_block_and_symbol(tmp_path):
    lines = ["<script>"]
    for bid, name, value in (("b2@00", "alpha", 1), ("b2@0", "beta", 2)):
        lines += [line.replace("#", "//", 1) for line in _block(bid)]
        lines += [f"function {name}() {{ return {value}; }}", ""]
    lines += ["</script>"]
    path = _write(tmp_path, "prefix.vue", lines)
    for bid, own, foreign in (("b2@0", "beta", "alpha"), ("b2@00", "alpha", "beta")):
        result = _pure(path, lambda: bocs.get_snippet_data(str(path), bid, 0))
        text = "\n".join(result[2][result[3] - 1:result[4]])
        assert f"function {own}" in text and f"function {foreign}" not in text
        assert result[6]["location"]["beacon_id"] == bid


def test_file_top_uses_one_physical_snapshot_even_after_a_search_miss(tmp_path, monkeypatch):
    path = _write(tmp_path, "plain.txt", ["first\fnot a second line", "last"])
    node, nodes = _nodes(path, "")
    node["name"] = "missing needle"
    calls = []
    original = bocs.read_source_snapshot

    def observe(path):
        calls.append(str(path))
        return original(path)

    monkeypatch.setattr(bocs, "read_source_snapshot", observe)
    read = _pure(path, lambda: asyncio.run(_L4().get_code_snippet_for_resolved_node(NODE, node, nodes, 0)))
    assert calls == [str(path)]
    assert read["file_lines"] == ["first\fnot a second line", "last"]
    assert read["core_end_line"] == 2 and read["resolution_strategy"] == "file_top"


def test_resolved_uuid_extraction_failure_never_falls_back_or_reads_suppression(tmp_path, monkeypatch):
    path = _clean(tmp_path)
    node, nodes = _nodes(path, "BEACON (SPAN)\nid: vanished@0\nkind: span")

    class Client(_L4):
        async def beacon_get_code_snippet(self, beacon_node_id, context):
            return await self.get_code_snippet_for_resolved_node(beacon_node_id, node, nodes, context)

        async def read_text_snippet_by_symbol(self, **kwargs):
            raise AssertionError("resolved UUID was retried as a symbol")

    async def no_suppression():
        raise AssertionError("resolved-node failure read suppression")

    monkeypatch.setattr(srv, "get_client", lambda: Client())
    monkeypatch.setattr(srv, "_rate_limiter", None)
    monkeypatch.setattr(srv, "_beacon_symbol_suppress_tags", no_suppression)
    monkeypatch.setattr(srv, "_resolve_beacon_scope_roots", lambda value: None)
    with pytest.raises(srv._BeaconOperationError) as error:
        _pure(path, lambda: asyncio.run(srv._beacon_snippet_impl(NODE)))
    assert error.value.code == "source_extraction_failed"
    assert error.value.extra["resolved_node_id"] == NODE
    assert error.value.extra["file_path"] == str(path)
    assert error.value.extra["beacon_id"] == "vanished@0"
    assert "Could not resolve" not in str(error.value)
    safe_node, safe_nodes = _nodes(path, "BEACON (SPAN)\nid: safe@0\nkind: span")
    assert _pure(path, lambda: asyncio.run(_L4().get_code_snippet_for_resolved_node(NODE, safe_node, safe_nodes, 0)))["success"]
