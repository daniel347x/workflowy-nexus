"""Regression lock: add_to_anchor's optional ``tags=`` path.

The ``tags=`` convenience applies slice-group tag(s) to the CREATED TOP-LEVEL
ROOT node(s) in the same call (replacing the old 'add_to_anchor then a separate
workflowy_add_tag' two-step), with a per-tag-FAMILY auto-expand postfix rule:

  * a root that HAS descendants (a real tree) gets the auto-expand postfix
    (e.g. '--expand') appended per family;
  * a root that is a bare LEAF (incl. the common single-node capture) SKIPS
    the postfix;
  * the postfix rule runs INDEPENDENTLY per family and DOES NOT clobber a
    family that already carries a recognized postfix.

WHY THIS FILE EXISTS (the coverage gap it closes): the ``tags=`` branch does a
LAZY import ``from ..f8_slice_policy import apply_auto_expand_postfix_per_family``
inside the ``if want_tags`` block. A first shipped version used a BARE
``from f8_slice_policy import ...`` which is NOT importable from the client
package -> the branch raised ``No module named 'f8_slice_policy'`` at runtime,
the moment tags were actually applied. The full suite passed anyway because
nothing exercised the tags path end-to-end. These tests DO exercise it (the
real lazy import runs), so that class of regression can never pass CI again.

All local: no network, no real Workflowy, no disk writes. The client is built
with ``__new__`` (no live connection) and its etch/tag/guard collaborators are
stubbed so the REAL ``add_to_anchor`` body -- including the lazy import and the
per-family postfix pass -- runs unmodified.
"""

import asyncio
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.client import api_client_nexus as nx  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402


_PARENT = "aaaaaaaa-1111-2222-3333-444444444444"


def _make_client(monkeypatch, *, created_root_ids, forest_seen):
    """A real client whose etch/tag/guard collaborators are stubbed.

    ``created_root_ids`` is what the stubbed ETCH returns as root_node_ids.
    ``forest_seen`` collects the (uuid -> tags) that workflowy_add_tag received,
    so a test can assert exactly what got tagged and with what.
    """
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)

    # --- parent-guard machinery: make the parent a plain UNMAPPED node ---
    async def _fake_export_nodes(node_id=None, use_cache=True, **kw):
        return {"nodes": [{"id": _PARENT, "name": "Anchor", "note": "", "parent_id": None}]}

    c.export_nodes = _fake_export_nodes  # type: ignore[assignment]
    # resolve_uuid_or_anchor_point_handle + _classify_node_map_state +
    # _anchor_target_guard_error are module-level in api_client_nexus; patch
    # them to the 'unmapped, allowed' verdict.
    monkeypatch.setattr(
        nx, "resolve_uuid_or_anchor_point_handle",
        lambda handle, nodes_by_id: {"resolved_uuid": handle, "resolved_via": "uuid"},
    )
    monkeypatch.setattr(
        nx, "_classify_node_map_state",
        lambda uuid, nodes_by_id: {"state": "unmapped", "file_path": None},
    )
    monkeypatch.setattr(nx, "_anchor_target_guard_error", lambda verdict: None)

    # --- stub ETCH: report the created root ids, success ---
    async def _fake_etch(parent_id, nodes, nodes_file=None):
        return {
            "success": True,
            "nodes_created": len(created_root_ids),
            "root_node_ids": list(created_root_ids),
            "errors": [],
        }

    c.workflowy_etch = _fake_etch  # type: ignore[assignment]

    # --- stub add_tag: record what each root was tagged with ---
    async def _fake_add_tag(node_id, tag, max_retries=10):
        tags = tag if isinstance(tag, list) else [tag]
        forest_seen[node_id] = list(tags)
        return {
            "success": True,
            "node_id": node_id,
            "tags_added": list(tags),
            "tags_already_present": [],
            "no_op": False,
        }

    c.workflowy_add_tag = _fake_add_tag  # type: ignore[assignment]
    return c


def _run(coro):
    return asyncio.run(coro)


def test_single_leaf_gets_plain_tag_no_expand_postfix(monkeypatch):
    """One bare leaf node -> tag applied PLAIN (no --expand). Also proves the
    lazy ``from ..f8_slice_policy import`` resolves (the import-bug regression)."""
    seen: dict[str, list[str]] = {}
    root_id = "11111111-0000-0000-0000-000000000001"
    c = _make_client(monkeypatch, created_root_ids=[root_id], forest_seen=seen)

    result = _run(c.add_to_anchor(
        parent_uuid=_PARENT,
        nodes=[{"name": "A lone captured point"}],  # NO children -> leaf
        tags=["build-v1"],
        auto_expand_postfix="expand",
        postfix_words=["expand", "suppress", "deprecated"],
    ))

    assert result["success"] is True
    assert seen[root_id] == ["build-v1"]  # PLAIN, no --expand
    applied = {r["node_id"]: r for r in result["tags_applied"]}
    assert applied[root_id]["expand_postfix_applied"] is False


def test_root_with_descendants_gets_expand_postfix(monkeypatch):
    """A root that HAS children -> the auto-expand postfix is appended."""
    seen: dict[str, list[str]] = {}
    root_id = "22222222-0000-0000-0000-000000000002"
    c = _make_client(monkeypatch, created_root_ids=[root_id], forest_seen=seen)

    result = _run(c.add_to_anchor(
        parent_uuid=_PARENT,
        nodes=[{"name": "A tree root", "children": [{"name": "child"}]}],
        tags=["build-v1"],
        auto_expand_postfix="expand",
        postfix_words=["expand", "suppress", "deprecated"],
    ))

    assert result["success"] is True
    assert seen[root_id] == ["build-v1--expand"]
    applied = {r["node_id"]: r for r in result["tags_applied"]}
    assert applied[root_id]["expand_postfix_applied"] is True


def test_dont_clobber_family_reports_expand_not_applied(monkeypatch):
    """A root WITH descendants whose tag family ALREADY carries a recognized
    postfix is left untouched by the don't-clobber rule -- so no --expand is
    added, and expand_postfix_applied MUST be False (it reports whether a
    postfix was ACTUALLY appended, not merely whether the pass ran)."""
    seen: dict[str, list[str]] = {}
    root_id = "66666666-0000-0000-0000-000000000007"
    c = _make_client(monkeypatch, created_root_ids=[root_id], forest_seen=seen)

    result = _run(c.add_to_anchor(
        parent_uuid=_PARENT,
        nodes=[{"name": "Tree root", "children": [{"name": "kid"}]}],
        tags=["beta", "beta-backend--deprecated"],  # family has a postfix member
        auto_expand_postfix="expand",
        postfix_words=["expand", "suppress", "deprecated"],
    ))

    assert result["success"] is True
    # Whole family left untouched (NO --expand appended to either).
    assert seen[root_id] == ["beta", "beta-backend--deprecated"]
    applied = {r["node_id"]: r for r in result["tags_applied"]}
    # The pass RAN (root has descendants) but changed nothing -> flag False.
    assert applied[root_id]["expand_postfix_applied"] is False


def test_mixed_forest_tags_each_root_by_its_own_shape(monkeypatch):
    """A forest of [tree-with-children, bare-leaf] -> the tree root gets
    --expand, the leaf root gets the plain tag. root_ids align to payload order."""
    seen: dict[str, list[str]] = {}
    tree_id = "33333333-0000-0000-0000-000000000003"
    leaf_id = "33333333-0000-0000-0000-000000000004"
    c = _make_client(monkeypatch, created_root_ids=[tree_id, leaf_id], forest_seen=seen)

    result = _run(c.add_to_anchor(
        parent_uuid=_PARENT,
        nodes=[
            {"name": "Tree root", "children": [{"name": "kid"}]},
            {"name": "Leaf root"},
        ],
        tags=["build-v1"],
        auto_expand_postfix="expand",
        postfix_words=["expand", "suppress", "deprecated"],
    ))

    assert result["success"] is True
    assert seen[tree_id] == ["build-v1--expand"]
    assert seen[leaf_id] == ["build-v1"]


def test_stringified_forest_payload_still_gates_by_children(monkeypatch):
    """When nodes arrive as a STRINGIFIED JSON payload (an agent habit), the
    shared parser still recovers the forest so the has-descendants gating is
    correct -- a stringified tree root still gets --expand."""
    import json
    seen: dict[str, list[str]] = {}
    root_id = "44444444-0000-0000-0000-000000000005"
    c = _make_client(monkeypatch, created_root_ids=[root_id], forest_seen=seen)

    payload = json.dumps([{"name": "Str tree", "children": [{"name": "c"}]}])
    result = _run(c.add_to_anchor(
        parent_uuid=_PARENT,
        nodes=payload,
        tags=["build-v1"],
        auto_expand_postfix="expand",
        postfix_words=["expand", "suppress", "deprecated"],
    ))

    assert result["success"] is True
    assert seen[root_id] == ["build-v1--expand"]


def test_no_tags_is_plain_capture_no_tags_applied_block(monkeypatch):
    """Omitting tags -> byte-for-byte the prior behavior: no add_tag calls,
    no tags_applied block."""
    seen: dict[str, list[str]] = {}
    root_id = "55555555-0000-0000-0000-000000000006"
    c = _make_client(monkeypatch, created_root_ids=[root_id], forest_seen=seen)

    result = _run(c.add_to_anchor(
        parent_uuid=_PARENT,
        nodes=[{"name": "Plain capture"}],
    ))

    assert result["success"] is True
    assert seen == {}  # no add_tag calls
    assert "tags_applied" not in result
