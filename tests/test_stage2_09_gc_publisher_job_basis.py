"""Stage2-09: three GC passes protect old base, active candidate, new base.

Historical job-basis test names remain; no job-owner process or job GC pin.
The rendezvous precedes the real publisher's single strict candidate read.
"""

from __future__ import annotations

import asyncio
import json
import os

from coherence_harness import make_coherence_root, s02_node, s04_install_base_manifest
from test_coherence_s02_bootstrap_gc import _make_client
from test_coherence_s02_refresh_claim import async_test

from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp.coherence import refresh_authority as ra
from workflowy_mcp.coherence.schema import read_manifest
from workflowy_mcp.coherence.snapshot_store import write_snapshot_candidate

_API_BASE_URL = "https://workflowy.com/api/v1"
_API_KEY = "test-key-1234"
_B0_NAME = "20250101-000000-000001_1_nodes_export_snapshot.json"
_GARBAGE1_NAME = "20250101-000000-000002_1_nodes_export_snapshot.json"
_GARBAGE2_NAME = "20250101-000000-000003_1_nodes_export_snapshot.json"
_GARBAGE3_NAME = "99999999-999999-999999_99999_nodes_export_snapshot.json"


def _account() -> str:
    return gov.derive_account_scope_id(gov.normalize_api_base_url(_API_BASE_URL), _API_KEY)


def _seed(root):
    """Epoch-0 lineage plus controlled-name unreferenced garbage."""
    account = _account()
    paths = make_coherence_root(root, account)
    snapshot_dir = root / "nodes_export_cache"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    b0 = snapshot_dir / _B0_NAME
    write_snapshot_candidate(
        str(b0), {"nodes": [s02_node("base")]}, account_scope_id=account,
        epoch_number=0, epoch_id="b" * 32, incorporated_through_seq=0, publication_kind="full_refresh",
    )
    for name in (_GARBAGE1_NAME, _GARBAGE2_NAME):
        (snapshot_dir / name).write_text(json.dumps({"nodes": []}), encoding="utf-8")
    s04_install_base_manifest(paths, base_snapshot_path=str(b0), epoch_id="b" * 32)
    return paths, snapshot_dir


async def _run_gc_publication(root, monkeypatch, *, neutralize=False):
    paths, snapshot_dir = _seed(root)
    client = publisher = gc = None
    release_fetch, release_publish = asyncio.Event(), asyncio.Event()
    fetching, publishing = asyncio.Event(), asyncio.Event()
    passes = (1, 2) if neutralize else (1, 2, 3)
    run = {index: asyncio.Event() for index in passes}
    done = {index: asyncio.Event() for index in passes}
    results = {}
    try:
        client, jobs_root, _ = _make_client(root, monkeypatch)
        monkeypatch.setattr(client, "_get_nodes_export_snapshot_dir", lambda: str(snapshot_dir))
        assert jobs_root == root / "cartographer_jobs"
        if neutralize or os.environ.get("STAGE2_NEUTRALIZE_CLAIM_EDGE"):
            # Test-only: remove just the candidate projection; base stays.
            monkeypatch.setattr(ra, "_active_candidate_snapshot_paths", lambda jobs_root: frozenset())
        real_publish = ra.publish_epoch

        async def parked_publish(**kwargs):
            publishing.set()
            await release_publish.wait()
            return await real_publish(**kwargs)
        monkeypatch.setattr(ra, "publish_epoch", parked_publish)

        async def fetch():
            fetching.set()
            await release_fetch.wait()
            return {"nodes": [s02_node("base", name="refreshed")]}

        async def scan():
            for index in passes:
                await run[index].wait()
                results[index] = client._gc_nodes_export_snapshots_diagnostic(max_keep=1, min_age_seconds=0)
                done[index].set()

        gc = asyncio.create_task(scan())
        publisher = asyncio.create_task(ra.run_full_refresh(
            paths=paths, fetch=fetch, refresh_kind="system", settle_seconds=0,
            snapshot_dir=str(snapshot_dir), poll_seconds=0.05,
        ))
        await asyncio.wait_for(fetching.wait(), 180)
        claim = ra._read_publication_record(paths)
        assert claim is not None and not claim.is_terminal and claim.cut_token is not None
        run[1].set()
        await asyncio.wait_for(done[1].wait(), 120)
        release_fetch.set()
        await asyncio.wait_for(publishing.wait(), 180)
        claim = ra._read_publication_record(paths)
        assert claim is not None and claim.candidate_snapshot_path
        b1_name = os.path.basename(claim.candidate_snapshot_path)
        (snapshot_dir / _GARBAGE3_NAME).write_text(json.dumps({"nodes": []}), encoding="utf-8")
        run[2].set()
        await asyncio.wait_for(done[2].wait(), 120)
        release_publish.set()
        outcome = (await asyncio.gather(publisher, return_exceptions=True))[0]
        pub = {
            "published": not isinstance(outcome, BaseException) and outcome.status == "published",
            "epoch_number": None if isinstance(outcome, BaseException) else outcome.epoch_number,
            "error": str(outcome) if isinstance(outcome, BaseException) else None,
        }
        if 3 in passes:
            run[3].set()
            await asyncio.wait_for(done[3].wait(), 120)
        await gc
        return paths, b1_name, results, pub
    finally:
        release_fetch.set()
        release_publish.set()
        for event in run.values():
            event.set()
        await asyncio.gather(*(task for task in (publisher, gc) if task is not None), return_exceptions=True)
        if client is not None:
            await client.close()
        ra._PUBLICATION_RECORDS.pop(ra._publication_key(paths), None)


@async_test
async def test_gc_protects_bases_and_job_basis_across_epoch_publication(tmp_path, monkeypatch):
    """Historical name: B0 before publish, candidate before parse, B1 after."""
    paths, b1_name, runs, pub = await _run_gc_publication(tmp_path / "share", monkeypatch)
    run1, run2, run3 = runs[1], runs[2], runs[3]
    assert run1["aborted"] is False
    assert _B0_NAME in run1["protected"]
    assert run1["deleted"] == [_GARBAGE1_NAME]
    assert _GARBAGE2_NAME in run1["kept_recent"]
    assert run2["aborted"] is False
    assert b1_name in run2["protected"]
    assert _B0_NAME in run2["protected"]
    assert run2["deleted"] == [_GARBAGE2_NAME]
    assert pub["published"] is True
    assert pub["epoch_number"] == 1
    assert read_manifest(paths).epoch_number == 1
    assert run3["aborted"] is False
    assert _B0_NAME in run3["deleted"]
    assert b1_name in run3["protected"] + run3["kept_recent"]


@async_test
async def test_mutation_check_claim_edge_disabled_deletes_candidate_and_fails_publish(tmp_path, monkeypatch):
    """The named test-only projection neutralization predicts deletion and failure."""
    paths, b1_name, runs, pub = await _run_gc_publication(tmp_path / "share", monkeypatch, neutralize=True)
    run1, run2 = runs[1], runs[2]
    assert run1["aborted"] is False
    assert _B0_NAME in run1["protected"]
    assert run1["deleted"] == [_GARBAGE1_NAME]
    assert run2["aborted"] is False
    assert b1_name in run2["deleted"]
    assert _B0_NAME in run2["protected"]
    assert pub["published"] is False
    assert pub["error"]
    assert read_manifest(paths).epoch_number == 0
