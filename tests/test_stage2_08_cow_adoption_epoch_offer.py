"""Stage2-08: real warm S2 rebuild + journal tail + racing local sparse COW.

The peer-adoption engine/processes are retired. A test-only synchronous
materialization callback lands COW before the one surviving local publisher.
STAGE2_NEUTRALIZE_MERGE loses that write and must fail its content oracle.
The mandatory lifecycle activation gate remains N08's, not this cut's.
"""
from __future__ import annotations

import asyncio

from coherence_harness import s02_node
from test_coherence_s03_cr02_sparse_effects import _make_lineage_client, _node_model, _append
from test_coherence_s03_adoption import _neutralize_merge, _warm_load
from workflowy_mcp.coherence import SemanticEffect, read_manifest, run_full_refresh, snapshot_store

BASE_NODES = [s02_node("A", name="alpha"), s02_node("B", name="beta", parent_id="A")]


def _rebuild_with_racing_cow(client, paths, monkeypatch):
    """Complete real S2, publish one COW successor, then finish real warm publication."""
    before = client._cache_view_publisher.capture()
    materialize = snapshot_store.materialize_current_payload
    fired = []

    def materialize_then_cow(*args, **kwargs):
        result = materialize(*args, **kwargs)
        fired.append(result)
        client._apply_update_to_nodes_export_cache(_node_model("A", name="cow-write"))
        return result

    monkeypatch.setattr(snapshot_store, "materialize_current_payload", materialize_then_cow)
    _neutralize_merge(monkeypatch, "STAGE2_NEUTRALIZE_MERGE")
    _neutralize_merge(monkeypatch, "CUT2B_NEUTRALIZE_REBUILD_MERGE")
    assert _warm_load(client)
    view = client._cache_view_publisher.capture()
    assert len(fired) == 1, "required materialization-to-publication interleave did not occur"
    assert view.adopted_sequence == read_manifest(paths, required=True).journal_head_seq == 1
    assert view.nodes_by_id["J1"]["name"] == "tail-record"
    assert view.nodes_by_id["A"]["name"] == "cow-write"
    assert set(client._transition_cow_effects) == {"A"}
    assert "A" in client._nodes_export_dirty_ids
    assert before.nodes_by_id["A"]["name"] == "alpha" and before.adopted_sequence == 0
    return view


def test_epoch_offer_reload_preserves_journal_tail_and_racing_cow(tmp_path, monkeypatch):
    """A real full-refresh epoch offer plus a post-offer tail precedes warm rebuild/COW."""
    client, paths = _make_lineage_client(tmp_path, monkeypatch, nodes=BASE_NODES)

    async def fetch():
        return {"nodes": [dict(node) for node in BASE_NODES]}

    outcome = asyncio.run(run_full_refresh(paths=paths, fetch=fetch, refresh_kind="system",
        settle_seconds=0.0, snapshot_dir=tmp_path / "snapshots", poll_seconds=0.05))
    assert outcome.status == "published" and outcome.epoch_number == 1
    _append(paths, [SemanticEffect.node_create(s02_node("J1", name="tail-record"))])
    view = _rebuild_with_racing_cow(client, paths, monkeypatch)
    assert view.epoch_number == 1 and view.epoch_id == outcome.epoch_id


def test_clean_base_recovery_preserves_journal_tail_and_racing_cow(tmp_path, monkeypatch):
    """Historical recovery name: failed explicit warm load, then lawful rebuild with racing COW."""
    client, paths = _make_lineage_client(tmp_path, monkeypatch, nodes=BASE_NODES)
    before = client._cache_view_publisher.capture()
    _append(paths, [SemanticEffect.node_create(s02_node("J1", name="tail-record"))])
    materialize = snapshot_store.materialize_current_payload

    def fail_materialization(*args, **kwargs):
        raise snapshot_store.CoherenceStateMissingError("injected missing retained basis")

    monkeypatch.setattr(snapshot_store, "materialize_current_payload", fail_materialization)
    assert _warm_load(client) is False
    assert client._nodes_export_lineage_error and client._cache_view_publisher.capture() is before
    monkeypatch.setattr(snapshot_store, "materialize_current_payload", materialize)
    view = _rebuild_with_racing_cow(client, paths, monkeypatch)
    assert view.epoch_number == 0 and client._nodes_export_lineage_error is None
