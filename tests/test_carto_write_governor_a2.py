from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from workflowy_mcp.carto_job_governance import (
    MutexWaitCancelledError,
)
from workflowy_mcp.carto_write_governor import (
    CartoWriteGovernor,
    WriteGovernorSettings,
    account_write_governance_paths,
)
from workflowy_mcp.models.config import ServerConfig
from workflowy_mcp.models.errors import (
    AccountWriteBlockedError,
    NetworkError,
    WorkFlowyError,
    WriteGovernorDeferredError,
    WriteGovernorUnavailableError,
    WriteSlotBusyTimeoutError,
)

_SCOPE = "a" * 32
_SCOPE_B = "b" * 32


class FakeClock:
    def __init__(self, value: float = 1_000.0):
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


async def _advance_sleep(clock: FakeClock, seconds: float, _operation: str) -> None:
    clock.advance(seconds)
    await asyncio.sleep(0)


async def _no_sleep(_seconds: float, _operation: str) -> None:
    await asyncio.sleep(0)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def scheduler_mutants(monkeypatch):
    from test_cut_2b_governor import _install_governor_mutants
    _install_governor_mutants(monkeypatch)


def test_a2_paths_reuse_jobs_root_and_account_scope(tmp_path):
    paths = account_write_governance_paths(_SCOPE, tmp_path)
    assert paths.jobs_root == tmp_path
    assert paths.account_dir == tmp_path / ".governance" / _SCOPE
    assert not hasattr(paths, "write_slot_mutex")
    assert paths.governor_state == paths.account_dir / "write_governor.json"
    assert paths.breaker_state == paths.account_dir / "write_breaker.json"

    derived = CartoWriteGovernor(
        api_base_url="HTTPS://WORKFLOWY.COM:443/api/v1/",
        api_key="secret",
        jobs_root=tmp_path,
    )
    same = CartoWriteGovernor(
        api_base_url="https://workflowy.com/api/v1",
        api_key="secret",
        jobs_root=tmp_path,
    )
    assert derived.paths.account_scope_id == same.paths.account_scope_id


def test_closed_config_defaults_and_no_extra_keys():
    config = ServerConfig.model_validate({"WORKFLOWY_API_KEY": "test-key"})
    if os.environ.get("CUT2B_NEUTRALIZE_CALLER_WINDOW_DEFAULT"):
        config.write_governor.max_wait_before_refusal_seconds = 30.0
    assert config.write_governor.model_dump() == {
        "interval_seconds": 1.0,
        "max_wait_before_refusal_seconds": 3600.0,
        "breaker_initial_cooldown_seconds": 900.0,
        "breaker_max_cooldown_seconds": 7200.0,
        "probe_lease_seconds": 120.0,
        "retry_after_fallback_seconds": 60.0,
    }
    assert config.admission.model_dump() == {
        "cooldown_seconds": 60.0,
    }
    # U5 (spec [ap:HFYAG2]): the two fail-closed refresh delete-thresholds
    # join the same locked default set (conservative defaults 10 / 100).
    assert config.governance.model_dump() == {
        "mutex_wait_seconds": 10.0,
        "carto_folder_file_delete_threshold": 10,
        "carto_reconcile_delete_threshold": 100,
    }
    with pytest.raises(ValueError):
        ServerConfig.model_validate(
            {
                "WORKFLOWY_API_KEY": "test-key",
                "write_governor": {"invented_key": 1},
            }
        )


def test_retired_admission_timing_keys_are_read_discard_only():
    """Only the three named legacy keys disappear; all other policy stays."""
    retired = {"start_grace_seconds": {"old": "anything"},
               "spawn_grace_seconds": -999, "orphan_grace_seconds": None}
    config = ServerConfig.model_validate({
        "WORKFLOWY_API_KEY": "test-key",
        "admission": {**retired, "cooldown_seconds": 137.0},
        "write_governor": {"interval_seconds": 0.5, "max_wait_before_refusal_seconds": 7.0},
    })
    observed = config.admission.model_dump()
    if __import__("os").environ.get("CUT2B_NEUTRALIZE_CONFIG_DISCARD"):
        observed.update(retired)
    assert observed == {"cooldown_seconds": 137.0}, "retired admission fields were emitted"
    assert config.write_governor.interval_seconds == 0.5
    assert config.write_governor.max_wait_before_refusal_seconds == 7.0
    with pytest.raises(ValueError):
        ServerConfig.model_validate({"WORKFLOWY_API_KEY": "test-key",
                                     "admission": {"unrelated_unknown": 1}})


def test_two_real_processes_issue_globally_ordered_spaced_tickets(tmp_path):
    """Historical name: two loop tasks/objects share the real 1.0 s stream."""
    async def run():
        clock = FakeClock()
        governors = [CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock) for _ in range(2)]
        tickets = await asyncio.gather(*(g.acquire_write_slot("task", sleep_fn=_no_sleep) for g in governors))
        slots = sorted(ticket.slot_at for ticket in tickets)
        assert slots == [clock.value, clock.value + 1.0]
        assert (await governors[0].snapshot())["governor"]["next_slot_at"] == clock.value + 2
        assert not governors[0].paths.governor_state.exists()
    asyncio.run(run())


def test_account_scopes_pace_independently_and_identical_scopes_share(tmp_path):
    clock = FakeClock()
    settings = WriteGovernorSettings(interval_seconds=2.0)
    first = CartoWriteGovernor(
        account_scope_id=_SCOPE, jobs_root=tmp_path, settings=settings, clock=clock
    )
    same = CartoWriteGovernor(
        account_scope_id=_SCOPE, jobs_root=tmp_path, settings=settings, clock=clock
    )
    other = CartoWriteGovernor(
        account_scope_id=_SCOPE_B, jobs_root=tmp_path, settings=settings, clock=clock
    )

    ticket_1 = asyncio.run(first.acquire_write_slot("one", sleep_fn=_no_sleep))
    ticket_2 = asyncio.run(same.acquire_write_slot("two", sleep_fn=_no_sleep))
    ticket_other = asyncio.run(other.acquire_write_slot("other", sleep_fn=_no_sleep))
    assert ticket_2.slot_at == ticket_1.slot_at + 2.0
    assert ticket_other.slot_at == ticket_1.slot_at


def test_busy_timeout_is_distinct_from_broken_and_contention_is_never_broken(tmp_path):
    async def run():
        governor = CartoWriteGovernor(
            account_scope_id=_SCOPE,
            jobs_root=tmp_path,
            settings=WriteGovernorSettings(mutex_wait_seconds=0.1, mutex_poll_seconds=0.01),
        )
        await governor._schedule.lock.acquire()
        async def release_later():
            await asyncio.sleep(0.2)
            if governor._schedule.lock.locked():
                governor._schedule.lock.release()
        releaser = asyncio.create_task(release_later())
        try:
            with pytest.raises(WriteSlotBusyTimeoutError) as caught:
                await governor.snapshot()
            assert caught.value.governance_path == str(governor.paths.account_dir)
            assert not isinstance(caught.value, WriteGovernorUnavailableError)
            assert not governor._schedule.initialized
        finally:
            if governor._schedule.lock.locked():
                governor._schedule.lock.release()
            await releaser
        assert (await governor.snapshot())["governor"]["next_slot_at"] == 0
    asyncio.run(run())


def test_broken_governance_path_fails_closed_and_names_exact_path(tmp_path):
    broken_root = tmp_path / "not-a-directory"
    broken_root.write_text("file", encoding="utf-8")
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=broken_root)
    with pytest.raises(WriteGovernorUnavailableError) as caught:
        asyncio.run(governor.snapshot())
    assert caught.value.governance_path == str(governor.paths.governor_state)


def test_unwritable_state_writer_fails_closed_and_names_state_path(tmp_path):
    def broken_writer(path, _payload):
        raise PermissionError(f"denied {path}")

    governor = CartoWriteGovernor(
        account_scope_id=_SCOPE,
        jobs_root=tmp_path,
        atomic_writer=broken_writer,
    )
    with pytest.raises(WriteGovernorUnavailableError) as caught:
        asyncio.run(governor.defer_global_writes_until(75))
    assert caught.value.governance_path == str(governor.paths.governor_state)


def test_cancellation_during_mutex_wait_closes_pending_handle(tmp_path):
    """Historical handle name: a cancelled memory wait leaves no late acquire."""
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 3

    async def run():
        governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path,
            settings=WriteGovernorSettings(mutex_wait_seconds=0.1, mutex_poll_seconds=0.005))
        await governor._schedule.lock.acquire()
        try:
            with pytest.raises(MutexWaitCancelledError):
                await governor.acquire_write_slot("cancel-lock", sleep_fn=_no_sleep, cancel_check=cancelled)
        finally:
            governor._schedule.lock.release()
        assert governor._schedule.next_slot_at == 0
        await governor.snapshot()
        assert not governor._schedule.lock.locked()
    asyncio.run(run())


def test_cancellation_during_ticket_sleep_raises_promptly_after_state_advance(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    asyncio.run(governor.acquire_write_slot("first", sleep_fn=_no_sleep))

    async def cancelled_sleep(_seconds: float, _operation: str) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(governor.acquire_write_slot("second", sleep_fn=cancelled_sleep))
    assert asyncio.run(governor.snapshot())["governor"]["next_slot_at"] == clock.value + 2.0
    assert not governor.paths.governor_state.exists()


def test_no_ticket_sleep_occurs_while_write_slot_mutex_is_held(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    asyncio.run(governor.acquire_write_slot("first", sleep_fn=_no_sleep))
    acquired_during_sleep = False

    async def sleeper(_seconds: float, _operation: str) -> None:
        nonlocal acquired_during_sleep
        assert not governor._schedule.lock.locked(), "paced sleep held the short scheduler lock"
        await governor._schedule.lock.acquire()
        acquired_during_sleep = True
        governor._schedule.lock.release()

    asyncio.run(governor.acquire_write_slot("second", sleep_fn=sleeper))
    assert acquired_during_sleep


def test_bounded_wait_refuses_before_state_advance_and_leaves_bytes_unchanged(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(
        account_scope_id=_SCOPE,
        jobs_root=tmp_path,
        clock=clock,
        settings=WriteGovernorSettings(max_wait_before_refusal_seconds=30.0),
    )
    governor.paths.account_dir.mkdir(parents=True)
    governor.paths.governor_state.write_text(
        json.dumps({"next_slot_at": clock.value + 31.0}, separators=(",", ":")),
        encoding="utf-8",
    )
    before = governor.paths.governor_state.read_bytes()
    before_cursor = asyncio.run(governor.snapshot())["governor"]["next_slot_at"]
    with pytest.raises(WriteGovernorDeferredError) as caught:
        asyncio.run(governor.acquire_write_slot("too-far", sleep_fn=_no_sleep))
    assert caught.value.retry_after == 31.0
    assert caught.value.deferred_until == clock.value + 31.0
    assert governor.paths.governor_state.read_bytes() == before
    assert asyncio.run(governor.snapshot())["governor"]["next_slot_at"] == before_cursor
    implicit = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path / "implicit", clock=clock)
    asyncio.run(implicit.defer_global_writes_until(60))
    assert asyncio.run(implicit.acquire_write_slot("default-window", sleep_fn=_no_sleep)).slot_at == clock.value + 60


def test_crash_after_ticket_issuance_safely_wastes_slot(tmp_path):
    """Historical crash name: cancelled sleep wastes memory, not durable safety."""
    from test_cut_2b_governor import _reset_schedule
    async def run():
        clock = FakeClock()
        governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
        await governor.defer_global_writes_until(5)
        entered = asyncio.Event()
        async def parked(delay, operation):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(governor.acquire_write_slot("cancel-sleeper", sleep_fn=parked))
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert governor._schedule.next_slot_at == 1006.0
        assert (await governor.acquire_write_slot("same-process", sleep_fn=_no_sleep)).slot_at == 1006.0
        assert _json(governor.paths.governor_state) == {"next_slot_at": 1005.0}
        _reset_schedule(governor)
        restarted = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
        assert (await restarted.acquire_write_slot("restart", sleep_fn=_no_sleep)).slot_at == 1005.0
    asyncio.run(run())


def test_missing_manual_deletion_and_malformed_governor_state_self_heal(tmp_path):
    """Missing/malformed startup floors need no file repair; live edits do not reload."""
    from test_cut_2b_governor import _reset_schedule
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path)
    first = asyncio.run(governor.snapshot())
    assert first["governor"] == {"next_slot_at": 0.0}
    assert not governor.paths.governor_state.exists()
    governor.paths.account_dir.mkdir(parents=True)
    governor.paths.governor_state.write_text("{bad json", encoding="utf-8")
    _reset_schedule(governor)
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path)
    healed = asyncio.run(governor.snapshot())
    assert healed["governor"] == {"next_slot_at": 0.0}
    assert governor.paths.governor_state.read_text() == "{bad json"
    assert not list(governor.paths.account_dir.glob("write_governor.json.corrupt.*"))

    cursor = asyncio.run(governor.defer_global_writes_until(75))
    governor.paths.governor_state.unlink()
    ticket = asyncio.run(governor.acquire_write_slot("after-delete", sleep_fn=_no_sleep))
    assert ticket.slot_at == cursor, "live deletion reset the memory schedule"
    _reset_schedule(governor)
    restarted = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path)
    assert asyncio.run(restarted.snapshot())["governor"] == {"next_slot_at": 0.0}


def test_retry_after_defer_is_monotonic_max_merge_and_zero_is_noop(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    governor.paths.account_dir.mkdir(parents=True)
    governor.paths.governor_state.write_text(
        json.dumps({"next_slot_at": clock.value + 100.0}, separators=(",", ":")),
        encoding="utf-8",
    )
    assert asyncio.run(governor.defer_global_writes_until(10.0)) == clock.value + 100.0
    assert _json(governor.paths.governor_state) == {"next_slot_at": clock.value + 100.0}
    before = governor.paths.governor_state.read_bytes()
    assert asyncio.run(governor.defer_global_writes_until(0.0)) == clock.value + 100.0
    assert governor.paths.governor_state.read_bytes() == before
    assert asyncio.run(governor.defer_global_writes_until(200.0)) == clock.value + 200.0


def test_initial_wfb_trip_opens_and_open_state_refuses_locally(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    transition = asyncio.run(governor.trip_wfb(reason="WFB(3)", evidence_excerpt="403 WFB(3)"))
    assert transition.breaker["state"] == "open"
    assert transition.breaker["blocked_until"] == clock.value + 900.0
    slept = False

    async def sleeper(_seconds: float, _operation: str) -> None:
        nonlocal slept
        slept = True

    with pytest.raises(AccountWriteBlockedError) as caught:
        asyncio.run(governor.acquire_write_slot("blocked", sleep_fn=sleeper))
    assert caught.value.retry_after == 900.0
    assert caught.value.blocked_until == clock.value + 900.0
    assert not slept


def _claim_probe(governor: CartoWriteGovernor, clock: FakeClock):
    asyncio.run(governor.trip_wfb())
    clock.advance(900.0)
    return asyncio.run(governor.acquire_write_slot("probe", sleep_fn=_no_sleep))


def test_expired_open_claims_exactly_one_probe_and_live_probe_blocks_others(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    ticket = _claim_probe(governor, clock)
    assert ticket.is_probe
    state = _json(governor.paths.breaker_state)
    assert state["state"] == "half_open"
    assert state["probe_claim_id"] == ticket.probe_claim_id
    assert state["probe_lease_expires_at"] == clock.value + 120.0

    with pytest.raises(AccountWriteBlockedError) as caught:
        asyncio.run(governor.acquire_write_slot("other", sleep_fn=_no_sleep))
    assert caught.value.retry_after == 120.0


def test_state_agnostic_live_probe_guard_blocks_expired_open_word(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    asyncio.run(governor.snapshot())
    governor.paths.account_dir.mkdir(parents=True, exist_ok=True)
    governor.paths.breaker_state.write_text(
        json.dumps(
            {
                "state": "open",
                "blocked_until": clock.value - 1,
                "cooldown_s": 900,
                "probe_claim_id": "1" * 32,
                "probe_lease_expires_at": clock.value + 50,
                "reason": "probe live",
                "evidence_excerpt": "",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    with pytest.raises(AccountWriteBlockedError) as caught:
        asyncio.run(governor.acquire_write_slot("blocked", sleep_fn=_no_sleep))
    assert caught.value.retry_after == 50.0


def test_expired_probe_lease_allows_new_claim_and_old_token_is_stale(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    first = _claim_probe(governor, clock)
    clock.advance(120.0)
    second = asyncio.run(governor.acquire_write_slot("new-probe", sleep_fn=_no_sleep))
    assert second.probe_claim_id != first.probe_claim_id
    before = governor.paths.breaker_state.read_bytes()
    result = asyncio.run(governor.record_probe_result(first.probe_claim_id, "success"))
    assert not result.applied
    assert governor.paths.breaker_state.read_bytes() == before


def test_probe_claim_and_ticket_are_persisted_in_one_transactional_acquire(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    ticket = _claim_probe(governor, clock)
    breaker = _json(governor.paths.breaker_state)
    pacing = asyncio.run(governor.snapshot())["governor"]
    assert breaker["probe_claim_id"] == ticket.probe_claim_id
    assert pacing["next_slot_at"] == ticket.slot_at + 1.0
    assert not governor.paths.governor_state.exists()


def test_probe_holder_recheck_passes_and_pretrip_reservation_is_blocked(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    reservation = asyncio.run(governor.acquire_write_slot("pre-trip", sleep_fn=_no_sleep))
    asyncio.run(governor.trip_wfb())
    with pytest.raises(AccountWriteBlockedError):
        asyncio.run(governor.recheck_before_send(reservation))

    clock.advance(900.0)
    probe = asyncio.run(governor.acquire_write_slot("probe", sleep_fn=_no_sleep))
    asyncio.run(governor.recheck_before_send(probe))


def test_expired_recheck_retry_after_is_never_negative(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    reservation = asyncio.run(governor.acquire_write_slot("old", sleep_fn=_no_sleep))
    asyncio.run(governor.trip_wfb())
    clock.advance(901.0)
    with pytest.raises(AccountWriteBlockedError) as caught:
        asyncio.run(governor.recheck_before_send(reservation))
    assert caught.value.retry_after == 0
    assert "fresh acquire" in caught.value.reason


@pytest.mark.parametrize("outcome", ["success", "client_error"])
def test_matching_probe_2xx_and_non_wfb_4xx_close(outcome, tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    probe = _claim_probe(governor, clock)
    result = asyncio.run(governor.record_probe_result(probe.probe_claim_id, outcome))
    assert result.applied
    assert result.breaker["state"] == "closed"
    assert result.breaker["probe_claim_id"] is None


def test_matching_probe_429_closes_and_max_merges_defer_atomically(tmp_path, monkeypatch):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    probe = _claim_probe(governor, clock)
    asyncio.run(governor.defer_global_writes_until(300))
    result = asyncio.run(
        governor.record_probe_result(
            probe.probe_claim_id,
            "rate_limited",
            retry_after_seconds=10,
        )
    )
    assert result.applied
    assert result.breaker["state"] == "closed"
    assert result.next_slot_at == clock.value + 300
    assert _json(governor.paths.governor_state)["next_slot_at"] == clock.value + 300

    # The covered +300/+10 case needs no new floor write. Exercise ORDER on
    # a genuinely increasing floor too, never by restoring redundant writes.
    changed = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path / "changed-floor", clock=clock)
    next_probe = _claim_probe(changed, clock)
    writes = []
    original = changed._write_state
    def ordered_write(path, payload):
        writes.append(Path(path).name)
        return original(path, payload)
    monkeypatch.setattr(changed, "_write_state", ordered_write)
    result = asyncio.run(changed.record_probe_result(next_probe.probe_claim_id, "rate_limited", retry_after_seconds=10))
    assert result.applied and result.breaker["state"] == "closed"
    assert writes == ["write_governor.json", "write_breaker.json"], "changed deferral was not durable before close"
    assert _json(changed.paths.governor_state) == {"next_slot_at": clock.value + 10}


def test_matching_probe_429_zero_delay_closes_with_explicit_noop_defer(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    probe = _claim_probe(governor, clock)
    before = asyncio.run(governor.snapshot())["governor"]["next_slot_at"]
    before_file = governor.paths.governor_state.read_bytes() if governor.paths.governor_state.exists() else None
    result = asyncio.run(
        governor.record_probe_result(probe.probe_claim_id, "rate_limited", retry_after_seconds=0)
    )
    assert result.breaker["state"] == "closed"
    assert result.next_slot_at == before
    assert (governor.paths.governor_state.read_bytes() if governor.paths.governor_state.exists() else None) == before_file


@pytest.mark.parametrize("outcome", ["server_error", "timeout", "network_error"])
def test_matching_probe_failure_reopens_at_bounded_current_cooldown(outcome, tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    probe = _claim_probe(governor, clock)
    result = asyncio.run(
        governor.record_probe_result(probe.probe_claim_id, outcome, reason=f"probe {outcome}")
    )
    assert result.breaker["state"] == "open"
    assert result.breaker["cooldown_s"] == 900.0
    assert result.breaker["blocked_until"] == clock.value + 900.0


def test_matching_wfb_probe_doubles_to_cap(tmp_path):
    clock = FakeClock()
    settings = WriteGovernorSettings(
        breaker_initial_cooldown_seconds=10.0,
        breaker_max_cooldown_seconds=20.0,
        probe_lease_seconds=5.0,
    )
    governor = CartoWriteGovernor(
        account_scope_id=_SCOPE,
        jobs_root=tmp_path,
        clock=clock,
        settings=settings,
    )
    asyncio.run(governor.trip_wfb())
    clock.advance(10.0)
    probe = asyncio.run(governor.acquire_write_slot("probe", sleep_fn=_no_sleep))
    doubled = asyncio.run(governor.trip_wfb(probe_claim_id=probe.probe_claim_id))
    assert doubled.breaker["cooldown_s"] == 20.0
    clock.advance(20.0)
    probe_2 = asyncio.run(governor.acquire_write_slot("probe-2", sleep_fn=_no_sleep))
    capped = asyncio.run(governor.trip_wfb(probe_claim_id=probe_2.probe_claim_id))
    assert capped.breaker["cooldown_s"] == 20.0


def test_wfb_while_open_never_shortens_or_repeatedly_doubles(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    first = asyncio.run(governor.trip_wfb())
    clock.advance(100.0)
    second = asyncio.run(governor.trip_wfb(evidence_excerpt="stale one"))
    clock.advance(100.0)
    third = asyncio.run(governor.trip_wfb(evidence_excerpt="stale two"))
    assert second.breaker["blocked_until"] >= first.breaker["blocked_until"]
    assert third.breaker["blocked_until"] >= second.breaker["blocked_until"]
    assert second.breaker["cooldown_s"] == 900.0
    assert third.breaker["cooldown_s"] == 900.0


def test_nonmatching_half_open_wfb_reopens_without_doubling(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    _claim_probe(governor, clock)
    result = asyncio.run(governor.trip_wfb(probe_claim_id="f" * 32))
    assert result.breaker["state"] == "open"
    assert result.breaker["cooldown_s"] == 900.0
    assert result.breaker["blocked_until"] == clock.value + 900.0


def test_stale_probe_outcomes_cannot_close_or_reopen_newer_generation(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    first = _claim_probe(governor, clock)
    clock.advance(120.0)
    second = asyncio.run(governor.acquire_write_slot("second", sleep_fn=_no_sleep))
    before = governor.paths.breaker_state.read_bytes()
    for outcome in ("success", "client_error", "server_error", "timeout", "network_error"):
        result = asyncio.run(governor.record_probe_result(first.probe_claim_id, outcome))
        assert not result.applied
        assert governor.paths.breaker_state.read_bytes() == before
    assert second.probe_claim_id == _json(governor.paths.breaker_state)["probe_claim_id"]


def test_probe_cancellation_or_death_does_not_proactively_release_and_lease_recovers(tmp_path):
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    probe = _claim_probe(governor, clock)
    # Cooperative cancellation/process death reports no result by design.
    state = _json(governor.paths.breaker_state)
    assert state["probe_claim_id"] == probe.probe_claim_id
    with pytest.raises(AccountWriteBlockedError):
        asyncio.run(governor.acquire_write_slot("before-expiry", sleep_fn=_no_sleep))
    clock.advance(120.0)
    replacement = asyncio.run(governor.acquire_write_slot("after-expiry", sleep_fn=_no_sleep))
    assert replacement.probe_claim_id != probe.probe_claim_id


def test_malformed_breaker_state_is_quarantined_and_manual_delete_is_safe(tmp_path):
    """Historical quarantine name: one fixed conservative cooldown, no sidecar."""
    clock = FakeClock()
    governor = CartoWriteGovernor(account_scope_id=_SCOPE, jobs_root=tmp_path, clock=clock)
    asyncio.run(governor.snapshot())
    governor.paths.account_dir.mkdir(parents=True)
    governor.paths.breaker_state.write_text("[]", encoding="utf-8")
    healed = asyncio.run(governor.snapshot())["breaker"]
    assert healed["state"] == "open" and healed["blocked_until"] == clock.value + 900
    assert not list(governor.paths.account_dir.glob("write_breaker.json.corrupt.*"))
    clock.advance(900)
    probe = asyncio.run(governor.acquire_write_slot("recovery", sleep_fn=_no_sleep))
    asyncio.run(governor.record_probe_result(probe.probe_claim_id, "success"))
    assert asyncio.run(governor.snapshot())["breaker"]["state"] == "closed"
    governor.paths.breaker_state.unlink()
    assert asyncio.run(governor.snapshot())["breaker"]["state"] == "closed"


def test_deferred_and_blocked_error_contracts_and_nonretryable_hierarchy():
    deferred = WriteGovernorDeferredError(
        retry_after=5,
        deferred_until=123,
        reason="queue too far",
    )
    assert deferred.details == {
        "retry_after": 5.0,
        "deferred_until": 123.0,
        "reason": "queue too far",
    }
    blocked = AccountWriteBlockedError(
        retry_after=-1,
        blocked_until=456,
        reason="WFB",
    )
    assert blocked.details == {
        "retry_after": 0.0,
        "blocked_until": 456.0,
        "reason": "WFB",
    }
    assert isinstance(deferred, WorkFlowyError)
    assert isinstance(blocked, WorkFlowyError)
    assert not isinstance(deferred, NetworkError)
    assert not isinstance(blocked, NetworkError)


def test_a2_substrate_is_consumed_only_by_the_phase_b_client_authority():
    repo_root = Path(__file__).resolve().parents[1]
    api_core = (repo_root / "src/workflowy_mcp/client/api_client_core.py").read_text(
        encoding="utf-8"
    )
    server = (repo_root / "src/workflowy_mcp/server.py").read_text(encoding="utf-8")
    assert "async def _send_workflowy_write(" in api_core
    assert "from ..carto_write_governor import" in api_core
    assert "carto_write_governor" not in server
