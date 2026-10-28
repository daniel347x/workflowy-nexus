"""Tests for the node.export output_file archive path (Unit 1, Gate 1b).

T16 is a compatibility lock (in-band behaviour unchanged); T17-T26 are the
new archive behaviour.  End-to-end tests run server._node_export_impl over
a real client warm-started from a coherence lineage inside an operation
boundary, exactly as the S03 consumer-migration tests do.
"""

from __future__ import annotations

import asyncio
from functools import wraps
from pathlib import Path

import pytest

from coherence_harness import NOW, s02_node

from workflowy_mcp import server
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.mcp_api_registry import validate_params
from workflowy_mcp.models import APIConfiguration, NetworkError
from workflowy_mcp.node_archive import (
    ARCHIVE_FORMAT,
    ARCHIVE_FORMAT_VERSION,
    ArchiveError,
    ArchiveVerificationError,
    parse_archive,
)
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


BASE_NODES = [
    s02_node("A", name="alpha", note="root note & <special>"),
    s02_node("B", name="beta", parent_id="A", priority=2),
    s02_node("C", name="gamma", parent_id="B", priority=1),
    s02_node("D", name="delta", parent_id="A", priority=1),
]


def _make_client(tmp_path: Path, monkeypatch):
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
    config = APIConfiguration(
        api_key=SecretStr("test-key-1234"), base_url="https://workflowy.com/api/v1"
    )
    return WorkFlowyClientCore(config), jobs_root, snapshot_dir


def _make_lineage_etch_client(tmp_path: Path, monkeypatch, nodes=BASE_NODES):
    """A WorkFlowyClientEtch warm-started from a coherence lineage."""
    from pydantic import SecretStr

    probe, _jobs_root, snapshot_dir = _make_client(tmp_path, monkeypatch)
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
    config = APIConfiguration(
        api_key=SecretStr("test-key-1234"), base_url="https://workflowy.com/api/v1"
    )
    return WorkFlowyClientEtch(config)


def _wire_server(monkeypatch, client):
    monkeypatch.setattr(server, "get_client", lambda: client)


# ---------------------------------------------------------------------------
# T16 · In-band unchanged (compatibility lock)
# ---------------------------------------------------------------------------


@async_test
async def test_t16_in_band_export_unchanged(tmp_path, monkeypatch):
    client = _make_lineage_etch_client(tmp_path, monkeypatch)
    _wire_server(monkeypatch, client)
    async with cache_operation_boundary(client, "op"):
        subtree = await server._node_export_impl("A")
        direct_subtree = await client.export_nodes("A")
        assert subtree == direct_subtree
        assert subtree["_filtered_count"] == 4

        whole = await server._node_export_impl(None)
        direct_whole = await client.export_nodes(None)
        assert whole == direct_whole
        assert [node["id"] for node in whole["nodes"]] == ["A", "B", "C", "D"]


# ---------------------------------------------------------------------------
# T17 · Subtree archive
# ---------------------------------------------------------------------------


@async_test
async def test_t17_subtree_archive_verifies_and_matches_view(tmp_path, monkeypatch):
    client = _make_lineage_etch_client(tmp_path, monkeypatch)
    _wire_server(monkeypatch, client)
    destination = tmp_path / "archives" / "sub.json"
    destination.parent.mkdir()
    async with cache_operation_boundary(client, "op") as snapshot:
        result = await server._node_export_impl(
            "A",
            output_file=str(destination),
            archive_name="My archive",
            archive_description="desc",
        )
    assert list(result) == [
        "success",
        "output_file",
        "path_record",
        "node_id",
        "root_ids",
        "node_count",
        "bytes_written",
        "nodes_sha256",
        "format",
        "format_version",
        "verified",
    ]
    assert result["success"] is True
    assert result["verified"] is True
    assert result["output_file"] == str(destination)
    assert result["node_id"] == "A"
    assert "nodes" not in result

    raw = destination.read_bytes()
    assert result["bytes_written"] == len(raw)
    parsed = parse_archive(raw, nest=client._build_hierarchy)
    assert parsed["archive"]["root_ids"] == result["root_ids"] == ["A"]
    assert parsed["archive"]["node_count"] == result["node_count"] == 4
    assert parsed["archive"]["nodes_sha256"] == result["nodes_sha256"]
    assert parsed["archive"]["name"] == "My archive"
    assert parsed["archive"]["description"] == "desc"
    view_records = {
        record["id"]: record
        for record in snapshot.view.nodes_export["nodes"]
    }
    for record in parsed["nodes"]:
        assert record == view_records[record["id"]]


# ---------------------------------------------------------------------------
# T18 · Whole vault
# ---------------------------------------------------------------------------


@async_test
async def test_t18_whole_vault_archive(tmp_path, monkeypatch):
    client = _make_lineage_etch_client(tmp_path, monkeypatch)
    _wire_server(monkeypatch, client)
    destination = tmp_path / "vault.json"
    async with cache_operation_boundary(client, "op") as snapshot:
        result = await server._node_export_impl(None, output_file=str(destination))
    assert result["node_id"] is None
    parsed = parse_archive(destination.read_bytes(), nest=client._build_hierarchy)
    assert parsed["archive"]["name"] == "Workflowy vault"
    assert parsed["archive"]["source_parent_id"] is None
    assert result["node_count"] == len(snapshot.view.nodes_export["nodes"]) == 4

    with pytest.raises(server.OperationValidationError) as excinfo:
        await server._node_export_impl("", output_file=str(tmp_path / "x.json"))
    assert excinfo.value.code == "invalid_parameter_type"
    assert not (tmp_path / "x.json").exists()


# ---------------------------------------------------------------------------
# T19 · Cold cache
# ---------------------------------------------------------------------------


@async_test
async def test_t19_cold_cache_raises_and_writes_nothing(tmp_path, monkeypatch):
    """A daemon with no adopted lineage refuses the cache-backed export with
    the refresh-required dialect (the operation boundary's
    CoherenceStateMissingError, or the impl's NetworkError one layer down)
    and creates no file."""
    from pydantic import SecretStr

    from workflowy_mcp.coherence.errors import CoherenceStateMissingError

    _probe, _jobs_root, _snapshot_dir = _make_client(tmp_path, monkeypatch)
    config = APIConfiguration(
        api_key=SecretStr("test-key-1234"), base_url="https://workflowy.com/api/v1"
    )
    client = WorkFlowyClientEtch(config)
    _wire_server(monkeypatch, client)
    destination = tmp_path / "cold.json"
    with pytest.raises((CoherenceStateMissingError, NetworkError)) as excinfo:
        async with cache_operation_boundary(client, "op"):
            await server._node_export_impl("A", output_file=str(destination))
    assert "refresh" in str(excinfo.value).lower()
    assert not destination.exists()


# ---------------------------------------------------------------------------
# T20 · Unknown node
# ---------------------------------------------------------------------------


@async_test
async def test_t20_unknown_node_is_loud_and_writes_nothing(tmp_path, monkeypatch):
    client = _make_lineage_etch_client(tmp_path, monkeypatch)
    _wire_server(monkeypatch, client)

    async def _ghost_resolver(node_id, *, label, required=True):
        return "ghost-id-not-in-view"

    monkeypatch.setattr(server, "_resolve_generic_node_handle", _ghost_resolver)
    destination = tmp_path / "ghost.json"
    with pytest.raises(NetworkError) as excinfo:
        async with cache_operation_boundary(client, "op"):
            await server._node_export_impl("A", output_file=str(destination))
    assert "ghost-id-not-in-view" in str(excinfo.value)
    assert "system.cache.refresh" in str(excinfo.value)
    assert not destination.exists()


# ---------------------------------------------------------------------------
# T21 · Refusals before any read
# ---------------------------------------------------------------------------


@async_test
async def test_t21_refusals_happen_before_any_cache_or_client_call(
    tmp_path, monkeypatch
):
    client = _make_lineage_etch_client(tmp_path, monkeypatch)
    _wire_server(monkeypatch, client)

    async def _no_resolve(node_id, *, label, required=True):
        raise AssertionError("resolver must not be called")

    async def _no_export(node_id=None, **kwargs):
        raise AssertionError("client export must not be called")

    monkeypatch.setattr(server, "_resolve_generic_node_handle", _no_resolve)
    monkeypatch.setattr(client, "export_nodes", _no_export)

    destination = tmp_path / "never.json"

    with pytest.raises(server.OperationValidationError) as excinfo:
        await server._node_export_impl(None, archive_name="A")
    assert excinfo.value.code == "invalid_parameter_combination"
    with pytest.raises(server.OperationValidationError):
        await server._node_export_impl(None, output_file="")
    with pytest.raises(server.OperationValidationError):
        await server._node_export_impl(None, output_file=5)
    with pytest.raises(server.ToolPathError):
        await server._node_export_impl(None, output_file="relative/path.json")
    with pytest.raises(server.ToolPathError):
        await server._node_export_impl(None, output_file=str(tmp_path))
    with pytest.raises(server.ToolPathError):
        await server._node_export_impl(
            None, output_file=str(tmp_path / "missing-dir" / "x.json")
        )

    assert not destination.exists()
    assert (tmp_path / "missing-dir").exists() is False


# ---------------------------------------------------------------------------
# T21b · Unarchivable content
# ---------------------------------------------------------------------------


@async_test
async def test_t21b_nan_record_refused(tmp_path, monkeypatch):
    nan_nodes = [s02_node("A", name="a", data={"k": float("nan")})]
    client = _make_lineage_etch_client(tmp_path, monkeypatch, nodes=nan_nodes)
    _wire_server(monkeypatch, client)
    destination = tmp_path / "nan.json"
    with pytest.raises(ArchiveError):
        async with cache_operation_boundary(client, "op"):
            await server._node_export_impl(None, output_file=str(destination))
    assert not destination.exists()


@async_test
async def test_t21b_parent_cycle_vault_refused(tmp_path, monkeypatch):
    cycle_nodes = [
        s02_node("A", name="a", parent_id="B"),
        s02_node("B", name="b", parent_id="A"),
        s02_node("C", name="c"),
    ]
    client = _make_lineage_etch_client(tmp_path, monkeypatch, nodes=cycle_nodes)
    _wire_server(monkeypatch, client)
    destination = tmp_path / "cycle.json"
    with pytest.raises(ArchiveError):
        async with cache_operation_boundary(client, "op"):
            await server._node_export_impl(None, output_file=str(destination))
    assert not destination.exists()


# ---------------------------------------------------------------------------
# T22 · Pure read
# ---------------------------------------------------------------------------


@async_test
async def test_t22_export_is_a_pure_cache_read(tmp_path, monkeypatch):
    client = _make_lineage_etch_client(tmp_path, monkeypatch)
    _wire_server(monkeypatch, client)

    def _raiser(*args, **kwargs):
        raise AssertionError("export must not touch the network or any writer")

    monkeypatch.setattr(client, "_send_workflowy_write", _raiser)
    monkeypatch.setattr(client, "refresh_nodes_export_cache", _raiser)
    monkeypatch.setattr(client, "save_nodes_export_cache", _raiser)
    monkeypatch.setattr(client, "_save_nodes_export_cache_to_disk", _raiser)
    monkeypatch.setattr(
        "workflowy_mcp.client.api_client_etch.fetch_nodes_export_uncached", _raiser
    )
    monkeypatch.setattr(
        "workflowy_mcp.coherence.snapshot_store.write_snapshot_candidate", _raiser
    )
    monkeypatch.setattr(
        "workflowy_mcp.coherence.epoch_publisher.publish_epoch", _raiser
    )
    from workflowy_mcp.client.api_client_core import WorkFlowyClientCore

    monkeypatch.setattr(
        WorkFlowyClientCore,
        "client",
        property(lambda self: _raiser()),
    )

    subtree_target = tmp_path / "pure-sub.json"
    vault_target = tmp_path / "pure-vault.json"
    async with cache_operation_boundary(client, "op"):
        subtree = await server._node_export_impl("A", output_file=str(subtree_target))
        vault = await server._node_export_impl(None, output_file=str(vault_target))
    assert subtree["verified"] is True
    assert vault["verified"] is True
    assert subtree_target.exists() and vault_target.exists()


# ---------------------------------------------------------------------------
# T23a · Pre-write verification bites
# ---------------------------------------------------------------------------


@async_test
async def test_t23a_serialize_dropping_a_record_is_unverified(tmp_path, monkeypatch):
    client = _make_lineage_etch_client(tmp_path, monkeypatch)
    _wire_server(monkeypatch, client)

    real_serialize = server.serialize_archive

    def dropping_serialize(document):
        trimmed = {"archive": document["archive"], "nodes": document["nodes"][:-1]}
        return real_serialize(trimmed)

    monkeypatch.setattr(server, "serialize_archive", dropping_serialize)
    destination = tmp_path / "dropped.json"
    with pytest.raises(ArchiveVerificationError) as excinfo:
        async with cache_operation_boundary(client, "op"):
            await server._node_export_impl("A", output_file=str(destination))
    message = str(excinfo.value)
    assert "UNVERIFIED" in message
    assert str(destination) in message
    assert "do not delete the source" in message
    assert not destination.exists()


@async_test
async def test_t23a_build_dropping_a_leaf_is_unverified(tmp_path, monkeypatch):
    """A self-consistent document built from every selected record but one
    leaf passes every reader rule — the pinned-view comparison is what
    catches it."""
    client = _make_lineage_etch_client(tmp_path, monkeypatch)
    _wire_server(monkeypatch, client)

    real_build = server.build_archive_document

    def dropping_build(records, **kwargs):
        return real_build(list(records)[:-1], **kwargs)

    monkeypatch.setattr(server, "build_archive_document", dropping_build)
    destination = tmp_path / "leafless.json"
    with pytest.raises(ArchiveVerificationError) as excinfo:
        async with cache_operation_boundary(client, "op"):
            await server._node_export_impl("A", output_file=str(destination))
    message = str(excinfo.value)
    assert "UNVERIFIED" in message
    assert str(destination) in message
    assert "do not delete the source" in message
    assert not destination.exists()


# ---------------------------------------------------------------------------
# T23b · Post-write verification bites
# ---------------------------------------------------------------------------


@async_test
async def test_t23b_readback_mismatch_is_unverified(tmp_path, monkeypatch):
    client = _make_lineage_etch_client(tmp_path, monkeypatch)
    _wire_server(monkeypatch, client)

    real_writer = server.atomic_write_text

    def flipped_writer(path, text, **kwargs):
        return real_writer(path, text[:-1], **kwargs)

    monkeypatch.setattr(server, "atomic_write_text", flipped_writer)
    destination = tmp_path / "flipped.json"
    with pytest.raises(ArchiveVerificationError) as excinfo:
        async with cache_operation_boundary(client, "op"):
            await server._node_export_impl("A", output_file=str(destination))
    message = str(excinfo.value)
    assert "UNVERIFIED" in message
    assert str(destination) in message
    assert "do not delete the source" in message
    assert destination.exists()  # the unverified file is left in place


# ---------------------------------------------------------------------------
# T24 · One file, replace in place
# ---------------------------------------------------------------------------


@async_test
async def test_t24_exactly_one_file_and_in_place_replace(tmp_path, monkeypatch):
    client = _make_lineage_etch_client(tmp_path, monkeypatch)
    _wire_server(monkeypatch, client)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    destination = out_dir / "one.json"
    async with cache_operation_boundary(client, "op"):
        first = await server._node_export_impl("A", output_file=str(destination))
        assert sorted(path.name for path in out_dir.iterdir()) == ["one.json"]
        second = await server._node_export_impl("A", output_file=str(destination))
    assert first["nodes_sha256"] == second["nodes_sha256"]
    assert sorted(path.name for path in out_dir.iterdir()) == ["one.json"]


# ---------------------------------------------------------------------------
# T24b · Write failure leaves the destination untouched
# ---------------------------------------------------------------------------


@async_test
async def test_t24b_replace_failure_leaves_destination_and_no_temp(
    tmp_path, monkeypatch
):
    from workflowy_mcp import carto_job_governance

    client = _make_lineage_etch_client(tmp_path, monkeypatch)
    _wire_server(monkeypatch, client)
    destination = tmp_path / "guarded.json"
    destination.write_text("PRE-EXISTING", encoding="utf-8")

    def _boom_replace(tmp, target):
        raise PermissionError("locked by a scanner")

    monkeypatch.setattr(carto_job_governance, "_default_replace", _boom_replace)

    with pytest.raises(PermissionError):
        async with cache_operation_boundary(client, "op"):
            await server._node_export_impl("A", output_file=str(destination))

    assert destination.read_text(encoding="utf-8") == "PRE-EXISTING"
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith(".")] == []


# ---------------------------------------------------------------------------
# T25 · Provenance
# ---------------------------------------------------------------------------


@async_test
async def test_t25_provenance_from_pinned_view(tmp_path, monkeypatch):
    client = _make_lineage_etch_client(tmp_path, monkeypatch)
    _wire_server(monkeypatch, client)
    destination = tmp_path / "prov.json"
    async with cache_operation_boundary(client, "op") as snapshot:
        view = snapshot.view
        result = await server._node_export_impl(
            "A",
            output_file=str(destination),
            archive_name="Named",
            archive_description="Described",
        )
    parsed = parse_archive(destination.read_bytes(), nest=client._build_hierarchy)
    assert parsed["archive"]["source_cache"] == {
        "epoch_number": view.epoch_number,
        "epoch_id": view.epoch_id,
        "adopted_sequence": view.adopted_sequence,
    }
    assert result["nodes_sha256"] == parsed["archive"]["nodes_sha256"]
    exported_at = parsed["archive"]["exported_at"]
    assert len(exported_at) == 20 and exported_at.endswith("Z")
    assert exported_at[4] == "-" and exported_at[10] == "T"
    assert parsed["archive"]["name"] == "Named"
    assert parsed["archive"]["description"] == "Described"


# ---------------------------------------------------------------------------
# T26 · Registration and describe
# ---------------------------------------------------------------------------


def test_t26_registration_and_describe():
    registry = server.build_node_registry()
    spec = registry.resolve("export")
    assert spec.side_effects == ("pure_read", "artifact_write")
    full = registry.describe_full("export")
    properties = full["params_schema"]["properties"]
    for name in (
        "node_handle",
        "output_file",
        "archive_name",
        "archive_description",
    ):
        assert name in properties
    assert "system.cache.refresh" in full["summary"]
    assert "verified" in full["returns"]["description"]
    for example in full["examples"]:
        validate_params(spec, example["call"].get("params") or {})

    verbs = [item.verb for item in registry.operations()]
    assert verbs == [
        "read",
        "search",
        "edit",
        "update_emoji",
        "prepend_emoji",
        "append_emoji",
        "append_note",
        "prepend_note",
        "etch",
        "move",
        "complete",
        "uncomplete",
        "delete",
        "export",
        "scry",
        "ancestors",
        "resolve",
    ]
    # the legacy surface stays disjoint under the consolidated profile
    legacy = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in legacy}
    assert "workflowy_export_node" not in names
    assert "node" in names
