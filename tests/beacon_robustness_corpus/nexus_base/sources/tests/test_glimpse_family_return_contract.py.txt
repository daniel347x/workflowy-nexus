"""Regression lock: the GLIMPSE-FAMILY agent-facing return contract.

The output-shape cleanup (Dan): every GLIMPSE-family MCP tool -- ``glimpse``
(workflowy_glimpse) and ``lightning_rod`` -- delivers to the AGENT exactly what
the human sees, and nothing else, via the ONE shared finalizer
``server._shape_glimpse_family_return``:

  * MARKDOWN mode  -> the RAW Markdown STRING (real newlines, like
    read_text_file). NOT a dict, NO ``preview_markdown`` field, and -- the bug
    that started this -- NO ``rows`` object doubling the payload.
  * GATHER (Lightning Rod) -> the RAW tag-outline STRING (its ``preview_text``,
    byte-identical to the modal's Gather). Markdown is just raw text, so gather
    lumps in cleanly; ANY future raw-text mode that fills ``preview_text`` (or
    ``preview_markdown``) is delivered raw with ZERO further change.
  * FULL / UUID mode -> the structured dict with ``preview_tree`` plus useful
    metadata (node_count, depth, _source, render_scope, ...), but no ``rows``;
    in ``headings_only`` UUID-harvest mode, bulky live-DOM ``root``/``children``
    are also stripped.
  * output_file / errors / push acks -> the structured dict, UNCHANGED (so a
    file write still reports its path, and a failure still signals success:
    False).

These tests exercise the REAL async tools end-to-end against a tiny in-memory
/nodes-export cache (the self-contained bare-root RAW-DUMP + Lightning Rod
cells, which need no live DOM -- the Anchor-Dock guard's tab-agnostic config
read is faked by _patch_common), plus direct unit coverage of the finalizer.

All local: no network, no real Workflowy, no disk writes.
"""

import asyncio
import os
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.mcp_api_registry import dispatch_operation  # noqa: E402


_ROOT = "44444444-1111-bbbb-cccc-000000000001"
_CHILD_A = "44444444-2222-bbbb-cccc-000000000002"
_CHILD_B = "44444444-3333-bbbb-cccc-000000000003"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Contract Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _CHILD_A, "name": "Alpha point #mytag", "note": "body A", "parent_id": _ROOT, "priority": 0},
    {"id": _CHILD_B, "name": "Beta point", "note": "body B", "parent_id": _ROOT, "priority": 1},
]


def _make_client():
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]
    return c


def _patch_common(monkeypatch):
    monkeypatch.setattr(srv, "get_client", _make_client)
    # The bare-root raw-dump cell's Anchor-Dock guard reads the live Dock-tag
    # vocabulary over a tab-agnostic config read. Fake it here: the contract
    # nodes carry no Dock tag, so the guard passes silently and these tests
    # exercise exactly the render contract they always did.
    async def _fake_note_session_config(target_tab=None):
        return (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})

    monkeypatch.setattr(
        srv, "_resolve_note_session_config", _fake_note_session_config
    )


# ---------------------------------------------------------------------------
# The finalizer, in isolation (the ONE seam).
# ---------------------------------------------------------------------------
def test_finalizer_markdown_returns_raw_string():
    out = srv._shape_glimpse_family_return(
        {"success": True, "node_count": 3, "preview_markdown": "# Hi\n\nbody", "rows": [1, 2, 3]},
        output_file=None,
    )
    assert out == "# Hi\n\nbody"  # raw string; rows/node_count dropped


def test_finalizer_preview_text_returns_raw_string():
    out = srv._shape_glimpse_family_return(
        {"success": True, "tags": ["a", "b"], "preview_text": "#a\n#b", "rows": []},
        output_file=None,
    )
    assert out == "#a\n#b"


def test_finalizer_full_mode_keeps_tree_and_metadata_drops_rows():
    tree = ["[WG1] Root", "  [WG2] Child"]
    out = srv._shape_glimpse_family_return(
        {
            "success": True, "_source": "cache_root_uuid",
            "node_count": 2, "depth": 2,
            "render_scope": {"kind": "raw_root"},
            "preview_tree": tree, "rows": [1],
            # Non-headings-only full mode keeps richer raw tree payloads when a
            # caller has them (live-DOM GLIMPSE), because that mode is asking
            # for more structure/metadata.
            "root": {"id": "r"}, "children": [{"id": "c"}],
        },
        output_file=None,
        headings_only=False,
    )
    # FULL mode -> structured dict: preview_tree + the useful metadata KEPT,
    # only the redundant `rows` content twin dropped.
    assert isinstance(out, dict), out
    assert out.get("preview_tree") == tree
    assert out.get("node_count") == 2
    assert out.get("depth") == 2
    assert out.get("_source") == "cache_root_uuid"
    assert out.get("render_scope") == {"kind": "raw_root"}
    assert out.get("root") == {"id": "r"}
    assert out.get("children") == [{"id": "c"}]
    assert "rows" not in out
    assert "preview_markdown" not in out and "preview_text" not in out


def test_finalizer_full_headings_only_strips_raw_tree_payload():
    tree = ["[WG1] Root", "  [WG2] Child"]
    out = srv._shape_glimpse_family_return(
        {
            "success": True,
            "node_count": 2,
            "depth": 2,
            "preview_tree": tree,
            "rows": [1],
            "root": {"id": "r"},
            "children": [{"id": "c"}],
        },
        output_file=None,
        headings_only=True,
    )
    # The headings-only UUID-harvest pass is the compact mapping pass: keep the
    # UUID preview_tree + stats, drop rows AND bulky raw root/children.
    assert isinstance(out, dict), out
    assert out.get("preview_tree") == tree
    assert out.get("node_count") == 2
    assert out.get("depth") == 2
    assert "rows" not in out
    assert "root" not in out
    assert "children" not in out


def test_finalizer_output_file_passthrough():
    payload = {"success": True, "output_file": "/tmp/x.md", "bytes_written": 12}
    out = srv._shape_glimpse_family_return(payload, output_file="/tmp/x.md")
    assert out is payload  # unchanged, structured


def test_finalizer_error_passthrough():
    payload = {"success": False, "error": "boom"}
    out = srv._shape_glimpse_family_return(payload, output_file=None)
    assert out is payload  # errors stay structured so failures still signal


def test_finalizer_push_ack_passthrough():
    ack = {"success": True, "pushed_to_modal": True, "root_uuid": _ROOT}
    out = srv._shape_glimpse_family_return(ack, output_file=None)
    assert out is ack  # no preview_* -> structured ack unchanged


def test_finalizer_empty_markdown_is_empty_string():
    # An empty since-window / empty selection sets preview_markdown="" -- a str,
    # so it is returned raw (an empty document is a valid answer).
    out = srv._shape_glimpse_family_return(
        {"success": True, "node_count": 0, "empty_window": True, "preview_markdown": ""},
        output_file=None,
    )
    assert out == ""


# ---------------------------------------------------------------------------
# [ap:XDVDRG] STALE-FOCUS GUARD: a stale-focus fault payload (focus_unresolved
# + error + an ALWAYS-EMPTY preview string) must NEVER be delivered as its raw
# '' string in Markdown/raw-text mode -- it must surface as the loud
# structured error. A legitimate empty (no focus_unresolved) stays ''.
# ---------------------------------------------------------------------------
def test_finalizer_stale_focus_markdown_returns_loud_structured_error():
    stale = {
        "success": True,
        "focus_unresolved": "dead-uuid",
        "node_count": 0,
        "depth": 0,
        "error": (
            "The requested focus row could not be resolved"
            " occurrence-exactly against the current render tree, so it"
            " will not be guessed. Refresh the nodes cache and retry"
            " (re-apply the slice if you edited Workflowy)."
        ),
        "preview_markdown": "",
        "rows": [],
    }
    out = srv._shape_glimpse_family_return(stale, output_file=None)
    assert isinstance(out, dict), out
    assert out.get("success") is False
    assert out.get("focus_unresolved") == "dead-uuid"
    assert "could not be resolved" in str(out.get("error"))
    assert "preview_markdown" not in out
    assert "rows" not in out


def test_finalizer_stale_focus_preview_text_returns_loud_structured_error():
    stale = {
        "success": True,
        "focus_unresolved": "dead-uuid",
        "error": "The requested focus row could not be resolved",
        "preview_text": "",
        "rows": [],
    }
    out = srv._shape_glimpse_family_return(stale, output_file=None)
    assert isinstance(out, dict), out
    assert out.get("success") is False
    assert out.get("focus_unresolved") == "dead-uuid"
    assert "error" in out
    assert "preview_text" not in out


def test_finalizer_full_mode_stale_focus_stays_structured_success():
    # Full/UUID mode was ALREADY loud (the dict carries error +
    # focus_unresolved); the guard must not change its shape.
    stale = {
        "success": True,
        "focus_unresolved": "dead-uuid",
        "node_count": 0,
        "error": "stale",
        "preview_tree": [],
        "rows": [],
    }
    out = srv._shape_glimpse_family_return(stale, output_file=None)
    assert isinstance(out, dict), out
    assert out.get("success") is True
    assert out.get("focus_unresolved") == "dead-uuid"
    assert out.get("preview_tree") == []
    assert "rows" not in out


def test_finalizer_output_file_stale_focus_passthrough():
    # output_file passthrough is rule (1): the file summary is the deliverable.
    stale = {
        "success": True,
        "focus_unresolved": "dead-uuid",
        "error": "stale",
        "preview_markdown": "",
        "rows": [],
    }
    out = srv._shape_glimpse_family_return(stale, output_file="/tmp/x.md")
    assert out is stale


def test_glimpse_run_render_spec_blob_stale_focus_is_loud_error(monkeypatch):
    # THE to-do's headline path, MCP-facing: a saved spec blob (Markdown mode)
    # whose focus row no longer resolves. Through the family dispatch the
    # agent gets the loud error envelope -- never ''.
    _patch_common(monkeypatch)
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        focus={
            "mode": "row",
            "uuid": "no-such-uuid",
            "ancestor_path": [_ROOT],
            "occurrence": 0,
        },
    )
    reply = asyncio.run(
        dispatch_operation(
            srv.GLIMPSE_REGISTRY, "run", {"render_spec": spec.to_render_spec_blob()}
        )
    )
    assert isinstance(reply, dict), reply
    assert reply.get("success") is False, reply
    err = reply.get("error") or {}
    assert err.get("code") == "glimpse_request_failed", err
    assert "could not be resolved" in str(err.get("message")), err
    legacy = (err.get("extra") or {}).get("legacy_result") or {}
    assert legacy.get("focus_unresolved") == "no-such-uuid"


# ---------------------------------------------------------------------------
# End-to-end: glimpse(root_uuid=...) raw-dump cell.
# ---------------------------------------------------------------------------
def test_glimpse_root_uuid_markdown_returns_raw_string(monkeypatch):
    _patch_common(monkeypatch)
    reply = asyncio.run(srv.glimpse(root_uuid=_ROOT))
    assert isinstance(reply, str), reply
    assert "Contract Root" in reply
    assert "Alpha point" in reply and "Beta point" in reply


def test_glimpse_root_uuid_full_mode_returns_dict_with_tree_and_stats(monkeypatch):
    _patch_common(monkeypatch)
    reply = asyncio.run(srv.glimpse(root_uuid=_ROOT, as_markdown=False))
    # FULL mode -> structured dict WITH the useful metadata, preview_tree kept,
    # rows dropped.
    assert isinstance(reply, dict), reply
    assert reply.get("node_count") == 3, reply
    assert reply.get("depth") is not None, reply
    assert "rows" not in reply, reply
    tree = reply.get("preview_tree")
    assert isinstance(tree, list) and tree, reply
    flat = "\n".join(tree)
    # Full/UUID mode carries the UUIDs (the whole point of full mode).
    assert _ROOT in flat and _CHILD_A in flat


def test_glimpse_root_uuid_output_file_returns_summary(monkeypatch):
    _patch_common(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "out.md")
        reply = asyncio.run(srv.glimpse(root_uuid=_ROOT, output_file=path))
        # output_file path -> structured summary UNCHANGED (not a raw string).
        assert isinstance(reply, dict), reply
        assert reply.get("output_file") == path
        assert reply.get("success") is True
        with open(path, "r", encoding="utf-8") as f:
            disk = f.read()
        assert "Contract Root" in disk


# ---------------------------------------------------------------------------
# End-to-end: lightning_rod cells.
# ---------------------------------------------------------------------------
def test_lightning_rod_render_tags_markdown_returns_raw_string(monkeypatch):
    _patch_common(monkeypatch)
    reply = asyncio.run(srv.lightning_rod(mode="render_tags", root_uuid=_ROOT, tags=["mytag"]))
    assert isinstance(reply, str), reply
    # The #mytag node is Alpha; Beta is not a member.
    assert "Alpha point" in reply
    assert "Beta point" not in reply


def test_lightning_rod_gather_returns_raw_text_outline(monkeypatch):
    _patch_common(monkeypatch)
    reply = asyncio.run(srv.lightning_rod(mode="gather", root_uuid=_ROOT))
    # gather -> the raw tag-outline STRING (preview_text), NOT a dict.
    assert isinstance(reply, str), reply
    assert "mytag" in reply


def test_lightning_rod_render_tags_full_mode_returns_dict_with_tree(monkeypatch):
    _patch_common(monkeypatch)
    reply = asyncio.run(srv.lightning_rod(
        mode="render_tags", root_uuid=_ROOT, tags=["mytag"], as_markdown=False
    ))
    # FULL mode -> structured dict with preview_tree + metadata, no rows.
    assert isinstance(reply, dict), reply
    assert isinstance(reply.get("preview_tree"), list), reply
    assert "rows" not in reply, reply


def test_lightning_rod_error_stays_structured(monkeypatch):
    _patch_common(monkeypatch)
    # render_tags with no tags -> a loud error dict (NOT a raw string).
    reply = asyncio.run(srv.lightning_rod(mode="render_tags", root_uuid=_ROOT))
    assert isinstance(reply, dict), reply
    assert reply.get("success") is False


# ---------------------------------------------------------------------------
# 9a ([ap:G2UALX]) — the empty-output stub at the public funnel. In-scope row:
# a gather over a subtree with no tags legitimately returns nothing; the
# funnel delivers the literal "(empty)", never a bare ''. Out of scope by
# construction: dict returns (error envelopes, full/UUID mode) are never
# touched, and non-empty strings pass through byte-identically.
# ---------------------------------------------------------------------------
_UNTAGGED_CACHE_NODES = [
    {"id": _ROOT, "name": "Bare Root", "note": "", "parent_id": None, "priority": 0},
    {
        "id": _CHILD_A,
        "name": "Alpha point",
        "note": "no tags anywhere",
        "parent_id": _ROOT,
        "priority": 0,
    },
]


def _make_untagged_client():
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _UNTAGGED_CACHE_NODES]
    return c


def test_gather_over_untagged_subtree_returns_empty_stub(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(srv, "get_client", _make_untagged_client)
    # The one in-scope producer row, end-to-end through the ONE public
    # funnel: zero tags in scope is a legitimate "nothing" answer, delivered
    # as the literal stub so it can never be mistaken for a fault.
    reply = asyncio.run(
        dispatch_operation(
            srv.LIGHTNING_ROD_REGISTRY, "gather", {"root_handle": _ROOT}
        )
    )
    assert reply == "(empty)", reply


def test_gather_over_tagged_subtree_still_returns_raw_outline(monkeypatch):
    _patch_common(monkeypatch)
    # Non-empty results are byte-identical: the stub only fires on a
    # whitespace-only body.
    reply = asyncio.run(
        dispatch_operation(
            srv.LIGHTNING_ROD_REGISTRY, "gather", {"root_handle": _ROOT}
        )
    )
    assert isinstance(reply, str), reply
    assert "mytag" in reply


def test_legacy_wrapper_boundary_stubs_whitespace_only_result(monkeypatch):
    # The legacy profile's single public seam wraps every cache-backed legacy
    # tool; the stub runs on the raw handler result there too -- render
    # families included, so the wrapper's glimpse/lightning_rod advisory skip
    # must not hide the stub. The coherence boundary itself is S04's subject
    # (tests/test_coherence_s04_legacy_wrapper_provenance.py owns it); this
    # test bypasses it with a noop async CM and pins the stub ordering.
    import contextlib

    @contextlib.asynccontextmanager
    async def _noop_boundary(_label):
        yield

    monkeypatch.setattr(srv, "_mcp_operation_boundary", _noop_boundary)

    async def _blank(*_a, **_k):
        return "  \n"

    wrapped_blank = srv._wrap_legacy_cache_operation_boundary(
        _blank, "workflowy_glimpse"
    )
    assert asyncio.run(wrapped_blank()) == "(empty)"

    async def _body(*_a, **_k):
        return "body"

    wrapped_body = srv._wrap_legacy_cache_operation_boundary(
        _body, "workflowy_glimpse"
    )
    assert asyncio.run(wrapped_body()) == "body"

    async def _envelope(*_a, **_k):
        return {"success": True, "preview_tree": []}

    wrapped_envelope = srv._wrap_legacy_cache_operation_boundary(
        _envelope, "workflowy_glimpse"
    )
    assert asyncio.run(wrapped_envelope()) == {
        "success": True,
        "preview_tree": [],
    }
