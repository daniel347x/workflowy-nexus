"""Chain-2 r1 (F1, WorkOrder W1): a saved group's EFFECTIVE STORED root
participates in the one operation-gated, live-first archive mechanism.

Fix-encoding tests assert the two outcomes the audit reproduced at the
pre-fix baseline and that the repair must deliver:

* default automatic service (flag absent/false): every supported group
  carrier renders from the automatic augmented re-run instead of an
  ordinary group/cache miss;
* the exact central §5.1 warning in EVERY inclusion route (fallback rerun
  and ``include_archives=true`` alike), with structured provenance.

Controls (labeled) keep the established contracts on both sides of the fix:
live stored roots, live override roots (an unused archived default neither
warns nor refuses), sinks, no folder configured, genuine unknown roots, and
ordinary estate entry isolation.
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

LIVE_ROOT = "dddddddd-0000-0000-0000-000000000001"
LIVE_LEAF = "dddddddd-0000-0000-0000-000000000002"
ARCH_ROOT = "eeeeeeee-0000-0000-0000-0000000000ee"
ARCH_CHILD = "cccccccc-0000-0000-0000-000000000003"
ARCH2_ROOT = "f2f2f2f2-0000-0000-0000-0000000000f2"
UNKNOWN_ROOT = "99999999-0000-0000-0000-000000009999"
AP_CORE = "ARC1"

BANNER_HEAD = "⚠️⚠️⚠️ ARCHIVED NODE: NOT IN WORKFLOWY. Served read-only from export file "
BANNER_TAIL = " (exported 2026-10-01T12:00:00Z). Tell the human."
EXPORTED_AT = "2026-10-01T12:00:00Z"

ARCH_GROUP_ID = "grp-aaaaaaa1aaaaaaa1aaaaaaa1aaaaaaa1"
ARCH2_GROUP_ID = "grp-aaaaaaa2aaaaaaa2aaaaaaa2aaaaaaa2"
LIVE_GROUP_ID = "grp-bbbbbbb2bbbbbbb2bbbbbbb2bbbbbbb2"
DORMANT_GROUP_ID = "grp-ccccccc3ccccccc3ccccccc3ccccccc3"
GONE_GROUP_ID = "grp-ddddddd4ddddddd4ddddddd4ddddddd4"

_GROUPS = [
    {
        "id": ARCH_GROUP_ID,
        "name": "ARCH-GROUP",
        "root_uuid": ARCH_ROOT,
        "prefixes": ["x"],
        "tag_prefixes": ["x"],
    },
    {
        "id": ARCH2_GROUP_ID,
        "name": "ARCH2-GROUP",
        "root_uuid": ARCH2_ROOT,
        "prefixes": ["y"],
        "tag_prefixes": ["y"],
    },
    {
        "id": LIVE_GROUP_ID,
        "name": "LIVE-GROUP",
        "root_uuid": LIVE_ROOT,
        "prefixes": ["x"],
        "tag_prefixes": ["x"],
    },
    {
        "id": DORMANT_GROUP_ID,
        "name": "DORMANT-GROUP",
        "root_uuid": LIVE_LEAF,
        "prefixes": ["z"],
        "tag_prefixes": ["z"],
    },
    {
        "id": GONE_GROUP_ID,
        "name": "GONE-GROUP",
        "root_uuid": UNKNOWN_ROOT,
        "prefixes": ["q"],
        "tag_prefixes": ["q"],
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


@pytest.fixture()
def group_env(tmp_path, monkeypatch):
    """Active archives (two files) + a dormant one + a populated live cache.

    The active archives' roots are absent from live and plant under the live
    LIVE_ROOT (the parent-rule precondition), so no orphan/parent-rule
    excuse exists.  ``get_node`` raises for every id: an archived node must
    never be fetched live (fix-encoding safety stub).
    """
    (tmp_path / "arch.json").write_bytes(
        serialize_archive(
            _archive_doc(
                [
                    _rec(
                        ARCH_ROOT,
                        name=f"[ap:{AP_CORE}] Archived root #x",
                        parent_id=LIVE_ROOT,
                        note="archived root note",
                    ),
                    _rec(
                        ARCH_CHILD,
                        name="Archived child #x",
                        parent_id=ARCH_ROOT,
                        note="child archived note",
                    ),
                ],
                node_id=ARCH_ROOT,
                name="fixture archive",
            )
        ).encode("utf-8")
    )
    (tmp_path / "arch2.json").write_bytes(
        serialize_archive(
            _archive_doc(
                [
                    _rec(
                        ARCH2_ROOT,
                        name="[ap:ARC2] Second root #y",
                        parent_id=LIVE_ROOT,
                        note="second root note",
                    ),
                ],
                node_id=ARCH2_ROOT,
                name="second fixture archive",
                exported_at="2026-10-01T13:00:00Z",
            )
        ).encode("utf-8")
    )
    # Dormant: its root (LIVE_LEAF) is still live, so the archive never answers.
    (tmp_path / "dormant.json").write_bytes(
        serialize_archive(
            _archive_doc(
                [
                    _rec(LIVE_LEAF, name="Live leaf #z", parent_id=LIVE_ROOT),
                    _rec(
                        "d0000000-0000-0000-0000-0000000000d1",
                        name="Dormant child #z",
                        parent_id=LIVE_LEAF,
                    ),
                ],
                node_id=LIVE_LEAF,
                name="dormant export",
            )
        ).encode("utf-8")
    )
    store = ArchiveStore(folder_provider=lambda: str(tmp_path))
    set_archive_store_for_tests(store)
    live = [
        _rec(LIVE_ROOT, name="Live root", note="root note"),
        _rec(LIVE_LEAF, name="Live leaf", parent_id=LIVE_ROOT),
    ]
    client = object.__new__(server.WorkFlowyClient)

    async def _get_node(node_id, max_retries=1):
        raise AssertionError(
            f"get_node must never run for an archived id (got {node_id})"
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
    yield {
        "store": store,
        "tmp_path": tmp_path,
        "client": client,
        "arch_path": tmp_path / "arch.json",
        "arch2_path": tmp_path / "arch2.json",
    }
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


def _assert_archive_answer(env, result, *, arch_path=None, exported_at=EXPORTED_AT):
    """The exact §5.1 answer shape: banner first (string) or the two keys
    with full provenance (dict), and no live GET ever happened (the env's
    raising ``get_node`` proves that by not raising)."""
    if isinstance(result, str):
        first = result.splitlines()[0]
        assert first.startswith(BANNER_HEAD), result
        assert first.endswith(BANNER_TAIL), first
        if arch_path is not None:
            assert str(arch_path) in first, first
        assert exported_at in first, first
        return
    assert isinstance(result, dict), type(result)
    assert result.get("success") is True, result
    warning = result.get("archive_warning")
    assert isinstance(warning, str) and warning.startswith(BANNER_HEAD), result
    assert exported_at in warning, warning
    archived = result.get("archived")
    assert isinstance(archived, list) and archived, result
    entry = archived[0]
    assert set(entry) == {
        "handle",
        "uuid",
        "archive_file",
        "archive_name",
        "exported_at",
        "archive_root",
    }, entry
    assert entry["exported_at"] == exported_at, entry


# ---------------------------------------------------------------------------
# Postcondition 1 + 2 — default automatic service AND the warning in every
# inclusion route, per carrier, flag absent/false/true.  Fix-encoding: at the
# pre-fix baseline the absent/false calls return an ordinary group/cache miss
# and the true calls render 📦 content WITHOUT the banner.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", [None, False, True])
def test_glimpse_run_group_archived_root_served_under_banner(
    group_env, flag
):
    params = {"group_handle": "ARCH-GROUP"}
    if flag is not None:
        params["include_archives"] = flag
    result = _run(group_env, "glimpse.run", params)
    _assert_archive_answer(group_env, result, arch_path=group_env["arch_path"])
    assert "[ap:ARC1] 📦 Archived root #x" in result, result[:400]
    assert "Archived child #x" in result, result[:400]


@pytest.mark.parametrize("flag", [None, False, True])
def test_slice_group_render_group_atom_served_under_banner(group_env, flag):
    params = {"lens": {"kind": "group", "group_handle": "ARCH-GROUP"}}
    if flag is not None:
        params["include_archives"] = flag
    result = _run(group_env, "slice_group.render", params)
    _assert_archive_answer(group_env, result, arch_path=group_env["arch_path"])
    # A group-atom render shows MEMBERS (the root contraction is pruned), so
    # the archived child heading is the member evidence (audit P1 shape).
    assert "📦 Archived child #x" in result, result[:400]


def test_slice_group_render_group_atom_structured_provenance(group_env):
    """A representative structured (``as_markdown=false``) group render: the
    dict carries ``archive_warning`` + ``archived`` with the effective root
    UUID, file/name/date/root and the supplied handle."""
    result = _run(
        group_env,
        "slice_group.render",
        {
            "lens": {"kind": "group", "group_handle": "ARCH-GROUP"},
            "as_markdown": False,
        },
    )
    _assert_archive_answer(group_env, result, arch_path=group_env["arch_path"])
    entry = result["archived"][0]
    assert entry["handle"] == ARCH_ROOT, entry
    assert entry["uuid"] == ARCH_ROOT, entry
    assert entry["archive_root"] == ARCH_ROOT, entry
    assert entry["archive_name"] == "fixture archive", entry
    assert entry["archive_file"] == str(group_env["arch_path"]), entry
    preview = json.dumps(result.get("preview_tree") or "", ensure_ascii=False)
    assert "📦 Archived child #x" in preview, preview[:300]


@pytest.mark.parametrize("flag", [None, True])
def test_slice_group_sift_group_atom_served_under_banner(group_env, flag):
    """The grep tier (sift's default ``context``): the banner opens the
    report, above the Source line, and the archived notes are greppable."""
    params = {
        "lens": {"kind": "group", "group_handle": "ARCH-GROUP"},
        "query": "archived",
    }
    if flag is not None:
        params["include_archives"] = flag
    result = _run(group_env, "slice_group.sift", params)
    _assert_archive_answer(group_env, result, arch_path=group_env["arch_path"])
    assert "Archived child #x" in result, result[:400]


@pytest.mark.parametrize("flag", [None, True])
def test_merge_render_nested_group_atom_served_under_banner(group_env, flag):
    """A recursive merged lens cannot hide an archived effective root: the
    union of a live subtree and the archived group renders, banner first."""
    params = {
        "lens": {
            "kind": "merge",
            "operation": "union",
            "inputs": [
                {"kind": "subtree", "root_handle": LIVE_ROOT},
                {"kind": "group", "group_handle": "ARCH-GROUP"},
            ],
        }
    }
    if flag is not None:
        params["include_archives"] = flag
    result = _run(group_env, "slice_group.merge.render", params)
    _assert_archive_answer(group_env, result, arch_path=group_env["arch_path"])
    assert "📦 Archived root #x" in result, result[:400]
    assert "Live root" in result, result[:400]


@pytest.mark.parametrize("flag", [None, True])
def test_estate_qualified_group_entry_served_under_banner(group_env, flag):
    params = {"handle": {"group": "ARCH-GROUP"}}
    if flag is not None:
        params["include_archives"] = flag
    result = _run(group_env, "glimpse.estate", params)
    _assert_archive_answer(group_env, result, arch_path=group_env["arch_path"])
    assert "📦 Archived root #x" in result, result[:400]


@pytest.mark.parametrize("flag", [None, True])
def test_estate_named_estate_group_entry_served_under_banner(group_env, flag):
    """The live named-estate carrier: LIVE_ROOT's note parses to one
    named-group entry whose stored root is archived.  The live note read is
    the ONLY get_node (the env stub raises for any archived id)."""
    from workflowy_mcp.models.node import WorkFlowyNode

    async def live_note(node_id, **kwargs):
        assert node_id == LIVE_ROOT, (
            "get_node must never run for an archived id"
        )
        return WorkFlowyNode.model_validate(
            {
                "id": node_id,
                "name": "Saved estate",
                "note": json.dumps(
                    [{"alias": "ARCH", "group": "ARCH-GROUP"}]
                ),
            }
        )

    group_env["client"].get_node = live_note
    params = {"handle": LIVE_ROOT}
    if flag is not None:
        params["include_archives"] = flag
    result = _run(group_env, "glimpse.estate", params)
    _assert_archive_answer(group_env, result, arch_path=group_env["arch_path"])
    assert "📦 Archived root #x" in result, result[:400]


@pytest.mark.parametrize("flag", [None, True])
def test_estate_formal_json_group_entry_served_under_banner(group_env, flag):
    """The formal (direct-JSON) group estate entry carrier."""
    params = {
        "handle": json.dumps([{"alias": "ARCH", "group": "ARCH-GROUP"}])
    }
    if flag is not None:
        params["include_archives"] = flag
    result = _run(group_env, "glimpse.estate", params)
    _assert_archive_answer(group_env, result, arch_path=group_env["arch_path"])
    assert "📦 Archived root #x" in result, result[:400]


def test_two_answering_archives_one_banner_line_per_file_in_order(group_env):
    """Two group roots in two different archive files, one merged lens: one
    banner line per answering file, in first-answered order, and both
    provenance rows."""
    result = _run(
        group_env,
        "slice_group.merge.render",
        {
            "lens": {
                "kind": "merge",
                "operation": "union",
                "inputs": [
                    {"kind": "group", "group_handle": "ARCH-GROUP"},
                    {"kind": "group", "group_handle": "ARCH2-GROUP"},
                ],
            },
            "as_markdown": False,
        },
    )
    _assert_archive_answer(group_env, result)
    warning = result["archive_warning"]
    lines = [ln for ln in warning.splitlines() if ln.startswith(BANNER_HEAD)]
    assert len(lines) == 2, warning
    assert str(group_env["arch_path"]) in lines[0], warning
    assert str(group_env["arch2_path"]) in lines[1], warning
    assert "2026-10-01T13:00:00Z" in lines[1], warning
    handles = [entry["uuid"] for entry in result["archived"]]
    assert handles == [ARCH_ROOT, ARCH2_ROOT], handles


def test_fallback_rerun_runs_the_handler_exactly_once_more(group_env, monkeypatch):
    """Fallback → include/automatic, where practical: the gate ran (the
    stored root reached its archive phase on the live-only first run) and
    the handler ran EXACTLY twice (first outcome + one re-run).  The
    include-mode call runs it exactly once."""
    from workflowy_mcp import archive_context

    gate_calls = {"n": 0}
    real_gate = archive_context.archive_fallback_gate

    def _counting_gate(*args, **kwargs):
        gate_calls["n"] += 1
        return real_gate(*args, **kwargs)

    monkeypatch.setattr(
        archive_context, "archive_fallback_gate", _counting_gate
    )
    stage1_calls = {"n": 0}
    real_stage1 = server.build_stage1_base_tree

    def _counting_stage1(*args, **kwargs):
        stage1_calls["n"] += 1
        return real_stage1(*args, **kwargs)

    monkeypatch.setattr(server, "build_stage1_base_tree", _counting_stage1)

    result = _run(group_env, "glimpse.run", {"group_handle": "ARCH-GROUP"})
    _assert_archive_answer(group_env, result, arch_path=group_env["arch_path"])
    assert gate_calls["n"] >= 1, "the stored root never reached the archive phase"
    assert stage1_calls["n"] == 2, stage1_calls

    gate_calls["n"] = 0
    stage1_calls["n"] = 0
    result = _run(
        group_env,
        "glimpse.run",
        {"group_handle": "ARCH-GROUP", "include_archives": True},
    )
    _assert_archive_answer(group_env, result, arch_path=group_env["arch_path"])
    assert gate_calls["n"] == 0, "include mode must not consult the gate"
    assert stage1_calls["n"] == 1, stage1_calls


# ---------------------------------------------------------------------------
# Postcondition 3 — precedence, inheritance, and the live-root include case.
# ---------------------------------------------------------------------------


def test_live_root_include_with_archived_descendants_marks_without_banner(
    group_env,
):
    """A live stored root with 📦 archived descendants under the flag is a
    mark-only inclusion: successful, the descendants shown marked, and NO
    banner — no handle was answered from an archive."""
    result = _run(
        group_env,
        "glimpse.run",
        {"group_handle": "LIVE-GROUP", "include_archives": True},
    )
    assert isinstance(result, str), type(result)
    assert result.startswith("# Live root"), result[:200]
    assert "📦 Archived root #x" in result, result[:600]
    assert not result.startswith(BANNER_HEAD), result[:120]
    assert "ARCHIVED NODE" not in result, result[:120]


@pytest.mark.parametrize("flag", [None, True])
def test_live_override_replaces_archived_default_completely(group_env, flag):
    """A live override root wins: the unused archived stored default neither
    warns nor refuses the call."""
    params = {"group_handle": "ARCH-GROUP", "root_handle": LIVE_ROOT}
    if flag is not None:
        params["include_archives"] = flag
    result = _run(group_env, "glimpse.run", params)
    assert isinstance(result, str), type(result)
    assert result.startswith("# Live root"), result[:200]
    assert "ARCHIVED NODE" not in result, result[:120]
    if flag:
        # The archived descendants ride the augmented view, marked.
        assert "📦" in result, result[:600]


def test_lens_override_replaces_archived_default_completely(group_env):
    """A lens override root replaces the stored root completely: the render
    is scoped to the LIVE root with the archives left out (the footer), never
    the archived default answered."""
    result = _run(
        group_env,
        "slice_group.render",
        {
            "lens": {
                "kind": "group",
                "group_handle": "ARCH-GROUP",
                "root_handle": LIVE_ROOT,
            }
        },
    )
    assert isinstance(result, str), type(result)
    assert "ARCHIVED NODE" not in result, result[:120]
    # The live scope's archives were left out (footer), not served.
    assert "archived subtree(s) under this scope were left out" in result, (
        result[:400]
    )


# ---------------------------------------------------------------------------
# Postcondition 4 — sinks keep the universal refusal; a default-group fix
# must never cause a sink fallback re-run.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verb_params",
    [
        ("glimpse.run", {"group_handle": "ARCH-GROUP"}),
        (
            "slice_group.render",
            {"lens": {"kind": "group", "group_handle": "ARCH-GROUP"}},
        ),
    ],
)
def test_group_sink_still_refuses_and_never_reruns(
    group_env, monkeypatch, verb_params
):
    from workflowy_mcp import archive_context

    gate_hits = {"n": 0}
    real_gate = archive_context.archive_fallback_gate

    def _counting_gate(*args, **kwargs):
        hit = real_gate(*args, **kwargs)
        if hit:
            gate_hits["n"] += 1
        return hit

    monkeypatch.setattr(
        archive_context, "archive_fallback_gate", _counting_gate
    )
    verb, base = verb_params
    out = group_env["tmp_path"] / "group-sink.md"
    params = dict(base)
    params["output_file"] = str(out)
    result = _run(group_env, verb, params)
    assert result.get("success") is False, result
    error = result["error"]
    assert error["code"] == "archived_node_read_only", error
    assert ARCH_ROOT in error["message"], error["message"]
    assert (
        "archived material is never written to a file or pushed to the modal"
        in error["message"]
    ), error["message"]
    assert "exported 2026-10-01T12:00:00Z)" in error["message"], error["message"]
    assert not out.exists()
    assert gate_hits["n"] == 0, "a sink must never request the fallback re-run"


def test_group_modal_push_sink_keeps_chain1_matrix(group_env, monkeypatch):
    commands: list = []

    async def _connected(**kwargs):
        return object(), object()

    async def _request(*args, **kwargs):
        commands.append(kwargs.get("payload"))
        return {"success": True, "node_count": 0}

    monkeypatch.setattr(
        server, "get_ws_connection_with_grace_wait", _connected
    )
    group_env["client"]._ws_request_response = _request
    result = _run(
        group_env,
        "glimpse.run",
        {"group_handle": "ARCH-GROUP", "push_to_modal": True},
    )
    assert result.get("success") is False, result
    error = result["error"]
    assert error["code"] == "archived_node_read_only", error
    assert commands == [], commands


# ---------------------------------------------------------------------------
# Postcondition 5 — controls that keep the established behavior.
# ---------------------------------------------------------------------------


def test_no_folder_group_read_keeps_established_error(group_env, monkeypatch, tmp_path):
    """No archive folder configured: the stored-root resolution finds no
    archive, and the group read keeps today's ordinary miss (no banner, no
    refusal)."""
    empty = tmp_path / "empty-archives"
    empty.mkdir()
    set_archive_store_for_tests(
        ArchiveStore(folder_provider=lambda: str(empty))
    )
    try:
        result = _run(group_env, "glimpse.run", {"group_handle": "ARCH-GROUP"})
        assert result.get("success") is False, result
        error = result["error"]
        assert error.get("code") != "archived_node_read_only", error
        message = error.get("message") or (
            error.get("error") if isinstance(error, dict) else str(error)
        )
        assert "ARCHIVED NODE" not in str(message), message
    finally:
        set_archive_store_for_tests(group_env["store"])


@pytest.mark.parametrize(
    "carrier",
    [
        ("glimpse.run", {"group_handle": "GONE-GROUP"}),
        (
            "slice_group.render",
            {"lens": {"kind": "group", "group_handle": "GONE-GROUP"}},
        ),
    ],
)
def test_genuinely_unknown_stored_root_keeps_established_error(
    group_env, carrier
):
    """A stored root that is neither live nor archived keeps today's exact
    group/cache-miss behavior — it is never turned into a live corrective
    target merely to make the call render."""
    verb, base = carrier
    result = _run(group_env, verb, dict(base))
    assert result.get("success") is False, result
    error = result["error"]
    assert error.get("code") != "archived_node_read_only", error
    message = json.dumps(error)
    assert "ARCHIVED NODE" not in message, message


def test_dormant_stored_root_renders_live_without_banner(group_env):
    """A dormant archive (its root still live) never answers: the group at
    that live root renders live content, no banner."""
    result = _run(
        group_env, "glimpse.run", {"group_handle": "DORMANT-GROUP"}
    )
    assert isinstance(result, str), result
    assert result.startswith("# Live leaf"), result[:200]
    assert "ARCHIVED NODE" not in result, result[:120]
    assert "Dormant child" not in result, result[:400]


def test_superseded_archive_answer_names_the_newer_file(group_env, tmp_path):
    """Two exports of one subtree: the older file is superseded; the group
    answers from the NEWER archive and the banner names it."""
    (tmp_path / "older.json").write_bytes(
        serialize_archive(
            _archive_doc(
                [
                    _rec(
                        ARCH_ROOT,
                        name="[ap:ARC1] Older root #x",
                        parent_id=LIVE_ROOT,
                        note="older note",
                    ),
                ],
                node_id=ARCH_ROOT,
                name="older export",
                exported_at="2026-09-01T09:00:00Z",
            )
        ).encode("utf-8")
    )
    result = _run(
        group_env,
        "slice_group.render",
        {"lens": {"kind": "group", "group_handle": "ARCH-GROUP"}},
    )
    _assert_archive_answer(group_env, result, arch_path=group_env["arch_path"])
    assert "Older root" not in result, result[:400]


def test_estate_unknown_group_entry_keeps_per_entry_isolation(group_env):
    """An ordinary unknown (non-archive) named-group entry among a live
    entry keeps today's per-entry isolation: the live entry renders, the
    unknown one is a failed row, never the archive refusal."""
    from workflowy_mcp.models.node import WorkFlowyNode

    async def live_note(node_id, **kwargs):
        assert node_id == LIVE_ROOT
        return WorkFlowyNode.model_validate(
            {
                "id": node_id,
                "name": "Saved estate",
                "note": json.dumps(
                    [
                        {"alias": "LIVEFORM", "spec": {"kind": "raw_root", "root_uuid": LIVE_LEAF, "as_markdown": True}},
                        {"alias": "GONE", "group": "NO-SUCH-GROUP"},
                    ]
                ),
            }
        )

    group_env["client"].get_node = live_note
    result = _run(group_env, "glimpse.estate", {"handle": LIVE_ROOT})
    assert isinstance(result, str), type(result)
    assert "ARCHIVED NODE" not in result, result[:200]
    # The live entry rendered; the unknown group entry is a loud failed row.
    assert "Live leaf" in result, result[:600]
    assert "NO-SUCH-GROUP" in result, result[:600]
