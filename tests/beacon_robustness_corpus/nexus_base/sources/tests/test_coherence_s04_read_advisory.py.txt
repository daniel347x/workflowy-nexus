"""S04 §14.3 — hard-barrier read/admission contract tests.

The standardized advisory (A1 format locks), the mutation/identity-proof
refusals, recovery/control exceptions, the §7.3 disk table, universal error
codes, and clean-path byte identity.
"""

from __future__ import annotations

import asyncio

import pytest
from coherence_harness import (
    ACCOUNT_A,
    make_boundary_capable,
    make_coherence_root,
    s04_boundary_status,
    s04_raise_barrier,
)

from workflowy_mcp import server
from workflowy_mcp.coherence import (
    COHERENCE_READ_ADVISORY_LINE,
    CoherenceBarrierActiveError,
    apply_coherence_read_advisory,
    cache_operation_boundary,
)
from workflowy_mcp.coherence.read_advisory import (
    current_boundary_status,
)
from workflowy_mcp.mcp_api_registry import CORE_ERROR_CODES, dispatch_operation


def _double(nodes=(), status=None):
    client = object.__new__(server.WorkFlowyClient)
    return make_boundary_capable(client, nodes=nodes, boundary_status=status)


def _dispatch(registry, verb, params):
    return asyncio.run(
        dispatch_operation(
            registry,
            verb,
            params,
            error_logger=lambda *_a: None,
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
        )
    )


HARD = s04_boundary_status(
    hard_dirty=True,
    hard_generation=7,
    hard_reason_code="verified_effect_unpublishable",
    hard_transaction_id="tx-advisory",
)


# ---------------------------------------------------------------------------
# Helper mechanics (the ONE advisory authority)
# ---------------------------------------------------------------------------


def test_string_prefix_shape_and_idempotence():
    out = apply_coherence_read_advisory("PAYLOAD", status=HARD)
    assert out.startswith(COHERENCE_READ_ADVISORY_LINE + "\n\n")
    assert out.endswith("PAYLOAD")
    assert out.count("COHERENCE FAILURE") == 1
    lines = out.split("\n")
    assert lines[0] == COHERENCE_READ_ADVISORY_LINE
    assert lines[1] == ""  # ONE blank separator line
    assert "advisory only; NOT part of the returned content" in lines[0]
    # Idempotent: a second application is a no-op.
    assert apply_coherence_read_advisory(out, status=HARD) == out


def test_clean_status_is_byte_identical():
    clean = s04_boundary_status(hard_dirty=False)
    assert apply_coherence_read_advisory("PAYLOAD", status=clean) == "PAYLOAD"
    result = {"success": True, "preview_markdown": "doc"}
    assert apply_coherence_read_advisory(result, status=clean) == result


def test_dict_shapes_and_error_passthrough():
    preview = apply_coherence_read_advisory(
        {"success": True, "preview_markdown": "doc", "node_count": 1}, status=HARD
    )
    assert preview["preview_markdown"].startswith(COHERENCE_READ_ADVISORY_LINE)
    assert preview["coherence_advisory"]["status"] == "hard_dirty"
    assert preview["coherence_advisory"]["dirty_generation"] == 7
    assert preview["coherence_advisory"]["reason_code"] == "verified_effect_unpublishable"
    assert preview["coherence_advisory"]["transaction_id"] == "tx-advisory"
    text = apply_coherence_read_advisory({"success": True, "preview_text": "tags"}, status=HARD)
    assert text["preview_text"].startswith(COHERENCE_READ_ADVISORY_LINE)
    generic = apply_coherence_read_advisory({"success": True, "data": 1}, status=HARD)
    assert "coherence_advisory" in generic and generic["data"] == 1
    error = {"success": False, "error": {"code": "x"}}
    assert apply_coherence_read_advisory(error, status=HARD) == error
    # Idempotence on dicts.
    assert apply_coherence_read_advisory(preview, status=HARD) == preview


def test_status_from_task_context_and_none_outside_boundary():
    # Outside any operation boundary: no captured status -> no advisory.
    assert current_boundary_status() is None
    assert apply_coherence_read_advisory("X") == "X"

    async def _inside():
        client = _double(status=HARD)
        async with cache_operation_boundary(client, "test"):
            return apply_coherence_read_advisory("X")

    out = asyncio.run(_inside())
    assert out.startswith(COHERENCE_READ_ADVISORY_LINE)


# ---------------------------------------------------------------------------
# MCP dispatch integration (kernel + hooks + boundary)
# ---------------------------------------------------------------------------


def test_node_read_advisory_and_tier2_disclosure(monkeypatch):
    async def _read_impl(*_a, **_kw):
        return "RAW DOCUMENT"

    monkeypatch.setattr(server, "_node_read_impl", _read_impl)
    monkeypatch.setattr(server, "get_client", lambda: _double(status=HARD))
    registry = server.build_node_registry()
    result = _dispatch(registry, "read", {"node_handle": "N"})
    assert isinstance(result, str)
    assert result.startswith(COHERENCE_READ_ADVISORY_LINE + "\n\nRAW DOCUMENT")
    assert result.count("COHERENCE FAILURE") == 1
    # The payload bytes follow the separator byte-identically.
    assert result.endswith("\n\nRAW DOCUMENT")
    # A1 Tier-2 disclosure in the registry returns description.
    spec = registry.resolve("read")
    assert "ADVISORY FRAMING" in spec.returns["description"]
    assert "NOT part of the returned" in spec.returns["description"]


def test_beacon_snippet_proceeds_with_advisory_and_disclosure(monkeypatch):
    async def _snippet_impl(*_a, **_kw):
        # U01 §9.1 item 21: the advisory precedes the snippet HEADER row.
        return "[snippet: X.py | lines 1-1 of 1 | 1 lines, 13 bytes, ~5 tokens]"

    monkeypatch.setattr(server, "_beacon_snippet_impl", _snippet_impl)
    monkeypatch.setattr(server, "get_client", lambda: _double(status=HARD))
    registry = server.build_beacon_registry()
    result = _dispatch(registry, "snippet", {"source_handle": "foo"})
    assert result.startswith(COHERENCE_READ_ADVISORY_LINE + "\n\n[snippet: ")
    spec = registry.resolve("snippet")
    assert "ADVISORY FRAMING" in spec.returns["description"]


def test_clean_dispatch_is_byte_identical(monkeypatch):
    async def _read_impl(*_a, **_kw):
        return "RAW DOCUMENT"

    monkeypatch.setattr(server, "_node_read_impl", _read_impl)
    monkeypatch.setattr(server, "get_client", lambda: _double(status=None))
    registry = server.build_node_registry()
    assert _dispatch(registry, "read", {"node_handle": "N"}) == "RAW DOCUMENT"


def test_anchor_inventory_and_point_resolve_advisory(monkeypatch):
    async def _dock_list(*_a, **_kw):
        return "DOCK INVENTORY"

    async def _point_resolve(*_a, **_kw):
        return {"success": True, "match_count": 1, "uuid": "u-1"}

    monkeypatch.setattr(server, "_anchor_dock_list_impl", _dock_list)
    monkeypatch.setattr(server, "_anchor_point_resolve_impl", _point_resolve)
    monkeypatch.setattr(server, "get_client", lambda: _double(status=HARD))
    registry = server.build_anchor_registry()
    text = _dispatch(registry, "dock.list", {})
    assert text.startswith(COHERENCE_READ_ADVISORY_LINE + "\n\nDOCK INVENTORY")
    resolved = _dispatch(registry, "point.resolve", {"ap_id": "[ap:K7M2QF]"})
    assert resolved["success"] is True
    assert resolved["coherence_advisory"]["status"] == "hard_dirty"
    assert resolved["uuid"] == "u-1"


def test_cache_free_operations_get_no_advisory(monkeypatch):
    async def _groups(*_a, **_kw):
        return "GROUPS"

    monkeypatch.setattr(server, "_slice_group_list_impl", _groups)
    monkeypatch.setattr(server, "get_client", lambda: _double(status=HARD))
    registry = server.build_slice_group_registry()
    # slice_group.list is cache-FREE (config read) — no boundary, no advisory.
    assert _dispatch(registry, "list", {}) == "GROUPS"


# ---------------------------------------------------------------------------
# Mutation / identity refusals (real gate, real barrier paths)
# ---------------------------------------------------------------------------


def _barrier_paths(tmp_path):
    paths = make_coherence_root(tmp_path / "jobs", ACCOUNT_A)
    s04_raise_barrier(paths, transaction_id="tx-gate")
    return paths


def test_mutation_refusal_envelope_before_any_side_effect(monkeypatch, tmp_path):
    paths = _barrier_paths(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    called: list[str] = []
    monkeypatch.setattr(
        server, "_node_edit_impl", lambda *_a, **_kw: called.append("edit") or {"success": True}
    )
    monkeypatch.setattr(server, "get_client", lambda: _double(status=HARD))
    registry = server.build_node_registry()
    result = _dispatch(registry, "edit", {"node_handle": "N", "name": "x"})
    assert called == []  # the handler never ran (refusal before side effects)
    assert result["success"] is False
    assert result["error"]["code"] == "coherence_barrier_active"
    assert result["error"]["code"] != "operation_failed"
    extra = result["error"]["extra"]
    assert extra["refresh_required"] is True
    assert extra["hard_dirty"] is True
    assert extra["dirty_generation"] == 1
    assert extra["reason_code"] == "journal_publication_failed"
    assert extra["transaction_id"] == "tx-gate"
    assert extra["recovery"] == {"family": "system", "verb": "cache.refresh", "params": {}}


def test_recovery_and_control_exceptions_proceed(monkeypatch, tmp_path):
    paths = _barrier_paths(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    calls: list[str] = []
    monkeypatch.setattr(server, "_system_cache_refresh_impl", lambda: calls.append("refresh") or {"success": True})
    monkeypatch.setattr(server, "_system_job_status_impl", lambda *_a: calls.append("status") or {"success": True})
    monkeypatch.setattr(server, "_system_job_cancel_impl", lambda *_a: calls.append("cancel") or {"success": True})
    monkeypatch.setattr(server, "get_client", lambda: _double(status=HARD))
    registry = server.build_system_registry()
    _dispatch(registry, "cache.refresh", {})
    _dispatch(registry, "job.status", {})
    _dispatch(registry, "job.cancel", {"job_id": "j-1"})
    assert calls == ["refresh", "status", "cancel"]


def test_slice_group_crud_proceeds_under_hard_dirt(monkeypatch, tmp_path):
    """S04 Remediation Chain 4 (work-order item 2): cache-free slice-group
    CRUD (class ``proceed``) is NOT blocked under hard dirt — the mutation
    gate fires only for refuse-class operations, and these fronts are
    cache-free config writes."""
    paths = _barrier_paths(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    calls: list[str] = []
    monkeypatch.setattr(
        server,
        "_slice_group_create_impl",
        lambda *_a, **_kw: calls.append("create") or {"success": True},
    )
    monkeypatch.setattr(
        server,
        "_slice_group_update_impl",
        lambda *_a, **_kw: calls.append("update") or {"success": True},
    )
    monkeypatch.setattr(
        server,
        "_slice_group_delete_impl",
        lambda *_a, **_kw: calls.append("delete") or {"success": True},
    )
    monkeypatch.setattr(server, "get_client", lambda: _double(status=HARD))
    registry = server.build_slice_group_registry()
    _dispatch(registry, "create", {"name": "g", "root_handle": "r", "tags": ["t"]})
    _dispatch(registry, "update", {"group_handle": "g", "name": "g2"})
    _dispatch(registry, "delete", {"group_handle": "g2"})
    assert calls == ["create", "update", "delete"]


def test_disk_table_refusals(monkeypatch, tmp_path):
    paths = _barrier_paths(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    calls: list[str] = []
    monkeypatch.setattr(server, "_beacon_tags_mutate_impl", lambda *_a, **_kw: calls.append("tags") or {})
    monkeypatch.setattr(server, "_beacon_sync_node_impl", lambda *_a, **_kw: calls.append("sync") or {})
    monkeypatch.setattr(server, "_beacon_map_refresh_impl", lambda *_a, **_kw: calls.append("map") or {})
    monkeypatch.setattr(server, "get_client", lambda: _double(status=HARD))
    registry = server.build_beacon_registry()
    for verb, params in (
        ("tags.ensure", {"symbols": ["x"], "tags": ["t"], "root_handles": ["r"]}),
        ("sync_node", {"node_handle": "N"}),
        ("map.refresh", {"node_handle": "N", "mode": "file"}),
    ):
        result = _dispatch(registry, verb, params)
        assert result["error"]["code"] == "coherence_barrier_active", verb
    assert calls == []


def test_ap_uniqueness_proof_refuses_under_hard_dirt(monkeypatch, tmp_path):
    paths = _barrier_paths(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    with pytest.raises(CoherenceBarrierActiveError):
        asyncio.run(server._anchor_reserve_ap_core("[ap:ABC123]", nodes_by_id={}))


def test_carto_launch_refuses_before_job_creation(monkeypatch, tmp_path):
    paths = _barrier_paths(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    jobs_dir = tmp_path / "carto_jobs"
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(jobs_dir))
    result = asyncio.run(server._start_carto_refresh_job("root", "file"))
    assert result["success"] is False
    assert result["error_kind"] == "coherence_barrier_active"
    assert result["coherence_admission"]["hard_dirty"] is True
    # No job artifact was created.
    assert not jobs_dir.exists() or list(jobs_dir.iterdir()) == []


def test_ws_mutation_ingress_refuses(monkeypatch, tmp_path):
    paths = _barrier_paths(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    sent: list[dict] = []

    class _WS:
        async def send(self, payload):
            import json as _json

            sent.append(_json.loads(payload))

    monkeypatch.setattr(server, "get_client", lambda: _double(status=HARD))
    asyncio.run(
        server._handle_mutate_node_tags(
            {"action": "mutate_node_tags", "request_id": "r1", "v": 1, "operations": []},
            _WS(),
        )
    )
    assert len(sent) == 1
    assert sent[0]["success"] is False
    assert sent[0]["error_code"] == "coherence_barrier_active"
    assert sent[0]["request_id"] == "r1"
    assert sent[0]["coherence_admission"]["hard_dirty"] is True


def test_async_job_recheck_terminates_with_zero_mutations(monkeypatch, tmp_path):
    paths = make_coherence_root(tmp_path / "jobs", ACCOUNT_A)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    monkeypatch.setattr(server, "get_client", lambda: _double(status=None))
    server._jobs.clear()
    mutated: list[str] = []

    import workflowy_mcp.coherence.operation_cache as _opc

    release: asyncio.Event

    async def _scenario():
        nonlocal release
        release = asyncio.Event()

        real_spawn = _opc.create_isolated_context_task

        def _holding_spawn(coro, *, name=None):
            async def _held():
                await release.wait()
                return await coro

            return real_spawn(_held(), name=name)

        # Hold the dispatched runner until the barrier is durably raised.
        _opc.create_isolated_context_task = _holding_spawn
        try:
            ack = await server._start_background_job("etch", {}, _factory)
            # Enqueue-clean, then dirty BEFORE the dispatched operation runs.
            s04_raise_barrier(paths, transaction_id="tx-async")
            release.set()
            await server._jobs[ack["job_id"]]["_task"]
        finally:
            _opc.create_isolated_context_task = real_spawn
        return ack

    async def _factory(_job_id):
        mutated.append("ran")
        return {"success": True}

    ack = asyncio.run(_scenario())
    job = server._jobs[ack["job_id"]]
    assert mutated == []  # zero mutations
    assert job["status"] == "failed"
    assert "CoherenceBarrierActiveError" in job["error"] or "dirty barrier" in job["error"]


def test_universal_codes_declared():
    assert "coherence_barrier_active" in CORE_ERROR_CODES
    assert "refresh_required" in CORE_ERROR_CODES


# ---------------------------------------------------------------------------
# Render seams (stage-2 tail: agent shaping + modal delivery + output file)
# ---------------------------------------------------------------------------


def test_stage2_tail_advisory_covers_preview_modal_and_output_file(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "get_client", lambda: _double(status=HARD))

    forest = {
        "id": "root",
        "name": "Root",
        "children": [{"id": "leaf", "name": "Leaf", "note": "body", "children": []}],
    }
    cache_by_id = {
        "root": {"id": "root", "name": "Root", "priority": 100},
        "leaf": {"id": "leaf", "name": "Leaf", "priority": 100, "note": "body"},
    }
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    spec = RenderProjectionSpec(root_uuid="root", as_markdown=True)
    out_file = tmp_path / "out.md"

    async def _run():
        client = _double(status=HARD)
        async with cache_operation_boundary(client, "test-render"):
            return server.render_stage2_filter_sort(
                spec=spec,
                final_subtree=forest,
                cache_by_id=cache_by_id,
                output_file=str(out_file),
                source_meta={"_source": "test"},
            )

    summary = asyncio.run(_run())
    # The written file carries the warning line.
    written_text = out_file.read_text(encoding="utf-8")
    assert written_text.startswith(COHERENCE_READ_ADVISORY_LINE + "\n\n")
    # The file summary carries the structured advisory.
    assert summary["coherence_advisory"]["status"] == "hard_dirty"

    async def _run_inline():
        client = _double(status=HARD)
        async with cache_operation_boundary(client, "test-render"):
            return server.render_stage2_filter_sort(
                spec=spec,
                final_subtree=forest,
                cache_by_id=cache_by_id,
                output_file=None,
                source_meta={"_source": "test"},
            )

    payload = asyncio.run(_run_inline())
    # The modal/agent-shared internal dict: preview prefixed + field, exactly once.
    assert payload["preview_markdown"].startswith(COHERENCE_READ_ADVISORY_LINE)
    assert payload["preview_markdown"].count("COHERENCE FAILURE") == 1
    assert payload["coherence_advisory"]["status"] == "hard_dirty"
    assert "Leaf" in payload["preview_markdown"]


def test_stage2_tail_clean_is_byte_identical(monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: _double(status=None))
    forest = {"id": "root", "name": "Root", "children": []}
    cache_by_id = {"root": {"id": "root", "name": "Root", "priority": 100}}
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    spec = RenderProjectionSpec(root_uuid="root", as_markdown=True)

    async def _run():
        client = _double(status=None)
        async with cache_operation_boundary(client, "test-render"):
            return server.render_stage2_filter_sort(
                spec=spec,
                final_subtree=forest,
                cache_by_id=cache_by_id,
                output_file=None,
                source_meta={"_source": "test"},
            )

    payload = asyncio.run(_run())
    assert "coherence_advisory" not in payload
    assert not payload["preview_markdown"].startswith("⚠️")


# ---------------------------------------------------------------------------
# S04 Remediation Chain 1 (work-order item 4) — node.scry output-FILE advisory
# ---------------------------------------------------------------------------

_SCRY_FLAT_NODES = [
    {"id": "ROOT", "name": "Root Node", "note": "root note", "priority": 1},
    {"id": "CHILD", "name": "Child Node", "parent_id": "ROOT", "priority": 1},
]


def _install_scry_stubs(monkeypatch, status):
    """Drive the REAL node.scry output-file path end-to-end through dispatch:
    real ``_node_scry_impl`` → real ``workflowy_scry`` → real
    ``bulk_export_to_file_impl`` (only the cache fetch and the client's
    prefix-fallback export are stubbed)."""
    import json as _json  # noqa: F401 — kept for parity with readers of this fixture

    from workflowy_mcp.client import api_client_etch

    client = _double(status=status)

    async def _no_export(*_a, **_kw):
        raise RuntimeError("export_nodes unavailable in this test")

    client.export_nodes = _no_export

    async def _export_impl(_client, node_id=None, **_kw):
        return {
            "nodes": [dict(node) for node in _SCRY_FLAT_NODES],
            "_total_fetched_from_api": len(_SCRY_FLAT_NODES),
        }

    monkeypatch.setattr(api_client_etch, "export_nodes_impl", _export_impl)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    return client


def _dispatch_scry(output_file):
    registry = server.build_node_registry()
    return _dispatch(registry, "scry", {"root_handle": "ROOT", "output_file": str(output_file)})


def test_node_scry_output_file_hard_dirty_advises_terrain_markdown_and_summary(
    tmp_path, monkeypatch
):
    """Under a captured hard-dirty boundary status, node.scry(output_file=...)
    advises ALL THREE surfaces through the ONE authority: valid terrain JSON
    with structured coherence_advisory metadata, the companion Markdown
    prefixed with the canonical warning line + blank separator, and the
    structured advisory in the returned file summary (exactly once).
    """
    import json as _json

    _install_scry_stubs(monkeypatch, HARD)
    out = tmp_path / "scry_out.json"
    summary = _dispatch_scry(out)

    # 1. The returned file summary carries the structured advisory once.
    assert summary["success"] is True
    assert summary["coherence_advisory"]["status"] == "hard_dirty"
    assert summary["coherence_advisory"]["dirty_generation"] == 7
    assert summary["coherence_advisory"]["reason_code"] == "verified_effect_unpublishable"
    assert summary["coherence_advisory"]["transaction_id"] == "tx-advisory"
    assert list(summary.keys()).count("coherence_advisory") == 1
    # Idempotence across the producer seam + the dispatch finalizer.
    assert apply_coherence_read_advisory(summary, status=HARD) == summary

    # 2. The terrain JSON stays VALID and gains the structured metadata.
    terrain = _json.loads(out.read_text(encoding="utf-8"))
    assert terrain["coherence_advisory"]["status"] == "hard_dirty"
    assert terrain["coherence_advisory"]["dirty_generation"] == 7
    assert terrain["nodes"], "payload nodes still present"

    # 3. The companion Markdown carries the canonical warning line + ONE
    # blank separator, exactly once, with the payload following.
    markdown = (tmp_path / "scry_out.md").read_text(encoding="utf-8")
    assert markdown.startswith(COHERENCE_READ_ADVISORY_LINE + "\n\n")
    assert markdown.count("COHERENCE FAILURE") == 1
    assert "Root Node" in markdown


def test_node_scry_output_file_clean_path_byte_identical(tmp_path, monkeypatch):
    """Clean-path lock: no advisory anywhere — artifacts and summary are
    byte-identical to the pre-fix output."""
    import json as _json

    _install_scry_stubs(monkeypatch, None)
    out = tmp_path / "scry_out.json"
    summary = _dispatch_scry(out)

    assert "coherence_advisory" not in summary
    terrain_text = out.read_text(encoding="utf-8")
    terrain = _json.loads(terrain_text)
    assert "coherence_advisory" not in terrain
    markdown = (tmp_path / "scry_out.md").read_text(encoding="utf-8")
    assert "COHERENCE FAILURE" not in markdown
    assert markdown.startswith("<!-- EXPORTED_ROOT_UUID: ROOT -->")
    assert not markdown.startswith("⚠️")


def test_node_scry_output_file_advisory_exactly_once_across_seams(
    tmp_path, monkeypatch
):
    """The producer seam and the dispatch finalizer BOTH legitimately touch
    the same result: exactly one advisory survives (idempotence lock)."""
    _install_scry_stubs(monkeypatch, HARD)
    out = tmp_path / "scry_out.json"
    summary = _dispatch_scry(out)
    assert list(summary.keys()).count("coherence_advisory") == 1
    markdown = (tmp_path / "scry_out.md").read_text(encoding="utf-8")
    assert markdown.count(COHERENCE_READ_ADVISORY_LINE) == 1
    assert markdown.count("COHERENCE FAILURE") == 1
