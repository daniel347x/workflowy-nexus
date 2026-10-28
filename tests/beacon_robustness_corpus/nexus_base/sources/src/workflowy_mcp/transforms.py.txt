# @beacon[
#   id=transforms@module,
#   slice_labels=nexus-loading-flow,
#   kind=ast,
#   comment=Server-level FastMCP transforms applied at startup; hosts the
#     Claude-Desktop-Windows outputSchema workaround and the mode-selected
#     published-params-schema override (ParamsSchemaTransform) for the six family tools,
# ]
"""FastMCP server-level transforms for the WorkFlowy MCP server.

This module defines :class:`Transform` subclasses that are attached to the
FastMCP server via :py:meth:`fastmcp.FastMCP.add_transform` so that they apply
uniformly to every registered tool, regardless of which @mcp.tool decorator
created it.

Why this module exists
----------------------
Claude Desktop on Windows (as of build 1.0.1768 and later, including 1.8555.2)
ships with a JSON-Schema validator that chokes on complex MCP ``outputSchema``
definitions containing ``$defs`` and ``$ref`` references — exactly the shape
that FastMCP auto-generates from Python return-type hints. When the validator
fails, the entire tool list is silently dropped on the renderer side: the
server is listed in the Connectors panel with "0 tools available", and the
model cannot see or invoke any tool.

This is an Anthropic-acknowledged bug, tracked at
modelcontextprotocol/mcpb#174 and closed there as "not planned" because the
fix belongs in Claude Desktop itself rather than the MCP SDK. It affects
Anthropic's own official servers (``server-postgres``, ``server-filesystem``,
``mcp-server-jetbrains``) on Windows too, not just third-party servers — so
the workaround documented here is the broadly-applicable mitigation until
Claude Desktop ships a fixed schema compiler.

The fix
-------
:class:`StripOutputSchemaTransform` overrides :py:meth:`Transform.list_tools`
to set ``output_schema`` to ``None`` on every tool just before serialization.
This stops FastMCP from emitting an ``outputSchema`` field in the wire
``tools/list`` response, which sidesteps Claude Desktop's broken validator.

Functional impact: none. The MCP SDK only performs output-schema validation
``if tool.outputSchema is not None`` (see ``mcp/server/lowlevel/server.py``
line ~560), so dropping the field simply skips an advisory client-side
validation step. Tools execute identically, structured content is still
returned when produced, and every other MCP client (TypingMind, Claude Code,
Cursor, Cline, etc.) handles the absence of ``outputSchema`` without issue.
"""

from __future__ import annotations

import sys
from typing import Any

# The fastmcp.server.transforms / fastmcp.tools.base Transform API was added
# in FastMCP 3.x. The legacy live install at C:\Temp\workflowy-mcp-vladzima
# currently runs FastMCP 2.13.x (system Python), which has no such module.
# We import defensively so importing this module never breaks the legacy
# install; the helper apply_strip_output_schema_workaround() below silently
# no-ops on FastMCP 2.x and only attaches the transform on FastMCP 3.x.
try:
    from fastmcp.server.transforms import Transform  # type: ignore[import-not-found]
    from fastmcp.tools.base import Tool  # type: ignore[import-not-found]

    _FASTMCP_TRANSFORMS_AVAILABLE = True
except ImportError:
    Transform = object  # type: ignore[misc, assignment]
    Tool = Any  # type: ignore[misc, assignment]
    _FASTMCP_TRANSFORMS_AVAILABLE = False


# @beacon[
#   id=transforms@StripOutputSchemaTransform,
#   slice_labels=nexus-loading-flow,
#   kind=ast,
#   comment=Claude-Desktop-Windows workaround: strip outputSchema from every tool before tools/list serialization (Anthropic bug mcpb#174),
# ]
class StripOutputSchemaTransform(Transform):  # type: ignore[misc, valid-type]
    """Strip ``output_schema`` from every tool before ``tools/list`` is sent.

    Attached to the FastMCP server via ``mcp.add_transform(...)`` so that
    every tool, regardless of which ``@mcp.tool`` decorator defined it, gets
    its ``output_schema`` cleared. This sidesteps the Claude-Desktop-Windows
    JSON-Schema validator bug (modelcontextprotocol/mcpb#174) that drops the
    entire tool list when complex ``outputSchema`` with ``$defs``/``$ref`` is
    present.

    Only available on FastMCP 3.x. On FastMCP 2.x this class still loads
    (inheriting from ``object``) but is never instantiated because the
    add_transform call site is itself gated; see
    :func:`apply_strip_output_schema_workaround` below.

    Safe to leave in place permanently. If Claude Desktop eventually ships a
    fixed validator, this transform becomes a no-op cost (one in-place mutation
    per tool per ``tools/list`` call) and can be removed at any time without
    behavioral change on the client side.
    """

    async def list_tools(self, tools):  # noqa: D401
        for tool in tools:
            # FastMCP's Tool model allows direct attribute mutation; we clear
            # output_schema in place so the subsequent to_mcp_tool() conversion
            # emits ``outputSchema=None`` (which the SDK then omits from the
            # wire JSON entirely, per mcp.types.Tool's Optional[dict] field).
            tool.output_schema = None
        return tools


# @beacon[
#   id=transforms@apply_strip_output_schema_workaround,
#   slice_labels=nexus-loading-flow,
#   kind=ast,
#   comment=Version-aware activator for the outputSchema strip; no-ops on FastMCP 2.x,
# ]
def apply_strip_output_schema_workaround(mcp) -> bool:
    """Attach :class:`StripOutputSchemaTransform` to ``mcp`` if supported.

    Returns True if the transform was attached (FastMCP 3.x install), False
    if the running FastMCP version doesn't expose the Transform API
    (e.g., the legacy FastMCP 2.13.x install used by the daily-driver
    TypingMind connector at C:\\Temp\\workflowy-mcp-vladzima). In the False
    case nothing is mutated; the server runs identically to before this
    workaround was added.

    Args:
        mcp: The FastMCP server instance to mutate.

    Returns:
        bool: True if the transform was attached, False on FastMCP 2.x.
    """
    if not _FASTMCP_TRANSFORMS_AVAILABLE:
        print(
            "[transforms] FastMCP Transform API not available; "
            "StripOutputSchemaTransform NOT attached. "
            "Claude-Desktop-Windows users may see '0 tools available' until "
            "the install is upgraded to FastMCP 3.x.",
            file=sys.stderr,
            flush=True,
        )
        return False

    mcp.add_transform(StripOutputSchemaTransform())
    print(
        "[transforms] \u2705 StripOutputSchemaTransform attached "
        "(Claude-Desktop-Windows outputSchema workaround active)",
        file=sys.stderr,
        flush=True,
    )
    return True


# ---------------------------------------------------------------------------
# Published `params` schema override for the six family tools
# ---------------------------------------------------------------------------
#
# The six progressive-disclosure family tools (anchor, node, tag, beacon,
# system, slice_group) are declared ``params: dict | str | None = None`` so the
# runtime keeps accepting a dict, a JSON string encoding an object, or
# null/omitted (-> ``_normalize_params``).  FastMCP renders that annotation as
# ``params: anyOf[object, string, null], default: null`` and OPTIONAL -- a
# shape under which an in-context-poisoned model was observed omitting its
# arguments entirely (``{"verb": "read"}``) dozens of times in a row, while a
# plain ``type: object`` + ``required`` made the same request emit full
# arguments every time.
#
# FastMCP 4.x separates the PUBLISHED schema (``Tool.parameters`` -> wire
# ``inputSchema``) from RUNTIME validation (a pydantic TypeAdapter built from
# the function signature), so the published shape can be overridden without
# touching a single Python signature -- exactly the seam
# ``StripOutputSchemaTransform`` already exploits for ``output_schema``.
# ``ParamsSchemaTransform`` is that override: in ``object`` mode it rewrites
# the six tools' published ``inputSchema`` to advertise ``params`` as a
# REQUIRED open object; in ``union`` mode it is simply not attached.  The
# object stays open (``additionalProperties: true``, no per-verb fields), so
# progressive disclosure is preserved.  ``glimpse`` and ``lightning_rod`` are
# deliberately out of scope (their ``verb`` is itself optional -- the
# zero-argument live-DOM rule).

#: The six in-scope family tools whose published ``params`` schema is
#: mode-selected. ``glimpse`` and ``lightning_rod`` are NOT in this set.
PARAMS_SCHEMA_IN_SCOPE_TOOLS: frozenset[str] = frozenset(
    {"anchor", "node", "tag", "beacon", "system", "slice_group"}
)

#: The canonical ``object``-mode published ``inputSchema`` for an in-scope
#: family tool.  ``additionalProperties: true`` describes the ``params``
#: property (the open object); the TOP level keeps FastMCP's generated
#: ``additionalProperties: false`` like every other tool.  Copied per tool
#: (``dict(...)``) so no two tools share one mutable mapping.
OBJECT_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verb": {"type": "string"},
        "params": {"type": "object", "additionalProperties": True},
    },
    "required": ["verb", "params"],
    "additionalProperties": False,
}


# @beacon[
#   id=transforms@ParamsSchemaTransform,
#   slice_labels=nexus-loading-flow,nexus--mcp-api,
#   kind=ast,
#   comment=Published-schema-only override: in object mode advertise params as a REQUIRED open object on the six family tools (anchor/node/tag/beacon/system/slice_group); runtime acceptance (dict | JSON-string-of-object | null) is untouched because FastMCP validates from the signature, not from Tool.parameters,
# ]
class ParamsSchemaTransform(Transform):  # type: ignore[misc, valid-type]
    """Advertise ``params`` as a required open object on the six family tools.

    Attached only when the import-time mode is ``object``.  ``list_tools``
    replaces ``tool.parameters`` for every tool whose name is in
    :data:`PARAMS_SCHEMA_IN_SCOPE_TOOLS` with a fresh copy of
    :data:`OBJECT_PARAMS_SCHEMA`; every other tool (including ``glimpse`` and
    ``lightning_rod``) passes through untouched.

    This is the sanctioned in-place ``Tool.parameters`` mutation -- the exact
    parallel of :class:`StripOutputSchemaTransform`'s ``tool.output_schema``
    mutation.  It changes what ``tools/list`` advertises and nothing else:
    ``FunctionTool.run`` validates arguments against the Python signature via
    a cached pydantic ``TypeAdapter``, so a dict, a JSON string encoding an
    object, or ``null``/omitted still reach the handler body exactly as today.
    """

    async def list_tools(self, tools):  # noqa: D401
        for tool in tools:
            if tool.name in PARAMS_SCHEMA_IN_SCOPE_TOOLS:
                tool.parameters = dict(OBJECT_PARAMS_SCHEMA)
        return tools


# @beacon[
#   id=transforms@apply_params_schema_transform,
#   slice_labels=nexus-loading-flow,nexus--mcp-api,
#   kind=ast,
#   comment=Version-aware activator for the params-schema override: attaches ParamsSchemaTransform only when mode == 'object' and the Transform API exists; no-ops (False + stderr line) on FastMCP 2.x, mirroring apply_strip_output_schema_workaround,
# ]
def apply_params_schema_transform(mcp, mode: str) -> bool:
    """Attach :class:`ParamsSchemaTransform` to ``mcp`` when ``mode == 'object'``.

    Mirrors :func:`apply_strip_output_schema_workaround`: returns ``True`` if
    the transform was attached, ``False`` otherwise.  Two ``False`` paths:

    * ``mode == 'union'`` -- the transform is deliberately not attached, so the
      six tools keep FastMCP's generated ``anyOf[object,string,null]`` shape
      (the pre-fix wire schema; the rollback position).
    * the Transform API is unavailable (FastMCP 2.x legacy install) -- nothing
      is mutated and a stderr line says so, so the legacy install is never
      broken.  In that case ``object`` mode is fail-soft: the flag does
      nothing and the six tools keep the union schema.

    Args:
        mcp: The FastMCP server instance to mutate.
        mode: The resolved import-time mode, ``'object'`` or ``'union'``
            (already normalized by ``resolve_params_schema_mode``).

    Returns:
        bool: True if the transform was attached, False otherwise.
    """
    if mode != "object":
        return False

    if not _FASTMCP_TRANSFORMS_AVAILABLE:
        print(
            "[transforms] FastMCP Transform API not available; "
            "ParamsSchemaTransform NOT attached. "
            "WORKFLOWY_MCP_PARAMS_SCHEMA=object has no effect on this install; "
            "the six family tools keep the union params schema.",
            file=sys.stderr,
            flush=True,
        )
        return False

    mcp.add_transform(ParamsSchemaTransform())
    print(
        "[transforms] \u2705 ParamsSchemaTransform attached "
        "(params advertised as a required open object on "
        + ", ".join(sorted(PARAMS_SCHEMA_IN_SCOPE_TOOLS))
        + ")",
        file=sys.stderr,
        flush=True,
    )
    return True
