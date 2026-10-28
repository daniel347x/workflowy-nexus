"""The dispatch invoker (Archive Reads U1, §U1.5 test 5).

The kernel hook is inert when absent; present, it wraps ``_invoke`` in the
same places the boundary does; the classification of every registered
operation matches §4.3.2 (with the U1 constant off and on); ``include``
installs the augmented snapshot and consumes the flag; and the §4.5 re-run
runs exactly once, augmented, with the first outcome discarded.
"""

from __future__ import annotations

import asyncio

import pytest

from coherence_harness import make_boundary_capable

from workflowy_mcp import server
from workflowy_mcp.archive_context import (
    ARCHIVE_MODE_NONE,
    current_archive_context,
)
from workflowy_mcp.archive_store import ArchiveStore, set_archive_store_for_tests
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
from workflowy_mcp.coherence.operation_cache import (
    cache_operation_boundary,
    current_operation_cache,
)
from workflowy_mcp.handle_corrections import (
    drain_handle_corrections,
    record_handle_correction,
    reset_handle_corrections,
)
from workflowy_mcp.mcp_api_registry import (
    OperationRegistry,
    OperationSpec,
    dispatch_operation,
)
from workflowy_mcp.node_archive import build_archive_document, serialize_archive

SOURCE_CACHE = {"epoch_number": 1, "epoch_id": "e1", "adopted_sequence": 3}


def _nest(records):
    return WorkFlowyClientEtch.__new__(WorkFlowyClientEtch)._build_hierarchy(
        list(records)
    )


def _rec(node_id, name="n", parent_id=None, priority=0, **extra):
    record = {
        "id": node_id,
        "name": name,
        "note": "",
        "parent_id": parent_id,
        "priority": priority,
    }
    record.update(extra)
    return record


def _archive_store(tmp_path):
    document = build_archive_document(
        [_rec("AR", name="archived root")],
        nest=_nest,
        node_id="AR",
        name="archived root",
        description=None,
        exported_at="2026-10-01T12:00:00Z",
        source_cache=dict(SOURCE_CACHE),
    )
    (tmp_path / "a.json").write_bytes(serialize_archive(document).encode("utf-8"))
    return ArchiveStore(folder_provider=lambda: str(tmp_path))


def _spec(verb, *, cache_backed=True, side_effects=("pure_read",), handler=None):
    if handler is None:

        async def handler(params):
            return {"ok": True}

    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    return OperationSpec(
        family="node",
        verb=verb,
        summary=f"probe {verb}",
        side_effects=tuple(side_effects),
        async_mode="never",
        params_schema=schema,
        returns={"type": "object"},
        examples=(),
        related=(),
        handler=handler,
        cache_backed=cache_backed,
    )


def _registry(specs):
    registry = OperationRegistry(family="node", family_summary="probe family")
    for spec in specs:
        registry.register(spec)
    return registry


# ---------------------------------------------------------------------------
# The kernel hook
# ---------------------------------------------------------------------------


def test_kernel_without_hook_is_unchanged():
    calls = []

    async def _handler(params):
        calls.append(("handler", dict(params)))
        return "raw result"

    spec = _spec("probeop", handler=_handler)
    registry = _registry([spec])
    result = asyncio.run(dispatch_operation(registry, "probeop", {}))
    assert result == "raw result"
    assert calls == [("handler", {})]


def test_kernel_hook_wraps_invoke_at_the_same_sites():
    events = []

    async def _handler_read(params):
        events.append("handler")
        return "raw result"

    async def _handler_write(params):
        events.append("handler2")
        return {"ok": 1}

    async def _invoker(spec, params, invoke):
        events.append(f"invoker:{spec.canonical_name}")
        return await invoke()

    spec_read = _spec("probeop", cache_backed=True, handler=_handler_read)
    spec_write = _spec("probewrite", cache_backed=False, handler=_handler_write)
    registry = _registry([spec_read, spec_write])
    client = make_boundary_capable(
        object.__new__(WorkFlowyClientNexus), nodes=[_rec("L1")]
    )

    async def _run():
        async with cache_operation_boundary(client, "t"):
            r1 = await dispatch_operation(
                registry, "probeop", {}, operation_invoker=_invoker
            )
            r2 = await dispatch_operation(
                registry, "probewrite", {}, operation_invoker=_invoker
            )
        return r1, r2

    r1, r2 = asyncio.run(_run())
    assert r1 == "raw result" and r2 == {"ok": 1}
    # inside the boundary for the cache_backed spec, outside for the other
    assert events == [
        "invoker:node.probeop",
        "handler",
        "invoker:node.probewrite",
        "handler2",
    ]


# ---------------------------------------------------------------------------
# The classification of every registered operation
# ---------------------------------------------------------------------------


def _all_registries():
    return [
        server.build_node_registry(),
        server.build_anchor_registry(),
        server.build_tag_registry(),
        server.build_slice_group_registry(),
        server.build_glimpse_registry(),
        server.build_lightning_rod_registry(),
        server.build_beacon_registry(),
        server.build_system_registry(),
    ]


_READ_SAFE = {"pure_read", "render", "modal_push", "artifact_write"}


def test_classification_of_every_registered_operation(monkeypatch):
    monkeypatch.setattr(server, "_ARCHIVE_FALLBACK_ENABLED", False)
    seen = 0
    for registry in _all_registries():
        for spec in registry.operations():
            mode, reason = server._archive_mode_for(spec, {})
            seen += 1
            if spec.family == "beacon":
                assert (mode, reason) == ("none", "beacon"), spec.canonical_name
            elif spec.family == "system":
                assert (mode, reason) == ("none", "system"), spec.canonical_name
            elif spec.canonical_name == "node.export":
                assert (mode, reason) == ("none", "export"), spec.canonical_name
            elif spec.canonical_name in (
                "tag.purge.preview",
                "slice_group.purge.preview",
            ):
                assert (
                    mode,
                    reason,
                ) == ("none", "purge_preview"), spec.canonical_name
            elif not spec.cache_backed:
                assert (mode, reason) == ("none", "write"), spec.canonical_name
            elif set(spec.side_effects) - _READ_SAFE:
                assert (mode, reason) == ("none", "write"), spec.canonical_name
            else:
                # U1: the constant is off — a read is none, never fallback
                assert mode == "none", spec.canonical_name
                assert reason is None, spec.canonical_name
    assert seen >= 60  # every registered operation was classified (P23: 70)


def test_classification_with_constant_on(monkeypatch):
    monkeypatch.setattr(server, "_ARCHIVE_FALLBACK_ENABLED", True)
    try:
        for registry in _all_registries():
            for spec in registry.operations():
                mode, reason = server._archive_mode_for(spec, {})
                if (
                    spec.family not in ("beacon", "system")
                    and spec.canonical_name != "node.export"
                    and spec.canonical_name
                    not in ("tag.purge.preview", "slice_group.purge.preview")
                    and spec.cache_backed
                    and not (set(spec.side_effects) - _READ_SAFE)
                ):
                    assert mode == "fallback", spec.canonical_name
                    assert reason is None
    finally:
        monkeypatch.setattr(server, "_ARCHIVE_FALLBACK_ENABLED", False)


def test_mode_decisions_sinks_flags_and_writes(monkeypatch):
    monkeypatch.setattr(server, "_ARCHIVE_FALLBACK_ENABLED", False)
    read_spec = _spec("probeop")
    write_spec = _spec(
        "probewrite", side_effects=("workflowy_mutation",), cache_backed=True
    )
    # a sink refuses first — even with the flag
    assert server._archive_mode_for(read_spec, {"output_file": "x"}) == ("none", "sink")
    # U3 (§5.9): the refused flag combinations RAISE before any handler
    # work (was: ("none", "sink") — the classifier's decision moved from a
    # silent mode to the loud invalid_parameter_combination refusal).
    import pytest as _pytest

    with _pytest.raises(server._ArchiveFlagRefusal):
        server._archive_mode_for(
            read_spec, {"output_file": "x", "include_archives": True}
        )
    with _pytest.raises(server._ArchiveFlagRefusal):
        server._archive_mode_for(
            read_spec, {"push_to_modal": True, "include_archives": True}
        )
    assert server._archive_mode_for(read_spec, {"push_to_modal": True}) == (
        "none",
        "sink",
    )
    # the flag includes (read, no sink)
    assert server._archive_mode_for(read_spec, {"include_archives": True}) == (
        "include",
        "flag",
    )
    # writes never — even with the flag (writes raise nothing: they never
    # carry the flag in a schema, and the classifier's write branch returns
    # before the flag check)
    assert server._archive_mode_for(write_spec, {"include_archives": True}) == (
        "none",
        "write",
    )
    assert server._archive_mode_for(
        write_spec, {"include_archives": True, "output_file": "x"}
    ) == ("none", "write")


# ---------------------------------------------------------------------------
# include installs the augmented snapshot; the flag is consumed
# ---------------------------------------------------------------------------


def test_include_installs_augmented_snapshot_and_consumes_flag(tmp_path):
    store = _archive_store(tmp_path)
    set_archive_store_for_tests(store)
    live_records = [_rec("L1", name="live one")]
    client = make_boundary_capable(
        object.__new__(WorkFlowyClientNexus), nodes=live_records
    )
    seen = {}

    async def _handler(params):
        snapshot = current_operation_cache()
        seen["params"] = dict(params)
        seen["has_archive_record"] = "AR" in snapshot.nodes_by_id
        seen["context"] = current_archive_context()
        return "handler saw it"

    spec = _spec("probeop", handler=_handler)

    async def _run():
        async with cache_operation_boundary(client, "t"):
            return await server._mcp_archive_operation_invoker(
                spec, {"include_archives": True}, lambda: spec.handler({})
            )

    result = asyncio.run(_run())
    assert result == "handler saw it"
    # the flag was consumed: the handler never saw it
    assert "include_archives" not in seen["params"]
    # the augmented snapshot was installed
    assert seen["has_archive_record"] is True
    assert seen["context"].mode == "include"
    assert seen["context"].reason == "flag"
    set_archive_store_for_tests(None)


def test_include_with_no_visible_record_runs_live(tmp_path):
    empty_store = ArchiveStore(folder_provider=lambda: "/nonexistent-xyz")
    set_archive_store_for_tests(empty_store)
    live_records = [_rec("L1", name="live one")]
    client = make_boundary_capable(
        object.__new__(WorkFlowyClientNexus), nodes=live_records
    )
    seen = {}

    async def _handler(params):
        snapshot = current_operation_cache()
        seen["has_archive_record"] = "AR" in snapshot.nodes_by_id
        seen["context"] = current_archive_context()
        return "ok"

    spec = _spec("probeop", handler=_handler)

    async def _run():
        async with cache_operation_boundary(client, "t"):
            return await server._mcp_archive_operation_invoker(
                spec, {"include_archives": True}, lambda: spec.handler({})
            )

    asyncio.run(_run())
    assert seen["has_archive_record"] is False
    # byte-identical behaviour: mode none / reason flag, no overlay
    assert seen["context"].mode == ARCHIVE_MODE_NONE
    assert seen["context"].reason == "flag"
    assert seen["context"].overlay is None
    set_archive_store_for_tests(None)


# ---------------------------------------------------------------------------
# The §4.5 re-run
# ---------------------------------------------------------------------------


def _with_constant_on():
    """Context-manager-free helper: the tests flip the constant explicitly."""


def test_rerun_exactly_once_augmented_reason_automatic(tmp_path):
    store = _archive_store(tmp_path)
    set_archive_store_for_tests(store)
    live_records = [_rec("L1", name="live one")]
    client = make_boundary_capable(
        object.__new__(WorkFlowyClientNexus), nodes=live_records
    )
    calls = []
    reset_handle_corrections()

    async def _handler(params):
        context = current_archive_context()
        calls.append(
            (context.mode, context.reason, "AR" in current_operation_cache().nodes_by_id)
        )
        if len(calls) == 1:
            # the first run requests a re-run (as the gate would)
            context.state.request_rerun()
            # and records a correction the re-run must NOT inherit
            record_handle_correction(
                supplied="x", resolved_uuid="L1", name="stale", short="x"
            )
            return "FIRST OUTCOME"
        return "SECOND OUTCOME"

    spec = _spec("probeop", handler=_handler)
    previous = server._ARCHIVE_FALLBACK_ENABLED
    server._ARCHIVE_FALLBACK_ENABLED = True
    try:

        async def _run():
            async with cache_operation_boundary(client, "t"):
                return await server._mcp_archive_operation_invoker(
                    spec, {}, lambda: spec.handler({})
                )

        result = asyncio.run(_run())
    finally:
        server._ARCHIVE_FALLBACK_ENABLED = previous
        set_archive_store_for_tests(None)
        reset_handle_corrections()
    # the first outcome is discarded; exactly one re-run, augmented
    assert result == "SECOND OUTCOME"
    assert calls == [
        ("fallback", None, False),
        ("include", "automatic", True),
    ]
    # corrections were reset between the runs: the stale record is gone
    assert drain_handle_corrections() == []


def test_rerun_discards_first_exception(tmp_path):
    store = _archive_store(tmp_path)
    set_archive_store_for_tests(store)
    live_records = [_rec("L1", name="live one")]
    client = make_boundary_capable(
        object.__new__(WorkFlowyClientNexus), nodes=live_records
    )
    calls = []

    async def _handler(params):
        context = current_archive_context()
        calls.append(context.mode)
        if len(calls) == 1:
            context.state.request_rerun()
            raise RuntimeError("first run failed")
        return "SECOND OUTCOME"

    spec = _spec("probeop", handler=_handler)
    previous = server._ARCHIVE_FALLBACK_ENABLED
    server._ARCHIVE_FALLBACK_ENABLED = True
    try:

        async def _run():
            async with cache_operation_boundary(client, "t"):
                return await server._mcp_archive_operation_invoker(
                    spec, {}, lambda: spec.handler({})
                )

        result = asyncio.run(_run())
    finally:
        server._ARCHIVE_FALLBACK_ENABLED = previous
        set_archive_store_for_tests(None)
    assert result == "SECOND OUTCOME"
    assert calls == ["fallback", "include"]


def test_rerun_first_error_stands_when_not_requested():
    live_records = [_rec("L1", name="live one")]
    client = make_boundary_capable(
        object.__new__(WorkFlowyClientNexus), nodes=live_records
    )
    calls = []

    async def _handler(params):
        context = current_archive_context()
        calls.append(context.mode)
        raise RuntimeError("boom")

    spec = _spec("probeop", handler=_handler)
    previous = server._ARCHIVE_FALLBACK_ENABLED
    server._ARCHIVE_FALLBACK_ENABLED = True
    try:

        async def _run():
            async with cache_operation_boundary(client, "t"):
                return await server._mcp_archive_operation_invoker(
                    spec, {}, lambda: spec.handler({})
                )

        asyncio.run(_run())
        raise AssertionError("expected the first error to propagate")
    except RuntimeError as exc:
        assert "boom" in str(exc)
    finally:
        server._ARCHIVE_FALLBACK_ENABLED = previous
    assert calls == ["fallback"]


def test_rerun_overlay_vanished_first_outcome_stands():
    live_records = [_rec("L1", name="live one")]
    client = make_boundary_capable(
        object.__new__(WorkFlowyClientNexus), nodes=live_records
    )
    calls = []

    async def _handler(params):
        context = current_archive_context()
        calls.append(context.mode)
        if len(calls) == 1:
            context.state.request_rerun()
            return "FIRST OUTCOME"
        raise AssertionError("must not re-run when the overlay vanished")

    spec = _spec("probeop", handler=_handler)
    previous = server._ARCHIVE_FALLBACK_ENABLED
    server._ARCHIVE_FALLBACK_ENABLED = True
    try:

        async def _run():
            async with cache_operation_boundary(client, "t"):
                # no store injected: the overlay is None at re-run time
                return await server._mcp_archive_operation_invoker(
                    spec, {}, lambda: spec.handler({})
                )

        result = asyncio.run(_run())
    finally:
        server._ARCHIVE_FALLBACK_ENABLED = previous
    assert result == "FIRST OUTCOME"
    assert calls == ["fallback"]


# ---------------------------------------------------------------------------
# U3's closed envelope: exactly the 16 flag verbs accept the flag (§U3.2 D1)
# ---------------------------------------------------------------------------

_FLAG_VERBS = {
    "node.search",
    "node.scry",
    "anchor.search",
    "anchor.dock.peek",
    "anchor.dock.render",
    "anchor.dock.list",
    "slice_group.render",
    "slice_group.sift",
    "slice_group.merge.render",
    "glimpse.run",
    "glimpse.estate",
    "lightning_rod.gather",
    "lightning_rod.render_tags",
    "lightning_rod.render_search",
    "lightning_rod.render_nodes",
    "lightning_rod.render_symbols",
}


def test_flag_schema_on_exactly_the_sixteen_verbs():
    carried = set()
    for registry in _all_registries():
        for spec in registry.operations():
            properties = (spec.params_schema or {}).get("properties") or {}
            if "include_archives" in properties:
                carried.add(spec.canonical_name)
                # Registration deep-freezes metadata, so identity cannot
                # survive; the VALUE equality plus the source-level
                # single-definition grep (test_archive_describe_texts.py)
                # pin the one-definition rule together.
                assert dict(properties["include_archives"]) == dict(
                    server._INCLUDE_ARCHIVES_PROPERTY
                ), spec.canonical_name
            additional = (spec.params_schema or {}).get("additionalProperties")
            assert additional is False, spec.canonical_name
    assert carried == _FLAG_VERBS, sorted(carried ^ _FLAG_VERBS)


def test_u2_ships_the_fallback_constant_on():
    # §U2.2 deliverable 1: U2 flips the module constant; the classification
    # above already proves both spellings behave.  This pins the SHIPPED
    # value so a silent revert fails loudly.
    assert server._ARCHIVE_FALLBACK_ENABLED is True
    # The universal archive error code is declared (§4.7.3).
    from workflowy_mcp.mcp_api_registry import CORE_ERROR_CODES

    assert "archived_node_read_only" in CORE_ERROR_CODES
