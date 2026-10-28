"""U06 R1/R2: explicit catalogue intents, not another writer, parser or grouping.

Each case has a read-only preview, exact binary publication through U02, a fresh
reread (idempotent or honestly no longer offered), and a stale refusal followed
by a committing same-file neighbour. At 352b582 this planner API is absent; the
R1's baseline run records collection failure, not per-predicate sensitivity.
At f040ce9e structural cases reach the explicit missing-dispatch exception;
catalogue corrections have separate pre-correction assertion witnesses.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# An isolated baseline must not silently import the runner venv's newer installed
# root-module copy when this API does not exist in that baseline's source tree.
assert (ROOT / "beacon_resolutions.py").is_file(), "U06 pure resolution planner is absent from this source tree"
import beacon_resolutions as resolutions
import beacon_findings as findings
from beacon_source import read_source_snapshot, source_snapshot_from_bytes
from beacon_edits import commit_beacon_edit_plan
import nexus_map_codebase as source
from workflowy_mcp import beacon_integrity as integrity


def _bytes(rows):
    return ("\r\n".join(rows) + "\r\n").encode("utf-8")


def _block(fields, *, end="# ]"):
    return ["# @beacon[", *["#   " + s for s in fields], end]


def _neighbour(language):
    if language == "markdown":
        return _bytes(["<!--", "@beacon[", "  id=safe@neighbour,", "  kind=ast,",
                       "  slice_labels=safe,", "]", "-->", "# Neighbour", "untouched prose"])
    if language == "js_ts":
        return _bytes(["// @beacon[", "//   id=safe@neighbour,", "//   kind=ast,",
                       "//   slice_labels=safe,", "// ]", "function neighbour() { return 77; }"])
    return _bytes([*_block(["id=safe@neighbour,", "kind=ast,", "slice_labels=safe,"]),
                   "def neighbour():", "    return 77"])


def _observe(path, language):
    snapshot = read_source_snapshot(path)
    inputs = integrity._snapshot_analysis_inputs(snapshot, language, source, repo_root=str(path.parent))
    rows = findings.analyze_beacon_snapshot(snapshot, **inputs)
    return snapshot, rows


def _row(rows, code, bid=None):
    return next(r for r in rows if r["code"] == code and (bid is None or r["beacon_id"] == bid))


BASE = ["id=demo@f,", "kind=ast,", "slice_labels=demo,", "comment=Original.,"]


def _case(name):
    """Quoted physical rows only; test source contains no live fixture blocks."""
    fields = list(BASE)
    language, ext, code, action, params = "python", ".py", "unknown_field_key", "set_field", {"member": 0}
    if name == "alias":
        fields[-1] = "comments=Original.,"
        code, action = "legacy_field", "normalize"
        before = _bytes([*_block(fields), "def f():", "    return 1"])
        after = before.replace(b"comments=", b"comment=")
    elif name == "terminator":
        code, action = "noncanonical_terminator", "normalize"
        before = _bytes([*_block(fields, end="# ], \t"), "def f():", "    return 1"])
        after = before.replace(b"# ], \t", b"# ]")
    elif name == "identical":
        fields.insert(2, "kind=ast,")
        code, action = "duplicate_field", "normalize"
        before = _bytes([*_block(fields), "def f():", "    return 1"])
        after = before.replace(b"kind=ast,\r\n#   kind=ast,", b"kind=ast,\r\n#   ")
    elif name == "set_duplicate":
        fields += ["comment=Other.,"]
        code, action = "duplicate_field", "set_field"
        params.update(field="comment", value="Chosen.")
        before = _bytes([*_block(fields), "def f():", "    return 1"])
        after = before.replace(b"comment=Original.,", b"comment=Chosen.,").replace(b"comment=Other.,", b"")
    elif name == "missing_id":
        fields.pop(0)
        code, action = "missing_id", "set_field"
        params.update(field="id", value="")
        before = _bytes([*_block(fields), "def f():", "    return 1"])
        after = before.replace(b"# ]", b"#   id=auto-beacon-2,\r\n# ]")
    elif name == "set_kind":
        fields[1] = "kind=asts,"
        code, action = "unknown_kind", "set_field"
        params.update(field="kind", value="ast")
        before = _bytes([*_block(fields), "def f():", "    return 1"])
        after = before.replace(b"kind=asts,", b"kind=ast,")
    elif name in {"rename_field", "delete_field", "opaque"}:
        fields += ["slice_label=other,"]
        action = "preserve_opaque" if name == "opaque" else name
        before = _bytes([*_block(fields), "def f():", "    return 1"])
        after = (before.replace(b"slice_label=", b"slice_labels=") if name == "rename_field"
                 else before.replace(b"slice_label=other,", b"") if name == "delete_field" else before)
        if name == "rename_field":
            params.update(field="slice_labels")
    elif name in {"inline_literal", "inline_split"}:
        code, action = "inline_field_ambiguity", "select_alternative"
        before = _bytes(["# @beacon[id=demo@f, kind=ast, slice_labels=demo, comment=Meaning, role=other]",
                         "def f():", "    return 1"])
        chosen = (b"\r\n#   comment=Meaning, role=other\r\n# " if name == "inline_literal"
                  else b"\r\n#   comment=Meaning\r\n#   role=other\r\n# ")
        after = before.replace(b"comment=Meaning, role=other", chosen)
    elif name == "replace_comment":
        fields[-1] = "comment=Original &amp; text.,"
        code, action = "comment_rejected", "replace_comment"
        params.update(comment="New,]")
        before = _bytes([*_block(fields), "def f():", "    return 1"])
        after = before.replace(b"Original &amp; text.", b'"New,]"')
    elif name == "append_comment":
        fields[-1] = "comment=Original &amp; text.,"
        code, action = "comment_rejected", "append_comment"
        params.update(comment="More.")
        before = _bytes([*_block(fields), "def f():", "    return 1"])
        after = before.replace(b"Original &amp; text.", b"Original &amp; text. More.")
    elif name == "descriptive":
        fields[0] = "id=demo@g,"
        code, action = "id_unlike_symbol", "edit_descriptive_field"
        params.update(field="comment", value="Concept description.")
        before = _bytes([*_block(fields), "def f():", "    return 1", "def g():", "    return 2"])
        after = before.replace(b"Original.", b"Concept description.")
    elif name == "convert":
        code, action = "block_not_mapped", "convert_to_span"
        before = _bytes([*_block(fields), '\"\"\"Module docstring.\"\"\"', "VALUE = 1"])
        after = before.replace(b"kind=ast,", b"kind=span,")
    elif name == "replace_metadata":
        code, action = "unterminated_block", "replace_metadata"
        fields[1] = "kind=asts,"
        params.update(text="kind=ast,")
        before = _bytes([*_block(fields)[:-1], "def f():", "    return 1"])
        after = before.replace(b"kind=asts,", b"kind=ast,")
    elif name in {"edit_metadata", "delete_metadata"}:
        language, ext, code, action = "js_ts", ".js", "mixed_comment_ownership", name
        before = _bytes(["/* ordinary note", " * @beacon[", " *   id=demo@f,", " *   kind=ast,",
                         " *   slice_labels=demo,", " *   comment=Original.,", " * ]", " * retained note",
                         " */", "function f() { return 1; }"])
        if name == "edit_metadata":
            params.update(text="slice_labels=other,")
            after = before.replace(b"slice_labels=demo,", b"slice_labels=other,")
        else:
            a, b = before.index(b"@beacon["), before.index(b" * ]") + len(b" * ]")
            after = before[:a] + before[b:]
    else:
        raise AssertionError(name)
    return language, ext, code, action, params, before, after


CASES = ["alias", "terminator", "identical", "set_duplicate", "missing_id", "set_kind",
         "rename_field", "delete_field", "opaque", "inline_literal", "inline_split",
         "replace_comment", "append_comment", "descriptive", "convert", "replace_metadata",
         "edit_metadata", "delete_metadata"]


def _params(name, row, params):
    params = dict(params)
    member = row["members"][0]
    if name in {"rename_field", "delete_field", "opaque"}:
        params["range"] = member["field_ranges"][-1]
    elif name in {"replace_metadata", "edit_metadata"}:
        params["range"] = member["field_ranges"][1 if name == "replace_metadata" else 2]
    elif name == "delete_metadata":
        params["range"] = member["write_ranges"][0]
    elif name.startswith("inline_"):
        params["alternative"] = row["detail"]["alternatives"][0 if name == "inline_literal" else 1]
    return params


def _commit_safe_neighbour(path, language):
    snapshot, rows = _observe(path, language)
    row = _row(rows, "empty_comment", "safe@neighbour")
    planned = resolutions.plan_beacon_resolution(snapshot, row, "replace_comment",
                {"member": 0, "comment": "Neighbour repaired."}, language=language)
    assert planned["success"] and planned["changed"], planned
    result = commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)
    assert result["success"], result
    assert b"Neighbour repaired." in path.read_bytes()


@pytest.mark.parametrize("name", CASES)
def test_resolution_preview_exact_apply_reread_and_stale_neighbour(tmp_path, name):
    language, ext, code, action, args, before, expected = _case(name)
    path = tmp_path / ("target" + ext)
    suffix = b"\r\n" + _neighbour(language)
    before, expected = b"\xef\xbb\xbf" + before + suffix, b"\xef\xbb\xbf" + expected + suffix
    path.write_bytes(before)
    snapshot, rows = _observe(path, language)
    row = _row(rows, code)
    params = _params(name, row, args)
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    planned_ids = {"auto-beacon"}
    planned = resolutions.plan_beacon_resolution(snapshot, row, action, params,
                                                language=language, planned_ids=planned_ids)
    assert planned["success"], planned
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    if action == "preserve_opaque":
        assert planned["status"] == "noop" and planned["edit_plan"] is None
    else:
        assert planned["proof"]["candidate_bytes"] == expected
        assert planned["edit_plan"].target_witness and planned["edit_plan"].intended_state
        applied = commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)
        assert applied["success"] and path.read_bytes() == expected, applied
        if name == "missing_id":
            assert planned_ids == {"auto-beacon", "auto-beacon-2"}
    fresh, current = _observe(path, language)
    still = [r for r in current if r["code"] == code and r["beacon_id"] != "safe@neighbour"]
    if still and name not in {"replace_metadata", "rename_field"}:
        repeated = resolutions.plan_beacon_resolution(fresh, still[0], action,
            _params(name, still[0], args), language=language)
        assert repeated["success"] and not repeated["changed"], repeated
    elif expected != before:
        assert resolutions.plan_beacon_resolution(fresh, row, action, params, language=language)["status"] == "stale"
    unchanged = (path.read_bytes(), path.stat().st_mtime_ns)
    # Invalid schema or unoffered action cannot become an arbitrary patch.
    rejected = resolutions.plan_beacon_resolution(fresh, row, action, {**params, "force": True}, language=language)
    assert rejected["error_kind"] == "invalid_params"
    assert resolutions.plan_beacon_resolution(fresh, row, "force_write", {}, language=language)["error_kind"] == "resolution_not_offered"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == unchanged
    # A real external edit invalidates this snapshot's witness, not neighbours.
    path.write_bytes(path.read_bytes() + (b"\r\n<!-- external -->\r\n" if language == "markdown" else
                     b"\r\n// external\r\n" if language == "js_ts" else b"\r\n# external\r\n"))
    current_snapshot, _ = _observe(path, language)
    stale_bytes = (path.read_bytes(), path.stat().st_mtime_ns)
    stale = resolutions.plan_beacon_resolution(current_snapshot, row, action, params, language=language)
    assert stale["status"] == "stale"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == stale_bytes
    _commit_safe_neighbour(path, language)


@pytest.mark.parametrize("choice", ["field_end", "body_end"])
def test_lost_bracket_candidates_reparse_separate_ids_without_other_changes(tmp_path, choice):
    path = tmp_path / "bracket.py"
    before = _bytes([*_block(BASE)[:-1], "# ordinary retained context", "# "])
    before += _neighbour("python")
    path.write_bytes(before)
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "unterminated_block")
    member = row["members"][0]
    assert len(set(member["repair_candidates"].values())) == 2
    boundary = member["repair_candidates"][choice]
    before_stat = (path.read_bytes(), path.stat().st_mtime_ns)
    planned = resolutions.plan_beacon_resolution(snapshot, row, "insert_bracket",
                        {"member": 0, "boundary": boundary}, language="python")
    assert planned["success"], planned
    expected = before[:boundary] + b"\r\n# ]" + before[boundary:]
    assert planned["proof"]["candidate_bytes"] == expected
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before_stat
    assert commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)["success"]
    assert path.read_bytes() == expected
    records = source._beacon_edit_records(read_source_snapshot(path), "python")
    assert [r["fields"]["id"] for r in records] == ["demo@f", "safe@neighbour"]
    assert all(r["complete"] for r in records)
    assert not any(r["code"] == "unterminated_block" for r in _observe(path, "python")[1])
    assert resolutions.plan_beacon_resolution(read_source_snapshot(path), row, "insert_bracket",
        {"member": 0, "boundary": boundary}, language="python")["status"] == "stale"
    _commit_safe_neighbour(path, "python")


def test_lost_wrapper_candidate_reveals_headings_and_keeps_stray_arrow(tmp_path):
    path = tmp_path / "wrapper.md"
    before = _bytes(["<!--", "@beacon[", "  id=demo@f,", "  kind=ast,", "  slice_labels=demo,",
                     "  comment=Original.,", "]", "# Hidden", "unchanged prose -->", "## Later"])
    before += _neighbour("markdown")
    path.write_bytes(before)
    snapshot, rows = _observe(path, "markdown")
    row = _row(rows, "missing_wrapper", "demo@f")
    boundary = row["members"][0]["repair_candidates"]["body_end"]
    unchanged = (path.read_bytes(), path.stat().st_mtime_ns)
    planned = resolutions.plan_beacon_resolution(snapshot, row, "insert_wrapper",
                          {"member": 0, "boundary": boundary}, language="markdown")
    assert planned["success"], planned
    expected = before[:boundary] + b" -->" + before[boundary:]
    assert planned["proof"]["candidate_bytes"] == expected
    assert (path.read_bytes(), path.stat().st_mtime_ns) == unchanged
    assert commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)["success"]
    assert path.read_bytes() == expected
    current, rows = _observe(path, "markdown")
    assert not any(r["code"] == "missing_wrapper" for r in rows)
    mapped = source.map_source_snapshot(current)
    assert "Hidden" in str(mapped) and "Later" in str(mapped)
    assert b"unchanged prose -->" in path.read_bytes()
    assert resolutions.plan_beacon_resolution(current, row, "insert_wrapper",
        {"member": 0, "boundary": boundary}, language="markdown")["status"] == "stale"
    _commit_safe_neighbour(path, "markdown")


def test_metadata_cannot_introduce_code_and_invalid_comment_is_local(tmp_path):
    path = tmp_path / "local.py"
    path.write_bytes(_bytes([*_block(BASE)[:-1], "def f():", "    return 1"]) + _neighbour("python"))
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "unterminated_block")
    bounds = row["members"][0]["field_ranges"][1]
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    rejected = resolutions.plan_beacon_resolution(snapshot, row, "replace_metadata",
        {"member": 0, "range": bounds, "text": "\ndef injected(): pass\n"}, language="python")
    assert rejected["error_kind"] == "plan_mismatch"
    comment = _row(rows, "empty_comment", "safe@neighbour")
    rejected_comment = resolutions.plan_beacon_resolution(snapshot, comment, "replace_comment",
        {"member": 0, "comment": "bad --> comment"}, language="python")
    assert not rejected_comment["success"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    _commit_safe_neighbour(path, "python")


def test_schema_binds_range_to_member_and_rejects_bool_indices(tmp_path):
    path = tmp_path / "fields.py"
    path.write_bytes(_bytes([*_block(BASE + ["slice_label=x,"]), "def f(): pass"]) + _neighbour("python"))
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "unknown_field_key")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    safe_range = _row(rows, "empty_comment", "safe@neighbour")["members"][0]["field_ranges"][0]
    for params in ({"member": 0, "range": safe_range}, {"member": False, "range": row["members"][0]["field_ranges"][-1]}):
        assert resolutions.plan_beacon_resolution(snapshot, row, "delete_field", params,
                    language="python")["error_kind"] == "invalid_params"
    forged = copy.deepcopy(row)
    forged["members"][0]["field_ranges"][-1] = safe_range
    # Even a mutated catalogue range is not source ownership.
    forged["fix"] = findings.beacon_finding_fix(forged["code"], forged["members"], forged["detail"])
    assert resolutions.plan_beacon_resolution(snapshot, forged, "delete_field",
        {"member": 0, "range": safe_range}, language="python")["status"] == "stale"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    _commit_safe_neighbour(path, "python")


def test_missing_ids_accumulate_and_explicit_collision_does_not_reserve(tmp_path):
    path = tmp_path / "ids.py"
    path.write_bytes(_bytes([*_block(BASE[1:]), "def f(): pass", *_block(BASE[1:]), "def g(): pass"]) + _neighbour("python"))
    snapshot, rows = _observe(path, "python")
    missing = [r for r in rows if r["code"] == "missing_id"]
    planned_ids = set()
    plans = [resolutions.plan_beacon_resolution(snapshot, row, "set_field",
        {"member": 0, "field": "id", "value": ""}, language="python", planned_ids=planned_ids) for row in missing]
    assert all(p["success"] for p in plans)
    assert planned_ids == {"auto-beacon", "auto-beacon-2"}
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    refused = resolutions.plan_beacon_resolution(snapshot, missing[0], "set_field",
        {"member": 0, "field": "id", "value": "safe@neighbour"}, language="python", planned_ids=planned_ids)
    assert not refused["success"] and "collides" in refused["error"]
    assert planned_ids == {"auto-beacon", "auto-beacon-2"}
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    _commit_safe_neighbour(path, "python")


def test_manual_failure_retry_and_keep_are_read_only(tmp_path):
    path = tmp_path / "broken.py"
    path.write_bytes(b"def broken(:\r\n" + _neighbour("python"))
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "mapper_parse_error")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    for action in ("retry", "keep"):
        planned = resolutions.plan_beacon_resolution(snapshot, row, action, language="python")
        assert planned["success"] and planned["status"] == "noop" and planned["edit_plan"] is None
    assert row["fix"]["class"] == "manual"
    assert resolutions.plan_beacon_resolution(snapshot, row, "edit_metadata", {},
                language="python")["error_kind"] == "resolution_not_offered"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    _commit_safe_neighbour(path, "python")


@pytest.mark.parametrize("language,ext,prefix,code_line", [
    ("python", ".py", "#", "def f(): pass"),
    ("js_ts", ".js", "//", "function f() {}"),
    ("sql", ".sql", "--", "SELECT 1;"),
    ("shell", ".sh", "#", "echo untouched"),
    ("yaml", ".yaml", "#", "untouched: 1"),
])
@pytest.mark.parametrize("ending", [b"\n", b"\r", b"\r\n"])
def test_delimiter_repair_uses_scanner_geometry_in_each_line_language(tmp_path, language, ext, prefix, code_line, ending):
    path = tmp_path / ("language" + ext)
    lines = [prefix + " @beacon[", prefix + "   id=demo@language,", prefix + "   kind=span,",
             prefix + "   slice_labels=demo,", prefix + "   comment=Description.,", code_line]
    before = b"\xef\xbb\xbf" + ending.join(s.encode() for s in lines)
    # A neighbour lives in this language's comment syntax; it is intentionally
    # open-only too. An existing diagnostic is never a file-wide refusal.
    safe = ending.join(s.encode() for s in ["", prefix + " @beacon[", prefix + "   id=safe@language,",
                      prefix + "   kind=span,", prefix + " ]", ""])
    path.write_bytes(before + safe)
    snapshot, rows = _observe(path, language)
    row = _row(rows, "unterminated_block", "demo@language")
    boundary = row["members"][0]["repair_candidates"]["body_end"]
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    planned = resolutions.plan_beacon_resolution(snapshot, row, "insert_bracket",
        {"member": 0, "boundary": boundary}, language=language)
    assert planned["success"], planned
    assert planned["proof"]["candidate_bytes"] == (before + safe)[:boundary] + ending + (prefix + " ]").encode() + (before + safe)[boundary:]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    assert commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)["success"]
    fresh, rows = _observe(path, language)
    assert not any(r["code"] == "unterminated_block" for r in rows)
    assert resolutions.plan_beacon_resolution(fresh, row, "insert_bracket",
        {"member": 0, "boundary": boundary}, language=language)["status"] == "stale"
    neighbour = _row(rows, "empty_comment", "safe@language")
    good = resolutions.plan_beacon_resolution(fresh, neighbour, "replace_comment",
        {"member": 0, "comment": "Safe."}, language=language)
    assert good["success"] and good["changed"], good
    assert commit_beacon_edit_plan(good["edit_plan"], scanner=source._beacon_edit_records)["success"]


def test_id_field_choice_renames_unique_close_and_reports_churn_without_prefix_lock(tmp_path):
    path = tmp_path / "paired.py"
    before = _bytes([*_block(["id=demo@span,", "kind=span,", "slice_labels=demo,", "unknown=opaque,"]),
                     "VALUE = 'span body unchanged'", "# @beacon-close[id=demo@span]",
                     "# in-scope citation demo@span"])
    before += _neighbour("python")
    path.write_bytes(before)
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "unknown_field_key", "demo@span")
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    planned = resolutions.plan_beacon_resolution(snapshot, row, "set_field",
        {"member": 0, "field": "id", "value": "different-prefix@chosen"}, language="python")
    assert planned["success"], planned
    # Only the two metadata occurrences; the textual citation is advisory data.
    expected = before.replace(b"id=demo@span", b"id=different-prefix@chosen")
    assert planned["proof"]["candidate_bytes"] == expected
    assert len(planned["edit_plan"].target_witness) == 2
    consequence = planned["consequences"][0]
    assert consequence["potential_workflowy_uuid_change"] and consequence["stale_note_anchors"]
    assert any("in-scope citation" in d["text"] for d in consequence["in_scope_textual_dependants"])
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    assert commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)["success"]
    assert path.read_bytes() == expected
    fresh, rows = _observe(path, "python")
    current = _row(rows, "unknown_field_key", "different-prefix@chosen")
    assert not resolutions.plan_beacon_resolution(fresh, current, "set_field",
        {"member": 0, "field": "id", "value": "different-prefix@chosen"}, language="python")["changed"]
    assert resolutions.plan_beacon_resolution(fresh, row, "set_field",
        {"member": 0, "field": "id", "value": "different-prefix@chosen"}, language="python")["status"] == "stale"
    _commit_safe_neighbour(path, "python")


def test_bounded_metadata_id_correction_cannot_bypass_current_and_planned_ids(tmp_path):
    path = tmp_path / "metadata.py"
    before = _bytes([*_block(BASE)[:-1], "def f(): pass"]) + _neighbour("python")
    path.write_bytes(before)
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "unterminated_block", "demo@f")
    bounds = row["members"][0]["field_ranges"][0]
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    taken = {"reserved@next"}
    for new_id in ("safe@neighbour", "reserved@next"):
        rejected = resolutions.plan_beacon_resolution(snapshot, row, "replace_metadata",
            {"member": 0, "range": bounds, "text": "id=" + new_id + ","},
            language="python", planned_ids=taken)
        assert not rejected["success"] and "collide" in rejected["error"], rejected
    assert taken == {"reserved@next"}
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    _commit_safe_neighbour(path, "python")


def test_same_geometry_snapshot_still_rejects_old_observed_hash(tmp_path):
    path = tmp_path / "freshness.py"
    before = _bytes([*_block(BASE + ["unknown=opaque,"]), "def f():", "    return 1"]) + _neighbour("python")
    path.write_bytes(before)
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "unknown_field_key", "demo@f")
    path.write_bytes(before.replace(b"return 1", b"return 2"))
    fresh, _ = _observe(path, "python")
    assert len(fresh.raw_bytes) == len(snapshot.raw_bytes)
    assert fresh.byte_offsets == snapshot.byte_offsets
    current = (path.read_bytes(), path.stat().st_mtime_ns)
    refused = resolutions.plan_beacon_resolution(fresh, row, "set_field",
        {"member": 0, "field": "comment", "value": "Choice."}, language="python")
    assert refused["status"] == "stale"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == current
    _commit_safe_neighbour(path, "python")


def test_normalize_only_selected_alias_even_with_another_alias_in_same_file(tmp_path):
    path = tmp_path / "aliases.py"
    first = _bytes([*_block(["id=demo@first,", "kind=ast,", "comments=First.,"]), "def first(): pass"])
    second = _bytes([*_block(["id=demo@second,", "kind=ast,", "comments=Second.,"]), "def second(): pass"])
    before = first + second + _neighbour("python")
    path.write_bytes(before)
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "legacy_field", "demo@first")
    unchanged = (path.read_bytes(), path.stat().st_mtime_ns)
    planned = resolutions.plan_beacon_resolution(snapshot, row, "normalize", {"member": 0}, language="python")
    assert planned["success"], planned
    assert planned["proof"]["candidate_bytes"] == first.replace(b"comments=", b"comment=") + second + _neighbour("python")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == unchanged
    assert commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)["success"]
    _commit_safe_neighbour(path, "python")


def test_planner_has_no_io_governor_or_second_proof_authority():
    tree = ast.parse(Path(resolutions.__file__).read_text(encoding="utf-8"))
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not calls & {"open", "read_source_snapshot", "os.replace", "commit_beacon_edit_plan",
                        "compute_footprint", "acquire_q3_leases_async", "Path.write_bytes"}
    assert "verify_beacon_edit_plan" in calls
    assert "source._build_beacon_block_for_language" in calls
    assert "source.plan_beacon_comment_aliases" in calls
    assert "source.allocate_beacon_id" in calls
    assert "workflowy_mcp" not in Path(resolutions.__file__).read_text(encoding="utf-8")


def _span(bid="demo@span"):
    return _block(["id=" + bid + ",", "kind=span,", "slice_labels=demo,", "comment=Span.,"])


def _structural_case(name):
    """Independent byte expectations, not a copy of the planner's candidate."""
    language, ext = "python", ".py"
    if name == "rename_id":
        before = _bytes([*_block(["id=demo@g,", "kind=ast,", "slice_labels=demo,", "comment=Concept.,"]),
                         "def f(): pass", "def g(): pass"])
        return language, ext, "id_unlike_symbol", name, {"member": 0, "new_id": "other-prefix@concept"}, before, before.replace(b"id=demo@g", b"id=other-prefix@concept")
    if name == "assign_ids":
        first = _bytes([*_span(), "FIRST = 1", "# @beacon-close[id=demo@span]"])
        second = _bytes([*_span(), "SECOND = 2", "# @beacon-close[id=demo@span]"])
        return language, ext, "duplicate_id", name, {"keeper": 0, "new_ids": [{"member": 1, "new_id": "new@second"}]}, first + second, first + second.replace(b"id=demo@span", b"id=new@second")
    if name in {"pair_close", "open_only"}:
        first, second = _bytes(_span()), _bytes(_span())
        tail = _bytes(["VALUE = 'body stays exact'", "# @beacon-close[id=demo@span]"])
        if name == "pair_close":
            params = {"opener": 1, "close": 0, "new_id": "new@pair"}
            expected = first.replace(b"id=demo@span", b"id=new@pair") + second + tail.replace(b"id=demo@span", b"id=new@pair")
        else:
            params = {"opener": 1, "open_only": True, "new_id": "new@open"}
            expected = first.replace(b"id=demo@span", b"id=new@open") + second + tail
        return language, ext, "pair_ambiguous", "pair_close", params, first + second + tail, expected
    if name == "retag_close":
        before = _bytes([*_span(), "BODY = 1", "# @beacon-close[id=demo@spna]"])
        return language, ext, "orphan_close", name, {"member": 0, "new_id": "demo@span"}, before, before.replace(b"id=demo@spna", b"id=demo@span")
    if name == "add_close":
        before = _bytes([*_span(), "BODY = 1"])
        boundary = len(before)
        expected = before + b"# @beacon-close[id=demo@span]\r\n"
        return language, ext, "span_without_close", name, {"member": 0, "boundary": boundary}, before, expected
    if name == "remove_close":
        before = _bytes(["# @beacon-close[id=orphan@close]", "BODY = 1"])
        return language, ext, "orphan_close", "delete_metadata", {"member": 0}, before, _bytes(["BODY = 1"])
    if name == "merge":
        first = _bytes(_block(["id=demo@first,", "kind=ast,", "slice_labels=one,", "comment=First.,", "show_span=true,", "role=FirstRole,"]))
        second = _bytes(_block(["id=demo@second,", "kind=ast,", "slice_labels=two,", "comment=Second.,", "show_span=false,", "role=SecondRole,"]))
        tail = _bytes(["# ordinary retained", "@decorate", "def f():", "    return 42"])
        expected = first.replace(b"slice_labels=one,", b"slice_labels=one,two,").replace(b"comment=First.,", b"comment=Chosen.,").replace(b"show_span=true,", b"show_span=false,").replace(b"role=FirstRole,", b"role=ChosenRole,") + tail
        return language, ext, "two_blocks_one_symbol", name, {"survivor": 0, "fields": {"comment": "Chosen.", "show_span": "false", "role": "ChosenRole"}}, first + second + tail, expected
    if name in {"relocate", "create_block"}:
        orphan = _bytes(_block(["id=demo@g,", "kind=ast,", "slice_labels=demo,", "comment=Concept.,", "unknown=retain,"]))
        tail = _bytes(["def f(): pass", "# ordinary retained", "@decorate", "def g():", "    return 2"])
        if name == "relocate":
            before = orphan + tail
            expected = _bytes(["def f(): pass", "# ordinary retained"]) + orphan + _bytes(["@decorate", "def g():", "    return 2"])
            return language, ext, "id_unlike_symbol", name, {"member": 0, "target": {"ast_qualname": "g"}}, before, expected
        before = orphan + _bytes(["VALUE = 1"]) + tail
        created = _bytes(_block(["id=new@block,", "kind=ast,"]))
        expected = orphan + _bytes(["VALUE = 1", "def f(): pass", "# ordinary retained"]) + created + _bytes(["@decorate", "def g():", "    return 2"])
        # A real twin-target finding offers creation on an explicitly different
        # current symbol without treating the observed tagged twin as absence.
        twins = _bytes(["# @beacon[id=demo@f,kind=ast,slice_labels=demo,comment=Concept.]", "def f(): pass", "def f(): return 2"])
        before = twins + _bytes(["# ordinary retained", "@decorate", "def g():", "    return 2"])
        expected = twins + _bytes(["# ordinary retained"]) + created + _bytes(["@decorate", "def g():", "    return 2"])
        return language, ext, "writer_target_ambiguous", name, {"target": {"ast_qualname": "g"}, "id": "new@block"}, before, expected
    raise AssertionError(name)


STRUCTURAL_CASES = ["rename_id", "assign_ids", "pair_close", "open_only", "retag_close", "add_close", "remove_close", "merge", "relocate", "create_block"]


@pytest.mark.parametrize("line,offset", [
    ("value = 123", 9),
    ("# ordinary note about the next call", 6),
])
def test_add_close_refuses_mid_line_without_changing_bytes_or_mtime(tmp_path, line, offset):
    path = tmp_path / "boundary.py"
    before = _bytes([*_span(), line, "print(123)"]) + _neighbour("python")
    path.write_bytes(before)
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "span_without_close", "demo@span")
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    result = resolutions.plan_beacon_resolution(snapshot, row, "add_close",
        {"member": 0, "boundary": before.index(line.encode()) + offset}, language="python")
    assert result["status"] == "refused" and result["error_kind"] == "no_safe_geometry", result
    assert "boundary must be a physical line start within the span section, or EOF" in result["error"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    _commit_safe_neighbour(path, "python")


def test_add_close_at_line_start_inserts_exactly_one_metadata_line(tmp_path):
    path = tmp_path / "line_start.py"
    before = _bytes([*_span(), "value = 123", "print(value)"])
    path.write_bytes(before)
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "span_without_close", "demo@span")
    boundary = snapshot.byte_offsets[len(_span()) + 1]
    assert boundary == before.index(b"print(value)")
    result = resolutions.plan_beacon_resolution(snapshot, row, "add_close",
        {"member": 0, "boundary": boundary}, language="python")
    assert result["success"], result
    expected = before[:boundary] + b"# @beacon-close[id=demo@span]\r\n" + before[boundary:]
    assert result["proof"]["candidate_bytes"] == expected
    assert commit_beacon_edit_plan(result["edit_plan"], scanner=source._beacon_edit_records)["success"]
    assert path.read_bytes() == expected
    ast.parse(path.read_bytes())


@pytest.mark.parametrize("terminated", [True, False])
def test_add_close_at_eof_preserves_termination_without_blank_line(tmp_path, terminated):
    path = tmp_path / "eof.py"
    before = _bytes([*_span(), "value = 123"])
    if not terminated:
        before = before[:-2]
    path.write_bytes(before)
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "span_without_close", "demo@span")
    result = resolutions.plan_beacon_resolution(snapshot, row, "add_close",
        {"member": 0, "boundary": len(before)}, language="python")
    assert result["success"], result
    suffix = (b"# @beacon-close[id=demo@span]\r\n" if terminated
              else b"\r\n# @beacon-close[id=demo@span]")
    assert result["proof"]["candidate_bytes"] == before + suffix
    option = next(o for o in row["fix"]["resolutions"] if o["resolution"] == "add_close")
    boundary_schema = option["params_schema"]["properties"]["boundary"]
    assert boundary_schema["type"] == "integer" and boundary_schema["minimum"] == 0
    assert "physical line start within the span section, or EOF" in boundary_schema["description"]
    assert commit_beacon_edit_plan(result["edit_plan"], scanner=source._beacon_edit_records)["success"]
    assert path.read_bytes() == before + suffix
    ast.parse(path.read_bytes())


@pytest.mark.parametrize("name", STRUCTURAL_CASES)
def test_structural_preview_exact_apply_reread_stale_and_neighbour(tmp_path, name):
    language, ext, code, action, params, before, expected = _structural_case(name)
    path = tmp_path / (name + ext)
    suffix = _neighbour(language)
    before, expected = b"\xef\xbb\xbf" + before + suffix, b"\xef\xbb\xbf" + expected + suffix
    # Byte operands are relative to the BOM-bearing observation.
    if action == "add_close":
        params["boundary"] += 3
    path.write_bytes(before)
    snapshot, rows = _observe(path, language)
    matches = [r for r in rows if r["code"] == code]
    row = next(r for r in matches if r["members"][0]["kind"] == "close") if name in {"pair_close", "open_only"} else matches[0]
    if name == "remove_close":
        params["range"] = row["members"][0]["write_ranges"][0]
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    planned = resolutions.plan_beacon_resolution(snapshot, row, action, params, language=language)
    assert planned["success"] and planned["changed"], planned
    assert planned["proof"]["candidate_bytes"] == expected
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    assert planned["edit_plan"].intended_state
    assert commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)["success"]
    assert path.read_bytes() == expected
    fresh, current = _observe(path, language)
    assert resolutions.plan_beacon_resolution(fresh, row, action, params, language=language)["status"] == "stale"
    if name in {"assign_ids", "merge", "remove_close", "retag_close", "add_close", "pair_close", "open_only", "rename_id", "relocate"}:
        assert not any(r["code"] == code and r["beacon_id"] != "safe@neighbour" for r in current)
    if name in {"rename_id", "pair_close", "open_only"}:
        assert any(c.get("potential_workflowy_uuid_change") for c in planned["consequences"])
    if name == "open_only":
        assert any(c.get("open_only") for c in planned["consequences"])
    if name == "relocate":
        rerow = next(r for r in current if r["code"] == "unknown_field_key" and r["beacon_id"] == "demo@g")
        # It now names its actual attachment; U03 verifies the current target.
        assert rerow["members"][0]["anchor"]["ast_qualname"] == "g"
    if name == "create_block":
        current_row = next(r for r in current if r["finding_id"] == row["finding_id"])
        repeated = resolutions.plan_beacon_resolution(fresh, current_row, action, params, language=language)
        assert not repeated["success"] and "collides" in repeated["error"]
    unchanged = (path.read_bytes(), path.stat().st_mtime_ns)
    assert resolutions.plan_beacon_resolution(fresh, row, action, {**params, "force": True}, language=language)["error_kind"] == "invalid_params"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == unchanged
    _commit_safe_neighbour(path, language)


@pytest.mark.parametrize("language,ext,rows", [
    ("markdown", ".md", ["<!--", "@beacon[", "  id=demo@wrap,", "  kind=ast,", "  comment=Description.,", "]", "# Hidden", "prose -->"]),
    ("vue", ".vue", ["<template>", "<!--", "@beacon[", "  id=demo@wrap,", "  kind=span,", "  comment=Description.,", "]", "<div>kept</div>", "</template>", "<script>", "export const safe = 1;", "</script>"]),
])
def test_every_offered_wrapper_boundary_plans_and_passes_u02(tmp_path, language, ext, rows):
    path = tmp_path / ("wrapper" + ext)
    before = _bytes(rows)
    path.write_bytes(before)
    snapshot, findings_rows = _observe(path, language)
    row = _row(findings_rows, "missing_wrapper")
    option = next(o for o in row["fix"]["resolutions"] if o["resolution"] == "insert_wrapper")
    boundaries = option["params_schema"]["properties"]["boundary"]["enum"]
    assert boundaries and all(b >= row["members"][0]["bracket_range"][1] for b in boundaries)
    unchanged = (path.read_bytes(), path.stat().st_mtime_ns)
    for boundary in boundaries:
        planned = resolutions.plan_beacon_resolution(snapshot, row, "insert_wrapper", {"member": 0, "boundary": boundary}, language=language)
        assert planned["success"] and planned["proof"]["success"], planned
        assert planned["proof"]["candidate_bytes"] == before[:boundary] + b" -->" + before[boundary:]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == unchanged


@pytest.mark.parametrize("new_id", ["other@chosen", ""])
@pytest.mark.parametrize("mode", ["pair", "open_only"])
def test_explicit_pair_or_open_only_id_and_allocation_consequence(tmp_path, new_id, mode):
    path = tmp_path / "choices.py"
    _lang, _ext, code, action, params, before, _expected = _structural_case("pair_close" if mode == "pair" else "open_only")
    path.write_bytes(before + _neighbour("python"))
    snapshot, rows = _observe(path, "python")
    row = next(r for r in rows if r["code"] == code and r["members"][0]["kind"] == "close")
    params["new_id"] = new_id
    planned_ids = {"demo@span-2"}
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    planned = resolutions.plan_beacon_resolution(snapshot, row, action, params, language="python", planned_ids=planned_ids)
    assert planned["success"], planned
    chosen = new_id or "demo@span-3"
    assert chosen in planned_ids
    assert any(c.get("new_ids") == [chosen] and c["potential_workflowy_uuid_change"] for c in planned["consequences"])
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    assert commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)["success"]
    records = source._beacon_edit_records(read_source_snapshot(path), "python")
    opening = next(r for r in records if r["fields"].get("id") == chosen and r["marker"] == "open")
    assert opening["pair_state"] == ("closed" if mode == "pair" else "open_only")
    _commit_safe_neighbour(path, "python")


def test_structural_choices_refuse_locally_without_hidden_keeper_pair_or_target(tmp_path):
    path = tmp_path / "negative.py"
    _l, _e, _c, _a, _p, before, _expected = _structural_case("pair_close")
    path.write_bytes(before + _neighbour("python"))
    snapshot, rows = _observe(path, "python")
    duplicate = _row(rows, "duplicate_id")
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    for params in ({"keeper": 0, "new_ids": []},
                   {"keeper": 0, "new_ids": [{"member": 1, "new_id": "new@id"}]},
                   {"keeper": 0, "new_ids": [{"member": 1, "new_id": "demo@span", "open_only": True}]},
                   {"keeper": 0, "new_ids": [{"member": 1, "new_id": "safe@neighbour", "open_only": True}]}):
        result = resolutions.plan_beacon_resolution(snapshot, duplicate, "assign_ids", params, language="python")
        assert not result["success"], result
    pair = next(r for r in rows if r["code"] == "pair_ambiguous" and r["members"][0]["kind"] == "close")
    for params in ({"opener": 1, "close": 0}, {"opener": 1, "close": 0, "open_only": True, "new_id": "new@id"},
                   {"opener": 1, "open_only": True, "new_id": "demo@span"}):
        result = resolutions.plan_beacon_resolution(snapshot, pair, "pair_close", params, language="python")
        assert not result["success"], result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    _commit_safe_neighbour(path, "python")


@pytest.mark.parametrize("open_only", [False, True])
def test_assign_ids_ambiguous_close_choice_uses_physical_operand(tmp_path, open_only):
    path = tmp_path / "duplicate.py"
    _l, _e, _c, _a, _p, before, _expected = _structural_case("pair_close")
    path.write_bytes(before + _neighbour("python"))
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "duplicate_id")
    close = row["detail"]["close_candidates"][0]["opener_start_byte"]
    choice = {"open_only": True} if open_only else {"close": close}
    params = {"keeper": 0, "new_ids": [{"member": 1, "new_id": "", **choice}]}
    planned = resolutions.plan_beacon_resolution(snapshot, row, "assign_ids", params, language="python")
    assert planned["success"], planned
    assert commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)["success"]
    records = source._beacon_edit_records(read_source_snapshot(path), "python")
    opening = next(r for r in records if r["marker"] == "open" and r["fields"].get("id") == "demo@span-2")
    assert opening["pair_state"] == ("open_only" if open_only else "closed")
    assert path.read_bytes().startswith(_bytes(_span()))  # explicit keeper untouched
    _commit_safe_neighbour(path, "python")


def test_merge_missing_conflict_choice_and_code_boundary_do_not_block_neighbour(tmp_path):
    path = tmp_path / "merge.py"
    _l, _e, _c, _a, params, before, _expected = _structural_case("merge")
    path.write_bytes(before + _neighbour("python"))
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "two_blocks_one_symbol")
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    params["fields"].pop("show_span")
    refused = resolutions.plan_beacon_resolution(snapshot, row, "merge", params, language="python")
    assert not refused["success"] and "show_span" in refused["error"], refused
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    _commit_safe_neighbour(path, "python")


def test_relocation_wrong_looking_id_preserves_indentation_and_all_opaque_metadata(tmp_path):
    path = tmp_path / "relocation.py"
    fields = ["id=demo@g,", "kind=ast,", "slice_labels=demo,", "comments=Retain alias.,", "unknown=opaque,"]
    block = _bytes(_block(fields))
    before = block + _bytes(["def f(): pass", "def g(): pass", "class C:", "    # ordinary retained", "    @decorate", "    def target(self):", "        return 42"]) + _neighbour("python")
    path.write_bytes(before)
    snapshot, rows = _observe(path, "python")
    # Explicit relocation is offered on a real resemblance row; the id points
    # to g but Dan expressly chooses C.target, with no rename or prefix veto.
    row = _row(rows, "id_unlike_symbol", "demo@g")
    planned = resolutions.plan_beacon_resolution(snapshot, row, "relocate", {"member": 0, "target": {"ast_qualname": "C.target"}}, language="python")
    assert planned["success"], planned
    assert b"    #   comments=Retain alias.,\r\n" in planned["proof"]["candidate_bytes"]
    assert b"    #   unknown=opaque,\r\n" in planned["proof"]["candidate_bytes"]
    assert b"    # ordinary retained\r\n" in planned["proof"]["candidate_bytes"]
    assert b"    @decorate\r\n    def target(self):\r\n        return 42\r\n" in planned["proof"]["candidate_bytes"]
    assert commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)["success"]
    _commit_safe_neighbour(path, "python")


def test_vue_script_relocation_and_template_close_are_section_owned(tmp_path):
    path = tmp_path / "component.vue"
    before = _bytes(["<template>", "<!-- @beacon[id=demo@template,kind=span,slice_labels=demo] -->", "<div>kept</div>", "</template>",
                     "<script>", "// @beacon[id=demo@g,slice_labels=demo,comment=Concept.]", "function f() {}", "// ordinary retained", "function g() {}", "</script>",
                     "<style>", "p { color: red; }", "</style>"])
    path.write_bytes(before)
    snapshot, rows = _observe(path, "vue")
    row = _row(rows, "implicit_kind_promoted", "demo@g")
    inputs = integrity._snapshot_analysis_inputs(snapshot, "vue", source)
    target = next(a["ast_qualname"] for a in inputs["attachment_index"]["anchors"] if a["header"] == "function g() {}")
    planned = resolutions.plan_beacon_resolution(snapshot, row, "relocate", {"member": 0, "target": {"ast_qualname": target}}, language="vue")
    assert planned["success"], planned
    assert commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)["success"]
    snapshot, rows = _observe(path, "vue")
    template = _row(rows, "span_without_close", "demo@template")
    record = source._beacon_edit_records(snapshot, "vue")[0]
    boundary = record["section"]["end_byte"]
    planned = resolutions.plan_beacon_resolution(snapshot, template, "add_close", {"member": 0, "boundary": boundary}, language="vue")
    assert planned["success"], planned
    assert b"<div>kept</div>\r\n" in planned["proof"]["candidate_bytes"]
    assert b"<style>\r\np { color: red; }\r\n</style>\r\n" in planned["proof"]["candidate_bytes"]
    assert commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)["success"]
    assert not any(r["code"] == "span_without_close" and r["beacon_id"] == "demo@template" for r in _observe(path, "vue")[1])


def test_prepared_relocation_mapping_is_verified_before_publication(tmp_path, monkeypatch):
    path = tmp_path / "wrong_mapping.py"
    _l, _e, _c, _a, params, before, _expected = _structural_case("relocate")
    path.write_bytes(before + _bytes(["def h(): pass"]) + _neighbour("python"))
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "id_unlike_symbol")
    real = resolutions._render_at_anchor
    inputs = integrity._snapshot_analysis_inputs(snapshot, "python", source)
    wrong = next(a for a in inputs["attachment_index"]["anchors"] if a["ast_qualname"] == "h")
    def misrender(snapshot, language, anchor, **kwargs):
        return real(snapshot, language, wrong, **kwargs)
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    with monkeypatch.context() as patch:
        patch.setattr(resolutions, "_render_at_anchor", misrender)
        result = resolutions.plan_beacon_resolution(snapshot, row, "relocate", params, language="python")
    assert not result["success"] and "explicitly selected current target" in result["error"], result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    _commit_safe_neighbour(path, "python")


def test_added_close_must_actually_close_selected_opener_not_duplicate_group(tmp_path):
    path = tmp_path / "ambiguous_close.py"
    _l, _e, _c, _a, _p, before, _expected = _structural_case("pair_close")
    path.write_bytes(before + _neighbour("python"))
    snapshot, rows = _observe(path, "python")
    row = next(r for r in rows if r["code"] == "pair_ambiguous" and r["members"][0]["kind"] == "close")
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    result = resolutions.plan_beacon_resolution(snapshot, row, "add_close", {"member": 1, "boundary": len(before)}, language="python")
    assert not result["success"] and result["error_kind"] == "plan_mismatch", result
    assert "non-metadata source" in result["error"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    _commit_safe_neighbour(path, "python")


def test_explicit_auto_merge_choices_reuse_u04_exact_edits(tmp_path):
    path = tmp_path / "auto_merge.py"
    before = _bytes(["# @beacon[id=demo@same,kind=ast,slice_labels=one,comment=First.]",
                     "# @beacon[id=demo@same,kind=ast,slice_labels=two,comment=Second.]", "def f(): pass"])
    path.write_bytes(before + _neighbour("python"))
    snapshot, rows = _observe(path, "python")
    row = _row(rows, "two_blocks_one_symbol")
    option = next(o for o in row["fix"]["resolutions"] if o["resolution"] == "merge")
    planned = resolutions.plan_beacon_resolution(snapshot, row, "merge", option["params"], language="python")
    assert planned["success"], planned
    inputs = integrity._snapshot_analysis_inputs(snapshot, "python", source)
    assert planned["edit_plan"].edits == inputs["hygiene"]["edit_plan"].edits
    assert commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)["success"]
    assert not any(r["code"] == "two_blocks_one_symbol" for r in _observe(path, "python")[1])
    _commit_safe_neighbour(path, "python")


def test_delete_operands_match_actual_scanner_whole_ownership_not_read_extent(tmp_path):
    path = tmp_path / "owned.js"
    before = _bytes(["/* ordinary", " * @beacon[", " * id=demo@mixed,", " * kind=ast,", " * slice_labels=demo,", " * ]", " * retained", " */", "function f() {}"])
    path.write_bytes(before + _neighbour("js_ts"))
    snapshot, rows = _observe(path, "js_ts")
    row = _row(rows, "mixed_comment_ownership")
    member = row["members"][0]
    a, b = member["write_ranges"][0]
    option = next(o for o in row["fix"]["resolutions"] if o["resolution"] == "delete_metadata")
    assert [a, b] in option["params_schema"]["properties"]["range"]["enum"]
    planned = resolutions.plan_beacon_resolution(snapshot, row, "delete_metadata", {"member": 0, "range": [a, b]}, language="js_ts")
    assert planned["success"] and planned["proof"]["candidate_bytes"] == (before + _neighbour("js_ts"))[:a] + (before + _neighbour("js_ts"))[b:]
    assert commit_beacon_edit_plan(planned["edit_plan"], scanner=source._beacon_edit_records)["success"]
    assert b"/* ordinary\r\n" in path.read_bytes() and b" * retained\r\n */" in path.read_bytes()
    _commit_safe_neighbour(path, "js_ts")
    # Missing-wrapper bracket evidence is NOT current write ownership. U02 is
    # unchanged: no fabricated full/read extent is offered as executable deletion.
    path = tmp_path / "unowned.md"
    path.write_bytes(_bytes(["<!--", "@beacon[", "  id=demo@missing,", "  kind=ast,", "]", "# Hidden", "prose -->"]) + _neighbour("markdown"))
    snapshot, rows = _observe(path, "markdown")
    row = _row(rows, "missing_wrapper")
    member = row["members"][0]
    assert member["bracket_range"] and member["write_ranges"] is None
    option = next(o for o in row["fix"]["resolutions"] if o["resolution"] == "delete_metadata")
    assert option["params_schema"]["properties"]["range"]["enum"] == [list(r) for r in member["field_ranges"]]
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    rejected = resolutions.plan_beacon_resolution(snapshot, row, "delete_metadata", {"member": 0, "range": member["read_range"]}, language="markdown")
    assert rejected["error_kind"] == "invalid_params"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    _commit_safe_neighbour(path, "markdown")


def test_complete_catalogue_has_no_missing_dispatch_and_keeps_shared_authorities():
    names = {name for entry in findings.BEACON_REPAIR_CATALOGUE.values() for name in entry["resolutions"]}
    assert names == resolutions.FIELD_RESOLUTIONS | resolutions.STRUCTURAL_RESOLUTIONS
    tree = ast.parse(Path(resolutions.__file__).read_text(encoding="utf-8"))
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert {"source.select_beacon_block_for_node", "source.beacon_attachment_index", "source.map_source_snapshot", "source.plan_beacon_hygiene"} <= calls
    assert "NotImplementedError" not in Path(resolutions.__file__).read_text(encoding="utf-8")
