"""CUT-2B §9.7.5 — in-process admission lock and registry-offload fences.

The four tests keep their subjects: an async Q3 acquisition waits on the real
in-process ``admission_lock`` without blocking the event loop, cancellation has
no late transaction, no await/HTTP/executor work lives inside an admission span
(recursively through the locked callees), and no async function calls a retired
synchronous admission wrapper or sends a registry adapter through an executor.
"""

from __future__ import annotations

import ast
import asyncio
import os
import time
from pathlib import Path

import pytest

from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp.mutation_locks import _governance_holds

ACCOUNT = "a" * 32
REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "workflowy_mcp"

LOCKED_TRANSACTION_FUNCTIONS = {
    "admit_carto_job_locked",
    "negative_exact_intent_gate_locked",
    "_acquire_q3_leases_impl",
    "_release_q3_leases_impl",
    "_finalize_carto_hold",
    "_reserve_carto_hold",
    "_acquire_q3_holds",
    "_release_q3_holds",
}
EXECUTOR_CALLS = {"to_thread", "run_in_executor", "run_carto_cpu", "submit"}
FORBIDDEN_UNDER_HOLD = {
    "run_carto_cpu",
    "to_thread",
    "run_in_executor",
    "sleep",
    "wait_for",
    # No short span reaches an HTTP write (the scan walks locked callees too).
    "post",
    "put",
    "patch",
}
REGISTRY_ADAPTERS = {
    "admit_carto_job_locked",
    "negative_exact_intent_gate_locked",
    "_acquire_q3_leases_impl",
    "_release_q3_leases_impl",
    "_finalize_carto_hold",
    "_reserve_carto_hold",
    "_acquire_q3_holds",
    "_release_q3_holds",
    "_governance_holds",
    "_governance_cooldowns",
    "registry_held_regions",
    "registry_held_keys",
}
RETIRED_SYNC_WRAPPERS = {
    "acquire_q3_leases",
    "release_q3_leases",
    "release_q3_leases_locked",
    "finalize_claim",
    "finalize_claim_locked",
    "finalize_cancelled_job_claim",
    "cleanup_carto_auxiliary_artifacts",
    "cleanup_carto_auxiliary_artifacts_locked",
    "sweep_region_governance",
    "sweep_region_governance_locked",
    "_delete_carto_job_json_if_unclaimed",
}


def _cache() -> dict[str, dict]:
    return {
        "vault": {"id": "vault"},
        "repo": {"id": "repo", "parent_id": "vault"},
        "file": {"id": "file", "parent_id": "repo"},
    }


def _footprint(tmp_path: Path) -> dict:
    target = tmp_path / "repo" / "a.py"
    target.parent.mkdir(exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    return gov.compute_footprint(target, "file", _cache(), kind="file")


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_admission_lock(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_name(node.func) == "admission_lock"


def _admission_span_offenders(source: str, filename: str = "<mutant>") -> list[str]:
    tree = ast.parse(source, filename=filename)
    locked_callees = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in LOCKED_TRANSACTION_FUNCTIONS
    }
    offenders: list[str] = []
    for function in [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)]:
        for span in [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.AsyncWith) and any(_is_admission_lock(item.context_expr) for item in node.items)
        ]:
            for child in [part for statement in span.body for part in ast.walk(statement)]:
                if isinstance(child, ast.Await):
                    offenders.append(f"await in admission span {filename}:{span.lineno}")
                if isinstance(child, ast.Call) and _call_name(child.func) in FORBIDDEN_UNDER_HOLD:
                    offenders.append(
                        f"forbidden call {_call_name(child.func)} in admission span {filename}:{span.lineno}"
                    )
                if isinstance(child, ast.Call) and _call_name(child.func) in LOCKED_TRANSACTION_FUNCTIONS:
                    # The synchronous locked function is the required carrier;
                    # the call itself is legal, but its body must not reach
                    # forbidden HTTP/executor/pacing work either.
                    callee = locked_callees.get(_call_name(child.func))
                    if callee is not None:
                        for part in ast.walk(callee):
                            if (
                                isinstance(part, ast.Call)
                                and _call_name(part.func) in FORBIDDEN_UNDER_HOLD
                            ):
                                offenders.append(
                                    f"forbidden call {_call_name(part.func)} in locked callee "
                                    f"{callee.name} of admission span {filename}:{span.lineno}"
                                )
                    continue
    return offenders


def _scan_package(extra_source: str | None = None) -> list[str]:
    offenders: list[str] = []
    files = sorted(PACKAGE.rglob("*.py"))
    if extra_source is not None:
        offenders.extend(_admission_span_offenders(extra_source, "<injected>"))
    for path in files:
        offenders.extend(
            _admission_span_offenders(path.read_text(encoding="utf-8"), path.name)
        )
    return offenders


def test_async_q3_lock_wait_keeps_event_loop_responsive(tmp_path):
    jobs = tmp_path / "jobs"

    async def scenario() -> int:
        holding = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with gov.admission_lock(jobs):
                holding.set()
                await release.wait()

        holder_task = asyncio.create_task(holder())
        await holding.wait()
        ticks = 0
        stop = asyncio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        ticker_task = asyncio.create_task(ticker())

        async def acquire() -> list[dict]:
            if os.environ.get("CUT2B_NEUTRALIZE_LOCK_WAIT_LOOP"):
                # Probe: replace the yield-on-poll loop with a loop-blocking
                # wait, so the ticker cannot advance while the lock is busy.
                deadline = time.monotonic() + 0.15
                while time.monotonic() < deadline:
                    time.sleep(0.005)
            return await gov.acquire_q3_leases_async(
                [_footprint(tmp_path)], account_scope_id=ACCOUNT, jobs_root=jobs
            )

        acquire_task = asyncio.create_task(acquire())
        await asyncio.sleep(0.12)
        assert ticks >= 5
        release.set()
        leases = await acquire_task
        assert len(leases) == 1
        assert await gov.release_q3_leases_async(leases, jobs_root=jobs) == []
        stop.set()
        await ticker_task
        await holder_task
        return ticks

    assert asyncio.run(scenario()) >= 5


def test_async_lock_wait_cancellation_stops_before_transaction(tmp_path):
    jobs = tmp_path / "jobs"

    async def scenario() -> None:
        holding = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with gov.admission_lock(jobs):
                holding.set()
                await release.wait()

        holder_task = asyncio.create_task(holder())
        await holding.wait()
        task = asyncio.create_task(
            gov.acquire_q3_leases_async(
                [_footprint(tmp_path)], account_scope_id=ACCOUNT, jobs_root=jobs
            )
        )
        await asyncio.sleep(0.08)
        if os.environ.get("CUT2B_NEUTRALIZE_CANCEL_WAIT"):
            # Probe: the wait ignores cancellation, so the late transaction runs
            # to insertion before the cancel lands.
            release.set()
            await holder_task
            for _ in range(200):
                if _governance_holds(jobs):
                    break
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert tuple(_governance_holds(jobs)) == ()
        release.set()
        await holder_task
        await asyncio.sleep(0.08)
        assert tuple(_governance_holds(jobs)) == ()

    asyncio.run(scenario())


def test_no_await_inside_admission_mutex_transactions():
    assert _scan_package() == []

    # Sensitivity: a source-copy await, HTTP or executor call inside an
    # admission span — or inside a synchronous locked callee — is rejected by
    # the same walker.
    mutants = (
        "async def mutant(paths):\n"
        "    async with admission_lock(paths):\n"
        "        await asyncio.sleep(0)\n"
        "        admit_carto_job_locked()\n",
        "async def mutant(paths, client):\n"
        "    async with admission_lock(paths):\n"
        "        client.post('/v1/nodes')\n",
        "def admit_carto_job_locked(paths):\n"
        "    run_carto_cpu('x', f)\n"
        "async def mutant(paths):\n"
        "    async with admission_lock(paths):\n"
        "        admit_carto_job_locked(paths)\n",
    )
    for mutant in mutants:
        assert _admission_span_offenders(mutant), mutant


def _async_offenders(source: str, filename: str = "<mutant>") -> list[tuple[str, str]]:
    tree = ast.parse(source, filename=filename)
    offenders: list[tuple[str, str]] = []
    for function in [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)]:
        for call in [node for node in ast.walk(function) if isinstance(node, ast.Call)]:
            name = _call_name(call.func)
            if name in RETIRED_SYNC_WRAPPERS:
                offenders.append((filename, f"{function.name}:{name}"))
                continue
            if name not in EXECUTOR_CALLS:
                continue
            referenced = {
                _call_name(node)
                for node in ast.walk(call)
                if isinstance(node, (ast.Name, ast.Attribute))
            }
            if referenced & REGISTRY_ADAPTERS:
                offenders.append((filename, f"{function.name}:{name}(registry)"))

    def walk_async(node: ast.AST, scope: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            child_name = getattr(child, "name", "")
            name = f"{scope}.{child_name}" if scope and child_name else child_name or scope
            if isinstance(child, (ast.ClassDef,)):
                walk_async(child, name)
            elif isinstance(child, ast.AsyncFunctionDef):
                for call in [part for part in ast.walk(child) if isinstance(part, ast.Call)]:
                    callee = _call_name(call.func)
                    if callee in RETIRED_SYNC_WRAPPERS:
                        offenders.append((filename, f"{name}:{callee}"))
                    if callee in EXECUTOR_CALLS:
                        referenced = {
                            _call_name(part)
                            for part in ast.walk(call)
                            if isinstance(part, (ast.Name, ast.Attribute))
                        }
                        if referenced & REGISTRY_ADAPTERS:
                            offenders.append((filename, f"{name}:{callee}(registry)"))

    walk_async(tree)
    return sorted(set(offenders))


def test_async_functions_do_not_call_sync_admission_wrappers_directly():
    offenders: list[tuple[str, str]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        offenders.extend(_async_offenders(path.read_text(encoding="utf-8"), path.name))
    assert offenders == []

    # Sensitivity: the deleted-wrapper name and a to_thread(locked adapter) edge
    # are both rejected; a plain synchronous call inside admission_lock is not.
    assert _async_offenders(
        "async def mutant():\n"
        "    acquire_q3_leases([])\n"
        "    await asyncio.to_thread(admit_carto_job_locked, job_file='x')\n"
    )
    assert not _async_offenders(
        "async def legal():\n"
        "    async with admission_lock('root'):\n"
        "        admit_carto_job_locked(job_file='x')\n"
    )
