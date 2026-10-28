"""anchor.point.touch -- ordered batch, content-preserving modifiedAt advance.

Real registry dispatch + real ``_node_edit_core`` / ``WorkFlowyClient`` code
over a fake cache and a fake Workflowy "server" (``get_node`` / ``update_node``
doubles that keep per-node state and stamp ``modifiedAt`` from a controllable
integer-second clock). Nothing here reaches the network.
"""
from __future__ import annotations

import asyncio
import html
import typing
from unittest.mock import AsyncMock

import pytest
from coherence_harness import make_boundary_capable, make_coherence_root, s04_raise_barrier
from n05_region_harness import isolate_claim_scope
from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation, validate_params
from workflowy_mcp.models.node import WorkFlowyNode
from workflowy_mcp.models.requests import NodeUpdateRequest

A = "aaaaaaaa-1111-4111-8111-111111111111"
B = "bbbbbbbb-2222-4222-8222-222222222222"
C = "cccccccc-3333-4333-8333-333333333333"
D = "dddddddd-4444-4444-8444-444444444444"      # Anchor Dock root
F = "ffffffff-5555-4555-8555-555555555555"      # mapped Markdown FILE node
G = "99999999-6666-4666-8666-666666666666"      # child inside the mapped file
ROOT = "00000000-0000-4000-8000-000000000000"

NAME_A = "[ap:TOUCHA] Alpha task 📊 #topic-x #keep"
NAME_B = "Beta task &amp; friends"
NAME_C = "[ap:TOUCHC] Gamma"
NOTE_A = "line one\nline &amp; two\n\nthree"


def _nodes():
    return [
        {"id": ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0,
         "createdAt": 50, "modifiedAt": 50},
        {"id": D, "name": "TODO Dock #wfy-ns", "note": "a dock", "parent_id": ROOT, "priority": 0,
         "createdAt": 60, "modifiedAt": 60},
        {"id": A, "name": NAME_A, "note": NOTE_A, "parent_id": D, "priority": 0,
         "createdAt": 100, "modifiedAt": 100, "data": {"layoutMode": "bullets"}},
        {"id": B, "name": NAME_B, "note": "", "parent_id": D, "priority": 1,
         "createdAt": 101, "modifiedAt": 300},
        {"id": C, "name": NAME_C, "note": "c", "parent_id": D, "priority": 2,
         "createdAt": 102, "modifiedAt": 200, "data": {"layoutMode": "todo"}},
        {"id": F, "name": "📄 notes.md", "note": "Path: docs/notes.md\nbody", "parent_id": ROOT,
         "priority": 1, "createdAt": 70, "modifiedAt": 70},
        {"id": G, "name": "Heading inside file", "note": "", "parent_id": F, "priority": 0,
         "createdAt": 71, "modifiedAt": 71},
    ]


class FakeServer:
    """Per-node live state + an integer-second clock; POST stamps modifiedAt."""

    def __init__(self, nodes, *, clock_start=1000, step=1):
        self.state = {}
        for node in nodes:
            self.state[node["id"]] = {
                "name": node["name"], "note": node.get("note") or "",
                "parent_id": node.get("parent_id"), "priority": node.get("priority"),
                "modifiedAt": node["modifiedAt"], "createdAt": node["createdAt"],
                "data": dict(node.get("data") or {}), "completedAt": None,
            }
        self.clock = clock_start
        self.step = step
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.fail_on_post: int | None = None      # 1-based POST ordinal that raises
        self.mutate_note_on_post: bool = False
        self.freeze_clock: bool = False

    def node(self, uuid):
        s = self.state[uuid]
        return WorkFlowyNode(
            id=uuid, name=s["name"], note=s["note"], parentId=s["parent_id"],
            priority=s["priority"], createdAt=s["createdAt"], modifiedAt=s["modifiedAt"],
            completedAt=s["completedAt"], data=dict(s["data"]) or None,
        )

    async def get_node(self, uuid):
        self.gets.append(uuid)
        return self.node(uuid)

    async def update_node(self, uuid, request: NodeUpdateRequest, *, basis=None):
        body = request.model_dump(exclude_none=True)
        self.posts.append((uuid, body))
        if self.fail_on_post is not None and len(self.posts) == self.fail_on_post:
            raise RuntimeError("simulated transport failure after POST -- outcome unknown")
        s = self.state[uuid]
        if "name" in body:
            s["name"] = body["name"]
        if "note" in body:
            s["note"] = body["note"]
        if "layoutMode" in body:
            s["data"]["layoutMode"] = body["layoutMode"]
        if self.mutate_note_on_post:
            s["note"] = s["note"] + " MUTATED"
        if not self.freeze_clock:
            self.clock += self.step
        s["modifiedAt"] = self.clock
        return self.node(uuid)


def setup(monkeypatch, tmp_path, *, nodes=None, **server_kwargs):
    nodes = nodes if nodes is not None else _nodes()
    paths = make_coherence_root(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    client = make_boundary_capable(object.__new__(server.WorkFlowyClient), nodes=nodes)
    isolate_claim_scope(client, tmp_path)
    fake = FakeServer(nodes, **server_kwargs)
    client.get_node = AsyncMock(side_effect=fake.get_node)
    client.update_node = AsyncMock(side_effect=fake.update_node)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)

    async def config(_tab):
        return (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})

    config_mock = AsyncMock(side_effect=config)
    monkeypatch.setattr(server, "_resolve_note_session_config", config_mock)
    sleeps: list[float] = []

    async def sleeper(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(server, "_anchor_touch_sleep", sleeper)
    return client, fake, paths, sleeps, config_mock


async def dispatch_async(params):
    return await dispatch_operation(
        server.build_anchor_registry(), "point.touch", params,
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    )


def touch(handles, **extra):
    return asyncio.run(dispatch_async({"handles": handles, **extra}))


def rows_by_handle(result):
    return {row["handle"]: row for row in result["detail"]["results"]}


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def test_reverse_order_writes_make_first_handle_newest(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = touch(["[ap:TOUCHA]", B[:8], C])
    assert result["success"] is True and result["changed"] is True
    assert result["partial_success"] is False and result["operation"] == "anchor.point.touch"
    # Writes happen LAST-to-FIRST so the first handle lands in the newest second.
    assert [uuid for uuid, _ in fake.posts] == [C, B, A]
    rows = result["detail"]["results"]
    assert [row["handle"] for row in rows] == ["[ap:TOUCHA]", B[:8], C]  # input order kept
    assert [row["uuid"] for row in rows] == [A, B, C]
    after = [row["modified_after"] for row in rows]
    assert after == [1003, 1002, 1001] and after[0] > after[1] > after[2]
    assert all(row["status"] == "touched" and row["order_verified"] is True for row in rows)
    assert result["detail"]["established_order"] == [A, B, C]
    summary = result["detail"]["summary"]
    assert summary["order_established"] is True and summary["stopped"] is False
    assert summary == {**summary, "requested": 3, "touched": 3, "skipped": 0, "failed": 0,
                       "not_run": 0, "writes": 3}
    assert rows[0]["ap_id"] == "[ap:TOUCHA]" and rows[1]["ap_id"] is None
    assert rows[0]["modified_before"] == 100 and rows[1]["modified_before"] == 300
    # An older, then a NEWER pre-touch modifiedAt in input order still ends strictly ordered.
    assert result["detail"]["order_convention"].startswith("handles[0] = highest priority")


def test_peek_recent_by_modified_selects_the_touched_set_and_desc_reads_input_order(monkeypatch, tmp_path):
    """End-to-end with the Unit 1 recency policy: the cache the next peek reads
    carries the server's post-write modifiedAt (update_node patches it), so a
    recent_by='modified' selection yields exactly the touched set. U3
    ([ap:SSVIC9]): the DEFAULT presentation is ascending (the reverse of the
    touch priority); the explicit sort_direction='desc' opt-in reads the set
    back in input order."""
    from workflowy_mcp.dock_peek_metrics import apply_dock_peek_recency

    client, fake, *_ = setup(monkeypatch, tmp_path)
    touch([C, A, B])   # priority order: C, A, B (C touched last = newest)
    rows = [
        {"uuid": uuid, "created_at": fake.state[uuid]["createdAt"],
         "modified_at": fake.state[uuid]["modifiedAt"]} for uuid in (A, B, C)
    ]
    selected, _selection, sort = apply_dock_peek_recency(
        rows, recent=3, recent_by="modified", sort_by=None, sort_direction=None,
    )
    # The selection IS the touched set; the ascending default shows it in
    # reverse priority order (B touched first = oldest, C last = newest).
    assert {row["uuid"] for row in selected} == {A, B, C}
    assert [row["uuid"] for row in selected] == [B, A, C]
    assert sort == {"by": "modified", "direction": "asc"}
    # The explicit opt-in restores the priority reading.
    newest_first, _sel2, sort2 = apply_dock_peek_recency(
        rows, recent=3, recent_by="modified", sort_by=None, sort_direction="desc",
    )
    assert [row["uuid"] for row in newest_first] == [C, A, B]
    assert sort2 == {"by": "modified", "direction": "desc"}


# ---------------------------------------------------------------------------
# Mechanism + content preservation
# ---------------------------------------------------------------------------

def test_layout_mode_echo_sends_zero_text_bytes_and_preserves_everything(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    before = {uuid: dict(fake.state[uuid]) for uuid in (A, C)}
    result = touch([A, C])
    assert result["success"] is True
    bodies = {uuid: body for uuid, body in fake.posts}
    assert bodies[A] == {"layoutMode": "bullets"} and bodies[C] == {"layoutMode": "todo"}
    rows = rows_by_handle(result)
    assert rows[A]["mechanism"] == "layout_mode_echo" and rows[C]["mechanism"] == "layout_mode_echo"
    for uuid in (A, C):
        for key in ("name", "note", "parent_id", "priority", "data", "createdAt", "completedAt"):
            assert fake.state[uuid][key] == before[uuid][key], (uuid, key)
        assert fake.state[uuid]["modifiedAt"] > before[uuid]["modifiedAt"]
    assert rows[A]["name"] == html.unescape(NAME_A)


def test_name_identity_fallback_when_layout_mode_absent_or_unechoable(monkeypatch, tmp_path):
    nodes = _nodes()
    nodes[3]["data"] = {"layoutMode": "board"}   # B: not echoable -> fallback
    client, fake, *_ = setup(monkeypatch, tmp_path, nodes=nodes)
    result = touch([B, A])
    assert result["success"] is True
    bodies = {uuid: body for uuid, body in fake.posts}
    # B has an unechoable layoutMode; A (bullets) still echoes.
    assert set(bodies[B]) == {"name"} and bodies[A] == {"layoutMode": "bullets"}
    assert html.unescape(bodies[B]["name"]) == html.unescape(NAME_B)
    rows = rows_by_handle(result)
    assert rows[B]["mechanism"] == "name_identity" and rows[A]["mechanism"] == "layout_mode_echo"
    assert fake.state[B]["data"] == {"layoutMode": "board"}   # never invented a mode


def test_name_identity_fallback_preserves_ap_id_tags_and_decoration(monkeypatch, tmp_path):
    nodes = _nodes()
    nodes[2].pop("data")     # A: no layoutMode at all -> fallback on a protected-region-rich name
    client, fake, *_ = setup(monkeypatch, tmp_path, nodes=nodes)
    result = touch([A])
    assert result["success"] is True
    (uuid, body), = fake.posts
    assert uuid == A and set(body) == {"name"}
    assert html.unescape(body["name"]) == html.unescape(NAME_A)
    assert fake.state[A]["note"] == NOTE_A
    assert rows_by_handle(result)[A]["mechanism"] == "name_identity"


def test_touch_echo_modes_match_the_request_models_literal():
    literal = NodeUpdateRequest.model_fields["layoutMode"].annotation
    allowed = set()
    for arg in typing.get_args(literal):
        allowed.update(typing.get_args(arg) or ())
    assert allowed == set(server._TOUCH_ECHO_LAYOUT_MODES)
    assert server._touch_mechanism_for(WorkFlowyNode(id="x", data={"layoutMode": "h2"})) == "layout_mode_echo"
    assert server._touch_mechanism_for(WorkFlowyNode(id="x", data={"layoutMode": "board"})) == "name_identity"
    assert server._touch_mechanism_for(WorkFlowyNode(id="x", data=None)) == "name_identity"


def test_touch_channel_never_combines_with_content_or_other_channels():
    for kwargs in (
        {"name": "x"}, {"note": "n"}, {"note_search": "a", "note_replace": "b"},
        {"note_edits": [{"search": "a"}]}, {"layout_mode": "h1"}, {"return_diff": True},
        {"_tag_transform": lambda t: t}, {"_assign_ap_core": "ABCDEF"}, {"_remove_ap_id": True},
    ):
        with pytest.raises(ValueError, match="_touch is the dedicated anchor.point.touch channel"):
            asyncio.run(server._node_edit_core(A, _touch=True, **kwargs))


def test_content_change_after_touch_fails_the_row_and_stops(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    fake.mutate_note_on_post = True
    result = touch([A, B, C])
    assert result["success"] is False and result["changed"] is False
    rows = rows_by_handle(result)
    assert rows[C]["status"] == "failed" and rows[C]["reason"] == "content_changed"
    assert rows[C]["changed_fields"] == ["note"]
    assert rows[B]["status"] == "not_run" and rows[A]["status"] == "not_run"
    assert len(fake.posts) == 1
    assert result["detail"]["summary"]["stopped"] is True
    assert result["detail"]["summary"]["stop_reason"] == "content_changed"


# ---------------------------------------------------------------------------
# Ties / timestamp verification
# ---------------------------------------------------------------------------

def test_same_second_landing_is_re_touched_once_after_a_wait(monkeypatch, tmp_path):
    client, fake, paths, sleeps, _ = setup(monkeypatch, tmp_path, step=0)
    # step=0 -> every POST lands in the same server second unless we advance it.
    original = fake.update_node

    async def ticking(uuid, request, *, basis=None):
        # First two POSTs share second 1000; the sleep "crosses" to 1001 for the retry.
        if len(fake.posts) >= 2:
            fake.clock = 1001
        return await original(uuid, request, basis=basis)

    client.update_node = AsyncMock(side_effect=ticking)
    result = touch([A, B])
    assert result["success"] is True
    rows = rows_by_handle(result)
    assert [uuid for uuid, _ in fake.posts] == [B, A, A]     # B, A, then A re-touched once
    assert sleeps == [server._ANCHOR_TOUCH_TIE_SLEEP_SECONDS]
    assert rows[B]["modified_after"] == 1000 and rows[A]["modified_after"] == 1001
    assert rows[A]["retried"] is True and rows[A]["order_verified"] is True
    assert rows[B]["retried"] is False
    assert result["detail"]["summary"]["order_established"] is True
    assert result["detail"]["summary"]["writes"] == 3


def test_unresolvable_tie_is_reported_never_claimed(monkeypatch, tmp_path):
    client, fake, paths, sleeps, _ = setup(monkeypatch, tmp_path, step=0)
    # Clock stuck at 1000 for every POST: B lands at 1000 (> its own 300), A ties.
    result = touch([A, B])
    rows = rows_by_handle(result)
    assert [uuid for uuid, _ in fake.posts] == [B, A, A]
    assert rows[A]["status"] == "touched" and rows[A]["order_verified"] is False
    assert rows[A]["reason"] == "tie_unresolved" and rows[A]["retried"] is True
    assert result["detail"]["summary"]["order_established"] is False
    assert result["success"] is True       # every row IS touched (all at the top)...
    assert any("could not establish a strict order" in w for w in result["warnings"])  # ...but say so


def test_timestamp_that_never_advances_stops_the_batch(monkeypatch, tmp_path):
    client, fake, paths, sleeps, _ = setup(monkeypatch, tmp_path)
    fake.freeze_clock = True
    fake.clock = 200            # == C's current modifiedAt -> never advances
    result = touch([A, C])
    rows = rows_by_handle(result)
    assert [uuid for uuid, _ in fake.posts] == [C, C]      # one spaced re-touch, then STOP
    assert rows[C]["status"] == "failed" and rows[C]["reason"] == "timestamp_not_advanced"
    assert rows[C]["modified_before"] == 200 and rows[C]["modified_after"] == 200
    assert rows[A]["status"] == "not_run"
    assert result["success"] is False and result["changed"] is False
    assert result["detail"]["summary"]["stop_reason"] == "timestamp_not_advanced"


def test_order_ok_predicate():
    ok = server._touch_order_ok
    assert ok(100, 101, None) and ok(100, 101, 100) and ok(None, 5, 4)
    assert not ok(100, 100, None) and not ok(100, 101, 101) and not ok(100, None, None)
    assert not ok(None, 5, 5)


# ---------------------------------------------------------------------------
# Batch semantics: SKIP / REFUSE / STOP
# ---------------------------------------------------------------------------

def test_unresolvable_handle_is_a_per_item_skip_and_the_batch_continues(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = touch([A, "[ap:NOSUCH]", C])
    assert result["success"] is False and result["partial_success"] is True
    assert result["changed"] is True
    rows = rows_by_handle(result)
    assert rows["[ap:NOSUCH]"]["status"] == "skipped"
    assert rows["[ap:NOSUCH]"]["reason"] == "unresolvable_handle"
    assert rows[A]["status"] == "touched" and rows[C]["status"] == "touched"
    assert rows[A]["modified_after"] > rows[C]["modified_after"]
    assert [uuid for uuid, _ in fake.posts] == [C, A]
    assert result["detail"]["summary"]["skipped"] == 1 and result["detail"]["summary"]["touched"] == 2


@pytest.mark.parametrize("handles", [
    [A, A],
    [A, "[ap:TOUCHA]"],            # same node via UUID and AP-ID
    ["[ap:toucha]", "[ap:TOUCHA]"],
    [B[:8], B],
])
def test_duplicate_handles_refuse_the_whole_batch_with_zero_writes(monkeypatch, tmp_path, handles):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = touch(handles)
    assert result["success"] is False and result["error"]["code"] == "duplicate_handles"
    assert fake.posts == []
    client.update_node.assert_not_awaited()


@pytest.mark.parametrize("params,code", [
    ({"handles": []}, "invalid_parameter_value"),
    ({"handles": "not-a-list"}, "invalid_parameter_type"),
    ({"handles": [A, 7]}, "invalid_parameter_type"),
    ({"handles": [A, "   "]}, "invalid_parameter_type"),
    ({"handles": [A], "allow_docks": "yes"}, "invalid_parameter_type"),
    ({"handles": [A], "order": "desc"}, "forbidden_parameter"),
    ({}, "missing_parameter"),
])
def test_malformed_batches_refuse_with_zero_writes(monkeypatch, tmp_path, params, code):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = asyncio.run(dispatch_async(params))
    assert result["success"] is False and result["error"]["code"] == code
    assert fake.posts == []


def test_unknown_write_outcome_stops_without_resending_and_leaves_higher_priority_not_run(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    fake.fail_on_post = 2          # C succeeds, B's POST raises, A never attempted
    result = touch([A, B, C])
    assert result["success"] is False and result["partial_success"] is True
    assert result["changed"] is True
    rows = rows_by_handle(result)
    assert rows[C]["status"] == "touched"
    assert rows[B]["status"] == "failed" and rows[B]["reason"] == "write_outcome_unknown"
    assert rows[A]["status"] == "not_run" and rows[A]["reason"] == "batch_stopped"
    assert [uuid for uuid, _ in fake.posts] == [C, B]      # B was NOT re-sent
    summary = result["detail"]["summary"]
    assert summary["stopped"] is True and summary["stop_reason"] == "write_outcome_unknown"
    assert summary == {**summary, "touched": 1, "failed": 1, "not_run": 1}
    assert any("HIGHER-priority" in w for w in result["warnings"])


def test_hard_barrier_refuses_before_any_read_or_write(monkeypatch, tmp_path):
    client, fake, paths, *_ = setup(monkeypatch, tmp_path)
    s04_raise_barrier(paths)
    result = touch([A])
    assert result["error"]["code"] == "coherence_barrier_active"
    client.get_node.assert_not_awaited()
    client.update_node.assert_not_awaited()


def test_mid_batch_coherence_refusal_keeps_universal_envelope_and_partial_ledger(monkeypatch, tmp_path):
    from workflowy_mcp.coherence.admission import require_cache_identity_proof

    client, fake, paths, *_ = setup(monkeypatch, tmp_path)
    original = fake.update_node

    async def refusing(uuid, request, *, basis=None):
        if len(fake.posts) >= 1:
            s04_raise_barrier(paths)
            await require_cache_identity_proof(lambda: paths, operation="test identity")
        return await original(uuid, request, basis=basis)

    client.update_node = AsyncMock(side_effect=refusing)
    result = touch([A, B, C])
    assert result["success"] is False and result["error"]["code"] == "coherence_barrier_active"
    ledger = result["error"]["extra"]["touch"]
    by = {row["uuid"]: row for row in ledger["results"]}
    assert by[C]["status"] == "touched"
    assert by[B]["status"] == "failed" and by[B]["reason"] == "coherence_barrier_active"
    assert by[A]["status"] == "not_run"
    assert ledger["summary"]["stop_reason"] == "coherence_barrier_active"


# ---------------------------------------------------------------------------
# Safe scope: mapped material and Anchor Docks
# ---------------------------------------------------------------------------

def test_mapped_file_and_inside_file_nodes_are_skipped(monkeypatch, tmp_path):
    client, fake, *_ = setup(monkeypatch, tmp_path)
    result = touch([F, A, G])
    rows = rows_by_handle(result)
    assert rows[F]["status"] == "skipped" and rows[F]["reason"] == "mapped_node_refused"
    assert "state=file" in rows[F]["detail"]
    assert rows[G]["status"] == "skipped" and rows[G]["reason"] == "mapped_node_refused"
    assert "state=inside_file" in rows[G]["detail"]
    assert rows[A]["status"] == "touched"
    assert [uuid for uuid, _ in fake.posts] == [A]
    assert result["success"] is False and result["partial_success"] is True


def test_anchor_dock_root_is_skipped_unless_allow_docks(monkeypatch, tmp_path):
    client, fake, paths, sleeps, config_mock = setup(monkeypatch, tmp_path)
    result = touch([D, A])
    rows = rows_by_handle(result)
    assert rows[D]["status"] == "skipped" and rows[D]["reason"] == "anchor_dock_refused"
    assert rows[A]["status"] == "touched"
    assert [uuid for uuid, _ in fake.posts] == [A]
    config_mock.assert_awaited_once()          # ONE vocabulary read for the whole batch

    config_mock.reset_mock()
    fake.posts.clear()
    result = touch([D, A], allow_docks=True)
    assert result["success"] is True
    assert [uuid for uuid, _ in fake.posts] == [A, D]
    config_mock.assert_not_awaited()           # no Dock classification needed


def test_node_absent_from_cache_is_skipped_fail_closed(monkeypatch, tmp_path):
    """The generic resolver already refuses an unknown exact UUID
    (``unresolvable_handle``); this locks the defensive branch for a handle
    that resolves but is absent from the PINNED operation snapshot (nothing
    to classify -> skip, never write)."""
    client, fake, *_ = setup(monkeypatch, tmp_path)
    ghost = "eeeeeeee-7777-4777-8777-777777777777"
    unresolved = touch([ghost, A])
    assert rows_by_handle(unresolved)[ghost]["reason"] == "unresolvable_handle"
    fake.posts.clear()

    async def resolve(handle, *, label, required=True):
        return handle

    monkeypatch.setattr(server, "_resolve_generic_node_handle", resolve)
    result = touch([ghost, A])
    rows = rows_by_handle(result)
    assert rows[ghost]["status"] == "skipped" and rows[ghost]["reason"] == "not_in_cache"
    assert "cache.refresh" in rows[ghost]["detail"]
    assert rows[A]["status"] == "touched"
    assert [uuid for uuid, _ in fake.posts] == [A]


# ---------------------------------------------------------------------------
# Discoverability
# ---------------------------------------------------------------------------

def test_registry_describe_examples_and_top_level_description_teach_the_order():
    registry = server.ANCHOR_REGISTRY
    spec = registry.resolve("point.touch")
    assert spec.side_effects == ("workflowy_mutation",) and spec.cache_backed is True
    assert "\n" not in spec.summary
    assert "handles[0] = highest priority" in spec.summary
    full = registry.describe_full("point.touch")
    props = full["params_schema"]["properties"]
    assert set(props) == {"handles", "allow_docks", "target_tab"}
    assert full["params_schema"]["required"] == ["handles"]
    assert "FIRST = highest priority" in props["handles"]["description"]
    for example in full["examples"]:
        validate_params(spec, example["call"]["params"])
    assert any(len(ex["call"]["params"]["handles"]) > 1 for ex in full["examples"])
    returns = full["returns"]["description"]
    for token in ("order_established", "duplicate_handles", "write_outcome_unknown",
                  "mapped_node_refused", "anchor_dock_refused", "HIGHER-priority",
                  "sealed program evidence"):
        assert token in returns, token
    # U01 added the timeout code; N05 ([ap:4UUBNV] §6.8) adds the conflict
    # and region codes on every affected mutation verb ([ap:ZU7ZWM]).
    assert set(spec.domain_error_codes) == {
        "duplicate_handles", "invalid_parameter_value", "mutation_serialization_timeout",
        "mutation_serialization_conflict", "mutation_region_error",
        server.ConcurrentEditDetectedError.code,
    }
    index_line = next(line for line in registry.index_lines() if line.startswith("point.touch"))
    assert "workflowy_mutation" in index_line
    description = server._ANCHOR_TOOL_DESCRIPTION
    assert "coming" not in description
    assert "point.touch" in description
    assert "first handle = highest priority = newest afterwards" in description
    assert "dock.peek recent=N brings the set back" in description
    assert "sort_direction='desc' to read it in priority order" in description
