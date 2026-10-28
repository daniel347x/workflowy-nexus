"""Tests for node.append_note / node.prepend_note ([ap:MZOKZ1]).

The decorated tool functions are plain callables in this codebase, so tests
invoke the impls (and the registry boundary) directly with a monkeypatched
client, handle resolver, and rate limiter -- the same harness as
test_edit_node_note_search_replace.py.

The contract: exact byte-level concatenation on the semantic note (ONE live
read + wire->semantic decode + pure-LF normalization, ONE write through
_node_edit_core with the same snapshot injected), explicit separator (default
"\n", skipped when the existing note is empty), loud error on empty text,
AP-ID/title/tags untouched, size returned so growth is visible without a
re-read, and no diff.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.models.node import WorkFlowyNode  # noqa: E402
from test_mutation_regions_n05_fences import legacy_region_fronts


@pytest.fixture(autouse=True)
def _region_boundary(monkeypatch, tmp_path):
    legacy_region_fronts(monkeypatch, tmp_path,
        nodes=[{"id": "11111111-1111-4111-8111-111111111111", "parent_id": None}],
        entries=("_node_edit_core", "_node_note_concat_impl"))


def _fake_node(name="Title", note=""):
    """A real WorkFlowyNode so model_copy/model_dump behave genuinely."""
    return WorkFlowyNode(id="11111111-1111-4111-8111-111111111111", name=name, note=note)


def _patch_boundary(monkeypatch, client):
    """Neutralize the client, handle resolution, and rate limiter."""
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)

    async def _resolve(node_id, *, label, required=True, role=None):
        return node_id

    monkeypatch.setattr(server, "_resolve_mutator_node_id", _resolve)
    monkeypatch.setattr(server, "_resolve_generic_node_handle", _resolve)


def _note_client(note, name="Title"):
    """A MagicMock client whose get_node serves ``note`` and update_node
    records the request, returning a fresh node."""
    current = _fake_node(name=name, note=note)
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node()
    client.update_node = AsyncMock(return_value=updated)
    return client, current


# --- Concatenation semantics -------------------------------------------------


def test_append_joins_with_default_newline(monkeypatch):
    client, _ = _note_client(note="first part")
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server._node_append_note_impl("11111111-1111-4111-8111-111111111111", "second part"))

    request = client.update_node.call_args[0][1]
    assert request.note == "first part\nsecond part"
    assert result["success"] is True
    assert result["node_id"] == "11111111-1111-4111-8111-111111111111"
    assert result["position"] == "append"
    assert result["note_chars"] == len("first part\nsecond part")
    assert result["note_lines"] == 2
    assert result["added_chars"] == len("\nsecond part")


def test_prepend_puts_text_before_existing_note(monkeypatch):
    client, _ = _note_client(note="existing")
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server._node_prepend_note_impl("11111111-1111-4111-8111-111111111111", "header"))

    request = client.update_node.call_args[0][1]
    assert request.note == "header\nexisting"
    assert result["position"] == "prepend"
    assert result["added_chars"] == len("header\n")


def test_separator_is_explicit_and_exact(monkeypatch):
    client, _ = _note_client(note="A")
    _patch_boundary(monkeypatch, client)

    asyncio.run(server._node_append_note_impl("11111111-1111-4111-8111-111111111111", "B", separator="\n\n"))
    assert client.update_node.call_args[0][1].note == "A\n\nB"

    client2, _ = _note_client(note="A")
    _patch_boundary(monkeypatch, client2)
    asyncio.run(server._node_append_note_impl("11111111-1111-4111-8111-111111111111", "C", separator=""))
    assert client2.update_node.call_args[0][1].note == "AC"


def test_empty_existing_note_takes_no_separator(monkeypatch):
    client, _ = _note_client(note="")
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server._node_append_note_impl("11111111-1111-4111-8111-111111111111", "first"))

    # Nothing to separate: no phantom leading newline on an empty note.
    assert client.update_node.call_args[0][1].note == "first"
    assert result["added_chars"] == len("first")

    client2, _ = _note_client(note="")
    _patch_boundary(monkeypatch, client2)
    asyncio.run(server._node_prepend_note_impl("11111111-1111-4111-8111-111111111111", "first"))
    assert client2.update_node.call_args[0][1].note == "first"


def test_text_is_written_verbatim_no_stripping(monkeypatch):
    client, _ = _note_client(note="keep")
    _patch_boundary(monkeypatch, client)

    asyncio.run(server._node_append_note_impl("11111111-1111-4111-8111-111111111111", "  spaced  "))
    assert client.update_node.call_args[0][1].note == "keep\n  spaced  "


# --- Loud refusals -----------------------------------------------------------


def test_empty_text_is_a_loud_error_before_any_io(monkeypatch):
    client, _ = _note_client(note="A")
    _patch_boundary(monkeypatch, client)

    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(server._node_append_note_impl("11111111-1111-4111-8111-111111111111", ""))
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(server._node_prepend_note_impl("11111111-1111-4111-8111-111111111111", ""))
    # Zero mutation, zero reads: validation fails before resolution and I/O.
    client.get_node.assert_not_called()
    client.update_node.assert_not_called()


def test_non_string_text_is_a_loud_error(monkeypatch):
    client, _ = _note_client(note="A")
    _patch_boundary(monkeypatch, client)
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(server._node_append_note_impl("11111111-1111-4111-8111-111111111111", None))
    client.update_node.assert_not_called()


def test_separator_must_be_a_string(monkeypatch):
    client, _ = _note_client(note="A")
    _patch_boundary(monkeypatch, client)
    with pytest.raises(ValueError, match="separator"):
        asyncio.run(server._node_append_note_impl("11111111-1111-4111-8111-111111111111", "B", separator=123))
    client.update_node.assert_not_called()


# --- Wire decode, newline normalization, and the one-read-one-write shape ----


def test_wire_decode_and_newline_normalization_before_concat(monkeypatch):
    # The live note arrives in WIRE form (entity-encoded, CRLF). The concat
    # runs on the SEMANTIC domain exactly as the note search/replace path
    # does: one entity layer decoded, CRLF/CR normalized to pure LF.
    wire_note = "alpha &amp; beta\r\nsecond\rthird"
    current = _fake_node(note=wire_note)
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    asyncio.run(server._node_append_note_impl("11111111-1111-4111-8111-111111111111", "tail"))

    request = client.update_node.call_args[0][1]
    assert request.note == "alpha & beta\nsecond\nthird\ntail"
    assert "\r" not in request.note


def test_one_live_read_one_write_name_and_tags_untouched(monkeypatch):
    client, current = _note_client(note="body", name="My Title #tag-a #tag-b")
    _patch_boundary(monkeypatch, client)

    asyncio.run(server._node_append_note_impl("11111111-1111-4111-8111-111111111111", "more"))

    # ONE live GET (the note read) and ONE write; the write carries ONLY the
    # note -- name (title + trailing tags) and layout ride as None, so the
    # stored AP-ID / title / tags are untouched by construction.
    client.get_node.assert_awaited_once()
    client.update_node.assert_awaited_once()
    request = client.update_node.call_args[0][1]
    assert request.note == "body\nmore"
    assert request.name is None
    assert request.layoutMode is None


# --- The acceptance scenario: a large note written in parts ------------------


def test_large_note_built_in_parts_reads_back_in_order(monkeypatch):
    """~300 KB note: point.capture analogue (part 1 as the initial note) then
    seven appends, one per call -- final note is the parts in order joined by
    exactly the requested separators."""

    class _StatefulClient:
        """Stores the semantic note like the service would (no re-encoding)."""

        def __init__(self, initial_note):
            self.note = initial_note
            self.reads = 0
            self.writes = 0

        async def get_node(self, node_id):
            self.reads += 1
            return _fake_node(note=self.note)

        async def update_node(self, node_id, request, *, basis=None):
            self.writes += 1
            self.note = request.note
            return _fake_node()

    parts = [f"part {i}: " + "x" * 38_000 for i in range(8)]
    client = _StatefulClient(parts[0])
    _patch_boundary(monkeypatch, client)

    for part in parts[1:]:
        result = asyncio.run(server._node_append_note_impl("11111111-1111-4111-8111-111111111111", part))

    expected = "\n".join(parts)
    assert client.note == expected
    assert len(client.note) >= 300_000
    assert result["note_chars"] == len(expected)
    assert result["note_lines"] == 8
    assert result["added_chars"] == len("\n" + parts[-1])
    # Eight parts: one write per call, one read per call (each call re-reads
    # the live note once -- the ONE-read-ONE-write contract is per call).
    assert client.writes == 7
    assert client.reads == 7


# --- Registry boundary --------------------------------------------------------


def test_dispatch_surfaces_empty_text_as_a_loud_error_envelope(monkeypatch):
    """The empty-text ValueError survives the family boundary as a loud,
    actionable error envelope (never a silent success)."""

    async def _raise(node_handle, text, separator="\n"):
        raise ValueError(
            "text must be a non-empty string: an append/prepend with no text "
            "would silently do nothing; pass the part to add."
        )

    monkeypatch.setattr(server, "_node_append_note_impl", _raise)
    registry = server.build_node_registry()
    result = asyncio.run(
        server.dispatch_operation(
            registry, "append_note", {"node_handle": "N", "text": ""}
        )
    )
    assert result["success"] is False
    assert result["error"]["code"] == "operation_failed"
    assert "non-empty" in result["error"]["message"]
