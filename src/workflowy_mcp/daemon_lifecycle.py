"""One process-local admission/drain authority, not a lock or mutation plane.

The full-server lifespan activates this controller after taking its writer
guard. Library/client-only uses have no active controller and pass through.
Accepted bodies and independent continuations outlive transport waiters; the
long deadline ends only a restart attempt, never the work being drained.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
from mcp.shared.exceptions import MCPError
from mcp_types import TextContent
from starlette.requests import Request
from starlette.responses import JSONResponse

from .daemon_identity import (
    DAEMON_META_KEY,
    IDENTITY_PROTOCOL,
    IdentityWarning,
    InstalledIdentity,
    can_replace_daemon,
    compare_install_identity,
    comparison_warnings,
    installed_identity,
    unknown_warning,
    valid_commit,
    valid_commit_time,
)

DRAIN_TIMEOUT_ENV = "WORKFLOWY_MCP_DAEMON_DRAIN_TIMEOUT_S"
DEFAULT_DRAIN_TIMEOUT_S = 3600.0
_CALL_TIMEOUT_ENV = "WORKFLOWY_MCP_SHIM_CALL_TIMEOUT_S"
_active: DaemonLifecycle | None = None
_work_context: ContextVar[tuple[DaemonLifecycle, WorkToken] | None] = ContextVar(
    "nexus_daemon_work", default=None,
)


class DaemonCallError(Exception):
    """A typed unsuccessful envelope shared by daemon admission and shim attach."""

    def __init__(self, error: dict[str, Any]):
        self.error = error
        super().__init__(error["message"])

    def payload(self) -> dict[str, Any]:
        return {"success": False, "error": self.error}

    def tool_result(self) -> ToolResult:
        payload = self.payload()
        return ToolResult(
            is_error=True, structured_content=payload,
            content=[TextContent(type="text", text=json.dumps(payload))],
        )

    def mcp_error(self) -> MCPError:
        code = -32071 if self.error["code"] == "upstream_outcome_unknown" else -32070
        return MCPError(code=code, message=str(self), data=self.error)


class RestartingError(DaemonCallError):
    def __init__(self, restart_id: str | None, *, exiting: bool = False):
        retry_after = 1 if exiting else 30
        super().__init__({
            "code": "restarting",
            "message": f"Workflowy daemon is restarting; this call was not admitted. Retry after {retry_after} seconds.",
            "retryable": True, "retry_after": retry_after, "outcome": "not_sent",
            "restart_id": restart_id,
        })


class RestartFailedError(DaemonCallError):
    def __init__(self, reason: str, *, drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S, url: str = ""):
        if reason == "drain_timeout":
            message = (
                f"Automatic daemon restart could not drain within {drain_timeout_s:g} seconds. "
                "The daemon is still serving and admitted work was not cancelled. Retry this attach "
                "from the same upgraded shim after the active work finishes."
            )
            recovery = "retry_attach_after_active_work"
        elif reason == "control_failed":
            message = (
                "Automatic daemon restart did not complete; the existing daemon is still serving. "
                "Retry this attach from the same upgraded shim. No process was killed."
            )
            recovery = "retry_attach_same_upgraded_shim"
        elif reason in {"exit_timeout", "spawn_failed"}:
            message = (
                f"Automatic daemon restart could not make the replacement ready at {url}. Start this "
                "same upgraded installation on the daemon's native host with workflowy-mcp --transport "
                "http and check its console/log. Do not delete writer.lock or clear the uv cache."
            )
            recovery = "start_same_upgraded_native_installation"
        else:
            raise ValueError("invalid restart failure reason")
        super().__init__({
            "code": "restart_failed", "message": message, "reason": reason,
            "retryable": True, "retry_after": 30, "outcome": "not_sent", "recovery": recovery,
        })


class DaemonUnavailableError(DaemonCallError):
    def __init__(self, url: str, *, local_is_wsl: bool = True):
        if not local_is_wsl:
            super().__init__({
                "code": "daemon_unavailable",
                "message": f"Workflowy daemon is not reachable at {url}; this shim starts a local daemon only when it dials loopback "
                "(localhost, 127.0.0.1 or ::1). Start the daemon on that host with workflowy-mcp --transport http, "
                "or dial loopback (leave WORKFLOWY_MCP_HTTP_DIAL_HOST unset and the bind host at its default) "
                "so this shim can start it. No local full-server fallback was attempted.",
                "retryable": True, "retry_after": 30, "outcome": "not_sent",
                "recovery": "start_daemon_on_that_host_or_dial_loopback",
            })
            return
        super().__init__({
            "code": "daemon_unavailable",
            "message": f"Workflowy daemon is not reachable at {url}; this shim cannot start it on that host OS. "
            "Start the native-host shim with workflowy-mcp-shim. No local full-server fallback was attempted.",
            "retryable": True, "retry_after": 30, "outcome": "not_sent",
            "recovery": "start_native_host_shim",
        })


class UpstreamOutcomeUnknownError(DaemonCallError):
    def __init__(self):
        super().__init__({
            "code": "upstream_outcome_unknown",
            "message": "The connection ended after this call was forwarded; its outcome is unknown and the shim "
            "did not resend it. Read back the affected state before deciding whether to issue a new call.",
            "retryable": False, "outcome": "unknown", "forwarded": True,
            "recovery": "read_back_before_retry",
        })


def attach_only_warning(peer_platform: str) -> IdentityWarning:
    windows = peer_platform == "win32"
    side = "Windows-side" if windows else "daemon-host"
    native = "from Windows" if windows else "on its native host"
    return IdentityWarning(
        "shim_newer_attach_only",
        "WARNING: this shim is newer than the daemon but cannot replace it from this host OS. "
        f"Continuing with the daemon's advertised tools. Upgrade/start the {side} shim with "
        f"workflowy-mcp-shim; it will restart the daemon {native}. Do not clear the uv cache.",
        "upgrade_windows_side_shim" if windows else "upgrade_daemon_host_shim",
    )


def older_shim_warning() -> IdentityWarning:
    return IdentityWarning(
        "shim_older",
        "WARNING: this shim is older than the running daemon; continuing with the daemon's "
        "advertised tools. No action is required.",
        "no_action_required",
    )


def _positive_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def drain_bound(
    requested: Any = None, *, environ: Mapping[str, str] | None = None,
) -> tuple[float, tuple[IdentityWarning, ...]]:
    """Lengthen only; call-timeout zero remains disabled, never a drain ceiling."""
    env = os.environ if environ is None else environ
    bound = DEFAULT_DRAIN_TIMEOUT_S
    adjusted: list[tuple[str, Any]] = []
    for setting, raw in ((DRAIN_TIMEOUT_ENV, env.get(DRAIN_TIMEOUT_ENV)),
                         (_CALL_TIMEOUT_ENV, env.get(_CALL_TIMEOUT_ENV)),
                         ("drain_timeout_s", requested)):
        if raw is None or raw == "":
            continue
        number = _positive_seconds(raw)
        # Zero is a pre-existing disabled CALL timeout, not an invalid drain.
        if setting == _CALL_TIMEOUT_ENV and not isinstance(raw, bool):
            try:
                if float(raw) == 0:
                    continue
            except (ValueError, TypeError, OverflowError):
                pass
        if number is None or number < DEFAULT_DRAIN_TIMEOUT_S:
            adjusted.append((setting, raw))
        if number is not None:
            bound = max(bound, number)
    warnings = tuple(IdentityWarning(
        "drain_setting_adjusted",
        f"WARNING: {setting}={raw} cannot shorten the safe drain bound; using {bound:g} seconds. "
        "Active work will never be cancelled by this bound.",
        "use_safe_drain_bound",
    ) for setting, raw in adjusted)
    return bound, warnings


def active_lifecycle() -> DaemonLifecycle | None:
    return _active


def set_active_lifecycle(controller: DaemonLifecycle | None) -> None:
    """Full lifespan only; importing this module never activates accounting."""
    global _active
    if controller is not None and _active is not None and controller is not _active:
        raise RuntimeError("a daemon lifecycle is already active")
    _active = controller


@asynccontextmanager
async def operation_accounting(label: str):
    """Nest lifetime accounting around the existing boundary, or pass through."""
    controller = active_lifecycle()
    if controller is None:
        yield
    else:
        async with controller.operation_scope(label):
            yield


@dataclass(eq=False)
class WorkToken:
    label: str
    owner: asyncio.Task[Any] | None = None
    task: asyncio.Task[Any] | None = None
    operation_depth: int = 0


@dataclass
class _RestartAttempt:
    restart_id: str
    target: dict[str, Any]
    started: float
    deadline: float
    bound: float
    warnings: tuple[IdentityWarning, ...] = ()
    result: dict[str, Any] | None = None
    failed_probes: set[str] = field(default_factory=set)
    requested_stops: set[tuple[str, asyncio.Task[Any]]] = field(default_factory=set)


async def _wait_event(event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass  # Only an event waiter expires. No work task is cancelled.


# @beacon[
#   id=daemon-lifecycle@controller,
#   role=DaemonLifecycle: one full-server admission and non-cutting drain controller,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Emits immutable startup diagnostics and reserves retained bodies before awaiting; shielded work independent children and finalizer handoffs survive waiter loss without gaps; existing-authority probes decide safe exit or RUNNING restoration without cancelling work.,
# ]
class DaemonLifecycle:
    """One startup diagnostic emission; STARTING → RUNNING → DRAINING → EXITING.

    A failed attempt returns to RUNNING without cancelling accepted work.
    """

    def __init__(
        self, *, identity: InstalledIdentity | None = None, restart_path: str = "/mcp/_nexus/restart",
        exit_callback: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        waiter: Callable[[asyncio.Event, float], Awaitable[None]] = _wait_event,
    ):
        self.identity = identity if identity is not None else installed_identity()
        self.restart_path = restart_path
        self.phase = "starting"
        self.drain_timeout_s, self.setting_warnings = drain_bound()
        for warning in self.identity.warnings + self.setting_warnings:
            print(warning.message, file=sys.stderr)
        self._clock, self._waiter = clock, waiter
        self._exit_callback = exit_callback
        self._tokens: set[WorkToken] = set()
        self._background: dict[asyncio.Task[Any], str] = {}
        self._background_tokens: dict[asyncio.Task[Any], WorkToken] = {}
        self._probes: dict[str, Callable[[], bool]] = {}
        self._warned_probes: set[str] = set()
        self._job_stops: dict[str, tuple[Callable[[str | None], None], asyncio.Task[Any]]] = {}
        self._attempt: _RestartAttempt | None = None
        self._coordinator: asyncio.Task[Any] | None = None
        self._changed = asyncio.Event()

    def _notify(self) -> None:
        event, self._changed = self._changed, asyncio.Event()
        event.set()

    def start(self) -> None:
        if self.phase != "starting":
            raise RuntimeError("daemon lifecycle already started")
        self.phase = "running"
        self._notify()

    def set_exit_callback(self, callback: Callable[[], None]) -> None:
        self._exit_callback = callback

    def metadata(self) -> dict[str, Any]:
        identity = self.identity
        return {
            "protocol": IDENTITY_PROTOCOL, **identity.source_payload(),
            "identity_kind": identity.identity_kind, "platform": identity.platform,
            "hostname": identity.hostname, "instance_id": identity.instance_id,
            "phase": self.phase,
            "drain_timeout_s": self._attempt.bound if self.phase in {"draining", "exiting"} and self._attempt else self.drain_timeout_s,
            "restart_path": self.restart_path,
            "restart_id": self._attempt.restart_id if self.phase in {"draining", "exiting"} and self._attempt else None,
        }

    def _current_token(self) -> WorkToken | None:
        value = _work_context.get()
        if value is not None and value[0] is self:
            token = value[1]
            if token in self._tokens and token.owner is asyncio.current_task():
                return token
        # Prepared tasks can be created by the existing job authority before
        # its synchronous observer registers their lifetime. That registration
        # authorizes their own later operation scopes, never a copied context
        # in another task. It is removed only after actual terminalization.
        token = self._background_tokens.get(asyncio.current_task())
        return token if token in self._tokens else None

    def admit_work(self, label: str) -> WorkToken:
        """Synchronously reserve NEW work, or refuse before its body is created."""
        if self.phase != "running":
            raise RestartingError(self.metadata()["restart_id"], exiting=self.phase == "exiting")
        token = WorkToken(label)
        self._tokens.add(token)
        return token

    def _complete_work(self, token: WorkToken, task: asyncio.Task[Any]) -> None:
        # The body's finally blocks and result publication have completed now.
        self._tokens.discard(token)
        self._notify()
        if not task.cancelled():
            task.exception()  # Retrieve orphaned failures, without changing awaits.

    async def _body(self, token: WorkToken, coro: Coroutine[Any, Any, Any]) -> Any:
        token.owner = asyncio.current_task()
        context = _work_context.set((self, token))
        try:
            return await coro
        finally:
            _work_context.reset(context)

    def run_admitted(self, token: WorkToken, coro: Coroutine[Any, Any, Any]) -> Coroutine[Any, Any, Any]:
        """Schedule synchronously; even a never-started waiter cannot drop the body."""
        if token not in self._tokens or token.task is not None or token.owner is not None:
            coro.close()
            raise RuntimeError("invalid or already-used daemon admission token")
        body = self._body(token, coro)
        try:
            token.task = asyncio.create_task(body, name=f"daemon-work:{token.label}")
        except BaseException:
            body.close()
            coro.close()
            self._tokens.discard(token)
            self._notify()
            raise
        token.task.add_done_callback(lambda task: self._complete_work(token, task))
        async def wait_for_body() -> Any:
            return await asyncio.shield(token.task)
        return wait_for_body()

    def spawn_admitted_child(
        self, label: str, coro: Coroutine[Any, Any, Any], *,
        continuation_of: asyncio.Task[Any] | None = None,
    ) -> asyncio.Task[Any]:
        """Reserve before scheduling; a registered done-callback handoff has no zero-count gap.

        An explicit continuation must run in the loop callback of a completed
        task whose lifetime is STILL registered. It never borrows a stale
        inherited ContextVar or authorizes another task to reuse its parent.
        """
        from .coherence.operation_cache import create_isolated_context_task

        try:
            registered_continuation = False
            if continuation_of is not None:
                parent = self._background_tokens.get(continuation_of)
                registered_continuation = (
                    asyncio.current_task() is None and continuation_of.done()
                    and parent in self._tokens and parent.owner is continuation_of
                )
                if not registered_continuation:
                    raise RuntimeError("invalid daemon continuation handoff")
            if registered_continuation or self._current_token() is not None:
                token = WorkToken(label)
                self._tokens.add(token)
            else:
                token = self.admit_work(label)
        except BaseException:
            coro.close()
            raise
        body = self._body(token, coro)
        try:
            task = create_isolated_context_task(body, name=f"daemon-child:{label}")
        except BaseException:
            body.close()
            coro.close()
            self._tokens.discard(token)
            self._notify()
            raise
        token.task = task
        task.add_done_callback(lambda finished: self._complete_work(token, finished))
        return task

    @asynccontextmanager
    async def operation_scope(self, label: str):
        """Account adoption through final reset; reuse only this task's live token."""
        token = self._current_token()
        own = token is None
        context = None
        if own:
            token = self.admit_work(label)
            token.owner = asyncio.current_task()
            context = _work_context.set((self, token))
        token.operation_depth += 1
        try:
            yield token
        finally:
            token.operation_depth -= 1
            if own:
                self._tokens.discard(token)
                _work_context.reset(context)
            self._notify()

    def track_background_task(self, task: asyncio.Task[Any], label: str = "background") -> None:
        """Retain an already-authorized actual task, not a job status or PID."""
        if task in self._background:
            return
        self._background[task] = label
        token = next((item for item in self._tokens if item.task is task), None)
        if token is None:
            token = WorkToken(label, owner=task, task=task)
            self._tokens.add(token)
        elif token.owner is None:
            token.owner = task
        self._background_tokens[task] = token
        def finished(done: asyncio.Task[Any]) -> None:
            self._background.pop(done, None)
            self._tokens.discard(self._background_tokens.pop(done))
            self._notify()
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        self._notify()

    def register_drain_probe(self, name: str, probe: Callable[[], bool]) -> None:
        self._probes[name] = probe

    def register_job_boundary_stop(
        self, job_id: str, callback: Callable[[str | None], None], task: asyncio.Task[Any] | None = None,
    ) -> None:
        """Request a non-killing boundary stop; None abandons an unobserved request."""
        task = task if task is not None else asyncio.current_task()
        if task is None:
            raise RuntimeError("a job boundary stop requires its actual task lifetime")
        self.track_background_task(task, f"job:{job_id}")
        self._job_stops[job_id] = (callback, task)
        def terminalized(done: asyncio.Task[Any]) -> None:
            if self._job_stops.get(job_id) == (callback, done):
                self._job_stops.pop(job_id, None)
        task.add_done_callback(terminalized)
        if self.phase == "draining" and self._attempt is not None:
            self._request_job_stop(job_id, callback, task, self._attempt)

    def _request_job_stop(self, job_id, callback, task, attempt) -> None:
        key = (job_id, task)
        if not task.done() and key not in attempt.requested_stops:
            attempt.requested_stops.add(key)
            self._call_boundary_stop(callback, attempt.restart_id)

    @staticmethod
    def _call_boundary_stop(callback: Callable[[str | None], None], restart_id: str | None) -> None:
        try:
            callback(restart_id)
        except Exception:
            print("WARNING: a restart boundary-stop callback failed; waiting for the actual task to finish.", file=sys.stderr)

    def _busy(self, attempt: _RestartAttempt | None = None) -> bool:
        busy = bool(self._tokens) or any(not task.done() for task in self._background)
        for name, probe in self._probes.items():
            if attempt is not None and name in attempt.failed_probes:
                busy = True
                continue
            try:
                busy = bool(probe()) or busy
            except Exception:
                if attempt is not None:
                    attempt.failed_probes.add(name)
                if name not in self._warned_probes:
                    self._warned_probes.add(name)
                    print("WARNING: a daemon drain probe failed; treating it as busy for this restart attempt.", file=sys.stderr)
                busy = True
        return busy

    async def join_work(self) -> None:
        """Upgrade teardown waits for real work and probes, never cancels them."""
        while self._busy():
            await self._waiter(self._changed, 0.1)

    def snapshot(self, restart_id: str | None = None) -> dict[str, Any]:
        attempt = self._attempt
        if restart_id is not None and (attempt is None or attempt.restart_id != restart_id):
            return {"status": "restart_superseded", "restart_id": restart_id, "daemon": self.metadata()}
        public: dict[str, Any] = {
            "status": "restarting" if self.phase in {"draining", "exiting"} else "not_required",
            "phase": self.phase, "instance_id": self.identity.instance_id,
            "restart_id": attempt.restart_id if attempt else None,
            "drain_timeout_s": attempt.bound if attempt else self.drain_timeout_s,
            "daemon": self.metadata(),
            "active_work": len(self._tokens),
            "active_operations": sum(token.operation_depth > 0 for token in self._tokens),
            "active_background": sum(not task.done() for task in self._background),
        }
        if attempt is not None:
            public.update(target_identity=dict(attempt.target), warnings=[warning.payload() for warning in attempt.warnings])
            if attempt.result is not None:
                public.update(attempt.result)
        if self.phase in {"draining", "exiting"}:
            public["retry_after"] = 1 if self.phase == "exiting" else 30
        return public

    def request_restart(self, body: Any) -> tuple[int, dict[str, Any]]:
        """Validate and change phase without an await; competitors join one deadline."""
        parsed = _parse_restart_request(body)
        if parsed is None:
            return 400, {"status": "invalid_restart_request", "success": False,
                         "error": {"code": "invalid_restart_request", "message": "Invalid daemon restart request."}}
        expected, target, requester, requested, warnings = parsed
        if expected != self.identity.instance_id:
            return 409, {"status": "daemon_changed", "daemon": self.metadata()}
        relation = compare_install_identity(target, self.identity)
        warnings += comparison_warnings(target, self.identity)
        capable = can_replace_daemon(requester["platform"], requester["hostname"], requester["is_wsl"], self.metadata())
        if relation != "local_newer" or not capable:
            if relation == "unknown" and not warnings:
                warnings += (unknown_warning("source identity unavailable"),)
            elif relation == "peer_newer":
                warnings += (older_shim_warning(),)
            elif relation == "local_newer" and not capable:
                warnings += (attach_only_warning(self.identity.platform),)
            return 200, {"status": "not_required" if relation != "local_newer" else "attach_only",
                         "daemon": self.metadata(), "warnings": [warning.payload() for warning in warnings]}
        if self.phase in {"draining", "exiting"} and self._attempt is not None:
            if compare_install_identity(target, self._attempt.target) == "local_newer":
                self._attempt.target = target
                self._notify()
            return 202, {**self.snapshot(), "accepted": False}
        if self.phase != "running":
            return 200, {"status": "restart_failed", **RestartFailedError("control_failed").payload(), "daemon": self.metadata()}
        bound, adjustments = drain_bound(requested)
        now = self._clock()
        attempt = _RestartAttempt(uuid4().hex, target, now, now + bound, bound, warnings + adjustments)
        self._attempt = attempt
        self.phase = "draining"
        self._notify()
        # Independent of the POST's/GET's lifetime and cache/region context.
        from .coherence.operation_cache import create_isolated_context_task
        coordinator = self._coordinate_restart(attempt)
        try:
            self._coordinator = create_isolated_context_task(coordinator, name="daemon-restart")
        except Exception:
            coordinator.close()
            self._fail_attempt(attempt, "control_failed")
            return 200, self.snapshot()
        return 202, {**self.snapshot(), "accepted": True}

    async def _coordinate_restart(self, attempt: _RestartAttempt) -> None:
        for job_id, (callback, task) in list(self._job_stops.items()):
            self._request_job_stop(job_id, callback, task, attempt)
        while True:
            event = self._changed
            if not self._busy(attempt):
                if self._exit_callback is None:
                    self._fail_attempt(attempt, "control_failed")
                    return
                self.phase = "exiting"
                self._notify()
                try:
                    self._exit_callback()
                except Exception:
                    self._fail_attempt(attempt, "control_failed")
                return
            remaining = attempt.deadline - self._clock()
            if remaining <= 0:
                self._fail_attempt(attempt, "drain_timeout")
                return
            await self._waiter(event, min(remaining, 0.1))

    def _fail_attempt(self, attempt: _RestartAttempt, reason: str) -> None:
        self.phase = "running"
        for job_id, (callback, task) in list(self._job_stops.items()):
            if not task.done() and (job_id, task) in attempt.requested_stops:
                self._call_boundary_stop(callback, None)
        attempt.result = {"status": "restart_failed", **RestartFailedError(reason, drain_timeout_s=attempt.bound).payload()}
        self._notify()

    async def wait_restart(self, restart_id: str) -> dict[str, Any]:
        while True:
            event = self._changed
            snapshot = self.snapshot(restart_id)
            if snapshot["status"] != "restarting" or self.phase == "exiting":
                return snapshot
            attempt = self._attempt
            await self._waiter(event, max(0.0, attempt.deadline - self._clock()))


def _parse_restart_request(body: Any) -> tuple[str, dict[str, Any], dict[str, Any], Any, tuple[IdentityWarning, ...]] | None:
    if not isinstance(body, dict) or type(body.get("protocol")) is not int or body["protocol"] != IDENTITY_PROTOCOL:
        return None
    if "drain_timeout_s" in body and type(body["drain_timeout_s"]) not in (int, float):
        return None  # Invalid control field type; never echo an arbitrary object.
    expected, target, requester = body.get("expected_instance_id"), body.get("target_identity"), body.get("requester")
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{32}", expected) is None:
        return None
    if not isinstance(target, dict) or not isinstance(requester, dict):
        return None
    if not isinstance(target.get("package_version"), str) or "commit" not in target or "commit_time" not in target:
        return None
    if target["commit"] is not None and not valid_commit(target["commit"]):
        return None
    if target["commit_time"] is not None and not valid_commit_time(target["commit_time"]):
        return None
    if not isinstance(requester.get("platform"), str) or not isinstance(requester.get("hostname"), str) or type(requester.get("is_wsl")) is not bool:
        return None
    extra = (set(body) - {"protocol", "expected_instance_id", "target_identity", "requester", "drain_timeout_s"}
             or set(target) - {"package_version", "commit", "commit_time"}
             or set(requester) - {"platform", "hostname", "is_wsl"})
    warnings = (unknown_warning("unrecognized identity metadata"),) if extra else ()
    return (expected, {key: target[key] for key in ("package_version", "commit", "commit_time")},
            {key: requester[key] for key in ("platform", "hostname", "is_wsl")}, body.get("drain_timeout_s"), warnings)


def merge_result_metadata(result: Any, key: str, value: dict[str, Any]) -> Any:
    """Merge the namespaced result metadata, never Implementation extras."""
    if result is None:
        return None
    if isinstance(result, dict):
        return {**result, "_meta": {**(result.get("_meta") or {}), key: value}}
    return result.model_copy(update={"meta": {**(result.meta or {}), key: value}})


# @beacon[
#   id=daemon-lifecycle@middleware,
#   role=DaemonLifecycleMiddleware: actual MCP tool/resource/prompt admission and negotiation projection,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Gates whole handler pipelines with shielded admission; initialize/discover expose the startup identity plus current phase without another probe; no active full lifespan means plain pass-through.,
# ]
class DaemonLifecycleMiddleware(Middleware):
    async def on_initialize(self, context, call_next):
        result = await call_next(context)
        controller = active_lifecycle()
        return merge_result_metadata(result, DAEMON_META_KEY, controller.metadata()) if controller is not None else result

    async def on_discover(self, context, call_next):
        return await self.on_initialize(context, call_next)

    async def _run(self, context, call_next, *, tool: bool):
        controller = active_lifecycle()
        if controller is None or controller._current_token() is not None:
            return await call_next(context)
        try:
            token = controller.admit_work(str(context.method))
        except RestartingError as exc:
            if tool:
                return exc.tool_result()
            raise exc.mcp_error() from None
        return await controller.run_admitted(token, call_next(context))

    async def on_call_tool(self, context, call_next):
        return await self._run(context, call_next, tool=True)

    async def on_read_resource(self, context, call_next):
        return await self._run(context, call_next, tool=False)

    async def on_get_prompt(self, context, call_next):
        return await self._run(context, call_next, tool=False)


# @beacon[
#   id=daemon-lifecycle@control-routes,
#   role=install_daemon_control_routes: authenticated same-origin restart and long-poll status,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Registers a control route on the same app before it is built; explicitly checks the existing bearer before parsing or changing state; only the controller accepts or joins one non-cutting restart.,
# ]
def install_daemon_control_routes(mcp: Any, token: str, path: str) -> None:
    @mcp.custom_route(path, methods=["GET", "POST"])
    async def restart(request: Request) -> JSONResponse:
        provided = request.headers.get("authorization", "")
        expected = f"Bearer {token}"
        if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            return JSONResponse({"status": "unauthorized", "success": False}, status_code=401)
        controller = active_lifecycle()
        if controller is None:
            return JSONResponse({"status": "restart_failed", **RestartFailedError("control_failed").payload()})
        if request.method == "POST":
            try:
                body = await request.json()
            except (ValueError, UnicodeError):
                body = None
            status, result = controller.request_restart(body)
            return JSONResponse(result, status_code=status)
        restart_id = request.query_params.get("restart_id")
        if request.query_params.get("wait") == "1" and restart_id is not None:
            return JSONResponse(await controller.wait_restart(restart_id))
        return JSONResponse(controller.snapshot(restart_id))
