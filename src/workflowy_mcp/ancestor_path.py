"""Ancestor-chain document core — the ONE shared F3 walk/format authority.

The extension's F3 hotkey (and its F2 verbose sibling) ask the daemon for the
full root→node ancestor chain rendered as Markdown: one heading per ancestor
(H1 = root, deepening by level), the target as the final ``•`` line, trailing
tags shown as part of each name, and the target's UUID after ``→``.  This
module is that walk and that grammar, lifted pure out of the WebSocket
handler so BOTH homes share one implementation and can never drift:

* ``server._resolve_uuid_path_and_respond`` — the F3/F2 WebSocket handler.
  It sends frames; the MCP verb NEVER calls it (binding constraint, U1).
* ``server._node_ancestors_impl`` — the ``node.ancestors`` MCP verb front.

Right-side markers use dock.peek's vocabulary (⚓ ⭐ Ⓛ ✓ plus its four source
metrics) extended with the mapped file/folder markers this feature exists
for: 🗺️ = a mapped FOLDER node (``Root:``/bare ``Path:`` header), 📄 = a
mapped FILE node (``Path:`` with an extension) — the on-line answer to "is
this node (or an ancestor) Cartographer-mapped?", so an agent never has to
infer mapping from a tag or a filename.  A key line sits at the top of every
standalone render, exactly as dock.peek does.

Purity contract: no I/O, no WS frames, no cache acquisition (callers pass a
pinned ``nodes_by_id`` snapshot), no logging, and source node dicts are NEVER
mutated — the decoded name rides on owned chain entries.  Classification and
metric authorities are REUSED, never re-implemented: ``classify_anchor_tags``
(Dock identity/attributes), ``_node_is_completed``, ``_classify_node_map_state``
(mapped verdict) and ``build_dock_peek_index``/``format_dock_peek_metrics``
(the peek source metrics).
"""
from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from typing import Any

from .dock_peek_metrics import build_dock_peek_index, format_dock_peek_metrics
from .f8_slice_policy import _node_is_completed, classify_anchor_tags

# The walk cap shared with every other cache parent-chain walk in the tree
# (_resolve_uuid_path_and_respond / _build_path_lines_from_cache /
# _classify_node_map_state): 512 hops, cycle-guarded.
MAX_ANCESTOR_HOPS = 512

# The F2/F3/F4 family as one parameter: 'leaf' = F3 (target UUID after →),
# 'every-level' = F2 (a UUID line under every heading), 'none' = no UUIDs
# (the F4 bare-UUID hotkey itself is client-side; this is the path without
# UUID noise).
UUIDS_MODES = ("leaf", "every-level", "none")

# Marker vocabulary — dock.peek's compact row order (⚓ ⭐ Ⓛ ✓) extended with
# the mapped file/folder markers. One key line, at the top of the render,
# exactly as dock.peek prints its Key: line (peek's "(recursion stops here)"
# gloss is a traversal fact of peek's own render and does not apply to a
# chain document, so the plain "⚓ Dock" form is used).
MARKER_KEY_LINE = (
    "Key: ⚓ Dock · ⭐ favorite Dock · Ⓛ local Dock · ✓ completed · "
    "🗺️ mapped folder · 📄 mapped file"
)
# The metrics legend is dock.peek's verbatim: same source metrics, same
# build_dock_peek_index authority, same snapshot caveats.
METRICS_LEGEND_LINE = (
    "Metrics: ▸ direct children · desc all descendants · self title+note UTF-8 size "
    "· tree self+all descendants; includes completed/suppressed content and content "
    "behind Dock signposts. Snapshot facts, not live verification."
)


# @beacon[
#   id=ancestor-path@walk,
#   slice_labels=nexus-path-resolution-logic,ra-workflowy-cache,nexus--mcp-api,node-ops,
#   kind=ast,
#   comment=Pure root→target parent-chain walk over a pinned /nodes-export map — the F3 walk lifted verbatim out of the WS handler (512-hop cap, visited-set cycle guard, HTML-entity-decoded names) so the F3 hotkey and the node.ancestors verb share ONE walk. Returns [] when the target is absent; a broken parent link truncates at the last resolvable ancestor. Source node dicts are never mutated (the decoded name rides on owned entries — the pre-lift handler wrote _decoded_name back into cache dicts; this core does not).,
# ]
def walk_ancestor_chain(
    target_uuid: str,
    nodes_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Walk the parent chain from ``target_uuid`` up to the root; return root→target.

    Each entry is an OWNED dict ``{"id", "name", "parent_id"}`` where ``name``
    is the full node name (trailing #tags included) with HTML entities
    decoded, exactly as the F3 handler always displayed it.
    """
    target = str(target_uuid or "").strip()
    if not target or target not in nodes_by_id:
        return []
    chain: list[dict[str, Any]] = []
    visited: set[str] = set()
    current: str | None = target
    hops = 0
    while current and current not in visited and hops < MAX_ANCESTOR_HOPS:
        visited.add(current)
        node = nodes_by_id.get(current)
        if not node:
            break
        raw_name = node.get("nm") or node.get("name") or "Untitled"
        parent_id = node.get("parent_id") or node.get("parentId")
        chain.append({
            "id": current,
            "name": html.unescape(str(raw_name)),
            "parent_id": parent_id,
        })
        current = str(parent_id) if parent_id else None
        hops += 1
    chain.reverse()
    return chain


# @beacon[
#   id=ancestor-path@document,
#   slice_labels=nexus-path-resolution-logic,nexus--mcp-api,node-ops,
#   kind=ast,
#   comment=The ONE ancestor-chain document composer shared by the F3/F2 WS handler and the node.ancestors verb (they can never drift): heading per ancestor with ⦿/• bullets, key line + dock.peek-verbatim metrics legend at the top of every standalone render, right-side markers in peek's compact order (⚓ ⭐ Ⓛ ✓) plus 🗺️ mapped folder / 📄 mapped file via the existing _classify_node_map_state authority, four source metrics via build_dock_peek_index rooted at the chain's topmost ancestor (ONE post-order pass). uuids: 'leaf' (F3 → `uuid` tail) | 'every-level' (F2 per-node UUID lines + Use Leaf UUID tail) | 'none'. standalone=False composes a fragment (no key/legend/tail — the WS handler's mirror-context block). Pure: callers pass the pinned map; source nodes never mutated; Dock classification runs only with the caller-supplied configured vocabulary (base_tags AND postfix kwargs — never defaulted per site).,
# ]
def build_ancestor_path_document(
    path_nodes: Sequence[Mapping[str, Any]],
    nodes_by_id: Mapping[str, Mapping[str, Any]],
    *,
    target_uuid: str | None = None,
    uuids: str = "leaf",
    markers: bool = True,
    metrics: bool = True,
    anchor_base_tags: Sequence[str] | None = None,
    anchor_postfix_kwargs: Mapping[str, Any] | None = None,
    standalone: bool = True,
) -> dict[str, Any]:
    """Compose the ancestor-chain Markdown document + structured path rows.

    ``path_nodes`` is a root→target chain of owned entries (as returned by
    :func:`walk_ancestor_chain`).  Returns ``{"markdown": str, "path":
    [row, ...]}`` where each row carries ``id, name, title, tags, parent_id,
    depth, completed, is_dock, is_favorite_dock, is_local_dock, map_state,
    metrics``.  ``map_state`` is ``None`` when ``markers=False`` (not
    evaluated) and otherwise one of ``_classify_node_map_state``'s verdicts
    (``unmapped`` / ``file`` / ``inside_file`` / ``folder``); only ``file``
    and ``folder`` earn an on-line marker — an ``inside_file`` node (an AST
    symbol or a heading inside a mapped file) is placed by its FILE/FOLDER
    ancestor's marker, exactly as the documented guidance describes.

    ``markers=False, metrics=False`` reproduces the historical F3/F2 output
    byte-for-byte (plain name lines + tail).
    """
    if not path_nodes:
        raise ValueError("ancestor path document requires a non-empty chain")
    if uuids not in UUIDS_MODES:
        raise ValueError(f"uuids must be one of {UUIDS_MODES}, got {uuids!r}")
    if anchor_base_tags is not None and anchor_postfix_kwargs is None:
        raise ValueError(
            "anchor_postfix_kwargs is required together with anchor_base_tags "
            "(pass _anchor_postfix_kwargs_from_reply(reply)): the Dock postfix "
            "vocabulary has ONE server-side home and is never defaulted per site."
        )
    if target_uuid is None:
        target_uuid = str(path_nodes[-1].get("id") or "")

    # Lazy function-local imports mirror the house pattern: nexus_map_codebase
    # imports nothing from workflowy_mcp (no cycle), and api_client_nexus is
    # reached locally to avoid the api_client_nexus->server import cycle.
    from nexus_map_codebase import split_name_and_tags  # type: ignore[import-not-found]

    from .client.api_client_nexus import _classify_node_map_state

    classify_docks = bool(markers) and bool(anchor_base_tags)

    # ONE post-order pass under the chain's topmost ancestor yields the four
    # source metrics for every chain node (each is a descendant of that root
    # by construction), with dock.peek's exact semantics and loud-failure
    # policy on malformed topology.
    metrics_by_id: Mapping[str, Mapping[str, int]] = {}
    if metrics:
        _, metrics_by_id = build_dock_peek_index(nodes_by_id, str(path_nodes[0]["id"]))

    leaf_index = len(path_nodes) - 1
    rows: list[dict[str, Any]] = []
    lines: list[str] = []
    if standalone:
        if markers:
            lines.append(MARKER_KEY_LINE)
        if metrics:
            lines.append(METRICS_LEGEND_LINE)
        if lines:
            lines.append("")

    for depth, entry in enumerate(path_nodes):
        node_id = str(entry.get("id") or "")
        name = str(entry.get("name") or "Untitled")
        source_node = nodes_by_id.get(node_id)
        title, tags = split_name_and_tags(name)
        completed = _node_is_completed(source_node)
        is_dock = is_favorite_dock = is_local_dock = False
        if classify_docks:
            verdict = classify_anchor_tags(
                tags, list(anchor_base_tags or []), **dict(anchor_postfix_kwargs or {}),
            )
            is_dock = bool(verdict.get("is_anchor"))
            is_favorite_dock = bool(verdict.get("is_favorite"))
            is_local_dock = bool(verdict.get("is_local"))
        map_state: str | None = None
        if markers:
            map_state = str(
                _classify_node_map_state(node_id, nodes_by_id).get("state") or "unmapped"
            )
        node_metrics = metrics_by_id.get(node_id) if metrics else None

        suffix = ""
        if metrics:
            suffix += format_dock_peek_metrics(node_metrics)
        if markers:
            # Compact marker order (dock.peek §6.8): ⚓ then ⭐ (only on a Dock
            # row) then Ⓛ then ✓ — extended with the mapped markers last.
            if is_dock:
                suffix += " ⚓"
                if is_favorite_dock:
                    suffix += " ⭐"
            if is_local_dock:
                suffix += " Ⓛ"
            if completed:
                suffix += " ✓"
            if map_state == "folder":
                suffix += " 🗺️"
            elif map_state == "file":
                suffix += " 📄"

        bullet = "•" if depth == leaf_index else "⦿"
        prefix = "#" * (depth + 1)
        lines.append(f"{prefix} {bullet} {name}{suffix}")
        if uuids == "every-level":
            lines.append(f"`{node_id}`")
            lines.append("")
        rows.append({
            "id": node_id,
            "name": name,
            "title": title,
            "tags": [str(tag) for tag in tags],
            "parent_id": entry.get("parent_id"),
            "depth": depth,
            "completed": completed,
            "is_dock": is_dock,
            "is_favorite_dock": is_favorite_dock,
            "is_local_dock": is_local_dock,
            "map_state": map_state,
            "metrics": dict(node_metrics) if node_metrics is not None else None,
        })

    if standalone and uuids == "leaf":
        lines.append("")
        lines.append(f"→ `{target_uuid}`")
    elif standalone and uuids == "every-level":
        lines.append("→ Use Leaf UUID:")
        lines.append(f"`{target_uuid}`")
    return {"markdown": "\n".join(lines), "path": rows}
