from __future__ import annotations

import asyncio
from types import SimpleNamespace

from workflowy_mcp import server
from workflowy_mcp.cache_render_spec import RenderProjectionSpec
from workflowy_mcp.lens_engine import LensEvalContext
from workflowy_mcp.mcp_api_registry import dispatch_operation


def _node(node_id, name, parent_id=None, priority=0, note="", *, completed=False):
    return {
        "id": node_id,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
        "completed": completed,
        "completedAt": 1 if completed else None,
        "createdAt": priority + 1,
        "modifiedAt": priority + 1,
    }


def _fixture_state():
    cache = {
        "R": _node("R", "Root", None, 0),
        "A": _node("A", "Architecture #arch--expand", "R", 0),
        "A1": _node("A1", "Resolver implementation", "A", 0),
        "B": _node("B", "Deployment", "R", 1),
        "B1": _node("B1", "Deploy normalization #deploy", "B", 0),
        "X": _node("X", "Outside-only needle", "R", 2),
        "S": _node("S", "Suppressed #wfy-suppress", "R", 3),
        "S1": _node("S1", "Raw subtree survivor", "S", 0),
    }
    groups = {
        "Architecture": {
            "id": "grp-arch",
            "name": "Architecture",
            "root_uuid": "R",
            "prefixes": ["arch"],
            "default_sort_by": "created",
            "default_sort_direction": "desc",
        },
        "grp-arch": {
            "id": "grp-arch",
            "name": "Architecture renamed later",
            "root_uuid": "R",
            "prefixes": ["arch"],
            "default_sort_by": "created",
            "default_sort_direction": "desc",
        },
        "Deployment": {
            "id": "grp-deploy",
            "name": "Deployment",
            "root_uuid": "R",
            "prefixes": ["deploy"],
        },
        "grp-deploy": {
            "id": "grp-deploy",
            "name": "Deployment",
            "root_uuid": "R",
            "prefixes": ["deploy"],
        },
        "Empty": {
            "id": "grp-empty",
            "name": "Empty",
            "root_uuid": "R",
            "prefixes": ["missing"],
        },
        "grp-empty": {
            "id": "grp-empty",
            "name": "Empty",
            "root_uuid": "R",
            "prefixes": ["missing"],
        },
    }
    return cache, groups


def _context(cache, groups):
    def resolve_group(handle):
        if handle not in groups:
            raise KeyError(handle)
        return groups[handle]

    def resolve_root(handle):
        if handle not in cache:
            raise KeyError(handle)
        return handle

    return LensEvalContext(
        cache_by_id=cache,
        resolve_group=resolve_group,
        resolve_root=resolve_root,
        global_never_stems=("wfy-suppress",),
        exclude_completed=True,
        anchor_boundary_predicate=lambda node: "#wfy-ns" in str(node.get("name") or ""),
    )


def _install_context_and_tail(monkeypatch, cache, groups):
    async def build_snapshot(target_tab=None, exclude_completed=None):
        return SimpleNamespace(
            context=_context(cache, groups),
            cache_by_id=cache,
        )

    calls = []

    def tail(**kwargs):
        calls.append(kwargs)
        payload = {
            "success": True,
            "node_count": 1,
            "depth": 1,
            "_test_tree": kwargs["final_subtree"],
            "_test_spec": kwargs["spec"],
        }
        # U01: mirror the real Stage-2 contract — Markdown mode fills
        # preview_markdown, full mode fills preview_tree — so the dispatch
        # leg exercises the same finalizer seam the agent sees.
        if kwargs["spec"].as_markdown:
            payload["preview_markdown"] = "rendered\n"
        else:
            payload["preview_tree"] = [{"id": "R"}]
        payload.update(kwargs["source_meta"])
        return payload

    monkeypatch.setattr(server, "_build_lens_stage0_snapshot", build_snapshot)
    monkeypatch.setattr(server, "render_stage2_filter_sort", tail)
    return calls


def _union_lens():
    return {
        "kind": "merge",
        "operation": "union",
        "inputs": [
            {"kind": "group", "group_handle": "Architecture"},
            {"kind": "group", "group_handle": "Deployment"},
        ],
    }


def test_render_canonicalizes_recipe_materializes_lca_and_ignores_saved_sort(monkeypatch):
    cache, groups = _fixture_state()
    calls = _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()

    # U01: as_markdown=false so the structured dict (with _test_tree/_test_spec)
    # still reaches the test; the Markdown-default leg returns the raw string.
    result = asyncio.run(
        dispatch_operation(
            registry, "render", {"lens": _union_lens(), "as_markdown": False}
        )
    )

    assert result["success"] is True
    assert result["root_uuid"] == "R"
    assert result["_test_tree"]["id"] == "R"
    assert result["_test_spec"].sort_by == "none"
    selection = result["render_scope"]["selection"]
    assert selection["mode"] == "render_lens"
    assert [row["group_handle"] for row in selection["lens"]["inputs"]] == [
        "grp-arch",
        "grp-deploy",
    ]
    assert result["render_scope"]["selection_kind"] == "lens"
    assert result["render_scope"]["atom_summary"] == {
        "count": 2,
        "by_kind": {"group": 2},
    }
    assert result["render_scope"]["leaf_count"] == 2
    assert calls[0]["spec"].root_uuid == "R"

    markdown = asyncio.run(
        dispatch_operation(registry, "render", {"lens": _union_lens()})
    )
    assert markdown == "rendered\n"


def test_sift_headings_tier_never_admits_outside_lens(monkeypatch):
    """tier='headings': the pre-5b tree render, unchanged."""
    cache, groups = _fixture_state()
    calls = _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()

    hit = asyncio.run(
        dispatch_operation(
            registry,
            "sift",
            {
                "lens": _union_lens(),
                "query": "deploy",
                "tier": "headings",
                "as_markdown": False,
            },
        )
    )
    assert hit["success"] is True
    assert hit["_test_spec"].headings_only is True
    assert hit["_test_tree"]["id"] == "R"
    assert [child["id"] for child in hit["_test_tree"]["children"]] == ["B"]

    miss = asyncio.run(
        dispatch_operation(
            registry,
            "sift",
            {"lens": _union_lens(), "query": "outside-only", "tier": "headings"},
        )
    )
    # U01: the zero-match empty result reaches the agent as its raw Markdown.
    # D7 ([ap:2EG7TD], 5c): the ONE shared empty-selection text -- the same
    # constant the (empty) stub uses -- for sift and render_search alike.
    assert miss == server.EMPTY_RESULT_STUB
    assert len(calls) == 1


def test_sift_defaults_to_context_grep_tier(monkeypatch):
    """[ap:47B7WO] ruling D2: no tier means the line report, not the tree."""
    cache, groups = _fixture_state()
    cache["B1"]["note"] = "normalize the deploy pipeline\nthen ship"
    calls = _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()

    report = asyncio.run(
        dispatch_operation(
            registry, "sift", {"lens": _union_lens(), "query": "deploy"}
        )
    )
    assert isinstance(report, str)
    # node.search's compact shape: per-node header, numbered rows, footer.
    # B is admitted by its NAME ("Deployment") with an empty note -> the D6
    # title-match marker; B1 contributes the one matching note line.
    assert 'Matched 0 lines in B "Deployment" (title match):' in report
    assert "Matched 1 lines in B1" in report
    assert "> 1 | normalize the deploy pipeline" in report
    assert "  2 | then ship" in report and "> 2 | then ship" not in report
    assert "Pagination: offset=0; returned_matches=1;" in report
    assert "has_more=false" in report
    # The Stage-2 tail was bypassed: no tree render call happened.
    assert calls == []


def test_sift_grep_never_reports_outside_lens_or_suppressed_pocket(monkeypatch):
    cache, groups = _fixture_state()
    cache["A1"]["note"] = "resolver for arch\nneedle lives in the lens"
    cache["X"]["note"] = "outside-only needle note"
    cache["S"]["note"] = "suppressed pocket note needle"
    cache["S1"]["note"] = "raw subtree survivor needle"
    _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()

    report = asyncio.run(
        dispatch_operation(
            registry,
            "sift",
            {"lens": _union_lens(), "query": "needle", "context_lines": 0},
        )
    )
    # The query hits X, S and S1 outside the lens; only the lens-admitted
    # node's line reports (A1 reached through A's --expand hydration).
    assert "Matched 1 lines in A1" in report
    assert "in X " not in report and "in S " not in report
    assert "in S1 " not in report
    assert "No matching note lines" not in report


def test_sift_grep_paging_banner_offset_and_occurrences(monkeypatch):
    cache, groups = _fixture_state()
    for index in range(3):
        cache[f"AA{index}"] = _node(
            f"AA{index}", f"Arch child {index}", "A", index + 1,
            note=f"resolver hit {index}",
        )
    _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()
    params = {
        "lens": _union_lens(),
        "query": "resolver",
        "context_lines": 0,
        "max_matches": 2,
    }

    first = asyncio.run(dispatch_operation(registry, "sift", dict(params)))
    assert first.startswith(
        "INFORMATIONAL, CRITICAL DETAIL: PAGINATION ACTIVE."
    )
    assert "returned_matches=2" in first
    assert "returned_occurrences=2" in first
    assert "has_more=true; next_offset=2" in first

    second = asyncio.run(
        dispatch_operation(registry, "sift", {**params, "offset": 2})
    )
    assert not second.startswith("INFORMATIONAL")
    assert "Matched 1 lines in AA2" in second
    assert "has_more=false" in second


def test_sift_grep_d6_markers_title_and_spans_lines(monkeypatch):
    cache, groups = _fixture_state()
    # A1 matches the query ONLY by its title (empty note).
    cache["A1"]["note"] = ""
    # B1 matches ONLY across note lines (the DOTALL regex needs both).
    cache["B1"]["name"] = "Deploy normalization #deploy"
    cache["B1"]["note"] = "start alpha\nend beta and nothing else"
    _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()

    report = asyncio.run(
        dispatch_operation(
            registry,
            "sift",
            {
                "lens": _union_lens(),
                "query": "alpha.end|Resolver",
                "query_mode": "regex",
                "context_lines": 1,
            },
        )
    )
    assert 'Matched 0 lines in A1 "Resolver implementation" (title match):' in report
    assert 'Matched 0 lines in B1' in report
    assert "(match spans lines)" in report
    # Marker nodes count zero lines for paging.
    assert "returned_matches=0" in report


def test_sift_grep_match_scope_name_reports_title_rows_only(monkeypatch):
    cache, groups = _fixture_state()
    cache["B1"]["note"] = "the deploy note body"
    _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()

    report = asyncio.run(
        dispatch_operation(
            registry,
            "sift",
            {
                "lens": _union_lens(),
                "query": "deploy",
                "match_scope": "name",
                "context_lines": 0,
            },
        )
    )
    # Under 'name' the note is never searched: admitted nodes report their
    # header row alone, marked as title matches.
    assert "(title match)" in report
    assert "the deploy note body" not in report
    assert "Matched 0 lines in B1" in report


def test_sift_grep_empty_lens_reports_the_formatters_zero_match_text(monkeypatch):
    cache, groups = _fixture_state()
    _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()

    report = asyncio.run(
        dispatch_operation(
            registry,
            "sift",
            {"lens": {"kind": "group", "group_handle": "Empty"}, "query": "deploy"},
        )
    )
    # An empty lens has no documents; the ONE formatter's zero-match text is
    # the grep tier's empty result (never the headings tier's "No matches.").
    assert isinstance(report, str)
    assert report.startswith("Source: slice_group.sift grep")
    assert "No matching note lines." in report
    assert "returned_matches=0" in report


def test_sift_grep_structured_variant_and_output_file(monkeypatch, tmp_path):
    cache, groups = _fixture_state()
    cache["B1"]["note"] = "deploy line one\ndeploy line two"
    _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()
    params = {
        "lens": _union_lens(),
        "query": "deploy",
        "context_lines": 0,
        "max_matches": 1,
    }

    structured = asyncio.run(
        dispatch_operation(
            registry, "sift", {**params, "as_markdown": False}
        )
    )
    assert isinstance(structured, dict) and structured["success"] is True
    assert structured["tier"] == "context"
    assert "Matched 1 lines in B1" in structured["text"]
    assert structured["has_more"] is True
    assert structured["next_offset"] == 1
    assert structured["returned_matches"] == 1
    assert structured["returned_occurrences"] == 1
    assert structured["empty_selection"] is False
    assert structured["render_scope"]["selection"]["query"] == "deploy"

    target = tmp_path / "sift-report.md"
    summary = asyncio.run(
        dispatch_operation(
            registry, "sift", {**params, "output_file": str(target)}
        )
    )
    assert summary["success"] is True
    assert summary["bytes_written"] == len(target.read_text(encoding="utf-8"))
    assert "Matched 1 lines in B1" in target.read_text(encoding="utf-8")


def test_sift_grep_refuses_push_modal_bad_tier_and_inert_knobs(monkeypatch):
    cache, groups = _fixture_state()
    _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()
    base = {"lens": _union_lens(), "query": "deploy"}

    async def pushed(**kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("push_to_modal must be refused in the grep tier")

    monkeypatch.setattr(server, "_glimpse_push_slice_to_modal", pushed)

    push = asyncio.run(
        dispatch_operation(registry, "sift", {**base, "push_to_modal": True})
    )
    assert push["error"]["code"] == "invalid_parameter_combination"
    assert "tier='headings'" in push["error"]["message"]

    bad_tier = asyncio.run(
        dispatch_operation(registry, "sift", {**base, "tier": "lines"})
    )
    assert bad_tier["error"]["code"] == "invalid_enum_value"

    # [ap:O3MDOY] half 2: sort_by/recent are now ACTIVE node-level controls
    # on the grep tier (the ONE match-control set) -- accepted, not inert.
    controls = asyncio.run(
        dispatch_operation(
            registry, "sift", {**base, "sort_by": "created"}
        )
    )
    assert isinstance(controls, str)
    assert "Pagination: offset=0" in controls
    controls_recent = asyncio.run(
        dispatch_operation(registry, "sift", {**base, "recent": "last 5"})
    )
    assert isinstance(controls_recent, str)
    assert "returned_nodes=" in controls_recent

    bad_lines = asyncio.run(
        dispatch_operation(registry, "sift", {**base, "context_lines": 11})
    )
    assert bad_lines["error"]["code"] == "invalid_parameter_value"

    # ...while the still-headings-only knobs stay inert-refused.
    inert = asyncio.run(
        dispatch_operation(
            registry, "sift", {**base, "full_uuids": True}
        )
    )
    assert inert["error"]["code"] == "invalid_parameter_combination"
    assert "full_uuids" in inert["error"]["message"]

    # tier='headings' keeps every heading-tier knob legal.
    async def push_ok(**kwargs):
        assert kwargs["lightning"]["mode"] == "render_lens"
        return {"success": True, "pushed_to_modal": True}

    monkeypatch.setattr(server, "_glimpse_push_slice_to_modal", push_ok)
    headings = asyncio.run(
        dispatch_operation(
            registry,
            "sift",
            {**base, "tier": "headings", "push_to_modal": True, "sort_by": "created"},
        )
    )
    assert headings == {"success": True, "pushed_to_modal": True}


def test_empty_lens_shapes_markdown_and_full_mode_without_connective_root(monkeypatch):
    cache, groups = _fixture_state()
    _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()
    lens = {"kind": "group", "group_handle": "Empty"}

    markdown = asyncio.run(dispatch_operation(registry, "render", {"lens": lens}))
    # U01: the Markdown-default render returns the raw string; the empty
    # payload's own shape (empty_selection, root_uuid, node_count) is pinned
    # by the full-mode leg below and by test_slice_group_markdown_return.
    # D7 ([ap:2EG7TD], 5c): the ONE shared empty-selection constant.
    assert markdown == server.EMPTY_RESULT_STUB

    full = asyncio.run(
        dispatch_operation(
            registry, "render", {"lens": lens, "as_markdown": False}
        )
    )
    assert full["success"] is True
    assert full["root_uuid"] is None
    assert full["preview_tree"] == []


def test_stale_focus_wins_over_successful_empty(monkeypatch):
    cache, groups = _fixture_state()
    _install_context_and_tail(monkeypatch, cache, groups)
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        lightning={
            "mode": "render_lens",
            "lens": _union_lens(),
            "query": "outside-only",
            "query_mode": "smart",
            "match_scope": "name_and_note",
        },
        focus={"mode": "row", "uuid": "A1", "ancestor_path": ["R", "A"], "occurrence": 0},
    )
    result = asyncio.run(
        server._lightning_rod_render_lens(
            spec, output_file=None, target_tab=None
        )
    )
    assert result["focus_unresolved"] == "A1"
    assert result.get("empty_selection") is not True


def test_lens_stale_focus_shaped_markdown_is_loud_not_empty(monkeypatch):
    # [ap:XDVDRG] The lens stale-focus payload (built by
    # _render_lens_empty_result) through the ONE family finalizer: the loud
    # structured error, never the silent '' string (Markdown mode).
    cache, groups = _fixture_state()
    _install_context_and_tail(monkeypatch, cache, groups)
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        lightning={
            "mode": "render_lens",
            "lens": _union_lens(),
            "query": "outside-only",
            "query_mode": "smart",
            "match_scope": "name_and_note",
        },
        focus={"mode": "row", "uuid": "A1", "ancestor_path": ["R", "A"], "occurrence": 0},
    )
    result = asyncio.run(
        server._lightning_rod_render_lens(spec, output_file=None, target_tab=None)
    )
    assert result["focus_unresolved"] == "A1"
    shaped = server._shape_glimpse_family_return(result, output_file=None)
    assert isinstance(shaped, dict), shaped
    assert shaped.get("success") is False
    assert shaped.get("focus_unresolved") == "A1"
    assert "will not be guessed" in str(shaped.get("error"))
    assert "preview_markdown" not in shaped


def test_blank_query_push_and_path_bearing_domain_errors(monkeypatch):
    cache, groups = _fixture_state()
    _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()

    blank = asyncio.run(
        dispatch_operation(
            registry, "sift", {"lens": _union_lens(), "query": "   "}
        )
    )
    assert blank["error"]["code"] == "missing_parameter"
    assert "slice_group.render" in blank["error"]["message"]

    async def pushed(**kwargs):
        assert kwargs["lightning"]["mode"] == "render_lens"
        return {"success": True, "pushed_to_modal": True}

    monkeypatch.setattr(server, "_glimpse_push_slice_to_modal", pushed)
    push = asyncio.run(
        dispatch_operation(
            registry,
            "render",
            {"lens": _union_lens(), "push_to_modal": True},
        )
    )
    assert push == {"success": True, "pushed_to_modal": True}

    bad = asyncio.run(
        dispatch_operation(
            registry,
            "render",
            {"lens": {"kind": "subtree", "root_handle": "MISSING", "descend": "all"}},
        )
    )
    assert bad["error"]["code"] == "root_resolution_failed"
    assert bad["error"]["extra"]["path"] == "$"


def test_dynamic_reprojection_and_global_never_raw_subtree_lock(monkeypatch):
    cache, groups = _fixture_state()
    calls = _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()
    arch = {"kind": "group", "group_handle": "Architecture"}

    first = asyncio.run(
        dispatch_operation(
            registry, "render", {"lens": arch, "as_markdown": False}
        )
    )
    assert first["render_scope"]["leaf_count"] == 1
    # The adopted recipe stores the stable group id, so a later name change
    # cannot break Refresh/readback re-projection.
    adopted_spec = first["_test_spec"]
    groups.pop("Architecture")
    groups["grp-arch"]["name"] = "Architecture renamed after adoption"
    renamed = asyncio.run(
        server._lightning_rod_render_lens(
            adopted_spec, output_file=None, target_tab=None
        )
    )
    assert renamed["success"] is True
    assert renamed["render_scope"]["selection"]["lens"]["group_handle"] == "grp-arch"

    cache["A2"] = _node("A2", "New architecture child", "A", 1)
    second = asyncio.run(
        server._lightning_rod_render_lens(
            adopted_spec, output_file=None, target_tab=None
        )
    )
    assert second["render_scope"]["leaf_count"] == 2
    assert second["_test_tree"]["id"] == "A"

    raw = asyncio.run(
        dispatch_operation(
            registry,
            "render",
            {
                "lens": {"kind": "subtree", "root_handle": "S", "descend": "all"},
                "as_markdown": False,
            },
        )
    )
    assert raw["success"] is True
    assert raw["_test_tree"]["id"] == "S1"
    assert len(calls) == 4


def test_tail_alias_explicit_sort_and_adopted_blob_round_trip(monkeypatch):
    cache, groups = _fixture_state()
    _install_context_and_tail(monkeypatch, cache, groups)
    registry = server.build_slice_group_registry()
    result = asyncio.run(
        dispatch_operation(
            registry,
            "render",
            {
                "lens": _union_lens(),
                "since": "last 1",
                "sort_by": "created",
                "sort_direction": "desc",
                "as_markdown": False,
            },
        )
    )
    spec = result["_test_spec"]
    assert spec.sort_by == "created"
    assert spec.sort_direction == "desc"
    assert spec.since.form == "item_count" and spec.since.count == 1
    blob = spec.to_render_spec_blob()
    round_tripped = RenderProjectionSpec.from_render_spec_blob(blob)
    assert round_tripped.lightning == spec.lightning
    assert round_tripped.root_uuid == spec.root_uuid


def test_render_spec_dispatch_routes_render_lens(monkeypatch):
    seen = {}

    async def projector(spec, *, output_file, target_tab, operation_spec=None):
        seen.update(spec=spec, output_file=output_file, target_tab=target_tab)
        return {"success": True}

    monkeypatch.setattr(server, "_lightning_rod_render_lens", projector)
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        lightning={
            "mode": "render_lens",
            "lens": {"kind": "subtree", "root_handle": "R", "descend": "all"},
            "query": None,
            "query_mode": "smart",
            "match_scope": "name_and_note",
        }
    )
    result = asyncio.run(
        server._glimpse_dispatch_spec(spec, output_file="out.md", target_tab="tab")
    )
    assert result == {"success": True}
    assert seen == {"spec": spec, "output_file": "out.md", "target_tab": "tab"}


def test_internal_push_frame_carries_render_lens_recipe(monkeypatch):
    class Client:
        async def _ws_request_response(self, action, **kwargs):
            assert action == "apply_slice_in_dock"
            self.payload = kwargs["payload"]
            return {"success": True, "rendering": True}

    client = Client()

    async def ws_wait(max_wait_seconds):
        return object(), object()

    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "get_ws_connection_with_grace_wait", ws_wait)
    selection = {
        "mode": "render_lens",
        "lens": _union_lens(),
        "query": None,
        "query_mode": "smart",
        "match_scope": "name_and_note",
    }
    result = asyncio.run(
        server._glimpse_push_slice_to_modal(
            sort_by="none",
            sort_direction="asc",
            target_tab="tab",
            lightning=selection,
        )
    )
    assert result["success"] is True and result["rendering"] is True
    packed = client.payload["render_spec"]["lightning"]
    assert packed["mode"] == "render_lens"
    assert packed["lens"] == _union_lens()


def test_projector_reaches_real_shared_stage2_and_composer(monkeypatch):
    cache, groups = _fixture_state()

    async def build_snapshot(target_tab=None, exclude_completed=None):
        return SimpleNamespace(
            context=_context(cache, groups),
            cache_by_id=cache,
        )

    monkeypatch.setattr(server, "_build_lens_stage0_snapshot", build_snapshot)
    render_client = object.__new__(server.WorkFlowyClient)
    monkeypatch.setattr(server, "get_client", lambda: render_client)
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        lightning={
            "mode": "render_lens",
            "lens": _union_lens(),
            "query": None,
            "query_mode": "smart",
            "match_scope": "name_and_note",
        }
    )
    result = asyncio.run(
        server._lightning_rod_render_lens(
            spec, output_file=None, target_tab=None
        )
    )
    assert result["success"] is True
    assert "Resolver implementation" in result["preview_markdown"]
    assert "Deploy normalization" in result["preview_markdown"]
    assert result["render_scope"]["selection_kind"] == "lens"


def test_context_builder_uses_one_config_read_and_builds_dock_boundary(monkeypatch):
    cache, groups = _fixture_state()
    cache["D"] = _node("D", "Child #wfy-ns--local", "R", 4)
    calls = []

    async def resolve_config(target_tab):
        calls.append(target_tab)
        return (
            [groups["Architecture"]],
            None,
            {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"},
            ["wfy-suppress"],
            {"note_session_tags": ["wfy-ns"], "note_session_local_postfixes": ["local"]},
        )

    class Client:
        def _get_nodes_export_cache_nodes(self):
            return list(cache.values())

    from coherence_harness import make_boundary_capable

    client = make_boundary_capable(Client(), nodes=list(cache.values()))
    monkeypatch.setattr(server, "_resolve_slice_group_config", resolve_config)
    monkeypatch.setattr(server, "get_client", lambda: client)

    # S03 §7.1: drive the builder inside an operation boundary (the direct
    # call is the explicit internal top-level operation for this test).
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary

    async def _drive():
        async with cache_operation_boundary(client, "test-lens-context"):
            return await server._build_lens_eval_context("tab")

    ctx = asyncio.run(_drive())
    assert calls == ["tab"]
    assert ctx.resolve_group("Architecture")["id"] == "grp-arch"
    assert ctx.resolve_root("R") == "R"
    assert ctx.anchor_boundary_predicate(cache["D"]) is True


def test_every_lens_domain_code_is_registry_declared():
    registry = server.SLICE_GROUP_REGISTRY
    expected = {
        "invalid_lens_shape",
        "invalid_lens_kind",
        "invalid_merge_operation",
        "empty_merge_inputs",
        "invalid_match_scope",
        "invalid_descend_policy",
        "lens_depth_exceeded",
        "lens_atom_budget_exceeded",
        "root_resolution_failed",
        "group_resolution_failed",
        "boundary_predicate_unavailable",
        "no_common_ancestor",
        "lightning_rod_query_error",
    }
    assert expected.issubset(set(registry.resolve("render").domain_error_codes))
    assert expected.issubset(set(registry.resolve("sift").domain_error_codes))
