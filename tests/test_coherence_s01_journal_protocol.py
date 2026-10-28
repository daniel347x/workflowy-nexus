"""S01 §13.4 — journal physical protocol and durability tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from coherence_harness import (
    ACCOUNT_A,
    ACCOUNT_B,
    InjectedFault,
    fault_ops,
    make_coherence_root,
)

from workflowy_mcp.coherence import (
    JournalProducer,
    SemanticEffect,
    append_journal_transaction,
    inspect_journal,
    journal_record_filename,
    read_manifest,
)
from workflowy_mcp.coherence.errors import (
    CoherenceAccountMismatchError,
    JournalIntegrityError,
    JournalPublicationError,
)
from workflowy_mcp.coherence.journal_store import _read_record_file
from workflowy_mcp.coherence.paths import coherence_paths


def _producer() -> JournalProducer:
    return JournalProducer(
        pid=os.getpid(), pid_create_time=1.0, owner_kind="process", ownership_kind="none"
    )


def _effects() -> list[SemanticEffect]:
    return [SemanticEffect.tombstone(["ghost"])]


def _append(paths, **kwargs):
    return append_journal_transaction(
        paths, operation_kind="test", effects=_effects(), producer=_producer(), **kwargs
    )


# ---------------------------------------------------------------------------
# Happy path and head/record ordering
# ---------------------------------------------------------------------------


def test_record_durable_before_head_advances(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    record, manifest = _append(paths)
    assert record.sequence == 1 and manifest.journal_head_seq == 1
    record_path = paths.journal_dir / journal_record_filename(1)
    assert record_path.exists()
    on_disk = _read_record_file(record_path, paths)
    assert on_disk == record
    assert read_manifest(paths).journal_head_seq == 1


def test_record_filename_20_digit_decimal(tmp_path):
    assert journal_record_filename(1) == "00000000000000000001.json"
    assert journal_record_filename(123) == "00000000000000000123.json"


def test_two_appends_chain_and_inspect(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _append(paths)
    record2, manifest2 = _append(paths)
    assert record2.previous_sequence == 1 and manifest2.journal_head_seq == 2
    report = inspect_journal(paths, read_manifest(paths))
    assert report["active_range_records"] == [1, 2]
    assert report["contiguous_active_range"] and not report["above_head"]


# ---------------------------------------------------------------------------
# Fault points around the record write
# ---------------------------------------------------------------------------


def test_failure_before_record_write_leaves_head_unchanged(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(InjectedFault):
        _append(paths, fs_ops=fault_ops("before_record_temp_write"))
    assert read_manifest(paths).journal_head_seq == 0
    assert inspect_journal(paths, read_manifest(paths))["above_head"] == []


def test_failure_before_record_replace_leaves_head_unchanged(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(InjectedFault):
        _append(paths, fs_ops=fault_ops("before_record_replace"))
    manifest = read_manifest(paths)
    assert manifest.journal_head_seq == 0
    # the un-replaced temp was cleaned; no committed/orphan record
    assert inspect_journal(paths, manifest)["above_head"] == []


def test_death_after_record_replace_before_manifest_leaves_recoverable_orphan(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(InjectedFault):
        _append(paths, fs_ops=fault_ops("after_record_replace_before_manifest"))
    assert read_manifest(paths).journal_head_seq == 0
    report = inspect_journal(paths, read_manifest(paths))
    assert report["above_head"] == [1]
    # A later locked open adopts the valid orphan exactly once.
    record, manifest = _append(paths)
    assert manifest.journal_head_seq == 2
    assert record.sequence == 2 and record.previous_sequence == 1
    assert inspect_journal(paths, read_manifest(paths))["active_range_records"] == [1, 2]


def test_valid_orphan_adopted_exactly_once_on_reopen(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    record1, _ = _append(paths)
    # Simulate a crash-after-record/pre-head for seq 2 by writing the record
    # file manually (durably) without advancing the head.
    from workflowy_mcp.coherence.durable_io import atomic_write_json_durable
    from workflowy_mcp.coherence.schema import JournalTransaction

    orphan = JournalTransaction(
        schema_version=1,
        account_scope_id=ACCOUNT_A,
        sequence=2,
        previous_sequence=1,
        transaction_id="orphan" + "0" * 26,
        published_epoch_id=record1.published_epoch_id,
        operation_kind="test",
        committed_at="2026-11-01T00:00:00+00:00",
        producer=_producer(),
        verification=(),
        effects=tuple(_effects()),
    )
    atomic_write_json_durable(paths.journal_dir / journal_record_filename(2), orphan.to_dict())
    record, manifest = _append(paths)  # triggers recovery, then appends seq 3
    assert manifest.journal_head_seq == 3
    assert record.sequence == 3
    report = inspect_journal(paths, read_manifest(paths))
    assert report["active_range_records"] == [1, 2, 3] and not report["above_head"]


def test_invalid_above_head_evidence_quarantined_fail_closed(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _append(paths)
    bad = paths.journal_dir / journal_record_filename(2)
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(JournalIntegrityError):
        _append(paths)
    report = inspect_journal(paths, read_manifest(paths))
    assert len(report["quarantined"]) == 1
    assert report["quarantined"][0].endswith(bad.name)
    assert read_manifest(paths).journal_head_seq == 1


def test_gap_above_head_quarantined(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _append(paths)
    gap = paths.journal_dir / journal_record_filename(5)
    gap.write_text("{}", encoding="utf-8")
    with pytest.raises(JournalIntegrityError):
        _append(paths)
    assert len(inspect_journal(paths, read_manifest(paths))["quarantined"]) == 1


def test_wrong_account_orphan_quarantined(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    record1, _ = _append(paths)
    from workflowy_mcp.coherence.durable_io import atomic_write_json_durable
    from workflowy_mcp.coherence.schema import JournalTransaction

    foreign = JournalTransaction(
        schema_version=1,
        account_scope_id=ACCOUNT_B,
        sequence=2,
        previous_sequence=1,
        transaction_id="foreign" + "0" * 25,
        published_epoch_id=record1.published_epoch_id,
        operation_kind="test",
        committed_at="2026-11-01T00:00:00+00:00",
        producer=_producer(),
        verification=(),
        effects=tuple(_effects()),
    )
    atomic_write_json_durable(paths.journal_dir / journal_record_filename(2), foreign.to_dict())
    with pytest.raises(CoherenceAccountMismatchError):
        _append(paths)
    # account-mismatch evidence is quarantined as invalid lineage
    assert len(inspect_journal(paths, read_manifest(paths))["quarantined"]) == 1


def test_manifest_pointing_at_missing_committed_record_fails_closed(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _append(paths)
    _append(paths)
    os.remove(paths.journal_dir / journal_record_filename(1))
    with pytest.raises(JournalIntegrityError):
        _append(paths)
    # not auto-healed: head unchanged, nothing quarantined
    assert read_manifest(paths).journal_head_seq == 2


def test_malformed_committed_record_fails_closed(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _append(paths)
    (paths.journal_dir / journal_record_filename(1)).write_text("{broken", encoding="utf-8")
    with pytest.raises(JournalIntegrityError):
        _append(paths)


def test_manifest_fault_before_replace_keeps_orphan_evidence(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises((InjectedFault, JournalPublicationError)):
        _append(paths, fs_ops=fault_ops("before_manifest_replace"))
    # record durable above head; head unchanged; adoptable later
    assert read_manifest(paths).journal_head_seq == 0
    report = inspect_journal(paths, read_manifest(paths))
    assert report["above_head"] == [1]


# ---------------------------------------------------------------------------
# Same-loop task interleaving — one contiguous stream per account
# ---------------------------------------------------------------------------


def test_concurrent_processes_append_one_contiguous_stream(tmp_path, monkeypatch):
    """Historical process carrier: three loop tasks retain all 12 real appends."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    from workflowy_mcp.coherence import journal_store as journal
    if os.environ.get("CUT2B_NEUTRALIZE_APPEND_HEAD"):
        import inspect
        source = inspect.getsource(journal.append_journal_transaction)
        old = "manifest_with_updates(manifest, journal_head_seq=next_sequence)"
        assert old in source
        namespace = dict(vars(journal))
        exec(compile(source.replace(old, "manifest_with_updates(manifest, journal_head_seq=manifest.journal_head_seq)", 1), "<cut2b-append-head>", "exec"), namespace)
        monkeypatch.setattr(journal, "append_journal_transaction", namespace["append_journal_transaction"])

    async def scenario():
        ready = asyncio.Event()
        calls = []
        async def writer(index):
            await ready.wait()
            for turn in range(4):
                journal.append_journal_transaction(
                    paths, operation_kind="test", effects=[SemanticEffect.tombstone(["x"])],
                    producer=_producer(), cache_by_id={"x": {"id": "x"}},
                )
                calls.append((index, turn))
                await asyncio.sleep(0)  # Between whole synchronous transactions only.
        workers = [asyncio.create_task(writer(index)) for index in range(3)]
        ready.set()
        await asyncio.gather(*workers)
        assert calls == [(index, turn) for turn in range(4) for index in range(3)]
    asyncio.run(scenario())
    report = inspect_journal(paths, read_manifest(paths))
    assert read_manifest(paths).journal_head_seq == 12
    assert report["active_range_records"] == list(range(1, 13))
    assert report["contiguous_active_range"]


def test_two_account_scopes_have_independent_streams(tmp_path):
    paths_a = make_coherence_root(tmp_path, ACCOUNT_A)
    jobs_root = paths_a.account_root.parent.parent
    paths_b = coherence_paths(ACCOUNT_B, jobs_root=jobs_root)
    from workflowy_mcp.coherence import initialize_manifest_if_absent, new_bootstrap_manifest

    initialize_manifest_if_absent(paths_b, new_bootstrap_manifest(ACCOUNT_B))
    _append(paths_a)
    _append(paths_a)
    _append(paths_b)
    assert read_manifest(paths_a).journal_head_seq == 2
    assert read_manifest(paths_b).journal_head_seq == 1
    assert paths_a.journal_dir != paths_b.journal_dir


# ---------------------------------------------------------------------------
# Audit-gate additions (A7, lineage)
# ---------------------------------------------------------------------------


def _plant_orphan(paths, sequence, epoch_id, mutate=None, account=ACCOUNT_A):
    from workflowy_mcp.coherence.durable_io import atomic_write_json_durable
    from workflowy_mcp.coherence.schema import JournalTransaction

    orphan = JournalTransaction(
        schema_version=1,
        account_scope_id=account,
        sequence=sequence,
        previous_sequence=sequence - 1,
        transaction_id=f"orphan{sequence:026d}",
        published_epoch_id=epoch_id,
        operation_kind="test",
        committed_at="2026-11-01T00:00:00+00:00",
        producer=_producer(),
        verification=(),
        effects=tuple(_effects()),
    )
    doc = orphan.to_dict()
    if mutate is not None:
        doc = mutate(doc)
    atomic_write_json_durable(paths.journal_dir / journal_record_filename(sequence), doc)


def test_unknown_transaction_version_orphan_quarantined(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    record1, _ = _append(paths)
    _plant_orphan(paths, 2, record1.published_epoch_id,
                  mutate=lambda d: {**d, "schema_version": 99})
    with pytest.raises(JournalIntegrityError):
        _append(paths)
    report = inspect_journal(paths, read_manifest(paths))
    assert len(report["quarantined"]) == 1
    assert read_manifest(paths).journal_head_seq == 1


def test_unknown_effect_version_orphan_quarantined(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    record1, _ = _append(paths)

    def mutate(doc):
        doc["effects"][0]["effect_version"] = 99
        return doc

    _plant_orphan(paths, 2, record1.published_epoch_id, mutate=mutate)
    with pytest.raises(JournalIntegrityError):
        _append(paths)
    assert len(inspect_journal(paths, read_manifest(paths))["quarantined"]) == 1


def test_invalid_effect_payload_orphan_quarantined(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    record1, _ = _append(paths)

    def mutate(doc):
        doc["effects"] = [{
            "effect_type": "node_fields",
            "effect_version": 1,
            "payload": {"node_id": "A", "field_mask": ["priority"], "values": {"priority": 1}},
        }]
        return doc

    _plant_orphan(paths, 2, record1.published_epoch_id, mutate=mutate)
    with pytest.raises(JournalIntegrityError):
        _append(paths)
    assert len(inspect_journal(paths, read_manifest(paths))["quarantined"]) == 1


def test_post_epoch_cut_lineage_from_fresh_process(tmp_path):
    """After an S02-style epoch cut, a FRESH process validates the post-cut
    tail via the manifest's DURABLE previous_epoch_id — no caller whitelist."""
    from workflowy_mcp.coherence import manifest_with_updates, write_manifest

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    record1, manifest1 = _append(paths)
    old_epoch = manifest1.epoch_id
    # Simulate an epoch cut: new epoch, base incorporated through seq 1,
    # journal floor 2, durable predecessor recorded.
    cut = manifest_with_updates(
        manifest1,
        epoch_number=1,
        epoch_id="f" * 32,
        base_snapshot_path=str(tmp_path / "base.json"),
        base_incorporated_through_seq=1,
        journal_floor_seq=2,
        journal_head_seq=1,
        coherence_mode="transition",
        previous_epoch_id=old_epoch,
    )
    write_manifest(paths, cut, expected_epoch_id=manifest1.epoch_id,
                   expected_head_seq=manifest1.journal_head_seq)
    # Post-cut orphan at seq 2 naming the PREVIOUS epoch: lineage-valid.
    _plant_orphan(paths, 2, old_epoch)
    # A second fresh process appends: adopts the orphan, appends seq 3.
    code = """
import os, sys
from workflowy_mcp.coherence import (
    JournalProducer, SemanticEffect, append_journal_transaction, coherence_paths,
)
paths = coherence_paths(sys.argv[1], jobs_root=sys.argv[2])
producer = JournalProducer(pid=os.getpid(), pid_create_time=1.0,
                           owner_kind='process', ownership_kind='none')
record, manifest = append_journal_transaction(
    paths, operation_kind='test',
    effects=[SemanticEffect.tombstone(['x'])], producer=producer,
    cache_by_id={'x': {'id': 'x'}},
)
print(manifest.journal_head_seq)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, ACCOUNT_A, str(paths.account_root.parent.parent)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "3"
    # A random-epoch post-cut orphan is lineage-INVALID: quarantined.
    _plant_orphan(paths, 4, "0" * 32)
    with pytest.raises(JournalIntegrityError):
        _append(paths)
    assert len(inspect_journal(paths, read_manifest(paths))["quarantined"]) == 1


# ---------------------------------------------------------------------------
# Audit residuals R2/R3 — lineage authority + malformed orphan shapes
# ---------------------------------------------------------------------------


def test_bootstrap_foreign_epoch_orphan_never_adopted(tmp_path):
    """R2: the manifest is the ONLY lineage authority — a foreign-epoch
    orphan in bootstrap is quarantined, and no caller input can bless it."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    record1, _ = _append(paths)
    _plant_orphan(paths, 2, "0" * 32)  # foreign epoch
    with pytest.raises(JournalIntegrityError):
        _append(paths)
    report = inspect_journal(paths, read_manifest(paths))
    assert len(report["quarantined"]) == 1
    assert read_manifest(paths).journal_head_seq == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: {**d, "verification": [7]},
        lambda d: {**d, "verification": [None]},
        lambda d: {**d, "producer": 7},
        lambda d: {**d, "effects": 7},
        lambda d: {**d, "effects": [7]},
        lambda d: {**d, "effects": [{"effect_type": "tombstone", "effect_version": 1, "payload": 7}]},
    ],
    ids=["verification-scalar", "verification-null", "producer-scalar",
         "effects-scalar", "effects-entry-scalar", "effect-payload-scalar"],
)
def test_malformed_nested_orphan_shapes_quarantined(tmp_path, mutate):
    """R3: every malformed above-head record shape is quarantined with
    JournalIntegrityError — never a raw escape."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    record1, _ = _append(paths)
    _plant_orphan(paths, 2, record1.published_epoch_id, mutate=mutate)
    with pytest.raises(JournalIntegrityError):
        _append(paths)
    report = inspect_journal(paths, read_manifest(paths))
    assert len(report["quarantined"]) == 1
    assert read_manifest(paths).journal_head_seq == 1


def test_durable_writer_single_source_conformance():
    """F-2 negative postcondition (permanent fence): exactly ONE
    temp+fsync+atomic-replace retry loop exists in the repository — the
    audited carto_job_governance.atomic_write_json; coherence.durable_io is
    a facade, not a parallel implementation."""
    src_root = Path(__file__).resolve().parents[1] / "src"
    sites = []
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "replace_deadline" in text:
            sites.append(py.name)
    assert sites == ["carto_job_governance.py"]
