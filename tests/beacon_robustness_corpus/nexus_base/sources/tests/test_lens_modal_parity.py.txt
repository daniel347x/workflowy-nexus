from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from workflowy_mcp import server
from workflowy_mcp.cache_render_spec import RenderProjectionSpec
from workflowy_mcp.mcp_api_registry import dispatch_operation


def _node(node_id, name, parent_id=None, priority=0, note=""):
    return {
        "id": node_id,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
        "createdAt": priority + 1,
        "modifiedAt": priority + 1,
    }


R = "10000000-0000-0000-0000-000000000001"
A = "10000000-0000-0000-0000-000000000002"
A1 = "10000000-0000-0000-0000-000000000003"
B = "10000000-0000-0000-0000-000000000004"
B1 = "10000000-0000-0000-0000-000000000005"


def _cache():
    return {
        R: _node(R, "Root"),
        A: _node(A, "Architecture #arch--expand", R, 0),
        A1: _node(A1, "Resolver", A, 0, "resolver body"),
        B: _node(B, "Deployment", R, 1),
        B1: _node(B1, "Deploy #deploy", B, 0, "deploy body"),
    }


def _reply(groups=None):
    return {
        "expand_groups": groups
        or [
            {"id": "grp-arch", "name": "Architecture", "root_uuid": R, "prefixes": ["arch"]},
            {"id": "grp-deploy", "name": "Deployment", "root_uuid": R, "prefixes": ["deploy"]},
        ],
        "active_group": None,
        "f8_postfix_auto_expand": "expand",
        "f8_postfix_suppress": "suppress",
        "f8_postfix_deprecated": "deprecated",
        "f8_global_never_expand_tags": ["wfy-suppress"],
        "note_session_tags": ["wfy-ns"],
        "note_session_local_postfixes": ["local"],
    }


def _lens():
    return {
        "kind": "merge",
        "operation": "union",
        "inputs": [
            {"kind": "group", "group_handle": "Architecture"},
            {"kind": "group", "group_handle": "Deployment"},
        ],
    }


class _WS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _client(cache):
    from coherence_harness import make_boundary_capable

    c = object.__new__(server.WorkFlowyClient)
    c._get_nodes_export_cache_nodes = lambda: list(cache.values())
    return make_boundary_capable(c, nodes=list(cache.values()))


def _spec(*, lens=None, query=None, root_uuid="stale-derived-root", focus=None, headings_only=False):
    return RenderProjectionSpec.normalize_from_glimpse_args(
        lightning={
            "mode": "render_lens",
            "lens": lens or _lens(),
            "query": query,
            "query_mode": "smart",
            "match_scope": "name_and_note",
        },
        root_uuid=root_uuid,
        focus=focus,
        headings_only=headings_only,
    )


def _run_modal(monkeypatch, *, spec=None, cache=None, config=None, action="render_slice_modal", threshold=200000):
    cache = cache or _cache()
    config = config or _reply()

    async def read_nested(**kwargs):
        assert kwargs.get("endpoint_id") == "tab-1"
        return config, None

    monkeypatch.setattr(server, "get_client", lambda: _client(cache))
    monkeypatch.setattr(server, "_read_slice_group_config_nested", read_nested)
    ws = _WS()
    frame = {
        "action": action,
        "request_id": "req-lens",
        "endpoint_id": "tab-1",
        "max_prerender_chars": threshold,
        "render_spec": (spec or _spec()).to_render_spec_blob(),
    }
    asyncio.run(server._handle_render_slice_modal(frame, ws))
    assert ws.sent
    return ws.sent[-1]


def test_modal_lens_full_document_adopts_canonical_recipe_and_ignores_prior_root(monkeypatch):
    result = _run_modal(monkeypatch)
    assert result["success"] is True, result
    assert result["cache_sourced"] is True
    assert "Resolver" in result["markdown"] and "Deploy" in result["markdown"]
    assert result["rows"]
    scope = result["render_scope"]
    assert scope["kind"] == "lightning_rod"
    assert scope["root_uuid"] == R
    assert scope["root_uuid"] != "stale-derived-root"
    assert scope["selection"]["mode"] == "render_lens"
    assert [x["group_handle"] for x in scope["selection"]["lens"]["inputs"]] == [
        "grp-arch",
        "grp-deploy",
    ]
    assert scope["live_gui_sync_supported"] is False
    assert scope["live_gui_sync_skipped_reason"] == "lightning"


def test_modal_lens_difference_round_trips_opaquely_and_subtracts(monkeypatch):
    lens = {
        "kind": "merge",
        "operation": "difference",
        "inputs": [
            {"kind": "subtree", "root_handle": R},
            {"kind": "group", "group_handle": "Deployment"},
        ],
    }
    result = _run_modal(monkeypatch, spec=_spec(lens=lens))
    assert result["success"] is True, result
    assert "Resolver" in result["markdown"]
    assert "Deploy" not in result["markdown"]
    adopted = result["render_scope"]["selection"]["lens"]
    assert adopted["operation"] == "difference"
    assert [item["kind"] for item in adopted["inputs"]] == ["subtree", "group"]


def test_modal_lens_row_is_occurrence_exact_and_stale_never_guesses(monkeypatch):
    whole = _run_modal(monkeypatch)
    row = next(r for r in whole["rows"] if r["uuid"] == A1)
    focus = {
        "mode": "row",
        "uuid": row["uuid"],
        "ancestor_path": row["ancestor_path"],
        "occurrence": row["occurrence"],
    }
    hit = _run_modal(
        monkeypatch,
        spec=_spec(focus=focus),
        action="render_slice_row",
    )
    assert hit["success"] is True and not hit.get("slice_row_stale")
    assert "Resolver" in hit["markdown"]

    stale_focus = dict(focus, occurrence=99)
    stale = _run_modal(
        monkeypatch,
        spec=_spec(focus=stale_focus),
        action="render_slice_row",
    )
    assert stale["success"] is True
    assert stale["slice_row_stale"] is True
    assert stale["markdown"] == ""


def test_modal_lens_snapshot_single_threshold_and_multi_select(monkeypatch):
    snapshot = _run_modal(monkeypatch, threshold=200000)
    assert snapshot["render_mode"] == "snapshot"
    assert isinstance(snapshot["renders"], dict)

    single = _run_modal(monkeypatch, threshold=1)
    assert single["render_mode"] == "single"
    assert single["renders"] is None

    rows = [r for r in snapshot["rows"] if r["uuid"] in (A1, B1)]
    multi_focus = {
        "mode": "rows",
        "rows": [
            {
                "uuid": r["uuid"],
                "ancestor_path": r["ancestor_path"],
                "occurrence": r["occurrence"],
            }
            for r in rows
        ],
    }
    multi = _run_modal(monkeypatch, spec=_spec(focus=multi_focus))
    assert multi["success"] is True
    assert "Resolver" in multi["markdown"] and "Deploy" in multi["markdown"]
    assert multi["render_mode"] == "single"


def test_modal_lens_refresh_re_evaluates_group_root_and_ignores_old_lca(monkeypatch):
    first = _run_modal(monkeypatch)
    adopted = first["render_scope"]["selection"]
    assert first["render_scope"]["root_uuid"] == R

    changed_config = _reply(
        groups=[
            {"id": "grp-arch", "name": "Architecture", "root_uuid": A, "prefixes": ["arch"]},
            {"id": "grp-deploy", "name": "Deployment", "root_uuid": A, "prefixes": ["missing-after-refresh"]},
        ]
    )
    refresh_spec = RenderProjectionSpec.normalize_from_glimpse_args(
        lightning=adopted,
        # This is the PRIOR echoed derived LCA. It must never override the
        # freshly evaluated lens root after group config changes.
        root_uuid=R,
    )
    refreshed = _run_modal(
        monkeypatch,
        spec=refresh_spec,
        config=changed_config,
    )
    assert refreshed["success"] is True, refreshed
    assert refreshed["render_scope"]["root_uuid"] == A1
    assert "Resolver" in refreshed["markdown"]
    assert "Deploy" not in refreshed["markdown"]


def test_modal_lens_empty_contract_and_stale_focus_precedence(monkeypatch):
    empty_lens = {"kind": "tags", "root_handle": R, "tags": ["missing"]}
    empty = _run_modal(monkeypatch, spec=_spec(lens=empty_lens, root_uuid=None))
    assert empty["success"] is True
    # The DOCK's empty reply keeps its own human-facing message; the D7
    # '(empty)' constant is the MCP verbs' contract (render_search/sift).
    assert empty["markdown"] == "No matches.\n"
    assert empty["rows"] == []
    assert empty["render_scope"]["root_uuid"] is None
    assert empty["empty_selection"] is True

    focused = _run_modal(
        monkeypatch,
        spec=_spec(
            lens=empty_lens,
            root_uuid=None,
            focus={"mode": "row", "uuid": A1, "ancestor_path": [R, A], "occurrence": 0},
        ),
        action="render_slice_row",
    )
    assert focused["slice_row_stale"] is True
    assert focused.get("empty_selection") is not True


def test_modal_lens_nested_transport_unknown_endpoint_is_preserved(monkeypatch):
    async def read_nested(**kwargs):
        return None, {
            "success": False,
            "code": "unknown_endpoint",
            "error": "tab is re-registering",
        }

    monkeypatch.setattr(server, "get_client", lambda: _client(_cache()))
    monkeypatch.setattr(server, "_read_slice_group_config_nested", read_nested)
    ws = _WS()
    asyncio.run(
        server._handle_render_slice_modal(
            {
                "action": "render_slice_modal",
                "request_id": "req-ghost",
                "endpoint_id": "tab-1",
                "render_spec": _spec().to_render_spec_blob(),
            },
            ws,
        )
    )
    assert ws.sent[-1]["code"] == "unknown_endpoint"
    assert ws.sent[-1]["error"] == "tab is re-registering"


def test_mcp_file_and_modal_markdown_are_byte_identical(monkeypatch, tmp_path):
    cache = _cache()
    config = _reply()
    snapshot = server._build_lens_stage0_snapshot_from_config(
        config,
        cache_by_id=cache,
        exclude_completed=True,
    )

    async def build_snapshot(target_tab=None, exclude_completed=None):
        return snapshot

    monkeypatch.setattr(server, "_build_lens_stage0_snapshot", build_snapshot)
    monkeypatch.setattr(server, "get_client", lambda: _client(cache))
    registry = server.build_slice_group_registry()
    agent = asyncio.run(dispatch_operation(registry, "render", {"lens": _lens()}))
    path = tmp_path / "lens.md"
    written = asyncio.run(
        dispatch_operation(
            registry,
            "render",
            {"lens": _lens(), "output_file": str(path)},
        )
    )
    assert written["success"] is True
    modal = _run_modal(monkeypatch, cache=cache, config=config)
    # U01 §9.2 item 30: the agent leg receives the raw Markdown string, and it
    # is byte-identical to the file bytes and to the modal's Markdown.
    assert isinstance(agent, str)
    assert agent == path.read_text(encoding="utf-8") == modal["markdown"]


def test_render_spec_modal_readback_reprojects_lens_exactly(monkeypatch):
    cache = _cache()
    config = _reply()
    shown = _run_modal(monkeypatch, cache=cache, config=config)
    selection = shown["render_scope"]["selection"]
    base_spec = RenderProjectionSpec.normalize_from_glimpse_args(
        lightning=selection,
        root_uuid=shown["render_scope"]["root_uuid"],
    )
    snapshot = server._build_lens_stage0_snapshot_from_config(
        config, cache_by_id=cache, exclude_completed=True
    )

    async def build_snapshot(target_tab=None, exclude_completed=None):
        return snapshot

    client = _client(cache)

    async def modal_spec_reply(*args, **kwargs):
        return {
            "success": True,
            "modal_payload_kind": "cache_render_spec",
            "render_spec": base_spec.to_render_spec_blob(),
        }

    client._ws_request_response = modal_spec_reply

    async def ws_wait(*args, **kwargs):
        return object(), object()

    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_build_lens_stage0_snapshot", build_snapshot)
    monkeypatch.setattr(server, "get_ws_connection_with_grace_wait", ws_wait)
    readback = asyncio.run(
        server._glimpse_read_modal(
            target_tab=None,
            output_file=None,
            ov_as_markdown=None,
            ov_headings_only=None,
            ov_sort_by=None,
            ov_sort_direction=None,
            ov_since=None,
        )
    )
    assert readback["preview_markdown"] == shown["markdown"]
    assert readback["render_scope"]["selection"] == selection


def test_shared_projector_is_transport_free_and_returns_exact_snapshot_cache(monkeypatch):
    cache = _cache()
    snapshot = server._build_lens_stage0_snapshot_from_config(
        _reply(), cache_by_id=cache, exclude_completed=True
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("transport must not be called by the shared projector")

    monkeypatch.setattr(server, "_resolve_slice_group_config", forbidden)
    monkeypatch.setattr(server, "_read_slice_group_config_nested", forbidden)
    projected = asyncio.run(
        server._project_render_lens_from_cache(
            _spec(root_uuid=None).lightning,
            snapshot=snapshot,
        )
    )
    assert projected["cache_by_id"] is snapshot.cache_by_id
    assert projected["cache_by_id"] is snapshot.context.cache_by_id


def test_scoped_over_lens_teaches_and_non_lens_message_is_unchanged():
    lens_spec = _spec(root_uuid=None).to_render_spec_blob()
    tree, err = asyncio.run(
        server._project_slice_base_tree_for_scoped_lightning(
            base_render_spec=lens_spec,
            endpoint_id="tab-1",
        )
    )
    assert tree is None
    assert "recursive cache/config recipe" in err["error"]
    assert "Scoped OFF (Global)" in err["error"]

    non_lens = RenderProjectionSpec.normalize_from_glimpse_args(
        lightning={"mode": "render_search", "query": "x", "query_mode": "smart"}
    ).to_render_spec_blob()
    tree, err = asyncio.run(
        server._project_slice_base_tree_for_scoped_lightning(
            base_render_spec=non_lens,
            endpoint_id="tab-1",
        )
    )
    assert tree is None
    assert "prior Lightning result is ephemeral" in err["error"]
