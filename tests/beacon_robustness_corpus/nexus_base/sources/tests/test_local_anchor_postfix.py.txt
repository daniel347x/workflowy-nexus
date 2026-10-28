"""Regression lock for the LOCAL-ANCHOR postfix feature (Dan, 2026),
widened by the Dock-FAVORITES unit to the full Anchor-Dock tag FAMILY.

Feature
-------
A "local anchor" is an ordinary anchor whose anchor tag carries a configured
LOCAL postfix (default ``local``), joined as ``base<SEP>word`` -> ``wfy-ns--local``.
A local anchor is a FULLY-REAL anchor: it nests, and it STILL bounds a
``descendants`` GLIMPSE exactly like a global anchor. The ONLY difference is
that it is HIDDEN from the top-level ``list_anchors`` enumeration.

The Dock-favorites unit generalizes the postfix leg into a FAMILY of THREE
registered attribute-word lists -- local / favorite (default ``fav``) /
deprecated (default ``deprecated``) -- with ONE attribute per tag (postfixes
are never stacked): ``wfy-ns--fav`` STARS a Dock (a fully-real Dock), and
``wfy-ns--deprecated`` RETIRES it (in the family and reported, but NEVER
conferring Dock identity -- for identity it is as if absent). classify_anchor_tags
is UNION-based: every proper family tag asserts its attribute; a bare base
asserts nothing (the historical "global form dominates" rule is superseded --
``#wfy-ns`` + ``#wfy-ns--local`` IS local).

The crack this feature could have introduced (and these tests forbid)
---------------------------------------------------------------------
Anchor-hood is matched EXACTLY by base tag. A naive implementation would make
``list_anchors`` match ``#wfy-ns`` exactly, so ``#wfy-ns--local`` would stop
being recognized as an anchor AT ALL -- vanishing not just from the listing
(intended) but ALSO from the recursion boundary (``build_anchor_boundary_
predicate``) and the edit guard (``update_note_session``). That would let a
local sub-anchor's descendants leak up into a parent's ``descendants`` GLIMPSE
-- the exact thing anchors exist to prevent.

The fix is ONE shared classifier -- ``classify_anchor_tags`` -- that both the
listing and the boundary consult. Listing uses ``is_anchor and not is_local``;
the boundary (and the edit guard) use ``is_anchor`` (global OR local). These
tests lock that single-source-of-truth so the two can never drift.

Locked here
-----------
  * match_anchor_tag: exact base only (no wildcard middle, no bleed); the
    bare / local / favorite / deprecated forms with their ``attribute``;
    multiple / custom postfix words; longest-first across the union;
    word-collision precedence local > favorite > deprecated; stacked
    postfixes are NOT a match.
  * classify_anchor_tags: is_anchor/is_local/is_favorite; UNION semantics
    (a bare base asserts nothing); --deprecated never confers identity;
    matched_bases order; family_tags with proper=False for deprecated;
    empty bases; '#'-optional + markup tolerance.
  * The KEY INVARIANT: a local anchor is EXCLUDED from listing but INCLUDED as
    a descendants boundary -- proved against the real
    build_anchor_boundary_predicate over a cache projection. Extended by the
    favorites unit: a ``--fav``-only node BOUNDS descent (a favorite is a
    Dock); a ``--deprecated``-only node does NOT (it is not a Dock).
  * DEFAULT_LOCAL_ANCHOR_POSTFIX is 'local', DEFAULT_FAVORITE_ANCHOR_POSTFIX
    is 'fav', DEFAULT_DEPRECATED_ANCHOR_POSTFIX is 'deprecated'.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pytest  # noqa: E402

from workflowy_mcp.f8_slice_policy import (  # noqa: E402
    DEFAULT_LOCAL_ANCHOR_POSTFIX,
    DEFAULT_FAVORITE_ANCHOR_POSTFIX,
    DEFAULT_DEPRECATED_ANCHOR_POSTFIX,
    match_anchor_tag,
    classify_anchor_tags,
    build_anchor_boundary_predicate,
    build_selected_subtree_from_cache,
)


LOCAL = ["local"]
BASES = ["wfy-ns", "wfy-anchor"]
ALL = lambda node: True  # noqa: E731


# ---------------------------------------------------------------------------
# match_anchor_tag -- EXACT base only
# ---------------------------------------------------------------------------

def test_match_global_form():
    assert match_anchor_tag("wfy-ns", "wfy-ns", local_postfixes=LOCAL) == {
        "matched_base": "wfy-ns", "is_local": False, "postfix": "",
        "attribute": None,
    }


def test_match_local_form():
    assert match_anchor_tag("wfy-ns--local", "wfy-ns", local_postfixes=LOCAL) == {
        "matched_base": "wfy-ns", "is_local": True, "postfix": "local",
        "attribute": "local",
    }


def test_match_favorite_form_default_word():
    # favorite_postfixes omitted -> the shipped default ['fav'] applies.
    assert match_anchor_tag("wfy-ns--fav", "wfy-ns", local_postfixes=LOCAL) == {
        "matched_base": "wfy-ns", "is_local": False, "postfix": "fav",
        "attribute": "favorite",
    }


def test_match_deprecated_form_default_word():
    # deprecated_postfixes omitted -> the shipped default ['deprecated'].
    assert match_anchor_tag("wfy-ns--deprecated", "wfy-ns", local_postfixes=LOCAL) == {
        "matched_base": "wfy-ns", "is_local": False, "postfix": "deprecated",
        "attribute": "deprecated",
    }


def test_match_configured_favorite_and_deprecated_words_honoured():
    m = match_anchor_tag(
        "wfy-ns--star", "wfy-ns",
        local_postfixes=LOCAL, favorite_postfixes=["star"],
    )
    assert m is not None and m["attribute"] == "favorite" and m["postfix"] == "star"
    # With 'star' configured as the ONLY favorite word, the shipped default
    # 'fav' is no longer recognized (configured words replace the default).
    assert match_anchor_tag(
        "wfy-ns--fav", "wfy-ns",
        local_postfixes=LOCAL, favorite_postfixes=["star"],
    ) is None
    m2 = match_anchor_tag(
        "wfy-ns--retired", "wfy-ns",
        local_postfixes=LOCAL, deprecated_postfixes=["retired"],
    )
    assert m2 is not None and m2["attribute"] == "deprecated"


def test_match_word_collision_precedence_local_favorite_deprecated():
    # Degenerate config: the SAME word in two lists resolves deterministically
    # by the fixed precedence local > favorite > deprecated (no error).
    word = {"matched_base": "wfy-ns", "postfix": "dual", "attribute": "local",
            "is_local": True}
    assert match_anchor_tag(
        "wfy-ns--dual", "wfy-ns",
        local_postfixes=["dual"], favorite_postfixes=["dual"],
    ) == word
    assert match_anchor_tag(
        "wfy-ns--dual", "wfy-ns",
        local_postfixes=[], favorite_postfixes=["dual"],
        deprecated_postfixes=["dual"],
    )["attribute"] == "favorite"
    assert match_anchor_tag(
        "wfy-ns--dual", "wfy-ns",
        local_postfixes=[], favorite_postfixes=[], deprecated_postfixes=["dual"],
    )["attribute"] == "deprecated"


def test_match_stacked_postfix_is_not_a_match():
    # ONE attribute per tag (I2): stacked postfixes are an UNKNOWN postfix,
    # therefore not an anchor tag at all.
    assert match_anchor_tag("wfy-ns--local--fav", "wfy-ns", local_postfixes=LOCAL) is None
    assert match_anchor_tag("wfy-ns--fav--local", "wfy-ns", local_postfixes=LOCAL) is None


def test_match_no_middle_segment_bleed():
    # EXACT base: an anchor tag is an identity, NOT a slice family. These must
    # all fail (contrast slice_tag_variant_match, which WOULD match a middle).
    assert match_anchor_tag("wfy-nsx", "wfy-ns", local_postfixes=LOCAL) is None
    assert match_anchor_tag("wfy-ns-project", "wfy-ns", local_postfixes=LOCAL) is None
    assert match_anchor_tag("wfy-ns-project--local", "wfy-ns", local_postfixes=LOCAL) is None


def test_match_unrecognized_postfix_is_not_a_match():
    # A slice postfix like --expand is NOT a registered attribute word -> not
    # an anchor tag by this matcher (the two postfix domains are separate).
    # Same for a typo like --loca.
    assert match_anchor_tag("wfy-ns--expand", "wfy-ns", local_postfixes=LOCAL) is None
    assert match_anchor_tag("wfy-ns--loca", "wfy-ns", local_postfixes=LOCAL) is None


def test_match_multiple_and_longest_first():
    words = ["priv", "local", "localvault"]
    assert match_anchor_tag("wfy-ns--priv", "wfy-ns", local_postfixes=words)["postfix"] == "priv"
    # 'localvault' must win over 'local' (longest-first), never leaving a
    # spurious '-vault' middle.
    m = match_anchor_tag("wfy-ns--localvault", "wfy-ns", local_postfixes=words)
    assert m is not None and m["postfix"] == "localvault" and m["is_local"] is True


def test_match_longest_first_across_the_union():
    # A favorite word that EXTENDS a local word must win over it across the
    # union (longest-first is union-wide, not per-list).
    m = match_anchor_tag(
        "wfy-ns--localstar", "wfy-ns",
        local_postfixes=["local"], favorite_postfixes=["localstar"],
    )
    assert m is not None and m["postfix"] == "localstar" and m["attribute"] == "favorite"


def test_match_hash_optional():
    assert match_anchor_tag("#wfy-ns--local", "#wfy-ns", local_postfixes=LOCAL)["is_local"] is True


# ---------------------------------------------------------------------------
# classify_anchor_tags
# ---------------------------------------------------------------------------

def _cls(tags, bases=BASES, local=LOCAL):
    return classify_anchor_tags(tags, bases, local_postfixes=local)


def test_classify_global_anchor():
    v = _cls(["wfy-ns"])
    assert v["is_anchor"] is True and v["is_local"] is False
    assert v["is_favorite"] is False
    assert v["matched_base"] == "wfy-ns" and v["matched_tag"] == "wfy-ns"
    assert v["attributes"] == frozenset()
    assert v["matched_bases"] == ("wfy-ns",)


def test_classify_local_anchor():
    v = _cls(["wfy-ns--local"])
    assert v["is_anchor"] is True and v["is_local"] is True
    assert v["matched_base"] == "wfy-ns"
    assert v["attributes"] == frozenset({"local"})


def test_classify_favorite_anchor():
    v = _cls(["wfy-ns--fav"])
    assert v["is_anchor"] is True and v["is_favorite"] is True
    assert v["is_local"] is False
    assert v["matched_base"] == "wfy-ns" and v["matched_tag"] == "wfy-ns--fav"
    assert v["attributes"] == frozenset({"favorite"})


def test_classify_second_base_local():
    assert _cls(["wfy-anchor--local"])["is_local"] is True


def test_classify_non_anchor():
    v = _cls(["nope", "idea"])
    assert v["is_anchor"] is False and v["is_local"] is False
    assert v["is_favorite"] is False
    assert v["matched_base"] is None and v["matched_tag"] is None
    assert v["attributes"] == frozenset()
    assert v["matched_bases"] == ()
    assert v["family_tags"] == ()
    assert v["has_deprecated"] is False


def test_classify_union_bare_base_asserts_nothing():
    # REPLACES test_classify_global_form_dominates_local (union semantics, I5):
    # a bare base asserts NO attribute, so #wfy-ns + #wfy-ns--local IS local.
    v = _cls(["wfy-ns--local", "wfy-ns"])
    assert v["is_anchor"] is True and v["is_local"] is True
    v2 = _cls(["wfy-ns", "wfy-ns--local"])
    assert v2["is_anchor"] is True and v2["is_local"] is True
    # matched_base/matched_tag follow the FIRST PROPER family tag in name order.
    assert v2["matched_base"] == "wfy-ns" and v2["matched_tag"] == "wfy-ns"
    assert v["matched_tag"] == "wfy-ns--local"


@pytest.mark.parametrize(
    "tags,is_anchor,is_local,is_favorite,matched_bases",
    [
        # §6.2 truth table (bases {wfy-ns, convrisk-docks}, default words).
        (["wfy-ns"], True, False, False, ("wfy-ns",)),
        (["wfy-ns--local"], True, True, False, ("wfy-ns",)),
        (["wfy-ns--fav"], True, False, True, ("wfy-ns",)),
        (["wfy-ns--local", "wfy-ns--fav"], True, True, True, ("wfy-ns",)),
        (["wfy-ns", "wfy-ns--local"], True, True, False, ("wfy-ns",)),
        (["wfy-ns--deprecated"], False, False, False, ()),
        (["wfy-ns--deprecated", "wfy-ns--local"], True, True, False, ("wfy-ns",)),
        (["wfy-ns", "wfy-ns--deprecated"], True, False, False, ("wfy-ns",)),
        (["wfy-ns--local", "convrisk-docks"], True, True, False,
         ("wfy-ns", "convrisk-docks")),
        (["wfy-ns--fav", "convrisk-docks--deprecated"], True, False, True,
         ("wfy-ns",)),
        (["wfy-ns--expand"], False, False, False, ()),
        (["wfy-ns--loca"], False, False, False, ()),
        (["wfy-ns-test"], False, False, False, ()),
        (["wfy-ns--local--fav"], False, False, False, ()),
    ],
)
def test_classify_truth_table(tags, is_anchor, is_local, is_favorite, matched_bases):
    bases = ["wfy-ns", "convrisk-docks"]
    v = classify_anchor_tags(tags, bases, local_postfixes=LOCAL)
    assert v["is_anchor"] is is_anchor
    assert v["is_local"] is is_local
    assert v["is_favorite"] is is_favorite
    assert v["matched_bases"] == matched_bases
    assert v["has_deprecated"] is any("--deprecated" in t for t in tags)


def test_classify_deprecated_never_confers_identity_but_is_reported():
    v = _cls(["wfy-ns--deprecated"])
    assert v["is_anchor"] is False
    assert v["has_deprecated"] is True
    assert v["matched_base"] is None and v["matched_tag"] is None
    # family_tags includes the deprecated tag with proper=False.
    assert v["family_tags"] == ({
        "tag": "wfy-ns--deprecated", "base": "wfy-ns",
        "attribute": "deprecated", "postfix": "deprecated", "proper": False,
    },)
    # 'deprecated' is NEVER a member of attributes.
    v2 = _cls(["wfy-ns--fav", "wfy-ns--deprecated"])
    assert v2["attributes"] == frozenset({"favorite"})


def test_classify_matched_bases_order_and_family_tags_order():
    v = _cls(
        ["convrisk-docks", "wfy-ns--local", "wfy-ns--deprecated"],
        bases=["wfy-ns", "convrisk-docks"],
    )
    # First-occurrence order of bases having a PROPER tag; the deprecated-only
    # occurrence does not add a base.
    assert v["matched_bases"] == ("convrisk-docks", "wfy-ns")
    assert v["matched_base"] == "convrisk-docks"
    assert v["matched_tag"] == "convrisk-docks"
    # family_tags: EVERY family-form tag in name order, deprecated included.
    assert [ft["tag"] for ft in v["family_tags"]] == [
        "convrisk-docks", "wfy-ns--local", "wfy-ns--deprecated",
    ]
    assert [ft["proper"] for ft in v["family_tags"]] == [True, True, False]
    assert [ft["attribute"] for ft in v["family_tags"]] == [
        None, "local", "deprecated",
    ]


def test_classify_duplicate_tags_union():
    # Duplicate --fav tags: union is still {favorite}; both appear in family_tags.
    v = _cls(["wfy-ns--fav", "wfy-ns--fav"])
    assert v["is_anchor"] is True and v["is_favorite"] is True
    assert v["attributes"] == frozenset({"favorite"})
    assert len(v["family_tags"]) == 2


def test_classify_configured_favorite_words_honoured():
    v = classify_anchor_tags(
        ["wfy-ns--star"], ["wfy-ns"],
        local_postfixes=LOCAL, favorite_postfixes=["star"],
    )
    assert v["is_anchor"] is True and v["is_favorite"] is True


def test_classify_no_bleed():
    assert _cls(["wfy-nsx"])["is_anchor"] is False
    assert _cls(["wfy-ns-project"])["is_anchor"] is False


def test_classify_empty_bases():
    v = classify_anchor_tags(["wfy-ns"], [], local_postfixes=LOCAL)
    assert v["is_anchor"] is False
    assert v["matched_bases"] == () and v["family_tags"] == ()


def test_classify_markup_tolerant():
    assert _cls(["#<u>wfy-ns--local</u>"])["is_local"] is True
    assert _cls(["#<u>wfy-ns--fav</u>"])["is_favorite"] is True


def test_default_postfix_word():
    assert DEFAULT_LOCAL_ANCHOR_POSTFIX == "local"
    assert DEFAULT_FAVORITE_ANCHOR_POSTFIX == "fav"
    assert DEFAULT_DEPRECATED_ANCHOR_POSTFIX == "deprecated"


# ---------------------------------------------------------------------------
# THE KEY INVARIANT: local anchor EXCLUDED from listing, INCLUDED as boundary
# ---------------------------------------------------------------------------

def _node(nid, name, parent_id, priority=0, note=None):
    return {"id": nid, "name": name, "note": note, "parent_id": parent_id, "priority": priority}


def _find(subtree, nid):
    if subtree is None:
        return None
    if subtree.get("id") == nid:
        return subtree
    for ch in subtree.get("children") or []:
        hit = _find(ch, nid)
        if hit is not None:
            return hit
    return None


def test_local_anchor_still_bounds_descendants():
    # The whole point of the feature: a LOCAL sub-anchor must still STOP a
    # descendants projection, exactly like a global one -- otherwise its
    # contents leak up into the parent view.
    #   root
    #     local-anchor (#wfy-ns--local)  -> emitted as a leaf, descendants pruned
    #       hidden
    #     global-anchor (#wfy-ns)        -> also a boundary
    #       hidden2
    cache = {
        n["id"]: n for n in [
            _node("root", "Root", None, 0),
            _node("la", "Local sub anchor #wfy-ns--local", "root", 0),
            _node("h", "Hidden under local", "la", 0),
            _node("ga", "Global sub anchor #wfy-ns", "root", 1),
            _node("h2", "Hidden under global", "ga", 0),
        ]
    }
    pred = build_anchor_boundary_predicate(BASES, local_postfixes=LOCAL)
    tree = build_selected_subtree_from_cache(cache, "root", ALL, boundary_predicate=pred)

    # BOTH sub-anchors are emitted as leaves; NEITHER's descendants leak in.
    la = _find(tree, "la")
    assert la is not None and la.get("children") == []
    assert _find(tree, "h") is None
    ga = _find(tree, "ga")
    assert ga is not None and ga.get("children") == []
    assert _find(tree, "h2") is None


def test_boundary_predicate_default_local_postfix():
    # When local_postfixes is omitted, the predicate falls back to the shipped
    # default 'local' -- so #wfy-ns--local still bounds without explicit config.
    cache = {
        n["id"]: n for n in [
            _node("root", "Root", None, 0),
            _node("la", "Local anchor #wfy-ns--local", "root", 0),
            _node("h", "Hidden", "la", 0),
        ]
    }
    pred = build_anchor_boundary_predicate(["wfy-ns"])  # no local_postfixes passed
    tree = build_selected_subtree_from_cache(cache, "root", ALL, boundary_predicate=pred)
    assert _find(tree, "la").get("children") == []
    assert _find(tree, "h") is None


def test_favorite_bounds_and_deprecated_does_not():
    # The same KEY INVARIANT extended to the family (I4 + I10):
    #   * a #wfy-ns--fav-ONLY node IS a Dock -> it BOUNDS descent (favorite
    #     has no bearing on descent -- a Dock is a Dock);
    #   * a #wfy-ns--deprecated-ONLY node is NOT a Dock -> it does NOT bound
    #     (its descendants project through it exactly like a plain node's).
    #   root
    #     fav-anchor (#wfy-ns--fav)      -> emitted as a leaf, descendants pruned
    #       hidden-under-fav
    #     dep-anchor (#wfy-ns--deprecated) -> NOT a boundary
    #       visible-under-dep            -> projects through
    cache = {
        n["id"]: n for n in [
            _node("root", "Root", None, 0),
            _node("fa", "Fav anchor #wfy-ns--fav", "root", 0),
            _node("hf", "Hidden under fav", "fa", 0),
            _node("da", "Deprecated anchor #wfy-ns--deprecated", "root", 1),
            _node("vd", "Visible under deprecated", "da", 0),
        ]
    }
    pred = build_anchor_boundary_predicate(BASES, local_postfixes=LOCAL)
    tree = build_selected_subtree_from_cache(cache, "root", ALL, boundary_predicate=pred)

    fa = _find(tree, "fa")
    assert fa is not None and fa.get("children") == []
    assert _find(tree, "hf") is None
    da = _find(tree, "da")
    assert da is not None
    assert _find(tree, "vd") is not None  # NOT bounded: the child projects through


def test_boundary_predicate_default_favorite_and_deprecated_words():
    # When the new kwargs are omitted, the shipped defaults (fav / deprecated)
    # apply inside the predicate exactly like the local default does.
    cache = {
        n["id"]: n for n in [
            _node("root", "Root", None, 0),
            _node("fa", "Fav anchor #wfy-ns--fav", "root", 0),
            _node("hf", "Hidden", "fa", 0),
        ]
    }
    pred = build_anchor_boundary_predicate(["wfy-ns"])  # no postfix kwargs passed
    tree = build_selected_subtree_from_cache(cache, "root", ALL, boundary_predicate=pred)
    assert _find(tree, "fa").get("children") == []
    assert _find(tree, "hf") is None


def test_listing_vs_boundary_use_same_classifier_differently():
    # Document the intended asymmetry as an executable spec:
    #   listing filter  = is_anchor AND NOT is_local   (local hidden)
    #   boundary filter = is_anchor                    (local still bounds)
    local_v = _cls(["wfy-ns--local"])
    global_v = _cls(["wfy-ns"])
    # Listing: global shown, local hidden.
    assert (global_v["is_anchor"] and not global_v["is_local"]) is True
    assert (local_v["is_anchor"] and not local_v["is_local"]) is False
    # Boundary: BOTH bound.
    assert global_v["is_anchor"] is True
    assert local_v["is_anchor"] is True
