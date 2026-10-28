# @beacon[
#   id=package-loading@main-module-bootstrap,
#   role=__main__.py import-and-run bootstrap,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=span,
#   show_span=true,
#   comment=Package entrypoint: resolve shim by default or explicit full stdio/HTTP; the shim branch returns before WSL refusal and heavy server import.,
# ]
"""Package entry for Workflowy MCP: the stdio shim by default.

workflowy-mcp and python -m workflowy_mcp use the same selection: shim by default, --transport stdio for the guarded full stdio server, or --transport http for the guarded HTTP daemon. WORKFLOWY_MCP_TRANSPORT supplies the selector when the CLI omits it. workflowy-mcp-shim remains an explicit proxy entry.

Only full transports refuse WSL before importing the app. The shim never imports server.py; WSL shims attach to the native-host daemon without locally spawning it.
"""

from __future__ import annotations

import argparse
import sys


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="workflowy-mcp",
        description="NEXUS Workflowy MCP: stdio shim by default; explicit stdio or http starts a guarded full server.",
    )
    parser.add_argument(
        "--transport",
        metavar="TRANSPORT",
        default=None,
        help="shim (default), stdio (guarded full server), or http (Streamable-HTTP daemon). CLI overrides WORKFLOWY_MCP_TRANSPORT; unknown values warn and use shim.",
    )
    parser.add_argument("--host", default=None, help="HTTP bind host; also the shim dial fallback unless WORKFLOWY_MCP_HTTP_DIAL_HOST is set (default 0.0.0.0).")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (env WORKFLOWY_MCP_HTTP_PORT, default 8766).")
    parser.add_argument("--path", default=None, help="HTTP path (env WORKFLOWY_MCP_HTTP_PATH, default /mcp).")
    return parser.parse_args(argv)


# @beacon[
#   id=package-loading@main-entrypoint,
#   role=main,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Delegates once to shim.main or enters the existing guarded full app after WSL refusal; package and console share this dispatch.,
# ]
def main(argv: list[str] | None = None) -> None:
    """Dispatch to the existing shim by default; refuse WSL before either full-server import."""
    import os

    from workflowy_mcp import daemon as _daemon

    args = _parse_args(argv)
    # CLI flags win over env; push them into env so daemon.py's resolvers
    # (shared with the shim) see one consistent configuration.
    if args.host is not None:
        os.environ[_daemon.HTTP_HOST_ENV_VAR] = args.host
    if args.port is not None:
        os.environ[_daemon.HTTP_PORT_ENV_VAR] = str(args.port)
    if args.path is not None:
        os.environ[_daemon.HTTP_PATH_ENV_VAR] = args.path
    transport = _daemon.resolve_transport(args.transport)

    if transport == "shim":
        from workflowy_mcp import shim as _shim

        _shim.main()
        return

    from workflowy_mcp.server_bootstrap import refuse_wsl_server

    refuse_wsl_server("full server")

    # Import here (not at module top) so `--help` and argument errors never
    # pay for building the whole FastMCP app.
    from workflowy_mcp.server import mcp

    if transport == "http":
        _daemon.run_http_daemon(mcp)
        return

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main(sys.argv[1:])
# @beacon-close[
#   id=package-loading@main-module-bootstrap,
# ]
