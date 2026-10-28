"""Two-phase resolution, live first (Archive Reads U1, §U1.5 test 4).

Layer 1, the AP primitive, the composite and Layer 3 (symbol runner
stubbed): archives answer only after a total live miss, at every primitive,
with the one sanctioned change (an exact archived full UUID beats a SHAVED
live match, E2) — and the partition holds even in a plain thread with no
operation context.
"""

from __future__ import annotations

import asyncio
import uuid as uuid_module
from concurrent.futures import ThreadPoolExecutor

import pytest

from coherence_harness import make_boundary_capable

from workflowy_mcp.archive_context import (
    ARCHIVE_MODE_FALLBACK,
    ARCHIVE_MODE_NONE,
    ArchiveOperationContext,
    ArchiveOperationState,
    current_archive_context,
)
from workflowy_mcp.archive_store import ArchiveStore, set_archive_store_for_tests
from workflowy_mcp.client.api_client_nexus import (
    WorkFlowyClientNexus,
    resolve_uuid_from_anchor_point,
    resolve_uuid_like_node_handle,
    resolve_uuid_or_anchor_point_handle,
)
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.coherence.cache_view import CacheView
from workflowy_mcp.coherence.operation_cache import (
    archive_operation_scope,
    cache_operation_boundary,
)
from workflowy_mcp.handle_corrections import (
    drain_handle_corrections,
    reset_handle_corrections,
)
from workflowy_mcp.node_archive import build_archive_document, serialize_archive

SOURCE_CACHE = {"epoch_number": 1, "epoch_id": "e1", "adopted_sequence": 3}

LIVE_UUID = "aaaaaaaa-0000-0000-0000-000000000001"
ARCHIVED_UUID = "aaaaaaaa-0000-0000-0000-0000000000ff"  # shares the 8-hex head
OTHER_LIVE = "bbbbbbbb-0000-0000-0000-000000000002"
ARCHIVE_ONLY = "cccccccc-0000-0000-0000-000000000003"


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


def _write_archive(tmp_path, records, node_id, name="arch.json"):
    document = build_archive_document(
        list(records),
        nest=_nest,
        node_id=node_id,
        name=name,
        description=None,
        exported_at="2026-10-01T12:00:00Z",
        source_cache=dict(SOURCE_CACHE),
    )
    (tmp_path / name).write_bytes(serialize_archive(document).encode("utf-8"))


def _live_view(records):
    return CacheView.build(
        epoch_number=7,
        epoch_id="live-epoch",
        adopted_sequence=11,
        nodes_export={"nodes": [dict(r) for r in records]},
    )


def _augmented_by_id(tmp_path, live_records, archive_records, archive_root):
    _write_archive(tmp_path, archive_records, archive_root)
    store = ArchiveStore(folder_provider=lambda: str(tmp_path))
    set_archive_store_for_tests(store)
    live_view = _live_view(live_records)
    overlay = store.overlay_for(live_view)
    assert overlay is not None
    return store, live_view, overlay.augmented_view.nodes_by_id


@pytest.fixture(autouse=True)
def _clean_echo_state():
    reset_handle_corrections()
    yield
    reset_handle_corrections()
    set_archive_store_for_tests(None)


# ---------------------------------------------------------------------------
# Layer 1 and the AP primitive over an AUGMENTED map
# ---------------------------------------------------------------------------


def test_layer1_live_first_on_augmented_map(tmp_path):
    _, _, nodes = _augmented_by_id(
        tmp_path,
        [_rec(LIVE_UUID, name="live"), _rec(OTHER_LIVE, name="other live")],
        [_rec(ARCHIVED_UUID, name="archived"), _rec(ARCHIVE_ONLY, name="a-only", parent_id=ARCHIVED_UUID)],
        ARCHIVED_UUID,
    )
    # live exact
    hit = resolve_uuid_like_node_handle(LIVE_UUID, nodes)
    assert hit["success"] and hit["resolved_uuid"] == LIVE_UUID
    assert hit["input_kind"] == "exact_uuid"
    # live clean prefix shared with an archived UUID: live wins, no ambiguity
    hit = resolve_uuid_like_node_handle("aaaaaaaa", nodes)
    assert hit["success"] and hit["resolved_uuid"] == LIVE_UUID
    assert hit["input_kind"] == "uuid_prefix"
    # archive answers only after a total live miss
    hit = resolve_uuid_like_node_handle(ARCHIVED_UUID, nodes)
    assert hit["success"] and hit["resolved_uuid"] == ARCHIVED_UUID
    from workflowy_mcp.archive_context import is_archive_record

    assert is_archive_record(nodes[ARCHIVED_UUID])
    hit = resolve_uuid_like_node_handle(ARCHIVE_ONLY, nodes)
    assert hit["success"] and hit["resolved_uuid"] == ARCHIVE_ONLY
    # an ambiguous live prefix stops: both live 8-hex heads collide
    live_c = _rec("dddddddd-1111-1111-1111-111111111111", name="c1")
    live_d = _rec("dddddddd-2222-2222-2222-222222222222", name="c2")
    wide = _live_view([live_c, live_d])
    assert not resolve_uuid_like_node_handle("dddddddd", wide.nodes_by_id)["success"]


def test_layer1_exact_archived_uuid_beats_shaved_live_match(tmp_path):
    _, _, nodes = _augmented_by_id(
        tmp_path,
        [_rec(LIVE_UUID, name="live")],
        [_rec(ARCHIVED_UUID, name="archived")],
        ARCHIVED_UUID,
    )
    hit = resolve_uuid_like_node_handle(ARCHIVED_UUID, nodes)
    assert hit["success"] and hit["resolved_uuid"] == ARCHIVED_UUID
    # E2: the archived exact won — NO correction echo was recorded for the
    # shave the live kernel would otherwise have adopted
    assert drain_handle_corrections() == []
    # the tail-with-one-char-changed string still shaves to the live node
    other = ARCHIVED_UUID[:-1] + "e"
    hit = resolve_uuid_like_node_handle(other, nodes)
    assert hit["success"] and hit["resolved_uuid"] == LIVE_UUID
    corrections = drain_handle_corrections()
    assert len(corrections) == 1
    assert corrections[0]["resolved_uuid"] == LIVE_UUID


def test_ap_primitive_live_wins_and_archive_answers_on_miss(tmp_path):
    _, _, nodes = _augmented_by_id(
        tmp_path,
        [_rec(LIVE_UUID, name="[ap:LIVE1] live node")],
        [
            _rec(ARCHIVED_UUID, name="[ap:LIVE1] archived twin"),
            _rec(ARCHIVE_ONLY, name="[ap:DUPID1] dup a", parent_id=ARCHIVED_UUID),
            _rec(OTHER_LIVE, name="[ap:DUPID1] dup b", parent_id=ARCHIVED_UUID),
        ],
        ARCHIVED_UUID,
    )
    # live AP over archived AP
    result = resolve_uuid_from_anchor_point("[ap:LIVE1]", nodes)
    assert result["match_count"] == 1
    assert result["candidates"][0]["uuid"] == LIVE_UUID
    # duplicate archived AP-IDs -> archive-phase candidates
    result = resolve_uuid_from_anchor_point("[ap:DUPID1]", nodes)
    assert result["match_count"] == 2
    assert {c["uuid"] for c in result["candidates"]} == {ARCHIVE_ONLY, OTHER_LIVE}
    # archive answers only after a total live miss
    result = resolve_uuid_from_anchor_point("[ap:ONLYA1]", nodes)
    assert result["match_count"] == 0  # no such core anywhere


def test_composite_four_leg_order(tmp_path):
    _, _, nodes = _augmented_by_id(
        tmp_path,
        [_rec(LIVE_UUID, name="[ap:LIVE1] live node")],
        [
            _rec(ARCHIVED_UUID, name="[ap:ARCH1] archived"),
            _rec(ARCHIVE_ONLY, name="[ap:LIVE1] archived twin", parent_id=ARCHIVED_UUID),
        ],
        ARCHIVED_UUID,
    )
    # live UUID
    result = resolve_uuid_or_anchor_point_handle(LIVE_UUID, nodes)
    assert result["success"] and result["resolved_via"] == "uuid"
    # archived UUID: live legs miss, archive UUID leg answers
    result = resolve_uuid_or_anchor_point_handle(ARCHIVED_UUID, nodes)
    assert result["success"] and result["resolved_uuid"] == ARCHIVED_UUID
    assert result["resolved_via"] == "uuid"
    # live AP beats archived AP
    result = resolve_uuid_or_anchor_point_handle("[ap:LIVE1]", nodes)
    assert result["success"] and result["resolved_uuid"] == LIVE_UUID
    assert result["resolved_via"] == "anchor_point"
    # archived AP: live AP misses (LIVE1 archived twin), archive AP answers
    result = resolve_uuid_or_anchor_point_handle("[ap:ARCH1]", nodes)
    assert result["success"] and result["resolved_uuid"] == ARCHIVED_UUID
    assert result["resolved_via"] == "anchor_point"


def test_universe_semantics(tmp_path):
    _, _, nodes = _augmented_by_id(
        tmp_path,
        [_rec(LIVE_UUID, name="live")],
        [
            _rec(ARCHIVE_ONLY, name="archived only"),
            _rec(
                ARCHIVED_UUID,
                name="archived shared head",
                parent_id=ARCHIVE_ONLY,
            ),
        ],
        ARCHIVE_ONLY,
    )
    # Rev 2 §4.4.2 steps 2(a)/3: an archive record's key is NEVER a live
    # exact hit -- universe='live' leaves it unresolved, even as an exact
    # full UUID (it answers only in the archive phase).
    hit = resolve_uuid_like_node_handle(ARCHIVE_ONLY, nodes, universe="live")
    assert not hit["success"] and hit.get("candidates") is None
    # ... and the exact archived UUID whose head is SHARED with a live node
    # does not answer live either: the kernel's shave of it is discarded
    # (E2, asked in every mode) -- no target, no correction echo.
    hit = resolve_uuid_like_node_handle(ARCHIVED_UUID, nodes, universe="live")
    assert not hit["success"]
    assert drain_handle_corrections() == []
    # a live exact and a live clean prefix still answer under universe='live'
    hit = resolve_uuid_like_node_handle(LIVE_UUID, nodes, universe="live")
    assert hit["success"] and hit["resolved_uuid"] == LIVE_UUID
    hit = resolve_uuid_like_node_handle("aaaaaaaa", nodes, universe="live")
    assert hit["success"] and hit["resolved_uuid"] == LIVE_UUID
    # universe='live': a PREFIX that only the archive can answer is invisible
    prefix = ARCHIVE_ONLY[:16]  # no exact hit; only the archive record matches
    assert not resolve_uuid_like_node_handle(prefix, nodes, universe="live")[
        "success"
    ]
    # universe='archive': the archive phase sees the archive partition only
    # -- a live-only handle is invisible (and the shared-head archived
    # record answers the live head's prefix there, unique among the
    # visible records)
    assert not resolve_uuid_like_node_handle("eeeeeeee", nodes, universe="archive")[
        "success"
    ]
    hit = resolve_uuid_like_node_handle(LIVE_UUID[:16], nodes, universe="archive")
    assert hit["success"] and hit["resolved_uuid"] == ARCHIVED_UUID
    # universe='archive': the exact archived id answers there
    hit = resolve_uuid_like_node_handle(ARCHIVE_ONLY, nodes, universe="archive")
    assert hit["success"] and hit["resolved_uuid"] == ARCHIVE_ONLY
    # auto (default) resolves the archived handle — exact and prefix
    assert resolve_uuid_like_node_handle(ARCHIVE_ONLY, nodes)["success"]
    hit = resolve_uuid_like_node_handle(prefix, nodes)
    assert hit["success"] and hit["resolved_uuid"] == ARCHIVE_ONLY
    # auto: the shared-head archived exact answers AFTER the discarded shave
    hit = resolve_uuid_like_node_handle(ARCHIVED_UUID, nodes)
    assert hit["success"] and hit["resolved_uuid"] == ARCHIVED_UUID
    assert drain_handle_corrections() == []


def test_record_archive_served_only_for_archive_answers(tmp_path):
    _, live_view, nodes = _augmented_by_id(
        tmp_path,
        [_rec(LIVE_UUID, name="live")],
        [_rec(ARCHIVED_UUID, name="archived")],
        ARCHIVED_UUID,
    )
    client = make_boundary_capable(
        object.__new__(WorkFlowyClientNexus), nodes=[_rec(LIVE_UUID, name="live")]
    )

    async def _run():
        async with cache_operation_boundary(client, "t"):
            context = ArchiveOperationContext(
                mode=ARCHIVE_MODE_NONE,
                reason=None,
                overlay=None,
                state=ArchiveOperationState(),
                operation="node.read",
            )
            with archive_operation_scope(context):
                resolve_uuid_like_node_handle(LIVE_UUID, nodes)
                assert context.state.served == []
                resolve_uuid_like_node_handle(ARCHIVED_UUID, nodes)
                served = context.state.served
                assert len(served) == 1
                assert served[0].uuid == ARCHIVED_UUID
                # a second answer for the same uuid is deduplicated
                resolve_uuid_like_node_handle(ARCHIVE_ONLY, nodes) if (
                    ARCHIVE_ONLY in nodes
                ) else None
                assert len(context.state.served) == 1

    asyncio.run(_run())


def test_live_first_in_plain_thread_without_context(tmp_path):
    _, live_view, nodes = _augmented_by_id(
        tmp_path,
        [_rec(LIVE_UUID, name="live")],
        [_rec(ARCHIVED_UUID, name="archived")],
        ARCHIVED_UUID,
    )
    # NO operation context here: the marker partition alone keeps it live-first
    def _resolve():
        hit = resolve_uuid_like_node_handle("aaaaaaaa", nodes)
        assert hit["success"] and hit["resolved_uuid"] == LIVE_UUID
        hit = resolve_uuid_like_node_handle(ARCHIVED_UUID, nodes)
        assert hit["success"] and hit["resolved_uuid"] == ARCHIVED_UUID
        return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(_resolve).result(timeout=10)


# ---------------------------------------------------------------------------
# The fallback gate (mode fallback, live-only map, injected store)
# ---------------------------------------------------------------------------


def _fallback_env(tmp_path, live_records, archive_records, archive_root):
    _write_archive(tmp_path, archive_records, archive_root)
    store = ArchiveStore(folder_provider=lambda: str(tmp_path))
    set_archive_store_for_tests(store)
    client = make_boundary_capable(
        object.__new__(WorkFlowyClientNexus), nodes=live_records
    )
    return store, client


def test_gate_uuid_hit_requests_rerun_and_returns_unresolved(tmp_path):
    _, client = _fallback_env(
        tmp_path,
        [_rec(OTHER_LIVE, name="unrelated live")],
        [_rec(ARCHIVED_UUID, name="archived")],
        ARCHIVED_UUID,
    )
    live_map = {r["id"]: r for r in [_rec(OTHER_LIVE, name="unrelated live")]}

    async def _run():
        async with cache_operation_boundary(client, "t"):
            context = ArchiveOperationContext(
                mode=ARCHIVE_MODE_FALLBACK,
                reason=None,
                overlay=None,
                state=ArchiveOperationState(),
                operation="node.read",
            )
            with archive_operation_scope(context):
                result = resolve_uuid_like_node_handle(ARCHIVED_UUID, live_map)
                assert not result["success"]
                assert context.state.rerun_requested is True

    asyncio.run(_run())


def test_gate_shaved_live_hit_discarded_exact_archived_wins(tmp_path):
    _, client = _fallback_env(
        tmp_path,
        [_rec(LIVE_UUID, name="live")],
        [_rec(ARCHIVED_UUID, name="archived")],
        ARCHIVED_UUID,
    )
    live_map = {LIVE_UUID: _rec(LIVE_UUID, name="live")}

    async def _run():
        async with cache_operation_boundary(client, "t"):
            context = ArchiveOperationContext(
                mode=ARCHIVE_MODE_FALLBACK,
                reason=None,
                overlay=None,
                state=ArchiveOperationState(),
                operation="node.read",
            )
            with archive_operation_scope(context):
                # the supplied string is the archived FULL uuid, whose head
                # uniquely matches the live node: a SHAVED live hit
                result = resolve_uuid_like_node_handle(ARCHIVED_UUID, live_map)
                assert not result["success"]
                assert context.state.rerun_requested is True
                assert drain_handle_corrections() == []

    asyncio.run(_run())


def test_exact_archive_beats_shave_in_mode_none_gate_stays_inert(tmp_path):
    """F2 (Chain-1 W2): in mode none the exact archived UUID is never
    corrected to the shaved live node.

    The old test locked in the UNSAFE outcome (the shaved live hit standing
    in a write context).  Rev 2 §4.4.3: the side-effect-free exact-archive
    test is asked in EVERY mode, so the shave is discarded -- unresolved, no
    correction echo -- and the site's archive-aware miss then refuses.  The
    fallback GATE stays inert (it never fires outside fallback mode and
    never from a live phase): no re-run is requested here.
    """
    _, client = _fallback_env(
        tmp_path,
        [_rec(LIVE_UUID, name="live")],
        [_rec(ARCHIVED_UUID, name="archived")],
        ARCHIVED_UUID,
    )
    live_map = {LIVE_UUID: _rec(LIVE_UUID, name="live")}

    async def _run():
        async with cache_operation_boundary(client, "t"):
            context = ArchiveOperationContext(
                mode=ARCHIVE_MODE_NONE,
                reason="write",
                overlay=None,
                state=ArchiveOperationState(),
                operation="node.delete",
            )
            with archive_operation_scope(context):
                result = resolve_uuid_like_node_handle(ARCHIVED_UUID, live_map)
                # the shave of the exact archived id is DISCARDED: the live
                # leg is unresolved, so the write site's archive-aware miss
                # (not the resolver) produces the refusal.
                assert not result["success"]
                assert result.get("candidates") is None
                # no correction echo for the discarded shave (E2)
                assert drain_handle_corrections() == []
                # the gate stays inert in mode none: no re-run request, and
                # the refusal flows through the site's archive-aware miss
                assert context.state.rerun_requested is False

            # the discarded shave is not a blanket rule: an altered tail that
            # is NOT an exact archived id still shaves to the live node (the
            # pre-existing correction behavior, W2.5).
            async with cache_operation_boundary(client, "t"):
                other = ARCHIVED_UUID[:-1] + "e"
                result = resolve_uuid_like_node_handle(other, live_map)
                assert result["success"] and result["resolved_uuid"] == LIVE_UUID
                corrections = drain_handle_corrections()
                assert len(corrections) == 1
                assert corrections[0]["resolved_uuid"] == LIVE_UUID

            # a clean 8-hex live prefix still identifies the live node (W2.5)
            async with cache_operation_boundary(client, "t"):
                result = resolve_uuid_like_node_handle("aaaaaaaa", live_map)
                assert result["success"] and result["resolved_uuid"] == LIVE_UUID
                assert drain_handle_corrections() == []

    asyncio.run(_run())


def test_gate_store_miss_does_nothing(tmp_path):
    _, client = _fallback_env(
        tmp_path,
        [_rec(OTHER_LIVE, name="live")],
        [_rec(ARCHIVED_UUID, name="archived")],
        ARCHIVED_UUID,
    )
    live_map = {OTHER_LIVE: _rec(OTHER_LIVE, name="live")}
    missing = str(uuid_module.uuid4())

    async def _run():
        async with cache_operation_boundary(client, "t"):
            context = ArchiveOperationContext(
                mode=ARCHIVE_MODE_FALLBACK,
                reason=None,
                overlay=None,
                state=ArchiveOperationState(),
                operation="node.read",
            )
            with archive_operation_scope(context):
                result = resolve_uuid_like_node_handle(missing, live_map)
                assert not result["success"]
                assert context.state.rerun_requested is False

    asyncio.run(_run())


def test_gate_ap_hit_requests_rerun(tmp_path):
    _, client = _fallback_env(
        tmp_path,
        [_rec(OTHER_LIVE, name="unrelated")],
        [_rec(ARCHIVED_UUID, name="[ap:GATEU1] archived")],
        ARCHIVED_UUID,
    )
    live_map = {OTHER_LIVE: _rec(OTHER_LIVE, name="unrelated")}

    async def _run():
        async with cache_operation_boundary(client, "t"):
            context = ArchiveOperationContext(
                mode=ARCHIVE_MODE_FALLBACK,
                reason=None,
                overlay=None,
                state=ArchiveOperationState(),
                operation="node.read",
            )
            with archive_operation_scope(context):
                result = resolve_uuid_from_anchor_point("[ap:GATEU1]", live_map)
                assert result["match_count"] == 0
                assert context.state.rerun_requested is True

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Layer 3 (symbol runner stubbed)
# ---------------------------------------------------------------------------


def _layer3_env(tmp_path):
    # an archived record whose 8-hex head is shared with NO live node, so a
    # short prefix can only ever be answered by the archive phase
    _, _, nodes = _augmented_by_id(
        tmp_path,
        [_rec(LIVE_UUID, name="live")],
        [_rec(ARCHIVE_ONLY, name="archived")],
        ARCHIVE_ONLY,
    )
    return nodes


def _symbol_runner(returning):
    async def _run(candidate):
        if returning is None:
            # a real runner reports a miss as a structured 0-match record
            return {"success": True, "match_count": 0, "candidates": []}
        return {
            "success": True,
            "match_count": 1,
            "candidates": [{"uuid": returning, "name": "via symbol"}],
        }

    return _run


def test_layer3_live_symbol_beats_archived_uuid(tmp_path):
    nodes = _layer3_env(tmp_path)
    client = object.__new__(WorkFlowyClientNexus)
    # a PREFIX that only the archive could answer, but the live symbol leg
    # resolves it first: the symbol leg runs BEFORE the archive phase
    archived_prefix = ARCHIVE_ONLY[:16]
    result = asyncio.run(
        client.resolve_node_handle(
            archived_prefix,
            nodes,
            allow_uuid=True,
            allow_anchor_point=True,
            allow_symbol=True,
            prefer="uuid",
            symbol_runner=_symbol_runner(LIVE_UUID),
        )
    )
    assert result["success"] and result["resolved_via"] == "symbol"
    assert result["resolved_uuid"] == LIVE_UUID

    # F3 (Chain-1 W2): the FULL EXACT archived UUID whose head is SHARED
    # with a live node -- the live UUID leg's shave of it is discarded, so
    # the real live symbol domain still runs and wins; the archive phase
    # never preempts it.
    shared_env = _augmented_by_id(
        tmp_path,
        [_rec(LIVE_UUID, name="live"), _rec(OTHER_LIVE, name="S")],
        [_rec(ARCHIVED_UUID, name="archived shared head")],
        ARCHIVED_UUID,
    )
    shared_nodes = shared_env[2]
    result = asyncio.run(
        client.resolve_node_handle(
            ARCHIVED_UUID,
            shared_nodes,
            allow_uuid=True,
            allow_anchor_point=True,
            allow_symbol=True,
            prefer="uuid",
            symbol_runner=_symbol_runner(OTHER_LIVE),
        )
    )
    assert result["success"] and result["resolved_via"] == "symbol"
    assert result["resolved_uuid"] == OTHER_LIVE
    # ... and when the symbol leg also misses, the archive phase serves the
    # exact archived id (the normal symbol-miss case, W2.5)
    result = asyncio.run(
        client.resolve_node_handle(
            ARCHIVED_UUID,
            shared_nodes,
            allow_uuid=True,
            allow_anchor_point=True,
            allow_symbol=True,
            prefer="uuid",
            symbol_runner=_symbol_runner(None),
        )
    )
    assert result["success"] and result["resolved_via"] == "uuid"
    assert result["resolved_uuid"] == ARCHIVED_UUID


def test_layer3_archive_uuid_after_symbol_miss(tmp_path):
    nodes = _layer3_env(tmp_path)
    client = object.__new__(WorkFlowyClientNexus)
    archived_prefix = ARCHIVE_ONLY[:16]
    result = asyncio.run(
        client.resolve_node_handle(
            archived_prefix,
            nodes,
            allow_uuid=True,
            allow_anchor_point=True,
            allow_symbol=True,
            prefer="uuid",
            symbol_runner=_symbol_runner(None),
        )
    )
    assert result["success"] and result["resolved_via"] == "uuid"
    assert result["resolved_uuid"] == ARCHIVE_ONLY


def test_layer3_live_exact_untouched(tmp_path):
    nodes = _layer3_env(tmp_path)
    client = object.__new__(WorkFlowyClientNexus)
    result = asyncio.run(
        client.resolve_node_handle(
            LIVE_UUID,
            nodes,
            allow_uuid=True,
            allow_anchor_point=True,
            allow_symbol=True,
            prefer="uuid",
            symbol_runner=_symbol_runner(ARCHIVED_UUID),
        )
    )
    assert result["success"] and result["resolved_via"] == "uuid"
    assert result["resolved_uuid"] == LIVE_UUID
