"""The derivative copy from an archived source (Archive Reads U2, §U2.5
test 3) — Axiom 6 / G8 end to end.

A stubbed transport captures the ETCH payload: no 📦 anywhere in it, fresh
AP-IDs on the root and every AP-bearing descendant, the trail line naming
the ARCHIVE as the origin, the archive file's bytes unchanged, and no
overlay ever installed during the operation (it is a write; Axiom 5).
Completed descendants: skipped without ``include_completed`` (counted on
the row), re-completed with it.
"""

from __future__ import annotations

import asyncio

import pytest

from coherence_harness import make_boundary_capable

from workflowy_mcp import server
from workflowy_mcp.archive_context import current_archive_context
from workflowy_mcp.archive_store import ArchiveStore, set_archive_store_for_tests
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.node_archive import build_archive_document, serialize_archive

SOURCE_CACHE = {"epoch_number": 1, "epoch_id": "e1", "adopted_sequence": 3}

ARCH_ROOT = "eeeeeeee-0000-0000-0000-0000000000ee"
ARCH_CHILD = "eeeeeeee-0000-0000-0000-0000000000ef"
ARCH_COMPLETED = "eeeeeeee-0000-0000-0000-0000000000ea"
LIVE_OTHER = "bbbbbbbb-0000-0000-0000-000000000002"


def _nest(records):
    return WorkFlowyClientEtch.__new__(WorkFlowyClientEtch)._build_hierarchy(
        list(records)
    )


def _rec(node_id, name, parent_id=None, priority=0, note="", completed=False, **extra):
    record = {
        "id": node_id,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
        "completed": completed,
        "completedAt": 1_700_000_100 if completed else None,
        "createdAt": 1_690_000_000,
        "modifiedAt": 1_699_000_000,
    }
    record.update(extra)
    return record


def _archive_records():
    return [
        _rec(ARCH_ROOT, "[ap:ARCROOT] Archived root", note="root note"),
        _rec(
            ARCH_CHILD,
            "[ap:ARCCHILD] Archived child",
            parent_id=ARCH_ROOT,
            note="child note &amp; tail",
        ),
        _rec(
            ARCH_COMPLETED,
            "Archived done item",
            parent_id=ARCH_ROOT,
            note="done note",
            completed=True,
        ),
    ]


@pytest.fixture()
def archive_env(tmp_path, monkeypatch):
    document = build_archive_document(
        _archive_records(),
        nest=_nest,
        node_id=ARCH_ROOT,
        name="archived root",
        description=None,
        exported_at="2026-10-01T12:00:00Z",
        source_cache=dict(SOURCE_CACHE),
    )
    archive_bytes = serialize_archive(document).encode("utf-8")
    (tmp_path / "arch.json").write_bytes(archive_bytes)
    store = ArchiveStore(folder_provider=lambda: str(tmp_path))
    set_archive_store_for_tests(store)
    live = [_rec(LIVE_OTHER, "live destination", note="")]
    client = object.__new__(server.WorkFlowyClient)

    etch_calls: list[dict] = []
    completed_calls: list[str] = []

    def _send(*args, **kwargs):
        raise AssertionError("the raw write transport must never be reached")

    client._send_workflowy_write = _send

    # A stand-in write governor over a REAL bootstrapped coherence root:
    # whatever reads paths (coherence plumbing on the double) finds a
    # manifest; every REAL write still raises above.
    from pathlib import Path as _Path

    from workflowy_mcp.coherence import (
        coherence_paths,
        initialize_manifest_if_absent,
    )

    _jobs_root = _Path(tmp_path) / "carto_jobs"
    _jobs_root.mkdir(parents=True, exist_ok=True)
    _coh = coherence_paths("a" * 32, jobs_root=_jobs_root)
    from workflowy_mcp.coherence import new_bootstrap_manifest

    initialize_manifest_if_absent(_coh, new_bootstrap_manifest("a" * 32))

    class _Paths:
        account_scope_id = "a" * 32
        jobs_root = _jobs_root

    class _Gov:
        paths = _Paths()

    client._write_governor = _Gov()

    def _count_nodes(payload):
        total = 0
        stack = list(payload) if isinstance(payload, list) else [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if "name" in item:
                    total += 1
                stack.extend(item.get("children") or [])
            elif isinstance(item, list):
                stack.extend(item)
        return total

    async def _etch_impl(parent_id, nodes=None, nodes_file=None, execution="sync", *, _explicit_ap_cores=None):
        # The stubbed transport: captures the ETCH payload the derivative
        # branch built (the contract's capture point).
        etch_calls.append({"parent_id": parent_id, "nodes": nodes})
        count = _count_nodes(nodes)
        from n05_region_harness import publish_fixture_nodes
        publish_fixture_nodes(client, [*live, {
            "id": "cccccccc-0000-0000-0000-00000000cccc", "name": "copy", "parent_id": parent_id,
        }])
        return {
            "success": True,
            "nodes_created": count,
            "root_node_ids": ["cccccccc-0000-0000-0000-00000000cccc"],
            "errors": [],
        }

    monkeypatch.setattr(server, "_node_etch_sync_impl", _etch_impl)

    async def _complete(node_id, max_retries=1):
        completed_calls.append(node_id)
        return {"success": True, "completed_id": node_id}

    client.complete_node = _complete

    class _Copy:
        def __init__(self, node_id, priority):
            self.id = node_id
            self.priority = priority

    async def _list_nodes(request, max_retries=1):
        # Re-complete locates the copies by ONE live list per copied parent,
        # matched by document order: answer with as many copy rows as the
        # captured payload has children for that parent (priority order).
        parent = getattr(request, "parent_id", None)
        kids: list[str] = []
        for call in etch_calls:
            if call["parent_id"] != parent and parent is not None:
                continue
            root = (call.get("nodes") or [{}])[0]
            for child in root.get("children") or []:
                kids.append(str(child.get("name")))
        rows = [_Copy(f"copy-{index}", index) for index in range(len(kids))]
        return rows, len(rows)

    client.list_nodes = _list_nodes

    make_boundary_capable(client, nodes=live)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)

    async def _no_admission(spec):
        return None

    monkeypatch.setattr(server, "_mcp_mutation_admission", _no_admission)

    async def _vocab(target_tab):
        return (
            ["#wfy-ns"],
            {
                "local_postfixes": ["local"],
                "favorite_postfixes": ["fav"],
                "deprecated_postfixes": ["deprecated"],
            },
        )

    monkeypatch.setattr(server, "_read_anchor_dock_vocabulary", _vocab)

    class _Config:
        note_session_tags = ["#wfy-ns"]
        global_tags = []
        default_parent = ""

    async def _config(target_tab):
        return _Config()

    monkeypatch.setattr(server, "_resolve_note_session_config", _config)
    yield {
        "client": client,
        "etch_calls": etch_calls,
        "completed_calls": completed_calls,
        "archive_bytes": archive_bytes,
        "tmp_path": tmp_path,
    }
    set_archive_store_for_tests(None)


def _promote(params):
    return asyncio.run(server.anchor(verb="point.promote", params=params))


def _all_names(payload):
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)


def test_derivative_from_archived_source_copies_verbatim(archive_env):
    result = _promote(
        {
            "items": [
                {
                    "handle": "[ap:ARCROOT]",
                    "destination": LIVE_OTHER,
                    "mode": "derivative",
                }
            ]
        }
    )
    payload = result.get("detail", result)
    row = payload["results"][0]
    assert row["status"] == "promoted", row
    assert row["mode"] == "derivative"
    # The ETCH payload: no 📦 anywhere, no marker key, verbatim names/notes.
    assert len(archive_env["etch_calls"]) == 1
    call = archive_env["etch_calls"][0]
    assert call["parent_id"] == LIVE_OTHER
    rendered = _all_names(call)
    assert "📦" not in rendered
    assert "_nexus_archive" not in rendered
    # Titles preserved; the ORIGINAL cores are gone (fresh ones minted).
    assert "Archived root" in rendered
    assert "Archived child" in rendered
    assert "root note" in rendered
    # The archived child note was served VERBATIM (never re-decoded, E7).
    assert "child note &amp; tail" in rendered
    # Fresh AP cores on the root and every AP-bearing descendant; the old
    # cores appear nowhere.
    assert "ap:ARCROOT" not in rendered
    assert "ap:ARCCHILD" not in rendered
    import re

    fresh = re.findall(r"\[ap:([A-Z0-9]+)\]", rendered)
    assert len(fresh) >= 2 and len(set(fresh)) == len(fresh)
    # Completed descendant skipped by default, counted on the row.
    assert "Archived done item" not in rendered
    assert row["copy"]["completed_skipped"] == 1
    # The trail line names the ARCHIVE as the origin.
    assert row["trail_line"].startswith("↩ derived from ")
    assert 'archive "archived root" (arch.json, exported 2026-10-01T12:00:00Z)' in row["trail_line"]
    # The archive file is untouched; the operation never ran augmented.
    current_bytes = (archive_env["tmp_path"] / "arch.json").read_bytes()
    assert current_bytes == archive_env["archive_bytes"]


def test_no_overlay_during_derivative_operation(archive_env, monkeypatch):
    seen = []

    async def _etch_impl(parent_id, nodes=None, nodes_file=None, execution="sync", *, _explicit_ap_cores=None):
        context = current_archive_context()
        seen.append(
            {
                "overlay": None if context is None else context.overlay,
                "mode": None if context is None else context.mode,
            }
        )
        return {
            "success": True,
            "nodes_created": 2,
            "root_node_ids": ["cccccccc-0000-0000-0000-00000000cccc"],
            "errors": [],
        }

    monkeypatch.setattr(server, "_node_etch_sync_impl", _etch_impl)
    result = _promote(
        {
            "items": [
                {
                    "handle": "[ap:ARCROOT]",
                    "destination": LIVE_OTHER,
                    "mode": "derivative",
                }
            ]
        }
    )
    payload = result.get("detail", result)
    assert payload["results"][0]["status"] == "promoted"
    assert seen and seen[0]["overlay"] is None


def test_derivative_include_completed_recompletes(archive_env):
    result = _promote(
        {
            "items": [
                {
                    "handle": "[ap:ARCROOT]",
                    "destination": LIVE_OTHER,
                    "mode": "derivative",
                }
            ],
            "include_completed": True,
        }
    )
    payload = result.get("detail", result)
    row = payload["results"][0]
    assert row["status"] == "promoted", row
    rendered = _all_names(archive_env["etch_calls"][0])
    assert "Archived done item" in rendered
    assert row["copy"]["completion"]["recompleted"] == 1
    assert archive_env["completed_calls"], "the completed copy was re-completed"


def test_children_false_copies_root_alone(archive_env):
    result = _promote(
        {
            "items": [
                {
                    "handle": "[ap:ARCROOT]",
                    "destination": LIVE_OTHER,
                    "mode": "derivative",
                    "children": False,
                }
            ]
        }
    )
    payload = result.get("detail", result)
    row = payload["results"][0]
    assert row["status"] == "promoted", row
    call = archive_env["etch_calls"][0]
    assert "Archived child" not in _all_names(call)
    assert row["copy"]["completed_skipped"] == 0
