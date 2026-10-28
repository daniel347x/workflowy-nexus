"""Unadvertised fixed-string grace on every query-taking search operation."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from dataclasses import replace

import pytest

from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import OperationRegistry, dispatch_operation


SEARCHES = (
    (server.build_node_registry, "search", {"node_handle": "R"}, "is_regex"),
    (server.build_anchor_registry, "search", {"dock_handle": "R"}, "is_regex"),
    (server.build_slice_group_registry, "sift", {"lens": {"subtree": "R"}}, "query_mode"),
    (server.build_lightning_rod_registry, "render_search", {"root_handle": "R"}, "query_mode"),
)


@pytest.fixture(params=SEARCHES, ids=("node", "anchor", "sift", "render_search"))
def search(request):
    build, verb, scope, mode_key = request.param
    original = build()
    calls = []

    async def echo(params):
        calls.append(params)
        return params

    # Real operation metadata and shared dispatch, with only downstream I/O
    # replaced. Any normalization must happen before admission/cache/handler.
    registry = OperationRegistry(original.family, "Search test.")
    document = original.describe_full(verb)
    registry.register(replace(
        original.resolve(verb), handler=echo,
        params_schema=document["params_schema"], returns=document["returns"],
        examples=document["examples"],
    ))
    return registry, verb, scope, mode_key, calls


def run(search, **params):
    registry, verb, scope, _, _ = search
    return asyncio.run(dispatch_operation(registry, verb, {**scope, "query": "a.* OR b", **params}))


def test_search_inventory_covers_every_query_taking_operation():
    builds = (
        server.build_node_registry, server.build_anchor_registry,
        server.build_slice_group_registry, server.build_lightning_rod_registry,
        server.build_tag_registry, server.build_beacon_registry,
        server.build_glimpse_registry, server.build_system_registry,
    )
    found = {
        spec.canonical_name
        for build in builds
        for spec in build().operations()
        if "query" in spec.params_schema["properties"]
    }
    assert found == {"node.search", "anchor.search", "slice_group.sift", "lightning_rod.render_search"}


@pytest.mark.parametrize("mode", ["default", "non_regex", "regex"])
def test_false_is_opt_out_not_implicit_regex(search, mode):
    mode_key = search[3]
    choice = (mode == "regex") if mode_key == "is_regex" else ("regex" if mode == "regex" else "smart")
    params = {} if mode == "default" else {mode_key: choice}
    assert run(search, literal=False, **params) == run(search, **params)


def test_true_preserves_grep_default_or_lowers_smart_to_escaped_regex(search):
    result = run(search, literal=True)
    assert "literal" not in result
    if search[3] == "is_regex":
        assert result == run(search)
    else:
        assert result["query"] == re.escape("a.* OR b")
        assert result["query_mode"] == "regex"


def test_true_accepts_explicit_non_regex_default(search):
    mode_key = search[3]
    result = run(search, literal=True, **{mode_key: False if mode_key == "is_regex" else "smart"})
    assert "success" not in result
    assert result["query"] == ("a.* OR b" if mode_key == "is_regex" else re.escape("a.* OR b"))


def test_regex_contradiction_is_short_before_admission_or_io(search):
    registry, verb, scope, mode_key, calls = search

    def forbidden(*args):
        pytest.fail("literal contradiction must fail before admission/cache/I/O")

    result = asyncio.run(dispatch_operation(
        registry, f"{registry.family}.{verb}",
        {**scope, "query": "x", "literal": True, mode_key: True if mode_key == "is_regex" else "regex"},
        mutation_admission=forbidden, operation_boundary=forbidden,
    ))
    assert result["error"]["code"] == "invalid_parameter_combination"
    assert result["error"]["message"] == f"literal=true conflicts with {mode_key}={'true' if mode_key == 'is_regex' else repr('regex')}; choose one search mode."
    assert result["operation"] == f"{registry.family}.{verb}"
    assert "operation_doc" not in result and "operation_doc_error" not in result
    assert len(json.dumps(result)) < 500
    assert calls == []


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_literal_requires_boolean_with_short_error(search, value):
    result = run(search, literal=value)
    assert result["error"]["code"] == "invalid_parameter_type"
    assert result["error"]["message"] == "parameter 'literal' must be boolean"
    assert "operation_doc" not in result
    assert search[4] == []


def test_other_validation_errors_still_include_the_operation_doc(search):
    result = run(search, literal=True, query=123)
    assert result["error"]["code"] == "invalid_parameter_type"
    assert result["operation_doc"] == search[0].describe_full(search[1])


def test_invalid_canonical_mode_is_not_hidden_by_literal(search):
    result = run(search, literal=True, **{search[3]: "bogus"})
    assert result["error"]["code"] in {"invalid_parameter_type", "invalid_enum_value"}
    assert "operation_doc" in result


def test_grace_is_unadvertised_and_does_not_mutate_caller_or_metadata(search):
    registry, verb, scope, _, _ = search
    before = registry.describe_full(verb)
    params = {**scope, "query": "[x]*?", "literal": True}
    saved = copy.deepcopy(params)
    dict_result = asyncio.run(dispatch_operation(registry, verb, params))
    assert asyncio.run(dispatch_operation(registry, verb, json.dumps(params))) == dict_result
    assert params == saved
    assert registry.describe_full(verb) == before
    assert "literal" not in before["params_schema"]["properties"]
    assert "literal" not in registry.resolve(verb).param_aliases


@pytest.mark.parametrize("build", [server.build_node_registry, server.build_anchor_registry])
def test_existing_regex_key_slip_is_normalized_before_literal_conflict(build):
    result = asyncio.run(dispatch_operation(build(), "search", {"query": "x", "literal": True, "regex": True}))
    assert result["error"]["code"] == "invalid_parameter_combination"
    assert "is_regex=true" in result["error"]["message"]
    assert "operation_doc" not in result


def test_literal_is_not_tolerated_on_non_search_verbs():
    registry = server.build_node_registry()
    result = asyncio.run(dispatch_operation(registry, "read", {"node_handle": "R", "literal": True}))
    assert result["error"]["code"] == "forbidden_parameter"
    assert result["operation_doc"] == registry.describe_full("read")
