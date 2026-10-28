"""Neutral isolated coherence/owner fixtures retained under a historical name.

Real warm load/local publication and journal setup use isolated roots.
The retired on-disk mutation-lease plane has no fixture carrier here.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp.client.api_client_core import WorkFlowyClientCore
from workflowy_mcp.coherence.cache_view import CacheViewPublisher, unpopulated_cache_view
from workflowy_mcp.coherence.transition_effects import TransitionEffectPolicy
from workflowy_mcp.coherence.effects import SemanticEffect
from workflowy_mcp.coherence.journal_store import append_journal_transaction
from workflowy_mcp.coherence.paths import coherence_paths
from workflowy_mcp.coherence.schema import (
    JournalProducer,
    initialize_manifest_if_absent,
    manifest_with_updates,
    new_bootstrap_manifest,
    write_manifest,
)
from workflowy_mcp.coherence.snapshot_store import (
    new_snapshot_candidate_path,
    write_snapshot_candidate,
)

NOW = "2026-11-01T00:00:00+00:00"
ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio_run(function(*args, **kwargs))

    return wrapper


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Node/cache builders
# ---------------------------------------------------------------------------


def node(node_id: str, parent_id: str | None = None, name: str | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": node_id,
        "name": name if name is not None else f"node-{node_id}",
        "note": "",
        "parent_id": parent_id,
        "priority": 0,
        "createdAt": 1,
        "modifiedAt": 1,
        "completed": None,
        "data": None,
    }
    payload.update(extra)
    return payload


def cache_of(*specs: tuple[str, str | None]) -> dict[str, dict[str, Any]]:
    """nodes_by_id from (id, parent_id) pairs."""

    return {node_id: node(node_id, parent) for node_id, parent in specs}


def producer() -> JournalProducer:
    return JournalProducer(
        pid=os.getpid(), pid_create_time=1.0, owner_kind="process", ownership_kind="none"
    )


# ---------------------------------------------------------------------------
# The S05 client double — real warm load over the isolated lineage
# ---------------------------------------------------------------------------


class S05Client(WorkFlowyClientCore):
    """An isolated data-only client using the real local publisher and warm loader."""

    def __init__(self, paths, publisher: CacheViewPublisher, snapshot_dir: Path):
        self._paths = paths
        self._cache_view_publisher = publisher
        self._snapshot_dir = str(snapshot_dir)
        self._nodes_export_dirty_ids = set()
        self._transition_effect_policy = TransitionEffectPolicy(on_dirty=self._nodes_export_dirty_ids.update)
        publisher.transition_policy = self._transition_effect_policy
        self._cache_view_snapshot_timestamp = None
        self._nodes_export_lineage_error = None

    def _coherence_paths(self):
        return self._paths

    def _get_nodes_export_snapshot_dir(self):
        return self._snapshot_dir

    def _log_debug(self, message):
        """No transport/logger is needed by the isolated warm-load fixture."""

    def _capture_view_and_dirty(self):
        return self._cache_view_publisher.capture(), frozenset(self._nodes_export_dirty_ids)

    def _coherence_boundary_status(self):
        from workflowy_mcp.coherence.barrier import read_coherence_boundary_status

        return read_coherence_boundary_status(self._paths, required=False)

    def _safe_account_scope_id(self) -> str:
        return self._paths.account_root.name


def make_lease_env(
    tmp_path: Path,
    account: str = ACCOUNT_A,
    *,
    nodes: Iterable[Mapping[str, Any]] = (),
) -> SimpleNamespace:
    """Isolated jobs root + coherence root + transition manifest + base snapshot
    + a real warm-load/local-publication S05Client."""

    jobs_root = tmp_path / "cartographer_jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    paths = coherence_paths(account, jobs_root=jobs_root)
    manifest = initialize_manifest_if_absent(paths, new_bootstrap_manifest(account, now=NOW))
    candidate = write_snapshot_candidate(
        new_snapshot_candidate_path(snapshot_dir),
        {"nodes": [dict(n) for n in nodes]},
        account_scope_id=account,
        epoch_number=manifest.epoch_number,
        epoch_id=manifest.epoch_id,
        incorporated_through_seq=0,
        publication_kind="full_refresh",
    )
    manifest = write_manifest(
        paths,
        manifest_with_updates(
            manifest, base_snapshot_path=candidate, coherence_mode="transition"
        ),
        expected_epoch_id=manifest.epoch_id,
        expected_head_seq=0,
    )
    publisher = CacheViewPublisher(unpopulated_cache_view())
    client = S05Client(paths, publisher, snapshot_dir)
    assert client._load_nodes_export_cache_from_disk()
    return SimpleNamespace(
        paths=paths,
        jobs_root=jobs_root,
        snapshot_dir=snapshot_dir,
        account=account,
        manifest=manifest,
        client=client,
        publisher=publisher,
    )


def append_tx(
    env: SimpleNamespace,
    transaction_id: str,
    effects: list[SemanticEffect],
    *,
    operation_kind: str = "test_op",
):
    """Append one journal transaction carrying an EXPLICIT transaction id."""

    return append_journal_transaction(
        env.paths,
        operation_kind=operation_kind,
        effects=effects,
        producer=producer(),
        transaction_id=transaction_id,
    )


def effects_create(node_dict: Mapping[str, Any]) -> list[SemanticEffect]:
    return [SemanticEffect.node_create(node_dict)]
