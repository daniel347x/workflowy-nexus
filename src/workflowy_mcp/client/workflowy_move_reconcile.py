"""Move-aware tree reconciliation: loop I/O driver for owned data decisions.

The data generator in carto_reconcile_cpu retains PHASE 0 (maps), PHASE 1
(CREATE), PHASE 2 (MOVE), PHASE 3 (REORDER), PHASE 4 (UPDATE) and PHASE 5
(DELETE), including original_ids_seen / explicitly_preserved_ids guardrails,
physical CREATE-order adoption, import policies and dry-run plans. Genuine
reorder detection remains unchanged. Every callback and synchronous cache
publication remains in the caller's task; only owned data decisions go to the
default executor. Threads are not native-parser fault/GIL isolation.
"""
from typing import Callable, Dict, List, Optional, Union, Any
from pathlib import Path
import asyncio

from ..carto_job_runtime import current_carto_job_context, run_carto_cpu
from ..config import get_runtime_subdir
from .carto_reconcile_cpu import (
    CartoDiagnosticSink, _advance_reconcile_steps, _reconcile_steps,
    _reset_reconcile_debug, ReconcileCreateRun, ReconcileCreateResult,
)

Node = Dict


# @beacon[
#   id=auto-beacon@reconcile_tree-v8e1,
#   role=reconcile_tree,
#   slice_labels=ra-reconcile,ra-logging,mci-lease-seams,ra-carto-jobs,
#   kind=ast,
#   comment=Dispatches plain CREATE run/results and parent conclusion on the owning loop while sequential empty-Context advances perform data decisions; unwind concludes before releasing the grant and genuine reorder detection remains in the generator. Diagnostics retain their original failure handlers.,
# ]
async def reconcile_tree(
    source_json: Union[List[Node], Dict],
    parent_uuid: str,
    list_nodes: Callable[[str], List[Node]],
    create_node: Callable[[ReconcileCreateRun], ReconcileCreateResult],
    update_node: Callable[[str, Dict], None],
    delete_node: Callable[[str], None],
    move_node: Callable[..., None],
    export_nodes: Callable[[str], Dict] = None,
    import_policy: str = 'strict',  # 'strict' | 'rebase' | 'clone'
    dry_run: bool = False,
    max_delete_threshold: Optional[int] = None,
    force: bool = False,
    log_weave_entry: Optional[Callable[[Dict[str, Any]], None]] = None,
    log_debug_msg: Optional[Callable[[str], None]] = None,
    log_to_file_msg: Optional[Callable[[str], None]] = None,
    skip_delete_bulk_export_wait: bool = False,
    debug_log_path: Optional[str] = None,
    allow_pure_reorder: bool = False,
    cancel_callback: Optional[Callable[[], bool]] = None,
    cancel_check_interval: int = 25,
    conclude_create_parent: Callable[..., Any] | None = None,
) -> Optional[Dict[str, Any]]:
    """Return the conservative v1 dry-run plan or apply verified CREATE order.

    Dry-run never selects a transport and may show interior v1 reorder debt
    which an applied healthy insert-after avoids. Its plan shape stays unchanged.

    The caller exclusively owns source_json during this await, as in H (CREATE
    IDs and desired annotations remain observable after return). Arbitrary
    nonjob log callbacks run on-loop, with an awaited data advance between
    events; exceptions are thrown back at the exact original phase location.
    A job installs a paths-only diagnostic_sink in its task-local context.
    """
    context = current_carto_job_context()
    sink = context.diagnostic_sink if context is not None else None
    fallback = None
    if sink is None and not log_to_file_msg:
        path = Path(debug_log_path) if debug_log_path else get_runtime_subdir("logs") / 'reconcile_debug.log'
        fallback = CartoDiagnosticSink(debug_path=path)
        await run_carto_cpu("reconcile:debug-open", _reset_reconcile_debug, path)

    # Creating a generator does not execute its body or walk the source.
    steps = _reconcile_steps(
        source_json, parent_uuid, import_policy, dry_run, max_delete_threshold,
        force, skip_delete_bulk_export_wait, allow_pure_reorder,
        cancel_check_interval, export_nodes=export_nodes is not None,
        cancel_callback=cancel_callback is not None,
        log_weave_entry=(sink.journal_path is not None if sink is not None else log_weave_entry is not None),
    )
    response, error = None, None
    try:
        while True:
            step = await run_carto_cpu(
                "reconcile:advance", _advance_reconcile_steps, steps, response, error,
            )
            response, error = None, None
            if step.done:
                return step.result
            try:
                if step.diagnostics:
                    # Each event resumes the generator only after its ordered
                    # sink/callback completes, retaining H exception semantics.
                    if sink is not None:
                        failure = await run_carto_cpu(
                            "reconcile:diagnostics", sink.append_batch, step.diagnostics,
                        )
                        # The FILE log callback swallowed debug I/O failures;
                        # journal failures still re-enter H's journal handler.
                        if failure is not None and step.diagnostics[0][0] != "log":
                            raise failure
                    else:
                        for kind, value, timestamp in step.diagnostics:
                            if kind == "journal":
                                log_weave_entry(value)
                            else:
                                if log_to_file_msg:
                                    log_to_file_msg(value)
                                elif fallback is not None:
                                    failure = await run_carto_cpu(
                                        "reconcile:diagnostics", fallback.append_batch,
                                        ((kind, value, timestamp),),
                                    )
                                    if failure is not None:
                                        raise failure
                                if log_debug_msg:
                                    log_debug_msg(value)
                else:
                    request = step.next_request
                    if request.kind == "list":
                        response = await list_nodes(*request.args, **request.kwargs)
                    elif request.kind == "export":
                        response = await export_nodes(*request.args, **request.kwargs)
                    elif request.kind == "create":
                        response = await create_node(*request.args, **request.kwargs)
                    elif request.kind == "create_parent_done":
                        response = (await conclude_create_parent(*request.args, **request.kwargs)
                                    if conclude_create_parent is not None else None)
                    elif request.kind == "move":
                        response = await move_node(*request.args, **request.kwargs)
                    elif request.kind == "update":
                        response = await update_node(*request.args, **request.kwargs)
                    elif request.kind == "delete":
                        response = await delete_node(*request.args, **request.kwargs)
                    elif request.kind == "sleep":
                        response = await asyncio.sleep(*request.args, **request.kwargs)
                    elif request.kind == "cancel_check":
                        response = cancel_callback()
                    else:
                        raise RuntimeError(f"Unknown internal reconcile request: {request.kind}")
            except BaseException as exc:
                # Even callback and diagnostics failures re-enter the original
                # generator try/except; no phase's logging or policy is replaced.
                if not isinstance(exc, Exception) and not getattr(exc, "reconcile_create_result", None):
                    raise
                error = exc
    finally:
        # No detached generator/future survives the caller's grant. Closing is
        # data-only too and the exact executor future is joined by the wrapper.
        try:
            if conclude_create_parent is not None:
                await conclude_create_parent(None)
        finally:
            await run_carto_cpu("reconcile:close", steps.close)


__all__ = ['reconcile_tree']
