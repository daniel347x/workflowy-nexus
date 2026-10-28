"""lightning_rod_tag_mutation_spec.py -- the frozen Workflowy-tag mutation request spec.

SECOND SIBLING to lightning_rod_mutation_spec.LightningRodMutationSpec (the
Q3 disk-beacon mutation family) and cache_render_spec.RenderProjectionSpec
(render) -- deliberately reusing NEITHER (SPEC v5 §1 invariant 1:
RENDER/MUTATION SEPARATION, extended by the Q4 audit: beacon mutation and
Workflowy-tag mutation are SEPARATE spec families). Render specs flow through
Stage 1/2, the modal, and the client wire; mutation never may.

This is the Q4 cell of the Lightning Rod 2x2: batch add/remove/rename of
Workflowy #tags on NODES -- each node addressed by a UUID (exact or unique
prefix) OR an ``[ap:CORE]`` / ``ap:CORE`` anchor-point handle -- as opposed
to Q3's disk-side beacon slice-label mutation on source symbols.

All Q4 mutation ingress funnels through the ONE normalizer core
``_normalize_operations`` (invariant 2: ONE NORMALIZER PER REQUEST KIND),
via two thin classmethod fronts:

  * :meth:`WorkflowyTagMutationSpec.normalize_from_tool_args` -- the
    ``lightning_rod`` MCP tool boundary (+ the orchestrator's fallback for
    direct internal callers);
  * :meth:`WorkflowyTagMutationSpec.from_wire` -- the §9 versioned client
    wire contract: ``{action:"mutate_node_tags", request_id, endpoint_id,
    v:1, operations:[...]}``.

Both delegate to the same core so the two transports can NEVER drift on the
grammar (STAGE-0 DOCTRINE: interpretation unified; transport per-origin).

PURE module: stdlib ONLY. NO beacon_mutation_contract import (comments are a
beacon-only concept), NO nexus_map_codebase (the dependence-graph
invariant, §2). NOTE: the tiny ``_coerce_tag_list`` token normalizer
deliberately MIRRORS the one in lightning_rod_mutation_spec.py
(byte-identical semantics) -- importing the beacon module would transitively
pull beacon_mutation_contract (the comment machinery) and break the purity
invariant; the module-purity constraint wins over the no-duplication
heuristic for this one 20-line pure function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# The Q4 mode set. Kept DISTINCT from the beacon family's MUTATION_MODES in
# lightning_rod_mutation_spec.py so the tool boundary / orchestrator can keep
# the two families explicit (post-audit ruling: separate spec families).
WORKFLOWY_TAG_MUTATION_MODES = ("mutate_node_tags",)

_TAG_MUTATION_OPERATIONS = ("add", "remove", "rename")

# The §9 wire frame's known top-level fields (strict: a misspelled field must
# fail loudly, never silently drop).
_WIRE_FIELDS = {"action", "request_id", "endpoint_id", "v", "operations"}
_WIRE_VERSION = 1


def _coerce_tag_list(value: Any) -> list[str]:
    """Normalize a tag/list input to '#'-less tokens; raises ValueError.

    Deliberate mirror of lightning_rod_mutation_spec._coerce_tag_list (see
    the module docstring for why this is not an import).
    """
    if value is None:
        return []
    raw_items = [value] if isinstance(value, str) else value
    if not isinstance(raw_items, list):
        raise ValueError("tags must be a string or list of strings")
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        s = str(raw or "").strip()
        if not s:
            continue
        s = s.lstrip("#").strip()
        if not s:
            continue
        if any(ch.isspace() for ch in s) or "#" in s:
            raise ValueError(f"invalid tag token {raw!r}; pass one tag per token")
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# @beacon[
#   id=lightning-rod@tag-mutation-operation,
#   slice_labels=nexus--tags,
#   kind=ast,
#   comment=ONE normalized Q4 Workflowy-tag mutation operation (frozen). handle is the raw input item (a Workflowy UUID / UUID prefix / [ap:CORE] anchor-point handle -- resolution happens LATER, in the orchestrator via the Layer-3 universal resolver, never here). operation is 'add' | 'remove' | 'rename'. tags / old_tag / new_tag are '#'-less tokens (tags empty for rename; old/new None for add/remove).,
# ]
@dataclass(frozen=True)
class TagMutationOperation:
    """ONE normalized Q4 Workflowy-tag mutation operation (frozen)."""

    input_index: int
    handle: str
    operation: str  # "add" | "remove" | "rename"
    tags: tuple  # '#'-less tokens; empty for rename
    old_tag: "str | None"  # rename only
    new_tag: "str | None"  # rename only

    def to_dict(self) -> dict:
        return {
            "input_index": self.input_index,
            "handle": self.handle,
            "operation": self.operation,
            "tags": list(self.tags),
            "old_tag": self.old_tag,
            "new_tag": self.new_tag,
        }


# @beacon[
#   id=lightning-rod@_normalize_operations,
#   slice_labels=nexus--tags,
#   kind=ast,
#   comment=THE ONE Q4 operations-grammar normalizer core (STAGE-0 DOCTRINE: interpretation unified, transport per-origin). Consumed by BOTH ingress fronts -- WorkflowyTagMutationSpec.normalize_from_tool_args (MCP tool args) and .from_wire (the section-9 client wire frame) -- so the two transports can never drift. Grammar: operations is a non-empty list of {handle, operation:'add'|'remove', tags:[...]} or {handle, operation:'rename', old_tag, new_tag} objects; unknown fields rejected per-operation-kind; duplicate handles rejected; '#'-prefix optional (canonicalized '#'-less); internal-whitespace tags rejected. Returns (tuple-of-TagMutationOperation, None) or (None, error-string) -- errors are returned, never raised, so boundaries produce structured failures. Pure; no I/O.,
# ]
def _normalize_operations(raw_ops: Any) -> "tuple[tuple | None, str | None]":
    """Normalize the Q4 granular ``operations`` list (the ONLY input form).

    Returns ``(operations_tuple, None)`` on success, ``(None, error)`` on any
    rejection.
    """
    if not isinstance(raw_ops, list) or not raw_ops:
        return None, (
            "operations must be a non-empty list of "
            "{handle, operation, tags? | old_tag?+new_tag?} objects."
        )
    ops: list[TagMutationOperation] = []
    seen_handles: set[str] = set()
    for idx, op in enumerate(raw_ops):
        if not isinstance(op, dict):
            return None, f"operations[{idx}] must be an object."
        verb = str(op.get("operation") or "").strip().lower()
        if not verb:
            return None, (
                f"operations[{idx}].operation is required "
                "('add' | 'remove' | 'rename')."
            )
        if verb not in _TAG_MUTATION_OPERATIONS:
            return None, (
                f"operations[{idx}].operation {op.get('operation')!r} is "
                "unknown; use 'add', 'remove', or 'rename'."
            )
        allowed = (
            {"handle", "operation", "tags"}
            if verb in ("add", "remove")
            else {"handle", "operation", "old_tag", "new_tag"}
        )
        extra = set(op.keys()) - allowed
        if extra:
            return None, (
                f"operations[{idx}] has unknown field(s): {sorted(extra)}."
            )
        handle = str(op.get("handle") or "").strip()
        if not handle:
            return None, (
                f"operations[{idx}].handle is required (a Workflowy UUID "
                "or [ap:CORE] anchor-point handle)."
            )
        if handle in seen_handles:
            return None, (
                f"duplicate operation for handle {handle!r}; pass each "
                "handle once."
            )
        seen_handles.add(handle)
        try:
            if verb in ("add", "remove"):
                op_tags = _coerce_tag_list(op.get("tags"))
                if not op_tags:
                    return None, f"operations[{idx}].tags must be a non-empty list."
                ops.append(
                    TagMutationOperation(
                        input_index=idx,
                        handle=handle,
                        operation=verb,
                        tags=tuple(op_tags),
                        old_tag=None,
                        new_tag=None,
                    )
                )
            else:  # rename
                old_tokens = _coerce_tag_list(op.get("old_tag"))
                new_tokens = _coerce_tag_list(op.get("new_tag"))
                if not old_tokens:
                    return None, f"operations[{idx}].old_tag is required for rename."
                if not new_tokens:
                    return None, f"operations[{idx}].new_tag is required for rename."
                ops.append(
                    TagMutationOperation(
                        input_index=idx,
                        handle=handle,
                        operation=verb,
                        tags=(),
                        old_tag=old_tokens[0],
                        new_tag=new_tokens[0],
                    )
                )
        except ValueError as e:
            return None, f"operations[{idx}]: {e}"
    return tuple(ops), None


# @beacon[
#   id=lightning-rod@tag-mutation-spec,
#   slice_labels=nexus--tags,
#   kind=ast,
#   comment=ONE normalized, immutable Q4 Workflowy-tag mutation request (frozen). Produced ONLY by normalize_from_tool_args (MCP ingress) or from_wire (the section-9 versioned client frame) -- both delegating to the ONE _normalize_operations core (invariant 2). The tool-boundary front ALSO rejects the beacon-only / render-only / scoping args at the grammar level: comment (beacon-only concept), symbols (Q3's quadrant), top-level tags (the beacon shorthand), root_uuid/root_uuids (handles are self-scoping -- Q3 needs roots only to scope SYMBOL resolution), favor='symbol' (no symbols here). Handles are NOT shape-validated here (a UUID / prefix / [ap:CORE] / bare-core all pass through) -- resolution + the bare-core rejection happen in the orchestrator via the Layer-3 universal resolver, exactly like the beacon spec defers handle resolution.,
# ]
@dataclass(frozen=True)
class WorkflowyTagMutationSpec:
    """ONE normalized, immutable Q4 Workflowy-tag mutation request (frozen).

    Fields are JSON-serializable; :meth:`to_dict` is provided for logs/tests
    and the §9 wire path.
    """

    mode: Literal["mutate_node_tags"]
    operations: tuple  # tuple[TagMutationOperation, ...]

    @classmethod
    def normalize_from_tool_args(
        cls,
        *,
        mode: Any,
        operations: Any,
        comment: Any,
        symbols: Any,
        tags: Any,
        root_uuids: Any,
        root_uuid: Any,
        favor: Any,
    ) -> "tuple[WorkflowyTagMutationSpec | None, str | None]":
        """THE ONE MCP-ingress normalizer for Q4 -- every tool-arg mutation
        ingress funnels here (the tool boundary, plus the orchestrator's
        fallback for direct internal callers).

        Returns ``(spec, None)`` on success, ``(None, error)`` on any
        rejection -- errors are returned as strings, never raised.
        """
        _mode = str(mode or "").strip().lower()
        if _mode not in WORKFLOWY_TAG_MUTATION_MODES:
            return None, f"internal error: unexpected tag-mutation mode {mode!r}"

        # ---- Boundary-level argument rejections (the Q4 grammar owns these,
        # so the two transports + the orchestrator fallback stay identical) --
        if comment is not None:
            return None, (
                f"{_mode} does not accept `comment` (comments are a "
                "beacon-only concept; ensure_beacons/remove_beacon_tags only)."
            )
        if bool([s for s in (symbols or []) if str(s).strip()]):
            return None, (
                f"{_mode} mutates Workflowy nodes by handle (UUID or "
                "[ap:CORE]); source-code symbols belong to the disk-beacon "
                "modes (ensure_beacons/remove_beacon_tags)."
            )
        if tags is not None:
            return None, (
                f"{_mode} is operations-only: pass per-operation `tags` "
                "inside each operations item. Top-level tags are the "
                "beacon-mode shorthand only."
            )
        _roots_multi = [
            str(r).strip() for r in (root_uuids or [])
            if str(r or "").strip()
        ] if isinstance(root_uuids, (list, tuple)) else (
            [root_uuids.strip()] if str(root_uuids or "").strip() else []
        )
        if _roots_multi or str(root_uuid or "").strip():
            return None, (
                f"{_mode} does not use root_uuid/root_uuids -- each handle "
                "(UUID or [ap:CORE]) is self-scoping."
            )
        _favor = str(favor or "uuid").strip().lower()
        if _favor != "uuid":
            return None, (
                f"{_mode} resolves handles UUID-first (favor='symbol' is "
                "meaningless: symbols are not accepted)."
            )

        ops, err = _normalize_operations(operations)
        if err:
            return None, err
        return cls(mode=_mode, operations=ops), None  # type: ignore[arg-type]

    @classmethod
    def from_wire(cls, data: Any) -> "tuple[WorkflowyTagMutationSpec | None, str | None]":
        """THE ONE wire-ingress normalizer (the §9 client contract).

        Frame: ``{action:"mutate_node_tags", request_id, endpoint_id, v:1,
        operations:[...]}``. Strict: unknown top-level fields and any
        version other than 1 are rejected loudly. The operations grammar is
        the SAME ``_normalize_operations`` core the MCP boundary uses.

        Returns ``(spec, None)`` on success, ``(None, error)`` on any
        rejection.
        """
        if not isinstance(data, dict):
            return None, "mutate_node_tags wire frame must be an object."
        extra = set(data.keys()) - _WIRE_FIELDS
        if extra:
            return None, (
                f"mutate_node_tags wire frame has unknown field(s): "
                f"{sorted(extra)}."
            )
        try:
            version = int(data.get("v", _WIRE_VERSION))
        except (TypeError, ValueError):
            return None, f"mutate_node_tags wire frame `v` must be an integer (got {data.get('v')!r})."
        if version != _WIRE_VERSION:
            return None, (
                f"unsupported mutate_node_tags wire version {version!r} "
                f"(this server speaks v{_WIRE_VERSION})."
            )
        ops, err = _normalize_operations(data.get("operations"))
        if err:
            return None, err
        return cls(mode=WORKFLOWY_TAG_MUTATION_MODES[0], operations=ops), None

    def to_dict(self) -> dict:
        """JSON-serializable form for logs/tests/wire echoes (§9)."""
        return {
            "mode": self.mode,
            "operations": [op.to_dict() for op in self.operations],
        }
