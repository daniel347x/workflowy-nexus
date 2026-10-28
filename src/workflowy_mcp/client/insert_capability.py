"""One client's memory-only insert capability, separate from the write lane.

All selection/recording methods are synchronous, with no await or I/O in their
state transitions: concurrent tasks in the daemon cannot interleave a claim.
No timer, waiter, startup probe, session cache or persistent state is created.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Literal


class InsertCapabilityState(str, Enum):
    UNKNOWN = "UNKNOWN"
    AVAILABLE = "AVAILABLE"
    DEGRADED_UNTIL = "DEGRADED_UNTIL"
    FORCED_V1 = "FORCED_V1"
    HALF_OPEN_PROBE = "HALF_OPEN_PROBE"


@dataclass(frozen=True)
class InsertCapabilitySnapshot:
    state: InsertCapabilityState
    reason: str | None
    retry_deadline: float | None


@dataclass(frozen=True)
class InsertSelection:
    transport: Literal["llm", "v1"]
    _generation: int
    _owner: object
    _probe_token: object | None = None


def _configured_v1_only() -> bool:
    # Deliberately lazy: client construction and snapshot do not load settings.
    from ..config import get_server_config
    return get_server_config().v1_only


class InsertCapabilityController:
    """Atomic task-local transitions for the existing client's account scope."""
    def __init__(
        self, account_scope_id: str, *, clock: Callable[[], float] = time.monotonic,
        v1_only: Callable[[], bool] = _configured_v1_only,
    ):
        self.account_scope_id = account_scope_id
        self._clock = clock
        self._v1_only = v1_only
        self._latched = False
        self._owner = object()
        self._generation = 0
        self._state = InsertCapabilityState.UNKNOWN
        self._reason = None
        self._deadline = None
        self._probe_token = None
        self._prior = None

    def snapshot(self) -> InsertCapabilitySnapshot:
        """Local data only; no lazy configuration read or capability probe."""
        return InsertCapabilitySnapshot(self._state, self._reason, self._deadline)

    # @beacon[
    #   id=mci-n03@select-for-insert,
    #   slice_labels=mci-create-transport,
    #   kind=ast,
    #   comment=Selects real LLM work or immediate v1 without waiting; one UNKNOWN or expired-cooldown task owns the probe. Lazily latches v1_only and rejects stale outcome tokens.,
    # ]
    def select_for_insert(self) -> InsertSelection:
        """Claim at most one real probe; busy/degraded/forced use v1 at once."""
        if not self._latched:
            forced = self._v1_only()
            self._latched = True
            if forced:
                self._state = InsertCapabilityState.FORCED_V1
                self._reason = "v1_only"
        state = self._state
        if state == InsertCapabilityState.AVAILABLE:
            return InsertSelection("llm", self._generation, self._owner)
        if state == InsertCapabilityState.UNKNOWN or (
            state == InsertCapabilityState.DEGRADED_UNTIL and self._clock() >= self._deadline
        ):
            self._prior = self.snapshot()
            self._state = InsertCapabilityState.HALF_OPEN_PROBE
            self._generation += 1
            self._probe_token = object()
            return InsertSelection("llm", self._generation, self._owner, self._probe_token)
        return InsertSelection("v1", self._generation, self._owner)

    def _owns_result(self, selection: InsertSelection) -> bool:
        return (
            selection.transport == "llm" and selection._owner is self._owner
            and selection._generation == self._generation
            and (self._state == InsertCapabilityState.AVAILABLE or (
                self._state == InsertCapabilityState.HALF_OPEN_PROBE
                and selection._probe_token is self._probe_token
            ))
        )

    def record_success(self, selection: InsertSelection) -> None:
        """Only a healthy verified commit can make the owned probe available."""
        if not self._owns_result(selection):
            return
        if self._state == InsertCapabilityState.HALF_OPEN_PROBE:
            self._state = InsertCapabilityState.AVAILABLE
            self._reason = "verified_insert"
            self._deadline = None
            self._probe_token = self._prior = None
            self._generation += 1

    def record_capability_failure(self, selection: InsertSelection, reason: str) -> None:
        """Unsupported endpoint/schema evidence defers only retry eligibility."""
        if not self._owns_result(selection):
            return
        self._state = InsertCapabilityState.DEGRADED_UNTIL
        self._reason = reason
        self._deadline = self._clock() + 60.0
        self._probe_token = self._prior = None
        self._generation += 1

    def record_neutral(self, selection: InsertSelection) -> None:
        """Release an owned probe on session/lane/cancel/ambiguity, no cooldown."""
        if not self._owns_result(selection) or self._state != InsertCapabilityState.HALF_OPEN_PROBE:
            return
        prior = self._prior
        self._state, self._reason, self._deadline = prior.state, prior.reason, prior.retry_deadline
        self._probe_token = self._prior = None
        self._generation += 1
