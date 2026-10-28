"""Regression lock: add_to_anchor's ``content=`` single-node capture path + ``name`` override.

``content`` is the third payload form (alongside ``nodes`` and ``note_file``):
a raw string (write_file-parity) with a REQUIRED ``name`` -- normalized at the
edge by ``_node_payload_from_content`` into the standard single-node ETCH
payload (name = NAME verbatim, content = NOTE verbatim, newlines -> LF) so the
guarded capture funnel runs byte-for-byte unchanged. ``name`` is also an
OPTIONAL override with ``note_file`` (overrides the file stem; skips the
stem-derived tag-region check + AP-ID transform).

Locked here:
  * helper happy path (name + content -> payload + meta), CRLF/CR -> LF,
    content="" -> empty note, name None/whitespace -> NetworkError, name used
    VERBATIM (leading [ap:K7M2QF] passes through; trailing #tag passes through);
  * _node_payload_from_note_file name_override: override wins over stem,
    stem tag-region check SKIPPED, AP transform SKIPPED, meta name_override=True;
    no-override behavior unchanged (meta name_override=False); blank override
    -> NetworkError;
  * client three-way EXACTLY-ONE validation (none / each single / each pair /
    all three, all rejected BEFORE etch); nodes+name -> error; content without
    name -> error; content end-to-end (ETCH receives normalized payload, tags
    plain, _source.content echo); note_file+name end-to-end (override name,
    _source.note_file.name_override=True);
  * server wrapper: matrix validation BEFORE delegation (fake.calls == []),
    including nodes+name and content-without-name; verbatim forwarding of
    content/name; schema test (content + name present + optional, parent_uuid
    still required).

All local: no network, no real Workflowy. Client tests reuse the
test_add_to_anchor_note_file.py / test_add_to_anchor_tags.py stub pattern
(real client via __new__; etch / tag / guard collaborators stubbed).
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, Mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.client import api_client_nexus as nx  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.models import NetworkError  # noqa: E402
from coherence_harness import make_boundary_capable
from n05_region_harness import isolate_claim_scope
from workflowy_mcp.tool_path_resolution import resolve_tool_path

_PARENT = "aaaaaaaa-1111-2222-3333-444444444444"


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure helper tests: _node_payload_from_content
# ---------------------------------------------------------------------------

def test_content_helper_happy_path():
    payload, meta = nx._node_payload_from_content("My subject", "# Heading\n\nBody text")
    assert payload == [{"name": "My subject", "note": "# Heading\n\nBody text"}]
    assert meta == {"name": "My subject", "note_chars": len("# Heading\n\nBody text")}


def test_content_helper_crlf_and_cr_to_lf():
    payload, meta = nx._node_payload_from_content("Test", "line one\r\nline two\rline three\n")
    assert payload == [{"name": "Test", "note": "line one\nline two\nline three\n"}]
    assert meta["note_chars"] == len("line one\nline two\nline three\n")


def test_content_helper_empty_content_is_empty_note():
    payload, meta = nx._node_payload_from_content("Subject only", "")
    assert payload == [{"name": "Subject only", "note": ""}]
    assert meta["note_chars"] == 0


def test_content_helper_name_none_rejected():
    with pytest.raises(NetworkError, match="`name` is required"):
        nx._node_payload_from_content(None, "body")


def test_content_helper_name_whitespace_rejected():
    with pytest.raises(NetworkError, match="`name` is required"):
        nx._node_payload_from_content("   ", "body")


def test_content_helper_name_stripped():
    """Name is stripped of leading/trailing whitespace but otherwise verbatim."""
    payload, meta = nx._node_payload_from_content("  Spaced name  ", "body")
    assert payload[0]["name"] == "Spaced name"
    assert meta["name"] == "Spaced name"


def test_content_helper_ap_id_in_name_passes_through():
    """A leading [ap:K7M2QF] in name passes through unchanged -- no AP-ID
    transform (that is note_file-only; name is a directly-authored string)."""
    payload, meta = nx._node_payload_from_content("[ap:K7M2QF] subject", "body")
    assert payload[0]["name"] == "[ap:K7M2QF] subject"


def test_content_helper_trailing_tag_in_name_passes_through():
    """A trailing #tag in name is ALLOWED (directly-authored, not a filename)."""
    payload, meta = nx._node_payload_from_content("Meeting notes #comm-jim", "body")
    assert payload[0]["name"] == "Meeting notes #comm-jim"


# ---------------------------------------------------------------------------
# _node_payload_from_note_file name_override tests
# ---------------------------------------------------------------------------

def test_note_file_override_wins_over_stem(tmp_path):
    p = tmp_path / "filename stem.md"
    p.write_bytes(b"body")
    payload, meta = nx._node_payload_from_note_file(str(p), name_override="Override name")
    assert payload[0]["name"] == "Override name"
    assert payload[0]["note"] == "body"
    assert meta["node_name"] == "Override name"
    assert meta["ap_id_transformed"] is False
    assert meta["name_override"] is True


def test_note_file_override_skips_tag_region_check(tmp_path):
    """A file whose stem embeds a #tag succeeds when name_override is given
    (the stem tag-region check is SKIPPED -- the name comes from the caller)."""
    p = tmp_path / "Meeting notes #comm-jim.md"
    p.write_bytes(b"body")
    payload, meta = nx._node_payload_from_note_file(str(p), name_override="My meeting")
    assert payload[0]["name"] == "My meeting"


def test_note_file_override_skips_ap_transform(tmp_path):
    """The [ap_CORE] -> [ap:CORE] transform is SKIPPED when name_override is given."""
    p = tmp_path / "[AP_K7M2QF] subject.md"
    p.write_bytes(b"body")
    payload, meta = nx._node_payload_from_note_file(str(p), name_override="Custom name")
    assert payload[0]["name"] == "Custom name"
    assert meta["ap_id_transformed"] is False


def test_note_file_no_override_unchanged(tmp_path):
    """Without name_override, the behavior is byte-for-byte the prior behavior."""
    p = tmp_path / "My capture notes.md"
    p.write_bytes(b"body")
    payload, meta = nx._node_payload_from_note_file(str(p))
    assert payload == [{"name": "My capture notes", "note": "body"}]
    assert meta["node_name"] == "My capture notes"
    assert meta["name_override"] is False


def test_note_file_blank_override_rejected(tmp_path):
    p = tmp_path / "x.md"
    p.write_bytes(b"body")
    with pytest.raises(NetworkError, match="empty"):
        nx._node_payload_from_note_file(str(p), name_override="   ")


# ---------------------------------------------------------------------------
# Client-level tests: three-way validation + forwarding
# ---------------------------------------------------------------------------

def _make_client(monkeypatch, *, etch_capture, tag_calls):
    """Real client (via __new__) with etch/tag/guard collaborators stubbed."""
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)

    async def _fake_export_nodes(node_id=None, use_cache=True, **kw):
        return {"nodes": [{"id": _PARENT, "name": "Anchor", "note": "", "parent_id": None}]}

    c.export_nodes = _fake_export_nodes  # type: ignore[assignment]
    monkeypatch.setattr(
        nx,
        "resolve_uuid_or_anchor_point_handle",
        lambda handle, nodes_by_id: {"resolved_uuid": handle, "resolved_via": "uuid"},
    )
    monkeypatch.setattr(
        nx,
        "_classify_node_map_state",
        lambda uuid, nodes_by_id: {"state": "unmapped", "file_path": None},
    )
    monkeypatch.setattr(nx, "_anchor_target_guard_error", lambda verdict: None)

    async def _fake_etch(parent_id, nodes, nodes_file=None):
        etch_capture["nodes"] = nodes
        return {
            "success": True,
            "nodes_created": 1,
            "root_node_ids": ["root-1"],
            "errors": [],
        }

    c.workflowy_etch = _fake_etch  # type: ignore[assignment]

    async def _fake_add_tag(node_id, tag, max_retries=10):
        tags = tag if isinstance(tag, list) else [tag]
        tag_calls.append((node_id, list(tags)))
        return {
            "success": True,
            "tags_added": list(tags),
            "tags_already_present": [],
        }

    c.workflowy_add_tag = _fake_add_tag  # type: ignore[assignment]
    return c


def test_client_content_end_to_end(monkeypatch):
    etch_capture: dict = {}
    tag_calls: list = []
    c = _make_client(monkeypatch, etch_capture=etch_capture, tag_calls=tag_calls)

    result = _run(c.add_to_anchor(
        parent_uuid=_PARENT,
        name="My subject",
        content="# Heading\n\nBody text",
        tags=["build-v1"],
        auto_expand_postfix="expand",
        postfix_words=["expand", "suppress", "deprecated"],
    ))

    assert result["success"] is True
    # ETCH received the normalized single-node payload.
    assert etch_capture["nodes"] == [
        {"name": "My subject", "note": "# Heading\n\nBody text"}
    ]
    # Single bare leaf => tags applied PLAIN (no --expand postfix).
    assert tag_calls == [("root-1", ["build-v1"])]
    applied = {r["node_id"]: r for r in result["tags_applied"]}
    assert applied["root-1"]["expand_postfix_applied"] is False
    # Provenance echo.
    src = result["_source"]["content"]
    assert src["name"] == "My subject"
    assert src["note_chars"] == len("# Heading\n\nBody text")


def test_client_content_empty_string_is_legal(monkeypatch):
    """content='' is legal (presence is `is not None`); it becomes an empty note."""
    etch_capture: dict = {}
    c = _make_client(monkeypatch, etch_capture=etch_capture, tag_calls=[])
    result = _run(c.add_to_anchor(
        parent_uuid=_PARENT,
        name="Subject only",
        content="",
    ))
    assert result["success"] is True
    assert etch_capture["nodes"] == [{"name": "Subject only", "note": ""}]
    assert result["_source"]["content"]["note_chars"] == 0


def test_client_note_file_with_name_override_end_to_end(tmp_path, monkeypatch):
    p = tmp_path / "stem name.md"
    p.write_bytes(b"notes body")
    etch_capture: dict = {}
    c = _make_client(monkeypatch, etch_capture=etch_capture, tag_calls=[])

    result = _run(c.add_to_anchor(
        parent_uuid=_PARENT,
        note_file=str(p),
        name="Override name",
    ))

    assert result["success"] is True
    # ETCH received the OVERRIDE name, not the stem.
    assert etch_capture["nodes"] == [{"name": "Override name", "note": "notes body"}]
    src = result["_source"]["note_file"]
    assert src["node_name"] == "Override name"
    assert src["name_override"] is True


# --- EXACTLY-ONE validation across all combos ---

@pytest.mark.parametrize(
    "kwargs",
    [
        {"nodes": [{"name": "n"}], "note_file": "E:\\x.md"},        # nodes + note_file
        {"nodes": [{"name": "n"}], "content": "body"},               # nodes + content
        {"note_file": "E:\\x.md", "content": "body"},               # note_file + content
        {"nodes": [{"name": "n"}], "note_file": "E:\\x.md", "content": "body"},  # all three
        {},  # neither
    ],
)
def test_client_exactly_one_rejection(monkeypatch, kwargs):
    etch_capture: dict = {}
    c = _make_client(monkeypatch, etch_capture=etch_capture, tag_calls=[])
    with pytest.raises(NetworkError, match="EXACTLY ONE"):
        _run(c.add_to_anchor(parent_uuid=_PARENT, **kwargs))
    assert etch_capture == {}  # rejected BEFORE any etch


def test_client_nodes_with_name_rejected(monkeypatch):
    etch_capture: dict = {}
    c = _make_client(monkeypatch, etch_capture=etch_capture, tag_calls=[])
    with pytest.raises(NetworkError, match="`name` is only valid"):
        _run(c.add_to_anchor(
            parent_uuid=_PARENT,
            nodes=[{"name": "n"}],
            name="Extra name",
        ))
    assert etch_capture == {}


def test_client_content_without_name_rejected(monkeypatch):
    etch_capture: dict = {}
    c = _make_client(monkeypatch, etch_capture=etch_capture, tag_calls=[])
    with pytest.raises(NetworkError, match="`content` requires `name`"):
        _run(c.add_to_anchor(
            parent_uuid=_PARENT,
            content="body",
        ))
    assert etch_capture == {}


def test_client_empty_list_nodes_counts_as_supplied(monkeypatch):
    """nodes=[] alongside content is a BOTH error (presence is `is not None`)."""
    etch_capture: dict = {}
    c = _make_client(monkeypatch, etch_capture=etch_capture, tag_calls=[])
    with pytest.raises(NetworkError, match="EXACTLY ONE"):
        _run(c.add_to_anchor(
            parent_uuid=_PARENT,
            nodes=[],
            name="x",
            content="body",
        ))
    assert etch_capture == {}


# ---------------------------------------------------------------------------
# Server-wrapper tests: early validation + verbatim forwarding + schema.
# ---------------------------------------------------------------------------

class _RecordingAnchorClient:
    def __init__(self):
        self.calls = []

    async def add_to_anchor(self, **kwargs):
        self.calls.append(kwargs)
        return {"success": True}


def _server_tool_fn():
    """The callable behind the @mcp.tool registration."""
    tool = server.add_to_anchor
    fn = getattr(tool, "fn", None)
    return fn if callable(fn) else tool


def test_server_wrapper_forwards_content_and_name_verbatim(monkeypatch, tmp_path):
    # [ap:MJ8II8]: freeze the same bytes/title before deriving the held cores.
    fake = make_boundary_capable(_RecordingAnchorClient(), nodes=[{"id": _PARENT, "parent_id": None}])
    isolate_claim_scope(fake, tmp_path)
    monkeypatch.setattr(server, "get_client", lambda: fake)
    monkeypatch.setattr(server, "_resolve_generic_node_handle", AsyncMock(return_value=_PARENT))
    res = _run(_server_tool_fn()(
        parent_uuid=_PARENT,
        name="My subject",
        content="# Heading\n\nBody",
    ))
    assert res == {"success": True, "_source": {"content": {
        "name": "My subject", "note_chars": len("# Heading\n\nBody"),
    }}}
    assert len(fake.calls) == 1
    kw = fake.calls[0]
    assert kw["nodes"] == [{"name": "My subject", "note": "# Heading\n\nBody"}]
    assert kw["parent_uuid"] == _PARENT
    assert not {"content", "name", "note_file"}.intersection(kw)


def test_server_wrapper_forwards_note_file_with_name_override(monkeypatch, tmp_path):
    # [ap:MJ8II8]: title override and provenance survive the moved read seam.
    path = tmp_path / "note.md"
    path.write_text("# Heading\n\nBody", encoding="utf-8")
    fake = make_boundary_capable(_RecordingAnchorClient(), nodes=[{"id": _PARENT, "parent_id": None}])
    isolate_claim_scope(fake, tmp_path)
    monkeypatch.setattr(server, "get_client", lambda: fake)
    monkeypatch.setattr(server, "_resolve_generic_node_handle", AsyncMock(return_value=_PARENT))
    reader = Mock(wraps=nx._read_note_file_text)
    monkeypatch.setattr(nx, "_read_note_file_text", reader)
    res = _run(_server_tool_fn()(
        parent_uuid=_PARENT,
        note_file=str(path),
        name="Override",
    ))
    assert res == {"success": True, "_source": {"note_file": {
        "path": str(path), "node_name": "Override", "note_chars": len("# Heading\n\nBody"),
        "ap_id_transformed": False, "name_override": True,
    }}, "path_record": resolve_tool_path(str(path), param="note_file", context="add_to_anchor", direction="read").as_dict()}
    assert len(fake.calls) == 1
    kw = fake.calls[0]
    assert kw["nodes"] == [{"name": "Override", "note": "# Heading\n\nBody"}]
    assert kw["parent_uuid"] == _PARENT
    assert not {"note_file", "name", "content"}.intersection(kw)
    reader.assert_called_once_with(str(path), context="add_to_anchor")


def test_server_wrapper_rejects_all_combos_before_delegation(monkeypatch):
    fake = _RecordingAnchorClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)

    # Neither
    r = _run(_server_tool_fn()(parent_uuid=_PARENT))
    assert r["success"] is False and "EXACTLY ONE" in r["error"]

    # nodes + content (pair)
    r = _run(_server_tool_fn()(
        parent_uuid=_PARENT, nodes=[{"name": "n"}], content="body", name="x"
    ))
    assert r["success"] is False and "EXACTLY ONE" in r["error"]

    # nodes + name (name not valid with nodes)
    r = _run(_server_tool_fn()(
        parent_uuid=_PARENT, nodes=[{"name": "n"}], name="x"
    ))
    assert r["success"] is False and "ambiguous" in r["error"]

    # content without name
    r = _run(_server_tool_fn()(
        parent_uuid=_PARENT, content="body"
    ))
    assert r["success"] is False and "requires `name`" in r["error"]

    # Rejected BEFORE delegation (and before any live config read).
    assert fake.calls == []


def test_server_tool_schema_exposes_content_and_name():
    """Schema-level lock: content and name are exposed and OPTIONAL;
    parent_uuid is still required."""
    params = getattr(server.add_to_anchor, "parameters", None)
    if params is None:
        pytest.skip("FastMCP tool object does not expose a schema dict here")
    props = params.get("properties", {})
    assert "content" in props
    assert "name" in props
    assert "note_file" in props
    assert "nodes" in props
    required = params.get("required", [])
    assert "content" not in required
    assert "name" not in required
    assert "note_file" not in required
    assert "nodes" not in required
    assert "parent_uuid" in required
