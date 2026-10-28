from __future__ import annotations

import pytest

import workflowy_mcp.lens_engine as lens_engine_module
from workflowy_mcp.f8_slice_policy import (
    build_anchor_boundary_predicate,
    build_slice_subtree_from_cache,
    build_stage1_base_tree,
)
from workflowy_mcp.lens_engine import (
    LensEmptyResult,
    LensEvalContext,
    LensResultTree,
    build_lens_result_tree,
    evaluate_lens,
)
from workflowy_mcp.lens_spec import (
    GroupAtom,
    LensSpecError,
    MergeNode,
    SubtreeAtom,
    TagsAtom,
)
from workflowy_mcp.merge_slice_groups_policy import collect_effective_slice_leaf_ids


def _n(nid, name, parent_id, priority, *, note="", completed=False):
    return {
        "id": nid,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
        "completed": completed,
        "completedAt": 1 if completed else None,
    }


@pytest.fixture
def cache():
    return {
        "R": _n("R", "Root", None, 0),
        "A": _n("A", "Architecture #arch--expand", "R", 0),
        "A1": _n("A1", "Architecture detail", "A", 0),
        "A2": _n("A2", "Completed architecture detail", "A", 1, completed=True),
        "B": _n("B", "Deployment", "R", 1),
        "B1": _n("B1", "Deploy detail #deploy", "B", 0),
        "S": _n("S", "Suppressed #wfy-suppress", "R", 2),
        "S1": _n("S1", "Hidden architecture #arch", "S", 0),
        "D": _n("D", "Child Dock #wfy-ns", "R", 3),
        "D1": _n("D1", "Dock child", "D", 0),
        "N": _n("N", "Note-only member", "R", 4, note="#noteonly"),
        "T": _n("T", "Other top-level", None, 1),
    }


@pytest.fixture
def groups():
    return {
        "Architecture": {"id": "grp-arch", "name": "Architecture", "root_uuid": "R", "prefixes": ["arch"]},
        "grp-arch": {"id": "grp-arch", "name": "Architecture", "root_uuid": "R", "prefixes": ["arch"]},
        "Deployment": {"id": "grp-deploy", "name": "Deployment", "root_uuid": "R", "prefixes": ["deploy"]},
        "Empty": {"id": "grp-empty", "name": "Empty", "root_uuid": "R", "prefixes": ["missing"]},
        "Note": {"id": "grp-note", "name": "Note", "root_uuid": "R", "prefixes": ["noteonly"]},
    }


def _ctx(cache, groups, *, boundary=True, counters=None):
    counters = counters if counters is not None else {"group": 0, "root": 0}

    def resolve_group(handle):
        counters["group"] += 1
        if handle not in groups:
            raise KeyError(handle)
        return groups[handle]

    def resolve_root(handle):
        counters["root"] += 1
        aliases = {"[ap:ROOT01]": "R", "[ap:AREA01]": "A"}
        resolved = aliases.get(handle, handle)
        if resolved not in cache:
            raise KeyError(handle)
        return resolved

    predicate = build_anchor_boundary_predicate(["wfy-ns"]) if boundary else None
    return LensEvalContext(
        cache_by_id=cache,
        resolve_group=resolve_group,
        resolve_root=resolve_root,
        global_never_stems=("wfy-suppress",),
        exclude_completed=True,
        anchor_boundary_predicate=predicate,
    )


def test_group_and_tags_atom_equivalence_and_expand_hydration(cache, groups):
    ctx = _ctx(cache, groups)
    group_result = evaluate_lens(GroupAtom("Architecture"), ctx)
    tags_result = evaluate_lens(TagsAtom("R", ("arch",)), ctx)

    direct_tree, direct_plan, _meta = build_stage1_base_tree(
        cache,
        "R",
        ["arch"],
        global_never_expand_tags=("wfy-suppress",),
        exclude_completed=True,
    )
    direct = collect_effective_slice_leaf_ids(direct_tree, direct_plan, ["arch"])
    assert group_result.effective_leaf_ids == frozenset(direct) == frozenset({"A1"})
    assert tags_result.effective_leaf_ids == group_result.effective_leaf_ids
    assert "A2" not in group_result.effective_leaf_ids
    assert "S1" not in group_result.effective_leaf_ids


def test_subtree_atom_matches_raw_projection_and_ignores_global_never(cache, groups):
    ctx = _ctx(cache, groups)
    result = evaluate_lens(SubtreeAtom("R"), ctx)
    direct = build_slice_subtree_from_cache(
        cache, "R", (), member_all=True, exclude_completed=True
    )

    def leaves(node):
        children = node.get("children") or []
        if not children:
            return {node["id"]}
        return set().union(*(leaves(child) for child in children))

    assert result.effective_leaf_ids == frozenset(leaves(direct))
    assert "S1" in result.effective_leaf_ids
    assert "A2" not in result.effective_leaf_ids


def test_dock_boundary_is_per_subtree_atom_and_missing_predicate_is_loud(cache, groups):
    bounded = evaluate_lens(
        SubtreeAtom("R", "stop_at_child_docks"), _ctx(cache, groups)
    )
    assert "D" in bounded.effective_leaf_ids
    assert "D1" not in bounded.effective_leaf_ids

    with pytest.raises(LensSpecError) as raised:
        evaluate_lens(
            SubtreeAtom("R", "stop_at_child_docks"),
            _ctx(cache, groups, boundary=False),
        )
    assert raised.value.code == "boundary_predicate_unavailable"
    assert raised.value.path == "$"


def test_union_intersection_and_connective_root_false_positive_lock(cache, groups):
    ctx = _ctx(cache, groups)
    union = evaluate_lens(
        MergeNode("union", (GroupAtom("Architecture"), GroupAtom("Deployment"))),
        ctx,
    )
    intersection = evaluate_lens(
        MergeNode("intersection", (GroupAtom("Architecture"), GroupAtom("Deployment"))),
        ctx,
    )
    empty_atom = evaluate_lens(GroupAtom("Empty"), ctx)
    assert union.effective_leaf_ids == frozenset({"A1", "B1"})
    assert intersection.effective_leaf_ids == frozenset()
    assert empty_atom.effective_leaf_ids == frozenset()


def test_ordered_difference_is_nary_order_sensitive_and_nested(cache, groups):
    ctx = _ctx(cache, groups)
    difference = evaluate_lens(
        MergeNode(
            "difference",
            (
                SubtreeAtom("R"),
                GroupAtom("Architecture"),
                GroupAtom("Deployment"),
            ),
        ),
        ctx,
    )
    assert difference.effective_leaf_ids == frozenset({"S1", "D1", "N"})

    reverse = evaluate_lens(
        MergeNode("difference", (GroupAtom("Architecture"), SubtreeAtom("R"))),
        ctx,
    )
    assert reverse.effective_leaf_ids == frozenset()

    nested = evaluate_lens(
        MergeNode(
            "difference",
            (
                SubtreeAtom("R"),
                MergeNode(
                    "difference",
                    (SubtreeAtom("R"), GroupAtom("Deployment")),
                ),
            ),
        ),
        ctx,
    )
    assert nested.effective_leaf_ids == frozenset({"B1"})

    with pytest.raises(LensSpecError) as raised:
        evaluate_lens(
            MergeNode("xor", (GroupAtom("Architecture"), GroupAtom("Deployment"))),
            ctx,
        )
    assert raised.value.code == "invalid_merge_operation"
    assert raised.value.path == "$"


def test_nested_merge_reports_paths_and_resolves_each_atom_once(cache, groups):
    counters = {"group": 0, "root": 0}
    ctx = _ctx(cache, groups, counters=counters)
    lens = MergeNode(
        "union",
        (
            MergeNode(
                "intersection",
                (GroupAtom("Architecture"), TagsAtom("R", ("arch",))),
            ),
            GroupAtom("Deployment"),
        ),
    )
    result = evaluate_lens(lens, ctx)
    assert [report.path for report in result.per_atom_reports] == [
        "inputs[0].inputs[0]",
        "inputs[0].inputs[1]",
        "inputs[1]",
    ]
    # Chain-2 r1 (F1): a group atom's INHERITED stored root now resolves
    # through the same shared resolve_root seam an override root uses, so the
    # two group atoms each add one root call beside TagsAtom's ("R" three
    # times) — the shared seam governs every atom root.
    assert counters == {"group": 2, "root": 3}
    assert result.per_atom_reports[0].resolved_root_uuid == "R"
    assert result.per_atom_reports[0].canonical_identity["group_id"] == "grp-arch"
    assert result.canonical_lens.inputs[0].inputs[0].group_handle == "grp-arch"


def test_canonical_lens_keeps_group_root_inheritance_but_rewrites_explicit_handles(cache, groups):
    result = evaluate_lens(
        MergeNode(
            "union",
            (
                GroupAtom("Architecture"),
                GroupAtom("Architecture", root_handle="[ap:AREA01]"),
                SubtreeAtom("[ap:ROOT01]"),
            ),
        ),
        _ctx(cache, groups),
    )
    inherited, overridden, subtree = result.canonical_lens.inputs
    assert inherited.group_handle == "grp-arch"
    assert inherited.root_handle is None
    assert inherited.match_scope == "name_and_note"
    assert overridden.root_handle == "A"
    assert subtree.root_handle == "R"


def test_group_match_scope_default_and_transient_title_override(cache, groups):
    ctx = _ctx(cache, groups)
    default = evaluate_lens(GroupAtom("Note"), ctx)
    title_only = evaluate_lens(GroupAtom("Note", match_scope="name"), ctx)
    assert default.effective_leaf_ids == frozenset({"N"})
    assert title_only.effective_leaf_ids == frozenset()


def test_result_tree_lca_one_leaf_empty_and_disjoint_failure(cache, groups):
    ctx = _ctx(cache, groups)
    one = build_lens_result_tree({"A1"}, ctx)
    assert isinstance(one, LensResultTree)
    assert one.root_uuid == "A1"
    assert one.tree["id"] == "A1"

    empty = build_lens_result_tree(set(), ctx)
    assert isinstance(empty, LensEmptyResult)
    assert empty.root_uuid is None and empty.tree is None and empty.empty_selection

    with pytest.raises(LensSpecError) as raised:
        build_lens_result_tree({"A1", "T"}, ctx)
    assert raised.value.code == "no_common_ancestor"


def test_result_tree_rebuild_reenforces_completed_exclusion(cache, groups):
    ctx = _ctx(cache, groups)
    rebuilt = build_lens_result_tree({"A1", "A2"}, ctx)
    assert rebuilt.root_uuid == "A"

    def ids(node):
        return {node["id"]} | set().union(*(ids(child) for child in node.get("children") or []))

    assert "A1" in ids(rebuilt.tree)
    assert "A2" not in ids(rebuilt.tree)


def test_all_phantom_lca_forms_are_rejected(cache, groups, monkeypatch):
    ctx = _ctx(cache, groups)
    for phantom in (None, "", "missing-cache-node"):
        monkeypatch.setattr(
            lens_engine_module,
            "compute_lca_from_cache",
            lambda _cache, _ids, value=phantom: value,
        )
        with pytest.raises(LensSpecError) as raised:
            build_lens_result_tree({"A1", "B1"}, ctx)
        assert raised.value.code == "no_common_ancestor"


def test_root_resolver_failure_keeps_nested_atom_path(cache, groups):
    lens = MergeNode("union", (GroupAtom("Architecture"), SubtreeAtom("missing")))
    with pytest.raises(LensSpecError) as raised:
        evaluate_lens(lens, _ctx(cache, groups))
    assert raised.value.code == "root_resolution_failed"
    assert raised.value.path == "inputs[1]"


def test_resolver_failures_keep_exact_nested_atom_path(cache, groups):
    lens = MergeNode("union", (GroupAtom("Architecture"), GroupAtom("Missing")))
    with pytest.raises(LensSpecError) as raised:
        evaluate_lens(lens, _ctx(cache, groups))
    assert raised.value.code == "group_resolution_failed"
    assert raised.value.path == "inputs[1]"
