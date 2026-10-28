"""SCRY retains its requested root note, including truncation and file export.

Real cache/export/serializer/dispatch code; pinned isolated fixture snapshot,
no network, no live cache refresh, and no alternate root-node fetch.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from coherence_harness import make_boundary_capable, s04_boundary_status

from workflowy_mcp import server
from workflowy_mcp.coherence import COHERENCE_READ_ADVISORY_LINE
from workflowy_mcp.mcp_api_registry import dispatch_operation

ROOT = "62a570d8-ef69-42fb-9ab4-3b6a152d94a7"
CHILD = "129683c7-eb13-49d1-99e2-0bc31de4c5bc"
GRANDCHILD = "0e504539-7497-4b5a-907e-aa6150634f21"
NOTE = "# ROOT_NOTE_SENTINEL 📊\nfull root content &lt;literal&gt;\nlast line  "


def _setup(monkeypatch, *, leaf=False, root_last=False, note=NOTE, hard=False):
    root = {"id": ROOT, "name": "[ap:ROOTNT] Root", "note": note, "parent_id": None}
    descendants = [] if leaf else [
        {"id": CHILD, "name": "Child", "note": "child note", "parent_id": ROOT, "priority": 0},
        {"id": GRANDCHILD, "name": "Grandchild", "note": "grandchild note", "parent_id": CHILD},
    ]
    nodes = descendants + [root] if root_last else [root] + descendants
    before = copy.deepcopy(nodes)
    status = s04_boundary_status(
        hard_dirty=hard, hard_generation=3,
        hard_reason_code="verified_effect_unpublishable", hard_transaction_id="tx-root-note",
    )
    client = make_boundary_capable(
        object.__new__(server.WorkFlowyClient), nodes=nodes, boundary_status=status,
    )
    client.get_node = AsyncMock(side_effect=AssertionError("must not fetch root via a second/live path"))
    client.list_nodes = AsyncMock(side_effect=AssertionError("must use the pinned subtree"))
    client.refresh_nodes_export_cache = AsyncMock(side_effect=AssertionError("must not refresh"))
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    return client, nodes, before


def _scry(**params):
    # No browser dependency: the existing Dock-root guard is independently
    # tested in test_anchor_dock_root_guard.py. All dispatch/coherence hooks
    # and the real shared UUID/AP resolver remain active here.
    return asyncio.run(dispatch_operation(
        server.build_node_registry(), "scry",
        {"root_handle": "[ap:ROOTNT]", "allow_anchor_dock": True, **params},
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    ))


@pytest.mark.parametrize("leaf", [False, True])
@pytest.mark.parametrize("depth", [None, 1])
def test_in_memory_scry_includes_root_note(monkeypatch, leaf, depth):
    client, nodes, before = _setup(monkeypatch, leaf=leaf)
    result = _scry(depth=depth)
    assert result["success"] is True
    assert result["root"]["id"] == ROOT
    assert result["root"]["note"] == NOTE
    assert "ROOT_NOTE_SENTINEL" in "\n".join(result["preview_tree"])
    assert result["node_count"] == (1 if leaf else 3)
    assert nodes == before
    client.get_node.assert_not_awaited()
    client.list_nodes.assert_not_awaited()
    client.refresh_nodes_export_cache.assert_not_awaited()


@pytest.mark.parametrize("size_limit", [1, 2])
@pytest.mark.parametrize("root_last", [False, True])
def test_size_limit_never_drops_requested_root(monkeypatch, size_limit, root_last):
    client, nodes, before = _setup(monkeypatch, root_last=root_last)
    result = _scry(size_limit=size_limit)
    assert result["success"] is True
    assert result["root"]["id"] == ROOT
    assert result["root"]["note"] == NOTE
    assert result["node_count"] == size_limit
    assert result["export_root_children_status"] == "truncated_by_max_nodes"
    if size_limit == 1:
        assert result["children"] == []
        assert len(result["preview_tree"]) == 1
    else:
        assert [n["id"] for n in result["children"]] == [CHILD]
    assert nodes == before
    client.get_node.assert_not_awaited()


@pytest.mark.parametrize("leaf", [False, True])
@pytest.mark.parametrize("hard", [False, True])
def test_file_scry_retains_root_note_and_children_layout(monkeypatch, tmp_path, leaf, hard):
    client, nodes, before = _setup(monkeypatch, leaf=leaf, hard=hard)
    path = tmp_path / "scry_root.json"
    result = _scry(output_file=str(path), depth=1)
    assert result["success"] is True
    terrain = json.loads(path.read_text(encoding="utf-8"))
    assert terrain["export_root_id"] == ROOT
    assert terrain["export_root_note"] == NOTE
    # Additive metadata only: children stay at their established positions;
    # no synthetic root inserted into the editable children array/preview IDs.
    assert [n["id"] for n in terrain["nodes"]] == ([] if leaf else [CHILD])
    markdown_path = Path(result["markdown_file"])
    markdown = markdown_path.read_text(encoding="utf-8")
    assert NOTE in markdown
    assert markdown.count("ROOT_NOTE_SENTINEL") == 1
    if not leaf:
        assert markdown.index("ROOT_NOTE_SENTINEL") < markdown.index("# Child")
        assert "child note" in markdown
    assert ("coherence_advisory" in result) is hard
    assert ("coherence_advisory" in terrain) is hard
    assert markdown.startswith(COHERENCE_READ_ADVISORY_LINE) is hard
    assert path.with_suffix(".original.json").read_bytes() == path.read_bytes()
    assert path.with_suffix(".original.md").read_bytes() == markdown_path.read_bytes()
    assert nodes == before
    client.get_node.assert_not_awaited()
    client.list_nodes.assert_not_awaited()
    client.refresh_nodes_export_cache.assert_not_awaited()


@pytest.mark.parametrize("note", [None, "", "  \n\t"])
def test_file_scry_preserves_empty_root_note_metadata(monkeypatch, tmp_path, note):
    _setup(monkeypatch, leaf=True, note=note)
    path = tmp_path / "empty.json"
    result = _scry(output_file=str(path))
    assert result["success"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["export_root_note"] == note
