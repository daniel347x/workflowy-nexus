"""One account-scoped in-process Workflowy write scheduler with persisted WFB
breaker and Retry-After deferral. All sends enter through the Phase-B client
authority. Ordinary ticket reservations are memory-only; retained state uses
the existing atomic JSON writer.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .carto_job_governance import (
    _check_cancel_async,
    atomic_write_json,
    derive_account_scope_id,
    get_jobs_root,
)
from .models.errors import (
    AccountWriteBlockedError,
    WriteGovernorDeferredError,
    WriteGovernorUnavailableError,
    WriteSlotBusyTimeoutError,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_WRITE_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_WAIT_BEFORE_REFUSAL_SECONDS = 3600.0
DEFAULT_BREAKER_INITIAL_COOLDOWN_SECONDS = 900.0
DEFAULT_BREAKER_MAX_COOLDOWN_SECONDS = 7200.0
DEFAULT_PROBE_LEASE_SECONDS = 120.0
DEFAULT_RETRY_AFTER_FALLBACK_SECONDS = 60.0
DEFAULT_MUTEX_WAIT_SECONDS = 10.0
DEFAULT_MUTEX_POLL_SECONDS = 0.05

_ACCOUNT_SCOPE_RE = re.compile(r"^[0-9a-f]{32}$")
_BREAKER_STATES = frozenset({"closed", "open", "half_open"})
_PROBE_OUTCOMES = frozenset(
    {"success", "rate_limited", "client_error", "server_error", "timeout", "network_error"}
)


@dataclass(frozen=True)
class WriteGovernorSettings:
    """Stable scheduler and breaker settings; the caller window is lengthened
    without changing pacing or backoff defaults.
    """

    interval_seconds: float = DEFAULT_WRITE_INTERVAL_SECONDS
    max_wait_before_refusal_seconds: float = DEFAULT_MAX_WAIT_BEFORE_REFUSAL_SECONDS
    breaker_initial_cooldown_seconds: float = DEFAULT_BREAKER_INITIAL_COOLDOWN_SECONDS
    breaker_max_cooldown_seconds: float = DEFAULT_BREAKER_MAX_COOLDOWN_SECONDS
    probe_lease_seconds: float = DEFAULT_PROBE_LEASE_SECONDS
    retry_after_fallback_seconds: float = DEFAULT_RETRY_AFTER_FALLBACK_SECONDS
    mutex_wait_seconds: float = DEFAULT_MUTEX_WAIT_SECONDS
    mutex_poll_seconds: float = DEFAULT_MUTEX_POLL_SECONDS

    def __post_init__(self) -> None:
        positive = (
            "interval_seconds",
            "breaker_initial_cooldown_seconds",
            "breaker_max_cooldown_seconds",
            "probe_lease_seconds",
            "mutex_wait_seconds",
        )
        nonnegative = (
            "max_wait_before_refusal_seconds",
            "retry_after_fallback_seconds",
            "mutex_poll_seconds",
        )
        for name in positive:
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and > 0")
        for name in nonnegative:
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and >= 0")
        if self.breaker_max_cooldown_seconds < self.breaker_initial_cooldown_seconds:
            raise ValueError(
                "breaker_max_cooldown_seconds must be >= breaker_initial_cooldown_seconds"
            )


@dataclass(frozen=True)
class WriteGovernancePaths:
    """Account-scoped write-governance paths beneath one authoritative jobs root."""

    jobs_root: Path
    account_scope_id: str
    account_dir: Path
    governor_state: Path
    breaker_state: Path


@dataclass(frozen=True)
class WriteSlotTicket:
    """One anonymous pacing reservation and optional half-open probe claim."""

    slot_at: float
    issued_at: float
    operation: str
    probe_claim_id: str | None = None

    @property
    def is_probe(self) -> bool:
        return self.probe_claim_id is not None


@dataclass(frozen=True)
class BreakerTransitionResult:
    """Token-aware breaker transition result for Phase-B integration."""

    applied: bool
    breaker: dict[str, Any]
    next_slot_at: float | None = None


def account_write_governance_paths(
    account_scope_id: str,
    jobs_root: str | os.PathLike[str] | None = None,
) -> WriteGovernancePaths:
    """Derive every A2 path from A1's exact jobs root and account scope."""

    scope = str(account_scope_id)
    if not _ACCOUNT_SCOPE_RE.fullmatch(scope):
        raise ValueError("account_scope_id must be 32 lowercase hexadecimal characters")
    root = get_jobs_root(jobs_root)
    account_dir = root / ".governance" / scope
    return WriteGovernancePaths(
        jobs_root=root,
        account_scope_id=scope,
        account_dir=account_dir,
        governor_state=account_dir / "write_governor.json",
        breaker_state=account_dir / "write_breaker.json",
    )


def _nonnegative_finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return normalized


def _validate_governor_state(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {"next_slot_at"}:
        raise ValueError("governor state must contain only next_slot_at")
    return {"next_slot_at": _nonnegative_finite(value["next_slot_at"], "next_slot_at")}


def _validate_breaker_state(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "state",
        "blocked_until",
        "cooldown_s",
        "probe_claim_id",
        "probe_lease_expires_at",
        "reason",
        "evidence_excerpt",
    }
    if set(value) != required:
        raise ValueError(f"breaker state fields must be exactly {sorted(required)}")
    state = str(value["state"])
    if state not in _BREAKER_STATES:
        raise ValueError(f"invalid breaker state: {state!r}")
    probe_claim_id = value["probe_claim_id"]
    if probe_claim_id is not None and (
        not isinstance(probe_claim_id, str) or not re.fullmatch(r"[0-9a-f]{32}", probe_claim_id)
    ):
        raise ValueError("probe_claim_id must be null or uuid4 hex")
    if not isinstance(value["reason"], str) or not isinstance(value["evidence_excerpt"], str):
        raise ValueError("breaker reason/evidence_excerpt must be strings")
    return {
        "state": state,
        "blocked_until": _nonnegative_finite(value["blocked_until"], "blocked_until"),
        "cooldown_s": _nonnegative_finite(value["cooldown_s"], "cooldown_s"),
        "probe_claim_id": probe_claim_id,
        "probe_lease_expires_at": _nonnegative_finite(
            value["probe_lease_expires_at"], "probe_lease_expires_at"
        ),
        "reason": value["reason"],
        "evidence_excerpt": value["evidence_excerpt"],
    }


# @beacon[
#   id=carto-governance@inprocess-write-schedule,
#   slice_labels=carto-governance,mci-lease-governor,ra-carto-jobs,
#   kind=ast,
#   comment=One shared memory ticket cursor per account/root; startup reads the retained Retry-After floor, while breaker state remains durable. No OS lane, per-ticket file writes or background maintenance.,
# ]
@dataclass
class _WriteSchedule:
    """Process-lifetime pacing cursor and conservative failed-write fallback."""

    lock: asyncio.Lock
    initialized: bool = False
    next_slot_at: float = 0.0
    deferral_until: float = 0.0
    breaker_fallback: dict[str, Any] | None = None


_WRITE_SCHEDULES: dict[tuple[str, str], _WriteSchedule] = {}


class CartoWriteGovernor:
    """Account-scoped in-process write scheduler and persisted WFB circuit breaker."""

    def __init__(
        self,
        *,
        account_scope_id: str | None = None,
        api_base_url: str | None = None,
        api_key: str | None = None,
        jobs_root: str | os.PathLike[str] | None = None,
        settings: WriteGovernorSettings | None = None,
        clock: Callable[[], float] = time.time,
        atomic_writer: Callable[
            [str | os.PathLike[str], Mapping[str, Any]], Any
        ] = atomic_write_json,
    ):
        if account_scope_id is None:
            if api_base_url is None or api_key is None:
                raise ValueError("provide account_scope_id or both api_base_url and api_key")
            account_scope_id = derive_account_scope_id(api_base_url, api_key)
        self.paths = account_write_governance_paths(account_scope_id, jobs_root)
        self.settings = settings or WriteGovernorSettings()
        self._clock = clock
        self._atomic_writer = atomic_writer
        key = (
            os.path.normcase(os.path.abspath(os.fspath(self.paths.jobs_root))),
            self.paths.account_scope_id,
        )
        self._schedule = _WRITE_SCHEDULES.setdefault(key, _WriteSchedule(asyncio.Lock()))

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now) or now < 0:
            raise WriteGovernorUnavailableError(
                str(self.paths.account_dir), "governor clock returned an invalid epoch"
            )
        return now

    def now_epoch(self) -> float:
        """Expose the governor's persisted-state clock to its client authority."""

        return self._now()

    def _default_governor(self) -> dict[str, Any]:
        return {"next_slot_at": 0.0}

    def _default_breaker(self) -> dict[str, Any]:
        return {
            "state": "closed",
            "blocked_until": 0.0,
            "cooldown_s": float(self.settings.breaker_initial_cooldown_seconds),
            "probe_claim_id": None,
            "probe_lease_expires_at": 0.0,
            "reason": "",
            "evidence_excerpt": "",
        }

    def _write_state(self, path: Path, payload: Mapping[str, Any]) -> None:
        """Persist retained state; a failed OPEN write remains conservative locally."""
        try:
            self._atomic_writer(path, payload)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if path == self.paths.breaker_state and payload.get("state") == "open":
                self._schedule.breaker_fallback = dict(payload)
            raise WriteGovernorUnavailableError(str(path), str(exc)) from exc
        if path == self.paths.breaker_state:
            self._schedule.breaker_fallback = None

    def _load_state(
        self,
        path: Path,
        *,
        default: dict[str, Any],
        validator: Callable[[Mapping[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        """Read retained state without seeding missing files or moving corrupt ones."""
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            if not isinstance(value, dict):
                raise ValueError("state JSON must contain an object")
            return validator(value)
        except FileNotFoundError:
            return dict(default)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
            LOGGER.error("Unreadable write-governance state at %s", path)
            if path != self.paths.breaker_state:
                return dict(default)
            conservative = dict(default)
            conservative.update(
                state="open",
                blocked_until=self._now() + self.settings.breaker_initial_cooldown_seconds,
                cooldown_s=float(self.settings.breaker_initial_cooldown_seconds),
                probe_claim_id=None,
                probe_lease_expires_at=0.0,
                reason="Unreadable breaker state; recovery probe follows the existing cooldown.",
                evidence_excerpt="",
            )
            self._schedule.breaker_fallback = dict(conservative)
            try:
                self._write_state(path, conservative)
            except WriteGovernorUnavailableError:
                LOGGER.error("Could not persist conservative breaker recovery at %s", path)
            return conservative
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise WriteGovernorUnavailableError(str(path), str(exc)) from exc

    def _load_governor(self) -> dict[str, Any]:
        """Read the legacy/durable floor once during shared-schedule initialization."""
        return self._load_state(
            self.paths.governor_state,
            default=self._default_governor(),
            validator=_validate_governor_state,
        )

    def _load_breaker(self) -> dict[str, Any]:
        """Fresh durable breaker state, unless a fixed failed-OPEN fallback applies."""
        if self._schedule.breaker_fallback is not None:
            return dict(self._schedule.breaker_fallback)
        return self._load_state(
            self.paths.breaker_state,
            default=self._default_breaker(),
            validator=_validate_breaker_state,
        )

    @asynccontextmanager
    async def _write_slot_lock(self, *, cancel_check: Callable[[], Any] | None = None):
        """The one cancel-aware short transaction, with lazy read-once floor load."""
        lock = self._schedule.lock
        deadline = time.monotonic() + self.settings.mutex_wait_seconds
        while True:
            await _check_cancel_async(cancel_check, self.paths.account_dir)
            if not lock.locked():
                await lock.acquire()
                break
            if time.monotonic() >= deadline:
                raise WriteSlotBusyTimeoutError(
                    str(self.paths.account_dir), self.settings.mutex_wait_seconds
                )
            await _check_cancel_async(cancel_check, self.paths.account_dir)
            await asyncio.sleep(max(0.0, min(
                self.settings.mutex_poll_seconds, deadline - time.monotonic()
            )))
        try:
            if not self._schedule.initialized:
                floor = self._load_governor()["next_slot_at"]
                self._schedule.deferral_until = floor
                self._schedule.next_slot_at = floor
                self._schedule.initialized = True
            yield
        finally:
            lock.release()

    def _blocked_error(
        self,
        breaker: Mapping[str, Any],
        *,
        now: float,
        recheck: bool = False,
    ) -> AccountWriteBlockedError:
        if breaker["state"] == "half_open" or (
            breaker.get("probe_claim_id") is not None
            and float(breaker.get("probe_lease_expires_at", 0.0)) > now
        ):
            deadline = float(breaker.get("probe_lease_expires_at", 0.0))
            reason = str(breaker.get("reason") or "another recovery probe owns the account")
        else:
            deadline = float(breaker.get("blocked_until", 0.0))
            reason = str(breaker.get("reason") or "account write breaker is open")
        retry_after = max(0.0, deadline - now)
        if recheck and retry_after == 0:
            reason = f"{reason}; retry the operation as a fresh acquire"
        return AccountWriteBlockedError(
            retry_after=retry_after,
            blocked_until=deadline,
            reason=reason,
        )

    # @beacon[
    #   id=carto-governance@write-ticket,
    #   slice_labels=ra-carto-jobs,nexus-core-v1,mci-lease-governor,
    #   kind=ast,
    #   comment=One shared in-process ticket stream with breaker/probe checks, no-advance refusal, and cancel-aware sleep after the short lock; ticket reservations never write disk.,
    # ]
    async def acquire_write_slot(
        self,
        operation: str,
        *,
        sleep_fn: Callable[[float, str], Awaitable[Any] | Any],
        cancel_check: Callable[[], Any] | None = None,
    ) -> WriteSlotTicket:
        """Issue one paced ticket, then sleep toward it outside the mutex."""

        async with self._write_slot_lock(cancel_check=cancel_check):
            breaker = self._load_breaker()
            now = self._now()

            if breaker["state"] == "open" and breaker["blocked_until"] > now:
                raise self._blocked_error(breaker, now=now)

            live_probe = (
                breaker["probe_claim_id"] is not None and breaker["probe_lease_expires_at"] > now
            )
            if live_probe:
                raise self._blocked_error(breaker, now=now)

            probe_claim_id: str | None = None
            proposed_breaker: dict[str, Any] | None = None
            if breaker["state"] in {"open", "half_open"}:
                probe_claim_id = uuid4().hex
                proposed_breaker = dict(breaker)
                proposed_breaker.update(
                    {
                        "state": "half_open",
                        "probe_claim_id": probe_claim_id,
                        "probe_lease_expires_at": now + self.settings.probe_lease_seconds,
                    }
                )

            my_slot = max(now, self._schedule.next_slot_at, self._schedule.deferral_until)
            slot_wait = max(0.0, my_slot - now)
            if slot_wait > self.settings.max_wait_before_refusal_seconds:
                raise WriteGovernorDeferredError(
                    retry_after=slot_wait,
                    deferred_until=my_slot,
                    reason="global write slot is beyond the bounded caller-wait window",
                )

            if proposed_breaker is not None:
                self._write_state(self.paths.breaker_state, proposed_breaker)
            self._schedule.next_slot_at = my_slot + self.settings.interval_seconds
            ticket = WriteSlotTicket(
                slot_at=my_slot,
                issued_at=now,
                operation=str(operation),
                probe_claim_id=probe_claim_id,
            )

        delay = max(0.0, ticket.slot_at - self._now())
        if delay > 0:
            result = sleep_fn(delay, str(operation))
            if inspect.isawaitable(result):
                await result
        return ticket

    # @beacon[
    #   id=auto-beacon@CartoWriteGovernor.recheck_before_send-xrca,
    #   role=CartoWriteGovernor.recheck_before_send,
    #   slice_labels=mci-lease-governor,
    #   kind=ast,
    # ]
    async def recheck_before_send(
        self,
        ticket: WriteSlotTicket,
        *,
        cancel_check: Callable[[], Any] | None = None,
    ) -> None:
        """Suppress pre-trip reservations while allowing the live probe holder."""

        async with self._write_slot_lock(cancel_check=cancel_check):
            breaker = self._load_breaker()
            now = self._now()
            if breaker["state"] == "closed":
                return
            if (
                breaker["state"] == "half_open"
                and ticket.probe_claim_id is not None
                and breaker["probe_claim_id"] == ticket.probe_claim_id
            ):
                return
            raise self._blocked_error(breaker, now=now, recheck=True)

    # @beacon-close[
    #   id=carto-governance@write-ticket,
    # ]

    async def defer_global_writes_until(
        self,
        delay_seconds: float,
        *,
        operation: str = "Workflowy write",
        cancel_check: Callable[[], Any] | None = None,
    ) -> float:
        """Max-merge a positive Retry-After into the retained durable floor and
        memory schedule; zero is a no-write no-op.
        """

        delay = _nonnegative_finite(delay_seconds, "delay_seconds")
        async with self._write_slot_lock(cancel_check=cancel_check):
            if delay == 0:
                return self._schedule.next_slot_at
            now = self._now()
            new_deferral = max(self._schedule.deferral_until, now + delay)
            merged = max(self._schedule.next_slot_at, new_deferral)
            self._schedule.next_slot_at = merged
            if new_deferral > self._schedule.deferral_until:
                self._write_state(self.paths.governor_state, {"next_slot_at": new_deferral})
                self._schedule.deferral_until = new_deferral
            LOGGER.warning(
                "Deferred account writes: delay=%ss next_slot_at=%s scope=%s operation=%s",
                delay,
                merged,
                self.paths.account_scope_id[:8],
                operation,
            )
            return merged

    # @beacon[
    #   id=carto-governance@breaker-transitions,
    #   slice_labels=ra-carto-jobs,nexus-core-v1,mci-lease-governor,
    #   kind=ast,
    #   comment=Token-safe persisted breaker transitions; a matching 429 durably max-merges changed Retry-After before closing the breaker, with no per-ticket file lane.,
    # ]
    async def trip_wfb(
        self,
        *,
        probe_claim_id: str | None = None,
        reason: str = "Workflowy account write block WFB(3)",
        evidence_excerpt: str = "",
        cancel_check: Callable[[], Any] | None = None,
    ) -> BreakerTransitionResult:
        """Trip/re-trip WFB without shortening or repeatedly compounding a generation."""

        evidence = str(evidence_excerpt)[:1000]
        async with self._write_slot_lock(cancel_check=cancel_check):
            breaker = self._load_breaker()
            now = self._now()
            state = breaker["state"]

            if state == "closed":
                cooldown = self.settings.breaker_initial_cooldown_seconds
                blocked_until = now + cooldown
            elif state == "open":
                cooldown = min(
                    max(
                        breaker["cooldown_s"],
                        self.settings.breaker_initial_cooldown_seconds,
                    ),
                    self.settings.breaker_max_cooldown_seconds,
                )
                blocked_until = max(breaker["blocked_until"], now + cooldown)
            elif probe_claim_id is not None and probe_claim_id == breaker["probe_claim_id"]:
                cooldown = min(
                    max(
                        breaker["cooldown_s"],
                        self.settings.breaker_initial_cooldown_seconds,
                    )
                    * 2.0,
                    self.settings.breaker_max_cooldown_seconds,
                )
                blocked_until = max(breaker["blocked_until"], now + cooldown)
            else:
                cooldown = min(
                    max(
                        breaker["cooldown_s"],
                        self.settings.breaker_initial_cooldown_seconds,
                    ),
                    self.settings.breaker_max_cooldown_seconds,
                )
                blocked_until = max(breaker["blocked_until"], now + cooldown)

            updated = {
                "state": "open",
                "blocked_until": blocked_until,
                "cooldown_s": cooldown,
                "probe_claim_id": None,
                "probe_lease_expires_at": 0.0,
                "reason": str(reason),
                "evidence_excerpt": evidence,
            }
            self._write_state(self.paths.breaker_state, updated)
            LOGGER.error(
                "Workflowy WFB breaker open until %s scope=%s reason=%s",
                blocked_until,
                self.paths.account_scope_id[:8],
                reason,
            )
            return BreakerTransitionResult(True, updated)

    def _probe_matches(self, breaker: Mapping[str, Any], probe_claim_id: str) -> bool:
        return (
            breaker["state"] == "half_open"
            and bool(probe_claim_id)
            and breaker["probe_claim_id"] == probe_claim_id
        )

    def _closed_breaker(self, *, reason: str) -> dict[str, Any]:
        return {
            "state": "closed",
            "blocked_until": 0.0,
            "cooldown_s": float(self.settings.breaker_initial_cooldown_seconds),
            "probe_claim_id": None,
            "probe_lease_expires_at": 0.0,
            "reason": str(reason),
            "evidence_excerpt": "",
        }

    def _reopened_probe_breaker(
        self,
        breaker: Mapping[str, Any],
        *,
        now: float,
        reason: str,
        evidence_excerpt: str,
    ) -> dict[str, Any]:
        cooldown = min(
            max(
                float(breaker["cooldown_s"]),
                self.settings.breaker_initial_cooldown_seconds,
            ),
            self.settings.breaker_max_cooldown_seconds,
        )
        return {
            "state": "open",
            "blocked_until": now + cooldown,
            "cooldown_s": cooldown,
            "probe_claim_id": None,
            "probe_lease_expires_at": 0.0,
            "reason": str(reason),
            "evidence_excerpt": str(evidence_excerpt)[:1000],
        }

    async def record_probe_result(
        self,
        probe_claim_id: str,
        outcome: Literal[
            "success",
            "rate_limited",
            "client_error",
            "server_error",
            "timeout",
            "network_error",
        ],
        *,
        retry_after_seconds: float | None = None,
        reason: str = "",
        evidence_excerpt: str = "",
        cancel_check: Callable[[], Any] | None = None,
    ) -> BreakerTransitionResult:
        """Apply one exhaustive probe outcome iff its token still owns the generation."""

        if outcome not in _PROBE_OUTCOMES:
            raise ValueError(f"unsupported probe outcome: {outcome!r}")
        parsed_delay: float | None = None
        if outcome == "rate_limited":
            if retry_after_seconds is None:
                raise ValueError("rate_limited probe result requires retry_after_seconds")
            parsed_delay = _nonnegative_finite(retry_after_seconds, "retry_after_seconds")

        async with self._write_slot_lock(cancel_check=cancel_check):
            breaker = self._load_breaker()
            if not self._probe_matches(breaker, str(probe_claim_id)):
                return BreakerTransitionResult(False, breaker)

            now = self._now()
            if outcome in {"success", "client_error", "rate_limited"}:
                updated = self._closed_breaker(
                    reason=reason or f"matching recovery probe returned {outcome}"
                )
                next_slot_at: float | None = None
                if outcome == "rate_limited":
                    assert parsed_delay is not None
                    if parsed_delay > 0:
                        new_deferral = max(self._schedule.deferral_until, now + parsed_delay)
                        self._schedule.next_slot_at = max(self._schedule.next_slot_at, new_deferral)
                        if new_deferral > self._schedule.deferral_until:
                            self._write_state(self.paths.governor_state, {"next_slot_at": new_deferral})
                            self._schedule.deferral_until = new_deferral
                    next_slot_at = self._schedule.next_slot_at
                    # A two-file atomic commit is unavailable. Publish the
                    # account-wide defer first, then close the breaker. Every
                    # failure boundary therefore remains conservative: either
                    # the breaker still blocks, or the required defer is already
                    # durable before observers can see breaker=closed.
                    self._write_state(self.paths.breaker_state, updated)
                    return BreakerTransitionResult(True, updated, next_slot_at)
                self._write_state(self.paths.breaker_state, updated)
                return BreakerTransitionResult(True, updated)

            updated = self._reopened_probe_breaker(
                breaker,
                now=now,
                reason=reason or f"matching recovery probe returned {outcome}",
                evidence_excerpt=evidence_excerpt,
            )
            self._write_state(self.paths.breaker_state, updated)
            return BreakerTransitionResult(True, updated)

    # @beacon-close[
    #   id=carto-governance@breaker-transitions,
    # ]

    async def snapshot(
        self, *, cancel_check: Callable[[], Any] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Report the memory ticket cursor and current breaker state under the
        in-process scheduler lock.
        """

        async with self._write_slot_lock(cancel_check=cancel_check):
            return {
                "governor": {"next_slot_at": self._schedule.next_slot_at},
                "breaker": self._load_breaker(),
            }


__all__ = [
    "BreakerTransitionResult",
    "CartoWriteGovernor",
    "DEFAULT_BREAKER_INITIAL_COOLDOWN_SECONDS",
    "DEFAULT_BREAKER_MAX_COOLDOWN_SECONDS",
    "DEFAULT_MAX_WAIT_BEFORE_REFUSAL_SECONDS",
    "DEFAULT_MUTEX_WAIT_SECONDS",
    "DEFAULT_PROBE_LEASE_SECONDS",
    "DEFAULT_RETRY_AFTER_FALLBACK_SECONDS",
    "DEFAULT_WRITE_INTERVAL_SECONDS",
    "WriteGovernancePaths",
    "WriteGovernorSettings",
    "WriteSlotTicket",
    "account_write_governance_paths",
]
