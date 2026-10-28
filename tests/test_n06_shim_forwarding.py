"""N06 shim protocol on real SDK sessions and isolated fake endpoints.

Pure attach schedules use a fake negotiation/transport, not fake forwarding.
The real-SDK tests separately prove ProxyTool conversion and request isolation.
The old/new *process* upgrade witnesses remain separate. The cold-start
measurement and 100ms proxy-timeout witness stay serial; other real-SDK session
witnesses use private Scenario roots/ports with hang guards, not elapsed thresholds.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import Middleware
from fastmcp.server.middleware.dereference import DereferenceRefsMiddleware
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient, ProxyMetadataMiddleware
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, TextContent

from n06_harness import WAIT, Scenario
from workflowy_mcp import daemon, daemon_lifecycle as lifecycle, shim
from workflowy_mcp.daemon_identity import DAEMON_META_KEY, SHIM_META_KEY, InstalledIdentity


def identity(char="a", source_time=10, platform="linux", host="test-host"):
    return InstalledIdentity("1.0.1", char * 40, source_time, "git", platform, host, char * 32)


def metadata(value=None, **updates):
    value = value or identity()
    return {
        "protocol": 1, **value.source_payload(), "identity_kind": value.identity_kind,
        "platform": value.platform, "hostname": value.hostname, "instance_id": value.instance_id,
        "phase": "running", "drain_timeout_s": 3600, "restart_path": "/mcp/_nexus/restart",
        "restart_id": None, **updates,
    }


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    scenario = Scenario.create()
    scenario.install_environment(monkeypatch)
    monkeypatch.setenv("WORKFLOWY_MCP_SHIM_CALL_TIMEOUT_S", "0")
    lifecycle.set_active_lifecycle(None)
    fault = os.environ.get("N06_TEST_MUTATION", "")
    if fault == "shim_retry_forwarded":
        original = shim.HandshakingProxyClient.call_tool_mcp
        async def retry(self, *args, **kwargs):
            try:
                return await original(self, *args, **kwargs)
            except lifecycle.UpstreamOutcomeUnknownError:
                return await original(self, *args, **kwargs)
        monkeypatch.setattr(shim.HandshakingProxyClient, "call_tool_mcp", retry)
    elif fault == "shim_sent_is_not_sent":
        original = shim.HandshakingProxyClient.call_tool_mcp
        async def wrong(self, *args, **kwargs):
            try:
                return await original(self, *args, **kwargs)
            except lifecycle.UpstreamOutcomeUnknownError:
                raise lifecycle.DaemonUnavailableError(self.attach_state.url)
        monkeypatch.setattr(shim.HandshakingProxyClient, "call_tool_mcp", wrong)
    elif fault == "shim_compare_each_session":
        original = shim.AttachState.observe
        def repeated(self, peer):
            self.instance_id = None
            return original(self, peer)
        monkeypatch.setattr(shim.AttachState, "observe", repeated)
    elif fault == "shim_remove_capability":
        monkeypatch.setattr(shim, "can_replace_daemon", lambda *args: True)
    elif fault == "shim_all_posix_attach_only":
        original = shim.can_replace_daemon
        monkeypatch.setattr(shim, "can_replace_daemon", lambda platform, *args: platform == "win32" and original(platform, *args))
    elif fault == "shim_wrong_middleware_order":
        original = FastMCPProxy.__init__
        def backwards(self, *args, **kwargs):
            original(self, *args, **kwargs)
            self.middleware.reverse()
        monkeypatch.setattr(FastMCPProxy, "__init__", backwards)
    elif fault == "shim_swallow_typed":
        monkeypatch.setattr(shim, "_daemon_error", lambda error: None)
    elif fault == "shim_cached_cold_veto":
        original = shim.AttachState.ensure_reachable
        async def stale_veto(self, *, deadline, allow_spawn):
            return await original(self, deadline=deadline, allow_spawn=allow_spawn and (self.instance_id is None or self.replacement_capable))
        monkeypatch.setattr(shim.AttachState, "ensure_reachable", stale_veto)
    elif fault == "shim_loop_on_decline":
        original = shim.AttachState.restart_or_join
        async def forget_decline(self, *args, **kwargs):
            await original(self, *args, **kwargs)
            self.restart_declined = False
        monkeypatch.setattr(shim.AttachState, "restart_or_join", forget_decline)
    elif fault == "shim_exit_established":
        original = shim.ShimOutcomeMiddleware.on_discover
        async def forget_attach(self, *args, **kwargs):
            self._attached = False
            return await original(self, *args, **kwargs)
        monkeypatch.setattr(shim.ShimOutcomeMiddleware, "on_discover", forget_attach)
    elif fault == "shim_drop_received_reply":
        original = shim.HandshakingProxyClient.call_tool_mcp
        async def forget_reply(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            self._received_result = False
            return result
        monkeypatch.setattr(shim.HandshakingProxyClient, "call_tool_mcp", forget_reply)
    elif fault == "shim_emit_every_call":
        original = shim.AttachState.emit_warnings
        def repeated_warnings(self):
            self._printed_warnings.clear()
            return original(self)
        monkeypatch.setattr(shim.AttachState, "emit_warnings", repeated_warnings)
    elif fault == "shim_extra_identity_request":
        original = shim.HandshakingProxyClient.__aenter__
        async def extra_request(self):
            result = await original(self)
            await self.attach_state.control("GET", deadline=time.monotonic() + 3780)
            return result
        monkeypatch.setattr(shim.HandshakingProxyClient, "__aenter__", extra_request)
    elif fault == "shim_no_negotiation_reconnect":
        monkeypatch.setattr(shim, "_pre_forward_connection_lost", lambda error: False)
    elif fault == "shim_ignore_higher_target":
        async def wrong_replacement(self, deadline):
            await self.ensure_reachable(deadline=deadline, allow_spawn=True)
        monkeypatch.setattr(shim.AttachState, "replacement_ready", wrong_replacement)
    yield scenario
    lifecycle.set_active_lifecycle(None)


def state(scenario, local=None, wsl=False):
    return shim.AttachState(
        f"http://127.0.0.1:{scenario.env['WORKFLOWY_MCP_HTTP_PORT']}/mcp",
        "n06-fake-broker-token", local=local or identity(), local_is_wsl=wsl,
    )


@pytest.mark.parametrize(("local", "wsl", "peer", "relation", "capable", "warning"), [
    (identity(), False, metadata(), "equal", True, None),
    (identity("b", 20), False, metadata(), "local_newer", True, None),
    (identity("b", 20, "win32"), False, metadata(identity(platform="win32")), "local_newer", True, None),
    (identity("b", 20, "darwin"), False, metadata(identity(platform="darwin")), "local_newer", True, None),
    (identity("b", 20), True, metadata(identity(platform="win32")), "local_newer", False, "shim_newer_attach_only"),
    (identity("b", 20), False, metadata(identity(host="elsewhere")), "local_newer", False, "shim_newer_attach_only"),
    (identity("b", 20), False, metadata(identity(platform="win32")), "local_newer", False, "shim_newer_attach_only"),
    (identity("b", 1), False, metadata(), "peer_newer", True, "shim_older"),
    (identity("b", 10), False, metadata(), "unknown", True, "identity_unknown"),
    (identity(), False, None, "unknown", False, "legacy_daemon"),
    (identity(), False, {**metadata(), "protocol": 2}, "equal", False, "identity_unknown"),
])
def test_attach_comparison_and_affinity_warn_once_without_per_call_work(monkeypatch, capsys, isolated_environment, local, wsl, peer, relation, capable, warning):
    value = state(isolated_environment, local, wsl)
    calls = []
    original = shim.compare_install_identity
    def compare(*args):
        calls.append("comparison")
        return original(*args)
    monkeypatch.setattr(shim, "compare_install_identity", compare)
    value.observe(peer)
    assert value.relation == relation and value.replacement_capable is capable
    first = capsys.readouterr().err
    assert bool(first) is (warning is not None)
    if warning:
        assert warning in {item["code"] for item in value.metadata()["warnings"]}
    for _ in range(20):
        value.observe(peer)
    assert len(calls) == (0 if peer is None else 1)
    assert capsys.readouterr().err == ""


def test_new_incarnation_invalidates_only_the_four_allowed_component_caches(isolated_environment):
    value = state(isolated_environment)
    caches = SimpleNamespace(_tools_cache=1, _resources_cache=2, _templates_cache=3, _prompts_cache=4, unrelated=5)
    value.provider = caches
    value.observe(metadata())
    assert caches._tools_cache == 1
    value.observe(metadata(identity("b", 20)))
    assert vars(caches) == {"_tools_cache": None, "_resources_cache": None, "_templates_cache": None, "_prompts_cache": None, "unrelated": 5}
    caches._tools_cache = "refetched"
    value.observe(metadata(identity("b", 20)))
    assert caches._tools_cache == "refetched"


@pytest.mark.parametrize("failure", [TimeoutError, ConnectionError, RuntimeError])
def test_forwarding_calls_the_base_once_then_reports_unknown(monkeypatch, isolated_environment, failure):
    value = state(isolated_environment)
    client = shim.HandshakingProxyClient(value.url, attach_state=value)
    effects = []
    async def call(self, *args, **kwargs):
        effects.append((args, kwargs))
        assert client.invocation_state == "forwarded"
        raise failure("fake transport loss, not a success")
    monkeypatch.setattr(ProxyClient, "call_tool_mcp", call)
    async def run():
        try:
            await client.call_tool_mcp("write", {"value": "one"}, meta={"trace": "preserved"})
        except lifecycle.DaemonCallError as error:
            caught = error
        else:
            pytest.fail("forwarded loss returned success")
        assert len(effects) == 1  # Independent physical-invocation oracle, before taxonomy.
        assert isinstance(caught, lifecycle.UpstreamOutcomeUnknownError)
        error = caught.error
        assert error["outcome"] == "unknown" and error["retryable"] is False and error["forwarded"]
        assert error["recovery"] == "read_back_before_retry" and "retry_after" not in error
    asyncio.run(run())
    assert effects == [(("write", {"value": "one"}), {"meta": {"trace": "preserved"}})]


def test_known_restarting_and_other_upstream_errors_are_passed_through_without_resend(monkeypatch, isolated_environment):
    value = state(isolated_environment)
    client = shim.HandshakingProxyClient(value.url, attach_state=value)
    refusal = lifecycle.RestartingError("a" * 32).payload()
    result = CallToolResult(content=[TextContent(type="text", text=json.dumps(refusal))], structuredContent=refusal, isError=True, _meta={"original": 42})
    calls = []
    async def call(self, *args, **kwargs):
        calls.append(1)
        return result
    monkeypatch.setattr(ProxyClient, "call_tool_mcp", call)
    async def run():
        assert await client.call_tool_mcp("write", {}) is result
        assert client.invocation_state == "not_sent"
        assert result.meta == {"original": 42} and result.structured_content["error"]["retry_after"] == 30
        result.structured_content = {"success": False, "error": {"code": "ordinary_upstream_error"}}
        assert await client.call_tool_mcp("write", {}) is result
        assert client.invocation_state == "forwarded"
    asyncio.run(run())
    assert calls == [1, 1]


def test_real_proxy_negotiation_and_steady_calls_add_no_identity_request_or_comparison(monkeypatch, isolated_environment):
    async def run():
        value = state(isolated_environment)
        ctl = lifecycle.DaemonLifecycle(identity=identity())
        ctl.start()
        lifecycle.set_active_lifecycle(ctl)
        exchanges, effects, comparisons = [], [], []
        class Count(Middleware):
            async def on_initialize(self, context, call_next):
                exchanges.append("initialize")
                return await call_next(context)
        upstream = FastMCP("real-upstream", version="1.2.3", instructions="upstream instructions", middleware=[Count(), lifecycle.DaemonLifecycleMiddleware()])
        @upstream.tool
        async def write(value: str) -> dict:
            effects.append(value)
            return {"value": value}
        original = shim.compare_install_identity
        def compare(*args):
            comparisons.append(1)
            return original(*args)
        monkeypatch.setattr(shim, "compare_install_identity", compare)
        probes = []
        async def reachable(**kwargs):
            probes.append("tcp-probe")
        monkeypatch.setattr(value, "ensure_reachable", reachable)
        async def forbidden(*args, **kwargs):
            pytest.fail("equal healthy attach issued a control/identity request")
        monkeypatch.setattr(value, "control", forbidden)
        def factory():
            return shim.HandshakingProxyClient(upstream, attach_state=value, timeout=0)
        proxy = FastMCPProxy(client_factory=factory, identity="upstream", middleware=[shim.ShimOutcomeMiddleware(value)], provider_error_strategy="raise")
        assert [type(item) for item in proxy.middleware] == [shim.ShimOutcomeMiddleware, DereferenceRefsMiddleware, ProxyMetadataMiddleware]
        value.provider = proxy.middleware[-1].provider
        async with Client(proxy, mode="legacy") as downstream:
            initialized = downstream.initialize_result
            assert initialized.server_info.name == "real-upstream"
            assert initialized.server_info.version == "1.2.3" and initialized.instructions == "upstream instructions"
            assert initialized.meta[DAEMON_META_KEY] == ctl.metadata()
            assert initialized.meta[SHIM_META_KEY] == {"protocol": 1, "replacement_capable": True, "relation": "equal", "warnings": []}
            for item in ("first", "second", "third"):
                result = await downstream.call_tool_mcp("write", {"value": item})
                assert result.structured_content == {"value": item} and not result.is_error
            assert effects == ["first", "second", "third"]
        assert len(exchanges) == len(probes) and len(exchanges) >= 4
        assert comparisons == [1]
    asyncio.run(run())


def test_outcome_middleware_preserves_pre_forward_and_unknown_fields(isolated_environment):
    async def run():
        value = state(isolated_environment)
        mw = shim.ShimOutcomeMiddleware(value)
        for error in (lifecycle.DaemonUnavailableError(value.url), lifecycle.RestartFailedError("drain_timeout"), lifecycle.UpstreamOutcomeUnknownError()):
            async def fails(context):
                raise error
            result = await mw.on_call_tool(None, fails)
            assert result.is_error and result.structured_content == error.payload()
            assert json.loads(result.content[0].text) == error.payload()
    asyncio.run(run())


def test_real_proxy_preserves_typed_failure_before_forward_and_recovers_next_call(monkeypatch, isolated_environment):
    async def run():
        value = state(isolated_environment)
        effects, failing = [], [False]
        upstream = FastMCP("pre-forward")
        @upstream.tool
        async def write(value: str) -> dict:
            effects.append(value)
            return {"value": value}
        async def reachable(**kwargs):
            if failing[0]:
                raise lifecycle.DaemonUnavailableError(value.url)
        monkeypatch.setattr(value, "ensure_reachable", reachable)
        proxy = FastMCPProxy(client_factory=lambda: shim.HandshakingProxyClient(upstream, attach_state=value), identity="upstream", middleware=[shim.ShimOutcomeMiddleware(value)], provider_error_strategy="raise")
        async with Client(proxy, mode="legacy") as downstream:
            await downstream.list_tools()  # Prime its normal component catalog.
            failing[0] = True
            refusal = await downstream.call_tool_mcp("write", {"value": "not-sent"})
            assert refusal.is_error and refusal.structured_content["error"]["code"] == "daemon_unavailable"
            assert refusal.structured_content["error"]["outcome"] == "not_sent" and effects == []
            failing[0] = False
            result = await downstream.call_tool_mcp("write", {"value": "independent"})
            assert result.structured_content == {"value": "independent"} and effects == ["independent"]
    asyncio.run(run())


def test_downstream_cancellation_logs_unknown_without_cancelling_accepted_body(monkeypatch, capsys, isolated_environment):
    async def run():
        value = state(isolated_environment)
        ctl = lifecycle.DaemonLifecycle(identity=identity())
        ctl.start()
        lifecycle.set_active_lifecycle(ctl)
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        effects = []
        upstream = FastMCP("cancel", middleware=[lifecycle.DaemonLifecycleMiddleware()])
        @upstream.tool
        async def write() -> str:
            effects.append("one-write")
            started.set()
            await release.wait()
            finished.set()
            return "done"
        async def reachable(**kwargs):
            pass
        monkeypatch.setattr(value, "ensure_reachable", reachable)
        proxy = FastMCPProxy(client_factory=lambda: shim.HandshakingProxyClient(upstream, attach_state=value, timeout=0), identity="upstream", middleware=[shim.ShimOutcomeMiddleware(value)])
        async with Client(proxy, mode="legacy", timeout=0) as downstream:
            call = asyncio.create_task(downstream.call_tool_mcp("write", {}))
            await asyncio.wait_for(started.wait(), WAIT)
            call.cancel()  # Host cancellation, never a restart mechanism.
            try:
                with pytest.raises(asyncio.CancelledError):
                    await call
                # The actual accepted body remains alive while cancellation
                # propagates through both SDK session dispatchers.
                assert effects == ["one-write"] and ctl.snapshot()["active_work"] == 1
            finally:
                release.set()
            await asyncio.wait_for(finished.wait(), WAIT)
        assert effects == ["one-write"]
    asyncio.run(run())
    assert "outcome is unknown" in capsys.readouterr().err


def install_fake_negotiation(monkeypatch, value, public):
    """No SDK internals: replace just the public base context-manager seam."""
    class Negotiated(shim.HandshakingProxyClient):
        @property
        def initialize_result(self):
            return SimpleNamespace(meta={DAEMON_META_KEY: public[0]})
    async def enter(self):
        await asyncio.sleep(0)
        return self
    async def leave(self, *args):
        pass
    async def reachable(**kwargs):
        pass
    monkeypatch.setattr(ProxyClient, "__aenter__", enter)
    monkeypatch.setattr(ProxyClient, "__aexit__", leave)
    monkeypatch.setattr(value, "ensure_reachable", reachable)
    return Negotiated


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
@pytest.mark.parametrize("count", [2, 20])
def test_concurrent_native_attaches_singleflight_before_forwarding(monkeypatch, isolated_environment, platform, count):
    async def run():
        value = state(isolated_environment, identity("b", 20, platform))
        public = [metadata(identity(platform=platform))]
        client_type = install_fake_negotiation(monkeypatch, value, public)
        requested, release, events = asyncio.Event(), asyncio.Event(), []
        async def restart(peer, **kwargs):
            events.append(("restart", peer["instance_id"]))
            requested.set()
            await release.wait()
            public[0] = metadata(value.local)
        monkeypatch.setattr(value, "restart_or_join", restart)
        async def attach():
            async with client_type(value.url, attach_state=value):
                events.append(("ready", value.instance_id))
        tasks = [asyncio.create_task(attach()) for _ in range(count)]
        observing = asyncio.create_task(requested.wait())
        try:
            await asyncio.wait_for(asyncio.wait([*tasks, observing], return_when=asyncio.FIRST_COMPLETED), WAIT)
            assert requested.is_set() and events == [("restart", "a" * 32)]
        finally:
            release.set()
            requested.set()
            await asyncio.wait_for(asyncio.gather(*tasks, observing), WAIT)
        assert events.count(("restart", "a" * 32)) == 1
        assert events.count(("ready", "b" * 32)) == count
    asyncio.run(run())


@pytest.mark.parametrize("peer", [None, metadata(protocol=2), metadata(phase={"unknown": True}), metadata(hostname=None)])
def test_unrecognized_or_legacy_peer_attaches_without_control(monkeypatch, isolated_environment, peer):
    async def run():
        value = state(isolated_environment, identity("b", 20))
        client_type = install_fake_negotiation(monkeypatch, value, [peer])
        async def forbidden(*args, **kwargs):
            pytest.fail("unknown/legacy peer was stopped")
        monkeypatch.setattr(value, "restart_or_join", forbidden)
        async with client_type(value.url, attach_state=value):
            assert value.warnings
    asyncio.run(run())


def test_wsl_newer_peer_warns_and_forwards_without_stop_or_spawn(monkeypatch, capsys, isolated_environment):
    async def run():
        value = state(isolated_environment, identity("b", 20), True)
        client_type = install_fake_negotiation(monkeypatch, value, [metadata(identity(platform="win32"))])
        async def forbidden(*args, **kwargs):
            pytest.fail("incapable client requested a restart")
        monkeypatch.setattr(value, "restart_or_join", forbidden)
        async with client_type(value.url, attach_state=value):
            assert value.metadata()["replacement_capable"] is False
        async with client_type(value.url, attach_state=value):
            pass
        assert capsys.readouterr().err.count("this shim is newer") == 1
    asyncio.run(run())


@pytest.mark.parametrize("changed", [False, True])
def test_control_loss_renegotiates_replacement_once_or_preserves_same_peer_failure(monkeypatch, isolated_environment, changed):
    async def run():
        value = state(isolated_environment, identity("b", 20))
        public = [metadata()]
        client_type = install_fake_negotiation(monkeypatch, value, public)
        attempts = []
        monkeypatch.setattr(daemon, "is_daemon_listening", lambda: True)
        async def raced(peer, **kwargs):
            attempts.append(peer["instance_id"])
            if changed:
                public[0] = metadata(value.local)
            raise lifecycle.RestartFailedError("control_failed")
        monkeypatch.setattr(value, "restart_or_join", raced)
        client = client_type(value.url, attach_state=value)
        if changed:
            async with client:
                assert value.instance_id == "b" * 32 and value.relation == "equal"
        else:
            with pytest.raises(lifecycle.RestartFailedError) as raised:
                async with client:
                    pytest.fail("same serving daemon falsely counted as replacement")
            assert raised.value.error["reason"] == "control_failed"
        assert attempts == ["a" * 32] and client.invocation_state == "not_sent"
    asyncio.run(run())


@pytest.mark.parametrize("loss", ["connection", "backend_transport", "sdk_closed", "wrapped", "http_status", "configuration"])
def test_probe_to_initialize_exit_race_reconnects_only_before_forwarding(monkeypatch, isolated_environment, loss):
    import httpx2
    from mcp_types import CONNECTION_CLOSED

    async def run():
        value = state(isolated_environment, identity("b", 20))
        public = [metadata(value.local)]
        client_type = install_fake_negotiation(monkeypatch, value, public)
        value.observe(metadata())  # Previously observed old daemon, not a send.
        attempts, probes, forwarded = [], [], []
        if loss == "connection":
            error = ConnectionError("test socket closed before initialize")
        elif loss == "backend_transport":
            error = httpx2.ConnectError("test backend connection refused")
        elif loss == "sdk_closed":
            error = MCPError(code=CONNECTION_CLOSED, message="Connection closed")
        elif loss == "wrapped":
            error = RuntimeError("test SDK connection wrapper")
            error.__cause__ = ConnectionError("test inner connection loss")
        elif loss == "http_status":
            request = httpx2.Request("POST", value.url)
            error = httpx2.HTTPStatusError("test auth failure", request=request, response=httpx2.Response(401, request=request))
        else:
            error = ValueError("test invalid configuration")
        async def enter(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise error
            return self
        async def reachable(**kwargs):
            probes.append(kwargs["allow_spawn"])
        async def forbidden(*args, **kwargs):
            forwarded.append(1)
            pytest.fail("an attach replayed a tool")
        monkeypatch.setattr(ProxyClient, "__aenter__", enter)
        monkeypatch.setattr(ProxyClient, "call_tool_mcp", forbidden)
        monkeypatch.setattr(value, "ensure_reachable", reachable)
        client = client_type(value.url, attach_state=value)
        if loss in {"http_status", "configuration"}:
            with pytest.raises(lifecycle.RestartFailedError) as raised:
                async with client:
                    pytest.fail("configuration/auth error was silently retried")
            assert raised.value.error["reason"] == "control_failed" and attempts == [1]
        else:
            async with client:
                assert value.instance_id == value.local.instance_id and value.relation == "equal"
            assert attempts == [1, 1] and probes == [True, True]
        assert client.invocation_state == "not_sent" and forwarded == []
    asyncio.run(run())


def test_reconnect_during_current_higher_target_attempt_never_spawns_known_older_build(monkeypatch, isolated_environment):
    async def run():
        value = state(isolated_environment, identity("b", 20))
        public = [metadata()]
        client_type = install_fake_negotiation(monkeypatch, value, public)
        calls, probes, waits = [], [], []
        newer = identity("c", 30)
        async def restart(peer, **kwargs):
            value.accepted_target = newer.source_payload()
            public[0] = metadata(newer)
        async def enter(self):
            calls.append(1)
            if len(calls) == 2:
                raise ConnectionError("test replacement negotiation raced closure")
            return self
        async def reachable(**kwargs):
            assert value.accepted_target is None, "deliberately cold-spawned below the current higher target"
            probes.append(kwargs["allow_spawn"])
        def listening():
            waits.append(1)
            return len(waits) > 1  # The external higher participant becomes ready.
        monkeypatch.setattr(value, "restart_or_join", restart)
        monkeypatch.setattr(value, "ensure_reachable", reachable)
        monkeypatch.setattr(ProxyClient, "__aenter__", enter)
        monkeypatch.setattr(daemon, "is_daemon_listening", listening)
        async with client_type(value.url, attach_state=value) as client:
            assert value.instance_id == newer.instance_id and value.relation == "peer_newer"
            assert client.invocation_state == "not_sent"
        assert calls == [1, 1, 1] and probes == [True] and len(waits) == 2
    asyncio.run(run())


def test_incapable_missing_listener_has_not_sent_remedy_without_spawn(monkeypatch, isolated_environment):
    async def run():
        value = state(isolated_environment, wsl=True)
        monkeypatch.setattr(daemon, "is_daemon_listening", lambda: False)
        def forbidden(**kwargs):
            pytest.fail("WSL tried to spawn a full server")
        monkeypatch.setattr(daemon, "ensure_daemon_running", forbidden)
        with pytest.raises(lifecycle.DaemonUnavailableError) as raised:
            async with shim.HandshakingProxyClient(value.url, attach_state=value):
                pytest.fail("missing peer was ready")
        assert raised.value.error["outcome"] == "not_sent" and raised.value.error["retryable"] is True
    asyncio.run(run())


def g1_no_network(monkeypatch):
    """Nonlocal fixtures may exercise real policy, never an actual connect or spawn."""
    import socket
    import subprocess
    def forbidden(*args, **kwargs):
        pytest.fail("G1 nonlocal fixture attempted network or Popen")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def g1_mutation(monkeypatch):
    """Neutralize only the named CF seam, in memory; no production fault flag."""
    import inspect
    import textwrap
    from workflowy_mcp import daemon_identity
    fault = os.environ.get("CUT_G1_TEST_MUTATION", "")
    choices = {
        "main_locality_bypass": (shim, "main", "_can_spawn_local_daemon(url, local_is_wsl=local_is_wsl)", "not local_is_wsl"),
        "recovery_locality_bypass": (shim.AttachState, "ensure_reachable", "allow_spawn and self.local_spawn_allowed", "allow_spawn"),
        "capability_locality_bypass": (shim.AttachState, "observe", "self.local_spawn_allowed and can_replace_daemon", "can_replace_daemon"),
        "nonlocal_expiry_native": (shim.AttachState, "replacement_ready", "if self.local_spawn_allowed:", "if not self.local_is_wsl:"),
        "identity_old_kind": (daemon_identity, "_source", '"git" if set(value) == {"package_version", "commit", "commit_time"} else None', '"git"'),
    }
    if fault in choices:
        owner, name, old, new = choices[fault]
        original = getattr(owner, name)
        text = textwrap.dedent(inspect.getsource(original))
        assert text.count(old) == 1, (fault, text)
        namespace = dict(original.__globals__)
        exec(compile(text.replace(old, new), "<cut-g1-neutralized>", "exec"), namespace)
        monkeypatch.setattr(owner, name, namespace[name])
    elif fault == "spawn_helper_bypass":
        monkeypatch.setattr(shim, "_can_spawn_local_daemon", lambda url, **kwargs: True)


def g1_native_unavailable(url):
    return {
        "code": "daemon_unavailable",
        "message": f"Workflowy daemon is not reachable at {url}; this shim starts a local daemon only when it dials loopback "
        "(localhost, 127.0.0.1 or ::1). Start the daemon on that host with workflowy-mcp --transport http, "
        "or dial loopback (leave WORKFLOWY_MCP_HTTP_DIAL_HOST unset and the bind host at its default) "
        "so this shim can start it. No local full-server fallback was attempted.",
        "retryable": True, "retry_after": 30, "outcome": "not_sent",
        "recovery": "start_daemon_on_that_host_or_dial_loopback",
    }


def test_local_spawn_applicability_is_lexical(monkeypatch, isolated_environment):
    g1_no_network(monkeypatch)
    g1_mutation(monkeypatch)
    local = ["localhost", "LOCALHOST.", "127.0.0.1", "127.0.0.2", "[::1]", "[::ffff:127.0.0.1]"]
    nonlocal_hosts = ["192.0.2.1", "ordinary.invalid", "", "[broken"]
    for host in local + nonlocal_hosts:
        url = f"http://{host}:54321/mcp"
        assert shim._can_spawn_local_daemon(url, local_is_wsl=False) is (host in local), host
        assert shim._can_spawn_local_daemon(url, local_is_wsl=True) is False, host


def test_nonlocal_destination_never_spawns_on_initial_or_recovery_attach(monkeypatch, capsys, isolated_environment):
    g1_no_network(monkeypatch)
    g1_mutation(monkeypatch)
    scenario = isolated_environment
    scenario.assert_endpoint()
    monkeypatch.setenv(daemon.HTTP_DIAL_HOST_ENV_VAR, "192.0.2.1")
    url = daemon.daemon_url()
    assert url == f"http://192.0.2.1:{scenario.env[daemon.HTTP_PORT_ENV_VAR]}/mcp"
    def forbidden_spawn(**kwargs):
        pytest.fail("forbidden nonlocal local spawn at ensure_daemon_running")
    monkeypatch.setattr(daemon, "ensure_daemon_running", forbidden_spawn)
    monkeypatch.setattr(daemon, "is_daemon_listening", lambda: False)
    monkeypatch.setattr(shim.server_bootstrap, "is_wsl", lambda: False)
    monkeypatch.setattr(shim, "installed_identity", identity)
    with pytest.raises(SystemExit) as exit_info:
        shim.main()
    assert exit_info.value.code == 2
    assert capsys.readouterr().err == f"[workflowy-mcp shim] {g1_native_unavailable(url)['message']}\n"

    async def run():
        value = shim.AttachState(url, "g1-fake-token", local=identity(), local_is_wsl=False)
        for cached in (False, True):
            if cached:
                value.observe(metadata())
            with pytest.raises(lifecycle.DaemonUnavailableError) as raised:
                await value.ensure_reachable(deadline=time.monotonic() + 3780, allow_spawn=True)
            assert raised.value.error == g1_native_unavailable(url)
            with pytest.raises(lifecycle.DaemonUnavailableError) as raised:
                async with shim.HandshakingProxyClient(url, attach_state=value):
                    pytest.fail("nonlocal missing target attached")
            assert raised.value.error == g1_native_unavailable(url)
        middleware = shim.ShimOutcomeMiddleware(value, console=True)
        async def initialized(context):
            return {"_meta": {}}
        await middleware.on_initialize(None, initialized)
        client_type = install_fake_negotiation(monkeypatch, value, [metadata()])
        monkeypatch.setattr(value, "ensure_reachable", shim.AttachState.ensure_reachable.__get__(value))
        effects = []
        async def forwarded(self, *args, **kwargs):
            effects.append("one send")
            return CallToolResult(content=[TextContent(type="text", text="ok")])
        monkeypatch.setattr(ProxyClient, "call_tool_mcp", forwarded)
        async def invoke(context):
            async with client_type(url, attach_state=value) as client:
                return await client.call_tool_mcp("fixture", {})
        refused = await middleware.on_call_tool(None, invoke)
        assert refused.is_error and refused.structured_content == {"success": False, "error": g1_native_unavailable(url)}
        monkeypatch.setattr(daemon, "is_daemon_listening", lambda: True)
        result = await middleware.on_call_tool(None, invoke)
        assert not result.is_error and effects == ["one send"]
        probes = iter((True, False))
        monkeypatch.setattr(daemon, "is_daemon_listening", lambda: next(probes))
        async def lost(self):
            raise ConnectionError("fixture probe-to-initialize loss")
        monkeypatch.setattr(ProxyClient, "__aenter__", lost)
        with pytest.raises(lifecycle.DaemonUnavailableError) as raised:
            async with client_type(url, attach_state=value):
                pytest.fail("nonlocal initialize loss spawned")
        assert raised.value.error == g1_native_unavailable(url) and effects == ["one send"]
    asyncio.run(run())


def test_nonlocal_reachable_peer_is_never_stopped(monkeypatch, capsys, isolated_environment):
    g1_no_network(monkeypatch)
    g1_mutation(monkeypatch)
    isolated_environment.assert_endpoint()
    url = f"http://192.0.2.1:{isolated_environment.env[daemon.HTTP_PORT_ENV_VAR]}/mcp"
    value = shim.AttachState(url, "g1-fake-token", local=identity("b", 20), local_is_wsl=False)
    peer = metadata()
    value.observe(peer)
    assert value.relation == "local_newer" and value.replacement_capable is False, "nonlocal replacement capability"
    async def run():
        client_type = install_fake_negotiation(monkeypatch, value, [peer])
        monkeypatch.setattr(value, "ensure_reachable", shim.AttachState.ensure_reachable.__get__(value))
        monkeypatch.setattr(daemon, "is_daemon_listening", lambda: True)
        def forbidden_spawn(**kwargs):
            pytest.fail("nonlocal reachable peer spawned")
        monkeypatch.setattr(daemon, "ensure_daemon_running", forbidden_spawn)
        controls, effects = [], []
        clock = [0.0]
        async def control(method, **kwargs):
            controls.append(method)
            assert method == "GET", "nonlocal shim stopped a peer"
            clock[0] = 5000.0
            return 202, {"status": "restarting", "phase": "exiting"}
        monkeypatch.setattr(value, "control", control)
        async def forward(self, *args, **kwargs):
            effects.append("forwarded")
            return CallToolResult(content=[])
        monkeypatch.setattr(ProxyClient, "call_tool_mcp", forward)
        for _ in range(2):
            async with client_type(url, attach_state=value) as client:
                if not effects:
                    await client.call_tool_mcp("advertised", {})
        assert controls == [] and effects == ["forwarded"]
        monkeypatch.setattr(shim, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(daemon, "is_daemon_listening", lambda: False)
        draining = {**peer, "phase": "draining", "restart_id": "d" * 32}
        with pytest.raises(lifecycle.DaemonUnavailableError) as raised:
            await value.restart_or_join(draining, deadline=3780, started=0)
        assert raised.value.error == g1_native_unavailable(url), "nonlocal expiry remedy"
        assert controls == ["GET"] and effects == ["forwarded"]
    asyncio.run(run())
    assert capsys.readouterr().err.count("this shim is newer") == 1
    assert [warning.code for warning in value.warnings] == ["shim_newer_attach_only"]


def test_missing_full_identity_kind_warns_once_and_forwards(monkeypatch, capsys, isolated_environment):
    g1_no_network(monkeypatch)
    g1_mutation(monkeypatch)
    isolated_environment.assert_endpoint()
    value = state(isolated_environment, identity("b", 20))
    peer = metadata()
    del peer["identity_kind"]
    comparisons, effects = [], []
    original = shim.compare_install_identity
    def compare(*args):
        comparisons.append(1)
        return original(*args)
    monkeypatch.setattr(shim, "compare_install_identity", compare)
    value.observe(peer)
    assert value.relation == "unknown", "missing-kind peer was source-ordered"
    assert value.replacement_capable is True
    assert len(value.metadata()["warnings"]) == 1 and value.warnings[0].code == "identity_unknown"
    assert "unrecognized identity metadata" in value.warnings[0].message
    async def run():
        client_type = install_fake_negotiation(monkeypatch, value, [peer])
        async def forbidden(*args, **kwargs):
            pytest.fail("missing-kind peer was stopped")
        monkeypatch.setattr(value, "control", forbidden)
        async def forward(self, *args, **kwargs):
            effects.append("sent")
            return CallToolResult(content=[TextContent(type="text", text="advertised surface")])
        monkeypatch.setattr(ProxyClient, "call_tool_mcp", forward)
        async with client_type(value.url, attach_state=value) as client:
            reply = await client.call_tool_mcp("advertised", {})
            assert not reply.is_error and reply.content[0].text == "advertised surface"
        for _ in range(20):
            value.observe(peer)
        async with client_type(value.url, attach_state=value):
            pass
    asyncio.run(run())
    assert comparisons == [1] and effects == ["sent"]
    assert capsys.readouterr().err.count("installed identity cannot be ordered reliably") == 1


@pytest.mark.parametrize("peer", [None, metadata(protocol=2), metadata(hostname=None), metadata(identity(platform="win32")), metadata(identity("c", 30))])
@pytest.mark.parametrize("wsl", [False, True])
def test_missing_listener_cold_start_ignores_cached_peer_but_never_wsl(monkeypatch, isolated_environment, peer, wsl):
    """ZOV1IB's independent-call transition is not replacement of a live peer."""
    async def run():
        value = state(isolated_environment, identity("b", 20), wsl)
        public = [peer]
        client_type = install_fake_negotiation(monkeypatch, value, public)
        async with client_type(value.url, attach_state=value):
            pass
        # Restore just the real reachability seam after a genuine observation.
        monkeypatch.setattr(value, "ensure_reachable", shim.AttachState.ensure_reachable.__get__(value))
        listening, spawns = [False], []
        monkeypatch.setattr(daemon, "is_daemon_listening", lambda: listening[0])
        def spawn(**kwargs):
            spawns.append(1)
            listening[0] = True
            public[0] = metadata(value.local)
            return True
        monkeypatch.setattr(daemon, "ensure_daemon_running", spawn)
        if wsl:
            with pytest.raises(lifecycle.DaemonUnavailableError) as raised:
                async with client_type(value.url, attach_state=value):
                    pytest.fail("WSL cold-spawned")
            assert raised.value.error["outcome"] == "not_sent" and raised.value.error["code"] == "daemon_unavailable"
            assert spawns == []
        else:
            async with client_type(value.url, attach_state=value):
                assert value.instance_id == value.local.instance_id
            assert spawns == [1]
    asyncio.run(run())


@pytest.mark.parametrize("status", ["not_required", "attach_only"])
def test_authoritative_control_decline_lands_forwarding_instead_of_reposting(monkeypatch, capsys, isolated_environment, status):
    async def run():
        value = state(isolated_environment, identity("b", 20))
        public = [metadata()]
        client_type = install_fake_negotiation(monkeypatch, value, public)
        calls = []
        warning = lifecycle.attach_only_warning("linux").payload()
        async def control(method, **kwargs):
            calls.append(method)
            assert len(calls) == 1, "authoritative refusal became a restart loop"
            return 200, {"status": status, "daemon": public[0], "warnings": [warning]}
        monkeypatch.setattr(value, "control", control)
        for _ in range(3):
            async with client_type(value.url, attach_state=value):
                assert value.restart_declined
                assert warning in value.metadata()["warnings"]
        assert calls == ["POST"]
    asyncio.run(run())
    assert capsys.readouterr().err.count("this shim is newer") == 1


def test_only_initial_console_attach_failure_exits_and_warnings_are_not_repeated(capsys, isolated_environment):
    async def run():
        value = state(isolated_environment, identity("b", 1))
        value.observe(metadata())
        capsys.readouterr()
        middleware = shim.ShimOutcomeMiddleware(value, console=True)
        error = lifecycle.RestartFailedError("control_failed")
        async def fails(context):
            raise error
        with pytest.raises(SystemExit) as raised:
            await middleware.on_initialize(None, fails)
        assert raised.value.code == 2
        async def succeeds(context):
            return {"_meta": {"kept": True}}
        result = await middleware.on_initialize(None, succeeds)
        assert result["_meta"]["kept"] and result["_meta"][SHIM_META_KEY]["relation"] == "peer_newer"
        capsys.readouterr()
        await middleware.on_discover(None, succeeds)
        assert capsys.readouterr().err == ""
        with pytest.raises(MCPError) as raised:
            await middleware.on_discover(None, fails)
        assert raised.value.data == error.error
    asyncio.run(run())


@pytest.mark.parametrize("payload", [lifecycle.RestartingError("f" * 32).payload(), {"success": False, "error": {"code": "ordinary_upstream_error"}}, {"value": "known-success"}])
def test_received_tool_result_is_authoritative_even_if_transport_teardown_fails(monkeypatch, isolated_environment, payload):
    async def run():
        value = state(isolated_environment)
        client = shim.HandshakingProxyClient(value.url, attach_state=value)
        result = CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))], structuredContent=payload, isError=payload.get("success") is False, _meta={"original": 42})
        calls = []
        async def call(self, *args, **kwargs):
            calls.append(1)
            return result
        async def leave(self, *args):
            raise ConnectionError("fake loss AFTER the reply")
        monkeypatch.setattr(ProxyClient, "call_tool_mcp", call)
        monkeypatch.setattr(ProxyClient, "__aexit__", leave)
        assert await client.call_tool_mcp("write", {}) is result
        assert await client.__aexit__(None, None, None) is None
        assert result.meta == {"original": 42} and result.structured_content == payload and calls == [1]
    asyncio.run(run())


def test_control_uses_derived_origin_bearer_no_redirect_and_long_wait(monkeypatch, isolated_environment):
    async def run():
        value = state(isolated_environment)
        actual, calls = httpx.AsyncClient, []
        def reply(request):
            assert request.url.host == "127.0.0.1" and request.url.port not in (8765, 8766)
            assert request.url.path == "/mcp/_nexus/restart"
            assert request.headers["authorization"] == "Bearer n06-fake-broker-token"
            calls.append((request.method, dict(request.url.params)))
            return httpx.Response(202, json={"status": "restarting", "target_identity": identity().source_payload()})
        def client(**kwargs):
            assert kwargs["timeout"] >= 3600 and kwargs["follow_redirects"] is False
            return actual(transport=httpx.MockTransport(reply), **kwargs)
        monkeypatch.setattr(shim.httpx, "AsyncClient", client)
        await value.control("POST", deadline=time.monotonic() + 3780, body={"fake": "safe"})
        await value.control("GET", deadline=time.monotonic() + 3780, restart_id="f" * 32)
        assert calls == [("POST", {}), ("GET", {"restart_id": "f" * 32, "wait": "1"})]
        assert value.accepted_target == identity().source_payload()
    asyncio.run(run())


@pytest.mark.parametrize("status", [301, 400, 401, 500])
def test_bad_control_reply_is_sanitized_typed_failure(monkeypatch, isolated_environment, status):
    async def run():
        value = state(isolated_environment)
        actual = httpx.AsyncClient
        def client(**kwargs):
            return actual(transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"status": "bad"})), **kwargs)
        monkeypatch.setattr(shim.httpx, "AsyncClient", client)
        with pytest.raises(lifecycle.RestartFailedError) as raised:
            await value.control("POST", deadline=time.monotonic() + 3780, body={})
        assert raised.value.error["reason"] == "control_failed"
        assert "n06-fake-broker-token" not in str(raised.value)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["drain_timeout", "control_failed", "exit_timeout", "spawn_failed"])
def test_restart_failure_reasons_and_larger_accepted_bound_survive(monkeypatch, isolated_environment, failure):
    async def run():
        value = state(isolated_environment, identity("b", 20))
        deadlines = []
        replies = [
            {"status": "restarting", "phase": "draining", "restart_id": "f" * 32, "drain_timeout_s": 7200},
            {"status": "restart_failed", "error": {"reason": failure}, "drain_timeout_s": 7200},
        ]
        async def control(method, **kwargs):
            deadlines.append(kwargs["deadline"])
            return 202, replies.pop(0)
        monkeypatch.setattr(value, "control", control)
        started = time.monotonic()
        with pytest.raises(lifecycle.RestartFailedError) as raised:
            await value.restart_or_join(metadata(), started=started, deadline=started + 3780)
        assert raised.value.error["reason"] == failure
        assert deadlines == [started + 3780, started + 7380]
    asyncio.run(run())


@pytest.mark.parametrize("method", ["read_resource", "get_prompt"])
def test_resource_prompt_not_sent_error_is_preserved_and_loss_is_unknown(monkeypatch, isolated_environment, method):
    async def run():
        value = state(isolated_environment)
        client = shim.HandshakingProxyClient(value.url, attach_state=value)
        error = lifecycle.RestartingError("f" * 32).mcp_error()
        async def refuses(*args, **kwargs):
            raise error
        monkeypatch.setattr(ProxyClient, method, refuses)
        with pytest.raises(MCPError) as raised:
            await getattr(client, method)("fake")
        assert raised.value is error and client.invocation_state == "not_sent"
        async def loses(*args, **kwargs):
            raise ConnectionError("fake peer loss")
        monkeypatch.setattr(ProxyClient, method, loses)
        with pytest.raises(MCPError) as raised:
            await getattr(client, method)("fake")
        assert raised.value.code == -32071 and raised.value.data["retryable"] is False
    asyncio.run(run())


def test_two_real_downstream_sessions_with_equal_ids_never_cross_replies(monkeypatch, isolated_environment):
    async def run():
        value = state(isolated_environment)
        ctl = lifecycle.DaemonLifecycle(identity=identity())
        ctl.start()
        lifecycle.set_active_lifecycle(ctl)
        effects, incoming = [], []
        both = asyncio.Event()
        upstream = FastMCP("correlation", middleware=[lifecycle.DaemonLifecycleMiddleware()])
        @upstream.tool
        async def write(value: str) -> dict:
            effects.append(value)
            if len(effects) == 2:
                both.set()
            await both.wait()
            return {"reply_for": value}
        async def reachable(**kwargs):
            pass
        monkeypatch.setattr(value, "ensure_reachable", reachable)
        class FrontIds(Middleware):
            async def on_call_tool(self, context, call_next):
                incoming.append(context.fastmcp_context.request_context.request_id)
                return await call_next(context)
        proxy = FastMCPProxy(client_factory=lambda: shim.HandshakingProxyClient(upstream, attach_state=value, timeout=0), identity="upstream", middleware=[shim.ShimOutcomeMiddleware(value), FrontIds()])
        # Raw public HTTP messages deliberately use the same numeric id=1 in
        # two separate clients; no SDK request-id counter is patched.
        import uvicorn
        app = proxy.http_app(path="/mcp", stateless_http=True, json_response=True)
        started = asyncio.Event()
        class Server(uvicorn.Server):
            async def startup(self, *args, **kwargs):
                await super().startup(*args, **kwargs)
                if self.started:
                    started.set()
        server = Server(uvicorn.Config(app, host="127.0.0.1", port=int(isolated_environment.env["WORKFLOWY_MCP_HTTP_PORT"]), log_level="critical", timeout_graceful_shutdown=None))
        service = asyncio.create_task(server.serve())
        try:
            await asyncio.wait_for(started.wait(), WAIT)
            isolated_environment.assert_endpoint()
            headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
            async with httpx.AsyncClient(headers=headers, timeout=WAIT) as first, httpx.AsyncClient(headers=headers, timeout=WAIT) as second:
                initialize = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "n06-fake", "version": "1"}}}
                for client in (first, second):
                    response = await client.post(value.url, json=initialize)
                    assert response.status_code == 200 and response.json()["id"] == 0
                def message(payload):
                    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "write", "arguments": {"value": payload}}}
                a, b = await asyncio.wait_for(asyncio.gather(first.post(value.url, json=message("A")), second.post(value.url, json=message("B"))), WAIT)
                assert a.json()["id"] == b.json()["id"] == 1
                assert a.json()["result"]["structuredContent"] == {"reply_for": "A"}
                assert b.json()["result"]["structuredContent"] == {"reply_for": "B"}
            assert incoming == ["1", "1"] and sorted(effects) == ["A", "B"]
        finally:
            server.should_exit = True  # test server cleanup, not production restart
            both.set()
            await asyncio.wait_for(service, WAIT)
    asyncio.run(run())


@pytest.mark.timing
def test_real_proxy_timeout_keeps_admitted_body_and_next_call_is_independent(monkeypatch, isolated_environment):
    async def run():
        value = state(isolated_environment)
        ctl = lifecycle.DaemonLifecycle(identity=identity())
        ctl.start()
        lifecycle.set_active_lifecycle(ctl)
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        effects = []
        upstream = FastMCP("timeout", middleware=[lifecycle.DaemonLifecycleMiddleware()])
        @upstream.tool
        async def write(value: str) -> dict:
            effects.append(value)
            if value == "first":
                started.set()
                await release.wait()
                finished.set()
            return {"value": value}
        async def reachable(**kwargs):
            pass
        monkeypatch.setattr(value, "ensure_reachable", reachable)
        proxy = FastMCPProxy(client_factory=lambda: shim.HandshakingProxyClient(upstream, attach_state=value, timeout=0.1), identity="upstream", middleware=[shim.ShimOutcomeMiddleware(value)])
        async with Client(proxy, mode="legacy", timeout=0) as downstream:
            task = asyncio.create_task(downstream.call_tool_mcp("write", {"value": "first"}))
            await asyncio.wait_for(started.wait(), WAIT)
            try:
                result = await asyncio.wait_for(task, WAIT)
                assert result.is_error and result.structured_content["error"]["code"] == "upstream_outcome_unknown"
                assert result.structured_content["error"]["retryable"] is False
                assert effects == ["first"] and ctl.snapshot()["active_work"] == 1
            finally:
                release.set()
            await asyncio.wait_for(finished.wait(), WAIT)
            second = await downstream.call_tool_mcp("write", {"value": "second"})
            assert second.structured_content == {"value": "second"} and effects == ["first", "second"]
    asyncio.run(run())


@pytest.mark.timing
def test_cold_start_baseline_and_new_with_41864_synthetic_nodes(isolated_environment):
    """T10 measurement only, no workstation-millisecond acceptance threshold."""
    import io
    import subprocess
    import tarfile
    from pathlib import Path
    from n06_harness import SETUP_WAIT, cleanup

    scenario = isolated_environment
    repo = Path(__file__).resolve().parents[1]
    baseline = scenario.lane("baseline-source")
    if (repo / ".git").exists():
        exported = subprocess.run(
            ["git", "archive", "3b61db4707e4071495a53754c32b1c17835319dc", "src"],
            cwd=repo, capture_output=True, check=True, timeout=90,
        ).stdout
    else:
        # The protocol's ext4 clone intentionally has no .git. The dispatch
        # pre-authorizes this exact read-only export from the main checkout.
        exported_path = Path("C:/tmp/n06b-baseline-source-3b61db47.tar") if os.name == "nt" else Path("/mnt/c/tmp/n06b-baseline-source-3b61db47.tar")
        assert exported_path.exists(), "prepare the N06 baseline git archive before the ext4 measurement"
        exported = exported_path.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(exported)) as archive:
        # Pinned source-only export: no branch, checkout, worktree or git files.
        for member in archive.getmembers():
            target = baseline / member.name
            assert (member.name.startswith("src/") or member.name == "src" and member.isdir()) and ".." not in Path(member.name).parts
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
    snapshot = scenario.root / "synthetic.json"
    snapshot.write_text(json.dumps({"nodes": [{"id": f"{i:08x}-0000-4000-8000-000000000000", "nm": f"Synthetic {i}", "parent_id": None} for i in range(41864)]}), encoding="utf-8")
    timings = {}
    for name, source in (("baseline", baseline / "src"), ("new", repo / "src")):
        child = scenario.start(
            "cold-" + name, "daemon", _N06_SYNTHETIC_SNAPSHOT=str(snapshot),
            PYTHONPATH=os.pathsep.join((str(source), str(repo / "tests"))),
        )
        try:
            scenario.ready("cold-" + name)
            lane = scenario.lane("cold-" + name)
            timings[name] = json.loads((lane / "cold-start.json").read_text(encoding="utf-8"))["seconds"]
            assert int((lane / "node-count").read_text()) == 41864
            assert scenario.events("cold-" + name).count("client") == 1
            assert scenario.events("cold-" + name).count("cache-bootstrap") == 1
            assert timings[name] > 0
        finally:
            scenario.release("cold-" + name)
            child.wait(timeout=SETUP_WAIT)
            cleanup(child)
    print("N06_COLD_START_TIMINGS " + json.dumps({"nodes": 41864, **timings}, sort_keys=True))
