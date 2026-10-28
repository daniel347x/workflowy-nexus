# @beacon[
#   id=shim@module,
#   role=stdio SHIM: native install-aware attach and one-send proxy to the single daemon,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=span,
#   show_span=true,
#   comment=Uses the ordinary upstream initialize for install/affinity comparison; eligible native shims drain and replace older code from their own installation while incapable clients warn/forward; forwarded calls are never resent. Local spawn/replacement requires a native loopback destination.,
# ]
"""A stdio proxy, never a full server or a fetch-latest launcher.

Fresh legacy ProxyClients preserve SDK routing/progress/roots. One local attach
lock coalesces pre-forward upgrades; the daemon's lifecycle decides drain/exit.
The own-interpreter spawn and bearer acquisition are unchanged. Failure after
entering the forwarding API is unknown, never permission to replay a tool.
Local spawn is only for native loopback destinations; other destinations remain attach-only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import os
import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpx
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.proxy import ProxyClient
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED

try:
    # FastMCP 4.0.5's public HTTP transport errors come from httpx2, not
    # the httpx client used for our control route. No dependency internals.
    from httpx2 import TransportError as _BackendTransportError
except ImportError:  # A later FastMCP may use the ordinary httpx backend.
    _BackendTransportError = httpx.TransportError

from . import daemon, server_bootstrap
from .daemon_identity import (
    DAEMON_META_KEY, SHIM_META_KEY, IDENTITY_PROTOCOL, IdentityWarning,
    InstalledIdentity, installed_identity, compare_install_identity,
    comparison_warnings, can_replace_daemon, unknown_warning,
)
from .daemon_lifecycle import (
    DaemonCallError, DaemonUnavailableError, RestartFailedError,
    UpstreamOutcomeUnknownError, attach_only_warning, older_shim_warning,
    drain_bound, merge_result_metadata,
)

TIMEOUT_ENV_VAR = "WORKFLOWY_MCP_SHIM_CALL_TIMEOUT_S"
DEFAULT_CALL_TIMEOUT_S = 900.0
_initializing: ContextVar[bool] = ContextVar("nexus_shim_initializing", default=False)


def _log(msg: str) -> None:
    print(f"[workflowy-mcp shim] {msg}", file=sys.stderr, flush=True)


def _can_spawn_local_daemon(url: str, *, local_is_wsl: bool) -> bool:
    """Classify local spawn applicability lexically, without DNS or changing forwarding eligibility."""
    if local_is_wsl:
        return False
    try:
        host = urlsplit(url).hostname
        if not host:
            return False
        host = host.casefold().rstrip(".")
        if host == "localhost":
            return True
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            return address.ipv4_mapped.is_loopback
        return address.is_loopback
    except ValueError:
        return False


def _positive(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _daemon_error(error: BaseException) -> DaemonCallError | None:
    """Recover our typed error through FastMCP's public exception causality.

    The component runner wraps arbitrary provider exceptions in ToolError or
    ResourceError before returning to middleware. Do not parse its message or
    turn an unrelated upstream is_error result into one of our diagnostics.
    """
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, DaemonCallError):
            return error
        if isinstance(error, MCPError) and error.code in (-32070, -32071) and isinstance(error.data, dict) and isinstance(error.data.get("message"), str):
            return DaemonCallError(error.data)
        cause = error.__cause__ or error.__context__
        if cause is None:
            break
        error = cause
    return None


def _pre_forward_connection_lost(error: BaseException) -> bool:
    """Recognize public transport loss through SDK causes/groups, not auth errors.

    This is used only while entering a connection: no tool has been forwarded.
    Protocol/config/HTTP-status failures still take the typed failure path.
    """
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (httpx.TransportError, _BackendTransportError, ConnectionError, EOFError, anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError)):
            return True
        if isinstance(current, MCPError) and current.code == CONNECTION_CLOSED:
            return True
        pending.extend(child for child in getattr(current, "exceptions", ()) if isinstance(child, BaseException))
        for child in (current.__cause__, current.__context__):
            if child is not None:
                pending.append(child)
    return False


@dataclass
class AttachState:
    url: str
    token: str = field(repr=False)
    local: InstalledIdentity = field(default_factory=installed_identity)
    local_is_wsl: bool = field(default_factory=server_bootstrap.is_wsl)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    instance_id: str | None = None
    relation: str = "unknown"
    replacement_capable: bool = False
    restart_supported: bool = False
    warnings: tuple[IdentityWarning, ...] = ()
    provider: Any = field(default=None, repr=False)
    accepted_target: dict[str, Any] | None = field(default=None, repr=False)
    restart_declined: bool = False
    _printed_warnings: set[IdentityWarning] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self.local_spawn_allowed = _can_spawn_local_daemon(self.url, local_is_wsl=self.local_is_wsl)
        self.bound, self.setting_warnings = drain_bound()
        self.spawn_budget = float(os.getenv(daemon.DAEMON_SPAWN_TIMEOUT_ENV_VAR) or daemon.DEFAULT_SPAWN_TIMEOUT_S)

    def metadata(self) -> dict[str, Any]:
        return {
            "protocol": IDENTITY_PROTOCOL, "replacement_capable": self.replacement_capable,
            "relation": self.relation, "warnings": [warning.payload() for warning in self.warnings],
        }

    def emit_warnings(self) -> None:
        for warning in self.warnings:
            if warning not in self._printed_warnings:
                print(warning.message, file=sys.stderr, flush=True)
                self._printed_warnings.add(warning)

    def observe(self, peer: Any) -> None:
        """One comparison/invalidation per incarnation; same instance is O(1)."""
        key = peer.get("instance_id") if isinstance(peer, dict) else None
        key = key if isinstance(key, str) and key else "legacy"
        if key == self.instance_id:
            return
        changed = self.instance_id is not None
        self.instance_id = key
        self.accepted_target = None
        self.restart_declined = False
        self._printed_warnings.clear()
        warnings = [*self.local.warnings, *self.setting_warnings]
        self.replacement_capable = False
        self.restart_supported = False
        self.relation = "unknown"
        if not isinstance(peer, dict):
            warnings.append(IdentityWarning(
                "legacy_daemon",
                "WARNING: this daemon does not expose the N06 restart handshake. Continuing with its advertised tools. "
                "The first activation must upgrade shim and daemon together on the daemon's native host; do not clear the uv cache.",
                "activate_pair_together_on_native_host",
            ))
        else:
            self.relation = compare_install_identity(self.local, peer)
            self.replacement_capable = self.local_spawn_allowed and can_replace_daemon(self.local.platform, self.local.hostname, self.local_is_wsl, peer)
            peer_host, peer_platform = peer.get("hostname"), peer.get("platform")
            if isinstance(peer_host, str) and isinstance(peer_platform, str):
                self.restart_supported = can_replace_daemon(peer_platform, peer_host, False, peer)
            warnings.extend(comparison_warnings(self.local, peer))
            known = {"protocol", "package_version", "commit", "commit_time", "identity_kind", "platform", "hostname", "instance_id", "phase", "drain_timeout_s", "restart_path", "restart_id"}
            if not self.restart_supported or set(peer) - known:
                warnings.append(unknown_warning("unrecognized identity metadata"))
            if self.relation == "peer_newer":
                warnings.append(older_shim_warning())
            elif self.relation == "local_newer" and not self.replacement_capable:
                warnings.append(attach_only_warning(str(peer.get("platform") or "")))
            elif self.relation == "unknown" and not any(w.code == "identity_unknown" for w in warnings):
                warnings.append(unknown_warning("source identity unavailable"))
        self.warnings = tuple(dict.fromkeys(warnings))
        if changed and self.provider is not None:
            # The four expressly permitted FastMCP adapter fields (§14.3).
            for name in ("_tools_cache", "_resources_cache", "_templates_cache", "_prompts_cache"):
                setattr(self.provider, name, None)
        if not _initializing.get():
            self.emit_warnings()

    def _extend_deadline(self, deadline: float, started: float, peer: dict[str, Any]) -> float:
        self.bound = max(self.bound, _positive(peer.get("drain_timeout_s")))
        return max(deadline, started + self.bound + 2 * self.spawn_budget)

    async def ensure_reachable(self, *, deadline: float, allow_spawn: bool) -> None:
        """Keep native-loopback cold recovery independent of stale peers; attach-only destinations never spawn."""
        if daemon.is_daemon_listening():
            return
        if not (allow_spawn and self.local_spawn_allowed):
            raise DaemonUnavailableError(self.url, local_is_wsl=self.local_is_wsl)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not await asyncio.to_thread(daemon.ensure_daemon_running, timeout_s=min(self.spawn_budget, remaining)):
            raise RestartFailedError("spawn_failed", url=self.url)

    async def control(self, method: str, *, deadline: float, body: dict[str, Any] | None = None, restart_id: str | None = None) -> tuple[int, dict[str, Any]]:
        # Derive the suffix from OUR configured URL: never forward credentials to
        # a peer-supplied origin, path or redirect destination.
        parsed = urlsplit(self.url)
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/_nexus/restart", "", ""))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RestartFailedError("control_failed")
        try:
            async with httpx.AsyncClient(timeout=remaining, follow_redirects=False, headers={"Authorization": f"Bearer {self.token}"}) as client:
                params = {"restart_id": restart_id, "wait": "1"} if restart_id is not None else None
                response = await client.request(method, url, json=body if method == "POST" else None, params=params)
                result = response.json()
                if not isinstance(result, dict) or response.status_code not in {200, 202, 409}:
                    raise ValueError("invalid control response")
                target = result.get("target_identity")
                if isinstance(target, dict):
                    self.accepted_target = target
                return response.status_code, result
        except (httpx.HTTPError, ValueError, TypeError):
            raise RestartFailedError("control_failed") from None

    async def restart_or_join(self, peer: dict[str, Any], *, deadline: float, started: float) -> None:
        """Orchestrate only before forwarding, never retry a user invocation."""
        if peer.get("phase") in ("draining", "exiting") and peer.get("restart_id"):
            _, reply = await self.control("GET", deadline=deadline, restart_id=peer["restart_id"])
        else:
            _, reply = await self.control("POST", deadline=deadline, body={
                "protocol": IDENTITY_PROTOCOL, "expected_instance_id": peer["instance_id"],
                "target_identity": self.local.source_payload(),
                "requester": {"platform": self.local.platform, "hostname": self.local.hostname, "is_wsl": self.local_is_wsl},
                "drain_timeout_s": self.bound,
            })
        deadline = self._extend_deadline(deadline, started, reply)
        while reply.get("status") == "restarting" and reply.get("phase") != "exiting":
            restart_id = reply.get("restart_id")
            if not isinstance(restart_id, str):
                raise RestartFailedError("control_failed")
            _, reply = await self.control("GET", deadline=deadline, restart_id=restart_id)
            deadline = self._extend_deadline(deadline, started, reply)
        status = reply.get("status")
        if status == "restart_failed":
            error = reply.get("error") or {}
            reason = error.get("reason") if isinstance(error, dict) else None
            raise RestartFailedError(reason if reason in {"drain_timeout", "control_failed", "exit_timeout", "spawn_failed"} else "control_failed", drain_timeout_s=_positive(reply.get("drain_timeout_s")) or self.bound, url=self.url)
        if status in {"not_required", "attach_only"}:
            public = reply.get("daemon")
            if isinstance(public, dict) and public.get("instance_id") == peer.get("instance_id"):
                # The daemon rechecks applicability. An authoritative refusal
                # leaves it serving: land warn-and-forward, not a POST loop.
                self.restart_declined = True
                if status == "attach_only":
                    self.replacement_capable = False
                warnings = []
                for entry in reply.get("warnings", ()):
                    if isinstance(entry, dict) and all(isinstance(entry.get(key), str) for key in ("code", "message", "recovery")):
                        warnings.append(IdentityWarning(entry["code"], entry["message"], entry["recovery"]))
                self.warnings = tuple(dict.fromkeys((*self.warnings, *warnings)))
                if not _initializing.get():
                    self.emit_warnings()
            return  # Fresh negotiation before using the still-serving daemon.
        if status in {"daemon_changed", "restart_superseded"}:
            return  # Fresh negotiation, not an inference about a user tool.
        if status != "restarting" or reply.get("phase") != "exiting":
            raise RestartFailedError("control_failed")
        # A closed port is NOT guard release. The existing spawn helper handles
        # an own child's exit 75. A raced replacement is checked by negotiation.
        while daemon.is_daemon_listening():
            if time.monotonic() >= deadline:
                raise RestartFailedError("exit_timeout", url=self.url)
            try:
                _, current = await self.control("GET", deadline=deadline)
            except RestartFailedError:
                await asyncio.sleep(0.1)
                continue
            public = current.get("daemon")
            if isinstance(public, dict) and public.get("instance_id") != peer.get("instance_id"):
                return
            await asyncio.sleep(0.1)
        await self.replacement_ready(deadline)

    async def replacement_ready(self, deadline: float) -> None:
        may_spawn = self.replacement_capable and not (
            isinstance(self.accepted_target, dict) and compare_install_identity(self.local, self.accepted_target) == "peer_newer"
        )
        if may_spawn:
            await self.ensure_reachable(deadline=deadline, allow_spawn=True)
        else:
            # Incapable and known-older participants never deliberately respawn
            # their own older installation for another participant's target.
            while not daemon.is_daemon_listening():
                if time.monotonic() >= deadline:
                    if self.local_spawn_allowed:
                        raise RestartFailedError("spawn_failed", url=self.url)
                    raise DaemonUnavailableError(self.url, local_is_wsl=self.local_is_wsl)
                await asyncio.sleep(0.1)


# @beacon[
#   id=shim@handshaking-proxy-client,
#   role=HandshakingProxyClient: pre-forward singleflight attach and one-send outcome classification,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Consumes existing initialize before forwarding; bounded negotiation reconnects honor the current higher target without replay; independent native-loopback cold recovery ignores stale capability and control declines land forwarding; one base send preserves genuine replies or reports unknown.,
# ]
class HandshakingProxyClient(ProxyClient):
    """Bounded pre-forward negotiation/cold recovery never replay a forwarded call.

    A received upstream result stays authoritative even if session teardown
    fails afterward; a failure of the forwarding API itself stays unknown.
    A TCP probe can race shutdown: only connection-loss errors before metadata
    are re-negotiated, paced under the original attach deadline, never after send.
    The current attempt's higher target is honored without letting a stale
    peer/attempt disable a later independent native-loopback cold start.
    """

    def __init__(self, transport: Any, *, attach_state: AttachState, **kwargs):
        self.attach_state = attach_state
        self.invocation_state = "not_sent"
        self._known_refusal = False
        self._received_result = False
        kwargs["mode"] = "legacy"
        kwargs["init_timeout"] = attach_state.bound + 2 * attach_state.spawn_budget
        super().__init__(transport, **kwargs)

    async def __aenter__(self):
        state = self.attach_state
        started = time.monotonic()
        deadline = started + state.bound + 2 * state.spawn_budget
        failed_control = None
        restart_considered = False
        async with state.lock:
            deadline = max(deadline, started + state.bound + 2 * state.spawn_budget)
            # ZOV1IB: cached observations govern a REACHABLE peer only. Keep
            # the base native cold-start path and its dial/config conditions.
            allow_spawn = not state.local_is_wsl
            await state.ensure_reachable(deadline=deadline, allow_spawn=allow_spawn)
            while True:
                if time.monotonic() >= deadline:
                    raise RestartFailedError("control_failed")
                connected, peer = False, None
                try:
                    await super().__aenter__()
                    connected = True
                    initialized = self.initialize_result
                    peer = (initialized.meta or {}).get(DAEMON_META_KEY) if initialized is not None else None
                    if failed_control is not None:
                        old_instance, error = failed_control
                        if not isinstance(peer, dict) or peer.get("instance_id") == old_instance:
                            raise error  # Same daemon still serves: preserve its named remedy.
                        failed_control = None  # A real fresh negotiation proved replacement.
                    state.observe(peer)
                    if not isinstance(peer, dict) or not state.restart_supported:
                        return self
                    deadline = state._extend_deadline(deadline, started, peer)
                    draining = peer.get("phase") in ("draining", "exiting") and isinstance(peer.get("restart_id"), str)
                    if not draining and not (state.relation == "local_newer" and state.replacement_capable and not state.restart_declined):
                        return self
                    await super().__aexit__(None, None, None)
                    connected = False
                    # Only a target learned in THIS attach's control exchange
                    # constrains replacement. A prior peer never vetoes the
                    # independent cold-start decision above (ZOV1IB).
                    state.accepted_target = None
                    restart_considered = True
                    await state.restart_or_join(peer, deadline=deadline, started=started)
                    deadline = state._extend_deadline(deadline, started, {})
                except BaseException as exc:
                    if connected:
                        try:
                            await super().__aexit__(*sys.exc_info())
                        except Exception:
                            pass  # No tool has been sent; retain the original attach failure.
                    if isinstance(exc, (asyncio.CancelledError, SystemExit, KeyboardInterrupt)):
                        raise
                    if peer is None and _pre_forward_connection_lost(exc):
                        # A readiness probe is not an MCP negotiation. An old
                        # listener can disappear between them while another
                        # shim upgrades it. Reconnect only BEFORE forwarding;
                        # auth/config errors are not transport-loss retries.
                        if restart_considered and state.accepted_target is not None:
                            # Do not turn a reconnect into a deliberate spawn
                            # of a build older than this attempt's known target.
                            await state.replacement_ready(deadline)
                        else:
                            await state.ensure_reachable(deadline=deadline, allow_spawn=not state.local_is_wsl)
                        remaining = deadline - time.monotonic()
                        if remaining > 0:
                            await asyncio.sleep(min(0.1, remaining))
                        continue
                    error = exc if isinstance(exc, DaemonCallError) else RestartFailedError("control_failed")
                    if isinstance(error, RestartFailedError) and error.error["reason"] == "control_failed" and failed_control is None and isinstance(peer, dict) and state.restart_supported:
                        # A listener may already be the replacement: a failed POST
                        # or GET alone proves neither old service nor completion.
                        # Re-negotiate ONCE, before any tool, then decide by instance.
                        failed_control = (peer.get("instance_id"), error)
                        if not daemon.is_daemon_listening():
                            await state.replacement_ready(deadline)
                        continue
                    raise error from None

    async def __aexit__(self, exc_type, exc_value, traceback):
        try:
            return await super().__aexit__(exc_type, exc_value, traceback)
        except asyncio.CancelledError:
            if self.invocation_state == "forwarded":
                _log(str(UpstreamOutcomeUnknownError()))
            raise
        except Exception:
            if self._received_result or self._known_refusal:
                return None  # A genuine result/refusal survives teardown loss.
            if self.invocation_state == "forwarded":
                raise UpstreamOutcomeUnknownError() from None
            raise RestartFailedError("control_failed") from None

    async def call_tool_mcp(self, *args, **kwargs):
        self._known_refusal = False
        self._received_result = False
        self.invocation_state = "forwarded"
        try:
            result = await super().call_tool_mcp(*args, **kwargs)
        except asyncio.CancelledError:
            _log(str(UpstreamOutcomeUnknownError()))
            raise
        except Exception:
            raise UpstreamOutcomeUnknownError() from None
        self._received_result = True
        payload = result.structured_content
        if result.is_error and isinstance(payload, dict) and isinstance(payload.get("error"), dict) and payload["error"].get("code") == "restarting":
            self.invocation_state, self._known_refusal = "not_sent", True
        return result

    async def read_resource(self, *args, **kwargs):
        self._known_refusal = False
        self._received_result = False
        self.invocation_state = "forwarded"
        try:
            result = await super().read_resource(*args, **kwargs)
        except asyncio.CancelledError:
            _log(str(UpstreamOutcomeUnknownError()))
            raise
        except Exception as exc:
            if getattr(exc, "code", None) == -32070:
                self.invocation_state, self._known_refusal = "not_sent", True
                raise
            raise UpstreamOutcomeUnknownError().mcp_error() from None
        self._received_result = True
        return result

    async def get_prompt(self, *args, **kwargs):
        self._known_refusal = False
        self._received_result = False
        self.invocation_state = "forwarded"
        try:
            result = await super().get_prompt(*args, **kwargs)
        except asyncio.CancelledError:
            _log(str(UpstreamOutcomeUnknownError()))
            raise
        except Exception as exc:
            if getattr(exc, "code", None) == -32070:
                self.invocation_state, self._known_refusal = "not_sent", True
                raise
            raise UpstreamOutcomeUnknownError().mcp_error() from None
        self._received_result = True
        return result


# @beacon[
#   id=shim@outcome-middleware,
#   role=ShimOutcomeMiddleware: outermost attach diagnostics and typed unsuccessful results,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Awaits proxy metadata negotiation then merges deduplicated shim diagnostics; recovers typed fields through FastMCP exception causes; only initial console attach failure exits while an established proxy keeps reporting per-call failures without replay.,
# ]
class ShimOutcomeMiddleware(Middleware):
    """Outermost typed diagnostics; only a console's first attach may exit 2."""
    def __init__(self, state: AttachState, *, console: bool = False):
        self.state, self.console = state, console
        self._attached = False

    async def on_initialize(self, context, call_next):
        token = _initializing.set(True)
        try:
            result = await call_next(context)
            self.state.emit_warnings()
            if result is not None:
                self._attached = True
            return merge_result_metadata(result, SHIM_META_KEY, self.state.metadata())
        except Exception as wrapper:
            exc = _daemon_error(wrapper)
            if exc is None:
                raise
            if self.console and not self._attached:
                _log(str(exc))
                raise SystemExit(2) from None
            raise exc.mcp_error() from None
        finally:
            _initializing.reset(token)

    async def on_discover(self, context, call_next):
        return await self.on_initialize(context, call_next)

    async def on_call_tool(self, context, call_next):
        try:
            return await call_next(context)
        except Exception as wrapper:
            exc = _daemon_error(wrapper)
            if exc is None:
                raise
            return exc.tool_result()

    async def on_read_resource(self, context, call_next):
        try:
            return await call_next(context)
        except Exception as wrapper:
            exc = _daemon_error(wrapper)
            if exc is None:
                raise
            raise exc.mcp_error() from None

    async def on_get_prompt(self, context, call_next):
        return await self.on_read_resource(context, call_next)


# @beacon[
#   id=shim@main,
#   role=main: native-loopback probe-or-spawn and an upstream-identity legacy FastMCPProxy,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Constructs one install/affinity state; only native loopback destinations may spawn or replace locally; WSL and nonlocal destinations probe/attach with existing typed outcomes and upstream identity.,
# ]
def main() -> None:
    """Serve the existing stdio proxy; only a native loopback destination may spawn locally."""
    from fastmcp.server.providers.proxy import FastMCPProxy, ProxyMetadataMiddleware

    identity, local_is_wsl = installed_identity(), server_bootstrap.is_wsl()
    url = daemon.daemon_url()
    local_spawn_allowed = _can_spawn_local_daemon(url, local_is_wsl=local_is_wsl)
    ready = daemon.ensure_daemon_running() if local_spawn_allowed else daemon.is_daemon_listening()
    if not ready:
        error = RestartFailedError("spawn_failed", url=url) if local_spawn_allowed else DaemonUnavailableError(url, local_is_wsl=local_is_wsl)
        _log(str(error))
        raise SystemExit(2)
    state = AttachState(url, daemon.shared_token(), local=identity, local_is_wsl=local_is_wsl)
    timeout = float(os.getenv(TIMEOUT_ENV_VAR) or DEFAULT_CALL_TIMEOUT_S)

    def _client_factory() -> ProxyClient:
        return HandshakingProxyClient(state.url, attach_state=state, auth=state.token, timeout=timeout)

    proxy = FastMCPProxy(
        client_factory=_client_factory, identity="upstream", provider_error_strategy="raise",
        name="NEXUS — Workflowy MCP Server (shim)",
        middleware=[ShimOutcomeMiddleware(state, console=True)],
    )
    for middleware in proxy.middleware:
        if isinstance(middleware, ProxyMetadataMiddleware):
            state.provider = middleware.provider
    _log(f"proxying stdio <-> {state.url}")
    proxy.run(transport="stdio")


if __name__ == "__main__":
    main()
# @beacon-close[
#   id=shim@module,
# ]
