"""The daemon's memory of the archive folder (Archive Reads U3, §U3.5 test 3).

Reply parsing, remembering, persistence across a simulated restart, the
absent key (an older extension), the deferral inside an augmented
operation (E36) and the flush at the next ordinary read.
"""

from __future__ import annotations

import json

import pytest

from workflowy_mcp import archive_store
from workflowy_mcp.archive_store import (
    _archive_folder_memory_path,
    _reset_archive_folder_memory_for_tests,
    read_remembered_archive_folder,
    remember_archive_folder_from_reply,
    remembered_archive_folder_learned_at,
)


@pytest.fixture()
def memory_env(tmp_path, monkeypatch):
    """The memory file lives in a scratch runtime dir; the process memory
    starts cold."""
    from workflowy_mcp import config as nexus_config

    monkeypatch.setattr(
        nexus_config, "get_runtime_base_dir", lambda: tmp_path
    )
    _reset_archive_folder_memory_for_tests()
    yield tmp_path
    _reset_archive_folder_memory_for_tests()


def _file_value(tmp_path):
    path = tmp_path / "archive_folder.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("archive_folder")


def test_absent_key_changes_nothing(memory_env):
    remember_archive_folder_from_reply({"note_session_tags": ["wfy-ns"]})
    assert read_remembered_archive_folder() == ""
    assert _file_value(memory_env) is None


def test_reply_parsing_and_remembering(memory_env):
    remember_archive_folder_from_reply({"archive_folder": "  C:\\arc  "})
    assert read_remembered_archive_folder() == "C:\\arc"
    assert _file_value(memory_env) == "C:\\arc"
    learned = remembered_archive_folder_learned_at()
    assert isinstance(learned, str) and learned.endswith("Z")


def test_changed_value_updates_memory_and_file(memory_env):
    remember_archive_folder_from_reply({"archive_folder": "C:\\one"})
    remember_archive_folder_from_reply({"archive_folder": "C:\\two"})
    assert read_remembered_archive_folder() == "C:\\two"
    assert _file_value(memory_env) == "C:\\two"


def test_unchanged_value_is_a_no_op(memory_env):
    remember_archive_folder_from_reply({"archive_folder": "C:\\one"})
    path = memory_env / "archive_folder.json"
    first = path.read_text(encoding="utf-8")
    remember_archive_folder_from_reply({"archive_folder": "C:\\one"})
    assert path.read_text(encoding="utf-8") == first


def test_persistence_across_a_simulated_restart(memory_env):
    remember_archive_folder_from_reply({"archive_folder": "C:\\keep"})
    # A restart: the process memory resets; the file carries the value.
    _reset_archive_folder_memory_for_tests()
    assert read_remembered_archive_folder() == "C:\\keep"
    assert remembered_archive_folder_learned_at() is not None


def test_malformed_file_reads_as_no_folder(memory_env):
    (_archive_folder_memory_path()).write_text("{broken", encoding="utf-8")
    _reset_archive_folder_memory_for_tests()
    assert read_remembered_archive_folder() == ""


def test_non_string_value_reads_as_no_folder(memory_env):
    (_archive_folder_memory_path()).write_text(
        json.dumps({"archive_folder": 42}), encoding="utf-8"
    )
    _reset_archive_folder_memory_for_tests()
    assert read_remembered_archive_folder() == ""


def test_deferral_inside_an_augmented_operation(memory_env, monkeypatch):
    """E36/Axiom 5: inside an augmented operation the in-memory value
    updates at once but the FILE WRITE waits; the next ordinary read
    flushes it — even when the reported value is unchanged."""

    from workflowy_mcp import archive_context

    class _Ctx:
        overlay = True

    # The consumer imports current_archive_context from the archive_context
    # module at call time, so the patch target is that module.
    monkeypatch.setattr(
        archive_context,
        "current_archive_context",
        lambda: _Ctx(),
    )
    remember_archive_folder_from_reply({"archive_folder": "C:\\deferred"})
    assert read_remembered_archive_folder() == "C:\\deferred"
    assert _file_value(memory_env) is None  # deferred, not written

    # The next ordinary (non-augmented) settings read: same value, and the
    # deferred write flushes.
    monkeypatch.setattr(
        archive_context,
        "current_archive_context",
        lambda: None,
    )
    remember_archive_folder_from_reply({"archive_folder": "C:\\deferred"})
    assert _file_value(memory_env) == "C:\\deferred"
