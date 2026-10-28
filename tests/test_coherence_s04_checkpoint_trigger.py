"""S04 Remediation Chain 1 (work-order item 2) — the checkpoint trigger
must NEVER fail a durably sealed mutation.

Two closed holes locked here:

1. The lock-free manifest→record-stat TOCTOU in ``request_checkpoint_if_due``
   (an epoch publication + journal retirement landing BETWEEN the trigger's
   manifest observation and its record stats used to raise CheckpointError
   out of a durably sealed mutation).  The observation and the file facts
    are now adjacent run-to-completion observations on the daemon loop.
2. ``seal_verified_journal_transaction`` never propagates checkpoint
   scheduling/maintenance failure once the journal record and manifest head
   are durable — the seal still reports published/recovered, a bounded
   diagnostic is logged, and no barrier is raised.
"""

from __future__ import annotations

import asyncio
import ast
import inspect
import os
from pathlib import Path

from coherence_harness import (
    ACCOUNT_A,
    make_coherence_root,
    s02_node,
    s04_append,
    s04_append_n,
    s04_checkpoint_config,
    s04_install_base_manifest,
    s04_write_base_snapshot,
)

from workflowy_mcp.coherence import checkpoint as checkpoint_module
from workflowy_mcp.coherence import read_manifest, run_checkpoint_rotation
from workflowy_mcp.coherence.checkpoint import request_checkpoint_if_due
from workflowy_mcp.coherence.errors import CheckpointError


def _snapshot_dir(tmp_path: Path) -> Path:
    return tmp_path / "nodes_export_cache"


def _based_manifest(paths, tmp_path: Path, *, nodes=None):
    base = s04_write_base_snapshot(
        paths,
        _snapshot_dir(tmp_path),
        nodes if nodes is not None else [s02_node("a")],
        epoch_number=0,
        epoch_id="b" * 32,
    )
    s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32)
    return base


def test_epoch_publication_and_retirement_between_observation_and_stats_never_fails_seal(
    tmp_path: Path, monkeypatch
):
    """A call_soon publisher cannot interleave inside the synchronous pair.

    Runtime ordering retains the durable seal/epoch/barrier observations;
    the static half rejects introducing a scheduling suspension in the pair.
    """
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)
    config = s04_checkpoint_config(record_threshold=2)

    real_stats = checkpoint_module._light_tail_stats
    armed = {"once": True}
    racers, events = [], []
    async def _concurrent_rotation():
        events.append("rotation")
        return await run_checkpoint_rotation(paths, snapshot_dir=_snapshot_dir(tmp_path), config=config)

    def _racing_stats(p, manifest):
        first_observation = armed["once"]
        if first_observation:
            armed["once"] = False
            asyncio.get_running_loop().call_soon(
                lambda: racers.append(asyncio.create_task(_concurrent_rotation()))
            )
        result = real_stats(p, manifest)
        if first_observation:
            events.append("stats")
            assert "rotation" not in events
        return result

    monkeypatch.setattr(checkpoint_module, "_light_tail_stats", _racing_stats)

    spawned: list = []

    def _hook():
        task = request_checkpoint_if_due(
            paths, snapshot_dir=_snapshot_dir(tmp_path), config=config
        )
        spawned.append(task)
        return task

    async def _scenario():
        seal = s04_append(
            paths, transaction_id="tx-trigger-race", checkpoint_request=_hook
        )
        events.append("seal")
        task = spawned[0] if spawned else None
        if task is not None:
            # The scheduled maintenance completes (any non-due/published/
            # deferred disposition is acceptable here).
            await task
        await asyncio.sleep(0)
        await asyncio.gather(*racers)
        return seal

    seal = asyncio.run(_scenario())
    assert seal.disposition == "published"
    assert seal.transaction.sequence == 4
    assert seal.checkpoint_requested is True
    manifest = read_manifest(paths, required=True)
    # The journal head includes the transaction: its verified effect is
    # durable either as an active-tail record or as incorporated base
    # content (a racing rotation legitimately incorporates + retires it).
    assert manifest.journal_head_seq >= 4
    assert (
        manifest.base_incorporated_through_seq >= 4
        or (paths.journal_dir / "00000000000000000004.json").exists()
    )
    assert manifest.dirty_status.active is False
    assert events.index("stats") < events.index("seal") < events.index("rotation")
    source = inspect.getsource(request_checkpoint_if_due)
    if os.environ.get("CUT2B_NEUTRALIZE_SPLIT_CHECKPOINT_OBSERVATION"):
        source = source.replace("    stats = _light_tail_stats", "    await asyncio.sleep(0)\n    stats = _light_tail_stats", 1)
    tree = ast.parse(source)
    assert not any(isinstance(node, (ast.Await, ast.AsyncWith, ast.AsyncFor)) for node in ast.walk(tree)), "checkpoint observation can suspend"


def test_checkpoint_hook_failure_never_propagates_and_never_dirties(
    tmp_path: Path, caplog
):
    """A checkpoint scheduling/maintenance failure after a durable seal is
    swallowed with a bounded diagnostic: the seal reports published, the
    journal head includes the transaction, and NO barrier is raised
    (optional compaction failure never hard-dirties)."""
    import logging

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)

    def _boom():
        raise CheckpointError("simulated trigger maintenance failure")

    with caplog.at_level(logging.WARNING, logger="workflowy_mcp.coherence.barrier"):
        seal = s04_append(paths, transaction_id="tx-hook-boom", checkpoint_request=_boom)
    assert seal.disposition == "published"
    assert seal.checkpoint_requested is False
    manifest = read_manifest(paths, required=True)
    assert manifest.journal_head_seq == 1
    assert manifest.dirty_status.active is False
    assert any(
        "checkpoint scheduling failed" in record.message for record in caplog.records
    )
