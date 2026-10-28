"""S02 §16.5 — transitional compat-patch (mini epoch publication) tests."""

from __future__ import annotations

import asyncio
import os
from functools import wraps
from pathlib import Path

import pytest
from coherence_harness import (
    ACCOUNT_A,
    RecordedSleep,
    make_coherence_root,
    s02_fetch,
    s02_make_export,
    s02_node,
    s02_set_barrier,
    s02_set_external_dirt,
)

from workflowy_mcp.coherence import (
    JournalProducer,
    SemanticEffect,
    append_journal_transaction,
    read_external_dirt,
    read_manifest,
    run_full_refresh,
)
from workflowy_mcp.coherence.errors import CoherenceError, CoherenceStateMissingError
from workflowy_mcp.coherence.refresh_authority import run_compat_patch
from workflowy_mcp.coherence.snapshot_store import materialize_current_payload


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _snapshot_dir(tmp_path: Path) -> Path:
    target = tmp_path / "snapshots"
    target.mkdir(parents=True, exist_ok=True)
    return target


async def _seed_base(paths, tmp_path, nodes):
    return await run_full_refresh(
        paths=paths,
        fetch=s02_fetch(s02_make_export(nodes)),
        settle_seconds=0,
        snapshot_dir=_snapshot_dir(tmp_path),
        sleep=RecordedSleep(),
        poll_seconds=0.02,
    )


async def _patch(paths, tmp_path, patches, **overrides):
    kwargs = {
        "paths": paths,
        "patches": patches,
        "snapshot_dir": _snapshot_dir(tmp_path),
        "sleep": RecordedSleep(),
        "poll_seconds": 0.02,
    }
    kwargs.update(overrides)
    return await run_compat_patch(**kwargs)


def _view_nodes(paths) -> dict[str, dict]:
    manifest = read_manifest(paths, required=True)
    view = materialize_current_payload(paths, manifest)
    return {str(n["id"]): n for n in view.payload["nodes"]}


@async_test
async def test_patch_materializes_base_plus_journal_before_patching(tmp_path):
    """A journaled-but-not-yet-based node must be visible to the patch."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    await _seed_base(paths, tmp_path, [s02_node("a", note="old")])
    # Journal a create AFTER the base (simulated live tail).
    append_journal_transaction(
        paths,
        operation_kind="create",
        effects=[SemanticEffect.node_create(s02_node("journaled", note="j-old"))],
        producer=JournalProducer(
            pid=os.getpid(), pid_create_time=1.0, owner_kind="process", ownership_kind="none"
        ),
    )
    outcome = await _patch(paths, tmp_path, [{"id": "journaled", "note": "j-new"}])
    nodes = _view_nodes(paths)
    assert nodes["journaled"]["note"] == "j-new"
    assert outcome.incorporated_through_seq == 1
    # The new base folded BOTH the tail record and the patch.
    assert outcome.epoch_number == 2
    manifest = read_manifest(paths, required=True)
    assert manifest.base_incorporated_through_seq == 1
    assert manifest.journal_head_seq == 1


@async_test
async def test_patch_values_use_semantic_codec_exactly_once(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    await _seed_base(paths, tmp_path, [s02_node("a", note="plain")])
    # Entity-bearing caller input is normalized exactly as the legacy patcher
    # did (one pinned cascading dewhiten), then stored semantically.
    await _patch(paths, tmp_path, [{"id": "a", "note": "x &amp; y &lt;b&gt;"}])
    nodes = _view_nodes(paths)
    assert nodes["a"]["note"] == "x & y <b>"


@async_test
async def test_multiple_patches_all_or_none(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    await _seed_base(paths, tmp_path, [s02_node("a"), s02_node("b")])
    outcome = await _patch(
        paths, tmp_path, [{"id": "a", "name": "A2"}, {"id": "b", "note": "B2"}]
    )
    assert (outcome.details or {}).get("patched") == 2
    nodes = _view_nodes(paths)
    assert nodes["a"]["name"] == "A2"
    assert nodes["b"]["note"] == "B2"


@async_test
async def test_missing_target_refuses_with_zero_publication(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    await _seed_base(paths, tmp_path, [s02_node("a")])
    before = read_manifest(paths, required=True)
    with pytest.raises(CoherenceError):
        await _patch(paths, tmp_path, [{"id": "a", "note": "x"}, {"id": "ghost", "note": "y"}])
    after = read_manifest(paths, required=True)
    assert after.epoch_id == before.epoch_id
    assert after.epoch_number == before.epoch_number
    # The valid patch was NOT partially published.
    nodes = _view_nodes(paths)
    assert nodes["a"]["note"] == ""


@async_test
async def test_new_epoch_clears_no_dirt_or_barrier(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    await _seed_base(paths, tmp_path, [s02_node("a", note="old")])
    s02_set_barrier(paths, 2, active=True)
    s02_set_external_dirt(paths, 3, active=True)
    outcome = await _patch(paths, tmp_path, [{"id": "a", "note": "new"}])
    manifest = read_manifest(paths, required=True)
    assert outcome.epoch_number == 2
    assert outcome.hard_barrier_cleared is False
    assert outcome.external_dirt_cleared is False
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.generation == 2
    dirt = read_external_dirt(paths)
    assert dirt is not None and dirt.active is True and dirt.generation == 3


@async_test
async def test_concurrent_append_during_patch_is_preserved_tail(tmp_path):
    """A journal append landing mid-patch (after the materialization capture)
    survives as the new epoch's active tail."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    await _seed_base(paths, tmp_path, [s02_node("a", note="old")])
    from workflowy_mcp.carto_job_governance import (
        _default_mkdir,
        _default_replace,
        _default_tmp_name,
        _default_unlink,
        _default_write_fsync,
    )
    from workflowy_mcp.coherence import DurableFileOps

    def write_fsync(path: Path, text: str) -> None:
        _default_write_fsync(path, text)
        if "_nodes_export_snapshot.json" in path.name:
            append_journal_transaction(
                paths,
                operation_kind="create",
                effects=[SemanticEffect.node_create(s02_node("tail"))],
                producer=JournalProducer(
                    pid=os.getpid(), pid_create_time=1.0,
                    owner_kind="process", ownership_kind="none",
                ),
            )

    fs_ops = DurableFileOps(
        write_fsync=write_fsync,
        replace=_default_replace,
        mkdir=_default_mkdir,
        unlink=_default_unlink,
        tmp_name=_default_tmp_name,
    )
    outcome = await _patch(paths, tmp_path, [{"id": "a", "note": "new"}], fs_ops=fs_ops)
    manifest = read_manifest(paths, required=True)
    # The patch incorporated nothing new; the appended record is the tail.
    assert manifest.journal_head_seq == 1
    assert manifest.base_incorporated_through_seq == 0
    nodes = _view_nodes(paths)
    assert nodes["a"]["note"] == "new"
    assert "tail" in nodes
    assert outcome.incorporated_through_seq == 0


@async_test
async def test_no_manifest_base_fails_closed_refresh_required(tmp_path):
    """No manifest base → fail closed; no in-memory fallback."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    # Bootstrap manifest (epoch 0, no base) exists but has no base snapshot.
    with pytest.raises(CoherenceStateMissingError):
        await _patch(paths, tmp_path, [{"id": "a", "note": "x"}])


@async_test
async def test_bulk_style_sequential_patches_monotonic_epochs(tmp_path):
    """Bulk F12+4 volume shape: one mini epoch per leaf, monotonic, none lost."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    await _seed_base(paths, tmp_path, [s02_node(str(i)) for i in range(5)])
    for index in range(5):
        outcome = await _patch(paths, tmp_path, [{"id": str(index), "note": f"n{index}"}])
        assert outcome.epoch_number == index + 2
    nodes = _view_nodes(paths)
    for index in range(5):
        assert nodes[str(index)]["note"] == f"n{index}"


@async_test
async def test_patch_requires_a_real_target_and_real_field(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    await _seed_base(paths, tmp_path, [s02_node("a")])
    with pytest.raises(CoherenceError):
        await _patch(paths, tmp_path, [{"id": "a", "priority": 7}])
    with pytest.raises(CoherenceError):
        await _patch(paths, tmp_path, [{"note": "x"}])


def test_fail_open_lock_cannot_proceed_unlocked(tmp_path, monkeypatch):
    """H3(d): the retired snapshot lock can never proceed unlocked again."""
    from pydantic import SecretStr

    from workflowy_mcp.client.api_client_core import WorkFlowyClientCore
    from workflowy_mcp.models import APIConfiguration

    jobs_root = tmp_path / "cartographer_jobs"
    jobs_root.mkdir(parents=True)
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "workflowy_mcp.carto_job_governance.get_cartographer_jobs_dir", lambda: jobs_root
    )
    monkeypatch.setattr(
        "workflowy_mcp.client.api_client_core.get_runtime_subdir",
        lambda *parts: snapshot_dir,
    )
    client = WorkFlowyClientCore(
        APIConfiguration(
            api_key=SecretStr("test-key-1234"), base_url="https://workflowy.com/api/v1"
        )
    )
    # A live (fresh) lock file forces the bounded wait to expire.
    lock_path = snapshot_dir / ".nodes_export_patch.lock"
    lock_path.write_text("pid", encoding="utf-8")
    with pytest.raises(RuntimeError, match="refusing to proceed unlocked"):
        with client._nodes_export_snapshot_lock():
            pass
