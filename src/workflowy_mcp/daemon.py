# @beacon[
#   id=daemon@module,
#   role=single-daemon HTTP transport: URL/token resolution, liveness probe, ensure-running spawn, and the HTTP run entrypoint,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=span,
#   show_span=true,
#   comment=One guarded HTTP execution domain with an owned non-cutting uvicorn runner; shims use their unchanged local installation and retry only an own guard-busy child within the spawn budget.,
# ]
"""Single-daemon HTTP transport for the Workflowy MCP server.

Architecture (Manager-Session-012 ruling, see Workflowy [ap:ORT2S5]/[ap:EL6I7T]):

  * Exactly ONE Workflowy MCP *execution domain* per machine/account: the
    **daemon** -- this same FastMCP server, run with ``transport="http"``
    (Streamable HTTP) instead of stdio. It owns the nodes cache, the write
    governor, Cartographer jobs and the broker membership.
  * Hosts that speak Streamable HTTP (OpenCode, Claude Code, Codex, ...) dial
    the daemon's URL directly.
  * Hosts that only speak stdio (TypingMind's connector, Claude Desktop, ...)
    launch the **shim** (default package entry, or explicit ``workflowy-mcp-shim``): a thin stdio MCP proxy that finds-or-boots the local daemon and forwards every request to it.

This module deliberately mirrors the broker pattern already proven in
``server._ensure_broker_running``: probe the port; if nothing answers, spawn a
detached singleton; everyone else connects as a client. It imports NOTHING
from ``server`` at module level so the shim can stay light (importing
``server`` builds the whole FastMCP app).

Shared secret: the daemon requires a bearer token and reuses the broker's
shared secret (``broker_protocol.get_or_create_token``) -- one local-mesh
secret (env ``WORKFLOWY_BROKER_TOKEN`` or the per-user ``broker_token`` file),
no second file to pair.

Host/Origin protection: FastMCP 4 validates the HTTP ``Host`` header only when
the server binds a loopback address or an explicit ``allowed_hosts`` list is
supplied. The daemon binds ``0.0.0.0`` by default (so WSL can reach it at the
Windows gateway IP, exactly like the broker) and relies on the bearer token
for access control; set ``WORKFLOWY_MCP_HTTP_ALLOWED_HOSTS`` to opt into an
explicit allow-list.

The HTTP entry owns its uvicorn.Server rather than using FastMCP's timed
shutdown runner. It preserves FastMCP's app and reference-counted lifespan;
an active full-server lifecycle can request exit only after quiescence, with
no graceful-shutdown task cut. A disappearing listener is not lock-release
proof: cold-start retries observe only an own child's guard-busy exit.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from typing import Any

# ---------------------------------------------------------------------------
# Configuration surface (env names are the single source of truth here).
# ---------------------------------------------------------------------------
TRANSPORT_ENV_VAR = "WORKFLOWY_MCP_TRANSPORT"          # "shim" (default) | "stdio" | "http"
HTTP_HOST_ENV_VAR = "WORKFLOWY_MCP_HTTP_HOST"          # bind address, default 0.0.0.0
HTTP_PORT_ENV_VAR = "WORKFLOWY_MCP_HTTP_PORT"          # default 8766 (broker is 8765)
HTTP_PATH_ENV_VAR = "WORKFLOWY_MCP_HTTP_PATH"          # default /mcp
HTTP_DIAL_HOST_ENV_VAR = "WORKFLOWY_MCP_HTTP_DIAL_HOST"  # where CLIENTS connect (never a wildcard)
HTTP_ALLOWED_HOSTS_ENV_VAR = "WORKFLOWY_MCP_HTTP_ALLOWED_HOSTS"  # optional, comma-separated
HTTP_STATELESS_ENV_VAR = "WORKFLOWY_MCP_HTTP_STATELESS"  # optional, truthy => stateless_http=True
DAEMON_SPAWN_TIMEOUT_ENV_VAR = "WORKFLOWY_MCP_DAEMON_SPAWN_TIMEOUT_S"  # default 90

DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 8766
DEFAULT_HTTP_PATH = "/mcp"
DEFAULT_SPAWN_TIMEOUT_S = 90.0


def _truthy(raw: str | None) -> bool:
    return bool(raw) and raw.strip().lower() in ("1", "true", "yes", "on")


# @beacon[
#   id=daemon@resolve-transport,
#   role=resolve_transport(): shim|stdio|http from CLI arg or env,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=One transport selector; defaults to the shim and warns rather than exits on unknown values; explicit full transports retain their meanings.,
# ]
def resolve_transport(explicit: str | None = None) -> str:
    """Return shim, stdio or http; explicit CLI wins over env, blank defaults to shim, unknown warns and uses shim."""
    selected = explicit if explicit is not None else os.getenv(TRANSPORT_ENV_VAR)
    raw = (selected or "").strip().lower()
    if raw in ("", "shim"):
        return "shim"
    if raw in ("http", "streamable-http", "streamable_http"):
        return "http"
    if raw == "stdio":
        return "stdio"
    print(
        "WARNING: unrecognized Workflowy MCP transport; using the stdio shim. "
        "Use --transport stdio for a guarded full stdio server or --transport http for the daemon.",
        file=sys.stderr,
    )
    return "shim"


def http_bind_host() -> str:
    return (os.getenv(HTTP_HOST_ENV_VAR) or DEFAULT_HTTP_HOST).strip()


def http_port() -> int:
    return int(os.getenv(HTTP_PORT_ENV_VAR) or DEFAULT_HTTP_PORT)


def http_path() -> str:
    path = (os.getenv(HTTP_PATH_ENV_VAR) or DEFAULT_HTTP_PATH).strip() or DEFAULT_HTTP_PATH
    return path if path.startswith("/") else "/" + path


# @beacon[
#   id=daemon@dial-host,
#   role=http_dial_host(): concrete address clients dial (never the wildcard bind address),
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
# ]
def http_dial_host() -> str:
    """Address CLIENTS connect to.

    Same rule as ``broker_protocol.BROKER_DIAL_HOST``: a wildcard bind address
    cannot be dialed, so on the daemon's own machine clients use 127.0.0.1;
    from across a network boundary (WSL -> Windows) they set
    ``WORKFLOWY_MCP_HTTP_DIAL_HOST`` to the host's concrete IP.
    """
    explicit = os.getenv(HTTP_DIAL_HOST_ENV_VAR)
    if explicit and explicit.strip():
        return explicit.strip()
    bind = http_bind_host()
    return "127.0.0.1" if bind in ("0.0.0.0", "::", "") else bind


def daemon_url() -> str:
    """Full Streamable-HTTP URL clients (shim / OpenCode) should use."""
    return f"http://{http_dial_host()}:{http_port()}{http_path()}"


# @beacon[
#   id=daemon@shared-token,
#   role=shared_token(): the ONE local-mesh secret, reused from the broker,
#   slice_labels=nexus-loading-flow,nexus--daemon,nexus--broker,
#   kind=ast,
# ]
def shared_token() -> str:
    """Return the bearer token the daemon requires and the shim presents.

    Reuses the broker's shared secret so a single-machine install has exactly
    one secret to manage (env ``WORKFLOWY_BROKER_TOKEN`` or the per-user
    ``broker_token`` file, generated on first use).
    """
    from workflowy_mcp.broker_protocol import get_or_create_token

    token, _source = get_or_create_token()
    return token


# @beacon[
#   id=daemon@is-listening,
#   role=is_daemon_listening(): TCP liveness probe of the daemon port,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
# ]
def is_daemon_listening(timeout: float = 0.5) -> bool:
    """True if something accepts TCP connections on the daemon's dial address.

    Uvicorn binds its socket only after the ASGI lifespan startup completes,
    so an accepted connection implies the FastMCP app (and the server's own
    ``lifespan``: config, client, broker link) has finished starting.
    """
    try:
        with socket.create_connection((http_dial_host(), http_port()), timeout=timeout):
            return True
    except OSError:
        return False


def _log(msg: str) -> None:
    # stderr only: the shim shares its stdout with the MCP stdio protocol.
    print(f"[workflowy-mcp daemon] {msg}", file=sys.stderr, flush=True)


# @beacon[
#   id=daemon@ensure-running,
#   role=ensure_daemon_running(): probe-or-spawn the detached singleton daemon (the broker pattern applied to the server),
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Spawns with the SAME interpreter argv env and console/session flags; retains only its own child and retries exit 75 with bounded 0.5-to-5-second backoff while probing for the winning listener; other exits are not relaunched.,
# ]
def ensure_daemon_running(*, wait: bool = True, timeout_s: float | None = None) -> bool:
    """Make sure the single HTTP daemon is up; spawn it detached if not.

    Returns True once the daemon accepts connections (or immediately if it
    already did). Returns False on spawn/other child-start failure, or if no
    listener answered within ``timeout_s`` (default 90 s -- a cold start loads
    the nodes cache and dials the broker).

    Racing starts are decided by the lifetime writer guard before bootstrap.
    A closed old port does not prove that guard has been released: observe
    only our own child's exit 75, then retry with bounded backoff while still
    probing for a winner. Readiness here is only TCP; the shim subsequently
    negotiates and verifies the actual daemon identity before forwarding.
    """
    if is_daemon_listening():
        return True

    timeout = timeout_s if timeout_s is not None else float(
        os.getenv(DAEMON_SPAWN_TIMEOUT_ENV_VAR) or DEFAULT_SPAWN_TIMEOUT_S
    )

    cmd = [sys.executable, "-m", "workflowy_mcp", "--transport", "http"]
    env = dict(os.environ)
    env[TRANSPORT_ENV_VAR] = "http"

    def spawn() -> subprocess.Popen | None:
        # Reopen the same POSIX log destination for each own-child retry.
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "env": env,
            "close_fds": True,
        }
        if sys.platform == "win32":
            # Own console + process group; host Ctrl+C never reaches daemon.
            popen_kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            popen_kwargs["start_new_session"] = True
            out: Any = subprocess.DEVNULL
            try:
                from workflowy_mcp.broker_protocol import ensure_log_dir, log_dir

                if ensure_log_dir() is not None:
                    out = open(os.path.join(log_dir(), "workflowy-mcp-daemon.log"), "a", encoding="utf-8")
            except Exception:  # noqa: BLE001
                out = subprocess.DEVNULL
            popen_kwargs["stdout"] = out
            popen_kwargs["stderr"] = subprocess.STDOUT if out is not subprocess.DEVNULL else subprocess.DEVNULL

        _log(f"no daemon on {http_dial_host()}:{http_port()} -- spawning: {' '.join(cmd)}")
        try:
            return subprocess.Popen(cmd, **popen_kwargs)
        except Exception as exc:  # noqa: BLE001
            _log(f"could not spawn daemon ({type(exc).__name__})")
            return None
        finally:
            out_handle = popen_kwargs.get("stdout")
            if hasattr(out_handle, "close"):
                try:
                    out_handle.close()
                except Exception:  # noqa: BLE001
                    pass

    deadline = time.monotonic() + timeout
    child = spawn()
    if child is None:
        return False
    if not wait:
        return True

    delay, retry_at = 0.5, None
    while time.monotonic() < deadline:
        if is_daemon_listening():
            _log(f"daemon is up at {daemon_url()}")
            return True
        if child is not None:
            code = child.poll()
            if code == 75:
                child = None
                retry_at = time.monotonic() + delay
                delay = min(delay * 2, 5.0)
            elif code is not None:
                _log(f"daemon child exited {code} before readiness; not relaunching")
                return False
        elif retry_at is not None and time.monotonic() >= retry_at:
            child = spawn()
            if child is None:
                return False
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.5, remaining))
    _log(f"daemon did not start listening within {timeout:.0f}s")
    return False


# @beacon[
#   id=daemon@run-http,
#   role=run_http_daemon(): attach bearer auth and run the FastMCP app as the Streamable-HTTP daemon,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Keeps shared-token acquisition and bearer auth at the existing entry then runs the same FastMCP app through the sole owned HTTP runner; no legacy mcp.run(http) path remains.,
# ]
def run_http_daemon(mcp: Any) -> None:
    """Run ``mcp`` (the server's FastMCP instance) as the HTTP daemon.

    Keep token acquisition at its unchanged pre-lifespan site. The owned
    async runner retains the existing app and reference-counted lifespan,
    while automatic restart can exit only after the controller drains work.
    """
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    token = shared_token()
    mcp.auth = StaticTokenVerifier(
        tokens={token: {"client_id": "workflowy-mcp-local-mesh", "scopes": []}},
    )

    run_kwargs: dict[str, Any] = {
        "host": http_bind_host(),
        "port": http_port(),
        "path": http_path(),
    }
    allowed_raw = os.getenv(HTTP_ALLOWED_HOSTS_ENV_VAR)
    if allowed_raw and allowed_raw.strip():
        run_kwargs["allowed_hosts"] = [h.strip() for h in allowed_raw.split(",") if h.strip()]
    if _truthy(os.getenv(HTTP_STATELESS_ENV_VAR)):
        run_kwargs["stateless_http"] = True

    _log(
        f"starting Streamable-HTTP daemon on {run_kwargs['host']}:{run_kwargs['port']}"
        f"{run_kwargs['path']} (clients dial {daemon_url()}; bearer token required)"
    )
    asyncio.run(run_http_daemon_async(mcp, token=token, **run_kwargs))


# @beacon[
#   id=daemon@run-http-async,
#   role=run_http_daemon_async: sole owned HTTP runner and safe-exit callback,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Mirrors FastMCP Host/Origin app setup and its reference-counted lifespan around uvicorn.serve; graceful shutdown has no timer cut and only the drained lifecycle coordinator requests should_exit.,
# ]
async def run_http_daemon_async(
    mcp: Any, *, token: str, host: str, port: int, path: str,
    allowed_hosts: list[str] | None = None, stateless_http: bool | None = None,
) -> None:
    """Serve the authenticated existing app, preserving FastMCP transport policy."""
    import fastmcp
    import uvicorn
    from fastmcp.server.mixins.transport import _resolve_allowed_hosts_for_run
    from fastmcp.utilities.logging import temporary_log_level

    from .daemon_lifecycle import active_lifecycle, install_daemon_control_routes

    protection = fastmcp.settings.http_host_origin_protection
    resolved_hosts = _resolve_allowed_hosts_for_run(
        host=host, host_origin_protection=protection, allowed_hosts=allowed_hosts,
        configured_allowed_hosts=fastmcp.settings.http_allowed_hosts,
    )
    install_daemon_control_routes(mcp, token, path.rstrip("/") + "/_nexus/restart")
    app = mcp.http_app(
        path=path, transport="http", stateless_http=stateless_http,
        host_origin_protection=protection, allowed_hosts=resolved_hosts,
    )
    with temporary_log_level(None):
        async with mcp._lifespan_manager():
            config = uvicorn.Config(
                app, host=host, port=port, log_level=fastmcp.settings.log_level.lower(),
                lifespan="on", timeout_graceful_shutdown=None, ws="websockets-sansio",
            )
            server = uvicorn.Server(config)
            controller = active_lifecycle()
            if controller is not None:
                def request_exit() -> None:
                    server.should_exit = True
                controller.set_exit_callback(request_exit)
            await server.serve()
# @beacon-close[
#   id=daemon@module,
# ]
