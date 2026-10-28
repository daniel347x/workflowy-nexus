"""Whole-note diff reporting on the existing edit/admission/snapshot rails.

All runtime state is isolated by the suite/harness; only live client GET and
update are mocked. No production Workflowy or operational files are written.
"""
from __future__ import annotations

import asyncio
import copy
import html
import json
from unittest.mock import AsyncMock, Mock

import pytest
from coherence_harness import make_boundary_capable, make_coherence_root, s04_raise_barrier
from n05_region_harness import isolate_claim_scope

from workflowy_mcp import server
from workflowy_mcp.client import api_client_nexus as nx
from workflowy_mcp.coherence.errors import CoherenceBarrierActiveError
from workflowy_mcp.coherence.operation_cache import current_operation_cache
from workflowy_mcp.mcp_api_registry import dispatch_operation, validate_params
from workflowy_mcp.models.node import WorkFlowyNode

ROOT = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"
NAME = "[ap:DIFF01] Existing #keep"


def setup(monkeypatch, tmp_path, before="old note"):
    paths = make_coherence_root(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    client = make_boundary_capable(object.__new__(server.WorkFlowyClient), nodes=[
        {"id": ROOT, "name": "[ap:DIFF01] STALE #cache", "note": "STALE CACHE NOTE", "parent_id": None, "priority": 10},
        {"id": CHILD, "name": "Keep child", "note": "child body", "parent_id": ROOT},
    ])
    isolate_claim_scope(client, tmp_path)
    current = WorkFlowyNode(id=ROOT, name=NAME, note=html.escape(before) if before is not None else None)
    client.get_node = AsyncMock(return_value=current)
    client.update_node = AsyncMock(return_value=WorkFlowyNode(id=ROOT, name=NAME, note="updated"))
    client.create_node = AsyncMock(side_effect=AssertionError("no create"))
    client.move_node = AsyncMock(side_effect=AssertionError("no move"))
    client.delete_node = AsyncMock(side_effect=AssertionError("no delete"))
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    return client, paths, current


def dispatch(**params):
    return asyncio.run(dispatch_operation(
        server.build_node_registry(), "edit", {"node_handle": ROOT, **params},
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    ))


def note_input(tmp_path, source, text):
    if source == "note":
        return {"note": text}, text
    path = tmp_path / "ignored title.md"
    path.write_bytes(text.encode("utf-8"))
    return {"note_file": str(path)}, text.replace("\r\n", "\n").replace("\r", "\n")


def parse_report(report):
    assert isinstance(report, str), report
    meta, end = json.JSONDecoder().raw_decode(report)
    suffix = report[end:]
    # The diff block is either the unified diff, the [ap:4JSBO5] empty-old-note
    # line, or nothing at all (note_changed=false).
    assert (
        suffix == "\n"
        or suffix.startswith("\n\n--- note (before)\n+++ note (after)\n")
        or suffix.startswith("\n\nnote was empty;")
    ), repr(suffix[:80])
    assert "diff" not in meta
    return meta, suffix[2:] if suffix.startswith("\n\n") else ""


@pytest.mark.parametrize("source", ["note", "note_file"])
@pytest.mark.parametrize("before,after", [
    ("old note", "new note"),
    ("old note", ""),
    ("", "new note"),
    (None, ""),
    ("", ""),
    ("same\n", "same\n"),
    ("π📊 &lt; <x> & \u00a0", "λ✅ &lt; <y> & \u00a0"),
    ("A\r\nB\r", "A\r\nC\r"),
    ("same", "same\n"),
    ("same\r\n", "same\r\n"),
    ("same\r", "same\n"),
    ("\ufeff# old\n", "\ufeff# new\r\n"),
])
def test_whole_note_report_uses_live_snapshot_and_existing_formatters(monkeypatch, tmp_path, source, before, after):
    client, _, current = setup(monkeypatch, tmp_path, before)
    params, expected = note_input(tmp_path, source, after)
    expected_diff = (
        server._render_note_unified_diff(before or "", expected)
        if (before or "") != expected else ""
    )
    original = current.model_dump()
    view = client._cache_view_publisher.capture()
    snapshot = copy.deepcopy(view.nodes_export)
    diff_formatter = Mock(wraps=server._render_note_unified_diff)
    report_formatter = Mock(wraps=server._render_note_edit_report)
    decoder = Mock(wraps=server._note_wire_to_semantic)
    monkeypatch.setattr(server, "_render_note_unified_diff", diff_formatter)
    monkeypatch.setattr(server, "_render_note_edit_report", report_formatter)
    monkeypatch.setattr(server, "_note_wire_to_semantic", decoder)

    result = dispatch(return_diff=True, **params)
    meta, diff = parse_report(result)
    changed = (before or "") != expected
    if source == "note_file":
        assert meta.pop("path_record")["requested"] == params["note_file"]
    else:
        assert "path_record" not in meta
    assert meta == {"success": True, "no_op": False, "note_changed": changed, "node_id": ROOT, "note_chars": len(expected)}
    assert diff == expected_diff
    report_formatter.assert_called_once_with(meta, diff)
    if changed:
        diff_formatter.assert_called_once_with(before or "", expected, mode="full")
    else:
        diff_formatter.assert_not_called()
    decoder.assert_called_once_with(current.note or "")
    client.get_node.assert_awaited_once_with(ROOT)
    client.update_node.assert_awaited_once()
    node_id, request = client.update_node.await_args.args
    assert node_id == ROOT and request.note == expected
    assert request.name is None and request.layoutMode is None
    assert current.model_dump() == original
    assert client._cache_view_publisher.capture() is view and view.nodes_export == snapshot
    client.create_node.assert_not_awaited()
    client.move_node.assert_not_awaited()
    client.delete_node.assert_not_awaited()
    assert current_operation_cache(required=False) is None
    if source == "note_file":
        assert (tmp_path / "ignored title.md").read_bytes() == after.encode("utf-8")


@pytest.mark.parametrize("source", ["note", "note_file"])
@pytest.mark.parametrize("after", ["new", "old note", ""])
def test_whole_note_diff_is_the_public_default(monkeypatch, tmp_path, source, after):
    # [ap:4JSBO5]: the flag omitted on a whole-note replacement now returns
    # the raw diff report (the default receipt), costing the ONE memoized
    # live GET for the before text.
    client, _, _ = setup(monkeypatch, tmp_path, "old note")
    params, expected = note_input(tmp_path, source, after)
    result = dispatch(**params)
    assert isinstance(result, str)
    meta, diff = parse_report(result)
    changed = "old note" != expected
    if source == "note_file":
        assert meta.pop("path_record")["requested"] == params["note_file"]
    assert meta == {"success": True, "no_op": False, "note_changed": changed, "node_id": ROOT, "note_chars": len(expected)}
    if changed:
        assert diff
    else:
        assert diff == ""
    client.get_node.assert_awaited_once_with(ROOT)
    assert client.update_node.await_count == 1
    assert client.update_node.await_args.args[1].note == expected


@pytest.mark.parametrize("source", ["note", "note_file"])
@pytest.mark.parametrize("after", ["new", "old note", ""])
def test_explicit_false_returns_only_the_uniform_header(monkeypatch, tmp_path, source, after):
    # Alias false = none on every note write: no diff rendering or body echo.
    # One live snapshot still supplies truthful note_changed metadata.
    client, _, _ = setup(monkeypatch, tmp_path, "old note")
    params, expected = note_input(tmp_path, source, after)
    meta, diff = parse_report(dispatch(**{**params, "return_diff": False}))
    expected_keys = {"success", "no_op", "note_changed", "node_id", "note_chars"}
    if source == "note_file":
        expected_keys.add("path_record")
    assert set(meta) == expected_keys
    assert meta["success"] and not meta["no_op"]
    assert meta["note_changed"] == ("old note" != expected)
    assert meta["node_id"] == ROOT and meta["note_chars"] == len(expected)
    assert diff == ""
    client.get_node.assert_awaited_once_with(ROOT)
    assert client.update_node.await_count == 1
    assert client.update_node.await_args.args[1].note == expected


def test_name_only_ack_carries_no_note_markers(monkeypatch, tmp_path):
    # PHO4BG: a name/layout edit writes no note, so the acknowledgement
    # carries no note fields at all — and still never a "note": null.
    client, _, _ = setup(monkeypatch, tmp_path)
    result = dispatch(name="Renamed")
    assert result["success"] is True and result["changed"] is True
    detail = result["detail"]
    assert isinstance(detail, dict) and detail["id"] == ROOT
    for absent in ("note", "children", "no", "ch", "note_written", "note_chars", "note_lines"):
        assert absent not in detail
    client.get_node.assert_awaited_once()
    client.update_node.assert_awaited_once()


@pytest.mark.parametrize("source", ["note", "note_file"])
@pytest.mark.parametrize("handle", [ROOT, ROOT[:8], "[ap:DIFF01]"])
def test_combined_name_layout_note_reuses_live_snapshot(monkeypatch, tmp_path, source, handle):
    client, _, _ = setup(monkeypatch, tmp_path)
    params, _ = note_input(tmp_path, source, "new")
    result = dispatch(node_handle=handle, name="Renamed", layout_mode="h2", return_diff=True, **params)
    assert parse_report(result)[0]["node_id"] == ROOT
    request = client.update_node.await_args.args[1]
    assert request.name == "[ap:DIFF01] Renamed #keep"  # never the stale cache name/tags
    assert request.note == "new" and request.layoutMode == "h2"
    client.get_node.assert_awaited_once_with(ROOT)
    client.update_node.assert_awaited_once()


def test_canonical_auto_link_decode_is_old_side_only(monkeypatch, tmp_path):
    client, _, _ = setup(monkeypatch, tmp_path)
    wire = '<a href="https://example.test/?x=1&amp;y=2">https://example.test/?x=1&amp;y=2</a> &amp;lt;'
    before = "https://example.test/?x=1&y=2 &lt;"
    client.get_node.return_value = WorkFlowyNode(id=ROOT, name=NAME, note=wire)
    result = dispatch(note=before, return_diff=True)
    assert parse_report(result) == ({"success": True, "no_op": False, "note_changed": False, "node_id": ROOT, "note_chars": len(before)}, "")
    assert client.update_node.await_args.args[1].note == before


@pytest.mark.parametrize("bad", [None, 0, 1, "true", "false", [], {}])
@pytest.mark.parametrize("source", ["note", "note_file"])
def test_invalid_flags_refuse_before_boundary_file_or_live_read(monkeypatch, tmp_path, bad, source):
    client, _, _ = setup(monkeypatch, tmp_path)
    reader = Mock(side_effect=AssertionError("no file read"))
    monkeypatch.setattr(nx, "_read_note_file_text", reader)
    result = dispatch(return_diff=bad, **{source: "not read"})
    assert result["error"]["code"] == "invalid_parameter_type"
    assert result["operation_doc"] == server.build_node_registry().describe_full("edit")
    reader.assert_not_called()
    client.get_node.assert_not_awaited()
    client.update_node.assert_not_awaited()


@pytest.mark.parametrize("bad", [None, 0, 1, "true", [], {}])
def test_core_validates_flag_without_client_access(monkeypatch, bad):
    client_factory = Mock(side_effect=AssertionError("no client access"))
    monkeypatch.setattr(server, "get_client", client_factory)
    with pytest.raises(ValueError, match="return_diff must be a boolean"):
        asyncio.run(server._node_edit_core(ROOT, note="new", return_diff=bad))
    client_factory.assert_not_called()


@pytest.mark.parametrize("params", [
    {}, {"name": "Renamed"}, {"layout_mode": "h2"},
    {"name": "Renamed", "layout_mode": "todo", "note": None, "note_file": None},
    {"note": "whole", "note_search": "part"},
    {"note": "whole", "note_edits": [{"search": "part"}]},
    {"note_search": ""}, {"note_replace": "unpaired"},
    {"note_edits": [{"search": "x", "return_diff": True}]},
    {"note": "whole", "replace_all": True},
    {"note_file": "not-read", "replace_all": True},
    {"note_file": "not-read", "note": ""},
    {"note_file": "not-read", "note_search": "x"},
    {"note_file": "not-read", "note_replace": "x"},
    {"note_file": "not-read", "note_edits": []},
])
def test_inapplicable_and_conflicting_modes_are_zero_write(monkeypatch, tmp_path, params):
    client, _, _ = setup(monkeypatch, tmp_path)
    reader = Mock(side_effect=AssertionError("reject before file read"))
    monkeypatch.setattr(nx, "_read_note_file_text", reader)
    result = dispatch(return_diff=True, **params)
    assert result["success"] is False
    assert result["operation_doc"] == server.build_node_registry().describe_full("edit")
    reader.assert_not_called()
    client.get_node.assert_not_awaited()
    client.update_node.assert_not_awaited()


@pytest.mark.parametrize("kind", ["missing", "relative", "empty", "directory", "binary", "unreadable"])
def test_file_failure_with_diff_has_no_live_io(monkeypatch, tmp_path, kind):
    client, _, _ = setup(monkeypatch, tmp_path)
    path = tmp_path / "body.md"
    value = str(path)
    if kind == "relative":
        value = "relative.md"
    elif kind == "empty":
        value = " "
    elif kind == "directory":
        value = str(tmp_path)
    elif kind == "binary":
        path.write_bytes(b"\xff\xfe")
    elif kind == "unreadable":
        path.write_text("body", encoding="utf-8")
        # Host-aware file paths ([ap:1T7JZ0]): the note_file read's open()
        # now lives in the ONE resolver module, so the permission failure is
        # simulated there (9.5(e) update — same intent, new home).
        from workflowy_mcp import tool_path_resolution as tpr

        monkeypatch.setattr(tpr, "open", Mock(side_effect=PermissionError("denied")), raising=False)
    result = dispatch(note_file=value, return_diff=True)
    assert result["success"] is False and "note_file" in result["error"]["message"]
    client.get_node.assert_not_awaited()
    client.update_node.assert_not_awaited()


@pytest.mark.parametrize("name,code", [("New #bad", "tags_in_name_rejected"), ("[ap:OTHER] New", "ap_id_mutation_rejected")])
@pytest.mark.parametrize("source", ["note", "note_file"])
def test_protected_names_remain_no_write(monkeypatch, tmp_path, name, code, source):
    client, _, _ = setup(monkeypatch, tmp_path)
    params, _ = note_input(tmp_path, source, "new")
    result = dispatch(name=name, return_diff=True, **params)
    assert result["error"]["code"] == code
    assert result["operation_doc"] == server.build_node_registry().describe_full("edit")
    client.get_node.assert_awaited_once()
    client.update_node.assert_not_awaited()


@pytest.mark.parametrize("params", [{"note": "new"}, {"note_file": "not-read"}])
def test_hard_barrier_precedes_file_read_snapshot_and_update(monkeypatch, tmp_path, params):
    client, paths, _ = setup(monkeypatch, tmp_path)
    s04_raise_barrier(paths)
    reader = Mock(side_effect=AssertionError("no file read"))
    monkeypatch.setattr(nx, "_read_note_file_text", reader)
    result = dispatch(return_diff=True, **params)
    assert result["error"]["code"] == "coherence_barrier_active"
    assert result["operation_doc"] == server.build_node_registry().describe_full("edit")
    reader.assert_not_called()
    client.get_node.assert_not_awaited()
    client.update_node.assert_not_awaited()


@pytest.mark.parametrize("stage", ["read", "update"])
@pytest.mark.parametrize("typed", [False, True])
def test_failures_keep_original_exception_recovery_and_never_return_success_diff(monkeypatch, tmp_path, stage, typed):
    client, _, _ = setup(monkeypatch, tmp_path)
    exc = (
        CoherenceBarrierActiveError("repair required", dirty_generation=9, transaction_id="TX")
        if typed else RuntimeError("original failure; read live before retry")
    )
    if stage == "read":
        client.get_node.side_effect = exc
    else:
        client.update_node.side_effect = exc
    formatter = Mock(side_effect=AssertionError("do not report success on failure"))
    monkeypatch.setattr(server, "_render_note_edit_report", formatter)
    result = dispatch(note="new", return_diff=True)
    assert result["success"] is False
    assert result["operation_doc"] == server.build_node_registry().describe_full("edit")
    if typed:
        assert result["error"]["code"] == "coherence_barrier_active"
        assert result["error"]["extra"] == exc.refusal_extra()
    else:
        assert result["error"]["code"] == "operation_failed"
        assert result["error"]["message"] == f"RuntimeError: {exc}"
    formatter.assert_not_called()
    client.get_node.assert_awaited_once()
    assert client.update_node.await_count == int(stage == "update")


@pytest.mark.parametrize("params", [
    {"note_search": "x", "note_replace": "y"},
    {"note_search": "x", "note_replace": "y", "replace_all": True},
    {"note_search": "absent", "replace_all": True},
    {"note_search": "absent", "name": "Renamed"},
    {"note_search": "x", "note_replace": "x"},
    {"note_edits": [{"search": "x", "replace": "y"}, {"search": "absent"}]},
    {"replace_all": True, "note_edits": [{"search": "x", "replace": "y"}, {"search": "y", "replace": "z", "replace_all": False}]},
    {"note_edits": [{"search": "absent"}]},
])
def test_search_replace_modes_change_only_the_diff_not_header_or_requests(monkeypatch, tmp_path, params):
    client, _, _ = setup(monkeypatch, tmp_path, "x x")
    reports, requests, counts = [], [], []
    for flag in ({}, {"return_diff": False}, {"return_diff": True}):
        client.get_node.reset_mock()
        client.update_node.reset_mock()
        reports.append(dispatch(**params, **flag))
        requests.append([call.args[1].model_dump() for call in client.update_node.await_args_list])
        counts.append((client.get_node.await_count, client.update_node.await_count))
    assert isinstance(reports[0], str)
    headers = [parse_report(report)[0] for report in reports]
    assert headers[0] == headers[1] == headers[2]
    assert parse_report(reports[1])[1] == ""  # explicit false suppresses search diffs too
    assert requests[0] == requests[1] == requests[2]
    assert counts[0] == counts[1] == counts[2]
    assert counts[0][0] == 1 and counts[0][1] <= 1


def test_large_diff_is_clamped_with_one_elision_row_naming_hunks_and_totals(monkeypatch, tmp_path):
    # [ap:4JSBO5]: the default-on diff is size-guarded by the ONE shared
    # clamp (snippet_view.clamp_span_to_head_tail at DEFAULT_NOTE_MAX_LINES):
    # head + tail + ONE elision row naming the omitted region, the hidden
    # @@ hunks and the whole-diff totals.
    import difflib

    before_lines = [f"line {i}" for i in range(2000)]
    after_lines = list(before_lines)
    for i in range(40, 2000, 40):
        after_lines[i] = f"CHANGED {i}"
    before, after = "\n".join(before_lines), "\n".join(after_lines)
    raw_lines = [
        chunk if chunk.endswith("\n") else chunk + "\n"
        for chunk in difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="note (before)",
            tofile="note (after)",
            n=3,
        )
    ]
    raw_lines = [line for chunk in raw_lines for line in chunk.splitlines()]
    assert len(raw_lines) > 400  # the guard must actually fire
    hunk_numbers = [n for n, line in enumerate(raw_lines, start=1) if line.startswith("@@")]
    added = sum(1 for line in raw_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in raw_lines if line.startswith("-") and not line.startswith("---"))

    client, _, _ = setup(monkeypatch, tmp_path, before)
    result = dispatch(note=after, diff="full")  # full retains the existing clamp exactly
    _, diff = parse_report(result)
    lines = diff.splitlines()
    tail_const = server.DEFAULT_NOTE_MAX_LINES // 4
    head_const = server.DEFAULT_NOTE_MAX_LINES - tail_const
    assert len(lines) == server.DEFAULT_NOTE_MAX_LINES + 1  # head + elision + tail
    assert lines[:head_const] == raw_lines[:head_const]
    assert lines[head_const + 1:] == raw_lines[-tail_const:]
    elision = lines[head_const]
    elided_count = len(raw_lines) - server.DEFAULT_NOTE_MAX_LINES
    elided_start, elided_end = head_const + 1, len(raw_lines) - tail_const
    hidden_hunks = sum(1 for n in hunk_numbers if elided_start <= n <= elided_end)
    assert elision == (
        f"... {elided_count} diff lines not shown ({elided_start}-{elided_end}); "
        f"{hidden_hunks} of {len(hunk_numbers)} hunks hidden; "
        f"totals +{added} / -{removed}"
    )
    assert hidden_hunks > 0 and added == removed and added > 0
    client.update_node.assert_awaited_once()


def test_registry_summary_schema_and_examples_disclose_diff():
    registry = server.build_node_registry()
    spec = registry.resolve("edit")
    full = registry.describe_full("edit")
    properties = full["params_schema"]["properties"]
    flag = properties["return_diff"]
    assert flag["type"] == "boolean"
    assert "default" not in flag
    assert "true = full, false = none" in flag["description"]
    mode = properties["diff"]
    assert mode["default"] == "seams"
    assert mode["enum"] == ["seams", "seams_full", "full", "none"]
    assert "EVERY note write" in mode["description"]
    assert "size-guarded" in mode["description"]
    assert "size-guarded" in full["returns"]["description"]
    assert "note was empty" in full["returns"]["description"]
    assert "no output-size cap" not in full["returns"]["description"]
    assert spec.cache_backed and spec.side_effects == ("workflowy_mutation",)
    default_sources = set()
    opt_out_sources = set()
    for example in full["examples"]:
        params = example["call"]["params"]
        validate_params(spec, params)
        note_source = set(params) & {"note", "note_file"}
        if params.get("diff") == "none" and note_source:
            opt_out_sources.update(note_source)
        if "return_diff" not in params and "diff" not in params and note_source:
            default_sources.update(note_source)
    # Both whole-note sources appear once WITHOUT the flag (the default-on
    # receipt) and once with the explicit opt-out.
    assert default_sources == {"note", "note_file"}
    assert opt_out_sources == {"note", "note_file"}
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "node")
    assert "'seams' (default" in tool.description
