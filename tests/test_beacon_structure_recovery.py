"""U01 structural recovery; all source examples are quoted physical rows.

The old implementation swallows neighbours, loses opener/EOF fields, pairs
substring mentions and skips adjacent closes. These are repair witnesses,
not accept-both characterization tests. Literal examples are never live blocks.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

import beacon_mutation_contract as fields
import beacon_source as source
import nexus_map_codebase as nmc


PARSERS = {
    "python": nmc.parse_python_beacon_blocks, "js_ts": nmc.parse_js_beacon_blocks,
    "markdown": nmc.parse_markdown_beacon_blocks, "sql": nmc.parse_sql_beacon_blocks,
    "shell": nmc.parse_sh_beacon_blocks, "yaml": nmc.parse_yaml_beacon_blocks,
}
PREFIX = {"python": "#", "js_ts": "//", "sql": "--", "shell": "#", "yaml": "#"}
DATA = Path(__file__).with_name("beacon_robustness_corpus")


def block(language, bid="bad", kind="ast", close=False, inline=False):
    marker = "@beacon-close[" if close else "@beacon["
    values = [f"id={bid},"] if close else [f"id={bid},", "slice_labels=one,two,", f"kind={kind},", f"comment=Own {bid}.,"]
    body = [marker + " " + " ".join(values) + " ]"] if inline else [marker, *values, "]"]
    return ["<!--", *body, "-->"] if language == "markdown" else [f"{PREFIX[language]} {row}" for row in body]


def scan(language, rows, **kwargs):
    snap = source.source_snapshot_from_bytes("\n".join(rows).encode())
    return snap, nmc._scan_source_beacons(snap, language, **kwargs)


def corrupt(language, number):
    rows = block(language)
    bracket = len(rows) - (2 if language == "markdown" else 1)
    prefix = "" if language == "markdown" else PREFIX[language] + " "
    if number == 1:
        del rows[bracket]
    elif number == 2:
        rows[bracket] += ","
    elif number == 3:
        rows[bracket] += "  "
    elif number == 4:
        rows.insert(0, prefix + "]")
    elif number == 5:
        rows[bracket:bracket] = block(language, "nested")
    elif number == 6:
        rows = block(language, inline=True)
    elif number == 7:
        rows = block(language, kind="span") + block(language, close=True)
        del rows[-(2 if language == "markdown" else 1)]
    elif number == 8:
        rows.insert(0, prefix + "id=outside,")
    elif number == 9:
        rows = rows[:bracket]
    elif number in {10, 11}:
        example = rows if number == 11 else rows[:bracket]
        if language == "markdown":
            rows = ["```text", *example, "```"]
        else:
            boundaries = {"python": ['sample = """', '"""'], "js_ts": ["const sample = `", "`;"],
                          "sql": ["SELECT '", "';"], "shell": ["cat <<'EXAMPLE'", "EXAMPLE"],
                          "yaml": ["sample: |", "next: true"]}
            a, b = boundaries[language]
            if language == "yaml":
                example = ["  " + row for row in example]
            rows = [a, *example, b]
    elif number == 12:
        rows = rows[:bracket] + ["values = [", "]"]
    elif number == 13:
        rows = rows[:bracket] + ["kind = compute()", "id = other()"]
    elif number == 14:
        rows.insert(bracket, prefix + "id=conflicting,")
    elif number == 15:
        if language == "markdown":
            rows.pop()
            rows += ["# Independent heading", "Real prose -> not a wrapper"]
        else:
            rows.insert(bracket, "broken comment prefix")
    return rows


@pytest.mark.parametrize("language", PARSERS)
@pytest.mark.parametrize("number", range(1, 16))
@pytest.mark.parametrize("position", ["bof", "middle", "eof"])
def test_fifteen_by_six_recovery_and_exact_neighbours(language, number, position):
    """Baseline: k1/k6/k10/k13 swallow B2; F1 extents cross code."""
    before, after = block(language, "good-before"), block(language, "good-after")
    bad = corrupt(language, number)
    # Physical EOF cannot have a following block. Both neighbours are tested
    # at middle; BOF/EOF independently exercise the file boundaries too.
    pieces = ([bad, after] if position == "bof" else [before, bad] if position == "eof" else [before, bad, after])
    rows = [r for piece in pieces for r in piece]
    snap, records = scan(language, rows)
    sink = []
    got = PARSERS[language](rows, diagnostics=sink)
    assert got == PARSERS[language](rows)
    if number in {10, 11}:
        assert not any(r["fields"].get("id") == "bad" for r in records)
        assert not any(b.get("id") == "bad" for b in got)
    for bid, piece in [("good-before", before), ("good-after", after)]:
        if piece not in pieces:
            continue
        start = sum(len(p) for p in pieces[:pieces.index(piece)])
        line = start + (2 if language == "markdown" else 1)
        match = [r for r in records if r["fields"].get("id") == bid]
        assert len(match) == 1, (language, number, position, records)
        record = match[0]
        assert record["fields"] == {"id": bid, "slice_labels": "one,two", "kind": "ast", "comment": f"Own {bid}."}
        assert (record["line"], record["line_extent"], record["terminated_by"]) == (line, (start, start + len(piece) - 1), "bracket")
        assert record["complete"] and record["write_ranges"]
        a, b = record["write_ranges"][0]
        assert snap.raw_bytes[a:b].decode().rstrip("\n") == "\n".join(piece)
        beacon = next(b for b in got if b.get("id") == bid)
        assert (beacon["slice_labels"], beacon["kind"], beacon["comment"], beacon["comment_line"]) == ("one,two", "ast", f"Own {bid}.", line)
        assert nmc._beacon_block_extent_for_language(language, rows, beacon) == record["line_extent"]
    for record in records:
        assert record["terminated_by"] in {"bracket", "next_opener", "non_comment_line", "wrapper_end", "section_end", "eof"}
        if not record["complete"]:
            assert record["write_ranges"] is None and record["line_extent"] is None
            if record["marker"] == "open":
                assert nmc._beacon_block_extent_for_language(language, rows, {"comment_line": record["line"]}) is None
    if number in {1, 9, 12, 13, 15}:
        damaged = next(r for r in records if any(o["key"] == "id" and o["value"] == "bad" for o in r["occurrences"]))
        assert not damaged["complete"]
        expected = ("wrapper_end" if language == "markdown" and number == 1 else
                    "eof" if number == 9 and position == "eof" else
                    "next_opener" if number == 9 else
                    "eof" if number == 1 and position == "eof" else
                    "next_opener" if number == 1 else
                    "non_comment_line" if language != "markdown" else None)
        if expected:
            assert damaged["terminated_by"] == expected


@pytest.mark.parametrize("language", PARSERS)
def test_historical_corruption_inputs_keep_independent_own_fields(language):
    """The measured real fixtures, not just newly constructed equivalents."""
    frozen = json.loads((DATA / "area1_q1/area1_q1_corruptions.json").read_text())
    for case in (c for c in frozen["cases"] if c["lang"] == language):
        rows = source.physical_source_lines(case["text"])
        _, records = scan(language, rows)
        parsed = PARSERS[language](rows)
        for bid in ("b1@0", "b2@0", "b3@0", "b4@0"):
            # k14 intentionally changes b2's id; all other own blocks survive.
            if "duplicate" in case["case"] and bid == "b2@0":
                continue
            own_line = next((i for i, row in enumerate(rows, 1) if f"id={bid}," in row), None)
            if own_line is None:
                continue
            record = next(r for r in records if r["fields"].get("id") == bid)
            beacon = next(b for b in parsed if b.get("id") == bid)
            assert record["line"] < own_line <= record["read_end_line"], case["key"]
            assert beacon["comment_line"] == record["line"], case["key"]
            assert beacon["slice_labels"] == "lab-" + bid.split("@")[0], case["key"]
            assert beacon["comment"] == f"Comment of {bid}.", case["key"]


@pytest.mark.parametrize("language", PARSERS)
@pytest.mark.parametrize("close_id,matched", [("s1-x", False), ("s1 ,", True), ("S1", False), ('"s1"', True), ('"s1 "', False)])
def test_exact_id_pair_normalization(language, close_id, matched):
    rows = block(language, "s1", "span") + ["real body"] + block(language, close_id, close=True, inline=True)
    _, records = scan(language, rows)
    opening = records[0]
    assert opening["closed"] is matched
    assert records[-1]["pair_state"] == ("matched" if matched else "orphan")
    beacon = PARSERS[language](rows)[0]
    if matched:
        assert (beacon["span_lineno_start"], beacon["span_lineno_end"]) == opening["inner_lines"]
    else:
        assert beacon.get("span_lineno_start") is None
    if matched:
        assert nmc._find_beacon_close_block_extent(language, rows, "s1") == records[-1]["line_extent"]


@pytest.mark.parametrize("language", PARSERS)
@pytest.mark.parametrize("layout", ["adjacent", "nested", "interleaved", "empty", "open_only", "orphan_above_below"])
def test_pairing_geometry_and_no_skip_after_close(language, layout):
    a, b = block(language, "a", "span"), block(language, "b", "span", inline=True)
    ca, cb = block(language, "a", close=True), block(language, "b", close=True)
    if layout == "nested":
        rows = a + b + ["body"] + cb + ca
    elif layout in {"adjacent", "interleaved"}:
        rows = a + b + ["body"] + ca + cb
    elif layout == "empty":
        rows = a + ca
    elif layout == "open_only":
        rows = a + ["body"]
    else:
        rows = cb + a + ["body"] + ca + ca
    snap, records = scan(language, rows)
    opens = [r for r in records if r["marker"] == "open"]
    assert all(r["closed"] == (layout != "open_only") for r in opens)
    for record in opens:
        x, y = record["inner_range"]
        assert 0 <= x <= y <= len(snap.raw_bytes)
        includes_other = ((layout == "nested" and record["fields"]["id"] == "a") or
                          (layout in {"interleaved", "adjacent"} and record["fields"]["id"] == "b"))
        assert snap.raw_bytes[x:y].decode().count("@beacon-close[") == int(includes_other)
        if record["closed"]:
            beacon = next(b for b in PARSERS[language](rows) if b["id"] == record["fields"]["id"])
            assert (beacon["span_lineno_start"], beacon["span_lineno_end"]) == record["inner_lines"]
    if layout == "empty":
        assert opens[0]["inner_lines"][0] > opens[0]["inner_lines"][1]
        assert opens[0]["inner_range"][0] == opens[0]["inner_range"][1]
        assert PARSERS[language](rows)[0]["closed"] is True
    if layout == "orphan_above_below":
        assert [r["pair_state"] for r in records if r["marker"] == "close"] == ["orphan", "matched", "orphan"]


@pytest.mark.parametrize("language", PARSERS)
def test_conflicting_ids_pair_ambiguity_and_independent_neighbour(language):
    rows = block(language, "s1", "span") + block(language, "s1", "span") + block(language, "s1", close=True) + block(language, "good")
    _, records = scan(language, rows)
    assert [r["pair_state"] for r in records[:3]] == ["ambiguous"] * 3
    assert records[2]["pair_candidates"] == [records[0]["line"], records[1]["line"]]
    assert all(r["write_ranges"] is None for r in records[:3])
    assert records[-1]["complete"] and records[-1]["write_ranges"]
    assert nmc._find_beacon_close_block_extent(language, rows, "s1") is None


def test_fields_occurrences_quotes_unknowns_and_inline_ambiguity():
    occurrences, diagnostics = [], []
    result = fields.parse_beacon_field_lines(["id=x,", "id=x,", "comment=first,", "", "paragraph two,", "unknown=value", "kind=ast,"], occurrences=occurrences, diagnostics=diagnostics)
    assert result == {"id": "x", "comment": "first, paragraph two", "kind": "ast"}
    assert any(d["code"] == "unknown_field" and d["key"] == "unknown" for d in diagnostics)
    duplicate = next(d for d in diagnostics if d["code"] == "duplicate_field")
    assert duplicate["key"] == "id" and not duplicate["conflicting"]
    assert [o["line"] for o in occurrences if o["key"] == "id"] == [1, 2]
    _, records = scan("python", ['# @beacon[ id="s1", slice_labels=a,b, kind=ast, comment=describes kind=span, id=other, ]'])
    record = records[0]
    assert record["fields"]["id"] == "s1"
    assert record["fields"]["slice_labels"] == "a,b"
    assert record["fields"]["comment"] == "describes kind=span, id=other"
    assert any(d["code"] == "inline_field_ambiguity" for d in record["diagnostics"])
    assert record["write_ranges"] is None
    assert fields.normalize_beacon_id('"x,]" ,') == "x,]"
    for second_id, conflicting in [("s1", False), ("s2", True)]:
        snap, repeated = scan("python", ["# @beacon[", "# id=s1,", f"# id={second_id},", "# kind=span,", "# ]", *block("python", "neighbour")])
        own = repeated[0]
        assert own["id_conflicting"] is conflicting
        assert [o["line"] for o in own["occurrences"] if o["key"] == "id"] == [2, 3]
        assert bool(own["write_ranges"]) is not conflicting
        assert own["pair_state"] == ("ambiguous" if conflicting else "open_only")
        assert repeated[1]["fields"]["id"] == "neighbour" and repeated[1]["write_ranges"]
    # Only MATCHING quotes protect delimiters. A human apostrophe or an
    # unmatched quote must not manufacture an incomplete/unwritable block.
    for comment in ["Dan's block", "unpaired ' text", 'unpaired " text',
                    "'Dan's quoted, kind=ast ] block'", '"quoted, kind=ast ] block"']:
        rows = [f"# @beacon[ id=x, kind=ast, comment={comment}, ]", *block("python", "safe")]
        _, records = scan("python", rows)
        expected = comment[1:-1] if comment[0] in "\"'" and comment[-1] == comment[0] else comment
        assert records[0]["complete"] and records[0]["line_extent"] == (0, 0)
        assert records[0]["fields"]["comment"] == expected
        assert records[1]["fields"]["id"] == "safe" and records[1]["write_ranges"]


@pytest.mark.parametrize("language", PARSERS)
def test_bounded_eof_keeps_last_field_and_offers_read_not_deletion(language):
    prefix = "" if language == "markdown" else PREFIX[language] + " "
    rows = (["<!--"] if language == "markdown" else []) + [prefix + "@beacon[", prefix + "id=last,", prefix + "kind=ast"]
    _, records = scan(language, rows)
    record = records[0]
    assert record["fields"] == {"id": "last", "kind": "ast"}
    assert record["terminated_by"] == "eof"
    assert record["write_ranges"] is None
    beacon = PARSERS[language](rows)[0]
    assert beacon["kind"] == "ast" and beacon["incomplete"]
    assert record["repair_unambiguous"] == (language != "markdown")


@pytest.mark.parametrize("form", ["html", "c", "jsdoc"])
def test_wrapper_byte_ownership_inline_and_mixed(form):
    language = "markdown" if form == "html" else "js_ts"
    a, b = ("<!--", "-->") if form == "html" else ("/*", "*/")
    marker = "@beacon[ id=x, kind=ast, ]"
    snap, records = scan(language, [a + " " + marker + " " + b, "keep"])
    record = records[0]
    assert record["line_extent"] == (0, 0)
    assert snap.raw_bytes[slice(*record["write_ranges"][0])].decode().rstrip("\n") == a + " " + marker + " " + b
    prefix = " * " if form == "jsdoc" else ""
    snap, records = scan(language, [a, prefix + "ordinary before", prefix + marker, prefix + "ordinary after", b])
    record = records[0]
    assert record["complete"] and not record["wrapper_dedicated"]
    assert record["line_extent"] is None
    assert snap.raw_bytes[slice(*record["write_ranges"][0])].decode() == marker
    if form == "html":
        _, records = scan(language, [a, "@beacon[", "id=x,", "kind=ast,", "comment=first paragraph,", "", "# Heading-like continuation", "]", b])
        assert records[0]["complete"]
        assert records[0]["fields"]["comment"] == "first paragraph, # Heading-like continuation"


def test_section_bounds_and_original_byte_coordinates_with_bom_crlf_unicode():
    raw = b"\xef\xbb\xbf" + "é\r\n# @beacon[ id=x, kind=span, ]\r\nbody\r\n# @beacon-close[ id=x, ]\r\n".encode()
    snap = source.source_snapshot_from_bytes(raw)
    records = source.scan_beacon_structure(snap, "python", section_start=2, section_end=3, section_type="script")
    assert len(records) == 1 and records[0]["pair_state"] == "open_only"
    assert records[0]["section"] == {"type": "script", "start_line": 2, "end_line": 3}
    assert raw[slice(*records[0]["opener_range"])] == b"@beacon["
    assert raw[slice(*records[0]["inner_range"])] == b"body\r\n"
    assert [raw[slice(*o["byte_range"])].decode() for o in records[0]["occurrences"]] == ["id=x", "kind=span,"]


def test_markdown_fences_and_structural_marker_mentions_do_not_pair():
    real = block("markdown", "s1", "span")
    rows = real + ["```text", *block("markdown", "s1", close=True), "```", "prose @beacon-close[", "id=s1,"] + block("markdown", "s1", close=True)
    _, records = scan("markdown", rows)
    assert len(records) == 2 and records[0]["close_line"] == records[1]["line"]


@pytest.mark.parametrize("language", PARSERS)
def test_independent_planned_delete_keeps_damaged_neighbour_and_real_code(tmp_path, language):
    """Pre-fix F3: selecting the stolen good id deletes the prior real code."""
    bad = block(language, "bad")
    del bad[-(2 if language == "markdown" else 1)]
    suffix = {"python": ".py", "js_ts": ".js", "markdown": ".md", "sql": ".sql", "shell": ".sh", "yaml": ".yaml"}[language]
    before = bad + ["keep_this_code()"]
    good = block(language, "good", "span")
    body = ["independent_body()"]
    rows = before + good + body + block(language, "good", close=True)
    path = tmp_path / ("source" + suffix)
    raw = ("\n".join(rows) + "\n").encode()
    path.write_bytes(raw)
    stamp = path.stat().st_mtime_ns
    lines, beacons = nmc._parse_disk_beacons_for_language(language, str(path))
    damaged = next(b for b in beacons if b["id"] == "bad")
    assert nmc._beacon_block_extent_for_language(language, lines, damaged) is None
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, stamp)
    from beacon_plan_fixture import physical_operand
    result = nmc._execute_beacon_write_plan(language=language, file_path=str(path), name="good", note="",
                                           write_intent=object(), write_plan=SimpleNamespace(
                                               success=True, planned_operation="delete", beacon_id="good",
                                               intended_slice_labels=(), **physical_operand(path, language, "good")))
    assert result.get("operation") == "deleted_beacon", result
    assert path.read_bytes() == ("\n".join(before + body) + "\n").encode()


def test_parsers_are_total_on_arbitrary_content():
    rng = random.Random(394)
    atoms = ["# @beacon[", "// @beacon[", "<!--", "/*", "@beacon-close[", "# id=x,", "// kind=span,", "]", "# ]", "-->", "*/", "", "\ud800", "é", "```", "# @beacon-close["]
    for _ in range(4000):
        rows = [rng.choice(atoms) for _ in range(rng.randrange(20))]
        for parser in PARSERS.values():
            sink = []
            assert parser(rows, diagnostics=sink) == parser(rows)
