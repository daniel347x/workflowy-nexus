"""Session-wide test-runtime isolation (S02 round 4, F1).

Two guarantees for the whole suite:

1. **Isolation.** ``WORKFLOWY_MCP_TEST_RUNTIME_DIR`` is set for the entire
   session, so ``get_runtime_base_dir()`` and ``get_cartographer_jobs_dir()``
   resolve beneath a private temp directory.  Because spawned subprocesses
   inherit the environment, real-subprocess tests are covered too — no test
   (client construction warm start, bootstrap, GC, or harness code) can
   write into the operator's real runtime/governance directories.

2. **Regression proof.** A before/after inventory of the REAL
   per-user ``workflowy-mcp`` state-home tree (``cartographer_jobs``,
   ``nodes_export_cache``, NEXUS run state, and every present/future
   sibling) asserts the suite leaves it **file-set/mtime/size-identical**:
   no new files, no deleted files, no mtime or size changes.  Pre-existing
   content is fine; drift is not.  (The check is an inventory triple
   comparison — file-set/mtime/size, not a byte hash.  Exactly one
   exception: the TOP-LEVEL ``logs/`` diagnostic directory is excluded
   because live MCP hosts write server logs continuously; nested ``logs``
   directories under state-bearing trees REMAIN fenced — see
   ``_inventory``.)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_TEST_RUNTIME_DIR = Path(tempfile.mkdtemp(prefix="workflowy-mcp-test-runtime-"))


@pytest.fixture(autouse=True)
def _isolate_write_schedules():
    """Reset only newly created test-root schedules after their tasks joined."""
    module = sys.modules.get("workflowy_mcp.carto_write_governor")
    before = set(getattr(module, "_WRITE_SCHEDULES", {}))
    yield
    module = sys.modules.get("workflowy_mcp.carto_write_governor")
    if module is not None:
        for key in set(module._WRITE_SCHEDULES) - before:
            assert not module._WRITE_SCHEDULES[key].lock.locked(), (
                f"test left a scheduler short lock held: {key}")
            del module._WRITE_SCHEDULES[key]


@pytest.fixture(autouse=True)
def _isolate_coherence_short_locks():
    """Drop only this test's isolated-root locks after its tasks have joined.

    The production table is process-lifetime; no client-close/startup reset is
    added. Preserve pre-existing keys rather than hiding another fixture's state.
    """
    module = sys.modules.get("workflowy_mcp.coherence.paths")
    before = set(module._COHERENCE_LOCKS) if module is not None else set()
    yield
    module = sys.modules.get("workflowy_mcp.coherence.paths")
    if module is not None:
        for key in set(module._COHERENCE_LOCKS) - before:
            lock = module._COHERENCE_LOCKS[key]
            assert not lock.locked(), f"test left a coherence short lock held: {key}"
            del module._COHERENCE_LOCKS[key]


@pytest.fixture(autouse=True)
def _isolate_memory_ownership_tables():
    """Drop only this test's isolated-root memory ownership after its tasks joined.

    Autouse setup precedes every explicit fixture, so teardown runs after their
    joins. The production tables are process-lifetime; no client-close/startup
    reset is added here, and pre-existing keys are preserved rather than hiding
    another fixture's state. Coherence short locks have their own fixture above.
    """
    module = sys.modules.get("workflowy_mcp.mutation_locks")
    registry = getattr(module, "_LOCKS", None) if module is not None else None
    before_holders = set(registry.holders) if registry is not None else set()
    before_cooldowns = set(registry.cooldowns) if registry is not None else set()
    governance = sys.modules.get("workflowy_mcp.carto_job_governance")
    before_admission = set(getattr(governance, "_ADMISSION_LOCKS", {}))
    yield
    module = sys.modules.get("workflowy_mcp.mutation_locks")
    registry = getattr(module, "_LOCKS", None) if module is not None else None
    if registry is not None:
        for key in set(registry.holders) - before_holders:
            hold = registry.holders[key]
            assert hold.state is None or hold.released, (
                f"test left a bound region hold: {getattr(hold.governance, 'claim_id', key)}")
            del registry.holders[key]
        for key in set(registry.cooldowns) - before_cooldowns:
            del registry.cooldowns[key]
    governance = sys.modules.get("workflowy_mcp.carto_job_governance")
    if governance is not None:
        for key in set(getattr(governance, "_ADMISSION_LOCKS", {})) - before_admission:
            lock = governance._ADMISSION_LOCKS[key]
            assert not lock.locked(), f"test left an admission short lock held: {key}"
            del governance._ADMISSION_LOCKS[key]


def pytest_configure(config):  # noqa: ARG001 - pytest hook signature
    """Activate test-runtime isolation at COLLECTION time (S03 seal O1).

    The session-scoped fixture below activates at fixture setup, which is
    too late for any future module-scope client construction during test
    collection.  Setting the environment variable here (pytest's earliest
    hook) guarantees no collection-time code can touch the operator's real
    runtime/governance directories.
    """

    os.environ["WORKFLOWY_MCP_TEST_RUNTIME_DIR"] = str(_TEST_RUNTIME_DIR)
    # Full-server tests may run under WSL; this skips only the platform refusal,
    # never the process-lifetime account writer guard.
    os.environ["WORKFLOWY_MCP_TEST_ALLOW_WSL_SERVER"] = "1"
    # CF-A01 (Waypoint A Stage2-15): opt-in test-mode deep-freeze of every
    # published CacheView payload, session-wide.  The env var sets the
    # import-time default in every subprocess actor (harness launchers
    # merge os.environ); the session fixture below flips the authoritative
    # parent-process runtime flag.
    os.environ["WORKFLOWY_MCP_TEST_DEEP_FREEZE"] = "1"


def _real_state_home() -> Path | None:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        return None
    return Path(base) / "workflowy-mcp"


def _inventory(root: Path | None = None) -> dict[str, tuple[float, int]]:
    """Map of absolute file path -> (mtime, size) across a state-home tree.

    Covers the WHOLE tree by construction (never a hard-coded directory
    list).  Exactly ONE approved exception (seal-3 item 3): the TOP-LEVEL
    ``logs/`` diagnostic directory is excluded — live MCP hosts append to
    their server logs on ordinary tool calls and create new log files when
    hosts cycle, so log content is guaranteed noise, not coherence state.
    The exception is deliberately TOP-LEVEL ONLY: a ``logs`` directory
    nested under a state-bearing tree (e.g. ``cartographer_jobs/x/logs/``)
    is state and REMAINS fenced.  ``root`` is injectable so the rule can
    be tested against a temporary tree without touching the operator's
    real state home.
    """

    entries: dict[str, tuple[float, int]] = {}
    if root is None:
        root = _real_state_home()
        if root is None:
            return entries
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == "logs":
            continue  # top-level diagnostic directory (see docstring)
        try:
            stat = path.stat()
        except OSError:
            continue
        entries[str(path)] = (stat.st_mtime, stat.st_size)
    return entries


@pytest.fixture(autouse=True, scope="session")
def _isolate_test_runtime_dirs():
    os.environ["WORKFLOWY_MCP_TEST_RUNTIME_DIR"] = str(_TEST_RUNTIME_DIR)
    yield
    os.environ.pop("WORKFLOWY_MCP_TEST_RUNTIME_DIR", None)
    os.environ.pop("WORKFLOWY_MCP_TEST_ALLOW_WSL_SERVER", None)
    # S02 forward-ledger item 10c: remove the per-run isolated runtime tree at
    # session teardown (KB-scale residue otherwise accumulates per run).
    import shutil

    shutil.rmtree(_TEST_RUNTIME_DIR, ignore_errors=True)


@pytest.fixture(autouse=True, scope="session")
def _cf_a01_deep_freeze_cache_views():
    """CF-A01 (Stage2-15): run the WHOLE suite with deep-frozen published
    CacheView payloads.  Any cross-function alias mutation — the residual
    class the static alias scanner cannot see — raises TypeError at the
    mutation site.  Production is untouched: the flag exists only here and
    in subprocess actors inheriting the test environment.
    """

    from workflowy_mcp.coherence.cache_view import (
        is_test_deep_freeze_active,
        set_test_deep_freeze,
    )

    previous = is_test_deep_freeze_active()
    set_test_deep_freeze(True)
    try:
        yield
    finally:
        set_test_deep_freeze(previous)
        os.environ.pop("WORKFLOWY_MCP_TEST_DEEP_FREEZE", None)


_CF_A01_PRECEDING_TEST = "<session start>"


@pytest.fixture(autouse=True)
def _cf_a01_assert_deep_freeze(request, _cf_a01_deep_freeze_cache_views):
    """Detect a leaked freeze toggle before the next test can run unfrozen."""
    from workflowy_mcp.coherence.cache_view import is_test_deep_freeze_active

    global _CF_A01_PRECEDING_TEST
    assert is_test_deep_freeze_active(), (
        "CF-A01 deep-freeze is disabled at test start; "
        f"preceding test: {_CF_A01_PRECEDING_TEST}; current test: {request.node.nodeid}"
    )
    try:
        yield
    finally:
        _CF_A01_PRECEDING_TEST = request.node.nodeid


@pytest.fixture(autouse=True, scope="session")
def _prove_real_runtime_dirs_untouched():
    """Session-level fence: the real state-home tree must be
    file-set/mtime/size-identical before and after the suite (spec §16.7,
    seal-2 item 6).

    Operational caveat (S02 ledger): UNRELATED live MCP/CARTO activity
    mutating the real state home during a full-suite run trips this fence
    conservatively — read the named offending paths before assuming test
    leakage; prefer running the full suite with no active CARTO jobs.
    """

    before = _inventory()
    yield
    after = _inventory()
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        path for path in set(before) & set(after) if before[path] != after[path]
    )
    problems: list[str] = []
    if added:
        problems.append(f"NEW files in the real state home: {added}")
    if removed:
        problems.append(f"REMOVED files from the real state home: {removed}")
    if changed:
        problems.append(f"MODIFIED files in the real state home: {changed}")
    assert not problems, (
        "Test suite leaked into the real workflowy-mcp state-home tree "
        "(§16.7). " + " | ".join(problems)
    )
