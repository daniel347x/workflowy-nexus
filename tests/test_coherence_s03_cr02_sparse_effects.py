"""S03 transition-effect repairs and pre-S07 closure evidence.

The original 7d8e7cd work order repaired simple field-write adoption and
recovery.  This file now also covers the follow-up Family-5 compound-create,
repeated-write, and complete-subtree-delete findings.  CF-S03CR-10 remains
open in the program ledger until independent audit verifies this evidence.

Covered contracts:

- WO-1 / Contract D — newer lineage offers through the real local publisher
  rebase through the SAME sparse merge authority and retain verified COW.
- WO-2 / Contracts C/E — per-field authority, including post-create
  sparse writes and repeated-write causal chains, plus subtree-delete scope
  that expands over descendants present only in a newer offer/recovery view.
  Unrelated offered metadata survives and every non-clean resolution leaves
  legacy-dirty evidence.
- WO-3 / Contracts A/B — generation pinning (the reverse-order test lives
  in ``test_coherence_s03_closure_repairs.py``; this file adds the
  per-field and composition matrices).
- WO-4 / CF-S03CR-10 — explicit warm S2 rebuild converges with racing COW,
  through a test-only synchronous callback before the real local publication.

No test touches real Workflowy, broker, governance, or cache state (the
session-wide test-runtime isolation seam covers spawned subprocesses).
"""

from __future__ import annotations

import asyncio
import json
import os
from functools import wraps
from pathlib import Path

from coherence_harness import (
    NOW,
    s02_node,
)

from workflowy_mcp.coherence import (
    JournalProducer,
    SemanticEffect,
    append_journal_transaction,
    initialize_manifest_if_absent,
    manifest_with_updates,
    new_bootstrap_manifest,
    read_manifest,
    write_manifest,
)
from workflowy_mcp.coherence import snapshot_store
from coherence_harness import publish_fixture_head
from test_coherence_s03_adoption import _neutralize_merge
from workflowy_mcp.coherence.snapshot_store import (
    new_snapshot_candidate_path,
    write_snapshot_candidate,
)


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


BASE_NODES = [
    s02_node("A", name="alpha"),
    s02_node("B", name="beta", parent_id="A"),
    s02_node("C", name="gamma", parent_id="B"),
]


# ---------------------------------------------------------------------------
# Client factories (isolated jobs root + snapshot dir, real client semantics)
# ---------------------------------------------------------------------------


def _make_client(tmp_path: Path, monkeypatch):
    from pydantic import SecretStr

    from workflowy_mcp.client.api_client_core import WorkFlowyClientCore
    from workflowy_mcp.models import APIConfiguration

    jobs_root = tmp_path / "cartographer_jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "workflowy_mcp.carto_job_governance.get_cartographer_jobs_dir",
        lambda: jobs_root,
    )

    def _runtime_subdir(*parts):
        if parts == ("nodes_export_cache",):
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            return snapshot_dir
        return tmp_path.joinpath(*parts)

    monkeypatch.setattr(
        "workflowy_mcp.client.api_client_core.get_runtime_subdir", _runtime_subdir
    )
    config = APIConfiguration(
        api_key=SecretStr("test-key-1234"), base_url="https://workflowy.com/api/v1"
    )
    return WorkFlowyClientCore(config), jobs_root, snapshot_dir


def _populate(client, nodes=BASE_NODES, *, epoch_id: str = "e" * 32, seq: int = 0):
    return client._publish_lineage_view(
        {"nodes": [dict(n) for n in nodes]},
        epoch_number=1,
        epoch_id=epoch_id,
        adopted_sequence=seq,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )


def _node_model(node_id: str, **fields):
    from workflowy_mcp.models import WorkFlowyNode

    return WorkFlowyNode(id=node_id, **fields)


def _seed_lineage(probe, snapshot_dir: Path, nodes=BASE_NODES):
    paths = probe._coherence_paths()
    manifest = initialize_manifest_if_absent(
        paths, new_bootstrap_manifest(paths.account_root.name, now=NOW)
    )
    candidate = write_snapshot_candidate(
        new_snapshot_candidate_path(snapshot_dir),
        {"nodes": [dict(n) for n in nodes]},
        account_scope_id=paths.account_root.name,
        epoch_number=manifest.epoch_number,
        epoch_id=manifest.epoch_id,
        incorporated_through_seq=0,
        publication_kind="full_refresh",
    )
    write_manifest(
        paths,
        manifest_with_updates(
            manifest, base_snapshot_path=candidate, coherence_mode="transition"
        ),
        expected_epoch_id=manifest.epoch_id,
        expected_head_seq=0,
    )
    return paths


def _make_lineage_client(tmp_path: Path, monkeypatch, nodes=BASE_NODES):
    probe, _jr, snapshot_dir = _make_client(tmp_path, monkeypatch)
    paths = _seed_lineage(probe, snapshot_dir, nodes)
    client, _jr2, _sd2 = _make_client(tmp_path, monkeypatch)
    return client, paths


def _cut_epoch(
    paths,
    tmp_path: Path,
    new_nodes,
    *,
    new_epoch_id: str = "f" * 32,
    publication_kind: str = "full_refresh",
):
    """Simulate an S02 epoch cut by ANOTHER publisher: new base + new epoch
    identity on the manifest (previous_epoch_id recorded, floor advanced)."""

    manifest = read_manifest(paths, required=True)
    candidate = write_snapshot_candidate(
        new_snapshot_candidate_path(tmp_path / "snapshots"),
        {"nodes": [dict(n) for n in new_nodes]},
        account_scope_id=paths.account_root.name,
        epoch_number=manifest.epoch_number + 1,
        epoch_id=new_epoch_id,
        incorporated_through_seq=0,
        publication_kind=publication_kind,
    )
    write_manifest(
        paths,
        manifest_with_updates(
            manifest,
            epoch_number=manifest.epoch_number + 1,
            epoch_id=new_epoch_id,
            previous_epoch_id=manifest.epoch_id,
            base_snapshot_path=candidate,
            base_incorporated_through_seq=0,
            journal_floor_seq=1,
            journal_head_seq=0,
        ),
        expected_epoch_id=manifest.epoch_id,
        expected_head_seq=manifest.journal_head_seq,
    )
    return read_manifest(paths, required=True)


def _producer() -> JournalProducer:
    return JournalProducer(
        pid=os.getpid(), pid_create_time=1.0, owner_kind="process", ownership_kind="none"
    )


def _append(paths, effects):
    return append_journal_transaction(
        paths, operation_kind="test", effects=effects, producer=_producer()
    )


# ===========================================================================
# WO-1 — ordinary/cross-process epoch adoption preserves the sparse effect
# ===========================================================================


@async_test
async def test_ordinary_epoch_reload_adoption_carries_sparse_effect(
    tmp_path, monkeypatch
):
    """Historical adoption name: a newer real S2 offer uses the surviving local sparse publisher."""
    client, paths = _make_lineage_client(tmp_path, monkeypatch)
    client._apply_update_to_nodes_export_cache(_node_model("A", name="local-write"))
    assert set(client._transition_cow_effects) == {"A"}

    _cut_epoch(
        paths,
        tmp_path,
        [
            s02_node("A", name="alpha"),  # stale: predates the write
            s02_node("B", name="beta-b-new", parent_id="A"),  # unrelated newer content
            s02_node("C", name="gamma", parent_id="B"),
        ],
    )

    _neutralize_merge(monkeypatch, "CUT2B_NEUTRALIZE_PUBLISH_MERGE")
    view = publish_fixture_head(client, paths)
    assert view.epoch_number == 1
    assert view.nodes_by_id["A"]["name"] == "local-write"  # owned field carried
    assert view.nodes_by_id["B"]["name"] == "beta-b-new"  # offer adopted otherwise
    assert set(client._transition_cow_effects) == {"A"}  # still unreflected
    assert "A" in client._nodes_export_dirty_ids  # carry evidence marked


@async_test
async def test_cross_process_epoch_publication_preserves_local_cow(
    tmp_path, monkeypatch
):
    """Historical process name: a publisher task offers the newer epoch; sparse COW survives."""
    client, paths = _make_lineage_client(tmp_path, monkeypatch)
    client._apply_update_to_nodes_export_cache(_node_model("A", name="local-write"))

    async def publish_offer():
        await asyncio.sleep(0)
        _cut_epoch(paths, tmp_path, [s02_node("A", name="alpha"),
            s02_node("B", name="beta-b-new", parent_id="A")])
        return publish_fixture_head(client, paths)

    _neutralize_merge(monkeypatch, "CUT2B_NEUTRALIZE_PUBLISH_MERGE")
    view = await asyncio.create_task(publish_offer())
    assert view.epoch_number == 1
    assert view.epoch_id == "f" * 32
    assert view.nodes_by_id["A"]["name"] == "local-write"
    assert view.nodes_by_id["B"]["name"] == "beta-b-new"
    assert set(client._transition_cow_effects) == {"A"}
    assert "A" in client._nodes_export_dirty_ids


# ===========================================================================
# WO-2 — per-field authority matrix (never whole-node replacement)
# ===========================================================================


@async_test
async def test_per_field_authority_matrix(tmp_path, monkeypatch):
    """WO-2/Contract C: each narrow mutation shape owns exactly its fields.
    A newer offer stale on the OWNED fields but newer on UNRELATED fields
    must carry the owned fields and preserve the unrelated newer content
    (name / note / parent / priority / completion / timestamps / unknown
    metadata).  Discriminates against whole-node replacement: under that
    mutation the unrelated assertions fail."""
    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    _populate(client)

    # name-only (the true narrow patcher — owns ONLY name).
    await client.update_cached_node_name("A", "local-name")
    # note-only (owns ONLY note).
    await client.update_cached_node_note("B", "local-note")
    # parent-only move (owns ONLY parentage).
    client._apply_move_to_nodes_export_cache("C", "Z-parent")
    # completion-only via the verified-fetch update patcher.
    client._apply_update_to_nodes_export_cache(
        _node_model("A", completedAt=123, completed_flag=True)
    )

    published = client._publish_lineage_view(
        {
            "nodes": [
                # Stale on every locally-owned field; NEWER everywhere else:
                # priority (owned vocabulary but NOT this effect's mask),
                # timestamps, data, and unknown cache-only metadata.
                s02_node(
                    "A",
                    name="alpha",
                    parent_id=None,
                    priority=999,
                    modifiedAt=777,
                    data={"gui": 1},
                    custom_meta="kept",
                ),
                s02_node("B", name="beta", parent_id="A", priority=888),
                s02_node("C", name="gamma-renamed", parent_id="B", priority=777),
            ]
        },
        epoch_number=2,
        epoch_id="f" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )

    node_a = published.nodes_by_id["A"]
    # Locally-owned fields carried: name (name-write) + completion fields
    # (completion update, verified by the fetch model).
    assert node_a["name"] == "local-name"
    assert node_a["completed"] is True
    assert node_a["completedAt"] == 123
    # Unrelated newer fields PRESERVED from the offer (whole-node
    # replacement would have regressed every one of these):
    assert node_a["priority"] == 999
    assert node_a["modifiedAt"] == 777
    assert node_a["data"] == {"gui": 1}
    assert node_a["custom_meta"] == "kept"

    node_b = published.nodes_by_id["B"]
    assert node_b["note"] == "local-note"  # owned: carried
    assert node_b["priority"] == 888  # unrelated: offer wins

    node_c = published.nodes_by_id["C"]
    assert node_c["parent_id"] == "Z-parent"  # owned: carried
    assert node_c["name"] == "gamma-renamed"  # unrelated: offer wins
    assert node_c["priority"] == 777  # unrelated: offer wins

    # Every carried effect leaves refresh-required evidence until reflected.
    assert client._nodes_export_dirty_ids >= {"A", "B", "C"}
    surviving = client._transition_cow_effects
    assert set(surviving["A"].fields) == {"name", "completed", "completedAt"}
    assert set(surviving["B"].fields) == {"note"}
    assert set(surviving["C"].fields) == {"parent_id"}


def test_delete_effect_carries_subtree_and_retires_on_reflection(tmp_path, monkeypatch):
    """WO-2 (delete authority): a local delete owns ABSENCE for the whole
    subtree; a stale offer re-presenting the nodes loses them; the first
    offer that agrees retires the tombstones."""
    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    _populate(client)  # A -> B -> C
    client._apply_delete_to_nodes_export_cache("B")  # removes B AND C
    assert set(client._transition_cow_effects) == {"B", "C"}

    published = client._publish_lineage_view(
        {"nodes": [dict(n) for n in BASE_NODES]},  # stale: still has B and C
        epoch_number=2,
        epoch_id="f" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )
    assert "B" not in published.nodes_by_id
    assert "C" not in published.nodes_by_id
    assert client._nodes_export_dirty_ids >= {"B", "C"}

    # The next offer agrees they are gone: tombstones retire, state clean.
    reflected = client._publish_lineage_view(
        {"nodes": [s02_node("A", name="alpha")]},
        epoch_number=3,
        epoch_id="1" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )
    assert "B" not in reflected.nodes_by_id
    assert client._transition_cow_effects == {}
    assert "B" not in client._nodes_export_dirty_ids


# ===========================================================================
# Overlay composition (publication order, deterministic)
# ===========================================================================


@async_test
async def test_overlay_composition_rules(tmp_path, monkeypatch):
    """Contract C composition: multiple local COW effects compose
    deterministically in publication order — earliest prior / latest
    verified on overlap, create+update stays a create, create+delete is a
    tombstone — and unrelated offered metadata survives the merge."""
    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    _populate(client, nodes=BASE_NODES[:2])  # A, B only

    # Different fields on the same node + repeated writes to one field.
    await client.update_cached_node_name("A", "n1")
    await client.update_cached_node_note("A", "note-1")
    await client.update_cached_node_name("A", "n2")  # repeated field
    # Move plus content update on B.
    client._apply_move_to_nodes_export_cache("B", "Z-parent")
    await client.update_cached_node_name("B", "b-renamed")
    # Create then update (creation authority and later sparse write stay distinct).

    client._apply_create_to_nodes_export_cache(_node_model("N", name="created"), parent_id="A")
    await client.update_cached_node_name("N", "n-renamed")
    # Create then delete (a tombstone).
    client._apply_create_to_nodes_export_cache(_node_model("M", name="ephemeral"), parent_id="A")
    client._apply_delete_to_nodes_export_cache("M")

    effects = client._transition_cow_effects
    assert effects["A"].kind == "fields"
    assert effects["A"].fields["name"].prior == "alpha"  # earliest prior
    assert effects["A"].fields["name"].verified == "n2"  # latest verified
    assert effects["A"].fields["note"].verified == "note-1"
    assert effects["B"].fields["parent_id"].verified == "Z-parent"
    assert effects["B"].fields["name"].verified == "b-renamed"
    assert effects["N"].kind == "create"
    assert effects["N"].record["name"] == "created"
    assert effects["N"].fields["name"].prior == "created"
    assert effects["N"].fields["name"].verified == "n-renamed"

    assert effects["M"].kind == "delete"

    published = client._publish_lineage_view(
        {
            "nodes": [  # stale on all owned fields; carries unknown metadata
                s02_node("A", name="alpha", custom_meta="kept"),
                s02_node("B", name="beta", parent_id="A"),
            ]
        },
        epoch_number=2,
        epoch_id="f" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )

    node_a = published.nodes_by_id["A"]
    assert node_a["name"] == "n2"
    assert node_a["note"] == "note-1"
    assert node_a["custom_meta"] == "kept"  # offered metadata preserved
    node_b = published.nodes_by_id["B"]
    assert node_b["parent_id"] == "Z-parent"
    assert node_b["name"] == "b-renamed"
    assert published.nodes_by_id["N"]["name"] == "n-renamed"  # create carried
    assert "M" not in published.nodes_by_id  # create+delete => absent


# ===========================================================================
# Contract E — a later legitimate server value is never masked forever
# ===========================================================================


def test_later_server_value_retires_effect_and_survives_dirty(tmp_path, monkeypatch):
    """Contract E (the Orange/Oranges case): local verified value = Orange;
    a later authoritative offer carries Oranges.  The offer WINS, the
    effect retires, dirty evidence survives — and the NEXT offer is clean
    (Orange never silently replaces Oranges on future publications)."""
    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    _populate(client)
    client._apply_update_to_nodes_export_cache(_node_model("A", name="Orange"))

    first = client._publish_lineage_view(
        {
            "nodes": [
                s02_node("A", name="Oranges", completed=False),  # later server value
                s02_node("B", name="beta", parent_id="A", completed=False),
            ]
        },
        epoch_number=2,
        epoch_id="f" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )
    assert first.nodes_by_id["A"]["name"] == "Oranges"  # offer wins the conflict
    assert client._transition_cow_effects == {}  # effect retired (never re-applied)
    assert "A" in client._nodes_export_dirty_ids  # conflict evidence survives

    # The next authoritative publication is clean: nothing masks it.
    second = client._publish_lineage_view(
        {
            "nodes": [
                s02_node("A", name="Oranges", completed=False),
                s02_node("B", name="beta", parent_id="A", completed=False),
            ]
        },
        epoch_number=3,
        epoch_id="1" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )
    assert second.nodes_by_id["A"]["name"] == "Oranges"
    assert "A" not in client._nodes_export_dirty_ids


# ===========================================================================
# One-shot audit closure — compound create, repeated writes, subtree delete
# ===========================================================================


@async_test
async def test_post_create_sparse_write_matrix_survives_intermediate_offer(
    tmp_path, monkeypatch
):
    """F-1: creation reflection cannot retire a later sparse local write.

    Each offer was captured after create and before the named local write.
    The offer's unrelated metadata must survive while the later local field
    remains carried with dirty evidence.
    """

    cases = ("name", "note", "move", "completion")
    for index, case in enumerate(cases):
        case_root = tmp_path / case
        case_root.mkdir()
        client, _jr, _sd = _make_client(case_root, monkeypatch)
        _populate(client, nodes=[s02_node("A", name="parent")])
        client._apply_create_to_nodes_export_cache(
            _node_model(
                "N",
                name="first",
                note="initial-note",
                completed_flag=False,
            ),
            parent_id="A",
        )
        offered_n = dict(client._cache_view_publisher.capture().nodes_by_id["N"])
        offered_n["modifiedAt"] = 900 + index
        offered_n["offered_meta"] = case

        if case == "name":
            await client.update_cached_node_name("N", "second")
            expected_fields = {"name"}
        elif case == "note":
            await client.update_cached_node_note("N", "second-note")
            expected_fields = {"note"}
        elif case == "move":
            client._apply_move_to_nodes_export_cache("N", "Z")
            expected_fields = {"parent_id"}
        else:
            client._apply_update_to_nodes_export_cache(
                _node_model("N", completedAt=123, completed_flag=True)
            )
            expected_fields = {"completed", "completedAt"}

        published = client._publish_lineage_view(
            {"nodes": [s02_node("A", name="parent"), offered_n]},
            epoch_number=2,
            epoch_id=f"{index + 1:x}" * 32,
            adopted_sequence=0,
            snapshot_timestamp=NOW,
            account_scope_id=client._safe_account_scope_id(),
            clear_dirty=True,
        )
        node = published.nodes_by_id["N"]
        if case == "name":
            assert node["name"] == "second"
        elif case == "note":
            assert node["note"] == "second-note"
        elif case == "move":
            assert node["parent_id"] == "Z"
        else:
            assert node["completed"] is True
            assert node["completedAt"] == 123
        assert node["modifiedAt"] == 900 + index
        assert node["offered_meta"] == case
        effect = client._transition_cow_effects["N"]
        assert effect.kind == "fields"  # creation reflected; later write remains
        assert set(effect.fields) == expected_fields
        assert "N" in client._nodes_export_dirty_ids


@async_test
async def test_actual_epoch_adoption_preserves_post_create_write(
    tmp_path, monkeypatch
):
    """F-1 local publication: offer captured after create/before update carries the later field write."""

    client, paths = _make_lineage_client(
        tmp_path, monkeypatch, nodes=[s02_node("A", name="parent")]
    )
    client._apply_create_to_nodes_export_cache(
        _node_model("N", name="first"), parent_id="A"
    )
    delayed_offer = [
        dict(node) for node in client._cache_view_publisher.capture().nodes_export["nodes"]
    ]
    await client.update_cached_node_name("N", "second")
    _cut_epoch(paths, tmp_path, delayed_offer)

    _neutralize_merge(monkeypatch, "CUT2B_NEUTRALIZE_PUBLISH_MERGE")
    view = publish_fixture_head(client, paths)
    assert view.nodes_by_id["N"]["name"] == "second"
    assert client._transition_cow_effects["N"].kind == "fields"
    assert set(client._transition_cow_effects["N"].fields) == {"name"}
    assert "N" in client._nodes_export_dirty_ids


@async_test
async def test_post_create_multiple_fields_remain_sparse(tmp_path, monkeypatch):
    """F-1 symmetric matrix: create followed by content, move, completion,
    and repeated writes carries only those later fields over an intermediate
    post-create offer; offered metadata remains authoritative."""

    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    _populate(client, nodes=[s02_node("A", name="parent")])
    client._apply_create_to_nodes_export_cache(
        _node_model("N", name="first", note="note-1", completed_flag=False),
        parent_id="A",
    )
    offered_n = dict(client._cache_view_publisher.capture().nodes_by_id["N"])
    offered_n["data"] = {"offered": True}
    offered_n["custom_meta"] = "keep"

    await client.update_cached_node_name("N", "second")
    await client.update_cached_node_name("N", "third")
    await client.update_cached_node_note("N", "note-2")
    client._apply_move_to_nodes_export_cache("N", "Z")
    client._apply_update_to_nodes_export_cache(
        _node_model("N", completedAt=456, completed_flag=True)
    )

    published = client._publish_lineage_view(
        {"nodes": [s02_node("A", name="parent"), offered_n]},
        epoch_number=2,
        epoch_id="f" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )
    node = published.nodes_by_id["N"]
    assert node["name"] == "third"
    assert node["note"] == "note-2"
    assert node["parent_id"] == "Z"
    assert node["completed"] is True
    assert node["completedAt"] == 456
    assert node["data"] == {"offered": True}
    assert node["custom_meta"] == "keep"
    effect = client._transition_cow_effects["N"]
    assert effect.kind == "fields"
    assert set(effect.fields) == {
        "name",
        "note",
        "parent_id",
        "completed",
        "completedAt",
    }
    assert effect.fields["name"].prior == "first"
    assert "second" in effect.fields["name"].intermediate
    assert effect.fields["name"].verified == "third"


@async_test
async def test_repeated_field_write_intermediate_offer_carries_latest(
    tmp_path, monkeypatch
):
    """F-1 adjacent shape: Apple→Orange→Oranges with a delayed offer at
    Orange.  Orange is a known local predecessor, not a later conflict, so
    Oranges survives with effect + dirty evidence."""

    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    _populate(client, nodes=[s02_node("A", name="Apple")])
    await client.update_cached_node_name("A", "Orange")
    intermediate_offer = dict(client._cache_view_publisher.capture().nodes_by_id["A"])
    intermediate_offer["custom_meta"] = "offered"
    await client.update_cached_node_name("A", "Oranges")

    published = client._publish_lineage_view(
        {"nodes": [intermediate_offer]},
        epoch_number=2,
        epoch_id="f" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )
    assert published.nodes_by_id["A"]["name"] == "Oranges"
    assert published.nodes_by_id["A"]["custom_meta"] == "offered"
    write = client._transition_cow_effects["A"].fields["name"]
    assert write.prior == "Apple"
    assert write.intermediate == ("Orange",)
    assert write.verified == "Oranges"
    assert "A" in client._nodes_export_dirty_ids


def test_subtree_delete_removes_unknown_offered_descendant_cycle_safely(
    tmp_path, monkeypatch
):
    """F-2: a delete rooted at B removes offered descendant D even though
    the stale local basis knew only B→C.  A B↔D cycle terminates safely and
    unrelated X remains."""

    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    _populate(client)  # local A -> B -> C; D is unknown locally
    client._apply_delete_to_nodes_export_cache("B")

    published = client._publish_lineage_view(
        {
            "nodes": [
                s02_node("A", name="alpha"),
                s02_node("B", name="beta", parent_id="D"),
                s02_node("C", name="gamma", parent_id="B"),
                s02_node("D", name="unknown", parent_id="B"),
                s02_node("X", name="outside", parent_id="A"),
            ]
        },
        epoch_number=2,
        epoch_id="f" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )
    assert set(published.nodes_by_id) == {"A", "X"}
    # F-3 root-scoped representation: absence authority lives on root B; C's
    # descendant marker retires at first merge (it never carries authority).
    assert set(client._transition_cow_effects) == {"B"}
    assert client._nodes_export_dirty_ids >= {"B", "C", "D"}

    reflected = client._publish_lineage_view(
        {
            "nodes": [
                s02_node("A", name="alpha"),
                s02_node("X", name="outside", parent_id="A"),
            ]
        },
        epoch_number=3,
        epoch_id="1" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )
    assert set(reflected.nodes_by_id) == {"A", "X"}
    assert client._transition_cow_effects == {}
    assert not ({"B", "C", "D"} & client._nodes_export_dirty_ids)


# ===========================================================================
# F-3 — root-authoritative tombstones: escaped descendants are never removed
# ===========================================================================


def test_escaped_descendant_survives_delete_and_tombstone_retires(
    tmp_path, monkeypatch
):
    """F-3 (ordinary offer): C escaped B's subtree before the local delete
    committed.  The offer has B absent and C alive under A: C and unrelated X
    must survive, the B tombstone must retire, no surviving effect may pin C,
    and a SECOND identical offer must be stable with zero delete-caused dirt.
    """

    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    _populate(client)  # local basis A -> B -> C (stale: C already moved to A)
    client._apply_delete_to_nodes_export_cache("B")

    escaped_offer = {
        "nodes": [
            s02_node("A", name="alpha"),
            s02_node("C", name="gamma", parent_id="A"),  # escaped: alive under A
            s02_node("X", name="outside", parent_id="A"),
        ]
    }
    first = client._publish_lineage_view(
        {"nodes": [dict(n) for n in escaped_offer["nodes"]]},
        epoch_number=2,
        epoch_id="f" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )
    assert set(first.nodes_by_id) == {"A", "C", "X"}
    assert first.nodes_by_id["C"]["parent_id"] == "A"
    assert client._transition_cow_effects == {}  # B tombstone retired; C unpinned

    second = client._publish_lineage_view(
        {"nodes": [dict(n) for n in escaped_offer["nodes"]]},
        epoch_number=3,
        epoch_id="1" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )
    assert set(second.nodes_by_id) == {"A", "C", "X"}  # stable: C never re-removed
    assert client._transition_cow_effects == {}
    assert not ({"B", "C"} & client._nodes_export_dirty_ids)  # no dirt churn


def test_escaped_descendant_with_added_unknown_descendant(tmp_path, monkeypatch):
    """F-3 + F-2 combined: C escaped to A while unknown D was added beneath B.
    C must survive; B and D must be absent (root authority removes exactly the
    offered subtree beneath B, no more and no less)."""

    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    _populate(client)  # local A -> B -> C
    client._apply_delete_to_nodes_export_cache("B")

    published = client._publish_lineage_view(
        {
            "nodes": [
                s02_node("A", name="alpha"),
                s02_node("C", name="gamma", parent_id="A"),  # escaped
                s02_node("D", name="unknown", parent_id="B"),  # added beneath B
            ]
        },
        epoch_number=2,
        epoch_id="f" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )
    assert set(published.nodes_by_id) == {"A", "C"}
    assert published.nodes_by_id["C"]["parent_id"] == "A"
    # D was beneath the root, so the tombstone acted and survives with dirt.
    assert set(client._transition_cow_effects) == {"B"}
    assert client._nodes_export_dirty_ids >= {"B", "D"}
    assert "C" not in client._nodes_export_dirty_ids  # escaped C untouched


def test_removed_cycle_fallback_roots_component_once():
    """F-3 record-fidelity lock: a removed 2-ring {X↔Y} with no qualifying
    root yields exactly ONE root tombstone (the FIRST uncovered member in
    before-list order) plus ONE descendant marker naming it — never
    per-member self-roots.  Merging against an offer where the OTHER ring
    member is offered-alive OUTSIDE the root's offered subtree proves it
    survives and nothing pins.  This test FAILS if the fallback is changed
    to per-member self-root tombstones (Y would then carry its own absence
    authority: removed from the offer, pinned in the surviving store)."""

    from workflowy_mcp.coherence.cache_view import CacheView
    from workflowy_mcp.coherence.transition_effects import (
        derive_transition_effects,
        merge_offer_with_transition_effects,
    )

    before = [
        s02_node("A", name="alpha"),
        s02_node("X", name="ring-x", parent_id="Y"),  # first ring member
        s02_node("Y", name="ring-y", parent_id="X"),  # second ring member
    ]
    after = [s02_node("A", name="alpha")]  # both ring members locally removed

    effects = derive_transition_effects(before, after)
    assert set(effects) == {"X", "Y"}
    root_ids = {nid for nid, e in effects.items() if e.subtree_root in (None, nid)}
    assert root_ids == {"X"}  # exactly one root: the FIRST uncovered member
    assert effects["Y"].kind == "delete"
    assert effects["Y"].subtree_root == "X"  # descendant marker naming the root

    # Offer: Y escaped alive under A, OUTSIDE X's offered subtree; X absent.
    offer = CacheView.build(
        epoch_number=2,
        epoch_id="f" * 32,
        adopted_sequence=0,
        nodes_export={
            "nodes": [
                s02_node("A", name="alpha"),
                s02_node("Y", name="ring-y", parent_id="A"),  # offered-alive escapee
            ]
        },
        account_scope_id=None,
    )
    merged = merge_offer_with_transition_effects(offer, effects)
    assert "Y" in merged.rebased.nodes_by_id  # the escapee survives
    assert merged.rebased.nodes_by_id["Y"]["parent_id"] == "A"
    assert dict(merged.surviving_effects) == {}  # nothing pins (root retired too)
    assert merged.dirty_ids == frozenset()


# ===========================================================================
# WO-S03-CLOSURE-F5-1 — authoritative retirement of orphaned CREATE effects
# ===========================================================================


def test_one_full_refresh_absence_carries_create(tmp_path, monkeypatch):
    """P1: the first full-refresh absence may be an in-flight export race."""

    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    _populate(client, nodes=[s02_node("A", name="parent")])
    client._apply_create_to_nodes_export_cache(
        _node_model("N", name="new"), parent_id="A"
    )

    first = client._publish_lineage_view(
        {"nodes": [s02_node("A", name="parent")]},
        epoch_number=2,
        epoch_id="f" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        publication_kind="full_refresh",
        clear_dirty=True,
    )

    assert "N" in first.nodes_by_id
    assert client._transition_cow_effects["N"].absent_full_refreshes == 1
    assert client._nodes_export_dirty_ids == {"N"}


def test_non_authoritative_absences_never_advance_create_counter(
    tmp_path, monkeypatch
):
    """P2: compat-patch/checkpoint/job-basis offers cannot prove absence."""

    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    _populate(client, nodes=[s02_node("A", name="parent")])
    client._apply_create_to_nodes_export_cache(
        _node_model("N", name="new"), parent_id="A"
    )

    for epoch_number, publication_kind in enumerate(
        ("compat_patch", "checkpoint", "job_basis"), start=2
    ):
        published = client._publish_lineage_view(
            {"nodes": [s02_node("A", name="parent")]},
            epoch_number=epoch_number,
            epoch_id=str(epoch_number) * 32,
            adopted_sequence=0,
            snapshot_timestamp=NOW,
            account_scope_id=client._safe_account_scope_id(),
            publication_kind=publication_kind,
            clear_dirty=True,
        )
        assert "N" in published.nodes_by_id
        assert client._transition_cow_effects["N"].absent_full_refreshes == 0


def test_new_local_write_resets_create_absence_evidence(tmp_path, monkeypatch):
    """A new local write restarts the one-full-refresh race allowance."""

    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    _populate(client, nodes=[s02_node("A", name="parent")])
    client._apply_create_to_nodes_export_cache(
        _node_model("N", name="new"), parent_id="A"
    )
    client._publish_lineage_view(
        {"nodes": [s02_node("A", name="parent")]},
        epoch_number=2,
        epoch_id="2" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        publication_kind="full_refresh",
    )
    assert client._transition_cow_effects["N"].absent_full_refreshes == 1

    asyncio.run(client.update_cached_node_name("N", "newer-local-write"))
    assert client._transition_cow_effects["N"].absent_full_refreshes == 0

    carried = client._publish_lineage_view(
        {"nodes": [s02_node("A", name="parent")]},
        epoch_number=3,
        epoch_id="3" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        publication_kind="full_refresh",
    )
    assert carried.nodes_by_id["N"]["name"] == "newer-local-write"
    assert client._transition_cow_effects["N"].absent_full_refreshes == 1


@async_test
async def test_cross_process_deleted_create_retires_after_second_full_refresh(
    tmp_path, monkeypatch
):
    """Historical process name: first full absence carries CREATE; second retires it dirty.

    The former peer-delete half remains in the real local delete-effect witness;
    this one publisher never rebuilds a peer adoption/liveness plane.
    """
    client, _paths = _make_lineage_client(tmp_path, monkeypatch, nodes=[s02_node("A", name="parent")])
    client._apply_create_to_nodes_export_cache(_node_model("N", name="new"), parent_id="A")
    def offer(number, kind):
        return client._publish_lineage_view({"nodes": [s02_node("A", name="parent")]},
            epoch_number=number, epoch_id=str(number) * 32, adopted_sequence=0,
            snapshot_timestamp=NOW, account_scope_id=client._safe_account_scope_id(),
            publication_kind=kind)
    # A nonauthoritative absence cannot count toward the two full observations.
    for number, kind in ((1, "compat_patch"), (2, "checkpoint")):
        assert "N" in offer(number, kind).nodes_by_id
        assert client._transition_cow_effects["N"].absent_full_refreshes == 0
    first = offer(3, "full_refresh")
    assert "N" in first.nodes_by_id and client._nodes_export_dirty_ids == {"N"}
    kind = "checkpoint" if os.environ.get("CUT2B_NEUTRALIZE_ABSENCE_AUTHORITY") else "full_refresh"
    second = offer(4, kind)
    assert "N" not in second.nodes_by_id
    assert "N" not in client._transition_cow_effects
    assert client._nodes_export_dirty_ids == {"N"}


def test_failed_publication_keeps_view_effects_and_dirty_evidence(
    tmp_path, monkeypatch
):
    """Failure atomicity: exhausted CAS publishes no partial epoch/view and
    retains the compound create+write effect plus pre-existing dirty evidence."""

    from workflowy_mcp.coherence.errors import CacheViewError

    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    _populate(client, nodes=[s02_node("A", name="parent")])
    client._apply_create_to_nodes_export_cache(
        _node_model("N", name="first"), parent_id="A"
    )
    asyncio.run(client.update_cached_node_name("N", "second"))
    # An absent offer carries the create and establishes durable local dirt.
    client._publish_lineage_view(
        {"nodes": [s02_node("A", name="parent")]},
        epoch_number=2,
        epoch_id="f" * 32,
        adopted_sequence=0,
        snapshot_timestamp=NOW,
        account_scope_id=client._safe_account_scope_id(),
        clear_dirty=True,
    )
    before_view = client._cache_view_publisher.capture()
    before_effects = client._transition_cow_effects
    before_dirty = frozenset(client._nodes_export_dirty_ids)
    assert before_effects["N"].kind == "create"
    assert set(before_effects["N"].fields) == {"name"}

    def _always_race(*_args, **_kwargs):
        raise CacheViewError("forced exhausted CAS")

    monkeypatch.setattr(
        client._cache_view_publisher, "publish_with_commit", _always_race
    )
    try:
        client._publish_lineage_view(
            {"nodes": [s02_node("A", name="parent"), s02_node("N", name="first", parent_id="A")]},
            epoch_number=3,
            epoch_id="1" * 32,
            adopted_sequence=0,
            snapshot_timestamp=NOW,
            account_scope_id=client._safe_account_scope_id(),
            clear_dirty=True,
        )
    except CacheViewError as exc:
        assert "publication raced beyond 4 attempts" in str(exc)
    else:  # pragma: no cover - assertion form keeps the typed failure explicit
        raise AssertionError("expected exhausted-CAS CacheViewError")

    assert client._cache_view_publisher.capture() is before_view
    assert client._transition_cow_effects is before_effects
    assert frozenset(client._nodes_export_dirty_ids) == before_dirty


# ===========================================================================
# 🔴 CF-S03CR-10 — clean-base recovery must not overwrite a racing COW
# ===========================================================================


@async_test
async def test_cf_s03cr10_clean_base_recovery_merges_racing_cow(tmp_path, monkeypatch):
    """Historical recovery name: real warm S2 rebuild, then COW, then one local publication."""
    client, paths = _make_lineage_client(tmp_path, monkeypatch)
    view = await _force_recovery_interleave(client, paths, monkeypatch,
        journal_effects=[SemanticEffect.node_create(s02_node("C2", name="gamma-2"))],
        local_mutation=lambda: client._apply_update_to_nodes_export_cache(_node_model("A", name="cow-write")))
    assert view.adopted_sequence == 1  # markers advanced with the completed view
    assert view.nodes_by_id["C2"]["name"] == "gamma-2"  # journal tail applied
    # THE FORBIDDEN OUTCOME CHECK: the racing COW effect must NOT be gone.
    assert view.nodes_by_id["A"]["name"] == "cow-write"
    # Effect + legacy-dirty evidence retained (Contract F/E).
    assert set(client._transition_cow_effects) == {"A"}
    assert "A" in client._nodes_export_dirty_ids


@async_test
async def test_cf_s03cr10_cow_before_recovery_also_converges(tmp_path, monkeypatch):
    """Historical recovery name: COW lands BEFORE the explicit real warm rebuild."""
    client, paths = _make_lineage_client(tmp_path, monkeypatch)
    client._apply_update_to_nodes_export_cache(_node_model("A", name="cow-write"))
    _append(paths, [SemanticEffect.node_create(s02_node("C2", name="gamma-2"))])

    _neutralize_merge(monkeypatch, "CUT2B_NEUTRALIZE_REBUILD_MERGE")
    assert client._load_nodes_export_cache_from_disk()
    view = client._cache_view_publisher.capture()
    assert view.adopted_sequence == 1
    assert view.nodes_by_id["C2"]["name"] == "gamma-2"
    assert view.nodes_by_id["A"]["name"] == "cow-write"
    assert set(client._transition_cow_effects) == {"A"}


async def _force_recovery_interleave(
    client, paths, monkeypatch, *, journal_effects, local_mutation
):
    """Historical helper name: actual warm materialization, synchronous COW, then real local publication."""

    _append(paths, journal_effects)

    real_materialize = snapshot_store.materialize_current_payload
    fired = {"done": False}

    def _materialize_then_mutate(*args, **kwargs):
        materialized = real_materialize(*args, **kwargs)
        if not fired["done"]:
            fired["done"] = True
            local_mutation()
        return materialized

    monkeypatch.setattr(
        snapshot_store, "materialize_current_payload", _materialize_then_mutate
    )
    _neutralize_merge(monkeypatch, "CUT2B_NEUTRALIZE_REBUILD_MERGE")
    assert client._load_nodes_export_cache_from_disk()
    assert fired["done"] is True
    assert client._cache_view_publisher.capture().adopted_sequence == 1
    return client._cache_view_publisher.capture()


@async_test
async def test_cf_s03cr10_recovery_move_shape(tmp_path, monkeypatch):
    """CF matrix — a pinned-N move landing during recovery preserves the
    move while the unrelated N+1 journal state and markers survive."""

    client, paths = _make_lineage_client(tmp_path, monkeypatch)
    view = await _force_recovery_interleave(
        client,
        paths,
        monkeypatch,
        journal_effects=[SemanticEffect.node_create(s02_node("U", name="unrelated"))],
        local_mutation=lambda: client._apply_move_to_nodes_export_cache("C", "Z"),
    )
    assert view.adopted_sequence == 1
    assert view.nodes_by_id["U"]["name"] == "unrelated"
    assert view.nodes_by_id["C"]["parent_id"] == "Z"
    assert set(client._transition_cow_effects["C"].fields) == {"parent_id"}
    assert "C" in client._nodes_export_dirty_ids


@async_test
async def test_cf_s03cr10_recovery_create_then_update_shape(tmp_path, monkeypatch):
    """CF matrix — N+1 contains the post-create/pre-update state while the
    pinned-N operation publishes create→update during recovery.  The later
    local write must survive; existence reflection retires only creation."""

    client, paths = _make_lineage_client(tmp_path, monkeypatch)

    def _create_then_update():
        client._apply_create_to_nodes_export_cache(
            _node_model("N", name="first"), parent_id="A"
        )
        client._apply_update_to_nodes_export_cache(_node_model("N", name="second"))

    view = await _force_recovery_interleave(
        client,
        paths,
        monkeypatch,
        journal_effects=[
            SemanticEffect.node_create(s02_node("N", name="first", parent_id="A")),
            SemanticEffect.node_create(s02_node("U", name="unrelated")),
        ],
        local_mutation=_create_then_update,
    )
    assert view.adopted_sequence == 1
    assert view.nodes_by_id["U"]["name"] == "unrelated"
    assert view.nodes_by_id["N"]["name"] == "second"
    effect = client._transition_cow_effects["N"]
    assert effect.kind == "fields"
    assert effect.fields["name"].prior == "first"
    assert effect.fields["name"].verified == "second"
    assert "N" in client._nodes_export_dirty_ids


@async_test
async def test_cf_s03cr10_recovery_subtree_delete_unknown_descendant(
    tmp_path, monkeypatch
):
    """CF-S03CR-10 mandatory S07-armed race.

    The operation pins N with B→C; journal N+1 introduces D beneath B plus
    unrelated U; recovery pauses; the pinned-N delete(B) publishes; recovery
    resumes.  N+1 markers and U survive while B, C, and previously unseen D
    are all absent.  This fails against 7d8e7cd, which leaves D orphaned.
    """

    client, paths = _make_lineage_client(tmp_path, monkeypatch)
    view = await _force_recovery_interleave(
        client,
        paths,
        monkeypatch,
        journal_effects=[
            SemanticEffect.node_create(s02_node("D", name="new-child", parent_id="B")),
            SemanticEffect.node_create(s02_node("U", name="unrelated", parent_id="A")),
        ],
        local_mutation=lambda: client._apply_delete_to_nodes_export_cache("B"),
    )
    assert view.adopted_sequence == 1
    assert view.nodes_by_id["U"]["name"] == "unrelated"
    assert {"B", "C", "D"}.isdisjoint(view.nodes_by_id)
    # F-3 root-scoped representation: only root B survives as authority.
    assert set(client._transition_cow_effects) == {"B"}
    assert client._nodes_export_dirty_ids >= {"B", "C", "D"}


@async_test
async def test_cf_s03cr10_recovery_escaped_descendant_shape(tmp_path, monkeypatch):
    """CF-S03CR-10 escaped-descendant recovery shape (F-3, S07-armed).

    The operation pins N with B→C; journal N+1 MOVES C out to A (parentage
    effect) and creates unrelated U; recovery pauses during materialization;
    the pinned-N delete(B) publishes; recovery resumes.  Final: markers N+1;
    U and C present (C under A); B absent; coherent effect/dirty evidence —
    the escaped C is never removed by B's tombstone.
    """

    client, paths = _make_lineage_client(tmp_path, monkeypatch)
    view = await _force_recovery_interleave(
        client,
        paths,
        monkeypatch,
        journal_effects=[
            SemanticEffect.node_parentage("C", "A"),  # C escapes B before delete
            SemanticEffect.node_create(s02_node("U", name="unrelated", parent_id="A")),
        ],
        local_mutation=lambda: client._apply_delete_to_nodes_export_cache("B"),
    )
    assert view.adopted_sequence == 1  # markers N+1 with the completed view
    assert view.nodes_by_id["U"]["name"] == "unrelated"
    assert view.nodes_by_id["C"]["parent_id"] == "A"  # escaped C survives
    assert "B" not in view.nodes_by_id
    # B was offered by the recovery candidate (the journal never touched it),
    # so the root tombstone acted on exactly {B} and survives with dirt; C is
    # neither pinned by an effect nor marked dirty by the delete.
    assert set(client._transition_cow_effects) == {"B"}
    assert "B" in client._nodes_export_dirty_ids
    assert "C" not in client._nodes_export_dirty_ids
