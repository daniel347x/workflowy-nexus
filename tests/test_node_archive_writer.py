"""Tests for the durable writer split (Unit 1, Gate 1a): atomic_write_text
owns temp/write/fsync/cleanup; its replace retry is shared with the status
writer through one governance helper. atomic_write_json remains its JSON front."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from workflowy_mcp import carto_job_governance, config as carto_config
from workflowy_mcp.carto_job_governance import (
    DurableFileOps,
    atomic_write_json,
    atomic_write_text,
)

REPO_SRC = Path(carto_job_governance.__file__).resolve().parents[1]


def _recording_ops():
    """A DurableFileOps whose every operation records its call."""
    calls: list[tuple[str, tuple]] = []

    def tmp_name(target):
        calls.append(("tmp_name", (target,)))
        return target.with_name(f".{target.name}.test.tmp")

    ops = DurableFileOps(
        write_fsync=lambda path, text: calls.append(("write_fsync", (path, text))),
        replace=lambda tmp, target: calls.append(("replace", (tmp, target))),
        dir_sync=lambda directory: calls.append(("dir_sync", (directory,))),
        mkdir=lambda path: calls.append(("mkdir", (path,))),
        unlink=lambda path: calls.append(("unlink", (path,))),
        sleep=lambda seconds: calls.append(("sleep", (seconds,))),
        monotonic=lambda: 0.0,
        tmp_name=tmp_name,
    )
    return ops, calls


# ---------------------------------------------------------------------------
# T14a · atomic_write_json golden (compatibility lock)
# ---------------------------------------------------------------------------


def test_t14a_atomic_write_json_bytes_and_ops_unchanged(tmp_path):
    payload = {"k": "välice 🌲", "n": [1, 2], "nested": {"b": 1, "a": 2}}
    target = tmp_path / "artifact.json"
    atomic_write_json(target, payload)
    expected = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    assert target.read_bytes() == expected  # no trailing newline

    ops, calls = _recording_ops()
    atomic_write_json(target, payload, fs_ops=ops)
    ops2, calls2 = _recording_ops()
    atomic_write_json(target, payload, fs_ops=ops2, sync_parent_dir=True)
    assert [name for name, _args in calls] == [
        "mkdir", "tmp_name", "write_fsync", "replace", "unlink",
    ]
    assert [name for name, _args in calls2] == [
        "mkdir", "tmp_name", "write_fsync", "replace", "dir_sync", "unlink",
    ]
    written = calls[2][1][1]
    assert written == json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# T14b · atomic_write_text
# ---------------------------------------------------------------------------


def test_t14b_atomic_write_text_byte_exact_and_cleans_up(tmp_path):
    target = tmp_path / "doc.txt"
    text = "line1\nline2 🌲\n"
    returned = atomic_write_text(target, text)
    assert returned == target
    assert target.read_bytes() == text.encode("utf-8")
    assert list(tmp_path.iterdir()) == [target]

    def _boom_write(path, text_):
        raise OSError("disk full")

    with pytest.raises(OSError):
        atomic_write_text(
            target, "new", fs_ops=DurableFileOps(write_fsync=_boom_write)
        )
    assert target.read_bytes() == text.encode("utf-8")
    assert list(tmp_path.iterdir()) == [target]  # temp removed

    def _boom_replace(tmp, target_):
        raise PermissionError("locked")

    # A clock that is past the writer's 2 s deadline on its second read, so
    # the bounded retry fires once and re-raises instead of looping.
    clock = {"t": 0.0}

    def _clock():
        clock["t"] += 3.0
        return clock["t"]

    with pytest.raises(PermissionError):
        atomic_write_text(
            target,
            "new",
            fs_ops=DurableFileOps(replace=_boom_replace, monotonic=_clock),
        )
    assert target.read_bytes() == text.encode("utf-8")
    assert list(tmp_path.iterdir()) == [target]  # temp removed


def test_t14b_atomic_write_text_refuses_non_str_before_creating_anything(tmp_path):
    missing_dir = tmp_path / "not-created"
    with pytest.raises(TypeError):
        atomic_write_text(missing_dir / "x.txt", b"bytes")
    with pytest.raises(TypeError):
        atomic_write_text(missing_dir / "x.txt", 42)
    assert not missing_dir.exists()


# ---------------------------------------------------------------------------
# T15 · One loop (structural)
# ---------------------------------------------------------------------------


def test_t15_atomic_write_json_delegates_and_one_loop_exists():
    source = inspect.getsource(atomic_write_json)
    for forbidden in ("replace_deadline", "PermissionError", "write_fsync", "tmp_name"):
        assert forbidden not in source, (
            f"atomic_write_json still mentions {forbidden}: the loop must "
            "live only in the shared governance helper"
        )
    text_source = inspect.getsource(atomic_write_text)
    for marker in ("write_fsync", "tmp_name"):
        assert marker in text_source
    retry_source = inspect.getsource(carto_job_governance._replace_with_retry)
    for marker in ("replace_deadline", "PermissionError"):
        assert marker not in text_source
        assert marker in retry_source
    for forbidden in ("write_fsync", "tmp_name"):
        assert forbidden not in retry_source

    # Retarget the closed conformance check to JR1's shared retry extraction:
    # one actual loop, and one delegation edge from each writer/front.
    trees = {name: ast.parse(text) for name, text in {
        "json": source, "text": text_source, "retry": retry_source,
        "merge": inspect.getsource(carto_config.merge_write_carto_job_status),
    }.items()}
    assert {name: sum(isinstance(node, ast.While) for node in ast.walk(tree))
            for name, tree in trees.items()} == {"json": 0, "text": 0, "retry": 1, "merge": 0}
    for owner, callee in (("json", "atomic_write_text"), ("text", "_replace_with_retry"),
                           ("merge", "_replace_with_retry")):
        assert sum(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == callee for node in ast.walk(trees[owner])) == 1

    hits = []
    for path in sorted(REPO_SRC.rglob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if "os.replace(" in line:
                hits.append((path.relative_to(REPO_SRC).as_posix(), number))
    # CUT-2B removed the quarantine/sentinel sites; JR1 now delegates config's
    # replacement to governance's existing primitive rather than adding a site.
    expected_owners = [
        "workflowy_mcp/carto_job_governance.py",
        "workflowy_mcp/coherence/journal_store.py",
        "workflowy_mcp/server.py",
    ]
    assert [owner for owner, _ in hits] == expected_owners, (
        f"expected exactly three owned os.replace( sites, found {hits}"
    )
