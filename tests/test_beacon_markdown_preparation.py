"""U08 pure generation witnesses; fixture source is always quoted physical rows."""
import builtins
import json

import pytest

import markdown_roundtrip as mdr
import nexus_map_codebase as source
from beacon_source import source_snapshot_from_bytes
from beacon_edits import verify_beacon_edit_plan


def note(bid, comment="own", *, path=None, kind="ast", show_span=None):
    rows = (["MD_PATH:", *path, "---"] if path else []) + [
        "BEACON (MD AST)", f"id: {bid}", f"kind: {kind}", f"comment: {comment}"]
    if show_span is not None:
        rows.append(f"show_span: {show_span}")
    return "\n".join([*rows, "---"])


def block(bid, label, comment="own", *, kind="ast", ending="\n"):
    rows = ["<!--", "@beacon[", f"  id={bid},", f"  slice_labels={label},",
            f"  kind={kind},", f"  comment={comment},", "]", "-->"]
    return (ending.join(rows) + ending).encode()


def heading(name, bid=None, *, comment="own", children=(), priority=100, node_id=None):
    return {"id": node_id or name, "name": name, "priority": priority,
            "note": note(bid, comment) if bid else "", "children": list(children)}


def prepare(raw, children, *, previous=b"old\n", path="fixture.md"):
    return mdr.prepare_generated_markdown_beacons(raw, {"children": children},
        source_snapshot_from_bytes(previous, str(path)))


def attachments(raw):
    snapshot = source_snapshot_from_bytes(raw, "fixture.md")
    structures = source._beacon_selection_structures(snapshot, "markdown")
    index = source.beacon_attachment_index(snapshot, structures["outline"], structures)
    return [(b["record"]["fields"], b["anchor"]["header"] if b["anchor"] else None,
             b["anchor"]["line"] if b["anchor"] else None) for b in index["blocks"]]


def test_three_siblings_count_untagged_heading_before_tagged_candidates():
    children = [heading("Dump Bay"), heading("Dump Bay #second", "second"),
                heading("Dump Bay #third", "third")]
    result = prepare(b"# Dump Bay\n\n# Dump Bay\n\n# Dump Bay\n", children)
    assert result["final_bytes"] == (b"# Dump Bay\n\n" + block("second", "second") +
        b"# Dump Bay\n\n" + block("third", "third") + b"# Dump Bay\n")
    assert [o["occurrence"] for o in result["outcomes"]] == [2, 3]
    assert [o["node_id"] for o in result["outcomes"]] == ["Dump Bay #second", "Dump Bay #third"]


def test_nested_repeated_paths_follow_generator_priority_not_path_sort():
    children = [heading("Parent", children=[heading("Child #later", "c", priority=20),
                                           heading("Child", priority=10)]),
                heading("Parent", children=[heading("Child #last", "d")])]
    result = prepare(b"# Parent\n\n## Child\n\n## Child\n\n# Parent\n\n## Child\n", children)
    assert result["final_bytes"] == (b"# Parent\n\n## Child\n\n" + block("c", "later") +
        b"## Child\n\n# Parent\n\n" + block("d", "last") + b"## Child\n")
    assert [o["occurrence"] for o in result["outcomes"]] == [2, 3]


def test_occurrences_use_the_selectors_path_identity_not_raw_title_spelling():
    result = prepare(b"# A\n\n# a\n\n# A\n", [heading("A"), heading("a #second", "second"),
        heading("A #third", "third")])
    assert result["final_bytes"] == (b"# A\n\n" + block("second", "second") + b"# a\n\n" +
                                     block("third", "third") + b"# A\n")
    assert [o["occurrence"] for o in result["outcomes"]] == [2, 3]


def test_equal_note_ids_keep_both_own_labels_comment_fields_and_duplicate_choice():
    children = [heading("Alpha #first", "shared", comment="alpha"),
                heading("Beta #second", "shared", comment="beta")]
    children[1]["note"] = note("shared", "beta", show_span="false")
    result = prepare(b"# Alpha\n\n# Beta\n", children)
    rows = attachments(result["final_bytes"])
    assert [(f["id"], f["slice_labels"], f["comment"], h) for f, h, _ in rows] == [
        ("shared", "first", "alpha", "# Alpha"), ("shared", "second", "beta", "# Beta")]
    assert rows[1][0]["show_span"] == "false"
    assert b"--dup" not in result["final_bytes"]
    finding, = [f for f in result["findings"] if f["code"] == "duplicate_id"]
    assert {r["resolution"] for r in finding["fix"]["resolutions"]} == {"assign_ids", "keep"}
    assert len(finding["members"]) == 2
    assert all(o["success"] for o in result["outcomes"])


def test_missing_heading_is_local_and_names_node_file_id_with_safe_neighbour():
    result = prepare(b"# Good\n", [heading("Missing #bad", "missing", node_id="missing-node"),
        heading("Good #good", "good", node_id="good-node")])
    bad, good = result["outcomes"]
    assert not bad["success"] and bad["node_id"] == "missing-node"
    assert bad["file_path"] == "fixture.md" and bad["beacon_id"] == "missing"
    assert bad["error_kind"] == "no_beacon_anchor_for_node"
    assert good["success"] and attachments(result["final_bytes"])[0][0]["id"] == "good"
    assert result["omissions"][0]["id"] == "missing"


def test_missing_duplicate_occurrence_cannot_redirect_to_the_one_remaining_heading():
    result = prepare(b"# Same\n", [heading("Same"), heading("Same #second", "second"),
                                     heading("Good #good", "good")])
    assert not result["outcomes"][0]["success"]
    assert b"@beacon" not in result["final_bytes"]
    # Independent positive control in the same generated file.
    control = prepare(b"# Same\n\n# Good\n", [heading("Same"), heading("Same #second", "second"),
        heading("Good #good", "good")])
    assert not control["outcomes"][0]["success"] and control["outcomes"][1]["success"]
    assert attachments(control["final_bytes"])[0][0]["id"] == "good"


@pytest.mark.parametrize("ids", [("one", "two"), ("shared", "shared")])
def test_several_records_on_one_heading_remain_selectable_and_neighbour_works(ids):
    node = heading("Group #group")
    node["note"] = note(ids[0], "first") + "\n" + note(ids[1], "second")
    result = prepare(b"# Group\n\n# Good\n", [node, heading("Good #good", "good")])
    rows = attachments(result["final_bytes"])
    assert [(f["id"], f["comment"], h) for f, h, _ in rows] == [
        (ids[0], "first", "# Group"), (ids[1], "second", "# Group"), ("good", "own", "# Good")]
    finding, = [f for f in result["findings"] if f["code"] == "two_blocks_one_symbol"]
    assert [m["fields"]["comment"] for m in finding["members"]] == ["first", "second"]
    assert result["outcomes"][0]["source_records"][1]["comment"] == "second"
    assert all(o["success"] for o in result["outcomes"])


@pytest.mark.parametrize("damage", [
    ["<!--", "@beacon[", " id=broken,", " kind=ast,", "-->", "# Bad"],
    ["<!--", "@beacon-close[", " id=broken,", "-->", "# Bad"],
    ["]", "# Bad"],
])
def test_corrupt_metadata_never_stops_other_heading(damage):
    raw = ("\n".join([*damage, "", "# Good", ""]) ).encode()
    result = prepare(raw, [heading("Bad #bad", "broken"), heading("Good #good", "good")])
    good = result["outcomes"][1]
    assert good["success"]
    assert any(f["id"] == "good" and h == "# Good" for f, h, _ in attachments(result["final_bytes"]))
    assert b"# Bad\n" in result["final_bytes"]


def test_c0_parse_view_selects_own_heading_without_rewriting_input_bytes():
    result = prepare(b"\x07\n" + block("a", "a") + b"# A\n\n# B\n", [heading("A #a", "a"), heading("B #b", "b")])
    assert result["final_bytes"].startswith(b"\x07\n" + block("a", "a"))
    assert [h for _, h, _ in attachments(result["final_bytes"])] == ["# A", "# B"]


@pytest.mark.parametrize("raw,expected", [
    (b"# Heading\n\n\n", b"# Heading\n"),  # E01
    ("# Heading\nA\u2028B\n".encode(), "# Heading\nA\u2028B\n".encode()),  # E02
    ("# Head\x85ing\n".encode(), "# Head\x85ing\n".encode()),  # E03
    ("# Head\u2029ing\n".encode(), "# Head\u2029ing\n".encode()),  # E04
    (b"# Heading", b"# Heading\n"),  # E05
    (b"# Heading\r\n\r\n\r\n", b"# Heading\r\n"),  # E06
    (b"# Heading\n \t\n\n", b"# Heading\n"),  # E07
    (b"# Heading\r\n", b"# Heading\r\n"),  # E08
    (b"# Heading\r\r", b"# Heading\r"),
    (b" \r\n\t\r\n", b""),
])
def test_eof_e01_e08_changes_only_physical_generation_tail(raw, expected):
    result = prepare(raw, [])
    assert result["final_bytes"] == expected
    assert verify_beacon_edit_plan(result["publication_plan"])["candidate_bytes"] == expected


def test_bom_crlf_and_clean_golden_are_exact_and_preparation_is_pure(tmp_path, monkeypatch):
    path = tmp_path / "source.md"
    previous = b"\xef\xbb\xbf# Old\r\n"
    path.write_bytes(previous)
    before = path.stat().st_mtime_ns
    snapshot = source_snapshot_from_bytes(previous, str(path))
    forest = {"children": [heading("Clean #clean", "clean", comment="clean comment")]}
    expected = b"\xef\xbb\xbf" + block("clean", "clean", "clean comment", ending="\r\n") + b"# Clean\r\n"
    with monkeypatch.context() as ctx:
        ctx.setattr(builtins, "open", lambda *a, **kw: pytest.fail("pure preparation performed IO"))
        ctx.setattr(source, "read_source_snapshot", lambda *a, **kw: pytest.fail("source reopened"))
        result = mdr.prepare_generated_markdown_beacons(b"# Clean\r\n\r\n", forest, snapshot)
    assert result["final_bytes"] == expected
    assert result["publication_plan"].snapshot is snapshot
    assert result["publication_policy"]["had_bom"] is True
    assert path.read_bytes() == previous and path.stat().st_mtime_ns == before
    repeated = mdr.prepare_generated_markdown_beacons(b"# Clean\r\n\r\n", forest, snapshot)
    assert result == repeated
    json.dumps({k: v for k, v in result.items() if k not in {"publication_plan", "final_bytes"}})


def test_minted_ids_include_supplied_later_and_planned_ids_and_are_repeatable(monkeypatch):
    calls = []
    allocate = source.allocate_beacon_id
    def collision(taken, **kwargs):
        calls.append(set(taken))
        return allocate(taken, base_id="same")
    monkeypatch.setattr(source, "allocate_beacon_id", collision)
    children = [heading("A #a"), heading("B #b"), heading("C #c", "same")]
    result = prepare(b"# A\n\n# B\n\n# C\n", children)
    assert [f["id"] for f, _, _ in attachments(result["final_bytes"])] == ["same-2", "same-3", "same"]
    assert all("same" in taken for taken in calls)
    assert result == prepare(b"# A\n\n# B\n\n# C\n", children)


def test_m02_detached_ast_relocation_reports_original_metadata_and_target():
    previous = block("h-a", "old", "detached") + b"ordinary prose\n\n# A\n\n# Good\n"
    snapshot = source_snapshot_from_bytes(previous, "m02.md")
    forest = {"children": source.parse_markdown_structure("m02.md", snapshot=snapshot)}
    result = mdr.prepare_generated_markdown_beacons(b"ordinary prose\n\n# A\n\n# Good\n", forest, snapshot)
    row, = result["relocations"]
    assert row["id"] == "h-a" and row["original_metadata"]["fields"]["comment"] == "detached"
    assert row["original_position"]["intervening_prose_lines"]
    assert row["final_position"]["intervening_prose_lines"] == []
    assert row["target"]["md_path"] == ["# A"] and row["available_actions"]
    assert b"ordinary prose\n" in result["final_bytes"]


@pytest.mark.parametrize("case,kind,reason", [
    ("m04", "ast", "demoted_ast_not_re_emitted"),
    ("m05", None, "implicit_span_not_re_emitted"),
    ("m10", "span", "reverse_markdown_span_not_supported"),
])
def test_m04_m05_m10_omissions_retain_source_metadata_and_safe_heading(case, kind, reason):
    rows = ["# A", "", "<!--", "@beacon[", " id=h-x,", " slice_labels=lost,", " comment=original,"]
    if kind:
        rows.append(f" kind={kind},")
    previous = ("\n".join([*rows, "]", "-->", "ordinary prose", ""]) ).encode()
    snapshot = source_snapshot_from_bytes(previous, case + ".md")
    forest = {"children": source.parse_markdown_structure(case + ".md", snapshot=snapshot)}
    forest["children"].append(heading("Good #good", "good"))
    result = mdr.prepare_generated_markdown_beacons(b"# A\n\nordinary prose\n\n# Good\n", forest, snapshot)
    row, = result["omissions"]
    assert row["id"] == "h-x" and row["reason"] == reason and row["file"] == case + ".md"
    assert row["original_metadata"]["fields"]["slice_labels"] == "lost"
    assert row["original_metadata"]["fields"]["comment"] == "original"
    assert row["node_id"] is None  # parser's synthetic span need not have a Workflowy UUID
    assert row["available_actions"] and row["owning_heading"]
    assert attachments(result["final_bytes"])[0][0]["id"] == "good"


def test_untagged_source_header_is_an_explicit_omission_not_a_dropped_record():
    previous = block("removed", "old") + b"# A\n"
    child = heading("A", "removed")
    result = prepare(b"# A\n", [child], previous=previous)
    row, = result["omissions"]
    assert row["reason"] == "heading_tags_removed" and row["node_id"] == "A"
    assert result["final_bytes"] == b"# A\n"


def test_analysis_failure_is_data_and_does_not_veto_independent_heading(monkeypatch):
    import beacon_findings
    monkeypatch.setattr(beacon_findings, "analyze_beacon_snapshot", lambda *a, **kw: (_ for _ in ()).throw(ValueError("analysis")))
    result = prepare(b"# Good\n", [heading("Good #good", "good")])
    assert result["outcomes"][0]["success"]
    assert result["analysis_error"]["exception"] == "ValueError: analysis"
    assert attachments(result["final_bytes"])[0][0]["id"] == "good"
