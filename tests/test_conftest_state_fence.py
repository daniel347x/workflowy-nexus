"""Seal-3 item 3 — real-state-fence inventory rule tests (temporary trees only).

These tests never touch the operator's real ``workflowy-mcp`` state home;
they exercise ``conftest._inventory`` against injected temporary roots.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import _inventory


def _touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_top_level_logs_directory_is_excluded(tmp_path):
    target = _touch(tmp_path / "logs" / "workflowy-mcp-server-1.log")
    _touch(tmp_path / "logs" / "deep" / "nested-server.log")
    _touch(tmp_path / "cartographer_jobs" / "job.json")

    entries = _inventory(tmp_path)

    assert str(target) not in entries
    assert str(tmp_path / "logs" / "deep" / "nested-server.log") not in entries
    assert str(tmp_path / "cartographer_jobs" / "job.json") in entries


def test_nested_logs_directories_under_state_trees_REMAIN_fenced(tmp_path):
    carto_log = _touch(tmp_path / "cartographer_jobs" / "x" / "logs" / "state.json")
    nexus_log = _touch(tmp_path / "nexus_runs" / "logs" / "state.json")
    governance = _touch(tmp_path / "cartographer_jobs" / ".governance" / "acct" / "write_governor.json")
    cache = _touch(tmp_path / "nodes_export_cache" / "snapshot.json")
    nexus_run = _touch(tmp_path / "nexus_runs" / "run-1" / "data.json")

    entries = _inventory(tmp_path)

    # Nested 'logs' directories are NOT the top-level diagnostic exception:
    # their contents are state and remain covered.
    assert str(carto_log) in entries
    assert str(nexus_log) in entries
    # Ordinary governance/cache/NEXUS state files are covered.
    assert str(governance) in entries
    assert str(cache) in entries
    assert str(nexus_run) in entries


def test_inventory_records_file_set_mtime_size_not_hash(tmp_path):
    state = _touch(tmp_path / "cartographer_jobs" / "job.json", "abc")
    entries = _inventory(tmp_path)
    mtime, size = entries[str(state)]
    assert size == 3  # the comparison is an inventory triple, not a hash
    assert mtime > 0


def _memory_table_fixture():
    """The production isolation fixture, or a named counterfactual of its failure.

    ``CUT2B_NEUTRALIZE_CONFTEST_OWNERSHIP`` selects the mutation the two tests
    must reject: the neutralized teardown neither drops this test's new keys nor
    raises on a still-bound hold (it silently removes it). It wraps the real
    fixture so the snapshot/join ordering is unchanged and only the asserted rule
    is violated; production conftest is never edited or reset.
    """
    import conftest
    target = conftest._isolate_memory_ownership_tables
    target = getattr(target, "__wrapped__", target)
    if not os.environ.get("CUT2B_NEUTRALIZE_CONFTEST_OWNERSHIP"):
        return target

    def neutralized():
        import sys
        locks = sys.modules.get("workflowy_mcp.mutation_locks")
        gov = sys.modules.get("workflowy_mcp.carto_job_governance")
        before_holders = set(locks._LOCKS.holders) if locks is not None else set()
        before_cooldowns = set(locks._LOCKS.cooldowns) if locks is not None else set()
        before_admission = set(getattr(gov, "_ADMISSION_LOCKS", {})) if gov is not None else set()
        real = target()
        next(real)
        try:
            yield
        finally:
            new_holders = ({key: hold for key, hold in locks._LOCKS.holders.items()
                            if key not in before_holders} if locks is not None else {})
            new_cooldowns = ({key: value for key, value in locks._LOCKS.cooldowns.items()
                              if key not in before_cooldowns} if locks is not None else {})
            new_admission = ({key: value for key, value in getattr(gov, "_ADMISSION_LOCKS", {}).items()
                              if key not in before_admission} if gov is not None else {})
            try:
                next(real)
            except StopIteration:
                pass
            except AssertionError:
                # The counterfactual silently drops the leaked bound hold.
                for key in set(locks._LOCKS.holders) - before_holders:
                    del locks._LOCKS.holders[key]
            # ...and never drops the remaining new entries the real fixture removed.
            for key, hold in new_holders.items():
                locks._LOCKS.holders.setdefault(key, hold)
            for key, value in new_cooldowns.items():
                locks._LOCKS.cooldowns.setdefault(key, value)
            for key, value in new_admission.items():
                gov._ADMISSION_LOCKS.setdefault(key, value)
    return neutralized


def test_memory_ownership_tables_are_isolated_after_tasks_join():
    """Only this test's isolated-root holds/cooldowns/admission locks are dropped."""
    import asyncio
    from workflowy_mcp import carto_job_governance as gov, mutation_locks as locks

    keep_holder, drop_holder = object(), object()
    keep_cooldown = ("keep-root", "a" * 32, "keep-key")
    drop_cooldown = ("drop-root", "a" * 32, "drop-key")
    keep_admission = "keep-admission"
    # Pre-existing state as another fixture would have left it; the isolation
    # snapshot must preserve it.
    locks._LOCKS.holders[keep_holder] = locks._RegionHold(keep_holder, None, (), None, "short", None)
    locks._LOCKS.cooldowns[keep_cooldown] = {"last_terminal_at": "2026-01-01T00:00:00"}
    gov._ADMISSION_LOCKS[keep_admission] = asyncio.Lock()
    generator = _memory_table_fixture()()
    next(generator)
    try:
        locks._LOCKS.holders[drop_holder] = locks._RegionHold(drop_holder, None, (), None, "short", None)
        locks._LOCKS.cooldowns[drop_cooldown] = {"last_terminal_at": "2026-01-01T00:00:00"}
        gov._ADMISSION_LOCKS["drop-admission"] = asyncio.Lock()
        with pytest.raises(StopIteration):
            next(generator)
        assert drop_holder not in locks._LOCKS.holders and keep_holder in locks._LOCKS.holders
        assert drop_cooldown not in locks._LOCKS.cooldowns and keep_cooldown in locks._LOCKS.cooldowns
        assert "drop-admission" not in gov._ADMISSION_LOCKS and keep_admission in gov._ADMISSION_LOCKS
    finally:
        locks._LOCKS.holders.pop(keep_holder, None)
        locks._LOCKS.holders.pop(drop_holder, None)
        locks._LOCKS.cooldowns.pop(keep_cooldown, None)
        gov._ADMISSION_LOCKS.pop(keep_admission, None)


def test_memory_ownership_tables_reject_a_bound_hold_left_by_a_task():
    """A task that never joined leaves a bound grant; isolation must not hide it."""
    from workflowy_mcp import mutation_locks as locks

    generator = _memory_table_fixture()()
    next(generator)
    leaked = object()
    try:
        locks._LOCKS.holders[leaked] = locks._RegionHold(leaked, object(), (), None, "long_lived", None)
        observed = None
        try:
            next(generator)
        except AssertionError as error:
            observed = str(error)
        except StopIteration:
            pass
        assert observed is not None and "bound region hold" in observed, "isolation silently accepted a bound region hold"
    finally:
        locks._LOCKS.holders.pop(leaked, None)
