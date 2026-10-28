"""Tests for workflowy_edit_node note_search/note_replace + note_edits modes
(and the workflowy_read_node as_raw_node mode).

The decorated tool functions are plain callables in this codebase (the
FastMCP tool decorator returns the function unchanged), so tests invoke
server.edit_node / server.read_node directly with a monkeypatched client,
handle resolver, and rate limiter.

server.py lives under src/workflowy_mcp/ and is therefore covered by the
editable install -- no reinstall is needed after editing it.
"""

import asyncio
import json
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
        entries=("_node_edit_core",))


def _fake_node(name="Title", note=""):
    """A real WorkFlowyNode so model_copy/model_dump behave genuinely."""
    return WorkFlowyNode(id="11111111-1111-4111-8111-111111111111", name=name, note=note)


def _parse_edit_report(report: str) -> tuple[dict, str]:
    """Test helper for the human-facing edit report document.

    The JSON metadata object must END before the blank line; everything after
    it is literal raw diff text. A no-change report has metadata only.
    """
    assert isinstance(report, str)
    if "\n\n" in report:
        header, diff = report.split("\n\n", 1)
    else:
        header, diff = report.rstrip("\n"), ""
    metadata = json.loads(header)
    assert "diff" not in metadata
    return metadata, diff


def _patch_boundary(monkeypatch, client):
    """Neutralize the client, handle resolution, and rate limiter."""
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)

    async def _resolve(node_id, *, label, required=True, role=None):
        return node_id

    monkeypatch.setattr(server, "_resolve_mutator_node_id", _resolve)
    monkeypatch.setattr(server, "_resolve_generic_node_handle", _resolve)


# --- Argument validation (raises before any client interaction) ---


def test_search_replace_rejects_empty_search():
    with pytest.raises(ValueError, match="must not be empty"):
        asyncio.run(server.edit_node("some-uuid", note_search="", note_replace="x"))


def test_search_replace_rejects_lone_note_replace():
    with pytest.raises(ValueError, match="requires note_search"):
        asyncio.run(server.edit_node("some-uuid", note_replace="x"))


def test_search_replace_rejects_combination_with_other_fields():
    # note (wholesale replace conflicts with the computed note) and _completed
    # (deprecated here) REMAIN exclusive. name + layout_mode are now ALLOWED
    # alongside a search-and-replace (disjoint fields, same single update call).
    with pytest.raises(ValueError, match="cannot be combined"):
        asyncio.run(
            server.edit_node("some-uuid", note="full note", note_search="a", note_replace="b")
        )
    with pytest.raises(ValueError, match="cannot be combined"):
        asyncio.run(
            server.edit_node("some-uuid", _completed=True, note_search="a", note_replace="b")
        )


# --- Search-and-replace behavior ---


def test_search_replace_first_occurrence_only(monkeypatch):
    current = _fake_node(note="foo and foo again")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", note_search="foo", note_replace="bar"))

    # The WRITE carries the full edited note; the RESPONSE is a compact diff report.
    request = client.update_node.call_args[0][1]
    assert request.note == "bar and foo again"
    metadata, diff = _parse_edit_report(result)
    assert metadata["success"] is True
    assert metadata["no_op"] is False
    assert metadata["note_changed"] is True
    assert metadata["node_id"] == "11111111-1111-4111-8111-111111111111"
    assert metadata["edits_applied"] == 1
    assert metadata["edits_noop"] == 0
    assert metadata["edit_results"] == [{"ordinal": 0, "matched": True, "index": 0}]
    assert "-foo and foo again" in diff
    assert "+bar and foo again" in diff
    assert "note (before)" in diff
    assert "note (after)" in diff
    # Human-facing contract: the JSON object ends, then the diff is literal.
    assert result.startswith("{\n")
    assert "\n\n--- note (before)\n+++ note (after)\n" in result
    assert '"diff"' not in result.split("\n\n", 1)[0]


def test_search_replace_no_match_is_noop(monkeypatch):
    current = _fake_node(note="hello world")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    client.update_node = AsyncMock()
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", note_search="absent", note_replace="x"))

    # No match AND nothing else to write -> a true no-op (NO API write), empty diff.
    client.update_node.assert_not_called()
    metadata, diff = _parse_edit_report(result)
    assert metadata["success"] is True
    assert metadata["no_op"] is True
    assert metadata["note_changed"] is False
    assert metadata["edits_applied"] == 0
    assert metadata["edits_noop"] == 1
    assert metadata["edit_results"] == [{"ordinal": 0, "matched": False, "index": -1}]
    assert diff == ""


def test_search_replace_matches_across_crlf_lf(monkeypatch):
    current = _fake_node(note="alpha\r\nbeta")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", note_search="alpha\nbeta", note_replace="gamma"))

    request = client.update_node.call_args[0][1]
    assert request.note == "gamma"


def test_search_replace_final_note_is_always_pure_lf(monkeypatch):
    """CRLF inside note_replace is canonicalized: the submitted note is pure LF."""
    current = _fake_node(note="start\r\nmiddle\r\nend")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    asyncio.run(
        server.edit_node("11111111-1111-4111-8111-111111111111", note_search="middle", note_replace="new\r\nlines")
    )

    request = client.update_node.call_args[0][1]
    assert request.note == "start\nnew\nlines\nend"
    assert "\r" not in request.note


def test_search_replace_delete_default(monkeypatch):
    current = _fake_node(note="remove THIS part")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", note_search=" THIS"))

    request = client.update_node.call_args[0][1]
    assert request.note == "remove part"


def test_search_prefetch_reports_rate_limit(monkeypatch):
    """A RateLimitError escaping the prefetch GET is reported to the limiter."""

    class RateLimitError(Exception):
        pass

    err = RateLimitError("slow down")
    err.retry_after = 30

    client = MagicMock()
    client.get_node = AsyncMock(side_effect=err)
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", limiter)

    async def _resolve(node_id, *, label, required=True, role=None):
        return node_id

    monkeypatch.setattr(server, "_resolve_mutator_node_id", _resolve)

    with pytest.raises(RateLimitError):
        asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", note_search="a", note_replace="b"))

    limiter.on_rate_limit.assert_called_once_with(30)


# --- get_node as_raw_note ---


def test_get_node_as_raw_note(monkeypatch):
    node = _fake_node(name="My Title", note="line one\nline two")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=node)
    _patch_boundary(monkeypatch, client)

    out = asyncio.run(server.read_node("11111111-1111-4111-8111-111111111111", as_raw_node=True))

    assert out == "[name: not editable] My Title\n\nline one\nline two"


def test_get_node_as_raw_note_decodes_one_layer(monkeypatch):
    """as_raw_note returns semantic (GUI-visible) text: exactly one decode layer."""
    node = _fake_node(
        name="Title",
        note="Replace &lt;placeholder_name&gt; &amp; show &amp;lt;Hello&amp;gt; <code>a &amp;lt; b</code>",
    )
    client = MagicMock()
    client.get_node = AsyncMock(return_value=node)
    _patch_boundary(monkeypatch, client)

    out = asyncio.run(server.read_node("11111111-1111-4111-8111-111111111111", as_raw_node=True))

    assert out == (
        "[name: not editable] Title\n\n"
        "Replace <placeholder_name> & show &lt;Hello&gt; <code>a &lt; b</code>"
    )


def test_search_replace_semantic_round_trip_byte_identical(monkeypatch):
    """Untouched wire content is byte-identical after decode->splice->encode.

    Simulates the real pipeline: the server decodes the fetched wire-form note,
    splices in the semantic replacement, and the client encoder re-encodes. We
    apply the REAL encoder to the captured request.note to prove the final wire
    form differs from the original ONLY at the replacement site.
    """
    from workflowy_mcp.client.api_client_core import WorkFlowyClientCore

    wire_note = (
        "start &lt;tag&gt; <code>x &amp;lt; y</code> "
        "UNIQUE_TOKEN &amp;lt;Hello&amp;gt; end"
    )
    current = _fake_node(note=wire_note)
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    asyncio.run(
        server.edit_node(
            "11111111-1111-4111-8111-111111111111", note_search="UNIQUE_TOKEN", note_replace="REPLACED & <done>"
        )
    )

    request = client.update_node.call_args[0][1]
    final_wire = WorkFlowyClientCore.encode_for_workflowy_api(
        request.note, "note", operation="update"
    )
    # Update-path ampersand rule (discovered via this test): the update
    # encoder encodes '&' -> '&amp;' ONLY when it begins an entity pattern
    # (so literal-entity text like '&lt;Hello&gt;' IS preserved
    # byte-identically), while a BARE '&' is left alone (renders identically
    # in the GUI). Untouched entity content is byte-identical.
    assert final_wire == (
        "start &lt;tag&gt; <code>x &amp;lt; y</code> "
        "REPLACED & &lt;done&gt; &amp;lt;Hello&amp;gt; end"
    )


def test_search_replace_unwraps_auto_links(monkeypatch):
    """Workflowy auto-link wrappers unwrap to the bare URL in the semantic domain.

    Wire form carries Workflowy's auto-link wrapper (<a href="URL">URL</a>);
    the semantic splice must see (and submit) the BARE URL. Workflowy re-wraps
    it server-side on write, so the link survives the round trip.
    """
    wire_note = (
        'see [search](<a href="https://example.com/q?a=1&b=2">'
        "https://example.com/q?a=1&amp;b=2</a>) UNIQUE_TOKEN done"
    )
    current = _fake_node(note=wire_note)
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    asyncio.run(
        server.edit_node("11111111-1111-4111-8111-111111111111", note_search="UNIQUE_TOKEN", note_replace="REPLACED")
    )

    request = client.update_node.call_args[0][1]
    assert "<a href" not in request.note
    assert request.note == "see [search](https://example.com/q?a=1&b=2) REPLACED done"


def test_named_links_are_not_unwrapped(monkeypatch):
    """A named link (anchor text != href) must NOT be unwrapped (anchor text preserved)."""
    wire_note = 'see <a href="https://example.com">Example Label</a> UNIQUE_TOKEN done'
    current = _fake_node(note=wire_note)
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    asyncio.run(
        server.edit_node("11111111-1111-4111-8111-111111111111", note_search="UNIQUE_TOKEN", note_replace="REPLACED")
    )

    request = client.update_node.call_args[0][1]
    assert '<a href="https://example.com">Example Label</a>' in request.note
    assert "REPLACED" in request.note


def test_encoded_anchor_source_not_unwrapped(monkeypatch):
    """Intentionally authored encoded HTML source (&lt;a href=...&gt;) is decoded
    to literal tags by html.unescape but is NOT unwrapped to a bare URL.

    The auto-link regex runs on the WIRE form where only literal <a href> tags
    match; entity-encoded source (&lt;a) is invisible to the regex and survives
    as a literal anchor tag after one-layer decoding.
    """
    wire = 'see &lt;a href="https://x"&gt;https://x&lt;/a&gt; UNIQUE_TOKEN done'
    current = _fake_node(note=wire)
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    asyncio.run(
        server.edit_node("11111111-1111-4111-8111-111111111111", note_search="UNIQUE_TOKEN", note_replace="REPLACED")
    )

    request = client.update_node.call_args[0][1]
    assert '<a href="https://x">https://x</a>' in request.note
    assert "REPLACED" in request.note


def test_get_node_as_raw_note_preserves_encoded_anchor_source(monkeypatch):
    """as_raw_note decodes encoded HTML source to literal tags, NOT bare URLs."""
    node = _fake_node(
        name="Title",
        note='see &lt;a href="https://x"&gt;https://x&lt;/a&gt; end',
    )
    client = MagicMock()
    client.get_node = AsyncMock(return_value=node)
    _patch_boundary(monkeypatch, client)

    out = asyncio.run(server.read_node("11111111-1111-4111-8111-111111111111", as_raw_node=True))

    assert out == (
        "[name: not editable] Title\n\n"
        'see <a href="https://x">https://x</a> end'
    )


def test_get_node_as_raw_note_unwraps_autolink(monkeypatch):
    """as_raw_note shows the bare URL, not the <a href> wrapper."""
    node = _fake_node(
        name="Title",
        note=(
            'see [search](<a href="https://example.com/q?a=1&b=2">'
            "https://example.com/q?a=1&amp;b=2</a>) end"
        ),
    )
    client = MagicMock()
    client.get_node = AsyncMock(return_value=node)
    _patch_boundary(monkeypatch, client)

    out = asyncio.run(server.read_node("11111111-1111-4111-8111-111111111111", as_raw_node=True))

    assert out == (
        "[name: not editable] Title\n\n"
        "see [search](https://example.com/q?a=1&b=2) end"
    )


# --- Multi-edit (note_edits) argument validation ---


def _fake_node_with_dump(name="Title", note=""):
    """Alias kept for readability: multi-edit mode embeds node.model_dump() in
    the report, and a real WorkFlowyNode provides the genuine model_dump."""
    return _fake_node(name, note)


def test_note_edits_rejects_combination_with_single_form():
    with pytest.raises(ValueError, match="cannot be combined with note_search"):
        asyncio.run(
            server.edit_node(
                "some-uuid",
                note_edits=[{"search": "a", "replace": "b"}],
                note_search="a",
                note_replace="b",
            )
        )


def test_note_edits_rejects_combination_with_other_fields():
    # note + _completed remain exclusive; name + layout_mode are now ALLOWED.
    with pytest.raises(ValueError, match="cannot be combined"):
        asyncio.run(server.edit_node("some-uuid", note="full note", note_edits=[{"search": "a"}]))
    with pytest.raises(ValueError, match="cannot be combined"):
        asyncio.run(server.edit_node("some-uuid", _completed=True, note_edits=[{"search": "a"}]))


def test_note_edits_rejects_empty_list():
    with pytest.raises(ValueError, match="non-empty list"):
        asyncio.run(server.edit_node("some-uuid", note_edits=[]))


def test_note_edits_rejects_non_object_item():
    with pytest.raises(ValueError, match=r"note_edits\[0\] must be an object"):
        asyncio.run(server.edit_node("some-uuid", note_edits=["oops"]))


def test_note_edits_rejects_empty_item_search():
    with pytest.raises(ValueError, match=r"note_edits\[1\]\.search must be a non-empty string"):
        asyncio.run(server.edit_node("some-uuid", note_edits=[{"search": "a"}, {"search": ""}]))


def test_note_edits_rejects_malformed_json_string():
    with pytest.raises(ValueError, match="did not parse"):
        asyncio.run(server.edit_node("some-uuid", note_edits="this is not json"))


# --- Multi-edit (note_edits) behavior ---


def test_note_edits_sequential_chained_edits(monkeypatch):
    """Each edit sees the evolving text left by the previous edits (a later
    edit CAN match text an earlier edit just inserted); a miss is a per-edit
    no-op; the whole batch is ONE live fetch + ONE write."""
    current = _fake_node_with_dump(note="foo and bar")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node_with_dump()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(
        server.edit_node(
            "11111111-1111-4111-8111-111111111111",
            note_edits=[
                {"search": "foo", "replace": "foobar"},
                {"search": "foobar", "replace": "BAZ"},  # matches text edit 1 inserted
                {"search": "absent", "replace": "x"},     # per-edit no-op
            ],
        )
    )

    client.get_node.assert_called_once()
    client.update_node.assert_called_once()
    request = client.update_node.call_args[0][1]
    assert request.note == "BAZ and bar"

    metadata, diff = _parse_edit_report(result)
    assert metadata["success"] is True
    assert metadata["no_op"] is False
    assert metadata["note_changed"] is True
    assert metadata["edits_applied"] == 2
    assert metadata["edits_noop"] == 1
    assert [row["matched"] for row in metadata["edit_results"]] == [True, True, False]
    assert metadata["edit_results"][2]["index"] == -1
    # The compact ledger does NOT echo the full search/replace strings.
    assert all("search" not in row and "replace" not in row for row in metadata["edit_results"])
    # ONE aggregate before->after diff of the semantic note.
    assert "-foo and bar" in diff
    assert "+BAZ and bar" in diff


def test_note_edits_all_miss_is_noop(monkeypatch):
    """Every edit missing -> NO API write; a structured no_op report."""
    current = _fake_node_with_dump(note="hello world")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    client.update_node = AsyncMock()
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(
        server.edit_node(
            "11111111-1111-4111-8111-111111111111",
            note_edits=[{"search": "nope"}, {"search": "nada", "replace": "x"}],
        )
    )

    client.update_node.assert_not_called()
    metadata, diff = _parse_edit_report(result)
    assert metadata["success"] is True
    assert metadata["no_op"] is True
    assert metadata["note_changed"] is False
    assert metadata["edits_applied"] == 0
    assert metadata["edits_noop"] == 2
    assert [row["matched"] for row in metadata["edit_results"]] == [False, False]
    assert diff == ""


def test_note_edits_replace_default_is_deletion(monkeypatch):
    """An omitted 'replace' means deletion (single-form parity)."""
    current = _fake_node_with_dump(note="aXb")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node_with_dump()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", note_edits=[{"search": "X"}]))

    request = client.update_node.call_args[0][1]
    assert request.note == "ab"
    metadata, diff = _parse_edit_report(result)
    assert metadata["edit_results"] == [{"ordinal": 0, "matched": True, "index": 1}]
    assert "-aXb" in diff
    assert "+ab" in diff


def test_note_edits_accepts_json_string(monkeypatch):
    """The stringified-JSON form (the ETCH `nodes` precedent) parses identically."""
    current = _fake_node_with_dump(note="foo baz")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node_with_dump()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    payload = json.dumps([{"search": "foo", "replace": "bar"}])
    result = asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", note_edits=payload))

    request = client.update_node.call_args[0][1]
    assert request.note == "bar baz"
    metadata, _diff = _parse_edit_report(result)
    assert metadata["edits_applied"] == 1


def test_note_edits_crlf_normalized_per_item(monkeypatch):
    """Search matches across CRLF/LF, and CRLF inside a replace is canonicalized:
    the submitted note is pure LF."""
    current = _fake_node_with_dump(note="alpha\r\nbeta")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node_with_dump()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    asyncio.run(
        server.edit_node(
            "11111111-1111-4111-8111-111111111111",
            note_edits=[{"search": "alpha\nbeta", "replace": "one\r\ntwo"}],
        )
    )

    request = client.update_node.call_args[0][1]
    assert request.note == "one\ntwo"


def test_note_edits_first_occurrence_only_per_edit(monkeypatch):
    """Each edit replaces only the FIRST occurrence in the evolving text."""
    current = _fake_node_with_dump(note="foo and foo")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node_with_dump()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", note_edits=[{"search": "foo", "replace": "bar"}]))

    request = client.update_node.call_args[0][1]
    assert request.note == "bar and foo"


# --- note_edits item-level aliases: oldText/newText for search/replace (O7RP65) ---


def test_note_edits_accepts_oldtext_newtext_aliases(monkeypatch):
    """An item shaped {oldText, newText} edits identically to {search, replace}
    (the filesystem edit_file grammar, accepted per item)."""
    current = _fake_node_with_dump(note="foo and bar")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node_with_dump()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(
        server.edit_node(
            "11111111-1111-4111-8111-111111111111",
            note_edits=[{"oldText": "foo", "newText": "foobar"}],
        )
    )

    request = client.update_node.call_args[0][1]
    assert request.note == "foobar and bar"
    metadata, diff = _parse_edit_report(result)
    assert metadata["success"] is True
    assert metadata["edits_applied"] == 1
    assert metadata["edit_results"] == [{"ordinal": 0, "matched": True, "index": 0}]
    assert "-foo and bar" in diff
    assert "+foobar and bar" in diff


def test_note_edits_accepts_mixed_alias_and_canonical_keys(monkeypatch):
    """Mixing both spellings across items (and within one item) is accepted:
    keys are read after alias resolution, so any combination works."""
    current = _fake_node_with_dump(note="foo and bar")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node_with_dump()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    asyncio.run(
        server.edit_node(
            "11111111-1111-4111-8111-111111111111",
            note_edits=[
                {"oldText": "foo", "replace": "FOO"},   # alias + canonical, mixed
                {"search": "bar", "newText": "BAR"},    # canonical + alias, mixed
            ],
        )
    )

    request = client.update_node.call_args[0][1]
    assert request.note == "FOO and BAR"


def test_note_edits_alias_canonical_conflict_different_values_rejected(monkeypatch):
    """An alias and its canonical key in ONE item with DIFFERENT values is a
    loud per-item rejection naming both keys; NO write occurs."""
    current = _fake_node_with_dump(note="foo and bar")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    client.update_node = AsyncMock()
    _patch_boundary(monkeypatch, client)

    with pytest.raises(
        ValueError,
        match=r"note_edits\[0\]: keys 'oldText' and 'search' are both present with different values",
    ):
        asyncio.run(
            server.edit_node(
                "11111111-1111-4111-8111-111111111111",
                note_edits=[{"search": "foo", "oldText": "bar", "replace": "x"}],
            )
        )

    client.update_node.assert_not_called()


def test_note_edits_alias_canonical_identical_values_accepted(monkeypatch):
    """An alias and its canonical key with IDENTICAL values are harmless:
    accepted, the alias key dropped, and the edit behaves exactly like the
    canonical-only spelling (deliberately different from the top-level
    param-alias rule, which rejects even identical)."""
    current = _fake_node_with_dump(note="foo and bar")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node_with_dump()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    asyncio.run(
        server.edit_node(
            "11111111-1111-4111-8111-111111111111",
            note_edits=[{"search": "foo", "oldText": "foo", "newText": "FOO"}],
        )
    )

    request = client.update_node.call_args[0][1]
    assert request.note == "FOO and bar"


def test_note_edits_accepts_json_string_with_alias_keys(monkeypatch):
    """The stringified-JSON form accepts alias keys too (same normalization)."""
    current = _fake_node_with_dump(note="foo baz")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node_with_dump()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    asyncio.run(
        server.edit_node(
            "11111111-1111-4111-8111-111111111111",
            note_edits=json.dumps([{"oldText": "baz", "newText": "qux"}]),
        )
    )

    request = client.update_node.call_args[0][1]
    assert request.note == "foo qux"


# --- Combining search-and-replace with disjoint fields (name / layout_mode) ---


def test_search_replace_allows_name_combination(monkeypatch):
    """name + single edit in ONE call: the single update carries both fields."""
    current = _fake_node(note="foo and foo again")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(
        server.edit_node("11111111-1111-4111-8111-111111111111", name="New title", note_search="foo", note_replace="bar")
    )

    client.get_node.assert_called_once()
    client.update_node.assert_called_once()
    request = client.update_node.call_args[0][1]
    assert request.name == "New title"
    assert request.note == "bar and foo again"
    metadata, diff = _parse_edit_report(result)
    assert metadata["no_op"] is False
    assert metadata["note_changed"] is True
    assert "+bar and foo again" in diff


def test_search_replace_miss_still_writes_name(monkeypatch):
    """A search miss is a NOTE no-op, not a call no-op: with a name update
    also requested, the name write still happens and the note is untouched."""
    current = _fake_node(note="hello world")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(
        server.edit_node("11111111-1111-4111-8111-111111111111", name="New title", note_search="absent", note_replace="x")
    )

    client.update_node.assert_called_once()
    request = client.update_node.call_args[0][1]
    assert request.name == "New title"
    assert request.note is None  # note untouched
    metadata, diff = _parse_edit_report(result)
    assert metadata["no_op"] is False  # a write DID happen (the name)
    assert metadata["note_changed"] is False  # but the note itself did not change
    assert metadata["edits_applied"] == 0
    assert diff == ""


def test_note_edits_allows_name_and_layout_combination(monkeypatch):
    """name + layout_mode + multi-edit in ONE call: one update, one report."""
    current = _fake_node_with_dump(note="foo and bar")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node_with_dump()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(
        server.edit_node(
            "11111111-1111-4111-8111-111111111111",
            name="New title",
            layout_mode="h1",
            note_edits=[{"search": "foo", "replace": "baz"}, {"search": "absent"}],
        )
    )

    client.get_node.assert_called_once()
    client.update_node.assert_called_once()
    request = client.update_node.call_args[0][1]
    assert request.name == "New title"
    assert request.layoutMode == "h1"
    assert request.note == "baz and bar"
    metadata, _diff = _parse_edit_report(result)
    assert metadata["no_op"] is False
    assert metadata["edits_applied"] == 1
    assert metadata["edits_noop"] == 1


def test_note_edits_all_miss_with_name_still_writes(monkeypatch):
    """All edits miss but a name update was requested: the name write happens
    (note untouched) and the report is honest -- no_op False, edits_applied 0."""
    current = _fake_node_with_dump(note="hello world")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node_with_dump()
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(
        server.edit_node(
            "11111111-1111-4111-8111-111111111111",
            name="New title",
            note_edits=[{"search": "nope"}, {"search": "nada", "replace": "x"}],
        )
    )

    client.update_node.assert_called_once()
    request = client.update_node.call_args[0][1]
    assert request.name == "New title"
    assert request.note is None
    metadata, diff = _parse_edit_report(result)
    assert metadata["success"] is True
    assert metadata["no_op"] is False  # a write DID happen (the name)
    assert metadata["note_changed"] is False  # but the note itself did not change
    assert metadata["edits_applied"] == 0
    assert metadata["edits_noop"] == 2
    assert diff == ""


def test_diff_report_shows_change_but_write_carries_full_note(monkeypatch):
    """The agent-facing response is a compact diff report (no note body, no
    full search/replace echo), while the submitted write carries the full
    edited note and the client's node object is never mutated in place."""
    current = _fake_node(note="hello UNIQUE world")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node(note="hello REPLACED world")
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(
        server.edit_node("11111111-1111-4111-8111-111111111111", note_search="UNIQUE", note_replace="REPLACED")
    )

    # The WRITE still carries the full edited note.
    request = client.update_node.call_args[0][1]
    assert request.note == "hello REPLACED world"
    metadata, diff = _parse_edit_report(result)
    # The RESPONSE shows ONLY the change (the diff), not the note body.
    assert metadata["note_changed"] is True
    assert "-hello UNIQUE world" in diff
    assert "+hello REPLACED world" in diff
    # The compact ledger carries no full search/replace strings.
    assert metadata["edit_results"] == [{"ordinal": 0, "matched": True, "index": 6}]
    # The client's node object was NOT mutated in place.
    assert updated.note == "hello REPLACED world"


# --- read_node default-mode + tool registration ---


def test_read_node_default_is_raw(monkeypatch):
    """read_node returns the raw semantic note string BY DEFAULT (the document
    is the primary read surface now)."""
    node = _fake_node(name="Doc", note="line one\nline two")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=node)
    _patch_boundary(monkeypatch, client)

    out = asyncio.run(server.read_node("11111111-1111-4111-8111-111111111111"))

    assert out == "[name: not editable] Doc\n\nline one\nline two"


def test_read_node_structured_when_not_raw(monkeypatch):
    """as_raw_node=False returns the structured WorkFlowyNode object."""
    node = _fake_node(name="Doc", note="body")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=node)
    _patch_boundary(monkeypatch, client)

    out = asyncio.run(server.read_node("11111111-1111-4111-8111-111111111111", as_raw_node=False))

    assert out is node
    assert out.note == "body"


def test_edit_node_diff_empty_on_identical_replace(monkeypatch):
    """A matched replace that produces IDENTICAL text still writes but reports
    no change: note_changed=False and an empty diff (honest, not a fake diff)."""
    current = _fake_node(note="foo bar")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node(note="foo bar")
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", note_search="foo", note_replace="foo"))

    client.update_node.assert_called_once()  # a write DID occur
    metadata, diff = _parse_edit_report(result)
    assert metadata["no_op"] is False
    assert metadata["note_changed"] is False
    assert metadata["edits_applied"] == 1  # the edit DID match
    assert diff == ""  # but the content is unchanged


# --- Unicode space-fold matching + actionable near-miss diagnostics ---------


@pytest.mark.parametrize(
    "space",
    [
        "\u00a0",  # NBSP (Workflowy/contenteditable indentation)
        "\u1680",  # OGHAM SPACE MARK
        "\u2000", "\u2001", "\u2002", "\u2003", "\u2004", "\u2005",
        "\u2006", "\u2007", "\u2008", "\u2009", "\u200a",
        "\u202f",  # NARROW NO-BREAK SPACE
        "\u205f",  # MEDIUM MATHEMATICAL SPACE
        "\u3000",  # IDEOGRAPHIC SPACE
    ],
)
def test_note_space_fold_family_is_one_to_one(space):
    new_note, idx, info = server._apply_note_search_replace(
        f"A{space}B", "A B", "matched"
    )
    assert new_note == "matched"
    assert idx == 0
    assert info == {"tier": "space_fold"}


def test_exact_match_anywhere_beats_earlier_space_fold_candidate():
    note = "\u00a0target| target"
    new_note, idx, info = server._apply_note_search_replace(
        note, " target", " exact"
    )
    assert idx == len("\u00a0target|")
    assert new_note == "\u00a0target| exact"
    assert info == {}  # Tier 1 exact, not the earlier folded occurrence.


def test_space_fold_splice_uses_original_index_and_preserves_surroundings():
    note = "P\u00a0\u2009target\u00a0S"
    new_note, idx, info = server._apply_note_search_replace(
        note, "  target", "done"
    )
    assert idx == 1
    assert new_note == "Pdone\u00a0S"  # outside NBSP remains untouched
    assert info == {"tier": "space_fold"}


def test_space_fold_preserves_common_prefix_and_suffix_from_source():
    note = "\u00a0 old text\u2009\u00a0"
    search = "  old text  "
    replace = "  new text  "
    new_note, idx, info = server._apply_note_search_replace(
        note, search, replace
    )
    assert idx == 0
    assert new_note == "\u00a0 new text\u2009\u00a0"
    assert info == {"tier": "space_fold"}


def test_space_fold_exact_tier_can_deliberately_convert_nbsp_to_ascii():
    # Supplying the actual NBSP in search makes this Tier 1 exact. Tier 1 always
    # splices replacement verbatim, so explicit whitespace conversion remains
    # available despite Tier-2's conservative preservation default.
    new_note, idx, info = server._apply_note_search_replace(
        "\u00a0 old", "\u00a0 old", "  old"
    )
    assert (new_note, idx, info) == ("  old", 0, {})


def test_space_fold_changed_indentation_is_honored():
    # Q and R differ at indentation, so it belongs to the edited middle. The
    # exact common suffix 'old' is retained from source; the new tab is inserted.
    new_note, idx, info = server._apply_note_search_replace(
        "\u00a0 old", "  old", "\told"
    )
    assert (new_note, idx, info) == ("\told", 0, {"tier": "space_fold"})


def test_space_fold_short_replacement_caps_prefix_suffix_overlap():
    # Replacement is entirely a common prefix; suffix must stay zero rather
    # than overlapping the prefix. Original NBSP prefix remains load-bearing.
    new_note, idx, info = server._apply_note_search_replace(
        "\u00a0 aaa", "  aaa", "  a"
    )
    assert (new_note, idx, info) == ("\u00a0 a", 0, {"tier": "space_fold"})


def test_space_fold_deletion_remains_full_span_deletion():
    new_note, idx, info = server._apply_note_search_replace(
        "X\u00a0 oldY", "  old", ""
    )
    assert (new_note, idx, info) == ("XY", 1, {"tier": "space_fold"})


def test_space_fold_does_not_fold_tabs_or_collapse_runs():
    assert server._apply_note_search_replace("A\tB", "A B", "x")[1] == -1
    assert server._apply_note_search_replace("A  B", "A B", "x")[1] == -1


def test_nbsp_entity_incident_succeeds_first_call_and_reports_tier(monkeypatch):
    # Mirrors the real incident: wire '&nbsp; ' decodes to U+00A0 U+0020,
    # while the agent naturally supplies two ordinary U+0020 spaces.
    current = _fake_node(note="&nbsp; Retrieve old wording")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(
        server.edit_node(
            "11111111-1111-4111-8111-111111111111",
            note_search="  Retrieve old wording",
            note_replace="  Retrieve new wording",
        )
    )

    request = client.update_node.call_args[0][1]
    # Search/replacement both repeated the same two ASCII spaces, so Tier 2
    # interprets that prefix as unchanged context and retains the source's
    # load-bearing NBSP + ASCII-space indentation.
    assert request.note == "\u00a0 Retrieve new wording"
    metadata, _diff = _parse_edit_report(result)
    assert metadata["edit_results"] == [
        {"ordinal": 0, "matched": True, "index": 0, "tier": "space_fold"}
    ]


def test_multi_edit_inherits_space_fold_authority(monkeypatch):
    current = _fake_node(note="A\u00a0B and C\u2009D")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(
        server.edit_node(
            "11111111-1111-4111-8111-111111111111",
            note_edits=[
                {"search": "A B", "replace": "first"},
                {"search": "C D", "replace": "second"},
            ],
        )
    )
    assert client.update_node.call_args[0][1].note == "first and second"
    metadata, _diff = _parse_edit_report(result)
    assert [row.get("tier") for row in metadata["edit_results"]] == [
        "space_fold", "space_fold"
    ]


def test_zero_width_miss_returns_copyable_exact_retry(monkeypatch):
    current = _fake_node(note="alpha\u200bbeta")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    client.update_node = AsyncMock()
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(
        server.edit_node("11111111-1111-4111-8111-111111111111", note_search="alphabeta", note_replace="gamma")
    )
    client.update_node.assert_not_called()  # diagnostic only, never fuzzy mutation
    metadata, diff = _parse_edit_report(result)
    row = metadata["edit_results"][0]
    assert row["matched"] is False
    near = row["near_miss"]
    assert near["kind"] == "zero_width_mismatch"
    assert near["suggested_search"] == "alpha\u200bbeta"
    assert near["suggested_edit"] == {
        "search": "alpha\u200bbeta", "replace": "gamma"
    }
    assert near["first_difference"]["note"]["codepoint"] == "U+200B"
    assert "\\u200b" in result  # invisible character is visible in raw report JSON
    assert diff == ""

    # The returned suggested edit is sufficient for an exact retry.
    new_note, idx, info = server._apply_note_search_replace(
        current.note, near["suggested_edit"]["search"], near["suggested_edit"]["replace"]
    )
    assert (new_note, idx, info) == ("gamma", 0, {})


def test_unique_visible_anchor_returns_actionable_same_length_retry():
    note = "prefix uniqueAnchor actual tail"
    search = "prefix uniqueAnchor expect tail"  # same length; one differing word
    new_note, idx, info = server._apply_note_search_replace(note, search, "replacement")
    assert new_note == note and idx == -1
    near = info["near_miss"]
    assert near["kind"] == "anchored_text_mismatch"
    assert near["suggested_search"] == note
    assert near["suggested_edit"] == {"search": note, "replace": "replacement"}


# --- Existing surface/format locks -----------------------------------------


def test_tools_registered_under_new_names():
    """Consolidated default surface: node family present, retired names absent."""
    tools = asyncio.run(server.mcp.list_tools())
    names = {getattr(t, "name", None) for t in tools}
    assert "node" in names
    assert "workflowy_read_node" not in names
    assert "workflowy_edit_node" not in names
    assert "workflowy_get_node" not in names
    assert "workflowy_update_node" not in names


# --- unified-diff chunk separation (no trailing-newline notes) ---


def test_render_note_unified_diff_separates_single_line_change():
    """A single-line note with NO trailing newline must not concatenate the
    -before/+after chunks (the difflib keepends edge)."""
    diff = server._render_note_unified_diff("old text", "new text")
    assert diff == (
        "--- note (before)\n"
        "+++ note (after)\n"
        "@@ -1 +1 @@\n"
        "-old text\n"
        "+new text\n"
    )


def test_render_note_unified_diff_separates_final_line_without_newline():
    """A multiline note whose FINAL line lacks a trailing newline: the last
    -before/+after chunks stay on separate lines."""
    diff = server._render_note_unified_diff("alpha\nbeta\nold tail", "alpha\nbeta\nnew tail")
    lines = diff.splitlines()
    assert lines[0] == "--- note (before)"
    assert lines[1] == "+++ note (after)"
    assert "-old tail" in lines
    assert "+new tail" in lines
    assert not any("-old tail+new tail" in line for line in lines)


def test_edit_node_diff_chunks_are_newline_separated(monkeypatch):
    """End-to-end: edit_node's diff on a note with no trailing newline emits
    separate -before/+after lines (never a concatenated chunk)."""
    current = _fake_node(note="old text")  # no trailing newline
    client = MagicMock()
    client.get_node = AsyncMock(return_value=current)
    updated = _fake_node(note="new text")
    client.update_node = AsyncMock(return_value=updated)
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", note_search="old text", note_replace="new text"))

    _metadata, diff = _parse_edit_report(result)
    lines = diff.splitlines()
    assert "-old text" in lines
    assert "+new text" in lines
