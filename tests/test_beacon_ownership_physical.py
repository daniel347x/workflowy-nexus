"""U01 finishing matrices. Source examples are quoted physical rows only.

Parent-baseline and narrow-mutation witnesses are recorded in leg 5. The
legacy byte serializer's BOM/line-ending limitations are NOT claimed fixed
by a structural ownership test; U02 owns that mutation primitive.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

import beacon_source as source
import nexus_map_codebase as nmc

BOM = b"\xef\xbb\xbf"
SPECIALS = "\f\v\x85\x1c\x1d\x1e\u2028\u2029"


def records(rows, language, bom=False, ending="\n"):
    snap = source.source_snapshot_from_bytes((BOM if bom else b"") + (ending.join(rows) + ending).encode())
    return snap, nmc._scan_source_beacons(snap, language)


def delete(path, language, bid):
    from beacon_plan_fixture import physical_operand
    return nmc._execute_beacon_write_plan(language=language, file_path=str(path), name="", note="", write_intent=object(),
        write_plan=SimpleNamespace(success=True, planned_operation="delete", beacon_id=bid, intended_slice_labels=(),
                                   **physical_operand(path, language, bid)))


@pytest.mark.parametrize("form", ["c", "jsdoc", "html"])
@pytest.mark.parametrize("mixed", [False, True])
def test_wrapper_owned_range_and_field_subrange_keep_ordinary_bytes(form, mixed):
    language = "markdown" if form == "html" else "js_ts"
    a, b = ("<!--", "-->") if form == "html" else ("/**" if form == "jsdoc" else "/*", "*/")
    body = (["ordinary before"] if mixed else []) + ["@beacon[", "id=x,", "slice_labels=lab,", "kind=ast,", "]"] + (["ordinary after"] if mixed else [])
    rows = [a, *[(" * " if form == "jsdoc" else "") + row for row in body], b]
    snap, found = records(rows, language)
    own = found[0]
    assert own["complete"] and own["wrapper_dedicated"] is not mixed
    owned = snap.raw_bytes[slice(*own["write_ranges"][0])]
    assert owned == (snap.raw_bytes[slice(own["opener_range"][0], own["read_end"])] if mixed else snap.raw_bytes)
    assert (own["line_extent"] is None) is mixed
    occurrence = next(o for o in own["occurrences"] if o["key"] == "slice_labels")
    left, right = occurrence["byte_range"]
    changed = snap.raw_bytes[:left] + b"slice_labels=lab,second," + snap.raw_bytes[right:]
    assert changed.count(b.encode()) == 1
    again = nmc._scan_source_beacons(source.source_snapshot_from_bytes(changed), language)[0]
    assert again["fields"]["slice_labels"] == "lab,second"
    if mixed:
        assert b"ordinary before" in changed and b"ordinary after" in changed and b.encode() not in owned


@pytest.mark.parametrize("language,a,b", [("js_ts", "/*", "*/"), ("markdown", "<!--", "-->")])
def test_complete_inline_wrapper_with_trailing_code_owns_only_wrapper(language, a, b):
    text = f"{a} @beacon[ id=x, kind=ast, ] {b}"
    snap, found = records([text + " following_code();"], language)
    assert found[0]["complete"] and found[0]["line_extent"] is None
    assert snap.raw_bytes[slice(*found[0]["write_ranges"][0])] == text.encode()


@pytest.mark.parametrize("language,a,b,ext", [("js_ts", "/*", "*/", ".js"), ("markdown", "<!--", "-->", ".md")])
def test_mixed_wrapper_refusal_preserves_bytes_mtime_and_safe_neighbour(tmp_path, language, a, b, ext):
    rows = [a, "ordinary before", "@beacon[", "id=mixed,", "kind=ast,", "]", "ordinary after", b,
            f"{a} @beacon[ id=safe, kind=ast, ] {b}", "keep()"]
    raw = ("\n".join(rows) + "\n").encode()
    path = tmp_path / ("mixed" + ext)
    path.write_bytes(raw)
    stamp = path.stat().st_mtime_ns
    result = delete(path, language, "mixed")
    assert result["error_kind"] == "beacon_extent_unreadable" and result["operation"] == "noop"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, stamp)
    assert delete(path, language, "safe")["operation"] == "deleted_beacon"
    assert path.read_bytes() == ("\n".join(rows[:8] + rows[9:]) + "\n").encode()


@pytest.mark.parametrize("language,prefix,ext", [("python", "#", ".py"), ("js_ts", "//", ".js"), ("sql", "--", ".sql"), ("shell", "#", ".sh"), ("yaml", "#", ".yaml")])
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_disk_snapshot_bom_twins_physical_specials_and_record_offsets(tmp_path, language, prefix, ext, ending):
    rows = [f'TEXT = "{SPECIALS}"', f"{prefix} @beacon[ id=own, kind=span, ]", "code()", f"{prefix} @beacon-close[ id=own, ]"]
    twins = []
    for bom in [False, True]:
        snap, found = records(rows, language, bom, ending)
        path = tmp_path / (str(bom) + ext)
        path.write_bytes(snap.raw_bytes)
        stamp = path.stat().st_mtime_ns
        lines, beacons = nmc._parse_disk_beacons_for_language(language, str(path))
        assert lines == (["\ufeff" + rows[0], *rows[1:]] if bom else rows)
        assert beacons[0]["comment_line"] == 2
        assert nmc._beacon_block_extent_for_language(language, lines, beacons[0]) == (1, 1)
        assert nmc._find_beacon_close_block_extent(language, lines, "own") == (3, 3)
        assert snap.raw_bytes[slice(*found[0]["opener_range"])] == b"@beacon["
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (snap.raw_bytes, stamp)
        twins.append(found)
    assert twins[1][0]["opener_range"][0] == twins[0][0]["opener_range"][0] + 3


@pytest.mark.parametrize("language,prefix,ext", [("python", "#", ".py"), ("js_ts", "//", ".js"), ("sql", "--", ".sql"), ("shell", "#", ".sh"), ("yaml", "#", ".yaml")])
def test_recorded_deletion_ranges_keep_physical_specials_outside_target(tmp_path, language, prefix, ext):
    rows = [f'TEXT = "{SPECIALS}"', f"{prefix} @beacon[ id=own, kind=span, ]", "code()", f"{prefix} @beacon-close[ id=own, ]"]
    path = tmp_path / ("source" + ext)
    path.write_bytes(("\n".join(rows) + "\n").encode())
    stamp = path.stat().st_mtime_ns
    snap = source.read_source_snapshot(path)
    found = nmc._scan_source_beacons(snap, language)
    proposed = snap.raw_bytes
    for left, right in sorted([r["write_ranges"][0] for r in found], reverse=True):
        proposed = proposed[:left] + proposed[right:]
    assert proposed == (rows[0] + "\ncode()\n").encode()
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (snap.raw_bytes, stamp)


def test_vue_style_and_inline_ownership_refuse_locally_and_script_deletes(tmp_path):
    rows = ["<template><!-- @beacon[ id=inline, kind=span, ] --></template>", "<style>",
            "/* @beacon[ id=style, kind=span, ] */", ".a{}", "</style>", "<script>",
            "// @beacon[", "// id=bad,", "// kind=span,", "function a() {}", "</script>",
            "<script>", "// @beacon[ id=safe, kind=ast, ]", "function b() {}", "</script>"]
    raw = ("\n".join(rows) + "\n").encode()
    path = tmp_path / "sections.vue"
    path.write_bytes(raw)
    stamp = path.stat().st_mtime_ns
    lines, beacons = nmc._parse_disk_beacons_for_language("vue", str(path))
    assert [b["id"] for b in beacons] == ["inline", "style", "bad", "safe"]
    assert nmc._vue_beacon_section_for_extent(lines, 2) == "style"
    for bid, error in [("inline", "operation_not_supported_for_target"), ("style", "operation_not_supported_for_target"), ("bad", "beacon_extent_unreadable")]:
        assert delete(path, "vue", bid)["error_kind"] == error
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, stamp)
    assert delete(path, "vue", "safe")["operation"] == "deleted_beacon"
    assert path.read_bytes() == ("\n".join(rows[:12] + rows[13:]) + "\n").encode()
