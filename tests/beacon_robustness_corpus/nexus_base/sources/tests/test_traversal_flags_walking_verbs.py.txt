"""[ap:EPR86W] part b — the traversal pair on the OTHER walking verbs.

Unit 4b (group G9) of the MCP API Unification program: part (a) gave
``dock.render`` / ``glimpse.run`` / the estate ``{dock}`` form the pair and
gated the ONE shared walker; this unit carries ``recursive`` +
``descend_policy`` through the remaining walking verbs' schemas and
boundaries under dock.peek's exact names and values:

* the Lightning Rod walking verbs (``gather`` / ``render_tags`` /
  ``render_search``) accept the pair via the ONE shared selection fragment
  (``_render_selection_properties`` -- one definition site, shared with
  ``glimpse.run``), and ``recursive=False`` is refused LOUDLY at the public
  boundary (before the push branch, so a pushed selection cannot silently
  drop the request) with the orchestrator keeping the same refusal as its
  internal backstop (one refusal text, ``_lightning_recursive_refusal``);
* ``render_symbols`` / ``render_nodes`` keep REJECTING the pair (the
  selection is an explicit node/root set, per-item ``expand`` is the depth
  knob) -- said in their describes, refused by the closed schema;
* ``slice_group.render`` / ``sift`` / ``merge.render`` keep NO top-level
  pair: the lens subtree atom's (and the ``{dock}`` form's) ``descend`` key
  is this surface's traversal spelling, now taught once in the shared
  ``lens`` property description;
* ``node.scry`` / ``node.search`` say in their describes that they read the
  whole subtree (scry's ``depth`` is an export cap, not the traversal leg).

Every test here FAILS against the pre-fix code: the lightning schemas had no
``recursive`` (a closed-schema forbidden_parameter), the impl had no
``recursive`` kwarg (TypeError), and the describe sentences did not exist
(would-it-fail check applied per §5.5a of the role manual).

All local: no network, no real Workflowy, no disk writes.
"""

import asyncio
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pytest  # noqa: E402

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.mcp_api_registry import (  # noqa: E402
    OperationValidationError,
    dispatch_operation,
    validate_params,
)


# ---------------------------------------------------------------------------
# 1 · the shared fragment: one definition site, carried by the schemas.
# ---------------------------------------------------------------------------

def test_recursive_lives_in_the_shared_selection_fragment():
    fragment = srv._render_selection_properties()
    assert "recursive" in fragment
    assert fragment["recursive"]["type"] == "boolean"
    assert fragment["recursive"]["description"] == srv._RECURSIVE_DESCRIPTION
    # The pair stays together: the boundary leg rides the same fragment.
    assert "descend_policy" in fragment
    # One text, teaching the honouring AND the refusal.
    desc = fragment["recursive"]["description"]
    assert "RAW-ROOT traversal policy" in desc
    assert "refuses it loudly" in desc
    assert "glimpse.run root_handle" in desc
    # The descend_policy description is the boundary leg's teaching.
    assert "Boundary leg of the traversal pair" in fragment["descend_policy"]["description"]
    assert "moot" in fragment["descend_policy"]["description"]


@pytest.mark.parametrize("verb", ["gather", "render_tags", "render_search"])
def test_lightning_walking_verbs_carry_the_pair(verb):
    registry = srv.build_lightning_rod_registry()
    props = registry.resolve(verb).params_schema["properties"]
    assert props["recursive"]["type"] == "boolean"
    assert props["recursive"]["description"] == srv._RECURSIVE_DESCRIPTION
    assert list(props["descend_policy"]["enum"]) == [
        "all",
        "stop_at_child_anchors",
        "stop_at_child_docks",
    ]


@pytest.mark.parametrize("verb,params", [
    ("gather", {"root_handle": "R"}),
    ("gather", {"root_handle": "R", "recursive": False}),
    ("render_tags", {"root_handle": "R", "tags": ["t"], "recursive": True, "descend_policy": "all"}),
    ("render_search", {"root_handle": "R", "query": "x", "recursive": False}),
])
def test_lightning_walking_verbs_validate_the_pair(verb, params):
    registry = srv.build_lightning_rod_registry()
    validate_params(registry.resolve(verb), params)


# ---------------------------------------------------------------------------
# 2 · render_symbols / render_nodes keep REJECTING the pair, said in describe.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verb", ["render_symbols", "render_nodes"])
@pytest.mark.parametrize("flag_params", [
    {"recursive": False},
    {"recursive": True},
    {"descend_policy": "all"},
    {"descend_policy": "stop_at_child_anchors"},
])
def test_symbol_verbs_reject_both_flags(verb, flag_params):
    registry = srv.build_lightning_rod_registry()
    props = registry.resolve(verb).params_schema["properties"]
    assert "recursive" not in props and "descend_policy" not in props
    params = {"symbols": ["fn"], "root_handles": "R", **flag_params}
    with pytest.raises(OperationValidationError):
        validate_params(registry.resolve(verb), params)


@pytest.mark.parametrize("verb", ["render_symbols", "render_nodes"])
def test_symbol_verbs_describe_say_why_not(verb):
    registry = srv.build_lightning_rod_registry()
    ret = registry.resolve(verb).returns["description"]
    assert "recursive/descend_policy are NOT accepted here" in ret
    assert "per-item expand is the depth knob" in ret


# ---------------------------------------------------------------------------
# 3 · routing: the registry threads recursive to the ONE legacy authority.
# ---------------------------------------------------------------------------

def _lightning_calls(monkeypatch):
    calls = []

    async def impl(**kwargs):
        calls.append(kwargs)
        return "RAW"

    monkeypatch.setattr(srv, "_lightning_rod_legacy_impl", impl)
    return calls


def test_registry_routes_recursive_false_into_impl_kwargs(monkeypatch):
    calls = _lightning_calls(monkeypatch)
    registry = srv.build_lightning_rod_registry()
    asyncio.run(dispatch_operation(
        registry, "gather", {"root_handle": "R", "recursive": False}
    ))
    assert calls[-1]["recursive"] is False
    asyncio.run(dispatch_operation(registry, "gather", {"root_handle": "R"}))
    assert calls[-1]["recursive"] is True


def test_registry_routes_recursive_for_both_render_modes(monkeypatch):
    calls = _lightning_calls(monkeypatch)
    registry = srv.build_lightning_rod_registry()
    asyncio.run(dispatch_operation(
        registry,
        "render_tags",
        {"root_handle": "R", "tags": ["t"], "recursive": False, "descend_policy": "stop_at_child_anchors"},
    ))
    assert calls[-1]["recursive"] is False
    assert calls[-1]["descend_policy"] == "stop_at_child_anchors"
    asyncio.run(dispatch_operation(
        registry, "render_search", {"root_handle": "R", "query": "x"}
    ))
    assert calls[-1]["recursive"] is True


def test_render_symbols_kwargs_carry_no_recursive_key(monkeypatch):
    # The explicit-selection verb drops the default depth leg, exactly as it
    # drops match_scope/descend_policy: its kwargs stay as before this unit.
    calls = _lightning_calls(monkeypatch)
    registry = srv.build_lightning_rod_registry()
    asyncio.run(dispatch_operation(
        registry, "render_symbols", {"symbols": ["fn"], "root_handles": "R"}
    ))
    assert "recursive" not in calls[-1]
    assert "descend_policy" not in calls[-1]


def test_mutation_modes_are_not_registry_operations():
    # The mutation modes were retired from the lightning registry (the
    # describe points at beacon.tags.ensure/remove and tag.batch), so no
    # mutation surface carries the traversal pair at all.
    registry = srv.build_lightning_rod_registry()
    for verb in ("ensure_beacons", "remove_beacon_tags", "mutate_node_tags"):
        assert registry.resolve(verb) is None


# ---------------------------------------------------------------------------
# 4 · the loud refusal (public boundary + orchestrator backstop, one text).
# ---------------------------------------------------------------------------

def test_impl_refuses_recursive_false_before_any_io():
    result = asyncio.run(
        srv._lightning_rod_legacy_impl(
            mode="gather",
            root_uuid="00000000-0000-0000-0000-000000000000",
            recursive=False,
        )
    )
    assert result["success"] is False
    assert "RAW-ROOT traversal policy" in result["error"]
    assert "Lightning Rod selection" in result["error"]
    assert "glimpse.run" in result["error"]


def test_impl_refusal_fires_before_the_push_branch():
    # The exact silent-ignore the to-do forbids: a pushed lightning selection
    # used to drop the flag; the boundary now refuses before pushing.
    result = asyncio.run(
        srv._lightning_rod_legacy_impl(
            mode="render_tags",
            root_uuid="00000000-0000-0000-0000-000000000000",
            tags=["t"],
            push_to_modal=True,
            recursive=False,
        )
    )
    assert result["success"] is False
    assert "RAW-ROOT traversal policy" in result["error"]


@pytest.mark.parametrize("mode,params", [
    ("gather", {}),
    ("render_tags", {"tags": ["t"]}),
    ("render_search", {"query": "x"}),
])
def test_orchestrator_backstop_refuses_recursive_false(mode, params):
    result = asyncio.run(
        srv._lightning_rod_from_cache(
            mode=mode,
            root_uuid="00000000-0000-0000-0000-000000000000",
            recursive=False,
            **params,
        )
    )
    assert result["success"] is False
    assert "RAW-ROOT traversal policy" in result["error"]


def test_recursive_true_default_never_refuses():
    # The default must reach the orchestrator untouched (the refusal is about
    # False only): a True default returns the ordinary not-a-client path, not
    # the traversal refusal.
    with pytest.raises(RuntimeError, match="client not initialized"):
        asyncio.run(
            srv._lightning_rod_from_cache(
                mode="gather",
                root_uuid="00000000-0000-0000-0000-000000000000",
            )
        )


# ---------------------------------------------------------------------------
# 5 · the describe texts.
# ---------------------------------------------------------------------------

def test_lightning_walking_verbs_describe_the_pair_per_verb():
    registry = srv.build_lightning_rod_registry()
    gather_ret = registry.resolve("gather").returns["description"]
    assert "Traversal pair:" in gather_ret
    assert "recursive=False is refused loudly" in gather_ret
    assert "tags are still gathered" in gather_ret
    for verb in ("render_tags", "render_search"):
        ret = registry.resolve(verb).returns["description"]
        assert "Traversal pair:" in ret
        assert "leaf signpost" in ret
        assert "recursive=False is refused loudly" in ret


def test_glimpse_run_and_lightning_share_one_recursive_description():
    # ONE definition site: the fragment's text is byte-identical wherever the
    # pair is carried.
    glimpse_props = srv.build_glimpse_registry().resolve("run").params_schema["properties"]
    lr_props = srv.build_lightning_rod_registry().resolve("render_tags").params_schema["properties"]
    assert (
        glimpse_props["recursive"]["description"]
        == lr_props["recursive"]["description"]
        == srv._RECURSIVE_DESCRIPTION
    )


def test_slice_group_lens_description_teaches_the_traversal_spelling():
    registry = srv.build_slice_group_registry()
    lens_desc = registry.resolve("render").params_schema["properties"]["lens"]["description"]
    assert "subtree atom's" in lens_desc
    assert "NO top-level recursive/descend_policy" in lens_desc
    # The shared property rides all three render verbs.
    for verb in ("sift", "merge.render"):
        other = registry.resolve(verb).params_schema["properties"]["lens"]["description"]
        assert other == lens_desc


def test_slice_group_schemas_reject_top_level_traversal_pair():
    registry = srv.build_slice_group_registry()
    lens = {"kind": "group", "group_handle": "A"}
    with pytest.raises(OperationValidationError):
        validate_params(registry.resolve("render"), {"lens": lens, "recursive": False})
    with pytest.raises(OperationValidationError):
        validate_params(registry.resolve("sift"), {"lens": lens, "query": "x", "descend_policy": "all"})


def test_node_scry_depth_describes_the_export_cap():
    registry = srv.build_node_registry()
    props = registry.resolve("scry").params_schema["properties"]
    assert "NOT the render verbs" in props["depth"]["description"]
    assert "no recursive/descend_policy" in props["depth"]["description"]
    # And the schema still rejects the pair outright (closed properties).
    with pytest.raises(OperationValidationError):
        validate_params(registry.resolve("scry"), {"root_handle": "R", "recursive": False})


def test_node_search_returns_teach_the_whole_subtree():
    registry = srv.build_node_registry()
    ret = registry.resolve("search").returns["description"]
    assert "a grep has no recursive/descend_policy" in ret
    assert "whole subtree beneath the root" in ret
    assert "export cap, not a traversal leg" in ret
    with pytest.raises(OperationValidationError):
        validate_params(registry.resolve("search"), {"query": "x", "recursive": False})
