"""Regression lock: render_spec re-projection of a LIGHTNING ROD modal scope.

[ap:GLMPMODAL] (v1). Previously ``_glimpse_dispatch_spec`` refused a
``kind='lightning_rod'`` spec ("only slice / slice_override / raw_root scopes
are supported here"), so a docked modal showing a Lightning Rod result could
NOT be re-projected via ``render_spec="modal"`` (or a pasted blob). The fix is
a PURE DISPATCH ADDITION: the fourth scope cell routes by
``spec.lightning['mode']`` to the SAME ``_lightning_rod_from_cache`` /
``_lightning_rod_render_symbols`` projectors a fresh ``lightning_rod()`` call
uses -- no new render path.

These tests drive ``_glimpse_dispatch_spec`` DIRECTLY with lightning specs over
a tiny in-memory /nodes-export cache (the same pattern as
test_glimpse_render_spec_modal.py), and lock:

  1. render_tags   -> the matching subtree is re-projected (Markdown string).
  2. render_search -> the query-matched subtree is re-projected.
  3. A FOCUSED render_tags modal read now SUCCEEDS (ap:GLMPMODAL follow-up):
     spec.focus is threaded into the projector -> the ONE spec -> the shared
     tail's single focus authority, rendering the option-Y row view. (This
     test FLIPPED from the v1 'focus rejected' lock.)
  3b. A FOCUSED *gather* read is still loud-rejected (a gather is a tag
     outline, not a node tree -- there is no real row to focus).
  4. A lightning spec with NO `lightning` selection fails loudly.
  5. An unknown lightning mode fails loudly.

All local: no network, no real Workflowy, no disk writes.
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
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402


# --- a tiny in-memory /nodes-export cache (root + two tagged children) ------
_ROOT = "33333333-1111-bbbb-cccc-000000000001"
_CHILD_A = "33333333-2222-bbbb-cccc-000000000002"
_CHILD_B = "33333333-3333-bbbb-cccc-000000000003"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Lightning Anchor Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _CHILD_A, "name": "Alpha point #keepme", "note": "invoice body", "parent_id": _ROOT, "priority": 0},
    {"id": _CHILD_B, "name": "Beta point", "note": "receipt body", "parent_id": _ROOT, "priority": 1},
]


def _make_client():
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]
    return c


def _patch_common(monkeypatch):
    monkeypatch.setattr(srv, "get_client", _make_client)


def _lightning_spec(selection, *, root_uuid=_ROOT, focus=None):
    """Build a resolved kind='lightning_rod' spec exactly like the client stores."""
    return RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=root_uuid,
        lightning=selection,
        focus=focus,
    )


# ---------------------------------------------------------------------------
# 1. render_tags re-projects the tag-matched subtree.
# ---------------------------------------------------------------------------
def test_lightning_render_tags_reprojects(monkeypatch):
    _patch_common(monkeypatch)
    spec = _lightning_spec({"mode": "render_tags", "tags": ["keepme"], "match_scope": "name_and_note"})
    reply = asyncio.run(srv._glimpse_dispatch_spec(spec, output_file=None, target_tab=None))
    # Markdown-mode dispatch returns the internal dict here (dispatch is below
    # the agent-facing _shape_glimpse_family_return seam); the render is in
    # preview_markdown. The keepme-tagged node is present; the untagged one is
    # not a member (only the connective root + the match).
    assert isinstance(reply, dict), reply
    md = reply.get("preview_markdown")
    assert isinstance(md, str), reply
    assert "Alpha point" in md, reply
    assert "Beta point" not in md, reply


# ---------------------------------------------------------------------------
# 2. render_search re-projects the query-matched subtree.
# ---------------------------------------------------------------------------
def test_lightning_render_search_reprojects(monkeypatch):
    _patch_common(monkeypatch)
    spec = _lightning_spec(
        {"mode": "render_search", "query": "receipt", "query_mode": "smart", "match_scope": "name_and_note"}
    )
    reply = asyncio.run(srv._glimpse_dispatch_spec(spec, output_file=None, target_tab=None))
    assert isinstance(reply, dict), reply
    md = reply.get("preview_markdown")
    assert isinstance(md, str), reply
    assert "Beta point" in md, reply
    assert "Alpha point" not in md, reply


# ---------------------------------------------------------------------------
# 3. A FOCUSED render_tags modal read now SUCCEEDS (FLIPPED from the v1 lock).
#    The focus is resolved occurrence-exactly by the shared tail's single
#    focus authority and the option-Y row view is rendered.
# ---------------------------------------------------------------------------
def test_lightning_render_tags_focus_reprojects(monkeypatch):
    _patch_common(monkeypatch)
    spec = _lightning_spec(
        {"mode": "render_tags", "tags": ["keepme"], "match_scope": "name_and_note"},
        focus={"mode": "row", "uuid": _CHILD_A, "ancestor_path": [_ROOT], "occurrence": 0},
    )
    reply = asyncio.run(srv._glimpse_dispatch_spec(spec, output_file=None, target_tab=None))
    assert isinstance(reply, dict), reply
    # No longer an error: the focused option-Y view renders successfully.
    assert reply.get("success") is not False, reply
    md = reply.get("preview_markdown")
    assert isinstance(md, str), reply
    # The focused row (Alpha) is present; Beta (not a member) never is.
    assert "Alpha point" in md, reply
    assert "Beta point" not in md, reply


# ---------------------------------------------------------------------------
# 3b. A FOCUSED *gather* read is still loud-rejected -- a gather is a tag
#     outline (one synthetic root row), not a navigable node tree.
# ---------------------------------------------------------------------------
def test_lightning_gather_focus_rejected(monkeypatch):
    _patch_common(monkeypatch)
    spec = _lightning_spec(
        {"mode": "gather", "match_scope": "name_and_note"},
        focus={"mode": "row", "uuid": _CHILD_A, "ancestor_path": [_ROOT], "occurrence": 0},
    )
    reply = asyncio.run(srv._glimpse_dispatch_spec(spec, output_file=None, target_tab=None))
    assert isinstance(reply, dict), reply
    assert reply.get("success") is False, reply
    assert "gather" in (reply.get("error") or "").lower(), reply


# ---------------------------------------------------------------------------
# 4. A lightning spec with NO selection fails loudly.
# ---------------------------------------------------------------------------
def test_lightning_missing_selection_fails(monkeypatch):
    _patch_common(monkeypatch)
    # Force kind='lightning_rod' but strip the selection.
    spec = _lightning_spec({"mode": "render_tags", "tags": ["keepme"]}).with_overrides(lightning=None)
    reply = asyncio.run(srv._glimpse_dispatch_spec(spec, output_file=None, target_tab=None))
    assert isinstance(reply, dict), reply
    assert reply.get("success") is False, reply
    assert "lightning" in (reply.get("error") or "").lower(), reply


# ---------------------------------------------------------------------------
# 5. An unknown lightning mode fails loudly.
# ---------------------------------------------------------------------------
def test_lightning_unknown_mode_fails(monkeypatch):
    _patch_common(monkeypatch)
    spec = _lightning_spec({"mode": "nonsense_mode"})
    reply = asyncio.run(srv._glimpse_dispatch_spec(spec, output_file=None, target_tab=None))
    assert isinstance(reply, dict), reply
    assert reply.get("success") is False, reply
    assert "mode" in (reply.get("error") or "").lower(), reply
