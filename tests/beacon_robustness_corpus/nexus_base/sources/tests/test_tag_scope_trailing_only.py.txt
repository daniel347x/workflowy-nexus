"""Semantic tag search under ``match_scope='name'`` reads the trailing region.

Contract [ap:LQ8RDA] + [ap:DR0C0W] (rulings [ap:8HPSTZ], [ap:ZG5UJQ]): a
name-only tag search must read tags ONLY from the trailing tag region of the
node NAME, via the ONE canonical splitter (``nexus_map_codebase.
split_name_and_tags`` -- reached by import, never copied). ``name_and_note``
keeps today's Workflowy-search parity. This file pins the four behaviour
groups the brief requires, at the predicate / surface levels:

  * a mid-title ``#tag`` (prose) is NOT a member under name-only, IS under
    name_and_note;
  * a note-only ``#tag`` is NOT a member under name-only, IS under
    name_and_note;
  * a trailing ``#tag`` IS a member under both;
  * the curation cursor ignores a mid-title curated-family tag under name-only;
  * the Anchor descent boundary ignores a mid-title Dock tag;
  * Lightning-Rod ``gather`` under name-only reads only the trailing region;
  * ``slice_group.render``/``merge.render`` (via the shared lens projector)
    default to name-only, with an explicit per-atom ``match_scope`` winning;
  * ``sift``'s tag atoms default name-only while its query half keeps
    ``name_and_note`` (F5).
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import MappingProxyType

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.f8_slice_policy import (  # noqa: E402
    build_anchor_boundary_predicate,
    build_stage1_base_tree,
    classify_node_tags,
    collect_subtree_ids,
    gather_tags_in_subtree,
    is_slice_member,
    name_has_global_never_tag,
    node_has_suppress_ancestor,
    node_is_curated_for_prefixes,
)
from workflowy_mcp.lens_engine import LensEvalContext  # noqa: E402
from workflowy_mcp.merge_slice_groups_policy import compute_merge_tag_plan  # noqa: E402

from test_phase3_hydration_suppression import _ids, _mk  # noqa: E402


def _n(nid, name, parent_id, priority, *, note=""):
    return {
        "id": nid,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
        "completed": False,
        "completedAt": None,
    }


def _cache() -> dict[str, dict]:
    """R has three would-be members: one trailing, one mid-title, one note-only."""
    return {
        "R": _n("R", "Root", None, 0),
        "TRAIL": _n("TRAIL", "Trailing member #topic", "R", 0),
        "MID": _n("MID", "Discusses #topic in prose", "R", 1),
        "NOTE": _n("NOTE", "Plain title", "R", 2, note="the note mentions #topic here"),
        "CUR": _n("CUR", "Checkpoint #curated", "R", 3),
        "DOC": _n("DOC", "Prose about the #curated cursor", "R", 4),
        "DOCKMID": _n("DOCKMID", "Talks about #wfy-ns docks", "R", 5),
        "DOCKTRAIL": _n("DOCKTRAIL", "Real dock #wfy-ns", "R", 6),
        # Stem-only-mid-title and stem-only-note nodes: their stems exist in NO
        # trailing region, so they are the mutation-sensitive evidence for the
        # gather test (pre-fix name-only gather read the whole title).
        "MIDONLY": _n("MIDONLY", "mentions #midonly in prose", "R", 7),
        "NOTEONLY": _n("NOTEONLY", "Plain title here", "R", 8, note="#noteonly in the body"),
    }


# ---------------------------------------------------------------------------
# is_slice_member: the four core behaviours
# ---------------------------------------------------------------------------
def test_mid_title_tag_not_member_under_name_but_member_under_parity():
    cache = _cache()
    name = cache["MID"]["name"]
    note = cache["MID"]["note"]
    assert is_slice_member(name, ["topic"], note=note, match_scope="name") is False
    assert (
        is_slice_member(name, ["topic"], note=note, match_scope="name_and_note")
        is True
    )


def test_note_only_tag_not_member_under_name_but_member_under_parity():
    cache = _cache()
    name = cache["NOTE"]["name"]
    note = cache["NOTE"]["note"]
    assert is_slice_member(name, ["topic"], note=note, match_scope="name") is False
    assert (
        is_slice_member(name, ["topic"], note=note, match_scope="name_and_note")
        is True
    )


def test_trailing_tag_is_member_under_both_scopes():
    cache = _cache()
    name = cache["TRAIL"]["name"]
    assert is_slice_member(name, ["topic"], note="", match_scope="name") is True
    assert (
        is_slice_member(name, ["topic"], note="", match_scope="name_and_note")
        is True
    )


def test_mid_title_curated_tag_does_not_curate_under_name_scope():
    cache = _cache()
    assert (
        node_is_curated_for_prefixes(
            cache["DOC"]["name"], ["curated"], note=cache["DOC"]["note"], match_scope="name"
        )
        is False
    )
    assert (
        node_is_curated_for_prefixes(
            cache["DOC"]["name"],
            ["curated"],
            note=cache["DOC"]["note"],
            match_scope="name_and_note",
        )
        is True
    )
    # The genuine trailing checkpoint curates under both.
    assert (
        node_is_curated_for_prefixes(
            cache["CUR"]["name"], ["curated"], note="", match_scope="name"
        )
        is True
    )


# ---------------------------------------------------------------------------
# Anchor descent boundary: uniformly trailing-only
# ---------------------------------------------------------------------------
def test_anchor_boundary_ignores_mid_title_dock_tag():
    cache = _cache()
    pred = build_anchor_boundary_predicate(["wfy-ns"])
    assert pred(cache["DOCKMID"]) is False
    assert pred(cache["DOCKTRAIL"]) is True


# ---------------------------------------------------------------------------
# Lightning Rod gather: name-only reads only the trailing region
# ---------------------------------------------------------------------------
def test_gather_name_only_is_trailing_region_only():
    cache = _cache()
    name_only = gather_tags_in_subtree(cache, "R", match_scope="name")
    parity = gather_tags_in_subtree(cache, "R", match_scope="name_and_note")
    # Trailing tags are gathered under name-only.
    assert "topic" in name_only
    assert "curated" in name_only  # trailing #curated on CUR
    assert "wfy-ns" in name_only  # trailing #wfy-ns on DOCKTRAIL
    # Stems that exist in NO trailing region are ABSENT under name-only ...
    assert "midonly" not in name_only
    assert "noteonly" not in name_only
    # ... and PRESENT under parity (the mutation-sensitive contrast).
    assert "midonly" in parity
    assert "noteonly" in parity
    assert set(name_only) <= set(parity)


# ---------------------------------------------------------------------------
# slice_group.render / merge.render default and per-atom override
# ---------------------------------------------------------------------------
def _lens_context(cache):
    groups = {"G": {"id": "grp-g", "name": "G", "root_uuid": "R", "prefixes": ["topic"]}}

    def resolve_group(handle):
        return groups[handle]

    def resolve_root(handle):
        return handle

    return LensEvalContext(
        cache_by_id=cache,
        resolve_group=resolve_group,
        resolve_root=resolve_root,
        anchor_boundary_predicate=build_anchor_boundary_predicate(["wfy-ns"]),
    )


def _snapshot(cache):
    return server._LensStage0Snapshot(
        context=_lens_context(cache),
        cache_by_id=cache,
        groups_cfg=tuple(),
        active_group_id=None,
        postfixes=MappingProxyType({}),
        global_never=tuple(),
        raw_config=MappingProxyType({}),
    )


def _leaf_ids(tree):
    if not isinstance(tree, dict):
        return set()
    children = [c for c in (tree.get("children") or []) if isinstance(c, dict)]
    if not children:
        return {tree.get("id")}
    out = set()
    for c in children:
        out |= _leaf_ids(c)
    return out


def _project(selection):
    cache = _cache()
    return asyncio.run(
        server._project_render_lens_from_cache(selection, snapshot=_snapshot(cache))
    )


def test_render_lens_defaults_name_only_and_explicit_atom_wins():
    # No match_scope on surface OR atom -> name-only (trailing region).
    default = _project({"mode": "render_lens", "lens": {"kind": "group", "group_handle": "G"}})
    assert _leaf_ids(default["final_tree"]) == {"TRAIL"}

    # Surface match_scope='name_and_note' restores parity for atoms that omit
    # their own.
    parity = _project(
        {
            "mode": "render_lens",
            "lens": {"kind": "group", "group_handle": "G"},
            "match_scope": "name_and_note",
        }
    )
    assert _leaf_ids(parity["final_tree"]) == {"TRAIL", "MID", "NOTE"}

    # An explicit per-atom match_scope always wins over the surface default.
    atom_wins = _project(
        {
            "mode": "render_lens",
            "lens": {
                "kind": "group",
                "group_handle": "G",
                "match_scope": "name_and_note",
            },
        }
    )
    assert _leaf_ids(atom_wins["final_tree"]) == {"TRAIL", "MID", "NOTE"}


def test_tags_atom_defaults_name_only_but_sift_query_stays_parity():
    # TagsAtom with no match_scope + a sift query: the TAG ATOM defaults
    # name-only, while the QUERY half keeps name_and_note (F5). The query
    # ("topic") matches under name_and_note; if the atom default were parity the
    # atom tree would also admit MID and NOTE, so a result of {TRAIL} proves the
    # atom default is name-only while the query still searches name+note.
    projected = _project(
        {
            "mode": "render_lens",
            "lens": {"kind": "tags", "root_handle": "R", "tags": ["topic"]},
            "query": "topic",
            "query_mode": "smart",
        }
    )
    assert _leaf_ids(projected["final_tree"]) == {"TRAIL"}
    # The adopted echo records the QUERY scope (name_and_note), not the atom
    # scope -- the two axes are independent.
    assert projected["adopted_selection"]["match_scope"] == "name_and_note"


# ---------------------------------------------------------------------------
# Acceptance check 5: a mid-title curated-family tag no longer moves the
# curation high-water mark under name-only (the surface the acceptance test
# names). Pre-fix this read the whole title via extract_name_tags, so the
# mid-title mention DID advance the cursor.
# ---------------------------------------------------------------------------
def test_mid_title_curated_tag_does_not_move_cursor_under_name_scope():
    cache = {
        "root": {"id": "root", "name": "Dock", "note": "", "createdAt": 900},
        "chk": {
            "id": "chk",
            "parent_id": "root",
            "name": "checkpoint #curated",
            "note": "",
            "createdAt": 1000,
        },
        "ap1": {
            "id": "ap1",
            "parent_id": "root",
            "name": "uncurated AP",
            "note": "",
            "createdAt": 2000,
        },
        "doc": {
            "id": "doc",
            "parent_id": "root",
            "name": "docs mention #curated mid-title here",
            "note": "",
            "createdAt": 3000,
        },
    }

    def _mark(**kwargs):
        return server._compute_curated_high_water_mark(
            cache_by_id=cache,
            root_uuid="root",
            active_prefixes=["curated"],
            axis="created",
            axis_direction="asc",
            **kwargs,
        )

    # name-only: the mid-title prose mention is invisible -> mark stays on the
    # genuine trailing checkpoint (1000).
    assert _mark(match_scope="name") == 1000
    # parity keeps Workflowy-search behaviour: the mid-title mention counts.
    assert _mark(match_scope="name_and_note") == 3000


# ---------------------------------------------------------------------------
# 084 matrix ([ap:8SSPVB], ruling 084): suppression and every family postfix
# (--suppress / --deprecated / --expand) count ONLY in the name's TRAILING
# tag region under match_scope='name'; the global never-expand tag counts
# ONLY in the trailing tag region under EVERY scope (Dan's D1). Positions
# per axis: trailing / mid-title / note-prose / note-backticked; scopes:
# name + name_and_note. The pre-fix behaviour read the whole name (global)
# or name+note prose (family postfixes), so every "inert under parity"
# assertion below is a regression row against the exact 084 repro.
# ---------------------------------------------------------------------------
def _classify(name, note="", prefixes=("foo",), scope="name", global_tags=()):
    return classify_node_tags(
        name,
        list(prefixes),
        note=note,
        global_never_expand_tags=list(global_tags),
        match_scope=scope,
    )


def test_084_global_suppress_mid_title_inert_under_every_scope():
    # THE 084 repro: a mid-title global tag suppressed before (whole-name
    # harvest); under the trailing-region doctrine it is prose.
    for scope in ("name", "name_and_note"):
        f = _classify(
            "Talks about #wfy-suppress today", global_tags=("wfy-suppress",), scope=scope
        )
        assert f["has_global_never_expand"] is False, scope


def test_084_global_suppress_in_note_inert_under_every_scope():
    for scope in ("name", "name_and_note"):
        f = _classify(
            "Plain title",
            note="see #wfy-suppress for why",
            global_tags=("wfy-suppress",),
            scope=scope,
        )
        assert f["has_global_never_expand"] is False, scope


def test_084_global_suppress_trailing_controls_under_both_scopes():
    # The control: a genuine trailing tag still suppresses; the off-switch
    # stays clean ('#wfy-suppress-old' is a different, inert tag).
    f = _classify(
        "Pocket #foo #wfy-suppress",
        prefixes=("foo",),
        global_tags=("wfy-suppress",),
        scope="name",
    )
    assert f["has_global_never_expand"] is True
    f2 = _classify(
        "Pocket #foo #wfy-suppress-old",
        prefixes=("foo",),
        global_tags=("wfy-suppress",),
        scope="name_and_note",
    )
    assert f2["has_global_never_expand"] is False


def test_084_family_suppress_positions_and_scopes():
    # Trailing: an operation under both scopes.
    assert _classify("A #foo--suppress", scope="name")["has_suppress"] is True
    assert _classify("A #foo--suppress", scope="name_and_note")["has_suppress"] is True
    # Mid-title: prose under 'name', an operation under parity.
    assert _classify("B discusses #foo--suppress here", scope="name")["has_suppress"] is False
    assert (
        _classify("B discusses #foo--suppress here", scope="name_and_note")["has_suppress"]
        is True
    )
    # Note prose: prose under 'name', an operation under parity.
    assert _classify("C plain", note="ops #foo--suppress", scope="name")["has_suppress"] is False
    assert (
        _classify("C plain", note="ops #foo--suppress", scope="name_and_note")["has_suppress"]
        is True
    )
    # Note backticks: inert under BOTH (the closing backtick rides the token,
    # so the postfix word never peels cleanly -- pre-existing parity quirk).
    assert _classify("D plain", note="ops `#foo--suppress`", scope="name")["has_suppress"] is False
    assert (
        _classify("D plain", note="ops `#foo--suppress`", scope="name_and_note")["has_suppress"]
        is False
    )


def test_084_expand_and_deprecated_same_positions():
    # --expand
    assert _classify("A #foo--expand", scope="name")["has_auto"] is True
    assert _classify("B about #foo--expand here", scope="name")["has_auto"] is False
    assert (
        _classify("B about #foo--expand here", scope="name_and_note")["has_auto"] is True
    )
    assert _classify("C plain", note="note #foo--expand", scope="name")["has_auto"] is False
    assert (
        _classify("C plain", note="note #foo--expand", scope="name_and_note")["has_auto"]
        is True
    )
    # --deprecated
    assert _classify("A #foo--deprecated", scope="name")["has_deprecated"] is True
    assert _classify("B about #foo--deprecated here", scope="name")["has_deprecated"] is False
    assert (
        _classify("B about #foo--deprecated here", scope="name_and_note")["has_deprecated"]
        is True
    )
    assert (
        _classify("C plain", note="note #foo--deprecated", scope="name")["has_deprecated"]
        is False
    )
    assert (
        _classify("C plain", note="note #foo--deprecated", scope="name_and_note")[
            "has_deprecated"
        ]
        is True
    )
    # Mid-name-only member: the base tag itself mid-title never made the node
    # a member under 'name'; its postfix axes follow the same positions.
    f = _classify("E mentions #foo--suppress mid-title", scope="name")
    assert f["has_active"] is False
    assert f["has_suppress"] is False


def test_084_name_has_global_never_tag_reader_positions():
    stems = {"wfy-suppress"}
    assert name_has_global_never_tag("Pocket #wfy-suppress", stems) is True
    assert name_has_global_never_tag("Talks about #wfy-suppress today", stems) is False
    assert name_has_global_never_tag("Pocket #wfy-suppress-x", stems) is False  # exact, not prefix
    assert name_has_global_never_tag("Pocket", stems) is False
    assert name_has_global_never_tag("Pocket", set()) is False


def test_084_node_has_suppress_ancestor_ignores_mid_title_global_tag():
    # 084 D1: an ancestor carrying #wfy-suppress MID-TITLE no longer
    # suppresses its subtree -- the reader is scope-free, so one control and
    # one flip cover every scope.
    cache = {
        "root": _mk("root", "Root #wfy-suppress", None, 0),
        "kid": _mk("kid", "Kid", "root", 1),
    }
    assert node_has_suppress_ancestor(cache, "kid", ["wfy-suppress"]) is True
    cache["root"]["name"] = "Root mentions #wfy-suppress in prose"
    assert node_has_suppress_ancestor(cache, "kid", ["wfy-suppress"]) is False


def test_084_collect_subtree_ids_includes_mid_title_global_child():
    # The render_symbols expand sweep's suppression guard now reads the
    # trailing region: a mid-title global tag no longer seals a child (and
    # its subtree) out of the sweep; a trailing one still does.
    cache = {
        "root": _mk("root", "Root", None, 0),
        "a": _mk("a", "A #wfy-suppress", "root", 1),
        "b": _mk("b", "B mentions #wfy-suppress today", "root", 2),
        "bchild": _mk("bchild", "BC", "b", 3),
    }
    got = collect_subtree_ids(cache, ["root"], suppress_tags=["wfy-suppress"])
    assert "a" not in got
    assert "b" in got
    assert "bchild" in got


def test_084_funnel_mid_title_global_member_renders():
    # Funnel row (build_stage1_base_tree): a MEMBER with a TRAILING #foo and
    # a MID-TITLE #wfy-suppress. Pre-fix the global flag read anywhere-in-
    # name and collapsed the member under BOTH scopes; under 084 D1 the
    # mid-title tag is prose and the member renders. The trailing control
    # still suppresses and zombie-prunes.
    cache = {
        "root": _mk("root", "Root #foo", None, 0),
        "pocket": _mk("pocket", "Pocket mentions #wfy-suppress today #foo", "root", 1),
        "live": _mk("live", "Live #foo", "root", 2),
    }
    for scope in ("name", "name_and_note"):
        final_subtree, plan, _prune = build_stage1_base_tree(
            cache, "root", ["foo"], global_never_expand_tags=["wfy-suppress"], match_scope=scope,
        )
        assert final_subtree is not None, scope
        assert _ids(final_subtree) == {"root", "pocket", "live"}, scope
        assert "pocket" not in plan["collapse_suppressed"], scope
    # Control: the trailing tag still collapses + prunes the member.
    cache2 = {
        "root": _mk("root", "Root #foo", None, 0),
        "pocket": _mk("pocket", "Pocket #foo #wfy-suppress", "root", 1),
        "live": _mk("live", "Live #foo", "root", 2),
    }
    final2, plan2, _p2 = build_stage1_base_tree(
        cache2, "root", ["foo"], global_never_expand_tags=["wfy-suppress"], match_scope="name",
    )
    assert "pocket" in plan2["collapse_suppressed"]
    assert _ids(final2) == {"root", "live"}


def test_084_merge_plan_reaches_leaf_under_mid_title_global_node():
    # Merge planner row (A7 + A6): a merged leaf whose ANCESTOR carries
    # #wfy-suppress MID-TITLE. Pre-fix the anywhere-in-name read sealed the
    # ancestor, the leaf was unreachable, and the planner refused naming the
    # spine blocker; under 084 D1 the mid-title tag is prose, the ancestor
    # is visible, and the plan completes (root expands over the branch).
    # The trailing control still seals and refuses.
    cache = {
        "root": _mk("root", "Root", None, 0),
        "gn": _mk("gn", "GN mentions #wfy-suppress today", "root", 1),
        "leaf": _mk("leaf", "Leaf #topic", "gn", 2),
    }
    plan = compute_merge_tag_plan(
        cache,
        {"leaf"},
        "root",
        target_tag="topic",
        expand_postfix_word="expand",
        global_never_expand_tags=["wfy-suppress"],
    )
    assert plan["ok"] is True, plan
    assert plan["expand_nodes"] == ["root"]
    # Control: the trailing tag still seals -> the leaf is unreachable ->
    # the planner refuses, naming the blocker.
    cache2 = {
        "root": _mk("root", "Root", None, 0),
        "gn": _mk("gn", "GN #wfy-suppress", "root", 1),
        "leaf": _mk("leaf", "Leaf #topic", "gn", 2),
    }
    plan2 = compute_merge_tag_plan(
        cache2,
        {"leaf"},
        "root",
        target_tag="topic",
        expand_postfix_word="expand",
        global_never_expand_tags=["wfy-suppress"],
    )
    assert plan2["ok"] is False
    assert "gn" in plan2.get("spine_blockers", [])
