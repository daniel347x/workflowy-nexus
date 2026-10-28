"""[ap:I506WY] (unit 5g, G12) — lens-scaffold ancestor-note suppression.

Dan's ask: on a merge-union (or any lens) render, the ANCESTOR PATH above the
input roots (the LCA down to each input root) renders HEADINGS-ONLY — never
the ancestor's note — EVEN IN DOCUMENT ORDER. The live case: a modal push of a
distant-tree union dragged in the ~100 KB Manager Sessions description and the
other ancestor notes on the path. Member nodes (the input roots and their
subtrees) are unaffected. ``ancestor_notes: true`` is the call-level opt-out
on sift / merge.render / render (and any render_lens projection).

Mechanism locks (mirroring the U6 test file's discipline):

  1. ONE computation — ``lens_engine.lens_scaffold_ancestor_ids`` is the pure
     set computation (final tree + per-atom resolved roots); the projection
     returns it; the lens render tails forward it producer-side with the
     ``ancestor_notes`` opt-out folded THERE (exactly like the chronological
     boolean's fold).
  2. ONE cause — the materializer owns the occurrence/index-path keying
     (``lens_scaffold_ancestor_emit_body_false``), consumed by BOTH renderers
     through ``emit_body_false_for``'s render_key branch. No second
     suppression path, no tree mutation, membership/ordering untouched.
  3. MEMBER REGION — a node below an input root is never scaffold, even when
     it sits above a nested input root; the degenerate single-root lens (LCA
     == the input root) suppresses nothing and stays byte-identical.
  4. NON-LEAF OCCURRENCES ONLY — a scaffold id at an occurrence with no dict
     children is a leaf there and its note prints (the cause withholds the
     ancestor PATH's notes, not "these ids never render notes").

Patterns after ``tests/test_chronological_ancestor_notes.py`` and
``tests/test_lightning_rod_render_lens.py``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402
from workflowy_mcp.lens_engine import LensEvalContext  # noqa: E402
from workflowy_mcp.mcp_api_registry import dispatch_operation  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures — a DISTANT-TREE union (the live case): two input roots under a
# big-noted LCA that belongs to no atom.
# R (BIG-ROOT-NOTE)          <- LCA, pure scaffold
# ├── A (ARCH-ROOT-NOTE)     <- input root 1
# │   └── A1 (RESOLVER-BODY) <- member leaf
# └── B (DEPLOY-ROOT-NOTE)   <- input root 2
#     └── B1 (DEPLOY-BODY)   <- member leaf
# ---------------------------------------------------------------------------

def _node(node_id, name, parent_id=None, priority=0, note=""):
    return {
        "id": node_id,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
        "completed": False,
        "completedAt": None,
        "createdAt": priority + 1,
        "modifiedAt": priority + 1,
    }


def _fixture_state():
    cache = {
        "R": _node("R", "Shared root", None, 0, "BIG-ROOT-NOTE\n\nThe whole framing document."),
        "A": _node("A", "Architecture", "R", 1, "ARCH-ROOT-NOTE"),
        "A1": _node("A1", "Resolver implementation", "A", 2, "RESOLVER-BODY"),
        "A2": _node("A2", "Fresh architecture child", "A", 5, "A2-BODY"),
        "B": _node("B", "Deployment", "R", 3, "DEPLOY-ROOT-NOTE"),
        "B1": _node("B1", "Deploy normalization", "B", 4, "DEPLOY-BODY"),
    }
    groups: dict[str, dict] = {}
    return cache, groups


def _context(cache, groups):
    def resolve_group(handle):
        if handle not in groups:
            raise KeyError(handle)
        return groups[handle]

    def resolve_root(handle):
        if handle not in cache:
            raise KeyError(handle)
        return handle

    return LensEvalContext(
        cache_by_id=cache,
        resolve_group=resolve_group,
        resolve_root=resolve_root,
        global_never_stems=("wfy-suppress",),
        exclude_completed=True,
        anchor_boundary_predicate=lambda node: False,
    )


def _install_snapshot(monkeypatch, cache, groups):
    """Patch the Stage-0 snapshot AND the global client: the real Stage-2
    tail must run (pure render methods over the injected cache)."""

    async def build_snapshot(target_tab=None, exclude_completed=None):
        return SimpleNamespace(
            context=_context(cache, groups),
            cache_by_id=cache,
        )

    from coherence_harness import make_boundary_capable

    client = object.__new__(server.WorkFlowyClient)
    client._get_nodes_export_cache_nodes = lambda: list(cache.values())
    monkeypatch.setattr(server, "_build_lens_stage0_snapshot", build_snapshot)
    monkeypatch.setattr(
        server,
        "get_client",
        lambda: make_boundary_capable(client, nodes=list(cache.values())),
    )


def _union_subtree_lens():
    return {
        "kind": "merge",
        "operation": "union",
        "inputs": [
            {"kind": "subtree", "root_handle": "A"},
            {"kind": "subtree", "root_handle": "B"},
        ],
    }


def _lens_spec(lens=None, **kwargs):
    return RenderProjectionSpec.normalize_from_glimpse_args(
        lightning={
            "mode": "render_lens",
            "lens": lens or _union_subtree_lens(),
            "query": None,
            "query_mode": "smart",
            "match_scope": "name",
        },
        **kwargs,
    )


def _render_lens_markdown(monkeypatch, cache, groups, spec):
    _install_snapshot(monkeypatch, cache, groups)
    result = asyncio.run(
        server._lightning_rod_render_lens(spec, output_file=None, target_tab=None)
    )
    assert result["success"] is True, result
    assert isinstance(result.get("preview_markdown"), str), result
    return result


# ---------------------------------------------------------------------------
# 1 · the pure computation (lens_engine.lens_scaffold_ancestor_ids)
# ---------------------------------------------------------------------------

def test_scaffold_pure_function_distant_lca():
    from workflowy_mcp.lens_engine import lens_scaffold_ancestor_ids

    cache, _groups = _fixture_state()
    tree = {
        "id": "R",
        "children": [
            {"id": "A", "children": [{"id": "A1", "children": []}]},
            {"id": "B", "children": [{"id": "B1", "children": []}]},
        ],
    }
    assert lens_scaffold_ancestor_ids(tree, ["A", "B"]) == frozenset({"R"})


def test_scaffold_pure_function_degenerate_single_root_is_empty():
    from workflowy_mcp.lens_engine import lens_scaffold_ancestor_ids

    tree = {"id": "A", "children": [{"id": "A1", "children": []}]}
    assert lens_scaffold_ancestor_ids(tree, ["A"]) == frozenset()


def test_scaffold_pure_function_nested_input_roots_stay_member_region():
    from workflowy_mcp.lens_engine import lens_scaffold_ancestor_ids

    # R is BOTH an input root and the ancestor of the nested input root B:
    # everything at-or-below R is member region (M between R and B included).
    tree = {
        "id": "R",
        "children": [
            {
                "id": "M",
                "children": [
                    {"id": "B", "children": [{"id": "B1", "children": []}]},
                    {"id": "L2", "children": []},
                ],
            }
        ],
    }
    assert lens_scaffold_ancestor_ids(tree, ["R", "B"]) == frozenset()


def test_scaffold_pure_function_absent_root_and_bad_shapes():
    from workflowy_mcp.lens_engine import lens_scaffold_ancestor_ids

    tree = {"id": "R", "children": [{"id": "A", "children": []}]}
    # R is above the input root A (a GHOST root id is harmless): scaffold.
    assert lens_scaffold_ancestor_ids(tree, ["A", "GHOST"]) == frozenset({"R"})
    # With NO declared input roots nothing may be suppressed.
    assert lens_scaffold_ancestor_ids(tree, []) == frozenset()
    assert lens_scaffold_ancestor_ids(None, ["A"]) == frozenset()


def test_projection_returns_scaffold_ids(monkeypatch):
    cache, groups = _fixture_state()
    _install_snapshot(monkeypatch, cache, groups)

    async def run():
        snapshot = await server._build_lens_stage0_snapshot(None)
        return await server._project_render_lens_from_cache(
            _lens_spec().lightning, snapshot=snapshot
        )

    projected = asyncio.run(run())
    assert projected["lens_scaffold_ids"] == frozenset({"R"})


# ---------------------------------------------------------------------------
# 2 · the default suppression (document order), end to end
# ---------------------------------------------------------------------------

def test_lens_union_document_order_suppresses_scaffold_note_by_default(monkeypatch):
    cache, groups = _fixture_state()
    result = _render_lens_markdown(
        monkeypatch, cache, groups, _lens_spec(headings_only=False)
    )
    md = result["preview_markdown"]
    # The scaffold node renders its HEADING, never its note.
    assert "Shared root" in md
    assert "BIG-ROOT-NOTE" not in md
    # Member nodes (input roots and their subtrees) are unaffected.
    assert "ARCH-ROOT-NOTE" in md
    assert "RESOLVER-BODY" in md
    assert "DEPLOY-ROOT-NOTE" in md
    assert "DEPLOY-BODY" in md


def test_lens_union_group_atoms_suppress_scaffold_note_by_default(monkeypatch):
    """The same rule over group atoms (the merge-union shape of the live case)."""

    cache, groups = _fixture_state()
    groups["Architecture"] = {
        "id": "grp-arch",
        "name": "Architecture",
        "root_uuid": "A",
        "prefixes": ["arch"],
    }
    groups["Deployment"] = {
        "id": "grp-deploy",
        "name": "Deployment",
        "root_uuid": "B",
        "prefixes": ["deploy"],
    }
    cache["A"]["name"] = "Architecture #arch--expand"
    cache["B"]["name"] = "Deployment #deploy--expand"
    lens = {
        "kind": "merge",
        "operation": "union",
        "inputs": [
            {"kind": "group", "group_handle": "Architecture"},
            {"kind": "group", "group_handle": "Deployment"},
        ],
    }
    result = _render_lens_markdown(
        monkeypatch, cache, groups, _lens_spec(lens=lens, headings_only=False)
    )
    md = result["preview_markdown"]
    assert "BIG-ROOT-NOTE" not in md
    assert "ARCH-ROOT-NOTE" in md
    assert "DEPLOY-ROOT-NOTE" in md
    assert "DEPLOY-BODY" in md


def test_full_uuid_mode_suppresses_scaffold_note_too(monkeypatch):
    cache, groups = _fixture_state()
    _install_snapshot(monkeypatch, cache, groups)
    spec = _lens_spec(headings_only=False, as_markdown=False)
    result = asyncio.run(
        server._lightning_rod_render_lens(spec, output_file=None, target_tab=None)
    )
    assert result["success"] is True, result
    assert result.get("preview_markdown") is None
    rendered = repr(result.get("preview_tree"))
    assert "BIG-ROOT-NOTE" not in rendered
    assert "RESOLVER-BODY" in rendered


def test_degenerate_single_root_lens_keeps_its_note(monkeypatch):
    """The design rationale holds: a single-root render genuinely frames its
    children, so the LCA IS the input root and nothing is suppressed."""

    cache, groups = _fixture_state()
    spec = _lens_spec(
        lens={"kind": "subtree", "root_handle": "A"}, headings_only=False
    )
    result = _render_lens_markdown(monkeypatch, cache, groups, spec)
    md = result["preview_markdown"]
    assert "ARCH-ROOT-NOTE" in md
    assert "RESOLVER-BODY" in md
    assert "A2-BODY" in md


def test_sorted_lens_render_still_suppresses_scaffold_occurrences(monkeypatch):
    """Occurrence keying: under a timestamp sort the same scaffold uuid
    re-emits per branch and every occurrence stays heading-only."""

    cache, groups = _fixture_state()
    spec = _lens_spec(headings_only=False, sort_by="created", sort_direction="desc")
    result = _render_lens_markdown(monkeypatch, cache, groups, spec)
    assert "BIG-ROOT-NOTE" not in result["preview_markdown"]
    assert "RESOLVER-BODY" in result["preview_markdown"]


def test_chronological_lens_render_suppresses_all_ancestor_notes(monkeypatch):
    """The U6 cause composes: a chronological lens render withholds every
    non-leaf note (the scaffold cause is a subset there, not a conflict)."""

    cache, groups = _fixture_state()
    spec = _lens_spec(headings_only=False, sort_by="created")
    result = _render_lens_markdown(monkeypatch, cache, groups, spec)
    md = result["preview_markdown"]
    assert "BIG-ROOT-NOTE" not in md
    assert "ARCH-ROOT-NOTE" not in md  # chronological: input roots too
    assert "RESOLVER-BODY" in md


def test_non_lens_render_without_scaffold_ids_is_untouched(monkeypatch):
    """The tail's default (no lens_scaffold_ids) stays byte-identical: a raw
    root dump keeps its ancestor notes in document order."""

    from coherence_harness import make_boundary_capable

    cache, _groups = _fixture_state()
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="R", headings_only=False
    )
    tree = {
        "id": "R",
        "name": "Shared root",
        "note": "BIG-ROOT-NOTE",
        "children": [
            {"id": "A", "name": "Architecture", "note": "ARCH-ROOT-NOTE", "children": []}
        ],
    }
    client = object.__new__(server.WorkFlowyClient)
    client._get_nodes_export_cache_nodes = lambda: list(cache.values())
    monkeypatch.setattr(
        server,
        "get_client",
        lambda: make_boundary_capable(client, nodes=list(cache.values())),
    )
    result = server.render_stage2_filter_sort(
        spec=spec,
        final_subtree=tree,
        cache_by_id=cache,
        output_file=None,
        source_meta={"_source": "test"},
    )
    assert result["success"] is True, result
    assert "BIG-ROOT-NOTE" in result["preview_markdown"]


# ---------------------------------------------------------------------------
# 3 · the ancestor_notes override on the three MCP surfaces
# ---------------------------------------------------------------------------

def _registry():
    return server.build_slice_group_registry()


def test_render_override_restores_scaffold_note(monkeypatch):
    cache, groups = _fixture_state()
    _install_snapshot(monkeypatch, cache, groups)
    registry = _registry()
    result = asyncio.run(
        dispatch_operation(
            registry,
            "render",
            {"lens": _union_subtree_lens(), "ancestor_notes": True},
        )
    )
    assert isinstance(result, str), result
    assert "BIG-ROOT-NOTE" in result
    assert "RESOLVER-BODY" in result


def test_render_default_suppresses_scaffold_note(monkeypatch):
    cache, groups = _fixture_state()
    _install_snapshot(monkeypatch, cache, groups)
    registry = _registry()
    result = asyncio.run(
        dispatch_operation(registry, "render", {"lens": _union_subtree_lens()})
    )
    assert isinstance(result, str), result
    assert "BIG-ROOT-NOTE" not in result


def test_sift_headings_tier_default_and_override(monkeypatch):
    cache, groups = _fixture_state()
    _install_snapshot(monkeypatch, cache, groups)
    registry = _registry()
    params = {
        "lens": _union_subtree_lens(),
        "query": "resolver",
        "tier": "headings",
        # notes must render for the assertion (sift's headings tier defaults
        # headings_only true).
        "headings_only": False,
    }
    default_md = asyncio.run(dispatch_operation(registry, "sift", dict(params)))
    assert isinstance(default_md, str), default_md
    assert "BIG-ROOT-NOTE" not in default_md
    assert "ARCH-ROOT-NOTE" in default_md
    override_md = asyncio.run(
        dispatch_operation(
            registry, "sift", {**params, "ancestor_notes": True}
        )
    )
    assert isinstance(override_md, str), override_md
    assert "BIG-ROOT-NOTE" in override_md


def test_sift_context_tier_still_refuses_ancestor_notes(monkeypatch):
    """The grep tier has no tree render: the flag stays inert (loud)."""

    cache, groups = _fixture_state()
    _install_snapshot(monkeypatch, cache, groups)
    registry = _registry()
    result = asyncio.run(
        dispatch_operation(
            registry,
            "sift",
            {
                "lens": _union_subtree_lens(),
                "query": "resolver",
                "ancestor_notes": True,
            },
        )
    )
    assert isinstance(result, dict) and result.get("success") is False, result
    assert (
        (result.get("error") or {}).get("code") == "invalid_parameter_combination"
    ), result


def test_merge_render_default_and_override(monkeypatch):
    cache, groups = _fixture_state()
    _install_snapshot(monkeypatch, cache, groups)
    registry = _registry()
    default_md = asyncio.run(
        dispatch_operation(
            registry, "merge.render", {"lens": _union_subtree_lens()}
        )
    )
    assert isinstance(default_md, str), default_md
    assert "BIG-ROOT-NOTE" not in default_md
    override_md = asyncio.run(
        dispatch_operation(
            registry,
            "merge.render",
            {"lens": _union_subtree_lens(), "ancestor_notes": True},
        )
    )
    assert isinstance(override_md, str), override_md
    assert "BIG-ROOT-NOTE" in override_md


# ---------------------------------------------------------------------------
# 4 · the docked-modal path (the live case): full document + row views
# ---------------------------------------------------------------------------

def _modal_node(node_id, name, parent_id=None, priority=0, note=""):
    return {
        "id": node_id,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
        "createdAt": priority + 1,
        "modifiedAt": priority + 1,
    }


def _modal_cache():
    return {
        "R": _modal_node("R", "Shared root", None, 0, "BIG-ROOT-NOTE\n\nThe whole framing document."),
        "A": _modal_node("A", "Architecture", "R", 1, "ARCH-ROOT-NOTE"),
        "A1": _modal_node("A1", "Resolver implementation", "A", 2, "RESOLVER-BODY"),
        "A2": _modal_node("A2", "Fresh architecture child", "A", 5, "A2-BODY"),
        "B": _modal_node("B", "Deployment", "R", 3, "DEPLOY-ROOT-NOTE"),
        "B1": _modal_node("B1", "Deploy normalization", "B", 4, "DEPLOY-BODY"),
    }


def _modal_config():
    return {
        "expand_groups": [],
        "active_group": None,
        "f8_postfix_auto_expand": "expand",
        "f8_postfix_suppress": "suppress",
        "f8_postfix_deprecated": "deprecated",
        "f8_global_never_expand_tags": ["wfy-suppress"],
        "note_session_tags": ["wfy-ns"],
        "note_session_local_postfixes": ["local"],
    }


class _WS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        import json

        self.sent.append(json.loads(raw))


def _run_modal(monkeypatch, *, spec, cache=None):
    from coherence_harness import make_boundary_capable

    cache = cache or _modal_cache()

    async def read_nested(**kwargs):
        return _modal_config(), None

    client = object.__new__(server.WorkFlowyClient)
    client._get_nodes_export_cache_nodes = lambda: list(cache.values())
    monkeypatch.setattr(
        server, "get_client", lambda: make_boundary_capable(client, nodes=list(cache.values()))
    )
    monkeypatch.setattr(server, "_read_slice_group_config_nested", read_nested)
    ws = _WS()
    frame = {
        "action": "render_slice_modal",
        "request_id": "req-5g",
        "endpoint_id": "tab-1",
        "max_prerender_chars": 200000,
        "render_spec": spec.to_render_spec_blob(),
    }
    asyncio.run(server._handle_render_slice_modal(frame, ws))
    assert ws.sent
    return ws.sent[-1]


def _modal_spec(**kwargs):
    return RenderProjectionSpec.normalize_from_glimpse_args(
        lightning={
            "mode": "render_lens",
            "lens": _union_subtree_lens(),
            "query": None,
            "query_mode": "smart",
            "match_scope": "name",
        },
        root_uuid="stale-derived-root",
        **kwargs,
    )


def test_modal_full_document_suppresses_scaffold_note_by_default(monkeypatch):
    result = _run_modal(monkeypatch, spec=_modal_spec())
    assert result["success"] is True, result
    assert "BIG-ROOT-NOTE" not in result["markdown"], result["markdown"][:400]
    assert "Shared root" in result["markdown"]
    assert "ARCH-ROOT-NOTE" in result["markdown"]
    assert "RESOLVER-BODY" in result["markdown"]


def test_modal_override_restores_scaffold_note(monkeypatch):
    result = _run_modal(monkeypatch, spec=_modal_spec(ancestor_notes=True))
    assert result["success"] is True, result
    assert "BIG-ROOT-NOTE" in result["markdown"]


def test_modal_row_snapshot_matches_full_document_on_scaffold(monkeypatch):
    """The per-row snapshot pre-renders (the small-slice path every row click
    is served from) MUST match the full document: a MEMBER row keeps its
    notes; no scaffold note leaks into any row view."""

    result = _run_modal(monkeypatch, spec=_modal_spec())
    rows = result.get("rows") or []
    renders = result.get("renders") or {}
    assert renders, result.get("render_mode")
    member_row = next((r for r in rows if r.get("uuid") == "A1"), None)
    assert member_row is not None, rows
    member_md = renders[server._slice_row_key(member_row)]["markdown"]
    assert "RESOLVER-BODY" in member_md
    assert "BIG-ROOT-NOTE" not in member_md
    # Every row view agrees: the scaffold note is gone everywhere.
    for entry in renders.values():
        assert "BIG-ROOT-NOTE" not in entry["markdown"]
