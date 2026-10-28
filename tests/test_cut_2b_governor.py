"""CUT-2B scheduler: memory tickets, unchanged durable recovery, real restarts.

Counterfactuals are test-only monkeypatches. No product flag/reset/await seam.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
from dataclasses import replace
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import workflowy_mcp.carto_write_governor as G
from workflowy_mcp.carto_job_governance import MutexWaitCancelledError
from workflowy_mcp.models.errors import AccountWriteBlockedError, WriteGovernorDeferredError, WriteGovernorUnavailableError
from test_carto_write_governor_a2 import FakeClock, _no_sleep

SCOPE = 'a' * 32


def _reset_schedule(governor):
    """A joined isolated-root restart, never a production reset operation."""
    key = (os.path.normcase(os.path.abspath(os.fspath(governor.paths.jobs_root))), governor.paths.account_scope_id)
    assert not governor._schedule.lock.locked()
    G._WRITE_SCHEDULES.pop(key, None)


def _json_or_none(path):
    return json.loads(path.read_text()) if path.exists() else None


def _install_governor_mutants(monkeypatch):
    """Named test counterfactuals shared with the assigned A2/GOV03 witnesses."""
    enabled = os.environ.get
    init, acquire, defer, record = G.CartoWriteGovernor.__init__, G.CartoWriteGovernor.acquire_write_slot, G.CartoWriteGovernor.defer_global_writes_until, G.CartoWriteGovernor.record_probe_result
    write, load, breaker, lock = G.CartoWriteGovernor._write_state, G.CartoWriteGovernor._load_state, G.CartoWriteGovernor._load_breaker, G.CartoWriteGovernor._write_slot_lock

    def altered_init(self, *args, **kwargs):
        if enabled('CUT2B_NEUTRALIZE_CALLER_WINDOW_DEFAULT') and kwargs.get('settings') is None:
            kwargs['settings'] = G.WriteGovernorSettings(max_wait_before_refusal_seconds=30.0)
        init(self, *args, **kwargs)
        if enabled('CUT2B_NEUTRALIZE_SHARED_SCHEDULES'):
            self._schedule = G._WriteSchedule(asyncio.Lock())
        if enabled('CUT2B_NEUTRALIZE_SCHEDULE_KEY'):
            self._schedule = next(state for (root, scope), state in G._WRITE_SCHEDULES.items()
                                  if root == os.path.normcase(os.path.abspath(os.fspath(self.paths.jobs_root))) or scope == self.paths.account_scope_id)

    async def altered_acquire(self, operation, *, sleep_fn, cancel_check=None):
        if enabled('CUT2B_NEUTRALIZE_REFUSAL_ADVANCE') and self._schedule.initialized and self._schedule.next_slot_at - self._now() > self.settings.max_wait_before_refusal_seconds:
            self._schedule.next_slot_at += self.settings.interval_seconds
        original_sleep = sleep_fn
        if enabled('CUT2B_NEUTRALIZE_SLEEP_LOCK'):
            async def sleep_fn(delay, operation):
                async with self._schedule.lock:
                    await original_sleep(delay, operation)
        before_cursor = self._schedule.next_slot_at
        saved_breaker = self._load_breaker
        bypass_probe = enabled('CUT2B_NEUTRALIZE_LIVE_PROBE')
        if bypass_probe:
            def acquire_without_live_probe():
                state = saved_breaker()
                return self._default_breaker() if state['state'] == 'half_open' else state
            self._load_breaker = acquire_without_live_probe
        try:
            ticket = await acquire(self, operation, sleep_fn=sleep_fn, cancel_check=cancel_check)
        except asyncio.CancelledError:
            if enabled('CUT2B_NEUTRALIZE_CANCEL_RESERVATION'):
                self._schedule.next_slot_at = before_cursor
            raise
        finally:
            if bypass_probe:
                self._load_breaker = saved_breaker
        if enabled('CUT2B_NEUTRALIZE_PACING'):
            self._schedule.next_slot_at = ticket.slot_at
        if enabled('CUT2B_NEUTRALIZE_TICKET_PERSISTENCE'):
            self._write_state(self.paths.governor_state, {'next_slot_at': self._schedule.next_slot_at})
        return ticket

    def altered_write(self, path, payload):
        if path == self.paths.breaker_state and payload.get('state') == 'half_open' and enabled('CUT2B_NEUTRALIZE_PROBE_PERSISTENCE'):
            return
        if path == self.paths.governor_state:
            if enabled('CUT2B_NEUTRALIZE_DEFER_PERSISTENCE'):
                return
            if enabled('CUT2B_NEUTRALIZE_DEFER_QUEUE_PERSISTENCE'):
                payload = {'next_slot_at': self._schedule.next_slot_at}
        try:
            return write(self, path, payload)
        except WriteGovernorUnavailableError:
            if path == self.paths.governor_state and enabled('CUT2B_NEUTRALIZE_FAILED_DEFER_COMMIT'):
                self._schedule.deferral_until = payload['next_slot_at']
            if enabled('CUT2B_NEUTRALIZE_OPEN_FALLBACK'):
                self._schedule.breaker_fallback = None
            if not enabled('CUT2B_NEUTRALIZE_STATE_IO_REFUSAL'):
                raise

    def altered_load(self, path, **kwargs):
        try:
            state = load(self, path, **kwargs)
        except WriteGovernorUnavailableError:
            if not enabled('CUT2B_NEUTRALIZE_STATE_IO_REFUSAL'):
                raise
            return dict(kwargs['default'])
        if path == self.paths.breaker_state and enabled('CUT2B_NEUTRALIZE_CORRUPT_BREAKER_CLOSED') and state['reason'].startswith('Unreadable breaker'):
            return self._default_breaker()
        return state

    def altered_breaker(self):
        state = breaker(self)
        if enabled('CUT2B_NEUTRALIZE_BREAKER_RESTART'):
            return self._default_breaker()
        if enabled('CUT2B_NEUTRALIZE_CORRUPT_REARM') and state['reason'].startswith('Unreadable breaker'):
            state['blocked_until'] = self._now() + self.settings.breaker_initial_cooldown_seconds
        return state

    async def altered_defer(self, delay, **kwargs):
        if enabled('CUT2B_NEUTRALIZE_DEFER_MAX'):
            async with lock(self, cancel_check=kwargs.get('cancel_check')):
                pass
            self._schedule.deferral_until = 0.0
        result = await defer(self, delay, **kwargs)
        if enabled('CUT2B_NEUTRALIZE_QUEUE_MAX') and delay > 0:
            self._schedule.next_slot_at = self._schedule.deferral_until
            result = self._schedule.next_slot_at
        return result

    async def altered_record(self, token, outcome, **kwargs):
        if enabled('CUT2B_NEUTRALIZE_DEFER_MAX'):
            self._schedule.deferral_until = 0.0
        if enabled('CUT2B_NEUTRALIZE_DEFER_FIRST'):
            original = self._write_state
            def close_first(path, payload):
                if path == self.paths.governor_state:
                    original(self.paths.breaker_state, self._closed_breaker(reason='test-only reordered close'))
                return original(path, payload)
            self._write_state = close_first
            try:
                result = await record(self, token, outcome, **kwargs)
            finally:
                self._write_state = original
        else:
            result = await record(self, token, outcome, **kwargs)
        if outcome == 'rate_limited' and kwargs.get('retry_after_seconds') == 0:
            if enabled('CUT2B_NEUTRALIZE_ZERO_DEFER_WRITE'):
                self._write_state(self.paths.governor_state, {'next_slot_at': self._schedule.deferral_until})
            if enabled('CUT2B_NEUTRALIZE_ZERO_PROBE_CURSOR'):
                result = replace(result, next_slot_at=self._schedule.deferral_until)
        return result

    @asynccontextmanager
    async def altered_lock(self, **kwargs):
        settings = self.settings
        if enabled('CUT2B_NEUTRALIZE_LOCK_BUDGET'):
            self.settings = replace(settings, mutex_wait_seconds=1.0)
        try:
            async with lock(self, **kwargs):
                if enabled('CUT2B_NEUTRALIZE_GOVERNOR_RELOAD'):
                    self._load_governor()
                yield
        finally:
            self.settings = settings

    monkeypatch.setattr(G.CartoWriteGovernor, '__init__', altered_init)
    monkeypatch.setattr(G.CartoWriteGovernor, 'acquire_write_slot', altered_acquire)
    monkeypatch.setattr(G.CartoWriteGovernor, 'defer_global_writes_until', altered_defer)
    monkeypatch.setattr(G.CartoWriteGovernor, 'record_probe_result', altered_record)
    monkeypatch.setattr(G.CartoWriteGovernor, '_write_state', altered_write)
    monkeypatch.setattr(G.CartoWriteGovernor, '_load_state', altered_load)
    monkeypatch.setattr(G.CartoWriteGovernor, '_load_breaker', altered_breaker)
    monkeypatch.setattr(G.CartoWriteGovernor, '_write_slot_lock', altered_lock)
    if enabled('CUT2B_NEUTRALIZE_RESTART_DEFERRAL'):
        monkeypatch.setattr(G.CartoWriteGovernor, '_load_governor', lambda self: {'next_slot_at': 0.0})
    if enabled('CUT2B_NEUTRALIZE_PROBE_TOKEN'):
        monkeypatch.setattr(G.CartoWriteGovernor, '_probe_matches', lambda self, state, token: state['state'] == 'half_open')
    if enabled('CUT2B_NEUTRALIZE_CANCEL_WAIT'):
        async def no_cancel(*args):
            pass
        monkeypatch.setattr(G, '_check_cancel_async', no_cancel)


@pytest.fixture(autouse=True)
def governor_mutants(monkeypatch):
    _install_governor_mutants(monkeypatch)


async def _fake_send(governor, clock, sends):
    """Ticket + real recheck precede an isolated MockTransport HTTP send."""
    async def sleep(delay, operation):
        clock.advance(delay)
        await asyncio.sleep(0)
    ticket = await governor.acquire_write_slot('fake write', sleep_fn=sleep)
    await governor.recheck_before_send(ticket)
    async def handler(request):
        assert str(request.url).startswith('https://cut2b.invalid/')
        sends.append(clock.value)
        return httpx.Response(200, json={'ok': True}, request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await client.post('https://cut2b.invalid/nodes')
    return ticket


def test_legacy_retry_after_survives_first_upgrade_before_first_send(tmp_path):
    async def run():
        clock, sends = FakeClock(), []
        governor = G.CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock)
        governor.paths.account_dir.mkdir(parents=True)
        G.atomic_write_json(governor.paths.governor_state, {'next_slot_at': clock.value + 75})
        G.atomic_write_json(governor.paths.breaker_state, governor._default_breaker())
        before = governor.paths.governor_state.read_bytes()
        await _fake_send(governor, clock, sends)
        assert sends == [1075.0], f'first upgraded send ignored the legacy floor: {sends}'
        assert governor.paths.governor_state.read_bytes() == before
    asyncio.run(run())


@pytest.mark.parametrize('probe', [False, True])
def test_current_nonprobe_and_probe_retry_after_survive_restart(tmp_path, probe):
    async def run():
        clock, sends = FakeClock(), []
        governor = G.CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock)
        if probe:
            await governor.trip_wfb()
            clock.advance(900)
            ticket = await governor.acquire_write_slot('probe', sleep_fn=_no_sleep)
            await governor.record_probe_result(ticket.probe_claim_id, 'rate_limited', retry_after_seconds=75)
            assert (await governor.snapshot())['breaker']['state'] == 'closed'
        else:
            await governor.defer_global_writes_until(75)
        floor = clock.value + 75
        _reset_schedule(governor)
        restarted = G.CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock)
        await _fake_send(restarted, clock, sends)
        assert sends == [floor], f'restart lost the complete 429 floor: {sends}, expected {floor}'
    asyncio.run(run())


def test_wfb_trip_survives_restart_before_first_send(tmp_path):
    async def run():
        clock, sends = FakeClock(), []
        governor = G.CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock)
        await governor.trip_wfb()
        _reset_schedule(governor)
        restarted = G.CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock)
        with pytest.raises(AccountWriteBlockedError):
            await _fake_send(restarted, clock, sends)
        assert sends == []
        clock.advance(900)
        ticket = await _fake_send(restarted, clock, sends)
        assert ticket.is_probe and sends == [1900.0]
        with pytest.raises(AccountWriteBlockedError):
            await _fake_send(restarted, clock, sends)
    asyncio.run(run())


def test_governor_reads_legacy_floor_once_and_never_persists_tickets(tmp_path, monkeypatch):
    async def run():
        clock = FakeClock()
        governors = [G.CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock) for _ in range(2)]
        path = governors[0].paths.governor_state
        path.parent.mkdir(parents=True)
        G.atomic_write_json(path, {'next_slot_at': 1000.0})
        before = path.read_bytes(), path.stat().st_mtime_ns
        reads = []
        original = Path.open
        def watched(path_arg, *args, **kwargs):
            if path_arg == path and (not args or 'r' in args[0]):
                reads.append(str(path_arg))
            return original(path_arg, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', watched)
        tickets = []
        for i in range(20):
            governor = governors[i % 2]
            tickets.append(await governor.acquire_write_slot(str(i), sleep_fn=_no_sleep))
            await governor.snapshot()
        assert [ticket.slot_at for ticket in tickets] == [1000.0 + i for i in range(20)], 'shared 1.0 s memory stream lost'
        assert reads == [str(path)], f'governor floor reloaded: {reads}'
        monkeypatch.setattr(Path, 'open', original)
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before, 'ordinary tickets persisted'
        other = G.CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path/'independent', clock=clock)
        assert (await other.acquire_write_slot('independent', sleep_fn=_no_sleep)).slot_at == 1000.0
    asyncio.run(run())


def test_deferral_only_file_does_not_capture_ticket_backlog(tmp_path):
    async def run():
        clock = FakeClock()
        governor = G.CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock)
        for _ in range(20):
            await governor.acquire_write_slot('queued', sleep_fn=_no_sleep)
        cursor = (await governor.snapshot())['governor']['next_slot_at']
        assert await governor.defer_global_writes_until(5) == cursor, 'Retry-After shortened the memory queue'
        assert _json_or_none(governor.paths.governor_state) == {'next_slot_at': 1005.0}, 'ticket backlog leaked into durable floor'
        assert await governor.defer_global_writes_until(2) == cursor
        assert _json_or_none(governor.paths.governor_state) == {'next_slot_at': 1005.0}, 'durable max-merge shortened'
        _reset_schedule(governor)
        restarted = G.CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock)
        assert (await restarted.acquire_write_slot('restart', sleep_fn=_no_sleep)).slot_at == 1005.0
        def denied(path, payload):
            raise PermissionError('test-only defer failure')
        restarted._atomic_writer = denied
        with pytest.raises(WriteGovernorUnavailableError):
            await restarted.defer_global_writes_until(50)
        assert restarted._schedule.next_slot_at == 1050.0
        assert restarted._schedule.deferral_until == 1005.0, 'failed write claimed a durable safety floor'
        assert _json_or_none(restarted.paths.governor_state) == {'next_slot_at': 1005.0}
        restarted._atomic_writer = G.atomic_write_json
        await restarted.defer_global_writes_until(50)
        assert _json_or_none(restarted.paths.governor_state) == {'next_slot_at': 1050.0}, 'later real defer did not retry its needed persistence'
    asyncio.run(run())


@pytest.mark.parametrize('repair_fails', [False, True])
def test_missing_and_corrupt_state_never_require_startup_repair(tmp_path, monkeypatch, caplog, repair_fails):
    async def run():
        clock = FakeClock()
        governor = G.CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock)
        assert not governor.paths.account_dir.exists(), 'constructor performed startup I/O'
        assert (await governor.snapshot())['breaker']['state'] == 'closed'
        assert not governor.paths.governor_state.exists() and not governor.paths.breaker_state.exists()
        _reset_schedule(governor)
        governor.paths.account_dir.mkdir(parents=True)
        governor.paths.governor_state.write_text('{bad governor')
        governor.paths.breaker_state.write_text('{bad breaker')
        def denied(path, payload):
            raise PermissionError('test-only denied repair')
        governor = G.CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock,
                                       **({'atomic_writer': denied} if repair_fails else {}))
        with pytest.raises(AccountWriteBlockedError):
            await governor.acquire_write_slot('corrupt', sleep_fn=_no_sleep)
        initial = (await governor.snapshot())['breaker']
        assert initial['state'] == 'open' and initial['blocked_until'] == 1900.0
        clock.advance(10)
        assert (await governor.snapshot())['breaker']['blocked_until'] == 1900.0, 'corrupt breaker rearmed'
        assert governor.paths.governor_state.read_text() == '{bad governor'
        assert not list(governor.paths.account_dir.glob('*.corrupt.*'))
        assert sum('Unreadable write-governance state' in rec.message and 'write_governor.json' in rec.message for rec in caplog.records) == 1
        if repair_fails:
            assert governor._schedule.breaker_fallback == initial
            governor._atomic_writer = G.atomic_write_json
        clock.advance(890)
        probe = await governor.acquire_write_slot('recovery', sleep_fn=_no_sleep)
        assert probe.is_probe
        await governor.record_probe_result(probe.probe_claim_id, 'success')
        assert (await governor.snapshot())['breaker']['state'] == 'closed'
        assert governor._schedule.breaker_fallback is None
        await governor.defer_global_writes_until(20)
        assert _json_or_none(governor.paths.governor_state) == {'next_slot_at': 1920.0}

        # A normal failed OPEN persistence also preserves its fixed known state.
        separate = G.CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path/'failed-trip', clock=clock, atomic_writer=denied)
        with pytest.raises(WriteGovernorUnavailableError):
            await separate.trip_wfb()
        with pytest.raises(AccountWriteBlockedError):
            await separate.acquire_write_slot('failed-open', sleep_fn=_no_sleep)
    asyncio.run(run())


@pytest.mark.parametrize('awaitable', [False, True])
def test_scheduler_lock_wait_cancel_and_sleep_leave_no_late_transaction(tmp_path, awaitable):
    async def run():
        clock = FakeClock()
        governor = G.CartoWriteGovernor(account_scope_id=SCOPE, jobs_root=tmp_path, clock=clock,
            settings=G.WriteGovernorSettings(mutex_wait_seconds=0.2, mutex_poll_seconds=0.005))
        await governor.snapshot()
        lock = governor._schedule.lock
        await lock.acquire()
        calls, ticks = [], []
        def cancel():
            calls.append(True)
            return len(calls) >= 3
        async def async_cancel():
            await asyncio.sleep(0)
            return cancel()
        task = asyncio.create_task(governor.acquire_write_slot('cancelled', sleep_fn=_no_sleep, cancel_check=async_cancel if awaitable else cancel))
        async def ticker():
            while not task.done():
                ticks.append(1)
                await asyncio.sleep(0)
        tick = asyncio.create_task(ticker())
        try:
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            assert calls and isinstance(result, MutexWaitCancelledError), f'wait did not observe cancellation: {result!r}'
            assert ticks and governor._schedule.next_slot_at == 0.0
        finally:
            lock.release()
            await asyncio.gather(task, tick, return_exceptions=True)
        assert governor._schedule.next_slot_at == 0.0, 'cancelled contender entered late'
        await lock.acquire()
        native = asyncio.create_task(governor.acquire_write_slot('native-cancel', sleep_fn=_no_sleep))
        await asyncio.sleep(0)
        native.cancel()
        try:
            result = (await asyncio.gather(native, return_exceptions=True))[0]
            assert isinstance(result, asyncio.CancelledError)
        finally:
            lock.release()
        assert governor._schedule.next_slot_at == 0.0
        await governor.acquire_write_slot('first', sleep_fn=_no_sleep)
        async def unlocked_sleep(delay, operation):
            assert not lock.locked(), 'paced sleep held the short scheduler lock'
            await lock.acquire()
            lock.release()
        await governor.acquire_write_slot('second', sleep_fn=unlocked_sleep)
    asyncio.run(run())


def _scheduler_offenders(source):
    if os.environ.get('CUT2B_NEUTRALIZE_SCHEDULER_FENCE'):
        return []
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == '_WriteSchedule':
            fields = {part.target.id for part in node.body if isinstance(part, ast.AnnAssign) and isinstance(part.target, ast.Name)}
            if fields != {'lock','initialized','next_slot_at','deferral_until','breaker_fallback'}:
                offenders.append('scheduler field shape')
        if isinstance(node, (ast.Name, ast.Attribute)) and (getattr(node, 'id', None) or getattr(node, 'attr', None)) in {'write_slot_mutex','acquire_os_lock','acquire_os_lock_async','_quarantine_and_reinitialize'}:
            offenders.append('retired OS/file lane')
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any('acquire_os_lock' in alias.name for alias in node.names):
            offenders.append('retired OS/file lane')
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and ('write_slot.mutex' in node.value or '.corrupt.' in node.value):
            offenders.append('retired OS/file lane')
        if isinstance(node, ast.FunctionDef) and node.name == '_quarantine_and_reinitialize':
            offenders.append('retired OS/file lane')
        if isinstance(node, ast.Call) and (getattr(node.func, 'attr', None) == '_write_state') and node.args and ast.unparse(node.args[0]) == 'self.paths.governor_state':
            cursor, owners, guards = node, [], []
            while cursor in parents:
                cursor = parents[cursor]
                if isinstance(cursor, ast.If):
                    guards.append(ast.unparse(cursor.test))
                if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owners.append(cursor)
            if not owners or owners[0].name not in {'defer_global_writes_until','record_probe_result'}:
                offenders.append('non-deferral governor writer')
            if 'new_deferral > self._schedule.deferral_until' not in guards:
                offenders.append('unchanged/zero deferral writer')
            if owners and owners[0].name == 'record_probe_result' and 'parsed_delay > 0' not in guards:
                offenders.append('unchanged/zero deferral writer')
            if owners and owners[0].name == 'defer_global_writes_until' and not any(
                isinstance(part, ast.If) and ast.unparse(part.test) == 'delay == 0'
                and any(isinstance(statement, ast.Return) for statement in part.body)
                for part in ast.walk(owners[0])
            ):
                offenders.append('unchanged/zero deferral writer')
        if isinstance(node, ast.Call) and (getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)) in {'create_task','ensure_future','Thread','Timer'}:
            offenders.append('background scheduler')
    return offenders


@pytest.mark.parametrize('mutation', [
        "from .carto_job_governance import acquire_os_lock_async as lane\n",
        "write_slot_mutex = 'write_slot.mutex'\n",
        "def _quarantine_and_reinitialize(path):\n    return str(path) + '.corrupt.1'\n",
        "class Bad:\n    def acquire_write_slot(self):\n        self._write_state(self.paths.governor_state, {})\n",
        "class Bad:\n    def snapshot(self):\n        self._write_state(self.paths.governor_state, {})\n",
        "class Bad:\n    def _load_governor(self):\n        self._write_state(self.paths.governor_state, {})\n",
        "class Bad:\n    def _write_slot_lock(self):\n        return acquire_os_lock(path)\n",
        "class Bad:\n    def defer_global_writes_until(self):\n        self._write_state(self.paths.governor_state, {})\n",
        "class _WriteSchedule:\n    lock: object\n    initialized: bool = False\n    next_slot_at: float = 0\n    deferral_until: float = 0\n    breaker_fallback: object = None\n    owner: int = 0\n",
    ], ids=['os-import','lock-path','quarantine','ticket-write','snapshot-write','startup-write','os-call','zero-defer-write','extra-schedule-field'])
def test_scheduler_negative_fence_rejects_file_lane_and_os_lock(mutation):
    source = Path(G.__file__).read_text()
    assert _scheduler_offenders(source) == []
    assert _scheduler_offenders(mutation), f'scheduler absence/writer tripwire missed {mutation}'
    calls = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == '_write_state' and node.args and ast.unparse(node.args[0]) == 'self.paths.governor_state':
            calls.append(node)
    assert len(calls) == 2, 'only the two positive changed-defer paths may persist the floor'
