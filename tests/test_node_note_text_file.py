"""File-backed sources for node.append_note / node.prepend_note (text_file).

Only file ingestion is new: real dispatch/admission/cache boundary/resolution
and the unchanged concat core are exercised with an isolated runtime and
mocked HTTP client methods. No production vault, cache or governance state is
mutated.

Contract: exactly one of inline ``text`` / non-null ``text_file``; the file is
read through the ONE host-aware strict-UTF-8/universal-newline reader AFTER
dispatch admission and BEFORE any live note read; the ack carries the
top-level ``path_record`` (empty file = the existing empty-text error); inline
behavior is unchanged.
"""
from __future__ import annotations

import asyncio
import html
import io
from unittest.mock import AsyncMock, Mock

import pytest

from coherence_harness import make_boundary_capable, make_coherence_root, s04_raise_barrier
from n05_region_harness import isolate_claim_scope
from workflowy_mcp import server
from workflowy_mcp import tool_path_resolution as tpr
from workflowy_mcp.mcp_api_registry import dispatch_operation, validate_params
from workflowy_mcp.models.node import WorkFlowyNode

ROOT = "11111111-1111-4111-8111-111111111111"
NAME = "[ap:FILE01] Existing #keep"


@pytest.fixture
def host(monkeypatch):
    """Simulate the daemon's host family via the ONE detection seam."""

    def _set(name: str) -> str:
        assert name in ("windows", "wsl", "linux")
        monkeypatch.setattr(tpr, "_detect_host_raw", lambda: name)
        return name

    return _set


def setup(monkeypatch, tmp_path):
    paths = make_coherence_root(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    client = make_boundary_capable(
        object.__new__(server.WorkFlowyClient),
        nodes=[
            {"id": ROOT, "name": NAME, "note": "old note", "parent_id": None, "priority": 10},
        ],
    )
    isolate_claim_scope(client, tmp_path)
    current = WorkFlowyNode(id=ROOT, name=NAME, note=html.escape("old note"))
    client.get_node = AsyncMock(return_value=current)

    async def update(node_id, request, *, basis=None):
        return WorkFlowyNode(
            id=node_id,
            name=request.name if request.name is not None else NAME,
            note=html.escape(request.note) if request.note is not None else current.note,
        )

    client.update_node = AsyncMock(side_effect=update)
    client.create_node = AsyncMock(side_effect=AssertionError("no create"))
    client.move_node = AsyncMock(side_effect=AssertionError("no move"))
    client.delete_node = AsyncMock(side_effect=AssertionError("no delete"))
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    return client, paths


def dispatch(verb: str, params: dict):
    return asyncio.run(
        dispatch_operation(
            server.build_node_registry(), verb, {"node_handle": ROOT, **params},
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
        )
    )


def _install_open_spy(monkeypatch, texts: dict):
    """Reads answered from ``texts`` (path -> content); records (path, mode)."""

    calls: list[tuple] = []

    def spy_open(file, mode="r", *args, **kwargs):
        calls.append((str(file), mode, kwargs))
        return io.StringIO(texts[str(file)])

    monkeypatch.setattr(tpr, "open", spy_open, raising=False)
    return calls


# --- The file source ---------------------------------------------------------


def _real_record(f) -> dict:
    """The path_record a real-file source must produce on THIS runner's host."""
    return {
        "requested": str(f), "resolved": str(f),
        "translated": False, "host": tpr.detect_host(),
    }


def test_append_text_file_reads_through_resolver_record_on_ack(monkeypatch, tmp_path):
    client, _ = setup(monkeypatch, tmp_path)
    f = tmp_path / "part4.md"
    f.write_text("part from file", encoding="utf-8")
    result = dispatch("append_note", {"text_file": str(f)})
    assert result["success"] is True
    assert result["path_record"] == _real_record(f)
    client.update_node.assert_awaited_once()
    assert client.update_node.await_args.args[1].note == "old note\npart from file"
    detail = result["detail"]
    assert isinstance(detail, dict)
    assert detail["node_id"] == ROOT
    assert detail["position"] == "append"
    assert detail["note_chars"] == len("old note\npart from file")
    assert detail["added_chars"] == len("\npart from file")
    # No body echo: the acknowledgement never carries the document back.
    assert "note" not in detail and "note_text" not in detail and "text" not in detail
    assert "note" not in result and "text" not in result


def test_prepend_text_file_lands_before_existing_note(monkeypatch, tmp_path):
    client, _ = setup(monkeypatch, tmp_path)
    f = tmp_path / "preamble.md"
    f.write_text("preamble", encoding="utf-8")
    result = dispatch("prepend_note", {"text_file": str(f)})
    assert result["success"] is True
    assert client.update_node.await_args.args[1].note == "preamble\nold note"
    assert result["detail"]["position"] == "prepend"
    assert result["detail"]["added_chars"] == len("preamble\n")
    assert result["path_record"] == _real_record(f)


def test_text_file_honours_the_explicit_separator(monkeypatch, tmp_path):
    client, _ = setup(monkeypatch, tmp_path)
    f = tmp_path / "part.md"
    f.write_text("part", encoding="utf-8")
    result = dispatch("append_note", {"text_file": str(f), "separator": "\n\n"})
    assert result["success"] is True
    assert client.update_node.await_args.args[1].note == "old note\n\npart"


def test_translating_host_reads_resolved_path_and_records_translation(monkeypatch, host, tmp_path):
    """A translating host is exercised with an open spy: the reader opens the
    RESOLVED location and the ack record shows the translation."""
    host("windows")
    client, _ = setup(monkeypatch, tmp_path)
    calls = _install_open_spy(monkeypatch, {"E:\\notes\\part4.md": "translated body"})
    monkeypatch.setattr(tpr.os.path, "isfile", lambda p: True)
    result = dispatch("append_note", {"text_file": "/mnt/e/notes/part4.md"})
    assert result["success"] is True
    assert result["path_record"] == {
        "requested": "/mnt/e/notes/part4.md",
        "resolved": "E:\\notes\\part4.md",
        "translated": True, "host": "windows",
    }
    assert [c[0] for c in calls] == ["E:\\notes\\part4.md"]
    assert client.update_node.await_args.args[1].note == "old note\ntranslated body"


@pytest.mark.parametrize("text", [
    "\ufeff# BOM retained\r\n",
    '{"name":"literal JSON","children":[]}\n',
    "AT&amp;T\rline2",
])
def test_file_bytes_are_semantic_text_verbatim(monkeypatch, tmp_path, text):
    """Same reader contract as note_file: strict UTF-8, CRLF/CR -> LF, no
    entity/Markdown decoding, no stripping -- then byte-exact concatenation."""
    client, _ = setup(monkeypatch, tmp_path)
    path = tmp_path / "part.md"
    path.write_bytes(text.encode("utf-8"))
    expected = text.replace("\r\n", "\n").replace("\r", "\n")
    result = dispatch("append_note", {"text_file": str(path)})
    assert result["success"] is True
    assert client.update_node.await_args.args[1].note == "old note\n" + expected
    assert result["detail"]["added_chars"] == len("\n" + expected)


def test_empty_file_is_the_existing_empty_text_error(monkeypatch, tmp_path):
    client, _ = setup(monkeypatch, tmp_path)
    f = tmp_path / "empty.md"
    f.write_text("", encoding="utf-8")
    result = dispatch("append_note", {"text_file": str(f)})
    assert result["success"] is False
    assert result["error"]["code"] == "operation_failed"
    assert "non-empty" in result["error"]["message"]
    client.get_node.assert_not_awaited()
    client.update_node.assert_not_awaited()


# --- Exactly one source: refusals before any file or live I/O ----------------


@pytest.mark.parametrize("params", [
    {"text": "inline", "text_file": "C:/notes/missing.md"},
    {"text": "", "text_file": "C:/notes/missing.md"},
    {},
])
def test_source_combination_refused_before_file_read_or_edit(monkeypatch, host, tmp_path, params):
    host("windows")
    client, _ = setup(monkeypatch, tmp_path)
    reader = Mock(side_effect=AssertionError("must reject before file I/O"))
    monkeypatch.setattr(server, "read_tool_text_file", reader)
    result = dispatch("append_note", params)
    assert result["success"] is False
    assert result["error"]["code"] == "invalid_parameter_combination"
    assert "exactly one of" in result["error"]["message"]
    assert result["operation_doc"] == server.build_node_registry().describe_full("append_note")
    reader.assert_not_called()
    client.get_node.assert_not_awaited()
    client.update_node.assert_not_awaited()


def test_neither_source_refused_for_both_verbs(monkeypatch, tmp_path):
    client, _ = setup(monkeypatch, tmp_path)
    reader = Mock(side_effect=AssertionError("must reject before file I/O"))
    monkeypatch.setattr(server, "read_tool_text_file", reader)
    for verb in ("append_note", "prepend_note"):
        result = dispatch(verb, {})
        assert result["error"]["code"] == "invalid_parameter_combination"
        assert f"node.{verb}" in result["error"]["message"]
        assert "text_file" in result["error"]["message"]
    reader.assert_not_called()
    client.update_node.assert_not_awaited()


def test_null_text_file_preserves_inline_behavior(monkeypatch, host, tmp_path):
    host("linux")
    client, _ = setup(monkeypatch, tmp_path)
    reader = Mock(side_effect=AssertionError("null file must be ignored"))
    monkeypatch.setattr(server, "read_tool_text_file", reader)
    result = dispatch("append_note", {"text": "inline tail", "text_file": None})
    assert result["success"] is True
    assert "path_record" not in result
    client.update_node.assert_awaited_once()
    assert client.update_node.await_args.args[1].note == "old note\ninline tail"
    reader.assert_not_called()


# --- File failures: no note read, no write ------------------------------------


@pytest.mark.parametrize("kind,needle", [
    ("empty", "empty path"), ("relative", "ABSOLUTE"),
    ("missing", "not an existing file"), ("directory", "not an existing file"),
    ("binary", "not valid UTF-8"), ("unreadable", "could not be read"),
])
def test_text_file_failures_leave_note_untouched(monkeypatch, tmp_path, kind, needle):
    client, _ = setup(monkeypatch, tmp_path)
    path = tmp_path / "part.md"
    value = str(path)
    if kind == "empty":
        value = " "
    elif kind == "relative":
        value = "notes/part.md"
    elif kind == "directory":
        value = str(tmp_path)
    elif kind == "binary":
        path.write_bytes(b"\xff\xfe\x00")
    elif kind == "unreadable":
        path.write_text("not read", encoding="utf-8")
        monkeypatch.setattr(
            tpr, "open", Mock(side_effect=PermissionError("denied")), raising=False
        )
    else:  # missing: never created -- the isfile check must refuse it.
        assert not path.exists()
    result = dispatch("append_note", {"text_file": value})
    assert result["success"] is False
    assert "node.append_note text_file" in result["error"]["message"]
    assert needle in result["error"]["message"]
    client.get_node.assert_not_awaited()
    client.update_node.assert_not_awaited()
    client.create_node.assert_not_awaited()


def test_hard_barrier_refuses_before_text_file_ingestion(monkeypatch, tmp_path):
    client, paths = setup(monkeypatch, tmp_path)
    s04_raise_barrier(paths)
    reader = Mock(side_effect=AssertionError("hard barrier must precede file read"))
    monkeypatch.setattr(server, "read_tool_text_file", reader)
    result = dispatch("append_note", {"text_file": str(tmp_path / "not-read.md")})
    assert result["error"]["code"] == "coherence_barrier_active"
    reader.assert_not_called()
    client.update_node.assert_not_awaited()


# --- Registry and description disclosure --------------------------------------


def test_registry_discloses_text_file_source():
    registry = server.build_node_registry()
    for verb in ("append_note", "prepend_note"):
        spec = registry.resolve(verb)
        assert spec.cache_backed and spec.side_effects == ("workflowy_mutation",)
        full = registry.describe_full(verb)
        props = full["params_schema"]["properties"]
        assert props["text"]["type"] == ["string", "null"]
        assert props["text_file"]["type"] == ["string", "null"]
        assert "empty file" in props["text_file"]["description"]
        assert "exactly one" in props["text_file"]["description"]
        assert full["params_schema"]["required"] == ["node_handle"]
        for example in full["examples"]:
            validate_params(spec, example["call"]["params"])
