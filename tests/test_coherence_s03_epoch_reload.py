"""Historical reload names retain strict real warm-load lineage witnesses; S1 is gone."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from coherence_harness import NOW, s02_node
from test_coherence_s03_transition_cow import _make_client, _seed_lineage
from test_coherence_s03_adoption import BASE_NODES, _warm_load
from workflowy_mcp.coherence import read_manifest, write_manifest, manifest_with_updates
from workflowy_mcp.coherence import snapshot_store
from workflowy_mcp.coherence.snapshot_store import write_snapshot_candidate, new_snapshot_candidate_path


def _unpopulated_fixture(tmp_path, monkeypatch):
    client, _jobs, snapshots = _make_client(tmp_path, monkeypatch)
    paths = _seed_lineage(client, snapshots, BASE_NODES)
    return client, paths, snapshots


def test_reload_uses_only_the_manifest_base_never_timestamp_order(tmp_path, monkeypatch):
    client, paths, snapshots = _unpopulated_fixture(tmp_path, monkeypatch)
    decoy = snapshots / "99999999-999999-999999_pid9999_nodes_export_snapshot.json"
    decoy.write_text(json.dumps({"snapshot_timestamp": NOW,
        "nodes_export": {"nodes": [{"id": "DECOY"}]}}), encoding="utf-8")
    if os.environ.get("CUT2B_NEUTRALIZE_MANIFEST_FIRST"):
        real = snapshot_store.materialize_current_payload
        def choose_decoy(*args, **kwargs):
            view = real(*args, **kwargs)
            return SimpleNamespace(payload={"nodes": [{"id": "DECOY"}]},
                epoch_number=view.epoch_number, epoch_id=view.epoch_id,
                adopted_sequence=view.adopted_sequence, snapshot_timestamp=view.snapshot_timestamp,
                base_snapshot_path=view.base_snapshot_path)
        monkeypatch.setattr(snapshot_store, "materialize_current_payload", choose_decoy)
    assert _warm_load(client)
    view = client._cache_view_publisher.capture()
    assert "DECOY" not in view.nodes_by_id and view.nodes_by_id["A"]["name"] == "alpha"
    assert view.epoch_id == read_manifest(paths, required=True).epoch_id
    assert client._nodes_export_lineage_error is None


def test_base_envelope_lineage_mismatch_fails_closed(tmp_path, monkeypatch):
    client, paths, _snapshots = _unpopulated_fixture(tmp_path, monkeypatch)
    manifest = read_manifest(paths, required=True)
    last_good = client._cache_view_publisher.capture()
    write_snapshot_candidate(manifest.base_snapshot_path, {"nodes": BASE_NODES},
        account_scope_id=paths.account_root.name, epoch_number=manifest.epoch_number,
        epoch_id="0" * 31 + "1", incorporated_through_seq=0, publication_kind="full_refresh")
    if os.environ.get("CUT2B_NEUTRALIZE_ENVELOPE"):
        monkeypatch.setattr(snapshot_store, "validate_snapshot_lineage", lambda *args, **kwargs: None)
    assert _warm_load(client) is False
    assert "CoherenceSchemaError" in client._nodes_export_lineage_error
    assert client._cache_view_publisher.capture() is last_good


def test_missing_base_file_fails_closed(tmp_path, monkeypatch):
    client, paths, _snapshots = _unpopulated_fixture(tmp_path, monkeypatch)
    last_good = client._cache_view_publisher.capture()
    Path(read_manifest(paths, required=True).base_snapshot_path).unlink()
    assert _warm_load(client) is False
    assert "CoherenceStateMissingError" in client._nodes_export_lineage_error
    assert client._cache_view_publisher.capture() is last_good and last_good.epoch_id == "0" * 32


def test_reload_fails_loud_when_basis_vanishes_with_unchanged_manifest(tmp_path, monkeypatch):
    """A newer retained basis vanishes: no rotation restart/fallback, last good populated view stays."""
    client, paths, snapshots = _unpopulated_fixture(tmp_path, monkeypatch)
    original = read_manifest(paths, required=True)
    last_good = client._publish_lineage_view({"nodes": [s02_node("OLD", name="older")]},
        epoch_number=original.epoch_number, epoch_id=original.epoch_id,
        adopted_sequence=0, snapshot_timestamp=NOW)
    candidate = write_snapshot_candidate(new_snapshot_candidate_path(snapshots), {"nodes": BASE_NODES},
        account_scope_id=paths.account_root.name, epoch_number=original.epoch_number + 1,
        epoch_id="f" * 32, incorporated_through_seq=0, publication_kind="full_refresh")
    write_manifest(paths, manifest_with_updates(original, epoch_number=original.epoch_number + 1,
        epoch_id="f" * 32, previous_epoch_id=original.epoch_id, base_snapshot_path=candidate),
        expected_epoch_id=original.epoch_id, expected_head_seq=0)
    Path(candidate).unlink()
    assert _warm_load(client) is False
    assert "CoherenceStateMissingError" in client._nodes_export_lineage_error
    assert client._cache_view_publisher.capture() is last_good
    assert last_good.nodes_by_id["OLD"]["name"] == "older"
