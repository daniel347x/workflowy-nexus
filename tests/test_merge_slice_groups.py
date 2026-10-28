"""tests/test_merge_slice_groups.py -- MergeSliceGroups test suite.

Covers (per MRGSPC §10 + MRGAMD A13):
  1. Pure planner/safety tests over synthetic caches (union, intersection,
     expand coverage, plain leaves, suppressed pockets, completed nodes).
  2. Spec normalizer rejections (<2 groups, bad mode, empty/whitespace/
     postfix-carrying tag, dynamic ref missing root/tags, unknown wire
     fields, wrong wire version).
  3. Effective-leaf extraction (A1): no-membership-hit group, pruned
     connective leaf, expand-admitted leaves, rescued deprecated ancestor.
  4. Deep coverage (A2): internal nodes absent from merged_leaf_ids.
  5. Congruent-residue + bidirectional + global-never safety checks (A3/A4).
  6. Vault-root (lca=None) case.
  7. Completed-node invisibility across all checks (Ruling A / A5).
  8. N-ary union (N=3).
  9. Mixed named+dynamic inputs.
"""

from __future__ import annotations

import pytest

from workflowy_mcp.merge_slice_groups_spec import (
    MergeSliceGroupsSpec,
    MergeGroupRef,
    _normalize_group_refs,
)
from workflowy_mcp.merge_slice_groups_policy import (
    collect_effective_slice_leaf_ids,
    compute_merge_full_cover,
    compute_merge_tag_plan,
    find_tag_family_collisions,
    build_merge_overlay_cache,
    simulate_merge_projection,
    verify_merge_projection,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _n(nid, name="", children=None, note=None, completed=False, parent_id=None):
    node = {
        "id": nid,
        "name": name,
        "note": note,
        "children": children or [],
        "parent_id": parent_id,
    }
    if completed:
        node["completed"] = True
        node["completedAt"] = 1234567890
    return node


def _build_cache(nodes_flat):
    """Build a cache_by_id dict from a flat list of node dicts."""
    return {n["id"]: n for n in nodes_flat}


def _flat(nodes):
    """Flatten a tree of _n() nodes into a list."""
    out = []
    def _walk(n):
        out.append(n)
        for c in n.get("children", []):
            _walk(c)
    for n in nodes:
        _walk(n)
    return out


def _tree(nodes_flat):
    """Build a tree from flat nodes (with parent_id) and return the roots."""
    by_id = {n["id"]: n for n in nodes_flat}
    roots = []
    for n in nodes_flat:
        pid = n.get("parent_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(n)
        else:
            roots.append(n)
    return roots


# ===========================================================================
# 1. Spec normalizer tests
# ===========================================================================

class TestSpecNormalizer:
    def test_two_named_groups(self):
        spec, err = MergeSliceGroupsSpec.normalize_from_tool_args(
            groups=["group-a", "group-b"],
            mode="union",
            tag="merged",
        )
        assert err is None
        assert spec is not None
        assert len(spec.groups) == 2
        assert spec.mode == "union"
        assert spec.tag == "merged"
        assert spec.exclude_completed is True
        # Contract [ap:LQ8RDA]+[ap:DR0C0W] (MergeSliceGroupsSpec per-group
        # projection default flip): this spec default was 'name_and_note' and is
        # now 'name' -- a semantic tag search reads the trailing tag region.
        assert spec.match_scope == "name"

    def test_dynamic_group(self):
        spec, err = MergeSliceGroupsSpec.normalize_from_tool_args(
            groups=[
                {"root_uuid": "abc-123", "tags": ["foo", "bar"]},
                "named-group",
            ],
            mode="intersection",
            tag="merged",
        )
        assert err is None
        assert spec is not None
        assert spec.groups[0].kind == "dynamic"
        assert spec.groups[0].root_uuid == "abc-123"
        assert spec.groups[0].tags == ("foo", "bar")
        assert spec.groups[1].kind == "named"

    def test_fewer_than_2_groups(self):
        _, err = MergeSliceGroupsSpec.normalize_from_tool_args(
            groups=["only-one"],
            mode="union",
            tag="merged",
        )
        assert err is not None
        assert "at least 2" in err

    def test_bad_mode(self):
        _, err = MergeSliceGroupsSpec.normalize_from_tool_args(
            groups=["a", "b"],
            mode="xor",
            tag="merged",
        )
        assert err is not None
        assert "union" in err or "intersection" in err

    def test_empty_tag(self):
        _, err = MergeSliceGroupsSpec.normalize_from_tool_args(
            groups=["a", "b"],
            mode="union",
            tag="",
        )
        assert err is not None

    def test_postfix_carrying_tag(self):
        _, err = MergeSliceGroupsSpec.normalize_from_tool_args(
            groups=["a", "b"],
            mode="union",
            tag="foo--expand",
        )
        assert err is not None
        assert "postfix" in err.lower() or "--" in err

    def test_dynamic_missing_root(self):
        _, err = MergeSliceGroupsSpec.normalize_from_tool_args(
            groups=[{"tags": ["foo"]}, "named"],
            mode="union",
            tag="merged",
        )
        assert err is not None
        assert "root_uuid" in err

    def test_dynamic_missing_tags(self):
        _, err = MergeSliceGroupsSpec.normalize_from_tool_args(
            groups=[{"root_uuid": "abc"}, "named"],
            mode="union",
            tag="merged",
        )
        assert err is not None
        assert "tags" in err

    def test_dynamic_postfix_tag_rejected(self):
        _, err = MergeSliceGroupsSpec.normalize_from_tool_args(
            groups=[{"root_uuid": "abc", "tags": ["foo--expand"]}, "named"],
            mode="union",
            tag="merged",
        )
        assert err is not None
        assert "postfix" in err.lower() or "--" in err

    def test_from_wire_ok(self):
        spec, err = MergeSliceGroupsSpec.from_wire({
            "action": "merge_slice_groups",
            "request_id": "r1",
            "endpoint_id": "ep1",
            "v": 1,
            "groups": ["a", "b"],
            "mode": "union",
            "tag": "merged",
        })
        assert err is None
        assert spec is not None
        assert spec.tag == "merged"

    def test_from_wire_unknown_field(self):
        _, err = MergeSliceGroupsSpec.from_wire({
            "action": "merge_slice_groups",
            "request_id": "r1",
            "v": 1,
            "groups": ["a", "b"],
            "mode": "union",
            "tag": "merged",
            "bogus_field": 1,
        })
        assert err is not None
        assert "unknown field" in err.lower()

    def test_from_wire_wrong_version(self):
        _, err = MergeSliceGroupsSpec.from_wire({
            "action": "merge_slice_groups",
            "request_id": "r1",
            "v": 2,
            "groups": ["a", "b"],
            "mode": "union",
            "tag": "merged",
        })
        assert err is not None
        assert "version" in err.lower()

    def test_from_wire_rejects_target_tab(self):
        """A10: target_tab is NOT a spec field; the wire frame must reject it."""
        _, err = MergeSliceGroupsSpec.from_wire({
            "action": "merge_slice_groups",
            "request_id": "r1",
            "v": 1,
            "groups": ["a", "b"],
            "mode": "union",
            "tag": "merged",
            "target_tab": "spine",
        })
        assert err is not None
        assert "unknown field" in err.lower()

    def test_to_dict_roundtrip(self):
        spec, _ = MergeSliceGroupsSpec.normalize_from_tool_args(
            groups=["a", {"root_uuid": "r", "tags": ["t"]}],
            mode="intersection",
            tag="merged",
        )
        d = spec.to_dict()
        assert d["mode"] == "intersection"
        assert d["tag"] == "merged"
        assert len(d["groups"]) == 2


# ===========================================================================
# 2. Effective-leaf extraction (A1)
# ===========================================================================

class TestEffectiveLeafIds:
    def test_no_membership_hit_group(self):
        """A group with zero matching members contributes nothing, NOT {root}."""
        root = _n("root", "Root #other", parent_id=None)
        child = _n("c1", "Child", parent_id="root")
        root["children"] = [child]
        cache = _build_cache(_flat([root]))

        final_subtree = {
            "id": "root",
            "name": "Root #other",
            "note": None,
            "children": [],
        }
        plan = {"expand": [], "warnings": []}
        result = collect_effective_slice_leaf_ids(
            final_subtree, plan, ["tag-a"],
        )
        assert result == set()

    def test_directly_live_leaf(self):
        leaf = _n("l1", "Leaf #tag-a", parent_id="root")
        final_subtree = {
            "id": "root",
            "name": "Root",
            "note": None,
            "children": [{"id": "l1", "name": "Leaf #tag-a", "note": None, "children": []}],
        }
        plan = {"expand": [], "warnings": []}
        result = collect_effective_slice_leaf_ids(
            final_subtree, plan, ["tag-a"],
        )
        assert result == {"l1"}

    def test_expansion_admitted_leaf(self):
        """A leaf pulled in by --expand hydration is effective."""
        final_subtree = {
            "id": "root",
            "name": "Root",
            "note": None,
            "children": [
                {
                    "id": "exp",
                    "name": "Expand #tag-a--expand",
                    "note": None,
                    "children": [
                        {"id": "l2", "name": "Child", "note": None, "children": []},
                    ],
                },
            ],
        }
        plan = {"expand": ["exp"], "warnings": []}
        result = collect_effective_slice_leaf_ids(
            final_subtree, plan, ["tag-a"],
        )
        assert "l2" in result

    def test_deprecated_leaf_not_effective(self):
        """A deprecated leaf is NOT effective (is_slice_member returns False)."""
        final_subtree = {
            "id": "root",
            "name": "Root",
            "note": None,
            "children": [
                {"id": "l3", "name": "Leaf #tag-a--deprecated", "note": None, "children": []},
            ],
        }
        plan = {"expand": [], "warnings": []}
        result = collect_effective_slice_leaf_ids(
            final_subtree, plan, ["tag-a"],
        )
        assert result == set()


# ===========================================================================
# 3. Coverage computation (A2)
# ===========================================================================

class TestComputeMergeFullCover:
    def test_deep_coverage(self):
        """Internal nodes absent from merged_leaf_ids don't block coverage."""
        root = _n("root", "Root", parent_id=None)
        a = _n("a", "A", parent_id="root")
        b = _n("b", "B", parent_id="a")
        c = _n("c", "C #tag", parent_id="b")
        root["children"] = [a]
        a["children"] = [b]
        b["children"] = [c]
        cache = _build_cache(_flat([root]))

        cover = compute_merge_full_cover(
            cache, ["root"], {"c"}, set(),
        )
        # a and b cover because their only visible terminal leaf is c (wanted)
        assert "a" in cover
        assert "b" in cover
        # root also covers
        assert "root" in cover

    def test_partial_coverage(self):
        """A branch with an unwanted leaf does NOT cover."""
        root = _n("root", "Root", parent_id=None)
        a = _n("a", "A", parent_id="root")
        b = _n("b", "B", parent_id="root")
        c = _n("c", "C #tag", parent_id="a")
        d = _n("d", "D", parent_id="b")
        root["children"] = [a, b]
        a["children"] = [c]
        b["children"] = [d]
        cache = _build_cache(_flat([root]))

        cover = compute_merge_full_cover(
            cache, ["root"], {"c"}, set(),
        )
        assert "a" in cover  # a covers (only leaf c is wanted)
        assert "b" not in cover  # b has unwanted leaf d
        assert "root" not in cover  # root has unwanted descendant d

    def test_suppressed_descendant_ignored(self):
        """A suppressed descendant does NOT poison coverage."""
        root = _n("root", "Root", parent_id=None)
        a = _n("a", "A", parent_id="root")
        s = _n("s", "Suppressed #wfy-suppress", parent_id="root")
        c = _n("c", "C #tag", parent_id="a")
        root["children"] = [a, s]
        a["children"] = [c]
        cache = _build_cache(_flat([root]))

        cover = compute_merge_full_cover(
            cache, ["root"], {"c"}, {"wfy-suppress"},
        )
        assert "a" in cover
        assert "root" in cover  # s is suppression-invisible, doesn't poison

    def test_completed_descendant_ignored(self):
        """A completed descendant does NOT poison coverage (Ruling A)."""
        root = _n("root", "Root", parent_id=None)
        a = _n("a", "A", parent_id="root")
        done = _n("done", "Done", parent_id="root", completed=True)
        c = _n("c", "C #tag", parent_id="a")
        root["children"] = [a, done]
        a["children"] = [c]
        cache = _build_cache(_flat([root]))

        cover = compute_merge_full_cover(
            cache, ["root"], {"c"}, set(), skip_completed=True,
        )
        assert "a" in cover
        assert "root" in cover  # done is completed-invisible


# ===========================================================================
# 4. Tag plan (Phases C+D+E)
# ===========================================================================

class TestComputeMergeTagPlan:
    def test_expand_earned_at_fully_covered_branch(self):
        """A fully-covered branch earns ONE expand tag; no descendants tagged.
        When the ROOT is fully covered, the ROOT earns the expand tag (it is
        the highest-level fully-covered branch)."""
        root = _n("root", "Root", parent_id=None)
        a = _n("a", "A", parent_id="root")
        l1 = _n("l1", "L1 #tag", parent_id="a")
        l2 = _n("l2", "L2 #tag", parent_id="a")
        root["children"] = [a]
        a["children"] = [l1, l2]
        cache = _build_cache(_flat([root]))

        result = compute_merge_tag_plan(
            cache, {"l1", "l2"}, "root",
            target_tag="merged", expand_postfix_word="expand",
        )
        assert result["ok"] is True
        assert result["expand_nodes"] == ["root"]
        assert result["plain_nodes"] == []

    def test_plain_tags_for_uncovered_leaves(self):
        """When a branch has an unwanted terminal leaf, no expand is earned
        at that branch; the uncovered merged leaves get plain tags."""
        root = _n("root", "Root", parent_id=None)
        a = _n("a", "A", parent_id="root")
        b = _n("b", "B", parent_id="root")
        l1 = _n("l1", "L1 #tag", parent_id="a")
        l2 = _n("l2", "L2", parent_id="b")
        root["children"] = [a, b]
        a["children"] = [l1]
        b["children"] = [l2]
        cache = _build_cache(_flat([root]))

        # Only l1 is in the merged set; l2 is NOT wanted.
        result = compute_merge_tag_plan(
            cache, {"l1"}, "root",
            target_tag="merged", expand_postfix_word="expand",
        )
        assert result["ok"] is True
        # root is NOT fully covered (b->l2 is unwanted), so no expand at root.
        # a IS fully covered (its only terminal leaf l1 is wanted), so a gets expand.
        assert result["expand_nodes"] == ["a"]
        assert result["plain_nodes"] == []

    def test_mixed_expand_and_plain(self):
        """Some branches earn expand; remaining leaves get plain tags.
        Root is not fully covered (b has an unwanted leaf), so no expand at root."""
        root = _n("root", "Root", parent_id=None)
        a = _n("a", "A", parent_id="root")
        b = _n("b", "B", parent_id="root")
        l1 = _n("l1", "L1 #tag", parent_id="a")
        l2 = _n("l2", "L2 #tag", parent_id="a")
        l3 = _n("l3", "L3", parent_id="b")
        unwanted = _n("uw", "Unwanted", parent_id="b")
        root["children"] = [a, b]
        a["children"] = [l1, l2]
        b["children"] = [l3, unwanted]
        cache = _build_cache(_flat([root]))

        result = compute_merge_tag_plan(
            cache, {"l1", "l2", "l3"}, "root",
            target_tag="merged", expand_postfix_word="expand",
        )
        assert result["ok"] is True
        assert result["expand_nodes"] == ["a"]
        assert result["plain_nodes"] == ["l3"]

    def test_planner_completeness_assertion(self):
        """Every merged leaf must be covered by expand or plain."""
        root = _n("root", "Root", parent_id=None)
        a = _n("a", "A", parent_id="root")
        l1 = _n("l1", "L1 #tag", parent_id="a")
        root["children"] = [a]
        a["children"] = [l1]
        cache = _build_cache(_flat([root]))

        # Normal case: should pass
        result = compute_merge_tag_plan(
            cache, {"l1"}, "root",
            target_tag="merged", expand_postfix_word="expand",
        )
        assert result["ok"] is True

    def test_vault_root(self):
        """lca=None -> start at top-levels, root_is_vault_root=True.
        Each top-level node that fully covers earns expand."""
        t1 = _n("t1", "T1", parent_id=None)
        t2 = _n("t2", "T2", parent_id=None)
        l1 = _n("l1", "L1 #tag", parent_id="t1")
        l2 = _n("l2", "L2 #tag", parent_id="t2")
        t1["children"] = [l1]
        t2["children"] = [l2]
        cache = _build_cache(_flat([t1, t2]))

        result = compute_merge_tag_plan(
            cache, {"l1", "l2"}, None,
            target_tag="merged", expand_postfix_word="expand",
        )
        assert result["ok"] is True
        assert result["root_is_vault_root"] is True
        assert set(result["start_ids"]) == {"t1", "t2"}
        # Each top-level fully covers its subtree (t1->l1, t2->l2 all wanted),
        # so each earns expand at the top level.
        assert set(result["expand_nodes"]) == {"t1", "t2"}
        assert result["plain_nodes"] == []


# ===========================================================================
# 5. Safety checker (A3/A4)
# ===========================================================================

class TestFindTagFamilyCollisions:
    def test_no_collisions(self):
        root = _n("root", "Root", parent_id=None)
        cache = _build_cache(_flat([root]))
        result = find_tag_family_collisions(
            cache, ["root"], "newtag",
            postfix_words=["expand", "suppress", "deprecated"],
        )
        assert result["count"] == 0

    def test_exact_match(self):
        root = _n("root", "Root", parent_id=None)
        child = _n("c1", "Child #foo", parent_id="root")
        root["children"] = [child]
        cache = _build_cache(_flat([root]))
        result = find_tag_family_collisions(
            cache, ["root"], "foo",
            postfix_words=["expand", "suppress", "deprecated"],
        )
        assert result["count"] == 1
        assert result["collisions"][0]["matched_tag"] == "foo"

    def test_family_variant_match(self):
        root = _n("root", "Root", parent_id=None)
        child = _n("c1", "Child #foo-bar", parent_id="root")
        root["children"] = [child]
        cache = _build_cache(_flat([root]))
        result = find_tag_family_collisions(
            cache, ["root"], "foo",
            postfix_words=["expand", "suppress", "deprecated"],
        )
        assert result["count"] == 1

    def test_postfix_variant_match(self):
        root = _n("root", "Root", parent_id=None)
        child = _n("c1", "Child #foo--deprecated", parent_id="root")
        root["children"] = [child]
        cache = _build_cache(_flat([root]))
        result = find_tag_family_collisions(
            cache, ["root"], "foo",
            postfix_words=["expand", "suppress", "deprecated"],
        )
        assert result["count"] == 1

    def test_hyphen_boundary_no_match(self):
        """#tagx does NOT match target 'tag' (hyphen boundary)."""
        root = _n("root", "Root", parent_id=None)
        child = _n("c1", "Child #tagx", parent_id="root")
        root["children"] = [child]
        cache = _build_cache(_flat([root]))
        result = find_tag_family_collisions(
            cache, ["root"], "tag",
            postfix_words=["expand", "suppress", "deprecated"],
        )
        assert result["count"] == 0

    def test_note_body_tag_match(self):
        """A tag in the note body IS a collision."""
        root = _n("root", "Root", parent_id=None)
        child = _n("c1", "Child", note="has #foo in note", parent_id="root")
        root["children"] = [child]
        cache = _build_cache(_flat([root]))
        result = find_tag_family_collisions(
            cache, ["root"], "foo",
            postfix_words=["expand", "suppress", "deprecated"],
        )
        assert result["count"] == 1

    def test_suppressed_node_still_checked(self):
        """Suppressed nodes are NOT skipped (suppression must not hide a collision)."""
        root = _n("root", "Root", parent_id=None)
        child = _n("c1", "Child #wfy-suppress #foo", parent_id="root")
        root["children"] = [child]
        cache = _build_cache(_flat([root]))
        result = find_tag_family_collisions(
            cache, ["root"], "foo",
            postfix_words=["expand", "suppress", "deprecated"],
        )
        assert result["count"] == 1

    def test_completed_node_skipped(self):
        """Completed nodes are skipped wholesale (Ruling A)."""
        root = _n("root", "Root", parent_id=None)
        child = _n("c1", "Child #foo", parent_id="root", completed=True)
        root["children"] = [child]
        cache = _build_cache(_flat([root]))
        result = find_tag_family_collisions(
            cache, ["root"], "foo",
            postfix_words=["expand", "suppress", "deprecated"],
            skip_completed=True,
        )
        assert result["count"] == 0

    def test_count_preserving(self):
        """The count must reflect the true total, not just the capped rows."""
        root = _n("root", "Root", parent_id=None)
        nodes = [root]
        for i in range(30):
            child = _n(f"c{i}", f"Child {i} #foo", parent_id="root")
            root["children"].append(child)
            nodes.append(child)
        cache = _build_cache(nodes)
        result = find_tag_family_collisions(
            cache, ["root"], "foo",
            postfix_words=["expand", "suppress", "deprecated"],
            max_rows=25,
        )
        assert result["count"] == 30
        assert len(result["collisions"]) == 25
        assert result["truncated"] == 5


# ===========================================================================
# 6. Overlay cache + dry-run + verify
# ===========================================================================

class TestOverlayAndDryRun:
    def test_overlay_appends_tags(self):
        root = _n("root", "Root", parent_id=None)
        child = _n("c1", "Child", parent_id="root")
        root["children"] = [child]
        cache = _build_cache(_flat([root]))

        overlay = build_merge_overlay_cache(
            cache, ["root"], ["c1"], "merged", "expand",
        )
        assert "#merged--expand" in overlay["root"]["name"]
        assert "#merged" in overlay["c1"]["name"]
        assert "#merged--expand" not in overlay["c1"]["name"]
        # Original cache is untouched
        assert "#merged" not in cache["root"]["name"]

    def test_simulate_projection(self):
        """A simple overlay projection returns the expected node set."""
        root = _n("root", "Root #merged--expand", parent_id=None)
        l1 = _n("l1", "L1 #merged", parent_id="root")
        l2 = _n("l2", "L2 #merged", parent_id="root")
        root["children"] = [l1, l2]
        cache = _build_cache(_flat([root]))

        overlay = build_merge_overlay_cache(
            cache, ["root"], [], "merged", "expand",
        )
        projected = simulate_merge_projection(
            overlay, ["root"], "merged",
        )
        assert projected is not None
        assert "root" in projected
        assert "l1" in projected
        assert "l2" in projected

    def test_verify_matched(self):
        """Post-mutation verification passes when the set matches."""
        root = _n("root", "Root #merged--expand", parent_id=None)
        l1 = _n("l1", "L1 #merged", parent_id="root")
        root["children"] = [l1]
        cache = _build_cache(_flat([root]))

        expected = {"root", "l1"}
        result = verify_merge_projection(
            cache, ["root"], "merged", expected,
        )
        assert result["matched"] is True
        assert result["missing"] == []
        assert result["unexpected"] == []

    def test_verify_mismatch(self):
        """Post-mutation verification fails when the set doesn't match."""
        root = _n("root", "Root #merged--expand", parent_id=None)
        l1 = _n("l1", "L1 #merged", parent_id="root")
        root["children"] = [l1]
        cache = _build_cache(_flat([root]))

        expected = {"root", "l1", "extra"}
        result = verify_merge_projection(
            cache, ["root"], "merged", expected,
        )
        assert result["matched"] is False
        assert "extra" in result["missing"]


# ===========================================================================
# 7. N-ary + mixed inputs
# ===========================================================================

class TestNAryAndMixed:
    def test_spec_n3_union(self):
        """N=3 groups normalize fine (N-ary union)."""
        spec, err = MergeSliceGroupsSpec.normalize_from_tool_args(
            groups=["a", "b", "c"],
            mode="union",
            tag="merged",
        )
        assert err is None
        assert len(spec.groups) == 3

    def test_spec_mixed_named_dynamic(self):
        """Mixed named + dynamic in one call."""
        spec, err = MergeSliceGroupsSpec.normalize_from_tool_args(
            groups=[
                "named-group",
                {"root_uuid": "abc-123", "tags": ["foo"]},
            ],
            mode="intersection",
            tag="merged",
        )
        assert err is None
        assert spec.groups[0].kind == "named"
        assert spec.groups[1].kind == "dynamic"


# ===========================================================================
# 8. Orchestrator integration + wire contract (post-audit regression locks)
# ===========================================================================

import asyncio  # noqa: E402

from workflowy_mcp import server as _server  # noqa: E402


def _mk(nid, name, parent_id=None, note=None, completed=False, priority=0):
    return {
        "id": nid,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
        "completed": completed,
        "completedAt": (1234567890 if completed else None),
    }


class _FakeClient:
    def __init__(self, nodes):
        self.nodes = nodes
        # S03-B: the merge_slice_groups WS front wraps the handler in a
        # cache_operation_boundary; install the boundary surface.  The pinned
        # view is a frozen copy of the initial nodes; the live ``self.nodes``
        # list (below) remains the accessor's product so the orchestrator's
        # post-mutation verification observes the simulated eager patches.
        from coherence_harness import make_boundary_capable

        make_boundary_capable(self, nodes=nodes)

    def _get_nodes_export_cache_nodes(self):
        return self.nodes


def _config(groups, global_never=()):
    return (
        list(groups),
        {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"},
        list(global_never),
    )


def _groups_two(root="root", p1="g1", p2="g2"):
    return [
        {"id": "g1", "name": "one", "root_uuid": root, "prefixes": [p1]},
        {"id": "g2", "name": "two", "root_uuid": root, "prefixes": [p2]},
    ]


class _Q4Recorder:
    """Fake _lightning_rod_mutate_tags. Applies ops to the fake client's node
    list the way update_node does -- REPLACING each touched list entry with a
    fresh dict (the stale-verification-map regression lock, audit m3)."""

    def __init__(self, client, fail_handles=(), apply_changes=True):
        self.client = client
        self.fail_handles = set(fail_handles)
        self.apply_changes = apply_changes
        self.calls = []

    async def __call__(self, *, spec):
        self.calls.append(spec)
        rows = []
        for op in spec.operations:
            if op.handle in self.fail_handles:
                rows.append({
                    "handle": op.handle, "uuid": op.handle, "operation": "add",
                    "status": "error", "error": "simulated commit failure",
                })
                continue
            if self.apply_changes:
                for i, node in enumerate(self.client.nodes):
                    if node["id"] == op.handle:
                        tag_str = "#" + op.tags[0]
                        if tag_str not in (node["name"] or "").split():
                            self.client.nodes[i] = {
                                **node, "name": (node["name"] or "") + " " + tag_str,
                            }
            rows.append({
                "handle": op.handle, "uuid": op.handle,
                "operation": "add", "status": "committed",
            })
        ok = all(r["status"] in ("committed", "noop") for r in rows)
        return {
            "success": ok,
            "results": rows,
            "nodes_changed": [r["uuid"] for r in rows if r["status"] == "committed"],
            "summary": {
                "requested": len(rows),
                "committed": sum(1 for r in rows if r["status"] == "committed"),
                "noop": 0,
                "preflight_failures": 0,
                "commit_failures": sum(1 for r in rows if r["status"] == "error"),
            },
        }


class _CreateRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not str(kwargs.get("root_uuid") or "").strip():
            # Mirrors the real create_slice_group guard (Ruling B).
            return {"success": False, "error": "root_uuid is required (non-empty)."}
        return {
            "success": True,
            "slice_group": {
                "id": "gen-id",
                "name": kwargs.get("name"),
                "root_uuid": kwargs.get("root_uuid"),
                "tags": kwargs.get("tags"),
            },
        }


def _run_merge(spec, config, client, q4, create, monkeypatch):
    monkeypatch.setattr(_server, "get_client", lambda: client)
    monkeypatch.setattr(_server, "_lightning_rod_mutate_tags", q4)
    monkeypatch.setattr(_server, "create_slice_group", create)
    return asyncio.run(_server._merge_slice_groups(spec=spec, config=config))


def _spec(groups, mode, tag):
    spec, err = MergeSliceGroupsSpec.normalize_from_tool_args(groups=groups, mode=mode, tag=tag)
    assert err is None, err
    return spec


class TestOrchestratorCore:
    def test_union_root_expand_and_persist(self, monkeypatch):
        nodes = [_mk("root", "Root"), _mk("x", "X #g1", "root"), _mk("y", "Y #g2", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        r = _run_merge(_spec(["one", "two"], "union", "merged"), _config(_groups_two()),
                       client, q4, create, monkeypatch)
        assert r["success"] is True
        assert r["root_uuid"] == "root"
        assert r["root_is_vault_root"] is False
        assert r["planned_expand_count"] == 1 and r["planned_plain_count"] == 0
        ops = q4.calls[0].operations
        assert [(o.handle, list(o.tags)) for o in ops] == [("root", ["merged--expand"])]
        assert r["verification"]["dry_run"]["matched"] is True
        # Audit m3: post-mutation verification must read the REBUILT cache.
        assert r["verification"]["post_mutation"]["matched"] is True
        assert len(create.calls) == 1
        assert create.calls[0]["name"] == "merged-root (generated slice group)"
        assert create.calls[0]["root_uuid"] == "root"
        assert create.calls[0]["tags"] == ["merged"]
        assert r["slice_group"]["success"] is True
        assert r["committed_count"] == 1

    def test_non_top_level_lca_no_false_spine_refusal(self, monkeypatch):
        # Audit m2: the expected spine stops AT the LCA -- ancestors above it
        # must not be expected (the old code falsely refused these merges).
        nodes = [
            _mk("top", "Top"),
            _mk("lca", "LCA", "top"),
            _mk("a", "A", "lca"), _mk("x", "X #g1", "a"),
            _mk("b", "B", "lca"), _mk("y", "Y #g2", "b"),
        ]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        cfg = _config([
            {"id": "g1", "name": "one", "root_uuid": "a", "prefixes": ["g1"]},
            {"id": "g2", "name": "two", "root_uuid": "b", "prefixes": ["g2"]},
        ])
        r = _run_merge(_spec(["one", "two"], "union", "merged"), cfg, client, q4, create, monkeypatch)
        assert r["success"] is True
        assert r["root_uuid"] == "lca"
        assert r["verification"]["dry_run"]["matched"] is True
        assert r["verification"]["post_mutation"]["matched"] is True

    def test_vault_root_merge(self, monkeypatch):
        # Ruling B: a vault-root LCA tags normally; the empty-root persistence
        # guard fails NON-fatally.
        nodes = [
            _mk("t1", "T1"), _mk("x", "X #g1", "t1"),
            _mk("t2", "T2"), _mk("y", "Y #g2", "t2"),
        ]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        cfg = _config([
            {"id": "g1", "name": "one", "root_uuid": "t1", "prefixes": ["g1"]},
            {"id": "g2", "name": "two", "root_uuid": "t2", "prefixes": ["g2"]},
        ])
        r = _run_merge(_spec(["one", "two"], "union", "merged"), cfg, client, q4, create, monkeypatch)
        assert r["success"] is True
        assert r["root_is_vault_root"] is True
        assert r["root_uuid"] == ""
        assert r["planned_expand_count"] == 2
        assert r["verification"]["post_mutation"]["matched"] is True
        assert r["slice_group"]["attempted"] is True
        assert r["slice_group"]["success"] is False

    def test_empty_intersection_loud_error(self, monkeypatch):
        nodes = [_mk("root", "Root"), _mk("x", "X #g1", "root"), _mk("y", "Y #g2", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        r = _run_merge(_spec(["one", "two"], "intersection", "merged"), _config(_groups_two()),
                       client, q4, create, monkeypatch)
        assert r["success"] is False
        assert "empty" in r["error"]
        assert q4.calls == []


class TestOrchestratorLifecycle:
    def test_post_verification_failure_flips_success(self, monkeypatch):
        # Q4 reports success but the cache does not reflect the tags ->
        # verification mismatch -> top-level failure + no persistence (A3.4).
        nodes = [_mk("root", "Root"), _mk("x", "X #g1", "root"), _mk("y", "Y #g2", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client, apply_changes=False)
        create = _CreateRecorder()
        r = _run_merge(_spec(["one", "two"], "union", "merged"), _config(_groups_two()),
                       client, q4, create, monkeypatch)
        assert r["success"] is False
        assert "verification" in (r.get("error") or "")
        assert r["slice_group"]["attempted"] is False
        assert create.calls == []

    def test_partial_commit_no_persist(self, monkeypatch):
        nodes = [_mk("root", "Root"), _mk("x", "X #g1", "root"), _mk("y", "Y #g2", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client, fail_handles={"root"})
        create = _CreateRecorder()
        r = _run_merge(_spec(["one", "two"], "union", "merged"), _config(_groups_two()),
                       client, q4, create, monkeypatch)
        assert r["success"] is False
        assert r["slice_group"]["attempted"] is False
        assert r["slice_group"]["skipped_reason"] == "tag mutation did not complete exactly"
        assert create.calls == []

    def test_congruent_residue_rerun_accepted(self, monkeypatch):
        # A3.2: exact required-form trailing NAME residue on the planned node
        # is accepted as no-op resume residue.
        nodes = [_mk("root", "Root #merged--expand"), _mk("x", "X #g1", "root"), _mk("y", "Y #g2", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        r = _run_merge(_spec(["one", "two"], "union", "merged"), _config(_groups_two()),
                       client, q4, create, monkeypatch)
        assert r["success"] is True
        assert len(q4.calls) == 1
        assert len(create.calls) == 1


class TestOrchestratorSafety:
    def test_note_body_residue_is_not_congruent(self, monkeypatch):
        nodes = [_mk("root", "Root", note="#merged--expand"), _mk("x", "X #g1", "root"), _mk("y", "Y #g2", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        r = _run_merge(_spec(["one", "two"], "union", "merged"), _config(_groups_two()),
                       client, q4, create, monkeypatch)
        assert r["success"] is False
        assert "not brand-new" in r["error"]
        assert q4.calls == []

    def test_unrelated_variant_on_congruent_node_aborts(self, monkeypatch):
        nodes = [_mk("root", "Root #merged--expand #merged-bad"), _mk("x", "X #g1", "root"), _mk("y", "Y #g2", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        r = _run_merge(_spec(["one", "two"], "union", "merged"), _config(_groups_two()),
                       client, q4, create, monkeypatch)
        assert r["success"] is False
        assert q4.calls == []

    def test_truncated_report_cannot_hide_noncongruent(self, monkeypatch):
        # Audit M1: 25 congruent residue rows first, 5 noncongruent variants
        # beyond the old 25-row cap -- the checker must still abort.
        nodes = [_mk("root", "Root")]
        for i in range(30):
            tag = "#merged" if i < 25 else "#merged-x"
            nodes.append(_mk(f"c{i}", f"C{i} #g1 {tag}", "root", priority=i))
        nodes.append(_mk("u", "U", "root", priority=99))  # unmerged -> root not covered
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        cfg = _config(_groups_two(root="root", p1="g1", p2="g1"))
        r = _run_merge(_spec(["one", "two"], "union", "merged"), cfg, client, q4, create, monkeypatch)
        assert r["success"] is False
        assert "5 conflicting" in r["error"]
        assert q4.calls == []

    def test_completed_start_root_is_checked(self, monkeypatch):
        # A5: an explicit start ROOT is completion-EXEMPT -- its tags ARE checked.
        # (Two merged children make the completed node the actual LCA; with a
        # single merged leaf the LCA would be the leaf itself and the completed
        # parent would simply be out of the safety check's scope.)
        nodes = [
            _mk("root", "Root #merged", completed=True),
            _mk("x", "X #g1", "root"),
            _mk("y", "Y #g2", "root"),
        ]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        cfg = _config(_groups_two())
        r = _run_merge(_spec(["one", "two"], "union", "merged"), cfg, client, q4, create, monkeypatch)
        assert r["success"] is False
        assert "not brand-new" in r["error"]
        assert q4.calls == []

    def test_completed_descendant_skipped(self, monkeypatch):
        nodes = [
            _mk("root", "Root"),
            _mk("done", "Done #merged", "root", completed=True),
            _mk("x", "X #g1", "root"),
        ]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        cfg = _config(_groups_two(root="root", p1="g1", p2="g1"))
        r = _run_merge(_spec(["one", "two"], "union", "merged"), cfg, client, q4, create, monkeypatch)
        assert r["success"] is True

    def test_reverse_prefix_persisted_reject(self, monkeypatch):
        nodes = [_mk("root", "Root"), _mk("x", "X #g1", "root"), _mk("y", "Y #g2", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        cfg = _config(_groups_two() + [
            {"id": "old", "name": "old", "root_uuid": "root", "prefixes": ["foo"]},
        ])
        r = _run_merge(_spec(["one", "two"], "union", "foo-bar"), cfg, client, q4, create, monkeypatch)
        assert r["success"] is False
        assert "matches existing slice-group prefix" in r["error"]
        assert q4.calls == []

    def test_reverse_prefix_dynamic_input_reject(self, monkeypatch):
        nodes = [_mk("root", "Root"), _mk("x", "X #g1", "root"), _mk("y", "Y #foo", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        cfg = _config([{"id": "g1", "name": "one", "root_uuid": "root", "prefixes": ["g1"]}])
        spec = _spec(["one", {"root_uuid": "root", "tags": ["foo"]}], "union", "foo-bar")
        r = _run_merge(spec, cfg, client, q4, create, monkeypatch)
        assert r["success"] is False
        assert "matches existing slice-group prefix" in r["error"]
        assert q4.calls == []

    def test_global_never_target_reject(self, monkeypatch):
        nodes = [_mk("root", "Root"), _mk("x", "X #g1", "root"), _mk("y", "Y #g2", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        cfg = _config(_groups_two(), global_never=["merged"])
        r = _run_merge(_spec(["one", "two"], "union", "merged"), cfg, client, q4, create, monkeypatch)
        assert r["success"] is False
        assert "global-never" in r["error"]
        assert q4.calls == []

    def test_global_never_expand_form_reject(self, monkeypatch):
        nodes = [_mk("root", "Root"), _mk("x", "X #g1", "root"), _mk("y", "Y #g2", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        cfg = _config(_groups_two(), global_never=["merged--expand"])
        r = _run_merge(_spec(["one", "two"], "union", "merged"), cfg, client, q4, create, monkeypatch)
        assert r["success"] is False
        assert "global-never" in r["error"]
        assert q4.calls == []


class TestOrchestratorPersistence:
    def test_identical_generated_group_already_existed(self, monkeypatch):
        # A3.5: a rerun whose identical generated group already exists must
        # reach the already_existed no-op (the reverse check must not abort
        # on our own prior persistence).
        gen = {"id": "gen", "name": "merged-root (generated slice group)",
               "root_uuid": "root", "prefixes": ["merged"]}
        nodes = [_mk("root", "Root #merged--expand"), _mk("x", "X #g1", "root"), _mk("y", "Y #g2", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        cfg = _config(_groups_two() + [gen])
        r = _run_merge(_spec(["one", "two"], "union", "merged"), cfg, client, q4, create, monkeypatch)
        assert r["success"] is True
        assert r["slice_group"]["already_existed"] is True
        assert create.calls == []

    def test_same_name_conflict_nonfatal(self, monkeypatch):
        # A3.5: same deterministic NAME, different fields -> nonfatal conflict,
        # never a blind duplicate.
        conflict = {"id": "gen", "name": "merged-root (generated slice group)",
                    "root_uuid": "root", "prefixes": ["other"]}
        nodes = [_mk("root", "Root"), _mk("x", "X #g1", "root"), _mk("y", "Y #g2", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        cfg = _config(_groups_two() + [conflict])
        r = _run_merge(_spec(["one", "two"], "union", "merged"), cfg, client, q4, create, monkeypatch)
        assert r["success"] is True
        assert r["slice_group"]["conflict"] is True
        assert r["slice_group"]["success"] is False
        assert create.calls == []


class TestWireJsContract:
    def test_js_canonical_frame_accepted(self):
        # Locks the JS-produced frame shape (audit M2): named groups are bare
        # STRINGS; dynamic groups are {root_uuid, tags} ONLY.
        spec, err = MergeSliceGroupsSpec.from_wire({
            "action": "merge_slice_groups",
            "request_id": "r1",
            "endpoint_id": "ep1",
            "v": 1,
            "groups": ["grp-a", {"root_uuid": "u-1", "tags": ["t1", "t2"]}],
            "mode": "union",
            "tag": "merged",
        })
        assert err is None
        assert spec.groups[0].kind == "named"
        assert spec.groups[0].handle == "grp-a"
        assert spec.groups[1].kind == "dynamic"
        assert spec.groups[1].root_uuid == "u-1"
        assert spec.groups[1].tags == ("t1", "t2")

    def test_internal_ref_shape_rejected(self):
        # The pre-fix JS sent internal {kind, handle, ...} refs; the grammar
        # must keep rejecting them loudly so the two transports cannot drift
        # back apart silently.
        _, err = MergeSliceGroupsSpec.from_wire({
            "action": "merge_slice_groups",
            "request_id": "r1",
            "endpoint_id": "ep1",
            "v": 1,
            "groups": [
                {"kind": "named", "handle": "a", "root_uuid": None, "tags": []},
                "b",
            ],
            "mode": "union",
            "tag": "merged",
        })
        assert err is not None
        assert "unknown field" in err.lower()

    def test_wire_timeout_seconds_tolerated_not_on_spec(self):
        # The caller-declared client wait deadline is INTERIM transport
        # metadata: tolerated on the frame, never a spec field.
        spec, err = MergeSliceGroupsSpec.from_wire({
            "action": "merge_slice_groups",
            "request_id": "r1",
            "endpoint_id": "ep1",
            "v": 1,
            "groups": ["a", "b"],
            "mode": "union",
            "tag": "merged",
            "timeout_seconds": 300,
        })
        assert err is None
        assert spec is not None
        assert "timeout_seconds" not in spec.to_dict()

    def test_wire_timeout_seconds_invalid_rejected(self):
        for bad in ("five", -5, 0, True):
            _, err = MergeSliceGroupsSpec.from_wire({
                "action": "merge_slice_groups",
                "request_id": "r1",
                "endpoint_id": "ep1",
                "v": 1,
                "groups": ["a", "b"],
                "mode": "union",
                "tag": "merged",
                "timeout_seconds": bad,
            })
            assert err is not None, bad
            assert "timeout_seconds" in err


# ===========================================================================
# 8. Post-audit regression tests (second audit round)
# ===========================================================================

class TestCollisionProvenance:
    def test_duplicate_trailing_and_note_occurrence(self):
        """Post-audit: a congruent trailing NAME copy must NOT hide a
        duplicate NOTE-body occurrence (extract_node_tags dedupes name+note;
        the checker scans the three regions independently)."""
        root = _n("root", "Root #merged--expand", note="#merged--expand")
        cache = _build_cache(_flat([root]))
        result = find_tag_family_collisions(
            cache, ["root"], "merged",
            postfix_words=["expand", "suppress", "deprecated"],
            planned_node_forms={"root": "expand"},
            expand_form="merged--expand",
        )
        assert result["congruent_count"] == 1
        assert result["count"] == 1
        assert result["collisions"][0]["source"] == "note"

    def test_inline_name_occurrence_not_congruent(self):
        """A family tag in the NON-trailing name text is noncongruent even
        when the exact required form also sits in the trailing region."""
        root = _n("root", "Root #merged--expand inline #merged--expand")
        cache = _build_cache(_flat([root]))
        result = find_tag_family_collisions(
            cache, ["root"], "merged",
            postfix_words=["expand", "suppress", "deprecated"],
            planned_node_forms={"root": "expand"},
            expand_form="merged--expand",
        )
        assert result["congruent_count"] == 1
        assert result["count"] == 1
        assert result["collisions"][0]["source"] == "name_inline"

    def test_duplicate_note_residue_aborts_merge(self, monkeypatch):
        """Orchestrator level: exact required-form residue in the trailing
        NAME region is congruent, but a DUPLICATE in the NOTE aborts (A3.2)."""
        nodes = [
            _mk("root", "Root #merged--expand", note="#merged--expand"),
            _mk("x", "X #g1", "root"),
            _mk("y", "Y #g2", "root"),
        ]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        r = _run_merge(_spec(["one", "two"], "union", "merged"), _config(_groups_two()),
                       client, q4, create, monkeypatch)
        assert r["success"] is False
        assert "not brand-new" in r["error"]
        assert q4.calls == []


class TestSpineBlockerDiagnostics:
    def test_completed_spine_blocker_named(self, monkeypatch):
        """A6: a COMPLETED node on the LCA spine refuses pre-mutation and
        NAMES the blocker node, not just the blocked leaf."""
        nodes = [
            _mk("top", "Top"),
            _mk("done", "Done", "top", completed=True),
            _mk("x", "X #g1", "done"),
            _mk("other", "Other", "top"),
            _mk("y", "Y #g2", "other"),
        ]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        cfg = _config([
            {"id": "g1", "name": "one", "root_uuid": "done", "prefixes": ["g1"]},
            {"id": "g2", "name": "two", "root_uuid": "other", "prefixes": ["g2"]},
        ])
        r = _run_merge(_spec(["one", "two"], "union", "merged"), cfg,
                       client, q4, create, monkeypatch)
        assert r["success"] is False
        assert q4.calls == []  # ZERO mutations
        assert "spine" in (r.get("error") or "")
        assert r.get("spine_blockers") == ["done"]

    def test_global_never_spine_blocker_named(self, monkeypatch):
        """A6: a GLOBAL-NEVER node on the LCA spine refuses pre-mutation and
        NAMES the blocker node."""
        nodes = [
            _mk("top", "Top"),
            _mk("blocked", "Blocked #wfy-suppress", "top"),
            _mk("x", "X #g1", "blocked"),
            _mk("other", "Other", "top"),
            _mk("y", "Y #g2", "other"),
        ]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        cfg = _config([
            {"id": "g1", "name": "one", "root_uuid": "blocked", "prefixes": ["g1"]},
            {"id": "g2", "name": "two", "root_uuid": "other", "prefixes": ["g2"]},
        ], global_never=["wfy-suppress"])
        r = _run_merge(_spec(["one", "two"], "union", "merged"), cfg,
                       client, q4, create, monkeypatch)
        assert r["success"] is False
        assert q4.calls == []
        assert "spine" in (r.get("error") or "")
        assert r.get("spine_blockers") == ["blocked"]


class TestReportFields:
    def test_report_carries_start_ids_and_warnings(self, monkeypatch):
        """A12: the report includes start_ids and a (possibly empty) warnings
        list accumulated from the Phase-A projections."""
        nodes = [_mk("root", "Root"), _mk("x", "X #g1", "root"), _mk("y", "Y #g2", "root")]
        client = _FakeClient(nodes)
        q4 = _Q4Recorder(client)
        create = _CreateRecorder()
        r = _run_merge(_spec(["one", "two"], "union", "merged"), _config(_groups_two()),
                       client, q4, create, monkeypatch)
        assert r["success"] is True
        assert r["start_ids"] == ["root"]
        assert r["warnings"] == []


class TestWireHandlerDispatch:
    def test_handle_merge_slice_groups_end_to_end(self, monkeypatch):
        """Post-audit regression: the wire handler must call the nested config
        read with the keyword the helper actually accepts (endpoint_id) and
        reply merge_slice_groups_result echoing request_id."""
        import json as _json

        async def _noop_gate(paths, *, operation):
            return None

        # S04: the hard-stop admission gate is covered by the S04 suites; this
        # test exercises the wire-handler dispatch in isolation.
        monkeypatch.setattr(
            "workflowy_mcp.coherence.admission.require_coherence_mutation_admission",
            _noop_gate,
        )

        class _WS:
            def __init__(self):
                self.sent = []

            async def send(self, payload):
                self.sent.append(_json.loads(payload))

        captured = {}

        async def _fake_nested(*, endpoint_id=None, context, error_type=None, timeout_s=15.0):
            captured["endpoint_id"] = endpoint_id
            captured["context"] = context
            return ({"expand_groups": []}, None)

        async def _fake_merge(*, spec, config=None, target_tab=None):
            captured["spec_tag"] = spec.tag
            captured["target_tab"] = target_tab
            return {"success": True, "tag": spec.tag}

        monkeypatch.setattr(_server, "_read_slice_group_config_nested", _fake_nested)
        monkeypatch.setattr(_server, "_merge_slice_groups", _fake_merge)

        # S03-B: the merge_slice_groups WS front wraps the handler in a
        # cache_operation_boundary; install a boundary-capable client double.
        from types import SimpleNamespace

        from coherence_harness import make_boundary_capable

        monkeypatch.setattr(
            _server,
            "get_client",
            lambda: make_boundary_capable(SimpleNamespace(), nodes=[]),
        )

        ws = _WS()
        asyncio.run(_server._handle_merge_slice_groups({
            "action": "merge_slice_groups",
            "request_id": "r1",
            "endpoint_id": "ep1",
            "v": 1,
            "groups": ["a", "b"],
            "mode": "union",
            "tag": "merged",
        }, ws))

        assert captured["endpoint_id"] == "ep1"
        assert captured["target_tab"] == "ep1"
        assert captured["spec_tag"] == "merged"
        assert ws.sent and ws.sent[0]["action"] == "merge_slice_groups_result"
        assert ws.sent[0]["request_id"] == "r1"
        assert ws.sent[0]["success"] is True


class TestNormalizerHashParity:
    def test_double_hash_tag_rejected(self):
        """'##foo' strips ONE leading '#' then rejects the embedded '#' --
        the JS twin strips exactly one too (dual-normalizer parity)."""
        _, err = MergeSliceGroupsSpec.normalize_from_tool_args(
            groups=["a", "b"], mode="union", tag="##foo",
        )
        assert err is not None
