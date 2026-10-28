"""U01 lexical repair witnesses, with sources represented only as quoted rows.

Baseline parsers accept the examples and the JS comment mask treats quoted
comment sugar as live. Every exclusion/uncertainty case includes a definite
neighbour whose original-byte metadata is still writable.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import beacon_source as source
import nexus_map_codebase as nmc


def block(prefix, bid):
    return [prefix + " @beacon[", prefix + " id=" + bid + ",",
            prefix + " kind=span,", prefix + " slice_labels=one,two,", prefix + " ]"]


def scan(language, rows, ending="\n", bom=False):
    raw = (ending.join(rows) + ending).encode()
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    snapshot = source.source_snapshot_from_bytes(raw)
    return snapshot, nmc._scan_source_beacons(snapshot, language)


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("bom", [False, True])
@pytest.mark.parametrize("delimiter", ['"""', "'''", 'r"""', 'f"""'])
def test_python_string_tokens_exclude_examples_but_not_comment_neighbour(ending, bom, delimiter):
    """Before §6.8: the fake opener is live, including in docstrings."""
    rows = ["example = " + delimiter, *block("#", "example"), delimiter[-3:], *block("#", "safe")]
    snapshot, records = scan("python", rows, ending, bom)
    assert [r["fields"]["id"] for r in records] == ["safe"]
    assert [b["id"] for b in nmc.parse_python_beacon_blocks(rows)] == ["safe"]
    assert snapshot.raw_bytes[slice(*records[0]["write_ranges"][0])].decode().strip() == ending.join(block("#", "safe"))
    assert nmc._beacon_block_extent_for_language("python", rows, {"comment_line": 2}) is None


@pytest.mark.parametrize("failure", ["indent", "single_quote", "open_paren", "syntax"])
def test_python_tokenize_failure_is_not_a_file_wide_gate(failure):
    """Failed tokenize/AST is not authority to drop a later definite comment."""
    leading = {
        "indent": ["if True:", "  pass", " pass"],
        "single_quote": ['broken = "unterminated'],
        "open_paren": ["broken = ("],
        "syntax": ["def invalid ???"],
    }[failure]
    rows = [*leading, *block("#", "safe"), 'example = """', *block("#", "example"), '"""']
    _, records = scan("python", rows)
    assert [r["fields"]["id"] for r in records] == ["safe"]
    assert records[0]["literal_certainty"] == "comment" and records[0]["write_ranges"]


def test_python_uncertain_tail_never_disables_a_definite_neighbour(tmp_path):
    """Before evidence: an unresolved literal is an ordinary destructive target."""
    rows = [*block("#", "safe"), 'example = """', *block("#", "maybe")]
    snapshot, records = scan("python", rows)
    assert [r["fields"]["id"] for r in records] == ["safe", "maybe"]
    assert records[0]["write_ranges"]
    assert records[1]["literal_certainty"] == "uncertain"
    assert records[1]["line_extent"] is None and records[1]["write_ranges"] is None
    assert any(d["code"] == "lexical_uncertainty" for d in records[1]["diagnostics"])
    path = tmp_path / "source.py"
    path.write_bytes(snapshot.raw_bytes)
    stamp = path.stat().st_mtime_ns
    from beacon_plan_fixture import physical_operand
    refused = nmc._execute_beacon_write_plan(language="python", file_path=str(path), name="maybe", note="",
        write_intent=object(), write_plan=SimpleNamespace(success=True, planned_operation="delete", beacon_id="maybe", intended_slice_labels=(), **physical_operand(path, "python", "maybe")))
    assert refused.get("error_kind") == "beacon_extent_unreadable"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (snapshot.raw_bytes, stamp)
    result = nmc._execute_beacon_write_plan(language="python", file_path=str(path), name="safe", note="",
        write_intent=object(), write_plan=SimpleNamespace(success=True, planned_operation="delete", beacon_id="safe", intended_slice_labels=(), **physical_operand(path, "python", "safe")))
    assert result.get("operation") == "deleted_beacon", result
    assert path.read_bytes() == ('example = """\n' + "\n".join(block("#", "maybe")) + "\n").encode()


@pytest.mark.parametrize("depth", [1, 2, 8, 1500])
def test_js_nested_templates_keep_real_expression_comments(depth):
    """Before evidence: template text beacons are live; expression comments survive."""
    rows = ["const text = `", *block("//", "example"), "${(() => {"]
    rows += ["const nested = `raw ${(() => {"] * (depth - 1)
    rows += block("//", "expression") + ["return 1;"]
    rows += ["})()} tail`; "] * (depth - 1)
    rows += ["})()} tail", "`;", *block("//", "safe")]
    _, records = scan("js_ts", rows)
    assert [r["fields"]["id"] for r in records] == ["expression", "safe"]
    assert all(r["write_ranges"] for r in records)
    mask, cleaned = nmc._build_js_comment_mask(rows)
    assert not any(mask[1:6])
    assert mask[rows.index("// id=expression,")]
    assert cleaned[rows.index("// id=expression,")] == "id=expression,"


def test_js_strings_escapes_regex_and_template_raw_sugar_do_not_leak():
    """Quoted /* formerly starts the shared mask's block-comment state."""
    rows = ['const s = "/*";', "const r = /[/*]/;", "const raw = `", "/* @beacon[ id=example, kind=span, ] */", "`;",
            "const continued = 'escaped\\", *[row + "\\" for row in block("//", "continued")], "tail';",
            *block("//", "safe")]
    _, records = scan("js_ts", rows)
    assert [r["fields"]["id"] for r in records] == ["safe"]
    mask, _ = nmc._build_js_comment_mask(rows)
    assert not any(mask[:rows.index("// @beacon[")])
    assert all(mask[-5:])
    # The lexical repair must not change the established body projection for
    # actual JSDoc comments (the kit's byte golden pins those leading stars).
    assert nmc._build_js_comment_mask(["/**", " * Greets people.", " */", "// real"]) == (
        [True, True, True, True], ["*", "* Greets people.", "", "real"])
    assert nmc._build_js_comment_mask(["const code = 1; // trailing comment"])[0] == [False]
    assert nmc._build_js_comment_mask(["// Real  ", "/*", " * ", "*/"])[1] == ["Real  ", "", "* ", ""]


def test_js_broken_single_line_quote_recovers_and_unclosed_template_is_local():
    rows = ['const broken = "unterminated', *block("//", "safe"), "const unfinished = `", *block("//", "maybe")]
    _, records = scan("js_ts", rows)
    assert records[0]["fields"]["id"] == "safe" and records[0]["write_ranges"]
    assert records[1]["literal_certainty"] == "uncertain" and records[1]["write_ranges"] is None


def test_uncertain_close_cannot_authorize_pairing_or_delete():
    rows = [*block("//", "safe"), "const raw = `", "// @beacon-close[ id=safe, ]"]
    _, records = scan("js_ts", rows)
    assert not records[0]["closed"] and records[0]["pair_state"] == "open_only"
    assert records[0]["write_ranges"]
    assert records[1]["literal_certainty"] == "uncertain" and records[1]["line_extent"] is None


def test_normalized_byte_offsets_roundtrip_bom_crlf_and_utf8():
    raw = b"\xef\xbb\xbf" + "é\r\n中\rthird\n".encode()
    snapshot = source.source_snapshot_from_bytes(raw)
    normalized = snapshot.normalized_text.encode()
    for needle in ["é", "中", "third"]:
        norm = normalized.index(needle.encode())
        original = snapshot.normalized_byte_offset(norm)
        assert raw[original:original + len(needle.encode())] == needle.encode()
    assert snapshot.normalized_byte_offset(len(normalized)) == len(raw)
