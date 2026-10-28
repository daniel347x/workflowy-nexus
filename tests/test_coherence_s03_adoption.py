"""Historical S03 names now witness real warm rebuild/local publication, not S1."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from coherence_harness import NOW, s02_node
from test_coherence_s03_transition_cow import _make_client, _make_lineage_client
from workflowy_mcp.coherence import SemanticEffect, append_journal_transaction, read_manifest
from workflowy_mcp.coherence import snapshot_store
from workflowy_mcp.coherence.schema import JournalProducer
from workflowy_mcp.models import WorkFlowyNode

BASE_NODES = [s02_node("A", name="alpha"), s02_node("B", name="beta")]


def _append(paths, effects):
    return append_journal_transaction(paths, operation_kind="cut2b-rebuild", effects=effects,
        producer=JournalProducer(pid=os.getpid(), pid_create_time=1.0,
                                 owner_kind="process", ownership_kind="none"))


def _neutralize_merge(monkeypatch, flag):
    """Test-only loss of sparse rebase; no replacement production replay route."""
    if os.environ.get(flag):
        from workflowy_mcp.client import api_client_core
        monkeypatch.setattr(api_client_core, "merge_offer_with_transition_effects",
            lambda offer, *args, **kwargs: SimpleNamespace(
                rebased=offer, surviving_effects=(), dirty_ids=frozenset()))


def _warm_load(client):
    """The real loader, with an explicit wrong-fallback counterfactual only in tests."""
    result = client._load_nodes_export_cache_from_disk()
    if not result and os.environ.get("CUT2B_NEUTRALIZE_LINEAGE_ERROR"):
        old = client._cache_view_publisher.capture()
        client._publish_lineage_view({"nodes": [s02_node("DECOY")]},
            epoch_number=old.epoch_number + 1, epoch_id="d" * 32,
            adopted_sequence=old.adopted_sequence, snapshot_timestamp=NOW)
        client._nodes_export_lineage_error = None
        return True
    return result


def test_incremental_failure_triggers_one_clean_base_recovery(tmp_path, monkeypatch):
    """Historical name: ONE explicit real warm rebuild, no incremental failure/recovery engine."""
    client, paths = _make_lineage_client(tmp_path, monkeypatch, BASE_NODES)
    old = client._cache_view_publisher.capture()
    _append(paths, [SemanticEffect.node_create(s02_node("C", name="gamma"))])
    materialize = snapshot_store.materialize_current_payload
    calls = []

    def materialize_then_cow(*args, **kwargs):
        result = materialize(*args, **kwargs)
        calls.append(result)
        client._apply_update_to_nodes_export_cache(WorkFlowyNode(id="A", name="alpha-local"))
        return result

    monkeypatch.setattr(snapshot_store, "materialize_current_payload", materialize_then_cow)
    _neutralize_merge(monkeypatch, "CUT2B_NEUTRALIZE_REBUILD_MERGE")
    assert _warm_load(client)
    view = client._cache_view_publisher.capture()
    assert len(calls) == 1 and view.adopted_sequence == 1
    assert view.nodes_by_id["C"]["name"] == "gamma"
    assert view.nodes_by_id["A"]["name"] == "alpha-local"
    assert old.nodes_by_id["A"]["name"] == "alpha" and old.adopted_sequence == 0
    assert "A" in client._nodes_export_dirty_ids


def test_failed_recovery_leaves_last_good_view_and_markers(tmp_path, monkeypatch):
    """Historical name: a malformed retained base fails explicit warm rebuild without publishing."""
    client, paths = _make_lineage_client(tmp_path, monkeypatch, BASE_NODES)
    last_good = client._cache_view_publisher.capture()
    _append(paths, [SemanticEffect.node_create(s02_node("C"))])
    Path(read_manifest(paths, required=True).base_snapshot_path).write_text("{not-json", encoding="utf-8")
    assert _warm_load(client) is False
    assert client._nodes_export_lineage_error
    assert client._cache_view_publisher.capture() is last_good
    assert last_good.adopted_sequence == 0 and set(last_good.nodes_by_id) == {"A", "B"}


def test_missing_manifest_bootstrap_adopts_legacy_snapshot(tmp_path, monkeypatch):
    """No manifest: the real warm loader bootstraps the usable legacy wrapper once."""
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    legacy = snapshots / "20260101-000000-000000_pid1_nodes_export_snapshot.json"
    legacy.write_text(json.dumps({"snapshot_timestamp": NOW,
        "nodes_export": {"nodes": [{"id": "L", "name": "legacy"}]}}), encoding="utf-8")
    if os.environ.get("CUT2B_NEUTRALIZE_BOOTSTRAP"):
        from workflowy_mcp.client.api_client_core import WorkFlowyClientCore
        monkeypatch.setattr(WorkFlowyClientCore, "_load_nodes_export_cache_from_disk", lambda self: False)
    client, _jobs, _snapshots = _make_client(tmp_path, monkeypatch)
    view = client._cache_view_publisher.capture()
    assert "L" in view.nodes_by_id, "bootstrap did not publish its legacy marker"
    assert view.nodes_by_id["L"]["name"] == "legacy" and view.adopted_sequence == 0
    assert client._nodes_export_cache is view.nodes_export and client._nodes_export_lineage_error is None
    assert read_manifest(client._coherence_paths(), required=True).base_snapshot_path is not None


def test_schema_corruption_in_manifest_fails_closed(tmp_path, monkeypatch):
    client, paths = _make_lineage_client(tmp_path, monkeypatch, BASE_NODES)
    last_good = client._cache_view_publisher.capture()
    _append(paths, [SemanticEffect.node_create(s02_node("C"))])
    paths.manifest_path.write_text("{not-json", encoding="utf-8")
    assert _warm_load(client) is False
    assert "CoherenceSchemaError" in client._nodes_export_lineage_error
    assert client._cache_view_publisher.capture() is last_good
    assert last_good.adopted_sequence == 0


def test_cow_patch_racing_adoption_is_rebased_never_discarded(tmp_path, monkeypatch):
    """Historical name: newer real S2 offer through the kept local publisher carries prior COW."""
    from coherence_harness import publish_fixture_head
    client, paths = _make_lineage_client(tmp_path, monkeypatch, BASE_NODES)
    old = client._cache_view_publisher.capture()
    _append(paths, [SemanticEffect.node_create(s02_node("C", name="gamma"))])
    client._apply_update_to_nodes_export_cache(WorkFlowyNode(id="A", name="alpha-local"))
    assert client._cache_view_publisher.capture().adopted_sequence == 0
    _neutralize_merge(monkeypatch, "CUT2B_NEUTRALIZE_PUBLISH_MERGE")
    view = publish_fixture_head(client, paths)
    assert view is client._cache_view_publisher.capture() and view.adopted_sequence == 1
    assert view.nodes_by_id["A"]["name"] == "alpha-local" and view.nodes_by_id["C"]["name"] == "gamma"
    assert old.nodes_by_id["A"]["name"] == "alpha" and old.adopted_sequence == 0
