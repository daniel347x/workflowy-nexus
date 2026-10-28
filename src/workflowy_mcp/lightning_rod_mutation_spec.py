"""lightning_rod_mutation_spec.py -- the frozen mutation request spec.

SIBLING pattern to cache_render_spec.RenderProjectionSpec -- deliberately NOT
reusing it (SPEC v5 §1 invariant 1: RENDER/MUTATION SEPARATION). Render specs
flow through Stage 1/2, the modal, and the client wire; mutation never may.

All mutation ingress (the ``lightning_rod`` MCP tool boundary today; a future
client trigger per §9) funnels through the ONE normalizer
:meth:`LightningRodMutationSpec.normalize_from_tool_args`, producing ONE
frozen spec. PURE module (like lightning_rod_search.py): imports ONLY
beacon_mutation_contract + stdlib. It must NEVER import nexus_map_codebase
(the dependence-graph invariant, §2).

Also homed here: :class:`BeaconWritePlan`, the executable per-item commit
operand produced by the plan seam (§5) and consumed by the commit seam (§6)
via duck-typed field access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from beacon_mutation_contract import (
    COMMENT_MUTATION_MODES,
    BeaconCommentIntent,
    BeaconWriteIntent,
    validate_beacon_comment_text,
)

MUTATION_MODES = ("ensure_beacons", "remove_beacon_tags")


def _coerce_tag_list(value: Any) -> list[str]:
    """Normalize a tag/list input to '#'-less tokens; raises ValueError."""
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


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@BeaconMutationOperation-pnha,
#   role=BeaconMutationOperation,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Frozen identifiable item keeps input position local validation error and safe-label comment field outcomes for the ordered batch ledger.,
# ]
class BeaconMutationOperation:
    """ONE identifiable item, including local validation and field outcomes.

    ``handle`` is the raw input item (a symbol / UUID / UUID prefix / AP-ID
    -- resolution happens LATER, in the shared resolver core, never here).
    ``tags`` are '#'-less tokens. ``comment`` is the typed intent
    (``preserve`` when no comment was requested).
    """

    input_index: int
    handle: str
    tags: tuple
    comment: BeaconCommentIntent
    item_error: Optional[str] = None
    field_outcomes: tuple = ()

    def to_dict(self) -> dict:
        return {
            "input_index": self.input_index,
            "handle": self.handle,
            "tags": list(self.tags),
            "comment": self.comment.to_dict(),
            "item_error": self.item_error,
            "field_outcomes": list(self.field_outcomes),
        }


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@LightningRodMutationSpec-mulk,
#   role=LightningRodMutationSpec,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Single frozen request normalizer keeps envelope/root errors global and text or comment errors local while preserving duplicate input slots and explicit atomic policy.,
# ]
class LightningRodMutationSpec:
    """ONE normalized, immutable beacon-mutation request (frozen).

    Produced ONLY by :meth:`normalize_from_tool_args` (invariant 2: ONE
    NORMALIZER PER REQUEST KIND). Fields are JSON-serializable;
    :meth:`to_dict` is provided for logs/tests. A strict ``from_wire()`` +
    versioned wire shape is DEFERRED (§9) until a real client mutation
    action exists -- do not freeze a speculative wire contract now.
    """

    mode: Literal["ensure_beacons", "remove_beacon_tags"]
    root_handles: tuple
    favor: Literal["uuid", "symbol"]
    operations: tuple
    # [ap:LPC157] residue: the all-or-nothing opt-in. False (the default)
    # keeps per-item partial success; True restores the strict preflight
    # veto -- any item failure leaves every other row not_run with zero
    # writes and no lease. Interpreted by the batch orchestrator only.
    atomic: bool = False

    @classmethod
    def normalize_from_tool_args(
        cls,
        *,
        mode: Any,
        symbols: Any,
        tags: Any,
        operations: Any,
        comment: Any,
        comment_mode: Any = None,
        atomic: Any = False,
        root_uuids: Any,
        root_uuid: Any,
        favor: Any,
    ) -> "tuple[LightningRodMutationSpec | None, str | None]":
        """Normalize the envelope once; retain identifiable bad items as rows.

        Roots, mode, favor and mixed payload shapes are request errors. Text
        errors are item-local. An invalid ensure comment becomes a failed
        field with preserve intent, so valid label deltas can still land.
        Duplicates retain their input slots for physical collision admission.
        Atomic is an explicit caller veto, interpreted by the batch executor.
        """
        try:
            _mode = str(mode or "").strip().lower()
            if _mode not in MUTATION_MODES:
                return None, f"internal error: unexpected mutation mode {mode!r}"

            _favor = str(favor or "uuid").strip().lower()
            if _favor not in ("uuid", "symbol"):
                return None, f"Unknown favor {favor!r}; use 'uuid' or 'symbol'."

            _atomic = False if atomic is None else atomic
            if not isinstance(_atomic, bool):
                return None, f"atomic must be a boolean, got {atomic!r}."

            def _coerce_roots(value: Any) -> list[str]:
                if value is None:
                    return []
                if isinstance(value, str):
                    s = value.strip()
                    return [s] if s else []
                if not isinstance(value, (list, tuple)):
                    return []
                out: list[str] = []
                for r in value:
                    s = str(r or "").strip()
                    if s:
                        out.append(s)
                return out

            roots_multi = _coerce_roots(root_uuids)
            roots_single = _coerce_roots(root_uuid)
            if roots_multi and roots_single:
                return None, (
                    "pass root_uuid or root_uuids -- one or the other, not both."
                )
            root_handles = tuple(roots_multi or roots_single)
            if not root_handles:
                return None, (
                    f"{_mode} requires root_uuids (or root_uuid) to scope "
                    "symbol resolution."
                )

            has_operations = operations is not None
            if symbols is not None and not isinstance(symbols, list):
                return None, "symbols must be a list of strings."
            has_symbols = bool(symbols)
            if has_operations:
                if has_symbols or tags or comment is not None or comment_mode is not None:
                    return None, (
                        "Use either granular `operations=[{symbol,tags,comment?,comment_mode?}, ...]` "
                        "OR shorthand `symbols=[...], tags=[...]`; do not mix operations "
                        "with top-level symbols/tags/comment/comment_mode."
                    )
                if not isinstance(operations, list) or not operations:
                    return None, "operations must be a non-empty list of {symbol, tags, comment?} objects."
                raw_items = operations
            else:
                if not has_symbols:
                    return None, f"{_mode} requires either operations=[...] or a non-empty symbols=[...] list."
                raw_items = [{"symbol": sym, "tags": tags, "comment": comment,
                              "comment_mode": comment_mode} for sym in symbols]

            ops: list[BeaconMutationOperation] = []
            for idx, op in enumerate(raw_items):
                if not isinstance(op, dict):
                    return None, f"operations[{idx}] must be an object."
                if not isinstance(op.get("symbol"), str) or not op["symbol"].strip():
                    return None, (f"operations[{idx}].symbol" if has_operations else f"symbols[{idx}]") + " must be a STRING and non-empty."
                sym = op["symbol"].strip()
                error = None
                fields = []
                extra = set(op) - {"symbol", "tags", "comment", "comment_mode"}
                if extra:
                    error = f"operations[{idx}] has unknown field(s): {sorted(extra)}."
                try:
                    op_tags = _coerce_tag_list(op.get("tags"))
                    if not op_tags:
                        raise ValueError(f"operations[{idx}].tags must be a non-empty list.")
                except ValueError as exc:
                    op_tags = []
                    error = error or str(exc)
                intent = BeaconCommentIntent.preserve()
                try:
                    text = validate_beacon_comment_text(op.get("comment"))
                    word = str(op.get("comment_mode") or "fill").strip().lower() or "fill"
                    if word not in COMMENT_MUTATION_MODES:
                        raise ValueError(f"unknown comment_mode {word!r}; use 'fill', 'replace', or 'append'.")
                    if _mode == "remove_beacon_tags" and (text is not None or op.get("comment_mode") is not None):
                        raise ValueError("remove_beacon_tags does not accept comment or comment_mode.")
                    if text is None and op.get("comment_mode") is not None and str(op["comment_mode"]).strip():
                        raise ValueError("comment_mode requires comment (there is no comment to apply it to).")
                    if text is not None:
                        intent = (BeaconCommentIntent.replace(text) if word == "replace" else
                                  BeaconCommentIntent.append(text) if word == "append" else
                                  BeaconCommentIntent.fill_if_empty(text))
                except ValueError as exc:
                    if _mode == "ensure_beacons":
                        fields.append({"field": "comment", "status": "error", "error_kind": "invalid_comment",
                                       "error": str(exc), "outcome": "preserved_disk"})
                    else:
                        error = error or str(exc)
                ops.append(BeaconMutationOperation(input_index=idx, handle=sym, tags=tuple(op_tags),
                                                   comment=intent, item_error=error,
                                                   field_outcomes=tuple(fields)))
            return cls(
                mode=_mode,  # type: ignore[arg-type]
                root_handles=root_handles,
                favor=_favor,  # type: ignore[arg-type]
                operations=tuple(ops),
                atomic=_atomic,
            ), None
        except ValueError as e:
            return None, str(e)

    def to_dict(self) -> dict:
        """JSON-serializable form for logs/tests (§9)."""
        return {
            "mode": self.mode,
            "root_handles": list(self.root_handles),
            "favor": self.favor,
            "operations": [op.to_dict() for op in self.operations],
            "atomic": self.atomic,
        }


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@BeaconWritePlan-mrug,
#   role=BeaconWritePlan,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Executable physical beacon operand carrying same-snapshot observations findings candidates and field outcomes through preflight JIT and commit projections; target_key claims a physical block not every equal id.,
# ]
class BeaconWritePlan:
    """Executable per-item operand and same-observation findings carrier.

    Produced JUST-IN-TIME by ``plan_beacon_write_from_node`` against the
    CURRENT file state (which includes every prior successful same-batch
    write) and passed INTO the commit seam as an operand -- no synthetic note
    text anywhere, no rediscovery at write time.

    ``write_intent`` is the AUTHORITATIVE intent, populated at plan
    construction (commit never reverse-engineers intent from outcomes).
    ``occurrence`` retains the cached Markdown ordinal for compatibility;
    alone it is not physical identity. ``target_key`` retains its canonical
    path and id/anchor prefix and adds the physical section, opener byte,
    block hash and anchor descriptor. Distinct anchored copies do not collide;
    two claims on the same physical block still do. ``physical_witness`` is
    revalidated by the executor, never replaced by an id-count lookup.
    ``source_sha256`` is the fingerprint of the CURRENT RAW BYTES at plan
    time (§7's CAS domain). Any conflict/unsupported/unresolvable target ->
    ``success=False`` with ``error``/``error_kind``; nothing is written.
    """

    success: bool
    planned_operation: Optional[str]  # 'create' | 'update' | 'delete' | 'noop' | 'unsupported'
    node_id: str
    target_key: Optional[tuple]
    cached_name: str
    intended_name: str
    write_intent: Optional[BeaconWriteIntent]
    occurrence: Optional[int]
    file_node_id: Optional[str]
    source_path: Optional[str]
    language: Optional[str]
    beacon_id: Optional[str]
    note_comment: Optional[str]
    disk_comment: Optional[str]
    comment_outcome: Optional[str]
    resolved_comment: Optional[str]
    intended_slice_labels: tuple
    source_sha256: Optional[str]
    error: Optional[str] = None
    error_kind: Optional[str] = None
    # Why a ``noop`` plan writes nothing (None for every other outcome):
    # 'virtual_group' | 'labels_already_absent' | 'already_satisfied' |
    # 'no_beacon_at_target'. Surfaced on the batch result row so a skipped
    # write is never silent (Manager-Session-088).
    noop_reason: Optional[str] = None
    # 2R4E1P: the agent-facing kept-conflict message (both texts) when
    # comment_outcome == 'kept_existing_conflict' -- a fill-mode comment
    # conflict is a WARNING (labels still ensured), never a plan failure.
    comment_warning: Optional[str] = None
    physical_witness: Optional[dict] = None
    selection_target: Optional[dict] = None
    candidates: tuple = ()
    diagnostics: tuple = ()
    evidence: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "planned_operation": self.planned_operation,
            "node_id": self.node_id,
            "target_key": list(self.target_key) if self.target_key else None,
            "cached_name": self.cached_name,
            "intended_name": self.intended_name,
            "write_intent": self.write_intent.to_dict() if self.write_intent else None,
            "occurrence": self.occurrence,
            "file_node_id": self.file_node_id,
            "source_path": self.source_path,
            "language": self.language,
            "beacon_id": self.beacon_id,
            "note_comment": self.note_comment,
            "disk_comment": self.disk_comment,
            "comment_outcome": self.comment_outcome,
            "resolved_comment": self.resolved_comment,
            "intended_slice_labels": list(self.intended_slice_labels),
            "source_sha256": self.source_sha256,
            "error": self.error,
            "error_kind": self.error_kind,
            "comment_warning": self.comment_warning,
            "physical_witness": self.physical_witness,
            "selection_target": self.selection_target,
            "candidates": list(self.candidates),
            "diagnostics": list(self.diagnostics),
            "evidence": self.evidence,
            "noop_reason": self.noop_reason,
        }
