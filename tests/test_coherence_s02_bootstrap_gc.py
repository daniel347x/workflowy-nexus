"""S02 §16.4 — bootstrap, warm start, and manifest-aware GC tests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from test_cut_2a_publication import _publication_records_cleanup

from coherence_harness import (
    ACCOUNT_A,
    NOW,
    s02_node,
    seed_publication_record,
)

from workflowy_mcp.coherence import (
    CoherenceManifest,
    coherence_paths,
    read_manifest,
)
from workflowy_mcp.coherence.refresh_authority import (
    _active_candidate_snapshot_paths,
    bootstrap_coherence_from_legacy,
)
from workflowy_mcp.coherence.snapshot_store import (
    collect_protected_snapshot_paths,
    normalize_snapshot_path,
    write_snapshot_candidate,
)

TESTS_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Client factory (isolated jobs root + snapshot dir, real client semantics)
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


def _legacy_snapshot(snapshot_dir: Path, name: str, nodes: list[dict]) -> Path:
    path = snapshot_dir / name
    path.write_text(
        json.dumps(
            {"snapshot_timestamp": NOW, "nodes_export": {"nodes": nodes}}
        ),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Bootstrap (no manifest)
# ---------------------------------------------------------------------------


def test_bootstrap_no_manifest_no_snapshot_initializes_bootstrap_manifest(tmp_path):
    jobs_root = tmp_path / "cartographer_jobs"
    jobs_root.mkdir(parents=True)
    from workflowy_mcp.coherence import coherence_paths

    paths = coherence_paths(ACCOUNT_A, jobs_root=jobs_root)
    manifest = bootstrap_coherence_from_legacy(paths, snapshot_dir=tmp_path / "snapshots")
    assert manifest.coherence_mode == "bootstrap"
    assert manifest.base_snapshot_path is None
    assert manifest.epoch_number == 0
    assert read_manifest(paths, required=True).epoch_id == manifest.epoch_id


def test_bootstrap_adopts_newest_usable_legacy_snapshot(tmp_path):
    jobs_root = tmp_path / "cartographer_jobs"
    jobs_root.mkdir(parents=True)
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    from workflowy_mcp.coherence import coherence_paths

    older = _legacy_snapshot(
        snapshot_dir, "20260101-000000-000000_pid1_nodes_export_snapshot.json",
        [s02_node("old")],
    )
    newer = _legacy_snapshot(
        snapshot_dir, "20260102-000000-000000_pid1_nodes_export_snapshot.json",
        [s02_node("new"), s02_node("child", parent_id="new")],
    )
    paths = coherence_paths(ACCOUNT_A, jobs_root=jobs_root)
    manifest = bootstrap_coherence_from_legacy(paths, snapshot_dir=snapshot_dir)
    assert manifest.coherence_mode == "transition"
    assert manifest.base_snapshot_path is not None
    assert manifest.base_snapshot_path != str(newer)  # a NEW wrapped copy
    # The wrapped copy carries lineage and the newest legacy content.
    from workflowy_mcp.coherence.snapshot_store import read_snapshot_envelope

    envelope = read_snapshot_envelope(manifest.base_snapshot_path)
    assert envelope.legacy is False
    assert envelope.epoch_number == 0
    assert envelope.incorporated_through_seq == 0
    ids = {n["id"] for n in envelope.nodes_export["nodes"]}
    assert ids == {"new", "child"}
    # The legacy files were never mutated.
    assert json.loads(older.read_text(encoding="utf-8")).get("snapshot_schema_version") is None


def test_bootstrap_concurrent_processes_converge_on_one_manifest(tmp_path, monkeypatch):
    """Historical carrier: loop tasks converge; a later initializer cannot replace it."""
    jobs_root = tmp_path / "cartographer_jobs"
    jobs_root.mkdir(parents=True)
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    _legacy_snapshot(
        snapshot_dir, "20260102-000000-000000_pid1_nodes_export_snapshot.json",
        [s02_node("new")],
    )
    from workflowy_mcp.coherence import schema
    if os.environ.get("CUT2B_NEUTRALIZE_INITIALIZE_EXISTING"):
        monkeypatch.setattr(schema, "read_manifest", lambda *a, **k: None)
    paths = coherence_paths(ACCOUNT_A, jobs_root=jobs_root)
    async def scenario():
        async def bootstrap():
            await asyncio.sleep(0)
            return bootstrap_coherence_from_legacy(paths, snapshot_dir=snapshot_dir).epoch_id
        return await asyncio.gather(bootstrap(), bootstrap(), bootstrap())
    outputs = asyncio.run(scenario())
    # All three tasks converged on the ONE persisted manifest.
    assert len(set(outputs)) == 1
    first = read_manifest(paths)
    later = schema.initialize_manifest_if_absent(paths, schema.new_bootstrap_manifest(ACCOUNT_A))
    assert later == first and read_manifest(paths) == first, "initializer overwrote bootstrap lineage"


def test_bootstrap_skips_unusable_legacy_and_takes_next_older(tmp_path):
    jobs_root = tmp_path / "cartographer_jobs"
    jobs_root.mkdir(parents=True)
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "20260103-000000-000000_pid1_nodes_export_snapshot.json").write_text(
        "{corrupt", encoding="utf-8"
    )
    _legacy_snapshot(
        snapshot_dir, "20260102-000000-000000_pid1_nodes_export_snapshot.json",
        [s02_node("good")],
    )
    from workflowy_mcp.coherence import coherence_paths

    paths = coherence_paths(ACCOUNT_A, jobs_root=jobs_root)
    manifest = bootstrap_coherence_from_legacy(paths, snapshot_dir=snapshot_dir)
    assert manifest.base_snapshot_path is not None
    from workflowy_mcp.coherence.snapshot_store import read_snapshot_envelope

    envelope = read_snapshot_envelope(manifest.base_snapshot_path)
    ids = {n["id"] for n in envelope.nodes_export["nodes"]}
    assert ids == {"good"}


def test_bootstrap_never_adopts_another_accounts_s02_snapshot(tmp_path):
    """A1: the snapshot dir is per-USER, not per-account — a fresh account
    must never adopt an already-lineaged S02 wrapper (any account)."""
    jobs_root = tmp_path / "cartographer_jobs"
    jobs_root.mkdir(parents=True)
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    # Account A's S02 lineage wrapper sits in the shared snapshot dir (and is
    # lexically NEWER than the legacy file).
    write_snapshot_candidate(
        snapshot_dir / "20260103-000000-000000_pid1_nodes_export_snapshot.json",
        {"nodes": [s02_node("account-a-secret")]},
        account_scope_id="a" * 32,
        epoch_number=1,
        epoch_id="a" * 32,
        incorporated_through_seq=0,
        publication_kind="full_refresh",
    )
    _legacy_snapshot(
        snapshot_dir, "20260102-000000-000000_pid1_nodes_export_snapshot.json",
        [s02_node("shared-legacy")],
    )
    from workflowy_mcp.coherence import coherence_paths

    paths_b = coherence_paths("b" * 32, jobs_root=jobs_root)
    manifest_b = bootstrap_coherence_from_legacy(paths_b, snapshot_dir=snapshot_dir)
    assert manifest_b.account_scope_id == "b" * 32
    # B adopted the genuinely-LEGACY file — never A's lineaged wrapper.
    assert manifest_b.base_snapshot_path is not None
    from workflowy_mcp.coherence.snapshot_store import read_snapshot_envelope

    envelope = read_snapshot_envelope(manifest_b.base_snapshot_path)
    ids = {n["id"] for n in envelope.nodes_export["nodes"]}
    assert ids == {"shared-legacy"}
    assert "account-a-secret" not in ids


def test_bootstrap_with_only_s02_wrappers_yields_no_base_manifest(tmp_path):
    """A1 (same-account edge): manifest loss with ONLY S02 wrappers on disk
    must produce a no-base bootstrap manifest requiring explicit refresh —
    never an adoption of a lineaged wrapper."""
    jobs_root = tmp_path / "cartographer_jobs"
    jobs_root.mkdir(parents=True)
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    write_snapshot_candidate(
        snapshot_dir / "20260103-000000-000000_pid1_nodes_export_snapshot.json",
        {"nodes": [s02_node("lineaged-only")]},
        account_scope_id=ACCOUNT_A,
        epoch_number=1,
        epoch_id="c" * 32,
        incorporated_through_seq=0,
        publication_kind="full_refresh",
    )
    from workflowy_mcp.coherence import coherence_paths

    paths = coherence_paths(ACCOUNT_A, jobs_root=jobs_root)
    manifest = bootstrap_coherence_from_legacy(paths, snapshot_dir=snapshot_dir)
    assert manifest.coherence_mode == "bootstrap"
    assert manifest.base_snapshot_path is None


# ---------------------------------------------------------------------------
# Warm start (manifest exists)
# ---------------------------------------------------------------------------


def test_warm_start_no_manifest_no_snapshot_is_ordinary_cache_missing(tmp_path, monkeypatch):
    client, jobs_root, _snapshot_dir = _make_client(tmp_path, monkeypatch)
    # Construction ran warm start: bootstrap manifest with no base.
    assert client._nodes_export_cache is None
    assert client._nodes_export_lineage_error is None
    paths = client._coherence_paths()
    manifest = read_manifest(paths, required=True)
    assert manifest.coherence_mode == "bootstrap"


def _adopt_base_manifest(paths, base_path: str, *, epoch_id: str) -> None:
    """CAS the (bootstrap-initialized) manifest to reference a base with a
    matching epoch identity."""
    from workflowy_mcp.coherence import manifest_with_updates, write_manifest

    existing = read_manifest(paths, required=True)
    write_manifest(
        paths,
        manifest_with_updates(
            existing,
            epoch_id=epoch_id,
            base_snapshot_path=normalize_snapshot_path(base_path),
            coherence_mode="transition",
        ),
        expected_epoch_id=existing.epoch_id,
        expected_head_seq=existing.journal_head_seq,
    )


def test_warm_start_adopts_manifest_base_and_ignores_newer_unreferenced(
    tmp_path, monkeypatch
):
    client, jobs_root, snapshot_dir = _make_client(tmp_path, monkeypatch)
    paths = client._coherence_paths()
    account = paths.account_root.name
    # Publish a manifest-linked base with node "base-node".
    base_path = write_snapshot_candidate(
        snapshot_dir / "20260101-000000-000000_pid1_nodes_export_snapshot.json",
        {"nodes": [s02_node("base-node")]},
        account_scope_id=account,
        epoch_number=0,
        epoch_id="b" * 32,
        incorporated_through_seq=0,
        publication_kind="full_refresh",
    )
    _adopt_base_manifest(paths, base_path, epoch_id="b" * 32)
    # A LEXICALLY NEWER unreferenced file must be ignored.
    _legacy_snapshot(
        snapshot_dir, "20260109-000000-000000_pid1_nodes_export_snapshot.json",
        [s02_node("unreferenced-newer")],
    )
    assert client._load_nodes_export_cache_from_disk() is True
    ids = {n["id"] for n in client._nodes_export_cache["nodes"]}
    assert ids == {"base-node"}
    assert client._nodes_export_lineage_error is None


def test_warm_start_malformed_manifest_records_distinct_fault(tmp_path, monkeypatch):
    client, jobs_root, snapshot_dir = _make_client(tmp_path, monkeypatch)
    paths = client._coherence_paths()
    paths.manifest_path.write_text("{not json", encoding="utf-8")
    # A tempting newest-timestamp snapshot exists but must NOT be used.
    _legacy_snapshot(
        snapshot_dir, "20260109-000000-000000_pid1_nodes_export_snapshot.json",
        [s02_node("should-not-load")],
    )
    assert client._load_nodes_export_cache_from_disk() is False
    assert client._nodes_export_cache is None
    assert client._nodes_export_lineage_error is not None
    assert "CoherenceSchemaError" in client._nodes_export_lineage_error


def test_warm_start_lineage_mismatch_fails_distinctly(tmp_path, monkeypatch):
    client, jobs_root, snapshot_dir = _make_client(tmp_path, monkeypatch)
    paths = client._coherence_paths()
    account = paths.account_root.name
    # Manifest references a base whose epoch_id disagrees with the manifest.
    base_path = write_snapshot_candidate(
        snapshot_dir / "20260101-000000-000000_pid1_nodes_export_snapshot.json",
        {"nodes": [s02_node("x")]},
        account_scope_id=account,
        epoch_number=0,
        epoch_id="c" * 32,
        incorporated_through_seq=0,
        publication_kind="full_refresh",
    )
    _adopt_base_manifest(paths, base_path, epoch_id="d" * 32)  # mismatch
    assert client._load_nodes_export_cache_from_disk() is False
    assert client._nodes_export_lineage_error is not None


# ---------------------------------------------------------------------------
# Manifest-aware GC
# ---------------------------------------------------------------------------


def _gc_fixture(tmp_path: Path, monkeypatch):
    client, jobs_root, snapshot_dir = _make_client(tmp_path, monkeypatch)
    paths = client._coherence_paths()
    return client, paths, jobs_root, snapshot_dir


def _make_snapshot_file(snapshot_dir: Path, name: str, *, age_seconds: float = 0) -> Path:
    path = snapshot_dir / name
    path.write_text(json.dumps({"nodes_export": {"nodes": []}}), encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def _publish_base(paths, snapshot_dir: Path, name: str) -> str:
    account = paths.account_root.name
    base = write_snapshot_candidate(
        snapshot_dir / name,
        {"nodes": [s02_node("base")]},
        account_scope_id=account,
        epoch_number=0,
        epoch_id="b" * 32,
        incorporated_through_seq=0,
        publication_kind="full_refresh",
    )
    from workflowy_mcp.coherence import (
        DirtyBarrierState,
        initialize_manifest_if_absent,
        manifest_with_updates,
        write_manifest,
    )

    # The client constructor already initialized a bootstrap manifest (no
    # base); adopt it first, then CAS in the base lineage.  Epoch 0 keeps a
    # null predecessor, so keep its epoch id for consistency.
    existing = initialize_manifest_if_absent(
        paths,
        CoherenceManifest(
            schema_version=1,
            account_scope_id=account,
            epoch_number=0,
            epoch_id="b" * 32,
            base_snapshot_path=normalize_snapshot_path(base),
            base_incorporated_through_seq=0,
            journal_floor_seq=1,
            journal_head_seq=0,
            coherence_mode="transition",
            dirty_status=DirtyBarrierState.clean(account),
            created_at=NOW,
            updated_at=NOW,
        ),
    )
    if existing.base_snapshot_path is None:
        write_manifest(
            paths,
            manifest_with_updates(
                existing,
                epoch_id="b" * 32,
                base_snapshot_path=normalize_snapshot_path(base),
                coherence_mode="transition",
            ),
            expected_epoch_id=existing.epoch_id,
            expected_head_seq=existing.journal_head_seq,
        )
    return base


def test_gc_protects_manifest_base_regardless_of_age(tmp_path, monkeypatch):
    client, paths, jobs_root, snapshot_dir = _gc_fixture(tmp_path, monkeypatch)
    base = _publish_base(paths, snapshot_dir, "20260101-000000-000000_pid1_nodes_export_snapshot.json")
    ancient = time.time() - 3 * 86400
    os.utime(base, (ancient, ancient))
    # Enough newer files to exceed max_keep.
    for index in range(3):
        _make_snapshot_file(
            snapshot_dir, f"2026010{index + 2}-000000-000000_pid1_nodes_export_snapshot.json"
        )
    result = client._gc_nodes_export_snapshots_diagnostic(max_keep=1, min_age_seconds=1)
    assert result["aborted"] is False
    assert Path(base).exists()
    assert Path(base).name in result["protected"]


def test_gc_protects_active_claim_candidate_and_active_job_snapshot(tmp_path, monkeypatch):
    client, paths, jobs_root, snapshot_dir = _gc_fixture(tmp_path, monkeypatch)
    _publish_base(paths, snapshot_dir, "20260101-000000-000000_pid1_nodes_export_snapshot.json")
    # An unreferenced OLD candidate is still collected.  (Name ordering
    # matters: max_keep retains the lexicographically NEWEST name, so the
    # unreferenced file must not be the newest.)
    unreferenced = _make_snapshot_file(
        snapshot_dir, "20260102-000000-000000_pid1_nodes_export_snapshot.json", age_seconds=90000
    )
    # Active claim naming a candidate.
    candidate = _make_snapshot_file(
        snapshot_dir, "20260103-000000-000000_pid1_nodes_export_snapshot.json", age_seconds=90000
    )
    manifest = read_manifest(paths, required=True)
    seed_publication_record(
        paths,
        status="publishing",
        requested_hard_generation=0,
        requested_external_generation=0,
        target_epoch_number=manifest.epoch_number + 1,
        target_epoch_id="e" * 32,
        started_at=NOW,
        updated_at=NOW,
        result=None,
        candidate_snapshot_path=normalize_snapshot_path(candidate),
    )
    # Historical job metadata no longer pins a snapshot. A newer decoy
    # keeps max_keep from independently retaining this job-only candidate.
    job_snapshot = _make_snapshot_file(
        snapshot_dir, "20260104-000000-000000_pid1_nodes_export_snapshot.json", age_seconds=90000
    )
    (jobs_root / "job-running.json").write_text(
        json.dumps(
            {
                "id": "job-running",
                "status": "running",
                "snapshot_path": str(job_snapshot),
            }
        ),
        encoding="utf-8",
    )
    _make_snapshot_file(
        snapshot_dir, "20260105-000000-000000_pid1_nodes_export_snapshot.json", age_seconds=90000
    )
    if os.environ.get("CUT2B_NEUTRALIZE_JOB_BASIS_GC"):
        from workflowy_mcp.coherence import snapshot_store
        real_collect = snapshot_store.collect_protected_snapshot_paths

        def job_pin(*args, **kwargs):
            protected, diagnostics = real_collect(*args, **kwargs)
            protected.add(normalize_snapshot_path(job_snapshot))
            return protected, diagnostics

        monkeypatch.setattr(snapshot_store, "collect_protected_snapshot_paths", job_pin)
    result = client._gc_nodes_export_snapshots_diagnostic(max_keep=1, min_age_seconds=1)
    assert result["aborted"] is False
    assert candidate.exists()
    assert not job_snapshot.exists(), "historical job-only snapshot must be eligible for GC"
    assert job_snapshot.name in result["deleted"]
    assert not unreferenced.exists()
    assert unreferenced.name in result["deleted"]


def test_gc_aborts_conservatively_on_malformed_manifest(tmp_path, monkeypatch):
    client, paths, jobs_root, snapshot_dir = _gc_fixture(tmp_path, monkeypatch)
    _publish_base(paths, snapshot_dir, "20260101-000000-000000_pid1_nodes_export_snapshot.json")
    # Corrupt the manifest AFTER publishing.
    paths.manifest_path.write_text("{corrupt", encoding="utf-8")
    old_file = _make_snapshot_file(
        snapshot_dir, "20260102-000000-000000_pid1_nodes_export_snapshot.json", age_seconds=90000
    )
    for index in range(2, 5):
        _make_snapshot_file(
            snapshot_dir, f"2026010{index + 2}-000000-000000_pid1_nodes_export_snapshot.json",
            age_seconds=90000,
        )
    result = client._gc_nodes_export_snapshots_diagnostic(max_keep=1, min_age_seconds=1)
    assert result["aborted"] is True
    # NOTHING was deleted.
    assert old_file.exists()
    assert result["deleted"] == []


def test_gc_aborts_on_parseable_but_schema_invalid_manifest(tmp_path, monkeypatch):
    """A4: a parseable future-version manifest must abort GC exactly like an
    unparseable one (unknown versions fail closed)."""
    client, paths, jobs_root, snapshot_dir = _gc_fixture(tmp_path, monkeypatch)
    base = _publish_base(paths, snapshot_dir, "20260101-000000-000000_pid1_nodes_export_snapshot.json")
    manifest = read_manifest(paths, required=True)
    payload = manifest.to_dict()
    payload["schema_version"] = 999  # parseable, but unknown future version
    paths.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    victim = _make_snapshot_file(
        snapshot_dir, "20260102-000000-000000_pid1_nodes_export_snapshot.json", age_seconds=90000
    )
    for index in range(3, 5):
        _make_snapshot_file(
            snapshot_dir, f"2026010{index}-000000-000000_pid1_nodes_export_snapshot.json",
            age_seconds=90000,
        )
    result = client._gc_nodes_export_snapshots_diagnostic(max_keep=1, min_age_seconds=1)
    assert result["aborted"] is True
    assert result["deleted"] == []
    assert victim.exists()
    assert base and Path(base).exists()


def test_gc_collects_unreferenced_candidates_by_age_and_count(tmp_path, monkeypatch):
    client, paths, jobs_root, snapshot_dir = _gc_fixture(tmp_path, monkeypatch)
    _publish_base(paths, snapshot_dir, "20260101-000000-000000_pid1_nodes_export_snapshot.json")
    victims = [
        _make_snapshot_file(
            snapshot_dir, f"2026010{index}-000000-000000_pid1_nodes_export_snapshot.json",
            age_seconds=90000,
        )
        for index in range(2, 5)
    ]
    result = client._gc_nodes_export_snapshots_diagnostic(max_keep=1, min_age_seconds=1)
    assert result["aborted"] is False
    # max_keep retains the lexicographically NEWEST name; the two older
    # unreferenced candidates are collected.
    assert victims[0].name not in result["kept_recent"]
    assert not victims[0].exists()
    assert not victims[1].exists()
    assert victims[2].exists()  # newest by name → kept by max_keep
    assert len(result["deleted"]) == 2


def test_collect_protected_snapshot_paths_never_edits_state(tmp_path, monkeypatch):
    client, paths, jobs_root, snapshot_dir = _gc_fixture(tmp_path, monkeypatch)
    base = _publish_base(paths, snapshot_dir, "20260101-000000-000000_pid1_nodes_export_snapshot.json")
    before = {path: path.read_bytes() for path in jobs_root.rglob("*.json")}
    job = jobs_root / "job-forbidden-read.json"
    job.write_text(json.dumps({"status": "running", "snapshot_path": base}), encoding="utf-8")
    before[job] = job.read_bytes()
    real_open = Path.open
    opened_jobs = []

    def watched_open(path, *args, **kwargs):
        if path.parent == jobs_root and path.suffix == ".json":
            opened_jobs.append(path.name)
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as watch:
        watch.setattr(Path, "open", watched_open)
        if os.environ.get("CUT2B_NEUTRALIZE_NO_JOB_OPEN"):
            job.read_text(encoding="utf-8")
        protected, diagnostics = collect_protected_snapshot_paths(
            jobs_root, active_candidate_paths=_active_candidate_snapshot_paths(jobs_root),
        )
    assert opened_jobs == [], f"collector opened job JSON: {opened_jobs}"
    assert diagnostics["jobs_read"] == 0
    assert {path: path.read_bytes() for path in jobs_root.rglob("*.json")} == before
    assert diagnostics["abort"] is False
    assert normalize_snapshot_path(base) in protected
