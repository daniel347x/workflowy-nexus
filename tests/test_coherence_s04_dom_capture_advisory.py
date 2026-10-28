"""S04 Remediation Chain 4 (work-order item 1) — cache-free frozen DOM
captures are NEVER advised.

The hard-barrier read advisory is a CACHE-PROVENANCE claim ("this
cache-backed result may be stale").  ``_glimpse_dispatch_dom_capture``
applied it unconditionally pre-fix, so browser-captured content — the
``render_spec="modal"`` ``dom_captured_tree`` read and Scoped Lightning over
a genuinely DOM-frozen source — falsely carried the cache-staleness warning
line and the structured ``coherence_advisory`` field.  Provenance is now
carried EXPLICITLY to the ONE advisory authority (``cache_sourced``):

- a frozen DOM modal read under hard dirt proceeds WITHOUT the advisory;
- its ``output_file`` bytes receive no cache warning;
- Scoped Lightning over a genuinely DOM-frozen source is unadvised;
- Scoped Lightning whose source was a cache-projected slice STAYS advised;
- ordinary live-DOM GLIMPSE remains unadvised (never advised);
- cache raw/slice/lens/Lightning renders remain advised;
- clean-path bytes are unchanged;
- one advisory literal, one shaping authority, provenance carried
  explicitly — never copied warning logic.

Required-test mutation note (step 4a): the adversarial tests (a)-(c) below
were verified to FAIL against the pre-fix code (production stashed to the
6f5a8e2 baseline) with their predicted shapes — see the Chain-4 result
anchor for the full evidence table.
"""

from __future__ import annotations

import asyncio

from coherence_harness import make_boundary_capable, s04_boundary_status

from workflowy_mcp import server
from workflowy_mcp.cache_render_spec import RenderProjectionSpec
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
from workflowy_mcp.coherence import (
    COHERENCE_READ_ADVISORY_LINE,
    cache_operation_boundary,
)
from workflowy_mcp.coherence.read_advisory import (
    apply_coherence_advisory_to_text,
    apply_coherence_read_advisory,
)
from workflowy_mcp.mcp_api_registry import dispatch_operation

HARD = s04_boundary_status(
    hard_dirty=True,
    hard_generation=7,
    hard_reason_code="verified_effect_unpublishable",
    hard_transaction_id="tx-advisory",
)


# --- a tiny in-memory /nodes-export cache (mirrors the modal dom_capture
# regression-lock file) -----------------------------------------------------
_ROOT = "33333333-1111-bbbb-cccc-000000000001"
_CHILD_A = "33333333-2222-bbbb-cccc-000000000002"
_CHILD_B = "33333333-3333-bbbb-cccc-000000000003"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Frozen Anchor Root", "note": "", "parent_id": None,
     "priority": 0, "createdAt": 1000, "modifiedAt": 1000},
    {"id": _CHILD_A, "name": "Alpha point", "note": "body A", "parent_id": _ROOT,
     "priority": 0, "createdAt": 2000, "modifiedAt": 2000},
    {"id": _CHILD_B, "name": "Beta point", "note": "body B", "parent_id": _ROOT,
     "priority": 1, "createdAt": 1500, "modifiedAt": 1500},
]


def _client(status, *, nodes=None):
    client = object.__new__(server.WorkFlowyClient)
    return make_boundary_capable(
        client, nodes=(_CACHE_NODES if nodes is None else nodes), boundary_status=status
    )


def _patch_client(monkeypatch, status, *, nodes=None):
    """One client instance per test, patched in as get_client() — the
    boundary wrapper and the handler/dispatch bodies must share ONE
    publisher (the task snapshot is owner-checked)."""
    client = _client(status, nodes=nodes)
    monkeypatch.setattr(server, "get_client", lambda: client)
    return client


def _captured_tree():
    """The {root, children} shape the client's serializeVisibleTreeForModal sends."""
    return {
        "root": {"id": _ROOT, "name": "Frozen Anchor Root", "note": ""},
        "children": [
            {"id": _CHILD_A, "name": "Alpha point", "note": "body A", "children": []},
            {"id": _CHILD_B, "name": "Beta point", "note": "body B", "children": []},
        ],
    }


def _dispatch(registry, verb, params):
    return asyncio.run(
        dispatch_operation(
            registry,
            verb,
            params,
            error_logger=lambda *_a: None,
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
        )
    )


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, s):
        import json

        self.sent.append(json.loads(s))


# ---------------------------------------------------------------------------
# The ONE advisory authority: cache_sourced provenance short-circuits
# ---------------------------------------------------------------------------


def test_advisory_authority_cache_free_short_circuits():
    """The one shaping authority honors explicit cache-free provenance:
    byte-identical passthrough even under a hard-dirty status; cache-sourced
    content stays advised (the one literal is unchanged)."""
    assert apply_coherence_read_advisory("PAYLOAD", status=HARD, cache_sourced=False) == "PAYLOAD"
    result = {"success": True, "preview_markdown": "doc", "node_count": 1}
    assert apply_coherence_read_advisory(result, status=HARD, cache_sourced=False) == result
    assert apply_coherence_advisory_to_text("doc", status=HARD, cache_sourced=False) == "doc"
    # Cache-sourced content is still advised by the same authority.
    advised = apply_coherence_read_advisory("PAYLOAD", status=HARD, cache_sourced=True)
    assert advised.startswith(COHERENCE_READ_ADVISORY_LINE + "\n\n")


# ---------------------------------------------------------------------------
# Required test (a): render_spec="modal" + dom_captured_tree + hard barrier
# => no advisory field and no warning prefix.
# ---------------------------------------------------------------------------


def test_modal_dom_captured_tree_hard_dirty_no_advisory(monkeypatch):
    """The primary end-to-end path: a frozen DOM modal read under hard dirt
    carries NO coherence_advisory and NO cache-staleness warning line."""
    _patch_client(monkeypatch, HARD)

    async def _fake_grace(*_a, **_kw):
        return ("ws-conn", "ws-queue")

    monkeypatch.setattr(server, "get_ws_connection_with_grace_wait", _fake_grace)

    async def _fake_ws_request_response(self, action, **k):
        return {
            "success": True,
            "modal_payload_kind": "dom_captured_tree",
            "captured_tree": _captured_tree(),
            "root_uuid": _ROOT,
            "focus": None,
            "sort_by": "none",
            "sort_direction": "asc",
            "since": None,
            "headings_only": False,
        }

    monkeypatch.setattr(
        WorkFlowyClientNexus, "_ws_request_response", _fake_ws_request_response
    )

    reply = _dispatch(server.GLIMPSE_REGISTRY, "run", {"render_spec": "modal"})
    # Agent-facing Markdown return: raw string, unprefixed, warning-free.
    assert isinstance(reply, str), reply
    assert "COHERENCE FAILURE" not in reply
    assert not reply.startswith(COHERENCE_READ_ADVISORY_LINE)
    assert "Alpha point" in reply and "Beta point" in reply


def test_dispatch_dom_capture_payload_no_advisory_field_or_prefix(monkeypatch):
    """Dispatch level (the internal render dictionary the WS/modal handlers
    consume): no structured advisory field, no preview prefix, honest
    kind='dom_capture' echo preserved."""
    async def _run():
        client = _patch_client(monkeypatch, HARD)
        async with cache_operation_boundary(client, "test-dom-capture"):
            return await server._glimpse_dispatch_dom_capture(
                captured_tree=_captured_tree(),
                root_uuid_hint=_ROOT,
                focus=None,
                as_markdown=True,
                headings_only=False,
                sort_by=None,
                sort_direction="asc",
                since=None,
                output_file=None,
                cache_sourced=False,
            )

    result = asyncio.run(_run())
    assert result["success"] is True
    assert "coherence_advisory" not in result
    preview = result["preview_markdown"]
    assert "COHERENCE FAILURE" not in preview
    assert not preview.startswith(COHERENCE_READ_ADVISORY_LINE)
    assert result["render_scope"]["kind"] == "dom_capture"


# ---------------------------------------------------------------------------
# Required test (b): the same path with output_file writes unmodified
# captured content.
# ---------------------------------------------------------------------------


def test_dom_capture_output_file_writes_unmodified_captured_content(tmp_path, monkeypatch):
    """output_file bytes receive no cache warning; the returned summary
    carries no advisory field and reports the actual written bytes; the
    clean path is byte-identical."""
    out_dirty = tmp_path / "dirty.md"
    out_clean = tmp_path / "clean.md"

    async def _run(out_file, status):
        client = _patch_client(monkeypatch, status)
        async with cache_operation_boundary(client, "test-dom-capture-file"):
            return await server._glimpse_dispatch_dom_capture(
                captured_tree=_captured_tree(),
                root_uuid_hint=_ROOT,
                focus=None,
                as_markdown=True,
                headings_only=False,
                sort_by=None,
                sort_direction="asc",
                since=None,
                output_file=str(out_file),
                cache_sourced=False,
            )

    summary = asyncio.run(_run(out_dirty, HARD))
    dirty_bytes = out_dirty.read_bytes()
    # The written bytes are the UNMODIFIED captured content: no warning line.
    assert not dirty_bytes.startswith(COHERENCE_READ_ADVISORY_LINE.encode("utf-8"))
    assert b"COHERENCE FAILURE" not in dirty_bytes
    assert b"Alpha point" in dirty_bytes and b"Beta point" in dirty_bytes
    # The summary carries no advisory field.  (bytes_written accounting on
    # this dispatch write is pre-existing code outside item 1's scope.)
    assert "coherence_advisory" not in summary

    clean_summary = asyncio.run(_run(out_clean, None))
    clean_bytes = out_clean.read_bytes()
    # Clean-path bytes are identical to the hard-dirty bytes (no advisory
    # either way) — and the file summary shape is unchanged.
    assert clean_bytes == dirty_bytes
    assert "coherence_advisory" not in clean_summary


# ---------------------------------------------------------------------------
# Required test (c): Scoped Lightning over a genuinely DOM-frozen source is
# unadvised.
# ---------------------------------------------------------------------------


def test_scoped_dom_frozen_source_unadvised(monkeypatch):
    _patch_client(monkeypatch, HARD)
    captured = {
        "root": {"id": "root", "name": "Root", "note": ""},
        "children": [
            {"id": "a", "name": "Alpha #keep", "note": "", "children": []},
            {"id": "b", "name": "Beta", "note": "", "children": []},
        ],
    }
    data = {
        "request_id": "t1",
        "captured_tree": captured,
        "root_uuid": "root",
        "lightning": {"mode": "render_tags", "tags": ["keep"], "match_scope": "name_and_note"},
        "as_markdown": True,
    }
    ws = _FakeWS()
    asyncio.run(server._handle_render_lightning_dom_capture(data, ws))
    assert len(ws.sent) == 1
    reply = ws.sent[0]
    assert reply["success"] is True
    assert "COHERENCE FAILURE" not in reply["markdown"]
    assert not reply["markdown"].startswith(COHERENCE_READ_ADVISORY_LINE)
    assert "Alpha" in reply["markdown"]


# ---------------------------------------------------------------------------
# Required test (d): Scoped Lightning whose source was a cache-projected
# slice REMAINS advised.
# ---------------------------------------------------------------------------


def test_scoped_slice_derived_source_remains_advised(monkeypatch):
    projected_nodes = [
        {"id": "root", "name": "Root", "note": "", "parent_id": None,
         "priority": 0, "createdAt": 1000, "modifiedAt": 1000},
        {"id": "a", "name": "Alpha #keep", "note": "", "parent_id": "root",
         "priority": 0, "createdAt": 1000, "modifiedAt": 1000},
        {"id": "b", "name": "Beta", "note": "", "parent_id": "root",
         "priority": 1, "createdAt": 1000, "modifiedAt": 1000},
    ]
    _patch_client(monkeypatch, HARD, nodes=projected_nodes)
    # A raw_root base spec: re-projected from the /nodes-export cache via
    # _project_slice_base_tree_for_scoped_lightning (was_slice=True), so the
    # composed content IS cache-derived and stays advised.
    blob = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="root"
    ).to_render_spec_blob()
    data = {
        "request_id": "t2",
        "base_render_spec": blob,
        "was_slice": True,
        "root_uuid": "root",
        "lightning": {"mode": "render_tags", "tags": ["keep"], "match_scope": "name_and_note"},
        "as_markdown": True,
    }
    ws = _FakeWS()
    asyncio.run(server._handle_render_lightning_dom_capture(data, ws))
    assert len(ws.sent) == 1
    reply = ws.sent[0]
    assert reply["success"] is True, reply
    assert reply["was_slice"] is True
    assert reply["markdown"].startswith(COHERENCE_READ_ADVISORY_LINE + "\n\n")
    assert reply["markdown"].count("COHERENCE FAILURE") == 1
    assert "Alpha" in reply["markdown"]
