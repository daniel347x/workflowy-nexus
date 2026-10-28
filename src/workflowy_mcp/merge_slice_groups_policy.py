"""merge_slice_groups_policy.py -- the pure merge-compiler algorithms.

A CONSUMER of the Stage-1 projection engine (f8_slice_policy), NOT a new phase
of it.  This module holds the merge-specific pure algorithms; f8_slice_policy
gains NO new symbols (A11).

All functions are pure (no I/O, no network, no mutation).  They operate on
the /nodes-export cache dict and other pure data structures, reusing the
existing Stage-1 primitives verbatim.

Exports (all beaconed):

  * collect_effective_slice_leaf_ids (A1) -- effective leaf set extraction
  * compute_merge_full_cover (A2)       -- bottom-up coverage set
  * compute_merge_tag_plan (A7)        -- the planner (Phases C+D+E)
  * find_tag_family_collisions (A3/A4) -- the safety checker
  * build_merge_overlay_cache (A7)     -- dry-run overlay cache
  * simulate_merge_projection (A7)     -- dry-run projection
  * verify_merge_projection (A3.4)     -- post-mutation verification
"""

from __future__ import annotations

from typing import Any, Iterable

from .f8_slice_policy import (
    build_stage1_base_tree,
    compute_lca_from_cache,
    slice_tag_variant_match,
    extract_tags_from_text,
    extract_name_tags,
    normalize_global_never_stems,
    name_has_global_never_tag,
    is_slice_member,
    _ancestor_chain_ids,
    _node_is_completed,
    POSTFIX_SEP,
    SERVER_SIDE_GLIMPSE_EXCLUDE_COMPLETED,
    DEFAULT_AUTO_EXPAND_POSTFIX,
    DEFAULT_SUPPRESS_POSTFIX,
    DEFAULT_DEPRECATED_POSTFIX,
    SliceProjectionInvariantError,
)
from .glimpse_since_policy import collect_leaf_branches
from .client.api_client_core import _resolve_split_name_and_tags

# Re-export for convenience
__all__ = [
    "collect_effective_slice_leaf_ids",
    "compute_merge_full_cover",
    "compute_merge_tag_plan",
    "find_tag_family_collisions",
    "build_merge_overlay_cache",
    "simulate_merge_projection",
    "verify_merge_projection",
]


# ===========================================================================
# A1 -- effective slice leaf set
# ===========================================================================

# @beacon[
#   id=merge@collect-effective-slice-leaf-ids,
#   slice_labels=nexus--tags,slices,
#   kind=ast,
#   comment=A1: THE effective-leaf extractor for a projected slice final tree. A final-tree leaf L is EFFECTIVE iff DIRECTLY LIVE (is_slice_member with the SAME operands Phase 1 used) OR EXPANSION-ADMITTED (L's root-to-leaf branch passes through a surviving plan['expand'] target). The root-as-lone-leaf case fails BOTH tests -> correctly empty (a no-hit group contributes nothing, NOT {root}). Fixes the false-member bug where collect_leaf_branches alone reports the connective root as a 'leaf' when the funnel unconditionally keeps it. Pure; reuses collect_leaf_branches + is_slice_member verbatim -- no new membership engine.,
# ]
def collect_effective_slice_leaf_ids(
    final_subtree: "dict[str, Any] | None",
    plan: "dict[str, Any] | None",
    active_prefixes: "Iterable[str]",
    *,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
    match_scope: str = "name_and_note",
) -> set[str]:
    """Extract the EFFECTIVE leaf set from a projected slice final tree.

    A final-tree leaf L is EFFECTIVE iff:

    1. DIRECTLY LIVE: ``is_slice_member`` returns True with the SAME operands
       Phase 1 used; OR
    2. EXPANSION-ADMITTED: L's root-to-leaf branch passes through a surviving
       ``plan['expand']`` target (L was pulled in by ``--expand`` hydration).

    The root-as-lone-leaf case fails BOTH tests (no live prefix match, no
    expand ancestor) → correctly empty set.

    Pure; no I/O.
    """
    if final_subtree is None:
        return set()

    expand_ids: set[str] = set()
    if plan and isinstance(plan, dict):
        expand_ids = set(plan.get("expand") or [])

    # Collect the uuids of all nodes on branches that pass through an expand target.
    expand_admitted: set[str] = set()
    if expand_ids:
        for branch in collect_leaf_branches([final_subtree]):
            branch_uuids = {uuid for uuid, _n in branch}
            if branch_uuids & expand_ids:
                # Every node on this branch (including the leaf) is expand-admitted.
                expand_admitted |= branch_uuids

    effective: set[str] = set()
    for branch in collect_leaf_branches([final_subtree]):
        leaf_uuid, leaf_node = branch[-1]
        # Test 1: directly live
        if is_slice_member(
            leaf_node.get("name"),
            active_prefixes,
            note=leaf_node.get("note"),
            deprecated_postfix=deprecated_postfix,
            match_scope=match_scope,
        ):
            effective.add(leaf_uuid)
            continue
        # Test 2: expansion-admitted
        if leaf_uuid in expand_admitted:
            effective.add(leaf_uuid)
            continue

    return effective


# ===========================================================================
# A2 -- compute_merge_full_cover: bottom-up coverage set
# ===========================================================================

# @beacon[
#   id=merge@compute-merge-full-cover,
#   slice_labels=nexus--tags,slices,
#   kind=ast,
#   comment=A2: bottom-up coverage set -- the workhorse compute_merge_tag_plan calls. Returns the set of node ids B where EVERY visible TERMINAL leaf under B is in merged_leaf_ids (recursive). 'Visible' = NOT suppression-invisible (name_has_global_never_tag on the CHILD, root-exempt) and, when skip_completed=True, NOT completed (CHILD-only test, root-exempt) -- mirroring the Phase-3 HYDSUP hydration seals so a later render of the expand tag reproduces exactly the covered set. O(N) per start subtree; the children-by-parent index mirrors collect_subtree_ids' construction.,
# ]
def compute_merge_full_cover(
    cache_by_id: dict[str, dict[str, Any]],
    start_ids: "list[str]",
    merged_leaf_ids: set[str],
    global_never_stems: "set[str]",
    *,
    skip_completed: bool = True,
) -> set[str]:
    """Bottom-up coverage set: node ids B where every visible terminal leaf
    under B is in ``merged_leaf_ids``.

    A child is "visible" if NOT suppression-invisible
    (``name_has_global_never_tag`` on the CHILD name) and, when
    ``skip_completed=True``, NOT completed.  Both tests are CHILD-ONLY
    (root-exempt), mirroring the Phase-3 hydration seals.

    Pure; no I/O.  O(N) total.
    """
    # children index by parent_id
    children_by_parent: dict[str, list[str]] = {}
    for node in cache_by_id.values():
        pid = node.get("parent_id")
        nid = node.get("id")
        if pid is not None and nid:
            children_by_parent.setdefault(str(pid), []).append(str(nid))

    def _visible_children(nid: str) -> list[str]:
        result: list[str] = []
        for cid in children_by_parent.get(nid, []):
            child = cache_by_id.get(cid)
            if child is None:
                continue
            # Suppression: CHILD-only test (root-exempt)
            if name_has_global_never_tag(child.get("name"), global_never_stems):
                continue
            # Completed: CHILD-only test (root-exempt)
            if skip_completed and _node_is_completed(child):
                continue
            result.append(cid)
        return result

    cover_set: set[str] = set()

    def _cover(nid: str) -> bool:
        """True iff every visible terminal leaf under nid is in merged_leaf_ids."""
        children = _visible_children(nid)
        if not children:
            # Terminal leaf (in the visible sense): covers iff it's wanted.
            return nid in merged_leaf_ids
        # Branch: covers iff ALL visible children cover.
        all_cover = True
        for cid in children:
            if not _cover(cid):
                all_cover = False
                # do NOT early-return: we must compute cover for all children
                # so cover_set is fully populated for subtrees that ARE fully
                # covered (the caller needs the full set, not just the root
                # verdict).
        if all_cover:
            cover_set.add(nid)
        return all_cover

    for sid in start_ids:
        if sid in cache_by_id:
            _cover(sid)
    return cover_set


# ===========================================================================
# A7 -- compute_merge_tag_plan: the planner (Phases C+D+E)
# ===========================================================================

# @beacon[
#   id=merge@compute-merge-tag-plan,
#   slice_labels=nexus--tags,slices,
#   kind=ast,
#   comment=A7: the MERGE TAG PLANNER (Phases C+D+E of the goal spec). Given the merged leaf set + the LCA root (or top-level start_ids for vault-root), walks the cache top-down and locates the HIGHEST-LEVEL fully-covered branches (expand_nodes -- tagged once with tag+--expand) and the remaining uncovered merged leaves (plain_nodes -- tagged with just the tag). 'Highest-level' falls out by construction: full_cover is tested BEFORE descending; an expand node ends the walk beneath it. The PLANNER COMPLETENESS ASSERTION (A7) verifies every merged leaf ends up in (union of expand-covered sets) OR plain_nodes; on assertion failure the result carries spine_blockers naming any completed/global-never node(s) strictly between a start root and a blocked leaf (A6 diagnostics). Pure; no I/O.,
# ]
def compute_merge_tag_plan(
    cache_by_id: dict[str, dict[str, Any]],
    merged_leaf_ids: set[str],
    lca_root: "str | None",
    *,
    target_tag: str,
    expand_postfix_word: str,
    global_never_expand_tags: "Iterable[str]" = (),
    skip_completed: bool = True,
) -> dict[str, Any]:
    """Plan the tag operations (Phases C+D+E of the goal spec).

    Returns ``{ok: True, expand_nodes: [...], plain_nodes: [...],
    start_ids: [...], root_is_vault_root: bool}`` or
    ``{ok: False, error: str, spine_blockers: [...]}``.

    Pure; no I/O.
    """
    stems = normalize_global_never_stems(global_never_expand_tags)

    # Determine start_ids
    def _prio(nid: str) -> "float | int":
        pr = (cache_by_id.get(nid) or {}).get("priority")
        return pr if isinstance(pr, (int, float)) else 0

    root_is_vault_root = lca_root is None
    if root_is_vault_root:
        # LCA is the vault root: start at each top-level node that is an
        # ancestor of (or is) a merged leaf. A12: deterministic PRIORITY /
        # document order (priority first, UUID tiebreak), not raw UUID sort.
        start_ids: list[str] = sorted(
            {
                _ancestor_chain_ids(cache_by_id, leaf)[0]
                for leaf in merged_leaf_ids
                if _ancestor_chain_ids(cache_by_id, leaf)
            },
            key=lambda sid: (_prio(sid), sid),
        )
    else:
        start_ids = [lca_root]

    # Coverage set
    full_cover = compute_merge_full_cover(
        cache_by_id,
        start_ids,
        merged_leaf_ids,
        stems,
        skip_completed=skip_completed,
    )

    # children index by parent_id (for the merged_present + has-visible-child
    # checks). Priority-sorted (stable: ties keep cache insertion order) so the
    # plan's expand/plain node lists come out in Workflowy document order (A12).
    children_by_parent: dict[str, list[str]] = {}
    for node in cache_by_id.values():
        pid = node.get("parent_id")
        nid = node.get("id")
        if pid is not None and nid:
            children_by_parent.setdefault(str(pid), []).append(str(nid))
    for kids in children_by_parent.values():
        kids.sort(key=_prio)

    def _visible_children(nid: str) -> list[str]:
        result: list[str] = []
        for cid in children_by_parent.get(nid, []):
            child = cache_by_id.get(cid)
            if child is None:
                continue
            if name_has_global_never_tag(child.get("name"), stems):
                continue
            if skip_completed and _node_is_completed(child):
                continue
            result.append(cid)
        return result

    # merged_present: bottom-up -- does this subtree contain any merged leaf?
    merged_present_cache: dict[str, bool] = {}

    def _merged_present(nid: str) -> bool:
        if nid in merged_present_cache:
            return merged_present_cache[nid]
        if nid in merged_leaf_ids:
            merged_present_cache[nid] = True
            return True
        for cid in _visible_children(nid):
            if _merged_present(cid):
                merged_present_cache[nid] = True
                return True
        merged_present_cache[nid] = False
        return False

    # Pre-compute merged_present for all nodes under start_ids
    for sid in start_ids:
        _merged_present(sid)

    expand_nodes: list[str] = []
    plain_nodes: list[str] = []

    def _select(nid: str) -> None:
        """Top-down walk: tag expand at highest covered branch, plain at leaves."""
        if not _merged_present(nid):
            return  # prune: no merged content below
        vis_children = _visible_children(nid)
        if nid in full_cover and vis_children:
            # Highest-level fully-covered branch: tag once with tag--expand.
            expand_nodes.append(nid)
            return  # STOP descending
        if nid in merged_leaf_ids and not any(
            _merged_present(cid) for cid in vis_children
        ):
            # Merged-tree LEAF ONLY: tag plain (no postfix).
            plain_nodes.append(nid)
            return
        for cid in vis_children:
            _select(cid)

    for sid in start_ids:
        _select(sid)

    # PLANNER COMPLETENESS ASSERTION (A7): every merged leaf must end up
    # in (expand-covered sets) ∪ plain_nodes.  We check this by verifying
    # every merged_leaf_id is either covered by an expand node or in
    # plain_nodes.
    expand_covered: set[str] = set()
    for eid in expand_nodes:
        # Collect all visible terminal leaves under this expand node
        def _collect_visible_leaves(nid: str) -> None:
            children = _visible_children(nid)
            if not children:
                expand_covered.add(nid)
                return
            for cid in children:
                _collect_visible_leaves(cid)
        _collect_visible_leaves(eid)

    uncovered = merged_leaf_ids - expand_covered - set(plain_nodes)
    if uncovered:
        # A6 diagnostics: when an uncovered merged leaf is unreachable because
        # a COMPLETED or GLOBAL-NEVER node sits on the spine strictly between
        # a start root and the leaf, name the blocker node(s) -- the loud
        # pre-mutation refusal then points at the actual offending spine
        # node, not just the blocked leaf (both refuse with ZERO mutations).
        start_set = set(start_ids)
        blockers: list[str] = []
        for leaf in sorted(uncovered):
            chain = _ancestor_chain_ids(cache_by_id, leaf)
            on_spine = False
            for aid in chain:
                if aid in start_set:
                    on_spine = True
                    continue
                if not on_spine or aid == leaf:
                    continue
                anode = cache_by_id.get(aid) or {}
                if (
                    (skip_completed and _node_is_completed(anode))
                    or name_has_global_never_tag(anode.get("name"), stems)
                ):
                    if aid not in blockers:
                        blockers.append(aid)
        error = (
            f"planner completeness assertion failed: {len(uncovered)} "
            f"merged leaf/leaves not covered by any expand node or plain "
            f"tag: {sorted(uncovered)[:10]}"
        )
        if blockers:
            error += (
                f"; spine blocker(s) (a completed or globally-suppressed "
                f"node on the LCA spine, A6): {blockers[:10]}"
            )
        return {"ok": False, "error": error, "spine_blockers": blockers}

    return {
        "ok": True,
        "expand_nodes": expand_nodes,
        "plain_nodes": plain_nodes,
        "start_ids": start_ids,
        "root_is_vault_root": root_is_vault_root,
    }


# ===========================================================================
# A3/A4 -- find_tag_family_collisions: the safety checker
# ===========================================================================

# @beacon[
#   id=merge@find-tag-family-collisions,
#   slice_labels=nexus--tags,slices,
#   kind=ast,
#   comment=A3/A4: the Ruling-3 SAFETY CHECKER with the A3.2 CONGRUENT-RESIDUE rule folded INSIDE the walk. Walks every descendant of each start_id IGNORING global suppression (suppression must not hide a collision) and skipping completed nodes CHILD-ONLY (Ruling A + A5: completed DESCENDANTS are invisible; an explicit start ROOT stays completion-exempt). For every node the name is split FIRST via the canonical _resolve_split_name_and_tags and the THREE regions are scanned INDEPENDENTLY (trailing NAME tags / non-trailing base NAME text / NOTE body) -- extract_node_tags dedupes name+note, which would let a congruent trailing copy hide a duplicate note/inline occurrence of the same tag (post-audit fix). A match is TOLERATED as congruent resume residue ONLY when the node is in planned_node_forms AND the matched tag equals the exact required form (target_tag | expand_form) AND sits in the trailing NAME region. Every base-NAME or NOTE occurrence, a wrong form, an unplanned node, or an unrelated family variant is NONCONGRUENT. Counting happens DURING the full walk (never from a capped report): returns {count (true noncongruent total), collisions (first max_rows rows, document order, each with a source field), truncated, congruent_count}. Pure; no I/O.,
# ]
def find_tag_family_collisions(
    cache_by_id: dict[str, dict[str, Any]],
    start_ids: "list[str]",
    target_tag: str,
    *,
    postfix_words: "Iterable[str]",
    planned_node_forms: "dict[str, str] | None" = None,
    expand_form: "str | None" = None,
    skip_completed: bool = True,
    max_rows: int = 25,
) -> dict[str, Any]:
    """Walk descendants of each start_id and find NONCONGRUENT target-family
    tag occurrences.

    Ignores global suppression (suppression must NOT hide a collision) and
    skips completed nodes CHILD-ONLY (Ruling A + A5: completed DESCENDANTS
    are invisible; an explicit start ROOT stays completion-exempt).

    CONGRUENT-RESIDUE (A3.2) is evaluated INSIDE the walk: a matched tag is
    tolerated as no-op resume residue ONLY when ALL of these hold:
      * the node is in ``planned_node_forms`` (the planner's intended map);
      * the matched tag EQUALS the exact required form (``target_tag`` for a
        plain-planned node, ``expand_form`` for an expand-planned one);
      * the tag sits in the trailing NAME tag region (the canonical
        ``_resolve_split_name_and_tags`` split -- the same definition of
        'trailing' the tag-writing primitives use).
    A note-body tag, a non-trailing name tag, a wrong form, a match on an
    unplanned node, or an unrelated family variant is NONCONGRUENT. Counting
    happens DURING the full walk (never from a capped report), so a truncated
    report can never hide a real collision.

    The three regions (trailing NAME / non-trailing base NAME / NOTE body)
    are scanned INDEPENDENTLY so a duplicated occurrence is seen once per
    region -- a congruent trailing copy never masks a note-body or inline
    duplicate of the same tag (extract_node_tags dedupes name+note, which
    would hide it).

    Returns ``{count, collisions, truncated, congruent_count}`` where
    ``count`` is the TRUE noncongruent total, ``collisions`` its first
    ``max_rows`` rows (each ``{uuid, name, matched_tag, source}`` with
    source 'name_trailing' | 'name_inline' | 'note', in document order),
    ``truncated`` the hidden remainder, and ``congruent_count`` the
    tolerated-residue tally. When ``planned_node_forms`` is None every match
    is noncongruent (the strict brand-new behavior).
    """
    words = list(postfix_words)
    planned = dict(planned_node_forms or {})
    start_set = {str(s) for s in start_ids}

    # children index (priority-sorted: collision rows come out in document
    # order -- A12)
    children_by_parent: dict[str, list[str]] = {}
    for node in cache_by_id.values():
        pid = node.get("parent_id")
        nid = node.get("id")
        if pid is not None and nid:
            children_by_parent.setdefault(str(pid), []).append(str(nid))
    for kids in children_by_parent.values():
        kids.sort(
            key=lambda cid: (
                (cache_by_id.get(cid) or {}).get("priority")
                if isinstance((cache_by_id.get(cid) or {}).get("priority"), (int, float))
                else 0
            )
        )

    noncongruent: list[dict[str, Any]] = []
    congruent_count = 0
    visited: set[str] = set()

    def _walk(nid: str) -> None:
        nonlocal congruent_count
        if nid in visited:
            return
        visited.add(nid)
        node = cache_by_id.get(nid)
        if node is None:
            return
        # Completed policy: skip completed DESCENDANTS wholesale (never
        # descend); an explicit start ROOT is completion-exempt (A5).
        if skip_completed and nid not in start_set and _node_is_completed(node):
            return
        name = node.get("name")
        required_form = planned.get(nid)
        exact_required = (
            expand_form if required_form == "expand"
            else target_tag if required_form == "plain"
            else None
        )
        # PROVENANCE-SEPARATED scan (post-audit fix): split the name FIRST via
        # the canonical trailing-tag splitter, then scan the THREE regions
        # independently -- extract_node_tags dedupes name+note, which would
        # let a congruent trailing NAME copy hide a duplicate NOTE-body (or
        # inline NAME) occurrence of the same tag; A3.2 forbids both.
        #   * trailing NAME region: the ONLY place congruent residue can live
        #     (exact required form on a planned node).
        #   * non-trailing (base) NAME text: EVERY family match noncongruent.
        #   * NOTE body: EVERY family match noncongruent.
        base_name, trailing_tags = _resolve_split_name_and_tags(name or "")
        for t in trailing_tags:
            ts = t[1:] if t.startswith("#") else t
            if slice_tag_variant_match(ts, target_tag, postfix_words=words) is None:
                continue
            if exact_required is not None and ts == exact_required:
                congruent_count += 1
                continue
            noncongruent.append({
                "uuid": nid,
                "name": name or "",
                "matched_tag": ts,
                "source": "name_trailing",
            })
        for t in extract_tags_from_text(base_name):
            if slice_tag_variant_match(t, target_tag, postfix_words=words) is None:
                continue
            noncongruent.append({
                "uuid": nid,
                "name": name or "",
                "matched_tag": t,
                "source": "name_inline",
            })
        note = node.get("note")
        if note:
            for t in extract_tags_from_text(note):
                if slice_tag_variant_match(t, target_tag, postfix_words=words) is None:
                    continue
                noncongruent.append({
                    "uuid": nid,
                    "name": name or "",
                    "matched_tag": t,
                    "source": "note",
                })
        for cid in children_by_parent.get(nid, []):
            _walk(cid)

    for sid in start_ids:
        _walk(str(sid))

    total = len(noncongruent)
    truncated = max(0, total - max_rows)
    return {
        "count": total,
        "collisions": noncongruent[:max_rows],
        "truncated": truncated,
        "congruent_count": congruent_count,
    }


# ===========================================================================
# A7 -- build_merge_overlay_cache: dry-run overlay
# ===========================================================================

# @beacon[
#   id=merge@build-merge-overlay-cache,
#   slice_labels=nexus--tags,slices,
#   kind=ast,
#   comment=A7: build an in-memory OVERLAY cache for the pre-mutation dry-run. Shallow dict-copy of the affected node entries with the planned tags appended to their NAMEs (rendered exactly as workflowy_add_tag would append them). The overlay is a full cache_by_id dict so build_stage1_base_tree can project against it identically. Pure; no I/O.,
# ]
def build_merge_overlay_cache(
    cache_by_id: dict[str, dict[str, Any]],
    expand_nodes: "list[str]",
    plain_nodes: "list[str]",
    target_tag: str,
    expand_postfix_word: str,
) -> dict[str, dict[str, Any]]:
    """Build an overlay cache with planned tags appended to node NAMEs.

    Tags are appended as Workflowy-style trailing tags (`` #tag``), exactly
    as ``workflowy_add_tag`` would append them.  The overlay is a full
    ``cache_by_id`` dict (shallow copies of affected entries + the rest
    verbatim) so ``build_stage1_base_tree`` can project against it.
    """
    overlay: dict[str, dict[str, Any]] = dict(cache_by_id)
    expand_tag_str = f"#{target_tag}{POSTFIX_SEP}{expand_postfix_word}"
    plain_tag_str = f"#{target_tag}"

    for uid in expand_nodes:
        node = overlay.get(uid)
        if node is None:
            continue
        existing_name = node.get("name") or ""
        overlay[uid] = {
            **node,
            "name": f"{existing_name} {expand_tag_str}".strip(),
        }

    for uid in plain_nodes:
        node = overlay.get(uid)
        if node is None:
            continue
        existing_name = node.get("name") or ""
        overlay[uid] = {
            **node,
            "name": f"{existing_name} {plain_tag_str}".strip(),
        }

    return overlay


# ===========================================================================
# A7 -- simulate_merge_projection: dry-run projection
# ===========================================================================

# @beacon[
#   id=merge@simulate-merge-projection,
#   slice_labels=nexus--tags,slices,
#   kind=ast,
#   comment=A7: run a PRE-MUTATION DRY-RUN projection of the generated family through build_stage1_base_tree over the overlay cache. Returns the projected NODE SET (spine + leaves). The caller compares this against the expected set (spine ∪ merged leaves) -- mismatch catches spine-blocked merges (A6), compiler bugs, and postfix-generation mistakes BEFORE a single tag is written. For the vault-root case (lca=None), simulates once per top-level start root and unions the node sets. Pure; no I/O.,
# ]
def simulate_merge_projection(
    overlay_cache: dict[str, dict[str, Any]],
    start_ids: "list[str]",
    target_tag: str,
    *,
    auto_expand_postfix: str = DEFAULT_AUTO_EXPAND_POSTFIX,
    suppress_postfix: str = DEFAULT_SUPPRESS_POSTFIX,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
    global_never_expand_tags: "Iterable[str]" = (),
    exclude_completed: bool = True,
    match_scope: str = "name_and_note",
) -> "set[str] | None":
    """Dry-run: project the generated family from the overlay cache and
    return the node set.

    For the vault-root case (multiple start_ids), simulates once per
    top-level start root and unions the node sets.

    Returns the set of node ids in the projected tree, or ``None`` on
    projection failure (e.g. a root not in the overlay cache).
    """
    all_node_ids: set[str] = set()

    for sid in start_ids:
        try:
            final_subtree, _plan, _prune_meta = build_stage1_base_tree(
                overlay_cache,
                sid,
                [target_tag],
                auto_expand_postfix=auto_expand_postfix,
                suppress_postfix=suppress_postfix,
                deprecated_postfix=deprecated_postfix,
                global_never_expand_tags=global_never_expand_tags,
                exclude_completed=exclude_completed,
                match_scope=match_scope,
                boundary_predicate=None,
            )
        except SliceProjectionInvariantError:
            return None
        if final_subtree is None:
            return None

        # Collect ALL node ids in the final tree (spine + leaves)
        def _collect_ids(node: dict[str, Any]) -> None:
            nid = node.get("id")
            if nid:
                all_node_ids.add(str(nid))
            for child in (node.get("children") or []):
                if isinstance(child, dict):
                    _collect_ids(child)

        _collect_ids(final_subtree)

    return all_node_ids


# ===========================================================================
# A3.4 -- verify_merge_projection: post-mutation verification
# ===========================================================================

# @beacon[
#   id=merge@verify-merge-projection,
#   slice_labels=nexus--tags,slices,
#   kind=ast,
#   comment=A3.4: POST-MUTATION VERIFICATION. After a fully-successful mutation, re-project the generated family from the (patched) cache via build_stage1_base_tree and compare the node set against expected (spine ∪ merged leaves). Returns {matched: bool, missing: [...], unexpected: [...]} so the orchestrator can gate persistence and emit the verification block (A12). Shares the comparison core with the dry-run (simulate_merge_projection). Pure; no I/O.,
# ]
def verify_merge_projection(
    cache_by_id: dict[str, dict[str, Any]],
    start_ids: "list[str]",
    target_tag: str,
    expected_node_ids: set[str],
    *,
    auto_expand_postfix: str = DEFAULT_AUTO_EXPAND_POSTFIX,
    suppress_postfix: str = DEFAULT_SUPPRESS_POSTFIX,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
    global_never_expand_tags: "Iterable[str]" = (),
    exclude_completed: bool = True,
    match_scope: str = "name_and_note",
) -> dict[str, Any]:
    """Post-mutation verification: re-project from the (patched) cache and
    compare the node set against ``expected_node_ids``.

    Returns ``{matched: bool, missing: [...], unexpected: [...]}``.
    """
    projected = simulate_merge_projection(
        cache_by_id,
        start_ids,
        target_tag,
        auto_expand_postfix=auto_expand_postfix,
        suppress_postfix=suppress_postfix,
        deprecated_postfix=deprecated_postfix,
        global_never_expand_tags=global_never_expand_tags,
        exclude_completed=exclude_completed,
        match_scope=match_scope,
    )
    if projected is None:
        return {
            "matched": False,
            "missing": sorted(expected_node_ids),
            "unexpected": [],
            "error": "projection returned None (a start root is not in the cache)",
        }

    missing = sorted(expected_node_ids - projected)
    unexpected = sorted(projected - expected_node_ids)
    return {
        "matched": not missing and not unexpected,
        "missing": missing,
        "unexpected": unexpected,
    }
