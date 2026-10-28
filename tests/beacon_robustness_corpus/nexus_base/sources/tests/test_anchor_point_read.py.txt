"""anchor.point.read — the Anchor-family guarded read with a metadata header.

Locks:
* the metadata header grammar: dock.peek's row markers (⚓ ⭐ Ⓛ ✓ + 🗺️/📄)
  and four source metrics, created/modified epoch seconds + the 8-hex handle,
  the F3-style one-line ancestor path — composed from the pinned cache;
* the Dan ruling (2026-10-01): the read works with NO Workflowy tab — the
  ⚓/⭐/Ⓛ Dock markers degrade visibly ('dock markers unavailable' line),
  the read itself never refuses, and ✓ / 🗺️ / 📄 need no tab;
* ONE read implementation with node.read: the same wire->semantic decoder and
  the same guard policy (snippet_view.render_guarded_note_view), so the note
  body is byte-identical to node.read's numbered body for the same node;
* the shared flag set: numbered rows by default, max_lines 400 guard with the
  elision hint, size_only = header + size line, explicit windows unguarded,
  numbering:'none' raw rows;
* handle forms: [ap:CORE], full UUID and the 8-hex segment all read the same
  node.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from coherence_harness import make_boundary_capable
from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation
from workflowy_mcp.models.node import WorkFlowyNode
from workflowy_mcp.snippet_view import (
    SnippetViewOptions,
    render_guarded_note_view,
)

ROOT = "00000000-0000-4000-8000-000000000001"
DOCK = "11111111-2222-4222-8222-222222222222"
T = "44444444-4444-4444-4444-444444444444"
F = "77777777-7777-4777-8777-777777777777"

CONFIG_REPLY = {
    "note_session_local_postfixes": ["local"],
    "note_session_favorite_postfixes": ["fav"],
}


def rows():
    return [
        {"id": ROOT, "name": "🗡️ Test Root", "note": "", "parent_id": None,
         "createdAt": 10, "modifiedAt": 10},
        {"id": DOCK, "name": "📖 Docs Dock #wfy-ns--fav #wfy-ns",
         "note": "a favorite dock", "parent_id": ROOT,
         "createdAt": 20, "modifiedAt": 25},
        {"id": T, "name": "[ap:READT01] Done task &amp; friends #todo",
         "note": "line &amp; one\nline two", "parent_id": DOCK,
         "createdAt": 100, "modifiedAt": 200, "completed": True},
        {"id": F, "name": "📄 notes.md", "note": "Path: docs/notes.md",
         "parent_id": ROOT, "createdAt": 30, "modifiedAt": 30},
    ]


def setup(monkeypatch, *, live_note=None, ws=True, config_exc=None):
    all_rows = rows()
    client = make_boundary_capable(
        object.__new__(server.WorkFlowyClient), nodes=all_rows,
    )

    async def get_node(uuid):
        note = live_note if live_note is not None else "line &amp; one\nline two"
        node = next(r for r in all_rows if r["id"] == uuid)
        return WorkFlowyNode(
            id=uuid, name=node["name"], note=note, parentId=node.get("parent_id"),
            priority=node.get("priority"), createdAt=node.get("createdAt"),
            modifiedAt=node.get("modifiedAt"),
        )

    client.get_node = AsyncMock(side_effect=get_node)
    client.update_node = AsyncMock(side_effect=AssertionError("no writes"))
    client.export_nodes = AsyncMock(return_value={"nodes": all_rows})
    client._nodes_export_dirty_ids = set()
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    if ws:
        monkeypatch.setattr(server, "get_ws_connection", lambda: (object(), object()))
        config = AsyncMock(
            return_value=(["wfy-ns"], [], None, CONFIG_REPLY)
        )
        if config_exc is not None:
            config = AsyncMock(side_effect=config_exc)
        monkeypatch.setattr(server, "_resolve_note_session_config", config)
    else:
        monkeypatch.setattr(server, "get_ws_connection", lambda: (None, None))
    return client


def dispatch_verb(handle=T, **params):
    return asyncio.run(dispatch_operation(
        server.build_anchor_registry(), "point.read", {"node_handle": handle, **params},
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    ))


# ---------------------------------------------------------------------------
# The metadata header
# ---------------------------------------------------------------------------


def test_header_composes_peek_markers_metrics_dates_and_path(monkeypatch):
    setup(monkeypatch)
    doc = dispatch_verb()
    lines = doc.splitlines()
    # Line 1: full name (tags included) + Dock/favorite/completed markers +
    # the four peek source metrics (▸N · desc: · self: · tree:).
    assert lines[0].startswith("[ap:READT01] Done task & friends #todo")
    assert " ✓ " in lines[0]            # completed marker sits before the metrics
    assert "⚓" not in lines[0]          # the target itself is not a Dock
    assert " ▸0 · desc:0 · self:" in lines[0] and " · tree:" in lines[0]
    # Line 2: created/modified epoch seconds + the 8-hex handle.
    assert lines[1] == f"created 100 · modified 200 · {T[:8]}"
    # Line 3: the F3-style ancestor path (decoded names, tags included).
    assert lines[2] == "path: 🗡️ Test Root › 📖 Docs Dock #wfy-ns--fav #wfy-ns"
    # Body: numbered rows by default, semantically decoded.
    assert lines[4:] == ["1 | line & one", "2 | line two"]


def test_dock_rows_carry_anchor_favorite_local_markers(monkeypatch):
    setup(monkeypatch)
    doc = dispatch_verb(DOCK)
    line1 = doc.splitlines()[0]
    assert "⚓" in line1 and "⭐" in line1
    assert "📄" not in line1 and "🗺️" not in line1


def test_mapped_file_marker_needs_no_dock_vocabulary(monkeypatch):
    setup(monkeypatch)
    doc = dispatch_verb(F)
    line1 = doc.splitlines()[0]
    assert "📄" in line1 and "⚓" not in line1


# ---------------------------------------------------------------------------
# The Dan ruling: reads with NO Workflowy tab (degrade visibly)
# ---------------------------------------------------------------------------


def test_no_tab_degrades_visibly_and_never_refuses(monkeypatch):
    setup(monkeypatch, ws=False)
    doc = dispatch_verb()
    lines = doc.splitlines()
    assert "dock markers unavailable: no Workflowy tab connected" in lines
    assert "⚓" not in doc and "⭐" not in doc and "Ⓛ" not in doc
    # ✓ and the mapped markers need no tab and still show.
    assert " ✓" in lines[0]
    assert lines[5:] == ["1 | line & one", "2 | line two"]


def test_config_read_failure_degrades_with_the_reason(monkeypatch):
    setup(monkeypatch, config_exc=RuntimeError("broker hiccup"))
    doc = dispatch_verb()
    assert "dock markers unavailable (RuntimeError): no Workflowy tab connected" in doc
    assert doc.splitlines()[5:] == ["1 | line & one", "2 | line two"]


# ---------------------------------------------------------------------------
# ONE read implementation with node.read
# ---------------------------------------------------------------------------


def test_note_body_is_byte_identical_to_node_read_numbered(monkeypatch):
    setup(monkeypatch)
    point_doc = dispatch_verb()
    node_doc = asyncio.run(dispatch_operation(
        server.build_node_registry(), "read", {"node_handle": T, "with_line_numbers": True},
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    ))
    point_body = point_doc.split("\n\n", 1)[1]
    node_body = node_doc.split("\n\n", 1)[1]
    assert point_body == node_body == "1 | line & one\n2 | line two"


@pytest.mark.parametrize("params,long_note,windowed", [
    ({"offset": 1, "limit": 2, "with_line_numbers": True}, False, True),
    ({"start_line": 2, "end_line": 3, "numbering": "none"}, False, True),
    ({"with_line_numbers": True}, False, False),
    ({"numbering": "none"}, False, False),
    ({"numbering": "file"}, True, False),
    ({"size_only": True, "numbering": "none"}, False, False),
], ids=["offset-limit", "start-end", "numbered", "exact", "guarded", "size-only"])
def test_note_body_parity_with_mixed_terminators(monkeypatch, params, long_note, windowed):
    """WO-1: compare effective flags, not the two verbs' different defaults.

    The exact case must retain both CRLF and the trailing terminator; the
    numbered/windowed cases intentionally normalize them. The cached note
    differs from the live one, so parity cannot be satisfied by cache reads.
    """
    wire_note = "line &amp; one\r\nline two\nline three\n"
    if long_note:
        wire_note += "".join(f"row {i}\n" for i in range(4, 501))
    client = setup(monkeypatch, live_note=wire_note)
    point_doc = dispatch_verb(**params)
    client.get_node.assert_awaited_once_with(T)
    client.get_node.reset_mock()
    node_doc = asyncio.run(dispatch_operation(
        server.build_node_registry(), "read", {"node_handle": T, **params},
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    ))
    client.get_node.assert_awaited_once_with(T)
    point_body = point_doc.split("\n\n", 1)[1]
    node_body = node_doc if windowed else node_doc.split("\n\n", 1)[1]
    assert point_body == node_body
    if params == {"numbering": "none"}:
        assert point_body == "line & one\r\nline two\nline three\n"


@pytest.mark.parametrize("family,verb", [("node", "read"), ("anchor", "point.read")])
def test_note_fronts_delegate_fetch_decode_body_and_validation(monkeypatch, family, verb):
    """Exercise the real shared functions, not look-alike front implementations."""
    client = setup(monkeypatch, ws=False)
    limiter = Mock(acquire=AsyncMock())
    real_fetch = server._read_live_node_note

    async def counted_fetch(*args, **kwargs):
        # The operation boundary has its own acquisition. Count only this
        # fetch's delta, without bypassing either production path.
        acquired, succeeded = limiter.acquire.await_count, limiter.on_success.call_count
        result = await real_fetch(*args, **kwargs)
        assert limiter.acquire.await_count == acquired + 1
        assert limiter.on_success.call_count == succeeded + 1
        return result

    fetch = AsyncMock(side_effect=counted_fetch)
    decode = Mock(wraps=server._note_wire_to_semantic)
    render = Mock(wraps=server.render_note_body)
    validate = Mock(wraps=server.validate_note_read_lines)
    monkeypatch.setattr(server, "_read_live_node_note", fetch)
    monkeypatch.setattr(server, "_note_wire_to_semantic", decode)
    monkeypatch.setattr(server, "render_note_body", render)
    monkeypatch.setattr(server, "validate_note_read_lines", validate)
    monkeypatch.setattr(server, "_rate_limiter", limiter)
    registry = server.build_node_registry() if family == "node" else server.build_anchor_registry()
    doc = asyncio.run(dispatch_operation(
        registry, verb, {"node_handle": T, "offset": 1, "limit": 1, "numbering": "none"},
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    ))
    if family == "node":
        assert doc == "line two"
    else:
        assert doc.endswith("\n\nline two")
    fetch.assert_awaited_once()
    assert fetch.call_args.args == (T,)
    client.get_node.assert_awaited_once_with(T)
    decode.assert_called_once_with("line &amp; one\nline two")
    validate.assert_called_once_with(1, 1, False)
    render.assert_called_once()
    assert render.call_args.args[:4] == ("line & one\nline two", 1, 1, False)
    assert render.call_args.args[-1] is True
    limiter.on_rate_limit.assert_not_called()


def test_guard_fires_on_a_long_note_with_the_elision_hint(monkeypatch):
    setup(monkeypatch, live_note="\n".join(f"row {i}" for i in range(1, 501)))
    doc = dispatch_verb()
    assert "[note: lines 1-500 of 500 | 500 lines," in doc
    assert "... 100 lines not shown (301-400); " \
           "read them with start_line=301, end_line=400" in doc
    assert "\n500 | row 500" in doc


def test_size_only_returns_header_plus_size_line_alone(monkeypatch):
    setup(monkeypatch)
    doc = dispatch_verb(size_only=True)
    assert doc.count("[note:") == 1
    assert "[note: lines 1-2 of 2 | 2 lines," in doc
    assert " | line" not in doc  # no body rows


def test_size_only_on_an_empty_note(monkeypatch):
    setup(monkeypatch, live_note="")
    doc = dispatch_verb(size_only=True)
    assert doc.endswith("[note: empty]")


def test_explicit_window_is_never_guarded_and_returns_only_the_window(monkeypatch):
    setup(monkeypatch, live_note="\n".join(f"row {i}" for i in range(1, 501)))
    doc = dispatch_verb(offset=1, limit=1)
    assert "[note:" not in doc
    assert "..." not in doc
    assert doc.splitlines()[4:] == ["2 | row 2"]


def test_max_lines_zero_reads_the_whole_long_note(monkeypatch):
    setup(monkeypatch, live_note="\n".join(f"row {i}" for i in range(1, 501)))
    doc = dispatch_verb(max_lines=0)
    assert "[note:" not in doc and "..." not in doc
    assert "1 | row 1" in doc and "500 | row 500" in doc


def test_numbering_none_returns_raw_rows(monkeypatch):
    setup(monkeypatch)
    doc = dispatch_verb(numbering="none")
    assert doc.splitlines()[4:] == ["line & one", "line two"]


# ---------------------------------------------------------------------------
# Handle forms
# ---------------------------------------------------------------------------


def test_ap_id_full_uuid_and_8hex_segment_read_the_same_node(monkeypatch):
    setup(monkeypatch)
    by_ap = dispatch_verb("[ap:READT01]")
    by_uuid = dispatch_verb(T)
    by_hex = dispatch_verb(T[:8])
    assert by_ap == by_uuid == by_hex


# ---------------------------------------------------------------------------
# The shared guard policy helper
# ---------------------------------------------------------------------------


def test_render_guarded_note_view_policy():
    options = SnippetViewOptions(
        numbering="file", coordinates="file", start_line=None, end_line=None,
        limit=None, context=0, max_lines=400, size_only=False,
    )
    assert render_guarded_note_view("short", options, windowed=False) is None
    assert render_guarded_note_view("x\n" * 500, options, windowed=True) is None
    guarded = render_guarded_note_view("x\n" * 500, options, windowed=False)
    assert guarded is not None and guarded.startswith("[note: lines 1-500 of 500")
    size_only = SnippetViewOptions(
        numbering="file", coordinates="file", start_line=None, end_line=None,
        limit=None, context=0, max_lines=400, size_only=True,
    )
    assert render_guarded_note_view("", size_only, windowed=False) == "[note: empty]"
    assert render_guarded_note_view("a", size_only, windowed=False) == (
        "[note: lines 1-1 of 1 | 1 lines, 2 bytes, ~1 tokens]"
    )
