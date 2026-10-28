"""Chain-1 W3 (F4): ``system.archives.mount`` adopts the correct live parent.

A unique live AP-ID match always wins over an archived duplicate of the
same core (Axiom 1/G2); a UUID clean-prefix and exact live matches retain
precedence; only an ACTUAL corrective UUID shave is eligible for the
exact-archive exception.  A shave is told from the resolution outcome,
never from string inequality — an AP-ID input never compares equal to its
resolved UUID, so string inequality would misclassify every live AP match.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from coherence_harness import make_boundary_capable

from workflowy_mcp import server
from workflowy_mcp.archive_store import ArchiveStore, set_archive_store_for_tests
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.mcp_api_registry import dispatch_operation
from workflowy_mcp.node_archive import parse_archive, serialize_archive, build_archive_document

SOURCE_CACHE = {"epoch_number": 1, "epoch_id": "e1", "adopted_sequence": 3}

# The duplicate-core pair: the live carrier and the archived record share
# the [ap:DUPA1] core (an E28 state), and their UUID heads coincide so a
# shave of the archived full UUID onto the live node is reachable.
LIVE_CARRIER = "aaaaaaaa-0000-0000-0000-000000000001"
ARCHIVED_TWIN = "aaaaaaaa-0000-0000-0000-0000000000ff"
ORPHAN_ROOT = "eeeeeeee-0000-0000-0000-00000000b001"


def _nest(records):
    return WorkFlowyClientEtch.__new__(WorkFlowyClientEtch)._build_hierarchy(
        list(records)
    )


def _rec(node_id, name="n", parent_id=None, priority=0, note="", **extra):
    record = {
        "id": node_id,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
    }
    record.update(extra)
    return record


def _bytes_sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def mount_ap_env(tmp_path, monkeypatch):
    # A: active, its root carries the duplicated AP core and shares the live
    # carrier's head.  B: orphaned, the mount TARGET for the parent cases.
    for records, node_id, name in (
        (
            [_rec(ARCHIVED_TWIN, name="[ap:DUPA1] archived twin")],
            ARCHIVED_TWIN,
            "arch-twin.json",
        ),
        (
            [_rec(ORPHAN_ROOT, name="Orphan root", parent_id="99999999-0000-0000-0000-000000009999")],
            ORPHAN_ROOT,
            "arch-orphan.json",
        ),
    ):
        document = build_archive_document(
            records,
            nest=_nest,
            node_id=node_id,
            name=name,
            description=None,
            exported_at="2026-10-01T12:00:00Z",
            source_cache=dict(SOURCE_CACHE),
        )
        (tmp_path / name).write_bytes(serialize_archive(document).encode("utf-8"))
    store = ArchiveStore(folder_provider=lambda: str(tmp_path))
    set_archive_store_for_tests(store)
    client = object.__new__(server.WorkFlowyClient)
    make_boundary_capable(
        client,
        nodes=[_rec(LIVE_CARRIER, name="[ap:DUPA1] live carrier")],
    )
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    from coherence_harness import make_coherence_root

    monkeypatch.setattr(
        server, "_admission_coherence_paths", lambda: make_coherence_root(tmp_path)
    )

    async def _no_ws(max_wait_seconds=0.0):
        return None, None

    monkeypatch.setattr(server, "get_ws_connection_with_grace_wait", _no_ws)
    yield {"store": store, "tmp_path": tmp_path, "client": client}
    set_archive_store_for_tests(None)


def _mount(env, archive, parent_handle):
    async def _body():
        return await dispatch_operation(
            server.SYSTEM_REGISTRY,
            "archives.mount",
            {"archive": archive, "parent_handle": parent_handle},
            error_logger=lambda _m: None,
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
            operation_invoker=server._mcp_archive_operation_invoker,
        )

    async def _with_boundary():
        from workflowy_mcp.coherence.operation_cache import cache_operation_boundary

        async with cache_operation_boundary(env["client"], "t"):
            return await _body()

    return asyncio.run(_with_boundary())


def test_mount_live_ap_beats_archived_duplicate(mount_ap_env):
    """The audit's F5... (F4) reproduction: one live node and one active
    archived record carry [ap:DUPA1]; mounting a DIFFERENT archive under
    that AP stores the LIVE UUID, never the archived duplicate."""
    result = _mount(mount_ap_env, "arch-orphan.json", "[ap:DUPA1]")
    assert result.get("success") is True, result
    assert result["verified"] is True
    assert result["mount_parent_id"] == LIVE_CARRIER


def test_mount_containing_archive_under_live_ap_succeeds(mount_ap_env):
    """Mounting the archive that CONTAINS the duplicated record under the
    live AP succeeds (the parent is the live node — not a false
    'parent is inside the archive itself' refusal)."""
    result = _mount(mount_ap_env, "arch-twin.json", "[ap:DUPA1]")
    assert result.get("success") is True, result
    assert result["mount_parent_id"] == LIVE_CARRIER


def test_mount_clean_live_prefix_wins(mount_ap_env):
    """A live clean-prefix UUID match retains precedence (not a shave)."""
    result = _mount(mount_ap_env, "arch-orphan.json", LIVE_CARRIER[:8])
    assert result.get("success") is True, result
    assert result["mount_parent_id"] == LIVE_CARRIER


def test_mount_exact_archived_full_uuid_still_wins_the_shave(mount_ap_env):
    """Only an ACTUAL corrective UUID shave is eligible for the
    exact-archive exception: A's exact full UUID shaves onto the live
    carrier, the exact archived id displaces the shave, and the archive is
    planted under the archived record."""
    result = _mount(mount_ap_env, "arch-orphan.json", ARCHIVED_TWIN)
    assert result.get("success") is True, result
    assert result["mount_parent_id"] == ARCHIVED_TWIN


def test_mount_refusal_writes_nothing_and_set_clears_round_trip(mount_ap_env):
    """Bytes are unchanged on a real refusal; a successful set changes only
    mount_parent_id (nodes/hash preserved) and verifies on read-back."""
    path = mount_ap_env["tmp_path"] / "arch-orphan.json"
    before = _bytes_sha(path)
    refused = _mount(mount_ap_env, "arch-orphan.json", "99999999-0000-0000-0000-000000009999")
    assert refused.get("success") is False
    assert _bytes_sha(path) == before

    set_result = _mount(mount_ap_env, "arch-orphan.json", LIVE_CARRIER)
    assert set_result.get("success") is True and set_result["verified"] is True
    document = parse_archive(path.read_bytes(), nest=_nest)
    assert document["archive"]["mount_parent_id"] == LIVE_CARRIER

    clear_result = _mount(mount_ap_env, "arch-orphan.json", None)
    assert clear_result.get("success") is True
    assert clear_result["mount_parent_id"] is None
    assert clear_result["previous_mount_parent_id"] == LIVE_CARRIER
    document = parse_archive(path.read_bytes(), nest=_nest)
    assert "mount_parent_id" not in document["archive"]
