"""S01 §13.6 — fault-injection harness tests.

The durable-writer faults are TEST-ONLY named seams (:class:`InjectedFault`)
with a pre-raise relative-path→bytes image; the only subprocess left is the
ordinary isolated import child.
"""

from __future__ import annotations

import os
import threading

import pytest
from coherence_harness import (
    ACCOUNT_A,
    FAULT_POINTS,
    InjectedFault,
    SeamCapture,
    TransportOutcome,
    TransportScript,
    exit_ops,
    launch_subprocess,
    make_coherence_root,
    reset_fixture_memory,
    restore_successor,
    run_reader_loop,
    write_malformed,
    write_unknown_version,
)

from workflowy_mcp.coherence import (
    JournalProducer,
    SemanticEffect,
    append_journal_transaction,
    inspect_journal,
    new_bootstrap_manifest,
    read_manifest,
)
from workflowy_mcp.coherence.durable_io import atomic_write_json_durable
from workflowy_mcp.coherence.errors import (
    CoherenceSchemaError,
    CoherenceStateMissingError,
    UnsupportedCoherenceSchemaError,
)
from workflowy_mcp.coherence.paths import coherence_paths
from workflowy_mcp.coherence.schema import initialize_manifest_if_absent

# Expected post-kill state per named fault point:
#   point -> (head_after_kill, orphan_seqs_above_head)
_KILL_EXPECTATIONS = {
    "before_record_temp_write": (0, []),
    "after_record_file_fsync": (0, []),
    "before_record_replace": (0, []),
    "after_record_replace_before_manifest": (0, [1]),
    "before_manifest_temp_write": (0, [1]),
    "after_manifest_file_fsync": (0, [1]),
    "before_manifest_replace": (0, [1]),
    "after_manifest_replace": (1, []),
}

#: ``point -> (record temp file durable, manifest temp file durable)`` at the
#: PRE-RAISE instant.  The atomic writer's ``finally`` unlinks its own temp on
#: unwind, so these are exactly the artifacts that distinguish the captured
#: death image from the post-unwind files.
_KILL_TEMP_EXPECTATIONS = {
    "before_record_temp_write": (False, False),
    "after_record_file_fsync": (True, False),
    "before_record_replace": (True, False),
    "after_record_replace_before_manifest": (False, False),
    "before_manifest_temp_write": (False, False),
    "after_manifest_file_fsync": (False, True),
    "before_manifest_replace": (False, True),
    "after_manifest_replace": (False, False),
}


@pytest.mark.parametrize("point", FAULT_POINTS)
def test_kill_at_every_named_fault_point(tmp_path, point):
    """FAULT at every named seam; the PRE-RAISE image is the death state.

    CUT-2B: the seam raises :class:`InjectedFault` after capturing the
    isolated root's image.  The invocation unwinds; the successor fixture is
    reconstructed from that image with fresh in-memory authority.  The
    post-unwind files are never treated as hard death.
    """
    root = tmp_path / "death"
    root.mkdir()
    paths = make_coherence_root(root, ACCOUNT_A)
    capture = SeamCapture(root)
    with pytest.raises(InjectedFault) as raised:
        append_journal_transaction(
            paths,
            operation_kind="test",
            effects=[SemanticEffect.tombstone(["x"])],
            producer=JournalProducer(
                pid=os.getpid(), pid_create_time=1.0,
                owner_kind="process", ownership_kind="none",
            ),
            fs_ops=exit_ops(point, capture=capture),
        )
    # Explicit: the exact named seam was reached before the raise.
    assert raised.value.point == point
    assert capture.images, f"named seam {point} never tripped"
    assert capture.image.point == point

    successor_root = root
    image = capture.restart_image()
    record_temps = [
        relative
        for relative in image.files
        if "/journal/." in relative and relative.endswith(".tmp")
    ]
    manifest_temps = [
        relative
        for relative in image.files
        if ".manifest.json." in relative and relative.endswith(".tmp")
    ]
    expected_record_temp, expected_manifest_temp = _KILL_TEMP_EXPECTATIONS[point]
    assert bool(record_temps) is expected_record_temp, (point, record_temps)
    assert bool(manifest_temps) is expected_manifest_temp, (point, manifest_temps)
    reset_fixture_memory(root, accounts=(ACCOUNT_A,))
    paths = restore_successor(image, successor_root, ACCOUNT_A)
    reset_fixture_memory(successor_root, accounts=(ACCOUNT_A,))
    try:
        expected_head, expected_orphans = _KILL_EXPECTATIONS[point]
        manifest = read_manifest(paths)
        assert manifest.journal_head_seq == expected_head, point
        report = inspect_journal(paths, manifest)
        assert report["above_head"] == expected_orphans, point

        if expected_orphans:
            # deterministic recovery: the next locked append adopts the orphan
            # exactly once, then continues the contiguous stream.
            record, new_manifest = append_journal_transaction(
                paths,
                operation_kind="test",
                effects=[SemanticEffect.tombstone(["y"])],
                producer=JournalProducer(
                    pid=os.getpid(), pid_create_time=1.0,
                    owner_kind="process", ownership_kind="none",
                ),
            )
            assert new_manifest.journal_head_seq == 2
            assert record.sequence == 2 and record.previous_sequence == 1
    finally:
        reset_fixture_memory(root, accounts=(ACCOUNT_A,))
        reset_fixture_memory(successor_root, accounts=(ACCOUNT_A,))


def test_reader_loop_observes_only_complete_old_or_new_json(tmp_path):
    make_coherence_root(tmp_path, ACCOUNT_A)
    target = tmp_path / "artifact.json"
    gen_a = {"generation": "A", "payload": list(range(50))}
    gen_b = {"generation": "B", "payload": list(range(50, 100))}
    atomic_write_json_durable(target, gen_a)

    stop = threading.Event()
    result_holder: dict = {}

    def reader() -> None:
        result_holder["result"] = run_reader_loop(target, stop, acceptable=[gen_a, gen_b])

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    for _ in range(100):
        atomic_write_json_durable(target, gen_b)
        atomic_write_json_durable(target, gen_a)
    stop.set()
    thread.join(timeout=15)
    result = result_holder["result"]
    assert result.torn == 0
    assert not result.unexpected
    assert result.reads > 0


def test_corrupt_missing_unknown_version_artifact_injectors(tmp_path):
    paths = coherence_paths(ACCOUNT_A, jobs_root=tmp_path / "jobs")
    with pytest.raises(CoherenceStateMissingError):
        read_manifest(paths)

    manifest = new_bootstrap_manifest(ACCOUNT_A)
    initialize_manifest_if_absent(paths, manifest)

    write_malformed(paths.manifest_path)
    with pytest.raises(CoherenceSchemaError):
        read_manifest(paths)

    write_unknown_version(paths.manifest_path, manifest.to_dict())
    with pytest.raises(UnsupportedCoherenceSchemaError):
        read_manifest(paths)


def test_transport_outcome_seam():
    script = TransportScript(
        [
            TransportOutcome("success", {"id": "n1"}),
            TransportOutcome("timeout_committed"),
            TransportOutcome("ambiguous", detail="unverifiable"),
        ]
    )
    assert script({"op": 1}).kind == "success"
    assert script({"op": 2}).kind == "timeout_committed"
    assert script(None).kind == "ambiguous"
    assert len(script.calls) == 3
    with pytest.raises(AssertionError):
        script(None)
    with pytest.raises(ValueError):
        TransportOutcome("bogus_kind")


# ---------------------------------------------------------------------------
# Audit-gate addition (A8)
# ---------------------------------------------------------------------------


def test_import_coherence_does_not_import_server():
    """Importing the dependency-light coherence package must not pull the
    FastMCP server, credentials, or broker machinery."""
    code = (
        "import workflowy_mcp.coherence, sys; "
        "print('server-imported:', 'workflowy_mcp.server' in sys.modules)"
    )
    result = launch_subprocess(code)
    out, err = result.communicate(timeout=30)
    assert result.returncode == 0, err
    assert "server-imported: False" in out
    assert "server.py" not in err
