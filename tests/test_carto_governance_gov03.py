from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from workflowy_mcp.carto_write_governor import CartoWriteGovernor

SCOPE = "a" * 32


@pytest.fixture(autouse=True)
def scheduler_mutants(monkeypatch):
    from test_cut_2b_governor import _install_governor_mutants
    _install_governor_mutants(monkeypatch)


class FakeClock:
    def __init__(self, value: float = 1_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


async def _no_sleep(_seconds: float, _operation: str) -> None:
    await asyncio.sleep(0)


def _json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _probe(governor: CartoWriteGovernor, clock: FakeClock):
    asyncio.run(governor.trip_wfb())
    clock.advance(900.0)
    return asyncio.run(governor.acquire_write_slot("probe", sleep_fn=_no_sleep))


def _record_429(governor: CartoWriteGovernor, token: str) -> None:
    asyncio.run(
        governor.record_probe_result(
            token,
            "rate_limited",
            retry_after_seconds=75.0,
        )
    )


def test_probe_429_defer_write_failure_leaves_breaker_blocking(monkeypatch, tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock)
    probe = _probe(governor, clock)
    before_governor = _json(governor.paths.governor_state)
    original = governor._write_state

    def fail_before_defer(path, payload):
        if Path(path) == governor.paths.governor_state:
            raise OSError("defer persistence failed")
        return original(path, payload)

    monkeypatch.setattr(governor, "_write_state", fail_before_defer)
    with pytest.raises(OSError, match="defer persistence failed"):
        _record_429(governor, probe.probe_claim_id)

    assert _json(governor.paths.governor_state) == before_governor
    assert _json(governor.paths.breaker_state)["state"] == "half_open"


def test_probe_429_failure_after_defer_write_keeps_defer_and_blocker(monkeypatch, tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock)
    probe = _probe(governor, clock)
    original = governor._write_state

    def write_defer_then_fail(path, payload):
        if Path(path) == governor.paths.governor_state:
            original(path, payload)
            raise OSError("crash after defer publication")
        return original(path, payload)

    monkeypatch.setattr(governor, "_write_state", write_defer_then_fail)
    with pytest.raises(OSError, match="crash after defer publication"):
        _record_429(governor, probe.probe_claim_id)

    assert _json(governor.paths.governor_state) == {"next_slot_at": clock.value + 75.0}, "required defer was not durable before failure"
    assert _json(governor.paths.breaker_state)["state"] == "half_open"


def test_probe_429_breaker_close_failure_keeps_published_defer(monkeypatch, tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock)
    probe = _probe(governor, clock)
    original = governor._write_state

    def fail_breaker_close(path, payload):
        if Path(path) == governor.paths.breaker_state and payload.get("state") == "closed":
            raise OSError("breaker close failed")
        return original(path, payload)

    monkeypatch.setattr(governor, "_write_state", fail_breaker_close)
    with pytest.raises(OSError, match="breaker close failed"):
        _record_429(governor, probe.probe_claim_id)

    assert _json(governor.paths.governor_state) == {"next_slot_at": clock.value + 75.0}, "required defer was not durable before breaker close"
    assert _json(governor.paths.breaker_state)["state"] == "half_open"


def test_probe_429_success_publishes_defer_before_closed_breaker(monkeypatch, tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock)
    probe = _probe(governor, clock)
    original = governor._write_state
    writes: list[str] = []

    def record_order(path, payload):
        writes.append(Path(path).name)
        return original(path, payload)

    monkeypatch.setattr(governor, "_write_state", record_order)
    result = asyncio.run(
        governor.record_probe_result(
            probe.probe_claim_id,
            "rate_limited",
            retry_after_seconds=75.0,
        )
    )

    assert result.applied is True
    assert writes == [
        governor.paths.governor_state.name,
        governor.paths.breaker_state.name,
    ]
    assert _json(governor.paths.governor_state) == {"next_slot_at": clock.value + 75.0}
    assert _json(governor.paths.breaker_state)["state"] == "closed"
