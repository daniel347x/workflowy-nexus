"""Setup reuse must keep real route inputs and independent files, not cache results."""

import asyncio
import copy
from pathlib import Path

from n01_copy_fixture import fixture_identity, nid
from test_coherence_s03_transition_cow import _make_client
from test_ws_reader_nonblocking_coherence_replay import _SeedTemplate, _seed
from workflowy_mcp.coherence import read_manifest
from workflowy_mcp.coherence.snapshot_store import materialize_current_payload


def test_copied_seed_matches_fresh_inputs_and_cannot_poison_the_template(tmp_path, monkeypatch):
    # The five-record pattern's full geometry, with short text for this fixture
    # unit test. The performance witnesses still use the unchanged 67MB fixture.
    indexes = [0, 41, *range(42, 156), 5000]
    payload = {"nodes": [
        {"id": nid(i), "name": f"fixture {i}", "note": "fixture text",
         "parent_id": nid(41) if 42 <= i <= 152 else None, "priority": i}
        for i in indexes
    ]}
    clients = []

    def fresh(root, count, template=None, seed_payload=payload):
        client, _, snapshots = _make_client(root, monkeypatch)
        clients.append(client)
        assert client._cache_view_publisher.capture().nodes_export == {"nodes": []}
        paths, records = _seed(client, root, snapshots, seed_payload, count, template=template)
        assert client._cache_view_publisher.capture().nodes_export == {"nodes": []}
        return paths, records, read_manifest(paths, required=True)

    try:
        paths, _, manifest = fresh(tmp_path / "template", 10)
        template = _SeedTemplate(manifest.account_scope_id, Path(manifest.base_snapshot_path),
                                 paths.journal_dir, 10, fixture_identity(payload))
        base_bytes = template.base.read_bytes()
        record_bytes = {path.name: path.read_bytes() for path in template.journal.iterdir()}
        for count in (3, 10, 11):
            copied, records, copied_manifest = fresh(tmp_path / f"copy-{count}", count, template)
            original, expected_records, original_manifest = fresh(tmp_path / f"fresh-{count}", count)
            assert [record.sequence for record in records] == [record.sequence for record in expected_records]
            assert copied_manifest.journal_head_seq == original_manifest.journal_head_seq == count
            assert copied_manifest.journal_floor_seq == original_manifest.journal_floor_seq == 1
            assert copied_manifest.base_incorporated_through_seq == 0
            assert copied_manifest.epoch_id == original_manifest.epoch_id == "b" * 32
            assert not copied_manifest.dirty_status.active
            assert Path(copied_manifest.base_snapshot_path).is_relative_to(tmp_path / f"copy-{count}")
            if count <= template.count:
                assert Path(copied_manifest.base_snapshot_path).read_bytes() == base_bytes
                assert {path.name: path.read_bytes() for path in copied.journal_dir.iterdir()} == {
                    name: data for name, data in record_bytes.items() if int(Path(name).stem) <= count
                }
            else:
                assert len(list(copied.journal_dir.iterdir())) == count  # real-writer fallback
            assert materialize_current_payload(copied, copied_manifest).payload == \
                materialize_current_payload(original, original_manifest).payload
            # Real checkpoint retirement unlinks records. A future in-place write
            # must be isolated too, so exercise both kinds of fixture damage.
            first = copied.journal_dir / "00000000000000000001.json"
            first.write_bytes(b"damaged private copy")
            first.unlink()
            Path(copied_manifest.base_snapshot_path).write_bytes(b"damaged private base")
            assert template.base.read_bytes() == base_bytes
            assert {path.name: path.read_bytes() for path in template.journal.iterdir()} == record_bytes
        different = copy.deepcopy(payload)
        different["nodes"][0]["name"] = "a different base payload"
        changed, _, changed_manifest = fresh(tmp_path / "different-payload", 3, template, different)
        changed_nodes = materialize_current_payload(changed, changed_manifest).payload["nodes"]
        assert next(row for row in changed_nodes if row["id"] == nid(0))["name"] == "a different base payload"
    finally:
        async def close():
            for client in clients:
                await client.close()
        asyncio.run(close())
