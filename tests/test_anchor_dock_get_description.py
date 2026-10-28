"""Unit 7H ([ap:YLHDIN] part 1) — ``anchor.dock.get_description``.

Locks the Dock-description getter as a thin Dock-checked PRESET over the ONE
``anchor.point.read`` metadata header (no second header builder):

* a Dock returns the header (name + markers + metrics), a ``child docks: N``
  line counting DIRECT children that are themselves Docks (through
  the source topology's parent-key precedence and the ONE boundary predicate,
  without a discarded size aggregate), and the FULL unguarded note;
* a non-Dock is a loud ``not_an_anchor_dock`` teaching error pointing at
  ``anchor.point.read``;
* a handle absent from the pinned cache is a loud RuntimeError.

The shared read machinery (``_anchor_point_read_impl``) is used for real;
only acquisition is stubbed (pinned cache map, config reply, live note).
"""

from __future__ import annotations

import asyncio

import pytest

from workflowy_mcp import server


def _cache():
    return {
        "d1": {"id": "d1", "name": "Hub #wfy-ns", "note": "hub note", "parent_id": None},
        "c1": {"id": "c1", "name": "Child Dock #wfy-ns", "note": "", "parent_id": "d1"},
        "p1": {"id": "p1", "name": "Plain child", "note": "", "parent_id": "d1"},
        "g1": {
            "id": "g1", "name": "Grandchild Dock #wfy-ns", "note": "",
            "parent_id": "c1",
        },
    }


def _reply():
    return {
        "note_session_tags": ["wfy-ns"],
        "note_session_local_postfixes": ["local"],
        "note_session_favorite_postfixes": ["fav"],
        "note_session_deprecated_postfixes": ["deprecated"],
    }


@pytest.fixture()
def getter_env(monkeypatch):
    cache = _cache()

    async def _resolve(node_id, *, label, required=True):
        return node_id, dict(cache)

    async def _config(target_tab):
        return (["wfy-ns"], [], None, _reply())

    async def _live_note(node_id):
        return cache[node_id], "the description body"

    monkeypatch.setattr(
        server, "_resolve_generic_node_handle_with_cache", _resolve
    )
    monkeypatch.setattr(server, "_resolve_note_session_config", _config)
    monkeypatch.setattr(server, "_read_live_node_note", _live_note)
    return cache


def test_get_description_returns_header_child_dock_count_and_full_note(
    getter_env,
):
    out = asyncio.run(server._anchor_dock_get_description_impl("d1"))
    lines = out.splitlines()
    # The ONE metadata header, then the ONE Dock-only line directly beneath it.
    assert lines[0].startswith("Hub")
    assert lines[1] == "child docks: 1"  # c1 is a Dock; p1 is not; g1 is deeper.
    # Tab-less test env: point.read's Dock markers degrade VISIBLY (documented).
    assert "dock markers unavailable" in out
    # The full note rides unguarded (no elision header, numbered rows).
    assert "the description body" in out


def test_get_description_child_dock_count_includes_local_and_favorite(getter_env):
    cache = getter_env
    cache["l1"] = {
        "id": "l1", "name": "Local child #wfy-ns--local", "note": "",
        "parent_id": "d1",
    }
    cache["f1"] = {
        "id": "f1", "name": "Fav child #wfy-ns--fav", "note": "",
        "parent_id": "d1",
    }
    out = asyncio.run(server._anchor_dock_get_description_impl("d1"))
    assert "child docks: 3" in out  # c1 + l1 + f1 (g1 is not a DIRECT child).


def test_get_description_refuses_a_non_dock_loudly(getter_env):
    from workflowy_mcp.server import _AnchorOperationError

    with pytest.raises(_AnchorOperationError) as excinfo:
        asyncio.run(server._anchor_dock_get_description_impl("p1"))
    assert excinfo.value.code == "not_an_anchor_dock"
    assert "anchor.point.read" in excinfo.value.message


def test_get_description_refuses_a_cache_miss_loudly(monkeypatch):
    async def _resolve(node_id, *, label, required=True):
        return node_id, {}

    async def _config(target_tab):
        return (["wfy-ns"], [], None, _reply())

    monkeypatch.setattr(
        server, "_resolve_generic_node_handle_with_cache", _resolve
    )
    monkeypatch.setattr(server, "_resolve_note_session_config", _config)
    with pytest.raises(RuntimeError, match="system.cache.refresh"):
        asyncio.run(server._anchor_dock_get_description_impl("zz"))
