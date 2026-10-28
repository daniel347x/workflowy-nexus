from __future__ import annotations

import ast
import asyncio
import json
from datetime import datetime, timezone
from email.utils import format_datetime
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from workflowy_mcp import carto_job_governance
from workflowy_mcp.carto_job_governance import derive_account_scope_id
from workflowy_mcp.carto_write_governor import (
    BreakerTransitionResult,
    CartoWriteGovernor,
    WriteGovernorSettings,
    WriteSlotTicket,
)
from workflowy_mcp.client import api_client_core
from workflowy_mcp.client.api_client_core import (
    WorkFlowyClientCore,
    operation_cancel_scope,
)
from workflowy_mcp.client.api_client_etch import export_nodes_impl, fetch_nodes_export_uncached
from workflowy_mcp.models import (
    APIConfiguration,
    APIResponseError,
    NodeCreateRequest,
    NodeListRequest,
    NodeUpdateRequest,
    OperationCancelledError,
    RateLimitError,
)
from workflowy_mcp.models.config import (
    GovernanceConfiguration,
    WriteGovernorConfiguration,
)
from workflowy_mcp.models.errors import (
    AccountWriteBlockedError,
    WriteGovernorDeferredError,
    WriteGovernorUnavailableError,
    WriteSlotBusyTimeoutError,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCOPE = "b" * 32
_MUTATING_METHODS = {"POST", "DELETE", "PUT", "PATCH"}
_RUNTIME_ROOT_MODULES = (
    "beacon_mutation_contract.py",
    "beacon_obtain_code_snippet.py",
    "glimpse_deploy_trampoline.py",
    "markdown_roundtrip.py",
    "nexus_map_codebase.py",
)
_WRITE_METHODS = {
    "create_node",
    "update_node",
    "delete_node",
    "complete_node",
    "uncomplete_node",
    "move_node",
}
_READ_METHODS = {"get_node", "list_nodes"}


def async_test(function):
    """Run one async test in this suite's plugin-free pytest environment."""

    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


class FakeClock:
    def __init__(self, value: float = 1_000.0):
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


@pytest.fixture(autouse=True)
def scheduler_mutants(monkeypatch):
    from test_cut_2b_governor import _install_governor_mutants
    _install_governor_mutants(monkeypatch)


class RecordingGovernor:
    def __init__(self, *, recheck_error: Exception | None = None):
        self.settings = WriteGovernorSettings(
            interval_seconds=1.0,
            max_wait_before_refusal_seconds=30.0,
            retry_after_fallback_seconds=60.0,
        )
        self.paths = SimpleNamespace(account_scope_id=_SCOPE)
        self.acquire_calls: list[str] = []
        self.recheck_calls = 0
        self.recorded: list[tuple[str, str]] = []
        self.recheck_error = recheck_error

    async def acquire_write_slot(self, operation: str, **_kwargs: Any) -> WriteSlotTicket:
        self.acquire_calls.append(operation)
        return WriteSlotTicket(0.0, 0.0, operation)

    async def recheck_before_send(self, _ticket: WriteSlotTicket, **_kwargs: Any) -> None:
        self.recheck_calls += 1
        if self.recheck_error is not None:
            raise self.recheck_error

    async def record_probe_result(self, token: str, outcome: str, **_kwargs: Any):
        self.recorded.append((token, outcome))
        return BreakerTransitionResult(True, {"state": "closed"})

    async def defer_global_writes_until(self, delay: float, **_kwargs: Any) -> float:
        return 1_000.0 + delay

    async def trip_wfb(self, **_kwargs: Any) -> BreakerTransitionResult:
        return BreakerTransitionResult(
            True,
            {"state": "open", "blocked_until": 2_000.0, "cooldown_s": 900.0},
        )


def _make_client(transport: httpx.AsyncBaseTransport) -> WorkFlowyClientCore:
    client = WorkFlowyClientCore(APIConfiguration(api_key="phase-b-test-key"))
    client._client = httpx.AsyncClient(
        base_url="https://workflowy.com/api/v1",
        transport=transport,
    )
    return client


def _real_governed_client(
    tmp_path: Path,
    handler,
    *,
    clock: FakeClock | None = None,
    fallback: float = 60.0,
    initial_cooldown: float = 10.0,
    max_cooldown: float = 20.0,
    probe_lease: float = 5.0,
    max_wait: float = 30_000.0,
) -> tuple[WorkFlowyClientCore, CartoWriteGovernor, FakeClock]:
    clock = clock or FakeClock()
    client = _make_client(httpx.MockTransport(handler))
    governor = CartoWriteGovernor(
        account_scope_id=_SCOPE,
        jobs_root=tmp_path,
        clock=clock,
        settings=WriteGovernorSettings(
            interval_seconds=1.0,
            max_wait_before_refusal_seconds=max_wait,
            breaker_initial_cooldown_seconds=initial_cooldown,
            breaker_max_cooldown_seconds=max_cooldown,
            probe_lease_seconds=probe_lease,
            retry_after_fallback_seconds=fallback,
            mutex_wait_seconds=2.0,
            mutex_poll_seconds=0.001,
        ),
    )
    client._write_governor = governor

    async def advance_sleep(seconds: float, _operation: str) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    client._sleep_with_operation_cancel = advance_sleep  # type: ignore[method-assign]
    return client, governor, clock


def _response(request: httpx.Request, status: int, payload: Any = None, **kwargs: Any):
    if payload is None:
        return httpx.Response(status, request=request, **kwargs)
    return httpx.Response(status, json=payload, request=request, **kwargs)


@async_test
async def test_all_six_crud_writes_consume_slots_reads_do_not_and_trailing_gets_are_plain():
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("/nodes"):
            return _response(request, 200, [])
        if request.method == "GET":
            node_id = request.url.path.rsplit("/", 1)[-1]
            return _response(request, 200, {"node": {"id": node_id, "name": "node"}})
        if request.method == "POST" and request.url.path.endswith("/nodes/"):
            return _response(request, 200, {"item_id": "created"})
        return _response(request, 200, {"status": "ok"})

    client = _make_client(httpx.MockTransport(handler))
    governor = RecordingGovernor()
    client._write_governor = governor
    sleeps: list[tuple[float, str]] = []

    async def record_sleep(seconds: float, operation: str) -> None:
        sleeps.append((seconds, operation))

    client._sleep_with_operation_cancel = record_sleep  # type: ignore[method-assign]
    try:
        await client.create_node(
            NodeCreateRequest(name="created", parent_id="parent", position="bottom"),
            _internal_call=True,
        )
        await client.update_node("updated", NodeUpdateRequest(name="updated"))
        assert await client.delete_node("deleted")
        await client.complete_node("completed")
        await client.uncomplete_node("uncompleted")
        assert await client.move_node("moved", "parent", "bottom")

        await client.get_node("read-one")
        await client.list_nodes(NodeListRequest())
    finally:
        await client.close()

    assert governor.acquire_calls == [
        "create_node",
        "update_node",
        "delete_node",
        "complete_node",
        "uncomplete_node",
        "move_node",
    ]
    assert governor.recheck_calls == 6
    assert sum(method in _MUTATING_METHODS for method, _path in requests) == 6
    assert sum(method == "GET" for method, _path in requests) == 6
    assert sleeps == [
        (api_client_core.API_RATE_LIMIT_DELAY, "Workflowy API operation"),
        (api_client_core.API_RATE_LIMIT_DELAY, "Workflowy API operation"),
    ]


@async_test
async def test_create_still_waiting_diagnostic_survives_authority(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return _response(request, 200, {"item_id": "created"})

    client = _make_client(httpx.MockTransport(handler))
    client._write_governor = RecordingGovernor()
    warnings: list[str] = []
    monkeypatch.setattr(
        api_client_core._ClientLogger,
        "warning",
        lambda _self, msg, *a, **k: warnings.append(str(msg)),
    )
    real_wait = asyncio.wait
    calls = 0

    async def fake_wait(fs, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0)
            return set(), set(fs)
        return await real_wait(fs, timeout=timeout, return_when=return_when)

    monkeypatch.setattr(asyncio, "wait", fake_wait)
    try:
        await client.create_node(
            NodeCreateRequest(name="created", parent_id="parent", position="bottom"),
            _internal_call=True,
            fetch_created_node=False,
        )
    finally:
        await client.close()

    assert any("create_node[POST]: still waiting" in warning for warning in warnings)
    assert any("name='created'" in warning and "parent='parent'" in warning for warning in warnings)


@async_test
async def test_wfb_trigger_is_one_call_then_subsequent_calls_are_local_zero_traffic(tmp_path):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, text='{"code":"WFB(3)"}', request=request)

    client, governor, _clock = _real_governed_client(tmp_path, handler)
    try:
        with pytest.raises(AccountWriteBlockedError) as first:
            await client._send_workflowy_write("POST", "/nodes/x", operation="first")
        assert "write-block detected" in first.value.reason
        assert calls == 1

        with pytest.raises(AccountWriteBlockedError) as second:
            await client._send_workflowy_write("POST", "/nodes/y", operation="second")
        assert second.value.retry_after >= 0
        assert calls == 1
        assert (await governor.snapshot())["breaker"]["state"] == "open"
    finally:
        await client.close()


@async_test
async def test_matching_probe_2xx_closes_breaker(tmp_path):
    statuses = [403, 200]

    async def handler(request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status == 403:
            return httpx.Response(status, text="WFB(3)", request=request)
        return _response(request, status, {"status": "ok"})

    client, governor, clock = _real_governed_client(tmp_path, handler)
    try:
        with pytest.raises(AccountWriteBlockedError):
            await client._send_workflowy_write("POST", "/nodes/x", operation="trip")
        clock.advance(10.0)
        response = await client._send_workflowy_write("POST", "/nodes/x", operation="probe")
        assert response.status_code == 200
        assert (await governor.snapshot())["breaker"]["state"] == "closed"
    finally:
        await client.close()


@async_test
async def test_matching_probe_429_closes_and_defers_in_one_transition(tmp_path):
    statuses = [403, 429]

    async def handler(request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status == 403:
            return httpx.Response(status, text="WFB(3)", request=request)
        return httpx.Response(status, headers={"Retry-After": "30"}, request=request)

    client, governor, clock = _real_governed_client(tmp_path, handler)
    try:
        with pytest.raises(AccountWriteBlockedError):
            await client._send_workflowy_write("POST", "/nodes/x", operation="trip")
        clock.advance(10.0)
        response = await client._send_workflowy_write("POST", "/nodes/x", operation="probe")
        assert response.status_code == 429
        state = await governor.snapshot()
        assert state["breaker"]["state"] == "closed"
        assert state["governor"]["next_slot_at"] == clock.value + 30.0
        with pytest.raises(RateLimitError):
            await client._handle_response(response)
    finally:
        await client.close()


@async_test
async def test_matching_probe_non_wfb_4xx_closes_then_ordinary_mapping_fails(tmp_path):
    statuses = [403, 400]

    async def handler(request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        text = "WFB(3)" if status == 403 else "ordinary bad request"
        return httpx.Response(status, text=text, request=request)

    client, governor, clock = _real_governed_client(tmp_path, handler)
    try:
        with pytest.raises(AccountWriteBlockedError):
            await client._send_workflowy_write("POST", "/nodes/x", operation="trip")
        clock.advance(10.0)
        response = await client._send_workflowy_write("POST", "/nodes/x", operation="probe")
        assert (await governor.snapshot())["breaker"]["state"] == "closed"
        with pytest.raises(APIResponseError):
            await client._handle_response(response)
    finally:
        await client.close()


@async_test
@pytest.mark.parametrize("failure", ["server", "timeout", "network"])
async def test_matching_probe_no_health_evidence_reopens_and_transport_is_original(
    tmp_path, failure
):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(403, text="WFB(3)", request=request)
        if failure == "server":
            return httpx.Response(503, text="unavailable", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("probe timed out", request=request)
        raise httpx.ConnectError("probe network failed", request=request)

    client, governor, clock = _real_governed_client(tmp_path, handler)
    try:
        with pytest.raises(AccountWriteBlockedError):
            await client._send_workflowy_write("POST", "/nodes/x", operation="trip")
        clock.advance(10.0)
        if failure == "server":
            response = await client._send_workflowy_write("POST", "/nodes/x", operation="probe")
            assert response.status_code == 503
        elif failure == "timeout":
            with pytest.raises(httpx.ReadTimeout):
                await client._send_workflowy_write("POST", "/nodes/x", operation="probe")
        else:
            with pytest.raises(httpx.ConnectError):
                await client._send_workflowy_write("POST", "/nodes/x", operation="probe")
        breaker = (await governor.snapshot())["breaker"]
        assert breaker["state"] == "open"
        assert breaker["cooldown_s"] == 10.0
    finally:
        await client.close()


@async_test
async def test_matching_probe_wfb_doubles_and_uses_recovery_probe_teaching(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="code WFB(3)", request=request)

    client, governor, clock = _real_governed_client(tmp_path, handler)
    try:
        with pytest.raises(AccountWriteBlockedError):
            await client._send_workflowy_write("POST", "/nodes/x", operation="trip")
        clock.advance(10.0)
        with pytest.raises(AccountWriteBlockedError) as probe_error:
            await client._send_workflowy_write("POST", "/nodes/x", operation="probe")
        assert "your write was the recovery probe" in probe_error.value.reason
        assert probe_error.value.retry_after >= 0
        assert (await governor.snapshot())["breaker"]["cooldown_s"] == 20.0
    finally:
        await client.close()


@async_test
@pytest.mark.parametrize(
    ("raw_header", "expected_delay", "used_fallback"),
    [
        ("12", 12.0, False),
        (None, 17.0, True),
        ("", 17.0, True),
        ("later", 17.0, True),
        ("-4", 17.0, True),
        ("nan", 17.0, True),
    ],
)
async def test_ordinary_429_uses_shared_parser_and_configured_account_defer(
    tmp_path, raw_header, expected_delay, used_fallback
):
    async def handler(request: httpx.Request) -> httpx.Response:
        headers = {} if raw_header is None else {"Retry-After": raw_header}
        return httpx.Response(429, headers=headers, request=request)

    client, governor, clock = _real_governed_client(tmp_path, handler, fallback=17.0)
    try:
        response = await client._send_workflowy_write("POST", "/nodes/x", operation="429")
        state = await governor.snapshot()
        assert state["governor"]["next_slot_at"] == clock.value + expected_delay
        with pytest.raises(RateLimitError) as caught:
            await client._handle_response(response)
        assert caught.value.retry_after == expected_delay
        assert bool(caught.value.retry_after == 17.0) is used_fallback
    finally:
        await client.close()


@async_test
async def test_http_date_and_zero_retry_after_semantics(tmp_path):
    clock = FakeClock(1_000.0)
    headers = [
        format_datetime(datetime.fromtimestamp(1_030.0, tz=timezone.utc), usegmt=True),
        "0",
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": headers.pop(0)}, request=request)

    client, governor, _clock = _real_governed_client(tmp_path, handler, clock=clock)
    try:
        await client._send_workflowy_write("POST", "/nodes/x", operation="date")
        assert (await governor.snapshot())["governor"]["next_slot_at"] == 1_030.0
        clock.advance(30.0)
        response = await client._send_workflowy_write("POST", "/nodes/x", operation="zero")
        # Zero is valid: the defer itself is a no-op, leaving only the issued slot queue.
        assert (await governor.snapshot())["governor"]["next_slot_at"] == 1_031.0
        with pytest.raises(RateLimitError) as caught:
            await client._handle_response(response)
        assert caught.value.retry_after == 0
        assert client._bounded_rate_limit_retry_delay(caught.value, 4.0, "zero") == 4.0
    finally:
        await client.close()


@async_test
async def test_large_retry_after_persists_fully_but_crud_refuses_local_sleep(tmp_path):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "21600"}, request=request)

    client, governor, clock = _real_governed_client(tmp_path, handler, max_wait=30.0)
    try:
        with pytest.raises(RateLimitError) as caught:
            await client.create_node(
                NodeCreateRequest(name="x", parent_id="p", position="bottom"),
                _internal_call=True,
                max_retries=10,
                fetch_created_node=False,
            )
        assert caught.value.retry_after == 21_600
        assert calls == 1
        assert (await governor.snapshot())["governor"]["next_slot_at"] == clock.value + 21_600
    finally:
        await client.close()


@async_test
async def test_429_max_merge_never_regresses_later_queue(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "10"}, request=request)

    client, governor, clock = _real_governed_client(tmp_path, handler)
    await governor.defer_global_writes_until(100.0)
    try:
        await client._send_workflowy_write("POST", "/nodes/x", operation="max-merge")
        assert (await governor.snapshot())["governor"]["next_slot_at"] == clock.value + 10.0
    finally:
        await client.close()

    # Keep the original fake-clock advance above. Separately make QUEUE and
    # MAX consequential while the real authority has an HTTP request in flight.
    async def queued_handler(request):
        for _ in range(20):
            await queued_governor.acquire_write_slot("in-flight backlog", sleep_fn=lambda *_: None)
        return httpx.Response(429, headers={"Retry-After": "10"}, request=request)
    queued_client, queued_governor, queued_clock = _real_governed_client(tmp_path / "queued", queued_handler)
    try:
        await queued_client._send_workflowy_write("POST", "/nodes/x", operation="queued-429")
        assert (await queued_governor.snapshot())["governor"]["next_slot_at"] == queued_clock.value + 21, "429 shortened the later memory queue"
        assert json.loads(queued_governor.paths.governor_state.read_text()) == {"next_slot_at": queued_clock.value + 10}
    finally:
        await queued_client.close()

    async def floor_handler(request):
        await floor_governor.defer_global_writes_until(100)
        return httpx.Response(429, headers={"Retry-After": "10"}, request=request)
    floor_client, floor_governor, floor_clock = _real_governed_client(tmp_path / "floor", floor_handler)
    try:
        await floor_client._send_workflowy_write("POST", "/nodes/x", operation="standing-floor-429")
        assert (await floor_governor.snapshot())["governor"]["next_slot_at"] == floor_clock.value + 100
        assert json.loads(floor_governor.paths.governor_state.read_text()) == {"next_slot_at": floor_clock.value + 100}, "429 shortened the durable floor"
    finally:
        await floor_client.close()


@async_test
@pytest.mark.parametrize(
    "error",
    [
        AccountWriteBlockedError(retry_after=1, blocked_until=2, reason="blocked"),
        WriteGovernorUnavailableError("x", "broken"),
        WriteGovernorDeferredError(retry_after=31, deferred_until=32, reason="far"),
        WriteSlotBusyTimeoutError("x", 10),
    ],
)
async def test_governance_errors_propagate_from_crud_without_retry(error):
    client = WorkFlowyClientCore(APIConfiguration(api_key="phase-b-test-key"))
    calls = 0

    async def fail(*_args: Any, **_kwargs: Any):
        nonlocal calls
        calls += 1
        raise error

    client._send_workflowy_write = fail  # type: ignore[method-assign]
    with pytest.raises(type(error)) as caught:
        await client.delete_node("x", max_retries=10)
    assert caught.value is error
    assert calls == 1


@async_test
async def test_blocked_recheck_performs_zero_http_calls():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(request, 200, {"status": "ok"})

    blocked = AccountWriteBlockedError(retry_after=0, blocked_until=0, reason="retry fresh")
    client = _make_client(httpx.MockTransport(handler))
    client._write_governor = RecordingGovernor(recheck_error=blocked)
    try:
        with pytest.raises(AccountWriteBlockedError):
            await client._send_workflowy_write("POST", "/nodes/x", operation="blocked")
        assert calls == 0
    finally:
        await client.close()


@async_test
async def test_task_local_cancellation_interrupts_governor_before_http_without_cross_job_leak(
    tmp_path,
):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(request, 200, {"status": "ok"})

    client, _governor, _clock = _real_governed_client(tmp_path, handler)

    async def cancelled_job():
        with operation_cancel_scope(lambda: True):
            await client._send_workflowy_write("POST", "/nodes/x", operation="cancelled")

    async def unrelated_job():
        await client._sleep_with_operation_cancel(0, "unrelated")
        return "ok"

    try:
        cancelled, unrelated = await asyncio.gather(
            cancelled_job(), unrelated_job(), return_exceptions=True
        )
        assert isinstance(cancelled, OperationCancelledError)
        assert unrelated == "ok"
        assert calls == 0
    finally:
        await client.close()


@async_test
async def test_nodes_export_uses_cancel_aware_local_sleep_and_keeps_retry_after_log_only():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(request, 200, {"nodes": []})

    client = _make_client(httpx.MockTransport(handler))
    client.set_operation_cancel_callback(lambda: True)
    try:
        with pytest.raises(OperationCancelledError):
            # S02: the cancel-aware pacing/retry body lives in the single
            # fetch seam; export_nodes_impl's refresh branch delegates to the
            # coherence refresh authority (which owns pacing via this seam).
            await fetch_nodes_export_uncached(client)
        assert calls == 0
    finally:
        await client.close()

    source = (REPO_ROOT / "src/workflowy_mcp/client/api_client_etch.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for fn_name in ("export_nodes_impl", "fetch_nodes_export_uncached"):
        export_fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == fn_name
        )
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
            and node.func.attr == "sleep"
            for node in ast.walk(export_fn)
        )
    assert source.count("retry_after_hint") >= 3


def test_client_constructs_governor_from_exact_models_and_authoritative_jobs_root(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(carto_job_governance, "get_cartographer_jobs_dir", lambda: tmp_path)
    write_config = WriteGovernorConfiguration(
        interval_seconds=2.5,
        max_wait_before_refusal_seconds=7.0,
        breaker_initial_cooldown_seconds=11.0,
        breaker_max_cooldown_seconds=22.0,
        probe_lease_seconds=4.0,
        retry_after_fallback_seconds=9.0,
    )
    mutex_config = GovernanceConfiguration(mutex_wait_seconds=3.0)
    client = WorkFlowyClientCore(
        APIConfiguration(
            api_key="do-not-persist-this-secret",
            base_url="https://workflowy.com/api/v1",
        ),
        write_governor_config=write_config,
        governance_config=mutex_config,
    )
    governor = client._write_governor
    assert governor.paths.jobs_root == tmp_path
    assert governor.paths.account_scope_id == derive_account_scope_id(
        "https://workflowy.com/api/v1", "do-not-persist-this-secret"
    )
    assert governor.settings.interval_seconds == 2.5
    assert governor.settings.max_wait_before_refusal_seconds == 7.0
    assert governor.settings.retry_after_fallback_seconds == 9.0
    assert governor.settings.mutex_wait_seconds == 3.0
    asyncio.run(governor.snapshot())
    assert "do-not-persist-this-secret" not in "".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


def _runtime_python_files() -> list[Path]:
    files = list((REPO_ROOT / "src").rglob("*.py"))
    files.extend(REPO_ROOT / name for name in _RUNTIME_ROOT_MODULES)
    return sorted(path for path in files if path.is_file() and ".venv" not in path.parts)


def _literal_method(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.upper()
    return None


def _request_builder_method(node: ast.AST, assignments: dict[str, ast.AST]) -> str | None:
    if isinstance(node, ast.Name):
        assigned = assignments.get(node.id)
        return _request_builder_method(assigned, assignments) if assigned is not None else None
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in {"Request", "build_request"}:
        method_node = (
            node.args[0]
            if node.args
            else next((kw.value for kw in node.keywords if kw.arg == "method"), None)
        )
        return _literal_method(method_node) or "UNKNOWN"
    return None


def _mutating_http_calls(source: str, filename: str = "<source>") -> list[tuple[str, int, str]]:
    """One account-write door plus V8EI9P's exact local restart registration.

    This is a source registration, not receiver-type inference: an HTTP-looking
    alias/escape is not made safe by a variable name. The one local exception
    pins its owner, configured origin, path, auth, redirect policy and closed
    GET/POST call sites together. It never grants another account-write lane.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[tuple[str, int, str]] = []
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def owner(node: ast.AST) -> tuple[str, ...]:
        names = []
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(node.name)
        return tuple(reversed(names))

    def shape(node: ast.AST) -> str:
        return ast.dump(node, include_attributes=False)

    def expression(text: str) -> str:
        return shape(ast.parse(text, mode="eval").body)

    # JR2 G3's two accumulator updates are data-only, not HTTP PUTs. Register
    # exact module + qualified owner + entire call AST, never an attribute or
    # receiver-name exemption. A real transport call planted in either owner
    # still fails, as do a changed receiver, URL argument, alias or getattr.
    data_puts = {
        ("/src/workflowy_mcp/beacon_refresh_support.py", ("CartoOutcomeAccumulator", "set_phase8")):
            expression('self.put(row, section="phase8")'),
        ("/src/workflowy_mcp/carto_job_record.py", ("CartoJobRecord", "append_file_row")):
            expression('self.accumulator.put(row, section=section, delta=delta, nodes_reflected=receipt_count)'),
    }
    data_calls = {node for node in ast.walk(tree) if isinstance(node, ast.Call)
                  and any(filename.replace("\\", "/").endswith(path) and owner(node) == scope
                          and shape(node) == expected for (path, scope), expected in data_puts.items())}

    getter_names, builtin_modules = {"getattr"}, {"builtins"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            builtin_modules.update(alias.asname or alias.name for alias in node.names if alias.name == "builtins")
        elif isinstance(node, ast.ImportFrom) and node.module == "builtins":
            getter_names.update(alias.asname or alias.name for alias in node.names if alias.name == "getattr")

    def is_getter(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id in getter_names) or (
            isinstance(node, ast.Attribute) and node.attr == "getattr"
            and isinstance(node.value, ast.Name) and node.value.id in builtin_modules
        )

    # Close simple imported/module/callable aliases before counting calls.
    changed = True
    while changed:
        before = len(getter_names), len(builtin_modules)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if is_getter(node.value):
                    getter_names.add(node.targets[0].id)
                elif isinstance(node.value, ast.Name) and node.value.id in builtin_modules:
                    builtin_modules.add(node.targets[0].id)
        changed = before != (len(getter_names), len(builtin_modules))

    called_names = {(owner(node), node.func.id) for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    changed = True
    while changed:
        before = len(called_names)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Name) and (owner(node), node.targets[0].id) in called_names):
                called_names.add((owner(node), node.value.id))
        changed = before != len(called_names)

    shim_file = filename.replace("\\", "/").endswith("/src/workflowy_mcp/shim.py")
    local_call = None
    if shim_file:
        controls = [node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef)
                    and (*owner(node), node.name) == ("AttachState", "control")]
        if len(controls) == 1:
            control = controls[0]
            direct_calls = [node for node in ast.walk(control)
                            if isinstance(node, ast.Call) and owner(node) == ("AttachState", "control")
                            and isinstance(node.func, ast.Attribute) and node.func.attr == "request"]
            # These assignments and the context manager are the complete
            # credential/origin path, not a peer-reported restart URL.
            required = {
                "parsed": expression("urlsplit(self.url)"),
                "url": expression('urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/_nexus/restart", "", ""))'),
            }
            assignments = {name: [] for name in required}
            for node in ast.walk(control):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in required:
                    assignments[node.targets[0].id].append(shape(node.value))
            stores = {name: sum(isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == name
                                for node in ast.walk(control)) for name in ("parsed", "url", "method", "client")}
            clients = [node for node in ast.walk(control) if isinstance(node, ast.AsyncWith)
                       and len(node.items) == 1 and isinstance(node.items[0].optional_vars, ast.Name)
                       and node.items[0].optional_vars.id == "client"]
            expected_client = expression('httpx.AsyncClient(timeout=remaining, follow_redirects=False, headers={"Authorization": f"Bearer {self.token}"})')
            control_refs = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "control"]
            closed_methods = bool(control_refs) and all(
                owner(node) == ("AttachState", "restart_or_join")
                and isinstance(node.value, ast.Name) and node.value.id == "self"
                and isinstance(parents.get(node), ast.Call) and parents[node].func is node
                and bool(parents[node].args) and _literal_method(parents[node].args[0]) in {"GET", "POST"}
                for node in control_refs
            )
            constructors = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name) and node.func.id == "AttachState"]
            main_urls = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                         and owner(node) == ("main",) and len(node.targets) == 1
                         and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "url"]
            own_config = (
                len(constructors) == 1 and owner(constructors[0]) == ("main",)
                and len(constructors[0].args) == 2
                and shape(constructors[0].args[0]) == expression("url")
                and shape(constructors[0].args[1]) == expression("daemon.shared_token()")
                and len(main_urls) == 1 and shape(main_urls[0].value) == expression("daemon.daemon_url()")
                and not any(isinstance(node, ast.Attribute) and node.attr == "url" and not isinstance(node.ctx, ast.Load)
                            for node in ast.walk(tree))
                and not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr"
                            and len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and node.args[1].value == "url"
                            for node in ast.walk(tree))
            )
            if (len(direct_calls) == 1 and closed_methods and own_config
                    and all(assignments[name] == [expected] for name, expected in required.items())
                    and stores == {"parsed": 1, "url": 1, "method": 0, "client": 1}
                    and len(clients) == 1 and shape(clients[0].items[0].context_expr) == expected_client):
                candidate = direct_calls[0]
                if (shape(candidate) == expression('client.request(method, url, json=body if method == "POST" else None, params=params)')
                        and candidate in set(ast.walk(clients[0]))):
                    local_call = candidate
        if local_call is None:
            violations.append((filename, 0, "invalid_local_restart_registration"))

    def authority(node: ast.AST) -> bool:
        # Preserve the existing synthetic one-authority fixture; runtime code
        # cannot acquire authority merely by copying that function's name.
        return owner(node)[-1:] == ("_send_workflowy_write",) and (
            filename == "<source>" or filename.replace("\\", "/").endswith("/src/workflowy_mcp/client/api_client_core.py")
        )

    for node in ast.walk(tree):
        if authority(node):
            continue
        if isinstance(node, ast.Attribute) and node.attr in {"post", "delete", "put", "patch", "request", "control"}:
            parent = parents.get(node)
            if (node.attr == "request" and isinstance(parent, ast.Attribute)
                    and parent.value is node and parent.attr == "url"):
                continue  # Response.request.url is diagnostic data, not a callable escape.
            if not isinstance(parent, ast.Call) or parent.func is not node:
                violations.append((filename, node.lineno, "escaped_" + node.attr))
            elif node.attr == "control" and not (shim_file and local_call is not None and owner(node) == ("AttachState", "restart_or_join")):
                violations.append((filename, node.lineno, "unregistered_control"))
        elif isinstance(node, ast.Call) and is_getter(node.func) and len(node.args) > 1:
            attribute = node.args[1]
            parent = parents.get(node)
            invoked = isinstance(parent, ast.Call) and parent.func is node
            if isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
                invoked |= (owner(parent), parent.targets[0].id) in called_names
            if (isinstance(attribute, ast.Constant) and attribute.value in {"post", "delete", "put", "patch", "request", "control"}) or (
                invoked and not (isinstance(attribute, ast.Constant) and isinstance(attribute.value, str))
            ):
                violations.append((filename, node.lineno, "getattr_transport"))
    function_nodes = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for function in function_nodes:
        assignments: dict[str, ast.AST] = {}
        for node in ast.walk(function):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not function:
                continue
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                assignments[node.targets[0].id] = node.value
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.value is not None
            ):
                assignments[node.target.id] = node.value
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if owner(node)[-1:] != (function.name,):
                continue
            if node in data_calls:
                continue
            attr = node.func.attr
            receiver_name = ""
            if isinstance(node.func.value, ast.Name):
                receiver_name = node.func.value.id
            elif isinstance(node.func.value, ast.Attribute):
                receiver_name = node.func.value.attr
            if attr == "put" and receiver_name.endswith("queue"):
                # Registration-style syntactic allow-list for asyncio queues;
                # no receiver-type inference and no HTTP transport exemption.
                continue
            mutating = False
            if attr in {"post", "delete", "put", "patch"}:
                mutating = True
            elif attr == "request":
                method_node = (
                    node.args[0]
                    if node.args
                    else next((kw.value for kw in node.keywords if kw.arg == "method"), None)
                )
                method = _literal_method(method_node)
                mutating = method is None or method in _MUTATING_METHODS
            elif attr == "send" and node.args:
                method = _request_builder_method(node.args[0], assignments)
                mutating = method in _MUTATING_METHODS or method == "UNKNOWN"
            if mutating and not authority(node) and node is not local_call:
                violations.append((filename, node.lineno, attr))
    return violations


def test_registration_style_write_authority_fence_and_six_site_coverage():
    violations: list[tuple[str, int, str]] = []
    for path in _runtime_python_files():
        violations.extend(_mutating_http_calls(path.read_text(encoding="utf-8"), str(path)))
    assert violations == []

    core_path = REPO_ROOT / "src/workflowy_mcp/client/api_client_core.py"
    tree = ast.parse(core_path.read_text(encoding="utf-8"))
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name in _WRITE_METHODS | _READ_METHODS
    }
    assert set(methods) == _WRITE_METHODS | _READ_METHODS
    for name in _WRITE_METHODS:
        method = methods[name]
        authority_calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_send_workflowy_write"
        ]
        pacing_calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_sleep_with_operation_cancel"
            and any(
                isinstance(arg, ast.Name) and arg.id == "API_RATE_LIMIT_DELAY" for arg in node.args
            )
        ]
        assert len(authority_calls) == 1, name
        assert pacing_calls == [], name
    for name in _READ_METHODS:
        method = methods[name]
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_send_workflowy_write"
            for node in ast.walk(method)
        )
        assert (
            sum(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_sleep_with_operation_cancel"
                and any(
                    isinstance(arg, ast.Name) and arg.id == "API_RATE_LIMIT_DELAY"
                    for arg in node.args
                )
                for node in ast.walk(method)
            )
            == 1
        )


@pytest.mark.parametrize(
    "statement",
    [
        "client.post('/nodes/')",
        "client.delete('/nodes/x')",
        "client.put('/nodes/x')",
        "client.patch('/nodes/x')",
        "httpx.post('/nodes/')",
        "client.request('POST', '/nodes/')",
        "client.request(method, '/nodes/')",
        "client.request(method='PUT', url='/nodes/x')",
        "client.send(httpx.Request('DELETE', '/nodes/x'))",
        "client.send(client.build_request('POST', '/nodes/x'))",
        "request = client.build_request('PATCH', '/nodes/x')\nclient.send(request)",
        "request: object = httpx.Request(method, '/nodes/x')\nclient.send(request)",
    ],
)
def test_static_fence_rejects_every_mutating_transport_bypass_shape(statement):
    source = "async def bypass(client, httpx, method='POST'):\n    " + statement.replace(
        "\n", "\n    "
    )
    assert _mutating_http_calls(source)


def test_static_fence_allows_reads_websocket_send_and_the_one_authority():
    source = """
async def read_only(client, websocket, payload):
    await client.request('GET', '/nodes')
    await client.send(httpx.Request('GET', '/nodes'))
    await websocket.send(payload)

async def _send_workflowy_write(client, method):
    return await client.request(method, '/nodes')
"""
    assert _mutating_http_calls(source) == []


@pytest.mark.parametrize("relative, call", [
    ("beacon_refresh_support.py", 'self.put(row, section="phase8")'),
    ("carto_job_record.py", 'self.accumulator.put(row, section=section, delta=delta, nodes_reflected=receipt_count)'),
])
def test_accumulator_data_put_registration_is_exact_and_catches_real_http(relative, call):
    """Full-tier 157041c red: classify only the two exact non-HTTP calls."""
    path = REPO_ROOT / "src/workflowy_mcp" / relative
    source = path.read_text(encoding="utf-8")
    assert source.count(call) == 1
    assert _mutating_http_calls(source, str(path)) == []
    for replacement in ('self.client.put("/nodes/x")', 'self.put("/nodes/x")',
                        'self.accumulator.put("/nodes/x")'):
        assert _mutating_http_calls(source.replace(call, replacement), str(path)), replacement
    assert _mutating_http_calls(source, str(path).replace(relative, "other.py"))


def test_static_fence_local_restart_exception_is_exact_and_sensitive():
    """V8EI9P registers a local control request, never a second write authority."""
    path = REPO_ROOT / "src/workflowy_mcp/shim.py"
    source = path.read_text(encoding="utf-8")
    assert _mutating_http_calls(source, str(path)) == []
    call = 'response = await client.request(method, url, json=body if method == "POST" else None, params=params)'
    faults = {
        "other_function": source + "\nasync def other(client):\n    await client.post('/nodes')\n",
        "duplicate_control_call": source.replace(call, call + "\n                await client.request(method, url)"),
        "duplicate_control_definition": source + "\nclass AttachState:\n    async def control(self, client):\n        await client.post('/nodes')\n",
        "callable_alias": source.replace(call, 'send = client.request\n                response = await send(method, url)'),
        "getattr": source.replace(call, 'response = await getattr(client, "request")(method, url)'),
        "wrapper": source.replace(call, 'response = await wrapper(client, method, url)') + "\nasync def wrapper(client, method, url):\n    return await client.request(method, url)\n",
        "other_path": source.replace('"/_nexus/restart"', '"/nodes"'),
        "other_origin": source.replace('urlsplit(self.url)', 'urlsplit("https://workflowy.com/api/v1")'),
        "peer_origin": source.replace('urlsplit(self.url)', 'urlsplit(body["url"])'),
        "redirects": source.replace('follow_redirects=False', 'follow_redirects=True'),
        "no_bearer": source.replace('{"Authorization": f"Bearer {self.token}"}', '{}'),
        "other_method": source.replace('self.control("POST"', 'self.control("DELETE"'),
        "unknown_method": source.replace('self.control("POST"', 'self.control(method'),
        "escaped_control": source + "\nasync def other(state):\n    send = state.control\n    return await send('POST')\n",
        "getattr_alias": source + "\nasync def other(client):\n    lookup = getattr\n    return await lookup(client, 'request')('POST', '/nodes')\n",
        "getattr_import_alias": source + "\nfrom builtins import getattr as lookup\nasync def other(client):\n    return await lookup(client, 'post')('/nodes')\n",
        "dynamic_control_lookup": source + "\nasync def other(state, name):\n    return await getattr(state, name)('POST')\n",
        "getattr_module_alias": source + "\nimport builtins as b\nasync def other(client):\n    lookup = b.getattr\n    return await lookup(client, 'request')('POST', '/nodes')\n",
        "getattr_alias_chain": source + "\nasync def other(client):\n    lookup = getattr\n    again = lookup\n    return await again(client, 'post')('/nodes')\n",
        "dynamic_bound_control_lookup": source + "\nasync def other(state, name):\n    call = getattr(state, name)\n    again = call\n    return await again('POST')\n",
    }
    for fault, planted in faults.items():
        assert planted != source, fault
        assert _mutating_http_calls(planted, str(path)), fault
    assert _mutating_http_calls(source, str(path).replace("shim.py", "other.py"))
