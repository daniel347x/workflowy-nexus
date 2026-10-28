"""The three write guards (Archive Reads U1, §U1.5 test 6).

Inside an augmented scope each guarded function raises
``ArchiveReadOnlyViolation`` BEFORE its transport/transform/claim; inside a
context without an overlay and outside any context, behaviour is exactly
today's.
"""

from __future__ import annotations

import asyncio

import pytest

from coherence_harness import make_boundary_capable

from workflowy_mcp.archive_context import (
    ARCHIVE_MODE_NONE,
    ArchiveOperationContext,
    ArchiveReadOnlyViolation,
)
from workflowy_mcp.archive_store import ArchiveStore, set_archive_store_for_tests
from workflowy_mcp.client.api_client_core import WorkFlowyClientCore
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
from workflowy_mcp.coherence.cache_view import CacheView, CacheViewPublisher
from workflowy_mcp.coherence.operation_cache import (
    archive_operation_scope,
    cache_operation_boundary,
    publish_local_cache_view,
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


def _archive_overlay(tmp_path, live_records):
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
    store = ArchiveStore(folder_provider=lambda: str(tmp_path))
    set_archive_store_for_tests(store)
    live_view = CacheView.build(
        epoch_number=7,
        epoch_id="live-epoch",
        adopted_sequence=11,
        nodes_export={"nodes": [dict(r) for r in live_records]},
    )
    overlay = store.overlay_for(live_view)
    assert overlay is not None
    return overlay


@pytest.fixture(autouse=True)
def _restore_store():
    yield
    set_archive_store_for_tests(None)


def _run_augmented(overlay, live_records, afn):
    """Await ``afn`` inside an augmented operation scope."""

    async def _run():
        client = make_boundary_capable(
            object.__new__(WorkFlowyClientNexus), nodes=live_records
        )
        async with cache_operation_boundary(client, "t"):
            context = ArchiveOperationContext(
                mode="include",
                reason="flag",
                overlay=overlay,
                operation="node.read",
            )
            with archive_operation_scope(context, view=overlay.augmented_view):
                return await afn()

    return asyncio.run(_run())


def _run_plain(live_records, afn):
    """Await ``afn`` inside a NON-augmented boundary (mode none, no overlay)."""

    async def _run():
        client = make_boundary_capable(
            object.__new__(WorkFlowyClientNexus), nodes=live_records
        )
        async with cache_operation_boundary(client, "t"):
            context = ArchiveOperationContext(
                mode=ARCHIVE_MODE_NONE,
                reason="write",
                overlay=None,
                operation="node.edit",
            )
            with archive_operation_scope(context):
                return await afn()

    return asyncio.run(_run())


def test_guard_1_send_workflowy_write(tmp_path):
    overlay = _archive_overlay(tmp_path, [_rec("L1", name="live")])
    live_records = [_rec("L1", name="live")]
    core = object.__new__(WorkFlowyClientCore)

    async def _inside():
        await core._send_workflowy_write("POST", "https://x/", operation="update_node")
        raise AssertionError("the transport was reached inside an augmented scope")

    with pytest.raises(ArchiveReadOnlyViolation):
        _run_augmented(overlay, live_records, _inside)

    async def _outside():
        # outside an augmented scope the guard is inert: the call proceeds
        # (and fails on the unconfigured governor — NEVER with the archive
        # violation)
        try:
            await core._send_workflowy_write(
                "POST", "https://x/", operation="update_node"
            )
        except ArchiveReadOnlyViolation:
            raise AssertionError("guard fired outside an augmented operation")
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__
        return "sent"

    assert _run_plain(live_records, _outside) not in ("sent", None)


def test_guard_2_publish_local_cache_view(tmp_path):
    overlay = _archive_overlay(tmp_path, [_rec("L1", name="live")])
    live_records = [_rec("L1", name="live")]
    view = CacheView.build(
        epoch_number=7,
        epoch_id="live-epoch",
        adopted_sequence=11,
        nodes_export={"nodes": [dict(r) for r in live_records]},
    )
    publisher = CacheViewPublisher(view)

    def _transform(working):
        working["nodes"] = [dict(n, name="renamed") for n in working["nodes"]]
        return None

    async def _inside():
        publish_local_cache_view(publisher, _transform, reason="test")
        raise AssertionError("the transform ran inside an augmented scope")

    with pytest.raises(ArchiveReadOnlyViolation):
        _run_augmented(overlay, live_records, _inside)

    async def _outside():
        return publish_local_cache_view(publisher, _transform, reason="test")

    published = _run_plain(live_records, _outside)
    assert published.nodes_export["nodes"][0]["name"] == "renamed"


def test_guard_3_patch_nodes_export_snapshot_on_disk(tmp_path):
    overlay = _archive_overlay(tmp_path, [_rec("L1", name="live")])
    live_records = [_rec("L1", name="live")]
    core = object.__new__(WorkFlowyClientCore)

    async def _inside():
        await core.patch_nodes_export_snapshot_on_disk([])
        raise AssertionError("the publication ran inside an augmented scope")

    with pytest.raises(ArchiveReadOnlyViolation):
        _run_augmented(overlay, live_records, _inside)

    async def _outside():
        # the guard is a no-op: the empty-patches early return stands
        return await core.patch_nodes_export_snapshot_on_disk([])

    result = _run_plain(live_records, _outside)
    assert result == {"success": True, "patched": 0, "note": "no patches"}


def test_guards_inert_without_any_context():
    # outside any boundary the guard reads no snapshot at all: the
    # empty-patch path returns its ack exactly as today
    core = object.__new__(WorkFlowyClientCore)
    assert asyncio.run(core.patch_nodes_export_snapshot_on_disk([])) == {
        "success": True,
        "patched": 0,
        "note": "no patches",
    }
