"""Symbol isolation (Archive Reads U1, §U1.5 test 7).

The symbol legs read the LIVE list only: inside an augmented scope
``read_uuid_from_symbol`` never finds an archived mapped node, and the
per-generation symbol index slot keeps holding the live view's list
object.
"""

from __future__ import annotations

import asyncio

import pytest

from coherence_harness import make_boundary_capable

from workflowy_mcp.archive_context import ArchiveOperationContext
from workflowy_mcp.archive_store import ArchiveStore, set_archive_store_for_tests
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.client.api_client_nexus import (
    WorkFlowyClientNexus,
    _SYMBOL_INDEX_SLOT,
)
from workflowy_mcp.coherence.cache_view import CacheView
from workflowy_mcp.coherence.operation_cache import (
    archive_operation_scope,
    cache_operation_boundary,
)
from workflowy_mcp.node_archive import build_archive_document, serialize_archive

SOURCE_CACHE = {"epoch_number": 1, "epoch_id": "e1", "adopted_sequence": 3}

LIVE_UUID = "11111111-1111-1111-1111-111111111111"
ARCHIVE_SHARED = "22222222-2222-2222-2222-222222222222"
ARCHIVE_ONLY = "33333333-3333-3333-3333-333333333333"


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


def _live_records():
    return [
        _rec(
            LIVE_UUID,
            name="mapped_live #mapped-file",
            note="Path: /repo/thing.py\n---\nAST_QUALNAME: shared_symbol\n",
        ),
    ]


def _archive_records():
    return [
        _rec(
            ARCHIVE_SHARED,
            name="mapped_archived_shared",
            note="Path: /repo/archived.py\n---\nAST_QUALNAME: shared_symbol\n",
            parent_id=None,
        ),
        _rec(
            ARCHIVE_ONLY,
            name="mapped_archived_only",
            note="Path: /repo/archived.py\n---\nAST_QUALNAME: archive_only_symbol\n",
            parent_id=ARCHIVE_SHARED,
        ),
    ]


@pytest.fixture(autouse=True)
def _restore_store():
    yield
    set_archive_store_for_tests(None)


def _setup(tmp_path):
    document = build_archive_document(
        _archive_records(),
        nest=_nest,
        node_id=ARCHIVE_SHARED,
        name="archived mapped file",
        description=None,
        exported_at="2026-10-01T12:00:00Z",
        source_cache=dict(SOURCE_CACHE),
    )
    (tmp_path / "a.json").write_bytes(serialize_archive(document).encode("utf-8"))
    store = ArchiveStore(folder_provider=lambda: str(tmp_path))
    set_archive_store_for_tests(store)
    live_records = _live_records()
    live_view = CacheView.build(
        epoch_number=7,
        epoch_id="live-epoch",
        adopted_sequence=11,
        nodes_export={"nodes": [dict(r) for r in live_records]},
    )
    overlay = store.overlay_for(live_view)
    assert overlay is not None
    client = make_boundary_capable(
        object.__new__(WorkFlowyClientNexus), nodes=live_records
    )
    return client, overlay, live_view


async def _inside_augmented(client, overlay, probe):
    async with cache_operation_boundary(client, "t"):
        context = ArchiveOperationContext(
            mode="include",
            reason="flag",
            overlay=overlay,
            operation="lightning_rod.render_symbols",
        )
        with archive_operation_scope(context, view=overlay.augmented_view):
            return await probe()


async def _inside_plain(client, probe):
    async with cache_operation_boundary(client, "t"):
        return await probe()


def test_symbol_legs_never_see_overlay_records(tmp_path):
    client, overlay, _live_view = _setup(tmp_path)

    async def _probe_augmented():
        live_shared = await client.read_uuid_from_symbol(
            file_path=None, symbol="shared_symbol", symbol_kind="auto"
        )
        archive_only = await client.read_uuid_from_symbol(
            file_path=None, symbol="archive_only_symbol", symbol_kind="auto"
        )
        # read_uuid_from_symbol never raises on a miss: it reports match_count
        return live_shared, archive_only

    # inside an AUGMENTED scope: the shared symbol resolves to the LIVE node
    # only; the archive-only symbol has match_count 0
    live_result, archive_only = asyncio.run(
        _inside_augmented(client, overlay, _probe_augmented)
    )
    assert int(archive_only.get("match_count") or 0) == 0
    candidates = live_result.get("candidates") or []
    assert [c["uuid"] for c in candidates] == [LIVE_UUID]

    # on a plain (non-augmented) boundary: the same answers from today's list
    async def _probe_plain():
        return await client.read_uuid_from_symbol(
            file_path=None, symbol="shared_symbol", symbol_kind="auto"
        )

    live_result2 = asyncio.run(_inside_plain(client, _probe_plain))
    assert [c["uuid"] for c in live_result2.get("candidates") or []] == [LIVE_UUID]


def test_symbol_index_slot_holds_the_live_list(tmp_path):
    client, overlay, live_view = _setup(tmp_path)

    async def _probe():
        await client.read_uuid_from_symbol(
            file_path=None, symbol="shared_symbol", symbol_kind="auto"
        )
        assert _SYMBOL_INDEX_SLOT, "the index slot must be populated"
        index = _SYMBOL_INDEX_SLOT[0]
        # the slot's list IS the live view's list object — never the
        # augmented list, so no thrash and no archived symbols
        assert index.nodes_ref is live_view.nodes_export["nodes"]
        return True

    assert asyncio.run(_inside_augmented(client, overlay, _probe))
