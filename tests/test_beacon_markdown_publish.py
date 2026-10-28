"""U08 one-publication, explicit-choice and adapter retirement evidence."""
import ast
import hashlib
import inspect
import json
from pathlib import Path
import stat
import sys

import pytest

import beacon_edits
import beacon_source
import markdown_roundtrip as mdr
import nexus_map_codebase as source
from workflowy_mcp import markdown_generate_pipeline as mgp
from tests.beacon_markdown_fixtures import DUP_SPAN_ROWS, E_CASES, M_FIXTURES
from tests.test_beacon_markdown_preparation import attachments, block, heading


def prepared(path, raw=b"# Good\n", children=None):
    return mdr.prepare_generated_markdown_beacons(raw, {"children": children or [heading("Good #good", "good")]},
        beacon_source.read_source_snapshot(str(path)))


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("stage", ["forest", "metadata", "findings", "eof", "omissions"])
def test_abort_at_each_preparation_stage_leaves_disk_exact_and_neighbour_can_publish(tmp_path, monkeypatch, stage):
    path = tmp_path / "document.md"
    old = b"\xef\xbb\xbf# Old\r\nOriginal prose.\r\n"
    path.write_bytes(old)
    before = path.stat().st_mtime_ns
    owner, name = {"forest": (mdr, "_collect_markdown_ast_beacon_nodes"),
        "metadata": (source, "plan_beacon_node_mutation"), "findings": (mdr, "_markdown_generation_analysis"),
        "eof": (mdr, "_markdown_eof_preparation"), "omissions": (mdr, "_markdown_generation_omissions")}[stage]
    def abort(*a, **kw):
        raise SimulatedCrash(stage)
    with monkeypatch.context() as patch:
        patch.setattr(owner, name, abort)
        with pytest.raises(SimulatedCrash):
            prepared(path)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (old, before)
    assert not list(tmp_path.glob(".beacon_atomic_*"))
    good = prepared(path)
    assert mdr.publish_generated_markdown_beacons(good["publication_plan"])["success"]
    assert path.read_bytes() == good["final_bytes"]


@pytest.mark.parametrize("stage", ["proof", "temp", "fsync", "mode", "replace"])
def test_publish_abort_old_or_new_and_safe_retry_of_independent_plan(tmp_path, monkeypatch, stage):
    path = tmp_path / "document.md"
    old = b"# Old\n"
    path.write_bytes(old)
    before = path.stat().st_mtime_ns
    plan = prepared(path)
    def abort(*a, **kw):
        raise OSError("injected " + stage)
    with monkeypatch.context() as patch:
        if stage == "proof":
            patch.setattr(beacon_edits, "verify_beacon_edit_plan", lambda *a, **kw: {
                "success": False, "changed": False, "error_kind": "plan_mismatch", "error": "injected proof"})
        else:
            owner, name = {"temp": (beacon_edits.tempfile, "mkstemp"), "fsync": (beacon_edits.os, "fsync"),
                           "mode": (beacon_edits.shutil, "copymode"), "replace": (beacon_edits.os, "replace")}[stage]
            patch.setattr(owner, name, abort)
        result = mdr.publish_generated_markdown_beacons(plan["publication_plan"])
    assert not result["success"] and not result["changed"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (old, before)
    assert not list(tmp_path.glob(".beacon_atomic_*"))
    other = tmp_path / "other.md"
    other.write_bytes(old)
    neighbour = prepared(other)
    assert mdr.publish_generated_markdown_beacons(neighbour["publication_plan"])["success"]
    assert other.read_bytes() == neighbour["final_bytes"]


def test_external_save_at_final_hash_is_local_and_leaves_external_bytes(tmp_path):
    path = tmp_path / "document.md"
    path.write_bytes(b"# Old\n")
    plan = prepared(path)
    external = b"# External\r\nDo not overwrite.\r\n"
    observed = {}
    def save():
        path.write_bytes(external)
        observed["mtime"] = path.stat().st_mtime_ns
    result = mdr.publish_generated_markdown_beacons(plan["publication_plan"], before_final_hash=save)
    assert not result["success"] and result["error_kind"] == "source_changed_since_plan"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (external, observed["mtime"])
    assert not list(tmp_path.glob(".beacon_atomic_*"))
    other = tmp_path / "other.md"
    other.write_bytes(b"# Old\n")
    neighbour = prepared(other)
    assert mdr.publish_generated_markdown_beacons(neighbour["publication_plan"])["success"]


def test_success_has_one_atomic_replacement_and_exact_clean_bytes(tmp_path, monkeypatch):
    path = tmp_path / "document.md"
    old = b"\xef\xbb\xbf# Old\r\n"
    path.write_bytes(old)
    path.chmod(0o744)
    result = prepared(path, b"# Good\r\n\r\n")
    expected = b"\xef\xbb\xbf" + block("good", "good", ending="\r\n") + b"# Good\r\n"
    replacements = []
    replace = beacon_edits.os.replace
    def record(src, dst):
        assert path.read_bytes() == old
        assert Path(src).read_bytes() == expected
        replacements.append((src, dst))
        replace(src, dst)
    monkeypatch.setattr(beacon_edits.os, "replace", record)
    committed = mdr.publish_generated_markdown_beacons(result["publication_plan"])
    assert committed["success"] and len(replacements) == 1
    assert path.read_bytes() == expected == result["final_bytes"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o744


def test_crash_immediately_after_atomic_replace_leaves_the_whole_new_document(tmp_path, monkeypatch):
    path = tmp_path / "document.md"
    path.write_bytes(b"# Old\n")
    result = prepared(path)
    replace = beacon_edits.os.replace
    def replace_then_crash(*a, **kw):
        replace(*a, **kw)
        raise SimulatedCrash("after replace")
    with monkeypatch.context() as patch:
        patch.setattr(beacon_edits.os, "replace", replace_then_crash)
        with pytest.raises(SimulatedCrash):
            mdr.publish_generated_markdown_beacons(result["publication_plan"])
    assert path.read_bytes() == result["final_bytes"]
    assert not list(tmp_path.glob(".beacon_atomic_*"))
    other = tmp_path / "other.md"
    other.write_bytes(b"# Old\n")
    assert mdr.publish_generated_markdown_beacons(prepared(other)["publication_plan"])["success"]


def test_forest_only_span_omission_keeps_node_identity_and_verbatim_unknown_metadata(tmp_path):
    path = tmp_path / "document.md"
    path.write_bytes(b"# Good\n")
    note = "\n".join(["BEACON (MD SPAN)", "id: source-span", "kind: span", "slice_labels: span",
                       "start_snippet: opaque snippet", "future_field: opaque original", "---", "SPAN TEXT:", "body"])
    span = {"id": "span-node", "name": "Span #span", "note": note}
    forest = {"children": [heading("Good #good", "good", children=[span])]}
    results, warnings = mdr.reapply_markdown_ast_beacons(str(path), forest)
    assert results[0]["success"]
    omission, = [w["omission"] for w in warnings if w.get("type") == "beacon_omission"]
    assert omission["node_id"] == "span-node" and omission["id"] == "source-span"
    assert omission["original_metadata"]["source_note"] == note
    assert omission["owning_heading"]["md_path"] == ["# Good"]
    assert attachments(path.read_bytes())[0][0]["id"] == "good"


def test_compatibility_adapter_reads_once_publishes_once_and_returns_failed_headings(tmp_path, monkeypatch):
    path = tmp_path / "document.md"
    path.write_bytes(b"# Good\n\n# Other\n")
    counts = {"reads": 0, "replaces": 0}
    read, replace = beacon_source.read_source_snapshot, beacon_edits.os.replace
    def read_once(*a, **kw):
        counts["reads"] += 1
        return read(*a, **kw)
    def replace_once(*a, **kw):
        counts["replaces"] += 1
        return replace(*a, **kw)
    monkeypatch.setattr(beacon_source, "read_source_snapshot", read_once)
    monkeypatch.setattr(beacon_edits.os, "replace", replace_once)
    monkeypatch.setattr(source, "update_beacon_from_node_markdown", lambda *a, **kw: pytest.fail("per-heading disk writer reached"))
    log = []
    forest = {"children": [heading("Missing #bad", "bad"), heading("Good #good", "shared"),
                           heading("Other #other", "shared")]}
    results, warnings = mdr.reapply_markdown_ast_beacons(str(path), forest, log_callback=log.append)
    assert [r["success"] for r in results] == [False, True, True]
    assert counts == {"reads": 1, "replaces": 1}
    assert any(w["beacon_id"] == "bad" and w["file_path"] == str(path) and w["node_id"] for w in warnings if "beacon_id" in w)
    assert any(w.get("finding", {}).get("code") == "duplicate_id" for w in warnings)
    assert [f["slice_labels"] for f, _, _ in attachments(path.read_bytes())] == ["good", "other"]
    assert log[0].startswith("Phase 7: rehydrating") and "2 written, 1 skipped" in log[-1]


def test_adapter_publication_failure_never_claims_a_planned_heading_was_written(tmp_path, monkeypatch):
    path = tmp_path / "document.md"
    raw = b"# Good\n"
    path.write_bytes(raw)
    before = path.stat().st_mtime_ns
    with monkeypatch.context() as patch:
        patch.setattr(beacon_edits.os, "fsync", lambda *a: (_ for _ in ()).throw(OSError("failure")))
        rows, warnings = mdr.reapply_markdown_ast_beacons(str(path), {"children": [heading("Good #good", "good")]})
    assert not rows[0]["success"] and rows[0]["operation"] == "noop"
    assert rows[0]["prepared_operation"] == "created_beacon" and warnings
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, before)
    assert mdr.reapply_markdown_ast_beacons(str(path), {"children": [heading("Good #good", "good")]})[0][0]["success"]


@pytest.mark.parametrize("name,raw,expected", E_CASES)
def test_text_file_and_preparation_share_exact_e01_e08_tail_rule(tmp_path, name, raw, expected):
    assert mgp._normalize_markdown_eof(raw.decode()).encode() == expected
    path = tmp_path / (name + ".md")
    path.write_bytes(raw)
    result = mgp._normalize_markdown_file_eof(str(path))
    assert path.read_bytes() == expected
    assert result["delta_chars"] == len(expected.decode()) - len(raw.decode())
    snapshot = beacon_source.source_snapshot_from_bytes(raw, str(path))
    assert mdr.prepare_generated_markdown_beacons(raw, {"children": []}, snapshot)["final_bytes"] == expected


@pytest.mark.parametrize("keeper", [0, 1])
def test_duplicate_analyzer_is_read_only_then_explicit_u06_rename_changes_chosen_pair_only(tmp_path, keeper):
    path = tmp_path / "span.md"
    raw = ("\n".join(DUP_SPAN_ROWS) + "\n").encode()
    assert hashlib.sha256(raw).hexdigest() == "63daefefb7720a0c8efdd5e61e501d7f14ee57814a4d5e12be143514341b6103"
    path.write_bytes(raw)
    before = path.stat().st_mtime_ns
    report = mgp._disambiguate_duplicate_beacon_ids_in_file(str(path))
    assert not report["changed"] and report["renamed_count"] == 0 and "error" not in report
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, before)
    finding, = report["ambiguities"]
    selected = 1 - keeper
    request = {"finding_id": finding["finding_id"], "resolution": "assign_ids",
               "params": {"keeper": keeper, "new_ids": [{"member": selected, "new_id": "m@chosen"}]}}
    result = mgp._disambiguate_duplicate_beacon_ids_in_file(str(path), selected_resolution=request)
    assert result.get("success") and result["changed"] and result["renamed_count"] == 1, result
    split = raw.index(b"## Section B")
    expected = (raw[:split].replace(b"id=m@sp,", b"id=m@chosen,") + raw[split:]) if selected == 0 else (
        raw[:split] + raw[split:].replace(b"id=m@sp,", b"id=m@chosen,"))
    assert path.read_bytes() == expected
    records = source._beacon_edit_records(beacon_source.read_source_snapshot(str(path)), "markdown")
    assert [r["pair_state"] for r in records] == ["closed", "matched", "closed", "matched"]
    assert not mgp._disambiguate_duplicate_beacon_ids_in_file(str(path))["duplicates"]


def test_stale_explicit_request_is_local_read_only_and_current_choice_proceeds(tmp_path):
    path = tmp_path / "document.md"
    raw = block("shared", "a") + b"# A\n\n" + block("shared", "b") + b"# B\n"
    path.write_bytes(raw)
    before = path.stat().st_mtime_ns
    request = {"finding_id": "stale", "resolution": "assign_ids", "params": {}}
    result = mgp._disambiguate_duplicate_beacon_ids_in_file(str(path), selected_resolution=request)
    assert not result["changed"] and result["error_kind"] == "finding_not_found"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, before)
    finding, = result["ambiguities"]
    request = {"finding_id": finding["finding_id"], "resolution": "assign_ids",
               "params": {"keeper": 1, "new_ids": [{"member": 0, "new_id": "chosen"}]}}
    assert mgp._disambiguate_duplicate_beacon_ids_in_file(str(path), selected_resolution=request)["success"]


@pytest.mark.parametrize("case", ["m02", "m04", "m05", "m10"])
def test_original_recon_fixture_has_accounted_relocation_or_omission(tmp_path, case):
    rows, expected_sha = M_FIXTURES[case]
    raw = ("\n".join(rows) + "\n").encode()
    assert hashlib.sha256(raw).hexdigest() == expected_sha
    path = tmp_path / (case + ".md")
    path.write_bytes(raw)
    snapshot = beacon_source.read_source_snapshot(str(path))
    forest = {"children": source.parse_markdown_structure(str(path), snapshot=snapshot)}
    generated = raw
    # This is the recon's beacon-free phase-6 proxy, using U01 extents rather
    # than the recon's regex. Production receives already generated bytes.
    records = source._beacon_edit_records(snapshot, "markdown")
    for record in reversed(records):
        a, b = record["line_extent"]
        generated = generated[:snapshot.byte_offsets[a]] + generated[snapshot.byte_offsets[b + 1]:]
    result = mdr.prepare_generated_markdown_beacons(generated, forest, snapshot)
    row, = result["relocations"] if case == "m02" else result["omissions"]
    assert row["original_metadata"]["fields"]["id"] in {"h-a", "h-x", "h-f"}
    assert row["available_actions"] and row["file"] == str(path)
    assert mdr.publish_generated_markdown_beacons(result["publication_plan"])["success"]


def test_retired_authorities_have_no_callsite_in_the_active_adapters():
    tree = ast.parse(inspect.getsource(mdr.reapply_markdown_ast_beacons))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    names = [ast.unparse(n.func).split(".")[-1] for n in calls]
    assert names.count("read_source_snapshot") == names.count("prepare_generated_markdown_beacons") == names.count("publish_generated_markdown_beacons") == 1
    assert "update_beacon_from_node_markdown" not in names
    loops = [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While))]
    assert not any(isinstance(n, ast.Call) and ast.unparse(n.func).endswith("publish_generated_markdown_beacons")
                   for loop in loops for n in ast.walk(loop))
    duplicate = inspect.getsource(mgp._disambiguate_duplicate_beacon_ids_in_file)
    for retired in ("_next_free_id", "_find_id_line_index", "id_line_re", "blks[1:]"):
        assert retired not in duplicate
    assert "splitlines" not in inspect.getsource(mgp._normalize_markdown_eof)


def test_clean_adapter_golden_and_repeat_keep_own_bytes(tmp_path):
    path = tmp_path / "clean.md"
    path.write_bytes(b"# A\n\nbody\n\n# B\n")
    forest = {"children": [heading("A #a", "a"), heading("B #b", "b")]}
    expected = block("a", "a") + b"# A\n\nbody\n\n" + block("b", "b") + b"# B\n"
    first, warnings = mdr.reapply_markdown_ast_beacons(str(path), forest)
    assert not warnings and all(r["success"] for r in first)
    assert path.read_bytes() == expected
    second, warnings = mdr.reapply_markdown_ast_beacons(str(path), forest)
    assert not warnings and all(r["success"] for r in second)
    assert path.read_bytes() == expected


def test_roundtrip_strips_all_scoped_ast_records_not_body_text():
    from tests.test_beacon_markdown_preparation import note
    node = heading("Group #group")
    node["note"] = note("one", "first") + "\n" + note("two", "second") + "\n\nordinary body"
    raw = "\n".join(mdr.nexus_to_tokens({"children": [node]})).encode()
    assert b"BEACON (" not in raw and b"ordinary body" in raw
    result = mdr.prepare_generated_markdown_beacons(raw, {"children": [node]},
        beacon_source.source_snapshot_from_bytes(b"old\n", "group.md"))
    assert [(f["id"], f["comment"]) for f, _, _ in attachments(result["final_bytes"])] == [("one", "first"), ("two", "second")]


def test_inline_span_header_mention_does_not_omit_heading_or_descendants():
    node = heading("Ordinary #ordinary", "ordinary", children=[heading("Child #child", "child")])
    node["note"] += "\nBody mentions BEACON (MD SPAN) inline, not as a metadata header."
    forest = {"children": [node]}
    assert not mdr._is_markdown_span_beacon_node(node)
    generated = "\n".join(mdr.nexus_to_tokens(forest)).encode()
    result = mdr.prepare_generated_markdown_beacons(generated, forest,
        beacon_source.source_snapshot_from_bytes(b"old\n", "mention.md"))
    assert [f["id"] for f, _, _ in attachments(result["final_bytes"])] == ["ordinary", "child"]


@pytest.mark.parametrize("exists", [True, False])
def test_cli_uses_one_pre_generation_snapshot_and_one_publication(tmp_path, monkeypatch, capsys, exists):
    path = tmp_path / "output.md"
    input_path = tmp_path / "input.json"
    forest = {"name": "source.md", "children": [heading("Good #good", "good")]}
    input_path.write_text(json.dumps({"export_root_id": "file-root", "nodes": [forest]}))
    if exists:
        path.write_bytes(b"\xef\xbb\xbf# Old\n")
    calls = []
    replace = beacon_edits.os.replace
    def record(*a, **kw):
        calls.append(a)
        return replace(*a, **kw)
    monkeypatch.setattr(beacon_edits.os, "replace", record)
    monkeypatch.setattr(sys, "argv", ["markdown_roundtrip.py", str(input_path), "--output", str(path)])
    mdr.main()
    assert len(calls) == 1
    assert path.read_bytes() == (b"\xef\xbb\xbf" if exists else b"") + block("good", "good") + b"# Good\n"
    output = capsys.readouterr().out
    assert "Markdown AST beacons reapplied: 1" in output
    assert "Findings: 0; omissions: 0; relocations: 0" in output
