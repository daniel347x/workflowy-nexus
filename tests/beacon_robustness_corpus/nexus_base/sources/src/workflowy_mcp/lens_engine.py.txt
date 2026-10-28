"""Pure LensSpec evaluation over injected Workflowy cache/config snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from .f8_slice_policy import (
    DEFAULT_AUTO_EXPAND_POSTFIX,
    DEFAULT_DEPRECATED_POSTFIX,
    DEFAULT_SUPPRESS_POSTFIX,
    _ancestor_chain_ids,
    build_selected_subtree_from_cache,
    build_slice_subtree_from_cache,
    build_stage1_base_tree,
    compute_lca_from_cache,
    is_descendant_of_any_root,
    normalize_slice_group_tags,
)
from .lens_spec import (
    GroupAtom,
    LensNode,
    LensSpecError,
    MergeNode,
    SubtreeAtom,
    TagsAtom,
)
from .merge_slice_groups_policy import collect_effective_slice_leaf_ids


@dataclass(frozen=True)
class LensEvalContext:
    """Snapshot-backed operands required by pure LensSpec evaluation."""

    cache_by_id: Mapping[str, dict[str, Any]]
    resolve_group: Callable[[str], Mapping[str, Any]]
    resolve_root: Callable[[str], str]
    # Chain-2 r2 (R1, WO R2-W1): the OPTIONAL inherited-stored-root skin of
    # resolve_root — the stored-identity policy (a SHAVED corrective match of
    # a group atom's inherited stored root is rejected, never adopted, and
    # the speculative correction echo is surgically discarded). Production
    # contexts (the Stage-0 snapshot builder) always set it; it is OPTIONAL
    # with a None default so test fakes and direct/internal constructors that
    # provide only resolve_root keep today's forgiving behavior unchanged —
    # _resolve_root falls back to resolve_root when this is None.
    resolve_inherited_root: Callable[[str], str] | None = None
    auto_expand_postfix: str = DEFAULT_AUTO_EXPAND_POSTFIX
    suppress_postfix: str = DEFAULT_SUPPRESS_POSTFIX
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX
    global_never_stems: Iterable[str] = field(default_factory=tuple)
    exclude_completed: bool = False
    anchor_boundary_predicate: Callable[[dict[str, Any]], bool] | None = None
    # The default match_scope for lens atoms that OMIT their own. Kept here (not
    # on the atom) so the surface decides it per render: a RECIPE render threads
    # the surface match_scope (default 'name'), a SIFT threads 'name' for its tag
    # atoms while its query keeps 'name_and_note'. Left at 'name_and_note' for
    # direct/internal callers so presentation paths are untouched (RULING 4).
    default_match_scope: str = "name_and_note"


@dataclass(frozen=True)
class LensAtomReport:
    path: str
    kind: str
    resolved_root_uuid: str
    canonical_identity: Mapping[str, Any]
    leaf_count: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LensEvaluation:
    effective_leaf_ids: frozenset[str]
    canonical_lens: LensNode
    per_atom_reports: tuple[LensAtomReport, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class LensResultTree:
    root_uuid: str
    tree: dict[str, Any]
    leaf_count: int
    empty_selection: bool = False


@dataclass(frozen=True)
class LensEmptyResult:
    root_uuid: None = None
    tree: None = None
    leaf_count: int = 0
    empty_selection: bool = True


def _nested_path(parent: str, index: int) -> str:
    return f"inputs[{index}]" if parent == "$" else f"{parent}.inputs[{index}]"


def _resolver_error(code: str, path: str, subject: str, exc: Any) -> LensSpecError:
    if isinstance(exc, Mapping):
        detail = exc.get("error") or exc.get("message") or repr(dict(exc))
    else:
        detail = str(exc)
    return LensSpecError(code, f"Could not resolve {subject}: {detail}", path)


def _resolve_root(
    ctx: LensEvalContext, handle: str, path: str, *, inherited: bool = False
) -> str:
    try:
        if inherited and ctx.resolve_inherited_root is not None:
            # Chain-2 r2 (R1, WO R2-W1): the INHERITED stored root resolves
            # under the stored-identity policy (shaves rejected) when the
            # context provides the skin; without one (test fakes, internal
            # constructors) today's forgiving resolve_root stands.
            resolved = ctx.resolve_inherited_root(handle)
        else:
            resolved = ctx.resolve_root(handle)
    except Exception as exc:  # noqa: BLE001
        # Chain-1 W4 (F5): the archive read-only refusal is the ANSWER to a
        # sink / purge-preview / write aimed at an archived root — the
        # universal archived_node_read_only envelope (code + provenance),
        # never a lens resolution failure.  Everything else stays a
        # path-bearing LensSpecError.
        from .archive_context import ArchivedNodeReadOnlyError

        if isinstance(exc, ArchivedNodeReadOnlyError):
            raise
        raise _resolver_error("root_resolution_failed", path, repr(handle), exc) from exc
    root_uuid = str(resolved or "").strip()
    if not root_uuid or root_uuid not in ctx.cache_by_id:
        raise _resolver_error(
            "root_resolution_failed",
            path,
            repr(handle),
            "resolver returned no cache-present Workflowy UUID",
        )
    return root_uuid


def _resolve_group(
    ctx: LensEvalContext, handle: str, path: str
) -> Mapping[str, Any]:
    try:
        group = ctx.resolve_group(handle)
    except Exception as exc:  # noqa: BLE001
        raise _resolver_error("group_resolution_failed", path, repr(handle), exc) from exc
    if not isinstance(group, Mapping) or group.get("success") is False:
        raise _resolver_error(
            "group_resolution_failed", path, repr(handle), group
        )
    return group


def _tree_leaf_ids(tree: dict[str, Any] | None) -> set[str]:
    leaves: set[str] = set()

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        children = [child for child in (node.get("children") or []) if isinstance(child, dict)]
        if not children:
            node_id = str(node.get("id") or "").strip()
            if node_id:
                leaves.add(node_id)
            return
        for child in children:
            walk(child)

    walk(tree)
    return leaves


def _plan_warnings(plan: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(plan, Mapping):
        return ()
    raw = plan.get("warnings") or ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw)
    return (str(raw),)


# @beacon[
#   id=mcp-api@evaluate-lens,
#   role=ƒ evaluate_lens(node, ctx),
#   slice_labels=nexus--mcp-api,slices,
#   kind=ast,
#   comment=Pure recursive LensSpec evaluator. Resolves every atom once against injected snapshots, delegates group/tags/subtree meaning to the canonical Stage-1 primitives, extracts effective leaves, performs union/intersection/ordered-difference algebra over leaf IDs only, and returns a canonicalized recipe plus exact per-atom diagnostics. A group/tags atom that OMITS its own match_scope takes ctx.default_match_scope (the surface threads 'name' for a recipe render, 'name' for sift tag atoms); an explicit per-atom match_scope always wins. Chain-1 r2 (F5): a sink (write/export/purge_preview) whose GROUP atom's STORED root names a visible archived node raises ArchivedNodeReadOnlyError with the universal §5.3.2 envelope — the same answer _resolve_root gives an override root — while every other resolution failure stays a path-bearing LensSpecError. Chain-2 r1 (F1): a group atom's INHERITED stored root resolves through the SAME _resolve_root/Stage-0 resolve_root seam an override root uses (the shared two-phase composite resolve_uuid_or_anchor_point_handle), so an exported-and-deleted stored root is served by the automatic fallback re-run and an include-mode run records the archive answer (banner + provenance); a live stored root resolves to itself, and every resolution failure falls through to the pre-existing refusal check + group_resolution_failed error unchanged. Chain-2 r2 (R1): the inherited branch resolves under the STORED-IDENTITY policy — a SHAVED corrective match is REJECTED (never adopted as the atom's scope, even with no archive folder), the speculative correction echo is surgically discarded, and the rejection falls into the same miss path (group_resolution_failed / the archived-stored-root sink refusal); explicit override atoms keep today's forgiving correction, echo included, and an exact archived record still beats a shave upstream (E2).,
# ]
def evaluate_lens(node: LensNode, ctx: LensEvalContext) -> LensEvaluation:
    """Evaluate a parsed LensSpec into its effective Stage-1 leaf set."""

    cache = dict(ctx.cache_by_id)

    def evaluate(current: LensNode, path: str) -> LensEvaluation:
        if isinstance(current, MergeNode):
            children = [
                evaluate(child, _nested_path(path, index))
                for index, child in enumerate(current.inputs)
            ]
            child_sets = [set(child.effective_leaf_ids) for child in children]
            if current.operation == "union":
                combined = set().union(*child_sets)
            elif current.operation == "intersection":
                combined = set(child_sets[0])
                for child_set in child_sets[1:]:
                    combined.intersection_update(child_set)
            elif current.operation == "difference":
                # Ordered relative complement: the first input is the base;
                # every later input is excluded.  For N inputs this is
                # A - (B union C ...), equivalently a left-folded difference.
                combined = set(child_sets[0])
                for child_set in child_sets[1:]:
                    combined.difference_update(child_set)
            else:  # Defensive for directly-constructed MergeNode values.
                raise LensSpecError(
                    "invalid_merge_operation",
                    f"Unsupported merge operation {current.operation!r}.",
                    path,
                )
            reports = tuple(
                report for child in children for report in child.per_atom_reports
            )
            warnings = tuple(
                warning for child in children for warning in child.warnings
            )
            return LensEvaluation(
                effective_leaf_ids=frozenset(combined),
                canonical_lens=MergeNode(
                    current.operation,
                    tuple(child.canonical_lens for child in children),
                ),
                per_atom_reports=reports,
                warnings=warnings,
            )

        if isinstance(current, GroupAtom):
            group = _resolve_group(ctx, current.group_handle, path)
            group_id = str(
                group.get("id") or group.get("group_id") or current.group_handle
            ).strip()
            prefixes_raw = group.get("prefixes")
            if prefixes_raw is None:
                prefixes_raw = group.get("tags")
            if not isinstance(prefixes_raw, (list, tuple)):
                raise _resolver_error(
                    "group_resolution_failed",
                    path,
                    repr(current.group_handle),
                    "resolved group has no prefixes/tags array",
                )
            prefixes = [str(item).strip().lstrip("#") for item in prefixes_raw if str(item).strip()]
            if current.root_handle is not None:
                root_uuid = _resolve_root(ctx, current.root_handle, path)
                canonical_root_handle: str | None = root_uuid
            else:
                inherited_root = str(
                    group.get("root_uuid") or group.get("root_handle") or ""
                ).strip()
                # Chain-2 r1 (F1, WO W1): the group's EFFECTIVE STORED root
                # runs through the SAME shared root resolution an override
                # root gets — the ONE Stage-0 ``resolve_root`` callback (whose
                # single seam is the shared two-phase composite
                # resolve_uuid_or_anchor_point_handle: live legs first, then
                # the archive legs; the fallback gate and archive-service
                # recording ride inside it) — so a stored root that was
                # exported and deleted is served by the automatic fallback
                # re-run (mode fallback: the gate requests it; the re-run's
                # archive leg resolves the root, records the served root, and
                # the finalizer prints the §5.1 banner) and, in include mode,
                # records the archive answer the same way. A LIVE stored root
                # resolves to itself unchanged. Chain-2 r2 (R1, WO R2-W1):
                # the inherited branch resolves under the STORED-IDENTITY
                # policy (``inherited=True`` -> ctx.resolve_inherited_root,
                # the stored-root skin of the ONE closure): a SHAVED
                # corrective match is rejected, never adopted as this atom's
                # effective scope — the kernel's shortened match to an
                # unrelated live node is not the identity the group stored,
                # even with no archive folder — and the speculative
                # correction echo that resolution recorded is surgically
                # discarded. The rejection lands in the same fall-through as
                # every other miss (the archived-stored-root refusal check +
                # group error below), so the miss policy is byte-identical to
                # pre-Chain-2-r1; an exact archived record beats a shave
                # upstream (E2) and still serves the archive repair.
                # Resolution failure (a gate hit on the first fallback run, a
                # genuinely unknown root, a rejected shave, ambiguity) falls
                # through to the pre-existing refusal check + group error
                # below. No second resolver and no per-carrier
                # gate/lookup/banner copy.
                from .archive_context import ArchivedNodeReadOnlyError

                _inherited_resolved: str | None = None
                if inherited_root:
                    try:
                        _inherited_resolved = _resolve_root(
                            ctx, inherited_root, path, inherited=True
                        )
                    except ArchivedNodeReadOnlyError:
                        # The ANSWER for a sink / purge-preview context (the
                        # universal archived_node_read_only envelope) — never
                        # a lens resolution failure.
                        raise
                    except Exception:  # noqa: BLE001 - the fall-through below owns every miss
                        _inherited_resolved = None
                if _inherited_resolved is None:
                    if inherited_root:
                        # Chain-1 r2 (F5, WO common end state): a sink aimed
                        # at a group atom whose STORED root names a visible
                        # archived node refuses with the universal envelope
                        # — the same answer _resolve_root gives an override
                        # root. Everything else stays a path-bearing
                        # LensSpecError exactly as before. (Chain-2 r1: the
                        # archived-root case now answers inside _resolve_root
                        # above; this check remains as the fall-through's
                        # guard for any context _resolve_root's own seam does
                        # not cover.)
                        from .archive_context import (
                            ARCHIVE_REFUSAL_REASONS,
                            ArchivedNodeReadOnlyError as _AROError,
                            archive_miss,
                            current_archive_context,
                        )

                        _refusal_ctx = current_archive_context()
                        if (
                            _refusal_ctx is not None
                            and _refusal_ctx.reason in ARCHIVE_REFUSAL_REASONS
                        ):
                            _miss_text, _prov = archive_miss(inherited_root)
                            if _miss_text is not None:
                                raise _AROError(_miss_text, extra=_prov)
                    raise _resolver_error(
                        "group_resolution_failed",
                        path,
                        repr(current.group_handle),
                        "resolved group has no cache-present root UUID",
                    )
                root_uuid = _inherited_resolved
                # Preserve inheritance semantics: an omitted override must remain
                # omitted so refreshing the canonical recipe follows group edits.
                canonical_root_handle = None
            match_scope = current.match_scope or ctx.default_match_scope
            final_tree, plan, _prune = build_stage1_base_tree(
                cache,
                root_uuid,
                prefixes,
                auto_expand_postfix=ctx.auto_expand_postfix,
                suppress_postfix=ctx.suppress_postfix,
                deprecated_postfix=ctx.deprecated_postfix,
                global_never_expand_tags=ctx.global_never_stems,
                exclude_completed=ctx.exclude_completed,
                match_scope=match_scope,
            )
            if final_tree is None:
                raise _resolver_error(
                    "group_resolution_failed",
                    path,
                    repr(current.group_handle),
                    "group projection root is absent from the cache",
                )
            leaves = collect_effective_slice_leaf_ids(
                final_tree,
                plan,
                prefixes,
                deprecated_postfix=ctx.deprecated_postfix,
                match_scope=match_scope,
            )
            warnings = _plan_warnings(plan)
            canonical = GroupAtom(
                group_handle=group_id,
                root_handle=canonical_root_handle,
                match_scope=match_scope,
            )
            identity = {
                "group_id": group_id,
                "group_name": group.get("name"),
                "resolved_root_uuid": root_uuid,
                "prefixes": tuple(prefixes),
            }

        elif isinstance(current, TagsAtom):
            root_uuid = _resolve_root(ctx, current.root_handle, path)
            tags_scope = current.match_scope or ctx.default_match_scope
            normalized = normalize_slice_group_tags(
                current.tags,
                auto_expand_postfix=ctx.auto_expand_postfix,
                suppress_postfix=ctx.suppress_postfix,
                deprecated_postfix=ctx.deprecated_postfix,
            )
            prefixes = list(normalized["bases"])
            if not prefixes:
                raise LensSpecError(
                    "invalid_lens_shape",
                    "tags did not contain any usable slice-group prefixes.",
                    path,
                )
            final_tree, plan, _prune = build_stage1_base_tree(
                cache,
                root_uuid,
                prefixes,
                auto_expand_postfix=ctx.auto_expand_postfix,
                suppress_postfix=ctx.suppress_postfix,
                deprecated_postfix=ctx.deprecated_postfix,
                global_never_expand_tags=ctx.global_never_stems,
                exclude_completed=ctx.exclude_completed,
                match_scope=tags_scope,
            )
            if final_tree is None:
                raise _resolver_error(
                    "root_resolution_failed",
                    path,
                    repr(current.root_handle),
                    "projection root is absent from the cache",
                )
            leaves = collect_effective_slice_leaf_ids(
                final_tree,
                plan,
                prefixes,
                deprecated_postfix=ctx.deprecated_postfix,
                match_scope=tags_scope,
            )
            warnings = _plan_warnings(plan)
            canonical = TagsAtom(root_uuid, tuple(prefixes), tags_scope)
            identity = {
                "resolved_root_uuid": root_uuid,
                "prefixes": tuple(prefixes),
                "match_scope": tags_scope,
                "collapsed": tuple(normalized["collapsed"]),
                "dropped": tuple(normalized["dropped"]),
            }

        elif isinstance(current, SubtreeAtom):
            root_uuid = _resolve_root(ctx, current.root_handle, path)
            boundary = None
            if current.descend == "stop_at_child_docks":
                boundary = ctx.anchor_boundary_predicate
                if boundary is None:
                    raise LensSpecError(
                        "boundary_predicate_unavailable",
                        "stop_at_child_docks requires an injected Anchor Dock boundary predicate.",
                        path,
                    )
            final_tree = build_slice_subtree_from_cache(
                cache,
                root_uuid,
                (),
                member_all=True,
                exclude_completed=ctx.exclude_completed,
                boundary_predicate=boundary,
            )
            if final_tree is None:
                raise _resolver_error(
                    "root_resolution_failed",
                    path,
                    repr(current.root_handle),
                    "raw subtree root is absent from the cache",
                )
            leaves = _tree_leaf_ids(final_tree)
            warnings = ()
            canonical = SubtreeAtom(root_uuid, current.descend)
            identity = {
                "resolved_root_uuid": root_uuid,
                "descend": current.descend,
            }

        else:  # pragma: no cover - parser/type boundary owns this invariant.
            raise TypeError(f"Unsupported LensSpec node: {type(current).__name__}")

        report = LensAtomReport(
            path=path,
            kind=current.kind,
            resolved_root_uuid=root_uuid,
            canonical_identity=identity,
            leaf_count=len(leaves),
            warnings=warnings,
        )
        return LensEvaluation(
            effective_leaf_ids=frozenset(leaves),
            canonical_lens=canonical,
            per_atom_reports=(report,),
            warnings=warnings,
        )

    return evaluate(node, "$")


# @beacon[
#   id=mcp-api@build-lens-result-tree,
#   role=ƒ build_lens_result_tree(effective_leaf_ids, ctx),
#   slice_labels=nexus--mcp-api,slices,
#   kind=ast,
#   comment=Post-algebra LensSpec materializer. Empty selections become a typed zero-root result; non-empty leaf sets compute one real LCA, reject phantom/disjoint roots, and rebuild the minimal connective tree through the canonical cache selection primitive while re-enforcing completed exclusion.,
# ]
def build_lens_result_tree(
    effective_leaf_ids: Iterable[str], ctx: LensEvalContext
) -> LensResultTree | LensEmptyResult:
    """Reconstruct the minimal connective tree after LensSpec set algebra."""

    leaf_ids = frozenset(str(item).strip() for item in effective_leaf_ids if str(item).strip())
    if not leaf_ids:
        return LensEmptyResult()

    cache = dict(ctx.cache_by_id)
    if any(node_id not in cache for node_id in leaf_ids):
        missing = sorted(node_id for node_id in leaf_ids if node_id not in cache)
        raise LensSpecError(
            "no_common_ancestor",
            f"Effective leaves are absent from the cache: {missing!r}.",
            "$",
        )
    root_uuid = compute_lca_from_cache(cache, leaf_ids)
    if not root_uuid or not str(root_uuid).strip() or root_uuid not in cache:
        raise LensSpecError(
            "no_common_ancestor",
            "No common ancestor exists below the Workflowy document root. Narrow the lens inputs or scope them beneath a shared node.",
            "$",
        )

    tree = build_selected_subtree_from_cache(
        cache,
        root_uuid,
        lambda node: str(node.get("id") or "") in leaf_ids,
        exclude_completed=ctx.exclude_completed,
    )
    if tree is None:
        raise LensSpecError(
            "no_common_ancestor",
            "The effective leaf set could not be reconstructed beneath a real Workflowy root.",
            "$",
        )
    return LensResultTree(
        root_uuid=root_uuid,
        tree=tree,
        leaf_count=len(leaf_ids),
    )


# @beacon[
#   id=mcp-api@lens-scaffold-ancestor-ids,
#   role=ƒ lens_scaffold_ancestor_ids(tree, input_root_ids, cache_by_id),
#   slice_labels=nexus--mcp-api,slices,
#   kind=ast,
#   comment=[ap:I506WY] THE lens-scaffold computation: FINAL-tree ids strictly above an input root in the PINNED CACHE parent chain, excluding every input root's member region. Fed by _project_render_lens_from_cache (per-atom resolved roots + final post-sift tree + pinned cache) to the materializer's existing lens-scaffold cause. An input root absent from the final tree still protects all its descendants; if the final root is at-or-below an input root the scaffold set is empty. Distant-LCA connective paths stay heading-only by default; nested input roots never suppress an outer root's member notes. Reuses the canonical ancestor-chain/descendant predicates; pure, no I/O or second suppression path.,
# ]
def lens_scaffold_ancestor_ids(
    tree: dict[str, Any] | None,
    input_root_ids: Iterable[str],
    cache_by_id: Mapping[str, dict[str, Any]] | None = None,
) -> frozenset[str]:
    """Ids of the final lens tree's connective scaffolding ABOVE the input roots.

    Classify against the pinned parent graph, not the truncated final tree:
    an input root may be absent because the effective leaves' LCA is below
    it. Only strict ancestors of an input root can be scaffold, and nodes
    at-or-below ANY input root remain member region (nested roots included).
    The two-argument form retains tree-only compatibility for existing pure
    callers; production supplies the full pinned cache. Both use the SAME
    ancestor-chain and descendant predicates. Pure; inputs never mutated.
    """

    roots = {str(r).strip() for r in (input_root_ids or ()) if str(r).strip()}
    if not isinstance(tree, dict) or not roots:
        return frozenset()

    tree_ids: set[str] = set()
    tree_cache: dict[str, dict[str, Any]] = {}
    stack: list[tuple[dict[str, Any], str | None]] = [(tree, None)]
    while stack:
        node, parent_id = stack.pop()
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "").strip()
        if node_id:
            tree_ids.add(node_id)
            if cache_by_id is None:
                tree_cache[node_id] = {"id": node_id, "parent_id": parent_id}
        for child in node.get("children") or []:
            if isinstance(child, dict):
                stack.append((child, node_id or parent_id))

    graph = tree_cache if cache_by_id is None else cache_by_id
    strict_ancestors = {
        ancestor
        for root_id in roots
        for ancestor in _ancestor_chain_ids(graph, root_id)[:-1]
    }
    return frozenset(
        node_id
        for node_id in tree_ids & strict_ancestors
        if not is_descendant_of_any_root(graph, node_id, roots)
    )


__all__ = [
    "LensEvalContext",
    "LensAtomReport",
    "LensEvaluation",
    "LensResultTree",
    "LensEmptyResult",
    "evaluate_lens",
    "build_lens_result_tree",
    "lens_scaffold_ancestor_ids",
]
