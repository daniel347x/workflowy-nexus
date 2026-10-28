"""Audit D W1: the grep value/schema contract must not drift across verbs."""
from __future__ import annotations

import asyncio

from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation


def test_grep_parameter_contract_across_verbs(monkeypatch):
    """Pin schema facts and real ingress errors, not a mocked normalizer.

    This passes on c4fe45c while its copies agree. The deliberate mutation
    check changes only sift's maximum: the equality assertion must fail.
    """
    node = server.build_node_registry()
    anchor = server.build_anchor_registry()
    slices = server.build_slice_group_registry()
    operations = ((node, "search"), (anchor, "search"), (slices, "sift"))
    expected = {
        "context_lines": {"type": "integer", "minimum": 0, "maximum": 10, "default": 2},
        "max_matches": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "context_width": {"type": ["integer", "null"], "default": 160},
        "match_width": {"type": ["integer", "null"], "default": 1000},
    }
    facts = []
    for registry, verb in operations:
        properties = registry.describe_full(verb)["params_schema"]["properties"]
        facts.append({
            name: {key: value for key, value in properties[name].items() if key != "description"}
            for name in expected
        })
    assert facts[0] == facts[1] == facts[2] == expected

    # Bounds must fail before live resolution/config/projection. Do not
    # patch the owning validators, handlers or error-envelope machinery.
    async def no_io(*_args, **_kwargs):
        raise AssertionError("invalid grep bounds reached I/O")

    monkeypatch.setattr(server, "_node_read_impl", no_io)
    monkeypatch.setattr(server, "_resolve_note_session_config", no_io)
    monkeypatch.setattr(server, "_build_lens_stage0_snapshot", no_io)
    params = (
        {"node_handle": "12345678-0000-4000-8000-000000000001", "query": "hit"},
        {"dock_handle": "12345678-0000-4000-8000-000000000001", "query": "hit"},
        {"lens": {"kind": "subtree", "root_handle": "12345678"}, "query": "hit", "tier": "context"},
    )
    for name, value, message in (
        ("max_matches", 0, "max_matches must be 1..200"),
        ("context_lines", 11, "context_lines must be 0..10"),
    ):
        errors = []
        for (registry, verb), base_params in zip(operations, params):
            result = asyncio.run(dispatch_operation(registry, verb, {**base_params, name: value}))
            assert result["success"] is False
            errors.append((result["error"]["code"], result["error"]["message"]))
        assert errors == [("invalid_parameter_value", message)] * 3
