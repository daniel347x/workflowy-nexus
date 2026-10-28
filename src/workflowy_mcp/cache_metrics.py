"""Derived read indexes owned by the lifetime of an immutable CacheView.

Only the EXACT map/list of the current operation's view qualifies. Private
copies, export subsets and other caller-owned maps keep their uncached path.
Markers are deliberately not keys: read-your-writes and archive successors
can have equal markers and different content. No I/O, TTL or publication.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any
import weakref


class _ViewMetrics:
    """Private slots; each value is built completely before being installed.

    Values contain only derived IDs/scalars, never a view or its source nodes.
    Concurrent duplicate builds are benign. Installed indexes are immutable;
    extending source metrics replaces the whole immutable index, never edits it.
    """

    def __init__(self) -> None:
        self.indexes: dict[str, Any] = {}
        self.source_metrics: Mapping[str, Mapping[str, int]] = MappingProxyType({})


_VIEW_METRICS: dict[int, tuple[weakref.ReferenceType, _ViewMetrics]] = {}


def _forget_view(key: int, reference: weakref.ReferenceType) -> None:
    entry = _VIEW_METRICS.get(key)
    if entry is not None and entry[0] is reference:
        _VIEW_METRICS.pop(key, None)


# @beacon[
#   id=cache-metrics@view-indexes,
#   role=read-index authority,
#   slice_labels=nexus--anchors,glimpse-render,ra-workflowy-cache,
#   kind=ast,
#   comment=Reuse immutable derived indexes only for the current operation's exact CacheView map. Weak identity plus an is check prevents equal-marker overlays or recycled object ids from sharing facts; values retain no source nodes and die with the view. Duplicate thread builds are benign and only complete indexes are installed.,
# ]
def view_metrics_for(nodes_by_id: Any) -> _ViewMetrics | None:
    """Return derived storage iff this map IS the pinned authoritative map."""
    from .coherence.operation_cache import current_operation_cache

    snapshot = current_operation_cache(required=False)
    if snapshot is None or nodes_by_id is not snapshot.view.nodes_by_id:
        return None
    view = snapshot.view
    key = id(view)
    entry = _VIEW_METRICS.get(key)
    if entry is not None and entry[0]() is view:
        return entry[1]
    memo = _ViewMetrics()
    reference = weakref.ref(view, lambda ref: _forget_view(key, ref))
    _VIEW_METRICS[key] = (reference, memo)
    return memo


# @beacon[
#   id=cache-metrics@map-from-pinned-list,
#   role=cache map provenance,
#   slice_labels=glimpse-render,ra-workflowy-cache,
#   kind=ast,
#   comment=Borrow the pinned view's existing ID map only when the supplied nodes list IS that view's immutable export list. Other lists return None so each caller keeps its original filtering/rebuild semantics; no cache acquisition adoption or content comparison.,
# ]
def map_for_pinned_nodes(nodes: Any) -> Mapping | None:
    """An identity-only proof, not a guess based on markers or equal rows."""
    from .coherence.operation_cache import current_operation_cache

    snapshot = current_operation_cache(required=False)
    if snapshot is not None and nodes is snapshot.view.nodes_export.get("nodes"):
        return snapshot.view.nodes_by_id
    return None
