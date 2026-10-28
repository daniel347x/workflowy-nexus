"""The archive store (Archive Reads U1, §U1.5 test 2).

Every archive in a test is built with ``build_archive_document`` +
``serialize_archive`` into ``tmp_path``; the store is injected with
``set_archive_store_for_tests`` and restored by the fixture.  No real
export, no network, no Workflowy.
"""

from __future__ import annotations

import json
import os

import pytest

from workflowy_mcp.archive_context import ARCHIVE_RECORD_KEY
from workflowy_mcp.archive_store import (
    FILE_STATE_ACTIVE,
    FILE_STATE_DORMANT,
    FILE_STATE_ORPHANED,
    FILE_STATE_SKIPPED,
    FILE_STATE_SUPERSEDED,
    FILE_STATE_UNREADABLE,
    ArchiveStore,
    _reset_archive_folder_memory_for_tests,
    get_archive_store,
    read_remembered_archive_folder,
    set_archive_store_for_tests,
)
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.coherence.cache_view import CacheView
from workflowy_mcp.node_archive import (
    VAULT_ARCHIVE_NAME,
    build_archive_document,
    parse_archive,
    serialize_archive,
    with_mount_parent,
)

EXPORTED_AT = "2026-10-01T12:00:00Z"
LATER_AT = "2026-10-02T12:00:00Z"
SOURCE_CACHE = {"epoch_number": 1, "epoch_id": "e1", "adopted_sequence": 3}


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


def _archive_bytes(records, node_id=None, name=None, exported_at=EXPORTED_AT):
    document = build_archive_document(
        list(records),
        nest=_nest,
        node_id=node_id,
        name=name,
        description=None,
        exported_at=exported_at,
        source_cache=dict(SOURCE_CACHE),
    )
    return serialize_archive(document).encode("utf-8")


def _live_view(records):
    return CacheView.build(
        epoch_number=7,
        epoch_id="live-epoch",
        adopted_sequence=11,
        nodes_export={"nodes": [dict(r) for r in records]},
    )


def _states(store, live_view):
    overlay = store.overlay_for(live_view)
    files = {entry.name: entry for entry in overlay.files} if overlay else {}
    return overlay, files


@pytest.fixture
def store_env(tmp_path):
    store = ArchiveStore(folder_provider=lambda: str(tmp_path))
    set_archive_store_for_tests(store)
    try:
        yield tmp_path, store
    finally:
        set_archive_store_for_tests(None)
        _reset_archive_folder_memory_for_tests()


# ---------------------------------------------------------------------------
# Listing, laziness, no folder, reload
# ---------------------------------------------------------------------------


def test_listing_skips_dotfiles_temps_and_non_json(store_env, monkeypatch):
    tmp_path, store = store_env
    (tmp_path / "a.json").write_bytes(
        _archive_bytes([_rec("r1", name="root")], node_id="r1")
    )
    (tmp_path / "B.JSON").write_bytes(  # case-insensitive extension
        _archive_bytes([_rec("r2", name="root2")], node_id="r2")
    )
    (tmp_path / ".hidden.json").write_text("{}")
    (tmp_path / "note.txt").write_text("nope")
    (tmp_path / "x.1.234.abcdef.tmp").write_text("{}")
    (tmp_path / "folder.json").mkdir()
    live = _live_view([])
    overlay = store.overlay_for(live)
    names = {entry.name for entry in overlay.files}
    assert names == {"a.json", "B.JSON"}


def test_no_folder_means_no_io_at_all(monkeypatch):
    calls = []

    def _boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("os.scandir must never be called with no folder")

    monkeypatch.setattr("workflowy_mcp.archive_store.os.scandir", _boom)
    store = ArchiveStore(folder_provider=lambda: "")
    # every lookup misses without touching the filesystem
    assert store.overlay_for(_live_view([])) is None
    assert store.all_ap_cores() == {}
    assert store.lookup("12345678-1234-5678-1234-567812345678", "uuid", live_view=_live_view([])) is None
    assert calls == []


def test_laziness_and_reload_on_change(store_env, monkeypatch):
    tmp_path, store = store_env
    fresh = ArchiveStore(folder_provider=lambda: str(tmp_path))

    def _boom(*args, **kwargs):
        raise AssertionError("no file I/O before ensure_loaded")

    # construction alone performs no listing
    monkeypatch.setattr("workflowy_mcp.archive_store.os.scandir", _boom)
    monkeypatch.undo()

    path = tmp_path / "a.json"
    path.write_bytes(_archive_bytes([_rec("r1", name="root")], node_id="r1"))
    live = _live_view([])
    assert fresh.overlay_for(live) is not None
    generation = fresh._store_generation
    # unchanged listing: no reparse, generation stable
    fresh.overlay_for(live)
    assert fresh._store_generation == generation
    # an (mtime_ns, size) change reparses and bumps the generation
    os.utime(path, ns=(2000000000_000000000, 2000000000_000000000))
    fresh.overlay_for(live)
    assert fresh._store_generation == generation + 1
    # a vanished file is dropped
    path.unlink()
    assert fresh.overlay_for(live) is None


# ---------------------------------------------------------------------------
# File states and reasons
# ---------------------------------------------------------------------------


def test_every_file_state_with_reason(store_env):
    tmp_path, store = store_env
    (tmp_path / "active.json").write_bytes(
        _archive_bytes(
            [_rec("ar", name="archived root"), _rec("ac", parent_id="ar")],
            node_id="ar",
        )
    )
    (tmp_path / "dormant.json").write_bytes(
        _archive_bytes([_rec("dr", name="still live")], node_id="dr")
    )
    (tmp_path / "skipped.json").write_bytes(
        _archive_bytes([_rec("vr1"), _rec("vr2")], node_id=None,
                       name=VAULT_ARCHIVE_NAME)
    )
    (tmp_path / "orphan.json").write_bytes(
        _archive_bytes([_rec("or", name="orphan root", parent_id="ghost")],
                       node_id="or")
    )
    (tmp_path / "broken.json").write_text("{not json")

    live = _live_view([_rec("dr", name="still live")])
    overlay, files = _states(store, live)

    assert files["active.json"].state == FILE_STATE_ACTIVE
    assert files["active.json"].visible_count == 2
    assert files["dormant.json"].state == FILE_STATE_DORMANT
    assert "still lives in Workflowy" in files["dormant.json"].reason
    assert files["skipped.json"].state == FILE_STATE_SKIPPED
    assert files["orphan.json"].state == FILE_STATE_ORPHANED
    assert files["orphan.json"].reason == (
        "parent ghost is in neither Workflowy nor an active archive"
    )
    assert files["broken.json"].state == FILE_STATE_UNREADABLE
    assert files["broken.json"].reason
    # the overlay carries only the active/orphaned records
    assert sorted(overlay.by_id) == ["ac", "ar", "or"]


def test_unreadable_folder_state_c(store_env):
    _, store = store_env
    store2 = ArchiveStore(folder_provider=lambda: "/definitely/not/a/real/dir")
    live = _live_view([])
    assert store2.overlay_for(live) is None
    assert store2.folder_unreadable_reason
    assert "does not exist" in store2.folder_unreadable_reason


# ---------------------------------------------------------------------------
# Shadowing (E3 live-wins-per-subtree, E4 newer-wins-per-subtree)
# ---------------------------------------------------------------------------


def test_e3_moved_out_descendant_is_live_wins(store_env):
    tmp_path, store = store_env
    # the archive holds root R with child C (as it was at export time)
    (tmp_path / "sub.json").write_bytes(
        _archive_bytes(
            [_rec("R", name="root"), _rec("C", name="child", parent_id="R")],
            node_id="R",
        )
    )
    # C was moved OUT of R before R was deleted: C is live, R is not
    live = _live_view([_rec("C", name="child", priority=0)])
    overlay, files = _states(store, live)
    assert files["sub.json"].state == FILE_STATE_ACTIVE
    # R visible, its archived copy of C hidden (live wins per subtree);
    # R itself was a top-level export (parent None), so it plants at the top
    assert "R" in overlay.by_id
    assert "C" not in overlay.by_id
    assert overlay.by_id["R"]["parent_id"] is None


def test_e4_newer_archive_wins_per_subtree(store_env):
    tmp_path, store = store_env
    (tmp_path / "old.json").write_bytes(
        _archive_bytes([_rec("R", name="old root")], node_id="R",
                       exported_at=EXPORTED_AT)
    )
    (tmp_path / "new.json").write_bytes(
        _archive_bytes([_rec("R", name="new root")], node_id="R",
                       exported_at=LATER_AT)
    )
    live = _live_view([])
    overlay, files = _states(store, live)
    assert files["new.json"].state == FILE_STATE_ACTIVE
    assert files["old.json"].state == FILE_STATE_SUPERSEDED
    assert "new.json" in files["old.json"].reason
    assert overlay.by_id["R"]["name"] == "📦 new root"


def test_e4_equal_exported_at_file_name_decides(store_env):
    tmp_path, store = store_env
    for name in ("b.json", "a.json"):
        (tmp_path / name).write_bytes(
            _archive_bytes([_rec("R", name=f"root {name}")], node_id="R",
                           exported_at=EXPORTED_AT)
        )
    live = _live_view([])
    overlay, files = _states(store, live)
    # name ascending processed first: a.json wins, b.json superseded
    assert files["a.json"].state == FILE_STATE_ACTIVE
    assert files["b.json"].state == FILE_STATE_SUPERSEDED
    assert overlay.by_id["R"]["name"] == "📦 root a.json"


# ---------------------------------------------------------------------------
# Planting (§4.2.2)
# ---------------------------------------------------------------------------


def test_planting_under_live_parent_with_priority(store_env):
    tmp_path, store = store_env
    live = _live_view(
        [
            _rec("P", name="parent"),
            _rec("k1", parent_id="P", priority=2),
            _rec("k5", parent_id="P", priority=5),
        ]
    )
    (tmp_path / "a.json").write_bytes(
        _archive_bytes(
            [_rec("AR", name="archived", parent_id="P", priority=99)],
            node_id="AR",
        )
    )
    overlay, files = _states(store, live)
    root = overlay.by_id["AR"]
    assert files["a.json"].planted_parent_id == "P"
    assert root["parent_id"] == "P"
    # M = 5 (largest live sibling priority), k = 0 -> 6: after the live kids
    assert root["priority"] == 6


def test_planting_top_level_export_is_not_orphan(store_env):
    tmp_path, store = store_env
    (tmp_path / "t.json").write_bytes(
        _archive_bytes([_rec("TR", name="top root", parent_id=None)],
                       node_id="TR")
    )
    live = _live_view([_rec("x", priority=3)])
    overlay, files = _states(store, live)
    assert files["t.json"].state == FILE_STATE_ACTIVE
    root = overlay.by_id["TR"]
    assert root["parent_id"] is None
    # M = 3 among live top-level records, k = 0 -> 4
    assert root["priority"] == 4


def test_planting_mount_beats_source_parent(store_env):
    tmp_path, store = store_env
    live = _live_view([_rec("M", name="mount target"), _rec("S", name="source")])
    raw = _archive_bytes([_rec("AR", name="archived", parent_id="S")], node_id="AR")
    document = parse_archive(raw, nest=_nest)
    document = with_mount_parent(document, "M")
    (tmp_path / "m.json").write_bytes(serialize_archive(document).encode("utf-8"))
    overlay, files = _states(store, live)
    assert files["m.json"].planted_parent_id == "M"
    assert overlay.by_id["AR"]["parent_id"] == "M"


def test_planting_inside_another_active_archive(store_env):
    tmp_path, store = store_env
    (tmp_path / "outer.json").write_bytes(
        _archive_bytes(
            [_rec("OR", name="outer root"), _rec("OC", name="outer child", parent_id="OR")],
            node_id="OR",
        )
    )
    (tmp_path / "inner.json").write_bytes(
        _archive_bytes([_rec("IR", name="inner", parent_id="OC")], node_id="IR")
    )
    live = _live_view([])
    overlay, files = _states(store, live)
    assert files["inner.json"].state == FILE_STATE_ACTIVE
    assert files["inner.json"].planted_parent_id == "OC"
    assert overlay.by_id["IR"]["parent_id"] == "OC"


def test_e5_mount_cycle_orphans_both_at_top(store_env):
    tmp_path, store = store_env
    a_doc = parse_archive(
        _archive_bytes([_rec("AR", name="a root", parent_id="BR")], node_id="AR"),
        nest=_nest,
    )
    a_doc = with_mount_parent(a_doc, "BR")
    (tmp_path / "a.json").write_bytes(serialize_archive(a_doc).encode("utf-8"))
    b_doc = parse_archive(
        _archive_bytes([_rec("BR", name="b root", parent_id="AR")], node_id="BR"),
        nest=_nest,
    )
    b_doc = with_mount_parent(b_doc, "AR")
    (tmp_path / "b.json").write_bytes(serialize_archive(b_doc).encode("utf-8"))
    live = _live_view([])
    overlay, files = _states(store, live)
    assert files["a.json"].state == FILE_STATE_ORPHANED
    assert files["b.json"].state == FILE_STATE_ORPHANED
    assert "mount cycle with" in files["a.json"].reason
    assert "mount cycle with" in files["b.json"].reason
    # both planted at the top level
    assert overlay.by_id["AR"]["parent_id"] is None
    assert overlay.by_id["BR"]["parent_id"] is None


def test_several_roots_planted_in_order(store_env):
    tmp_path, store = store_env
    # two single-root archives planted under the same live parent: the k
    # ordering runs across files, by own priority then exported_at then name
    (tmp_path / "f1.json").write_bytes(
        _archive_bytes(
            [_rec("F1", name="first", parent_id="P", priority=9)],
            node_id="F1",
        )
    )
    (tmp_path / "f2.json").write_bytes(
        _archive_bytes(
            [_rec("F2", name="second", parent_id="P", priority=0)],
            node_id="F2",
        )
    )
    live = _live_view([_rec("P", name="parent"), _rec("k", parent_id="P", priority=0)])
    overlay, files = _states(store, live)
    # M = 0; ordered by own priority: F2 (0) then F1 (9) -> priorities 1, 2
    assert overlay.by_id["F2"]["priority"] == 1
    assert overlay.by_id["F1"]["priority"] == 2


# ---------------------------------------------------------------------------
# The 📦 overlay record
# ---------------------------------------------------------------------------


def _write_one(store_env, name, records, node_id):
    tmp_path, _store = store_env
    (tmp_path / name).write_bytes(_archive_bytes(records, node_id=node_id))


@pytest.mark.parametrize(
    "raw,expected",
    [
        # a valid AP handle (>= 3 core chars) keeps its token before the mark
        ("[ap:ABCDE1] Title #t", "[ap:ABCDE1] 📦 Title #t"),
        ("🧩 Title", "📦 🧩 Title"),
        ("Title", "📦 Title"),
        ("", "📦"),
        ("⚡[ap:FGHIJ2] T", "⚡[ap:FGHIJ2] 📦 T"),
        # a 1-char core is NOT an AP handle by the ONE grammar ({3,} floor):
        # the whole raw name is the ordinary title
        ("[ap:X] Title #t", "📦 [ap:X] Title #t"),
    ],
)
def test_archive_marked_names(store_env, raw, expected):
    live = _live_view([])
    tmp_path, store = store_env
    (tmp_path / "n.json").write_bytes(
        _archive_bytes([_rec("NR", name=raw)], node_id="NR")
    )
    overlay = store.overlay_for(live)
    assert overlay.by_id["NR"]["name"] == expected


def test_overlay_record_marker_provenance_and_verbatim(store_env):
    tmp_path, store = store_env
    records = [
        _rec("R", name="[ap:K7M2QF] root", note="hello", completedAt=123,
             data={"layoutMode": "bullets"}),
        _rec("C", name="child", parent_id="R", note="kid note", createdAt=5),
    ]
    (tmp_path / "v.json").write_bytes(_archive_bytes(records, node_id="R"))
    live = _live_view([])
    overlay = store.overlay_for(live)
    root = overlay.by_id["R"]
    prov = root[ARCHIVE_RECORD_KEY]
    assert prov["archive_file"] == str(tmp_path / "v.json")
    assert prov["archive_name"] == "[ap:K7M2QF] root"
    assert prov["exported_at"] == EXPORTED_AT
    assert prov["archive_root"] == "R"
    assert prov["state"] == FILE_STATE_ACTIVE
    assert overlay.provenance["C"] == prov
    # every other key copied verbatim (no 📦 in provenance, note untouched)
    assert root["note"] == "hello"
    assert root["completedAt"] == 123
    assert root["data"] == {"layoutMode": "bullets"}
    assert overlay.by_id["C"]["note"] == "kid note"
    assert overlay.by_id["C"][ARCHIVE_RECORD_KEY] == prov
    # the parsed originals are NOT the overlay records (new dict, never r)
    parsed_doc = store._parsed[str(tmp_path / "v.json")]["document"]
    assert overlay.by_id["R"] is not parsed_doc["nodes"][0]
    assert overlay.by_id["R"]["note"] == parsed_doc["nodes"][0]["note"]


def test_overlay_record_freezes_under_deep_freeze(store_env):
    from workflowy_mcp.coherence.cache_view import (
        is_test_deep_freeze_active,
        set_test_deep_freeze,
    )

    tmp_path, store = store_env
    (tmp_path / "f.json").write_bytes(
        _archive_bytes([_rec("R", name="root")], node_id="R")
    )
    overlay = store.overlay_for(_live_view([]))
    previous = is_test_deep_freeze_active()
    set_test_deep_freeze(True)
    try:
        view = overlay.augmented_view
        with pytest.raises(TypeError):
            view.nodes_by_id["R"]["name"] = "mutated"
        with pytest.raises(TypeError):
            view.nodes_export["nodes"][-1]["note"] = "mutated"
    finally:
        set_test_deep_freeze(previous)


# ---------------------------------------------------------------------------
# The all-cores index and lookups
# ---------------------------------------------------------------------------


def test_all_ap_cores_includes_every_readable_state(store_env):
    tmp_path, store = store_env
    (tmp_path / "active.json").write_bytes(
        _archive_bytes([_rec("a1", name="[ap:ACTIV1] x")], node_id="a1")
    )
    (tmp_path / "dormant.json").write_bytes(
        _archive_bytes([_rec("d1", name="[ap:DORMA1] x")], node_id="d1")
    )
    (tmp_path / "skipped.json").write_bytes(
        _archive_bytes([_rec("s1", name="[ap:SKIPE1] x")], node_id=None,
                       name=VAULT_ARCHIVE_NAME)
    )
    (tmp_path / "newer.json").write_bytes(
        _archive_bytes([_rec("n1", name="[ap:NEWER1] x")], node_id="n1",
                       exported_at=LATER_AT)
    )
    (tmp_path / "older.json").write_bytes(
        _archive_bytes([_rec("n1", name="[ap:OLDER1] x")], node_id="n1",
                       exported_at=EXPORTED_AT)
    )
    (tmp_path / "broken.json").write_text("nope")
    live = _live_view([_rec("d1", name="[ap:DORMA1] x")])
    cores = store.all_ap_cores()
    assert set(cores) == {"ACTIV1", "DORMA1", "SKIPE1", "NEWER1", "OLDER1"}
    # unreadable contributes nothing; skipped drops its records but keeps cores
    assert cores["SKIPE1"] == str(tmp_path / "skipped.json")
    store.ensure_loaded()
    assert store._parsed[str(tmp_path / "skipped.json")]["document"]["nodes"] == []


def test_lookup_uuid_exact_prefix_and_ap(store_env):
    tmp_path, store = store_env
    full = "11111111-1111-1111-1111-111111111111"
    (tmp_path / "a.json").write_bytes(
        _archive_bytes(
            [
                _rec(full, name="[ap:LOOKU1] one"),
                _rec("22222222-2222-2222-2222-222222222222",
                     name="two", parent_id=full),
            ],
            node_id=full,
        )
    )
    live = _live_view([])
    assert store.lookup(full, "uuid", live_view=live) == full
    assert store.lookup(full.replace("-", ""), "uuid", live_view=live) == full
    assert store.lookup("11111111", "uuid", live_view=live) == full
    # ambiguous prefix -> None (never a guess)
    (tmp_path / "b.json").write_bytes(
        _archive_bytes([_rec("11111111-2222-3333-4444-555555555555", name="twin")],
                       node_id="11111111-2222-3333-4444-555555555555")
    )
    live2 = _live_view([])
    assert store.lookup("11111111", "uuid", live_view=live2) is None
    assert store.lookup(full, "uuid", live_view=live2) == full  # exact still wins
    # exact_only rejects a prefix
    store2 = ArchiveStore(folder_provider=lambda: str(tmp_path))
    (tmp_path / "b.json").unlink()
    assert store2.lookup("11111111", "uuid", live_view=_live_view([]), exact_only=True) is None
    assert store2.lookup("11111111", "uuid", live_view=_live_view([])) == full
    # AP kind: the core in the visible ap_index
    assert store2.lookup("[ap:looku1]", "ap", live_view=_live_view([])) == [
        full
    ]
    assert store2.lookup("NOPE12", "ap", live_view=_live_view([])) is None
    assert store2.lookup("11111111", "uuid", live_view=None) is None


# ---------------------------------------------------------------------------
# The one production accessor, the test seam, the remembered folder reader
# ---------------------------------------------------------------------------


def test_get_archive_store_singleton_and_test_seam():
    first = get_archive_store()
    assert get_archive_store() is first
    store = ArchiveStore(folder_provider=lambda: "")
    set_archive_store_for_tests(store)
    try:
        assert get_archive_store() is store
    finally:
        set_archive_store_for_tests(None)
    # the reset clears the seam: a fresh production store is created on
    # the next access (never the test store)
    assert get_archive_store() is not store


def test_read_remembered_archive_folder(tmp_path, monkeypatch):
    from workflowy_mcp import archive_store as archive_store_module

    monkeypatch.setattr(
        archive_store_module,
        "read_remembered_archive_folder",
        archive_store_module.read_remembered_archive_folder,
    )
    monkeypatch.setattr(
        "workflowy_mcp.config.get_runtime_base_dir", lambda: tmp_path
    )
    _reset_archive_folder_memory_for_tests()
    # missing file -> ""
    assert read_remembered_archive_folder() == ""
    # a written file is read once and then served from memory
    (tmp_path / "archive_folder.json").write_text(
        json.dumps({"archive_folder": "/some/folder", "learned_at": "now"})
    )
    _reset_archive_folder_memory_for_tests()
    assert read_remembered_archive_folder() == "/some/folder"
    # the in-memory value sticks even after the file changes (once per process)
    (tmp_path / "archive_folder.json").write_text(
        json.dumps({"archive_folder": "/other", "learned_at": "later"})
    )
    assert read_remembered_archive_folder() == "/some/folder"
    # malformed file -> ""
    _reset_archive_folder_memory_for_tests()
    (tmp_path / "archive_folder.json").write_text("{oops")
    assert read_remembered_archive_folder() == ""
