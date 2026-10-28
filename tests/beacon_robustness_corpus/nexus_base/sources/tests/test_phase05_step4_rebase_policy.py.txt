"""Phase 0.5 Step 4 (ap:EXECLDG) — REBASE baseline relocation, UNFOCUSED parity.

Step 4 lifts the per-file / per-heading H1 rebase baseline
(``_min_md_depth_in_subtree``) OUT of the inline composer walk and into a
precomputed, render-forest-keyed ``RenderPolicy`` (produced by
``_compute_rebase_policy_for_forest``). The composer then CONSUMES the policy
baseline at BOTH rebase call-sites — the ``is_file_node`` branch AND the
bare-MD-heading-root branch (v3.3 #2) — instead of recomputing inline.

THE DELIVERABLE this file locks: on the UNFOCUSED path (render_forest ==
document_forest) the policy-driven render is BYTE-FOR-BYTE identical to the
inline (no-policy) render. That is the "composer-consumes-injected-policy ==
composer-computes-inline" parity the ledger's Step 4 requires. It MUST hold —
Step 4 is behavior-preserving by construction (§11): the baseline computed from
the unpruned document_forest equals the inline subtree walk because the tree is
the same, and a no-md-heading (source-file) subtree stores NO policy entry so
the composer's inline ``None`` fallback matches exactly.

⚠️ FIXTURE NOTE (a real coverage finding recorded during Step 4). A node counts
as an ``is_md_heading`` (and so exercises the rebase call-sites) ONLY when its
NOTE's first non-blank line is exactly ``MD_PATH:`` on its own line, followed by
a ``#``-heading line (``_md_depth_from_note_for_preview``). The Step-1 golden
``_md_file_forest`` fixture uses ``MD_PATH: doc.md#Intro`` (path on the SAME
line), which returns depth None — so its "headings" actually render via the
markdown-CONTENT embedding branch, NOT the ``is_md_heading`` rebase branch. The
fixtures HERE deliberately use the canonical bare-``MD_PATH:`` form so the
rebase branch is genuinely exercised (the whole point of Step 4). Steps 6/7
should not assume the Step-1 matrix already covers the ``is_md_heading`` rebase
path — it does not.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus as W,
)
from workflowy_mcp.cache_render_spec import RenderPolicy  # noqa: E402


# ---------------------------------------------------------------------------
# Tiny builder (mirrors the Step-1 golden baseline harness).
# ---------------------------------------------------------------------------
def _node(nid, name, note="", children=None):
    return {"id": nid, "name": name, "note": note, "children": children or []}


def _render(forest, policy=None):
    return W._build_markdown_preview_from_tree(
        forest, suppress_metadata=True, render_policy=policy
    )


def _assert_policy_parity(forest):
    """The Step-4 core assertion: policy-driven render == inline render.

    Precompute the rebase policy from the (unfocused) forest, render once with
    it and once without, and require byte-for-byte equality. Returns the policy
    so callers can additionally assert its key structure.
    """
    policy = W._compute_rebase_policy_for_forest(forest)
    inline = _render(forest, policy=None)
    policied = _render(forest, policy=policy)
    assert policied == inline, (
        "STEP-4 PARITY VIOLATION: policy-driven render diverged from the "
        f"inline render.\n  inline:   {inline!r}\n  policied: {policied!r}"
    )
    return policy, inline


# ===========================================================================
# FIXTURES
# ===========================================================================
def _plain_forest():
    # No Cartographer files, no md-headings -> the rebase call-sites never fire.
    return [
        _node("root", "Root", "root body", [
            _node("a", "Alpha", "alpha body", [
                _node("a1", "Alpha-1", "a1 body"),
                _node("a2", "Alpha-2", "a2 body"),
            ]),
            _node("b", "Beta", "beta body"),
        ]),
    ]


def _md_file_forest_real_headings():
    # A Cartographer Markdown FILE whose descendants are REAL is_md_heading
    # nodes (note first line == bare 'MD_PATH:'), starting deeper than H1 so
    # the per-file rebase math (base=1 + (md - min_d)) is genuinely exercised.
    #   doc.md  (file root)  -> file-node rebase call-site
    #     Intro   (### => H3, min in subtree)   -> rebased to H1
    #       Details (##### => H5)               -> rebased to H3
    return [
        _node("fr", "doc.md", "Root: E:\\tmp\\doc.md", [
            _node("h1", "Intro", "MD_PATH:\n### Intro", [
                _node("h2", "Details", "MD_PATH:\n##### Details\n\ndetails body"),
            ]),
        ]),
    ]


def _bare_md_heading_root_forest():
    # A top-level md-heading with NO file node above it: the composer's
    # bare-MD-heading-ROOT branch (file_base is None) fires and consults the
    # policy at index_path (0,). v3.3 #2 case.
    return [
        _node("h1", "Intro", "MD_PATH:\n### Intro", [
            _node("h2", "Sub", "MD_PATH:\n##### Sub"),
        ]),
    ]


def _source_regime_forest():
    # A '#'-comment note body under a non-Markdown source-file regime. No
    # md-headings anywhere -> _min_md_depth_in_subtree is None -> NO policy
    # entry -> the inline None fallback must match.
    return [
        _node("fn", "mod.py", "Path: mod.py", [
            _node("f", "some_function",
                  "# this is a python comment, not a heading\ncode line"),
        ]),
    ]


def _mixed_forest():
    # A plain subtree, then a Markdown file with real headings, then another
    # plain subtree — exercises the file boundary + post-file structural resume
    # alongside the rebase call-sites, all in one document.
    return [
        _node("root", "Root", "root body", [
            _node("plain", "Plain", "plain body"),
            _node("fr", "guide.md", "Root: E:\\tmp\\guide.md", [
                _node("g1", "Guide", "MD_PATH:\n## Guide", [
                    _node("g2", "Section", "MD_PATH:\n#### Section\n\nsec body"),
                ]),
            ]),
            _node("tail", "Tail", "tail body"),
        ]),
    ]


# ===========================================================================
# PARITY LOCKS — the Step-4 deliverable (policy render == inline render)
# ===========================================================================
def test_parity_plain_forest():
    """No files / no md-headings: rebase sites never fire; empty policy; parity."""
    policy, _ = _assert_policy_parity(_plain_forest())
    assert policy.rebase_by_render_key == {}


def test_parity_md_file_real_headings():
    """File-node rebase branch genuinely exercised; policy render == inline."""
    policy, out = _assert_policy_parity(_md_file_forest_real_headings())
    # The file node (0,) carries the shallowest md-depth (H3 Intro => 3).
    assert policy.rebase_base_for((0,)) == 3
    # Rebase produced H1 Intro / H3 Details (base 1 + (5-3)).
    assert "# Intro" in out and "### Details" in out


def test_parity_bare_md_heading_root():
    """Bare-MD-heading-root branch (v3.3 #2) consumes the policy; parity holds."""
    policy, out = _assert_policy_parity(_bare_md_heading_root_forest())
    assert policy.rebase_base_for((0,)) == 3
    assert out == "# Intro\n\n### Sub"


def test_parity_source_regime():
    """Source file, no md-headings: NO policy entry; inline None fallback matches."""
    policy, _ = _assert_policy_parity(_source_regime_forest())
    assert policy.rebase_by_render_key == {}


def test_parity_mixed_forest():
    """Plain + Markdown-file + plain in one doc: full-walk parity."""
    policy, _ = _assert_policy_parity(_mixed_forest())
    # The guide.md file node is at index_path (0, 1) (2nd child of root);
    # its shallowest md-depth is H2 Guide => 2.
    assert policy.rebase_base_for((0, 1)) == 2


# ===========================================================================
# PRODUCER STRUCTURE LOCKS — only REAL baselines stored; render-forest keying
# ===========================================================================
def test_producer_stores_only_real_baselines_no_none_values():
    """A no-md-heading subtree stores NO entry (never a None value).

    This is what removes the absent-key vs None-value ambiguity: the composer's
    inline None fallback for a source/no-heading subtree must line up with an
    ABSENT key, so the producer must never store None.
    """
    policy = W._compute_rebase_policy_for_forest(_source_regime_forest())
    assert policy.rebase_by_render_key == {}
    # And every stored value is a real int (never None).
    policy2 = W._compute_rebase_policy_for_forest(
        _md_file_forest_real_headings()
    )
    assert policy2.rebase_by_render_key  # non-empty
    assert all(isinstance(v, int) for v in policy2.rebase_by_render_key.values())


def test_producer_keys_are_render_forest_index_paths():
    """Keys are list-rooted index_path tuples in the composer's coordinate system.

    (0,) is the top-level file/heading root; deeper entries extend it by child
    index — the SAME coordinate _build_markdown_preview_from_tree seeds as
    (top_index,) and extends as index_path + (ci,).
    """
    policy = W._compute_rebase_policy_for_forest(
        _md_file_forest_real_headings()
    )
    keys = set(policy.rebase_by_render_key)
    # file root (0,), Intro heading (0,0), Details heading (0,0,0).
    assert keys == {(0,), (0, 0), (0, 0, 0)}
    assert all(isinstance(k, tuple) for k in keys)


def test_producer_returns_render_policy_with_empty_body_sets():
    """Step 4 populates ONLY the rebase map; body-suppression sets stay empty.

    (Those are filled by the focus/deprecated materializer in Steps 6/7.)
    """
    policy = W._compute_rebase_policy_for_forest(
        _md_file_forest_real_headings()
    )
    assert isinstance(policy, RenderPolicy)
    assert policy.breadcrumb_emit_body_false == frozenset()
    assert policy.deprecated_emit_body_false == frozenset()


def test_producer_is_pure_does_not_mutate_input():
    """The producer never mutates the input forest or its node dicts."""
    import copy

    forest = _mixed_forest()
    before = copy.deepcopy(forest)
    W._compute_rebase_policy_for_forest(forest)
    assert forest == before


# ===========================================================================
# BELT-AND-SUSPENDERS — no policy at all == pure inline (every legacy caller)
# ===========================================================================
# ===========================================================================
# HAZARD-B COVERAGE (added per Dan's request during Step 4) — the REAL
# is_md_heading rebase path with an OFF-PATH SHALLOWER SIBLING.
#
# WHY THIS EXISTS. Hazard-b is: a per-file/heading rebase baseline is the
# SHALLOWEST md-depth in the file's subtree; if a focus prune later removes
# the off-path sibling that OWNS that shallowest depth, an inline recompute
# over the pruned tree would shift every heading level. Step 4 makes that
# impossible by fixing the baseline from the UNPRUNED document_forest. These
# UNFOCUSED locks pin the correct pre-fix baseline + render so Step 6 (which
# introduces focused rendering through the materializer) has a real
# is_md_heading fixture to assert against.
#
# The Step-1 golden `_md_file_forest` fixture does NOT cover this: its notes
# use `MD_PATH: doc.md#Intro` (path on the same line) which yields md-depth
# None, so those nodes render via the markdown-CONTENT branch, not the
# is_md_heading rebase branch. The fixtures below use the canonical bare
# `MD_PATH:` form so the rebase branch is genuinely exercised.
# ===========================================================================
def _hazard_b_file_forest():
    """A Markdown file whose SHALLOWEST heading is an OFF-PATH sibling.

      doc.md
        Deep      (### => H3)          <- on a focus path to its child
          Deeper  (##### => H5)
        Shallow   (## => H2)  <- OFF-PATH sibling; OWNS the file min-depth (2)

    The file's shallowest md-depth is H2 (Shallow), so the whole file rebases
    against base=2: Deep(3)->H2, Deeper(5)->H4, Shallow(2)->H1. If a focus on
    'Deeper' later pruned 'Shallow' away, an inline recompute would see the
    shallowest as H3 and shift Deep->H1/Deeper->H3 — THAT is hazard-b. Here
    (unfocused) we lock the correct baseline=2 and the rebased render.
    """
    return [
        _node("fr", "doc.md", "Root: E:\\tmp\\doc.md", [
            _node("deep", "Deep", "MD_PATH:\n### Deep", [
                _node("deeper", "Deeper", "MD_PATH:\n##### Deeper\n\nbody"),
            ]),
            _node("shallow", "Shallow", "MD_PATH:\n## Shallow"),
        ]),
    ]


def _hazard_b_bare_heading_root_forest():
    """Same hazard-b shape but rooted at a BARE md-heading (v3.3 #2), no file.

      Deep     (### => H3)            <- render root; base is None -> policy
        Deeper (##### => H5)
      Shallow  (## => H2)  <- OFF-PATH sibling of Deep; OWNS min-depth (2)

    Wait: a bare-heading ROOT is a single top-level node, so 'Shallow' must be
    a SIBLING TOP-LEVEL node for it to be off-path yet in the same forest.
    The composer's bare-heading branch consults the policy for EACH top-level
    heading's own index_path; the point locked here is that each such root's
    baseline is taken from the policy (computed unfocused) rather than
    recomputed, and the two roots rebase independently + correctly.
    """
    return [
        _node("deep", "Deep", "MD_PATH:\n### Deep", [
            _node("deeper", "Deeper", "MD_PATH:\n##### Deeper\n\nbody"),
        ]),
        _node("shallow", "Shallow", "MD_PATH:\n## Shallow"),
    ]


def test_hazard_b_file_baseline_is_shallowest_offpath_sibling():
    """The file rebase baseline is the OFF-PATH sibling's shallower depth (2)."""
    forest = _hazard_b_file_forest()
    policy, out = _assert_policy_parity(forest)
    # File node (0,) baseline = shallowest md-depth in its subtree = H2.
    assert policy.rebase_base_for((0,)) == 2
    # Rebased render: Deep(3)->H2, Deeper(5)->H4, Shallow(2)->H1.
    assert "## Deep" in out
    assert "#### Deeper" in out
    assert "# Shallow" in out
    # Guard against accidental over-match (## Deep must not be read as # Deep).
    assert "\n# Deep" not in out


def test_hazard_b_file_render_exact():
    """Full exact render lock for the hazard-b file fixture (unfocused)."""
    forest = _hazard_b_file_forest()
    out = _render(forest, policy=W._compute_rebase_policy_for_forest(forest))
    # NOTE: an is_md_heading node's note is a Cartographer MD_PATH block, so
    # the composer strips the metadata and emits ONLY the heading (the trailing
    # 'body' text inside the MD_PATH block is not emitted as a separate body).
    assert out == (
        "# doc.md\n\n**E:\\tmp\\doc.md**\n\nRoot: E:\\tmp\\doc.md\n\n"
        "## Deep\n\n#### Deeper\n\n"
        "# Shallow\n\n---"
    )


def test_hazard_b_bare_heading_roots_rebase_independently():
    """Two bare-heading roots each consult their own policy baseline (v3.3 #2)."""
    forest = _hazard_b_bare_heading_root_forest()
    policy, out = _assert_policy_parity(forest)
    # Each top-level heading root has its own baseline: Deep subtree min = H3,
    # Shallow (leaf) min = H2.
    assert policy.rebase_base_for((0,)) == 3
    assert policy.rebase_base_for((1,)) == 2
    # Deep(3)->H1, Deeper(5)->H3, Shallow(2)->H1 (its own root). The MD_PATH
    # metadata (incl. the trailing 'body' inside Deeper's block) is stripped,
    # so only the rebased headings render.
    assert out == "# Deep\n\n### Deeper\n\n# Shallow"


def test_hazard_b_baseline_immune_to_offpath_sibling_removal():
    """THE hazard-b invariant, made concrete WITHOUT focus machinery.

    The Step-4 baseline is computed from the UNPRUNED forest, so it must NOT
    depend on whether the off-path shallower sibling is present. We assert
    that the file-node baseline computed over the FULL forest (2, owned by
    'Shallow') is DIFFERENT from what an inline recompute over a
    sibling-removed tree would give (3) — proving the precomputed policy
    carries the pre-prune baseline a focused render (Step 6) will rely on.
    """
    full = _hazard_b_file_forest()
    policy_full = W._compute_rebase_policy_for_forest(full)
    assert policy_full.rebase_base_for((0,)) == 2  # owned by off-path 'Shallow'

    # A hypothetical pruned tree (Shallow removed) would recompute to 3.
    pruned = _hazard_b_file_forest()
    pruned[0]["children"] = pruned[0]["children"][:1]  # drop 'Shallow'
    policy_pruned = W._compute_rebase_policy_for_forest(pruned)
    assert policy_pruned.rebase_base_for((0,)) == 3

    # The POINT: they differ, so computing the baseline from the UNPRUNED
    # document_forest (what Step 4 does) is what keeps a focused render
    # stable. This lock will be the anchor for Step 6's focused hazard-b case.
    assert policy_full.rebase_base_for((0,)) != policy_pruned.rebase_base_for((0,))


def test_no_policy_arg_is_pure_inline_for_every_fixture():
    """render_policy=None (the default all legacy callers use) == inline.

    A trivial identity, but it locks that the new parameter's default path is
    the untouched pre-Step-4 behavior.
    """
    for forest in (
        _plain_forest(),
        _md_file_forest_real_headings(),
        _bare_md_heading_root_forest(),
        _source_regime_forest(),
        _mixed_forest(),
    ):
        a = W._build_markdown_preview_from_tree(forest, suppress_metadata=True)
        b = W._build_markdown_preview_from_tree(
            forest, suppress_metadata=True, render_policy=None
        )
        assert a == b
