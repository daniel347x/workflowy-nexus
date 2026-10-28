"""U04 local hygiene: pure decisions, shared attachment and byte-local merges.

All source fixtures are quoted rows. Baseline ab0653d chose distinct ids,
rebuilt top members, lost fields, and had no group-local outcome surface.
"""
import ast
import hashlib
import json
from pathlib import Path

import pytest

import nexus_map_codebase as nmc
from beacon_source import read_source_snapshot, source_snapshot_from_bytes


def rows_bytes(rows, ending="\n"):
    return (ending.join(rows) + ending).encode("utf-8")


def block(language="python", bid="same@f", labels="a", comment="First.", extra=(), form="line"):
    fields = [f"id={bid},", f"slice_labels={labels},", "kind=ast,", *extra]
    if comment:
        fields.append(f"comment={comment},")
    if language == "markdown":
        return ["<!--", "@beacon[", *("  " + f for f in fields), "]", "-->"]
    if form == "jsdoc":
        return ["/**", " * @beacon[", *(" *   " + f for f in fields), " * ]", " */"]
    if form == "bare":
        return ["/* @beacon[", *("   " + f for f in fields), "] */"]
    prefix = "//" if language in {"js_ts", "vue"} else "#"
    return [f"{prefix} @beacon[", *(f"{prefix}   {f}" for f in fields), f"{prefix} ]"]


def declaration(language, name):
    return [f"# {name}"] if language == "markdown" else (
        [f"def {name}():", "    return 1"] if language == "python" else [f"function {name}() {{ return 1; }}"])


def control(language="python"):
    before = rows_bytes(block(language, "control", "control-a") +
                        block(language, "control", "control-b", "Second.") + declaration(language, "control"))
    after = rows_bytes(block(language, "control", "control-a,control-b", "First. | Second.") +
                       declaration(language, "control"))
    return before, after


def inputs(snapshot, language):
    if language == "vue":
        pairs = nmc._vue_section_records(snapshot)
        records, sections = [r for _s, rs in pairs for r in rs], [s for s, _rs in pairs]
    else:
        records, sections = nmc._beacon_edit_records(snapshot, language), None
    structures = {"language": language, "records": records, "sections": sections}
    index = nmc.beacon_attachment_index(snapshot, nmc._beacon_attachment_outline(snapshot, language), structures)
    return structures, index


def assert_outside_edits(before, after, result):
    cursor = new_cursor = 0
    for edit in sorted(result["edited_ranges"], key=lambda e: (e["start_byte"], e["end_byte"])):
        count = edit["start_byte"] - cursor
        assert before[cursor:edit["start_byte"]] == after[new_cursor:new_cursor + count]
        cursor = edit["end_byte"]
        new_cursor += count + edit["replacement_bytes"]
    assert before[cursor:] == after[new_cursor:]


def test_planner_is_pure_and_only_splices_lowest_member(tmp_path, monkeypatch):
    """ab0653d has no pure planner; its writer replaces the top whole block."""
    raw = rows_bytes(block() + ["# ordinary comment"] + block(labels="b", comment="Second.") + declaration("python", "f"))
    path = tmp_path / "pure.py"
    path.write_bytes(raw)
    snapshot = read_source_snapshot(path)
    structures, index = inputs(snapshot, "python")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("pure planner performed IO or rebuilt an outline")
    monkeypatch.setattr(nmc, "read_source_snapshot", forbidden)
    monkeypatch.setattr(nmc, "commit_beacon_edit_plan", forbidden)
    monkeypatch.setattr(nmc, "_beacon_attachment_outline", forbidden)
    before = path.read_bytes(), path.stat().st_mtime_ns
    plan = nmc.plan_beacon_hygiene(snapshot, structures, index)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not plan["skipped_groups"] and len(plan["applied_groups"]) == 1
    survivor = structures["records"][-1]
    edits = plan["edits"]
    assert all(e.owner == survivor["opener_range"][0] for e in edits if e.replacement)
    assert all(e.start_byte > survivor["opener_range"][1] for e in edits if e.replacement)
    assert plan["applied_groups"][0]["dropped_ids"] == ["same@f"]


@pytest.mark.parametrize("language,suffix,form", [("python", ".py", "line"), ("js_ts", ".ts", "line"),
    ("js_ts", ".js", "bare"), ("js_ts", ".js", "jsdoc"), ("markdown", ".md", "line"), ("vue", ".vue", "jsdoc")])
def test_merge_preserves_fields_form_and_writer_attachment(tmp_path, language, suffix, form):
    """ab0653d drops show_span/snippets/opaque fields and renders // in JSDoc."""
    extra = ["role=describes f,", "show_span=true,", 'start_snippet="return 1",', 'end_snippet="return 1",', "owner=dan,"]
    first = block(language, extra=extra, form=form)
    last = block(language, labels="b,a", comment='"Second,]"', extra=extra, form=form)
    gap = ["<!-- ordinary -->"] if language == "markdown" else ["// ordinary"] if language != "python" else ["# ordinary"]
    prefix, suffix_bytes = (b"<script>\n", b"</script>\n") if language == "vue" else (b"", b"")
    raw = prefix + rows_bytes(first + gap + last + declaration(language, "f")) + suffix_bytes
    path = tmp_path / ("fields" + suffix)
    path.write_bytes(raw)
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result["success"] and result["changed"], result
    assert result["merged_groups"] == result["removed_blocks"] == 1
    expected_last = rows_bytes(last).replace(b"slice_labels=b,a,", b"slice_labels=a,b,").replace(
        b'comment="Second,]",', b'comment="First. | Second,]",')
    assert path.read_bytes() == prefix + rows_bytes(gap) + expected_last + rows_bytes(declaration(language, "f")) + suffix_bytes
    assert_outside_edits(raw, path.read_bytes(), result)
    snapshot = read_source_snapshot(path)
    structures, index = inputs(snapshot, language)
    attached = [b for b in index["blocks"] if b["anchor"]]
    assert len(attached) == 1 and attached[0]["record"]["fields"]["show_span"] == "true"
    lines, beacons = nmc._parse_snapshot_beacons_for_language(snapshot, language)
    assert len(nmc._find_anchor_matched_beacons(language, lines, beacons, attached[0]["anchor"]["line"] - 1)) == 1
    event = result["events"][0]
    assert event["survivor"]["line"] > event["members"][0]["line"]
    assert event["before_sha256"] == hashlib.sha256(raw).hexdigest()
    assert event["after_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert event["retained_ids"] == event["dropped_ids"] == ["same@f"]
    json.dumps(result)  # outcome data never leaks dataclasses/AST nodes/bytes


@pytest.mark.parametrize("extra_a,extra_b,reason", [
    (["role=one,"], ["role=two,"], "conflicting_fields"),
    (["role=one,"], [], "conflicting_fields"),
    (['start_snippet="one",'], ['start_snippet="two",'], "conflicting_fields"),
    (['end_snippet="one",'], ['end_snippet="two",'], "conflicting_fields"),
    (["show_span=true,"], ["show_span=false,"], "conflicting_fields"),
    (["show_span=true,"], [], "conflicting_fields"),
    (["show_span=unknown,"], ["show_span=true,"], "conflicting_fields"),
    (["show_span=unknown,"], ["show_span=different,"], "conflicting_fields"),
    (["owner=dan,"], ["owner=other,"], "conflicting_fields"),
    (["owner=dan,"], [], "conflicting_fields"),
    (["comments=ambiguous,"], [], "conflicting_fields"),
    (["comment=duplicate,"], [], "conflicting_fields"),
    ([], ["comment=contains /* delimiter,"], "conflicting_fields"),
])
def test_conflict_skips_only_its_group_with_safe_neighbour(tmp_path, extra_a, extra_b, reason):
    """ab0653d joins conflicting roles, drops fields or leaves U02 to veto all."""
    bad = rows_bytes(block(extra=extra_a) + block(labels="b", comment="Second.", extra=extra_b) + declaration("python", "f"))
    safe_before, safe_after = control()
    raw = bad + safe_before
    path = tmp_path / "conflicts.py"
    path.write_bytes(raw)
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result["success"] and result["merged_groups"] == 1, result
    assert path.read_bytes() == bad + safe_after
    assert result["skipped_groups"][0]["reason"] == reason
    assert len(result["skipped_groups"][0]["candidates"]) == 2
    assert_outside_edits(raw, path.read_bytes(), result)


def test_invalid_comment_group_does_not_block_valid_group(tmp_path):
    bad = rows_bytes(block(comment="A /* literal") + block(labels="b", comment="B") + declaration("python", "f"))
    safe_before, safe_after = control()
    path = tmp_path / "invalid.py"
    path.write_bytes(bad + safe_before)
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result["success"] and path.read_bytes() == bad + safe_after
    assert result["skipped_groups"][0]["reason"] == "invalid_merged_comment"


@pytest.mark.parametrize("damage", ["missing_bracket", "missing_id", "nested", "junk", "unknown_kind"])
def test_mixed_file_bad_distinct_valid_and_source_are_local(tmp_path, damage):
    broken = block(bid="bad", labels="bad")
    if damage == "missing_bracket":
        broken.pop()
    elif damage == "missing_id":
        broken.pop(1)
    elif damage == "nested":
        broken = broken[:-1] + block(bid="inner")
    elif damage == "junk":
        broken[0] += " unexplained junk"
    else:
        broken = [s.replace("kind=ast", "kind=unknown") for s in broken]
    damaged = rows_bytes(broken + block(bid="other", labels="other") + declaration("python", "broken"))
    distinct = rows_bytes(block(bid="first@00001") + block(bid="second") + declaration("python", "distinct"))
    safe_before, safe_after = control()
    ordinary = rows_bytes(['UNICODE = "π💎\f\x85\u2028\u2029"', "# ordinary footer"])
    raw = b"\xef\xbb\xbf" + damaged + distinct + ordinary + safe_before
    path = tmp_path / "mixed.py"
    path.write_bytes(raw)
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result["success"] and result["merged_groups"] == 1, result
    assert path.read_bytes() == b"\xef\xbb\xbf" + damaged + distinct + ordinary + safe_after
    assert any(g["reason"] == "distinct_ids" for g in result["skipped_groups"])
    assert_outside_edits(raw, path.read_bytes(), result)
    for group in result["skipped_groups"]:
        assert group["candidates"] == group["members"]
        for member in group["members"]:
            assert member["block_hash"] and member["read_range"] and member["opener_range"]


def test_same_id_multiline_comment_union_keeps_survivor_layout(tmp_path):
    first = block(comment="One")
    first[-2] = first[-2].replace("comment=One,", "comment=One")
    first.insert(-1, "#     continued,")
    last = [r.replace("#", "##", 1) for r in block(labels="b", comment="Two")]
    raw = rows_bytes(first + last + declaration("python", "f"), "\r")
    path = tmp_path / "multiline.py"
    path.write_bytes(raw)
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result["success"] and result["changed"], result
    assert path.read_bytes() == rows_bytes(last + declaration("python", "f"), "\r").replace(
        b"slice_labels=b,", b"slice_labels=a,b,").replace(b"comment=Two,", b"comment=One continued | Two,")


def test_hygiene_has_one_production_authority_and_retired_names_are_gone():
    """ab0653d has parallel private parsers, next-line grouping and row bridge."""
    tree = ast.parse(Path(nmc.__file__).read_text(encoding="utf-8"))
    funcs = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    retired = {"_parse_blocks", "_next_anchor_after", "_next_heading_anchor_after", "_block_extent",
               "_is_numeric_suffix_beacon_id", "_looks_like_js_beacon_opener", "_strip_js_comment_sugar", "_commit_beacon_row_changes",
               "_extract_prefixed_body", "_extract_hash_body", "_extract_dashdash_body", "_extract_slashslash_body",
               "_extract_cblock_body", "_extract_plain_body", "_replace_comments_key"}
    assert not retired & {f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}
    for suffix in ("python", "js_ts", "markdown"):
        fn = funcs["_normalize_duplicate_ast_beacons_" + suffix]
        calls = [ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)]
        assert calls.count("_normalize_duplicate_ast_beacons") == 1
        assert not any(n in calls for n in ("commit_beacon_edit_plan", "_beacon_edit_records", "beacon_attachment_index"))
    adapter = funcs["_normalize_duplicate_ast_beacons"]
    calls = [ast.unparse(c.func) for c in ast.walk(adapter) if isinstance(c, ast.Call)]
    for name in ("read_source_snapshot", "plan_beacon_hygiene", "beacon_attachment_index", "commit_beacon_edit_plan"):
        assert calls.count(name) == 1
    assert "plan_beacon_hygiene" not in __import__("test_beacon_check_engine")._FORBIDDEN
    alias = funcs["normalize_beacon_comment_field_aliases_in_file"]
    names = {n.id for n in ast.walk(alias) if isinstance(n, ast.Name)}
    assert not names & {"mode", "pending_html_comment"}
    calls = [ast.unparse(c.func) for c in ast.walk(alias) if isinstance(c, ast.Call)]
    assert calls.count("plan_beacon_comment_aliases") == calls.count("read_source_snapshot") == calls.count("commit_beacon_edit_plan") == 1
    assert "_beacon_edit_records" not in calls  # publication callback only; selection is the pure planner's
    assert "plan_beacon_comment_aliases" not in __import__("test_beacon_check_engine")._FORBIDDEN


def test_mixed_jsdoc_keeps_ordinary_comment_bytes(tmp_path):
    """ab0653d re-renders // inside a shared JSDoc and fails the U02 proof."""
    first = block("js_ts", form="jsdoc")
    first.insert(1, " * Ordinary documentation for f.")
    last = block("js_ts", labels="b", comment="Second.", form="jsdoc")
    raw = rows_bytes(first + last + declaration("js_ts", "f"))
    path = tmp_path / "mixed.js"
    path.write_bytes(raw)
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result["success"] and result["changed"], result
    assert b" * Ordinary documentation for f.\n" in path.read_bytes()
    assert b"//" not in path.read_bytes()
    assert_outside_edits(raw, path.read_bytes(), result)


def test_implicit_kind_and_explicit_span_stacks_are_not_merge_operands(tmp_path):
    implicit = [r for r in block(bid="implicit") if "kind=" not in r]
    span = [r.replace("kind=ast", "kind=span") for r in block(bid="span")]
    untouched = rows_bytes(implicit + implicit + span + block(bid="span") + declaration("python", "f"))
    safe_before, safe_after = control()
    path = tmp_path / "kinds.py"
    path.write_bytes(untouched + safe_before)
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result["success"] and result["merged_groups"] == 1, result
    assert path.read_bytes() == untouched + safe_after
    assert len(result["events"]) == 1 and result["events"][0]["retained_ids"] == ["control"]


def test_wrong_after_state_rejects_only_candidate_and_other_group_commits(tmp_path, monkeypatch):
    """ab0653d never delegates its renderer or isolates a bad candidate plan."""
    from beacon_edits import BeaconEdit
    bad = rows_bytes(block(bid="bad") + block(bid="bad", labels="b") + declaration("python", "bad"))
    safe_before, safe_after = control()
    path = tmp_path / "proof.py"
    path.write_bytes(bad + safe_before)
    render = nmc._build_beacon_block_for_language
    def wrong(language, **kwargs):
        record = kwargs.get("record")
        if record and record["fields"].get("id") == "bad":
            a, b = record["field_ranges"][0]
            return [BeaconEdit(a, b, b"id=WRONG,", record["opener_range"][0])]
        return render(language, **kwargs)
    monkeypatch.setattr(nmc, "_build_beacon_block_for_language", wrong)
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result["success"] and result["merged_groups"] == 1, result
    assert path.read_bytes() == bad + safe_after
    assert result["skipped_groups"][0]["reason"] == "plan_mismatch"
    assert result["skipped_groups"][0]["proof"]["error_kind"] == "plan_mismatch"


def test_accepted_parsed_comment_decoration_is_not_dropped_or_a_merge_gate(tmp_path):
    """ab0653d strips decoration out of parsed comments while coalescing."""
    last = block(labels="b", comment="Second")
    raw = rows_bytes(block(comment="💎 First") + last + declaration("python", "f"))
    path = tmp_path / "decoration.py"
    path.write_bytes(raw)
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result["success"] and result["changed"] and not result["skipped_groups"], result
    assert path.read_bytes() == rows_bytes(last + declaration("python", "f")).replace(
        b"slice_labels=b,", b"slice_labels=a,b,").replace(b"comment=Second,", "comment=💎 First | Second,".encode())


def alias_rows(bid="alias", text="Alias."):
    return [r.replace("comment=", "comments=") for r in block(bid=bid, comment=text)]


def test_alias_planner_is_pure_and_uses_occurrence_byte_ranges(tmp_path, monkeypatch):
    """ab0653d has no pure alias selection; U05 must not invoke its publisher."""
    raw = b"\xef\xbb\xbf" + rows_bytes(['TEXT = "π💎"', *alias_rows(), *declaration("python", "f")], "\r\n")
    path = tmp_path / "pure_alias.py"
    path.write_bytes(raw)
    snapshot = read_source_snapshot(path)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("alias planner performed IO or built an outline")
    monkeypatch.setattr(nmc, "read_source_snapshot", forbidden)
    monkeypatch.setattr(nmc, "commit_beacon_edit_plan", forbidden)
    monkeypatch.setattr(nmc, "_beacon_attachment_outline", forbidden)
    before = path.read_bytes(), path.stat().st_mtime_ns
    planned = nmc.plan_beacon_comment_aliases(snapshot, "python")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert len(planned["edits"]) == len(planned["applied_aliases"]) == 1
    edit, row = planned["edits"][0], planned["applied_aliases"][0]
    assert edit.start_byte == raw.index(b"comments=") + len("comment")
    assert edit.end_byte == edit.start_byte + 1 and edit.replacement == b""
    assert row["status"] == "planned" and row["id"] == "alias"
    assert row["occurrence_range"][0] + len("comment") == edit.start_byte
    assert nmc.verify_beacon_edit_plan(planned["edit_plan"], scanner=nmc._beacon_edit_records)["success"]


@pytest.mark.parametrize("damage,reason", [
    ("both", "canonical_and_alias"), ("inline", "ambiguous_keys"),
    ("incomplete", "incomplete_record"), ("uncertain", "uncertain_record"),
    ("conflicting_duplicate", "ambiguous_keys"),
])
def test_alias_refusals_are_per_record_with_safe_alias_neighbour(tmp_path, monkeypatch, damage, reason):
    """ab0653d changes the collision or leaves U02 to reject the whole file."""
    bad_rows = alias_rows("bad")
    if damage == "both":
        bad_rows.insert(-1, "# comment=Canonical continuation,")
    elif damage == "inline":
        bad_rows = ["# @beacon[id=bad, kind=ast, comments=Ambiguous, role=words] "]
    elif damage == "incomplete":
        bad_rows.pop()
    elif damage == "conflicting_duplicate":
        bad_rows.insert(-1, "# comments=Different,")
    bad = rows_bytes(bad_rows + declaration("python", "bad"))
    safe = rows_bytes(alias_rows("safe") + declaration("python", "safe"))
    path = tmp_path / "alias_choices.py"
    raw = bad + safe + b'ordinary_code = "comments=not metadata"\n'
    path.write_bytes(raw)
    if damage == "uncertain":
        scan = nmc._beacon_edit_records
        def locally_uncertain(snapshot, language):
            records = scan(snapshot, language)
            first = records[0]
            first["literal_certainty"] = "uncertain"
            first["write_ranges"] = None
            first["diagnostics"].append({"code": "lexical_uncertainty", "line": first["line"]})
            return records
        monkeypatch.setattr(nmc, "_beacon_edit_records", locally_uncertain)
    result = nmc.normalize_beacon_comment_field_aliases_in_file(str(path))
    assert result["success"] and result["changed"], result
    assert path.read_bytes() == bad + safe.replace(b"comments=", b"comment=") + b'ordinary_code = "comments=not metadata"\n'
    assert len(result["skipped_aliases"]) == len(result["findings"]) == 1
    choice = result["skipped_aliases"][0]
    assert choice["reason"] == reason and choice["candidates"] == choice["members"]
    assert choice["members"][0]["id"] == "bad" and not choice["edited_ranges"]
    assert [e["id"] for e in result["events"]] == ["safe"]
    assert_outside_edits(raw, path.read_bytes(), result)
    json.dumps(result)


@pytest.mark.parametrize("rows", [
    ["# @beacon[id=a, kind=ast, comments=\"Quoted,] value\"]"],
    alias_rows("a")[:-1] + ["# comments=Alias.,", "# ]"],
    alias_rows("a")[:-1] + ["# owner=dan,", "# ]"],
])
def test_alias_safe_forms_keep_every_value_and_report_each_occurrence(tmp_path, rows):
    """ab0653d misses inline aliases or has no per-occurrence outcome evidence."""
    raw = rows_bytes(rows + declaration("python", "f"))
    path = tmp_path / "alias_forms.py"
    path.write_bytes(raw)
    result = nmc.normalize_beacon_comment_field_aliases_in_file(str(path))
    assert result["success"] and result["changed"], result
    assert path.read_bytes() == raw.replace(b"comments=", b"comment=")
    assert len(result["events"]) == raw.count(b"comments=")
    assert not result["skipped_aliases"] and not result["findings"]
    assert all(e["status"] == "committed" and e["retained_ids"] == ["a"] and not e["dropped_ids"] for e in result["events"])
    assert_outside_edits(raw, path.read_bytes(), result)


@pytest.mark.parametrize("error,kind", [(UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "bad byte"), "not_utf8"),
                                        (PermissionError("injected read failure"), "io_error")])
def test_alias_read_failure_is_a_finding_and_independent_file_still_renames(tmp_path, monkeypatch, error, kind):
    """ab0653d returns success:false without a finding or typed decode outcome."""
    path = tmp_path / "read_error.py"
    raw = rows_bytes(alias_rows() + declaration("python", "f"))
    path.write_bytes(raw)
    before = path.read_bytes(), path.stat().st_mtime_ns
    with monkeypatch.context() as patch:
        def fail(_path):
            raise error
        patch.setattr(nmc, "read_source_snapshot", fail)
        result = nmc.normalize_beacon_comment_field_aliases_in_file(str(path))
    assert not result["success"] and not result["changed"] and result["error_kind"] == kind
    assert result["findings"][0]["reason"] == kind and result["findings"][0]["candidates"] == []
    assert not result["events"] and not result["edited_ranges"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert nmc.normalize_beacon_comment_field_aliases_in_file(str(path))["changed"]
