"""U01 · the Vue SFC section path: one split, one scan per section, absolute
original-byte records and one aggregate duplicate warning.

Every fixture here is a quoted row list; no test source line is itself a live
beacon block. Each witness names the pre-fix failure it distinguishes, measured
on the parent-start tree 2bac52c: the whole-file JS + Markdown Vue parse (a
``//``-shaped block inside a template was live, and template blocks were
resolved through a script grammar), the opener-line section guess (a template
block read as ``script``), whole-file extents and close lookups, the refused
Vue diagnostics sink, and the per-section duplicate warning that left a
cross-section duplicate unreported.
"""
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

import beacon_source as source
import nexus_map_codebase as nmc

_TEMPLATE_SPAN = [
    "<template>",
    "  <div>",
    "    <!--",
    "    @beacon[",
    "      id=tpl@one,",
    "      slice_labels=lab-t,",
    "      kind=span,",
    "      comment=Template span.,",
    "    ]",
    "    -->",
    "    <p>hi</p>",
    "    <!--",
    "    @beacon-close[",
    "      id=tpl@one,",
    "    ]",
    "    -->",
    "  </div>",
    "</template>",
]

_TEMPLATE_LOST_BRACKET = [
    "<template>",
    "  <div>",
    "    <!--",
    "    @beacon[",
    "      id=tpl@lost,",
    "      slice_labels=lab-t,",
    "      kind=span,",
    "    -->",
    "  </div>",
    "</template>",
]

_SCRIPT_AST = [
    "<script>",
    "// @beacon[",
    "//   id=script@ok,",
    "//   slice_labels=lab-s,",
    "//   kind=ast,",
    "//   comment=Good script neighbour.,",
    "// ]",
    "function alpha() {",
    "  return 1;",
    "}",
    "</script>",
]

_STYLE_SPAN = [
    "<style>",
    "/* @beacon[",
    "   id=style@one,",
    "   slice_labels=lab-c,",
    "   kind=span,",
    " ] */",
    ".a { color: red; }",
    "</style>",
]

# A ``//``-shaped block written as TEMPLATE TEXT (inside <pre>), not a comment.
_FAKE_IN_TEMPLATE = [
    "<template>",
    "  <pre>",
    "// @beacon[",
    "//   id=fake@pre,",
    "//   slice_labels=fake,",
    "//   kind=ast,",
    "// ]",
    "  </pre>",
    "</template>",
]


def _write(tmp_path, name: str, rows: list[str], *, ending: str = "\n", bom: bool = False) -> str:
    body = (ending.join(rows) + ending).encode("utf-8")
    path = tmp_path / name
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + body)
    return str(path)


def _snapshot(rows: list[str], *, ending: str = "\n", bom: bool = False) -> source.SourceSnapshot:
    body = (ending.join(rows) + ending).encode("utf-8")
    return source.source_snapshot_from_bytes((b"\xef\xbb\xbf" if bom else b"") + body)


def _row(rows: list[str], text: str) -> int:
    """The 0-based row index of the (unique) quoted row ``text``."""
    return rows.index(text)


def _last_row(rows: list[str], text: str) -> int:
    """The 0-based row index of the LAST quoted row equal to ``text``."""
    return len(rows) - 1 - rows[::-1].index(text)


def _block_by_id(beacons, bid):
    matches = [b for b in beacons if (b.get("id") or "").strip() == bid]
    assert len(matches) == 1, [b.get("id") for b in beacons]
    return matches[0]


pytestmark = pytest.mark.skipif(
    not nmc._HAVE_TREE_SITTER, reason="tree-sitter not available"
)


# ---------------------------------------------------------------------------
# §6.2 · a section's own grammar owns its blocks; nothing crosses.
# ---------------------------------------------------------------------------


def test_template_code_shaped_text_is_not_a_live_beacon_and_neighbour_survives(tmp_path):
    """Pre-fix: the whole-file JS pass opened ``fake@pre`` inside <pre>.

    The template text and the good script block share one file, so the
    exclusion cannot be bought by dropping the file.
    """
    rows = _FAKE_IN_TEMPLATE + [""] + _SCRIPT_AST
    path = _write(tmp_path, "pre.vue", rows)
    lines, beacons = nmc._parse_disk_beacons_for_language("vue", path)
    assert [b["id"] for b in beacons] == ["script@ok"]
    good = _block_by_id(beacons, "script@ok")
    assert good["kind"] == "ast" and good["slice_labels"] == "lab-s"
    assert good["comment"] == "Good script neighbour."
    assert good["comment_line"] == _last_row(rows, "// @beacon[") + 1
    # No record is owned at the fake opener's line, so no extent is fabricated.
    fake_line = _row(rows, "// @beacon[") + 1
    assert nmc._beacon_block_extent_for_language(
        "vue", lines, {"comment_line": fake_line}
    ) is None
    assert nmc._vue_beacon_section_for_extent(lines, fake_line - 1) == "unresolved"


def test_template_span_is_classified_and_geometried_from_its_section(tmp_path):
    """Pre-fix: the JS pass came first and the guess called the template ``script``.

    The same file carries a good script block whose section and extent must
    stay exact; the pre-fix beacon order was the JS pass then the Markdown
    pass, not document order.
    """
    rows = _TEMPLATE_SPAN + [""] + _SCRIPT_AST
    path = _write(tmp_path, "span.vue", rows)
    lines, beacons = nmc._parse_disk_beacons_for_language("vue", path)
    assert [b["id"] for b in beacons] == ["tpl@one", "script@ok"]
    tpl = _block_by_id(beacons, "tpl@one")
    scr = _block_by_id(beacons, "script@ok")
    assert nmc._vue_beacon_section_for_extent(lines, tpl["comment_line"] - 1) == "template"
    assert nmc._vue_beacon_section_for_extent(lines, scr["comment_line"] - 1) == "script"
    template_extent = nmc._beacon_block_extent_for_language("vue", lines, tpl)
    assert template_extent == (_row(rows, "    <!--"), _row(rows, "    -->"))
    assert nmc._beacon_block_extent_for_language("vue", lines, scr) == (
        _row(rows, "// @beacon["), _row(rows, "// ]"),
    )
    # Section-bounded: the template geometry ends before the script section.
    assert template_extent[1] < _row(rows, "<script>")


def test_lost_bracket_in_template_stays_inside_its_section(tmp_path):
    """Pre-fix: a damaged template block was read through a script grammar.

    The record's bound and the good script neighbour's exact geometry are
    asserted in the same file.
    """
    rows = _TEMPLATE_LOST_BRACKET + [""] + _SCRIPT_AST
    path = _write(tmp_path, "lost.vue", rows)
    lines, beacons = nmc._parse_disk_beacons_for_language("vue", path)
    damaged = _block_by_id(beacons, "tpl@lost")
    good = _block_by_id(beacons, "script@ok")
    assert damaged.get("incomplete") is True
    assert nmc._beacon_block_extent_for_language("vue", lines, damaged) is None
    assert nmc._beacon_block_extent_for_language("vue", lines, good) == (
        _row(rows, "// @beacon["), _row(rows, "// ]"),
    )
    pairs = nmc._vue_section_records(_snapshot(rows))
    records = [record for _section, recs in pairs for record in recs]
    damaged_record = next(r for r in records if r["fields"].get("id") == "tpl@lost")
    assert damaged_record["complete"] is False
    assert damaged_record["terminated_by"] in {
        "wrapper_end", "non_comment_line", "next_opener", "section_end", "eof",
    }
    assert damaged_record["read_end_line"] < _row(rows, "<script>") + 1
    good_record = next(r for r in records if r["fields"].get("id") == "script@ok")
    assert good_record["section"]["type"] == "script_element"
    assert good_record["line_extent"] == (_row(rows, "// @beacon["), _row(rows, "// ]"))
    assert [r["section"]["type"] for r in records] == [
        "template_element", "script_element",
    ]


# ---------------------------------------------------------------------------
# §6.1/§6.2 · normalized tree-sitter indexes become original-byte records.
# ---------------------------------------------------------------------------


def test_section_windows_are_original_bytes_with_bom_and_crlf():
    """The old section dict had no byte windows at all.

    A CRLF + BOM file's written window is byte-exact against its own bytes;
    its BOM-less twin keeps the same line locators but shifts every byte
    bound, which is only possible from original coordinates.
    """
    rows = [row.replace("<p>hi</p>", "<p>é中</p>") for row in _TEMPLATE_SPAN] + [""] + _SCRIPT_AST
    marked = _snapshot(rows, ending="\r\n", bom=True)
    plain = _snapshot(rows, ending="\r\n", bom=False)
    marked_sections = nmc._vue_sfc_sections(
        marked, nmc._parse_vue_sfc_tree(marked.normalized_text.encode("utf-8"))
    )
    plain_sections = nmc._vue_sfc_sections(
        plain, nmc._parse_vue_sfc_tree(plain.normalized_text.encode("utf-8"))
    )
    assert [s["type"] for s in marked_sections] == ["template_element", "script_element"]
    template = marked_sections[0]
    raw = marked.raw_bytes
    content = raw[template["content_byte_start"]:template["content_byte_end"]]
    assert content.startswith(b"\r\n  <div>")
    assert content.rstrip(b"\r\n").endswith(b"</div>")
    # The twins agree on every line locator; only the original byte offsets
    # move, by the BOM's three bytes.
    assert template["content_start_line"] == plain_sections[0]["content_start_line"]
    assert template["content_end_line"] == plain_sections[0]["content_end_line"]
    assert template["content_byte_start"] - plain_sections[0]["content_byte_start"] == 3
    span_record = next(
        r for r in nmc._vue_section_beacons(template, snapshot=marked)
        if r["marker"] == "open"
    )
    assert span_record["section"]["start_byte"] == template["content_byte_start"]
    assert span_record["section"]["end_byte"] == template["content_byte_end"]
    wrapper_first = _row(rows, "    <!--")
    wrapper_last = _row(rows, "    -->")
    owned = span_record["write_ranges"][0]
    assert raw[slice(*owned)] == b"".join(
        (row + "\r\n").encode("utf-8") for row in rows[wrapper_first:wrapper_last + 1]
    )


def test_unclosed_script_template_literal_does_not_taint_another_section(tmp_path):
    """A whole-file JS lexer would keep the next script inside the raw template."""
    rows = ["<script>", "const raw = `", "// @beacon[ id=maybe, kind=span, ]", "</script>", *_SCRIPT_AST]
    path = _write(tmp_path, "local-quote.vue", rows)
    lines, beacons = nmc._parse_disk_beacons_for_language("vue", path)
    assert [b["id"] for b in beacons] == ["maybe", "script@ok"]
    records = [r for _s, own in nmc._vue_section_records(_snapshot(rows)) for r in own]
    assert records[0]["literal_certainty"] == "uncertain" and records[0]["write_ranges"] is None
    assert records[1]["literal_certainty"] == "comment" and records[1]["write_ranges"]
    assert nmc._beacon_block_extent_for_language("vue", lines, beacons[1]) == (_last_row(rows, "// @beacon["), _last_row(rows, "// ]"))


def test_same_id_close_in_another_script_never_pairs(tmp_path):
    """Before the section route the whole-file pass pairs these across script tags."""
    rows = ["<script>", "// @beacon[ id=cross, kind=span, ]", "const a = 1;", "</script>",
            "<script setup>", "// @beacon-close[ id=cross, ]", "// @beacon[ id=safe, kind=span, ]",
            "const b = 2;", "// @beacon-close[ id=safe, ]", "</script>"]
    path = _write(tmp_path, "cross-close.vue", rows)
    lines, beacons = nmc._parse_disk_beacons_for_language("vue", path)
    assert [b["id"] for b in beacons] == ["cross", "safe"]
    assert nmc._find_beacon_close_block_extent("vue", lines, "cross") is None
    assert nmc._find_beacon_close_block_extent("vue", lines, "safe") == (8, 8)
    records = [r for _s, own in nmc._vue_section_records(_snapshot(rows)) for r in own]
    assert records[0]["pair_state"] == "open_only"
    assert records[1]["pair_state"] == "orphan"
    assert records[2]["closed"] and records[2]["close_line"] == 9
    assert records[0]["inner_range"][1] == records[0]["section"]["end_byte"]


def test_disk_route_forwards_one_diagnostic_per_block_across_sections(tmp_path):
    """Pre-fix: a Vue sink raised ``ValueError``; no per-block facts came out."""
    rows = _TEMPLATE_SPAN + [""] + _SCRIPT_AST + [""] + _STYLE_SPAN
    path = _write(tmp_path, "diag.vue", rows)
    before = (Path(path).read_bytes(), Path(path).stat().st_mtime_ns)
    sink: list = []
    with_sink = nmc._parse_disk_beacons_for_language("vue", path, diagnostics=sink)
    assert with_sink == nmc._parse_disk_beacons_for_language("vue", path)
    assert (Path(path).read_bytes(), Path(path).stat().st_mtime_ns) == before
    _lines, beacons = with_sink
    open_rows = [
        _row(rows, "    @beacon[") + 1,
        _row(rows, "// @beacon[") + 1,
        _row(rows, "/* @beacon[") + 1,
    ]
    assert [b["comment_line"] for b in beacons] == open_rows
    blocks = [r for r in sink if r["record"] == "block"]
    assert [r["line"] for r in blocks] == open_rows
    assert [r["outcome"] for r in blocks] == ["kept"] * 3
    close_row = _row(rows, "    @beacon-close[") + 1
    assert [(r["line"], r["id"]) for r in sink if r["record"] == "close"] == [
        (close_row, "tpl@one"),
    ]
    assert [r["opener_line"] for r in sink if r["record"] == "close_match"] == [
        open_rows[0],
    ]


def test_close_selection_never_borrows_a_close_from_another_section(tmp_path):
    """Pre-fix: the whole-file JS pass read a close out of template text.

    The good script span's close is found exactly; the template text's
    close-shaped lines are owned by no record, and a bare comment fragment
    (no SFC section) is refused rather than borrowed.
    """
    rows = _FAKE_IN_TEMPLATE + [""] + [
        "<script>", "// @beacon[", "//   id=script@one,", "//   kind=span,",
        "// ]", "const a = 1;", "// @beacon-close[", "//   id=script@one,",
        "// ]", "</script>",
    ]
    path = _write(tmp_path, "close.vue", rows)
    lines, beacons = nmc._parse_disk_beacons_for_language("vue", path)
    assert [b["id"] for b in beacons] == ["script@one"]
    assert nmc._find_beacon_close_block_extent("vue", lines, "fake@pre", 0) is None
    assert nmc._find_beacon_close_block_extent("vue", lines, "script@one", 0) == (
        _last_row(rows, "// @beacon-close["), _last_row(rows, "// ]"),
    )
    fragment = ["// @beacon-close[", "//   id=span-1,", "// ]"]
    assert nmc._find_beacon_close_block_extent("vue", fragment, "span-1", 0) is None
    wrapped = ["<script>", *fragment, "</script>"]
    assert nmc._find_beacon_close_block_extent("vue", wrapped, "span-1", 0) == (1, 3)


# ---------------------------------------------------------------------------
# §6.2 · whole-file duplicate warnings aggregate once across sections.
# ---------------------------------------------------------------------------


def _warning_names(tree):
    names: list[str] = []

    def walk(nodes):
        for node in nodes:
            name = str(node.get("name") or "")
            if name.startswith("🚨 Duplicate beacon ID"):
                names.append(name)
            walk(node.get("children") or [])

    walk(tree)
    return names


def test_duplicate_ids_aggregate_once_across_sections(tmp_path, monkeypatch):
    """Pre-fix: a script id shared with a template block got no warning at all."""
    cross = _TEMPLATE_SPAN + [""] + [
        "<script>", "// @beacon[", "//   id=tpl@one,", "//   slice_labels=lab-s,",
        "//   kind=ast,", "// ]", "function beta() {", "  return 2;", "}", "</script>",
    ]
    observed = []
    original = nmc._prepend_duplicate_beacon_id_warning_nodes
    def aggregate(nodes, beacons, *, language):
        observed.append([b["id"] for b in beacons])
        return original(nodes, beacons, language=language)
    monkeypatch.setattr(nmc, "_prepend_duplicate_beacon_id_warning_nodes", aggregate)
    tree = nmc.parse_vue_outline(_write(tmp_path, "cross.vue", cross))
    assert observed == [["tpl@one", "tpl@one"]]
    assert _warning_names(tree) == ["🚨 Duplicate beacon ID (JS/TS): tpl@one"]
    assert str(tree[0].get("name") or "").startswith("🚨 Duplicate beacon ID")
    assert [n.get("name") for n in tree[1:]] == ["⦿ [template]", "⦿ [script]"]

    inside = [
        "<template>", "  <div>hi</div>", "</template>", "",
        "<script>",
        "// @beacon[", "//   id=dup@in,", "//   kind=ast,", "// ]",
        "function one() {", "  return 1;", "}",
        "// @beacon[", "//   id=dup@in,", "//   kind=ast,", "// ]",
        "function two() {", "  return 2;", "}",
        "</script>",
    ]
    tree = nmc.parse_vue_outline(_write(tmp_path, "inside.vue", inside))
    assert _warning_names(tree) == ["🚨 Duplicate beacon ID (JS/TS): dup@in"]

    unique = _TEMPLATE_SPAN + [""] + _SCRIPT_AST
    assert _warning_names(nmc.parse_vue_outline(_write(tmp_path, "unique.vue", unique))) == []


def test_vue_outline_keeps_sections_and_attaches_only_owned_beacons(tmp_path):
    """Control, not a repair witness: the section-node shape before and after.

    One node per section in document order, the template and style span nodes
    named from their own section's beacons, and the script AST node attached
    in the script section only. Both implementations satisfy it; it fails
    loudly if the new route loses a section, a span node or the attachment.
    """
    rows = _TEMPLATE_SPAN + [""] + _SCRIPT_AST + [""] + _STYLE_SPAN
    tree = nmc.parse_vue_outline(_write(tmp_path, "outline.vue", rows))
    assert [node.get("name") for node in tree] == ["⦿ [template]", "⦿ [script]", "⦿ [style]"]
    assert [child.get("name") for child in tree[0]["children"]] == ["🔱 tpl #lab-t"]
    script_names = [child.get("name") for child in tree[1]["children"]]
    assert any("alpha" in (name or "") for name in script_names)
    assert [child.get("name") for child in tree[2]["children"]] == ["🔱 style #lab-c"]


# ---------------------------------------------------------------------------
# The never-brittle side: one failed section split refuses the block, alone.
# ---------------------------------------------------------------------------


def test_writer_helpers_refuse_only_the_block_when_the_split_fails(tmp_path, monkeypatch):
    """Pre-fix: those helpers never consulted an SFC split at all.

    A Python block in the same batch keeps its exact extent.
    """
    rows = _TEMPLATE_SPAN + [""] + _SCRIPT_AST
    path = _write(tmp_path, "boom.vue", rows)
    lines, beacons = nmc._parse_disk_beacons_for_language("vue", path)
    script = _block_by_id(beacons, "script@ok")
    expected_extent = (_row(rows, "// @beacon["), _row(rows, "// ]"))

    def boom(*args, **kwargs):
        raise RuntimeError("no vue grammar")

    monkeypatch.setattr(nmc, "_parse_vue_sfc_tree", boom)
    assert nmc._beacon_block_extent_for_language("vue", lines, script) is None
    assert nmc._find_beacon_close_block_extent("vue", lines, "tpl@one", 0) is None
    assert nmc._vue_beacon_section_for_extent(lines, _row(rows, "    <!--")) == "unresolved"
    monkeypatch.undo()
    assert nmc._beacon_block_extent_for_language("vue", lines, script) == expected_extent
    python_rows = [
        "# @beacon[", "#   id=p@1,", "#   kind=ast,", "# ]", "def one():", "    return 1",
    ]
    assert nmc._beacon_block_extent_for_language(
        "python", python_rows, {"comment_line": 1}
    ) == (0, 3)


def test_disk_route_propagates_a_split_failure_instead_of_empty_source(tmp_path, monkeypatch):
    """Pre-fix: the whole-file Vue read could not fail this way at all.

    Nothing is returned: the caller must refuse the item, never act on an
    empty beacon list.
    """
    path = _write(tmp_path, "boom2.vue", _TEMPLATE_SPAN + [""] + _SCRIPT_AST)

    def boom(*args, **kwargs):
        raise RuntimeError("no vue grammar")

    monkeypatch.setattr(nmc, "_parse_vue_sfc_tree", boom)
    with pytest.raises(RuntimeError, match="no vue grammar"):
        nmc._parse_disk_beacons_for_language("vue", path)


def test_plan_seam_answers_the_template_capability_truthfully(tmp_path):
    """Pre-fix: a clean template span got ``plan_mismatch``, not the capability.

    The template copy is refused and byte-identical; the script copy in the
    same batch deletes exactly its own block.
    """
    rows = _TEMPLATE_SPAN + [""] + _SCRIPT_AST
    template_path = _write(tmp_path, "tpl.vue", rows)
    template_before = open(template_path, "rb").read()
    template_stamp = Path(template_path).stat().st_mtime_ns
    from beacon_plan_fixture import physical_operand
    result = nmc._execute_beacon_write_plan(
        language="vue", file_path=template_path, name="tpl@one", note="",
        write_intent=object(),
        write_plan=SimpleNamespace(
            success=True, planned_operation="delete", beacon_id="tpl@one",
            intended_slice_labels=(), source_path=template_path,
            **physical_operand(template_path, "vue", "tpl@one"),
        ),
    )
    assert result.get("operation") == "noop"
    assert result.get("error_kind") == "operation_not_supported_for_target"
    assert open(template_path, "rb").read() == template_before
    assert Path(template_path).stat().st_mtime_ns == template_stamp

    script_path = _write(tmp_path, "scr.vue", rows)
    result = nmc._execute_beacon_write_plan(
        language="vue", file_path=script_path, name="script@ok", note="",
        write_intent=object(),
        write_plan=SimpleNamespace(
            success=True, planned_operation="delete", beacon_id="script@ok",
            intended_slice_labels=(), source_path=script_path,
            **physical_operand(script_path, "vue", "script@ok"),
        ),
    )
    assert result.get("operation") == "deleted_beacon", result
    text = open(script_path, encoding="utf-8").read()
    assert "script@ok" not in text and "// @beacon" not in text
    assert "function alpha" in text and "<template>" in text and "tpl@one" in text


@pytest.mark.parametrize("script_operation", ["delete", "update"])
def test_legacy_lost_bracket_selection_is_local_and_good_script_commits(tmp_path, monkeypatch, script_operation):
    """004d1ba: template overshoots (U02 refuses); script delete misses;
    script update erases its comment and mints role. This is the exact U01
    legacy_lost_bracket_fixture.vue quoted in-repository, not a scratch read.
    """
    damaged = _TEMPLATE_LOST_BRACKET.copy()
    damaged.insert(7, "      comment=Template span with a lost bracket.,")
    path = Path(_write(tmp_path, "legacy-lost.vue", damaged + [""] + _SCRIPT_AST))
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    proposed = []
    commit = nmc.commit_beacon_edit_plan
    def record_plan(plan, **kwargs):
        proposed.append(plan)
        return commit(plan, **kwargs)
    monkeypatch.setattr(nmc, 'commit_beacon_edit_plan', record_plan)
    refused = nmc.update_beacon_from_node_vue(str(path), "source",
        "BEACON (SPAN)\nid: tpl@lost\nkind: span\n---\n")
    assert refused['operation'] == 'noop' and refused['error_kind'] == 'beacon_extent_unreadable', refused
    assert proposed == []  # selector/planner answer, NOT U02 vetoing a bad range
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, stamp)
    candidate = next(c for c in refused['candidates'] if c['id'] == 'tpl@lost')
    assert not candidate['complete'] and candidate['write_ranges'] is None
    assert candidate['section']['type'] == 'template_element' and candidate['repair_candidates']
    good = nmc.update_beacon_from_node_vue(str(path),
        "source" if script_operation == 'delete' else "source #lab-s #lab-new",
        "BEACON (AST)\nid: script@ok\nkind: ast\n---\n")
    assert good['operation'] == ('deleted_beacon' if script_operation == 'delete' else 'updated_beacon'), good
    assert good['language'] == ('vue' if script_operation == 'delete' else 'js_ts')
    assert good['physical_witness']['section']['type'] == 'script_element'
    snapshot = source.source_snapshot_from_bytes(before, str(path))
    record = next(r for r in nmc._beacon_edit_records(snapshot, 'vue') if r['fields'].get('id') == 'script@ok')
    a, b = record['write_ranges'][0]
    expected = before[:a] + before[b:] if script_operation == 'delete' else before.replace(
        b'slice_labels=lab-s,', b'slice_labels=lab-s,lab-new,')
    assert path.read_bytes() == expected
    from test_beacon_byte_edits import assert_outside_edits_identical
    assert_outside_edits_identical(before, path.read_bytes(), good)
