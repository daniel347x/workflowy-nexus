from __future__ import annotations

import asyncio
import json

import pytest

from workflowy_mcp.mcp_api_registry import (
    CORE_ERROR_CODES,
    EMPTY_RESULT_STUB,
    OperationRegistry,
    OperationSpec,
    OperationSpecError,
    OperationValidationError,
    _normalize_params,
    dispatch_operation,
    make_error,
    make_job_record,
    make_mutation_ack,
    stub_empty_text_result,
    validate_params,
)


async def _echo(params):
    return params


def _spec(**overrides):
    values = {
        "family": "demo",
        "verb": "run",
        "summary": "Run the demo operation.",
        "side_effects": ("pure_read",),
        "async_mode": "never",
        "params_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "returns": {"type": "object"},
        "examples": (),
        "related": (),
        "handler": _echo,
        # S03 §7.2: the demo spec defaults to cache-free; tests that exercise
        # the dispatch boundary hook pass cache_backed=True explicitly.
        "cache_backed": False,
    }
    values.update(overrides)
    return OperationSpec(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"side_effects": ()},
        {"side_effects": None},
        {"summary": "two\nlines"},
        {"side_effects": ("not_real",)},
        {"async_mode": "sometimes"},
        {"verb": "read", "side_effects": ("workflowy_mutation",)},
        {"verb": "render_custom", "side_effects": ("config_mutation",)},
        {"params_schema": {"type": "object"}},
        {"params_schema": None},
        {"returns": None},
        {"examples": ("not-an-object",)},
        {"related": ("",)},
        {"params_schema": {"type": "array", "additionalProperties": False}},
        {"verb": "describe"},
        {"verb": "demo.describe"},
        {"aliases": ("demo.describe",)},
        {"aliases": ("bad alias",)},
        {"aliases": None},
        {"domain_error_codes": ("Bad-Code",)},
        {"domain_error_codes": None},
        {"domain_error_codes": ("same", "same")},
        {"domain_error_codes": (CORE_ERROR_CODES[0],)},
    ],
)
def test_operation_spec_rejects_invalid_registration(overrides):
    with pytest.raises(OperationSpecError):
        _spec(**overrides)


def test_registry_error_code_union_and_operation_describe_exposure():
    registry = OperationRegistry("demo", "Demo operations.")
    spec = _spec(domain_error_codes=("domain_miss", "domain_busy"))
    registry.register(spec)

    assert registry.error_codes() == (*CORE_ERROR_CODES, "domain_miss", "domain_busy")
    full = registry.describe_full("run")
    assert full["operation"] == "demo.run"
    assert full["error_codes"] == [*CORE_ERROR_CODES, "domain_miss", "domain_busy"]


@pytest.mark.parametrize(
    "first,second",
    [
        (_spec(verb="one", aliases=("shortcut",)), _spec(verb="shortcut")),
        (_spec(verb="one", aliases=("shortcut",)), _spec(verb="two", aliases=("shortcut",))),
        (_spec(verb="one"), _spec(verb="one")),
        (_spec(verb="one"), _spec(verb="two", aliases=("demo.one",))),
    ],
)
def test_alias_collision_matrix(first, second):
    registry = OperationRegistry("demo", "Demo operations.")
    registry.register(first)
    with pytest.raises(OperationSpecError):
        registry.register(second)


def test_resolve_accepts_canonical_and_family_qualified_alias_only_exact_case():
    registry = OperationRegistry("demo", "Demo operations.")
    spec = _spec(verb="thing.read", aliases=("read.thing",))
    registry.register(spec)

    assert registry.resolve(" thing.read ") is spec
    assert registry.resolve("demo.thing.read") is spec
    assert registry.resolve("read.thing") is spec
    assert registry.resolve("Thing.Read") is None


def test_validator_reports_missing_forbidden_type_enum_items_and_all_problems():
    spec = _spec(
        params_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "mode": {"type": "string", "enum": ["a", "b"]},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name"],
            "additionalProperties": False,
        }
    )
    with pytest.raises(OperationValidationError) as caught:
        validate_params(
            spec,
            {
                "count": True,
                "ratio": False,
                "mode": "c",
                "tags": ["ok", 3],
                "extra": 1,
            },
        )
    assert caught.value.code == "missing_parameter"
    message = caught.value.message
    assert "name" in message
    assert "extra" in message
    assert "count" in message
    assert "ratio" in message
    assert "mode" in message
    assert "tags" in message

    validate_params(
        spec,
        {"name": "ok", "count": 2.0, "ratio": 2, "mode": "a", "tags": []},
    )
    with pytest.raises(OperationValidationError):
        validate_params(spec, {"name": "bad", "count": 2.5, "mode": "a", "tags": []})


def test_dispatch_json_string_grace_and_malformed_params():
    registry = OperationRegistry("demo", "Demo operations.")
    registry.register(
        _spec(
            params_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            }
        )
    )

    assert asyncio.run(dispatch_operation(registry, "run", '{"value":"yes"}')) == {
        "value": "yes"
    }
    for malformed in ("not json", "[]", [], 3):
        result = asyncio.run(dispatch_operation(registry, "run", malformed))
        assert result["error"]["code"] == "malformed_params"


def test_malformed_json_params_message_names_position_excerpt_and_key():
    """[ap:KXBY1L]: the malformed_params message for a JSON-string params
    points at WHERE the string broke -- the char position, an escaped
    excerpt around it, and a best-effort top-level key -- and says the
    string arrived malformed before NEXUS parsed it."""
    params = (
        '{"note_search": "(nothing yet)", "note_replace": "paragraph '
        + "x" * 200
        + '" "note_search2": "b"}'
    )
    with pytest.raises(OperationValidationError) as excinfo:
        _normalize_params(params)
    assert excinfo.value.code == "malformed_params"
    message = excinfo.value.message
    assert message.startswith("params is not valid JSON: Expecting")
    assert "at char " in message
    assert f"of {len(params)}" in message
    assert '"note_replace"' in message
    assert 'likely inside the value of top-level key "note_replace"' in message
    assert "calling client's serialization" in message


def test_malformed_json_params_message_escapes_excerpt_and_survives_empty():
    """Invisible characters in the excerpt are escaped to be visible, and an
    empty params string still reports malformed_params without a crash."""
    with pytest.raises(OperationValidationError) as excinfo:
        _normalize_params('{"a": "x\xa0y" "b": 1}')
    message = excinfo.value.message
    assert "\\xa0" in message  # the NBSP is made visible
    assert 'top-level key "a"' in message

    with pytest.raises(OperationValidationError) as excinfo:
        _normalize_params("")
    assert excinfo.value.code == "malformed_params"
    assert "not valid JSON" in excinfo.value.message


def test_intrinsic_describe_state_machine_and_unknown_of():
    registry = OperationRegistry("demo", "Demo operations.", "node_handle = UUID")
    registry.register(_spec())

    index = asyncio.run(dispatch_operation(registry, "describe", None))
    assert [row["verb"] for row in index["operations"]] == ["run", "describe"]
    assert index["handle_grammar"] == "node_handle = UUID"

    full = asyncio.run(dispatch_operation(registry, "demo.describe", {"of": "run"}))
    assert full["operation"] == "demo.run"

    missing = asyncio.run(
        dispatch_operation(registry, "describe", {"detail": "full"})
    )
    assert missing["error"]["code"] == "missing_parameter"

    invalid_combo = asyncio.run(
        dispatch_operation(
            registry, "describe", {"detail": "index", "of": "run"}
        )
    )
    assert invalid_combo["error"]["code"] == "invalid_parameter_combination"

    invalid_detail = asyncio.run(
        dispatch_operation(registry, "describe", {"detail": "examples"})
    )
    assert invalid_detail["error"]["code"] == "invalid_enum_value"

    unknown = asyncio.run(
        dispatch_operation(registry, "describe", {"of": "missing"})
    )
    assert unknown["error"]["code"] == "unknown_operation"
    assert "run —" in unknown["error"]["message"]


def test_error_extra_and_describe_hint_forms():
    index_error = make_error(
        family="demo", operation=None, code="missing_verb", message="missing"
    )
    assert index_error["error"]["describe_hint"] == {
        "tool": "demo",
        "verb": "describe",
        "detail": "index",
    }

    spec = _spec(domain_error_codes=("domain_miss",))
    op_error = make_error(
        family="demo",
        operation="demo.run",
        code="domain_miss",
        message="nope",
        extra={"path": [1, 2]},
        operation_spec=spec,
    )
    assert op_error["error"]["describe_hint"]["of"] == "run"
    assert op_error["error"]["extra"] == {"path": [1, 2]}


def test_make_error_rejects_undeclared_domain_codes():
    spec = _spec(domain_error_codes=("known_domain",))
    with pytest.raises(OperationSpecError):
        make_error(
            family="demo",
            operation="demo.run",
            code="unknown_domain",
            message="bad",
            operation_spec=spec,
        )
    with pytest.raises(OperationSpecError):
        make_error(
            family="demo",
            operation="demo.run",
            code="known_domain",
            message="bad",
        )


def test_dispatch_missing_unknown_passthrough_raw_and_exception_logger():
    registry = OperationRegistry("demo", "Demo operations.")

    async def raw(_params):
        return "RAW"

    registry.register(_spec(handler=raw))
    missing = asyncio.run(dispatch_operation(registry, None, None))
    assert missing["error"]["code"] == "missing_verb"
    assert "run —" in missing["error"]["message"]

    unknown = asyncio.run(dispatch_operation(registry, "wat", {}))
    assert unknown["error"]["code"] == "unknown_operation"
    assert asyncio.run(dispatch_operation(registry, "run", {})) == "RAW"

    async def boom(_params):
        raise RuntimeError("kaboom")

    failing = OperationRegistry("demo", "Demo operations.")
    failing.register(_spec(handler=boom))
    logged = []
    result = asyncio.run(
        dispatch_operation(failing, "run", {}, error_logger=logged.append)
    )
    assert result["error"]["code"] == "operation_failed"
    assert result["error"]["message"] == "RuntimeError: kaboom"
    assert logged and "demo.run failed" in logged[0]


# ---------------------------------------------------------------------------
# 9a ([ap:G2UALX]) — the empty-output stub at the one public funnel. A public
# verb whose whole result is a bare whitespace-only string returns the literal
# "(empty)"; JSON envelopes are out by construction.
# ---------------------------------------------------------------------------
def test_stub_empty_text_result_pure_shapes():
    assert EMPTY_RESULT_STUB == "(empty)"
    assert stub_empty_text_result("") == "(empty)"
    assert stub_empty_text_result("   \n\t ") == "(empty)"
    assert stub_empty_text_result("body") == "body"
    payload = {"success": True, "count": 0}
    assert stub_empty_text_result(payload) is payload
    assert stub_empty_text_result(None) is None
    assert stub_empty_text_result([]) == []


def test_dispatch_stubs_whitespace_only_string_result():
    async def blank(_params):
        return ""

    registry = OperationRegistry("demo", "Demo operations.")
    registry.register(_spec(handler=blank))
    assert asyncio.run(dispatch_operation(registry, "run", {})) == "(empty)"


def test_dispatch_stubs_whitespace_only_string_result_blank_chars():
    async def whitespace(_params):
        return "  \n\t  "

    registry = OperationRegistry("demo", "Demo operations.")
    registry.register(_spec(handler=whitespace))
    assert asyncio.run(dispatch_operation(registry, "run", {})) == "(empty)"


def test_dispatch_non_empty_string_and_dicts_pass_through_untouched():
    async def text(_params):
        return "body"

    async def envelope(_params):
        return {"success": True, "count": 0}

    async def failure(_params):
        return {"success": False, "error": {"code": "x", "message": "m"}}

    text_registry = OperationRegistry("demo", "Demo operations.")
    text_registry.register(_spec(handler=text))
    assert asyncio.run(dispatch_operation(text_registry, "run", {})) == "body"

    ok_registry = OperationRegistry("demo", "Demo operations.")
    ok_registry.register(_spec(handler=envelope))
    assert asyncio.run(dispatch_operation(ok_registry, "run", {})) == {
        "success": True,
        "count": 0,
    }

    fail_registry = OperationRegistry("demo", "Demo operations.")
    fail_registry.register(_spec(handler=failure))
    result = asyncio.run(dispatch_operation(fail_registry, "run", {}))
    # An explicit success:false dict gets the operation_doc attachment on the
    # failure-documentation path, never the stub: JSON is out by construction.
    assert result["success"] is False
    assert result["error"]["code"] == "x"


def test_dispatch_stub_runs_before_result_finalizer():
    # Sequencing caveat C1: the stub replaces the blank body on the RAW
    # handler result, BEFORE the finalizer, so a finalizer that prepends
    # advisory/correction lines above the body can never hide an empty body
    # from the stub.
    seen = {}

    def _finalizer(_spec, result):
        seen["result"] = result
        return result

    async def blank(_params):
        return "  \n"

    registry = OperationRegistry("demo", "Demo operations.")
    registry.register(_spec(handler=blank, cache_backed=True))
    out = asyncio.run(
        dispatch_operation(registry, "run", {}, result_finalizer=_finalizer)
    )
    assert out == "(empty)"
    assert seen["result"] == "(empty)"


def test_dispatch_exception_envelope_never_stubbed():
    async def boom(_params):
        raise RuntimeError("kaboom")

    failing = OperationRegistry("demo", "Demo operations.")
    failing.register(_spec(handler=boom))
    result = asyncio.run(dispatch_operation(failing, "run", {}))
    assert isinstance(result, dict)
    assert result["error"]["code"] == "operation_failed"


def test_registry_metadata_is_immutable_in_both_directions():
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "additionalProperties": False,
    }
    returns = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    example = {"call": {"verb": "run", "params": {"value": "x"}}, "note": "x"}
    spec = _spec(params_schema=schema, returns=returns, examples=(example,))
    registry = OperationRegistry("demo", "Demo operations.")
    registry.register(spec)

    schema["properties"]["value"]["type"] = "integer"
    returns["properties"]["ok"]["type"] = "string"
    example["call"]["params"]["value"] = "mutated"

    first = registry.describe_full("run")
    assert first["params_schema"]["properties"]["value"]["type"] == "string"
    assert first["returns"]["properties"]["ok"]["type"] == "boolean"
    assert first["examples"][0]["call"]["params"]["value"] == "x"

    first["params_schema"]["properties"]["value"]["type"] = "number"
    first["examples"][0]["note"] = "changed"
    second = registry.describe_full("run")
    assert second["params_schema"]["properties"]["value"]["type"] == "string"
    assert second["examples"][0]["note"] == "x"


def test_param_aliases_apply_before_validation_and_stay_out_of_describe():
    registry = OperationRegistry("demo", "Demo operations.")
    registry.register(
        _spec(
            params_schema={
                "type": "object",
                "properties": {"node_handle": {"type": "string"}},
                "required": ["node_handle"],
                "additionalProperties": False,
            },
            param_aliases={"node_id": "node_handle"},
        )
    )

    assert asyncio.run(
        dispatch_operation(registry, "run", {"node_id": "abc"})
    ) == {"node_handle": "abc"}
    full = registry.describe_full("run")
    assert "node_id" not in full["params_schema"]["properties"]
    assert full["params_schema"]["required"] == ["node_handle"]
    assert "legacy_field_aliases" in registry.describe_index()

    conflict = asyncio.run(
        dispatch_operation(
            registry,
            "run",
            {"node_id": "legacy", "node_handle": "canonical"},
        )
    )
    assert conflict["error"]["code"] == "forbidden_parameter"
    assert "node_id" in conflict["error"]["message"]
    assert "node_handle" in conflict["error"]["message"]


@pytest.mark.parametrize(
    "aliases",
    [
        {"node_id": "missing"},
        {"node_handle": "node_handle"},
        {"": "node_handle"},
        {"node_id": ""},
        None,
    ],
)
def test_param_alias_registration_validation(aliases):
    with pytest.raises(OperationSpecError):
        _spec(
            params_schema={
                "type": "object",
                "properties": {"node_handle": {"type": "string"}},
                "additionalProperties": False,
            },
            param_aliases=aliases,
        )


def test_harmless_ignored_params_drop_before_validation_and_stay_out_of_describe():
    """Unit B mechanism pair-mirror of the alias test above: registered keys
    pass the gate and are dropped -- never forwarded, whatever their value."""
    registry = OperationRegistry("demo", "Demo operations.")
    registry.register(
        _spec(
            params_schema={
                "type": "object",
                "properties": {"dock_handle": {"type": "string"}},
                "required": ["dock_handle"],
                "additionalProperties": False,
            },
            param_harmless_ignored=("headings_only",),
        )
    )

    # The echo handler returns exactly the params it received, so each result
    # proves the harmless key never reached the handler -- for ANY value.
    for value in (True, False, None, "nonsense", 0, {"nested": True}):
        assert asyncio.run(
            dispatch_operation(
                registry, "run", {"dock_handle": "D", "headings_only": value}
            )
        ) == {"dock_handle": "D"}

    # An unrelated unknown key is still rejected by the same gate.
    forbidden = asyncio.run(
        dispatch_operation(registry, "run", {"dock_handle": "D", "bogus": 1})
    )
    assert forbidden["error"]["code"] == "forbidden_parameter"

    # Invisible on every describe surface -- and a harmless-only registry gets
    # NO legacy_field_aliases index note (the slips discipline, not the
    # legacy aliases' one: silent acceptance is the feature).
    full = registry.describe_full("run")
    assert "headings_only" not in full["params_schema"]["properties"]
    assert "headings_only" not in json.dumps(full, default=str)
    index = registry.describe_index()
    assert "headings_only" not in json.dumps(index, default=str)
    assert "legacy_field_aliases" not in index


@pytest.mark.parametrize(
    "harmless",
    [
        ("dock_handle",),                    # collides with a canonical property
        ("node_id",),                         # collides with a param alias key
        ("headings_only", "headings_only"),   # duplicate entry
        ("",),                                # empty name
        (None,),                              # non-string entry
        None,                                 # not iterable
        42,                                   # not iterable
    ],
)
def test_harmless_ignored_registration_validation(harmless):
    with pytest.raises(OperationSpecError):
        _spec(
            params_schema={
                "type": "object",
                "properties": {"dock_handle": {"type": "string"}},
                "additionalProperties": False,
            },
            param_aliases={"node_id": "dock_handle"},
            param_harmless_ignored=harmless,
        )


def test_harmless_ignored_default_is_empty_and_entries_normalize_to_tuple():
    assert _spec().param_harmless_ignored == ()
    spec = _spec(param_harmless_ignored=["headings_only"])
    assert spec.param_harmless_ignored == ("headings_only",)


def test_job_and_mutation_ack_helpers_preserve_normalized_shapes():
    assert make_job_record(
        operation="node.etch",
        job_id="etch-7",
        state="started",
        extra={"kind": "etch"},
    ) == {
        "success": True,
        "operation": "node.etch",
        "job": {"id": "etch-7", "state": "started", "kind": "etch"},
    }

    ledger = {
        "success": False,
        "partial_success": True,
        "nodes_changed": ["A"],
        "results": [{"status": "committed"}, {"status": "error"}],
    }
    ack = make_mutation_ack(
        operation="tag.batch",
        result=ledger,
        changed=True,
        warnings=("partial",),
    )
    assert ack == {
        "success": False,
        "operation": "tag.batch",
        "changed": True,
        "warnings": ["partial"],
        "detail": ledger,
    }
    assert ack["detail"] is ledger

    noop = make_mutation_ack(
        operation="tag.add",
        result={"success": True, "no_op": True},
        changed=False,
    )
    assert noop["success"] is True
    assert noop["changed"] is False
