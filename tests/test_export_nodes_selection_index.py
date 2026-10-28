"""Unit 1, Gate 1b, D5: the one-pass subtree selection inside
``export_nodes_impl`` must return exactly the id set — and exactly the
cache-order list — of the retired per-node rescan, for every caller.

The oracle below is a VERBATIM copy of the pre-change ``add_descendants``
algorithm (the recursive per-node full scan), so the comparison is against
today's behaviour, not a re-derivation."""

from __future__ import annotations

import asyncio
import random
from functools import wraps
from pathlib import Path

import pytest

from coherence_harness import NOW, s02_node

from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.client.api_client_etch import export_nodes_impl
from workflowy_mcp.models import APIConfiguration
from workflowy_mcp.coherence import (
    initialize_manifest_if_absent,
    manifest_with_updates,
    new_bootstrap_manifest,
    write_manifest,
)
from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
from workflowy_mcp.coherence.snapshot_store import (
    new_snapshot_candidate_path,
    write_snapshot_candidate,
)


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


# ---------------------------------------------------------------------------
# The oracle: verbatim pre-change add_descendants
# ---------------------------------------------------------------------------


def _legacy_selection(all_nodes, node_id):
    included_ids = {node_id}
    nodes_by_id = {node["id"]: node for node in all_nodes if node.get("id")}

    def add_descendants(parent_id: str) -> None:
        """Recursively add all descendants whose parent matches parent_id.

        IMPORTANT: /nodes-export historically used `parent_id` (snake_case) in
        its payload, but our in-memory cache may also contain nodes that were
        appended via single-node operations (create_node, etc.) whose parent
        linkage lives in `parentId` (camelCase). To avoid losing newly-created
        children in subtree exports, we must honor BOTH keys here.
        """
        for node in all_nodes:
            nid = node.get("id")
            if not nid:
                continue
            pid = node.get("parent_id") or node.get("parentId")
            if pid == parent_id and nid not in included_ids:
                included_ids.add(nid)
                add_descendants(str(nid))

    if node_id in nodes_by_id:
        add_descendants(node_id)

    return [node for node in all_nodes if node["id"] in included_ids]


# ---------------------------------------------------------------------------
# Seeded forest fixtures
# ---------------------------------------------------------------------------


def _seeded_forest(seed: int = 20260926, count: int = 220):
    """A random forest: every node's parent is a random earlier id, linked
    through `parent_id` (most nodes) or the `parentId` alias (some nodes)."""
    rng = random.Random(seed)
    records = [s02_node("R0", name="root-0", priority=rng.randrange(0, 5))]
    for index in range(1, count):
        parent = records[rng.randrange(max(0, index - 20), index)]["id"]
        alias = index % 7 == 3
        record = s02_node(
            f"N{index:04d}",
            name=f"node-{index}",
            priority=rng.randrange(0, 5),
        )
        if alias:
            del record["parent_id"]
            record["parentId"] = parent
        else:
            record["parent_id"] = parent
        records.append(record)
    return records


def _with_cycles(records):
    """Add one two-node cycle and one cycle through a requested root."""
    cycled = [dict(record) for record in records]
    cycled.append(s02_node("CX", name="cycle-x", parent_id="CY"))
    cycled.append(s02_node("CY", name="cycle-y", parent_id="CX"))
    cycled.append(s02_node("ROOTC", name="cycle-root", parent_id="PC"))
    cycled.append(s02_node("PC", name="cycle-parent", parent_id="ROOTC"))
    return cycled


def _make_lineage_etch_client(tmp_path: Path, monkeypatch, nodes):
    from pydantic import SecretStr

    from workflowy_mcp.client.api_client_core import WorkFlowyClientCore

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
    probe_config = APIConfiguration(
        api_key=SecretStr("test-key-1234"), base_url="https://workflowy.com/api/v1"
    )
    probe = WorkFlowyClientCore(probe_config)
    paths = probe._coherence_paths()
    manifest = initialize_manifest_if_absent(
        paths, new_bootstrap_manifest(paths.account_root.name, now=NOW)
    )
    candidate = write_snapshot_candidate(
        new_snapshot_candidate_path(snapshot_dir),
        {"nodes": list(nodes)},
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
    return WorkFlowyClientEtch(probe_config)


# ---------------------------------------------------------------------------
# T28 · Selection parity
# ---------------------------------------------------------------------------


def _view_nodes(snapshot):
    return snapshot.view.nodes_export["nodes"]


@async_test
async def test_t28_selection_parity_over_seeded_forests(tmp_path, monkeypatch):
    for seed in (11, 20260926):
        records = _seeded_forest(seed=seed)
        client = _make_lineage_etch_client(tmp_path, monkeypatch, records)
        async with cache_operation_boundary(client, "op") as snapshot:
            all_nodes = _view_nodes(snapshot)
            assert len(all_nodes) >= 200

            for target in ("R0", "N0005", "N0219"):
                result = await export_nodes_impl(client, node_id=target)
                expected = _legacy_selection(all_nodes, target)
                assert result["nodes"] == expected, (
                    f"seed {seed}, target {target}: id set or order diverged"
                )

            leaf = "N0219"
            result = await export_nodes_impl(client, node_id=leaf)
            assert [node["id"] for node in result["nodes"]] == [leaf]

            ghost = "not-in-the-view"
            result = await export_nodes_impl(client, node_id=ghost)
            assert result["nodes"] == _legacy_selection(all_nodes, ghost) == []


@async_test
async def test_t28_selection_parity_with_cycles(tmp_path, monkeypatch):
    records = _with_cycles(_seeded_forest(seed=7))
    client = _make_lineage_etch_client(tmp_path, monkeypatch, records)
    async with cache_operation_boundary(client, "op") as snapshot:
        all_nodes = _view_nodes(snapshot)

        # a leaf
        result = await export_nodes_impl(client, node_id="N0100")
        assert result["nodes"] == _legacy_selection(all_nodes, "N0100")

        # the two-node cycle: requesting one member stops at the guard
        result = await export_nodes_impl(client, node_id="CX")
        assert result["nodes"] == _legacy_selection(all_nodes, "CX")
        included = {node["id"] for node in result["nodes"]}
        assert "CY" in included and "CX" in included

        # a cycle through the requested root: the guard at the root stops it
        result = await export_nodes_impl(client, node_id="ROOTC")
        assert result["nodes"] == _legacy_selection(all_nodes, "ROOTC")
        included = {node["id"] for node in result["nodes"]}
        assert "ROOTC" in included and "PC" in included


def test_t28_structural_no_legacy_rescan_remains():
    import workflowy_mcp.client.api_client_etch as etch_module

    source = Path(etch_module.__file__).read_text(encoding="utf-8")
    assert "def add_descendants" not in source
    assert "children_by_parent" in source
