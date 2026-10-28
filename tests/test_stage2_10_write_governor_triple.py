"""Stage2-10: three same-loop tasks share one scheduler and durable breaker.

Ordered ticket holder → trip/probe → Retry-After deferrer retains stale-ticket
recheck, one probe/exact token, and +5 vs +2 max-merge. No process/file actor.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from workflowy_mcp.carto_write_governor import CartoWriteGovernor
from workflowy_mcp.models.errors import AccountWriteBlockedError
from test_carto_write_governor_a2 import FakeClock, _no_sleep
from test_cut_2b_governor import _install_governor_mutants


def test_stale_ticket_recheck_refuses_one_probe_and_max_merge_never_shortens(tmp_path, monkeypatch):
    """Each retained oracle is measured through the actual scheduler methods."""
    _install_governor_mutants(monkeypatch)
    async def run():
        clock = FakeClock()
        governors = [CartoWriteGovernor(account_scope_id='a'*32, jobs_root=tmp_path, clock=clock) for _ in range(3)]
        held, tripped, live, deferred = (asyncio.Event() for _ in range(4))
        values = {}
        async def holder():
            ticket = await governors[0].acquire_write_slot('holder', sleep_fn=_no_sleep)
            assert ticket.probe_claim_id is None
            held.set()
            await tripped.wait()
            refused = False
            if not os.environ.get('STAGE2_NEUTRALIZE_RECHECK'):
                try:
                    await governors[0].recheck_before_send(ticket)
                except AccountWriteBlockedError:
                    refused = True
            values['recheck_refused'] = refused
        async def tripper():
            await held.wait()
            await governors[1].trip_wfb()
            tripped.set()
            clock.advance(900)
            ticket = await governors[1].acquire_write_slot('probe', sleep_fn=_no_sleep)
            state = json.loads(governors[1].paths.breaker_state.read_text())
            assert ticket.is_probe and state['state'] == 'half_open'
            assert state['probe_claim_id'] == ticket.probe_claim_id
            values['token'] = ticket.probe_claim_id
            live.set()
            await deferred.wait()
            transition = await governors[1].record_probe_result(ticket.probe_claim_id, 'rate_limited', retry_after_seconds=2)
            values['transition'] = transition
        async def deferrer():
            await live.wait()
            stale = await governors[2].record_probe_result('0'*32, 'success')
            assert not stale.applied, 'stale token closed the current probe'
            blocked = False
            try:
                await governors[2].acquire_write_slot('second probe', sleep_fn=_no_sleep)
            except AccountWriteBlockedError:
                blocked = True
            values['unique_probe'] = blocked
            values['floor'] = await governors[2].defer_global_writes_until(5)
            deferred.set()
        tasks = [asyncio.create_task(fn()) for fn in (holder, tripper, deferrer)]
        # Assertion failure in a contender must release the other test gates.
        for task in tasks:
            def wake_on_error(done):
                if not done.cancelled() and done.exception() is not None:
                    for event in (held, tripped, live, deferred):
                        event.set()
            task.add_done_callback(wake_on_error)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        assert values['recheck_refused'], 'stale pre-trip ticket passed recheck'
        assert values['unique_probe'], 'a second probe was admitted'
        transition = values['transition']
        assert transition.applied and transition.breaker['state'] == 'closed'
        assert transition.next_slot_at == values['floor'] == 1905.0
        assert json.loads(governors[1].paths.governor_state.read_text()) == {'next_slot_at': 1905.0}, 'probe +2 shortened the persisted +5 floor'
    asyncio.run(run())
