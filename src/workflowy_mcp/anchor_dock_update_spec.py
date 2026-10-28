"""anchor_dock_update_spec.py -- the frozen AnchorDockUpdate request spec.

The FIRST semantic concern on the section-9 client->server mutation channel
family (sibling to ``lightning_rod_tag_mutation_spec.WorkflowyTagMutationSpec``
and ``merge_slice_groups_spec.MergeSliceGroupsSpec``) -- deliberately NOT a
``RenderProjectionSpec`` (a mutation is not a render request; the render kind
taxonomy must NOT gain an 'anchor_dock_update' kind).

Where the Q4 tag spec carries raw ``#tags`` (the client is allowed to speak
tags in that one concern), THIS spec carries **intent only**: the Dock handle
plus the semantic attribute the human asked for (``favorite`` / ``local``) and,
optionally, the family base the write targets. It composes NO tag, picks NO
postfix word and reads NO node name to decide state -- the server's ONE
canonical seam ``_anchor_dock_update_impl`` owns every semantic decision
(``_attribute_transform`` -> ``_node_edit_core(_tag_transform=...)`` ->
``_compose_protected_name``). This is the crack the program exists to avoid:
riding ``mutate_node_tags`` with a client-composed ``#<base>--<fav>``.

``from_wire`` validates the TRANSPORT SHELL ONLY (K3): object, known fields,
``v == 1``, JSON types, and "at least one attribute present". It deliberately
does NOT normalize ``families`` (no strip, no '#' scrub, no de-dup, no
postfix-word resolution) and does NOT decide family presence -- the impl stays
the sole semantic authority, exactly as for the MCP ``anchor.dock.update``
path. The one shape canonicalization it performs -- folding a ``families``
string into the declared one-tuple spec type -- is type normalization, not
semantics.

PURE module: stdlib ONLY. No beacon_mutation_contract, no nexus_map_codebase,
no server-side imports -- the same purity invariant as the Q4 tag-mutation and
merge spec modules. NOTE: the server imports this module; it must never import
the server back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The §9 wire frame's known top-level fields (strict: a misspelled field must
# fail loudly, never silently drop). Mirrors _WIRE_FIELDS in the sibling specs.
_WIRE_FIELDS = {
    "action",
    "request_id",
    "endpoint_id",
    "v",
    "dock_handle",
    "favorite",
    "local",
    "families",
}
_WIRE_VERSION = 1


# @beacon[
#   id=anchor-widget@anchor-dock-update-spec,
#   slice_labels=nexus--explorer,nexus--anchors,
#   kind=ast,
#   comment=ONE normalized, immutable AnchorDockUpdate request (frozen) -- the FIRST semantic concern on the section-9 client->server mutation channel. Produced by from_wire (browser ingress); the MCP path needs no twin because anchor.dock.update passes params straight to _anchor_dock_update_impl, which owns the pure shape validation (the impl IS the shared core). Carries INTENT ONLY: dock_handle + favorite/local (set/unset) + optional families (the family base the attribute write targets). The client composes NO tag, resolves NO handle, picks NO postfix word, reads NO node name to decide state (the UI sends favorite=!is_favorite from the server-supplied row). _anchor_dock_update_impl stays the sole semantic authority -- from_wire validates the transport shell only (K3).,
# ]
@dataclass(frozen=True)
class AnchorDockUpdateSpec:
    """ONE normalized, immutable AnchorDockUpdate request (frozen).

    Fields are JSON-serializable; :meth:`to_dict` is provided for logs/tests
    and the §9 wire path.
    """

    dock_handle: str              # a UUID / unique prefix / [ap:CORE] handle
    favorite: "bool | None"       # set (True) / unset (False) / untouched (None)
    local: "bool | None"          # set (True) / unset (False) / untouched (None)
    families: "tuple[str, ...] | None"  # base tag(s) the write targets, or None

    @classmethod
    def from_wire(
        cls, data: Any
    ) -> "tuple[AnchorDockUpdateSpec | None, str | None]":
        """THE wire-ingress normalizer (the §9 client contract).

        Frame: ``{action:"anchor_dock_update", request_id, endpoint_id, v:1,
        dock_handle, favorite?, local?, families?}``. Strict: unknown
        top-level fields and any version other than 1 are rejected loudly.

        TRANSPORT SHELL ONLY (K3): validates object/fields/version/JSON types
        and "at least one attribute present". It does NOT normalize
        ``families`` (no strip, no '#' scrub, no de-dup) and does NOT decide
        family presence -- ``_anchor_dock_update_impl`` remains the sole
        semantic authority. The string->tuple fold of ``families`` is type
        normalization only.

        Returns ``(spec, None)`` on success, ``(None, error)`` on any
        rejection -- errors are returned as strings, never raised.
        """
        if not isinstance(data, dict):
            return None, "anchor_dock_update wire frame must be an object."
        extra = set(data.keys()) - _WIRE_FIELDS
        if extra:
            return None, (
                "anchor_dock_update wire frame has unknown field(s): "
                f"{sorted(extra)}."
            )
        try:
            version = int(data.get("v", _WIRE_VERSION))
        except (TypeError, ValueError):
            return None, (
                "anchor_dock_update wire frame `v` must be an integer "
                f"(got {data.get('v')!r})."
            )
        if version != _WIRE_VERSION:
            return None, (
                f"unsupported anchor_dock_update wire version {version!r} "
                f"(this server speaks v{_WIRE_VERSION})."
            )

        dock_handle = data.get("dock_handle")
        if not isinstance(dock_handle, str) or not dock_handle.strip():
            return None, (
                "anchor_dock_update wire frame `dock_handle` must be a "
                "non-empty string (a UUID, unique UUID prefix, or [ap:CORE])."
            )

        favorite = data.get("favorite")
        if favorite is not None and not isinstance(favorite, bool):
            return None, (
                "anchor_dock_update wire frame `favorite` must be a boolean "
                f"or absent (got {favorite!r})."
            )
        local = data.get("local")
        if local is not None and not isinstance(local, bool):
            return None, (
                "anchor_dock_update wire frame `local` must be a boolean or "
                f"absent (got {local!r})."
            )
        if favorite is None and local is None:
            return None, (
                "anchor_dock_update requires at least one of `favorite` or "
                "`local` (this concern carries attribute intent, not topic/"
                "description edits)."
            )

        raw_families = data.get("families")
        families: "tuple[str, ...] | None" = None
        if raw_families is not None:
            if isinstance(raw_families, str):
                # Type fold only: a lone base tag is the declared 1-tuple.
                families = (raw_families,)
            elif isinstance(raw_families, (list, tuple)):
                for entry in raw_families:
                    if not isinstance(entry, str):
                        return None, (
                            "anchor_dock_update wire frame `families` entries "
                            f"must be strings (got {entry!r})."
                        )
                families = tuple(raw_families)
            else:
                return None, (
                    "anchor_dock_update wire frame `families` must be a "
                    "string, a list of strings, or absent "
                    f"(got {raw_families!r})."
                )

        return (
            cls(
                dock_handle=dock_handle,
                favorite=favorite,
                local=local,
                families=families,
            ),
            None,
        )

    def to_dict(self) -> dict:
        """JSON-serializable form for logs/tests/wire echoes (§9)."""
        return {
            "dock_handle": self.dock_handle,
            "favorite": self.favorite,
            "local": self.local,
            "families": list(self.families) if self.families is not None else None,
        }
