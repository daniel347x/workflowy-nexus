"""Regression lock: the Phase-3 GLOBAL-NEVER hydration seal (ap:HYDSUP).

THE BUG THIS LOCKS: ``apply_expansion_plan_to_subtree``'s EXPAND hydration
(``_hydrate_from_cache``) re-grafts an expansion target's FULL descendant
subtree straight from the raw /nodes-export cache. A node carrying a
GLOBAL-never-expand suppression tag (``f8_global_never_expand_tags``, e.g.
``#wfy-suppress``) that is NOT a slice member never enters the Phase-1 minimal
subtree, so Phase 2 never classifies it and no contraction op covers it --
yet an ``--expand`` ANCESTOR of it would silently RE-INFLATE it (and its whole
subtree) from the raw cache. The shared-funnel hydration leak.

THE FIX (ap:HYDSUP, one-function): ``apply_expansion_plan_to_subtree`` gains a
``global_never_expand_tags`` keyword operand (default ``()``, so no other
caller changes; forwarded by ``build_stage1_base_tree``, which already
receives it), and the hydration walk skips + does-not-descend into any CHILD
whose NAME carries an EXACT-match global-never stem -- the SAME shape as the
existing completed-exclusion hydration seal ("LOAD-BEARING: skip a completed
CHILD entirely"), and the SAME NAME-only, exact-match semantics as
``node_has_suppress_ancestor`` (so the config field means ONE thing in Phase
2, in the render_symbols resolution filter, and here).

This fixes the leak for PERSISTED slice groups (the shared funnel) AND lets
Lightning Rod ``render_tags`` ``postfix_policy='slice'`` (ap:IMPPTX) pass the
real ``f8_global_never_expand_tags`` list into the funnel with absolute
suppression intact.

These tests drive the SHARED funnel ``build_stage1_base_tree`` end-to-end
over tiny in-memory caches and lock:
  1. a suppressed node under an ``--expand`` ancestor is excluded (the leak);
  2. a suppressed node nested under an UNTAGGED rehydrated parent is excluded;
  3. active-family ``--suppress`` still works (expand-first-then-collapse +
     Phase-4 zombie prune) with the operand threaded;
  4. the completed-exclusion seal is unaffected;
  5. the anchor-boundary seal is unaffected;
  6. default ``()`` preserves the prior (leaky) behavior byte-for-byte -- the
     seal is the ONLY guard, and un-threaded callers are unchanged;
  7. the seal is NAME-only (a note-only suppression tag does NOT seal) --
     mirroring ``node_has_suppress_ancestor`` exactly.

Pure-logic only: no MCP server, no WebSocket, no cache I/O.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.f8_slice_policy import (  # noqa: E402
    build_stage1_base_tree,
)


def _mk(nid, name, parent, prio, note=None, completed=False):
    return {
        "id": nid,
        "name": name,
        "note": note,
        "parent_id": parent,
        "priority": prio,
        **({"completed": True} if completed else {}),
    }


def _ids(subtree):
    """Set of node ids present in a materialized {id,name,note,children} tree."""
    if subtree is None:
        return None
    out = {subtree["id"]}
    for ch in subtree.get("children", []):
        out |= _ids(ch)
    return out


# ---------------------------------------------------------------------------
# 1. THE LEAK: a globally-suppressed node under an --expand ancestor is
#    excluded (with its whole subtree), even though Phase 2 never saw it.
# ---------------------------------------------------------------------------
def test_suppress_under_expand_excluded():
    cache = {
        "root": _mk("root", "Root #foo--expand", None, 0),
        "pocket": _mk("pocket", "Pocket #wfy-suppress", "root", 1),
        "secret": _mk("secret", "Secret", "pocket", 1),
        "live": _mk("live", "Live #foo", "root", 2),
    }
    final_subtree, plan, prune_meta = build_stage1_base_tree(
        cache, "root", ["foo"], global_never_expand_tags=["wfy-suppress"],
    )
    assert final_subtree is not None
    # root expands; live is a member; pocket + its whole subtree never render.
    assert _ids(final_subtree) == {"root", "live"}
    assert "root" in plan["expand"]
    assert prune_meta["pruned_zombie_count"] == 0


# ---------------------------------------------------------------------------
# 2. A suppressed node nested under an UNTAGGED (rehydrated) parent is
#    excluded: hydration descends through the untagged parent but seals the
#    suppressed child.
# ---------------------------------------------------------------------------
def test_suppress_nested_under_untagged_rehydrated_parent_excluded():
    cache = {
        "root": _mk("root", "Root #foo--expand", None, 0),
        "plain": _mk("plain", "Plain untagged", "root", 1),
        "pocket": _mk("pocket", "Pocket #wfy-suppress", "plain", 1),
        "secret": _mk("secret", "Secret", "pocket", 1),
        "live": _mk("live", "Live #foo", "root", 2),
    }
    final_subtree, plan, _prune = build_stage1_base_tree(
        cache, "root", ["foo"], global_never_expand_tags=["wfy-suppress"],
    )
    assert final_subtree is not None
    # 'plain' is hydrated (expansion reveals non-members); the suppressed
    # pocket under it is sealed at hydration time.
    assert _ids(final_subtree) == {"root", "plain", "live"}


# ---------------------------------------------------------------------------
# 3. Active-family --suppress still works with the operand threaded:
#    expand-first-then-collapse carves the pocket, Phase 4 prunes the zombie.
# ---------------------------------------------------------------------------
def test_active_family_suppress_still_collapses_and_prunes():
    cache = {
        "root": _mk("root", "Root #foo--expand", None, 0),
        "pocket": _mk("pocket", "Pocket #foo--suppress", "root", 1),
        "secret": _mk("secret", "Secret", "pocket", 1),
        "live": _mk("live", "Live #foo", "root", 2),
    }
    final_subtree, plan, prune_meta = build_stage1_base_tree(
        cache, "root", ["foo"], global_never_expand_tags=["wfy-suppress"],
    )
    assert final_subtree is not None
    # The family --suppress pocket is classified by Phase 2 (it IS a slice
    # member), collapsed by Phase 3 AFTER the expansion, and its childless
    # zombie pruned by Phase 4. The global-never seal did not interfere.
    assert "pocket" in plan["collapse_suppressed"]
    assert prune_meta["pruned_zombie_count"] == 1
    assert _ids(final_subtree) == {"root", "live"}


# ---------------------------------------------------------------------------
# 4. The completed-exclusion seal is UNAFFECTED by the new operand.
# ---------------------------------------------------------------------------
def test_completed_exclusion_seal_unaffected():
    cache = {
        "root": _mk("root", "Root #foo--expand", None, 0),
        "done": _mk("done", "Done child", "root", 1, completed=True),
        "live": _mk("live", "Live #foo", "root", 2),
    }
    final_subtree, plan, _prune = build_stage1_base_tree(
        cache, "root", ["foo"],
        exclude_completed=True,
        global_never_expand_tags=["wfy-suppress"],
    )
    assert final_subtree is not None
    assert _ids(final_subtree) == {"root", "live"}


# ---------------------------------------------------------------------------
# 5. The anchor-boundary seal is UNAFFECTED by the new operand: a boundary
#    child hydrates as a childless leaf; its subtree is not re-inflated.
# ---------------------------------------------------------------------------
def test_anchor_boundary_seal_unaffected():
    cache = {
        "root": _mk("root", "Root #foo--expand", None, 0),
        "anchor": _mk("anchor", "Child anchor #foo", "root", 1),
        "hidden": _mk("hidden", "Hidden grandchild", "anchor", 1),
        "live": _mk("live", "Live #foo", "root", 2),
    }
    boundary = lambda n: n.get("id") == "anchor"  # noqa: E731
    final_subtree, plan, _prune = build_stage1_base_tree(
        cache, "root", ["foo"],
        global_never_expand_tags=["wfy-suppress"],
        boundary_predicate=boundary,
    )
    assert final_subtree is not None
    assert _ids(final_subtree) == {"root", "anchor", "live"}


# ---------------------------------------------------------------------------
# 6. Default () preserves the PRIOR (leaky) behavior byte-for-byte: an
#    un-threaded caller still re-hydrates the suppressed subtree. (Locks that
#    the seal is the ONLY guard and that default threading is a no-op.)
# ---------------------------------------------------------------------------
def test_default_empty_list_preserves_prior_behavior():
    cache = {
        "root": _mk("root", "Root #foo--expand", None, 0),
        "pocket": _mk("pocket", "Pocket #wfy-suppress", "root", 1),
        "secret": _mk("secret", "Secret", "pocket", 1),
        "live": _mk("live", "Live #foo", "root", 2),
    }
    final_subtree, plan, _prune = build_stage1_base_tree(
        cache, "root", ["foo"],  # global_never_expand_tags defaults to ()
    )
    assert final_subtree is not None
    # Without the operand the expansion re-hydrates EVERYTHING (the old leak).
    assert _ids(final_subtree) == {"root", "pocket", "secret", "live"}


# ---------------------------------------------------------------------------
# 7. The seal is NAME-only, exactly like node_has_suppress_ancestor: a
#    suppression tag living only in the NOTE does NOT seal the child.
#    (This is the UNIFORM doctrine now: Phase 2's has_global_never_expand is
#    NAME-only too -- see the doctrine lock below.)
# ---------------------------------------------------------------------------
def test_suppression_tag_in_note_does_not_seal():
    cache = {
        "root": _mk("root", "Root #foo--expand", None, 0),
        "pocket": _mk("pocket", "Pocket", "root", 1, note="see #wfy-suppress"),
        "secret": _mk("secret", "Secret", "pocket", 1),
        "live": _mk("live", "Live #foo", "root", 2),
    }
    final_subtree, plan, _prune = build_stage1_base_tree(
        cache, "root", ["foo"], global_never_expand_tags=["wfy-suppress"],
    )
    assert final_subtree is not None
    # NAME-only semantics: the note-only tag does not seal; the expansion
    # reveals the pocket (and its child) exactly like any non-member.
    assert _ids(final_subtree) == {"root", "pocket", "secret", "live"}


# ===========================================================================
# GLOBAL-NEVER DOCTRINE (Dan, 2026-07, post-IMPPTX audit; tightened by the
# 084 ruling): TRAILING-TAG-REGION-ONLY, EVERYWHERE, via the ONE shared
# helper pair (normalize_global_never_stems + name_has_global_never_tag).
# Before this lock, Phase 2's classify_node_tags read global-never from
# NAME+NOTE (via extract_node_tags) while the HYDSUP Phase-3 seal read
# NAME-only -- so the SAME node was suppressed or leaked depending on
# whether Phase 1 happened to include it. The unified doctrine: global-never
# is an ABSOLUTE STRUCTURAL control, so it must be a deliberate TRAILING
# NAME tag; a note-only or mid-title occurrence is inert (under every
# scope). The active-family --expand/--suppress/--deprecated flags stay
# NAME+NOTE under the default 'name_and_note' scope (the parity reading --
# note-authored ops still classify) and read the TRAILING tag region under
# match_scope='name' (084; locked in the companion test below and in
# test_tag_scope_trailing_only.py).
# ===========================================================================

from workflowy_mcp.f8_slice_policy import (  # noqa: E402
    classify_node_tags,
    name_has_global_never_tag,
    node_has_suppress_ancestor,
    normalize_global_never_stems,
)


def test_normalize_global_never_stems_normalization():
    assert normalize_global_never_stems(["#wfy-suppress", " wfy-hide ", "", "  "]) == {
        "wfy-suppress",
        "wfy-hide",
    }
    assert normalize_global_never_stems([]) == set()
    assert normalize_global_never_stems(None) == set()


def test_name_has_global_never_tag_is_name_only_and_exact():
    stems = {"wfy-suppress"}
    assert name_has_global_never_tag("Pocket #wfy-suppress", stems) is True
    assert name_has_global_never_tag("Pocket #wfy-suppress-x", stems) is False  # exact, not prefix
    assert name_has_global_never_tag("Pocket", stems) is False
    assert name_has_global_never_tag("Pocket", set()) is False


def test_classify_global_never_name_only_but_family_ops_stay_name_plus_note():
    # NAME-tagged global-never -> classified.
    f = classify_node_tags(
        "Pocket #wfy-suppress", ["foo"], global_never_expand_tags=["wfy-suppress"],
    )
    assert f["has_global_never_expand"] is True
    # NOTE-only global-never -> deliberately INERT (the unified doctrine),
    # while a NOTE-authored family --suppress STILL classifies (NAME+NOTE
    # operation policy, the audited compatibility truth).
    f2 = classify_node_tags(
        "Pocket #foo--x",
        ["foo"],
        note="ops #wfy-suppress and #foo--suppress",
        global_never_expand_tags=["wfy-suppress"],
    )
    assert f2["has_global_never_expand"] is False
    assert f2["has_suppress"] is True


def test_classify_family_ops_trailing_only_under_name_scope():
    # 084 companion: under match_scope='name' the family-postfix
    # classification reads ONLY the name's TRAILING tag region (the same
    # reader membership uses). The SAME node as the default-scope test
    # above: the note-authored --suppress no longer classifies; a trailing
    # postfix still does; a mid-title postfix is prose under 'name' while
    # still an operation under the parity default; a mid-title global tag
    # is inert under EVERY scope.
    f = classify_node_tags(
        "Pocket #foo--x",
        ["foo"],
        note="ops #wfy-suppress and #foo--suppress",
        global_never_expand_tags=["wfy-suppress"],
        match_scope="name",
    )
    assert f["has_suppress"] is False
    assert f["has_global_never_expand"] is False
    # Trailing still classifies under 'name'.
    assert classify_node_tags("Pocket #foo--suppress", ["foo"], match_scope="name")[
        "has_suppress"
    ] is True
    # Mid-title: prose under 'name', still an operation under parity.
    assert classify_node_tags(
        "Discuss #foo--suppress in prose", ["foo"], match_scope="name"
    )["has_suppress"] is False
    assert classify_node_tags(
        "Discuss #foo--suppress in prose", ["foo"], match_scope="name_and_note"
    )["has_suppress"] is True
    # Mid-title global-never: inert under EVERY scope (084 D1).
    assert classify_node_tags(
        "Mentions #wfy-suppress mid-title",
        ["foo"],
        global_never_expand_tags=["wfy-suppress"],
    )["has_global_never_expand"] is False


def test_node_has_suppress_ancestor_name_only():
    cache = {
        "root": _mk("root", "Root", None, 0, note="#wfy-suppress"),
        "kid": _mk("kid", "Kid", "root", 1),
    }
    assert node_has_suppress_ancestor(cache, "kid", ["wfy-suppress"]) is False
    cache["root"]["name"] = "Root #wfy-suppress"
    assert node_has_suppress_ancestor(cache, "kid", ["wfy-suppress"]) is True


def test_doctrine_note_only_global_never_member_is_not_suppressed():
    # THE unified-doctrine lock: a slice MEMBER whose NOTE carries
    # #wfy-suppress is NOT suppressed (note-only global-never is inert), and
    # the expansion reveals it like any member.
    cache = {
        "root": _mk("root", "Root #foo--expand", None, 0),
        "pocket": _mk("pocket", "Pocket #foo", "root", 1, note="see #wfy-suppress"),
        "live": _mk("live", "Live #foo", "root", 2),
    }
    final_subtree, plan, _prune = build_stage1_base_tree(
        cache, "root", ["foo"], global_never_expand_tags=["wfy-suppress"],
    )
    assert final_subtree is not None
    assert _ids(final_subtree) == {"root", "pocket", "live"}
    assert "pocket" not in plan["collapse_suppressed"]


def test_doctrine_name_tagged_global_never_member_is_suppressed():
    # The same member, NAME-tagged -> classified + collapsed + Phase-4 pruned.
    cache = {
        "root": _mk("root", "Root #foo--expand", None, 0),
        "pocket": _mk("pocket", "Pocket #foo #wfy-suppress", "root", 1),
        "secret": _mk("secret", "Secret", "pocket", 1),
        "live": _mk("live", "Live #foo", "root", 2),
    }
    final_subtree, plan, prune_meta = build_stage1_base_tree(
        cache, "root", ["foo"], global_never_expand_tags=["wfy-suppress"],
    )
    assert final_subtree is not None
    assert "pocket" in plan["collapse_suppressed"]
    # Phase 2 classified it (it IS a member, in the Phase-1 tree), but the
    # HYDSUP seal then removes it at HYDRATION time -- it is never grafted
    # back when the enclosing expansion re-hydrates -- so no collapse zombie
    # is left behind for Phase 4 to count (contrast with the family
    # --suppress case above, which IS grafted-then-collapsed-then-pruned).
    assert prune_meta["pruned_zombie_count"] == 0
    assert _ids(final_subtree) == {"root", "live"}
