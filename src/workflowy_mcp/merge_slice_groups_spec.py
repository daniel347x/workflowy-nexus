"""merge_slice_groups_spec.py -- the frozen MergeSliceGroups mutation request spec.

Sibling to lightning_rod_tag_mutation_spec.WorkflowyTagMutationSpec (the Q4
Workflowy-tag mutation spec family) and cache_render_spec.RenderProjectionSpec
(the render spec).  This is a MUTATION spec (NOT a render spec) -- it belongs
to the Q4 / WorkflowyTagMutationSpec family, deliberately NOT to
RenderProjectionSpec (a merge is not a render request; RenderProjectionSpec's
kind taxonomy must NOT gain a 'merge' kind).

The merge-spec carries the two STAGE-1 admission knobs the per-group
projections need (exclude_completed + match_scope), read off it by the
orchestrator -- the same "Stage 1 reads fields off the spec" doctrine
ARCHMM2 §0 describes, with the MUTATION spec as the request object.

target_tab is deliberately NOT a spec field (it is TRANSPORT ROUTING, not merge
identity -- A10).  The orchestrator takes it as a separate argument.

PURE module: stdlib ONLY.  No beacon_mutation_contract, no nexus_map_codebase,
no server-side imports -- the same purity invariant as the Q4 tag-mutation
spec module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# -- wire frame fields (strict: a misspelled field must fail loudly) ----------
# timeout_seconds is a LIGHTWEIGHT INTERIM transport field: the optional
# caller-declared client-side wait deadline (e.g. set near a UI button: "how
# long are you willing to wait?"). Like request_id/endpoint_id it is transport
# metadata, NOT merge identity -- it is tolerated + shape-validated here but is
# deliberately NEVER a MergeSliceGroupsSpec field (A10 doctrine), and the
# server does not consume it today (the orchestrator runs to completion; a
# client that stops waiting simply re-invokes -- congruent-resume). SLATED FOR
# REMOVAL when merge_slice_groups becomes an async CARTO job (the
# workflowy_etch_async precedent), at which point waiting is job polling, not
# a request deadline. Touch points: requestMergeSliceGroups (JS, the ONE
# writer) and from_wire (the ONE reader/tolerator) -- nothing else.
_WIRE_FIELDS = {
    "action", "request_id", "endpoint_id", "v", "groups", "mode", "tag",
    "timeout_seconds",
}
_WIRE_VERSION = 1

_MERGE_MODES = ("union", "intersection")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _strip_hash(s: str) -> str:
    """Strip one leading ``#`` and trim whitespace."""
    s = s.strip()
    if s.startswith("#"):
        s = s[1:].strip()
    return s


# ---------------------------------------------------------------------------
# MergeGroupRef -- one normalized group reference (named OR dynamic)
# ---------------------------------------------------------------------------
# @beacon[
#   id=merge@group-ref,
#   slice_labels=nexus--tags,slices,
#   kind=ast,
#   comment=ONE normalized merge-group reference (frozen). kind='named' -> handle is a persisted slice-group id-or-name (resolved LATER by the orchestrator via _resolve_slice_group_handle). kind='dynamic' -> root_uuid + tags define an inline slice-group definition (the SAME shape Lightning Rod's dynamic slice definitions take). tags are '#'-less base stems (postfix variants collapsed by the normalizer). The handle field is named 'handle' (not 'name') per A10: it accepts an id OR a name string.,
# ]
@dataclass(frozen=True)
class MergeGroupRef:
    """ONE normalized merge-group reference (frozen)."""

    kind: Literal["named", "dynamic"]
    handle: "str | None"   # named: the group id-or-name; dynamic: None
    root_uuid: "str | None"  # dynamic: the root UUID; named: None (resolved later)
    tags: tuple             # dynamic: '#'-less tag stems; named: ()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "handle": self.handle,
            "root_uuid": self.root_uuid,
            "tags": list(self.tags),
        }


# ---------------------------------------------------------------------------
# _normalize_group_refs -- the ONE normalizer core (both transports)
# ---------------------------------------------------------------------------
# @beacon[
#   id=merge@normalize-group-refs,
#   slice_labels=nexus--tags,slices,
#   kind=ast,
#   comment=THE ONE group-refs normalizer core (STAGE-0 DOCTRINE: interpretation unified, transport per-origin). Consumed by BOTH ingress fronts -- MergeSliceGroupsSpec.normalize_from_tool_args (MCP tool args) and .from_wire (the section-9 client wire frame) -- so the two transports can never drift. Grammar: groups is a list of 2+ items; a STRING item is a NAMED group (id-or-name); an OBJECT {root_uuid, tags} item is a DYNAMIC group (inline definition, the SAME shape Lightning Rod's dynamic slice definitions take). tags on a dynamic ref are '#'-stripped, whitespace-trimmed, de-duped, and any '--<postfix>' tail is rejected (the orchestrator normalizes against the LIVE postfix words later; the spec stores plain bases). Returns (tuple-of-MergeGroupRef, None) or (None, error-string) -- errors returned, never raised.,
# ]
def _normalize_group_refs(
    raw: Any,
) -> "tuple[tuple[MergeGroupRef, ...] | None, str | None]":
    """Normalize the groups list (the ONE core for both transports).

    Returns ``(tuple, None)`` on success, ``(None, error)`` on any rejection.
    """
    if not isinstance(raw, list) or len(raw) < 2:
        return None, "groups must be a list of at least 2 items."

    refs: list[MergeGroupRef] = []
    for idx, item in enumerate(raw):
        if isinstance(item, str):
            handle = item.strip()
            if not handle:
                return None, f"groups[{idx}]: string handle must be non-empty."
            refs.append(
                MergeGroupRef(
                    kind="named",
                    handle=handle,
                    root_uuid=None,
                    tags=(),
                )
            )
        elif isinstance(item, dict):
            extra = set(item.keys()) - {"root_uuid", "tags"}
            if extra:
                return None, f"groups[{idx}]: unknown field(s): {sorted(extra)}."
            r = str(item.get("root_uuid") or "").strip()
            if not r:
                return None, (
                    f"groups[{idx}]: dynamic group requires a non-empty "
                    "root_uuid."
                )
            raw_tags = item.get("tags")
            if raw_tags is None:
                return None, (
                    f"groups[{idx}]: dynamic group requires a tags field."
                )
            # Normalize tags: comma-separated string OR list; '#'-optional;
            # whitespace/internal-# rejected; '--<postfix>' tails rejected
            # (the orchestrator normalizes against live postfix words later;
            # the spec stores plain bases).
            if isinstance(raw_tags, str):
                tokens = [t.strip() for t in raw_tags.split(",") if t.strip()]
            elif isinstance(raw_tags, (list, tuple)):
                tokens = [str(t).strip() for t in raw_tags if str(t).strip()]
            else:
                return None, f"groups[{idx}].tags must be a string or list."
            norm_tags: list[str] = []
            seen: set[str] = set()
            for tok in tokens:
                s = _strip_hash(tok)
                if not s:
                    continue
                if any(ch.isspace() for ch in s) or "#" in s:
                    return None, (
                        f"groups[{idx}].tags: invalid tag token {tok!r}."
                    )
                if "--" in s:
                    return None, (
                        f"groups[{idx}].tags: tag {tok!r} carries a '--' "
                        "postfix tail; pass plain base stems (the orchestrator "
                        "normalizes against the live postfix words)."
                    )
                if s not in seen:
                    seen.add(s)
                    norm_tags.append(s)
            if not norm_tags:
                return None, (
                    f"groups[{idx}].tags must yield at least one non-empty "
                    "tag stem."
                )
            refs.append(
                MergeGroupRef(
                    kind="dynamic",
                    handle=None,
                    root_uuid=r,
                    tags=tuple(norm_tags),
                )
            )
        else:
            return None, (
                f"groups[{idx}]: must be a string (named group) or object "
                "(dynamic group)."
            )

    return tuple(refs), None


# ---------------------------------------------------------------------------
# MergeSliceGroupsSpec -- the frozen request
# ---------------------------------------------------------------------------
# @beacon[
#   id=merge@merge-spec,
#   slice_labels=nexus--tags,slices,
#   kind=ast,
#   comment=ONE normalized, immutable MergeSliceGroups mutation request (frozen). Produced ONLY by normalize_from_tool_args (MCP ingress) or from_wire (the section-9 versioned client frame) -- both delegating to the ONE _normalize_group_refs core. NOT a RenderProjectionSpec (a merge is NOT a render request; the render kind taxonomy must NOT gain a 'merge' kind -- it is the Q4/WorkflowyTagMutationSpec mutation family). Carries the two STAGE-1 admission knobs the per-group projections need (exclude_completed default True = SERVER_SIDE_GLIMPSE_EXCLUDE_COMPLETED; match_scope default 'name' -- a semantic tag search reads the NAME's trailing tag region via the core splitter, so a mid-title prose '#tag' or a note-only '#tag' does not join the merge projection; pass 'name_and_note' for Workflowy-search parity) -- read OFF the spec by the orchestrator (the "Stage 1 reads fields off the spec" doctrine, with the MUTATION spec as the request object). target_tab is deliberately NOT a spec field (it is transport routing, not merge identity -- A10); the orchestrator takes it as a separate argument.,
# ]
@dataclass(frozen=True)
class MergeSliceGroupsSpec:
    """ONE normalized, immutable MergeSliceGroups mutation request (frozen).

    Fields are JSON-serializable; :meth:`to_dict` is provided for logs/tests
    and the §9 wire path.
    """

    groups: tuple               # tuple[MergeGroupRef, ...]
    mode: str                   # 'union' | 'intersection'
    tag: str                    # single target tag, '#'-less, no postfix tail
    exclude_completed: bool     # default True (SERVER_SIDE_GLIMPSE_EXCLUDE_COMPLETED)
    match_scope: str            # default 'name' (trailing-region semantic tag search)

    @classmethod
    def normalize_from_tool_args(
        cls,
        *,
        groups: Any,
        mode: Any,
        tag: Any,
    ) -> "tuple[MergeSliceGroupsSpec | None, str | None]":
        """THE ONE MCP-ingress normalizer for merge_slice_groups.

        Returns ``(spec, None)`` on success, ``(None, error)`` on any
        rejection -- errors are returned as strings, never raised.
        """
        refs, err = _normalize_group_refs(groups)
        if err:
            return None, err

        _mode = str(mode or "").strip().lower()
        if _mode not in _MERGE_MODES:
            return None, f"mode must be 'union' or 'intersection' (got {mode!r})."

        raw_tag = str(tag or "").strip()
        if not raw_tag:
            return None, "tag is required (exactly one base tag)."
        norm_tag = _strip_hash(raw_tag)
        if not norm_tag:
            return None, "tag is required (exactly one base tag)."
        if any(ch.isspace() for ch in norm_tag) or "#" in norm_tag:
            return None, (
                f"tag must be a single token with no internal whitespace or "
                f"'#'."
            )
        # Reject tags carrying a '--' postfix tail -- the tool derives the
        # expand variant itself from the LIVE auto-expand word.
        if "--" in norm_tag:
            return None, (
                f"tag {norm_tag!r} carries a '--' postfix tail; pass a plain "
                "base tag (the tool derives the expand variant itself from the "
                "live auto-expand word)."
            )

        return cls(
            groups=refs,
            mode=_mode,
            tag=norm_tag,
            exclude_completed=True,   # SERVER_SIDE_GLIMPSE_EXCLUDE_COMPLETED
            match_scope="name",
        ), None

    @classmethod
    def from_wire(cls, data: Any) -> "tuple[MergeSliceGroupsSpec | None, str | None]":
        """THE ONE wire-ingress normalizer (the §9 client contract).

        Frame: ``{action:"merge_slice_groups", request_id, endpoint_id, v:1,
        groups, mode, tag}``. Strict: unknown top-level fields and any
        version other than 1 are rejected loudly. An optional
        ``timeout_seconds`` (the caller-declared client wait deadline) is
        tolerated as INTERIM transport metadata -- shape-validated, never
        stored on the spec (see _WIRE_FIELDS). The groups grammar is
        the SAME ``_normalize_group_refs`` core the MCP boundary uses.

        Returns ``(spec, None)`` on success, ``(None, error)`` on any
        rejection.
        """
        if not isinstance(data, dict):
            return None, "merge_slice_groups wire frame must be an object."
        extra = set(data.keys()) - _WIRE_FIELDS
        if extra:
            return None, (
                f"merge_slice_groups wire frame has unknown field(s): "
                f"{sorted(extra)}."
            )
        try:
            version = int(data.get("v", _WIRE_VERSION))
        except (TypeError, ValueError):
            return None, (
                f"merge_slice_groups wire frame `v` must be an integer "
                f"(got {data.get('v')!r})."
            )
        if version != _WIRE_VERSION:
            return None, (
                f"unsupported merge_slice_groups wire version {version!r} "
                f"(this server speaks v{_WIRE_VERSION})."
            )
        # LIGHTWEIGHT INTERIM transport field (see _WIRE_FIELDS): tolerate an
        # optional caller-declared client wait deadline. Shape-validated here;
        # NEVER stored on the spec (A10: transport metadata, not merge
        # identity) and not consumed server-side today. stdlib-only checks
        # (this module's purity invariant).
        _to = data.get("timeout_seconds")
        if _to is not None and (
            isinstance(_to, bool) or not isinstance(_to, (int, float)) or not (_to > 0)
        ):
            return None, (
                f"merge_slice_groups wire frame `timeout_seconds` must be a "
                f"positive number of seconds (got {_to!r})."
            )
        return cls.normalize_from_tool_args(
            groups=data.get("groups"),
            mode=data.get("mode"),
            tag=data.get("tag"),
        )

    def to_dict(self) -> dict:
        """JSON-serializable form for logs/tests/wire echoes (§9)."""
        return {
            "groups": [ref.to_dict() for ref in self.groups],
            "mode": self.mode,
            "tag": self.tag,
            "exclude_completed": self.exclude_completed,
            "match_scope": self.match_scope,
        }
