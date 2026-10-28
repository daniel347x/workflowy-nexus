"""Full-size N01 cost regressions: real isolated S2 routes; S1 retired by CUT-2B.

Fixture/golden generation, journal setup and the one warm-up are excluded.
No network, live state home, production journal or publication offload.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
import statistics
import time
from types import SimpleNamespace

import pytest
import n01_reference as reference

from coherence_harness import make_coherence_root, s04_install_base_manifest, s04_producer, s04_checkpoint_config
from n01_copy_fixture import (build_fixture, fixture_identity, FIXTURE_DIGEST, FIXTURE_BYTES,
    N_TOTAL, GOLDEN_PAYLOADS, EMPTY_DIAGNOSTICS_DIGEST, digest, effect_tail, nid)
from test_coherence_s03_transition_cow import _make_client
from workflowy_mcp.coherence import (append_journal_transaction, read_manifest,
    run_checkpoint_rotation, write_snapshot_candidate, new_snapshot_candidate_path)
from workflowy_mcp.coherence import effects as engine
from workflowy_mcp.coherence import snapshot_store, refresh_authority, checkpoint
from workflowy_mcp.coherence.cache_view import CacheView, CacheViewPublisher, is_test_deep_freeze_active


@pytest.fixture(scope="module")
def full_fixture():
    payload, _ = build_fixture()
    assert len(payload["nodes"]) == N_TOTAL
    assert fixture_identity(payload) == (FIXTURE_DIGEST, FIXTURE_BYTES)
    assert is_test_deep_freeze_active()
    return payload


def _commit():
    # The runner's ext4 mirror intentionally has no .git directory. The
    # source-tree invocation supplies metadata without coupling tests to a
    # machine path or changing the fixture/engine under test.
    return os.environ.get("N01_TEST_COMMIT", "unavailable (N01_TEST_COMMIT not supplied)")


class _WindowLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        self.rows.append(record.getMessage())


async def measure(call):
    """Tick before the call and observe its synchronous gap after it yields."""
    loop = asyncio.get_running_loop()
    previous = loop.get_debug(), loop.slow_callback_duration
    logger = logging.getLogger("asyncio")
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    log = _WindowLog()
    logger.addHandler(log)
    loop.set_debug(True)
    loop.slow_callback_duration = .100
    delays, running = [], True
    started = asyncio.Event()
    async def heartbeat():
        last = time.perf_counter()
        while running:
            await asyncio.sleep(.010)
            now = time.perf_counter()
            delays.append(now - last)
            last = now
            started.set()
    task = asyncio.create_task(heartbeat(), name="N01 reader heartbeat")
    try:
        await started.wait()
        start = time.perf_counter()
        result = call()
        if hasattr(result, "__await__"):
            result = await result
        elapsed = time.perf_counter() - start
        await asyncio.sleep(.012)
        return result, elapsed, max(delays), log.rows
    finally:
        running = False
        await task
        loop.set_debug(previous[0])
        loop.slow_callback_duration = previous[1]
        logger.removeHandler(log)
        logger.setLevel(previous_level)


@dataclass(frozen=True)
class _SeedTemplate:
    account_scope_id: str
    base: Path
    journal: Path
    count: int
    payload_identity: tuple[str, int]


def _seed(client, root, snapshots, payload, count, *, template=None):
    """Seed private route inputs; optionally copy a pristine real-writer image.

    The caller still constructs a fresh, unpopulated client FIRST. Only setup
    is reused: each measured route reads/replays its own base and journal, and
    checkpoint catch-up still appends through the real writer inside measure().
    Return journal transactions in both branches, not unused per-append manifest
    objects. Different payload bytes always use the original fresh setup path.
    """
    paths = make_coherence_root(root, client._safe_account_scope_id())
    if template is not None and template.account_scope_id == paths.account_root.name \
            and 0 <= count <= template.count \
            and fixture_identity(payload) == template.payload_identity:
        from workflowy_mcp.coherence.paths import journal_record_filename
        base = new_snapshot_candidate_path(snapshots)
        # Independent files, not shared inodes: even an in-place-write regression
        # in one route cannot poison another repetition's setup image.
        shutil.copyfile(template.base, base)
        for sequence in range(1, count + 1):
            name = journal_record_filename(sequence)
            shutil.copyfile(template.journal / name, paths.journal_dir / name)
        s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32,
                                 journal_head=count)
        return paths, snapshot_store.read_journal_records(paths, 1, count)
    # A future larger workload or different account keeps the original real
    # setup path rather than being limited by this fixture's prepared image.
    base = write_snapshot_candidate(new_snapshot_candidate_path(snapshots), payload,
        account_scope_id=paths.account_root.name, epoch_number=0, epoch_id="b" * 32,
        incorporated_through_seq=0, publication_kind="full_refresh")
    s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32)
    records = []
    for effects in effect_tail(count):
        record, _ = append_journal_transaction(paths, operation_kind="n01-test",
            effects=effects, producer=s04_producer())
        records.append(record)
    return paths, records


# @beacon[
#   id=nexus-tests@seeded_journal_template,
#   slice_labels=mci-harness,
#   kind=ast,
#   comment=Builds a content-keyed full-size base and 500-record journal once through real writers; matching route repetitions copy private files before their measured windows.,
# ]
@pytest.fixture(scope="module")
def seeded_journal_template(tmp_path_factory, full_fixture):
    """Amortize quadratic journal construction, not any measured production work."""
    root = tmp_path_factory.mktemp("n01-pristine-seed")
    with pytest.MonkeyPatch.context() as patch:
        client, _, snapshots = _make_client(root, patch)
        try:
            paths, _ = _seed(client, root, snapshots, full_fixture, 500)
            manifest = read_manifest(paths, required=True)
            return _SeedTemplate(manifest.account_scope_id,
                                 Path(manifest.base_snapshot_path), paths.journal_dir, 500,
                                 fixture_identity(full_fixture))
        finally:
            asyncio.run(client.close())


def _instruments(monkeypatch):
    """Measure each ACTUAL S2 invocation, including checkpoint catch-up."""
    measurements = []
    components = []
    def phase(category, function):
        def wrapped(*args, **kwargs):
            start = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                components.append({"category": category, "elapsed_s": time.perf_counter()-start})
        return wrapped
    monkeypatch.setattr(snapshot_store, "read_snapshot_envelope", phase("snapshot_read", snapshot_store.read_snapshot_envelope))
    for module in (snapshot_store, refresh_authority, checkpoint):
        monkeypatch.setattr(module, "write_snapshot_candidate", phase("snapshot_serialization_write", module.write_snapshot_candidate))
    monkeypatch.setattr(refresh_authority, "canonicalize_nodes_export", phase("snapshot_ingestion_codec", refresh_authority.canonicalize_nodes_export))
    build = CacheView.build.__func__
    monkeypatch.setattr(CacheView, "build", classmethod(phase("build_freeze", build)))
    original = snapshot_store.materialize_payload_with_records
    def s2(payload, records, **kwargs):
        if kwargs.get("diagnostics") is None:
            kwargs["diagnostics"] = []
        diagnostics = kwargs["diagnostics"]
        copies, indexes, finishes = [], [], []
        old_copy = engine.copy.deepcopy
        old_init = engine._EffectState.__init__
        old_finish = engine._EffectState.finish
        copy_depth = 0
        account_size = len(payload["nodes"])
        def clone(value, *args, **kw):
            nonlocal copy_depth
            if copy_depth == 0:
                if isinstance(value, dict) and isinstance(value.get("nodes"), list):
                    copies.append(len(value["nodes"]))
                elif isinstance(value, list) and len(value) >= account_size and value and isinstance(value[0], dict):
                    copies.append(len(value))
            copy_depth += 1
            try:
                return old_copy(value, *args, **kw)
            finally:
                copy_depth -= 1
        def initialize(self, value):
            indexes.append(len(value["nodes"]))
            return old_init(self, value)
        def finish(self):
            finishes.append(1)
            return old_finish(self)
        engine.copy.deepcopy = clone
        engine._EffectState.__init__ = initialize
        engine._EffectState.finish = finish
        try:
            start = time.perf_counter()
            result = original(payload, records, **kwargs)
            elapsed = time.perf_counter() - start
        finally:
            engine.copy.deepcopy = old_copy
            engine._EffectState.__init__ = old_init
            engine._EffectState.finish = old_finish
        components.append({"category": "replay", "elapsed_s": elapsed})
        start = time.perf_counter()
        row = {"records": len(records), "elapsed_s": elapsed,
               "full_copy": len(copies), "index_build": len(indexes), "finish": len(finishes),
               "output_digest": digest(result), "diagnostic_digest": digest(diagnostics)}
        measurements.append(row)
        components.append({"category": "test_harness_digest", "elapsed_s": time.perf_counter()-start})
        assert len(copies) == len(indexes) == len(finishes) == 1
        assert elapsed <= 15.0
        return result
    for module in (snapshot_store, refresh_authority, checkpoint):
        monkeypatch.setattr(module, "materialize_payload_with_records", s2)
    return measurements, components


@pytest.mark.timing
@pytest.mark.parametrize("count", [200, 500])
def test_s2_full_size_segment_has_one_copy_and_matches_golden(full_fixture, count, monkeypatch, capsys):
    rows, components = _instruments(monkeypatch)
    records = [SimpleNamespace(sequence=i+1, account_scope_id="a"*32, effects=effects)
               for i, effects in enumerate(effect_tail(count))]
    samples = []
    for repetition in range(4):
        result, elapsed, gap, callbacks = asyncio.run(measure(lambda:
            snapshot_store.materialize_payload_with_records(full_fixture, records,
                start_sequence=1, account_scope_id="a"*32)))
        assert elapsed <= 15.0 and gap <= 15.0
        assert digest(result) == GOLDEN_PAYLOADS[("warm_start", count)]
        if repetition:
            samples.append(elapsed)
            with capsys.disabled():
                print("N01_METRIC " + json.dumps({"commit": _commit(), "route": "S2_segment", "N": count,
                    "fixture_digest": FIXTURE_DIGEST, "fixture_count": N_TOTAL, "fixture_bytes": FIXTURE_BYTES,
                    "freeze": True, "repetition": repetition, "elapsed_s": elapsed,
                    "heartbeat_max_delay_s": gap, "S2": rows[-1]}), flush=True)
                for text in callbacks:
                    print("N01_CALLBACK S2_segment; attribution=replay plus test-harness digest; "+text, flush=True)


@pytest.mark.timing
@pytest.mark.parametrize("count", [200, 500])
@pytest.mark.parametrize("route", ["warm_start", "full_refresh", "compat", "checkpoint"])
def test_real_s2_routes_with_reader_heartbeat(full_fixture, seeded_journal_template, tmp_path, monkeypatch, capsys, route, count):
    samples, gaps = [], []
    all_s2, components = _instruments(monkeypatch)
    for repetition in range(4):
        # All setup (including real record appends and serialization) is
        # outside the measured operation. Repetition0 is the warm-up.
        root = tmp_path / str(repetition)
        client, _, snapshots = _make_client(root, monkeypatch)
        paths, records = _seed(client, root, snapshots, full_fixture, count,
                               template=seeded_journal_template)
        before = read_manifest(paths, required=True)
        first = len(all_s2)
        first_component = len(components)
        if route == "warm_start":
            call = client._load_nodes_export_cache_from_disk
        elif route == "full_refresh":
            async def fetch(*args, **kwargs):
                return full_fixture
            monkeypatch.setattr("workflowy_mcp.client.api_client_etch.fetch_nodes_export_uncached", fetch)
            call = lambda: client.refresh_nodes_export_cache(settle_seconds=0)
        elif route == "compat":
            call = lambda: client.patch_nodes_export_snapshot_on_disk([{"id": nid(5000), "note": "N01 final narrow patch"}])
        else:
            real_write = checkpoint.write_snapshot_candidate
            caught_up = []
            def append_catchup(*args, **kwargs):
                result = real_write(*args, **kwargs)
                if not caught_up:
                    caught_up.append(True)
                    for effects in effect_tail(count + 5)[count:]:
                        append_journal_transaction(paths, operation_kind="n01-catchup", effects=effects, producer=s04_producer())
                return result
            monkeypatch.setattr(checkpoint, "write_snapshot_candidate", append_catchup)
            call = lambda: run_checkpoint_rotation(paths, snapshot_dir=snapshots,
                                                    config=s04_checkpoint_config(record_threshold=1))
        result, elapsed, gap, callbacks = asyncio.run(measure(call))
        if route == "checkpoint":
            monkeypatch.setattr(checkpoint, "write_snapshot_candidate", real_write)
            assert result.status == "published", result
            assert result.incorporated_through_seq == count + 5
            final = snapshot_store.load_manifest_base_envelope(paths, read_manifest(paths, required=True)).nodes_export
        else:
            assert result is True if route == "warm_start" else result["success"]
            final = client._cache_view_publisher.capture().nodes_export
            assert client._cache_view_publisher.capture().adopted_sequence == count
        after = read_manifest(paths, required=True)
        assert not after.dirty_status.active
        assert after.journal_head_seq == count + (5 if route == "checkpoint" else 0)
        assert digest(final) == GOLDEN_PAYLOADS[(route, count)]
        assert all(row["diagnostic_digest"] == EMPTY_DIAGNOSTICS_DIGEST for row in all_s2[first:])
        if repetition:
            samples.append(elapsed)
            gaps.append(gap)
            assert gap <= 15.0
            assert elapsed <= (20.0 if route == "warm_start" else 30.0)
            row = {"commit": _commit(), "fixture_digest": FIXTURE_DIGEST, "fixture_count": N_TOTAL,
                   "fixture_bytes": FIXTURE_BYTES, "route": route, "N": count, "repetition": repetition,
                   "freeze": is_test_deep_freeze_active(), "elapsed_s": elapsed,
                   "heartbeat_max_delay_s": gap, "cas_attempts": 1,
                   "catchup_count": 1 if route == "checkpoint" else 0,
                   "S2": all_s2[first:], "output_digest": digest(final),
                   "components": components[first_component:],
                   "diagnostic_digest": EMPTY_DIAGNOSTICS_DIGEST}
            with capsys.disabled():
                print("N01_METRIC " + json.dumps(row), flush=True)
                for text in callbacks:
                    print("N01_CALLBACK route_task; attribution=replay/build-freeze/snapshot-read-write/bounded-existing-claim-IO (constituent S2 spans above); " + text, flush=True)
        # Do not accumulate full private accounts across repetitions.
        del client, final, records
    with capsys.disabled():
        print("N01_METRIC " + json.dumps({"route": route, "N": count, "samples_s": samples,
            "median_s": statistics.median(samples), "maximum_s": max(samples),
            "heartbeat_max_delay_s": max(gaps)}), flush=True)


@pytest.mark.timing
def test_111_legacy_move_patch_storm(full_fixture, tmp_path, monkeypatch, capsys):
    samples = []
    expected = reference.apply_semantic_effects(full_fixture,
        [reference.SemanticEffect.node_parentage(nid(i), nid(41)) for i in reversed(range(42, 153))],
        mode=reference.ApplicationMode.REPLAY_AUTHORITATIVE)
    expected_digest = digest(expected)
    del expected
    for repetition in range(4):
        client, _, _ = _make_client(tmp_path / str(repetition), monkeypatch)
        client._publish_lineage_view(full_fixture, epoch_number=1, epoch_id="e"*32,
            adopted_sequence=0, snapshot_timestamp=None)
        durations = []
        async def storm():
            for index in reversed(range(42, 153)):
                start = time.perf_counter()
                client._apply_move_to_nodes_export_cache(nid(index), nid(41))
                durations.append(time.perf_counter() - start)
                await asyncio.sleep(0)
        _, elapsed, gap, callbacks = asyncio.run(measure(storm))
        assert len(durations) == 111
        assert max(durations) <= 2.0
        assert gap <= 15.0
        assert digest(client._cache_view_publisher.capture().nodes_export) == expected_digest
        if repetition:
            samples.append(elapsed)
            with capsys.disabled():
                print("N01_METRIC " + json.dumps({"commit": _commit(), "route": "111_move_patch_storm",
                    "fixture_digest": FIXTURE_DIGEST, "fixture_count": N_TOTAL, "fixture_bytes": FIXTURE_BYTES,
                    "freeze": True, "elapsed_s": elapsed, "per_patch_s": durations,
                    "maximum_patch_s": max(durations), "heartbeat_max_delay_s": gap}), flush=True)
                for text in callbacks:
                    print("N01_CALLBACK legacy_patch; attribution=build-freeze/fragment-preparation/test-harness; " + text, flush=True)


@pytest.mark.timing
@pytest.mark.parametrize("scenario", ["rebase", "overlay", "exhaustion"])
def test_full_size_patch_retry_overlay_and_exhaustion(full_fixture, tmp_path, monkeypatch, capsys, scenario):
    from workflowy_mcp.coherence.cache_view import prepare_cow_payload
    from workflowy_mcp.coherence.errors import CacheViewError
    from workflowy_mcp.coherence import operation_cache as boundary
    from test_coherence_s03_transition_cow import _make_lineage_client
    expected_effects = [reference.SemanticEffect.node_parentage(nid(154), nid(155))]
    if scenario == "rebase":
        expected_effects.insert(0, reference.SemanticEffect.node_fields(nid(0), ["name"], {"name": "rival"}))
    expected = reference.apply_semantic_effects(full_fixture, expected_effects,
                                               mode=reference.ApplicationMode.REPLAY_AUTHORITATIVE)
    expected_digest = digest(expected)
    del expected
    samples = []
    for repetition in range(4):
        root = tmp_path / str(repetition)
        if scenario == "overlay":
            client, _ = _make_lineage_client(root, monkeypatch, nodes=full_fixture["nodes"])
        else:
            client, _, _ = _make_client(root, monkeypatch)
            client._publish_lineage_view(full_fixture, epoch_number=1, epoch_id="e"*32,
                adopted_sequence=0, snapshot_timestamp=None)
        publisher = client._cache_view_publisher
        before = publisher.capture()
        original = publisher.publish_with_commit
        attempts = []
        def publish(candidate, **kwargs):
            attempts.append(1)
            if scenario == "exhaustion":
                raise CacheViewError("injected lost CAS")
            if scenario == "rebase" and len(attempts) == 1:
                payload = dict(before.nodes_export)
                payload["nodes"] = [dict(n, name="rival") if n["id"] == nid(0) else n for n in before.nodes_export["nodes"]]
                rival = CacheView.build(epoch_number=before.epoch_number, epoch_id=before.epoch_id,
                    adopted_sequence=before.adopted_sequence, account_scope_id=before.account_scope_id,
                    nodes_export=prepare_cow_payload(before, payload), ownership="transferred")
                original(rival, expected_current=before)
            return original(candidate, **kwargs)
        def patch():
            try:
                client._apply_move_to_nodes_export_cache(nid(154), nid(155))
                return "published"
            except CacheViewError:
                return "raced_out"
        async def exercise():
            if scenario == "overlay":
                async with boundary.cache_operation_boundary(client, "n01-full-overlay"):
                    pinned = boundary._OPERATION_CACHE_SNAPSHOT.get()
                    client._publish_lineage_view(full_fixture, epoch_number=1, epoch_id="f"*32,
                        adopted_sequence=0, snapshot_timestamp=None)
                    monkeypatch.setattr(publisher, "publish_with_commit", publish)
                    measured = await measure(patch)
                    own = boundary._OPERATION_CACHE_SNAPSHOT.get()
                    assert own.view.epoch_number == pinned.view.epoch_number == 0
                    assert own.view.nodes_by_id[nid(20000)] is pinned.view.nodes_by_id[nid(20000)]
                    assert digest(own.view.nodes_export) == expected_digest
                    return measured
            monkeypatch.setattr(publisher, "publish_with_commit", publish)
            return await measure(patch)
        result, elapsed, gap, callbacks = asyncio.run(exercise())
        monkeypatch.setattr(publisher, "publish_with_commit", original)
        assert len(attempts) == {"rebase": 2, "overlay": 1, "exhaustion": 4}[scenario]
        assert elapsed <= 2.0
        if scenario == "exhaustion":
            assert result == "raced_out" and publisher.capture() is before
        else:
            assert result == "published"
            assert digest(publisher.capture().nodes_export) == expected_digest
        if repetition:
            samples.append(elapsed)
            with capsys.disabled():
                print("N01_METRIC " + json.dumps({"commit": _commit(), "route": "single_patch_"+scenario,
                    "fixture_digest": FIXTURE_DIGEST, "fixture_count": N_TOTAL, "fixture_bytes": FIXTURE_BYTES,
                    "freeze": True, "repetition": repetition, "elapsed_s": elapsed,
                    "heartbeat_max_delay_s": gap, "cas_attempts": len(attempts)}), flush=True)
                for text in callbacks:
                    print("N01_CALLBACK patch_"+scenario+"; attribution=bounded COW preparation/build-freeze/task-overlay plus test race injection; "+text, flush=True)
