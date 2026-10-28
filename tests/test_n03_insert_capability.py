"""N03 local controller races and real config-loader precedence. No live I/O."""
import asyncio
import inspect
import json
import os
import textwrap

import pytest

from workflowy_mcp.client import insert_capability as cap
from workflowy_mcp.client.insert_capability import InsertCapabilityController, InsertCapabilityState as State


@pytest.fixture(autouse=True)
def controller_mutants(monkeypatch):
    mutant = os.getenv("N03_CONTROLLER_MUTANT")
    if not mutant:
        return
    method, old, new = {
        "probe_claim": ("select_for_insert", "self._state = InsertCapabilityState.HALF_OPEN_PROBE", "pass"),
        "stale_result": ("_owns_result", "selection._generation == self._generation", "True"),
    }[mutant]
    source = textwrap.dedent(inspect.getsource(getattr(InsertCapabilityController, method)))
    assert source.count(old) == 1
    source = source.replace(old, new)
    if mutant == "stale_result":
        source = source.replace("selection._probe_token is self._probe_token", "True")
    ns = dict(vars(cap))
    exec(compile(source, "<n03-controller-mutant>", "exec"), ns)
    monkeypatch.setattr(InsertCapabilityController, method, ns[method])


def controller():
    now = [100.0]
    return InsertCapabilityController("existing-scope", clock=lambda: now[0], v1_only=lambda: False), now


def test_unknown_probe_healthy_and_available():
    c, _ = controller()
    assert c.snapshot().state == State.UNKNOWN
    first = c.select_for_insert()
    assert first.transport == "llm" and first._probe_token is not None
    assert c.snapshot().state == State.HALF_OPEN_PROBE
    assert c.select_for_insert().transport == "v1"
    c.record_success(first)
    assert c.snapshot().state == State.AVAILABLE
    assert c.select_for_insert().transport == "llm"


@pytest.mark.parametrize("reason", ["unsupported_endpoint", "schema_drift", "llm_401", "llm_non_wfb_403", "redirect"])
def test_degradation_is_nonblocking_and_expires_for_real_work(reason):
    c, now = controller()
    selected = c.select_for_insert()
    c.record_capability_failure(selected, reason)
    assert c.snapshot() == cap.InsertCapabilitySnapshot(State.DEGRADED_UNTIL, reason, 160.0)
    assert c.select_for_insert().transport == "v1"
    now[0] = 159.99
    assert c.select_for_insert().transport == "v1"
    now[0] = 160.0
    probe = c.select_for_insert()
    assert probe.transport == "llm"
    assert c.select_for_insert().transport == "v1"
    c.record_success(probe)
    assert c.snapshot().state == State.AVAILABLE


@pytest.mark.parametrize("axis", ["read_required", "not_found", "invalid", "429", "deferral", "wfb", "cancel", "ambiguous", "timeout", "generic_5xx"])
def test_neutral_outcome_releases_probe_without_new_cooldown(axis):
    c, now = controller()
    first = c.select_for_insert()
    c.record_neutral(first)
    assert c.snapshot().state == State.UNKNOWN
    failing = c.select_for_insert()
    c.record_capability_failure(failing, "unsupported_endpoint")
    prior = c.snapshot()
    now[0] = prior.retry_deadline
    probe = c.select_for_insert()
    c.record_neutral(probe)
    assert c.snapshot() == prior
    assert c.select_for_insert().transport == "llm"
    assert axis  # Labels document caller-owned axis classification, not a parser.


def test_concurrent_selectors_claim_one_real_probe():
    async def run():
        c, now = controller()
        async def select():
            await asyncio.sleep(0)
            return c.select_for_insert()
        for expired in (False, True):
            if expired:
                now[0] = c.snapshot().retry_deadline
            selections = await asyncio.gather(*(select() for _ in range(10)))
            probes = [s for s in selections if s.transport == "llm"]
            assert len(probes) == 1
            assert sum(s.transport == "v1" for s in selections) == 9
            c.record_capability_failure(probes[0], "unsupported_endpoint")
    asyncio.run(run())


def test_old_success_cannot_clear_newer_degradation_or_steal_probe():
    c, now = controller()
    c.record_success(c.select_for_insert())
    old, failed = c.select_for_insert(), c.select_for_insert()
    c.record_capability_failure(failed, "schema_drift")
    degraded = c.snapshot()
    c.record_success(old)
    assert c.snapshot() == degraded
    now[0] = degraded.retry_deadline
    current = c.select_for_insert()
    c.record_success(old)
    assert c.snapshot().state == State.HALF_OPEN_PROBE
    assert c.select_for_insert().transport == "v1"
    c.record_neutral(current)
    assert c.snapshot() == degraded
    # A late neutral result cannot release the next owner's probe either.
    current = c.select_for_insert()
    c.record_neutral(old)
    assert c.snapshot().state == State.HALF_OPEN_PROBE
    c.record_success(current)
    assert c.snapshot().state == State.AVAILABLE


def test_forced_latches_only_on_selection_and_foreign_tokens_are_inert():
    setting, calls = [True], []
    c = InsertCapabilityController("scope", v1_only=lambda: calls.append("read") or setting[0])
    assert c.snapshot().state == State.UNKNOWN and not calls
    forced = c.select_for_insert()
    setting[0] = False
    other, _ = controller()
    foreign = other.select_for_insert()
    for selected in (forced, foreign):
        c.record_success(selected)
        c.record_neutral(selected)
        c.record_capability_failure(selected, "schema_drift")
    assert c.select_for_insert().transport == "v1"
    assert c.snapshot().state == State.FORCED_V1
    assert calls == ["read"]


def test_client_owns_one_inert_controller_and_restart_forgets(tmp_path, monkeypatch):
    from test_coherence_s03_closure_repairs import _make_client
    from workflowy_mcp import config
    reads = []
    monkeypatch.setattr(config, "get_server_config", lambda: reads.append("read") or type("Config", (), {"v1_only": False})())
    first, _, _ = _make_client(tmp_path, monkeypatch)
    assert first._insert_capability.account_scope_id == first._write_governor.paths.account_scope_id
    assert first._insert_capability.snapshot().state == State.UNKNOWN
    assert not reads and first._client is None
    choice = first._insert_capability.select_for_insert()
    first._insert_capability.record_capability_failure(choice, "unsupported_endpoint")
    second, _, _ = _make_client(tmp_path, monkeypatch)
    assert second._insert_capability is not first._insert_capability
    assert second._insert_capability.snapshot().state == State.UNKNOWN
    assert reads == ["read"] and second._client is None


@pytest.mark.parametrize("file_value,env_value,expected", [
    (None, None, False), (True, None, True), (False, None, False),
    (False, "true", True), (True, "false", False), (True, "0", False),
    (False, "YES", True), (True, "", True), (False, "  ", False), (None, "", False),
])
def test_real_loader_file_environment_default_and_blank(tmp_path, monkeypatch, file_value, env_value, expected):
    from workflowy_mcp import config
    path = tmp_path / "workflowy_nexus.config.local.json"
    path.write_text(json.dumps({"WORKFLOWY_API_KEY": "test-key", **({"v1_only": file_value} if file_value is not None else {})}))
    monkeypatch.setenv("WORKFLOWY_NEXUS_CONFIG", str(path))
    monkeypatch.delenv("V1_ONLY", raising=False)
    monkeypatch.delenv("WORKFLOWY_V1_ONLY", raising=False)
    if env_value is not None:
        monkeypatch.setenv("WORKFLOWY_V1_ONLY", env_value)
    monkeypatch.setattr(config, "_CONFIG_CACHE", None)
    loaded = config.load_server_config(reload=True)
    assert loaded.v1_only is expected
    assert type(loaded.v1_only) is bool
    assert config.get_server_config() is loaded


def test_sender_cancel_boundary_needs_explicit_stage_disposition(tmp_path):
    """STOP witness: the sole hook cannot expose whether HTTP began on cancel.

    No proposed production marker is implemented here. A real governor's final
    lock wait and a real MockTransport send produce the same caller-visible
    cancellation after the hook, despite zero versus one HTTP observations.
    """
    import httpx
    from test_carto_write_governor_phase_b import _real_governed_client

    async def run():
        observations = []
        for stage in ("recheck", "http"):
            ready, never = asyncio.Event(), asyncio.Event()
            seen, hook_calls = [], []
            async def server(request):
                seen.append(request)
                ready.set()
                await never.wait()
                return httpx.Response(200, json={})
            client, governor, _ = _real_governed_client(tmp_path / stage, server)
            async def preflight():
                hook_calls.append("complete")
                if stage == "recheck":
                    # Hold the real governor's mutex only in this test carrier,
                    # so recheck suspends before the sender creates HTTP work.
                    await governor._schedule.lock.acquire()
                    ready.set()
            async with client._client:
                task = asyncio.create_task(client._send_workflowy_write(
                    "POST", "/nodes/", operation="create", before_send=preflight,
                ))
                await ready.wait()
                await asyncio.sleep(0)
                task.cancel()
                try:
                    with pytest.raises(asyncio.CancelledError) as caught:
                        await task
                    observations.append((type(caught.value), caught.value.args, vars(caught.value), hook_calls, len(seen)))
                finally:
                    if stage == "recheck":
                        governor._schedule.lock.release()
        assert observations[0][:-1] == observations[1][:-1]
        assert [row[-1] for row in observations] == [0, 1]
    asyncio.run(run())
