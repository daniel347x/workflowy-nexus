"""The automatic fallback: reads answer archived handles (Archive Reads U2).

§U2.5 test 1 — the §U2.4 read paragraphs: the banner is line 1 (above an
injected coherence advisory and any correction line), dict results carry
``archive_warning`` + ``archived``, the 📦 name and the verbatim note, no
live GET ever runs for an archived id, a live handle in the same call is
unchanged, the E2 pair (an exact archived full UUID beats a shaved live
match, no correction echo), the resolve verbs' records, the beacon hint,
and the render_symbols tier skip.
"""

from __future__ import annotations

import asyncio

import pytest

from coherence_harness import make_boundary_capable, s04_boundary_status

from workflowy_mcp import server
from workflowy_mcp.archive_store import ArchiveStore, set_archive_store_for_tests
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.coherence import COHERENCE_READ_ADVISORY_LINE
from workflowy_mcp.models.node import WorkFlowyNode
from workflowy_mcp.node_archive import build_archive_document, serialize_archive

SOURCE_CACHE = {"epoch_number": 1, "epoch_id": "e1", "adopted_sequence": 3}

LIVE_UUID = "aaaaaaaa-0000-0000-0000-000000000001"
ARCHIVED_UUID = "aaaaaaaa-0000-0000-0000-0000000000ff"  # shares the 8-hex head
UNIQUE_ARCHIVED = "eeeeeeee-0000-0000-0000-0000000000ee"  # head no live node shares
LIVE_OTHER = "bbbbbbbb-0000-0000-0000-000000000002"
CHILD_UUID = "dddddddd-0000-0000-0000-000000000004"
AP_CORE = "ARC1"

BANNER_HEAD = "⚠️⚠️⚠️ ARCHIVED NODE: NOT IN WORKFLOWY."


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


def _write_archive(tmp_path, records, node_id, name="arch.json"):
    document = build_archive_document(
        list(records),
        nest=_nest,
        node_id=node_id,
        name=name,
        description=None,
        exported_at="2026-10-01T12:00:00Z",
        source_cache=dict(SOURCE_CACHE),
    )
    (tmp_path / name).write_bytes(serialize_archive(document).encode("utf-8"))


def _archive_records():
    return [
        _rec(
            ARCHIVED_UUID,
            name=f"[ap:{AP_CORE}] Archived root",
            note="archived root note",
        ),
        _rec(
            CHILD_UUID,
            name="Archived child",
            parent_id=ARCHIVED_UUID,
            note="child note with &lt;b&gt; entity &amp; tail",
        ),
        _rec(
            UNIQUE_ARCHIVED,
            name="Archived second child",
            parent_id=ARCHIVED_UUID,
            note="unique-head child note",
        ),
    ]


@pytest.fixture()
def archive_env(tmp_path, monkeypatch):
    """One active archive (root + child), a two-node live view, a client
    double whose ``get_node`` LOUDLY fails (reads must never reach it for
    archived ids), and the injected store."""
    _write_archive(tmp_path, _archive_records(), ARCHIVED_UUID)
    store = ArchiveStore(folder_provider=lambda: str(tmp_path))
    set_archive_store_for_tests(store)
    live = [
        _rec(LIVE_UUID, name="live one", note="live note"),
        _rec(LIVE_OTHER, name="live two"),
    ]
    client = object.__new__(server.WorkFlowyClient)

    async def _get_node(node_id, max_retries=1):
        raise AssertionError(f"get_node must never run in these reads (got {node_id})")

    client.get_node = _get_node
    make_boundary_capable(client, nodes=live)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    yield {"store": store, "tmp_path": tmp_path, "client": client}
    set_archive_store_for_tests(None)


@pytest.fixture()
def no_folder_env(monkeypatch):
    """No archive folder anywhere: every read is byte-identical to today
    except the state-A hint line."""
    set_archive_store_for_tests(ArchiveStore(folder_provider=lambda: ""))
    live = [_rec(LIVE_UUID, name="live one", note="live note")]
    client = object.__new__(server.WorkFlowyClient)
    live_node = WorkFlowyNode.model_validate(
        {
            "id": LIVE_UUID,
            "name": "live one",
            "note": "live note",
            "priority": 0,
        }
    )

    async def _get_node(node_id, max_retries=1):
        assert node_id == LIVE_UUID
        return live_node

    client.get_node = _get_node
    make_boundary_capable(client, nodes=live)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    yield {"client": client}
    set_archive_store_for_tests(None)


def _node(verb, params):
    return asyncio.run(server.node(verb=verb, params=params))


# ---------------------------------------------------------------------------
# node.read serves an archived handle under the banner
# ---------------------------------------------------------------------------


def test_node_read_archived_ap_banner_first(archive_env):
    result = _node("read", {"node_id": f"[ap:{AP_CORE}]"})
    assert isinstance(result, str)
    lines = result.split("\n")
    assert lines[0].startswith(BANNER_HEAD)
    assert "(exported 2026-10-01T12:00:00Z)" in lines[0]
    assert "Tell the human." in lines[0]
    # The 📦 overlay name (the mark rides the ordinary TITLE, after the AP
    # token, §4.2.2) and the VERBATIM archived note follow, as today's raw
    # document framing.
    assert "[name: not editable] [ap:ARC1] 📦 Archived root" in result
    assert "archived root note" in result


def test_node_read_archived_full_uuid_and_prefix(archive_env):
    # The full archived UUID is served; so is an 8-hex PREFIX that no live
    # node shares (UNIQUE_ARCHIVED's head) — a unique visible id with the
    # supplied prefix answers through the gate + re-run.
    for handle, needle in (
        (ARCHIVED_UUID, "archived root note"),
        (UNIQUE_ARCHIVED[:8], "unique-head child note"),
    ):
        result = _node("read", {"node_id": handle})
        assert isinstance(result, str), handle
        assert result.startswith(BANNER_HEAD), handle
        assert needle in result


def test_node_read_archived_structured_from_record(archive_env):
    # Chain-1 W1 (F1): the delivered STRUCTURED result carries the exact
    # §5.1 warning and the answered-handle/file/date/root provenance —
    # a bare WorkFlowyNode model with only the 📦 name is not the proof
    # (E30: a live title may begin with 📦).
    result = _node("read", {"node_id": ARCHIVED_UUID, "as_raw_node": False})
    assert not isinstance(result, WorkFlowyNode)
    assert isinstance(result, dict)
    assert result["id"] == ARCHIVED_UUID
    assert result["name"] == "[ap:ARC1] 📦 Archived root"  # the 📦 name remains
    assert result["note"] == "archived root note"
    assert result["parentId"] is None  # a planted root at the top level (by-alias dump)
    warning = result["archive_warning"]
    assert warning.startswith(BANNER_HEAD)
    assert "(exported 2026-10-01T12:00:00Z)" in warning
    assert "Tell the human." in warning
    (archived,) = result["archived"]
    assert archived["handle"] == ARCHIVED_UUID
    assert archived["uuid"] == ARCHIVED_UUID
    assert archived["archive_file"].endswith(".json")
    assert archived["archive_name"]
    assert archived["exported_at"] == "2026-10-01T12:00:00Z"
    assert archived["archive_root"] == ARCHIVED_UUID


def test_node_read_live_structured_keeps_model_contract(no_folder_env):
    # W1 control: a LIVE structured read (and any no-archive result) keeps
    # the established model contract byte-identically — the model object is
    # delivered unchanged, with no archive keys anywhere.
    result = _node("read", {"node_id": LIVE_UUID, "as_raw_node": False})
    assert isinstance(result, WorkFlowyNode)
    assert result.id == LIVE_UUID
    assert result.name == "live one"
    assert result.note == "live note"


def test_e2_exact_archived_uuid_beats_shaved_live_no_correction(archive_env):
    result = _node("read", {"node_id": ARCHIVED_UUID})
    assert result.startswith(BANNER_HEAD)
    assert "[ap:ARC1] 📦 Archived root" in result
    assert "live one" not in result  # the shaved live target was NOT served
    assert "resolved:" not in result  # no correction echo (E2)


def test_banner_above_injected_coherence_advisory(archive_env, monkeypatch):
    # Rebuild the double with a hard-barrier boundary status so the read
    # advisory is injected; the banner must still be the FIRST line.
    status = s04_boundary_status(
        hard_dirty=True,
        hard_generation=7,
        hard_reason_code="verified_effect_unpublishable",
        hard_transaction_id="tx-advisory",
    )
    client = object.__new__(server.WorkFlowyClient)

    async def _get_node(node_id, max_retries=1):
        raise AssertionError("get_node must never run")

    client.get_node = _get_node
    make_boundary_capable(
        client,
        nodes=[_rec(LIVE_UUID, name="live one"), _rec(LIVE_OTHER, name="live two")],
        boundary_status=status,
    )
    monkeypatch.setattr(server, "get_client", lambda: client)

    result = _node("read", {"node_id": f"[ap:{AP_CORE}]"})
    lines = result.split("\n")
    assert lines[0].startswith(BANNER_HEAD)
    # The advisory follows immediately (the banner block is banner + "\n";
    # the advisory carries its own trailing blank line).
    assert lines[1] == COHERENCE_READ_ADVISORY_LINE


def test_banner_order_above_correction_line():
    # Unit-level ordering proof: the finalizer applies the correction echo
    # first and the archive banner LAST, so the banner opens the result.
    from coherence_harness import make_boundary_capable as _mbc

    from workflowy_mcp.archive_context import (
        ArchiveOperationContext,
        ArchiveOperationState,
        ArchiveServed,
    )
    from workflowy_mcp.coherence.operation_cache import (
        archive_operation_scope,
        cache_operation_boundary,
    )
    from workflowy_mcp.handle_corrections import record_handle_correction

    record_handle_correction(
        supplied="aaaaaaaa",
        resolved_uuid=LIVE_UUID,
        name="live one",
        short="aaaaaaaa",
    )
    echoed = server._apply_handle_correction_echo("BODY")
    assert echoed.startswith("resolved: ")

    state = ArchiveOperationState()
    state.record_served(
        ArchiveServed(
            handle=f"[ap:{AP_CORE}]",
            uuid=ARCHIVED_UUID,
            archive_file="/f/a.json",
            archive_name="a",
            exported_at="2026-10-01T12:00:00Z",
            archive_root=ARCHIVED_UUID,
        )
    )
    ctx = ArchiveOperationContext(
        mode="include", reason="flag", overlay=None, state=state
    )
    client = _mbc(object.__new__(server.WorkFlowyClient), nodes=[_rec(LIVE_UUID)])

    async def _inside_boundary():
        # archive_operation_scope requires an active snapshot: run inside
        # the ONE boundary (the same posture the invoker provides).
        from workflowy_mcp.coherence.operation_cache import cache_operation_boundary

        async with cache_operation_boundary(client, "probe"):
            with archive_operation_scope(ctx):
                return server._apply_archive_banner(echoed)

    out = asyncio.run(_inside_boundary())
    assert out.startswith(BANNER_HEAD)
    assert "resolved: " in out
    assert out.index(BANNER_HEAD) < out.index("resolved: ")


def test_live_read_without_archives_byte_identical(no_folder_env):
    result = _node("read", {"node_id": LIVE_UUID})
    assert isinstance(result, str)
    assert BANNER_HEAD not in result
    assert "[name: not editable] live one" in result
    assert "live note" in result


def test_no_folder_state_a_hint_on_ap_shaped_miss(no_folder_env):
    record = _node("resolve", {"node_id": "[ap:NOPE1]"})
    assert record["success"] is True and record["match_count"] == 0
    hint = record.get("hint") or ""
    assert "no archive folder is configured" in hint
    # A UUID-shaped miss carries NO hint.
    record = _node("resolve", {"node_id": "c0000000-0000-0000-0000-000000000009"})
    assert "hint" not in record


# ---------------------------------------------------------------------------
# The resolve verbs: success records, hint states, the beacon line
# ---------------------------------------------------------------------------


def test_resolve_record_archived_fields(archive_env):
    record = _node("resolve", {"node_id": f"[ap:{AP_CORE}]"})
    assert record["success"] is True
    assert record["match_count"] == 1
    assert record["resolved_uuid"] == ARCHIVED_UUID
    assert record["ap_id"] == f"[ap:{AP_CORE}]"
    assert record.get("archive_warning", "").startswith(BANNER_HEAD)
    archived = record.get("archived") or []
    assert len(archived) == 1
    entry = archived[0]
    assert entry["handle"] == f"[ap:{AP_CORE}]"
    assert entry["uuid"] == ARCHIVED_UUID
    assert entry["archive_name"] == "arch.json"
    assert entry["exported_at"] == "2026-10-01T12:00:00Z"
    assert entry["archive_root"] == ARCHIVED_UUID


def test_resolve_hint_state_b_searched_not_found(archive_env):
    record = _node("resolve", {"node_id": "[ap:NOPE1]"})
    hint = record.get("hint") or ""
    assert "in the 1 active archive(s) in" in hint
    assert str(archive_env["tmp_path"]) in hint


def test_resolve_hint_state_c_unreadable_file(archive_env, tmp_path):
    # Corrupt the archive file: the folder is searched but not fully readable.
    (archive_env["tmp_path"] / "arch.json").write_bytes(b"{not an archive")
    record = _node("resolve", {"node_id": "[ap:NOPE1]"})
    hint = record.get("hint") or ""
    assert "could not be fully read" in hint
    assert "system(verb='archives.list')" in hint


def test_beacon_resolve_hint_is_the_beacon_line(archive_env, monkeypatch):
    # The beacon surface reads its suppress list only when the symbol leg
    # actually runs (it does here: the AP leg missed); stub the tab read and
    # the symbol engine so the 0/1/N record builds without a tab.
    async def _no_tags():
        return []

    monkeypatch.setattr(server, "_beacon_symbol_suppress_tags", _no_tags)

    async def _symbol_zero(*a, **k):
        return {"success": True, "match_count": 0, "candidates": [], "truncated": 0}

    archive_env["client"].read_uuid_from_symbol = _symbol_zero

    result = asyncio.run(
        server.beacon(verb="resolve", params={"source_handle": f"[ap:{AP_CORE}]"})
    )
    assert result["success"] is True
    assert result["match_count"] == 0  # the beacon family never serves archives
    hint = result.get("hint") or ""
    assert "beacon never reads export archives" in hint
    assert "node.resolve or anchor.point.resolve read them" in hint
    assert BANNER_HEAD not in str(result.get("archive_warning") or "")


def test_beacon_snippet_archived_uuid_keeps_today_not_found(archive_env):
    # Axiom 12: the beacon family serves nothing from archives. A
    # UUID-shaped archived handle misses exactly as an unknown one.
    from workflowy_mcp.mcp_api_registry import make_error  # noqa: F401

    result = asyncio.run(server.beacon(verb="snippet", params={"source_handle": ARCHIVED_UUID}))
    assert result.get("success") is False
    message = str((result.get("error") or {}).get("message") or "")
    assert "not present" in message or "did not resolve" in message or message


# ---------------------------------------------------------------------------
# render_symbols: an archive-resolved item never reaches a disk/snippet tier
# ---------------------------------------------------------------------------


def test_render_symbols_tier_skips_archive_records(archive_env):
    asyncio.run(_tier_skip_body(archive_env))


async def _tier_skip_body(archive_env):
    from workflowy_mcp.coherence.cache_view import CacheView

    store = archive_env["store"]
    live_view = CacheView.build(
        epoch_number=7,
        epoch_id="live-epoch",
        adopted_sequence=11,
        nodes_export={"nodes": []},
    )
    cache_by_id = store.overlay_for(live_view).augmented_view.nodes_by_id
    overlay_record = cache_by_id[ARCHIVED_UUID]

    class _Boom:
        async def get_code_snippet_for_resolved_node(self, *a, **k):
            raise AssertionError("disk tier must never run for an archive record")

    failures = await server._apply_render_symbols_tier(
        {
            "id": ARCHIVED_UUID,
            "name": overlay_record["name"],
            "note": overlay_record["note"],
            "children": [],
        },
        cache_by_id,
        _Boom(),
        {ARCHIVED_UUID},
        tier="context",
        context_lines=3,
        tail_lines=2,
    )
    assert failures == []


def test_render_symbols_archive_item_resolves_via_archive_leg(archive_env):
    # Inside an augmented map (as the re-run provides), a UUID item that is
    # only archived resolves through the ARCHIVE leg and lands in the
    # resolved bucket with the overlay name.
    from workflowy_mcp.coherence.cache_view import CacheView

    async def _body():
        store = archive_env["store"]
        live_view = CacheView.build(
            epoch_number=7,
            epoch_id="live-epoch",
            adopted_sequence=11,
            nodes_export={"nodes": []},
        )
        cache_by_id = store.overlay_for(live_view).augmented_view.nodes_by_id
        return await server._resolve_lightning_rod_symbol_items_from_cache(
            cache_by_id=cache_by_id,
            symbols=[ARCHIVED_UUID[:8]],
            resolved_roots=[ARCHIVED_UUID],
            suppress_tags=[],
            favor="uuid",
        )

    report = asyncio.run(_body())
    resolved = [r for r in report["records"] if r["status"] == "resolved"]
    assert len(resolved) == 1
    assert resolved[0]["uuid"] == ARCHIVED_UUID
    assert "📦" in resolved[0]["candidate"]["name"]
