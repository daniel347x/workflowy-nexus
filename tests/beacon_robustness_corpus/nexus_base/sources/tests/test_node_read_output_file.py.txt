"""node.read output_file: exact note-only UTF-8, existing boundaries intact.

Run real read/decoder/registry/finalizer code with a fake live GET and the
standard isolated coherence harness. Never reads or mutates the real vault.
"""

from __future__ import annotations

import asyncio
import html
from unittest.mock import AsyncMock

import pytest
from coherence_harness import make_boundary_capable, s04_boundary_status

from workflowy_mcp import server
from workflowy_mcp.coherence import COHERENCE_READ_ADVISORY_LINE
from workflowy_mcp.coherence.admission import mutation_admission_class
from workflowy_mcp.mcp_api_registry import dispatch_operation
from workflowy_mcp.models.node import WorkFlowyNode

UUID = "7b97b6a9-467c-43eb-9bab-90f736cedfe6"
NAME = "[ap:FILE01] Specification 📊 #tag"


def _client(monkeypatch, note="body", status=None):
    node = WorkFlowyNode(id=UUID, name=NAME, note=note)
    # Resolver snapshot deliberately has no note body: the export must use
    # the ONE live GET, not fetch the document from the resolver cache.
    client = make_boundary_capable(
        object.__new__(server.WorkFlowyClient),
        nodes=[{"id": UUID, "name": NAME, "note": "", "parent_id": None}],
        boundary_status=status,
    )
    client.get_node = AsyncMock(return_value=node)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    return client, node


def _dispatch(params, verb="read"):
    return asyncio.run(
        dispatch_operation(
            server.build_node_registry(), verb, params,
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
        )
    )


@pytest.mark.parametrize("handle", [UUID, UUID[:8], "[ap:FILE01]"])
def test_note_only_exact_utf8_through_real_resolver_and_dispatch(monkeypatch, tmp_path, handle):
    note = (
        "\n####### Specification 📊 🧑‍💻 中文 café\r\n"
        "\t  indentation\u00a0  \n"
        "<!-- literal comment -->\n<a href=\"x\">authored HTML</a>\n"
        "literal &lt; stays entity text\n"
        "```md\n## fenced heading\n```\n"
        "Path: body text is not metadata to strip\n\nend  "
    )
    client, node = _client(monkeypatch, html.escape(note, quote=True))
    path = tmp_path / "specification 📊.md"
    path.write_bytes(b"OLD CONTENT")
    result = _dispatch({"node_handle": handle, "output_file": str(path)})
    # Host-aware file paths ([ap:1T7JZ0]): the ack key-set is EXTENDED by the
    # ONE path_record; every established key is unchanged (9.5(e) update).
    record = result.pop("path_record")
    assert result == {
        "success": True, "node_id": UUID, "output_file": str(path),
        "bytes_written": len(note.encode("utf-8")), "encoding": "utf-8",
    }
    # P3 argument echo: output_file above carries the REQUESTED string
    # byte-identically; the record's requested/resolved agree (an
    # already-correct path passes through untranslated on every host).
    assert record["requested"] == str(path)
    assert record["resolved"] == str(path)
    assert record["translated"] is False
    assert record["host"] in ("windows", "wsl", "linux")
    assert path.read_bytes() == note.encode("utf-8")
    assert path.read_bytes().decode("utf-8", errors="strict") == note
    client.get_node.assert_awaited_once_with(UUID)
    assert node.note == html.escape(note, quote=True)  # source never rewritten
    assert list(tmp_path.iterdir()) == [path]  # no temp residue/sidecars


def test_same_canonical_autolink_and_one_layer_decode_as_ordinary_read(monkeypatch, tmp_path):
    wire = (
        '&amp;lt;literal&amp;gt;\n'
        '<a href="https://example.com?a=1&amp;b=2">https://example.com?a=1&amp;b=2</a>\n'
        '<a href="https://example.com">named link</a>\n<b>formatting</b>'
    )
    expected = (
        '&lt;literal&gt;\nhttps://example.com?a=1&b=2\n'
        '<a href="https://example.com">named link</a>\n<b>formatting</b>'
    )
    _client(monkeypatch, wire)
    ordinary = _dispatch({"node_handle": UUID})
    path = tmp_path / "note.md"
    written = _dispatch({"node_handle": UUID, "output_file": str(path)})
    assert ordinary == f"[name: not editable] {NAME}\n\n{expected}"
    assert written["success"] is True
    assert path.read_bytes() == expected.encode("utf-8")


@pytest.mark.parametrize(
    "note", [None, "", "\n\r\n\t  ", "📊\n" * 50000],
    ids=["null", "empty", "whitespace", "large"],
)
def test_empty_whitespace_and_large_notes_are_not_trimmed_or_truncated(monkeypatch, tmp_path, note):
    _client(monkeypatch, note)
    path = tmp_path / "note.md"
    result = _dispatch({"node_handle": UUID, "output_file": str(path)})
    expected = (note or "").encode("utf-8")
    assert result["success"] is True
    assert path.read_bytes() == expected
    assert result["bytes_written"] == len(expected)


@pytest.mark.parametrize("extra", [{}, {"output_file": None}])
def test_ordinary_and_structured_reads_unchanged(monkeypatch, extra):
    _client(monkeypatch, "&lt;literal&gt;\n")
    result = _dispatch({"node_handle": UUID, **extra})
    assert result == f"[name: not editable] {NAME}\n\n<literal>\n"
    _, node = _client(monkeypatch, "&lt;literal&gt;\n")
    assert _dispatch({"node_handle": UUID, "as_raw_node": False, **extra}) is node


@pytest.mark.parametrize("hard_dirty", [False, True])
def test_advisory_on_acknowledgement_only_no_file_contamination(monkeypatch, tmp_path, hard_dirty):
    status = s04_boundary_status(
        hard_dirty=hard_dirty, hard_generation=7,
        hard_reason_code="verified_effect_unpublishable", hard_transaction_id="tx-note-file",
    )
    _client(monkeypatch, "# Specification\n📊\n", status=status)
    path = tmp_path / "spec.md"
    result = _dispatch({"node_handle": "[ap:FILE01]", "output_file": str(path)})
    assert result["success"] is True
    assert path.read_bytes() == "# Specification\n📊\n".encode("utf-8")
    assert ("coherence_advisory" in result) is hard_dirty
    if hard_dirty:
        assert result["coherence_advisory"]["dirty_generation"] == 7
        assert result["coherence_advisory"]["transaction_id"] == "tx-note-file"
    ordinary = _dispatch({"node_handle": UUID})
    assert ordinary.startswith(COHERENCE_READ_ADVISORY_LINE) is hard_dirty


def test_note_text_resembling_advisory_is_not_stripped(monkeypatch, tmp_path):
    note = COHERENCE_READ_ADVISORY_LINE + "\n\n[name: not editable] literal note text\n"
    _client(monkeypatch, note)
    path = tmp_path / "note.md"
    assert _dispatch({"node_handle": UUID, "output_file": str(path)})["success"] is True
    assert path.read_bytes() == note.encode("utf-8")


@pytest.mark.parametrize("bad_path", ["", "  ", 42, False, []])
def test_invalid_paths_rejected_before_live_read(monkeypatch, bad_path):
    client, _ = _client(monkeypatch)
    result = _dispatch({"node_handle": UUID, "output_file": bad_path})
    assert result["success"] is False
    assert result["error"]["code"] == "invalid_parameter_type"
    client.get_node.assert_not_awaited()


def test_structured_mode_with_file_is_explicitly_rejected(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch)
    result = _dispatch({"node_handle": UUID, "output_file": str(tmp_path / "x.md"), "as_raw_node": False})
    assert result["success"] is False
    assert result["error"]["code"] == "invalid_parameter_combination"
    client.get_node.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["encoding", "get", "flush", "replace"])
def test_failures_preserve_existing_destination_and_clean_temp(monkeypatch, tmp_path, failure):
    client, _ = _client(monkeypatch, "unpaired \ud83d" if failure == "encoding" else "new content")
    path = tmp_path / "note.md"
    path.write_bytes(b"PREVIOUS FILE")

    def fail(*_args, **_kwargs):
        raise OSError("injected artifact failure")

    if failure == "get":
        client.get_node.side_effect = OSError("live GET failed")
    elif failure == "flush":
        monkeypatch.setattr(server.os, "fsync", fail)
    elif failure == "replace":
        monkeypatch.setattr(server.os, "replace", fail)
    result = _dispatch({"node_handle": UUID, "output_file": str(path)})
    assert result["success"] is False
    assert result["error"]["code"] == "operation_failed"
    assert path.read_bytes() == b"PREVIOUS FILE"
    assert list(tmp_path.iterdir()) == [path]


def test_missing_parent_is_loud_and_not_created(monkeypatch, tmp_path):
    _client(monkeypatch)
    path = tmp_path / "missing" / "note.md"
    result = _dispatch({"node_handle": UUID, "output_file": str(path)})
    assert result["success"] is False
    assert result["error"]["code"] == "operation_failed"
    assert not path.parent.exists()


def test_relative_path_and_python_delegate(monkeypatch, tmp_path):
    """A relative output_file is REFUSED on every host (spec [ap:1T7JZ0]
    §6.3 C7 — the legacy silent cwd-relative write is retired), and the
    Python delegate shares exactly the same read/file implementation for an
    already-correct absolute path."""
    from workflowy_mcp.tool_path_resolution import ToolPathError

    _client(monkeypatch, "# note\n")
    monkeypatch.chdir(tmp_path)

    async def run_relative():
        async with server._mcp_operation_boundary("node.read"):
            return await server.read_node(UUID, output_file="note.md")

    # The teaching refusal propagates (the kernel maps it to its
    # operation_failed envelope on the family route — §6.7); zero bytes.
    with pytest.raises(ToolPathError, match="ABSOLUTE"):
        asyncio.run(run_relative())
    assert not (tmp_path / "note.md").exists()

    async def run_absolute():
        async with server._mcp_operation_boundary("node.read"):
            return await server.read_node(UUID, output_file=str(tmp_path / "note.md"))

    result = asyncio.run(run_absolute())
    assert result["output_file"] == str(tmp_path / "note.md")
    assert result["path_record"]["translated"] is False
    assert (tmp_path / "note.md").read_bytes() == b"# note\n"


def test_describe_and_effects_remain_on_read_safe_registry_path():
    registry = server.build_node_registry()
    spec = registry.resolve("read")
    assert spec.cache_backed is True
    assert spec.side_effects == ("pure_read", "artifact_write")
    assert spec.async_mode == "never"
    assert mutation_admission_class(spec.canonical_name, spec.side_effects, spec.cache_backed) == "proceed"
    described = asyncio.run(dispatch_operation(registry, "describe", {"of": "read", "detail": "full"}))
    assert "output_file" in str(described)
    assert "acknowledgement only" in str(described)
