"""node.ancestors + the shared F3 ancestor-chain core (U1, tether [ap:5L20RM]).

Locks:
* the F3/F2 line grammar (heading per ancestor, ⦿/• bullets, tags verbatim,
  HTML-entity-decoded names, → UUID tail) byte-for-byte with markers/metrics
  off — the historical WS output shape;
* the dock.peek marker vocabulary (⚓ ⭐ Ⓛ ✓ + ▸N/desc/self/tree) plus the new
  🗺️ mapped-folder / 📄 mapped-file markers, with the key at the top;
* the BINDING CONSTRAINT: the verb NEVER calls _resolve_uuid_path_and_respond
  (it sends WS frames) — both homes delegate to ancestor_path, and the WS
  handler's markdown equals the verb's markdown for the same input (drift lock);
* purity: source cache node dicts are never mutated.
"""
from __future__ import annotations

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from coherence_harness import make_boundary_capable
from workflowy_mcp import server
from workflowy_mcp.ancestor_path import (
    MARKER_KEY_LINE,
    METRICS_LEGEND_LINE,
    build_ancestor_path_document,
    walk_ancestor_chain,
)
from workflowy_mcp.mcp_api_registry import OperationValidationError, dispatch_operation

R = "11111111-1111-4111-8111-111111111111"
D = "22222222-2222-4222-8222-222222222222"
L = "33333333-3333-4333-8333-333333333333"
T = "44444444-4444-4444-8444-444444444444"
U = "55555555-5555-4555-8555-555555555555"
G = "66666666-6666-4666-8666-666666666666"
F = "77777777-7777-4777-8777-777777777777"
S = "88888888-8888-4888-8888-888888888888"
X = "99999999-9999-4999-8999-999999999999"

CONFIG_REPLY = {"note_session_local_postfixes": ["local"]}


def rows():
    return [
        {"id": R, "name": "🗡️ Test Root", "note": "", "parent_id": None},
        {"id": D, "name": "📖 Docs Dock #dock--fav", "note": "d", "parent_id": R},
        {"id": L, "name": "Local Bin #dock--local", "note": "", "parent_id": D},
        {"id": T, "name": "[ap:TARGT01] Done task #todo", "note": "t", "parent_id": L, "completed": True},
        {"id": U, "name": "Untitled", "note": "", "parent_id": D},
        {"id": G, "name": "📁 Repositories", "note": "Root: /repo", "parent_id": R},
        {"id": F, "name": "server.py", "note": "Path: src/workflowy_mcp/server.py", "parent_id": G},
        {"id": S, "name": "ƒ do_thing", "note": "Docstring.", "parent_id": F},
        {"id": X, "name": "Amp &amp; name #tag", "note": "", "parent_id": S},
    ]


def by_id():
    return {n["id"]: n for n in rows()}


def size(node):
    return len((node.get("name") or "").encode("utf-8")) + len((node.get("note") or "").encode("utf-8"))


def setup(monkeypatch, *, nodes=None, config_side_effect=None):
    all_rows = rows() if nodes is None else nodes
    client = make_boundary_capable(
        object.__new__(server.WorkFlowyClient), nodes=all_rows,
    )
    client.get_node = AsyncMock(side_effect=AssertionError("no live node GET"))
    client.update_node = AsyncMock(side_effect=AssertionError("no writes"))
    client.create_node = AsyncMock(side_effect=AssertionError("no writes"))
    client.export_nodes = AsyncMock(return_value={"nodes": all_rows})
    client._nodes_export_dirty_ids = set()
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    config = AsyncMock(
        return_value=(["dock"], [], None, CONFIG_REPLY),
        side_effect=config_side_effect,
    )
    monkeypatch.setattr(server, "_resolve_note_session_config", config)
    return client, config


def dispatch_verb(handle=T, **params):
    return asyncio.run(dispatch_operation(
        server.build_node_registry(), "ancestors", {"node_handle": handle, **params},
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    ))


class _FakeWebSocket:
    def __init__(self):
        self.frames = []

    async def send(self, payload):
        self.frames.append(json.loads(payload))


# ---------------------------------------------------------------------------
# The pure walk
# ---------------------------------------------------------------------------


def test_walk_returns_root_to_target_with_decoded_names():
    chain = walk_ancestor_chain(X, by_id())
    assert [e["id"] for e in chain] == [R, G, F, S, X]
    assert chain[-1]["name"] == "Amp & name #tag"  # &amp; decoded, tags kept
    assert chain[-1]["parent_id"] == S


def test_walk_absent_target_returns_empty():
    assert walk_ancestor_chain("not-in-cache", by_id()) == []
    assert walk_ancestor_chain("", by_id()) == []


def test_walk_broken_parent_link_truncates_at_last_resolvable():
    nodes = by_id()
    nodes[S]["parent_id"] = "ghost-parent"
    chain = walk_ancestor_chain(X, nodes)
    assert [e["id"] for e in chain] == [S, X]


def test_walk_cycle_guard_terminates():
    nodes = {
        "a": {"id": "a", "name": "A", "parent_id": "b"},
        "b": {"id": "b", "name": "B", "parent_id": "a"},
    }
    chain = walk_ancestor_chain("a", nodes)
    assert sorted(e["id"] for e in chain) == ["a", "b"]


def test_source_nodes_are_never_mutated():
    # The pre-lift WS handler wrote "_decoded_name" back into cache dicts;
    # the shared core must not (fence-adjacent purity guarantee).
    nodes = by_id()
    before = copy.deepcopy(nodes)
    chain = walk_ancestor_chain(X, nodes)
    build_ancestor_path_document(
        chain, nodes, anchor_base_tags=["dock"],
        anchor_postfix_kwargs={"local_postfixes": ["local"]},
    )
    assert nodes == before


# ---------------------------------------------------------------------------
# The document grammar — legacy shape locks (markers=False, metrics=False)
# ---------------------------------------------------------------------------


def doc(nodes, target, **kwargs):
    kwargs.setdefault("markers", False)
    kwargs.setdefault("metrics", False)
    chain = walk_ancestor_chain(target, nodes)
    return build_ancestor_path_document(chain, nodes, target_uuid=target, **kwargs)


def test_legacy_f3_shape_is_preserved_byte_for_byte():
    markdown = doc(by_id(), X)["markdown"]
    assert markdown == "\n".join([
        "# ⦿ 🗡️ Test Root",
        "## ⦿ 📁 Repositories",
        "### ⦿ server.py",
        "#### ⦿ ƒ do_thing",
        "##### • Amp & name #tag",
        "",
        f"→ `{X}`",
    ])


def test_legacy_f2_every_level_shape_is_preserved_byte_for_byte():
    nodes = by_id()
    markdown = doc(nodes, T, uuids="every-level")["markdown"]
    assert markdown == "\n".join([
        "# ⦿ 🗡️ Test Root",
        f"`{R}`",
        "",
        "## ⦿ 📖 Docs Dock #dock--fav",
        f"`{D}`",
        "",
        "### ⦿ Local Bin #dock--local",
        f"`{L}`",
        "",
        "#### • [ap:TARGT01] Done task #todo",
        f"`{T}`",
        "",
        "→ Use Leaf UUID:",
        f"`{T}`",
    ])


def test_uuids_none_omits_every_uuid_line_and_the_tail():
    markdown = doc(by_id(), T, uuids="none")["markdown"]
    assert markdown == "\n".join([
        "# ⦿ 🗡️ Test Root",
        "## ⦿ 📖 Docs Dock #dock--fav",
        "### ⦿ Local Bin #dock--local",
        "#### • [ap:TARGT01] Done task #todo",
    ])
    assert "`" not in markdown and "→" not in markdown


def test_unknown_uuids_mode_raises():
    with pytest.raises(ValueError, match="uuids"):
        doc(by_id(), T, uuids="everything")


def test_empty_chain_raises():
    with pytest.raises(ValueError, match="non-empty"):
        build_ancestor_path_document([], by_id())


def test_base_tags_without_postfix_kwargs_raises_rather_than_defaulting():
    chain = walk_ancestor_chain(T, by_id())
    with pytest.raises(ValueError, match="anchor_postfix_kwargs"):
        build_ancestor_path_document(chain, by_id(), anchor_base_tags=["dock"])


def test_fragment_mode_has_no_key_no_legend_no_tail():
    nodes = by_id()
    fragment = doc(nodes, T, uuids="every-level", standalone=False)["markdown"]
    assert not fragment.startswith("Key:")
    assert "→" not in fragment
    assert fragment.startswith("# ⦿ 🗡️ Test Root")
    assert f"`{T}`" in fragment  # per-node UUID lines are line grammar, not the tail


# ---------------------------------------------------------------------------
# Markers + metrics
# ---------------------------------------------------------------------------


def full_doc(nodes, target, **kwargs):
    chain = walk_ancestor_chain(target, nodes)
    return build_ancestor_path_document(
        chain, nodes, target_uuid=target,
        anchor_base_tags=["dock"],
        anchor_postfix_kwargs={
            "local_postfixes": ["local"],
            "favorite_postfixes": ["fav"],
            "deprecated_postfixes": ["deprecated"],
        },
        **kwargs,
    )


def test_key_line_and_dock_marker_vocabulary_on_the_dock_chain():
    out = full_doc(by_id(), T)
    lines = out["markdown"].split("\n")
    assert lines[0] == MARKER_KEY_LINE == (
        "Key: ⚓ Dock · ⭐ favorite Dock · Ⓛ local Dock · ✓ completed · "
        "🗺️ mapped folder · 📄 mapped file"
    )
    assert lines[1] == METRICS_LEGEND_LINE
    assert lines[2] == ""
    # Dock rows: ⚓ then ⭐ (favorite) — after the metrics suffix, peek order.
    assert "## ⦿ 📖 Docs Dock #dock--fav" in lines[4]
    assert lines[4].endswith("⚓ ⭐")
    assert "### ⦿ Local Bin #dock--local" in lines[5]
    assert lines[5].endswith("⚓ Ⓛ")
    # Completed target row carries ✓.
    assert lines[6].startswith("#### • [ap:TARGT01] Done task #todo")
    assert lines[6].endswith("✓")
    # Root row has no markers at all (plain node, not completed, unmapped).
    assert lines[3].startswith("# ⦿ 🗡️ Test Root ▸")
    assert not any(m in lines[3] for m in ("⚓", "⭐", "Ⓛ", "✓", "🗺", "📄"))
    rows_out = {row["id"]: row for row in out["path"]}
    assert rows_out[D]["is_dock"] and rows_out[D]["is_favorite_dock"]
    assert rows_out[L]["is_dock"] and rows_out[L]["is_local_dock"]
    assert rows_out[T]["completed"] is True
    assert rows_out[R]["is_dock"] is False


def test_mapped_markers_folder_file_and_inside_file():
    out = full_doc(by_id(), X)
    lines = out["markdown"].split("\n")
    body = lines[3:]
    assert body[0].startswith("# ⦿ 🗡️ Test Root")          # unmapped: no marker
    assert body[1].startswith("## ⦿ 📁 Repositories")
    assert body[1].endswith("🗺️")                           # Root: header → folder
    assert body[2].startswith("### ⦿ server.py")
    assert body[2].endswith("📄")                            # Path: w/ extension → file
    assert body[3].startswith("#### ⦿ ƒ do_thing")
    assert "🗺" not in body[3] and "📄" not in body[3]       # inside_file: no own marker
    assert body[4].startswith("##### • Amp & name #tag")
    assert "🗺" not in body[4] and "📄" not in body[4]
    rows_out = {row["id"]: row for row in out["path"]}
    assert rows_out[R]["map_state"] == "unmapped"
    assert rows_out[G]["map_state"] == "folder"
    assert rows_out[F]["map_state"] == "file"
    assert rows_out[S]["map_state"] == "inside_file"
    assert rows_out[X]["map_state"] == "inside_file"


def test_multi_marker_row_locks_the_compact_order():
    # A completed favorite Dock that is ALSO a mapped folder must render the
    # suffix in dock.peek's compact order — metrics first, then ⚓ ⭐ Ⓛ ✓,
    # then the mapped marker last — locking the relative order on ONE row.
    root = {"id": "r", "name": "Root", "note": "", "parent_id": None}
    dock = {
        "id": "d", "name": "Done Dock #dock--fav #dock--local", "note": "Root: /x",
        "parent_id": "r", "completed": True,
    }
    nodes = {"r": root, "d": dock}
    out = build_ancestor_path_document(
        walk_ancestor_chain("d", nodes), nodes, target_uuid="d",
        anchor_base_tags=["dock"],
        anchor_postfix_kwargs={"local_postfixes": ["local"], "favorite_postfixes": ["fav"]},
    )
    dock_line = [l for l in out["markdown"].split("\n") if l.startswith("## •")][0]
    assert dock_line.startswith("## • Done Dock #dock--fav #dock--local ▸0 · desc:0 · self:")
    assert dock_line.endswith("⚓ ⭐ Ⓛ ✓ 🗺️")


def test_metrics_columns_match_the_peek_authority():
    nodes = by_id()
    out = full_doc(nodes, T)
    rows_out = {row["id"]: row for row in out["path"]}
    assert rows_out[R]["metrics"]["child_count"] == 2          # D + G
    assert rows_out[R]["metrics"]["descendant_count"] == 8     # everything below R
    assert rows_out[D]["metrics"]["child_count"] == 2          # L + U
    assert rows_out[D]["metrics"]["descendant_count"] == 3     # L, U, T
    assert rows_out[T]["metrics"] == {
        "child_count": 0, "descendant_count": 0,
        "self_bytes": size(nodes[T]), "subtree_bytes": size(nodes[T]),
    }
    # The rendered column uses peek's formatter verbatim (▸N · desc · self · tree).
    line = [l for l in out["markdown"].split("\n") if l.startswith("# ⦿")][0]
    assert " ▸2 · desc:8 · self:" in line and " · tree:" in line


def test_metrics_off_drops_legend_and_columns_but_keeps_markers():
    out = full_doc(by_id(), T, metrics=False)
    lines = out["markdown"].split("\n")
    assert lines[0] == MARKER_KEY_LINE
    assert METRICS_LEGEND_LINE not in out["markdown"]
    assert "▸" not in out["markdown"]
    assert lines[1] == ""
    # Root row carries no markers; the D row keeps its Dock markers.
    assert not any(m in lines[2] for m in ("⚓", "⭐", "Ⓛ", "✓", "🗺", "📄"))
    assert lines[3].endswith("⚓ ⭐")
    assert out["path"][0]["metrics"] is None


def test_markers_off_drops_key_and_dock_flags_and_skips_map_state():
    out = full_doc(by_id(), T, markers=False, metrics=True)
    assert not out["markdown"].startswith("Key:")
    assert "⚓" not in out["markdown"] and "📄" not in out["markdown"]
    assert METRICS_LEGEND_LINE in out["markdown"]
    assert all(row["map_state"] is None for row in out["path"])
    assert all(row["is_dock"] is False for row in out["path"])


# ---------------------------------------------------------------------------
# The MCP verb (node.ancestors) through the real dispatch funnel
# ---------------------------------------------------------------------------


def test_verb_compact_returns_the_document(monkeypatch):
    setup(monkeypatch)
    text = dispatch_verb()
    assert isinstance(text, str)
    assert text.split("\n")[0] == MARKER_KEY_LINE
    assert text.split("\n")[-1] == f"→ `{T}`"
    assert "#### • [ap:TARGT01] Done task #todo" in text


def test_verb_full_detail_rows_and_ap_id(monkeypatch):
    setup(monkeypatch)
    result = dispatch_verb(detail="full")
    assert isinstance(result, dict) and result["success"] is True
    assert result["node_handle"] == T
    assert result["count"] == 4
    assert [row["id"] for row in result["rows"]] == [R, D, L, T]
    assert result["rows"][-1]["ap_id"] == "[ap:TARGT01]"
    assert result["rows"][0]["ap_id"] is None
    assert result["rows"][-1]["completed"] is True
    assert result["rows"][-2]["is_local_dock"] is True
    assert result["markdown"].split("\n")[0] == MARKER_KEY_LINE


def test_verb_markers_false_skips_the_config_read(monkeypatch):
    _client, config = setup(monkeypatch)
    text = dispatch_verb(markers=False, metrics=False)
    config.assert_not_awaited()
    assert not text.startswith("Key:")
    assert text == doc(by_id(), T, uuids="leaf")["markdown"]


def test_verb_uuids_modes_route_through_dispatch(monkeypatch):
    setup(monkeypatch)
    assert "→ Use Leaf UUID:" in dispatch_verb(uuids="every-level")
    assert "`" not in dispatch_verb(uuids="none")


def test_verb_rejects_unknown_enum_via_impl_guard(monkeypatch):
    setup(monkeypatch)
    with pytest.raises(OperationValidationError):
        asyncio.run(server._node_ancestors_impl(T, uuids="both"))
    with pytest.raises(OperationValidationError):
        asyncio.run(server._node_ancestors_impl(T, detail="huge"))
    with pytest.raises(OperationValidationError):
        asyncio.run(server._node_ancestors_impl(T, markers="yes"))


def test_verb_cache_miss_fails_loudly(monkeypatch):
    setup(monkeypatch)
    resolver = AsyncMock(return_value=(T, {}))
    monkeypatch.setattr(server, "_resolve_generic_node_handle_with_cache", resolver)
    with pytest.raises(RuntimeError, match="system.cache.refresh"):
        asyncio.run(server._node_ancestors_impl(T))


def test_verb_never_calls_the_ws_frame_handler(monkeypatch):
    # BINDING CONSTRAINT (contract [ap:DQ38KN]): the verb must never touch
    # _resolve_uuid_path_and_respond — it sends WS frames. Poison it.
    setup(monkeypatch)

    async def poisoned(*args, **kwargs):
        raise AssertionError("node.ancestors called the WS frame handler")

    monkeypatch.setattr(server, "_resolve_uuid_path_and_respond", poisoned)
    text = dispatch_verb()
    assert text.startswith("Key:")
    result = dispatch_verb(detail="full")
    assert result["success"] is True


def test_verb_registry_spec_teaches_never_assume_mapped():
    registry = server.build_node_registry()
    spec = registry.resolve("ancestors")
    assert "Never assume" in spec.summary
    assert "🗺️" in spec.summary and "📄" in spec.summary
    assert spec.params_schema["properties"]["uuids"]["enum"] == ("leaf", "every-level", "none")
    assert spec.params_schema["required"] == ("node_handle",)
    assert spec.cache_backed is True
    assert spec.side_effects == ("pure_read",)
    # The family-level always-visible surface carries the guidance too.
    assert "never assume" in registry.family_summary.lower()


# ---------------------------------------------------------------------------
# The WS handler — delegation, parity, frames
# ---------------------------------------------------------------------------


def test_ws_handler_f3_frame_matches_the_verb_markdown(monkeypatch):
    # DRIFT LOCK: the F3 hotkey home and the verb home render the SAME
    # document for the same input, because both delegate to ancestor_path.
    setup(monkeypatch)
    ws = _FakeWebSocket()
    asyncio.run(server._resolve_uuid_path_and_respond(T, ws, "f3"))
    assert len(ws.frames) == 1
    frame = ws.frames[0]
    assert frame["action"] == "uuid_path_result" and frame["success"] is True
    assert frame["target_uuid"] == T
    assert frame["markdown"] == dispatch_verb()
    assert [row["id"] for row in frame["path"]] == [R, D, L, T]
    # The frame's path rows keep the legacy identifying fields.
    assert frame["path"][-1]["parent_id"] == L
    assert frame["path"][-1]["name"] == "[ap:TARGT01] Done task #todo"


def test_ws_handler_f2_frame_matches_every_level(monkeypatch):
    setup(monkeypatch)
    ws = _FakeWebSocket()
    asyncio.run(server._resolve_uuid_path_and_respond(T, ws, "f2"))
    assert ws.frames[0]["markdown"] == dispatch_verb(uuids="every-level")


def test_ws_handler_error_frames(monkeypatch):
    setup(monkeypatch)
    ws = _FakeWebSocket()
    asyncio.run(server._resolve_uuid_path_and_respond(None, ws, "f3"))
    assert ws.frames[0]["success"] is False
    assert ws.frames[0]["error"] == "No target_uuid provided"

    ws = _FakeWebSocket()
    asyncio.run(server._resolve_uuid_path_and_respond("virtual_only", ws, "f3"))
    # "virtual_only" splits to ["virtual", "only"] -> len 2 -> actual uuid "only"
    # -> absent from cache -> the manual-refresh error frame.
    assert ws.frames[0]["success"] is False
    assert "not found in cached /nodes-export" in ws.frames[0]["error"]

    ws = _FakeWebSocket()
    asyncio.run(server._resolve_uuid_path_and_respond("virtual_", ws, "f3"))
    # "virtual_" splits to ["virtual", ""] -> actual uuid "" -> absent from
    # cache -> the manual-refresh error frame. (The "Malformed virtual UUID"
    # branch requires a startswith("virtual_") target whose split has < 2
    # parts — unreachable; it is preserved verbatim from the pre-lift handler.)
    assert ws.frames[0]["success"] is False
    assert "not found in cached /nodes-export" in ws.frames[0]["error"]


def test_ws_handler_config_failure_is_loud_not_silent(monkeypatch):
    setup(monkeypatch, config_side_effect=RuntimeError("No live Workflowy tab"))
    ws = _FakeWebSocket()
    asyncio.run(server._resolve_uuid_path_and_respond(T, ws, "f3"))
    frame = ws.frames[0]
    assert frame["success"] is False
    assert "No live Workflowy tab" in frame["error"]


def test_ws_handler_mirror_context_filters_untitled_and_has_no_own_key(monkeypatch):
    setup(monkeypatch)
    ws = _FakeWebSocket()
    virtual = f"virtual_{T}_{U}_abc123"
    asyncio.run(server._resolve_uuid_path_and_respond(virtual, ws, "f3"))
    markdown = ws.frames[0]["markdown"]
    assert ws.frames[0]["success"] is True
    assert "**Mirror appears under:**" in markdown
    main, mirror = markdown.split("\n\n---\n\n", 1)
    # Main document resolved the ACTUAL uuid.
    assert main.split("\n")[-1] == f"→ `{T}`"
    # Mirror fragment: walk(U) = [R, D, U]; "Untitled" detritus filtered out,
    # leaving exactly the two real ancestors (with markers/metrics suffixes).
    assert mirror.startswith("**Mirror appears under:**\n\n")
    body = mirror.split("**Mirror appears under:**\n\n", 1)[1]
    assert "Untitled" not in body
    body_lines = body.split("\n")
    assert len(body_lines) == 2
    assert body_lines[0].startswith("# ⦿ 🗡️ Test Root")
    # The fragment's own last line is its leaf bullet — as the pre-lift
    # inline mirror block rendered it.
    assert body_lines[1].startswith("## • 📖 Docs Dock #dock--fav")
    assert "Key:" not in mirror  # the fragment carries no key of its own
    assert "→ `" not in mirror   # ...and no tail


def test_ws_dispatch_is_task_dispatched_not_inline():
    # The pump must not inline-await the handler any more: its Dock-marker
    # vocabulary read is a nested Direction-A round-trip (deadlock class).
    import inspect

    source = inspect.getsource(server._dispatch_ws_work)
    clause = source.split("if action == 'resolve_uuid_path':", 1)[1]
    clause = clause.split("continue", 1)[0]
    assert "create_isolated_context_task(" in clause
    assert "await _resolve_uuid_path_and_respond" not in clause
    assert "await _resolve_uuid_path_and_respond" not in inspect.getsource(server.websocket_handler)
