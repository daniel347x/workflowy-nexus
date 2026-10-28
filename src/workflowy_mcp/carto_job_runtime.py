"""Data and task-local state for daemon-owned Cartographer jobs (N07).

This module creates no client, publisher, registry, process or executor pool.
Only data work enters the daemon's default thread executor. Threads are NOT
native-parser crash isolation: a segfault can terminate the daemon, and a
native parser holding the GIL can delay its other Python work.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar


def _freeze_job_data(value: Any) -> Any:
    """Detach execution payload containers; never apply this to a cache view."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_job_data(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_job_data(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_job_data(item) for item in value)
    return value


def _thaw_job_data(value: Any) -> Any:
    """Make exclusively owned working containers from frozen job intent off-loop."""
    if isinstance(value, Mapping):
        return {key: _thaw_job_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_job_data(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return {_thaw_job_data(item) for item in value}
    return value


@dataclass(frozen=True)
class _FrozenJobPayload:
    """Trusted result of the data-only freeze, not a cache or callback bundle."""

    value: Any


def _freeze_job_payload(value: Any) -> _FrozenJobPayload:
    """Freeze proportional intent off-loop before installing loop-only callbacks."""
    return _FrozenJobPayload(_freeze_job_data(value))


@dataclass(frozen=True)
class CartoJobSpec:
    """Frozen execution intent, not a second client/cache or ownership record.

    ``selection_metadata`` contains only lineage/freshness diagnostics, not a
    deserialized snapshot. ``selection_nodes_by_id`` explicitly borrows the
    selected immutable mapping for data work; it is never copied or installed
    as an execution cache. Preparation/freshness callbacks run on the loop.
    """

    job_id: str
    job_file: Path
    log_file: Path | None
    job_type: str
    mode: str
    root_uuid: str
    claim_id: str
    account_scope_id: str
    claim_path: str | None = None
    source_regime: str = "standard"
    source_path_override: str | None = None
    delete_temp_file_after: bool = False
    force: bool = False
    selection_metadata: Mapping[str, Any] = field(default_factory=dict)
    selection_nodes_by_id: Mapping[str, Any] | None = None
    preparation_payload: Mapping[str, Any] = field(default_factory=dict)
    preparation: Callable[..., Any] | None = None
    preparation_gate: Callable[..., Mapping[str, Any]] | None = None
    freshness_callback: Callable[..., Any] | None = None
    visible_tree_payload: Mapping[str, Any] | None = None
    # Frozen membership from the existing off-loop bulk collector. Containment
    # is deliberately NOT stored here: the region factory re-proves it against
    # each current admission snapshot, never the GUI's parent relationships.
    bulk_groups: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_file", Path(self.job_file))
        if self.log_file is not None:
            object.__setattr__(self, "log_file", Path(self.log_file))
        for name in ("selection_metadata", "preparation_payload", "visible_tree_payload", "bulk_groups"):
            value = getattr(self, name)
            object.__setattr__(self, name, value.value if isinstance(value, _FrozenJobPayload) else _freeze_job_data(value))


@dataclass(eq=False)
class CartoFinalObligation:
    """Loop-owned lifetime responsibility; never restored from job/marker files.

    A terminal proof may carry an immutable status snapshot for presentation
    after the actual control unregisters, including failed durable status I/O.
    Losing this table on daemon exit loses refresh responsibility, not upstream
    writes: a later ordinary full refresh heals the cache; no job is resumed.
    """

    job_id: str
    path: str
    incarnation: str
    account_scope_id: str
    relevant: bool = False
    state: str = "observing"
    terminal_proof: Mapping[str, Any] | None = None
    attempt_token: str | None = None
    receipt: Mapping[str, Any] | None = None
    skip_reason: str | None = None
    diagnostic: str | None = None

    def snapshot(self):
        result = {"schema_version": 2, "job_id": self.job_id, "path": self.path,
            "incarnation": self.incarnation, "account_scope_id": self.account_scope_id,
            "relevant": self.relevant, "state": self.state, "terminal_proof": self.terminal_proof}
        for key in ("attempt_token", "receipt", "skip_reason", "diagnostic"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return MappingProxyType(result)


@dataclass(eq=False)
class CartoJobControl:
    """Loop-owned requests; only the owning task observes a safe checkpoint.

    N06 owns the restart request mapping; the scheduler shares that exact
    reference. Reading the user probe never observes a restart request and
    never interrupts a governed send or its synchronous cache patch.
    """

    user_requested: bool = False
    user_observed: bool = False
    restart_state: dict[str, Any] = field(default_factory=lambda: {
        "restart_id": None, "observed": False,
    })
    phase: str = "queued"
    may_have_workflowy_effects: bool = False
    cpu_future: asyncio.Future[Any] | None = None
    cpu_failure: tuple[str, BaseException] | None = None
    task: asyncio.Task[Any] | None = None
    finalizing: bool = False
    terminal_result: dict[str, Any] | None = None
    scheduling_failure: BaseException | None = None
    # Fixed-size loop presentation, not ownership or a second cache. The runner
    # shares its own scalar progress mapping here; scans receive value copies.
    presentation: Mapping[str, Any] = field(default_factory=dict)
    presentation_status: str = "preparing"
    progress: dict[str, Any] = field(default_factory=dict)

    def request_user_cancel(self) -> None:
        self.user_requested = True

    def user_cancel_requested(self) -> bool:
        return self.user_requested

    def checkpoint(self, phase: str, *, restart_boundary: bool = False) -> None:
        """Observe requests at the caller's safe point, never in another task."""
        if asyncio.current_task() is not self.task:
            raise RuntimeError("Cartographer checkpoint must run in its owning task")
        self.phase = phase
        if self.user_requested:
            self.user_observed = True
            raise asyncio.CancelledError("Cartographer user cancellation observed")
        if restart_boundary and (
            self.restart_state["restart_id"] is not None or self.restart_state["observed"]
        ):
            self.restart_state["observed"] = True
            raise asyncio.CancelledError("Cartographer daemon restart boundary observed")


@dataclass(eq=False)
class CartoJobContext:
    """One task's paths, job_control and diagnostics; never read by a CPU worker."""

    job_file: Path
    job_dir: Path
    log_file: Path | None
    job_control: CartoJobControl
    claim_id: str = ""
    progress: dict[str, Any] = field(default_factory=dict)
    warnings: list[Any] = field(default_factory=list)
    errors: list[Any] = field(default_factory=list)
    relpath_memo: dict[str, str | None] = field(default_factory=dict)
    # Installed by the file phase: an immutable paths-only CartoDiagnosticSink,
    # never an arbitrary callback or a live cache/client reference.
    diagnostic_sink: Any = None


_CARTO_JOB_CONTEXT: contextvars.ContextVar[CartoJobContext | None] = contextvars.ContextVar(
    "carto_job_context", default=None,
)


def current_carto_job_context() -> CartoJobContext | None:
    return _CARTO_JOB_CONTEXT.get()


@contextmanager
def carto_job_scope(context: CartoJobContext) -> Iterator[CartoJobContext]:
    token = _CARTO_JOB_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CARTO_JOB_CONTEXT.reset(token)


_Result = TypeVar("_Result")


# @beacon[
#   id=carto-runtime@run-carto-cpu,
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=Runs data-only CPU or job-local disk work on the default thread executor in an empty Context; loop-only job_control retains the exact joined future and failure latch; user stops occur at safe returns but never interrupt the exit door finalization; threads provide no native-crash isolation.,
# ]
async def run_carto_cpu(
    phase: str, function: Callable[..., _Result], *data_args: Any, **data_kwargs: Any,
) -> _Result:
    """Offload real data work, retaining its exact future until it has joined.

    Callers pass plain/owned data or immutable view references, never a live
    client, publisher, registry, HTTP callback or mutable task table. The fresh
    empty worker Context cannot borrow an operation, grant or job control.
    The context's job_control is loop-owned data, never an HTTP control callable.
    No task, pool, deadline, polling policy or background writer is created.
    A cooperative request does not stop a thread: it is observed only after
    that thread has finished. Restart stops remain the caller's group/phase
    boundary, not this user-cancellation return boundary.
    """
    context = current_carto_job_context()
    control = context.job_control if context is not None else None
    if control is not None:
        if asyncio.current_task() is not control.task:
            raise RuntimeError("Cartographer CPU work must be awaited by its owning task")
        if control.cpu_future is not None:
            raise RuntimeError("Cartographer data futures must be joined sequentially")
        control.phase = phase

    worker_context = contextvars.Context()
    future = asyncio.get_running_loop().run_in_executor(
        None, worker_context.run, partial(function, *data_args, **data_kwargs),
    )
    if control is not None:
        control.cpu_future = future
    cancelled: asyncio.CancelledError | None = None
    try:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError as exc:
                # Keep joining THIS future even if a waiter is cancelled more
                # than once. Shield only the data wait, never an async send.
                cancelled = exc
            except BaseException:
                # Retrieve and latch the exact worker outcome below, including
                # a job-origin SystemExit/KeyboardInterrupt, on the loop.
                break
        try:
            result = future.result()
        except BaseException as exc:
            if control is not None and not isinstance(exc, asyncio.CancelledError):
                if control.cpu_failure is None:
                    control.cpu_failure = (phase, exc)
            raise
    finally:
        if control is not None and control.cpu_future is future:
            control.cpu_future = None
    if cancelled is not None:
        raise cancelled
    if control is not None and not control.finalizing:
        control.checkpoint(phase)
    return result


__all__ = [
    "CartoJobSpec", "CartoJobControl", "CartoJobContext", "carto_job_scope",
    "current_carto_job_context", "run_carto_cpu",
]
