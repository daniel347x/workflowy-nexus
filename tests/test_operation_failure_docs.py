"""One response-only documentation attachment; real execution hooks stay authoritative.

No live Workflowy, broker or production runtime is used. The integration cases
reuse the isolated coherence harness and real nested validators, with writes
and live GETs forbidden. Pure cases lock the response contract across all families.
"""
from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from coherence_harness import make_boundary_capable, make_coherence_root
from workflowy_mcp import server
from workflowy_mcp.coherence.operation_cache import current_operation_cache
from workflowy_mcp.mcp_api_registry import (
    OperationRegistry,
    OperationSpec,
    _dispatch_operation_core,
    dispatch_operation,
    make_error,
    make_job_record,
    make_mutation_ack,
)


def demo(handler):
    registry = OperationRegistry("demo", "Demo operations.")
    registry.register(OperationSpec(
        family="demo", verb="run", summary="Run a demo.",
        side_effects=("workflowy_mutation",), async_mode="optional",
        params_schema={
            "type": "object", "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
        returns={"description": "Original outcome, with nested detail."},
        examples=({"call": {"verb": "run", "params": {"value": "📊"}}},),
        related=("demo.describe",), handler=handler, cache_backed=True,
        aliases=("go",), param_aliases={"old_value": "value"},
    ))
    return registry


def forbidden(*args, **kwargs):
    raise AssertionError("validation must not invoke execution/coherence hooks")


@pytest.mark.parametrize("family", [
    "node", "tag", "anchor", "beacon", "slice_group", "system", "glimpse", "lightning_rod",
])
def test_every_registered_operation_gets_exact_describe_without_execution(family):
    registry = getattr(server, f"build_{family}_registry")()
    hooks = dict(
        operation_boundary=forbidden, mutation_admission=forbidden,
        exception_mapper=forbidden, result_finalizer=forbidden,
    )
    for spec in registry.operations():
        params = {"__unknown_argument__": "unchanged"}
        result = asyncio.run(dispatch_operation(registry, spec.canonical_name, params, **hooks))
        baseline = asyncio.run(_dispatch_operation_core(registry, spec.canonical_name, params, **hooks))
        full = asyncio.run(dispatch_operation(registry, "describe", {"of": spec.verb}, **hooks))
        assert baseline["success"] is False
        assert result == {**baseline, "operation_doc": full}
        # Same serializer and JSON bytes, not a reduced/synthetic mini-skill.
        assert json.dumps(result["operation_doc"], ensure_ascii=False) == json.dumps(full, ensure_ascii=False)
        assert params == {"__unknown_argument__": "unchanged"}


@pytest.mark.parametrize("verb,params", [
    ("run", {"unknown": 1}),
    ("demo.run", {"value": 3}),
    (" go ", {"value": "x", "old_value": "y"}),
    ("run", "not JSON"),
    ("go", []),
    ("describe", {"detail": "full"}),
    ("demo.describe", {"detail": "index", "of": "run"}),
    ("describe", {"of": "unknown"}),
])
def test_aliases_malformed_params_and_intrinsic_describe_preserve_original_error(verb, params):
    registry = demo(forbidden)
    baseline = asyncio.run(_dispatch_operation_core(registry, verb, params))
    result = asyncio.run(dispatch_operation(registry, verb, params))
    assert result == {**baseline, "operation_doc": registry.describe_full(verb)}


@pytest.mark.parametrize("verb,params", [(None, {}), ("", {}), ("unknown", {}), ("unknown", "bad JSON")])
def test_unknown_or_missing_verb_keeps_original_family_teaching(verb, params):
    registry = demo(forbidden)
    baseline = asyncio.run(_dispatch_operation_core(registry, verb, params))
    result = asyncio.run(dispatch_operation(registry, verb, params))
    assert result == baseline
    assert "operation_doc" not in result


@pytest.mark.parametrize("payload", [
    "RAW document containing the word error",
    '{"success": false} is literal note text',
    {"success": True, "value": "ok"},
    {"error": "not an explicit failure envelope"},
    {"success": 0},
    {"success": None},
    {"success": "false"},
    False,
    None,
    make_job_record(operation="demo.run", job_id="J", state="started"),
    {"success": True, "job": {"status": "failed", "error": "background result"}},
])
def test_no_inference_no_success_growth_no_job_record_recursion(payload, monkeypatch):
    async def handler(params):
        return payload
    registry = demo(handler)
    monkeypatch.setattr(registry, "describe_full", forbidden)
    assert asyncio.run(dispatch_operation(registry, "run", {})) is payload


def test_partial_mutation_ledger_and_recovery_instructions_are_preserved():
    ledger = {
        "success": False, "partial_success": True, "nodes_created": 1,
        "root_node_ids": ["CREATED"],
        "errors": ["Source update outcome unknown; read live before retry."],
    }
    original = make_mutation_ack(operation="demo.run", result=ledger, changed=True, warnings=("inspect ledger",))
    before = copy.deepcopy(original)
    async def handler(params):
        return original
    registry = demo(handler)
    result = asyncio.run(dispatch_operation(registry, "run", {}))
    assert result is not original
    assert original == before and "operation_doc" not in original
    assert result["detail"] is ledger  # Preserved, not reclassified or rewritten.
    assert result["success"] is False and result["changed"] is True
    assert {k: v for k, v in result.items() if k != "operation_doc"} == before
    result["operation_doc"]["params_schema"]["properties"]["value"]["type"] = "integer"
    result["operation_doc"]["examples"][0]["call"]["params"]["value"] = "modified copy"
    fresh = registry.describe_full("run")
    assert fresh["params_schema"]["properties"]["value"]["type"] == "string"
    assert fresh["examples"][0]["call"]["params"]["value"] == "📊"


@pytest.mark.parametrize("raises", [False, True])
def test_documentation_runs_after_original_boundary_finalizer_and_exception_mapping(monkeypatch, raises):
    events = []
    original = {"success": False, "error": {"code": "unchanged"}}
    async def handler(params):
        events.append("handler")
        if raises:
            raise ValueError("argument problem")
        return original
    registry = demo(handler)
    def admission(spec):
        events.append("admission")
    @asynccontextmanager
    async def boundary(name):
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")
    def finalize(spec, result):
        events.append("finalizer")
        assert result is original
        return result
    def map_exception(spec, exc):
        events.append("exception_mapper")
        assert isinstance(exc, ValueError)
        return original
    describe = registry.describe_full
    def traced_describe(verb):
        events.append("describe")
        return describe(verb)
    monkeypatch.setattr(registry, "describe_full", traced_describe)
    result = asyncio.run(dispatch_operation(
        registry, "run", {}, operation_boundary=boundary,
        mutation_admission=admission, exception_mapper=map_exception,
        result_finalizer=finalize,
    ))
    middle = ["exit", "exception_mapper"] if raises else ["finalizer", "exit"]
    assert events == ["admission", "enter", "handler", *middle, "describe"]
    assert result == {**original, "operation_doc": describe("run")}
    assert "operation_doc" not in original


def test_admission_refusal_remains_pre_handler_and_preserves_typed_error():
    registry = demo(forbidden)
    refusal = make_error(
        family="demo", operation="demo.run", code="coherence_barrier_active",
        message="Stop; repair required", extra={"transaction_id": "TX", "dirty_generation": 9},
        operation_spec=registry.resolve("run"),
    )
    async def admission(spec):
        return refusal
    result = asyncio.run(dispatch_operation(
        registry, "run", {}, mutation_admission=admission,
        operation_boundary=forbidden, result_finalizer=forbidden, exception_mapper=forbidden,
    ))
    assert result == {**refusal, "operation_doc": registry.describe_full("run")}
    assert "operation_doc" not in refusal


def test_unmapped_exception_still_uses_original_error_logger_and_envelope():
    async def handler(params):
        raise ValueError("nested argument error")
    registry = demo(handler)
    logged = []
    result = asyncio.run(dispatch_operation(registry, "run", {}, error_logger=logged.append))
    assert result["error"]["code"] == "operation_failed"
    assert result["error"]["message"] == "ValueError: nested argument error"
    assert result["operation_doc"] == registry.describe_full("run")
    assert logged == ["demo.run failed: ValueError: nested argument error"]


def test_cancellation_is_not_converted_or_documented(monkeypatch):
    async def handler(params):
        raise asyncio.CancelledError()
    registry = demo(handler)
    monkeypatch.setattr(registry, "describe_full", forbidden)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dispatch_operation(registry, "run", {}))


def test_documentation_failure_cannot_hide_original_outcome(monkeypatch):
    original = {"success": False, "changed": True, "detail": {"created": ["A"]}}
    async def handler(params):
        return original
    registry = demo(handler)
    def broken_describe(verb):
        raise RuntimeError("metadata defect")
    monkeypatch.setattr(registry, "describe_full", broken_describe)
    result = asyncio.run(dispatch_operation(registry, "run", {}))
    assert result == {**original, "operation_doc_error": "RuntimeError: metadata defect"}
    assert "operation_doc" not in result
    assert "operation_doc_error" not in original


PARENT = "11111111-2222-3333-4444-555555555555"


@pytest.mark.parametrize("family,verb,params,needle", [
    ("node", "etch", {"parent_handle": PARENT, "nodes": [{"name": "X", "content": "wrong field"}]}, "nodes[0] has unknown field(s): ['content']"),
    ("node", "edit", {"node_handle": PARENT, "note_edits": [{"search": "x", "replacement": "y"}]}, "note_edits[0] has unknown field(s)"),
    ("tag", "batch", {"operations": []}, "operations"),
    ("beacon", "tags.ensure", {"operations": [], "root_handles": [PARENT]}, "operations"),
])
def test_real_nested_validators_keep_diagnostic_and_get_full_docs(monkeypatch, tmp_path, family, verb, params, needle):
    paths = make_coherence_root(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    client = make_boundary_capable(object.__new__(server.WorkFlowyClient), nodes=[
        {"id": PARENT, "name": "Parent", "note": "unchanged", "parent_id": None},
    ])
    from n05_region_harness import isolate_claim_scope
    isolate_claim_scope(client, tmp_path)
    client._log_to_file = lambda *a, **k: None
    client.get_node = AsyncMock(side_effect=AssertionError("no live GET"))
    client.create_node = AsyncMock(side_effect=AssertionError("no create"))
    client.update_node = AsyncMock(side_effect=AssertionError("no update"))
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    view = client._cache_view_publisher.capture()
    registry = getattr(server, f"build_{family}_registry")()
    result = asyncio.run(dispatch_operation(
        registry, verb, params,
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    ))
    assert result["success"] is False
    assert result["operation_doc"] == registry.describe_full(verb)
    diagnostic = json.dumps({k: v for k, v in result.items() if k != "operation_doc"})
    assert needle in diagnostic
    if verb == "etch":
        assert result["changed"] is False
        assert result["detail"]["nodes_created"] == 0
        assert "'note'" in result["detail"]["errors"][0]
    client.get_node.assert_not_awaited()
    client.create_node.assert_not_awaited()
    client.update_node.assert_not_awaited()
    assert client._cache_view_publisher.capture() is view
    assert current_operation_cache(required=False) is None
