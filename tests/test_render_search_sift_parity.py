"""[ap:2EG7TD] unit 5c — render_search and sift are ONE search, two scopes.

Locks the scope-identical behaviour the contract and the Wave 5 rulings
([ap:S3KS1M] D1/D2/D7/D8) demand:

* the SAME grammar, match_scope default and result rows on both verbs
  (render_search over every node under a root; sift inside a lens) --
  the headings tier renders byte-identical Markdown and the context tier
  reports byte-identical grep rows through the ONE shared tail
  (_sift_grep_documents + _render_sift_grep_result, Ledger CF-30);
* the D2 defaults: sift defaults tier 'context' (lines), render_search
  defaults tier 'headings' (nodes); both verbs accept both tiers;
* D8: render_search's headings_only default is True at the registry
  boundary only (the impl's own default stays False);
* D7: ONE empty-selection text -- the same shared constant the (empty)
  stub ([ap:G2UALX]) uses -- on both verbs' headings tier, and the ONE
  formatter's zero-match text on both verbs' context tier;
* the one-search sentence on both verbs' describes, both family summaries
  and node.search's cross-reference.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation


CANONICAL_SENTENCE = (
    "render_search searches every node under a root; sift is the same "
    "search inside a slice group or lens"
)


def _node(node_id, name, parent_id=None, priority=0, note="", *, completed=False):
    return {
        "id": node_id,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
        "completed": completed,
        "completedAt": 1 if completed else None,
        "createdAt": priority + 1,
        "modifiedAt": priority + 1,
    }


def _fixture_cache():
    return {
        "R": _node("R", "Root", None, 0),
        "A": _node("A", "Architecture #arch", "R", 0),
        "A1": _node("A1", "Resolver implementation", "A", 0),
        "B": _node("B", "Deployment", "R", 1),
        "B1": _node("B1", "Deploy normalization", "B", 0, note="normalize the deploy pipeline\nthen ship"),
        "X": _node("X", "Outside-only needle", "R", 2),
    }


@pytest.fixture()
def cache(monkeypatch):
    """A boundary-capable client double whose cache holds the fixture nodes.

    Lightning Rod verbs are cache_backed, so the family front wraps dispatch
    in a cache_operation_boundary (the S03-B client double), and
    _lightning_rod_from_cache reads the same nodes through
    _get_nodes_export_cache_nodes -- one welded fixture, no WebSocket
    (render_search defaults descend_policy='all'). The Stage-2 tail's pure
    materializer classmethod is attached from the real client class (it is
    stateless shadow-dict construction).
    """
    from coherence_harness import make_boundary_capable
    from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus

    nodes = _fixture_cache()

    class _Client(SimpleNamespace):
        def _get_nodes_export_cache_nodes(self):
            return list(nodes.values())

    # The Stage-2 tail drives several PURE classmethods on the real client
    # chain (materializer, composer, root-regime seed, focus helpers); bind
    # them THROUGH the real class (staticmethod wrappers) so their internal
    # cls-helpers resolve on the real inheritance chain while the double
    # supplies only the cache.
    for _name in (
        "materialize_from_document_forest",
        "_build_markdown_preview_from_tree",
        "_annotate_preview_ids_and_build_tree",
        "_build_abs_path_map_for_preview",
        "_build_modal_rows",
        "_node_uuid_at_index_path",
        "_resolve_focus_index_path",
        "_root_in_non_md_file_from_cache",
    ):
        setattr(
            _Client,
            _name,
            staticmethod(getattr(WorkFlowyClientNexus, _name)),
        )

    monkeypatch.setattr(
        server,
        "get_client",
        lambda: make_boundary_capable(_Client(), nodes=list(nodes.values())),
    )
    return nodes


def _lens_context(cache):
    """The minimal LensEvalContext for a {subtree} lens over the fixture."""

    def resolve_root(handle):
        if handle not in cache:
            raise KeyError(handle)
        return handle

    from workflowy_mcp.lens_engine import LensEvalContext

    return LensEvalContext(
        cache_by_id=cache,
        resolve_group=lambda handle: (_ for _ in ()).throw(KeyError(handle)),
        resolve_root=resolve_root,
        global_never_stems=("wfy-suppress",),
        exclude_completed=True,
        anchor_boundary_predicate=lambda node: False,
    )


def _install_lens_snapshot(monkeypatch, cache):
    async def build_snapshot(target_tab=None, exclude_completed=None):
        return SimpleNamespace(
            context=_lens_context(cache),
            cache_by_id=cache,
        )

    monkeypatch.setattr(server, "_build_lens_stage0_snapshot", build_snapshot)


_SUBTREE_LENS = {"kind": "subtree", "root_handle": "R", "descend": "all"}


@pytest.mark.parametrize("tier", ["context", "headings"])
@pytest.mark.parametrize("query", [
    "invoice OR receipt", "report -draft", 'a"b', r"C:\tmp\file",
    "inv*2026", "token?", "[ap:K7M2QF]", "(unfinished", "alpha  beta",
])
def test_literal_search_is_fixed_string_on_both_scopes_and_tiers(cache, monkeypatch, tier, query):
    """Do not silently accept literal=true while still tokenizing smart syntax.

    Escaped-regex lowering must reach BOTH the node predicate and line-span
    matcher; the same normalized selection can also round-trip via render_spec.
    """
    cache["A1"].update(name="Literal match", note=f"prefix {query} suffix {query.upper()}")
    cache["B1"].update(name="Decoy", note="invoice\nreceipt\nreport\ndraft\ninvXYZ2026\ntokenZ\nalpha beta")
    _install_lens_snapshot(monkeypatch, cache)
    params = {"query": query, "literal": True, "tier": tier, "context_lines": 0} if tier == "context" else {"query": query, "literal": True, "tier": tier}
    rs = asyncio.run(dispatch_operation(server.build_lightning_rod_registry(), "render_search", {"root_handle": "R", **params}))
    sf = asyncio.run(dispatch_operation(server.build_slice_group_registry(), "sift", {"lens": dict(_SUBTREE_LENS), **params}))
    assert isinstance(rs, str) and isinstance(sf, str)
    assert _without_source_line(rs) == _without_source_line(sf)
    assert "Literal match" in rs and "Decoy" not in rs
    if tier == "context":
        assert f"1 | prefix {query} suffix {query.upper()}" in rs
        assert "returned_occurrences=2" in rs


# --------------------------------------------------------------------------
# The two scopes agree: same grammar, same rows.
# --------------------------------------------------------------------------


def test_headings_tier_is_byte_identical_between_the_verbs(cache, monkeypatch):
    """The same query over the same scope renders byte-identical Markdown.

    sift over a {subtree} lens admits exactly what render_search's predicate
    admits over the same root; both render through the ONE Stage-2 tail and
    the ONE composer, so the strings must be equal -- not merely similar.
    """
    _install_lens_snapshot(monkeypatch, cache)
    lr = server.build_lightning_rod_registry()
    sg = server.build_slice_group_registry()

    rendered = asyncio.run(
        dispatch_operation(
            lr, "render_search", {"root_handle": "R", "query": "deploy"}
        )
    )
    # D2: the DEFAULTS differ (sift defaults 'context'), so the headings-tier
    # parity comparison asks sift for tier 'headings' explicitly. Per tier the
    # outputs must be byte-identical.
    sifted = asyncio.run(
        dispatch_operation(
            sg,
            "sift",
            {"lens": dict(_SUBTREE_LENS), "query": "deploy", "tier": "headings"},
        )
    )
    assert isinstance(rendered, str) and isinstance(sifted, str)
    assert rendered == sifted
    # The matching nodes are the only rows (no connective padding beyond the
    # shared spine both verbs build the same way).
    assert "Deploy normalization" in rendered
    assert "Outside-only needle" not in rendered


def test_context_tier_rows_are_identical_only_the_source_line_differs(
    cache, monkeypatch
):
    """tier 'context': the same documents, matcher and formatter -- the two
    reports differ ONLY in the Source line naming the calling verb."""
    _install_lens_snapshot(monkeypatch, cache)
    lr = server.build_lightning_rod_registry()
    sg = server.build_slice_group_registry()
    params = {"query": "deploy", "context_lines": 0, "max_matches": 10}

    rs = asyncio.run(
        dispatch_operation(
            lr,
            "render_search",
            {"root_handle": "R", "tier": "context", **params},
        )
    )
    sf = asyncio.run(
        dispatch_operation(
            sg, "sift", {"lens": dict(_SUBTREE_LENS), "tier": "context", **params}
        )
    )
    assert rs.startswith("Source: lightning_rod.render_search grep")
    assert sf.startswith("Source: slice_group.sift grep")
    assert _without_source_line(rs) == _without_source_line(sf)
    assert "Matched 1 lines in B1" in rs
    # context_lines=0 -> a bare hit list: the hit row carries no marker.
    assert "1 | normalize the deploy pipeline" in rs


def _without_source_line(report: str) -> str:
    return "".join(
        line + "\n"
        for line in report.splitlines()
        if not line.startswith("Source: ")
    )


def test_render_search_context_tier_reuses_the_shared_tail(cache, monkeypatch):
    """CF-30: no third producer. The context tier routes through the ONE
    _render_sift_grep_result / _sift_grep_documents pair -- asserted by
    stubbing the pair and watching render_search call it."""
    calls: list[dict] = []

    def fake_grep_result(**kwargs):
        calls.append(kwargs)
        return {"success": True, "tier": "context", "preview_text": "REPORT"}

    monkeypatch.setattr(server, "_render_sift_grep_result", fake_grep_result)
    lr = server.build_lightning_rod_registry()
    result = asyncio.run(
        dispatch_operation(
            lr,
            "render_search",
            {"root_handle": "R", "query": "deploy", "tier": "context"},
        )
    )
    assert result == "REPORT"  # shaped raw through preview_text
    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["tree"]["id"] == "R"
    assert callable(kwargs["predicate"])
    assert kwargs["query"] == "deploy"
    assert kwargs["query_mode"] == "smart"
    assert kwargs["match_scope"] == "name_and_note"
    assert kwargs["surface"].startswith("lightning_rod.render_search grep")
    assert kwargs["source_meta"]["_source"] == "lightning_rod_search"
    assert kwargs["grep_params"]["max_matches"] == 50  # the ONE owner's default


# --------------------------------------------------------------------------
# D2 defaults and the D8 flip -- registry boundary only.
# --------------------------------------------------------------------------


def test_render_search_defaults_tier_headings_and_sift_context(cache, monkeypatch):
    """D2: render_search's default tier is 'headings', sift's is 'context'."""
    _install_lens_snapshot(monkeypatch, cache)
    lr = server.build_lightning_rod_registry()
    sg = server.build_slice_group_registry()

    rendered = asyncio.run(
        dispatch_operation(lr, "render_search", {"root_handle": "R", "query": "deploy"})
    )
    assert isinstance(rendered, str)
    assert "Source: " not in rendered  # a tree render, not the grep report

    report = asyncio.run(
        dispatch_operation(sg, "sift", {"lens": dict(_SUBTREE_LENS), "query": "deploy"})
    )
    assert report.startswith("Source: slice_group.sift grep")


def test_render_search_headings_only_defaults_true_at_the_registry(monkeypatch):
    """D8: the registry default flips for render_search ONLY; the impl's own
    default stays False (glimpse.run / render_tags / render_symbols are
    byte-for-byte unchanged)."""
    calls: list[dict] = []

    async def impl(**kwargs):
        calls.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(server, "_lightning_rod_legacy_impl", impl)
    lr = server.build_lightning_rod_registry()

    asyncio.run(
        dispatch_operation(lr, "render_search", {"root_handle": "R", "query": "q"})
    )
    assert calls[-1]["headings_only"] is True  # the D8 default
    assert calls[-1].get("grep_params") is None  # headings tier: no grep tail

    asyncio.run(
        dispatch_operation(
            lr,
            "render_search",
            {"root_handle": "R", "query": "q", "headings_only": False},
        )
    )
    assert calls[-1]["headings_only"] is False

    asyncio.run(
        dispatch_operation(lr, "render_tags", {"root_handle": "R", "tags": ["t"]})
    )
    assert calls[-1]["headings_only"] is False  # other modes unchanged


def test_render_search_both_tiers_accepted_and_grep_values_owned(cache):
    """Both tiers work on render_search; the grep values come from the ONE
    owner (defaults and bounds), and the boundary refusals mirror sift's."""
    lr = server.build_lightning_rod_registry()
    context = {"root_handle": "R", "query": "deploy", "tier": "context"}

    report = asyncio.run(dispatch_operation(lr, "render_search", dict(context)))
    assert report.startswith("Source: lightning_rod.render_search grep")
    assert "Pagination: offset=0" in report

    push = asyncio.run(
        dispatch_operation(lr, "render_search", {**context, "push_to_modal": True})
    )
    assert "tier='context'" in str(push)

    # [ap:O3MDOY] half 2: sort_by/recent are now ACTIVE node-level controls
    # on the grep tier (the ONE match-control set) -- accepted, not inert.
    controls = asyncio.run(
        dispatch_operation(lr, "render_search", {**context, "sort_by": "created"})
    )
    assert isinstance(controls, str)
    assert "Pagination: offset=0" in controls

    controls_recent = asyncio.run(
        dispatch_operation(lr, "render_search", {**context, "recent": "last 5"})
    )
    assert isinstance(controls_recent, str)
    assert "returned_nodes=" in controls_recent

    # The still-headings-only knobs stay inert-refused on this tier.
    inert = asyncio.run(
        dispatch_operation(lr, "render_search", {**context, "full_uuids": True})
    )
    assert "full_uuids" in str(inert)

    bad_bound = asyncio.run(
        dispatch_operation(lr, "render_search", {**context, "context_lines": 11})
    )
    assert "context_lines" in str(bad_bound)


def test_render_search_context_tier_zero_line_matches_reports_zeros(cache):
    """The context tier over a scope whose only node matches no line reports
    the shared footer zeros and the D6 marker -- identical machinery to
    sift's context tier (the formatter's no-documents zero-match text cannot
    occur on render_search: the projection root is always a walked node)."""
    lr = server.build_lightning_rod_registry()
    report = asyncio.run(
        dispatch_operation(
            lr,
            "render_search",
            {"root_handle": "X", "query": "deploy", "tier": "context"},
        )
    )
    assert report.startswith("Source: lightning_rod.render_search grep")
    assert "Matched 0 lines in X" in report
    assert "returned_matches=0" in report
    assert "has_more=false" in report


# --------------------------------------------------------------------------
# D7: one empty-selection text.
# --------------------------------------------------------------------------


def test_render_search_zero_hit_returns_the_shared_empty_constant(cache):
    """A selection that admits nothing returns the literal '(empty)' -- the
    same shared constant sift's headings tier returns -- never a bare root
    skeleton that would read as a match."""
    lr = server.build_lightning_rod_registry()

    markdown = asyncio.run(
        dispatch_operation(lr, "render_search", {"root_handle": "R", "query": "zzz-no-hit"})
    )
    assert markdown == server.EMPTY_RESULT_STUB

    structured = asyncio.run(
        dispatch_operation(
            lr,
            "render_search",
            {"root_handle": "R", "query": "zzz-no-hit", "as_markdown": False},
        )
    )
    assert structured["success"] is True
    assert structured["empty_selection"] is True
    assert structured["preview_tree"] == []
    assert structured["node_count"] == 0


def test_sift_and_render_search_headings_empty_use_the_same_constant(
    cache, monkeypatch
):
    """D7, both verbs side by side: the same literal, from the ONE shared
    constant (_render_lens_empty_result serves both)."""
    _install_lens_snapshot(monkeypatch, cache)
    lr = server.build_lightning_rod_registry()
    sg = server.build_slice_group_registry()

    rs = asyncio.run(
        dispatch_operation(lr, "render_search", {"root_handle": "R", "query": "zzz-no-hit"})
    )
    sf = asyncio.run(
        dispatch_operation(
            sg,
            "sift",
            {"lens": {"kind": "tags", "root_handle": "R", "tags": ["missing"]}, "query": "zzz-no-hit", "tier": "headings"},
        )
    )
    assert rs == sf == server.EMPTY_RESULT_STUB


# --------------------------------------------------------------------------
# The one-search sentence, everywhere the contract names.
# --------------------------------------------------------------------------


def test_the_one_search_sentence_on_both_describes_and_family_summaries():
    lr = server.build_lightning_rod_registry()
    sg = server.build_slice_group_registry()
    node = server.build_node_registry()

    assert CANONICAL_SENTENCE in lr.resolve("render_search").summary
    assert CANONICAL_SENTENCE in sg.resolve("sift").summary
    assert CANONICAL_SENTENCE in lr.family_summary
    assert CANONICAL_SENTENCE in sg.family_summary
    # node.search keeps its "not a smart/Boolean search" teaching and gains
    # the cross-reference to the smart-grammar searches.
    assert "Not a rendered outline or smart/Boolean search" in node.resolve("search").summary
    assert CANONICAL_SENTENCE in node.resolve("search").summary


def test_both_verbs_advertise_both_tiers_with_their_own_defaults():
    lr = server.build_lightning_rod_registry()
    sg = server.build_slice_group_registry()

    rs_props = lr.resolve("render_search").params_schema["properties"]
    sf_props = sg.resolve("sift").params_schema["properties"]
    # The SAME tier vocabulary on both (D1), with the D2 defaults spelled out.
    # (OperationSpec deep-freeze turns schema lists into tuples.)
    assert list(rs_props["tier"]["enum"]) == ["headings", "context"]
    assert list(sf_props["tier"]["enum"]) == ["headings", "context"]
    assert "default 'headings'" in rs_props["tier"]["description"]
    assert "default 'context'" in sf_props["tier"]["description"]
    # The grep values come from the ONE schema fragment: identical
    # values/defaults/bounds on both verbs (each verb's description may name
    # its own scope, so compare the value shape only).
    for name in ("context_lines", "context_width", "match_width", "max_matches", "offset"):
        assert name in rs_props and name in sf_props
        assert {
            k: v for k, v in rs_props[name].items() if k != "description"
        } == {k: v for k, v in sf_props[name].items() if k != "description"}


def test_eight_hex_efficiency_advertised_in_the_summaries():
    """54e8356f(iv) rides with 5c: the summaries advertise the cheap 8-hex
    first-segment handle form."""
    lr = server.build_lightning_rod_registry()
    sg = server.build_slice_group_registry()
    assert "8-hex" in lr.resolve("render_symbols").summary
    assert "8-hex" in sg.resolve("sift").summary


# --------------------------------------------------------------------------
# [ap:O3MDOY] half 2 (unit G11-5e): the headings-tier node cap (max_nodes),
# end to end through the REAL Stage-2 tail -- it composes AFTER the sort and
# the recency selection, counts only MATCHING nodes (the caller's own
# predicate), and opens the Markdown with the one node-limit line. Every
# assertion fails against the pre-unit code: the parameter did not exist and
# the schema refused it.
# --------------------------------------------------------------------------

def test_max_nodes_caps_the_headings_render_on_both_search_verbs(cache, monkeypatch):
    _install_lens_snapshot(monkeypatch, cache)
    lr = server.build_lightning_rod_registry()
    sg = server.build_slice_group_registry()

    rendered = asyncio.run(
        dispatch_operation(
            lr, "render_search", {"root_handle": "R", "query": "deploy", "max_nodes": 1},
        )
    )
    # Matching nodes in display order: B, then B1. The cap keeps B and drops
    # B1's whole subtree; the line counts what it hid.
    assert rendered.startswith(
        "> Node limit (max_nodes=1): showing the first 1 of 2 matching nodes"
    )
    assert "Deployment" in rendered
    assert "normalize the deploy pipeline" not in rendered

    # The structured variant carries the raw echo and its header sentence.
    structured = asyncio.run(
        dispatch_operation(
            lr, "render_search",
            {"root_handle": "R", "query": "deploy", "max_nodes": 1,
             "as_markdown": False, "headings_only": True},
        )
    )
    assert structured["success"] is True
    assert structured["node_limit_applied"] == {
        "max_nodes": 1, "matched": 2, "shown": 1, "hidden": 1,
    }
    assert structured["node_limit_header"].startswith("Node limit (max_nodes=1)")
    # The stats describe what rendered: the connective root R and the
    # one capped surviving node B.
    assert structured["node_count"] == 2

    # sift's headings tier takes the same control over the same scope.
    sifted = asyncio.run(
        dispatch_operation(
            sg, "sift",
            {"lens": dict(_SUBTREE_LENS), "query": "deploy",
             "tier": "headings", "max_nodes": 1},
        )
    )
    assert isinstance(sifted, str)
    assert sifted.startswith(
        "> Node limit (max_nodes=1): showing the first 1 of 2 matching nodes"
    )

    # push_to_modal refuses the cap on both verbs (each at its headings
    # tier -- sift DEFAULTS to 'context', whose own push refusal fires
    # first): a pushed render shows the full selection in the tab.
    for registry, verb, extra in (
        (lr, "render_search", {"root_handle": "R", "query": "deploy"}),
        (sg, "sift", {"lens": dict(_SUBTREE_LENS), "query": "deploy", "tier": "headings"}),
    ):
        refused = asyncio.run(
            dispatch_operation(
                registry, verb,
                {**extra, "max_nodes": 1, "push_to_modal": True},
            )
        )
        assert refused["success"] is False
        assert "max_nodes" in refused["error"]["message"]
