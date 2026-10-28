"""CUT-2A end-state witnesses; isolated lineages and fake transport only."""

from __future__ import annotations

import asyncio
import copy
import os
from dataclasses import fields, replace
from pathlib import Path

import pytest

from coherence_harness import publish_fixture_head, s02_node
from test_coherence_s03_operation_boundary import _make_lineage_client, async_test
from n07_job_harness import scrubbed_runtime, task_core_environment, claimed_task_environment
from test_n07_carto_final_refresh import final_environment

from workflowy_mcp.coherence.operation_cache import (
    cache_operation_boundary,
    current_operation_cache,
)
from workflowy_mcp.coherence.schema import read_manifest


@pytest.fixture(autouse=True)
def _publication_records_cleanup(tmp_path, monkeypatch):
    """After joined tasks/loops, remove only this fixture's account-root slots."""
    from workflowy_mcp.coherence import refresh_authority as ra
    from workflowy_mcp.coherence import schema
    if os.environ.get("CUT2A_NEUTRALIZE_LEGACY_FILTER"):
        import inspect
        import textwrap
        source = textwrap.dedent(inspect.getsource(schema.CoherenceManifest.from_dict.__func__))
        line = '    payload = {k: v for k, v in payload.items() if k != "covered_barrier_required_lease_ids"}\n'
        assert source.count(line) == 1
        namespace = dict(vars(schema))
        exec(compile(source.replace(line, ""), schema.__file__, "exec"), namespace)
        monkeypatch.setattr(schema.CoherenceManifest, "from_dict", namespace["from_dict"])
    if os.environ.get("CUT2A_NEUTRALIZE_LEGACY_OUTPUT"):
        from workflowy_mcp.coherence import durable_io
        real_write = durable_io.atomic_write_json_durable
        def obsolete_output(path, payload, **kwargs):
            if Path(path).name == "manifest.json":
                payload = {**payload, "covered_barrier_required_lease_ids": []}
            return real_write(path, payload, **kwargs)
        monkeypatch.setattr(durable_io, "atomic_write_json_durable", obsolete_output)
    prefix = os.path.normcase(os.fspath(tmp_path.resolve())) + os.sep
    yield
    for key in tuple(ra._PUBLICATION_RECORDS):
        if key.startswith(prefix):
            ra._PUBLICATION_RECORDS.pop(key, None)


@async_test
async def test_legacy_claim_and_marker_artifacts_are_ignored_untouched(
    tmp_path, monkeypatch, final_environment
):
    """T02: stale malformed carriers are silent, inert bytes, never cleanup work."""
    import builtins
    import glob
    import io
    import logging
    from workflowy_mcp.client import api_client_etch
    from test_coherence_s03_operation_boundary import _make_client

    server = final_environment.server
    for present in (True, False):
        root = tmp_path / ("legacy-present" if present else "artifact-free")
        setup_client, paths = _make_lineage_client(root, monkeypatch)
        await setup_client.close()
        jobs = root / "cartographer_jobs"
        retired_claim = paths.account_root / "refresh_publication.json"
        retired_dir = jobs / ".refresh_claims"
        marker = retired_dir / "old.claim.json"
        if present:
            retired_claim.write_bytes(b"{malformed stale publication")
            retired_dir.mkdir()
            marker.write_bytes(b"{malformed stale marker")
            before = (retired_claim.read_bytes(), marker.read_bytes(), sorted(p.name for p in retired_dir.iterdir()))
        touched = []

        def targets_retired(value):
            if isinstance(value, (str, bytes, os.PathLike)):
                text = os.fsdecode(value)
                return "refresh_publication.json" in text or ".refresh_claims" in text
            if isinstance(value, (tuple, list)):
                return any(targets_retired(part) for part in value)
            if isinstance(value, dict):
                return any(targets_retired(part) for part in value.values())
            return False

        def install_spy(patch, owner, name):
            original = getattr(owner, name)
            def guarded(*args, **kwargs):
                if targets_retired(args) or targets_retired(kwargs):
                    touched.append(name)
                    raise AssertionError("retired publication/marker artifact touched: " + name)
                return original(*args, **kwargs)
            patch.setattr(owner, name, guarded)

        async def fake_fetch(*args, **kwargs):
            return {"nodes": [s02_node("A", name="refreshed"), s02_node("B")]}

        with monkeypatch.context() as patch:
            # Track swallowed failures too: touched is asserted after all surfaces.
            for owner, names in (
                (Path, ("open", "read_text", "read_bytes", "write_text", "write_bytes", "stat", "lstat", "glob", "rglob", "unlink", "mkdir", "rmdir", "touch", "rename", "replace", "iterdir", "exists", "is_file", "is_dir")),
                (os, ("open", "stat", "lstat", "scandir", "listdir", "mkdir", "makedirs", "unlink", "remove", "rmdir", "rename", "replace")),
                (builtins, ("open",)), (io, ("open",)),
                (glob, ("glob", "iglob")),
                (logging.Logger, ("warning", "error", "log")),
                (server, ("log_event",)),
            ):
                for name in names:
                    install_spy(patch, owner, name)

            # Construction performs the real warm load AFTER the traps exist.
            client, _, _ = _make_client(root, patch)
            patch.setattr(api_client_etch, "fetch_nodes_export_uncached", fake_fetch)
            patch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(jobs))
            patch.setattr(server, "get_client", lambda: client)
            try:
                warm = client._capture_view_and_dirty()[0]
                assert warm.epoch_id == read_manifest(paths).epoch_id
                full = await client.refresh_nodes_export_cache(settle_seconds=0)
                assert full["success"] is True
                compat = await client.patch_nodes_export_snapshot_on_disk([{"id": "A", "name": "patched"}])
                assert compat["success"] is True
                collected = client._gc_nodes_export_snapshots_diagnostic(max_keep=1, min_age_seconds=0)
                assert collected["aborted"] is False
                await server._refresh_carto_job_tracker_once()
                cleared = await server._clear_carto_job_history(str(jobs))
                assert cleared["success"] is True and "deleted_claims" not in cleared
                assert client._capture_view_and_dirty()[0].nodes_by_id["A"]["name"] == "patched"
            finally:
                await client.close()
        assert touched == []
        if present:
            assert (retired_claim.read_bytes(), marker.read_bytes(), sorted(p.name for p in retired_dir.iterdir())) == before
        else:
            assert not retired_claim.exists() and not retired_dir.exists()


@async_test
async def test_full_joined_and_compat_publications_pin_local_epoch_without_adoption(
    tmp_path, monkeypatch
):
    """T03: both real full wrappers and compat publish before a new boundary."""
    from workflowy_mcp.client import api_client_etch
    from workflowy_mcp.coherence import refresh_authority

    client, paths = _make_lineage_client(tmp_path, monkeypatch)
    fetch_started = asyncio.Event()
    joined = asyncio.Event()
    release_fetch = asyncio.Event()
    fetches = []
    join_outcomes = []
    real_join = refresh_authority._join_refresh

    async def observe_join(*args, **kwargs):
        joined.set()
        outcome = await real_join(*args, **kwargs)
        join_outcomes.append(outcome)
        return outcome

    async def fake_fetch(*args, **kwargs):
        fetches.append("fetch")
        fetch_started.set()
        await release_fetch.wait()
        return {"nodes": [s02_node("A", name="fresh"), s02_node("B", name="beta")]}

    monkeypatch.setattr(refresh_authority, "_join_refresh", observe_join)
    monkeypatch.setattr(api_client_etch, "fetch_nodes_export_uncached", fake_fetch)
    client._mark_nodes_export_dirty(["A", "B"])
    observed = {}

    async def refresh_and_capture(label):
        result = await client.refresh_nodes_export_cache(settle_seconds=0)
        manifest = read_manifest(paths, required=True)
        async with cache_operation_boundary(client, label) as snapshot:
            assert snapshot.view.epoch_number == manifest.epoch_number
            assert snapshot.view.epoch_id == manifest.epoch_id
            assert snapshot.nodes_by_id["A"]["name"] == "fresh"
            assert snapshot.dirty_ids == frozenset()
            assert snapshot.view is client._capture_view_and_dirty()[0]
            observed[label] = snapshot.view
        return result

    owner = asyncio.create_task(refresh_and_capture("owner"))
    await fetch_started.wait()
    joiner = asyncio.create_task(refresh_and_capture("joiner"))
    await joined.wait()
    assert fetches == ["fetch"]
    release_fetch.set()
    owner_result, joined_result = await asyncio.gather(owner, joiner)
    assert owner_result["success"] is True
    assert owner_result["deduplicated"] is False
    assert joined_result["success"] is True
    assert len(join_outcomes) == 1 and join_outcomes[0].status == "joined"
    assert joined_result["deduplicated"] is True
    assert owner_result["epoch_id"] == joined_result["epoch_id"]
    assert observed["owner"].epoch_id == observed["joiner"].epoch_id
    assert fetches == ["fetch"]

    client._mark_nodes_export_dirty(["A", "B"])
    compat_result = await client.patch_nodes_export_snapshot_on_disk(
        [{"id": "A", "name": "patched"}]
    )
    assert compat_result["success"] is True
    manifest = read_manifest(paths, required=True)
    async with cache_operation_boundary(client, "compat") as snapshot:
        assert snapshot.view.epoch_id == manifest.epoch_id
        assert snapshot.view.epoch_number == observed["owner"].epoch_number + 1
        assert snapshot.nodes_by_id["A"]["name"] == "patched"
        assert snapshot.nodes_by_id["B"]["name"] == "beta"
        assert snapshot.dirty_ids == frozenset({"B"})
        assert snapshot.view is client._capture_view_and_dirty()[0]
    await client.close()


@pytest.mark.parametrize("legacy_value", [[], ["1" * 32], 17, {"obsolete": [False]}, None, "absent"])
@async_test
async def test_legacy_manifest_epoch737_loads_and_next_write_omits_receipt(tmp_path, legacy_value):
    """T01: Dan-shaped raw legacy input is discarded without inspecting its value."""
    import json
    from coherence_harness import ACCOUNT_A, RecordedSleep, make_coherence_root, s02_fetch, s04_write_base_snapshot
    from workflowy_mcp.coherence import CoherenceManifest, run_full_refresh
    from workflowy_mcp.coherence.errors import CoherenceSchemaError

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    snapshot_dir = tmp_path / "snapshots"
    base = s04_write_base_snapshot(
        paths, snapshot_dir, [s02_node("base")], epoch_number=737, epoch_id="7" * 32,
    )
    payload = read_manifest(paths).to_dict()
    payload.update(
        epoch_number=737, epoch_id="7" * 32, previous_epoch_id="6" * 32,
        base_snapshot_path=str(base), base_incorporated_through_seq=0,
        journal_floor_seq=1, journal_head_seq=0, coherence_mode="transition",
    )
    if legacy_value != "absent":
        payload["covered_barrier_required_lease_ids"] = legacy_value
    original = copy.deepcopy(payload)
    loaded = CoherenceManifest.from_dict(payload)
    assert payload == original
    expected = {key: value for key, value in original.items() if key != "covered_barrier_required_lease_ids"}
    assert loaded.to_dict() == expected
    with pytest.raises(CoherenceSchemaError, match="unknown"):
        CoherenceManifest.from_dict({**payload, "another_unknown_key": True})
    paths.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    assert read_manifest(paths) == loaded
    outcome = await run_full_refresh(
        paths=paths, fetch=s02_fetch({"nodes": [s02_node("fresh")]}), settle_seconds=0,
        snapshot_dir=snapshot_dir, sleep=RecordedSleep(),
    )
    emitted = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
    assert outcome.epoch_number == 738
    assert emitted["previous_epoch_id"] == loaded.epoch_id
    assert emitted["coherence_mode"] == "transition" and emitted["schema_version"] == 1
    assert "covered_barrier_required_lease_ids" not in emitted
    assert payload == original


@async_test
async def test_record_serializes_uncovered_successor_and_accounts_are_isolated(tmp_path, monkeypatch):
    """T05: two uncovered contenders share one successor; independent roots don't collide."""
    from coherence_harness import ACCOUNT_A, ACCOUNT_B, make_coherence_root, s02_set_barrier, s02_set_external_dirt
    from workflowy_mcp.coherence import refresh_authority as ra
    from workflowy_mcp.coherence.schema import read_external_dirt

    if os.environ.get("CUT2A_NEUTRALIZE_TASK_KEY"):
        real_key = ra._publication_key
        monkeypatch.setattr(ra, "_publication_key", lambda paths: real_key(paths) + f":{id(asyncio.current_task())}")
    if os.environ.get("CUT2A_NEUTRALIZE_GLOBAL_KEY"):
        monkeypatch.setattr(ra, "_publication_key", lambda paths: "single-global-slot")
    paths = make_coherence_root(tmp_path / "one", ACCOUNT_A)
    other = make_coherence_root(tmp_path / "other-root", ACCOUNT_A)
    other_account = make_coherence_root(tmp_path / "other-account", ACCOUNT_B)
    holding, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
    waiting = [asyncio.Event(), asyncio.Event()]
    fetches = []
    real_join = ra._join_refresh
    async def observe_join(*args, **kwargs):
        joined.set()
        return await real_join(*args, **kwargs)
    monkeypatch.setattr(ra, "_join_refresh", observe_join)
    async def old_fetch():
        fetches.append("old")
        holding.set()
        await release.wait()
        return {"nodes": [s02_node("old")]}
    async def successor_fetch():
        fetches.append("successor")
        await joined.wait()
        return {"nodes": [s02_node("new")]}
    async def contender(index):
        async def poll(seconds):
            if seconds > 0:
                waiting[index].set()
            await asyncio.sleep(seconds)
        return await ra.run_full_refresh(
            paths=paths, fetch=successor_fetch, settle_seconds=0, snapshot_dir=tmp_path / "snapshots",
            sleep=poll, poll_seconds=0.001,
        )
    owner = asyncio.create_task(ra.run_full_refresh(
        paths=paths, fetch=old_fetch, settle_seconds=0, snapshot_dir=tmp_path / "snapshots", poll_seconds=0.001,
    ))
    contenders = []
    try:
        await asyncio.wait_for(holding.wait(), 10)
        active = ra._read_publication_record(paths)
        key = os.path.normcase(os.fspath(paths.account_root))
        assert active is not None, "owner slot is not process-global"
        # Read in another task as well: ownership is not task-/loop-local.
        async def read_in_task():
            return ra._read_publication_record(paths)
        assert await asyncio.create_task(read_in_task()) is active
        for independent in (other, other_account):
            acquired = await ra._acquire_or_join(
                independent, refresh_kind="system", snapshot_dir=tmp_path / "independent-snapshots",
                sleep=asyncio.sleep, poll_seconds=0.001, max_wait_seconds=0.01,
                cancel_check=None, fs_ops=None,
            )
            assert acquired.decision == "owner"
            assert acquired.claim.claim_id != active.claim_id
            assert ra._read_publication_record(paths) is active
        assert ra._publication_key(paths) == key
        assert ra._PUBLICATION_RECORDS[key] is active
        s02_set_barrier(paths, 2)
        s02_set_external_dirt(paths, 3)
        contenders = [asyncio.create_task(contender(index)) for index in range(2)]
        for event in waiting:
            await asyncio.wait_for(event.wait(), 10)
        assert fetches == ["old"] and not joined.is_set()
        release.set()
        previous, first, second = await asyncio.wait_for(asyncio.gather(owner, *contenders), 10)
        assert previous.status == "published"
        assert not previous.hard_barrier_cleared and not previous.external_dirt_cleared
        assert sorted(item.status for item in (first, second)) == ["joined", "published"]
        assert first.claim_id == second.claim_id != previous.claim_id
        assert first.epoch_number == second.epoch_number == previous.epoch_number + 1
        assert fetches == ["old", "successor"]
        assert read_manifest(paths).dirty_status.active is False
        assert read_external_dirt(paths).active is False
    finally:
        release.set()
        joined.set()
        await asyncio.gather(owner, *contenders, return_exceptions=True)


@pytest.mark.parametrize("kind,seam", [
    ("full", "fetch_error"), ("full", "materialize_error"),
    ("full", "settle_cancel"), ("full", "download_cancel"), ("full", "materialize_cancel"),
    ("compat", "materialize_error"), ("compat", "materialize_cancel"),
    ("checkpoint", "materialize_cancel"), ("full", "joiner_cancel"),
])
@async_test
async def test_owned_record_terminalizes_on_failure_and_cancellation(tmp_path, monkeypatch, kind, seam):
    """T06: only the owned record fails; joiner cancellation leaves its owner alone."""
    from coherence_harness import s02_set_barrier, s02_set_external_dirt, s04_append_n, s04_checkpoint_config
    from test_stage2_06_refresh_compat_contention import _based_root
    from workflowy_mcp.coherence import checkpoint as cp, refresh_authority as ra
    from workflowy_mcp.coherence.schema import read_external_dirt

    if os.environ.get("CUT2A_NEUTRALIZE_TERMINALIZE"):
        async def no_terminal(*args, **kwargs):
            pass
        monkeypatch.setattr(ra, "_terminalize_failed", no_terminal)
        monkeypatch.setattr(cp, "_terminalize_checkpoint_quiet", no_terminal)
    if os.environ.get("CUT2A_NEUTRALIZE_TERMINAL_TOKEN"):
        real_status = ra._write_claim_status
        async def wrong_token(paths, record, status, **kwargs):
            if status == "failed":
                record = replace(record, claim_id="wrong-terminal-token")
            return await real_status(paths, record, status, **kwargs)
        monkeypatch.setattr(ra, "_write_claim_status", wrong_token)
        monkeypatch.setattr(cp, "_write_claim_status", wrong_token)
    paths, snapshot_dir = _based_root(tmp_path / "fixture")
    if seam == "joiner_cancel":
        holding, joined, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        real_join = ra._join_refresh
        async def observe_join(*args, **kwargs):
            joined.set()
            return await real_join(*args, **kwargs)
        monkeypatch.setattr(ra, "_join_refresh", observe_join)
        async def fetch():
            holding.set()
            await release.wait()
            return {"nodes": [s02_node("base")]}
        owner = asyncio.create_task(ra.run_full_refresh(
            paths=paths, fetch=fetch, settle_seconds=0, snapshot_dir=snapshot_dir, poll_seconds=0.001,
        ))
        joiner = None
        try:
            await asyncio.wait_for(holding.wait(), 10)
            active = ra._read_publication_record(paths)
            joiner = asyncio.create_task(ra.run_full_refresh(
                paths=paths, fetch=fetch, settle_seconds=0, snapshot_dir=snapshot_dir, poll_seconds=0.001,
            ))
            await asyncio.wait_for(joined.wait(), 10)
            joiner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await joiner
            assert ra._read_publication_record(paths) is active
            assert active.status == "downloading" and not active.is_terminal
            release.set()
            assert (await owner).status == "published"
        finally:
            release.set()
            await asyncio.gather(*(task for task in (owner, joiner) if task is not None), return_exceptions=True)
        return

    if kind == "checkpoint":
        s04_append_n(paths, 1)
    s02_set_barrier(paths, 3)
    s02_set_external_dirt(paths, 4)
    before = paths.manifest_path.read_bytes(), paths.external_dirt_path.read_bytes()
    entered, release = asyncio.Event(), asyncio.Event()
    owned = []
    failure = asyncio.CancelledError("test materialization cancellation") if "cancel" in seam else RuntimeError("test failed work")

    def observe_owner():
        record = ra._read_publication_record(paths)
        assert record is not None and not record.is_terminal
        owned.append(record.claim_id)

    async def sleep(seconds):
        if seam == "settle_cancel":
            observe_owner()
            entered.set()
            await release.wait()
        else:
            await asyncio.sleep(0)

    async def fetch():
        if seam in {"fetch_error", "download_cancel"}:
            observe_owner()
        if seam == "fetch_error":
            raise failure
        if seam == "download_cancel":
            entered.set()
            await release.wait()
        return {"nodes": [s02_node("base")]}

    def fail_materialize(*args, **kwargs):
        observe_owner()
        raise failure

    with monkeypatch.context() as patch:
        if seam.startswith("materialize"):
            patch.setattr(cp if kind == "checkpoint" else ra, "materialize_payload_with_records", fail_materialize)
        if kind == "full":
            task = asyncio.create_task(ra.run_full_refresh(
                paths=paths, fetch=fetch, settle_seconds=0, snapshot_dir=snapshot_dir, sleep=sleep,
            ))
        elif kind == "compat":
            task = asyncio.create_task(ra.run_compat_patch(
                paths=paths, patches=[{"id": "base", "name": "patched"}], snapshot_dir=snapshot_dir,
            ))
        else:
            task = asyncio.create_task(cp.run_checkpoint_rotation(
                paths, snapshot_dir=snapshot_dir, config=s04_checkpoint_config(record_threshold=1),
            ))
        if seam in {"settle_cancel", "download_cancel"}:
            await asyncio.wait_for(entered.wait(), 10)
            task.cancel()
        if kind == "checkpoint":
            assert (await task).status == "failed"  # existing checkpoint taxonomy
        else:
            with pytest.raises(asyncio.CancelledError if "cancel" in seam else RuntimeError) as caught:
                await task
            if seam not in {"settle_cancel", "download_cancel"}:
                assert caught.value is failure
    failed = ra._read_publication_record(paths)
    assert owned == [failed.claim_id]
    assert failed.status == "failed" and failed.is_terminal
    assert failed.result["error_type"] == ("CancelledError" if "cancel" in seam else "RuntimeError")
    assert (paths.manifest_path.read_bytes(), paths.external_dirt_path.read_bytes()) == before
    assert read_manifest(paths).dirty_status.active and read_external_dirt(paths).active
    async def recover_fetch():
        return {"nodes": [s02_node("base")]}
    recovered = await ra.run_full_refresh(
        paths=paths, fetch=recover_fetch, settle_seconds=0, snapshot_dir=snapshot_dir,
    )
    assert recovered.status == "published" and recovered.claim_id != failed.claim_id


def _use_ceiling_variant(monkeypatch, hard_expression, external_expression):
    """Test-only source copy changes exactly the two authoritative ceiling reads."""
    import inspect
    from workflowy_mcp.coherence import epoch_publisher as ep, refresh_authority as ra
    source = inspect.getsource(ep.publish_epoch)
    for old, new in (
        ("effective_hard_ceiling = claim.cut_token.hard_barrier_generation", f"effective_hard_ceiling = {hard_expression}"),
        ("effective_external_ceiling = claim.cut_token.external_dirt_generation", f"effective_external_ceiling = {external_expression}"),
    ):
        assert source.count(old) == 1
        source = source.replace(old, new)
    namespace = dict(vars(ep))
    exec(compile(source, ep.__file__, "exec"), namespace)
    monkeypatch.setattr(ra, "publish_epoch", namespace["publish_epoch"])


@pytest.mark.parametrize("signal", ["hard", "external"])
@pytest.mark.parametrize("point", ["settle", "download"])
@async_test
async def test_in_memory_cut_preserves_post_cut_generations(tmp_path, monkeypatch, signal, point):
    """T07a: signals arriving during settle or fetch postdate the one memory cut."""
    from coherence_harness import ACCOUNT_A, make_coherence_root, s02_set_barrier, s02_set_external_dirt
    from workflowy_mcp.coherence import refresh_authority as ra
    from workflowy_mcp.coherence.schema import read_external_dirt
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    setter = s02_set_barrier if signal == "hard" else s02_set_external_dirt
    observed_cuts = []

    if os.environ.get("CUT2A_NEUTRALIZE_LATE_CEILINGS"):
        _use_ceiling_variant(
            monkeypatch, "read_manifest(paths, required=True).dirty_status.generation",
            "read_external_dirt(paths).generation",
        )
    real_publish = ra.publish_epoch
    async def publish(**kwargs):
        if os.environ.get("CUT2A_NEUTRALIZE_LATE_CUT"):
            record = ra._read_publication_record(paths)
            late = replace(
                record.cut_token,
                hard_barrier_generation=read_manifest(paths).dirty_status.generation,
                external_dirt_generation=read_external_dirt(paths).generation,
            )
            ra._store_publication_record(paths, replace(record, cut_token=late), expected_claim_id=record.claim_id)
            # Supply the SAME forged late token: the equality guard cannot mask it.
            kwargs.update(
                refresh_cut=late, clear_hard_through_generation=late.hard_barrier_generation,
                clear_external_through_generation=late.external_dirt_generation,
            )
        return await real_publish(**kwargs)
    monkeypatch.setattr(ra, "publish_epoch", publish)

    def late_signal():
        record = ra._read_publication_record(paths)
        assert record is not None and record.cut_token is not None
        observed_cuts.append(record.cut_token)
        setter(paths, 1)

    async def settle(seconds):
        if point == "settle":
            late_signal()
        await asyncio.sleep(0)
    async def fetch():
        if point == "download":
            late_signal()
        return {"nodes": [s02_node("base")]}
    outcome = await ra.run_full_refresh(
        paths=paths, fetch=fetch, settle_seconds=2.0, snapshot_dir=tmp_path / "snapshots", sleep=settle,
    )
    assert len(observed_cuts) == 1
    assert observed_cuts[0].hard_barrier_generation == observed_cuts[0].external_dirt_generation == 0
    state = read_manifest(paths).dirty_status if signal == "hard" else read_external_dirt(paths)
    assert state.active is True and state.generation == 1
    assert (outcome.hard_barrier_cleared if signal == "hard" else outcome.external_dirt_cleared) is False


@async_test
async def test_in_memory_cut_clears_pre_cut_generations(tmp_path, monkeypatch):
    """T07b: full clears covered generations monotonically; compat/checkpoint clear neither."""
    from coherence_harness import s02_set_barrier, s02_set_external_dirt, s04_append_n, s04_checkpoint_config
    from test_stage2_06_refresh_compat_contention import _based_root
    from workflowy_mcp.coherence import checkpoint as cp, refresh_authority as ra
    from workflowy_mcp.coherence.schema import read_external_dirt
    if os.environ.get("CUT2A_NEUTRALIZE_ZERO_CEILINGS"):
        _use_ceiling_variant(monkeypatch, "None", "None")
    for kind in ("full", "compat", "checkpoint"):
        paths, snapshot_dir = _based_root(tmp_path / kind)
        s02_set_barrier(paths, 5)
        s02_set_external_dirt(paths, 7)
        if kind == "full":
            async def fetch():
                return {"nodes": [s02_node("base")]}
            outcome = await ra.run_full_refresh(paths=paths, fetch=fetch, settle_seconds=0, snapshot_dir=snapshot_dir)
            assert outcome.hard_barrier_cleared and outcome.external_dirt_cleared
        elif kind == "compat":
            await ra.run_compat_patch(paths=paths, patches=[{"id": "base", "name": "patched"}], snapshot_dir=snapshot_dir)
        else:
            s04_append_n(paths, 1)
            result = await cp.run_checkpoint_rotation(
                paths, snapshot_dir=snapshot_dir, config=s04_checkpoint_config(record_threshold=1),
            )
            assert result.status == "published"
        hard, external = read_manifest(paths).dirty_status, read_external_dirt(paths)
        assert hard.generation == 5 and external.generation == 7
        assert hard.active is (kind != "full")
        assert external.active is (kind != "full")
        record = ra._read_publication_record(paths)
        if kind != "full":
            assert record.cut_token is None


@pytest.mark.parametrize("published", [False, True], ids=["unpublished", "durably-published"])
def test_restart_drops_active_record_and_keeps_durable_barrier(tmp_path, monkeypatch, published):
    """T08: a fresh process has no ownership to recover; durable state alone survives."""
    from coherence_harness import run_subprocess, s02_set_barrier, s02_set_external_dirt
    from test_coherence_s02_bootstrap_gc import _gc_fixture, _publish_base
    from workflowy_mcp.coherence import refresh_authority as ra
    from workflowy_mcp.coherence.schema import read_external_dirt

    client, paths, jobs_root, snapshot_dir = _gc_fixture(tmp_path, monkeypatch)
    _publish_base(paths, snapshot_dir, "20250101-000000-000001_pid1_nodes_export_snapshot.json")
    s02_set_barrier(paths, 5)
    s02_set_external_dirt(paths, 7)
    old = read_manifest(paths)
    before_files = set(snapshot_dir.iterdir())
    tests_dir = str(Path(__file__).resolve().parent)
    child = r'''
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, @TESTS@)
from coherence_harness import s02_node, s02_set_barrier, s02_set_external_dirt
from workflowy_mcp.coherence.paths import coherence_paths
from workflowy_mcp.coherence import refresh_authority as ra
root, account, published = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "1"
paths = coherence_paths(account, jobs_root=root / "cartographer_jobs")
real_publish, real_status = ra.publish_epoch, ra._write_claim_status
async def fetch():
    if published:
        s02_set_barrier(paths, 6)
        s02_set_external_dirt(paths, 8)
    return {"nodes": [s02_node("base")]}
async def parked_publish(**kwargs):
    record = ra._read_publication_record(paths)
    assert record is not None and record.candidate_snapshot_path
    if not published:
        os._exit(42)
    return await real_publish(**kwargs)
async def status(paths, record, state, **kwargs):
    if state == "completed":
        os._exit(42)
    return await real_status(paths, record, state, **kwargs)
ra.publish_epoch, ra._write_claim_status = parked_publish, status
asyncio.run(ra.run_full_refresh(paths=paths, fetch=fetch, settle_seconds=0, snapshot_dir=root / "snapshots"))
'''.replace("@TESTS@", repr(tests_dir))
    first = run_subprocess(child, str(tmp_path), paths.account_root.name, "1" if published else "0", timeout=60)
    assert first.returncode == 42, first.stderr
    after_exit = read_manifest(paths)
    assert after_exit.epoch_number == old.epoch_number + int(published)
    hard_generation, external_generation = (6, 8) if published else (5, 7)
    assert (after_exit.dirty_status.active, after_exit.dirty_status.generation) == (True, hard_generation)
    assert (read_external_dirt(paths).active, read_external_dirt(paths).generation) == (True, external_generation)
    candidates = set(snapshot_dir.iterdir()) - before_files
    assert len(candidates) == 1
    candidate = candidates.pop()
    for path in snapshot_dir.iterdir():
        os.utime(path, (1, 1))
    (snapshot_dir / "99999999-999999-999999_pid1_nodes_export_snapshot.json").write_text("{}")
    successor = r'''
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, @TESTS@)
import pytest
from coherence_harness import seed_publication_record, s02_node, s02_set_barrier
from test_coherence_s02_bootstrap_gc import _make_client
from workflowy_mcp.coherence import refresh_authority as ra
from workflowy_mcp.coherence.paths import coherence_paths
from workflowy_mcp.coherence.schema import read_manifest, read_external_dirt
root, account, candidate, published = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4] == "1"
paths = coherence_paths(account, jobs_root=root / "cartographer_jobs")
if os.environ.get("CUT2A_NEUTRALIZE_RECONSTRUCT_RECORD"):
    seed_publication_record(paths, status="publishing", candidate_snapshot_path=str(candidate))
assert ra._read_publication_record(paths) is None, "restart reconstructed ownership"
assert ra._PUBLICATION_RECORDS == {}, "fresh process retained publication memory"
if os.environ.get("CUT2A_NEUTRALIZE_RESTART_BARRIER"):
    s02_set_barrier(paths, 6 if published else 5, active=False)
manifest = read_manifest(paths)
assert (manifest.dirty_status.active, manifest.dirty_status.generation) == (True, 6 if published else 5)
assert (read_external_dirt(paths).active, read_external_dirt(paths).generation) == (True, 8 if published else 7)
durable_before = paths.manifest_path.read_bytes(), paths.external_dirt_path.read_bytes()
with pytest.MonkeyPatch.context() as patch:
    client, jobs_root, snapshot_dir = _make_client(root, patch)
    assert client._coherence_paths() == paths
    result = client._gc_nodes_export_snapshots_diagnostic(max_keep=1, min_age_seconds=0)
    assert result["aborted"] is False
    if published:
        assert candidate.exists() and candidate.name in result["protected"]
    else:
        assert not candidate.exists() and candidate.name in result["deleted"]
    assert (paths.manifest_path.read_bytes(), paths.external_dirt_path.read_bytes()) == durable_before
    async def fetch():
        return {"nodes": [s02_node("base")]}
    outcome = asyncio.run(ra.run_full_refresh(paths=paths, fetch=fetch, settle_seconds=0, snapshot_dir=snapshot_dir))
    assert outcome.status == "published"
    assert outcome.epoch_number == manifest.epoch_number + 1
    asyncio.run(client.close())
'''.replace("@TESTS@", repr(tests_dir))
    second = run_subprocess(successor, str(tmp_path), paths.account_root.name, str(candidate), "1" if published else "0", timeout=60)
    assert second.returncode == 0, second.stderr
    assert ra._read_publication_record(paths) is None
    assert read_manifest(paths).epoch_number == old.epoch_number + int(published) + 1
    asyncio.run(client.close())


@async_test
async def test_gc_consumes_frozen_active_candidate_projection(tmp_path, monkeypatch):
    """T09: data-only GC gets frozen strings; manifest/candidate edges remain.

    CUT-2B retires only the job-basis pin and its JOB_EDGE neutralization.
    """
    import json
    from test_coherence_s02_bootstrap_gc import _gc_fixture, _publish_base, _make_snapshot_file
    from workflowy_mcp.coherence import refresh_authority as ra, snapshot_store as store

    client, paths, jobs_root, snapshot_dir = _gc_fixture(tmp_path, monkeypatch)
    base = _publish_base(paths, snapshot_dir, "20250101-000000-000001_pid1_nodes_export_snapshot.json")
    unrelated = _make_snapshot_file(snapshot_dir, "20250101-000000-000003_pid1_nodes_export_snapshot.json", age_seconds=90000)
    holding, release = asyncio.Event(), asyncio.Event()
    real_publish = ra.publish_epoch
    async def park(**kwargs):
        holding.set()
        await release.wait()
        return await real_publish(**kwargs)
    monkeypatch.setattr(ra, "publish_epoch", park)
    async def fetch():
        return {"nodes": [s02_node("base")]}
    owner = asyncio.create_task(ra.run_full_refresh(
        paths=paths, fetch=fetch, settle_seconds=0, snapshot_dir=snapshot_dir,
    ))
    try:
        await asyncio.wait_for(holding.wait(), 10)
        record = ra._read_publication_record(paths)
        candidate = Path(record.candidate_snapshot_path)
        os.utime(candidate, (1, 1))
        frozen = ra._active_candidate_snapshot_paths(jobs_root)
        assert isinstance(frozen, frozenset)
        assert frozen == frozenset({store.normalize_snapshot_path(candidate)})
        if os.environ.get("CUT2A_NEUTRALIZE_GC_PROJECTION"):
            frozen = frozenset()
        _make_snapshot_file(snapshot_dir, "99999999-999999-999999_pid1_nodes_export_snapshot.json")
        def forbidden_accessor(*args, **kwargs):
            raise AssertionError("collector accessed the publication registry")
        with monkeypatch.context() as patch:
            patch.setattr(ra, "_read_publication_record", forbidden_accessor)
            patch.setattr(ra, "_active_candidate_snapshot_paths", lambda root: frozen)
            protected, diagnostic = await asyncio.to_thread(
                store.collect_protected_snapshot_paths, jobs_root, active_candidate_paths=frozen,
            )
            result = client._gc_nodes_export_snapshots_diagnostic(max_keep=1, min_age_seconds=0)
            assert diagnostic["abort"] is False and result["aborted"] is False
            assert "claims_read" not in diagnostic
            assert candidate.exists() and candidate.name in result["protected"]
            assert Path(base).exists() and store.normalize_snapshot_path(base) in protected
            assert not unrelated.exists() and unrelated.name in result["deleted"]
        release.set()
        assert (await owner).status == "published"
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        await client.close()


@async_test
async def test_publication_record_has_only_memory_shape(tmp_path, monkeypatch):
    """T10: exact frozen shapes and real token/kind/cut/basis refusals, no retired paths."""
    from coherence_harness import ACCOUNT_A, make_coherence_root, seed_publication_record
    from workflowy_mcp.coherence import epoch_publisher as ep, refresh_authority as ra
    from workflowy_mcp.coherence.errors import CoherenceSchemaError, CoherenceStateMissingError, ManifestConflictError
    from workflowy_mcp.coherence.schema import RefreshCutToken
    from workflowy_mcp.coherence.snapshot_store import new_snapshot_candidate_path, write_snapshot_candidate

    if os.environ.get("CUT2A_NEUTRALIZE_RECORD_SERIALIZER"):
        monkeypatch.setattr(ra._PublicationRecord, "to_dict", lambda self: {}, raising=False)
    expected = {
        "account_scope_id", "claim_id", "refresh_kind", "status", "cut_token",
        "requested_hard_generation", "requested_external_generation", "target_epoch_number",
        "target_epoch_id", "started_at", "updated_at", "result", "candidate_snapshot_path", "basis_head_seq",
    }
    assert {field.name for field in fields(ra._PublicationRecord)} == expected
    assert ra._PublicationRecord.__dataclass_params__.frozen
    for deleted in ("owner_pid", "owner_pid_create_time", "owner_start_ticks", "owner_boot_id", "schema_version", "_KEYS", "to_dict", "from_dict", "ttl", "lock", "event"):
        assert not hasattr(ra._PublicationRecord, deleted), deleted
    assert {field.name for field in fields(RefreshCutToken)} == {
        "account_scope_id", "epoch_number", "epoch_id", "journal_head_seq",
        "hard_barrier_generation", "external_dirt_generation", "captured_at",
    }
    assert not hasattr(RefreshCutToken, "to_dict") and not hasattr(RefreshCutToken, "from_dict")
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    record = seed_publication_record(paths, status="publishing")
    manifest = read_manifest(paths)
    candidate = write_snapshot_candidate(
        new_snapshot_candidate_path(tmp_path / "snapshots"), {"nodes": [s02_node("base")]},
        account_scope_id=ACCOUNT_A, epoch_number=record.target_epoch_number, epoch_id=record.target_epoch_id,
        incorporated_through_seq=0, publication_kind="full_refresh",
    )
    kwargs = dict(
        paths=paths, claim_id=record.claim_id, basis_epoch_id=manifest.epoch_id,
        candidate_snapshot_path=candidate, incorporated_through_seq=0, publication_kind="full_refresh",
        refresh_cut=record.cut_token, clear_hard_through_generation=None, clear_external_through_generation=None,
    )
    if os.environ.get("CUT2A_NEUTRALIZE_PUBLICATION_TOKEN"):
        real_guard = ep._validate_claim_for_publication
        def no_foreign_guard(record, *, claim_id, paths):
            if record.claim_id == claim_id:
                return real_guard(record, claim_id=claim_id, paths=paths)
        monkeypatch.setattr(ep, "_validate_claim_for_publication", no_foreign_guard)
    for shape, changed, error, message in (
        (None, {}, CoherenceStateMissingError, "no active refresh-publication record"),
        (record, {"claim_id": "foreign-token"}, ManifestConflictError, "claim token mismatch"),
        (replace(record, status="completed"), {}, ManifestConflictError, "already terminal"),
        (replace(record, refresh_kind="compat_patch", cut_token=None), {}, CoherenceSchemaError, "requires a full-refresh claim kind"),
        (record, {"refresh_cut": replace(record.cut_token, hard_barrier_generation=1)}, ManifestConflictError, "does not equal the record cut"),
        (record, {"basis_epoch_id": "f" * 32}, ManifestConflictError, "basis epoch changed"),
    ):
        if shape is None:
            ra._PUBLICATION_RECORDS.pop(ra._publication_key(paths), None)
        else:
            ra._store_publication_record(paths, shape)
        with pytest.raises(error, match=message) as caught:
            await ep.publish_epoch(**{**kwargs, **changed})
        assert "refresh_publication" not in str(caught.value)
        if "basis_epoch_id" not in changed:
            assert caught.value.path is None
    with pytest.raises(CoherenceSchemaError, match="must carry basis_head_seq"):
        replace(record, refresh_kind="checkpoint", cut_token=None)
    with pytest.raises(CoherenceSchemaError, match="checkpoint-only"):
        replace(record, basis_head_seq=0)
    ra._store_publication_record(paths, record)
    with pytest.raises(ManifestConflictError, match="token changed underneath writer") as caught:
        ra._store_publication_record(paths, record, expected_claim_id="foreign-token")
    assert caught.value.path is None
    result = {"private": True}
    updated = await ra._write_claim_status(paths, record, "settling", result=result)
    result["later"] = "not shared"
    assert updated.result == {"private": True}


@async_test
async def test_carto_seams_recapture_current_view_without_repinning_execution(
    tmp_path, monkeypatch
):
    """T04: initial kwargs use T1; selection and each admission retry use T2."""
    from workflowy_mcp import server
    from workflowy_mcp.coherence.effects import SemanticEffect
    from workflowy_mcp.coherence.journal_store import append_journal_transaction
    from workflowy_mcp.coherence.schema import JournalProducer

    import os

    client, paths = _make_lineage_client(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "get_client", lambda: client)
    async with cache_operation_boundary(client, "execution-t1") as t1:
        append_journal_transaction(
            paths,
            operation_kind="test",
            effects=[SemanticEffect.node_create(s02_node("T2", name="second"))],
            producer=JournalProducer(
                pid=os.getpid(), pid_create_time=1.0,
                owner_kind="process", ownership_kind="none",
            ),
        )
        t2 = publish_fixture_head(client, paths)
        assert current_operation_cache() is t1
        assert t2.adopted_sequence == t1.view.adopted_sequence + 1
        assert "T2" not in t1.nodes_by_id
        captures = []
        real_capture = client._capture_view_and_dirty

        def counted_capture():
            pair = real_capture()
            captures.append(pair)
            return pair

        def forbidden_reload(*args, **kwargs):
            raise AssertionError("Cartographer seam reloaded a snapshot")

        monkeypatch.setattr(client, "_capture_view_and_dirty", counted_capture)
        monkeypatch.setattr(client, "_load_nodes_export_cache_from_disk", forbidden_reload)
        monkeypatch.setattr(
            "workflowy_mcp.coherence.snapshot_store.read_snapshot_envelope",
            forbidden_reload,
        )
        kwargs = server._carto_admission_coherence_kwargs(
            root_uuid="A", mode="file", source_regime="test"
        )
        assert kwargs["live_mutation_lease_view"] is t1.nodes_by_id
        assert kwargs["footprint_basis_sequence"] == t1.view.adopted_sequence
        assert kwargs["footprint_basis_epoch_id"] == t1.view.epoch_id
        selection = await server._carto_select_live_basis(client, "performed")
        assert selection["cache_by_id"] is t2.nodes_by_id
        assert selection["live_basis_lineage"] == {
            "account_scope_id": t2.account_scope_id,
            "epoch_number": t2.epoch_number,
            "epoch_id": t2.epoch_id,
            "adopted_sequence": t2.adopted_sequence,
        }
        manifest = read_manifest(paths, required=True)
        assert selection["snapshot_path"] == manifest.base_snapshot_path
        assert selection["snapshot_lineage"] == {
            "account_scope_id": manifest.account_scope_id,
            "epoch_number": manifest.epoch_number,
            "epoch_id": manifest.epoch_id,
            "incorporated_through_seq": manifest.base_incorporated_through_seq,
        }
        assert selection["freshness_disposition"] == "performed"
        for _ in range(2):
            retry = await kwargs["adopt_fn"]()
            assert retry == {
                "view": t2.nodes_by_id,
                "basis_sequence": t2.adopted_sequence,
                "basis_epoch_id": t2.epoch_id,
            }
            assert retry["view"] is t2.nodes_by_id
            assert current_operation_cache() is t1
        assert len(captures) == 3
        assert all(pair[0] is t2 for pair in captures)
    await client.close()
