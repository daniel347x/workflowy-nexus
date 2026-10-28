"""Pure source-child metadata for safe structural inventories.

Counts come from a caller-owned operation snapshot, NEVER a projected render
forest: a filtered/collapsed parent can still have children. No I/O or cache
mutation. Completed, suppressed and out-of-slice children all count.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .cache_metrics import view_metrics_for


# @beacon[
#   id=node-children@count-index,
#   slice_labels=nexus--mcp-api,nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=ONE direct-child count authority over the full source map; immutable counts are reused for the exact pinned CacheView and returned as owned copies. Other maps recompute. Includes completed/suppressed/out-of-slice children; known leaf is zero absent node unknown. Shared by headings-only Dock peek and completion acknowledgements; never counts the filtered forest or mutates cache data.,
# ]
def build_direct_child_counts(nodes_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    """Return owned counts, reusing only the exact immutable operation view."""
    memo = view_metrics_for(nodes_by_id)
    if memo is not None and "direct_counts" in memo.indexes:
        return memo.indexes["direct_counts"].copy()
    counts = {str(node_id): 0 for node_id in nodes_by_id}
    for node in nodes_by_id.values():
        parent = node.get("parent_id") if "parent_id" in node else node.get("parentId")
        if parent is not None and str(parent) in counts:
            counts[str(parent)] += 1
    if memo is not None:
        memo.indexes.setdefault("direct_counts", MappingProxyType(dict(counts)))
    return counts


# @beacon[
#   id=auto-beacon@format_child_count_suffix-1m3v,
#   role=format_child_count_suffix,
#   slice_labels=glimpse-render,
#   kind=ast,
# ]
def format_child_count_suffix(node_id: str, counts: Mapping[str, int] | None) -> str:
    """▸N is total direct children; ▸? is unknown, never silently zero.

    UUID-less synthetic display headings are not actual nodes and get no
    count. Absence of a cache operand remains explicit on ad-hoc DOM renders.
    """
    if not node_id:
        return ""
    count = counts.get(node_id) if counts is not None else None
    return f" ▸{count}" if count is not None else " ▸?"
