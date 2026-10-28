"""The include_archives flag on the sixteen verbs (Archive Reads U3).

§U3.5 test 1 — per verb (16), flag true / false / absent against ONE
fixture tree (a live root with live children; one planted archive under
it; one planted outside it; one orphan; one completed archived child; one
suppressed archived child; one archived Dock): placement, order, the 📦
mark, completion and suppression parity, the Dock boundary, scope
exclusion, the §5.5 footer text and count, byte-identity when nothing is
planted under the scope, and the §5.9 refusals.
"""

from __future__ import annotations

import asyncio

import pytest

from coherence_harness import make_boundary_capable

from workflowy_mcp import server
from workflowy_mcp.archive_store import ArchiveStore, set_archive_store_for_tests
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.mcp_api_registry import dispatch_operation
from workflowy_mcp.node_archive import build_archive_document, serialize_archive

SOURCE_CACHE = {"epoch_number": 1, "epoch_id": "e1", "adopted_sequence": 3}

LIVE_ROOT = "11111111-0000-0000-0000-000000000001"
LIVE_LEAF = "22222222-0000-0000-0000-000000000002"
OUT_PARENT = "33333333-0000-0000-0000-000000000003"

ARCH_ROOT_A = "eeeeeeee-0000-0000-0000-00000000a001"
ARCH_COMPLETED = "eeeeeeee-0000-0000-0000-00000000a002"
ARCH_SUPPRESSED = "eeeeeeee-0000-0000-0000-00000000a003"
ARCH_DOCK = "eeeeeeee-0000-0000-0000-00000000a004"
ARCH_ROOT_B = "eeeeeeee-0000-0000-0000-00000000b001"
ARCH_ROOT_C = "eeeeeeee-0000-0000-0000-00000000c001"
NOWHERE = "99999999-0000-0000-0000-000000009999"

MARK = "📦"
WORD = "e2earch"
FOOTER_TAIL = (
    "archived subtree(s) under this scope were left out; "
    "include_archives=true includes them"
)


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


def _write_archive(tmp_path, records, node_id, name):
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


def _live_records():
    return [
        _rec(LIVE_ROOT, name="Live root #proj", note="the live root note"),
        _rec(LIVE_LEAF, name="Live leaf", parent_id=LIVE_ROOT, note="a live note"),
        _rec(OUT_PARENT, name="Outside parent", note="outside the scope"),
    ]


def _archive_a_records():
    return [
        _rec(
            ARCH_ROOT_A,
            name=f"[ap:ARC1] Archived root #{WORD}",
            parent_id=LIVE_ROOT,
            note=f"archived root note mentioning {WORD}",
        ),
        _rec(
            ARCH_COMPLETED,
            name=f"Archived done #{WORD}",
            parent_id=ARCH_ROOT_A,
            completed=True,
            completedAt=1750000000,
        ),
        _rec(
            ARCH_SUPPRESSED,
            name=f"Archived suppressed #{WORD} #wfy-suppress",
            parent_id=ARCH_ROOT_A,
        ),
        _rec(ARCH_DOCK, name="Archived Dock #proj", parent_id=ARCH_ROOT_A),
    ]


def _write_all_archives(tmp_path):
    _write_archive(tmp_path, _archive_a_records(), ARCH_ROOT_A, "arch-a.json")
    _write_archive(
        tmp_path,
        [_rec(ARCH_ROOT_B, name=f"Second archive #{WORD}", parent_id=OUT_PARENT)],
        ARCH_ROOT_B,
        "arch-b.json",
    )
    _write_archive(
        tmp_path,
        [_rec(ARCH_ROOT_C, name=f"Orphan archive #{WORD}", parent_id=NOWHERE)],
        ARCH_ROOT_C,
        "arch-c.json",
    )


CONFIG_REPLY = {"note_session_tags": ["proj"], "global_tags": []}
GROUPS = [
    {
        "id": "grp-11111111111111111111111111111111",
        "name": "All",
        "root_uuid": LIVE_ROOT,
        "prefixes": [WORD],
        "active": True,
    }
]
POSTFIXES = {
    "auto_expand": "expand",
    "suppress": "suppress",
    "deprecated": "deprecated",
}


@pytest.fixture()
def flag_env(tmp_path, monkeypatch):
    """The one fixture tree: three archives (A planted under LIVE_ROOT, B
    planted outside it, C orphaned), the live view, stubbed config reads."""
    _write_all_archives(tmp_path)
    store = ArchiveStore(folder_provider=lambda: str(tmp_path))
    set_archive_store_for_tests(store)
    client = object.__new__(server.WorkFlowyClient)

    async def _get_node(node_id, max_retries=1):
        raise AssertionError(f"get_node must never run here (got {node_id})")

    client.get_node = _get_node

    async def _scry(node_id, depth=None, size_limit=1000, output_file=None):
        from workflowy_mcp.coherence.operation_cache import current_operation_cache

        nodes = current_operation_cache().view.nodes_by_id
        flat: list[dict] = []

        def walk(pid):
            for nid, node in nodes.items():
                parent = node.get("parent_id") or node.get("parentId")
                if parent == pid:
                    flat.append(dict(node))
                    walk(nid)

        walk(str(node_id))
        root = dict(nodes.get(str(node_id), {}))
        return {"success": True, "root": root, "children": flat, "count": len(flat)}

    client.workflowy_scry = _scry
    make_boundary_capable(client, nodes=_live_records())
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)

    async def _note_session_config(target_tab):
        return ["proj"], [], None, dict(CONFIG_REPLY)

    async def _slice_group_config(target_tab):
        return (
            [dict(group) for group in GROUPS],
            "grp-11111111111111111111111111111111",
            dict(POSTFIXES),
            ["wfy-suppress"],
            dict(CONFIG_REPLY),
        )

    monkeypatch.setattr(server, "_resolve_note_session_config", _note_session_config)
    monkeypatch.setattr(server, "_resolve_slice_group_config", _slice_group_config)
    yield {"store": store, "tmp_path": tmp_path, "client": client}
    set_archive_store_for_tests(None)


_REGISTRIES = (
    ("node", "NODE_REGISTRY"),
    ("anchor", "ANCHOR_REGISTRY"),
    ("tag", "TAG_REGISTRY"),
    ("slice_group", "SLICE_GROUP_REGISTRY"),
    ("glimpse", "GLIMPSE_REGISTRY"),
    ("lightning_rod", "LIGHTNING_ROD_REGISTRY"),
    ("beacon", "BEACON_REGISTRY"),
    ("system", "SYSTEM_REGISTRY"),
)


def _registry_for(family):
    return getattr(server, dict(_REGISTRIES)[family])


def _run(flag_env, verb, params):
    """One full dispatch through the real server hooks (validation, the
    boundary, the archive invoker, the finalizer)."""

    async def _body():
        return await dispatch_operation(
            _registry_for(verb.split(".")[0]),
            verb,
            params,
            error_logger=lambda _message: None,
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
            operation_invoker=server._mcp_archive_operation_invoker,
        )

    client = flag_env["client"]
    return asyncio.run(_with_boundary(client, _body))


async def _with_boundary(client, body):
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary

    async with cache_operation_boundary(client, "t"):
        return await body()


# The 16 flag verbs with the params that put the planted archive (A) in
# each verb's scope.  (verb, params)
FLAG_VERB_PARAMS = (
    ("node.search", {"root_handle": LIVE_ROOT, "query": WORD}),
    ("node.scry", {"root_handle": OUT_PARENT}),
    ("anchor.search", {"dock_handle": LIVE_ROOT, "query": WORD}),
    ("anchor.dock.peek", {"dock_handle": LIVE_ROOT}),
    ("anchor.dock.render", {"dock_handle": LIVE_ROOT}),
    ("anchor.dock.list", {}),
    ("slice_group.render", {"lens": {"kind": "group", "group_handle": "All"}}),
    (
        "slice_group.sift",
        {"lens": {"kind": "group", "group_handle": "All"}, "query": WORD},
    ),
    (
        "slice_group.merge.render",
        {
            "lens": {
                "kind": "merge",
                "operation": "union",
                "inputs": [
                    {"kind": "subtree", "root_handle": LIVE_ROOT},
                    {"kind": "subtree", "root_handle": LIVE_LEAF},
                ],
            }
        },
    ),
    ("glimpse.run", {"root_handle": LIVE_ROOT, "allow_anchor_dock": True}),
    ("glimpse.estate", {"handle": {"subtree": LIVE_ROOT}}),
    ("lightning_rod.gather", {"root_handle": LIVE_ROOT}),
    (
        "lightning_rod.render_tags",
        {"root_handle": LIVE_ROOT, "tags": [WORD]},
    ),
    (
        "lightning_rod.render_search",
        {"root_handle": LIVE_ROOT, "query": WORD},
    ),
    (
        "lightning_rod.render_nodes",
        {"symbols": [{"uuid": LIVE_ROOT, "expand": True}]},
    ),
    (
        "lightning_rod.render_symbols",
        {"symbols": [{"uuid": LIVE_ROOT, "expand": True}], "root_handles": LIVE_ROOT},
    ),
)


def _as_text(result):
    if isinstance(result, str):
        return result
    import json as _json

    return _json.dumps(result, ensure_ascii=False, default=str)


@pytest.mark.parametrize("verb,params", FLAG_VERB_PARAMS, ids=[v for v, _ in FLAG_VERB_PARAMS])
def test_flag_true_places_archived_subtree_under_scope(flag_env, verb, params):
    """include_archives=true: the planted archive appears under its parent,
    marked 📦, and the §5.5 footer never appears."""
    result = _run(flag_env, verb, {**params, "include_archives": True})
    text = _as_text(result)
    assert not isinstance(result, dict) or result.get("success") is not False, (
        f"{verb} failed: {text[:400]}"
    )
    assert "archives_left_out" not in _as_text(result)
    if verb == "anchor.dock.list":
        # dock.list's scope is the whole vault: every archive is IN its
        # scope — but it lists Docks, and no archive is a Dock: its
        # inventory shows no archived rows and carries no footer.
        assert WORD not in text
        return
    # The planted archive's marked content is visible (the 📦 mark or the
    # distinctive word — the vocabulary gather shows tags, not names).
    assert MARK in text or WORD in text, f"{verb}: archived material invisible"
    # Scope exclusion: the archive planted OUTSIDE the scope (B) and the
    # orphan (C) never appear (Axiom 16).  node.scry's fixture scope root
    # IS OUT_PARENT (a non-Dock: the guard must run to resolve the root),
    # so archive B is IN its scope there.
    if verb not in ("lightning_rod.gather", "node.scry"):
        # gather surveys the tag vocabulary under the root — still only
        # A's tags; assert the other archives' distinctive names absent.
        assert "Second archive" not in text
        assert "Orphan archive" not in text


@pytest.mark.parametrize("verb,params", FLAG_VERB_PARAMS, ids=[v for v, _ in FLAG_VERB_PARAMS])
def test_flag_false_and_absent_leave_the_footer(flag_env, verb, params):
    """include_archives absent or false: today's result plus the §5.5
    footer — the last line of a string, archives_left_out on a dict —
    counting exactly the archive(s) planted under the scope."""
    for extra in ({}, {"include_archives": False}):
        result = _run(flag_env, verb, {**params, **extra})
        if verb == "anchor.dock.list":
            count = 3  # vault scope: A, B and C all count
        else:
            count = 1  # only archive A is planted under LIVE_ROOT
        if isinstance(result, str):
            last_line = result.rstrip("\n").rsplit("\n", 1)[-1]
            assert f"📦 {count} {FOOTER_TAIL}" in last_line, (
                f"{verb}: footer missing/wrong: {last_line[:200]}"
            )
            assert WORD not in result.replace(last_line, "")
        else:
            assert result.get("success") is not False, _as_text(result)[:400]
            left = result.get("archives_left_out")
            assert isinstance(left, dict) and left.get("count") == count, (
                f"{verb}: archives_left_out wrong: {left}"
            )
            assert FOOTER_TAIL in str(left.get("line", ""))


@pytest.mark.parametrize("verb,params", FLAG_VERB_PARAMS, ids=[v for v, _ in FLAG_VERB_PARAMS])
def test_no_archive_planted_under_scope_is_byte_identical(flag_env, verb, params):
    """Scope with nothing planted under it: flag true and false are
    byte-identical, and both equal the emptied-store run."""
    scope_params = dict(params)
    for key in ("root_handle", "dock_handle"):
        if key in scope_params:
            scope_params[key] = LIVE_LEAF
    if "handle" in scope_params:
        scope_params["handle"] = {"subtree": LIVE_LEAF}
    if verb == "anchor.dock.list":
        pytest.skip("dock.list's scope is the whole vault — nothing is 'outside' it")
    if verb in ("lightning_rod.render_nodes", "lightning_rod.render_symbols"):
        scope_params["symbols"] = [{"uuid": LIVE_LEAF, "expand": True}]
        if "root_handles" in scope_params:
            scope_params["root_handles"] = LIVE_LEAF
    if verb == "slice_group.render":
        scope_params = {"lens": {"kind": "subtree", "root_handle": LIVE_LEAF}}
    if verb == "slice_group.merge.render":
        scope_params = {
            "lens": {
                "kind": "merge",
                "operation": "union",
                "inputs": [
                    {"kind": "subtree", "root_handle": LIVE_LEAF},
                    {"kind": "subtree", "root_handle": LIVE_LEAF},
                ],
            }
        }
    if verb == "slice_group.sift":
        scope_params = {
            "lens": {"kind": "subtree", "root_handle": LIVE_LEAF},
            "query": WORD,
        }
    plain = _run(flag_env, verb, scope_params)
    flagged = _run(flag_env, verb, {**scope_params, "include_archives": True})
    assert _as_text(plain) == _as_text(flagged)
    assert FOOTER_TAIL not in _as_text(plain)


def test_completion_parity_on_dock_peek(flag_env):
    """The completed archived child hides by default exactly like a live
    one, and include_completed=true lists it again."""
    shown = _run(flag_env, "anchor.dock.peek", {"dock_handle": LIVE_ROOT})
    assert "Archived done" not in shown
    listed = _run(
        flag_env,
        "anchor.dock.peek",
        {
            "dock_handle": LIVE_ROOT,
            "include_archives": True,
            "include_completed": True,
            "recursive": True,
        },
    )
    assert "Archived done" in listed


def test_suppressed_parity_in_include_render(flag_env):
    """The #wfy-suppress archived child is suppressed under include
    exactly as a live one would be — in a SLICE selection, where the
    expand/suppress plan runs (a raw dump has no Phase 2/3/4 by design)."""
    result = _run(
        flag_env,
        "slice_group.render",
        {"lens": {"kind": "group", "group_handle": "All"}, "include_archives": True},
    )
    assert isinstance(result, str)
    assert result.strip() != "(empty)"
    assert "Archived suppressed" not in result


def test_archived_dock_bounds_descent(flag_env):
    """An archived Dock bounds descent in an include render exactly like a
    live one: the archived Dock's own subtree is signposted, not entered."""
    result = _run(
        flag_env,
        "glimpse.run",
        {
            "root_handle": LIVE_ROOT,
            "include_archives": True,
            "allow_anchor_dock": True,
            "descend_policy": "stop_at_child_anchors",
        },
    )
    assert isinstance(result, str)
    assert "Archived Dock #proj" in result


def test_include_order_after_live_children(flag_env):
    """Planted archived children sort after the live children of the same
    parent (§4.2.2): in the dock.peek rows the archived root follows the
    live leaf."""
    rows = _run(
        flag_env,
        "anchor.dock.peek",
        {"dock_handle": LIVE_ROOT, "detail": "full", "include_archives": True},
    )
    names = [row.get("name") for row in rows.get("rows", [])]
    assert "Live leaf" in names and any("📦" in (n or "") for n in names)
    assert names.index("Live leaf") < min(
        i for i, n in enumerate(names) if "📦" in (n or "")
    )


# ---------------------------------------------------------------------------
# §5.9 — the refused combinations, end to end through the invoker
# ---------------------------------------------------------------------------


def test_refusal_flag_with_output_file(flag_env):
    result = _run(
        flag_env,
        "node.scry",
        {"root_handle": LIVE_ROOT, "include_archives": True, "output_file": "/tmp/x"},
    )
    assert result.get("success") is False
    assert result["error"]["code"] == "invalid_parameter_combination"
    assert "cannot be combined with output_file" in result["error"]["message"]


def test_refusal_flag_with_push_to_modal(flag_env):
    result = _run(
        flag_env,
        "glimpse.run",
        {
            "root_handle": LIVE_ROOT,
            "include_archives": True,
            "push_to_modal": True,
        },
    )
    assert result.get("success") is False
    assert result["error"]["code"] == "invalid_parameter_combination"
    assert "cannot be combined with push_to_modal" in result["error"]["message"]


def test_refusal_flag_on_glimpse_run_without_scope(flag_env):
    result = _run(flag_env, "glimpse.run", {"include_archives": True})
    assert result.get("success") is False
    assert result["error"]["code"] == "invalid_parameter_combination"
    assert "needs a server-side scope" in result["error"]["message"]


def test_refusal_leaves_no_handler_trace(flag_env, monkeypatch):
    """The refusal returns BEFORE any handler work (no scope resolution,
    no boundary adoption beyond the classifier)."""
    ran = {"scry": False}

    async def _scry(node_id, depth=None, size_limit=1000, output_file=None):
        ran["scry"] = True
        return {"success": True}

    client = flag_env["client"]
    client.workflowy_scry = _scry
    result = _run(
        flag_env,
        "node.scry",
        {"root_handle": LIVE_ROOT, "include_archives": True, "output_file": "/tmp/x"},
    )
    assert result.get("success") is False
    assert ran["scry"] is False
