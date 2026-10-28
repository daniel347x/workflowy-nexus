"""S04 epoch rollover through real warm S2 and the local publisher, not peer adoption."""
from __future__ import annotations

import asyncio
import os

from coherence_harness import (assert_semantic_cache_equal, s02_node, s04_append,
    s04_append_n, s04_checkpoint_config)
from mutation_lease_harness import make_lease_env
from workflowy_mcp.coherence import SemanticEffect, read_manifest, run_checkpoint_rotation, snapshot_store


def test_old_process_reloads_new_epoch_and_converges(tmp_path):
    """Historical process name: explicit warm rebuild crosses the real rotation and later tail."""
    env = make_lease_env(tmp_path, nodes=[s02_node("a"), s02_node("b", parent_id="a")])
    s04_append_n(env.paths, 3)
    assert env.client._load_nodes_export_cache_from_disk()
    first = env.publisher.capture()
    assert first.adopted_sequence == 3
    assert first.epoch_id == read_manifest(env.paths, required=True).epoch_id
    result = asyncio.run(run_checkpoint_rotation(env.paths, snapshot_dir=env.snapshot_dir,
        config=s04_checkpoint_config(record_threshold=2)))
    assert result.status == "published", result.warnings
    manifest = read_manifest(env.paths, required=True)
    assert env.client._load_nodes_export_cache_from_disk()
    second = env.publisher.capture()
    assert (second.epoch_id, second.epoch_number, second.adopted_sequence) == (
        manifest.epoch_id, manifest.epoch_number, manifest.journal_head_seq)
    assert set(second.nodes_by_id) == {"a", "b"}
    assert first.epoch_id != second.epoch_id and first.adopted_sequence == 3
    s04_append(env.paths, effects=[SemanticEffect.node_create(s02_node("c", parent_id="a"))],
        transaction_id="tx-post-rotation")
    assert env.client._load_nodes_export_cache_from_disk()
    third = env.publisher.capture()
    assert "c" in third.nodes_by_id and "c" not in second.nodes_by_id
    assert env.client._load_nodes_export_cache_from_disk()
    assert env.publisher.capture() is third  # Equal-marker local publication preserves identity.


def test_duplicate_replay_is_idempotent_across_rotation(tmp_path):
    """S2 produces the same verified after-state twice before and after real rotation."""
    env = make_lease_env(tmp_path, nodes=[s02_node("a")])
    s04_append_n(env.paths, 2)
    before = env.publisher.capture()
    for rotated in (False, True):
        if rotated:
            result = asyncio.run(run_checkpoint_rotation(env.paths, snapshot_dir=env.snapshot_dir,
                config=s04_checkpoint_config(record_threshold=2)))
            assert result.status == "published"
        manifest = read_manifest(env.paths, required=True)
        first = snapshot_store.materialize_current_payload(env.paths, manifest)
        second = snapshot_store.materialize_current_payload(env.paths, manifest)
        if os.environ.get("CUT2B_NEUTRALIZE_REPLAY_IDEMPOTENCE"):
            second.payload["nodes"].append(dict(second.payload["nodes"][0]))
        assert len(second.payload["nodes"]) == len(first.payload["nodes"]), "replay duplicated a child"
        assert_semantic_cache_equal(first.payload, second.payload)
        assert env.client._load_nodes_export_cache_from_disk()
        assert_semantic_cache_equal(env.publisher.capture().nodes_export, first.payload)
    assert before.adopted_sequence == 0


def test_rotation_preserves_journal_effects_in_reloaded_base(tmp_path, monkeypatch):
    env = make_lease_env(tmp_path, nodes=[s02_node("a")])
    s04_append(env.paths, effects=[SemanticEffect.node_fields("a", ["name"], {"name": "renamed-a"})],
        transaction_id="tx-update")
    s04_append(env.paths, effects=[SemanticEffect.node_create(s02_node("b", parent_id="a"))],
        transaction_id="tx-create")
    result = asyncio.run(run_checkpoint_rotation(env.paths, snapshot_dir=env.snapshot_dir,
        config=s04_checkpoint_config(record_threshold=1)))
    assert result.status == "published", result.warnings
    s04_append(env.paths, effects=[SemanticEffect.node_create(s02_node("c", parent_id="a"))],
        transaction_id="tx-tail")
    if os.environ.get("CUT2B_NEUTRALIZE_REBUILD_TAIL"):
        monkeypatch.setattr(snapshot_store, "read_journal_records", lambda *args, **kwargs: [])
    assert env.client._load_nodes_export_cache_from_disk()
    view = env.publisher.capture()
    assert view.nodes_by_id["a"]["name"] == "renamed-a"
    assert {"b", "c"} <= view.nodes_by_id.keys()
