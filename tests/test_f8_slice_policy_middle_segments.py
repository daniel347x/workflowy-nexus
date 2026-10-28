"""Regression lock for the F8 slice-policy MIDDLE-SEGMENT wildcard tag rule.

Background
----------
A slice group is defined by a set of base tag PREFIXES (e.g. ``comm-jim``).
Originally, membership/classification matched a tag by a naked ``startswith``
on the prefix, and the deprecated exclusion looked for ``prefix + "--" +
deprecated`` glued directly to the prefix. That broke the moment a user put a
personal MIDDLE SEGMENT on a slice tag: ``#comm-jim-teams--deprecated`` was
counted as a LIVE member of the ``comm-jim`` slice (front-match) but NEVER as
deprecated (its ``--deprecated`` sits on the ``comm-jim-teams`` sub-family, not
on ``comm-jim--deprecated``), so deprecating it did nothing on the render side.

The fix introduced ONE shared recognizer,
``f8_slice_policy.slice_tag_variant_match``, matching::

    P [ '-<middle>' ] [ '--<postfix>' ]

* FRONT: literal ``startswith P``.
* MIDDLE: everything between P and any recognized trailing ``--<postfix>`` must
  be EMPTY or START WITH ``'-'`` (hyphen = required segment delimiter), so
  ``comm-jim`` matches ``comm-jim`` / ``comm-jim-teams`` / ``comm-jim--
  deprecated`` but NOT the unrelated family ``comm-jimx`` / ``comm-jimmy``.
* POSTFIX: a recognized postfix word (expand / suppress / deprecated) in the
  trailing ``--<word>`` region; otherwise the tag is a plain family member.

Every membership/classification/curation consumer funnels through that ONE
matcher (``is_slice_member``, ``classify_node_tags``,
``node_is_curated_for_prefixes``), so F8, the Markdown modal, and the
server-side slice GLIMPSE cannot drift.

These tests lock:
  * the matcher's front/middle/postfix decomposition + the hyphen boundary,
  * membership + classification for middle-segment variants (the bug case),
  * the MULTIPLE-PREFIX independent-family rule (A live wins even if B is
    deprecated; per-prefix OR),
  * the within-prefix deprecation rule (a deprecated variant of P evicts P
    even alongside a co-present live variant of P -- Workflowy-search-equiv),
  * the capped, variant-enumerating search-string compiler + its refusal,
  * ``collect_deprecated_variant_tags`` cache scan (family-local only).
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pytest  # noqa: E402

from workflowy_mcp.f8_slice_policy import (  # noqa: E402
    POSTFIX_SEP,
    SLICE_SEARCH_MAX_TERMS,
    slice_tag_variant_match,
    _recognized_postfix_words,
    is_slice_member,
    classify_node_tags,
    node_is_curated_for_prefixes,
    compile_slice_search_expression,
    collect_deprecated_variant_tags,
    build_slice_subtree_from_cache,
)


# ---------------------------------------------------------------------------
# The shared matcher: P [ '-<middle>' ] [ '--<postfix>' ]
# ---------------------------------------------------------------------------

PW = _recognized_postfix_words()  # {expand, suppress, deprecated}


def test_match_bare_prefix():
    assert slice_tag_variant_match("comm-jim", "comm-jim", postfix_words=PW) == {
        "matched_prefix": "comm-jim",
        "middle": "",
        "postfix": "",
    }


def test_match_prefix_plus_deprecated_no_middle():
    assert slice_tag_variant_match(
        "comm-jim--deprecated", "comm-jim", postfix_words=PW
    ) == {"matched_prefix": "comm-jim", "middle": "", "postfix": "deprecated"}


def test_match_the_bug_case_middle_plus_deprecated():
    # The exact real-world tag that triggered the whole fix.
    assert slice_tag_variant_match(
        "comm-jim-teams--deprecated", "comm-jim", postfix_words=PW
    ) == {"matched_prefix": "comm-jim", "middle": "-teams", "postfix": "deprecated"}


def test_match_middle_no_postfix_is_plain_family():
    assert slice_tag_variant_match(
        "comm-jim-teams", "comm-jim", postfix_words=PW
    ) == {"matched_prefix": "comm-jim", "middle": "-teams", "postfix": ""}


@pytest.mark.parametrize("word", ["expand", "suppress", "deprecated"])
def test_match_middle_plus_each_postfix(word):
    tag = f"comm-jim-teams{POSTFIX_SEP}{word}"
    m = slice_tag_variant_match(tag, "comm-jim", postfix_words=PW)
    assert m is not None and m["middle"] == "-teams" and m["postfix"] == word


@pytest.mark.parametrize("tag", ["comm-jimx", "comm-jimmy", "comm-jimx--deprecated"])
def test_boundary_rule_no_bleed_into_sibling_family(tag):
    # A middle NOT starting with '-' must NOT match (the hyphen-delimiter rule).
    assert slice_tag_variant_match(tag, "comm-jim", postfix_words=PW) is None


def test_match_unrelated_tag_is_none():
    assert slice_tag_variant_match("topic-legal", "comm-jim", postfix_words=PW) is None


# ---------------------------------------------------------------------------
# Membership + classification for middle-segment variants (the bug fix)
# ---------------------------------------------------------------------------

JP = ["comm-jim"]


def test_membership_middle_seg_deprecated_evicts():
    # THE BUG: previously wrongly LIVE; now correctly excluded.
    assert not is_slice_member("X #comm-jim-teams--deprecated", JP)


def test_membership_middle_seg_plain_is_member():
    assert is_slice_member("X #comm-jim-teams", JP)


def test_membership_sibling_family_not_member():
    assert not is_slice_member("X #comm-jimx", JP)


def test_classify_middle_seg_deprecated():
    f = classify_node_tags("X #comm-jim-teams--deprecated", JP)
    assert f["has_active"] and f["has_deprecated"]
    assert not f["has_auto"] and not f["has_suppress"]


def test_classify_middle_seg_plain_active_only():
    f = classify_node_tags("X #comm-jim-teams", JP)
    assert f["has_active"]
    assert not f["has_deprecated"] and not f["has_auto"] and not f["has_suppress"]


def test_classify_middle_seg_expand_and_suppress():
    assert classify_node_tags("X #comm-jim-teams--expand", JP)["has_auto"]
    assert classify_node_tags("X #comm-jim-teams--suppress", JP)["has_suppress"]


def test_classify_sibling_family_not_active():
    f = classify_node_tags("X #comm-jimx--deprecated", JP)
    assert not f["has_active"] and not f["has_deprecated"]


# ---------------------------------------------------------------------------
# WITHIN-PREFIX deprecation (Workflowy-search equivalence anchor)
# ---------------------------------------------------------------------------


def test_within_prefix_deprecated_variant_evicts_despite_coplain():
    # A deprecated variant of P deprecates P on the node even alongside a
    # co-present plain variant of the SAME base prefix -- exactly what the
    # compiled search `#comm-jim -#comm-jim--deprecated -#comm-jim-teams--
    # deprecated` computes (negation excludes on EITHER deprecated tag).
    assert not is_slice_member("X #comm-jim-teams #comm-jim--deprecated", JP)
    assert not is_slice_member("X #comm-jim #comm-jim-teams--deprecated", JP)


# ---------------------------------------------------------------------------
# MULTIPLE-PREFIX INDEPENDENT FAMILIES (Dan's A/B audit question)
# ---------------------------------------------------------------------------
# Two independent prefixes A and B. The per-prefix OR must isolate the two
# families: a live (non-deprecated) A keeps the node in even when B is
# deprecated, and vice versa; the node is out ONLY when BOTH families are
# deprecated.

AB = ["A", "B"]


def test_multi_prefix_A_live_B_deprecated_included():
    # Live via A (bare), B deprecated -> A wins (independent families).
    assert is_slice_member("X #A #B--deprecated", AB)


def test_multi_prefix_A_live_via_middle_B_deprecated_via_middle_included():
    # Live via a MIDDLE-SEGMENT A variant; B deprecated via a middle variant.
    # A's family is untouched by B's deprecation -> included.
    assert is_slice_member("X #A-personal #B-teams--deprecated", AB)


def test_multi_prefix_B_live_A_deprecated_included():
    assert is_slice_member("X #A--deprecated #B", AB)


def test_multi_prefix_both_deprecated_excluded():
    assert not is_slice_member("X #A--deprecated #B--deprecated", AB)


def test_multi_prefix_both_deprecated_via_middle_excluded():
    assert not is_slice_member("X #A-x--deprecated #B-y--deprecated", AB)


def test_multi_prefix_only_A_present_and_live():
    # B absent entirely; A live -> member (a single live family suffices).
    assert is_slice_member("X #A-note", AB)


# ---------------------------------------------------------------------------
# Curation predicate (since="curated") -- deprecated counts, presentation
# postfixes (--suppress / --expand) do NOT, strict on family
# ---------------------------------------------------------------------------


def test_curated_counts_deprecated_variants():
    # A --deprecated variant is STILL curated (triaging is a curation act),
    # including middle-segment variants. (The "#comm-jim--suppress is STILL
    # curated" assertion that used to live here pinned the Session-019
    # curated-cursor bug; it moved, flipped, into
    # test_curated_ignores_suppress_and_expand_variants below.)
    assert node_is_curated_for_prefixes("X #comm-jim-teams--deprecated", JP)
    assert node_is_curated_for_prefixes("X #comm-jim", JP)


def test_curated_ignores_suppress_and_expand_variants():
    # --suppress / --expand are PRESENTATION postfixes (a lens carve-out /
    # auto-expansion), NOT curation acts: they must NOT move the curation
    # cursor. Session-019 bug: a nested program Dock root tagged
    # #curated--suppress became the newest "curated" node and stranded every
    # AP captured before it.
    assert not node_is_curated_for_prefixes("X #comm-jim--suppress", JP)
    assert not node_is_curated_for_prefixes("X #comm-jim--expand", JP)
    # middle-segment presentation variants are equally non-curating
    assert not node_is_curated_for_prefixes("X #comm-jim-teams--suppress", JP)
    assert not node_is_curated_for_prefixes("X #comm-jim-teams--expand", JP)
    # a different family's presentation variant never curated anyway
    assert not node_is_curated_for_prefixes("X #comm-jimx--suppress", JP)
    # presentation variants in the NOTE field are equally non-curating
    assert not node_is_curated_for_prefixes("X", JP, note="body #comm-jim--suppress")
    # ...but a co-present plain or --deprecated variant of the family still
    # curates the node (the non-presentation match counts)
    assert node_is_curated_for_prefixes("X #comm-jim--suppress #comm-jim", JP)
    assert node_is_curated_for_prefixes(
        "X #comm-jim--suppress #comm-jim-teams--deprecated", JP
    )


def test_curated_respects_family_boundary():
    # comm-jimx is a DIFFERENT family -> does NOT curate the comm-jim slice
    # (this is the fix that stopped node_is_curated_for_prefixes from drifting
    # back to the loose startswith).
    assert not node_is_curated_for_prefixes("X #comm-jimx", JP)
    assert not node_is_curated_for_prefixes("X #comm-jimx--deprecated", JP)


# ---------------------------------------------------------------------------
# Compiled search string: bundle, variant enumeration, family isolation, cap
# ---------------------------------------------------------------------------


def test_compile_bundle_base_only():
    r = compile_slice_search_expression(["tag--a"])
    assert r["search_string"] == "#tag--a -#tag--a--deprecated"
    assert r["too_many_variants"] is False
    assert r["term_count"] == 2


def test_compile_bundle_two_prefixes_are_isolated_or_clauses():
    # Independent families -> two OR clauses, each carrying only ITS OWN
    # deprecated negations (A's variants under A's clause, B's under B's).
    r = compile_slice_search_expression(
        ["A", "B"],
        deprecated_variants_by_prefix={
            "A": ["A--deprecated", "A-x--deprecated"],
            "B": ["B--deprecated", "B-y--deprecated"],
        },
    )
    assert r["search_string"] == (
        "#A -#A--deprecated -#A-x--deprecated"
        " OR #B -#B--deprecated -#B-y--deprecated"
    )
    assert r["too_many_variants"] is False


def test_compile_enumerates_middle_variants():
    r = compile_slice_search_expression(
        ["comm-jim"],
        deprecated_variants_by_prefix={
            "comm-jim": [
                "comm-jim--deprecated",
                "comm-jim-teams--deprecated",
                "comm-jim-phone--deprecated",
            ]
        },
    )
    assert r["search_string"] == (
        "#comm-jim -#comm-jim--deprecated"
        " -#comm-jim-teams--deprecated -#comm-jim-phone--deprecated"
    )
    assert r["term_count"] == 4


def test_compile_cap_refuses():
    many = [f"comm-jim-v{i}--deprecated" for i in range(200)]
    r = compile_slice_search_expression(
        ["comm-jim"],
        deprecated_variants_by_prefix={"comm-jim": many},
        max_terms=SLICE_SEARCH_MAX_TERMS,
    )
    assert r["search_string"] is None
    assert r["too_many_variants"] is True
    assert r["term_count"] > SLICE_SEARCH_MAX_TERMS


def test_compile_default_cap_value():
    # Guard against an accidental cap change; 80 is the shipped value.
    assert SLICE_SEARCH_MAX_TERMS == 80


# ---------------------------------------------------------------------------
# collect_deprecated_variant_tags: cache scan is family-local
# ---------------------------------------------------------------------------


def _mk(nid, name, parent, prio):
    return {"id": nid, "name": name, "note": None, "parent_id": parent, "priority": prio}


def test_collect_deprecated_variants_family_local():
    cache = {
        "root": _mk("root", "Root", None, 0),
        "a": _mk("a", "A #comm-jim", "root", 1),
        "b": _mk("b", "B #comm-jim-teams--deprecated", "root", 2),
        "c": _mk("c", "C #comm-jim--deprecated", "root", 3),
        # comm-jimx is a DIFFERENT family and must NOT be collected under comm-jim.
        "d": _mk("d", "D #comm-jimx--deprecated", "root", 4),
    }
    dv = collect_deprecated_variant_tags(cache, "root", ["comm-jim"])
    assert dv == {
        "comm-jim": ["comm-jim--deprecated", "comm-jim-teams--deprecated"]
    }


def test_collect_deprecated_variants_empty_when_no_cache():
    assert collect_deprecated_variant_tags(None, "root", ["comm-jim"]) == {"comm-jim": []}
    assert collect_deprecated_variant_tags({}, "root", ["comm-jim"]) == {"comm-jim": []}


# ---------------------------------------------------------------------------
# End-to-end Phase-1 membership over the cache (the render-side fix)
# ---------------------------------------------------------------------------


def test_phase1_excludes_fully_deprecated_middle_variant_leaf():
    # The originating scenario: a leaf whose ONLY comm-jim tag is a middle-
    # segment deprecated variant must NOT appear in the projected subtree.
    cache = {
        "root": _mk("root", "Root", None, 0),
        "live": _mk("live", "Live #comm-jim", "root", 1),
        "dep": _mk("dep", "Dead #comm-jim-teams--deprecated", "root", 2),
    }
    sub = build_slice_subtree_from_cache(cache, "root", ["comm-jim"])
    ids = set()

    def gather(n):
        ids.add(n["id"])
        for ch in n["children"]:
            gather(ch)

    gather(sub)
    assert ids == {"root", "live"}, ids
