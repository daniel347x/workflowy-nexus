"""The shared note-read guard at the public node.read front (G1-2a).

One guard, one flag set with beacon.snippet (one implementation:
snippet_view.render_guarded_lines): a whole-note read longer than max_lines
(default 400 = 300 head + 100 tail) returns the name banner, a [note: ...]
size header, head rows, ONE elision row naming the hidden range and the
window that reads it back, then tail rows; size_only returns the header
alone. max_lines: 0, explicit windows (offset/limit/start_line/end_line),
output_file, as_raw_node=false and internal callers are never guarded.
Only transport is faked (same harness as test_node_lines).
"""
from __future__ import annotations

import asyncio
import html
import re
from unittest.mock import AsyncMock

import pytest
from coherence_harness import make_boundary_capable
from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation
from workflowy_mcp.models.node import WorkFlowyNode
from workflowy_mcp.note_lines import split_note_lines
from workflowy_mcp.snippet_view import DEFAULT_NOTE_MAX_LINES, normalize_note_view_params

ROOT = "7b97b6a9-467c-43eb-9bab-90f736cedfe6"
NAME = "[ap:GUARD01] Guarded note"


def lines_of(count: int) -> list[str]:
    return [f"line {i}" for i in range(1, count + 1)]


def size_header(lines: list[str]) -> str:
    """The [note: ...] size header, byte-identical to the shared core's."""
    byte_count = sum(len(line.encode("utf-8")) + 1 for line in lines)
    token_estimate = -(-byte_count // 3)
    return (
        f"[note: lines 1-{len(lines)} of {len(lines)} | "
        f"{len(lines)} lines, {byte_count} bytes, ~{token_estimate} tokens]"
    )


def client_for(monkeypatch, note: str):
    client = make_boundary_capable(
        object.__new__(server.WorkFlowyClient),
        nodes=[{"id": ROOT, "name": NAME, "note": "STALE CACHE", "parent_id": None}],
    )
    client.get_node = AsyncMock(
        return_value=WorkFlowyNode(id=ROOT, name=NAME, note=html.escape(note))
    )
    client.update_node = AsyncMock(side_effect=AssertionError("no writes"))
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    return client


async def dispatch_async(verb, params):
    return await dispatch_operation(
        server.build_node_registry(), verb, params,
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    )


def dispatch(verb, params):
    return asyncio.run(dispatch_async(verb, params))


def test_guarded_default_shape_300_head_100_tail(monkeypatch):
    note = "\n".join(lines_of(1000))
    client_for(monkeypatch, note)
    result = dispatch("read", {"node_handle": ROOT})
    rows = result.split("\n")
    assert rows[0] == f"[name: not editable] {NAME}"
    assert rows[1] == ""
    assert rows[2] == size_header(lines_of(1000))
    assert rows[3] == "line 1"
    assert rows[302] == "line 300"
    assert rows[303] == (
        "... 600 lines not shown (301-900); "
        "read them with start_line=301, end_line=900"
    )
    assert rows[304] == "line 901"
    assert rows[-1] == "line 1000"
    assert len(rows) == 3 + 300 + 1 + 100


def test_max_lines_zero_reads_whole(monkeypatch):
    note = "\n".join(lines_of(1000))
    client_for(monkeypatch, note)
    assert dispatch("read", {"node_handle": ROOT, "max_lines": 0}) == (
        f"[name: not editable] {NAME}\n\n{note}"
    )


def test_max_lines_small_threshold_guard_split(monkeypatch):
    note = "\n".join(lines_of(12))
    client_for(monkeypatch, note)
    result = dispatch("read", {"node_handle": ROOT, "max_lines": 10})
    rows = result.split("\n")
    assert rows[:3] == [f"[name: not editable] {NAME}", "", size_header(lines_of(12))]
    assert rows[3:11] == lines_of(8)
    assert rows[11] == (
        "... 2 lines not shown (9-10); read them with start_line=9, end_line=10"
    )
    assert rows[12:] == ["line 11", "line 12"]


def test_size_only_returns_banner_and_header_only(monkeypatch):
    note = "\n".join(lines_of(5))
    client_for(monkeypatch, note)
    assert dispatch("read", {"node_handle": ROOT, "size_only": True}) == (
        f"[name: not editable] {NAME}\n\n{size_header(lines_of(5))}"
    )


def test_size_only_on_empty_note(monkeypatch):
    client_for(monkeypatch, "")
    assert dispatch("read", {"node_handle": ROOT, "size_only": True}) == (
        f"[name: not editable] {NAME}\n\n[note: empty]"
    )


def test_default_threshold_constant_is_400():
    assert DEFAULT_NOTE_MAX_LINES == 400


def test_start_line_end_line_window_unnumbered(monkeypatch):
    client_for(monkeypatch, "\n".join(lines_of(10)))
    assert (
        dispatch("read", {"node_handle": ROOT, "start_line": 3, "end_line": 4})
        == "line 3\nline 4"
    )


def test_window_numbered_via_numbering_alias(monkeypatch):
    client_for(monkeypatch, "\n".join(lines_of(10)))
    assert (
        dispatch(
            "read",
            {"node_handle": ROOT, "numbering": "file", "start_line": 3, "end_line": 4},
        )
        == "3 | line 3\n4 | line 4"
    )


def test_numbering_none_matches_with_line_numbers_false_shape(monkeypatch):
    client_for(monkeypatch, "\n".join(lines_of(10)))
    assert (
        dispatch("read", {"node_handle": ROOT, "numbering": "none", "offset": 1})
        == dispatch("read", {"node_handle": ROOT, "offset": 1})
    )


def test_coordinates_accepted_as_noop_on_a_note(monkeypatch):
    client_for(monkeypatch, "\n".join(lines_of(10)))
    plain = dispatch("read", {"node_handle": ROOT, "start_line": 2, "end_line": 3})
    assert (
        dispatch(
            "read",
            {
                "node_handle": ROOT,
                "coordinates": "relative",
                "start_line": 2,
                "end_line": 3,
            },
        )
        == plain
    )


def test_explicit_windows_are_never_guarded(monkeypatch):
    note = "\n".join(lines_of(1000))
    client_for(monkeypatch, note)
    assert dispatch("read", {"node_handle": ROOT, "offset": 10}) == "\n".join(
        lines_of(1000)[10:]
    )
    assert dispatch("read", {"node_handle": ROOT, "limit": 5}) == "\n".join(
        lines_of(5)
    )


def test_window_past_eof_returns_empty_stub(monkeypatch):
    # A past-EOF window legitimately returns nothing; the 9a ([ap:G2UALX])
    # empty-output stub at the dispatch funnel delivers the literal marker,
    # never a bare '' (the node.read offset/limit window has no header, so it
    # is one of the stub's in-scope rows).
    client_for(monkeypatch, "\n".join(lines_of(3)))
    assert dispatch("read", {"node_handle": ROOT, "start_line": 50}) == "(empty)"


def test_guarded_elision_hint_round_trips_to_exactly_the_hidden_lines(monkeypatch):
    client_for(monkeypatch, "\n".join(lines_of(1000)))
    guarded = dispatch("read", {"node_handle": ROOT})
    match = re.search(
        r"read them with start_line=(\d+), end_line=(\d+)", guarded
    )
    assert match, guarded[:400]
    window = dispatch(
        "read",
        {
            "node_handle": ROOT,
            "start_line": int(match.group(1)),
            "end_line": int(match.group(2)),
        },
    )
    assert window == "\n".join(lines_of(1000)[300:900])


def test_guarded_numbered_rows_carry_the_gutter(monkeypatch):
    client_for(monkeypatch, "\n".join(lines_of(12)))
    result = dispatch(
        "read", {"node_handle": ROOT, "max_lines": 10, "with_line_numbers": True}
    )
    assert "12 | line 12" in result.split("\n")[-1]
    assert "  1 | line 1" in result
    assert " 11 | line 11" in result


@pytest.mark.parametrize("options", [
    {"offset": 1, "start_line": 2},
    {"end_line": 4, "limit": 2},
    {"numbering": "none", "with_line_numbers": True},
    {"numbering": "file", "with_line_numbers": False},
    {"size_only": True, "offset": 0},
    {"size_only": True, "limit": 2},
    {"as_raw_node": False, "max_lines": 10},
    {"as_raw_node": False, "size_only": True},
    {"output_file": "not-written.md", "size_only": True},
    {"output_file": "not-written.md", "max_lines": 10},
])
def test_view_flag_conflicts_are_loud_before_any_read(monkeypatch, options):
    client = client_for(monkeypatch, "a\nb\n")
    result = dispatch("read", {"node_handle": ROOT, **options})
    assert result["error"]["code"] == "invalid_parameter_combination"
    client.get_node.assert_not_awaited()


def test_start_line_bounds_are_checked_before_any_read(monkeypatch):
    client = client_for(monkeypatch, "a\nb\n")
    result = dispatch("read", {"node_handle": ROOT, "start_line": 0})
    assert result["error"]["code"] == "invalid_parameter_value"
    client.get_node.assert_not_awaited()


def test_internal_callers_read_unguarded(monkeypatch):
    note = "\n".join(lines_of(1000))
    client_for(monkeypatch, note)

    async def run():
        async with server._mcp_operation_boundary("node.read"):
            return await server._node_read_impl(ROOT)

    assert asyncio.run(run()) == f"[name: not editable] {NAME}\n\n{note}"


def test_output_file_is_never_guarded(monkeypatch, tmp_path):
    note = "\n".join(lines_of(1000))
    client_for(monkeypatch, note)
    destination = tmp_path / "note.md"
    result = dispatch(
        "read", {"node_handle": ROOT, "output_file": str(destination)}
    )
    assert result["success"] is True
    assert destination.read_text(encoding="utf-8") == note


def test_normalize_note_view_params_merges_the_two_window_spellings():
    plan = normalize_note_view_params({"start_line": 3})
    assert (plan.offset, plan.limit, plan.with_line_numbers) == (2, None, False)

    plan = normalize_note_view_params({"start_line": 3, "end_line": 5})
    assert (plan.offset, plan.limit) == (2, 3)

    plan = normalize_note_view_params({"end_line": 4})
    assert (plan.offset, plan.limit) == (None, 4)

    plan = normalize_note_view_params({"numbering": "file"})
    assert plan.with_line_numbers is True
    assert plan.view.numbering == "file"

    plan = normalize_note_view_params({})
    assert plan.with_line_numbers is False
    assert plan.view.max_lines == DEFAULT_NOTE_MAX_LINES == 400
    assert plan.view.size_only is False
    assert plan.view.context == 0
    assert plan.view.start_line is None and plan.view.limit is None


def test_normalize_note_view_params_default_matches_note_lines_domain():
    plan = normalize_note_view_params({})
    lines = ["α", "β"]
    assert split_note_lines("α\r\nβ\n") == lines
    assert plan.view.numbering == "none"
