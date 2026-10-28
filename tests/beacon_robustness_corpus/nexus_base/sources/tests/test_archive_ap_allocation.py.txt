"""AP-ID allocation against the archives (Archive Reads U1, §U1.5 test 8).

``'auto'`` skips every archived core; an explicit archived core refuses
with ``ap_id_collision`` and the §5.6 text; dormant, skipped and
superseded files' cores count; with no folder configured the behaviour is
today's.
"""

from __future__ import annotations

import asyncio

import pytest

from workflowy_mcp import server
from workflowy_mcp.archive_store import ArchiveStore, set_archive_store_for_tests
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.node_archive import (
    VAULT_ARCHIVE_NAME,
    build_archive_document,
    serialize_archive,
)

SOURCE_CACHE = {"epoch_number": 1, "epoch_id": "e1", "adopted_sequence": 3}

EXPORTED_AT = "2026-10-01T12:00:00Z"

LIVE_WITH_AP = "11111111-1111-1111-1111-111111111111"


def _nest(records):
    return WorkFlowyClientEtch.__new__(WorkFlowyClientEtch)._build_hierarchy(
        list(records)
    )


def _rec(node_id, name="n", parent_id=None, priority=0, **extra):
    record = {
        "id": node_id,
        "name": name,
        "note": "",
        "parent_id": parent_id,
        "priority": priority,
    }
    record.update(extra)
    return record


def _write(tmp_path, filename, records, node_id, *, name=None, exported_at=EXPORTED_AT):
    document = build_archive_document(
        list(records),
        nest=_nest,
        node_id=node_id,
        name=name,
        description=None,
        exported_at=exported_at,
        source_cache=dict(SOURCE_CACHE),
    )
    (tmp_path / filename).write_bytes(serialize_archive(document).encode("utf-8"))
    return document


@pytest.fixture(autouse=True)
def _restore_store():
    yield
    set_archive_store_for_tests(None)


@pytest.fixture(autouse=True)
def _no_identity_proof(monkeypatch):
    """The AP uniqueness proof's hard-barrier read is out of scope here."""

    async def _pass(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "workflowy_mcp.coherence.admission.require_cache_identity_proof", _pass
    )


def _nodes_by_id(records):
    return {str(r["id"]): r for r in records}


def test_auto_skips_archived_cores(tmp_path, monkeypatch):
    _write(
        tmp_path,
        "a.json",
        [_rec("aaaaaaaa-1111-1111-1111-111111111111", name="[ap:ARCH01] taken")],
        "aaaaaaaa-1111-1111-1111-111111111111",
    )
    set_archive_store_for_tests(ArchiveStore(folder_provider=lambda: str(tmp_path)))

    # inject the generator's draws: the archived core first, a fresh core second
    draws = iter(["ARCH01", "ZZZNEW"])

    def _fake_generate(existing_check, tries=40):
        for draw in draws:
            if not existing_check(draw):
                return draw
        raise AssertionError("no draw passed the collision check")

    monkeypatch.setattr(server, "generate_ap_core", _fake_generate)

    core = asyncio.run(
        server._anchor_reserve_ap_core("auto", nodes_by_id={})
    )
    assert core == "ZZZNEW"
    # the archived core was refused by _exists (never returned)
    assert core != "ARCH01"


def test_explicit_archived_core_refuses_with_section_5_6_text(tmp_path):
    _write(
        tmp_path,
        "a.json",
        [_rec("aaaaaaaa-1111-1111-1111-111111111111", name="[ap:ARCH01] taken")],
        "aaaaaaaa-1111-1111-1111-111111111111",
    )
    set_archive_store_for_tests(ArchiveStore(folder_provider=lambda: str(tmp_path)))
    with pytest.raises(server._AnchorOperationError) as excinfo:
        asyncio.run(server._anchor_reserve_ap_core("[ap:ARCH01]", nodes_by_id={}))
    assert excinfo.value.code == "ap_id_collision"
    message = str(excinfo.value)
    assert "ARCH01" in message
    assert "export file" in message
    assert str(tmp_path / "a.json") in message


def test_dormant_skipped_and_superseded_cores_count(tmp_path):
    live_uuid = "dddddddd-1111-1111-1111-111111111111"
    # dormant: its root still lives
    _write(
        tmp_path,
        "dormant.json",
        [_rec(live_uuid, name="[ap:DORMN1] still live")],
        live_uuid,
    )
    # skipped: a whole-vault export carrying an AP
    _write(
        tmp_path,
        "vault.json",
        [_rec("eeeeeeee-1111-1111-1111-111111111111", name="[ap:VAULT1] vaulted")],
        None,
        name=VAULT_ARCHIVE_NAME,
    )
    # active + superseded pair: same root exported twice
    _write(
        tmp_path,
        "older.json",
        [_rec("ffffffff-1111-1111-1111-111111111111", name="[ap:OLDER1] old")],
        "ffffffff-1111-1111-1111-111111111111",
        exported_at="2026-09-01T00:00:00Z",
    )
    _write(
        tmp_path,
        "newer.json",
        [_rec("ffffffff-1111-1111-1111-111111111111", name="[ap:NEWER1] new")],
        "ffffffff-1111-1111-1111-111111111111",
        exported_at="2026-10-02T00:00:00Z",
    )
    set_archive_store_for_tests(ArchiveStore(folder_provider=lambda: str(tmp_path)))
    nodes = _nodes_by_id([_rec(live_uuid, name="[ap:DORMN1] still live")])
    for core in ("DORMN1", "VAULT1", "OLDER1", "NEWER1"):
        with pytest.raises(server._AnchorOperationError) as excinfo:
            asyncio.run(server._anchor_reserve_ap_core(f"[ap:{core}]", nodes_by_id=nodes))
        assert excinfo.value.code == "ap_id_collision", core


def test_no_folder_today_behaviour(tmp_path):
    # no folder configured (an empty directory): allocation behaves as before
    set_archive_store_for_tests(ArchiveStore(folder_provider=lambda: str(tmp_path)))
    nodes = _nodes_by_id([_rec(LIVE_WITH_AP, name="[ap:LIVE11] live one")])
    # a live collision still refuses with today's text
    with pytest.raises(server._AnchorOperationError) as excinfo:
        asyncio.run(server._anchor_reserve_ap_core("[ap:LIVE11]", nodes_by_id=nodes))
    assert excinfo.value.code == "ap_id_collision"
    assert "already present or reserved" in str(excinfo.value)
    # a fresh explicit core succeeds
    core = asyncio.run(server._anchor_reserve_ap_core("FRESH1", nodes_by_id=nodes))
    assert core == "FRESH1"


def test_unreadable_file_contributes_nothing(tmp_path):
    (tmp_path / "broken.json").write_text("{ nope")
    set_archive_store_for_tests(ArchiveStore(folder_provider=lambda: str(tmp_path)))
    # allocation proceeds: the unreadable file's (unparseable) content is
    # simply not consulted (E42)
    core = asyncio.run(server._anchor_reserve_ap_core("ANYTH1", nodes_by_id={}))
    assert core == "ANYTH1"
