"""Remediation-2: genuine positional records, never examples in note bodies."""
import hashlib
import json
from pathlib import Path

import pytest

import beacon_mutation_contract as bmc
import beacon_obtain_code_snippet as snippets
from beacon_source import read_source_snapshot, source_snapshot_from_bytes
import markdown_roundtrip as mdr
import nexus_map_codebase as nmc
from workflowy_mcp.beacon_snippet import snippet_note_identity


def _record(header, line, fields):
    return {"header": header, "line": line, **dict(fields), "occurrences": [
        {"key": k, "value": v, "line": line + i + 1} for i, (k, v) in enumerate(fields)]}


@pytest.mark.parametrize("prefix,header,separator", [
    ([], "BEACON (SPAN)", [""]),
    (["AST_QUALNAME: f"], "BEACON (AST)", [""]),
    (["AST_QUALNAME: f"], "BEACON (JS/TS AST)", [""]),
    (["MD_PATH:", "# Parent", "## Heading", "---", ""], "BEACON (MD AST)", ["---", ""]),
])
def test_genuine_positional_layouts_retain_all_records_and_field_occurrences(prefix, header, separator):
    fields = [("id", "own@one"), ("kind", "span" if "SPAN" in header else "ast"),
              ("role", "own"), ("show_span", "false"), ("slice_labels", "one,two"),
              ("comment", "x > y,"), ("comment", "&lt; once")]
    physical = [f"{k}: {v}" for k, v in fields]
    physical[-2:] = ["comment: x &gt; y,", "comments: &amp;lt; once"]
    first = [header, *physical]
    second = [header, "id: own@two", "kind: ast"]
    rows = prefix + first + separator + second + separator + ["Ordinary body."]
    expected = [_record(header, len(prefix) + 1, fields),
                _record(header, len(prefix + first + separator) + 1, [("id", "own@two"), ("kind", "ast")])]
    assert bmc.parse_beacon_note_metadata("\n".join(rows)) == expected


def test_genuine_markdown_context_section_and_next_record_are_retained():
    rows = ["MD_PATH:", "# A", "---", "BEACON (MD AST)", "id: one", "kind: ast", "",
            "CONTEXT COMMENTS (MD):", "an attached source comment", "---", "",
            "BEACON (MD AST)", "id: two", "kind: ast", "---", "", "Body."]
    assert bmc.parse_beacon_note_metadata("\n".join(rows)) == [
        _record("BEACON (MD AST)", 4, [("id", "one"), ("kind", "ast")]),
        _record("BEACON (MD AST)", 12, [("id", "two"), ("kind", "ast")])]


@pytest.mark.parametrize("prefix,tail", [
    (["AST_QUALNAME: f"], ["", "---"]),
    (["AST_QUALNAME: f"], ["", "CONTEXT COMMENTS (JS/TS):"]),
    ([], ["", "CONTEXT COMMENTS (PYTHON):"]),
    ([], ["", "SPAN TEXT:"]),
    (["MD_PATH:", "# A", "---"], ["---", "", "Intro.", ""]),
    (["MD_PATH:", "# A", "---"], ["", "CONTEXT COMMENTS (MD):"]),
])
def test_body_docstring_span_and_context_lookalikes_never_supply_fields(prefix, tail):
    md = bool(prefix and prefix[0] == "MD_PATH:")
    header = "BEACON (MD AST)" if md else "BEACON (AST)"
    fields = [("id", "own"), ("kind", "ast"), ("comment", "own comment")]
    rows = prefix + [header, *[f"{k}: {v}" for k, v in fields]] + tail + [
        "BEACON (SPAN)", "id: example", "kind: span", "role: example", "show_span: true",
        "comment: example", "slice_labels: example", "---", "", "Body.",
        "BEACON (MD AST)", "id: later"]
    assert bmc.parse_beacon_note_metadata("\n".join(rows)) == [_record(header, len(prefix) + 1, fields)]


@pytest.mark.parametrize("prefix", [
    ["AST_QUALNAME: f", "---"],
    ["MD_PATH:", "# A", "---", "", "```text"],
    ["Ordinary note text", ""],
])
def test_untagged_notes_never_start_metadata_in_body(prefix):
    assert bmc.parse_beacon_note_metadata("\n".join(prefix + [
        "BEACON (MD AST)", "id: example", "kind: ast", "```", ""])) == []


def _walk(node):
    yield node
    for child in node.get("children", []):
        yield from _walk(child)


@pytest.mark.parametrize("tagged,fenced,example_kind", [
    (True, True, "MD AST"), (True, False, "MD AST"), (False, True, "MD AST"),
    (False, True, "MD SPAN"), (True, True, "MD SPAN"),
])
def test_markdown_generation_and_preparation_preserve_example_body_and_descendants(
        tmp_path, tagged, fenced, example_kind):
    body = ["Intro.", ""] + (["```text"] if fenced else []) + [
        f"BEACON ({example_kind})", "id: p@example", "kind: ast", "comment: Example text"] + (
        ["```"] if fenced else []) + ["", "Ordinary prose after the example."]
    beacon = ["<!--", "@beacon[", "id=p@real,", "kind=ast,", "slice_labels=old,",
              "comment=Real heading,", "]", "-->"] if tagged else []
    path = tmp_path / "examples.md"
    raw = ("\n".join(beacon + ["# A", *body, "", "## Child", "Child prose.", ""])).encode()
    path.write_bytes(raw)
    tree = nmc.map_codebase(str(path))
    generated = "\n".join(mdr.nexus_to_tokens(tree, depth=0)).encode()
    result = mdr.prepare_generated_markdown_beacons(generated, tree, read_source_snapshot(path))
    expected_body = "\n".join(body).encode()
    assert expected_body in generated
    assert expected_body in result["final_bytes"]
    assert b"# A\n" in generated and b"## Child\n" in generated
    assert b"Child prose." in result["final_bytes"]
    assert not any(f["code"] == "two_blocks_one_symbol" for f in result["findings"])
    if tagged:
        assert b"id=p@real," in result["final_bytes"]
        assert b"comment=Real heading," in result["final_bytes"]
        assert b"id=p@example," not in result["final_bytes"]
    else:
        assert b"@beacon[" not in result["final_bytes"]
    assert path.read_bytes() == raw


def test_python_span_examples_do_not_change_snippet_identity_or_extraction(tmp_path):
    path = tmp_path / "span.py"
    raw = ("\n".join(["# @beacon[", "# id=p@real,", "# kind=span,", "# slice_labels=old,",
        "# comment=Real span,", "# ]", 'payload = """', "BEACON (SPAN)", "id: p@example",
        "kind: span", "comment: example text", '"""', "# @beacon-close[id=p@real]", ""])).encode()
    path.write_bytes(raw)
    tree = nmc.map_codebase(str(path))
    node = next(n for n in _walk(tree) if n.get("note", "").startswith("BEACON (SPAN)"))
    assert snippet_note_identity(node["note"]) == ("p@real", "span", True)
    assert snippets.get_snippet_data(str(path), "p@real", 0)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("scope", ["unchanged", "lookalikes"])
def test_corpus_notes_match_frozen_08cb610_records_except_named_lookalikes(scope):
    frozen = json.loads((Path(__file__).parent / "beacon_note_metadata_08cb610.json").read_text())
    # Every mapped note, including notes with no records, is pinned to a stable
    # virtual path; no git, external scratch tree or live service is read here.
    changes = json.loads((Path(__file__).parent / "beacon_robustness_expected_changes.json").read_text())
    exceptions = {row["case"]: row for row in changes["changes"]
                  if row["test"] == "test_corpus_notes_match_frozen_08cb610_records_except_named_lookalikes"}
    seen, mismatches = set(), []
    for fixture in frozen["fixtures"]:
        raw = (Path(__file__).parent / "beacon_robustness_corpus" / fixture["fixture"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == fixture["raw_sha256"]
        tree = nmc.map_source_snapshot(source_snapshot_from_bytes(raw, fixture["vpath"]))
        actual_notes, actual_records = [], {}
        def visit(node, indices):
            note = node.get("note") or ""
            index = str(len(actual_notes))
            actual_notes.append([json.dumps(indices, separators=(",", ":")), node.get("name"),
                                 hashlib.sha256(note.encode()).hexdigest()])
            records = bmc.parse_beacon_note_metadata(note)
            key = fixture["fixture"] + ":" + json.dumps(indices, separators=(",", ":"))
            if key in exceptions:
                change = exceptions[key]
                assert fixture["records_by_note"].get(index, []) == change["old_result"]
                if scope == "lookalikes" and records != change["new_result"]:
                    mismatches.append(key)
                seen.add(key)
                records = change["old_result"]
            if records:
                actual_records[index] = records
            for i, child in enumerate(node.get("children", [])):
                visit(child, indices + [i])
        visit(tree, [])
        assert actual_notes == fixture["notes"], fixture["fixture"]
        assert actual_records == fixture["records_by_note"], fixture["fixture"]
    assert seen == set(exceptions)
    assert not mismatches, mismatches
