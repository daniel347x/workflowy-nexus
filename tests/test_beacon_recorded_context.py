"""U01 §6.9: source-record context projections, not another delimiter parser.

Quoted physical rows only. Before the fix one-line openers/close fields leak,
damaged metadata consumes real context, and literal markers are reinterpreted.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

import beacon_source as source
import nexus_map_codebase as nmc


PREFIX = {"python": "#", "js_ts": "//", "sql": "--", "sh": "#", "yaml": "#"}


def inline(language, bid="own", close=False):
    marker = "@beacon-close[" if close else "@beacon["
    fields = f'id="{bid}",' + ("" if close else " kind=span, slice_labels=context,")
    text = marker + " " + fields + " ]"
    return "<!-- " + text + " -->" if language == "markdown" else PREFIX[language] + " " + text


def context(language, rows, line=1):
    if language in {"sql", "sh", "yaml"}:
        return getattr(nmc, f"_extract_{language}_beacon_context")(rows, line, "own")
    if language == "markdown":
        return nmc._extract_markdown_beacon_context(rows, line, enabled=True)
    nodes = []
    if language == "python":
        nmc.apply_python_beacons("fixture.py", rows, nodes)
    else:
        nmc.apply_js_beacons("fixture.js", rows, nodes, nmc.parse_js_beacon_blocks(rows))
    node = next(n for n in nodes if "id: own\n" in n.get("note", ""))
    note = node["note"]
    label = "CONTEXT COMMENTS (PYTHON):\n" if language == "python" else "CONTEXT COMMENTS (JS/TS):\n"
    return note.split(label, 1)[1].split("\n\nSPAN TEXT:", 1)[0].splitlines() if label in note else []


@pytest.mark.parametrize("language", [*PREFIX, "markdown"])
def test_one_line_metadata_never_leaks_or_steals_context(language):
    comment = (lambda text: "<!-- " + text + " -->") if language == "markdown" else (lambda text: PREFIX[language] + " " + text)
    rows = [comment("above"), inline(language), comment("below"), "real_code()"]
    assert context(language, rows, 2) == ["above", "below"]


@pytest.mark.parametrize("language", PREFIX)
def test_incomplete_neighbour_preserves_unowned_human_context(language):
    p = PREFIX[language] + " "
    rows = [inline(language), p + "before", p + "@beacon[", p + "id=broken,", p + "kind=span,",
            p + "Real human documentation after the damaged fields.", "real_code()",
            inline(language, "safe"), p + "safe context", "safe_code()"]
    assert context(language, rows) == ["before", "Real human documentation after the damaged fields."]
    assert nmc._beacon_context_view(rows, language)[2][-2] == "safe context"


@pytest.mark.parametrize("language", ["python", "sql", "sh", "yaml"])
def test_context_stop_uses_recorded_exact_pair_not_private_id_parser(language):
    p = PREFIX[language] + " "
    rows = [inline(language), p + "before near miss", inline(language, "own-x", True),
            p + "after near miss", inline(language, "own", True), p + "outside"]
    assert context(language, rows) == ["before near miss", "after near miss"]


@pytest.mark.parametrize("language,opening,closing", [("js_ts", "/*", "*/"), ("markdown", "<!--", "-->")])
def test_mixed_wrapper_context_owns_only_recorded_metadata(language, opening, closing):
    rows = [opening, "ordinary before", '@beacon[ id=own, kind=span, slice_labels=context, ]', "ordinary after", closing, "code()"]
    assert context(language, rows, 3) == ["ordinary before", "ordinary after"]
    snap = source._source_line_view(rows)
    records = nmc._scan_source_beacons(snap, language)
    before = deepcopy(records)
    assert source.beacon_context_lines(snap, records) == [opening, "ordinary before", " " * len(rows[2]), "ordinary after", closing, "code()"]
    assert records == before and snap.raw_bytes == ("\n".join(rows) + "\n").encode()


@pytest.mark.parametrize("rows,expected", [
    (["@beacon[", "id=x,", "kind=span,", "Real following prose", "tail"], ["Real following prose", "tail"]),
    (["@beacon[ id=x, kind=span, ]", "Real following prose"], ["Real following prose"]),
    (["# @beacon[", "# id=x,", "# kind=span,", "# human context", "code()"], ["# human context", "code()"]),
    (["prose mentions @beacon[", "@beacon-close[ id=x, ]", "after"], ["prose mentions @beacon[", "after"]),
    (["@beacon[", "id=x,", "kind=span,", "@beacon[ id=safe, kind=span, ]", "after"], ["after"]),
    (["<!-- @beacon[ id=x, kind=span, ] -->", "after"], ["after"]),
])
def test_legacy_context_adapter_delegates_bounded_geometry(rows, expected):
    assert nmc._strip_beacon_meta_blocks_from_context(rows) == expected


def test_structured_context_never_rescans_supplied_records(monkeypatch):
    rows = [inline("python"), "# human", "code()"]
    snap = source._source_line_view(rows)
    records = nmc._scan_source_beacons(snap, "python")
    monkeypatch.setattr(nmc, "_scan_source_beacons", lambda *_a, **_kw: pytest.fail("unexpected structural rescan"))
    assert nmc._beacon_context_view(rows, "python", records)[2] == ["", "human", ""]


def test_ast_context_keeps_literal_examples_and_after_damaged_metadata(tmp_path):
    rows = ["# @beacon[", "# id=broken,", "# kind=ast,", "# Real documentation.",
            "def safe():", "    pass"]
    snapshot = source.source_snapshot_from_bytes(("\n".join(rows) + "\n").encode(), str(tmp_path / "example.py"))
    nodes, _ = nmc.parse_file_outline_core(snapshot.canonical_path, snapshot=snapshot)
    assert "CONTEXT COMMENTS (PYTHON):\nReal documentation." in nodes[0]["note"]
    assert "# id=broken" not in nodes[0]["note"]
    rows = ["// @beacon[", "// id=broken,", "// kind=ast,", "// Real documentation.", "function safe() {}"]
    mask, cleaned = nmc._build_js_comment_mask(rows)
    assert nmc._extract_js_ast_comment_context(5, rows, len(rows), mask, cleaned) == ["Real documentation."]


def test_markdown_promotion_uses_own_one_line_record_not_later_bracket():
    rows = ["<!-- @beacon[ id=own, slice_labels=context, ] -->", "# Heading", "body", "<!-- ] -->"]
    beacons = nmc.parse_markdown_beacon_blocks(rows)
    nodes = [{"name": "Heading", "note": "", "children": [], "_md_heading_line": 2}]
    nmc.apply_markdown_beacons(rows, nodes, beacons)
    assert beacons[0]["kind"] == "ast"
    assert "BEACON (MD AST)" in nodes[0]["note"]


@pytest.mark.parametrize("later", ["flow --> target", "ordinary prose --> tail", "<!-- ordinary comment -->", "-->\n# After stray"])
def test_markdown_missing_wrapper_never_hides_following_headings(tmp_path, later):
    rows = ["<!--", "@beacon[", "id=damaged,", "kind=ast,", "]", "# Independent", "real body", *later.split("\n"),
            "", "# Later", "<!-- @beacon[ id=safe, kind=ast, ] -->", "## Safe", "safe body"]
    path = tmp_path / "wrapper.md"
    raw = ("\n".join(rows) + "\n").encode()
    path.write_bytes(raw)
    stamp = path.stat().st_mtime_ns
    nodes = nmc.parse_markdown_structure(str(path))
    flat = []
    def walk(items):
        for item in items:
            flat.append(item)
            walk(item.get("children", []))
    walk(nodes)
    assert any(n["name"].startswith("Independent") for n in flat)
    assert any(n["name"].startswith("Later") for n in flat)
    assert any(n["name"].startswith("Safe") and "id: safe\n" in n.get("note", "") for n in flat)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, stamp)


def test_virtual_group_policy_is_preserved_in_recorded_and_legacy_context():
    rows = ["# @carto-group id=example label=Example", inline("python"), "# human", "code()"]
    assert context("python", rows, 2) == ["human"]
    assert nmc._strip_beacon_meta_blocks_from_context(["@carto-group id=example", "human"]) == ["human"]
