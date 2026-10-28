# @beacon[
#   id=package-loading@init-reexports,
#   role=package import surface and mcp re-export,
#   slice_labels=nexus-loading-flow,
#   kind=span,
#   show_span=true,
#   comment=Import-time package surface. All re-exports are LAZY (PEP 562 __getattr__) so importing dependency-light subpackages (e.g. workflowy_mcp.coherence) does NOT pull server.py/FastMCP; 'from workflowy_mcp import mcp' still works unchanged.,
# ]
"""WorkFlowy MCP Server - Model Context Protocol server for WorkFlowy API integration."""

__version__ = "1.0.1"

# Lazy re-export table (PEP 562).  Importing the package itself must not
# instantiate FastMCP, require credentials, or start server machinery — the
# coherence foundation (S01 §14) and other dependency-light subpackages
# inherit the parent __init__ on every import.
_LAZY_EXPORTS = {
    "mcp": (".server", "mcp"),
    "WorkFlowyNode": (".models", "WorkFlowyNode"),
    "NodeCreateRequest": (".models", "NodeCreateRequest"),
    "NodeUpdateRequest": (".models", "NodeUpdateRequest"),
    "NodeListRequest": (".models", "NodeListRequest"),
    "APIConfiguration": (".models", "APIConfiguration"),
    "WorkFlowyClient": (".client", "WorkFlowyClient"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    "mcp",
    "WorkFlowyNode",
    "NodeCreateRequest",
    "NodeUpdateRequest",
    "NodeListRequest",
    "APIConfiguration",
    "WorkFlowyClient",
]
# @beacon-close[
#   id=package-loading@init-reexports,
# ]
