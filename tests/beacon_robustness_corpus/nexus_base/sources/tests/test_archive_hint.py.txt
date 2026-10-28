"""The light hint (§5.4) and the §5.3 refusals (Archive Reads U2, §U2.5
tests 4 and 5).

One representative site per §U2.3 not-found site family: the shared
root-miss composer (glimpse raw-dump root), the lens ``resolve_root``,
``_render_nodes``'s refusal, the Anchor-Dock root guard, the beacon scope
roots; the resolve records' hints (states A/B/C and the beacon line);
UUID-shaped misses byte-identical.  Then §5.3.1 (node.export), §5.3.2
(output_file and push_to_modal on read verbs) and §5.3.3 (purge preview).
"""

from __future__ import annotations

import asyncio

import pytest

from coherence_harness import make_boundary_capable

from workflowy_mcp import server
from workflowy_mcp.archive_store import ArchiveStore, set_archive_store_for_tests
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.node_archive import build_archive_document, serialize_archive

SOURCE_CACHE = {"epoch_number": 1, "epoch_id": "e1", "adopted_sequence": 3}

LIVE_UUID = "aaaaaaaa-0000-0000-0000-000000000001"
ARCHIVED_UUID = "eeeeeeee-0000-0000-0000-0000000000ee"
AP_CORE = "ARC1"

STATE_A_MARK = "no archive folder is configured"
STATE_B_MARK = "active archive(s)"
STATE_C_MARK = "could not be fully read"
BEACON_MARK = "beacon never reads export archives"
SINK_MARK = "archived material is never written to a file or pushed to the modal"


def _nest(records):
    return WorkFlowyClientEtch.__new__(WorkFlowyClientEtch)._build_hierarchy(
        list(records)
    )


def _rec(node_id, name="n", parent_id=None, priority=0, note="", **extra):
    record = {
        "id": node_id,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
    }
    record.update(extra)
    return record


def _write_archive(tmp_path, name="arch.json"):
    document = build_archive_document(
        [_rec(ARCHIVED_UUID, name=f"[ap:{AP_CORE}] Archived root", note="archived")],
        nest=_nest,
        node_id=ARCHIVED_UUID,
        name="archived root",
        description=None,
        exported_at="2026-10-01T12:00:00Z",
        source_cache=dict(SOURCE_CACHE),
    )
    (tmp_path / name).write_bytes(serialize_archive(document).encode("utf-8"))


class _Env:
    def __init__(self, tmp_path, monkeypatch, *, with_folder=True, corrupt=False):
        if with_folder:
            _write_archive(tmp_path)
            if corrupt:
                (tmp_path / "arch.json").write_bytes(b"{oops")
            self.folder = str(tmp_path)
        else:
            self.folder = ""
        set_archive_store_for_tests(
            ArchiveStore(folder_provider=lambda: self.folder)
        )
        live = [_rec(LIVE_UUID, name="live one", note="live note")]
        client = object.__new__(server.WorkFlowyClient)

        async def _get_node(node_id, max_retries=1):
            from workflowy_mcp.models.node import WorkFlowyNode

            return WorkFlowyNode.model_validate(
                {"id": node_id, "name": "live one", "note": "live note"}
            )

        client.get_node = _get_node
        make_boundary_capable(client, nodes=live)
        monkeypatch.setattr(server, "get_client", lambda: client)
        monkeypatch.setattr(server, "_rate_limiter", None)

        async def _no_tags():
            return []

        monkeypatch.setattr(server, "_beacon_symbol_suppress_tags", _no_tags)

        async def _symbol_zero(*a, **k):
            return {"success": True, "match_count": 0, "candidates": [], "truncated": 0}

        client.read_text_snippet_by_symbol = _symbol_zero

        async def _config(target_tab):
            return (["#wfy-ns"], [], "", {"note_session_tags": ["#wfy-ns"]})

        monkeypatch.setattr(server, "_resolve_note_session_config", _config)


@pytest.fixture()
def env_b(tmp_path, monkeypatch):
    env = _Env(tmp_path, monkeypatch, with_folder=True)
    yield env
    set_archive_store_for_tests(None)


@pytest.fixture()
def env_a(tmp_path, monkeypatch):
    _Env(tmp_path, monkeypatch, with_folder=False)
    yield None
    set_archive_store_for_tests(None)


@pytest.fixture()
def env_c(tmp_path, monkeypatch):
    env = _Env(tmp_path, monkeypatch, with_folder=True, corrupt=True)
    yield env
    set_archive_store_for_tests(None)




def _stub_slice_group_config(monkeypatch):
    """The lens Stage-0 / purge paths read the slice-group config once; stub
    the tab read with the shape the parser hands on."""
    async def _config(target_tab):
        postfixes = {
            "auto_expand": ["expand"],
            "suppress": ["suppress"],
            "deprecated": ["deprecated"],
        }
        reply = {
            "note_session_tags": ["#wfy-ns"],
            "global_tags": [],
            "default_note_sessions_parent": "",
            "note_session_local_postfixes": ["local"],
            "note_session_favorite_postfixes": ["fav"],
        }
        return [], None, postfixes, [], reply

    monkeypatch.setattr(server, "_resolve_slice_group_config", _config)


# ---------------------------------------------------------------------------
# The hint states at representative sites
# ---------------------------------------------------------------------------


def test_hint_state_b_on_root_miss_composer(env_b):
    result = asyncio.run(
        server.glimpse_family_tool(verb="run", params={"root_handle": "[ap:NOPE1]"})
    )
    text = str(result)
    assert STATE_B_MARK in text
    assert "1 active archive(s)" in text
    # A UUID-shaped miss stays byte-identical (no hint).
    result = asyncio.run(
        server.glimpse_family_tool(
            verb="run",
            params={"root_handle": "cccccccc-0000-0000-0000-000000000009"},
        )
    )
    assert STATE_B_MARK not in str(result)


def test_hint_state_a_without_folder(env_a, tmp_path, monkeypatch):
    _Env(tmp_path, monkeypatch, with_folder=False)
    result = asyncio.run(
        server.glimpse_family_tool(verb="run", params={"root_handle": "[ap:NOPE1]"})
    )
    assert STATE_A_MARK in str(result)


def test_hint_state_c_when_folder_unreadable(env_c):
    result = asyncio.run(
        server.glimpse_family_tool(verb="run", params={"root_handle": "[ap:NOPE1]"})
    )
    text = str(result)
    assert STATE_C_MARK in text
    assert "system(verb='archives.list')" in text


def test_hint_on_render_nodes_refusal(env_b):
    result = asyncio.run(
        server.lightning_rod_family_tool(
            verb="render_nodes", params={"symbols": ["[ap:NOPE1]"]}
        )
    )
    assert STATE_B_MARK in str(result)


def test_hint_on_anchor_dock_root_guard(env_b):
    result = asyncio.run(
        server.node(verb="scry", params={"node_id": "[ap:NOPE1]"})
    )
    text = str(result)
    assert STATE_B_MARK in text
    assert "Anchor-Dock guard" in text or "root_not_in_cache" in text


def test_beacon_scope_roots_hint_is_the_beacon_line(env_b):
    result = asyncio.run(
        server.beacon(
            verb="snippet",
            params={"source_handle": LIVE_UUID, "root_handles": ["[ap:NOPE1]"]},
        )
    )
    text = str(result)
    assert BEACON_MARK in text
    assert STATE_B_MARK not in text


def test_lens_resolve_root_hint(env_b, monkeypatch):
    # A lens subtree atom whose root misses: the resolve_root closure raises
    # with the hint appended (a slice_group.render with a bare subtree atom).
    _stub_slice_group_config(monkeypatch)
    result = asyncio.run(
        server.slice_group(
            verb="render",
            params={"lens": {"subtree": "[ap:NOPE1]"}},
        )
    )
    assert STATE_B_MARK in str(result)


# ---------------------------------------------------------------------------
# §5.3 — the operations that never serve archives
# ---------------------------------------------------------------------------


def test_export_archived_root_refused(env_b, tmp_path):
    out = tmp_path / "never.json"
    result = asyncio.run(
        server.node(
            verb="export",
            params={
                "node_id": f"[ap:{AP_CORE}]",
                "output_file": str(out),
            },
        )
    )
    assert result["success"] is False
    error = result["error"]
    assert error["code"] == "archived_node_read_only"
    message = error["message"]
    assert message.startswith("node.export: ")
    assert "is already archived" in message
    assert "Copy that file instead. No file was written." in message
    assert not out.exists()


def test_export_operation_declares_the_code(env_b):
    # The universal code is declarable by node.export (a domain check: the
    # mapper builds the envelope against the resolved spec).
    from workflowy_mcp.mcp_api_registry import CORE_ERROR_CODES

    assert "archived_node_read_only" in CORE_ERROR_CODES


def test_sink_output_file_on_archived_read_refused(env_b, tmp_path):
    out = tmp_path / "never.txt"
    result = asyncio.run(
        server.node(
            verb="read",
            params={"node_id": f"[ap:{AP_CORE}]", "output_file": str(out)},
        )
    )
    assert result["success"] is False
    error = result["error"]
    assert error["code"] == "archived_node_read_only"
    assert SINK_MARK in error["message"]
    assert "node.read:" in error["message"]
    assert not out.exists()


def test_sink_push_to_modal_refused(env_b, monkeypatch):
    result = asyncio.run(
        server.glimpse_family_tool(
            verb="run",
            params={
                "root_handle": f"[ap:{AP_CORE}]",
                "push_to_modal": True,
            },
        )
    )
    assert result["success"] is False
    error = result["error"]
    assert error["code"] == "archived_node_read_only"
    assert SINK_MARK in error["message"]


def test_purge_preview_archived_root_refused(env_b, monkeypatch):
    # The purge-preview origin resolves its root through the lens
    # resolve_root closure; the §5.3.3 text rides the archive-aware miss.
    _stub_slice_group_config(monkeypatch)
    result = asyncio.run(
        server.tag(
            verb="purge.preview",
            params={"family": "topic-a", "root_handle": f"[ap:{AP_CORE}]"},
        )
    )
    text = str(result)
    assert "archives are never part of a tag purge" in text
    assert "Nothing was previewed." in text
