"""Regression lock for LIGHTNING ROD's Stage-1 SELECTION primitives.

Background
----------
Lightning Rod is the temporary/in-memory counterpart to a slice group: it
projects a subtree from the /nodes-export cache through the SAME universal
render pipeline, but runs Phase-1 (SELECTION) ONLY -- no presentation policy
(Phase 2/3/4) -- and supplies a custom admit predicate. Its three modes are:

  * gather        -> the tags found anywhere (name OR note) under a root.
  * render_tags   -> a prefix-FAMILY tag predicate (Option B; NO postfix
                     deprecation/suppress/expand semantics).
  * render_search -> a shlex 'smart' boolean/glob OR a 'regex' free-text
                     predicate.

The refactor that enabled Lightning Rod extracted the generic Phase-1
primitive ``build_selected_subtree_from_cache`` (predicate-driven) and turned
``build_slice_subtree_from_cache`` into a thin compatibility wrapper. These
tests lock:

  * the generic primitive: admit-by-predicate, minimal connective subtree,
    the ``exclude_completed`` traversal policy (root exempt),
  * the compatibility wrapper still reproduces slice-group + raw-dump behavior
    byte-for-byte (predicate seam did not change semantics),
  * ``build_tag_predicate`` Option-B family matching WITH NO postfix semantics
    (``monitor`` admits ``monitor-dell`` AND ``monitor--deprecated``, but NOT
    ``monitorx``); name+note reading,
  * ``gather_tags_in_subtree`` union + sort + de-dup + name/note; and the
    DOCUMENTED generous substring semantics it inherits from the shared
    ``extract_node_tags`` (a ``#`` inside prose is harvested -- intended),
  * ``build_search_predicate`` smart grammar (AND / OR / negation / phrase /
    glob, case-insensitive, name+note) + regex mode + loud errors on a
    malformed/empty query.

Pure-logic only: no MCP server, no WebSocket, no cache I/O -- the orchestrator
(_lightning_rod_from_cache) is a thin wrapper over these and is exercised
live, not unit-tested here.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pytest  # noqa: E402

from workflowy_mcp.f8_slice_policy import (  # noqa: E402
    build_selected_subtree_from_cache,
    build_slice_subtree_from_cache,
    build_tag_predicate,
    gather_tags_in_subtree,
    extract_strict_tags_from_text,
    extract_tags_from_text,
)
from workflowy_mcp.lightning_rod_search import (  # noqa: E402
    build_line_span_matcher,
    build_search_predicate,
    LightningRodQueryError,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _mk(nid, name, parent, prio, note=None):
    return {
        "id": nid,
        "name": name,
        "note": note,
        "parent_id": parent,
        "priority": prio,
    }


def _ids(subtree):
    """Set of node ids present in a materialized {id,name,note,children} tree."""
    if subtree is None:
        return None
    out = {subtree["id"]}
    for ch in subtree.get("children", []):
        out |= _ids(ch)
    return out


# A small mixed cache exercising: a plain family member, an unrelated-family
# near-miss (monitorx), a note-only tag, a middle-segment deprecated variant,
# and an untagged prose leaf.
def _monitor_cache():
    return {
        "root": _mk("root", "Root", None, 0),
        "a": _mk("a", "Dell screen #monitor-dell", "root", 1),
        "b": _mk("b", "Other #monitorx", "root", 2),
        "c": _mk("c", "Old", "root", 3, note="see #monitor--deprecated here"),
        "d": _mk("d", "Nothing here", "root", 4, note="just prose about a dell monitor"),
    }


# ---------------------------------------------------------------------------
# build_selected_subtree_from_cache -- the generic Stage-1 SELECTION primitive
# ---------------------------------------------------------------------------

def test_selected_subtree_admits_all_with_true_predicate():
    cache = _monitor_cache()
    sub = build_selected_subtree_from_cache(cache, "root", lambda n: True)
    assert _ids(sub) == {"root", "a", "b", "c", "d"}


def test_selected_subtree_minimal_connective_spine():
    # Only a deep leaf matches -> keep it + all ancestors up to root, drop the
    # rest. (Predicate: id == "leaf".)
    cache = {
        "root": _mk("root", "Root", None, 0),
        "mid": _mk("mid", "Mid", "root", 1),
        "leaf": _mk("leaf", "Leaf", "mid", 1),
        "sib": _mk("sib", "Sibling", "root", 2),
        "midkid": _mk("midkid", "MidKid", "mid", 2),
    }
    sub = build_selected_subtree_from_cache(cache, "root", lambda n: n["id"] == "leaf")
    # root + mid (connective ancestor) + leaf; sib and midkid pruned.
    assert _ids(sub) == {"root", "mid", "leaf"}


def test_selected_subtree_none_when_root_absent():
    cache = _monitor_cache()
    assert build_selected_subtree_from_cache(cache, "nope", lambda n: True) is None


def test_selected_subtree_children_priority_ordered():
    cache = {
        "root": _mk("root", "Root", None, 0),
        "x": _mk("x", "X", "root", 5),
        "y": _mk("y", "Y", "root", 1),
        "z": _mk("z", "Z", "root", 3),
    }
    sub = build_selected_subtree_from_cache(cache, "root", lambda n: True)
    order = [ch["id"] for ch in sub["children"]]
    assert order == ["y", "z", "x"], order


def test_selected_subtree_exclude_completed_prunes_child_subtree_root_exempt():
    # A completed CHILD (and its whole subtree) is pruned at admit time; the
    # ROOT is exempt even if completed.
    cache = {
        "root": _mk("root", "Root", None, 0),
        "done": _mk("done", "Done branch", "root", 1),
        "under": _mk("under", "Under done", "done", 1),
        "live": _mk("live", "Live branch", "root", 2),
    }
    cache["done"]["completedAt"] = 123456  # mark completed
    sub = build_selected_subtree_from_cache(
        cache, "root", lambda n: True, exclude_completed=True
    )
    # 'done' and its child 'under' are gone; root + live remain.
    assert _ids(sub) == {"root", "live"}


def test_selected_subtree_completed_root_itself_included():
    cache = {
        "root": _mk("root", "Root", None, 0),
        "live": _mk("live", "Live child", "root", 1),
    }
    cache["root"]["completedAt"] = 999  # completed ROOT
    sub = build_selected_subtree_from_cache(
        cache, "root", lambda n: True, exclude_completed=True
    )
    # Root is entered unconditionally; its non-completed child renders.
    assert _ids(sub) == {"root", "live"}


# ---------------------------------------------------------------------------
# build_slice_subtree_from_cache -- the compatibility WRAPPER is unchanged
# ---------------------------------------------------------------------------

def test_wrapper_member_all_raw_dump_matches_true_predicate():
    cache = _monitor_cache()
    raw = build_slice_subtree_from_cache(cache, "root", [], member_all=True)
    generic = build_selected_subtree_from_cache(cache, "root", lambda n: True)
    assert _ids(raw) == _ids(generic) == {"root", "a", "b", "c", "d"}


def test_wrapper_slice_membership_still_works():
    # Slice-group semantics via the wrapper: only nodes carrying #monitor-dell
    # (name) survive as members (+ connective ancestors).
    cache = _monitor_cache()
    sub = build_slice_subtree_from_cache(cache, "root", ["monitor-dell"])
    assert _ids(sub) == {"root", "a"}


def test_wrapper_none_when_root_absent():
    assert build_slice_subtree_from_cache(_monitor_cache(), "nope", ["x"]) is None


# ---------------------------------------------------------------------------
# build_tag_predicate -- Option-B prefix FAMILY match, NO postfix semantics
# ---------------------------------------------------------------------------

def test_tag_predicate_family_and_note_and_no_postfix_semantics():
    cache = _monitor_cache()
    pred = build_tag_predicate(["monitor"])
    sub = build_selected_subtree_from_cache(cache, "root", pred)
    # a: #monitor-dell (family) -> in.  c: note #monitor--deprecated -> STILL
    # in (no deprecation semantics).  b: #monitorx (different family) -> OUT.
    # d: no tag -> OUT.
    assert _ids(sub) == {"root", "a", "c"}


def test_tag_predicate_exact_family_stem():
    cache = _monitor_cache()
    pred = build_tag_predicate(["monitor-dell"])
    sub = build_selected_subtree_from_cache(cache, "root", pred)
    assert _ids(sub) == {"root", "a"}


def test_tag_predicate_strips_leading_hash_and_whitespace():
    cache = _monitor_cache()
    # Passing with '#' and surrounding whitespace must behave identically.
    pred = build_tag_predicate(["  #monitor "])
    sub = build_selected_subtree_from_cache(cache, "root", pred)
    assert _ids(sub) == {"root", "a", "c"}


def test_tag_predicate_empty_list_matches_nothing():
    cache = _monitor_cache()
    pred = build_tag_predicate([])
    # No requested prefixes -> predicate is False for every node -> only the
    # root survives as the connective anchor.
    sub = build_selected_subtree_from_cache(cache, "root", pred)
    assert _ids(sub) == {"root"}


def test_tag_predicate_monitorx_excluded_directly():
    pred = build_tag_predicate(["monitor"])
    assert pred({"name": "x #monitorx", "note": None}) is False
    assert pred({"name": "x #monitor-dell", "note": None}) is True
    assert pred({"name": "x", "note": "tagged only in note #monitor-lg-4k"}) is True


# ---------------------------------------------------------------------------
# gather_tags_in_subtree
# ---------------------------------------------------------------------------

def test_gather_union_sorted_deduped_name_and_note():
    cache = {
        "root": _mk("root", "Root #alpha", None, 0),
        "a": _mk("a", "A #beta #alpha", "root", 1),
        "b": _mk("b", "B", "root", 2, note="note tag #gamma and repeat #alpha"),
    }
    tags = gather_tags_in_subtree(cache, "root")
    # sorted, de-duped, '#'-stripped, union of name + note across the subtree.
    assert tags == ["alpha", "beta", "gamma"], tags


def test_gather_none_when_root_absent():
    assert gather_tags_in_subtree(_monitor_cache(), "nope") is None


def test_gather_empty_when_no_tags():
    cache = {
        "root": _mk("root", "Root", None, 0),
        "a": _mk("a", "Plain child", "root", 1),
    }
    assert gather_tags_in_subtree(cache, "root") == []


def test_gather_uses_strict_recognizer_no_code_junk():
    # THE FIX: gather uses the STRICT recognizer, so a '#' glued inside
    # prose/code (value.lstrip('#tail')) is NOT harvested as a tag -- only the
    # genuine leading-boundary #topic is returned. This is the opposite of the
    # generalized Workflowy-search harvester's behavior.
    cache = {
        "root": _mk("root", "Root", None, 0),
        "a": _mk("a", "real #topic", "root", 1, note="code: value.lstrip('#tail')"),
    }
    tags = gather_tags_in_subtree(cache, "root")
    assert tags == ["topic"], tags  # NO 'tail', NO junk


def test_gather_trims_trailing_punctuation_and_dedupes():
    # A real tag followed by sentence punctuation in prose is recognized WITHOUT
    # the trailing junk, and de-duped against a clean occurrence elsewhere.
    cache = {
        "root": _mk("root", "Root #alpha", None, 0),
        "a": _mk("a", "A", "root", 1, note="see #alpha), and also #beta."),
    }
    tags = gather_tags_in_subtree(cache, "root")
    assert tags == ["alpha", "beta"], tags


# ---------------------------------------------------------------------------
# extract_strict_tags_from_text -- the strict recognizer (contrast to the
# generalized Workflowy-search harvester)
# ---------------------------------------------------------------------------

def test_strict_recognizer_left_boundary_kills_glued_hashes():
    # The exact false positives the live smoke test surfaced from a code-bearing
    # note. NONE of these should be recognized as tags by the strict recognizer.
    junk = "value.lstrip('#tail') and #-autocomplete? no wait `#f8-expand`), foo#bar"
    strict = set(extract_strict_tags_from_text(junk))
    # 'tail' is glued after a quote -> rejected; 'bar' is glued after 'foo' ->
    # rejected; '-autocomplete' has no leading alnum after '#-'... actually '#-'
    # then 'autocomplete' IS alnum, but it is preceded by whitespace so '#-...'
    # -- the body starts with '-' which is legal, and it has alnum, so it IS a
    # tag ('-autocomplete'). That's acceptable (it genuinely reads as a tag in
    # prose). What must NOT appear: the quote/paren/word-glued ones.
    assert "tail" not in strict, strict
    assert "bar" not in strict, strict
    assert not any("').strip" in t or t.endswith("),") for t in strict), strict
    # A backtick is an opening-ish delimiter? No -- backtick is a TERMINATOR in
    # our set AND not an opening bracket, so `#f8-expand` is preceded by a
    # backtick (not whitespace/open-bracket) -> the '#' is NOT at a boundary ->
    # rejected. Confirm the backtick-glued form did not leak.
    assert "f8-expand" not in strict, strict


def test_strict_recognizer_accepts_real_tags_and_emoji():
    txt = "start #nexus--md-preview mid (#wfy-ns) and a trident #\U0001f531 too"
    strict = extract_strict_tags_from_text(txt)
    assert "nexus--md-preview" in strict
    assert "wfy-ns" in strict          # after an opening paren -> boundary OK
    assert "\U0001f531" in strict       # emoji body preserved


def test_strict_recognizer_start_of_string_and_trailing_trim():
    assert extract_strict_tags_from_text("#topic") == ["topic"]
    assert extract_strict_tags_from_text("#topic.") == ["topic"]
    assert extract_strict_tags_from_text("#topic, #other!") == ["topic", "other"]


def test_strict_recognizer_requires_alnum():
    # A '#' followed only by punctuation/hyphens is not a tag.
    assert extract_strict_tags_from_text("#--- and #.") == []


def test_strict_vs_generalized_divergence_is_real():
    # Lock the DELIBERATE divergence: the generalized harvester returns the
    # junk; the strict one does not. (Guards against someone "unifying" them.)
    code = "x = value.lstrip('#tail')"
    assert any(t.startswith("tail") for t in extract_tags_from_text(code))   # generalized: junk
    assert extract_strict_tags_from_text(code) == []                         # strict: clean


# ---------------------------------------------------------------------------
# build_search_predicate -- 'smart' grammar
# ---------------------------------------------------------------------------

def test_search_smart_and_is_default():
    pred = build_search_predicate("dell monitor")
    assert pred({"name": "Dell screen #monitor-dell", "note": None}) is True   # both
    assert pred({"name": "Nothing", "note": "just prose about a dell monitor"}) is True
    assert pred({"name": "dell only", "note": None}) is False                  # missing 'monitor'


def test_search_smart_or_alternation():
    pred = build_search_predicate("invoice OR receipt")
    assert pred({"name": "an invoice", "note": None}) is True
    assert pred({"name": "a receipt", "note": None}) is True
    assert pred({"name": "neither here", "note": None}) is False


def test_search_smart_negation_binds_within_group():
    # (invoice) OR (receipt AND NOT draft)
    pred = build_search_predicate("invoice OR receipt -draft")
    assert pred({"name": "invoice draft", "note": None}) is True    # matches group 1 (invoice)
    assert pred({"name": "receipt final", "note": None}) is True    # group 2, no draft
    assert pred({"name": "receipt draft", "note": None}) is False   # group 2 negated
    assert pred({"name": "quote only", "note": None}) is False


def test_search_smart_quoted_phrase():
    pred = build_search_predicate('"dell monitor"')
    assert pred({"name": "a dell monitor here", "note": None}) is True
    # words present but not contiguous -> no phrase match.
    assert pred({"name": "dell then later monitor", "note": None}) is False


def test_search_smart_glob():
    pred = build_search_predicate("inv*2026")
    assert pred({"name": "invoice-2026", "note": None}) is True
    assert pred({"name": "inv2026", "note": None}) is True
    assert pred({"name": "invoice-2025", "note": None}) is False


def test_search_smart_case_insensitive_and_note_field():
    pred = build_search_predicate("MONITOR")
    assert pred({"name": "no name tag", "note": "a Monitor in the note"}) is True


# ---------------------------------------------------------------------------
# build_search_predicate -- 'regex' mode
# ---------------------------------------------------------------------------

def test_search_regex_matches_name_or_note():
    pred = build_search_predicate(r"inv\d{4}", query_mode="regex")
    assert pred({"name": "inv2026 here", "note": None}) is True
    assert pred({"name": "no digits", "note": "note has inv1999"}) is True
    assert pred({"name": "invoice", "note": None}) is False   # no 4 digits


def test_search_regex_word_boundary_semantics():
    # \\w excludes '-', so 'monitor-\\w+' matches 'monitor-dell' but NOT
    # 'monitor--deprecated' (the second hyphen breaks \\w+). Locks the exact
    # regex semantics verified live.
    pred = build_search_predicate(r"monitor-\w+", query_mode="regex")
    assert pred({"name": "#monitor-dell", "note": None}) is True
    assert pred({"name": "#monitor--deprecated", "note": None}) is False


# ---------------------------------------------------------------------------
# build_search_predicate -- loud errors (never a silent empty slice)
# ---------------------------------------------------------------------------

def test_search_empty_query_raises():
    with pytest.raises(LightningRodQueryError):
        build_search_predicate("   ")


def test_search_bad_regex_raises():
    with pytest.raises(LightningRodQueryError):
        build_search_predicate("(unclosed", query_mode="regex")


def test_search_unbalanced_quotes_raises():
    with pytest.raises(LightningRodQueryError):
        build_search_predicate('"oops', query_mode="smart")


def test_search_unknown_mode_raises():
    with pytest.raises(LightningRodQueryError):
        build_search_predicate("term", query_mode="fuzzy")


def test_search_bare_negation_token_is_degenerate():
    # A query that parses to no usable positive/negative term is degenerate.
    with pytest.raises(LightningRodQueryError):
        build_search_predicate("-")


# ---------------------------------------------------------------------------
# build_line_span_matcher -- the grep tiers' per-line span matcher
# ([ap:47B7WO]); shares _smart_terms with build_search_predicate and returns
# the ONE span contract note_lines.search_note_documents consumes.
# ---------------------------------------------------------------------------

def test_line_span_matcher_smart_positive_terms_and_line_locality():
    matcher = build_line_span_matcher("dell monitor")
    assert matcher("Dell UltraSharp monitor arm") == [(0, 4), (16, 23)]
    assert matcher("dell only") == [(0, 4)]
    assert matcher("nothing here") == []
    # A negated term never marks a line (node level enforced it already).
    matcher = build_line_span_matcher("dell -monitor")
    assert matcher("dell monitor") == [(0, 4)]
    assert matcher("just dell") == [(5, 9)]


def test_line_span_matcher_or_groups_any_positive_term_hits():
    matcher = build_line_span_matcher("invoice OR receipt")
    assert matcher("the INVOICE") == [(4, 11)]
    assert matcher("a receipt") == [(2, 9)]
    assert matcher("neither") == []


def test_line_span_matcher_phrase_term_spans_contiguous_text():
    matcher = build_line_span_matcher('"27 inch monitor"')
    assert matcher('got a 27 INCH MONITOR today') == [(6, 21)]
    assert matcher("27 inch or monitor") == []


def test_line_span_matcher_glob_spans_the_concrete_match():
    matcher = build_line_span_matcher("inv*2026")
    spans = matcher("see INV-88-2026 filing")
    assert spans == [(4, 15)]
    # A bracketed contains-glob spans exactly the inner literal.
    matcher = build_line_span_matcher("*deploy*")
    assert matcher("redeploy now") == [(2, 8)]
    # A degenerate all-wildcard glob spans the whole line.
    matcher = build_line_span_matcher("*")
    assert matcher("anything") == [(0, 8)]


def test_line_span_matcher_regex_is_line_local_ignorecase_without_dotall():
    matcher = build_line_span_matcher(r"error\d+", query_mode="regex")
    assert matcher("first ERROR42 then error99") == [(6, 13), (19, 26)]
    assert matcher("no digits error") == []
    # A cross-line DOTALL pattern admitted the node at the node level but
    # must hit NO single line here -- the caller's D6 marker covers it.
    matcher = build_line_span_matcher("alpha.end", query_mode="regex")
    assert matcher("start alpha") == []
    assert matcher("end beta") == []


def test_line_span_matcher_errors_mirror_the_predicate_builder():
    with pytest.raises(LightningRodQueryError):
        build_line_span_matcher("   ")
    with pytest.raises(LightningRodQueryError):
        build_line_span_matcher("([unclosed", query_mode="regex")
    with pytest.raises(LightningRodQueryError):
        build_line_span_matcher("x", query_mode="fuzzy")
    # A query the predicate accepts, the matcher accepts too.
    build_search_predicate("inv*2026")
    build_line_span_matcher("inv*2026")
