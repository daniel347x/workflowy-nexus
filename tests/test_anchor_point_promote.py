"""anchor.point.promote -- batch move-with-provenance and promote-as-copy.

Real registry dispatch + the real ``_node_move_impl`` / ``_node_edit_core`` /
``_node_etch_sync_impl`` / AP-ID reservation code over a fake cache and a fake
Workflowy "server" (``get_node`` / ``update_node`` / ``move_node`` /
``workflowy_etch`` / ``list_nodes`` / ``complete_node`` doubles that keep
per-node state). Nothing here reaches the network.
"""
from __future__ import annotations

import asyncio
import html
from unittest.mock import AsyncMock

import pytest
from coherence_harness import make_boundary_capable, make_coherence_root, s04_raise_barrier
from n05_region_harness import isolate_claim_scope, publish_fixture_nodes
from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation, validate_params
from workflowy_mcp.models.node import WorkFlowyNode
from workflowy_mcp.models.requests import NodeUpdateRequest

ROOT = "00000000-0000-4000-8000-000000000000"
S = "5e551011-1111-4111-8111-111111111111"      # session Dock (LOCAL)
T = "70b1c000-2222-4222-8222-222222222222"      # topic Dock (global)
T2 = "70b1c000-3333-4333-8333-333333333333"     # second topic Dock (global)
PLAIN = "91a1a000-4444-4444-8444-444444444444"  # plain container (no Dock above it)
A = "aaaaaaaa-1111-4111-8111-111111111111"
B = "bbbbbbbb-2222-4222-8222-222222222222"
C = "cccccccc-3333-4333-8333-333333333333"
C1 = "cccccccc-3333-4333-8333-000000000001"
C2 = "cccccccc-3333-4333-8333-000000000002"     # completed child
C3 = "cccccccc-3333-4333-8333-000000000003"
C3A = "cccccccc-3333-4333-8333-00000000003a"
P = "dddddddd-5555-4555-8555-555555555555"      # under PLAIN
E = "eeeeeeee-6666-4666-8666-666666666666"
F = "ffffffff-7777-4777-8777-777777777777"      # mapped Markdown FILE
G = "99999999-8888-4888-8888-888888888888"      # child inside the mapped file
FD = "f01de000-9999-4999-8999-999999999999"     # mapped FOLDER
O = "0e9a0000-aaaa-4aaa-8aaa-aaaaaaaaaaaa"      # orphaned MD_PATH: note under S

NAME_A = "[ap:PROMA] Alpha task 📊 #keep"
NOTE_A = "alpha line one\nalpha &amp; two"
NAME_C = "🔱 [ap:PROMC] Gamma record #prog-result"
NOTE_C = "gamma body"


def _nodes():
    return [
        {"id": ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0},
        {"id": S, "name": "Session 015 #wfy-ns--local", "note": "session dock", "parent_id": ROOT, "priority": 0},
        {"id": T, "name": "[ap:TOPICA] Topic A #wfy-ns", "note": "topic", "parent_id": ROOT, "priority": 1},
        {"id": T2, "name": "Topic B #wfy-ns", "note": "", "parent_id": ROOT, "priority": 2},
        {"id": PLAIN, "name": "Plain container", "note": "", "parent_id": ROOT, "priority": 3},
        {"id": A, "name": NAME_A, "note": NOTE_A, "parent_id": S, "priority": 0,
         "data": {"layoutMode": "bullets"}},
        {"id": B, "name": "Beta &amp; co", "note": "", "parent_id": S, "priority": 1},
        {"id": C, "name": NAME_C, "note": NOTE_C, "parent_id": S, "priority": 2, "data": {"layoutMode": "h2"}},
        {"id": C1, "name": "[ap:PROMC1] Child one", "note": "c1 &lt;note&gt;", "parent_id": C, "priority": 0},
        {"id": C2, "name": "Child two (done)", "note": "", "parent_id": C, "priority": 1,
         "completed": True, "completedAt": 500},
        {"id": C3, "name": "Child three", "note": "", "parent_id": C, "priority": 2},
        {"id": C3A, "name": "[ap:PRMC3A] Grand #x", "note": "deep", "parent_id": C3, "priority": 0},
        {"id": E, "name": "Epsilon", "note": "e", "parent_id": S, "priority": 3},
        {"id": P, "name": "Under plain", "note": "", "parent_id": PLAIN, "priority": 0},
        {"id": F, "name": "📄 notes.md", "note": "Path: docs/notes.md\nbody", "parent_id": ROOT, "priority": 4},
        {"id": G, "name": "Heading inside file", "note": "", "parent_id": F, "priority": 0},
        {"id": FD, "name": "📂 docs", "note": "Path: docs", "parent_id": ROOT, "priority": 5},
        {"id": O, "name": "Orphan draft", "note": "MD_PATH: docs/old.md\nresidue", "parent_id": S, "priority": 4},
    ]


class FakeServer:
    """Per-node live state; logs every write; creates UUID-shaped ids."""

    def __init__(self, nodes):
        self.state = {}
        for node in nodes:
            self.state[node["id"]] = {
                "name": node["name"], "note": node.get("note") or "",
                "parent_id": node.get("parent_id"), "priority": node.get("priority") or 0,
                "createdAt": 100, "modifiedAt": 100, "completedAt": node.get("completedAt"),
                "data": dict(node.get("data") or {}),
            }
        self.posts: list[tuple[str, dict]] = []
        self.moves: list[tuple[str, str, str]] = []
        self.creates: list[tuple[str, dict]] = []
        self.completes: list[str] = []
        self.lists: list[str] = []
        self.gets: list[str] = []
        self.counter = 0
        self.fail_move: str | None = None          # source uuid whose move raises
        self.reject_move: str | None = None        # source uuid whose move returns False
        self.fail_update: str | None = None        # uuid whose POST raises
        self.fail_get: str | None = None           # uuid whose GET raises
        self.etch_error_on_name: str | None = None # ETCH swallows this node's failure
        self.etch_raise: bool = False
        self.list_fail: bool = False

    def node(self, uuid):
        s = self.state[uuid]
        return WorkFlowyNode(
            id=uuid, name=s["name"], note=s["note"], parentId=s["parent_id"], priority=s["priority"],
            createdAt=s["createdAt"], modifiedAt=s["modifiedAt"], completedAt=s["completedAt"],
            data=dict(s["data"]) or None,
        )

    def children(self, parent):
        return sorted(
            (uuid for uuid, s in self.state.items() if s["parent_id"] == parent),
            key=lambda u: self.state[u]["priority"],
        )

    async def get_node(self, uuid):
        self.gets.append(uuid)
        if uuid == self.fail_get:
            raise RuntimeError("simulated GET failure")
        return self.node(uuid)

    async def update_node(self, uuid, request: NodeUpdateRequest, *, basis=None):
        body = request.model_dump(exclude_none=True)
        self.posts.append((uuid, body))
        if uuid == self.fail_update:
            raise RuntimeError("simulated transport failure after POST -- outcome unknown")
        s = self.state[uuid]
        if "name" in body:
            s["name"] = html.escape(body["name"], quote=False)
        if "note" in body:
            s["note"] = html.escape(body["note"], quote=False)
        s["modifiedAt"] += 1
        return self.node(uuid)

    async def move_node(self, uuid, parent_id, position="top"):
        self.moves.append((uuid, parent_id, position))
        if uuid == self.fail_move:
            raise RuntimeError("simulated transport failure on move -- outcome unknown")
        if uuid == self.reject_move:
            return False
        s = self.state[uuid]
        s["parent_id"] = parent_id
        siblings = self.children(parent_id)
        s["priority"] = (max(self.state[x]["priority"] for x in siblings) + 1) if siblings else 0
        return True

    def _new_id(self):
        self.counter += 1
        return f"{self.counter:08x}-c0c0-4c0c-8c0c-c0c0c0c0c0c0"

    async def workflowy_etch(self, parent_id, nodes, *, fresh_root_hold=None):
        if self.etch_raise:
            raise RuntimeError("simulated ETCH transport failure")
        stats = {"created": 0, "errors": [], "roots": []}
        allowed = {"name", "note", "children", "layout_mode", "position"}

        def create(parent, items, top):
            for item in items:
                assert set(item) <= allowed, f"ETCH payload key leak: {set(item) - allowed}"
                assert isinstance(item["name"], str) and item["name"]
                if item["name"] == self.etch_error_on_name:
                    stats["errors"].append(f"Failed to create '{item['name']}': simulated")
                    continue
                uuid = self._new_id()
                kids = self.children(parent)
                self.state[uuid] = {
                    "name": html.escape(item["name"], quote=False),
                    "note": html.escape(item.get("note") or "", quote=False),
                    "parent_id": parent, "priority": len(kids), "createdAt": 200, "modifiedAt": 200,
                    "completedAt": None,
                    "data": {"layoutMode": item["layout_mode"]} if item.get("layout_mode") else {},
                }
                self.creates.append((uuid, dict(item)))
                stats["created"] += 1
                if top:
                    stats["roots"].append(uuid)
                if item.get("children"):
                    create(uuid, item["children"], False)

        create(parent_id, nodes, True)
        publish_fixture_nodes(self.client, ({"id": key, **value} for key, value in self.state.items()))
        return {
            "success": not stats["errors"], "nodes_created": stats["created"],
            "root_node_ids": stats["roots"], "api_calls": stats["created"], "retries": 0,
            "errors": stats["errors"],
        }

    async def list_nodes(self, request):
        self.lists.append(request.parentId)
        if self.list_fail:
            raise RuntimeError("simulated list failure")
        return [self.node(u) for u in self.children(request.parentId)], 0

    async def complete_node(self, uuid):
        self.completes.append(uuid)
        self.state[uuid]["completedAt"] = 999
        return self.node(uuid)


def setup(monkeypatch, tmp_path, *, nodes=None):
    nodes = nodes if nodes is not None else _nodes()
    paths = make_coherence_root(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    client = make_boundary_capable(object.__new__(server.WorkFlowyClient), nodes=nodes)
    isolate_claim_scope(client, tmp_path)
    fake = FakeServer(nodes)
    fake.client = client
    for name in ("get_node", "update_node", "move_node", "workflowy_etch", "list_nodes", "complete_node"):
        setattr(client, name, AsyncMock(side_effect=getattr(fake, name)))
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    monkeypatch.setattr(server, "_promote_today", lambda: "2026-09-19")

    async def config(_tab):
        return (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})

    config_mock = AsyncMock(side_effect=config)
    monkeypatch.setattr(server, "_resolve_note_session_config", config_mock)

    async def _noop_identity_proof(paths, *, operation):
        return None

    monkeypatch.setattr(
        "workflowy_mcp.coherence.admission.require_cache_identity_proof", _noop_identity_proof
    )
    server._ANCHOR_AP_ID_RESERVED_CORES.clear()
    return client, fake, paths, config_mock


async def dispatch_async(params):
    return await dispatch_operation(
        server.build_anchor_registry(), "point.promote", params,
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    )


def promote(**params):
    return asyncio.run(dispatch_async(params))


def rows_by_handle(result):
    return {row["handle"]: row for row in result["detail"]["results"]}


def wire_names(fake, uuids):
    return [fake.state[u]["name"] for u in uuids]


# ---------------------------------------------------------------------------
# Round trip (move)
# ---------------------------------------------------------------------------

def test_round_trip_move_ap_resolves_trail_present_tags_applied_title_identical(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = promote(items=[{"handle": "[ap:PROMA]", "destination": "[ap:TOPICA]", "tags": ["topic-a"]}])
    assert result["success"] is True and result["changed"] is True and result["partial_success"] is False
    assert result["operation"] == "anchor.point.promote"
    row = result["detail"]["results"][0]
    assert row["status"] == "promoted" and row["mode"] == "move" and row["uuid"] == A
    assert row["ap_id"] == "[ap:PROMA]"
    # Exactly 2 governed writes: the move, then ONE note+name update with the live node injected.
    assert fake.moves == [(A, T, "bottom")] and len(fake.posts) == 1
    assert fake.gets == [A]
    uuid, body = fake.posts[0]
    assert uuid == A and set(body) == {"name", "note"}
    # Origin = the LOCAL session Dock (nearest ancestor Dock), recorded as title (uuid8).
    assert row["from"] == {"uuid": S, "title": "Session 015", "is_dock": True, "is_local": True}
    assert row["to"] == {"uuid": T, "title": "Topic A"}
    assert row["trail_line"] == "↩ moved from Session 015 (5e551011) · 2026-09-19"
    # Note: trail line, blank line, old note (semantic), newest on top.
    assert html.unescape(fake.state[A]["note"]) == (
        "↩ moved from Session 015 (5e551011) · 2026-09-19\n\nalpha line one\nalpha & two"
    )
    # Title / AP-ID / emoji byte-identical; only the tag region gained the new tag.
    assert html.unescape(fake.state[A]["name"]) == "[ap:PROMA] Alpha task 📊 #keep #topic-a"
    assert row["tags_applied"] == ["#topic-a"]
    assert fake.state[A]["parent_id"] == T
    # The AP-ID still resolves against the LIVE post-move name (pure shared resolver).
    from workflowy_mcp.client.api_client_nexus import resolve_uuid_from_anchor_point

    live_map = {u: {"id": u, "name": fake.state[u]["name"]} for u in fake.state}
    assert resolve_uuid_from_anchor_point("[ap:PROMA]", live_map)["candidates"][0]["uuid"] == A
    summary = result["detail"]["summary"]
    assert summary == {**summary, "requested": 1, "promoted": 1, "moved": 1, "copied": 0,
                       "skipped": 0, "failed": 0, "not_run": 0, "stopped": False, "writes": 2}


def test_move_without_tags_writes_note_only_and_keeps_name_bytes(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    before = fake.state[B]["name"]
    result = promote(items=[{"handle": B, "destination": T2}])
    assert result["success"] is True
    uuid, body = fake.posts[0]
    assert set(body) == {"note"}                      # no name recomposition at all
    assert fake.state[B]["name"] == before
    assert html.unescape(fake.state[B]["note"]) == "↩ moved from Session 015 (5e551011) · 2026-09-19"  # empty note: no dangling blank lines
    assert rows_by_handle(result)[B]["tags_applied"] == []


def test_second_move_stacks_a_second_trail_line_on_top(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    promote(items=[{"handle": A, "destination": T}])
    monkeypatch.setattr(server, "_promote_today", lambda: "2026-09-20")
    # The pinned cache still shows A under S; the LIVE note (which the second
    # write reads) already carries the first trail line -- a node moved twice shows two lines.
    promote(items=[{"handle": A, "destination": T2}])
    lines = html.unescape(fake.state[A]["note"]).split("\n")
    assert lines[0] == "↩ moved from Session 015 (5e551011) · 2026-09-20"
    assert lines[2] == "↩ moved from Session 015 (5e551011) · 2026-09-19"
    assert lines[4] == "alpha line one"


def test_origin_falls_back_to_immediate_parent_when_no_dock_ancestor(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = promote(items=[{"handle": P, "destination": T}])
    row = result["detail"]["results"][0]
    assert row["from"] == {"uuid": PLAIN, "title": "Plain container", "is_dock": False, "is_local": False}
    assert row["trail_line"] == "↩ moved from Plain container (91a1a000) · 2026-09-19"


def test_origin_is_nearest_dock_not_immediate_parent_when_nested(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    # C3A sits under C3 (plain) under C (plain) under S (Dock): origin = S.
    result = promote(items=[{"handle": "[ap:PRMC3A]", "destination": T}])
    row = result["detail"]["results"][0]
    assert row["from"]["uuid"] == S and row["from"]["is_dock"] is True


# ---------------------------------------------------------------------------
# Refusals (zero writes)
# ---------------------------------------------------------------------------

def test_node_under_mapped_file_is_refused_with_zero_writes(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = promote(items=[{"handle": G, "destination": T}])
    row = result["detail"]["results"][0]
    assert row["status"] == "skipped" and row["reason"] == "mapped_node_refused"
    assert "inside_file" in row["detail"] and "disk is truth" in row["detail"]
    assert fake.moves == [] and fake.posts == [] and fake.creates == []
    assert result["success"] is False and result["changed"] is False and result["partial_success"] is False


def test_destination_under_mapped_folder_is_refused_with_zero_writes(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = promote(items=[{"handle": A, "destination": FD}, {"handle": B, "destination": F}])
    rows = rows_by_handle(result)
    assert rows[A]["reason"] == "mapped_node_refused" and "destination" in rows[A]["detail"] and "folder" in rows[A]["detail"]
    assert rows[B]["reason"] == "mapped_node_refused" and "state=file" in rows[B]["detail"]   # Markdown too: stricter than capture
    assert fake.moves == [] and fake.posts == []


def test_orphaned_md_path_note_is_refused_with_zero_writes(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    assert server._classify_node_map_state(O, {n["id"]: n for n in _nodes()})["state"] == "unmapped"
    result = promote(items=[{"handle": O, "destination": T, "mode": "derivative"}])
    row = result["detail"]["results"][0]
    assert row["status"] == "skipped" and row["reason"] == "mapped_node_refused"
    assert "MD_PATH:" in row["detail"]
    assert fake.moves == [] and fake.posts == [] and fake.creates == []


@pytest.mark.parametrize("first", ["AST_QUALNAME: pkg.mod.fn", "MD_PATH: a/b.md", "BEACON (x)"])
def test_first_line_metadata_regex(first):
    assert server._promote_mapped_metadata_reason(html.escape(first + "\nbody")) is not None
    assert server._promote_mapped_metadata_reason("text mentioning MD_PATH: later\nMD_PATH: x") is None
    assert server._promote_mapped_metadata_reason("") is None


# ---------------------------------------------------------------------------
# Batch semantics
# ---------------------------------------------------------------------------

def test_one_bad_handle_among_five_four_land_and_the_bad_one_is_named(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = promote(handles=[A, "[ap:NOSUCH]", B, E, P], destination=T, tags=["t"])
    assert result["success"] is False and result["partial_success"] is True and result["changed"] is True
    rows = rows_by_handle(result)
    assert rows["[ap:NOSUCH]"]["status"] == "skipped" and rows["[ap:NOSUCH]"]["reason"] == "unresolvable_handle"
    assert [rows[h]["status"] for h in (A, B, E, P)] == ["promoted"] * 4
    assert [m[0] for m in fake.moves] == [A, B, E, P]
    assert all(fake.state[u]["parent_id"] == T for u in (A, B, E, P))
    summary = result["detail"]["summary"]
    assert summary == {**summary, "requested": 5, "promoted": 4, "skipped": 1, "failed": 0, "not_run": 0, "writes": 8}


def test_several_bad_items_every_good_item_still_lands(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = promote(items=[
        {"handle": "[ap:NOSUCH]", "destination": T},
        {"handle": A, "destination": T},
        {"handle": G, "destination": T},                    # mapped source
        {"handle": B, "destination": "[ap:NODEST]"},        # unresolvable destination
        {"handle": E, "destination": T2},
        {"handle": P, "destination": FD},                   # mapped destination
    ])
    rows = rows_by_handle(result)
    assert rows[A]["status"] == "promoted" and rows[E]["status"] == "promoted"
    assert rows["[ap:NOSUCH]"]["reason"] == "unresolvable_handle"
    assert rows[G]["reason"] == "mapped_node_refused"
    assert rows[B]["reason"] == "unresolvable_destination"
    assert rows[P]["reason"] == "mapped_node_refused"
    assert [m[0] for m in fake.moves] == [A, E]
    assert result["partial_success"] is True
    assert result["detail"]["summary"]["skipped"] == 4 and result["detail"]["summary"]["promoted"] == 2


def test_five_good_five_moves_one_ledger(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = promote(handles=[A, B, C, E, P], destination=T)
    assert result["success"] is True and result["partial_success"] is False
    assert [m for m in fake.moves] == [(u, T, "bottom") for u in (A, B, C, E, P)]
    assert len(fake.posts) == 5 and len(result["detail"]["results"]) == 5
    assert all(r["status"] == "promoted" for r in result["detail"]["results"])
    # The subtree under C moved WITH it (a move never detaches children).
    assert fake.state[C1]["parent_id"] == C


def test_cycle_destination_inside_source_subtree_refuses_the_batch(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    for dest in (C1, C3A, C):
        result = promote(items=[{"handle": A, "destination": T}, {"handle": C, "destination": dest}])
        assert result["success"] is False and result["error"]["code"] == "promote_cycle_refused"
        assert fake.moves == [] and fake.posts == []       # the GOOD item A was not moved either: zero writes


@pytest.mark.parametrize("items", [
    [{"handle": A, "destination": T}, {"handle": A, "destination": T2}],
    [{"handle": A, "destination": T}, {"handle": "[ap:PROMA]", "destination": T2, "mode": "derivative"}],
    [{"handle": "[ap:proma]", "destination": T}, {"handle": "[ap:PROMA]", "destination": T}],
    [{"handle": A[:8], "destination": T}, {"handle": A, "destination": T}],
])
def test_duplicate_handles_refuse_the_whole_batch_with_zero_writes(monkeypatch, tmp_path, items):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = promote(items=items)
    assert result["success"] is False and result["error"]["code"] == "duplicate_handles"
    assert fake.moves == [] and fake.posts == [] and fake.creates == []


@pytest.mark.parametrize("params,code", [
    ({}, "invalid_parameter_combination"),
    ({"items": [{"handle": A, "destination": T}], "handles": [B], "destination": T}, "invalid_parameter_combination"),
    ({"items": [{"handle": A, "destination": T}], "destination": T}, "invalid_parameter_combination"),
    ({"handles": [A]}, "invalid_parameter_type"),                       # shorthand without destination
    ({"handles": [], "destination": T}, "invalid_parameter_value"),
    ({"items": []}, "invalid_parameter_value"),
    ({"items": [{"handle": A}]}, "invalid_parameter_value"),
    ({"items": [{"handle": A, "destination": T, "bogus": 1}]}, "invalid_parameter_value"),
    ({"items": [{"handle": A, "destination": T, "mode": "copy"}]}, "invalid_parameter_value"),
    ({"items": [{"handle": A, "destination": T, "children": "no"}]}, "invalid_parameter_type"),
    ({"items": [{"handle": A, "destination": T, "tags": ["a b"]}]}, "invalid_parameter_value"),
    ({"items": [{"handle": 7, "destination": T}]}, "invalid_parameter_type"),
    ({"items": ["not-an-object"]}, "invalid_parameter_type"),
    ({"handles": [A], "destination": T, "include_completed": "yes"}, "invalid_parameter_type"),
    ({"handles": [A], "destination": T, "mode": "teleport"}, "invalid_enum_value"),
    ({"handles": [A], "destination": T, "order": "desc"}, "forbidden_parameter"),
])
def test_malformed_batches_refuse_with_zero_writes(monkeypatch, tmp_path, params, code):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = promote(**params)
    assert result["success"] is False and result["error"]["code"] == code, result
    assert fake.moves == [] and fake.posts == [] and fake.creates == []


def test_unknown_move_outcome_stops_without_resending(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    fake.fail_move = B
    result = promote(handles=[A, B, E], destination=T)
    rows = rows_by_handle(result)
    assert rows[A]["status"] == "promoted"
    assert rows[B]["status"] == "failed" and rows[B]["reason"] == "write_outcome_unknown" and rows[B]["moved"] is False
    assert rows[E]["status"] == "not_run" and rows[E]["reason"] == "batch_stopped"
    assert [m[0] for m in fake.moves] == [A, B]          # B not re-sent, E never attempted
    assert result["success"] is False and result["partial_success"] is True and result["changed"] is True
    summary = result["detail"]["summary"]
    assert summary["stopped"] is True and summary["stop_reason"] == "write_outcome_unknown"
    assert any("STOPPED" in w for w in result["warnings"])


def test_unknown_trail_write_outcome_stops_and_reports_moved_without_trail(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    fake.fail_update = A
    result = promote(handles=[A, B], destination=T)
    rows = rows_by_handle(result)
    assert rows[A]["status"] == "failed" and rows[A]["reason"] == "write_outcome_unknown"
    assert rows[A]["moved"] is True and rows[A]["trail_written"] is False
    assert rows[B]["status"] == "not_run"
    assert fake.moves == [(A, T, "bottom")] and [p[0] for p in fake.posts] == [A]
    assert result["changed"] is True                    # the move DID commit ...
    assert result["partial_success"] is False           # ... but no item fully landed: not a partial success


def test_move_rejected_is_a_conclusive_per_item_failure_and_the_batch_continues(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    fake.reject_move = A
    result = promote(handles=[A, B], destination=T)
    rows = rows_by_handle(result)
    assert rows[A]["status"] == "failed" and rows[A]["reason"] == "move_rejected"
    assert rows[B]["status"] == "promoted"
    assert [p[0] for p in fake.posts] == [B]            # no trail write for the unmoved A


def test_trail_read_failure_after_move_is_reported_and_the_batch_continues(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    fake.fail_get = A
    result = promote(handles=[A, B], destination=T)
    rows = rows_by_handle(result)
    assert rows[A]["status"] == "failed" and rows[A]["reason"] == "trail_read_failed"
    assert rows[A]["moved"] is True and rows[A]["trail_written"] is False
    assert fake.state[A]["parent_id"] == T and fake.state[A]["note"] == NOTE_A
    assert rows[B]["status"] == "promoted"


def test_hard_barrier_refuses_before_any_read_or_write(monkeypatch, tmp_path):
    client, fake, paths, _ = setup(monkeypatch, tmp_path)
    s04_raise_barrier(paths)
    result = promote(handles=[A], destination=T)
    assert result["error"]["code"] == "coherence_barrier_active"
    assert fake.moves == [] and fake.posts == [] and fake.gets == []


def test_mid_batch_coherence_refusal_keeps_universal_envelope_and_partial_ledger(monkeypatch, tmp_path):
    from workflowy_mcp.coherence.errors import CoherenceBarrierActiveError

    client, fake, paths, _ = setup(monkeypatch, tmp_path)
    original = fake.move_node

    async def move(uuid, parent_id, position="top"):
        if uuid == B:
            raise CoherenceBarrierActiveError("barrier raised mid-batch")
        return await original(uuid, parent_id, position)

    client.move_node = AsyncMock(side_effect=move)
    result = promote(handles=[A, B, E], destination=T)
    assert result["success"] is False and result["error"]["code"] == "coherence_barrier_active"
    ledger = result["error"]["extra"]["promote"]
    statuses = {row["handle"]: row["status"] for row in ledger["results"]}
    assert statuses == {A: "promoted", B: "failed", E: "not_run"}
    assert ledger["summary"]["stop_reason"] == "coherence_barrier_active"


def test_one_dock_vocabulary_read_per_batch_and_none_when_nothing_is_eligible(monkeypatch, tmp_path):
    client, fake, paths, config_mock = setup(monkeypatch, tmp_path)
    promote(handles=[A, B, E], destination=T)
    assert config_mock.await_count == 1
    config_mock.reset_mock()
    promote(handles=[G, "[ap:NOSUCH]"], destination=T)
    assert config_mock.await_count == 0


# ---------------------------------------------------------------------------
# Derivative (promote-as-copy)
# ---------------------------------------------------------------------------

def _snapshot(fake, uuids):
    return {u: dict(fake.state[u], data=dict(fake.state[u]["data"])) for u in uuids}


def test_derivative_source_byte_identical_fresh_ap_ids_trail_and_tags(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    source_ids = (C, C1, C2, C3, C3A)
    before = _snapshot(fake, source_ids)
    result = promote(items=[{"handle": "[ap:PROMC]", "destination": "[ap:TOPICA]", "mode": "derivative", "tags": ["reusable"]}])
    assert result["success"] is True and result["changed"] is True
    row = result["detail"]["results"][0]
    assert row["status"] == "promoted" and row["mode"] == "derivative" and row["ap_id"] == "[ap:PROMC]"
    # (1) SOURCE byte-identical: name, note, parent, priority, completion, children -- and no source write at all.
    assert _snapshot(fake, source_ids) == before
    assert fake.moves == [] and fake.posts == [] and all(u != C for u, _ in fake.creates)
    # ONE subtree ETCH: root + C1 + C3 + C3A (C2 is completed -> skipped by default) = 4 creates.
    assert len(fake.creates) == 4 and row["copy"]["expected_nodes"] == 4 and row["copy"]["nodes_created"] == 4
    assert row["copy"]["completed_skipped"] == 1 and row["copy"]["children_copied"] is True
    copy_root = row["copy"]["uuid"]
    assert fake.state[copy_root]["parent_id"] == T
    # (2) Fresh, collision-free AP-IDs on the root and EVERY AP-bearing child; plain children get none.
    root_name = html.unescape(fake.state[copy_root]["name"])
    new_core = server._anchor_leading_ap_core(root_name)
    assert new_core and new_core != "PROMC" and row["copy"]["ap_id"] == f"[ap:{new_core}]"
    assert root_name == f"🔱 [ap:{new_core}] Gamma record #prog-result #reusable"   # decoration + title + tags preserved, tag appended
    kids = fake.children(copy_root)
    assert len(kids) == 2
    k1_name = html.unescape(fake.state[kids[0]]["name"])
    k1_core = server._anchor_leading_ap_core(k1_name)
    assert k1_core and k1_core not in {"PROMC1", "PROMC", new_core} and k1_name == f"[ap:{k1_core}] Child one"
    assert html.unescape(fake.state[kids[0]]["note"]) == "c1 <note>"                    # decoded once, encoded once
    assert fake.state[kids[1]]["name"] == "Child three" and server._anchor_leading_ap_core(fake.state[kids[1]]["name"]) is None
    grand = fake.children(kids[1])
    g_core = server._anchor_leading_ap_core(fake.state[grand[0]]["name"])
    assert g_core and g_core not in {"PRMC3A", new_core, k1_core}
    assert html.unescape(fake.state[grand[0]]["name"]) == f"[ap:{g_core}] Grand #x"
    # Every core reserved was distinct and no created name carries a SOURCE core (zero transient duplicates).
    created_cores = [server._anchor_leading_ap_core(html.unescape(item["name"])) for _, item in fake.creates]
    created_cores = [c for c in created_cores if c]
    assert len(created_cores) == len(set(created_cores)) == 3
    assert not ({"PROMC", "PROMC1", "PRMC3A"} & set(created_cores))
    assert new_core in server._ANCHOR_AP_ID_ALLOCATED_CORES and not server._ANCHOR_AP_ID_RESERVED_CORES
    # Trail line names origin, date AND the exact source AP/UUID; the rest of the note follows.
    assert row["trail_line"] == "↩ derived from Session 015 (5e551011) · 2026-09-19 · source [ap:PROMC] cccccccc"
    assert html.unescape(fake.state[copy_root]["note"]) == row["trail_line"] + "\n\ngamma body"
    assert fake.state[copy_root]["data"] == {"layoutMode": "h2"}
    assert row["tags_applied"] == ["#reusable"]
    assert row["to"] == {"uuid": T, "title": "Topic A"}
    summary = result["detail"]["summary"]
    assert summary == {**summary, "promoted": 1, "moved": 0, "copied": 1, "writes": 4}


def test_derivative_of_ap_less_leaf_gets_a_new_root_ap_id_and_children_false_copies_root_alone(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = promote(items=[
        {"handle": B, "destination": T, "mode": "derivative"},
        {"handle": C, "destination": T2, "mode": "derivative", "children": False},
    ])
    assert result["success"] is True
    rows = rows_by_handle(result)
    b_copy = rows[B]["copy"]["uuid"]
    b_name = html.unescape(fake.state[b_copy]["name"])
    assert server._anchor_leading_ap_core(b_name) and b_name.endswith(" Beta & co")
    assert rows[B]["trail_line"] == "↩ derived from Session 015 (5e551011) · 2026-09-19 · source bbbbbbbb"
    assert html.unescape(fake.state[b_copy]["note"]) == rows[B]["trail_line"]       # empty source note: trail alone
    c_copy = rows[C]["copy"]["uuid"]
    assert fake.children(c_copy) == [] and rows[C]["copy"]["expected_nodes"] == 1
    assert rows[C]["copy"]["children_copied"] is False and rows[C]["copy"]["completed_skipped"] == 0
    assert fake.state[c_copy]["parent_id"] == T2
    assert len(fake.creates) == 2


def test_derivative_include_completed_copies_and_recompletes(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = promote(items=[{"handle": C, "destination": T, "mode": "derivative"}], include_completed=True)
    assert result["success"] is True, result
    row = result["detail"]["results"][0]
    assert row["copy"]["expected_nodes"] == 5 and row["copy"]["nodes_created"] == 5
    assert row["copy"]["completed_skipped"] is None
    copy_root = row["copy"]["uuid"]
    kids = fake.children(copy_root)
    assert [fake.state[k]["name"].split(" ")[-1] for k in kids][1] == "(done)"
    done_copy = kids[1]
    assert fake.completes == [done_copy] and fake.state[done_copy]["completedAt"] == 999
    assert fake.state[C2]["completedAt"] == 500                                 # source untouched
    assert row["copy"]["completion"] == {"requested": 1, "recompleted": 1, "unlocated": 0}
    assert fake.lists[0] == copy_root and len(fake.lists) == 2                 # one list read per copied parent that has children (root, Child three)
    assert result["detail"]["summary"]["writes"] == 6


def test_derivative_include_completed_list_mismatch_warns_never_guesses(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    fake.list_fail = True
    result = promote(items=[{"handle": C, "destination": T, "mode": "derivative"}], include_completed=True)
    row = result["detail"]["results"][0]
    assert row["status"] == "promoted" and fake.completes == []
    assert row["copy"]["completion"] == {"requested": 1, "recompleted": 0, "unlocated": 1}
    assert any("completion state not reproduced" in w for w in result["warnings"])


def test_derivative_partial_forest_is_a_failed_row_source_untouched_batch_continues(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    fake.etch_error_on_name = "Child three"
    before = _snapshot(fake, (C, C1, C2, C3, C3A))
    result = promote(items=[
        {"handle": C, "destination": T, "mode": "derivative"},
        {"handle": A, "destination": T},
    ])
    rows = rows_by_handle(result)
    assert rows[C]["status"] == "failed" and rows[C]["reason"] == "copy_incomplete"
    assert rows[C]["copy"]["nodes_created"] == 2 and rows[C]["copy"]["expected_nodes"] == 4
    assert rows[C]["copy"]["uuid"] is not None and "clean up" in rows[C]["detail"]
    assert _snapshot(fake, (C, C1, C2, C3, C3A)) == before
    assert rows[A]["status"] == "promoted"                                   # conclusive failure: batch continues
    assert result["changed"] is True and result["partial_success"] is True
    assert not server._ANCHOR_AP_ID_RESERVED_CORES


def test_derivative_etch_transport_exception_is_a_failed_row_with_zero_created_and_releases_reservations(monkeypatch, tmp_path):
    """The ETCH front swallows transport exceptions into errors[] with the
    nodes_created count so far (here 0): a conclusive copy_incomplete row, the
    reservations released, nothing landed, and the batch continues."""
    client, fake, *_ = setup(monkeypatch, tmp_path)
    fake.etch_raise = True
    result = promote(items=[
        {"handle": C, "destination": T, "mode": "derivative"},
        {"handle": A, "destination": T},
    ])
    rows = rows_by_handle(result)
    assert rows[C]["status"] == "failed" and rows[C]["reason"] == "copy_incomplete"
    assert rows[C]["copy"]["nodes_created"] == 0 and rows[C]["copy"]["uuid"] is None
    assert "simulated ETCH transport failure" in rows[C]["detail"]
    assert rows[A]["status"] == "promoted"
    assert fake.creates == [] and result["changed"] is True and result["partial_success"] is True
    assert not server._ANCHOR_AP_ID_RESERVED_CORES
    # Nothing was created, so no reserved core was committed as allocated by this item.
    assert rows[C]["copy"]["ap_id"][4:-1] not in server._ANCHOR_AP_ID_ALLOCATED_CORES


def test_derivative_etch_front_raising_stops_the_batch(monkeypatch, tmp_path):
    """If the ETCH front itself ever raised (outcome unknown), the batch stops."""
    client, fake, *_ = setup(monkeypatch, tmp_path)

    async def boom(parent_id, nodes=None, nodes_file=None):
        raise RuntimeError("front raised")

    monkeypatch.setattr(server, "_node_etch_sync_impl", boom)
    result = promote(items=[
        {"handle": C, "destination": T, "mode": "derivative"},
        {"handle": A, "destination": T},
    ])
    rows = rows_by_handle(result)
    assert rows[C]["status"] == "failed" and rows[C]["reason"] == "write_outcome_unknown"
    assert rows[A]["status"] == "not_run" and fake.moves == []
    assert result["detail"]["summary"]["stop_reason"] == "write_outcome_unknown"
    assert not server._ANCHOR_AP_ID_RESERVED_CORES


def test_mixed_move_and_derivative_batch(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    before_c = _snapshot(fake, (C, C1))
    result = promote(items=[
        {"handle": A, "destination": T, "tags": ["topic-a"]},
        {"handle": "[ap:PROMC]", "destination": T2, "mode": "derivative", "tags": ["reusable"]},
        {"handle": B, "destination": T},
    ])
    assert result["success"] is True and result["partial_success"] is False
    rows = result["detail"]["results"]
    assert [r["mode"] for r in rows] == ["move", "derivative", "move"]
    assert [r["status"] for r in rows] == ["promoted"] * 3
    assert [m[0] for m in fake.moves] == [A, B] and fake.state[A]["parent_id"] == T
    assert _snapshot(fake, (C, C1)) == before_c and fake.state[C]["parent_id"] == S
    assert fake.state[rows[1]["copy"]["uuid"]]["parent_id"] == T2
    assert rows[0]["trail_line"].startswith("↩ moved from") and rows[1]["trail_line"].startswith("↩ derived from")
    summary = result["detail"]["summary"]
    assert summary == {**summary, "promoted": 3, "moved": 2, "copied": 1}


def test_derivative_never_invokes_point_extract(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    extract = AsyncMock()
    monkeypatch.setattr(server, "_anchor_point_extract_impl", extract)
    promote(items=[{"handle": C, "destination": T, "mode": "derivative"}])
    extract.assert_not_awaited()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_normalize_promote_items_shorthand_and_granular():
    norm = server.normalize_promote_items
    assert norm(handles=[" a ", "b"], destination=" d ", tags="x", mode=None, children=None) == [
        {"handle": "a", "destination": "d", "tags": ["#x"], "mode": "move", "children": True},
        {"handle": "b", "destination": "d", "tags": ["#x"], "mode": "move", "children": True},
    ]
    assert norm(items=[{"handle": "a", "destination": "d", "tags": ["#p", "p", "q"], "mode": "Derivative", "children": False}]) == [
        {"handle": "a", "destination": "d", "tags": ["#p", "#q"], "mode": "derivative", "children": False},
    ]


def test_nearest_anchor_dock_ancestor_pure():
    nodes = {n["id"]: n for n in _nodes()}
    origin = server.nearest_anchor_dock_ancestor(nodes, C3A, ["wfy-ns"], ["local"])
    assert origin == {"uuid": S, "title": "Session 015", "is_dock": True, "is_local": True}
    assert server.nearest_anchor_dock_ancestor(nodes, P, ["wfy-ns"], ["local"])["uuid"] == PLAIN
    assert server.nearest_anchor_dock_ancestor(nodes, S, ["wfy-ns"], ["local"]) == {
        "uuid": ROOT, "title": "Root", "is_dock": False, "is_local": False}
    assert server.nearest_anchor_dock_ancestor(nodes, ROOT, ["wfy-ns"], ["local"])["title"] == "(root)"


def test_enumerate_subtree_document_order_and_completed_policy():
    nodes = {n["id"]: n for n in _nodes()}
    plan = server._promote_enumerate_subtree(nodes, C, children=True, include_completed=False)
    assert [e["uuid"] for e in plan] == [C, C1, C3, C3A]
    assert [e["depth"] for e in plan] == [0, 1, 1, 2]
    plan = server._promote_enumerate_subtree(nodes, C, children=True, include_completed=True)
    assert [e["uuid"] for e in plan] == [C, C1, C2, C3, C3A] and plan[2]["completed"] is True
    assert [e["uuid"] for e in server._promote_enumerate_subtree(nodes, C, children=False, include_completed=True)] == [C]


def test_prepend_trail_and_is_within():
    assert server._promote_prepend_trail("T", "") == "T"
    assert server._promote_prepend_trail("T", "  \n") == "T"
    assert server._promote_prepend_trail("T", "old\nnote") == "T\n\nold\nnote"
    nodes = {n["id"]: n for n in _nodes()}
    assert server._promote_is_within(nodes, C3A, C) and server._promote_is_within(nodes, C, C)
    assert not server._promote_is_within(nodes, A, C)


# ---------------------------------------------------------------------------
# Discoverability
# ---------------------------------------------------------------------------

def test_registry_describe_examples_and_top_level_description():
    registry = server.build_anchor_registry()
    spec = registry.resolve("point.promote")
    assert spec.cache_backed is True and spec.side_effects == ("workflowy_mutation",)
    assert "\n" not in spec.summary and "↩ moved from" in spec.summary and "derivative" in spec.summary
    props = spec.params_schema["properties"]
    assert set(props) == {"items", "handles", "destination", "tags", "mode", "children", "include_completed", "target_tab"}
    assert "required" not in spec.params_schema and spec.params_schema["additionalProperties"] is False
    assert list(props["mode"]["enum"]) == ["move", "derivative", None]
    examples = registry.describe_full("point.promote")["examples"]
    assert len(examples) == 4
    modes = [tuple(i.get("mode", "move") for i in ex["call"]["params"].get("items", [])) for ex in examples]
    assert modes[0] == ("move", "move") and modes[1] == ("derivative",) and modes[2] == ("move", "derivative")
    assert "handles" in examples[3]["call"]["params"]
    for example in examples:
        validate_params(spec, example["call"]["params"])
    for token in ("promoted|skipped|failed|not_run", "mapped_node_refused", "promote_cycle_refused",
                  "write_outcome_unknown", "copy_incomplete", "disk is truth", "never a new live dispatch"):
        assert token in spec.returns["description"], token
    # U01 added the timeout code; N05 ([ap:4UUBNV] §6.8) adds the conflict
    # and region codes on every affected mutation verb ([ap:ZU7ZWM]).
    assert set(spec.domain_error_codes) == {
        "duplicate_handles", "invalid_parameter_value", "promote_cycle_refused",
        "mutation_serialization_timeout", "mutation_serialization_conflict", "mutation_region_error",
        server.ConcurrentEditDetectedError.code,
    }
    index = "\n".join(registry.index_lines())
    assert "point.promote — Batch move-with-provenance" in index
    # G8-5d ([ap:O3MDOY] h1) appends anchor.search at the END of the specs
    # tuple (the sequencing rule: never beside dock.peek/dock.render), so
    # promote is no longer the tuple's last verb.
    assert [s.verb for s in registry.operations()][-3:] == ["point.touch", "point.promote", "search"]
    desc = server._ANCHOR_TOOL_DESCRIPTION
    for token in ("anchor.point.promote", "↩ moved from", "derivative", "sealed", "Bad items are skipped"):
        assert token in desc, token


def test_node_edit_name_describe_text_teaches_tag_omission_despite_full_name_reads():
    text = server.build_node_registry().resolve("edit").params_schema["properties"]["name"]["description"]
    assert "dock.peek" in text and "full name" in text and "title only" in text


# ---------------------------------------------------------------------------
# Managed metadata ([ap:BNNUZ4]): a recognised block skips the trail line
# ---------------------------------------------------------------------------

PROG = "70b1c000-4444-4444-8444-444444444444"    # program root (managed note)
PROG_NOTE = (
    "# Old program\n\n```yaml\nprogram_interface:\n  name: p\n"
    "  sessions: []\n```"
)


def _nodes_with_program_root():
    return _nodes() + [
        {"id": PROG, "name": "[ap:PROGRM] Old program #prog-root", "note": PROG_NOTE,
         "parent_id": S, "priority": 5},
    ]


def test_move_of_a_program_root_skips_the_trail_line_and_reports_info(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path, nodes=_nodes_with_program_root())
    result = promote(items=[{"handle": PROG, "destination": T}])
    row = rows_by_handle(result)[PROG]
    assert row["status"] == "promoted" and row["moved"] is True
    assert row["trail_written"] is False
    assert row["info"] == "trail line skipped: the note carries managed metadata (program_interface)"
    # The computed would-be line stays in the ledger for provenance...
    assert row["trail_line"] == "↩ moved from Session 015 (5e551011) · 2026-09-19"
    # ...and the node itself moved with its note byte-untouched (no trail line).
    assert fake.moves == [(PROG, T, "bottom")]
    assert html.unescape(fake.state[PROG]["note"]) == PROG_NOTE
    # No second write at all when there is nothing else to change.
    assert all("note" not in body for uuid, body in fake.posts if uuid == PROG)


def test_move_of_managed_note_still_applies_requested_tags_but_no_trail(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path, nodes=_nodes_with_program_root())
    result = promote(items=[{"handle": PROG, "destination": T, "tags": ["topic-a"]}])
    row = rows_by_handle(result)[PROG]
    assert row["status"] == "promoted" and row["moved"] is True
    assert row["trail_written"] is False
    assert row["info"] == "trail line skipped: the note carries managed metadata (program_interface)"
    assert row["tags_applied"] == ["#topic-a"]
    # The tags-only edit touched the NAME, never the note.
    assert all("note" not in body for uuid, body in fake.posts if uuid == PROG)
    assert "#topic-a" in html.unescape(fake.state[PROG]["name"])
    assert html.unescape(fake.state[PROG]["note"]) == PROG_NOTE


def test_derivative_of_a_program_root_copies_the_note_without_the_derived_line(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path, nodes=_nodes_with_program_root())
    result = promote(items=[{"handle": PROG, "destination": T, "mode": "derivative", "children": False}])
    row = rows_by_handle(result)[PROG]
    assert row["status"] == "promoted" and row["reason"] is None
    assert row["info"] == "derived-from line skipped: the note carries managed metadata (program_interface)"
    assert row["trail_line"] == "↩ derived from Session 015 (5e551011) · 2026-09-19 · source [ap:PROGRM] 70b1c000"
    copy_root = row["copy"]["uuid"]
    assert copy_root and fake.creates
    copied_note = html.unescape(fake.state[copy_root]["note"])
    assert copied_note == PROG_NOTE          # the derived line is NOT baked in
    assert not copied_note.startswith("↩")
    # The source is untouched (derivative never writes it).
    assert html.unescape(fake.state[PROG]["note"]) == PROG_NOTE


def test_ordinary_note_still_gets_its_trail_line_and_no_info(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = promote(items=[{"handle": A, "destination": T}])
    row = rows_by_handle(result)[A]
    assert row["status"] == "promoted" and row["moved"] is True and row["trail_written"] is True
    assert row["info"] is None
    assert html.unescape(fake.state[A]["note"]).startswith("↩ moved from Session 015 (5e551011) · 2026-09-19")


def test_describe_explains_the_managed_metadata_rule(monkeypatch, tmp_path):
    registry = server.build_anchor_registry()
    spec = registry.resolve("point.promote")
    assert "managed_metadata.py" in spec.summary and "NO trail line" in spec.summary
    for token in ("info?", "trail line skipped", "derived-from line skipped", "managed metadata"):
        assert token in spec.returns["description"], token
    desc = server._ANCHOR_TOOL_DESCRIPTION
    assert "managed_metadata.py" in desc and "NO trail line" in desc


@pytest.mark.parametrize("mode", ["move", "derivative"])
def test_per_line_estate_survives_real_promote_path(monkeypatch, tmp_path, mode):
    from workflowy_mcp.glimpse_estate_payload import parse_estate_note

    note = "Recruiting\n[ap:K7M2QF]\n3f3754e2"
    nodes = _nodes()
    next(n for n in nodes if n["id"] == A)["note"] = note
    client, fake, *_ = setup(monkeypatch, tmp_path, nodes=nodes)
    expected, error = parse_estate_note(note)
    assert error is None and expected
    result = promote(items=[{"handle": A, "destination": T, "mode": mode, "children": False}])
    row = rows_by_handle(result)[A]
    assert row["status"] == "promoted"
    target = A if mode == "move" else row["copy"]["uuid"]
    actual, error = parse_estate_note(html.unescape(fake.state[target]["note"]))
    assert error is None and actual == expected
    if mode == "derivative":
        assert fake.state[A]["note"] == note  # source never rewritten


@pytest.mark.parametrize("mode", ["move", "derivative"])
def test_invalid_json_example_promote_keeps_provenance(monkeypatch, tmp_path, mode):
    note = 'Payload {"title": "x"} inline'
    nodes = _nodes()
    next(n for n in nodes if n["id"] == A)["note"] = note
    client, fake, *_ = setup(monkeypatch, tmp_path, nodes=nodes)
    result = promote(items=[{"handle": A, "destination": T, "mode": mode, "children": False}])
    row = rows_by_handle(result)[A]
    assert row["status"] == "promoted" and row["info"] is None
    target = A if mode == "move" else row["copy"]["uuid"]
    assert html.unescape(fake.state[target]["note"]) == row["trail_line"] + "\n\n" + note
