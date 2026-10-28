"""Regression lock: add_to_anchor's ``note_file=`` single-node capture path.

``note_file`` is the alternative payload form to ``nodes`` (EXACTLY ONE
required, loud on both/neither): an absolute-path UTF-8 text file becomes ONE
node -- file STEM (no extension) = node NAME, raw text (newlines -> LF) = node
NOTE -- normalized at the edge by ``_node_payload_from_note_file`` so the
guarded capture funnel (resolver -> guard -> parse-peek -> etch -> tags) runs
byte-for-byte unchanged.

Locked here:
  * happy-path normalization (stem name, verbatim note, CRLF/CR -> LF, meta);
  * loud rejection of empty / relative / missing / directory / non-UTF-8 paths;
  * loud rejection of a filename-embedded trailing #tag region (note_file tags
    MUST go through tags= -- enforced, not advisory);
  * the AP-ID filename convention: a leading ``[ap_CORE]`` stem handle is
    canonicalized to ``[ap:CORE]`` (lowercase 'ap' -- REQUIRED by
    _AP_ID_LEADING_RE; underscore because ':' is illegal in Windows filenames);
  * client + server EXACTLY-ONE validation (both / neither / nodes=[] counts
    as supplied) and verbatim forwarding;
  * a note_file capture tags as a bare LEAF (plain tags, NO --expand);
  * the ``_source.note_file`` provenance echo.

All local: tmp_path files, no network, no real Workflowy. Client tests reuse
the tests/test_add_to_anchor_tags.py stub pattern (real client via __new__;
etch / tag / guard collaborators stubbed) so the REAL add_to_anchor body --
including the note_file normalization -- runs unmodified.
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
# Pure helper tests: _node_payload_from_note_file
# ---------------------------------------------------------------------------

def test_helper_happy_path_stem_note_newlines_and_meta(tmp_path):
    p = tmp_path / "My capture notes.md"
    # Exact bytes (CRLF, lone CR, LF) so the universal-newlines read is proven.
    p.write_bytes(b"line one\r\nline two\rline three\n")
    payload, meta = nx._node_payload_from_note_file(str(p))
    assert payload == [
        {"name": "My capture notes", "note": "line one\nline two\nline three\n"}
    ]
    assert meta["path"] == str(p)
    assert meta["node_name"] == "My capture notes"
    assert meta["note_chars"] == len("line one\nline two\nline three\n")
    assert meta["ap_id_transformed"] is False


def test_helper_empty_file_is_empty_note(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_bytes(b"")
    payload, meta = nx._node_payload_from_note_file(str(p))
    assert payload == [{"name": "empty", "note": ""}]
    assert meta["note_chars"] == 0


def test_helper_rejects_empty_path():
    with pytest.raises(NetworkError, match="empty path"):
        nx._node_payload_from_note_file("   ")


def test_helper_rejects_relative_path():
    with pytest.raises(NetworkError, match="ABSOLUTE"):
        nx._node_payload_from_note_file("notes/today.md")


def test_helper_rejects_missing_file(tmp_path):
    missing = tmp_path / "nope.md"
    with pytest.raises(NetworkError, match="not an existing file"):
        nx._node_payload_from_note_file(str(missing))


def test_helper_rejects_directory(tmp_path):
    with pytest.raises(NetworkError, match="not an existing file"):
        nx._node_payload_from_note_file(str(tmp_path))


def test_helper_rejects_non_utf8(tmp_path):
    p = tmp_path / "binary.bin"
    p.write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    with pytest.raises(NetworkError, match="UTF-8"):
        nx._node_payload_from_note_file(str(p))


def test_helper_rejects_filename_embedded_tags(tmp_path):
    """A trailing #tag region in the STEM is rejected: note_file tags must go
    through tags= -- enforced via the canonical split_name_and_tags, so the
    'you do not have the option' contract is true by construction."""
    p = tmp_path / "Meeting notes #comm-jim.md"
    p.write_bytes(b"body")
    with pytest.raises(NetworkError, match="tags="):
        nx._node_payload_from_note_file(str(p))


def test_helper_mid_name_hash_is_not_a_tag(tmp_path):
    """A '#' that is NOT in the trailing tag region (e.g. 'C# notes') is fine."""
    p = tmp_path / "C# notes.md"
    p.write_bytes(b"body")
    payload, _meta = nx._node_payload_from_note_file(str(p))
    assert payload[0]["name"] == "C# notes"


@pytest.mark.parametrize(
    "filename, expected_name, transformed",
    [
        ("[AP_K7M2QF] subject.md", "[ap:K7M2QF] subject", True),
        ("[ap_abc123] lower.md", "[ap:ABC123] lower", True),
        ("[AP_APRSLV3] mnemonic.md", "[ap:APRSLV3] mnemonic", True),
        ("[AP_K7M2QF].md", "[ap:K7M2QF]", True),          # handle at end-of-stem
        ("[AP_AB] too short.md", "[AP_AB] too short", False),      # core < 3
        ("see [AP_K7M2QF] mid.md", "see [AP_K7M2QF] mid", False),  # not leading
        ("[AP_K7M2QF]no space.md", "[AP_K7M2QF]no space", False),  # no space/end
    ],
)
def test_helper_ap_id_filename_convention(tmp_path, filename, expected_name, transformed):
    """The '[ap_CORE]' filename form canonicalizes to '[ap:CORE]' (lowercase
    ap -- REQUIRED, _AP_ID_LEADING_RE is case-sensitive on the literal prefix;
    core uppercased to canonical form). Non-matching stems pass through as
    ordinary filenames, never an error."""
    p = tmp_path / filename
    p.write_bytes(b"body")
    payload, meta = nx._node_payload_from_note_file(str(p))
    assert payload[0]["name"] == expected_name
    assert payload[0]["note"] == "body"
    assert meta["ap_id_transformed"] is transformed


# ---------------------------------------------------------------------------
# Client-level tests: real WorkFlowyClientNexus.add_to_anchor body, stubbed
# collaborators (mirrors tests/test_add_to_anchor_tags.py).
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


def test_client_note_file_end_to_end_leaf_tags_and_source_echo(tmp_path, monkeypatch):
    p = tmp_path / "[AP_K7M2QF] weekly sync.md"
    p.write_bytes(b"notes body\r\nsecond line")
    etch_capture: dict = {}
    tag_calls: list = []
    c = _make_client(monkeypatch, etch_capture=etch_capture, tag_calls=tag_calls)

    result = _run(c.add_to_anchor(
        parent_uuid=_PARENT,
        note_file=str(p),
        tags=["build-v1"],
        auto_expand_postfix="expand",
        postfix_words=["expand", "suppress", "deprecated"],
    ))

    assert result["success"] is True
    # ETCH received the NORMALIZED single-node payload (stem name, LF note).
    assert etch_capture["nodes"] == [
        {"name": "[ap:K7M2QF] weekly sync", "note": "notes body\nsecond line"}
    ]
    # Single node => bare LEAF => tag applied PLAIN, no --expand postfix.
    assert tag_calls == [("root-1", ["build-v1"])]
    applied = {r["node_id"]: r for r in result["tags_applied"]}
    assert applied["root-1"]["expand_postfix_applied"] is False
    # Provenance echo under the established _source namespace.
    src = result["_source"]["note_file"]
    assert src["path"] == str(p)
    assert src["node_name"] == "[ap:K7M2QF] weekly sync"
    assert src["ap_id_transformed"] is True


def test_client_both_nodes_and_note_file_rejected_before_etch(tmp_path, monkeypatch):
    p = tmp_path / "x.md"
    p.write_bytes(b"body")
    etch_capture: dict = {}
    c = _make_client(monkeypatch, etch_capture=etch_capture, tag_calls=[])
    with pytest.raises(NetworkError, match="EXACTLY ONE"):
        _run(c.add_to_anchor(
            parent_uuid=_PARENT,
            nodes=[{"name": "n"}],
            note_file=str(p),
        ))
    assert etch_capture == {}  # rejected BEFORE any etch


def test_client_neither_nodes_nor_note_file_rejected(monkeypatch):
    etch_capture: dict = {}
    c = _make_client(monkeypatch, etch_capture=etch_capture, tag_calls=[])
    with pytest.raises(NetworkError, match="EXACTLY ONE"):
        _run(c.add_to_anchor(parent_uuid=_PARENT))
    assert etch_capture == {}


def test_client_empty_list_nodes_counts_as_supplied(tmp_path, monkeypatch):
    """nodes=[] is an EXPLICIT payload: alongside note_file it is a BOTH
    error, not a 'no nodes' pass-through (presence is `is not None`)."""
    p = tmp_path / "x.md"
    p.write_bytes(b"body")
    etch_capture: dict = {}
    c = _make_client(monkeypatch, etch_capture=etch_capture, tag_calls=[])
    with pytest.raises(NetworkError, match="EXACTLY ONE"):
        _run(c.add_to_anchor(parent_uuid=_PARENT, nodes=[], note_file=str(p)))
    assert etch_capture == {}


# ---------------------------------------------------------------------------
# Server-wrapper tests: early EXACTLY-ONE validation + verbatim forwarding.
# ---------------------------------------------------------------------------

class _RecordingAnchorClient:
    def __init__(self):
        self.calls = []

    async def add_to_anchor(self, **kwargs):
        self.calls.append(kwargs)
        return {"success": True}


def _server_tool_fn():
    """The callable behind the @mcp.tool registration, across FastMCP shapes:
    a FunctionTool exposes .fn; a plain function is used directly."""
    tool = server.add_to_anchor
    fn = getattr(tool, "fn", None)
    return fn if callable(fn) else tool


def test_server_wrapper_forwards_note_file_verbatim(monkeypatch, tmp_path):
    # [ap:MJ8II8]: the exact frozen payload replaces the raw-file snapshot.
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
    ))
    assert res == {"success": True, "_source": {"note_file": {
        "path": str(path), "node_name": "note", "note_chars": len("# Heading\n\nBody"),
        "ap_id_transformed": False, "name_override": False,
    }}, "path_record": resolve_tool_path(str(path), param="note_file", context="add_to_anchor", direction="read").as_dict()}
    assert len(fake.calls) == 1
    kw = fake.calls[0]
    assert kw["nodes"] == [{"name": "note", "note": "# Heading\n\nBody"}]
    assert not {"note_file", "name", "content"}.intersection(kw)
    assert kw["parent_uuid"] == _PARENT
    reader.assert_called_once_with(str(path), context="add_to_anchor")


def test_server_wrapper_rejects_both_and_neither_before_delegation(monkeypatch):
    fake = _RecordingAnchorClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    both = _run(_server_tool_fn()(
        parent_uuid=_PARENT, nodes=[{"name": "n"}], note_file="E:\\x.md"
    ))
    assert both["success"] is False and "EXACTLY ONE" in both["error"]
    neither = _run(_server_tool_fn()(parent_uuid=_PARENT))
    assert neither["success"] is False and "EXACTLY ONE" in neither["error"]
    # Rejected BEFORE delegation (and before any live postfix-config read).
    assert fake.calls == []


def test_server_tool_schema_exposes_optional_note_file():
    """Schema-level lock: note_file is exposed and OPTIONAL; nodes is no longer
    required (FastMCP FunctionTool exposes .parameters as a JSON-schema dict)."""
    params = getattr(server.add_to_anchor, "parameters", None)
    if params is None:
        pytest.skip("FastMCP tool object does not expose a schema dict here")
    props = params.get("properties", {})
    assert "note_file" in props
    assert "nodes" in props
    required = params.get("required", [])
    assert "note_file" not in required
    assert "nodes" not in required
    assert "parent_uuid" in required


# ---------------------------------------------------------------------------
# _looks_absolute_path hardening (the shared predicate the note_file path gate
# uses): native-isabs-first so POSIX absolute paths are recognized on POSIX,
# with Windows verdicts byte-identical to before.
# ---------------------------------------------------------------------------

def test_looks_absolute_path_windows_forms_unchanged():
    """Windows regression: the native-isabs-first hardening must not change any
    Windows verdict (drive-letter and UNC absolute; relative / drive-relative not)."""
    assert nx._looks_absolute_path("E:\\a\\b.md") is True
    assert nx._looks_absolute_path("E:/a/b.md") is True
    assert nx._looks_absolute_path("\\\\server\\share\\b.md") is True
    assert nx._looks_absolute_path("relative\\b.md") is False
    assert nx._looks_absolute_path("C:drive-relative.md") is False


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX absolute semantics exist only off Windows (ntpath.isabs('/x') is False).",
)
def test_looks_absolute_path_posix_absolute():
    assert nx._looks_absolute_path("/home/user/b.md") is True


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX absolute semantics exist only off Windows.",
)
def test_helper_accepts_posix_absolute_path(tmp_path):
    p = tmp_path / "posix style.md"
    p.write_bytes(b"body")
    payload, _meta = nx._node_payload_from_note_file(str(p))
    assert payload[0]["name"] == "posix style"
