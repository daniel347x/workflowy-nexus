"""Regression lock: _lightning_rod_from_cache THREADS descend_policy into ALL modes.

The bug ([ap:Q8F2NZ] handoff, "localized non-benign bug to fix first")
--------------------------------------------------------------------------
``_lightning_rod_from_cache`` builds a ``_boundary_predicate`` when
``descend_policy='stop_at_child_anchors'`` and correctly passes it to the
``gather`` harvest -- but for the two RENDER modes (``render_tags`` /
``render_search``) it FORGOT to forward it into
``build_selected_subtree_from_cache``. So the MCP-return render modes silently
DESCENDED PAST child anchors, diverging from:

  * ``gather`` (which already honored the boundary), and
  * the pushed / docked-modal Lightning Rod path (which honors it via the
    slice-projection funnel).

That divergence is exactly the "a new field reaches one route but is dropped in
another scalar call" failure mode the RenderProjectionSpec refactor is meant to
kill. This test drives the REAL orchestrator (not the pure primitive -- that is
already locked in test_f8_slice_policy_anchor_boundary.py) so it would have
caught the omission at the ONE place it lived: the orchestrator's plumbing.

Fixture universe (a child anchor with a matching descendant)::

    root
      parent            #topic          (a matching, non-anchor member)
      child-anchor      #wfy-ns #topic  (a child ANCHOR that also matches)
        deep            #topic          (matches, but lives UNDER the anchor)

With ``descend_policy='stop_at_child_anchors'``:
  * ``render_tags(['topic'])`` must render ``parent`` and the child-anchor
    signpost, but NOT ``deep`` (it is behind the boundary).
  * ``render_search('topic')`` must behave identically.
  * the DEFAULT (``descend_policy='all'``) must still render ``deep``
    (proving the boundary is opt-in and the fix did not change default
    behavior).

All local: no network, no real Workflowy, no disk. The client cache and the
anchor-tag config read are monkeypatched.
"""

import asyncio
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402


# --- a tiny in-memory /nodes-export cache -----------------------------------
_ROOT = "aaaaaaaa-0000-0000-0000-000000000001"
_PARENT = "aaaaaaaa-0000-0000-0000-000000000002"
_ANCHOR = "aaaaaaaa-0000-0000-0000-000000000003"
_DEEP = "aaaaaaaa-0000-0000-0000-000000000004"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _PARENT, "name": "Parent point #topic", "note": "", "parent_id": _ROOT, "priority": 0},
    {"id": _ANCHOR, "name": "Child anchor #wfy-ns #topic", "note": "", "parent_id": _ROOT, "priority": 1},
    {"id": _DEEP, "name": "Deep under anchor #topic", "note": "", "parent_id": _ANCHOR, "priority": 0},
]

_ANCHOR_TAGS = ["wfy-ns"]  # the live anchor-defining tag list (as extension stores it)


def _make_client():
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]
    return c


async def _fake_note_session_config(_target_tab):
    """Stand in for the Direction-A get_extension_settings anchor-tag read.

    Returns (note_session_tags, global_tags, default_parent, raw_reply) --
    the tuple _lightning_rod_from_cache unpacks. Only the first element matters
    for the boundary predicate.
    """
    return (_ANCHOR_TAGS, [], None, {})


def _run(monkeypatch, **kwargs):
    monkeypatch.setattr(srv, "get_client", _make_client)
    monkeypatch.setattr(srv, "_resolve_note_session_config", _fake_note_session_config)
    return asyncio.run(srv._lightning_rod_from_cache(root_uuid=_ROOT, **kwargs))


def _rendered_uuids(reply):
    """UUIDs present in a full/UUID-mode Lightning Rod preview_tree render.

    In ``as_markdown=False`` mode ``preview_tree`` is a FLAT list of display
    strings, each led by a bracketed UUID, e.g.::

        "[aaaaaaaa-0000-0000-0000-000000000001] [WG-1  ] \u29bf Root"

    We pull the leading ``[uuid]`` off each line. Robust to any indentation the
    renderer prepends -- we take the FIRST bracketed token per line.
    """
    import re
    tree = reply.get("preview_tree")
    assert tree is not None, reply
    lines = tree if isinstance(tree, list) else str(tree).splitlines()
    out: set[str] = set()
    _lead = re.compile(r"\[([0-9a-fA-F]{8}-[0-9a-fA-F-]+)\]")
    for line in lines:
        m = _lead.search(str(line))
        if m:
            out.add(m.group(1))
    return out


# ---------------------------------------------------------------------------
# render_tags -- the bug's primary victim
# ---------------------------------------------------------------------------
def test_render_tags_honors_stop_at_child_anchors(monkeypatch):
    reply = _run(
        monkeypatch,
        mode="render_tags",
        tags=["topic"],
        as_markdown=False,  # preview_tree gives us UUIDs to assert on
        descend_policy="stop_at_child_anchors",
    )
    assert reply.get("success") is True, reply
    ids = _rendered_uuids(reply)
    # parent matches and is rendered; the child anchor is the visible signpost;
    # DEEP is behind the boundary and MUST NOT appear.
    assert _PARENT in ids, reply
    assert _ANCHOR in ids, reply
    assert _DEEP not in ids, f"DEEP leaked past the child-anchor boundary: {reply}"


def test_render_tags_default_descends_past_anchor(monkeypatch):
    # Default descend_policy='all' -> the boundary is opt-in; DEEP renders.
    reply = _run(
        monkeypatch,
        mode="render_tags",
        tags=["topic"],
        as_markdown=False,
    )
    assert reply.get("success") is True, reply
    ids = _rendered_uuids(reply)
    assert _DEEP in ids, f"default policy should render DEEP: {reply}"


# ---------------------------------------------------------------------------
# render_search -- the bug's second victim (same crack, different predicate)
# ---------------------------------------------------------------------------
def test_render_search_honors_stop_at_child_anchors(monkeypatch):
    reply = _run(
        monkeypatch,
        mode="render_search",
        query="topic",
        as_markdown=False,
        descend_policy="stop_at_child_anchors",
    )
    assert reply.get("success") is True, reply
    ids = _rendered_uuids(reply)
    assert _PARENT in ids, reply
    assert _ANCHOR in ids, reply
    assert _DEEP not in ids, f"DEEP leaked past the child-anchor boundary: {reply}"


def test_render_search_default_descends_past_anchor(monkeypatch):
    reply = _run(
        monkeypatch,
        mode="render_search",
        query="topic",
        as_markdown=False,
    )
    assert reply.get("success") is True, reply
    ids = _rendered_uuids(reply)
    assert _DEEP in ids, f"default policy should render DEEP: {reply}"


# ---------------------------------------------------------------------------
# as_markdown MUST be truthful on the spec (the Step-2 crack).
#
# Before the spec-driven refactor, _lightning_rod_from_cache built its
# RenderProjectionSpec WITHOUT passing as_markdown (defaulting it True) and the
# shared tail was driven by a SEPARATE scalar as_markdown. Once the tail became
# spec-driven, a lightning render_tags/render_search with as_markdown=False
# would have silently rendered Markdown (preview_markdown) instead of the
# bracketed full/UUID form (preview_tree) -- because the spec lied. These lock
# that the OUTPUT FORMAT flows truthfully through the ONE spec: as_markdown=False
# -> preview_tree (and NO preview_markdown); as_markdown=True -> preview_markdown.
# ---------------------------------------------------------------------------
def test_render_tags_as_markdown_false_returns_preview_tree(monkeypatch):
    reply = _run(
        monkeypatch,
        mode="render_tags",
        tags=["topic"],
        as_markdown=False,
    )
    assert reply.get("success") is True, reply
    assert reply.get("preview_tree") is not None, reply
    assert "preview_markdown" not in reply, reply


def test_render_tags_as_markdown_true_returns_preview_markdown(monkeypatch):
    reply = _run(
        monkeypatch,
        mode="render_tags",
        tags=["topic"],
        as_markdown=True,
    )
    assert reply.get("success") is True, reply
    assert isinstance(reply.get("preview_markdown"), str), reply
    assert "preview_tree" not in reply, reply


def test_render_search_as_markdown_false_returns_preview_tree(monkeypatch):
    reply = _run(
        monkeypatch,
        mode="render_search",
        query="topic",
        as_markdown=False,
    )
    assert reply.get("success") is True, reply
    assert reply.get("preview_tree") is not None, reply
    assert "preview_markdown" not in reply, reply
