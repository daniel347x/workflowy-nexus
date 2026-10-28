"""Defect 1 -- the flag-switchable published ``params`` schema (UnitSpec §6.1-6.3, §9.1).

What is locked here
-------------------
* **Flag resolution** (``WORKFLOWY_MCP_PARAMS_SCHEMA``): ``object``/``union``
  case- and whitespace-insensitive; blank/whitespace/absent falls through to
  the ``object`` default; an invalid non-blank value raises -- at import, in a
  fresh process, loudly.
* **Published-schema snapshots** for ALL EIGHT consolidated tools (and the
  six family tools + legacy render surfaces of the legacy profile) under BOTH
  flag values, through the fresh-process ``dump_tool_surface`` rail.  Six
  change in object mode; everything else is byte-identical across modes.
* **Runtime acceptance under object mode through the REAL FastMCP call path**
  (in-memory ``fastmcp.Client`` -> JSON-RPC -> ``FunctionTool.run`` ->
  pydantic ``TypeAdapter`` -> ``_normalize_params``): dict, JSON-string-of-
  object, null and omitted all reach the body and normalize; ``""`` and a
  non-object string are the LOUD structured ``malformed_params``; a list is
  rejected by FastMCP before the body.  No ``""`` -> ``{}`` coercion anywhere.
* **No-argument verbs with ``params={}``** and their ``describe`` examples.
* **Text follows the schema**: the six in-scope descriptions' ``describe``
  hint line shows the ``params={...}`` convention in object mode and is
  byte-identical to the pre-flag text in union mode; ``_SERVER_INSTRUCTIONS``
  and the ``glimpse``/``lightning_rod`` descriptions are unchanged in both.

Change-detecting locks vs preservation assertions
-------------------------------------------------
Per the spec's anti-test-theater rule the two kinds are kept apart so a
pre-fix run is red on the RIGHT tests only.  Symbols that do not exist
pre-fix (``resolve_params_schema_mode``, ``OBJECT_PARAMS_SCHEMA``,
``ParamsSchemaTransform`` ...) are imported INSIDE the change-detecting tests,
never at module level, so the preservation assertions (union snapshots,
render-tool baselines, ``describe`` examples) stay green pre-fix instead of
failing through an incidental ImportError.

Model: fresh-process dumps + one in-memory client; no network, no real
Workflowy.  The in-memory client bypasses the server ``lifespan`` (which
needs credentials and a broker) -- the runtime-acceptance path under test
(``FunctionTool.run`` -> ``_normalize_params``) does not depend on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from workflowy_mcp import server
from workflowy_mcp.tool_surface_measurement import dump_tool_surface


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_VAR = "WORKFLOWY_MCP_PARAMS_SCHEMA"
IN_SCOPE = {"anchor", "node", "tag", "beacon", "system", "slice_group"}
MODES = ("object", "union")
PROFILES = ("consolidated", "legacy")

# §6.2 -- exact canonical (sort_keys) JSON forms.
OBJECT_SCHEMA_JSON = (
    '{"additionalProperties":false,"properties":{"params":{"additionalProperties":true,'
    '"type":"object"},"verb":{"type":"string"}},"required":["verb","params"],"type":"object"}'
)
UNION_SCHEMA_JSON = (
    '{"additionalProperties":false,"properties":{"params":{"anyOf":[{"additionalProperties":true,'
    '"type":"object"},{"type":"string"},{"type":"null"}],"default":null},"verb":{"type":"string"}},'
    '"required":["verb"],"type":"object"}'
)
RENDER_SCHEMA_JSON = (
    '{"additionalProperties":false,"properties":{"params":{"anyOf":[{"additionalProperties":true,'
    '"type":"object"},{"type":"string"},{"type":"null"}],"default":null},"verb":{"anyOf":[{"type":'
    '"string"},{"type":"null"}],"default":null}},"type":"object"}'
)

# §9.1 text locks -- the six hint lines, per mode, with each tool's own tail.
OBJECT_HINT = (
    "\nUse verb='describe' with params={'detail':'index'} or "
    "params={'of':'OPERATION','detail':'full'}"
)
UNION_HINT = "\nUse verb='describe' with detail='index' or of=OPERATION, detail='full'"
HINT_TAILS = {
    "system": ".",
    "anchor": " -- required for nested payloads (point.capture nodes, point.extract ranges).",
    "slice_group": " -- required for the nested lens (LensSpec) payload.",
    "node": " -- required for nested payloads (nodes, note_edits).",
    "tag": " -- required for the nested operations payload.",
    "beacon": " -- required for nested payloads (operations, symbols).",
}
RENDER_HINT_TAIL = UNION_HINT + "."


def _canon(schema) -> str:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


@contextlib.contextmanager
def _mode_env(mode: str | None):
    """Set/unset the flag in THIS process's environment (dump_tool_surface copies it)."""
    before = os.environ.get(ENV_VAR)
    try:
        if mode is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = mode
        yield
    finally:
        if before is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = before


@lru_cache(maxsize=None)
def _surface(profile: str, mode: str) -> dict[str, dict]:
    """Fresh-process tools/list for one (profile, mode) cell, keyed by tool name."""
    with _mode_env(mode):
        tools = dump_tool_surface(profile, cwd=str(REPO_ROOT))
    return {tool["name"]: tool for tool in tools}


# ---------------------------------------------------------------------------
# §6.1 flag resolution (change-detecting: the symbols do not exist pre-fix)
# ---------------------------------------------------------------------------


def test_flag_constants_and_normalizer():
    from workflowy_mcp import config as cfg

    assert cfg._PARAMS_SCHEMA_ENV_VAR == ENV_VAR
    assert cfg._VALID_PARAMS_SCHEMA_MODES == ("object", "union")
    assert cfg.normalize_params_schema_mode("object") == "object"
    assert cfg.normalize_params_schema_mode("  OBJECT \t") == "object"
    assert cfg.normalize_params_schema_mode("Union\n") == "union"
    with pytest.raises(ValueError) as excinfo:
        cfg.normalize_params_schema_mode("strict")
    assert "object, union" in str(excinfo.value)
    with pytest.raises(ValueError):
        cfg.normalize_params_schema_mode("json")


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("object", "object"),
        ("union", "union"),
        ("  UNION  ", "union"),
        ("Object", "object"),
        (None, "object"),  # absent -> default
        ("", "object"),  # blank -> unset -> default
        ("   \t ", "object"),  # whitespace -> unset -> default
    ],
)
def test_resolve_params_schema_mode_truth_table(monkeypatch, env_value, expected):
    from workflowy_mcp.config import resolve_params_schema_mode

    if env_value is None:
        monkeypatch.delenv(ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(ENV_VAR, env_value)
    assert resolve_params_schema_mode() == expected


@pytest.mark.parametrize("bad", ["strict", "json", "OBJ", "1"])
def test_resolve_params_schema_mode_invalid_non_blank_raises(monkeypatch, bad):
    from workflowy_mcp.config import resolve_params_schema_mode

    monkeypatch.setenv(ENV_VAR, bad)
    with pytest.raises(ValueError, match="Invalid MCP params schema mode"):
        resolve_params_schema_mode()


def test_invalid_mode_crashes_import_loudly_without_api_credentials():
    """§6.1 / invariant 6: fail-loud at import, in a fresh process, no credentials."""
    env = os.environ.copy()
    env[ENV_VAR] = "strict"
    env.pop("WORKFLOWY_API_KEY", None)
    completed = subprocess.run(
        [sys.executable, "-c", "import workflowy_mcp.server"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    assert "Invalid MCP params schema mode" in combined
    assert "object, union" in combined


def test_server_binds_mode_at_import_and_default_is_object():
    # The pytest process imports server under the ambient environment; the
    # suite's contract is the default (unset) environment, exactly like
    # test_default_profile_is_consolidated_eight_tool_surface.
    assert server.PARAMS_SCHEMA_MODE == "object"


# ---------------------------------------------------------------------------
# Transform unit behaviour (change-detecting)
# ---------------------------------------------------------------------------


class _FakeTool:
    def __init__(self, name, parameters):
        self.name = name
        self.parameters = parameters


class _FakeMcp:
    def __init__(self):
        self.transforms = []

    def add_transform(self, transform):
        self.transforms.append(transform)


def test_object_params_schema_constant_is_the_exact_spec_shape():
    from workflowy_mcp.transforms import OBJECT_PARAMS_SCHEMA, PARAMS_SCHEMA_IN_SCOPE_TOOLS

    assert _canon(OBJECT_PARAMS_SCHEMA) == OBJECT_SCHEMA_JSON
    assert set(PARAMS_SCHEMA_IN_SCOPE_TOOLS) == IN_SCOPE
    assert "glimpse" not in PARAMS_SCHEMA_IN_SCOPE_TOOLS
    assert "lightning_rod" not in PARAMS_SCHEMA_IN_SCOPE_TOOLS


def test_transform_rewrites_only_in_scope_tools_with_fresh_copies():
    from workflowy_mcp.transforms import OBJECT_PARAMS_SCHEMA, ParamsSchemaTransform

    generated = json.loads(UNION_SCHEMA_JSON)
    render = json.loads(RENDER_SCHEMA_JSON)
    tools = [
        *[_FakeTool(name, dict(generated)) for name in sorted(IN_SCOPE)],
        _FakeTool("glimpse", dict(render)),
        _FakeTool("lightning_rod", dict(render)),
        _FakeTool("workflowy_glimpse", {"type": "object", "properties": {"x": {}}}),
    ]
    out = asyncio.run(ParamsSchemaTransform().list_tools(tools))
    assert out is tools
    for tool in tools:
        if tool.name in IN_SCOPE:
            assert _canon(tool.parameters) == OBJECT_SCHEMA_JSON
            assert tool.parameters is not OBJECT_PARAMS_SCHEMA  # a copy per tool
        elif tool.name in {"glimpse", "lightning_rod"}:
            assert _canon(tool.parameters) == RENDER_SCHEMA_JSON
        else:
            assert tool.parameters == {"type": "object", "properties": {"x": {}}}
    in_scope_dicts = [t.parameters for t in tools if t.name in IN_SCOPE]
    assert len({id(d) for d in in_scope_dicts}) == len(in_scope_dicts)


def test_activator_attaches_only_in_object_mode():
    from workflowy_mcp.transforms import ParamsSchemaTransform, apply_params_schema_transform

    union_mcp = _FakeMcp()
    assert apply_params_schema_transform(union_mcp, "union") is False
    assert union_mcp.transforms == []

    object_mcp = _FakeMcp()
    assert apply_params_schema_transform(object_mcp, "object") is True
    assert len(object_mcp.transforms) == 1
    assert isinstance(object_mcp.transforms[0], ParamsSchemaTransform)


# ---------------------------------------------------------------------------
# §6.2 / §9.1 published-schema snapshots -- all eight, both modes, both profiles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_object_mode_publishes_required_open_object_on_the_six(profile):
    """CHANGE-DETECTING LOCK (red pre-fix): the six advertise params as a
    REQUIRED open object; never an anyOf while mode is object."""
    surface = _surface(profile, "object")
    for name in sorted(IN_SCOPE):
        schema = surface[name]["inputSchema"]
        assert _canon(schema) == OBJECT_SCHEMA_JSON, name
        assert "anyOf" not in json.dumps(schema)
        assert schema["properties"]["params"] == {
            "type": "object",
            "additionalProperties": True,
        }  # open: no per-verb fields


@pytest.mark.parametrize("profile", PROFILES)
def test_union_mode_six_are_byte_identical_to_pre_fix_shape(profile):
    """PRESERVATION (may be green pre-fix): union mode == today's wire shape."""
    surface = _surface(profile, "union")
    for name in sorted(IN_SCOPE):
        assert _canon(surface[name]["inputSchema"]) == UNION_SCHEMA_JSON, name


@pytest.mark.parametrize("mode", MODES)
def test_consolidated_render_tools_byte_identical_in_both_modes(mode):
    """PRESERVATION: glimpse / lightning_rod keep verb optional + the union
    params arm in BOTH modes (the zero-argument live-DOM rule survives)."""
    surface = _surface("consolidated", mode)
    assert set(surface) == IN_SCOPE | {"glimpse", "lightning_rod"}
    for name in ("glimpse", "lightning_rod"):
        assert _canon(surface[name]["inputSchema"]) == RENDER_SCHEMA_JSON, (name, mode)


@pytest.mark.parametrize("profile", PROFILES)
def test_only_the_six_differ_between_modes(profile):
    """Within each profile: the tool SET is mode-independent, the six differ
    exactly by the §6.2 swap, and EVERY other tool (legacy render surfaces
    included, on their own baselines) is byte-identical across modes."""
    obj = _surface(profile, "object")
    uni = _surface(profile, "union")
    assert set(obj) == set(uni)
    assert IN_SCOPE <= set(obj)
    for name in sorted(obj):
        o = _canon(obj[name]["inputSchema"])
        u = _canon(uni[name]["inputSchema"])
        if name in IN_SCOPE:
            assert (o, u) == (OBJECT_SCHEMA_JSON, UNION_SCHEMA_JSON), name
        else:
            assert o == u, name


def test_legacy_render_surfaces_keep_their_own_baselines():
    """PRESERVATION: legacy workflowy_glimpse and legacy lightning_rod (with
    its `mode` property) are untouched -- not required to share the
    consolidated fixture, just required not to move."""
    for mode in MODES:
        legacy = _surface("legacy", mode)
        assert "workflowy_glimpse" in legacy
        assert "glimpse" not in legacy
        assert "mode" in legacy["lightning_rod"]["inputSchema"]["properties"]
        for name in ("workflowy_glimpse", "lightning_rod"):
            assert "params" not in (legacy[name]["inputSchema"].get("required") or [])


# ---------------------------------------------------------------------------
# §6.3 runtime acceptance under object mode -- the REAL FastMCP call path
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _noop_lifespan(app):
    # The server lifespan needs credentials + a broker; the argument path under
    # test (FunctionTool.run -> TypeAdapter -> _normalize_params) does not.
    yield {}


@pytest.fixture
def real_client(monkeypatch):
    # No mode precondition HERE: the runtime-acceptance tests below are
    # preservation assertions (runtime is mode-independent by invariant 2) and
    # must stay runnable pre-fix. That this process IS in object mode is locked
    # separately by test_server_binds_mode_at_import_and_default_is_object and
    # by test_wire_input_schema_seen_by_a_real_client_is_the_object_form.
    from fastmcp import Client

    monkeypatch.setattr(server.mcp, "_lifespan", _noop_lifespan)
    return Client(server.mcp)


def _run(coro):
    return asyncio.run(coro)


def test_wire_input_schema_seen_by_a_real_client_is_the_object_form(real_client):
    """CHANGE-DETECTING LOCK: the schema a real MCP client receives over the
    protocol (not Tool.parameters read in-process) is the §6.2 object form for
    the six, and the render form for glimpse / lightning_rod."""

    assert server.PARAMS_SCHEMA_MODE == "object"

    async def go():
        async with real_client as c:
            return {t.name: t.input_schema for t in await c.list_tools()}

    schemas = _run(go())
    for name in sorted(IN_SCOPE):
        assert _canon(schemas[name]) == OBJECT_SCHEMA_JSON, name
    for name in ("glimpse", "lightning_rod"):
        assert _canon(schemas[name]) == RENDER_SCHEMA_JSON, name


async def _call(client, args, *, raise_on_error=True):
    async with client as c:
        return await c.call_tool("system", args, raise_on_error=raise_on_error)


def _describe_index_ok(result):
    assert result.is_error is False
    payload = result.structured_content
    assert payload["family"] == "system"
    assert payload.get("success") is not False
    verbs = {op["verb"] for op in payload["operations"]}
    assert {"ping", "describe"} <= verbs


@pytest.mark.parametrize(
    ("label", "args"),
    [
        ("dict", {"verb": "describe", "params": {"detail": "index"}}),
        ("json-string-of-object", {"verb": "describe", "params": '{"detail":"index"}'}),
        ("null", {"verb": "describe", "params": None}),
        ("omitted", {"verb": "describe"}),
        ("empty-object", {"verb": "describe", "params": {}}),
    ],
)
def test_object_mode_runtime_accepts_dict_string_null_and_omitted(real_client, label, args):
    """§6.3 rows 1-4 (+ the `{}` no-arg form): each reaches the body and
    normalizes to the describe index.  `omitted` proves the advertised
    `required` is advertisement only -- today's tolerance is preserved."""
    _describe_index_ok(_run(_call(real_client, args)))


@pytest.mark.parametrize(
    ("label", "params", "message_fragment"),
    [
        ("empty-string", "", "not valid JSON"),
        ("non-object-string", "hello", "not valid JSON"),
        ("json-number-string", "123", "must decode to an object"),
        ("json-list-string", "[1,2]", "must decode to an object"),
    ],
)
def test_object_mode_runtime_rejects_non_object_strings_loudly(
    real_client, label, params, message_fragment
):
    """§6.3 rows 5-6: the string arm still reaches the kernel and the kernel
    still says `malformed_params` -- LOUD, structured, never `""` -> `{}`."""
    result = _run(_call(real_client, {"verb": "describe", "params": params}, raise_on_error=False))
    assert result.is_error is False  # a structured envelope, not a protocol error
    payload = result.structured_content
    assert payload["success"] is False
    assert payload["error"]["code"] == "malformed_params"
    assert message_fragment in payload["error"]["message"]
    assert "operations" not in payload  # it did NOT quietly become the index


def test_object_mode_runtime_rejects_a_list_before_the_body(real_client):
    """§6.3 row 7: a list fails FastMCP's signature validation -> ToolError."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as excinfo:
        _run(_call(real_client, {"verb": "describe", "params": [1, 2]}))
    assert "validation error" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# §9.1 no-argument verbs with params={} and their describe examples
# ---------------------------------------------------------------------------


def test_no_argument_verbs_succeed_with_empty_params(real_client, monkeypatch):
    # ping and describe run for real. broker.status / cache.refresh need a live
    # Workflowy client + broker; their impls are stubbed so what is asserted is
    # the KERNEL accepting `{}` on a zero-parameter schema and reaching the
    # handler -- not broker liveness.
    reached = []

    async def _fake_broker_status():
        reached.append("broker.status")
        return {"success": True, "stub": True}

    async def _fake_cache_refresh():
        reached.append("cache.refresh")
        return {"success": True, "stub": True}

    monkeypatch.setattr(server, "_system_broker_status_impl", _fake_broker_status)
    monkeypatch.setattr(server, "_system_cache_refresh_impl", _fake_cache_refresh)

    async def go():
        out = {}
        async with real_client as c:
            for verb in ("ping", "broker.status", "cache.refresh", "describe"):
                r = await c.call_tool("system", {"verb": verb, "params": {}})
                out[verb] = r.structured_content
        return out

    out = _run(go())
    assert out["ping"]["ok"] is True
    assert out["broker.status"]["success"] is True
    assert out["cache.refresh"]["success"] is True
    assert out["describe"]["family"] == "system"
    assert reached == ["broker.status", "cache.refresh"]


def test_describe_full_examples_of_no_argument_verbs_carry_empty_params(real_client):
    async def go():
        out = {}
        async with real_client as c:
            for verb in ("ping", "broker.status", "cache.refresh"):
                r = await c.call_tool(
                    "system", {"verb": "describe", "params": {"of": verb, "detail": "full"}}
                )
                out[verb] = r.structured_content
            idx = await c.call_tool("system", {"verb": "describe", "params": {"detail": "index"}})
            out["__index__"] = idx.structured_content
        return out

    out = _run(go())
    for verb in ("ping", "broker.status", "cache.refresh"):
        examples = out[verb]["examples"]
        assert examples, verb
        for example in examples:
            assert example["call"]["params"] == {}, (verb, example)
    assert out["__index__"]["describe_usage"] == {
        "index": {"verb": "describe", "params": {"detail": "index"}},
        "full": {"verb": "describe", "params": {"of": "OPERATION", "detail": "full"}},
    }


# ---------------------------------------------------------------------------
# §9.1 text follows the schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(IN_SCOPE))
def test_object_mode_hint_line_shows_params_convention(name):
    """CHANGE-DETECTING LOCK (one per in-scope tool; red pre-fix): in object
    mode the description ends with the params={...} hint line."""
    desc = _surface("consolidated", "object")[name]["description"]
    assert desc.endswith(OBJECT_HINT + HINT_TAILS[name]), name
    assert UNION_HINT not in desc


@pytest.mark.parametrize("name", sorted(IN_SCOPE))
def test_union_mode_hint_line_is_byte_identical_to_today(name):
    """PRESERVATION: union mode carries today's hint line verbatim."""
    desc = _surface("consolidated", "union")[name]["description"]
    assert desc.endswith(UNION_HINT + HINT_TAILS[name]), name
    assert OBJECT_HINT not in desc


@pytest.mark.parametrize("profile", PROFILES)
def test_descriptions_differ_between_modes_only_by_the_hint_line(profile):
    obj = _surface(profile, "object")
    uni = _surface(profile, "union")
    for name in sorted(obj):
        if name in IN_SCOPE:
            assert uni[name]["description"].replace(UNION_HINT, OBJECT_HINT) == obj[name][
                "description"
            ], name
            assert uni[name]["description"] != obj[name]["description"], name
        else:
            assert uni[name]["description"] == obj[name]["description"], name


@pytest.mark.parametrize("mode", MODES)
def test_render_tool_descriptions_unchanged_in_both_modes(mode):
    surface = _surface("consolidated", mode)
    for name in ("glimpse", "lightning_rod"):
        assert surface[name]["description"].endswith(RENDER_HINT_TAIL), (name, mode)
        assert OBJECT_HINT not in surface[name]["description"]


def test_in_process_description_constants_follow_the_bound_mode():
    """The six module constants agree with the wire (object mode in-process)."""
    constants = {
        "system": server._SYSTEM_TOOL_DESCRIPTION,
        "anchor": server._ANCHOR_TOOL_DESCRIPTION,
        "slice_group": server._SLICE_GROUP_TOOL_DESCRIPTION,
        "node": server._NODE_TOOL_DESCRIPTION,
        "tag": server._TAG_TOOL_DESCRIPTION,
        "beacon": server._BEACON_TOOL_DESCRIPTION,
    }
    assert server.PARAMS_SCHEMA_MODE == "object"
    for name, desc in constants.items():
        assert desc.endswith(OBJECT_HINT + HINT_TAILS[name]), name
    for desc in (server._GLIMPSE_TOOL_DESCRIPTION, server._LIGHTNING_ROD_TOOL_DESCRIPTION):
        assert desc.endswith(RENDER_HINT_TAIL)


def _server_instructions_in_fresh_process(mode: str) -> str:
    env = os.environ.copy()
    env[ENV_VAR] = mode
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("WORKFLOWY_API_KEY", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, workflowy_mcp.server as s; print(json.dumps(s._SERVER_INSTRUCTIONS))",
        ],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads([line for line in completed.stdout.splitlines() if line.strip()][-1])


def test_server_instructions_unchanged_in_both_modes():
    """PRESERVATION (MUST DISPOSITION §14): _SERVER_INSTRUCTIONS already carries
    the params={...} convention and no bare family(verb='x') example; it is
    identical in both modes and identical to the in-process constant."""
    obj = _server_instructions_in_fresh_process("object")
    uni = _server_instructions_in_fresh_process("union")
    assert obj == uni == server._SERVER_INSTRUCTIONS
    assert "params={'detail':'index'}" in obj
    assert "params is an open object" in obj
    for family in sorted(IN_SCOPE | {"glimpse", "lightning_rod"}):
        assert f"{family}(verb=" not in obj
