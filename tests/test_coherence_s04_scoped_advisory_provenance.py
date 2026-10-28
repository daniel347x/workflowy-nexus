"""S04 Remediation Chain 5 (work-order item 2) — advisory provenance on
EVERY Scoped slice-derived result shape.

``_handle_render_lightning_dom_capture`` bypassed the ONE advisory
authority for slice-derived Gather and empty-match returns, and its
nonempty branch kept the warning text but dropped the structured
``coherence_advisory`` field from the final WS reply.

Chain 5: the ONE advisory authority (explicit ``cache_sourced=was_slice``
provenance — never a copied warning literal, never a second hard-stop
decision) now shapes ALL THREE Scoped result shapes:

- slice-derived Scoped Gather: warning framing + structured advisory;
- slice-derived empty Tags/Search: warning framing + structured advisory;
- slice-derived nonempty Tags/Search: warning framing AND the structured
  advisory preserved through the final reply (forwarded verbatim from the
  dispatch result);
- DOM-frozen versions of all three remain byte-identical and unadvised;
- clean controls unchanged;
- the client-facing ``cache_sourced`` wire field is NOT repurposed (it
  still describes artifact durability, consumed by the dock); advisory
  provenance rides the authority's own kwarg.

Required-test mutation note (step 4a): the adversarial tests below were
verified to FAIL against the pre-fix code (production stashed to the
a77e8ea baseline) with their predicted shapes — see the Chain-5 result
anchor for the full evidence table.
"""

from __future__ import annotations

import asyncio
import json

from coherence_harness import make_boundary_capable, s04_boundary_status

from workflowy_mcp import server
from workflowy_mcp.cache_render_spec import RenderProjectionSpec
from workflowy_mcp.coherence import COHERENCE_READ_ADVISORY_LINE

HARD = s04_boundary_status(
    hard_dirty=True,
    hard_generation=7,
    hard_reason_code="verified_effect_unpublishable",
    hard_transaction_id="tx-advisory",
)


def _client(status, *, nodes):
    client = object.__new__(server.WorkFlowyClient)
    return make_boundary_capable(client, nodes=nodes, boundary_status=status)


def _patch_client(monkeypatch, status, *, nodes):
    client = _client(status, nodes=nodes)
    monkeypatch.setattr(server, "get_client", lambda: client)
    return client


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, s):
        self.sent.append(json.loads(s))


_PROJECTED_NODES = [
    {"id": "root", "name": "Root", "note": "", "parent_id": None,
     "priority": 0, "createdAt": 1000, "modifiedAt": 1000},
    {"id": "a", "name": "Alpha #keep", "note": "", "parent_id": "root",
     "priority": 0, "createdAt": 1000, "modifiedAt": 1000},
    {"id": "b", "name": "Beta", "note": "", "parent_id": "root",
     "priority": 1, "createdAt": 1000, "modifiedAt": 1000},
]


def _slice_frame(lightning: dict) -> dict:
    """A slice-derived (cache-projected) Scoped frame."""
    blob = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="root"
    ).to_render_spec_blob()
    return {
        "request_id": "t-slice",
        "base_render_spec": blob,
        "was_slice": True,
        "root_uuid": "root",
        "lightning": lightning,
        "as_markdown": True,
    }


def _dom_frame(lightning: dict) -> dict:
    """A genuinely DOM-frozen (browser-captured) Scoped frame."""
    return {
        "request_id": "t-dom",
        "captured_tree": {
            "root": {"id": "root", "name": "Root", "note": ""},
            "children": [
                {"id": "a", "name": "Alpha #keep", "note": "", "children": []},
                {"id": "b", "name": "Beta", "note": "", "children": []},
            ],
        },
        "root_uuid": "root",
        "lightning": lightning,
        "as_markdown": True,
    }


def _drive(monkeypatch, status, frame):
    _patch_client(monkeypatch, status, nodes=_PROJECTED_NODES)
    ws = _FakeWS()
    asyncio.run(server._handle_render_lightning_dom_capture(frame, ws))
    assert len(ws.sent) == 1
    reply = ws.sent[0]
    assert reply["success"] is True, reply
    return reply


def _assert_advisory_payload(reply: dict) -> None:
    advisory = reply["coherence_advisory"]
    assert advisory["status"] == "hard_dirty"
    assert advisory["refresh_required"] is True
    assert advisory["dirty_generation"] == 7
    assert advisory["reason_code"] == "verified_effect_unpublishable"
    assert advisory["transaction_id"] == "tx-advisory"
    assert advisory["message"] == COHERENCE_READ_ADVISORY_LINE


# ---------------------------------------------------------------------------
# Required test (a): slice-derived Scoped Gather under hard dirt.
# ---------------------------------------------------------------------------


def test_slice_derived_gather_hard_dirt_warns_and_advises(monkeypatch):
    reply = _drive(
        monkeypatch,
        HARD,
        _slice_frame({"mode": "gather", "match_scope": "name_and_note"}),
    )
    # Warning framing on the gather outline.
    assert reply["markdown"].startswith(COHERENCE_READ_ADVISORY_LINE + "\n\n")
    assert reply["markdown"].count("COHERENCE FAILURE") == 1
    assert "keep" in reply["markdown"]
    # The structured advisory.
    _assert_advisory_payload(reply)
    # Client-facing wire semantics NOT repurposed.
    assert reply["was_slice"] is True
    assert reply["cache_sourced"] is False
    assert reply["tags"]


# ---------------------------------------------------------------------------
# Required test (b): slice-derived empty Tags/Search render under hard dirt.
# ---------------------------------------------------------------------------


def test_slice_derived_empty_render_hard_dirt_warns_and_advises(monkeypatch):
    reply = _drive(
        monkeypatch,
        HARD,
        _slice_frame(
            {"mode": "render_tags", "tags": ["nomatch"], "match_scope": "name_and_note"}
        ),
    )
    # Warning framing on the (empty) document: warning + ONE blank separator.
    assert reply["markdown"] == COHERENCE_READ_ADVISORY_LINE + "\n\n"
    # The structured advisory.
    _assert_advisory_payload(reply)
    # The empty-result contract is intact.
    assert reply["filtered_forest"] == []
    assert reply["rows"] == []
    assert reply["node_count"] == 0
    assert reply["was_slice"] is True
    assert reply["cache_sourced"] is False


# ---------------------------------------------------------------------------
# Required test (c): slice-derived nonempty render — warning framing AND the
# structured advisory preserved through the final reply.
# ---------------------------------------------------------------------------


def test_slice_derived_nonempty_render_warns_and_preserves_advisory(monkeypatch):
    reply = _drive(
        monkeypatch,
        HARD,
        _slice_frame(
            {"mode": "render_tags", "tags": ["keep"], "match_scope": "name_and_note"}
        ),
    )
    # Warning framing (exactly once — the internal seam already prefixed).
    assert reply["markdown"].startswith(COHERENCE_READ_ADVISORY_LINE + "\n\n")
    assert reply["markdown"].count("COHERENCE FAILURE") == 1
    assert "Alpha" in reply["markdown"]
    # The structured advisory the dispatch computed is preserved verbatim.
    _assert_advisory_payload(reply)
    assert reply["was_slice"] is True
    assert reply["cache_sourced"] is False


def test_slice_derived_nonempty_search_warns_and_preserves_advisory(monkeypatch):
    reply = _drive(
        monkeypatch,
        HARD,
        _slice_frame(
            {"mode": "render_search", "query": "Alpha", "match_scope": "name_and_note"}
        ),
    )
    assert reply["markdown"].startswith(COHERENCE_READ_ADVISORY_LINE + "\n\n")
    assert reply["markdown"].count("COHERENCE FAILURE") == 1
    _assert_advisory_payload(reply)


# ---------------------------------------------------------------------------
# Required test (d): DOM-frozen controls remain byte-identical and unadvised.
# ---------------------------------------------------------------------------


def test_dom_frozen_gather_unadvised_byte_identical(monkeypatch):
    reply = _drive(
        monkeypatch,
        HARD,
        _dom_frame({"mode": "gather", "match_scope": "name_and_note"}),
    )
    assert "COHERENCE FAILURE" not in reply["markdown"]
    assert not reply["markdown"].startswith(COHERENCE_READ_ADVISORY_LINE)
    assert "coherence_advisory" not in reply
    # The exact pre-fix reply shape.
    assert set(reply) == {
        "action", "request_id", "success", "markdown", "render_scope", "rows",
        "cache_sourced", "truncated", "tags", "tag_count", "node_count", "was_slice",
    }


def test_dom_frozen_empty_render_unadvised_byte_identical(monkeypatch):
    reply = _drive(
        monkeypatch,
        HARD,
        _dom_frame(
            {"mode": "render_tags", "tags": ["nomatch"], "match_scope": "name_and_note"}
        ),
    )
    assert reply["markdown"] == ""
    assert "coherence_advisory" not in reply
    assert set(reply) == {
        "action", "request_id", "success", "markdown", "render_scope",
        "filtered_forest", "rows", "cache_sourced", "truncated", "node_count",
        "was_slice",
    }


def test_dom_frozen_nonempty_render_unadvised_shape_unchanged(monkeypatch):
    reply = _drive(
        monkeypatch,
        HARD,
        _dom_frame(
            {"mode": "render_tags", "tags": ["keep"], "match_scope": "name_and_note"}
        ),
    )
    assert "COHERENCE FAILURE" not in reply["markdown"]
    assert "Alpha" in reply["markdown"]
    assert "coherence_advisory" not in reply
    assert set(reply) == {
        "action", "request_id", "success", "markdown", "preview_tree",
        "render_scope", "filtered_forest", "rows", "cache_sourced", "truncated",
        "node_count", "since_applied", "empty_window", "was_slice",
    }


# ---------------------------------------------------------------------------
# Required test (e): clean controls unchanged.
# ---------------------------------------------------------------------------


def test_slice_derived_gather_clean_unadvised_and_content_identical(monkeypatch):
    reply = _drive(
        monkeypatch,
        None,
        _slice_frame({"mode": "gather", "match_scope": "name_and_note"}),
    )
    assert "COHERENCE FAILURE" not in reply["markdown"]
    assert not reply["markdown"].startswith(COHERENCE_READ_ADVISORY_LINE)
    assert "coherence_advisory" not in reply
    assert "keep" in reply["markdown"]


def test_slice_derived_empty_render_clean_unadvised(monkeypatch):
    reply = _drive(
        monkeypatch,
        None,
        _slice_frame(
            {"mode": "render_tags", "tags": ["nomatch"], "match_scope": "name_and_note"}
        ),
    )
    assert reply["markdown"] == ""
    assert "coherence_advisory" not in reply
    assert reply["node_count"] == 0


def test_slice_derived_nonempty_render_clean_unadvised(monkeypatch):
    reply = _drive(
        monkeypatch,
        None,
        _slice_frame(
            {"mode": "render_tags", "tags": ["keep"], "match_scope": "name_and_note"}
        ),
    )
    assert "COHERENCE FAILURE" not in reply["markdown"]
    assert "Alpha" in reply["markdown"]
    assert "coherence_advisory" not in reply
