"""Machine-readable audit of the S10 legacy-to-consolidated MCP cutover."""

from __future__ import annotations

from dataclasses import dataclass


LEGACY_TOOL_NAMES = (
    "workflowy_edit_node",
    "workflowy_add_tag",
    "workflowy_remove_tag",
    "workflowy_rename_tag",
    "workflowy_read_node",
    "workflowy_delete_node",
    "workflowy_complete_node",
    "workflowy_uncomplete_node",
    "workflowy_move_node",
    "workflowy_export_node",
    "nexus_ping",
    "workflowy_broker_status",
    "workflowy_refresh_nodes_export_cache",
    "mcp_job_status",
    "mcp_cancel_job",
    "merge_slice_groups",
    "workflowy_glimpse",
    "list_anchors",
    "list_canonical_tags",
    "create_anchor",
    "update_anchor",
    "add_to_anchor",
    "workflowy_scry",
    "lightning_rod",
    "workflowy_etch",
    "workflowy_etch_async",
    "read_text_snippet",
    "read_text_snippet_by_uuid",
    "read_text_snippet_by_symbol",
    "read_uuid_from_symbol",
    "read_uuid_from_anchor_point",
    "update_beacon_from_node",
    "cartographer_refresh_async",
    "list_slice_groups",
    "get_slice_group",
    "create_slice_group",
    "update_slice_group",
)

CONSOLIDATED_TOOL_NAMES = (
    "anchor",
    "beacon",
    "glimpse",
    "lightning_rod",
    "node",
    "slice_group",
    "system",
    "tag",
)

LIGHTNING_ROD_LEGACY_SELECTORS = (
    "mode=gather",
    "mode=render_tags",
    "mode=render_search",
    "mode=render_symbols",
    "mode=ensure_beacons",
    "mode=remove_beacon_tags",
    "mode=mutate_node_tags",
)


@dataclass(frozen=True)
class LegacyRoute:
    """One selector-qualified route from a legacy tool to one canonical operation."""

    target: str
    selector: str | None = None


@dataclass(frozen=True)
class LegacyToolMapping:
    """One top-level legacy tool disposition and its complete capability routes."""

    legacy_tool: str
    disposition: str
    routes: tuple[LegacyRoute, ...]
    deltas: tuple[str, ...] = ()

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(route.target for route in self.routes)


def _mapped(
    legacy_tool: str,
    target: str,
    *deltas: str,
) -> LegacyToolMapping:
    return LegacyToolMapping(
        legacy_tool=legacy_tool,
        disposition="mapped",
        routes=(LegacyRoute(target=target),),
        deltas=tuple(deltas),
    )


# @beacon[
#   id=mcp-api@legacy-tool-mapping,
#   role=LEGACY_TOOL_MAPPING,
#   slice_labels=nexus--mcp-api,
#   kind=ast,
#   comment=Complete 37-tool capability audit for the mutually-exclusive legacy and consolidated FastMCP registration profiles; mega-tool selectors are explicit so no mode can hide inside a tool-level row,
# ]
LEGACY_TOOL_MAPPING = (
    _mapped("workflowy_edit_node", "node.edit"),
    _mapped("workflowy_add_tag", "tag.add"),
    _mapped("workflowy_remove_tag", "tag.remove"),
    _mapped("workflowy_rename_tag", "tag.rename"),
    _mapped("workflowy_read_node", "node.read"),
    _mapped("workflowy_delete_node", "node.delete"),
    _mapped("workflowy_complete_node", "node.complete"),
    _mapped("workflowy_uncomplete_node", "node.uncomplete"),
    _mapped("workflowy_move_node", "node.move"),
    _mapped("workflowy_export_node", "node.export"),
    _mapped("nexus_ping", "system.ping"),
    _mapped("workflowy_broker_status", "system.broker.status"),
    _mapped("workflowy_refresh_nodes_export_cache", "system.cache.refresh"),
    _mapped("mcp_job_status", "system.job.status"),
    _mapped("mcp_cancel_job", "system.job.cancel"),
    _mapped(
        "merge_slice_groups",
        "slice_group.merge.persist",
        "Flat legacy groups become a recursive LensSpec consumed by the new orchestrator, not a re-front of the old function.",
        "The consolidated persist operation requires one real LCA/root; the legacy cross-top-level rootless generated group was not usable or persisted.",
    ),
    _mapped(
        "workflowy_glimpse",
        "glimpse.run",
        "Public tool rename plus verb/params envelope conversion; the zero-argument live-DOM default is preserved.",
        "root_handle, group_handle, and tail are canonical; root_uuid, slice_group, and since remain transition aliases.",
    ),
    _mapped("list_anchors", "anchor.dock.list"),
    _mapped("list_canonical_tags", "system.config.get"),
    _mapped(
        "create_anchor",
        "anchor.dock.create",
        "Legacy description meant topic and detail meant description; consolidated topic/description are a semantic shift with deliberately no parameter aliases.",
    ),
    _mapped(
        "update_anchor",
        "anchor.dock.update",
        "Legacy description meant topic and detail meant description; consolidated topic/description are a semantic shift with deliberately no parameter aliases.",
    ),
    _mapped("add_to_anchor", "anchor.point.capture"),
    _mapped("workflowy_scry", "node.scry"),
    LegacyToolMapping(
        legacy_tool="lightning_rod",
        disposition="split_by_mode",
        routes=(
            LegacyRoute("lightning_rod.gather", "mode=gather"),
            LegacyRoute("lightning_rod.render_tags", "mode=render_tags"),
            LegacyRoute("lightning_rod.render_search", "mode=render_search"),
            LegacyRoute("lightning_rod.render_symbols", "mode=render_symbols"),
            LegacyRoute("beacon.tags.ensure", "mode=ensure_beacons"),
            LegacyRoute("beacon.tags.remove", "mode=remove_beacon_tags"),
            LegacyRoute("tag.batch", "mode=mutate_node_tags"),
        ),
        deltas=(
            "The consolidated Lightning Rod family is render-only; permanent mutation modes move to Beacon and Tag.",
        ),
    ),
    _mapped("workflowy_etch", "node.etch", "execution defaults to synchronous."),
    _mapped(
        "workflowy_etch_async",
        "node.etch",
        "Pass execution='async'; the result uses the normalized family job envelope.",
    ),
    _mapped(
        "read_text_snippet",
        "beacon.snippet",
        "One source_handle replaces auto-detected legacy input; canonical beacon.snippet numbers and length-guards its output (context defaults to 0); pass numbering='none', max_lines=0, context=20 for the legacy raw text.",
    ),
    _mapped(
        "read_text_snippet_by_uuid",
        "beacon.snippet",
        "Mapped-node UUID is supplied as source_handle; canonical beacon.snippet numbers and length-guards its output (context defaults to 0); pass numbering='none', max_lines=0, context=20 for the legacy raw text.",
    ),
    _mapped(
        "read_text_snippet_by_symbol",
        "beacon.snippet",
        "Mapped source symbol is supplied as source_handle; canonical beacon.snippet numbers and length-guards its output (context defaults to 0); pass numbering='none', max_lines=0, context=20 for the legacy raw text.",
    ),
    _mapped("read_uuid_from_symbol", "beacon.resolve"),
    _mapped("read_uuid_from_anchor_point", "anchor.point.resolve"),
    _mapped(
        "update_beacon_from_node",
        "beacon.sync_node",
        "The consolidated operation accepts node_handle only and performs a live server-side semantic fetch.",
        "It is disk-only: no automatic refresh and no cache patch; reconcile with beacon.map.refresh.",
        "It returns the standardized family acknowledgement envelope.",
    ),
    _mapped(
        "cartographer_refresh_async",
        "beacon.map.refresh",
        "Adds handle grace, map_target_invalid preflight, and the normalized job envelope.",
    ),
    _mapped("list_slice_groups", "slice_group.list"),
    _mapped("get_slice_group", "slice_group.get"),
    _mapped("create_slice_group", "slice_group.create"),
    _mapped(
        "update_slice_group",
        "slice_group.update",
        "group_handle owns id/name resolution; canonical name is the replacement name rather than the legacy resolver/new_name split.",
    ),
)


def mapping_by_legacy_name() -> dict[str, LegacyToolMapping]:
    """Return the immutable table indexed by legacy public tool name."""
    return {row.legacy_tool: row for row in LEGACY_TOOL_MAPPING}
