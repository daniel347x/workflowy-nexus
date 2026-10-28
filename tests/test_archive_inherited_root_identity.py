"""Chain-2 r2 (R1, WorkOrder [ap:1BAO34] R2-W1): an INHERITED stored group
root keeps its stored identity and its pre-round miss policy.

The r1 audit (focused, [ap:OQP2TH]/R1) reproduced one chain-introduced
regression: the two r1 stored-root seams adopted the shared resolver's
unrelated live corrective UUID, so a saved group whose valid full stored UUID
``D`` is deleted (absent from the live cache AND from every archive) silently
retargeted to a different live node ``L`` reachable by shaving ``D`` — even
with NO archive folder configured, where the frozen no-archive never-pass
guarantee forbids any output change.

Fix-encoding tests assert the baseline miss is restored on every affected
carrier (the audit's seven carriers, flags absent/false/true where
supported): no success, no unrelated live content, no corrective root
substitution, and no speculative correction echo.  The folder-configured
variants (empty folder / folder whose visible archives do not carry ``D``)
prove the rejection does not depend on the folder being absent.

Controls (labeled, pass on BOTH sides of the fix) keep every working
behavior: exact live stored roots; the explicit corrective override (forgiving
kernel + echo); the archive repair for an exact archived ``D`` INCLUDING the
E2 precedence over a competing live shave of the same prefix; the archived-D
sink refusal; and the mixed-operation correction-echo bookkeeping (a rejected
inherited resolution must neither deliver its speculative echo nor clobber a
legitimate sibling correction).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from coherence_harness import make_boundary_capable

from workflowy_mcp import server
from workflowy_mcp.archive_store import ArchiveStore, set_archive_store_for_tests
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.mcp_api_registry import dispatch_operation
from workflowy_mcp.node_archive import build_archive_document, serialize_archive

SOURCE_CACHE = {"epoch_number": 1, "epoch_id": "e1", "adopted_sequence": 3}

# The audit's exact ids: the deleted stored root D, the unrelated live root L
# sharing its long prefix, and a differently-prefixed live member C.
D_GONE = "dddddddd-0000-0000-0000-0000000000ff"
LIVE_ROOT = "dddddddd-0000-0000-0000-000000000001"  # == L: shares D's prefix
LIVE_MEMBER = "cccccccc-0000-0000-0000-000000000003"  # == C: different prefix
LIVE_ENTRY_NODE = "bbbbbbbb-0000-0000-0000-000000000002"
LIVE_ENTRY_SHAVE_HANDLE = "bbbbbbbb-0000-0000-0000-0000000000aa"

AP_CORE = "DLT1"
EXPORTED_AT = "2026-10-02T08:30:00Z"
BANNER_HEAD = "⚠️⚠️⚠️ ARCHIVED NODE: NOT IN WORKFLOWY. Served read-only from export file "

D_GROUP_ID = "grp-eeeeeee5eeeeeee5eeeeeee5eeeeeee5"
LIVE_GROUP_ID = "grp-eeeeeee6eeeeeee6eeeeeee6eeeeeee6"

_GROUPS = [
    {
        "id": D_GROUP_ID,
        "name": "DELETED-GROUP",
        "root_uuid": D_GONE,
        "prefixes": ["x"],
        "tag_prefixes": ["x"],
    },
    {
        "id": LIVE_GROUP_ID,
        "name": "LIVE-GROUP",
        "root_uuid": LIVE_ROOT,
        "prefixes": ["x"],
        "tag_prefixes": ["x"],
    },
]


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


def _archive_doc(records, node_id, name, exported_at=EXPORTED_AT):
    return build_archive_document(
        records,
        nest=_nest,
        node_id=node_id,
        name=name,
        description=None,
        exported_at=exported_at,
        source_cache=dict(SOURCE_CACHE),
    )


def _build_env(tmp_path, monkeypatch, *, folder_mode, archived_d_root=False):
    """The audit P3 shape: populated live cache with ONLY the unrelated live
    L (+ its member C); a saved group whose stored root is the deleted full
    UUID D.  ``folder_mode``: 'none' = no folder at all (the store provider
    returns None — stricter than r1's empty-directory control), 'empty' = a
    configured but empty folder, 'unrelated' = a folder whose visible archive
    carries only OTHER roots (D has no visible archived record).  With
    ``archived_d_root`` the folder instead holds an exact archive of D —
    the working-repair control (E2 against the competing live shave)."""

    if folder_mode == "none":
        store = ArchiveStore(folder_provider=lambda: None)
    elif folder_mode == "empty":
        empty = tmp_path / "empty-archives"
        empty.mkdir()
        store = ArchiveStore(folder_provider=lambda: str(empty))
    else:
        folder = tmp_path / "archives"
        folder.mkdir()
        if archived_d_root:
            folder.joinpath("deleted-root.json").write_bytes(
                serialize_archive(
                    _archive_doc(
                        [
                            _rec(
                                D_GONE,
                                name=f"[ap:{AP_CORE}] Deleted root #x",
                                parent_id=LIVE_ROOT,
                                note="deleted root archived note",
                            ),
                            _rec(
                                "d1dddddd-0000-0000-0000-00000000d001",
                                name="Deleted child #x",
                                parent_id=D_GONE,
                                note="deleted child archived note",
                            ),
                        ],
                        node_id=D_GONE,
                        name="deleted root archive",
                    )
                ).encode("utf-8")
            )
        else:
            folder.joinpath("other.json").write_bytes(
                serialize_archive(
                    _archive_doc(
                        [
                            _rec(
                                "eeeeeeee-0000-0000-0000-0000000000ee",
                                name="[ap:OTR1] Other root #x",
                                parent_id=LIVE_ROOT,
                                note="other archived note",
                            ),
                        ],
                        node_id="eeeeeeee-0000-0000-0000-0000000000ee",
                        name="unrelated archive",
                    )
                ).encode("utf-8")
            )
        store = ArchiveStore(folder_provider=lambda: str(folder))
    set_archive_store_for_tests(store)

    live = [
        _rec(LIVE_ROOT, name="UNRELATED LIVE ROOT #x", note="live root note"),
        _rec(
            LIVE_MEMBER,
            name="UNRELATED LIVE MEMBER #x",
            parent_id=LIVE_ROOT,
            note="archived",
        ),
        _rec(LIVE_ENTRY_NODE, name="Estate node entry #e", note="entry note"),
    ]
    client = object.__new__(server.WorkFlowyClient)

    live_ids = {LIVE_ROOT, LIVE_ENTRY_NODE}

    async def _get_node(node_id, max_retries=1):
        if node_id in live_ids:
            name = "UNRELATED LIVE ROOT #x" if node_id == LIVE_ROOT else (
                "Estate node entry #e"
            )
            note = (
                json.dumps([{"alias": "D", "group": "DELETED-GROUP"}])
                if node_id == LIVE_ROOT
                else "entry note"
            )
            from workflowy_mcp.models.node import WorkFlowyNode

            return WorkFlowyNode.model_validate(
                {
                    "id": node_id,
                    "name": name,
                    "note": note,
                }
            )
        raise AssertionError(
            "get_node must never run for the deleted/archived stored root "
            f"(got {node_id})"
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

    async def _note_session_config(target_tab):
        return ["#wfy-ns"], [], "", {"note_session_tags": ["#wfy-ns"]}

    monkeypatch.setattr(server, "_resolve_note_session_config", _note_session_config)

    async def _slice_group_config(target_tab):
        reply = {"note_session_tags": ["#wfy-ns"], "global_tags": []}
        return [dict(g) for g in _GROUPS], None, {}, [], reply

    monkeypatch.setattr(server, "_resolve_slice_group_config", _slice_group_config)
    return {
        "store": store,
        "tmp_path": tmp_path,
        "client": client,
    }


@pytest.fixture()
def gone_env(tmp_path, monkeypatch):
    """The no-folder D/L env (the frozen-criterion case)."""
    yield _build_env(tmp_path, monkeypatch, folder_mode="none")
    set_archive_store_for_tests(None)


@pytest.fixture(params=["empty", "unrelated"])
def folder_env(request, tmp_path, monkeypatch):
    """Folder configured but D has no visible archived record."""
    yield _build_env(
        tmp_path, monkeypatch, folder_mode=request.param
    )
    set_archive_store_for_tests(None)


@pytest.fixture()
def archived_d_env(tmp_path, monkeypatch):
    """The working-repair control env: D is an exact archived root AND the
    unrelated live L shares its prefix (E2 must keep beating the shave)."""
    yield _build_env(
        tmp_path, monkeypatch, folder_mode="folder", archived_d_root=True
    )
    set_archive_store_for_tests(None)


_REGISTRIES = {
    "node": "NODE_REGISTRY",
    "slice_group": "SLICE_GROUP_REGISTRY",
    "glimpse": "GLIMPSE_REGISTRY",
    "lightning_rod": "LIGHTNING_ROD_REGISTRY",
}


def _run(env, verb, params):
    async def _body():
        return await dispatch_operation(
            getattr(server, _REGISTRIES[verb.split(".")[0]]),
            verb,
            params,
            error_logger=lambda _message: None,
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
            operation_invoker=server._mcp_archive_operation_invoker,
        )

    async def _with_boundary():
        from workflowy_mcp.coherence.operation_cache import cache_operation_boundary

        async with cache_operation_boundary(env["client"], "t"):
            return await _body()

    return asyncio.run(_with_boundary())


def _assert_baseline_miss(result, *, code, identity=D_GONE):
    """The pre-round miss: the family error, no retarget, no echo.

    ``identity`` asserts the rejected stored UUID survives in the error
    text; the lens carriers' baseline message names the GROUP handle (not
    the root UUID), so they pass ``identity=None`` — the byte-shape the
    audit recorded at 466475a.
    """
    assert isinstance(result, dict), type(result)
    assert result.get("success") is False, result
    error = result.get("error")
    assert isinstance(error, dict), error
    assert error.get("code") == code, error
    assert error.get("code") != "archived_node_read_only", error
    text = json.dumps(result, ensure_ascii=False)
    assert "UNRELATED LIVE" not in text, text[:500]
    assert "corrected from" not in text, text[:500]
    assert not result.get("resolved_corrections"), (
        result.get("resolved_corrections")
    )
    assert "ARCHIVED NODE" not in text, text[:500]
    if identity is not None:
        # The rejected stored identity is preserved in its own miss message —
        # never silently replaced by another node's UUID.
        assert identity in text, text[:500]


def _group_params(verb, flag):
    if verb == "glimpse.run":
        base = {"group_handle": "DELETED-GROUP"}
    elif verb == "glimpse.estate":
        return {"handle": {"group": "DELETED-GROUP"}}
    else:
        base = {"lens": {"kind": "group", "group_handle": "DELETED-GROUP"}}
        if verb == "slice_group.sift":
            base["query"] = "archived"
        elif verb == "slice_group.merge.render":
            base["lens"] = {
                "kind": "merge",
                "operation": "union",
                "inputs": [
                    {"kind": "subtree", "root_handle": LIVE_ROOT},
                    {"kind": "group", "group_handle": "DELETED-GROUP"},
                ],
            }
    if flag is not None:
        base["include_archives"] = flag
    return base


# ---------------------------------------------------------------------------
# R2-W1 postcondition 1 — the D/L no-folder matrix, per carrier, per flag.
# Fix-encoding: at r1 (f6915fca) every one of these returned SUCCESS with the
# unrelated live L/C content and a 'resolved: … (corrected from D)' echo.
# ---------------------------------------------------------------------------


_CARRIER_CODES = {
    "glimpse.run": "glimpse_request_failed",
    "slice_group.render": "group_resolution_failed",
    "slice_group.sift": "group_resolution_failed",
    "slice_group.merge.render": "group_resolution_failed",
    "glimpse.estate": "glimpse_request_failed",
}

_CARRIER_FLAGS = {
    "glimpse.run": [None, False, True],
    "slice_group.render": [None, False, True],
    "slice_group.sift": [None, True],
    "slice_group.merge.render": [None, True],
    "glimpse.estate": [None, True],
}


@pytest.mark.parametrize("verb", list(_CARRIER_CODES))
@pytest.mark.parametrize("flag", [None, False, True])
def test_deleted_stored_root_keeps_baseline_miss(verb, flag, gone_env):
    if flag not in _CARRIER_FLAGS[verb]:
        pytest.skip("flag not supported by this carrier's matrix")
    identity = None if verb.startswith("slice_group") else D_GONE
    result = _run(gone_env, verb, _group_params(verb, flag))
    _assert_baseline_miss(result, code=_CARRIER_CODES[verb], identity=identity)


def test_estate_formal_json_deleted_group_entry_keeps_baseline_miss(gone_env):
    result = _run(
        gone_env,
        "glimpse.estate",
        {"handle": json.dumps([{"alias": "D", "group": "DELETED-GROUP"}])},
    )
    _assert_baseline_miss(result, code="glimpse_request_failed")


def test_estate_named_estate_deleted_group_entry_keeps_baseline_miss(gone_env):
    """The live named-estate carrier: LIVE_ROOT's note parses to one
    named-group entry whose stored root is the deleted D.  The estate read
    of the note is the only permitted live GET.  The estate's own envelope
    legitimately names its note-source node (LIVE_ROOT, 'UNRELATED LIVE
    ROOT #x'), so the retarget check is the ENTRY outcome: the group entry
    fails with the exact absent-root error carrying D, nothing renders, and
    no speculative echo survives."""
    result = _run(gone_env, "glimpse.estate", {"handle": LIVE_ROOT})
    assert isinstance(result, dict), type(result)
    assert result.get("success") is False, result
    error = result.get("error")
    assert error.get("code") == "glimpse_request_failed", error
    legacy = (error.get("extra") or {}).get("legacy_result") or {}
    estate_counts = legacy.get("estate") or {}
    assert estate_counts.get("rendered") == 0, estate_counts
    assert estate_counts.get("failed") == 1, estate_counts
    failed_rows = legacy.get("failed_entries") or []
    assert len(failed_rows) == 1, failed_rows
    row_text = json.dumps(failed_rows[0], ensure_ascii=False)
    assert D_GONE in row_text, row_text
    assert "is not present in the /nodes-export cache" in row_text, row_text
    full_text = json.dumps(result, ensure_ascii=False)
    assert "corrected from" not in full_text, full_text[:500]
    assert not result.get("resolved_corrections"), (
        result.get("resolved_corrections")
    )
    # The live member C (the only member the shave would have admitted) is
    # nowhere in the delivered payload's rendered content.
    assert "UNRELATED LIVE MEMBER" not in full_text, full_text[:500]


@pytest.mark.parametrize(
    "folder_mode", ["empty", "unrelated"], ids=["empty-folder", "other-archive"]
)
def test_folder_without_visible_d_keeps_baseline_miss(
    folder_env, folder_mode
):
    """Folder configured but D has no visible archived record: the rejection
    does not depend on the folder being absent (postcondition 1, last
    clause)."""
    result = _run(folder_env, "glimpse.run", {"group_handle": "DELETED-GROUP"})
    _assert_baseline_miss(result, code="glimpse_request_failed")
    result = _run(
        folder_env,
        "slice_group.render",
        {"lens": {"kind": "group", "group_handle": "DELETED-GROUP"}},
    )
    _assert_baseline_miss(
        result, code="group_resolution_failed", identity=None
    )


def test_deleted_stored_root_sink_writes_nothing(gone_env, tmp_path):
    """The file sink through the direct group carrier never retargets: the
    baseline miss, and NO file written (at r1 the file was written with the
    unrelated live L content)."""
    out = tmp_path / "gone-sink.md"
    result = _run(
        gone_env,
        "glimpse.run",
        {"group_handle": "DELETED-GROUP", "output_file": str(out)},
    )
    _assert_baseline_miss(result, code="glimpse_request_failed")
    assert not out.exists()


# ---------------------------------------------------------------------------
# Controls — pass on BOTH sides; each is labeled as a control, not a
# mutation proof.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", [None, True])
def test_control_exact_live_stored_root_renders_live(gone_env, flag):
    """CONTROL: an exact live stored root renders the same live scope as
    before (postcondition 2) — with the flag it may show marked archived
    descendants but gains no root-answer banner."""
    params = {"group_handle": "LIVE-GROUP"}
    if flag is not None:
        params["include_archives"] = flag
    result = _run(gone_env, "glimpse.run", params)
    assert isinstance(result, str), type(result)
    assert result.startswith("# UNRELATED LIVE ROOT #x"), result[:200]
    assert "ARCHIVED NODE" not in result, result[:120]


def test_control_explicit_corrective_override_keeps_result_and_echo(gone_env):
    """CONTROL (postcondition 4): an explicit corrective override that
    legitimately used the shared correction kernel keeps its result and its
    echo — the rejection never touches the explicit-handle policy."""
    result = _run(
        gone_env,
        "glimpse.run",
        {"group_handle": "DELETED-GROUP", "root_handle": D_GONE},
    )
    assert isinstance(result, str), type(result)
    assert "UNRELATED LIVE ROOT #x" in result, result[:400]
    assert "corrected from" in result, result[:200]


@pytest.mark.parametrize("flag", [None, True])
def test_control_exact_archived_d_beats_competing_live_shave_e2(
    archived_d_env, tmp_path, flag
):
    """CONTROL (postcondition 3 + E2): D is an exact visible archived root
    AND the unrelated live L shares its prefix.  The archive repair must
    still win — served under the exact central banner, never rejected, never
    retargeted to L, no correction echo."""
    params = {"group_handle": "DELETED-GROUP"}
    if flag is not None:
        params["include_archives"] = flag
    result = _run(archived_d_env, "glimpse.run", params)
    if isinstance(result, str):
        assert result.startswith(BANNER_HEAD), result[:200]
        assert EXPORTED_AT in result.splitlines()[0], result[:200]
    else:
        assert result.get("success") is True, result
        warning = result.get("archive_warning")
        assert isinstance(warning, str) and warning.startswith(BANNER_HEAD), result
    assert "Deleted root #x" in str(result), str(result)[:400]
    assert "Deleted child #x" in str(result), str(result)[:400]
    assert "corrected from" not in str(result), str(result)[:200]
    assert "UNRELATED LIVE" not in str(result), str(result)[:200]


def test_control_archived_d_sink_still_refuses(archived_d_env, tmp_path):
    """CONTROL (postcondition 5): the archived-D file sink keeps the exact
    universal §5.3.2 refusal with provenance, before any file writer."""
    out = tmp_path / "d-sink.md"
    result = _run(
        archived_d_env,
        "glimpse.run",
        {"group_handle": "DELETED-GROUP", "output_file": str(out)},
    )
    assert result.get("success") is False, result
    error = result["error"]
    assert error["code"] == "archived_node_read_only", error
    assert D_GONE in error["message"], error["message"]
    assert (
        "archived material is never written to a file or pushed to the modal"
        in error["message"]
    ), error["message"]
    assert f"exported {EXPORTED_AT})" in error["message"], error["message"]
    assert not out.exists()


def _estate_entry_result(env, entries):
    return _run(
        env,
        "glimpse.estate",
        {"handle": json.dumps(entries), "as_markdown": False},
    )


def test_control_mixed_estate_rejected_echo_does_not_clobber_legit_sibling(
    gone_env,
):
    """CONTROL for must-no-longer-exist #3, other-handles half: a mixed
    estate whose SUBTREE entry legitimately shaved (its echo recorded) and
    whose group entry's stored root is the rejected D — the call keeps
    per-entry isolation (the live entry rendered, the group entry a failed
    row), the LEGITIMATE sibling echo survives, and the speculative D echo
    is gone."""
    result = _estate_entry_result(
        gone_env,
        [
            {"alias": "E", "subtree": LIVE_ENTRY_SHAVE_HANDLE},
            {"alias": "D", "group": "DELETED-GROUP"},
        ],
    )
    assert result.get("success") is True, result
    text = json.dumps(result, ensure_ascii=False)
    assert "Estate node entry #e" in text, text[:600]
    corrections = result.get("resolved_corrections") or []
    assert [c["supplied"] for c in corrections] == [LIVE_ENTRY_SHAVE_HANDLE], (
        corrections
    )


def test_control_mixed_estate_rejection_before_legit_shave_keeps_later_echo(
    gone_env,
):
    """CONTROL, mirrored order: the rejected group entry first, the
    legitimately shaving subtree entry second — the later legitimate echo is
    recorded after the discard and survives."""
    result = _estate_entry_result(
        gone_env,
        [
            {"alias": "D", "group": "DELETED-GROUP"},
            {"alias": "E", "subtree": LIVE_ENTRY_SHAVE_HANDLE},
        ],
    )
    assert result.get("success") is True, result
    corrections = result.get("resolved_corrections") or []
    assert [c["supplied"] for c in corrections] == [LIVE_ENTRY_SHAVE_HANDLE], (
        corrections
    )


def test_control_identical_pair_discard_is_last_only(gone_env):
    """CONTROL for must-no-longer-exist #3, identical-pair half: entry A is
    an explicit subtree parcel whose handle D shaves (adopted — explicit
    handles keep their echo); entry B is the group whose STORED root is the
    same D (rejected).  The two corrections are identical (D → L); only the
    LAST one — the rejected inherited resolution's — is discarded, so the
    explicit entry's echo survives exactly once."""
    result = _estate_entry_result(
        gone_env,
        [
            {"alias": "A", "subtree": D_GONE},
            {"alias": "D", "group": "DELETED-GROUP"},
        ],
    )
    assert result.get("success") is True, result
    text = json.dumps(result, ensure_ascii=False)
    # Entry A legitimately renders the (explicitly requested) L scope.
    assert "UNRELATED LIVE ROOT #x" in text, text[:600]
    corrections = result.get("resolved_corrections") or []
    assert [c["supplied"] for c in corrections] == [D_GONE], corrections
    assert len(corrections) == 1, corrections
