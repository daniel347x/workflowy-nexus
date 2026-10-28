"""The archive inventory and mount verbs (Archive Reads U3, §U3.5 test 2).

Every file state and reason in ``system.archives.list`` (compact and
full); the no-tab operation (the zero-wait pull silently falls back to the
remembered value); ``system.archives.mount`` set / clear / archived parent
/ every §5.11 refusal (nothing written) / the verified write (hash
unchanged, parse-back accepts) / the UNVERIFIED path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from coherence_harness import make_boundary_capable

from workflowy_mcp import server
from workflowy_mcp.archive_store import (
    ArchiveStore,
    get_archive_store,
    set_archive_store_for_tests,
)
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.mcp_api_registry import dispatch_operation
from workflowy_mcp.node_archive import (
    build_archive_document,
    parse_archive,
    serialize_archive,
)

SOURCE_CACHE = {"epoch_number": 1, "epoch_id": "e1", "adopted_sequence": 3}

LIVE_PARENT = "11111111-0000-0000-0000-000000000001"
LIVE_OTHER = "22222222-0000-0000-0000-000000000002"
ARCH_A_ROOT = "eeeeeeee-0000-0000-0000-00000000a001"
ARCH_A_CHILD = "eeeeeeee-0000-0000-0000-00000000a002"
ARCH_A_GRAND = "eeeeeeee-0000-0000-0000-00000000a003"
ARCH_B_ROOT = "eeeeeeee-0000-0000-0000-00000000b001"
ARCH_DORMANT_ROOT = "eeeeeeee-0000-0000-0000-00000000d001"
NOWHERE = "99999999-0000-0000-0000-000000009999"


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


def _write(tmp_path, records, node_id, name, *, vault=False):
    document = build_archive_document(
        list(records),
        nest=_nest,
        node_id=None if vault else node_id,
        name=name,
        description=None,
        exported_at="2026-10-01T12:00:00Z",
        source_cache=dict(SOURCE_CACHE),
    )
    (tmp_path / name).write_bytes(serialize_archive(document).encode("utf-8"))
    return tmp_path / name


def _bytes_sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def inv_env(tmp_path, monkeypatch):
    """The folder with one archive per state, the live view, and the
    stubbed (no-tab) settings pull."""
    # active: A (root + child + grandchild), planted under LIVE_PARENT;
    # its child's subtree is the mount-cycle vehicle.
    _write(
        tmp_path,
        [
            _rec(ARCH_A_ROOT, name="Archive A root", parent_id=LIVE_PARENT),
            _rec(ARCH_A_CHILD, name="A child", parent_id=ARCH_A_ROOT),
            _rec(ARCH_A_GRAND, name="A grandchild", parent_id=ARCH_A_CHILD),
        ],
        ARCH_A_ROOT,
        "arch-active.json",
    )
    # orphaned: root's parent is nowhere.
    _write(
        tmp_path,
        [_rec(ARCH_B_ROOT, name="Archive B root", parent_id=NOWHERE)],
        ARCH_B_ROOT,
        "arch-orphan.json",
    )
    # dormant: its root still lives in Workflowy.
    _write(
        tmp_path,
        [_rec(ARCH_DORMANT_ROOT, name="Dormant root", parent_id=LIVE_OTHER)],
        ARCH_DORMANT_ROOT,
        "arch-dormant.json",
    )
    # skipped: a whole-vault export (node_id=None).
    _write(
        tmp_path,
        [_rec("cccccccc-0000-0000-0000-00000000c001", name="vault node")],
        None,
        "arch-vault.json",
        vault=True,
    )
    # unreadable: not an archive at all.
    (tmp_path / "arch-broken.json").write_text("{not json", encoding="utf-8")

    store = ArchiveStore(folder_provider=lambda: str(tmp_path))
    set_archive_store_for_tests(store)
    client = object.__new__(server.WorkFlowyClient)
    make_boundary_capable(
        client,
        nodes=[
            _rec(LIVE_PARENT, name="Live parent"),
            _rec(LIVE_OTHER, name="Live other", parent_id=LIVE_PARENT),
            _rec(ARCH_DORMANT_ROOT, name="Dormant root", parent_id=LIVE_OTHER),
        ],
    )
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    # The mount verb is refuse-class (disk mutation, cache-derived parent):
    # point the admission gate at a scratch coherence root (the touch
    # tests' pattern) so a CLEAN boundary proceeds.
    from coherence_harness import make_coherence_root

    monkeypatch.setattr(
        server, "_admission_coherence_paths", lambda: make_coherence_root(tmp_path)
    )

    async def _no_ws(max_wait_seconds=0.0):
        return None, None

    monkeypatch.setattr(
        server, "get_ws_connection_with_grace_wait", _no_ws
    )
    yield {"store": store, "tmp_path": tmp_path, "client": client}
    set_archive_store_for_tests(None)


def _run_system(inv_env, verb, params):
    async def _body():
        return await dispatch_operation(
            server.SYSTEM_REGISTRY,
            verb,
            params,
            error_logger=lambda _m: None,
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
            operation_invoker=server._mcp_archive_operation_invoker,
        )

    async def _with_boundary():
        from workflowy_mcp.coherence.operation_cache import cache_operation_boundary

        async with cache_operation_boundary(inv_env["client"], "t"):
            return await _body()

    return asyncio.run(_with_boundary())


# ---------------------------------------------------------------------------
# system.archives.list
# ---------------------------------------------------------------------------


def test_list_compact_names_every_state(inv_env):
    text = _run_system(inv_env, "archives.list", {})
    assert isinstance(text, str)
    assert text.startswith("📦 Archive folder:")
    assert "· learned never ·" in text
    for token in (
        "arch-active.json · active",
        "arch-orphan.json · orphaned",
        "arch-dormant.json · dormant",
        "arch-vault.json · skipped",
        "arch-broken.json · unreadable",
    ):
        assert token in text, token
    # the header counts: 5 files, 1 active, 1 orphaned, 1 dormant, 1
    # skipped, 1 unreadable
    assert "5 file(s): 1 active · 1 orphaned · 1 dormant · 0 superseded · 1 skipped · 1 unreadable" in text


def test_list_compact_row_fields(inv_env):
    text = _run_system(inv_env, "archives.list", {})
    # root: 8-hex + AP-ID when the root name carries one (A carries none —
    # 8-hex only); planted_under: 8-hex + the live parent's name.
    assert f"root {ARCH_A_ROOT[:8]}" in text
    assert f"planted under {LIVE_PARENT[:8]} Live parent" in text
    assert "planted under top level" in text  # the orphan row
    assert "3 node(s), 3 visible" in text  # the active archive's counts


def test_list_full_shape(inv_env):
    result = _run_system(inv_env, "archives.list", {"detail": "full"})
    assert result.get("success") is True
    assert result["learned_at"] is None
    assert isinstance(result["files"], list) and len(result["files"]) == 5
    states = sorted(row["state"] for row in result["files"])
    assert states == ["active", "dormant", "orphaned", "skipped", "unreadable"]
    row = next(r for r in result["files"] if r["file"] == "arch-active.json")
    for field in (
        "file", "state", "reason", "archive_name", "exported_at", "scope",
        "root", "node_count", "visible_count", "planted_under",
        "source_parent_id", "mount_parent_id",
    ):
        assert field in row, field
    assert row["scope"] == "subtree"
    assert row["planted_under"].startswith(LIVE_PARENT[:8])
    assert row["mount_parent_id"] is None


def test_list_works_with_no_tab(inv_env):
    """The settings pull is zero-wait and best-effort: no tab connected →
    the remembered value, no error, no wait."""
    text = _run_system(inv_env, "archives.list", {})
    assert "(none configured)" in text


def test_list_rejects_a_bad_detail(inv_env):
    result = _run_system(inv_env, "archives.list", {"detail": "loud"})
    assert result.get("success") is False
    assert result["error"]["code"] == "invalid_enum_value"


def test_list_summary_is_the_registry_summary():
    """Manager-Session-147: the always-on index line is the short summary;
    describe shows it followed by the canonical rules."""
    spec = server.SYSTEM_REGISTRY.resolve("archives.list")
    assert spec.summary.startswith("List the export archives")
    assert spec.describe_suffix.lstrip().startswith("Export archives, the rules:")
    document = server.SYSTEM_REGISTRY.describe_full("archives.list")
    assert "Export archives, the rules:" in document["summary"]


# ---------------------------------------------------------------------------
# system.archives.mount
# ---------------------------------------------------------------------------


def _mount(inv_env, archive, parent_handle):
    return _run_system(
        inv_env, "archives.mount", {"archive": archive, "parent_handle": parent_handle}
    )


def test_mount_set_and_round_trip(inv_env):
    path = inv_env["tmp_path"] / "arch-orphan.json"
    before = _bytes_sha(path)
    document_before = parse_archive(path.read_bytes(), nest=_nest)
    result = _mount(inv_env, "arch-orphan.json", LIVE_PARENT)
    assert result.get("success") is True, result
    assert result["verified"] is True
    assert result["previous_mount_parent_id"] is None
    assert result["mount_parent_id"] == LIVE_PARENT
    # nodes and their SHA-256 are byte-identical; the file re-parses.
    document_after = parse_archive(path.read_bytes(), nest=_nest)
    assert document_after["nodes"] == document_before["nodes"]
    assert (
        document_after["archive"]["nodes_sha256"]
        == document_before["archive"]["nodes_sha256"]
    )
    assert document_after["archive"]["mount_parent_id"] == LIVE_PARENT
    # the next operation plants the root under the new parent (the store
    # re-parses on the changed mtime): the inventory row says so.
    text = _run_system(inv_env, "archives.list", {})
    assert f"planted under {LIVE_PARENT[:8]} Live parent" in text


def test_mount_clear(inv_env):
    assert _mount(inv_env, "arch-orphan.json", LIVE_PARENT).get("success") is True
    result = _mount(inv_env, "arch-orphan.json", None)
    assert result.get("success") is True
    assert result["mount_parent_id"] is None
    assert result["previous_mount_parent_id"] == LIVE_PARENT
    path = inv_env["tmp_path"] / "arch-orphan.json"
    document = parse_archive(path.read_bytes(), nest=_nest)
    assert "mount_parent_id" not in document["archive"]


def test_mount_under_an_archived_parent(inv_env):
    """The parent resolves through the store's active overlay: an archived
    parent is allowed."""
    result = _mount(inv_env, "arch-orphan.json", ARCH_A_CHILD)
    assert result.get("success") is True, result
    assert result["mount_parent_id"] == ARCH_A_CHILD


def test_mount_no_such_archive(inv_env):
    result = _mount(inv_env, "nope.json", LIVE_PARENT)
    assert result.get("success") is False
    assert result["error"]["code"] == "invalid_parameter_value"
    assert "no archive file named nope.json" in result["error"]["message"]
    assert "nothing was written" not in result["error"]["message"]


def test_mount_vault_refusal(inv_env):
    result = _mount(inv_env, "arch-vault.json", LIVE_PARENT)
    assert result.get("success") is False
    assert "whole-vault export" in result["error"]["message"]


def test_mount_unreadable_refusal(inv_env):
    result = _mount(inv_env, "arch-broken.json", LIVE_PARENT)
    assert result.get("success") is False
    assert "cannot be read" in result["error"]["message"]


def test_mount_parent_nowhere(inv_env):
    result = _mount(inv_env, "arch-orphan.json", NOWHERE)
    assert result.get("success") is False
    assert "neither in Workflowy nor in an active archive" in result["error"]["message"]


def test_mount_parent_inside_itself(inv_env):
    result = _mount(inv_env, "arch-active.json", ARCH_A_CHILD)
    assert result.get("success") is False
    assert f"is inside arch-active.json itself" in result["error"]["message"]


def test_mount_cycle_refusal_writes_nothing(inv_env):
    """Planting A under a record of B whose chain runs back into A closes a
    cycle; the refusal names the other archive and writes NOTHING."""
    path = inv_env["tmp_path"] / "arch-active.json"
    before = _bytes_sha(path)
    # Step 1 is legitimate: B's root under A's grandchild (B's chain runs
    # A grandchild -> A child -> A root -> LIVE_PARENT; no return to B).
    first = _mount(inv_env, "arch-orphan.json", ARCH_A_GRAND)
    assert first.get("success") is True, first
    # Step 2 closes the cycle: A's root under B's root — B's root is
    # planted at A's grandchild, so A would become its own ancestor.
    result = _mount(inv_env, "arch-active.json", ARCH_B_ROOT)
    assert result.get("success") is False
    assert "would close a cycle" in result["error"]["message"]
    assert "arch-orphan.json" in result["error"]["message"]
    assert _bytes_sha(path) == before


def test_mount_unverified_path(inv_env, monkeypatch):
    """A read-back mismatch reports UNVERIFIED (code operation_failed) and
    the file is left in place."""
    outcome: dict = {}

    def _fake_write(self, archive, parent_id):
        outcome["called"] = True
        return {
            "path": str(inv_env["tmp_path"] / archive),
            "mount_parent_id": parent_id,
            "previous_mount_parent_id": None,
            "verified": False,
        }

    monkeypatch.setattr(
        type(inv_env["store"]), "write_mount_parent", _fake_write
    )
    result = _mount(inv_env, "arch-orphan.json", LIVE_PARENT)
    assert outcome["called"] is True
    assert result.get("success") is False
    assert result["error"]["code"] == "operation_failed"
    assert "UNVERIFIED" in result["error"]["message"]


def test_mount_summary_is_the_registry_summary():
    spec = server.SYSTEM_REGISTRY.resolve("archives.mount")
    assert spec.summary.startswith("Set or clear an archive's mount point")
