"""U02 byte-local plan writes; fixtures are quoted rows, not live beacons.

Baseline witnesses: BOM deletion (E1), literal/control corruption (E3), global
newline/EOF normalization, and a code-crossing selector accepted by the old
executor. Component-only tests additionally pin the new proof/map interface.
"""
import hashlib
import os
import stat
from types import SimpleNamespace

import pytest

import beacon_mutation_contract as bmc
from beacon_source import read_source_snapshot, source_snapshot_from_bytes
import nexus_map_codebase as nmc


def execute(path, operation, *, language="python", bid="target", labels=("new",), note=""):
    intent = bmc.BeaconWriteIntent(comment=bmc.BeaconCommentIntent.preserve(), source="test")
    snapshot = nmc.read_source_snapshot(path)
    target = {"language": language, "operation": "delta_ensure", "note_beacon_id": bid}
    if operation == "create":
        target.update(ast_qualname=nmc._extract_ast_qualname_from_note(note), md_path=nmc._extract_md_path_from_note(note))
    selection = nmc.select_beacon_block_for_node(snapshot, nmc._beacon_selection_structures(snapshot, language), target)
    plan = SimpleNamespace(success=True, planned_operation=operation, source_path=str(path),
                           source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                           intended_slice_labels=labels, resolved_comment=None,
                           beacon_id=bid, occurrence=None, selection_target=target,
                           physical_witness=selection["physical_witness"])
    return nmc._execute_beacon_write_plan(language=language, file_path=str(path), name="Target #new",
                                         note=note, write_intent=intent, write_plan=plan)


def block(language, bid="target", *, close=False, kind="span"):
    if language == "markdown":
        return ["<!--", "@beacon-close[" if close else "@beacon[", f"  id={bid},"] + (
            [] if close else ["  slice_labels=old,", f"  kind={kind},"]
        ) + ["]", "-->"]
    prefix = {"js_ts": "//", "vue": "//", "sql": "--"}.get(language, "#")
    return [f"{prefix} @beacon-close[" if close else f"{prefix} @beacon[", f"{prefix}   id={bid},"] + (
        [] if close else [f"{prefix}   slice_labels=old,", f"{prefix}   kind={kind},"]
    ) + [f"{prefix} ]"]


@pytest.mark.parametrize("language", ["python", "js_ts", "vue", "markdown", "sql", "shell", "yaml"])
@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize("regime", ["lf", "crlf", "cr", "mixed", "bom", "no-eof", "trailing", "controls"])
def test_executor_outside_bytes_are_identical(tmp_path, language, operation, regime):
    ending = {"crlf": "\r\n", "cr": "\r"}.get(regime, "\n")
    controls = "\x1b\f\v\x1c\x1d\x1e\x85\u2028\u2029" if regime == "controls" else "literal"
    prefix = (f'TEXT = "{controls}"' + ending).encode()
    if language == "vue":
        prefix = b"<script>\n" + prefix
    if regime == "bom":
        prefix = b"\xef\xbb\xbf" + prefix
    opening = (ending.join(block(language)) + ending).encode()
    body = b"keep_this_code()\r\n" if regime == "mixed" else ("keep_this_code()" + ending).encode()
    closing = (ending.join(block(language, close=True)) + ending).encode()
    suffix = (ending.join(block(language, "neighbour", kind="ast")) + ending + "neighbour()" + ending).encode()
    if regime == "trailing":
        suffix += (ending * 3).encode()
    if regime == "no-eof":
        suffix = suffix[:-len(ending)]
    if language == "vue":
        suffix += b"\n</script>"
    raw = prefix + opening + body + closing + suffix
    path = tmp_path / "source.txt"
    path.write_bytes(raw)
    os.chmod(path, 0o744)
    result = execute(path, operation, language=language, labels=("new,long",))
    assert result.get("success"), result
    expected = (prefix + opening.replace(b"slice_labels=old,", b"slice_labels=new,long,") + body + closing + suffix
                if operation == "update" else prefix + body + suffix)
    assert path.read_bytes() == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o744


def hygiene_fixture(language, ending="\n"):
    opening = ending.join(block(language, "first", kind="ast")) + ending
    duplicate = ending.join(block(language, "first", kind="ast")).replace("slice_labels=old,", "slice_labels=second,") + ending
    target = "# Target" if language == "markdown" else "def target():\n    return 1" if language == "python" else "function target() { return 1; }"
    safe = ending.join(block(language, "safe", kind="ast")) + ending
    safe_code = "# Safe" if language == "markdown" else "def safe():\n    return 2" if language == "python" else "function safe() { return 2; }"
    return (opening + duplicate + target.replace("\n", ending) + ending + safe + safe_code.replace("\n", ending)).encode()


@pytest.mark.parametrize("language,suffix", [("python", ".py"), ("js_ts", ".ts"), ("vue", ".vue"), ("markdown", ".md")])
@pytest.mark.parametrize("regime", ["lf", "crlf", "cr", "mixed", "bom", "no-eof", "trailing", "controls"])
def test_hygiene_byte_table_preserves_source_outside_merged_blocks(tmp_path, language, suffix, regime):
    """b8dbedd: hygiene truncates/rejoins normalized text without a final hash."""
    ending = {"crlf": "\r\n", "cr": "\r"}.get(regime, "\n")
    controls = "\x1b\f\v\x1c\x1d\x1e\x85\u2028\u2029" if regime == "controls" else "literal"
    prefix = (f'TEXT = "{controls}"' + ("\r\n" if regime == "mixed" else ending)).encode()
    raw = prefix + hygiene_fixture(language, ending)
    if regime != "no-eof":
        raw += ending.encode()
    if regime == "trailing":
        raw += (ending * 3).encode()
    if language == "vue":
        raw = b"<script>\n" + raw + b"\n</script>"
    if regime == "bom":
        raw = b"\xef\xbb\xbf" + raw
    path = tmp_path / ("hygiene" + suffix)
    path.write_bytes(raw)
    os.chmod(path, 0o744)
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result.get("success") and result.get("changed"), result
    assert result["merged_groups"] == result["removed_blocks"] == 1
    assert_outside_edits_identical(raw, path.read_bytes(), result)
    assert stat.S_IMODE(path.stat().st_mode) == 0o744
    records = nmc._beacon_edit_records(read_source_snapshot(path), language)
    assert [r["fields"]["id"] for r in records] == ["first", "safe"]
    assert records[0]["fields"]["slice_labels"] == "old,second"
    written, mtime = path.read_bytes(), path.stat().st_mtime_ns
    assert nmc.normalize_duplicate_ast_beacons_in_file(str(path))["changed"] is False
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (written, mtime)


@pytest.mark.parametrize("language,suffix", [("python", ".py"), ("js_ts", ".js"), ("vue", ".vue"), ("markdown", ".md"), ("sql", ".sql"), ("shell", ".sh"), ("yaml", ".yaml")])
@pytest.mark.parametrize("regime", ["lf", "crlf", "cr", "mixed", "bom", "no-eof", "trailing", "controls"])
def test_alias_byte_table_changes_only_owned_field_key(tmp_path, language, suffix, regime):
    """b8dbedd: alias writes back the entire universally-newline-decoded file."""
    ending = {"crlf": "\r\n", "cr": "\r"}.get(regime, "\n")
    controls = "\x1b\f\v\x1c\x1d\x1e\x85\u2028\u2029" if regime == "controls" else "literal"
    prefix = (f'TEXT = "comments={controls}"' + ("\r\n" if regime == "mixed" else ending)).encode()
    rows = block(language, kind="ast")
    row_prefix = {"js_ts": "// ", "vue": "// ", "sql": "-- ", "markdown": "  "}.get(language, "# ")
    rows.insert(-2 if language == "markdown" else -1, row_prefix + "comments = kept comment,")
    metadata = (ending.join(rows) + ending).encode()
    tail = ("# Target" if language == "markdown" else "target()").encode()
    if regime != "no-eof":
        tail += ending.encode()
    if regime == "trailing":
        tail += (ending * 3).encode()
    raw = prefix + metadata + tail
    if language == "vue":
        raw = b"<script>\n" + raw + b"\n</script>"
    if regime == "bom":
        raw = b"\xef\xbb\xbf" + raw
    path = tmp_path / ("alias" + suffix)
    path.write_bytes(raw)
    os.chmod(path, 0o744)
    result = nmc.normalize_beacon_comment_field_aliases_in_file(str(path))
    assert result.get("success") and result.get("changed"), result
    assert path.read_bytes() == raw.replace(b"comments = kept comment,", b"comment = kept comment,")
    assert_outside_edits_identical(raw, path.read_bytes(), result)
    assert stat.S_IMODE(path.stat().st_mode) == 0o744
    mtime = path.stat().st_mtime_ns
    assert nmc.normalize_beacon_comment_field_aliases_in_file(str(path))["changed"] is False
    assert path.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("regime", ["lf", "crlf", "cr", "mixed", "bom", "no-eof", "trailing", "controls"])
def test_mgp_eof_byte_table_changes_only_generation_tail(tmp_path, regime):
    """b8dbedd: the EOF helper normalizes internal endings/Unicode separators."""
    from workflowy_mcp import markdown_generate_pipeline as mgp
    ending = {"crlf": "\r\n", "cr": "\r"}.get(regime, "\n")
    controls = "\x1b\f\v\x1c\x1d\x1e\x85\u2028\u2029" if regime == "controls" else "literal"
    prefix = ("# H" + ("\r\n" if regime == "mixed" else ending) + f'TEXT = "{controls}"' + ending + "last  ").encode()
    if regime == "bom":
        prefix = b"\xef\xbb\xbf" + prefix
    raw = prefix if regime == "no-eof" else prefix + (ending + " \t" + ending * (4 if regime == "trailing" else 2)).encode()
    path = tmp_path / "eof.md"
    path.write_bytes(raw)
    os.chmod(path, 0o744)
    result = mgp._normalize_markdown_file_eof(str(path))
    assert result.get("changed"), result
    assert path.read_bytes() == prefix + ending.encode()
    assert_outside_edits_identical(raw, path.read_bytes(), result)
    assert result["before_chars"] == len(raw.decode())
    assert result["after_chars"] == len(path.read_bytes().decode())
    assert result["delta_chars"] == result["after_chars"] - result["before_chars"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o744
    mtime = path.stat().st_mtime_ns
    assert not mgp._normalize_markdown_file_eof(str(path))["changed"]
    assert path.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("regime", ["lf", "crlf", "cr", "mixed", "bom", "no-eof", "trailing", "controls"])
def test_mgp_id_byte_table_changes_only_duplicate_id_value(tmp_path, regime):
    """U08: default analysis is read-only; explicit chosen rename stays byte-local."""
    from workflowy_mcp import markdown_generate_pipeline as mgp
    ending = {"crlf": "\r\n", "cr": "\r"}.get(regime, "\n")
    controls = "\x1b\f\v\x1c\x1d\x1e\x85\u2028\u2029" if regime == "controls" else "literal"
    prefix = (f'literal "{controls}"' + ("\r\n" if regime == "mixed" else ending)).encode()
    opening = (ending.join(block("markdown", kind="ast")) + ending).encode()
    between = ("# First" + ending + "body" + ending).encode()
    suffix = ("# Second" + ("" if regime == "no-eof" else ending)).encode()
    if regime == "trailing":
        suffix += (ending * 3).encode()
    if regime == "bom":
        prefix = b"\xef\xbb\xbf" + prefix
    raw = prefix + opening + between + opening + suffix
    path = tmp_path / "ids.md"
    path.write_bytes(raw)
    os.chmod(path, 0o744)
    observed = mgp._disambiguate_duplicate_beacon_ids_in_file(str(path))
    finding, = [f for f in observed["findings"] if f["code"] == "duplicate_id"]
    result = mgp._disambiguate_duplicate_beacon_ids_in_file(str(path), selected_resolution={
        "finding_id": finding["finding_id"], "resolution": "assign_ids",
        "params": {"keeper": 0, "new_ids": [{"member": 1, "new_id": "target--dup2"}]}})
    assert result.get("changed") and result["renamed_count"] == 1, result
    assert path.read_bytes() == prefix + opening + between + opening.replace(b"id=target,", b"id=target--dup2,") + suffix
    assert_outside_edits_identical(raw, path.read_bytes(), result)
    assert stat.S_IMODE(path.stat().st_mode) == 0o744
    mtime = path.stat().st_mtime_ns
    assert not mgp._disambiguate_duplicate_beacon_ids_in_file(str(path))["changed"]
    assert path.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("case", ["hygiene-python", "hygiene-js_ts", "hygiene-markdown", "alias", "eof", "id"])
@pytest.mark.parametrize("fault", ["proof", "external_save", "fsync"])
def test_maintenance_failure_is_local_with_same_file_safe_neighbour(tmp_path, monkeypatch, case, fault):
    """Owned maintenance and U08 explicit-id requests retain local fault evidence."""
    from dataclasses import replace
    import beacon_edits
    from workflowy_mcp import markdown_generate_pipeline as mgp
    if case.startswith("hygiene-"):
        language = case.split("-", 1)[1]
        suffix = {"python": ".py", "js_ts": ".js", "markdown": ".md"}[language]
        raw = hygiene_fixture(language) + b"\n\n"
        module, run = nmc, nmc.normalize_duplicate_ast_beacons_in_file
    else:
        language, suffix = "markdown", ".md"
        opening = ("\n".join(block(language, kind="ast")) + "\n").encode()
        safe = ("\n".join(block(language, "safe", kind="ast")) + "\n# Safe\nbody\n\n").encode()
        if case == "alias":
            raw = opening.replace(b"  kind=ast,", b"  kind=ast,\n  comments=retained,") + b"# First\n" + safe
            module, run = nmc, nmc.normalize_beacon_comment_field_aliases_in_file
        elif case == "eof":
            raw = opening + b"# First\n" + safe
            module, run = mgp, mgp._normalize_markdown_file_eof
        else:
            raw = opening + b"# First\n" + opening + b"# Second\n" + safe
            module, run = mgp, mgp._disambiguate_duplicate_beacon_ids_in_file
    path = tmp_path / ("maintenance" + suffix)
    path.write_bytes(raw)
    if case == "id":
        observed = mgp._disambiguate_duplicate_beacon_ids_in_file(str(path))
        finding, = [f for f in observed["findings"] if f["code"] == "duplicate_id"]
        request = {"finding_id": finding["finding_id"], "resolution": "assign_ids",
                   "params": {"keeper": 0, "new_ids": [{"member": 1, "new_id": "chosen"}]}}
        run = lambda p: mgp._disambiguate_duplicate_beacon_ids_in_file(p, selected_resolution=request)
    before = raw, path.stat().st_mtime_ns
    external = raw + b"external save\r\n\r\n"
    state = {}
    with monkeypatch.context() as patch:
        if fault == "external_save":
            original = module.read_source_snapshot
            def observe_then_save(*args, **kwargs):
                snapshot = original(*args, **kwargs)
                path.write_bytes(external)
                state["mtime"] = path.stat().st_mtime_ns
                return snapshot
            patch.setattr(module, "read_source_snapshot", observe_then_save)
        elif fault == "proof":
            original = module.commit_beacon_edit_plan
            def crossing(plan, **kwargs):
                return original(replace(plan, edits=(replace(plan.edits[0], end_byte=len(raw) + 1), *plan.edits[1:])), **kwargs)
            patch.setattr(module, "commit_beacon_edit_plan", crossing)
        else:
            def fail(*_args, **_kwargs):
                raise PermissionError("injected fsync failure")
            patch.setattr(beacon_edits.os, "fsync", fail)
        result = run(str(path))
    expected_kind = {"proof": "plan_mismatch", "external_save": "source_changed_since_plan", "fsync": "disk_write_failed"}[fault]
    assert result.get("error_kind") == expected_kind, result
    assert result.get("success") is False and result["changed"] is False
    assert result["evidence"]["expected"] != result["evidence"]["actual"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == ((external, state["mtime"]) if fault == "external_save" else before)
    assert not list(tmp_path.glob(".beacon_atomic_*"))
    if case == "alias":
        assert not result["events"] and not result["edited_ranges"] and result["after_sha256"] is None
        assert len(result["findings"]) == len(result["skipped_aliases"]) == 1
        finding = result["findings"][0]
        assert finding["operation"] == "rename_comment_alias" and finding["reason"] == expected_kind
        assert finding["file_path"] == str(path) and finding["candidates"][0]["id"] == "target"
        assert finding["commit_evidence"]["evidence"] == result["evidence"]
    good_before = path.read_bytes()
    good = legacy(path, language, bid="safe")
    assert good.get("success"), good
    assert_outside_edits_identical(good_before, path.read_bytes(), good)


def test_first_row_delete_keeps_bom_and_literal_bytes(tmp_path):
    path = tmp_path / "first.py"
    opening = ("\n".join(block("python", kind="ast")) + "\n").encode()
    tail = 'TEXT = "\x1b\f\v\x1c\x1d\x1e\x85\u2028\u2029"\r\nkeep_this_code()\r\n\r\n'.encode()
    path.write_bytes(b"\xef\xbb\xbf" + opening + tail)
    result = execute(path, "delete")
    assert result.get("success"), result
    assert path.read_bytes() == b"\xef\xbb\xbf" + tail


@pytest.mark.parametrize("language,note,source", [
    ("python", "AST_QUALNAME: target\n---\n", "def target():\r\n    return 1"),
    ("markdown", "MD_PATH:\n# Target\n---\n", "# Target\r\nbody"),
])
def test_create_preserves_bom_controls_and_unterminated_tail(tmp_path, language, note, source):
    path = tmp_path / "create.txt"
    prefix = b"\xef\xbb\xbf" + '# literal \x1b\f\v\x85\u2028\u2029\r\n'.encode()
    tail = source.encode()
    path.write_bytes(prefix + tail)
    result = execute(path, "create", language=language, bid=None, note=note)
    assert result.get("success"), result
    written = path.read_bytes()
    assert written.startswith(prefix)
    assert written.endswith(tail)
    assert not written.endswith(b"\n")
    assert result["before_sha256"] == hashlib.sha256(prefix + tail).hexdigest()
    assert result["after_sha256"] == hashlib.sha256(written).hexdigest()


def test_code_crossing_candidate_refuses_only_item_and_safe_neighbour_commits(tmp_path, monkeypatch):
    path = tmp_path / "ranges.py"
    path.write_bytes(("\n".join(block("python", kind="ast")) + "\nreal_code()\n" +
                      "\n".join(block("python", "safe", kind="ast")) + "\nsafe_code()\n").encode())
    raw, mtime = path.read_bytes(), path.stat().st_mtime_ns
    original = nmc.source_line_edit

    def crossing(snapshot, start, stop, rows, owner):
        return original(snapshot, start, stop + 1, rows, owner)

    with monkeypatch.context() as patch:
        patch.setattr(nmc, "source_line_edit", crossing)
        failed = execute(path, "delete")
    assert failed.get("error_kind") == "plan_mismatch", failed
    assert failed["evidence"]["expected"] != failed["evidence"]["actual"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, mtime)
    good = execute(path, "update", bid="safe")
    assert good.get("success"), good
    assert path.read_bytes() == raw.replace(b"id=safe,\n#   slice_labels=old,", b"id=safe,\n#   slice_labels=new,")


def test_incomplete_neighbour_does_not_veto_good_update(tmp_path):
    path = tmp_path / "damaged.py"
    damaged = b"# @beacon[\n# id=damaged,\n# kind=ast,\n"
    path.write_bytes(damaged + ("\n".join(block("python", kind="ast")) + "\ntarget()\n").encode())
    result = execute(path, "update")
    assert result.get("success"), result
    assert path.read_bytes().startswith(damaged)


def test_outer_span_coverage_excludes_deleted_inner_metadata(tmp_path):
    path = tmp_path / "nested.py"
    raw = ("\n".join(block("python", "outer")) + "\nbody_a()\n" +
           "\n".join(block("python", kind="ast")) + "\nbody_b()\n" +
           "\n".join(block("python", "outer", close=True)) + "\n").encode()
    path.write_bytes(raw)
    result = execute(path, "delete")
    assert result.get("success"), result
    assert path.read_bytes() == raw.replace(("\n".join(block("python", kind="ast")) + "\n").encode(), b"")


def test_pure_proof_field_splice_and_reusable_offset_map():
    from beacon_edits import BeaconEdit, BeaconEditPlan, verify_beacon_edit_plan
    raw = ("\n".join(block("python", kind="ast")) + "\nkeep()\n").encode()
    snapshot = source_snapshot_from_bytes(raw)
    record = nmc._beacon_edit_records(snapshot, "python")[0]
    owner = record["opener_range"][0]
    a = raw.index(b"slice_labels=old,")
    edit = BeaconEdit(a, a + len(b"slice_labels=old,"), b"slice_labels=longer,new,", owner)
    proof = verify_beacon_edit_plan(BeaconEditPlan(snapshot, "python", (edit,), (owner,), (
        {"owner": owner, "operation": "present", "fields": {"slice_labels": "longer,new"}},
    )))
    assert proof["success"], proof
    assert proof["candidate_bytes"] == raw.replace(b"slice_labels=old,", b"slice_labels=longer,new,")
    assert proof["offset_map"].map_offset(raw.index(b"keep()")) == proof["candidate_bytes"].index(b"keep()")
    assert proof["offset_map"].map_offset(a + 3) is None


def test_wrong_after_state_refuses_and_safe_plan_still_verifies():
    from beacon_edits import BeaconEdit, BeaconEditPlan, verify_beacon_edit_plan
    raw = ("\n".join(block("python", kind="ast")) + "\nkeep()\n").encode()
    snapshot = source_snapshot_from_bytes(raw)
    owner = nmc._beacon_edit_records(snapshot, "python")[0]["opener_range"][0]
    a = raw.index(b"old")
    edit = BeaconEdit(a, a + 3, b"new", owner)
    bad = BeaconEditPlan(snapshot, "python", (edit,), (owner,), (
        {"owner": owner, "operation": "present", "fields": {"slice_labels": "wrong"}},
    ))
    assert verify_beacon_edit_plan(bad)["error_kind"] == "plan_mismatch"
    good = BeaconEditPlan(snapshot, "python", (edit,), (owner,), (
        {"owner": owner, "operation": "present", "fields": {"slice_labels": "new"}},
    ))
    assert verify_beacon_edit_plan(good)["success"]


def test_intended_absence_means_no_physical_block_not_just_no_id():
    from beacon_edits import BeaconEdit, BeaconEditPlan, verify_beacon_edit_plan
    raw = ("\n".join(block("python", kind="ast")) + "\nkeep()\n").encode()
    snapshot = source_snapshot_from_bytes(raw)
    owner = nmc._beacon_edit_records(snapshot, "python")[0]["opener_range"][0]
    start = raw.index(b"id=target,")
    removed_id_only = BeaconEdit(start, start + len(b"id=target,"), b"", owner)
    bad = BeaconEditPlan(snapshot, "python", (removed_id_only,), (owner,), (
        {"owner": owner, "operation": "absent"},
    ))
    assert verify_beacon_edit_plan(bad)["error_kind"] == "plan_mismatch"
    # Id rewrite is an explicit field edit, not a whole-block replacement or
    # an assumption that id equality proves physical identity.
    renamed = BeaconEdit(start, start + len(b"id=target,"), b"id=new-id,", owner)
    good = BeaconEditPlan(snapshot, "python", (renamed,), (owner,), (
        {"owner": owner, "operation": "present", "fields": {"id": "new-id"}},
    ))
    assert verify_beacon_edit_plan(good)["success"]


def test_insertion_at_unterminated_eof_keeps_outer_coverage_and_uses_nearest_ending():
    from beacon_edits import BeaconEditPlan, source_line_edit, verify_beacon_edit_plan
    raw = ("\r\n".join(block("python", "outer")) + "\r\ncode_without_final_newline()").encode()
    snapshot = source_snapshot_from_bytes(raw)
    count = len(snapshot.physical_lines)
    edit = source_line_edit(snapshot, count, count, block("python", "inserted", kind="ast"), "insertion")
    plan = BeaconEditPlan(snapshot, "python", (edit,), insertion_slots=(len(raw),))
    proof = verify_beacon_edit_plan(plan)
    assert proof["success"], proof
    assert proof["candidate_bytes"] == raw + ("\r\n" + "\r\n".join(block("python", "inserted", kind="ast"))).encode()
    # With a mixed file and an unterminated insertion neighbour, its previous
    # ending wins over the first file ending. No global newline choice.
    mixed = source_snapshot_from_bytes(b"first\nsecond\r\nlast")
    splice = source_line_edit(mixed, 2, 2, ["a", "b"], "insertion")
    assert splice.replacement == b"a\r\nb\r\n"


def test_repair_improves_unselected_close_interpretation_and_reports_effect():
    from beacon_edits import BeaconEdit, BeaconEditPlan, verify_beacon_edit_plan
    raw = ("\n".join(block("python", kind="ast")) + "\nbody()\n" +
           "\n".join(block("python", close=True)) + "\n").encode()
    snapshot = source_snapshot_from_bytes(raw)
    record = nmc._beacon_edit_records(snapshot, "python")[0]
    owner = record["opener_range"][0]
    start = raw.index(b"kind=ast,")
    edit = BeaconEdit(start, start + len(b"kind=ast,"), b"kind=span,", owner)
    proof = verify_beacon_edit_plan(BeaconEditPlan(snapshot, "python", (edit,), (owner,), (
        {"owner": owner, "operation": "present", "fields": {"kind": "span"}},
    )))
    assert proof["success"], proof
    assert proof["structural_effects"] == [{"owner": raw.index(b"@beacon-close["),
        "effect": "neighbour_interpretation_changed",
        "before": {"complete": True, "pair_state": "orphan", "terminated_by": "bracket"},
        "after": {"complete": True, "pair_state": "matched", "terminated_by": "bracket"}}]


def test_two_splices_and_insert_offset_affinity():
    from beacon_edits import BeaconEdit, BeaconEditPlan, verify_beacon_edit_plan
    raw = ("\n".join(block("python", kind="ast")) + "\nkeep()\n").encode()
    snapshot = source_snapshot_from_bytes(raw)
    owner = nmc._beacon_edit_records(snapshot, "python")[0]["opener_range"][0]
    a = raw.index(b"old")
    insertion = len(raw)
    inserted = ("\n".join(block("python", "inserted", kind="ast")) + "\n").encode()
    plan = BeaconEditPlan(snapshot, "python", (BeaconEdit(a, a + 3, b"long", owner),
        BeaconEdit(insertion, insertion, inserted, "insertion")), (owner,),
        insertion_slots=(insertion,))
    proof = verify_beacon_edit_plan(plan)
    assert proof["success"], proof
    assert proof["candidate_bytes"] == raw.replace(b"old", b"long") + inserted
    mapping = proof["offset_map"]
    assert mapping.map_offset(insertion, bias="left") == len(raw) + 1
    assert mapping.map_offset(insertion, bias="right") == len(proof["candidate_bytes"])


@pytest.mark.parametrize("language,rows", [
    ("python", ["# @beacon[", "# id=target,", "# slice_labels=old,", "# kind=ast,", "code()"]),
    ("js_ts", ["/* ordinary", " * @beacon[", " * id=target,", " * slice_labels=old,", " * kind=ast,", " * ]", " * ordinary retained", " */", "code();"]),
    ("markdown", ["<!-- ordinary", "@beacon[", "id=target,", "slice_labels=old,", "kind=ast,", "]", "ordinary retained -->", "# H"]),
])
def test_metadata_subrange_is_writable_in_incomplete_or_mixed_comment(language, rows):
    from beacon_edits import BeaconEdit, BeaconEditPlan, verify_beacon_edit_plan
    raw = ("\r\n".join(rows) + "\r\n").encode()
    snapshot = source_snapshot_from_bytes(raw)
    record = nmc._beacon_edit_records(snapshot, language)[0]
    owner = record["opener_range"][0]
    start = raw.index(b"old")
    edit = BeaconEdit(start, start + 3, b"new,long", owner)
    proof = verify_beacon_edit_plan(BeaconEditPlan(snapshot, language, (edit,), (owner,), (
        {"owner": owner, "operation": "present", "fields": {"slice_labels": "new,long"}},
    )))
    assert proof["success"], proof
    assert proof["candidate_bytes"] == raw.replace(b"old", b"new,long")


def test_delimiter_repair_does_not_require_unchanged_diagnostic_count():
    from beacon_edits import BeaconEdit, BeaconEditPlan, verify_beacon_edit_plan
    damaged = b"# @beacon[\r\n# id=damaged,\r\n# kind=ast,"
    neighbour = ("\r\n" + "\r\n".join(block("python", kind="ast")) + "\r\ntarget()\r\n").encode()
    raw = damaged + neighbour
    snapshot = source_snapshot_from_bytes(raw)
    owner = nmc._beacon_edit_records(snapshot, "python")[0]["opener_range"][0]
    plan = BeaconEditPlan(snapshot, "python", (BeaconEdit(len(damaged), len(damaged), b"\r\n# ]", owner),),
        (owner,), ({"owner": owner, "operation": "present", "fields": {"id": "damaged"}},),
        insertion_slots=(len(damaged),))
    proof = verify_beacon_edit_plan(plan)
    assert proof["success"], proof
    assert proof["candidate_bytes"] == damaged + b"\r\n# ]" + neighbour


@pytest.mark.parametrize("broken", ["code", "missing_field_edit"])
def test_metadata_replacement_cannot_smuggle_code_and_safe_neighbour_commits(tmp_path, monkeypatch, broken):
    path = tmp_path / "after-state.py"
    path.write_bytes(("\n".join(block("python", kind="ast")) + "\nreal_code()\n" +
        "\n".join(block("python", "safe", kind="ast")) + "\nsafe_code()\n").encode())
    before = path.read_bytes(), path.stat().st_mtime_ns
    original = nmc._build_beacon_block_for_language
    def wrong(*args, **kwargs):
        from dataclasses import replace
        edits = original(*args, **kwargs)
        return [replace(edits[0], replacement=edits[0].replacement + b"\nunrequested_code()")] if broken == "code" else edits[:-1]
    with monkeypatch.context() as patch:
        patch.setattr(nmc, "_build_beacon_block_for_language", wrong)
        result = execute(path, "update")
    assert result["error_kind"] == "plan_mismatch", result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert execute(path, "update", bid="safe").get("success")


def test_unselected_paired_close_cannot_lose_attachment_but_incomplete_close_may_remain(tmp_path, monkeypatch):
    path = tmp_path / "pair-proof.py"
    raw = ("\n".join(block("python")) + "\nbody()\n" +
        "\n".join(block("python", close=True)) + "\n" +
        "\n".join(block("python", "safe", kind="ast")) + "\nsafe()\n").encode()
    path.write_bytes(raw)
    before = path.read_bytes(), path.stat().st_mtime_ns
    # Simulate an incorrect candidate that forgets a uniquely paired close.
    original = nmc.commit_beacon_edit_plan
    def forget_close(plan, **kwargs):
        from dataclasses import replace
        return original(replace(plan, edits=plan.edits[:1], target_witness=plan.target_witness[:1],
                                intended_state=plan.intended_state[:1]), **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(nmc, "commit_beacon_edit_plan", forget_close)
        failed = execute(path, "delete")
    assert failed.get("error_kind") == "plan_mismatch", failed
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert execute(path, "update", bid="safe").get("success")
    # An already-incomplete/unpaired close is not a blanket veto on safe
    # opener removal. It remains byte-identical and independently fixable.
    incomplete = b"# @beacon-close[\n#   id=target,\n"
    tail = incomplete + ("\n".join(block("python", "safe", kind="ast")) + "\nsafe()\n").encode()
    opening = ("\n".join(block("python")) + "\n").encode()
    path.write_bytes(opening + b"body()\n" + tail)
    result = execute(path, "delete")
    assert result.get("success"), result
    assert path.read_bytes() == b"body()\n" + tail


LANGUAGES = ["python", "js_ts", "vue", "markdown", "sql", "shell", "yaml"]
REGIMES = ["lf", "crlf", "cr", "mixed", "bom", "no-eof", "trailing", "controls"]


def legacy(path, language, *, bid="target", labels=True, note=None):
    """Call the production no-plan writer, including its existing note policy."""
    if note is None:
        note = f"BEACON (test)\nid: {bid}\nkind: span\n---\n"
    return getattr(nmc, f"update_beacon_from_node_{language}")(
        str(path), "Target #new" if labels else "Target", note,
    )


def assert_outside_edits_identical(before, after, result):
    """Independently compare each untouched segment in raw byte coordinates."""
    old_cursor = new_cursor = 0
    for edit in sorted(result["edited_ranges"], key=lambda e: e["start_byte"]):
        start, stop = edit["start_byte"], edit["end_byte"]
        size = start - old_cursor
        assert before[old_cursor:start] == after[new_cursor:new_cursor + size]
        old_cursor = stop
        new_cursor += size + edit["replacement_bytes"]
    assert before[old_cursor:] == after[new_cursor:]


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize("regime", REGIMES)
def test_legacy_writers_preserve_every_outside_byte(tmp_path, language, operation, regime):
    """b8dbedd: every legacy writer globally serializes/sanitizes the file."""
    ending = {"crlf": "\r\n", "cr": "\r"}.get(regime, "\n")
    literal = "\x1b\f\v\x1c\x1d\x1e\x85\u2028\u2029" if regime == "controls" else "literal"
    prefix = (f'TEXT = "{literal}"' + ending).encode()
    if language == "vue":
        prefix = b"<script>\n" + prefix
    if regime == "bom":
        prefix = b"\xef\xbb\xbf" + prefix
    opening = (ending.join(block(language)) + ending).encode()
    body = b"keep_this_code()\r\n" if regime == "mixed" else ("keep_this_code()" + ending).encode()
    closing = (ending.join(block(language, close=True)) + ending).encode()
    suffix = (ending.join(block(language, "safe", kind="ast")) + ending + "safe()" + ending).encode()
    if regime == "trailing":
        suffix += (ending * 3).encode()
    if regime == "no-eof":
        suffix = suffix[:-len(ending)]
    if language == "vue":
        suffix += b"\n</script>"
    raw = prefix + opening + body + closing + suffix
    path = tmp_path / "source.txt"
    path.write_bytes(raw)
    os.chmod(path, 0o744)
    result = legacy(path, language, labels=operation == "update")
    assert result.get("operation") == ("updated_beacon" if operation == "update" else "deleted_beacon"), result
    written = path.read_bytes()
    assert written.startswith(prefix) and written.endswith(suffix)
    assert body in written
    if operation == "delete":
        assert written == prefix + body + suffix
    else:
        updated = block(language)
        replacement = (ending.join(updated) + ending).encode().replace(b"slice_labels=old,", b"slice_labels=new,")
        assert written == prefix + replacement + body + closing + suffix
    assert result.get("success"), result
    assert_outside_edits_identical(raw, written, result)
    assert result["before_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["after_sha256"] == hashlib.sha256(written).hexdigest()
    assert stat.S_IMODE(path.stat().st_mode) == 0o744


@pytest.mark.parametrize("language,note,source,suffix", [
    ("python", "AST_QUALNAME: target\n---\n", "def target():\n    return 1", ".py"),
    ("js_ts", "AST_QUALNAME: target\n---\n", "function target() { return 1; }", ".js"),
    ("vue", "AST_QUALNAME: vue:script0:target\n---\n", "<script>\nfunction target() { return 1; }\n</script>", ".vue"),
    ("markdown", "MD_PATH:\n# Target\n---\n", "# Target\nbody", ".md"),
])
@pytest.mark.parametrize("regime", REGIMES)
def test_legacy_create_uses_snapshot_and_preserves_bytes(tmp_path, language, note, source, suffix, regime):
    """b8dbedd: create normalizes endings/EOF and sanitizes unrelated bytes."""
    ending = {"crlf": "\r\n", "cr": "\r"}.get(regime, "\n")
    literal = "\x1b\f\v\x1c\x1d\x1e\x85\u2028\u2029" if regime == "controls" else "literal"
    prefix = ("<!-- " + literal + " -->" if language in {"vue", "markdown"} else
              ("// " if language == "js_ts" else "# ") + literal) + ending
    raw = (prefix + source.replace("\n", ending)).encode()
    if regime == "mixed":
        raw += b"\r\n\n"
    elif regime == "trailing":
        raw += (ending * 3).encode()
    elif regime != "no-eof":
        raw += ending.encode()
    if regime == "bom":
        raw = b"\xef\xbb\xbf" + raw
    path = tmp_path / ("create" + suffix)
    path.write_bytes(raw)
    os.chmod(path, 0o744)
    result = legacy(path, language, note=note)
    assert result.get("operation") == "created_beacon", result
    assert result.get("success"), result
    assert_outside_edits_identical(raw, path.read_bytes(), result)
    assert stat.S_IMODE(path.stat().st_mode) == 0o744


@pytest.mark.parametrize("language", LANGUAGES)
def test_legacy_first_row_delete_keeps_bom(tmp_path, language):
    path = tmp_path / "first.txt"
    opening = ("\r\n".join(block(language, kind="ast")) + "\r\n").encode()
    prefix = b"<script>\r\n" if language == "vue" else b""
    tail = 'TEXT = "\x1b\f\v\x85\u2028\u2029"\r\n\r\n'.encode()
    if language == "vue":
        tail += b"</script>"
    raw = b"\xef\xbb\xbf" + prefix + opening + tail
    path.write_bytes(raw)
    result = legacy(path, language, labels=False)
    assert result.get("success"), result
    assert path.read_bytes() == b"\xef\xbb\xbf" + prefix + tail


@pytest.mark.parametrize("language", LANGUAGES)
def test_legacy_code_crossing_refuses_only_node_and_safe_neighbour_commits(tmp_path, language):
    """All seven selectors withhold unowned deletion before producing an edit;
    incomplete records never authorize a private code-crossing range."""
    path = tmp_path / "crossing.txt"
    prefix = "<script>\n" if language == "vue" else ""
    # The first metadata block is incomplete. Old private selectors consume the
    # safe block's delimiter; the shared record never grants those source bytes.
    damaged = block(language, kind="ast")
    damaged = damaged[:-1]
    code = "# Real code heading" if language == "markdown" else "real_code()"
    raw = (prefix + "\n".join(damaged) + "\n" + code + "\n" +
           "\n".join(block(language, "safe", kind="ast")) + "\nsafe_code()\n" +
           ("</script>" if language == "vue" else "")).encode()
    path.write_bytes(raw)
    before = raw, path.stat().st_mtime_ns
    result = legacy(path, language, labels=False)
    assert result.get("error_kind") == "beacon_extent_unreadable", result
    assert result['candidates'][0]['complete'] is False
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    good = legacy(path, language, bid="safe")
    assert good.get("success"), good
    assert_outside_edits_identical(raw, path.read_bytes(), good)


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("fault", ["external_save", "fsync"])
def test_legacy_commit_failure_is_local_and_next_same_file_node_proceeds(tmp_path, monkeypatch, language, fault):
    """b8dbedd: no-plan writers have no initial fingerprint or typed failure."""
    path = tmp_path / "local.txt"
    raw = ("\n".join(block(language, kind="ast")) + "\ntarget()\n" +
           "\n".join(block(language, "safe", kind="ast")) + "\nsafe()\n").encode()
    if language == "vue":
        raw = b"<script>\n" + raw + b"</script>"
    path.write_bytes(raw)
    before = raw, path.stat().st_mtime_ns
    external = raw + b"\neditor_save()\r\n"
    state = {}
    with monkeypatch.context() as patch:
        if fault == "external_save":
            original = nmc.read_source_snapshot
            def observe_then_save(*args, **kwargs):
                snapshot = original(*args, **kwargs)
                path.write_bytes(external)
                state["mtime"] = path.stat().st_mtime_ns
                return snapshot
            patch.setattr(nmc, "read_source_snapshot", observe_then_save)
        else:
            def fail(*_args, **_kwargs):
                raise PermissionError("injected fsync failure")
            patch.setattr(os, "fsync", fail)
        result = legacy(path, language)
    assert result.get("error_kind") == ("source_changed_since_plan" if fault == "external_save" else "disk_write_failed"), result
    assert result.get("error") and result.get("success") is False
    assert result["evidence"]["expected"] != result["evidence"]["actual"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == ((external, state["mtime"]) if fault == "external_save" else before)
    assert not list(tmp_path.glob(".beacon_atomic_*"))
    good_before = path.read_bytes()
    good = legacy(path, language, bid="safe")
    assert good.get("success"), good
    assert_outside_edits_identical(good_before, path.read_bytes(), good)


@pytest.mark.parametrize("language,source,qualname,suffix,outline_name", [
    ("python", "def target():\r\n    return 1", "target", ".py", "parse_file_outline"),
    ("js_ts", "function target() { return 1; }", "target", ".js", "parse_js_ts_outline"),
    ("vue", "function target() { return 1; }\r\n</script>", "vue:script0:target", ".vue", "parse_vue_outline"),
    ("markdown", "# Target\r\nbody", None, ".md", "parse_markdown_structure"),
])
def test_legacy_qualname_delete_outline_and_ranges_share_one_snapshot(tmp_path, monkeypatch, language, source, qualname, suffix, outline_name):
    """A2-D14: old nested deletes parse disk again (Markdown then rereads again)."""
    path = tmp_path / ("delete" + suffix)
    opening = ("\r\n".join(block(language, kind="ast")) + "\r\n").encode()
    prefix = b"\xef\xbb\xbf" + (b"<script>\r\n" if language == "vue" else b"")
    raw = prefix + opening + source.encode()
    path.write_bytes(raw)
    note = f"AST_QUALNAME: {qualname}\n---\n" if qualname else "MD_PATH:\n# Target\n---\n"
    original_read = nmc.read_source_snapshot
    original_outline = getattr(nmc, outline_name)
    observations, outlines = [], []
    def read(*args, **kwargs):
        snapshot = original_read(*args, **kwargs)
        observations.append(snapshot)
        return snapshot
    def outline(*args, **kwargs):
        assert kwargs.get("snapshot") is observations[0]
        outlines.append(kwargs["snapshot"])
        return original_outline(*args, **kwargs)
    monkeypatch.setattr(nmc, "read_source_snapshot", read)
    monkeypatch.setattr(nmc, outline_name, outline)
    result = legacy(path, language, labels=False, note=note)
    assert result.get("operation") == "deleted_beacon", result
    assert result.get("success"), result
    assert len(observations) == len(outlines) == 1
    assert path.read_bytes() == prefix + source.encode()


@pytest.mark.parametrize("section,opener,closer,body", [
    ("template", "<!-- @beacon[", "] -->", "<div>keep</div>"),
    ("style", "/* @beacon[", "] */", ".keep { color: red; }"),
])
@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize("regime", REGIMES)
def test_legacy_vue_template_style_capability_keeps_bytes(tmp_path, section, opener, closer, body, operation, regime):
    """Legacy non-script capabilities stay available; no plan-only gate added."""
    ending = {"crlf": "\r\n", "cr": "\r"}.get(regime, "\n")
    literal = "\x1b\f\v\x85\u2028\u2029" if regime == "controls" else "literal"
    outside = f'<p title="{literal}">x</p>' if section == "template" else f'.literal {{ content: "{literal}"; }}'
    prefix = (f"<{section}>" + ending + outside + ending).encode()
    if regime == "bom":
        prefix = b"\xef\xbb\xbf" + prefix
    opening = (ending.join([opener, "id=target,", "slice_labels=old,", "kind=span,", closer]) + ending).encode()
    content = (body + ("\r\n" if regime == "mixed" else ending)).encode()
    closing = (ending.join([opener.replace("@beacon[", "@beacon-close["), "id=target,", closer]) + ending).encode()
    suffix = (ending + f"</{section}>" + ("" if regime == "no-eof" else ending)).encode()
    if regime == "trailing":
        suffix += (ending * 3).encode()
    raw = prefix + opening + content + closing + suffix
    path = tmp_path / "sections.vue"
    path.write_bytes(raw)
    os.chmod(path, 0o744)
    result = legacy(path, "vue", labels=operation == "update")
    assert result.get("operation") == ("updated_beacon" if operation == "update" else "deleted_beacon"), result
    assert result.get("success"), result
    assert_outside_edits_identical(raw, path.read_bytes(), result)
    assert path.read_bytes().startswith(prefix) and path.read_bytes().endswith(suffix)
    assert content in path.read_bytes()
    if operation == "delete":
        assert path.read_bytes() == prefix + content + suffix
    assert stat.S_IMODE(path.stat().st_mode) == 0o744
