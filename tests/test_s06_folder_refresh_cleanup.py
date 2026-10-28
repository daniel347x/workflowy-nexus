"""S06 Gate C — folder-refresh salvage retirement.

Proves refresh_folder_cartographer_sync performs no stash/reattach moves,
omits the retired result fields, deletes ONLY strict-classifier missing FILE
nodes (never diagnostic/prose/emoji nodes), and keeps the ordinary
refresh/create/delete contract intact.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from workflowy_mcp.client import api_client_nexus
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus


def _run_folder_sync(monkeypatch, tmp_path: Path, *, dry_run: bool):
    (tmp_path / "present.py").write_bytes(b"def present():\n    return 1\n")

    folder = {"id": "FOLDER", "name": "📂 folder", "note": f"Path: {tmp_path}"}
    f_present = {"id": "F1", "name": "🐍 present.py", "note": f"Path: {tmp_path / 'present.py'}", "parent_id": "FOLDER"}
    f_missing = {"id": "F2", "name": "🐍 missing.py", "note": f"Path: {tmp_path / 'missing.py'}", "parent_id": "FOLDER"}
    diag = {
        "id": "D1",
        "name": "🚨 Parse error: missing2.py",
        "note": f"File: {tmp_path / 'missing2.py'}\n---\nboom",
        "parent_id": "FOLDER",
    }
    prose = {
        "id": "D2",
        "name": "notes on paths",
        "note": f"some prose\nPath: {tmp_path / 'missing3.py'}\nmore prose",
        "parent_id": "FOLDER",
    }
    emoji_notes = {"id": "N1", "name": "📝 my notes", "note": "", "parent_id": "F1"}
    # A5 boundary: an EMOJI-prefixed node that otherwise FULLY QUALIFIES as a
    # missing FILE (first-line Path:, resolved abs candidate, basename in
    # name) — the retired emoji exemption must NOT save it.
    emoji_file = {
        "id": "F3",
        "name": "📝 missing3.py",
        "note": f"Path: {tmp_path / 'missing3.py'}",
        "parent_id": "FOLDER",
    }
    nodes = [folder, f_present, f_missing, diag, prose, emoji_notes, emoji_file]

    observed: dict = {"moves": [], "creates": [], "updates": [], "deletes": [], "refreshed": []}

    async def fake_export_nodes_impl(_client, node_id=None, **_kwargs):
        return {"nodes": [dict(n) for n in nodes]}

    def fake_resolve(*, node_id, nodes_by_id):
        table = {
            "FOLDER": str(tmp_path),
            "F1": str(tmp_path / "present.py"),
            "F2": str(tmp_path / "missing.py"),
            "F3": str(tmp_path / "missing3.py"),
        }
        return {"abs_path": table.get(str(node_id), ""), "root_abs": None}

    async def fake_refresh_file(_self, file_node_id, **kwargs):
        observed["refreshed"].append(str(file_node_id))
        return {"success": True, "refreshed_file_nodes": 1}

    async def fake_move(_self, node_id, parent, position):
        observed["moves"].append((node_id, parent, position))
        return True

    async def fake_create(_self, req, _internal_call=False, **_kw):
        created_id = f"NEW-{len(observed['creates']) + 1}"
        observed["creates"].append({"name": req.name, "parent_id": req.parent_id, "id": created_id})

        class _Created:
            id = created_id

        return _Created()

    async def fake_update(_self, node_id, req):
        observed["updates"].append(node_id)

    async def fake_delete(_self, node_id):
        observed["deletes"].append(node_id)
        return True

    monkeypatch.setattr(api_client_nexus, "export_nodes_impl", fake_export_nodes_impl)
    monkeypatch.setattr(api_client_nexus, "resolve_cartographer_path_from_node", fake_resolve)
    monkeypatch.setattr(WorkFlowyClientNexus, "refresh_file_node_beacons", fake_refresh_file)
    monkeypatch.setattr(WorkFlowyClientNexus, "move_node", fake_move)
    monkeypatch.setattr(WorkFlowyClientNexus, "create_node", fake_create)
    monkeypatch.setattr(WorkFlowyClientNexus, "update_node", fake_update)
    monkeypatch.setattr(WorkFlowyClientNexus, "delete_node", fake_delete)
    monkeypatch.delenv("CARTO_JOB_FILE", raising=False)

    client = object.__new__(WorkFlowyClientNexus)
    result = asyncio.run(
        client.refresh_folder_cartographer_sync("FOLDER", dry_run=dry_run)
    )
    return result, observed


def test_no_salvage_moves_and_retired_fields_gone(monkeypatch, tmp_path) -> None:
    result, observed = _run_folder_sync(monkeypatch, tmp_path, dry_run=False)
    assert result["success"] is True
    # §8.3: retired output contract fields are gone (no zero-valued shims).
    for key in ("notes_stashed", "notes_moved_to_root", "notes_reattached", "notes_unmatched"):
        assert key not in result
    # §8.1: zero salvage-only moves — the folder job performs NO MOVE calls
    # for emoji-prefixed notes (no stash, no reattach).
    assert observed["moves"] == []
    # The emoji notes child under F1 was never moved, deleted, or reattached.
    assert "N1" not in observed["deletes"]


def test_missing_file_deletion_uses_strict_classifier_only(monkeypatch, tmp_path) -> None:
    result, observed = _run_folder_sync(monkeypatch, tmp_path, dry_run=False)
    # F2 (real Path: header, resolved abs candidate, basename in name) and F3
    # (SAME qualifications but 📝-prefixed — the retired emoji exemption must
    # NOT save it, A5) are the ONLY deletions: the strict classifier excludes
    # the 🚨 diagnostic node (File: note), the prose node (Path: not on the
    # first line), and the emoji notes node (no Path: at all) — with NO emoji
    # whitelist involved.
    assert sorted(observed["deletes"]) == ["F2", "F3"]
    assert result["files_deleted"] == 2
    # Ordinary per-file refresh still runs for present files.
    assert observed["refreshed"] == ["F1"]
    assert result["refreshed_file_nodes"] == 1


def test_dry_run_performs_no_moves_or_deletes(monkeypatch, tmp_path) -> None:
    result, observed = _run_folder_sync(monkeypatch, tmp_path, dry_run=True)
    assert result["success"] is True
    assert observed["moves"] == []
    assert observed["deletes"] == []
    assert observed["creates"] == []
    assert result["files_deleted"] == 0


def test_disk_create_parity_new_file_and_intermediate_folder(monkeypatch, tmp_path) -> None:
    """§15.4 create parity (A5): on-disk files absent from Workflowy are
    created — a top-level 🐍 file node WITH its function child, and an
    intermediate 📂 folder for a nested path."""
    (tmp_path / "new.py").write_bytes(b"def new():\n    return 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.py").write_bytes(b"def inner():\n    return 2\n")

    result, observed = _run_folder_sync(monkeypatch, tmp_path, dry_run=False)
    assert result["success"] is True
    created_names = [c["name"] for c in observed["creates"]]
    # Top-level new file + its function child.
    assert "🐍 new.py" in created_names
    assert any(name.startswith("ƒ new(") for name in created_names)
    # Intermediate folder + nested file + its function child.
    assert "📂 sub" in created_names
    assert "🐍 inner.py" in created_names
    assert any(name.startswith("ƒ inner(") for name in created_names)
    # Parentage: the nested file lands under the created intermediate folder.
    folder_create = next(c for c in observed["creates"] if c["name"] == "📂 sub")
    file_create = next(c for c in observed["creates"] if c["name"] == "🐍 inner.py")
    assert file_create["parent_id"] == folder_create["id"]
    assert result["new_files_created"] == 2
    assert result["new_folders_created"] == 1
