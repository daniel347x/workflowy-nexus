"""FILE-NOTE HEADER FRESHNESS (Dan 2026) — regression tests.

Contract (RECON [ap:56G03E], SkyView [ap:H8RV5Z]): the FILE node's note
header (``Path:`` / ``LINE COUNT:`` / optional ``Source-SHA1:``) was written
once at mapping time and never brought up to date —
``refresh_file_node_beacons`` copied only ``file_map["children"]`` and reused
the old note, so every refresh left a stale ``LINE COUNT:`` behind. The fix:
after a successful ``map_codebase``, an ordinary eligible FILE root gets
exactly ONE note-only header update BEFORE the child reconcile — the fresh
LINE COUNT merged into the existing note by the pure merger
``_merge_file_note_header`` (owned header region only, suffix preserved
byte-for-byte, legacy ``Source-SHA1:`` dropped, deferred portability
Path-segment migration rides the same merged write).

What these tests pin:
1. grow AND shrink between refreshes update ``LINE COUNT:`` to the file on
   disk — asserting the ACTUAL update request, not just the seeded source —
   and an unchanged second refresh writes nothing;
2. every language dispatch the mapper handles, plus a generic fallback
   extension, an empty file (count 0) and an unterminated final line;
3. merge-boundary behaviour of the pure merger: Path-only notes gain a
   count, absent/stale/current/duplicate count lines are handled, legacy
   SHA is removed from the owned region only, user prose / blank lines /
   ``---`` / metadata-lookalike body lines are preserved verbatim, terminal
   newline and CRLF shape survive, and an absolute standalone Path and a
   Root-based legacy absolute Path both migrate to a segment in ONE
   preserved note;
4. the FILE-vs-folder classifiers still see a FILE after the refresh —
   including count 0 and an extension-less filename, where ``LINE COUNT``
   itself is load-bearing — and folder verdicts are untouched;
5. folder refresh brings existing FILE headers up to date through the REAL
   per-file method (one write per changed file, suffixes preserved, second
   pass a no-op), while new-file creation keeps its fresh-count/no-SHA note
   and intermediate folders keep their count-less notes;
6. purity and failures: dry_run never writes; ``source_path_override``
   split targets and saved MD_PATH dummy blocks are untouched; a Root-first
   note gets no FILE header; a requested cancellation skips the header
   write; a failed header write fails the refresh loudly; and the armed
   delete threshold still blocks symbol deletes while the sanctioned
   pre-refusal header update proceeds.

Harness pattern after ``tests/test_carto_dry_run_byte_purity.py`` and
``tests/test_s06_folder_refresh_cleanup.py``: fake the Workflowy client
surface, no live account; the fake ``update_node`` persists the FILE note so
a second run can prove the no-op.

Pre-fix status (would this test fail against pre-fix code?): tests 1, 2,
5 (existing-file halves), the merged-note assertions of 3 that require a
count line, 4's refreshed-note pins and the threshold test's header-write
assertion all exercise the new header write / merger and fail against the
pre-fix tree (the write never happened; the note stayed stale). The pure
merger tests and the special-target exemption tests prove the fix's own
contract and are trivially pre-fix-red only insofar as the merger did not
exist (ImportError). Stash-verified per test below where noted.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from workflowy_mcp.client import api_client_nexus, workflowy_move_reconcile
from workflowy_mcp.client.api_client_nexus import (
    WorkFlowyClientNexus,
    _merge_file_note_header,
)
from workflowy_mcp.models import NetworkError
from workflowy_mcp.server import (
    _cartographer_header_info,
    _is_file_like_payload_node,
    _is_folder_like_payload_node,
)

FILE_ID = "FILE"


# ---------------------------------------------------------------------------
# Shared offline per-file refresh harness (dry-run-byte-purity pattern)
# ---------------------------------------------------------------------------


def _install_file_refresh_fakes(
    monkeypatch,
    tmp_path: Path,
    *,
    nodes: list[dict],
    update_raises: bool = False,
    use_real_reconcile: bool = False,
) -> dict:
    observed: dict = {
        "updates": [],
        "creates": [],
        "deletes": [],
        "moves": [],
        "source_jsons": [],
    }

    def _find(node_id):
        for n in nodes:
            if str(n.get("id")) == str(node_id):
                return n
        return None

    async def fake_export_nodes_impl(_client, node_id=None, **_kwargs):
        assert node_id is None or _find(node_id) is not None
        return {"nodes": [dict(n) for n in nodes]}

    async def fake_update_node(_self, node_id, req):
        if update_raises:
            raise RuntimeError("simulated note update failure")
        observed["updates"].append(
            {
                "node_id": str(node_id),
                "name": req.name,
                "note": req.note,
                "layoutMode": req.layoutMode,
            }
        )
        target = _find(node_id)
        if target is not None and req.note is not None:
            target["note"] = req.note

    async def fail_create(_self, req, _internal_call=False, **_kw):  # pragma: no cover
        raise AssertionError("unexpected create_node in header-freshness test")

    async def fail_delete(_self, node_id):  # pragma: no cover
        raise AssertionError("unexpected delete_node in header-freshness test")

    async def fail_move(_self, node_id, parent, position="top"):  # pragma: no cover
        raise AssertionError("unexpected move_node in header-freshness test")

    async def fake_reconcile_tree(**kwargs):
        observed["source_jsons"].append(kwargs["source_json"])
        observed["reconcile_dry_run"] = kwargs["dry_run"]
        return {"success": True, "planned": True, "dry_run": kwargs["dry_run"]}

    async def fake_list_nodes(_self, request):
        parent = str(getattr(request, "parentId", "") or "")
        kids = [dict(n) for n in nodes if str(n.get("parent_id") or "") == parent]
        return [SimpleNamespace(model_dump=lambda n=node: dict(n)) for node in kids], None

    monkeypatch.setattr(api_client_nexus, "export_nodes_impl", fake_export_nodes_impl)
    monkeypatch.setattr(WorkFlowyClientNexus, "update_node", fake_update_node)
    monkeypatch.setattr(WorkFlowyClientNexus, "create_node", fail_create)
    monkeypatch.setattr(WorkFlowyClientNexus, "delete_node", fail_delete)
    monkeypatch.setattr(WorkFlowyClientNexus, "move_node", fail_move)
    monkeypatch.setattr(WorkFlowyClientNexus, "list_nodes", fake_list_nodes)
    if not use_real_reconcile:
        monkeypatch.setattr(workflowy_move_reconcile, "reconcile_tree", fake_reconcile_tree)
    monkeypatch.setattr(
        WorkFlowyClientNexus,
        "_mark_nodes_export_dirty",
        lambda _self, _ids: None,
    )
    monkeypatch.setenv("CARTO_JOB_DIR", str(tmp_path))
    monkeypatch.delenv("CARTO_JOB_FILE", raising=False)
    return observed


def _run_file_refresh(
    monkeypatch,
    tmp_path: Path,
    *,
    source_bytes: bytes,
    note: str,
    filename: str = "fixture.py",
    dry_run: bool = False,
    source_path_override: str | None = None,
    cancel_callback=None,
    update_raises: bool = False,
    extra_nodes: list[dict] | None = None,
):
    source_path = tmp_path / filename
    source_path.write_bytes(source_bytes)
    file_node = {"id": FILE_ID, "name": filename, "note": note}
    nodes = [file_node, *(dict(n) for n in (extra_nodes or []))]
    observed = _install_file_refresh_fakes(
        monkeypatch,
        tmp_path,
        nodes=nodes,
        update_raises=update_raises,
    )
    client = object.__new__(WorkFlowyClientNexus)
    result = asyncio.run(
        client.refresh_file_node_beacons(
            FILE_ID,
            dry_run=dry_run,
            source_path_override=source_path_override,
            cancel_callback=cancel_callback,
        )
    )
    return result, file_node, observed


def _line_count_of(note: str) -> int:
    for line in note.splitlines():
        if line.startswith("LINE COUNT:"):
            return int(line.split(":", 1)[1].strip())
    raise AssertionError(f"no LINE COUNT line in note: {note!r}")


# ---------------------------------------------------------------------------
# 1 · grow AND shrink between refreshes; unchanged second refresh is a no-op
# ---------------------------------------------------------------------------


def test_grow_and_shrink_update_line_count_and_preserve_suffix(monkeypatch, tmp_path):
    stale_note = (
        f"Path: {tmp_path / 'fixture.py'}\n"
        "LINE COUNT: 999\n"
        "Source-SHA1: deadbeef\n"
        "\n"
        "User notes below the header.\n"
        "More prose with   spacing.\n"
    )
    source_bytes = b"one\ntwo\n"

    # Run 1: stale count + stale SHA + user suffix → fresh count, SHA dropped,
    # suffix byte-for-byte, exactly one note-only update.
    result, file_node, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        source_bytes=source_bytes,
        note=stale_note,
    )
    expected_run1 = (
        f"Path: {tmp_path / 'fixture.py'}\n"
        "LINE COUNT: 2\n"
        "\n"
        "User notes below the header.\n"
        "More prose with   spacing.\n"
    )
    assert result["success"] is True
    assert len(observed["updates"]) == 1
    upd = observed["updates"][0]
    assert upd["node_id"] == FILE_ID
    assert upd["name"] is None and upd["layoutMode"] is None  # note-only
    assert upd["note"] == expected_run1
    assert file_node["note"] == expected_run1
    assert _line_count_of(file_node["note"]) == 2
    assert "Source-SHA1:" not in file_node["note"]
    # The header write is reflected in the refresh's own stats.
    assert result["nodes_updated"] == 1
    assert result["api_calls"] == 1

    # Run 2: the file GROWS → count follows.
    (tmp_path / "fixture.py").write_bytes(b"1\n2\n3\n4\n5\n")
    result2, file_node, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        source_bytes=(tmp_path / "fixture.py").read_bytes(),
        note=file_node["note"],
    )
    assert file_node["note"] == expected_run1.replace("LINE COUNT: 2", "LINE COUNT: 5")
    assert len(observed["updates"]) == 1

    # Run 3: the file SHRINKS below its original size → count follows.
    result3, file_node, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        source_bytes=b"only\n",
        note=file_node["note"],
    )
    assert _line_count_of(file_node["note"]) == 1
    assert "User notes below the header." in file_node["note"]

    # Run 4: unchanged file → merged header identical → ZERO note writes.
    result4, file_node, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        source_bytes=b"only\n",
        note=file_node["note"],
    )
    assert observed["updates"] == []
    assert result4["nodes_updated"] == 0


# ---------------------------------------------------------------------------
# 2 · every language dispatch the mapper handles
# ---------------------------------------------------------------------------


_LANGUAGE_CASES = [
    ("fixture.py", b"def f():\n    return 1\n", 2),
    ("fixture.js", b"const x = 1;\nfunction f() { return x; }\n", 2),
    ("fixture.jsx", b"const y = 2;\n", 1),
    ("fixture.ts", b"const z: number = 1;\nexport function f(): number { return z; }\n", 2),
    ("fixture.tsx", b"const w = 1;\n", 1),
    ("fixture.md", b"# Title\n\nbody\n", 3),
    ("fixture.sql", b"SELECT 1;\nSELECT 2;\n", 2),
    ("fixture.sh", b"#!/bin/sh\necho hi\n", 2),
    ("fixture.yml", b"key: value\nother: 2\n", 2),
    ("fixture.yaml", b"k: v\n", 1),
    ("fixture.vue", b"<template>\n  <div/>\n</template>\n", 3),
    ("fixture.text", b"a\nb\nc\n", 3),  # generic fallback dispatch
    ("fixture.markdown", b"# Md\n\ntext\n", 3),  # generic fallback dispatch
]


@pytest.mark.parametrize(
    "filename,source_bytes,expected_count",
    _LANGUAGE_CASES,
    ids=[c[0] for c in _LANGUAGE_CASES],
)
def test_every_language_dispatch_refreshes_line_count(
    monkeypatch, tmp_path, filename, source_bytes, expected_count
):
    stale_note = f"Path: {tmp_path / filename}\nLINE COUNT: 999\n"
    result, file_node, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        source_bytes=source_bytes,
        note=stale_note,
        filename=filename,
    )
    assert result["success"] is True
    assert len(observed["updates"]) == 1
    assert observed["updates"][0]["note"] == (
        f"Path: {tmp_path / filename}\nLINE COUNT: {expected_count}\n"
    )
    assert _line_count_of(file_node["note"]) == expected_count


def test_empty_source_refreshes_to_count_zero_and_stays_a_file(monkeypatch, tmp_path):
    stale_note = f"Path: {tmp_path / 'fixture.text'}\nLINE COUNT: 7\nSource-SHA1: old\n"
    result, file_node, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        source_bytes=b"",
        note=stale_note,
        filename="fixture.text",
    )
    assert result["success"] is True
    assert len(observed["updates"]) == 1
    assert file_node["note"] == f"Path: {tmp_path / 'fixture.text'}\nLINE COUNT: 0\n"
    node = {"name": "fixture.text", "note": file_node["note"]}
    assert _is_file_like_payload_node(node) is True  # count 0 is still a count


def test_unterminated_final_line_counts(monkeypatch, tmp_path):
    stale_note = f"Path: {tmp_path / 'fixture.text'}\nLINE COUNT: 99\n"
    result, file_node, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        source_bytes=b"a\nb",  # no trailing newline → 2 lines
        note=stale_note,
        filename="fixture.text",
    )
    assert result["success"] is True
    assert len(observed["updates"]) == 1
    assert _line_count_of(file_node["note"]) == 2


# ---------------------------------------------------------------------------
# 3 · the pure merger: owned region, suffix preservation, refusals
# ---------------------------------------------------------------------------


def test_merge_path_only_note_gains_count():
    assert _merge_file_note_header("Path: server.py", 5) == (
        "Path: server.py\nLINE COUNT: 5"
    )


def test_merge_replaces_stale_count_drops_sha_and_preserves_suffix():
    old = (
        "Path: a.py\n"
        "LINE COUNT: 719\n"
        "Source-SHA1: abc\n"
        "Source-SHA1: def\n"
        "\n"
        "Body line.\n"
    )
    assert _merge_file_note_header(old, 5095) == (
        "Path: a.py\nLINE COUNT: 5095\n\nBody line.\n"
    )


def test_merge_is_idempotent_when_count_current():
    old = "Path: a.py\nLINE COUNT: 3"
    assert _merge_file_note_header(old, 3) == old


def test_merge_deduplicates_owned_count_lines():
    old = "Path: a.py\nLINE COUNT: 1\nLINE COUNT: 2\nx\n"
    assert _merge_file_note_header(old, 9) == "Path: a.py\nLINE COUNT: 9\nx\n"


def test_merge_body_lookalikes_and_user_content_untouched():
    old = (
        "Path: a.py\n"
        "LINE COUNT: 1\n"
        "\n"
        "prose quoting LINE COUNT: 999\n"
        "---\n"
        "Source-SHA1: lives in the body, stays\n"
        "Ünïcödé — “quoted” prose\n"
        "Filebase-ish metadata: keep me\n"
    )
    assert _merge_file_note_header(old, 4) == (
        "Path: a.py\n"
        "LINE COUNT: 4\n"
        "\n"
        "prose quoting LINE COUNT: 999\n"
        "---\n"
        "Source-SHA1: lives in the body, stays\n"
        "Ünïcödé — “quoted” prose\n"
        "Filebase-ish metadata: keep me\n"
    )


def test_merge_preserves_crlf_shape():
    old = "Path: a.py\r\nLINE COUNT: 1\r\n\r\nbody\r\n"
    assert _merge_file_note_header(old, 2) == (
        "Path: a.py\r\nLINE COUNT: 2\r\n\r\nbody\r\n"
    )


def test_merge_preserves_missing_terminal_newline():
    assert _merge_file_note_header("Path: a.py", 2) == "Path: a.py\nLINE COUNT: 2"
    assert _merge_file_note_header("Path: a.py\nLINE COUNT: 1", 2) == (
        "Path: a.py\nLINE COUNT: 2"
    )


def test_merge_trailing_newline_survives_header_only_note():
    old = "Path: a.py\nLINE COUNT: 1\n"
    assert _merge_file_note_header(old, 2) == "Path: a.py\nLINE COUNT: 2\n"


def test_merge_applies_deferred_segment_migration_in_one_preserved_note():
    # A Root-based legacy ABSOLUTE Path migrates to its basename WITHOUT the
    # old writer's lossy whole-note split/join: body spacing and the terminal
    # newline survive.
    old = "Path: /old/absolute/a.py\nLINE COUNT: 1\n\nbody keeps   spacing\n"
    assert _merge_file_note_header(old, 2, pending_path_segment="a.py") == (
        "Path: a.py\nLINE COUNT: 2\n\nbody keeps   spacing\n"
    )


def test_merge_segment_already_current_only_refreshes_count():
    old = "Path: a.py\nLINE COUNT: 1\nx\n"
    assert _merge_file_note_header(old, 2, pending_path_segment="a.py") == (
        "Path: a.py\nLINE COUNT: 2\nx\n"
    )


def test_merge_refuses_root_first_note():
    assert _merge_file_note_header("Root: /repo\nLINE COUNT: 1\n", 3) is None


def test_merge_refuses_saved_dummy_md_path_note():
    dummy = "MD_PATH: x.md\nPath: /abs/subset.md\n---\nbody\n"
    assert _merge_file_note_header(dummy, 3) is None


def test_merge_refuses_ordinary_non_path_first_note():
    assert _merge_file_note_header("just some note\nPath: mentioned later\n", 3) is None
    assert _merge_file_note_header("", 3) is None


def test_merge_refuses_invalid_fresh_counts():
    assert _merge_file_note_header("Path: a.py", -1) is None
    assert _merge_file_note_header("Path: a.py", True) is None  # bool is not a count
    assert _merge_file_note_header("Path: a.py", "3") is None


# ---------------------------------------------------------------------------
# 4 · the FILE-vs-folder classifiers still see a FILE after the refresh
# ---------------------------------------------------------------------------


def test_classifier_file_verdict_survives_refresh_count_zero_included():
    for count in ("0", "1", "5"):
        refreshed = f"Path: x.py\nLINE COUNT: {count}"
        node = {"name": "🐍 x.py", "note": refreshed}
        first_header, has_count = _cartographer_header_info(refreshed)
        assert first_header == "Path: x.py"
        assert has_count is True
        assert _is_file_like_payload_node(node) is True
        assert _is_folder_like_payload_node(node) is False


def test_classifier_extensionless_name_still_file_because_count_is_load_bearing():
    # A Path whose value has NO filename extension: the count is the ONLY
    # thing telling FILE from FOLDER — the refresh must keep it present.
    counted = {"name": "LICENSE", "note": "Path: LICENSE\nLINE COUNT: 5"}
    assert _is_file_like_payload_node(counted) is True
    assert _is_folder_like_payload_node(counted) is False
    bare = {"name": "LICENSE", "note": "Path: LICENSE"}
    assert _is_file_like_payload_node(bare) is False
    assert _is_folder_like_payload_node(bare) is True


def test_classifier_folder_verdicts_untouched_by_the_change():
    root_node = {"name": "🗺️ repo", "note": "Root: /repo\n"}
    assert _is_folder_like_payload_node(root_node) is True
    assert _is_file_like_payload_node(root_node) is False
    ext_folder = {"name": "📂 src", "note": "Path: src"}  # count-less, no extension
    assert _is_folder_like_payload_node(ext_folder) is True
    assert _is_file_like_payload_node(ext_folder) is False


# ---------------------------------------------------------------------------
# 5 · purity and failure semantics
# ---------------------------------------------------------------------------


def test_dry_run_never_writes_the_header(monkeypatch, tmp_path):
    stale_note = f"Path: {tmp_path / 'fixture.py'}\nLINE COUNT: 999\n"
    result, file_node, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        source_bytes=b"a\nb\nc\n",
        note=stale_note,
        dry_run=True,
    )
    assert result["success"] is True
    assert observed["updates"] == []
    assert file_node["note"] == stale_note  # untouched, as before the fix


def test_source_path_override_split_target_note_untouched(monkeypatch, tmp_path):
    # F12+4 temporary-file regime: the node is an ordinary note (no Path:
    # header) being split from a throwaway .md — it must never acquire a
    # FILE header.
    ordinary_note = "An ordinary note\nwith prose below.\n"
    result, file_node, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        source_bytes=b"# throwaway\n\ntext\n",
        note=ordinary_note,
        filename="throwaway.md",
        source_path_override=str(tmp_path / "throwaway.md"),
    )
    assert result["success"] is True
    assert observed["updates"] == []
    assert file_node["note"] == ordinary_note


def test_saved_dummy_md_path_note_untouched_byte_for_byte(monkeypatch, tmp_path):
    dummy_note = (
        "MD_PATH:\n"
        f"Path: {tmp_path / 'fixture.md'}\n"
        "---\n"
        "dummy block body\n"
    )
    result, file_node, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        source_bytes=b"# saved\n\ngenerated file\n",
        note=dummy_note,
        filename="fixture.md",
    )
    assert result["success"] is True
    assert observed["updates"] == []
    assert file_node["note"] == dummy_note  # byte-for-byte unchanged


def test_root_first_note_gets_no_file_header(monkeypatch, tmp_path):
    root_note = f"Root: {tmp_path / 'fixture.py'}\nLINE COUNT: 999\n"
    result, file_node, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        source_bytes=b"a\nb\n",
        note=root_note,
    )
    assert result["success"] is True
    assert observed["updates"] == []
    assert file_node["note"] == root_note  # Root stays Root, count untouched


def test_requested_cancellation_skips_the_header_write(monkeypatch, tmp_path):
    stale_note = f"Path: {tmp_path / 'fixture.py'}\nLINE COUNT: 999\n"
    result, file_node, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        source_bytes=b"a\nb\n",
        note=stale_note,
        cancel_callback=lambda: True,
    )
    assert result["success"] is True
    assert observed["updates"] == []


def test_failed_header_write_fails_the_refresh_loudly(monkeypatch, tmp_path):
    stale_note = f"Path: {tmp_path / 'fixture.py'}\nLINE COUNT: 999\n"
    with pytest.raises(NetworkError):
        _run_file_refresh(
            monkeypatch,
            tmp_path,
            source_bytes=b"a\nb\n",
            note=stale_note,
            update_raises=True,
        )


# ---------------------------------------------------------------------------
# 6 · the armed delete threshold still blocks symbol deletes while the
#     sanctioned pre-refusal header update proceeds (REAL reconcile_tree)
# ---------------------------------------------------------------------------


def test_delete_threshold_still_blocks_while_header_updates(monkeypatch, tmp_path):
    source_path = tmp_path / "fixture.py"
    source_path.write_bytes(b"# accidentally blanked - the symbols are gone\n")
    file_node = {
        "id": FILE_ID,
        "name": "🐍 fixture.py",
        "note": f"Path: {source_path}\nLINE COUNT: 999\n",
    }
    children = [
        {
            "id": f"S{i}",
            "name": f"ƒ symbol_{i}()",
            "note": "",
            "parent_id": FILE_ID,
            "priority": i,
        }
        for i in range(5)
    ]
    nodes = [file_node, *children]
    observed = _install_file_refresh_fakes(
        monkeypatch,
        tmp_path,
        nodes=nodes,
        use_real_reconcile=True,
    )
    # Recording delete/move for the REAL reconcile engine (fail sentinels would
    # fire on the forced run's legitimate deletes).
    async def rec_delete(_self, node_id):
        observed["deletes"].append(str(node_id))
        return True

    async def rec_move(_self, node_id, parent, position="top"):
        observed["moves"].append((str(node_id), str(parent), position))

    monkeypatch.setattr(WorkFlowyClientNexus, "delete_node", rec_delete)
    monkeypatch.setattr(WorkFlowyClientNexus, "move_node", rec_move)

    governance = SimpleNamespace(
        carto_folder_file_delete_threshold=100,
        carto_reconcile_delete_threshold=2,
    )
    monkeypatch.setattr(
        api_client_nexus,
        "get_server_config",
        lambda **_kw: SimpleNamespace(governance=governance),
        raising=False,
    )

    client = object.__new__(WorkFlowyClientNexus)

    # Unforced: 5 unmatched roots > threshold 2 → the DELETE phase aborts,
    # but the header update (which precedes the reconcile) already happened.
    result = asyncio.run(client.refresh_file_node_beacons(FILE_ID, dry_run=False))
    assert [u["node_id"] for u in observed["updates"]] == [FILE_ID]
    assert observed["deletes"] == []
    assert (result.get("nodes_deleted") or 0) == 0
    assert _line_count_of(file_node["note"]) == 1  # header is fresh regardless
    assert source_path.read_bytes() == b"# accidentally blanked - the symbols are gone\n"

    # Forced: the same plan proceeds and deletes exactly the 5 roots; the
    # header write does NOT re-fire (the note is already fresh).
    result_forced = asyncio.run(
        client.refresh_file_node_beacons(FILE_ID, dry_run=False, force=True)
    )
    assert sorted(observed["deletes"]) == ["S0", "S1", "S2", "S3", "S4"]
    assert (result_forced.get("nodes_deleted") or 0) == 5
    # The header write does NOT re-fire (the note is already fresh).
    assert [u["node_id"] for u in observed["updates"]] == [FILE_ID]


# ---------------------------------------------------------------------------
# 7 · folder refresh through the REAL per-file method (do NOT mock it away)
# ---------------------------------------------------------------------------


def _install_folder_refresh_fakes(monkeypatch, tmp_path: Path, *, nodes, resolve_table):
    observed: dict = {
        "updates": [],
        "creates": [],
        "deletes": [],
        "moves": [],
        "source_jsons": [],
    }

    def _find(node_id):
        for n in nodes:
            if str(n.get("id")) == str(node_id):
                return n
        return None

    async def fake_export_nodes_impl(_client, node_id=None, **_kwargs):
        return {"nodes": [dict(n) for n in nodes]}

    def fake_resolve(*, node_id, nodes_by_id):
        return {"abs_path": resolve_table.get(str(node_id), ""), "root_abs": None}

    async def fake_update_node(_self, node_id, req):
        observed["updates"].append(
            {
                "node_id": str(node_id),
                "name": req.name,
                "note": req.note,
                "layoutMode": req.layoutMode,
            }
        )
        target = _find(node_id)
        if target is not None and req.note is not None:
            target["note"] = req.note

    async def fake_create_node(_self, req, _internal_call=False, **_kw):
        created_id = f"NEW-{len(observed['creates']) + 1}"
        observed["creates"].append(
            {
                "id": created_id,
                "name": req.name,
                "parent_id": req.parent_id,
                "note": req.note,
            }
        )
        return SimpleNamespace(id=created_id)

    async def fail_delete(_self, node_id):  # pragma: no cover
        raise AssertionError("unexpected delete_node in folder header test")

    async def fail_move(_self, node_id, parent, position="top"):  # pragma: no cover
        raise AssertionError("unexpected move_node in folder header test")

    async def fake_reconcile_tree(**kwargs):
        observed["source_jsons"].append(kwargs["source_json"])
        return {"success": True, "planned": True, "dry_run": kwargs["dry_run"]}

    monkeypatch.setattr(api_client_nexus, "export_nodes_impl", fake_export_nodes_impl)
    monkeypatch.setattr(
        api_client_nexus,
        "resolve_cartographer_path_from_node",
        fake_resolve,
    )
    monkeypatch.setattr(WorkFlowyClientNexus, "update_node", fake_update_node)
    monkeypatch.setattr(WorkFlowyClientNexus, "create_node", fake_create_node)
    monkeypatch.setattr(WorkFlowyClientNexus, "delete_node", fail_delete)
    monkeypatch.setattr(WorkFlowyClientNexus, "move_node", fail_move)
    monkeypatch.setattr(workflowy_move_reconcile, "reconcile_tree", fake_reconcile_tree)
    monkeypatch.setattr(
        WorkFlowyClientNexus,
        "_mark_nodes_export_dirty",
        lambda _self, _ids: None,
    )
    monkeypatch.setenv("CARTO_JOB_DIR", str(tmp_path))
    monkeypatch.delenv("CARTO_JOB_FILE", raising=False)
    return observed


def test_folder_refresh_updates_existing_file_headers_once_and_preserves_suffixes(
    monkeypatch, tmp_path
):
    f1 = tmp_path / "fixture_a.py"
    f1.write_bytes(b"def a():\n    return 1\n\ndef a2():\n    return 2\n")  # 5 lines
    f2 = tmp_path / "fixture_b.py"
    f2.write_bytes(b"only\n")  # 1 line (shrank since the last refresh)

    folder = {"id": "FOLDER", "name": "📂 folder", "note": f"Path: {tmp_path}"}
    n1 = {
        "id": "F1",
        "name": "🐍 fixture_a.py",
        "note": (
            f"Path: {f1}\nLINE COUNT: 999\nSource-SHA1: zzz\n\nsuffix A stays\n"
        ),
        "parent_id": "FOLDER",
    }
    n2 = {
        "id": "F2",
        "name": "🐍 fixture_b.py",
        "note": f"Path: {f2}\nLINE COUNT: 500\n\nsuffix B stays\n",
        "parent_id": "FOLDER",
    }
    nodes = [folder, n1, n2]
    resolve_table = {"FOLDER": str(tmp_path), "F1": str(f1), "F2": str(f2)}
    observed = _install_folder_refresh_fakes(
        monkeypatch, tmp_path, nodes=nodes, resolve_table=resolve_table
    )

    client = object.__new__(WorkFlowyClientNexus)
    result = asyncio.run(client.refresh_folder_cartographer_sync("FOLDER"))

    assert result["success"] is True
    assert result["refreshed_file_nodes"] == 2
    # Exactly one header write per changed FILE, through the real per-file
    # method, each preserving its suffix byte-for-byte.
    assert [u["node_id"] for u in observed["updates"]] == ["F1", "F2"]
    assert n1["note"] == f"Path: {f1}\nLINE COUNT: 5\n\nsuffix A stays\n"
    assert "Source-SHA1:" not in n1["note"]
    assert n2["note"] == f"Path: {f2}\nLINE COUNT: 1\n\nsuffix B stays\n"

    # Second folder pass: nothing changed → zero NEW note writes.
    updates_after_run1 = len(observed["updates"])
    result2 = asyncio.run(client.refresh_folder_cartographer_sync("FOLDER"))
    assert result2["success"] is True
    assert len(observed["updates"]) == updates_after_run1


def test_folder_create_path_new_files_fresh_count_no_sha_folders_countless(
    monkeypatch, tmp_path
):
    (tmp_path / "new.py").write_bytes(b"def new():\n    return 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.py").write_bytes(b"def inner():\n    return 2\n")

    folder = {"id": "FOLDER", "name": "📂 folder", "note": f"Path: {tmp_path}"}
    nodes = [folder]
    resolve_table = {"FOLDER": str(tmp_path)}
    observed = _install_folder_refresh_fakes(
        monkeypatch, tmp_path, nodes=nodes, resolve_table=resolve_table
    )

    client = object.__new__(WorkFlowyClientNexus)
    result = asyncio.run(client.refresh_folder_cartographer_sync("FOLDER"))

    assert result["success"] is True
    created_names = [c["name"] for c in observed["creates"]]
    assert "🐍 new.py" in created_names
    assert any(name.startswith("ƒ new(") for name in created_names)
    assert "📂 sub" in created_names
    assert "🐍 inner.py" in created_names
    assert any(name.startswith("ƒ inner(") for name in created_names)

    # New FILE nodes are born with a fresh count and no SHA; intermediate
    # folders keep their count-less note (existing create-path policy).
    new_file = next(c for c in observed["creates"] if c["name"] == "🐍 new.py")
    assert "LINE COUNT: 2" in new_file["note"]
    assert "Source-SHA1:" not in new_file["note"]
    sub_folder = next(c for c in observed["creates"] if c["name"] == "📂 sub")
    assert "LINE COUNT:" not in sub_folder["note"]
    inner_file = next(c for c in observed["creates"] if c["name"] == "🐍 inner.py")
    assert "LINE COUNT: 2" in inner_file["note"]
    assert inner_file["parent_id"] == sub_folder["id"]
    assert result["new_files_created"] == 2
    assert result["new_folders_created"] == 1
