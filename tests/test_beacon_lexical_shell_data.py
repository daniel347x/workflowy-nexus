"""U01 §6.8 lexical witnesses for SQL, shell and YAML.

Sources are quoted physical rows only. At base (2bac52c) a beacon-looking
example inside a SQL string or dollar-quoted body, a shell here-document body
or quoted region, and a YAML block or quoted scalar is a live, writable
record — the parser has no literal evidence. After the lexical adapters the
example is literal (or locally uncertain) while every definite neighbour
keeps its original-byte metadata and stays writable.
"""
from __future__ import annotations

import random

import pytest

import beacon_source as source
import nexus_map_codebase as nmc


def block(prefix, bid, indent=""):
    return [indent + prefix + " @beacon[", indent + prefix + " id=" + bid + ",",
            indent + prefix + " kind=span,", indent + prefix + " slice_labels=one,two,",
            indent + prefix + " ]"]


def scan(language, rows, ending="\n", bom=False):
    raw = (ending.join(rows) + ending).encode()
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    snapshot = source.source_snapshot_from_bytes(raw)
    return snapshot, nmc._scan_source_beacons(snapshot, language)


def live_ids(records):
    return [r["fields"]["id"] for r in records if r["marker"] == "open"]


def certainty_at(snapshot, evidence, needle):
    """The first evidence row covering the needle's bytes, or 'live'."""
    offset = snapshot.raw_bytes.index(needle.encode())
    for row in evidence:
        if row["start"] <= offset < row["end"]:
            return row["certainty"]
    return "live"


def live_at(snapshot, evidence, needle):
    """True when the needle is a definite comment and never literal or uncertain."""
    return certainty_at(snapshot, evidence, needle) in {"comment", "live"}


ADAPTERS = {"sql": source.sql_lexical_evidence,
            "sh": source.shell_lexical_evidence,
            "yaml": source.yaml_lexical_evidence}


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("bom", [False, True])
def test_sql_string_example_is_literal_and_neighbour_survives(ending, bom):
    """Before §6.8: the example opener inside the string is a live record."""
    rows = ["SELECT '", *block("--", "example"), "';", *block("--", "safe")]
    snapshot, records = scan("sql", rows, ending, bom)
    assert live_ids(records) == ["safe"]
    assert records[0]["write_ranges"] and records[0]["literal_certainty"] == "comment"
    assert snapshot.raw_bytes[slice(*records[0]["write_ranges"][0])].decode().strip() == ending.join(block("--", "safe"))
    evidence = ADAPTERS["sql"](snapshot)
    assert certainty_at(snapshot, evidence, "-- @beacon[") == "literal"
    assert live_at(snapshot, evidence, "-- id=safe,")
    assert nmc._beacon_block_extent_for_language("sql", rows, {"comment_line": 2}) is None
    assert nmc.parse_sql_beacon_blocks(rows) == [b for b in nmc.parse_sql_beacon_blocks(rows, diagnostics=[])]
    assert [b["id"] for b in nmc.parse_sql_beacon_blocks(rows)] == ["safe"]


@pytest.mark.parametrize("wrapper", ["$tag$", "$body$", "$$"])
def test_sql_dollar_quoted_example_and_parameter_are_not_openers(wrapper):
    """Before §6.8: a dollar-quoted body supplies a live, writable block."""
    rows = ["SELECT " + wrapper, *block("--", "example"), wrapper + ";", *block("--", "safe")]
    snapshot, records = scan("sql", rows)
    assert live_ids(records) == ["safe"]
    assert certainty_at(snapshot, source.sql_lexical_evidence(snapshot), "-- @beacon[") == "literal"
    rows = ["SELECT * FROM t WHERE id = $1 AND note = '", *block("--", "example"), "';", *block("--", "safe")]
    snapshot, records = scan("sql", rows)
    assert live_ids(records) == ["safe"], "a positional parameter must not open a dollar quote"


def test_sql_comments_are_lexed_first_and_nested():
    """Before §6.8: an apostrophe inside a comment opens a string over the file."""
    rows = ["-- Dan's note; it's plain comment text", "# and a hash comment with it's", "SELECT 1;", *block("--", "safe")]
    snapshot, records = scan("sql", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    assert live_at(snapshot, source.sql_lexical_evidence(snapshot), "-- @beacon[")
    rows = ["/* outer /* inner ' still comment", "   it's still inside", "*/", *block("--", "safe")]
    snapshot, records = scan("sql", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]


def test_sql_doubled_quotes_and_estrings_and_unterminated_tail():
    """Doubling keeps one region; an unterminated quote is locally uncertain."""
    rows = ["SELECT 'it''s", "-- @beacon[", "-- id=example,", "-- ]", "';", *block("--", "safe")]
    snapshot, records = scan("sql", rows)
    assert live_ids(records) == ["safe"]
    rows = ["SELECT E'it\\'s ok', 'plain';", *block("--", "safe")]
    snapshot, records = scan("sql", rows)
    assert live_ids(records) == ["safe"], "an E-string's escape is definite"
    rows = [*block("--", "safe"), "SELECT 'unterminated", *block("--", "maybe")]
    snapshot, records = scan("sql", rows)
    assert live_ids(records) == ["safe", "maybe"]
    assert records[0]["write_ranges"]
    assert records[1]["literal_certainty"] == "uncertain" and records[1]["write_ranges"] is None
    assert any(d["code"] == "lexical_uncertainty" for d in records[1]["diagnostics"])


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("bom", [False, True])
def test_shell_heredoc_body_is_literal_and_neighbour_survives(ending, bom):
    """Before §6.8: a here-document body supplies a live, writable block."""
    rows = ["cat > out.py <<'PY'", *block("#", "example"), "PY", *block("#", "safe")]
    snapshot, records = scan("sh", rows, ending, bom)
    assert live_ids(records) == ["safe"]
    assert records[0]["write_ranges"] and records[0]["literal_certainty"] == "comment"
    assert snapshot.raw_bytes[slice(*records[0]["write_ranges"][0])].decode().strip() == ending.join(block("#", "safe"))
    assert certainty_at(snapshot, ADAPTERS["sh"](snapshot), "# @beacon[") == "literal"
    assert nmc._beacon_block_extent_for_language("shell", rows, {"comment_line": 2}) is None
    assert [b["id"] for b in nmc.parse_sh_beacon_blocks(rows)] == ["safe"]


@pytest.mark.parametrize("operator", ["<<EOF", "<<-EOF", "<<\\EOF", "<<\"EOF\""])
def test_shell_heredoc_quoting_and_tabs(operator):
    """Before §6.8: every here-document body is live source text."""
    indent = "\t" if operator == "<<-EOF" else ""
    rows = ["cat " + operator, *block("#", "example", indent), indent + "EOF", *block("#", "safe")]
    snapshot, records = scan("sh", rows)
    assert live_ids(records) == ["safe"], operator
    assert certainty_at(snapshot, ADAPTERS["sh"](snapshot), "# @beacon[") == "literal"
    assert records[0]["write_ranges"]


def test_shell_heredoc_inside_substitution_and_double_heredocs():
    """Nested here-documents are still data; two bodies keep source order."""
    rows = ["x=$(cat <<EOF", *block("#", "example"), "EOF", ")", *block("#", "safe")]
    snapshot, records = scan("sh", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    rows = ["cat <<A <<B", "one", "A", "two", "B", *block("#", "safe")]
    snapshot, records = scan("sh", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    assert all(r["certainty"] == "literal" for r in ADAPTERS["sh"](snapshot) if r["certainty"] != "comment")


def test_shell_shifts_and_herestrings_do_not_queue_heredocs():
    """Before §6.8: a shift body search can swallow later definite blocks."""
    rows = ["echo $((1 << 2))", "echo $((x << 3))", "echo $[1<<2]", 'echo "$[1<<2]"', "read -r line <<< \"quoted text\"", *block("#", "safe")]
    snapshot, records = scan("sh", rows)
    assert live_ids(records) == ["safe"]
    assert records[0]["write_ranges"] and records[0]["literal_certainty"] == "comment"
    assert live_at(snapshot, ADAPTERS["sh"](snapshot), "# @beacon[")


def test_shell_quoting_and_real_substitution_comments():
    """Quoted regions are literal; a comment inside $( ) is still live code."""
    rows = ["value='", *block("#", "example"), "'", *block("#", "safe")]
    snapshot, records = scan("sh", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    rows = ["x=$'", *block("#", "example"), "'", *block("#", "safe")]
    snapshot, records = scan("sh", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    rows = ["echo \"$(cat \"inner.txt\")\"", "echo \"${x#y}\"", *block("#", "safe")]
    snapshot, records = scan("sh", rows)
    assert live_ids(records) == ["safe"], "nested quoting must not leak over later code"
    assert records[0]["write_ranges"]
    rows = ["x=$(", *block("#", "inside"), ")", *block("#", "safe")]
    snapshot, records = scan("sh", rows)
    assert live_ids(records) == ["inside", "safe"]
    assert all(r["write_ranges"] for r in records)
    rows = ["x=$(cat <<'PY'", *block("#", "data"), "PY", ")", *block("#", "safe")]
    snapshot, records = scan("sh", rows)
    assert live_ids(records) == ["safe"]


def test_shell_comment_word_and_unterminated_tail():
    """A comment's apostrophe stays text; an open quote is locally uncertain."""
    rows = ["# it's only a note", *block("#", "safe")]
    snapshot, records = scan("sh", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    assert live_at(snapshot, ADAPTERS["sh"](snapshot), "# @beacon[")
    rows = [*block("#", "safe"), "value='unterminated", *block("#", "maybe")]
    snapshot, records = scan("sh", rows)
    assert live_ids(records) == ["safe", "maybe"]
    assert records[0]["write_ranges"]
    assert records[1]["literal_certainty"] == "uncertain" and records[1]["write_ranges"] is None
    rows = ["echo a#notacomment", *block("#", "safe")]
    snapshot, records = scan("sh", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("bom", [False, True])
def test_yaml_block_scalar_body_is_literal_and_neighbour_survives(ending, bom):
    """Before §6.8: a | body supplies a live, writable block."""
    rows = ["sample: |", *block("#", "example", "  "), "next: true", *block("#", "safe")]
    snapshot, records = scan("yaml", rows, ending, bom)
    assert live_ids(records) == ["safe"]
    assert records[0]["write_ranges"] and records[0]["literal_certainty"] == "comment"
    assert snapshot.raw_bytes[slice(*records[0]["write_ranges"][0])].decode().strip() == ending.join(block("#", "safe"))
    assert certainty_at(snapshot, ADAPTERS["yaml"](snapshot), "# @beacon[") == "literal"
    assert nmc._beacon_block_extent_for_language("yaml", rows, {"comment_line": 2}) is None
    assert [b["id"] for b in nmc.parse_yaml_beacon_blocks(rows)] == ["safe"]


@pytest.mark.parametrize("indicator,indent", [(">", "  "), (">-", "    "), ("|- # keep", "  "), (">+2", "  ")])
def test_yaml_folded_and_chomped_block_scalars(indicator, indent):
    """Before §6.8: a folded body supplies a live, writable block."""
    rows = ["sample: " + indicator, *block("#", "example", indent), "next: true", *block("#", "safe")]
    snapshot, records = scan("yaml", rows)
    assert live_ids(records) == ["safe"], indicator
    assert certainty_at(snapshot, ADAPTERS["yaml"](snapshot), "# @beacon[") == "literal"
    assert records[0]["write_ranges"]


def test_yaml_no_body_and_plain_scalars_are_not_block_scalars():
    """An indicator with no real body, and a | inside a plain scalar, stay text."""
    rows = ["sample: |", "next: true", *block("#", "safe")]
    snapshot, records = scan("yaml", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    assert live_at(snapshot, ADAPTERS["yaml"](snapshot), "# @beacon[")
    rows = ["formula: a|b |> c", "title: it's fine", *block("#", "safe")]
    snapshot, records = scan("yaml", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    assert live_at(snapshot, ADAPTERS["yaml"](snapshot), "# @beacon[")
    rows = ["- - |", *block("#", "example", "    "), "next: true", *block("#", "safe")]
    snapshot, records = scan("yaml", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]


def test_yaml_quoting_comment_and_unterminated_tail():
    """Quoted scalars are literal; unterminated quotes are locally uncertain."""
    rows = ["key: '", *block("#", "example"), "'", *block("#", "safe")]
    snapshot, records = scan("yaml", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    rows = ['key: "a # not a comment"', *block("#", "safe")]
    snapshot, records = scan("yaml", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    rows = ["key: 'it''s", *block("#", "example"), "'", *block("#", "safe")]
    snapshot, records = scan("yaml", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    rows = ["# it's a comment", *block("#", "safe")]
    snapshot, records = scan("yaml", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    rows = [*block("#", "safe"), "key: 'unterminated", *block("#", "maybe")]
    snapshot, records = scan("yaml", rows)
    assert live_ids(records) == ["safe", "maybe"]
    assert records[0]["write_ranges"]
    assert records[1]["literal_certainty"] == "uncertain" and records[1]["write_ranges"] is None


@pytest.mark.parametrize("delimiter", ["123", "$END", "'E\\'", "E\"N\"D"])
def test_shell_all_delimiter_words_preserve_body_and_safe_neighbour(delimiter):
    """The early adapter missed legal numeric/punctuated and single-quote words."""
    word = {"'E\\'": "E\\", 'E"N"D': "END"}.get(delimiter, delimiter)
    rows = ["cat <<" + delimiter, *block("#", "example"), word, *block("#", "safe")]
    _, records = scan("shell", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]


@pytest.mark.parametrize("header,content_indent", [("sample: |2", "  "), ("- - |2", "    "), ("- key: |2", "    ")])
def test_yaml_explicit_indent_is_not_inferred_from_extra_indented_first_row(header, content_indent):
    """Early adapter ended the scalar when extra first-row indentation decreased."""
    rows = [header, content_indent + "  extra indent", *block("#", "example", content_indent), "next: true", *block("#", "safe")]
    _, records = scan("yaml", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]


def test_yaml_single_backslash_and_flow_quotes_do_not_disable_safe_comments():
    """YAML single quotes do not backslash-escape their closing quote."""
    rows = ["path: 'C:\\'", *block("#", "safe")]
    _, records = scan("yaml", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    rows = ["items: ['", *block("#", "example"), "', 2]", *block("#", "safe")]
    _, records = scan("yaml", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]
    # Punctuation inside a plain scalar must not be mistaken for flow syntax.
    rows = ["text: an ordinary [ ' punctuation", *block("#", "safe")]
    _, records = scan("yaml", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]


@pytest.mark.parametrize("properties", ["!!str", "&capture", "!<tag:example.org,2026:literal> &capture"])
def test_yaml_scalar_properties_do_not_turn_literal_bodies_into_live_comments(properties):
    """Before this correction a tag/anchor before the quote or | hid its node position."""
    for rows in [
        ["sample: " + properties + " '", *block("#", "example"), "'", *block("#", "safe")],
        ["sample: " + properties + " |2", *block("#", "example", "  "), "next: true", *block("#", "safe")],
        ["items: [" + properties + " '", *block("#", "example"), "', 2]", *block("#", "safe")],
    ]:
        _, records = scan("yaml", rows)
        assert live_ids(records) == ["safe"] and records[0]["write_ranges"]


@pytest.mark.parametrize("header,delimiter", [(['cat <<"E\\\"F"'], 'E"F'), (["cat <<EO\\", "F"], "EOF")])
def test_shell_heredoc_quote_removal_and_line_continuation_preserve_neighbour(header, delimiter):
    """Before correction escaped quote/newline bytes were retained in the delimiter."""
    rows = [*header, *block("#", "example"), delimiter, *block("#", "safe")]
    _, records = scan("shell", rows)
    assert live_ids(records) == ["safe"] and records[0]["write_ranges"]


ATOMS = ["--", "#", "/*", "*/", "'", '"', "`", "$", "$$", "$tag$", "$((", "))", "$(", ")",
         "<<", "<<-", "<<<", "EOF", "PY", "|", ">", "|-", ": ", "- ", "  ", "\\", "\\'",
         "@beacon[", "@beacon-close[", "]", "id=x,", "kind=span,", "it's", "a$b$c",
         "# @beacon[", "-- @beacon[", "\t"]


def test_adapters_are_total_and_bounded():
    """No language lexical adapter may raise, gate or escape the snapshot."""
    rng = random.Random(20261019)
    for _ in range(400):
        language = rng.choice(list(ADAPTERS))
        rows = [rng.choice(ATOMS) for _ in range(rng.randrange(12))]
        ending = rng.choice(["\n", "\r\n", "\r"])
        raw = (ending.join(rows) + ending).encode()
        if rng.choice([False, True]):
            raw = b"\xef\xbb\xbf" + raw
        snapshot = source.source_snapshot_from_bytes(raw)
        evidence = ADAPTERS[language](snapshot)
        last = -1
        for row in evidence:
            assert row["certainty"] in {"literal", "uncertain", "comment"}
            assert 0 <= row["start"] < row["end"] <= len(snapshot.raw_bytes)
            assert row["start"] >= last
            last = row["start"]
        records = nmc._scan_source_beacons(snapshot, language)
        assert nmc._scan_source_beacons(snapshot, language) == records
        for record in records:
            if record["literal_certainty"] == "uncertain":
                assert record["write_ranges"] is None and record["line_extent"] is None
        assert source.scan_beacon_structure(snapshot, language) is not None
