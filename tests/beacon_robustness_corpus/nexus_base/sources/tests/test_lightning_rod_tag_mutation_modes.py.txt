"""Lightning Rod Q4 Workflowy-tag mutation mode (mutate_node_tags) -- regression locks.

Covers: the ONE tag-mutation normalizer (normalize_from_tool_args), the §9
wire normalizer (from_wire + parity), the tool boundary guards, the
_lightning_rod_mutate_tags orchestrator (resolve -> duplicate-resolved-UUID
preflight -> sequential commit) against a controlled client double, and the
_handle_mutate_node_tags WebSocket wire path.
"""

import asyncio
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.lightning_rod_tag_mutation_spec import (  # noqa: E402
    WORKFLOWY_TAG_MUTATION_MODES,
    WorkflowyTagMutationSpec,
)

_NODE_A = "aaaaaaaa-1111-1111-1111-111111111111"
_NODE_B = "bbbbbbbb-2222-2222-2222-222222222222"
_NODE_C = "cccccccc-3333-3333-3333-333333333333"
_H1 = "handle-one"
_H2 = "handle-two"
_H3 = "handle-three"
_AP1 = "[ap:K7M2QF]"


async def _noop_mutation_gate(paths, *, operation):
    """S04 test seam: bypass the hard-stop admission gate (covered by S04 suites)."""

    return None


def _stub_coherence_mutation_gate(monkeypatch):
    monkeypatch.setattr(
        "workflowy_mcp.coherence.admission.require_coherence_mutation_admission",
        _noop_mutation_gate,
    )


def _norm(*, operations=None, comment=None, symbols=None, tags=None,
          root_uuids=None, root_uuid=None, favor="uuid", mode="mutate_node_tags"):
    return WorkflowyTagMutationSpec.normalize_from_tool_args(
        mode=mode,
        operations=operations,
        comment=comment,
        symbols=symbols,
        tags=tags,
        root_uuids=root_uuids,
        root_uuid=root_uuid,
        favor=favor,
    )


def _spec(operations):
    spec, err = _norm(operations=operations)
    assert err is None, err
    assert spec is not None
    return spec


# ---------------------------------------------------------------------------
# NORMALIZER (normalize_from_tool_args)
# ---------------------------------------------------------------------------


def test_normalizer_add_remove_rename_shapes_and_hash_coercion():
    spec, err = _norm(operations=[
        {"handle": "u1", "operation": "add", "tags": ["#alpha", "beta"]},
        {"handle": "u2", "operation": "remove", "tags": ["gamma"]},
        {"handle": "u3", "operation": "rename", "old_tag": "#x", "new_tag": "y"},
    ])
    assert err is None
    assert spec.mode == "mutate_node_tags"
    assert [op.operation for op in spec.operations] == ["add", "remove", "rename"]
    assert list(spec.operations[0].tags) == ["alpha", "beta"]
    assert list(spec.operations[1].tags) == ["gamma"]
    assert spec.operations[2].tags == ()
    assert spec.operations[2].old_tag == "x"
    assert spec.operations[2].new_tag == "y"


def test_normalizer_rejects_unknown_fields_per_operation_kind():
    _, err = _norm(operations=[{"handle": "u", "operation": "add", "tags": ["a"], "comment": "x"}])
    assert err and "unknown field" in err
    _, err = _norm(operations=[{"handle": "u", "operation": "rename", "old_tag": "a", "new_tag": "b", "tags": ["c"]}])
    assert err and "unknown field" in err
    # 'expand' is the render_symbols grammar -- never a mutation field.
    _, err = _norm(operations=[{"handle": "u", "operation": "add", "tags": ["a"], "expand": True}])
    assert err and "unknown field" in err


def test_normalizer_operation_required_and_known():
    _, err = _norm(operations=[{"handle": "u", "tags": ["a"]}])
    assert err and "operation is required" in err
    _, err = _norm(operations=[{"handle": "u", "operation": "explode", "tags": ["a"]}])
    assert err and "unknown" in err


def test_normalizer_tags_required_for_add_remove():
    _, err = _norm(operations=[{"handle": "u", "operation": "add"}])
    assert err and "tags must be a non-empty list" in err
    _, err = _norm(operations=[{"handle": "u", "operation": "remove", "tags": []}])
    assert err and "non-empty list" in err


def test_normalizer_rename_requires_old_and_new():
    _, err = _norm(operations=[{"handle": "u", "operation": "rename", "new_tag": "b"}])
    assert err and "old_tag is required" in err
    _, err = _norm(operations=[{"handle": "u", "operation": "rename", "old_tag": "a"}])
    assert err and "new_tag is required" in err


def test_normalizer_rejects_duplicate_handles():
    _, err = _norm(operations=[
        {"handle": "u", "operation": "add", "tags": ["a"]},
        {"handle": "u", "operation": "remove", "tags": ["b"]},
    ])
    assert err and "duplicate operation for handle" in err


def test_normalizer_rejects_internal_whitespace_tags():
    _, err = _norm(operations=[{"handle": "u", "operation": "add", "tags": ["a b"]}])
    assert err and "invalid tag token" in err
    _, err = _norm(operations=[{"handle": "u", "operation": "rename", "old_tag": "a", "new_tag": "b c"}])
    assert err and "invalid tag token" in err


def test_normalizer_operations_must_be_non_empty_list():
    _, err = _norm(operations="not-a-list")
    assert err and "non-empty list" in err
    _, err = _norm(operations=[])
    assert err and "non-empty list" in err
    _, err = _norm(operations=None)
    assert err and "non-empty list" in err
    _, err = _norm(operations=["not-an-object"])
    assert err and "must be an object" in err
    _, err = _norm(operations=[{"operation": "add", "tags": ["a"]}])
    assert err and "handle is required" in err


def test_normalizer_rejects_beacon_only_and_scoping_args():
    base = [{"handle": "u", "operation": "add", "tags": ["a"]}]
    _, err = _norm(operations=base, comment="x")
    assert err and "comment" in err
    _, err = _norm(operations=base, symbols=["foo"])
    assert err and "symbols" in err
    _, err = _norm(operations=base, tags=["a"])
    assert err and "operations-only" in err
    _, err = _norm(operations=base, root_uuids=["r"])
    assert err and "self-scoping" in err
    _, err = _norm(operations=base, root_uuid="r")
    assert err and "self-scoping" in err
    _, err = _norm(operations=base, favor="symbol")
    assert err and "favor" in err


def test_normalizer_rejects_unexpected_mode():
    _, err = _norm(operations=[{"handle": "u", "operation": "add", "tags": ["a"]}], mode="ensure_beacons")
    assert err and "unexpected tag-mutation mode" in err


def test_spec_to_dict_is_json_serializable():
    spec = _spec([{"handle": "u", "operation": "rename", "old_tag": "a", "new_tag": "b"}])
    json.dumps(spec.to_dict())


# ---------------------------------------------------------------------------
# §9 WIRE NORMALIZER (from_wire) + parity
# ---------------------------------------------------------------------------


def test_from_wire_parity_with_tool_args():
    ops = [
        {"handle": "u1", "operation": "add", "tags": ["#a", "b"]},
        {"handle": "u2", "operation": "rename", "old_tag": "x", "new_tag": "y"},
    ]
    spec1, err1 = _norm(operations=ops)
    spec2, err2 = WorkflowyTagMutationSpec.from_wire({
        "action": "mutate_node_tags",
        "request_id": "r",
        "endpoint_id": "e",
        "v": 1,
        "operations": ops,
    })
    assert err1 is None and err2 is None
    assert spec1.mode == spec2.mode == "mutate_node_tags"
    assert spec1.operations == spec2.operations


def test_from_wire_rejects_bad_version_unknown_fields_and_missing_ops():
    good = [{"handle": "u", "operation": "add", "tags": ["a"]}]
    _, err = WorkflowyTagMutationSpec.from_wire({"v": 2, "operations": good})
    assert err and "version" in err
    _, err = WorkflowyTagMutationSpec.from_wire({"v": "x", "operations": good})
    assert err and "integer" in err
    _, err = WorkflowyTagMutationSpec.from_wire({"v": 1, "operations": good, "bogus": 1})
    assert err and "unknown field" in err
    _, err = WorkflowyTagMutationSpec.from_wire({"v": 1})
    assert err and "non-empty list" in err
    _, err = WorkflowyTagMutationSpec.from_wire("nope")
    assert err and "must be an object" in err


def test_from_wire_grammar_errors_match_tool_args():
    # Same grammar core: a duplicate-handle rejection reads identically.
    ops = [
        {"handle": "u", "operation": "add", "tags": ["a"]},
        {"handle": "u", "operation": "add", "tags": ["b"]},
    ]
    _, err1 = _norm(operations=ops)
    _, err2 = WorkflowyTagMutationSpec.from_wire({"v": 1, "operations": ops})
    assert err1 == err2 and "duplicate" in err1


# ---------------------------------------------------------------------------
# TOOL BOUNDARY
# ---------------------------------------------------------------------------


def test_boundary_tag_mutation_never_parses_since_codec():
    res = asyncio.run(srv.lightning_rod(
        mode="mutate_node_tags",
        operations=[{"handle": _H1, "operation": "add", "tags": ["a"]}],
        since="definitely-not-a-since",
    ))
    assert res["success"] is False
    assert "render-only" in res["error"]
    assert "since" in res["error"]


def test_boundary_tag_mutation_rejects_render_only_args():
    res = asyncio.run(srv.lightning_rod(
        mode="mutate_node_tags",
        operations=[{"handle": _H1, "operation": "add", "tags": ["a"]}],
        sort_by="created",
    ))
    assert res["success"] is False
    assert "render-only" in res["error"] and "sort_by" in res["error"]


def test_boundary_tag_mutation_rejects_push_to_modal():
    res = asyncio.run(srv.lightning_rod(
        mode="mutate_node_tags",
        operations=[{"handle": _H1, "operation": "add", "tags": ["a"]}],
        push_to_modal=True,
    ))
    assert res["success"] is False
    assert "mutation mode" in res["error"]


def test_boundary_render_mode_rejects_operations_mentions_all_mutation_modes():
    res = asyncio.run(srv.lightning_rod(
        mode="render_tags",
        root_uuid=_NODE_A,
        tags=["x"],
        operations=[{"handle": _H1, "operation": "add", "tags": ["a"]}],
    ))
    assert res["success"] is False
    assert "mutate_node_tags" in res["error"]


def test_boundary_tag_mutation_normalization_errors_surface():
    # comment is a beacon-only concept -- rejected through the boundary.
    res = asyncio.run(srv.lightning_rod(
        mode="mutate_node_tags",
        operations=[{"handle": _H1, "operation": "add", "tags": ["a"]}],
        comment="nope",
    ))
    assert res["success"] is False
    assert "comment" in res["error"]
    # symbols are Q3's quadrant.
    res = asyncio.run(srv.lightning_rod(
        mode="mutate_node_tags",
        operations=[{"handle": _H1, "operation": "add", "tags": ["a"]}],
        symbols=["foo"],
    ))
    assert res["success"] is False
    assert "symbols" in res["error"]
    # roots are meaningless for self-scoping handles.
    res = asyncio.run(srv.lightning_rod(
        mode="mutate_node_tags",
        operations=[{"handle": _H1, "operation": "add", "tags": ["a"]}],
        root_uuids=["some-root"],
    ))
    assert res["success"] is False
    assert "self-scoping" in res["error"]


# ---------------------------------------------------------------------------
# ORCHESTRATOR (controlled client double)
# ---------------------------------------------------------------------------


class _TagMutationClient:
    """Controlled client for the Q4 orchestrator.

    ``handles``: handle -> uuid (str) | candidate list | None (unresolved).
    ``fail_ops``: (verb, uuid) -> the primitive RAISES.
    ``fail_result``: (verb, uuid) -> the primitive returns success=False.
    ``noop_ops``: (verb, uuid) -> the primitive returns no_op=True.
    """

    def __init__(self, *, handles, fail_ops=(), fail_result=(), noop_ops=()):
        self.handles = dict(handles)
        self.fail_ops = set(fail_ops)
        self.fail_result = set(fail_result)
        self.noop_ops = set(noop_ops)
        self.resolve_calls = []  # (candidate, kwargs)
        self.tag_calls = []      # (verb, uuid, payload)
        # S03-B: the mutate_node_tags WS front wraps the handler in a
        # cache_operation_boundary; install the boundary surface.
        from coherence_harness import make_boundary_capable

        make_boundary_capable(self, nodes=[])

    @staticmethod
    def _canon(tags):
        return [t if str(t).startswith("#") else f"#{t}" for t in tags]

    async def resolve_node_handle(self, candidate, **kwargs):
        self.resolve_calls.append((candidate, kwargs))
        v = self.handles.get(candidate)
        if isinstance(v, str):
            return {
                "success": True,
                "resolved_uuid": v,
                "resolved_via": "uuid",
                "input_kind": "exact_uuid",
                "candidates": None,
            }
        if isinstance(v, list):
            return {
                "success": False,
                "resolved_uuid": None,
                "resolved_via": None,
                "input_kind": None,
                "candidates": v,
                "candidates_via": "uuid",
                "match_count": len(v),
            }
        return {
            "success": False,
            "resolved_uuid": None,
            "resolved_via": None,
            "input_kind": None,
            "candidates": None,
        }

    async def workflowy_add_tag(self, node_id, tag, max_retries=10):
        tags = self._canon(tag)
        self.tag_calls.append(("add", node_id, tags))
        if ("add", node_id) in self.fail_ops:
            raise RuntimeError("injected add failure")
        if ("add", node_id) in self.fail_result:
            return {"success": False, "error": "injected add failure", "error_kind": "api_error"}
        if ("add", node_id) in self.noop_ops:
            return {"success": True, "node_id": node_id, "old_name": "n", "new_name": "n",
                    "tags_added": [], "tags_already_present": tags, "no_op": True}
        return {"success": True, "node_id": node_id, "old_name": "n",
                "new_name": "n " + " ".join(tags),
                "tags_added": tags, "tags_already_present": [], "no_op": False}

    async def workflowy_remove_tag(self, node_id, tag, max_retries=10):
        tags = self._canon(tag)
        self.tag_calls.append(("remove", node_id, tags))
        if ("remove", node_id) in self.fail_ops:
            raise RuntimeError("injected remove failure")
        if ("remove", node_id) in self.fail_result:
            return {"success": False, "error": "injected remove failure", "error_kind": "api_error"}
        if ("remove", node_id) in self.noop_ops:
            return {"success": True, "node_id": node_id, "old_name": "n", "new_name": "n",
                    "tags_removed": [], "tags_not_present": tags, "no_op": True}
        return {"success": True, "node_id": node_id, "old_name": "n #x", "new_name": "n",
                "tags_removed": tags, "tags_not_present": [], "no_op": False}

    async def workflowy_rename_tag(self, node_id, old_tag, new_tag, max_retries=10):
        old_n, new_n = self._canon([old_tag, new_tag])
        self.tag_calls.append(("rename", node_id, (old_n, new_n)))
        if ("rename", node_id) in self.fail_ops:
            raise RuntimeError("injected rename failure")
        if ("rename", node_id) in self.fail_result:
            return {"success": False, "error": "injected rename failure", "error_kind": "api_error"}
        if ("rename", node_id) in self.noop_ops:
            return {"success": True, "node_id": node_id, "old_name": "n", "new_name": "n",
                    "old_tag": old_n, "new_tag": new_n, "renamed": False,
                    "old_occurrences_removed": 0, "new_tag_already_present": False, "no_op": True}
        return {"success": True, "node_id": node_id, "old_name": f"n {old_n}",
                "new_name": f"n {new_n}", "old_tag": old_n, "new_tag": new_n,
                "renamed": True, "old_occurrences_removed": 1,
                "new_tag_already_present": False, "no_op": False}


def _run(client, spec):
    return asyncio.run(srv._lightning_rod_mutate_tags(spec=spec))


def test_mixed_add_remove_rename_in_one_call(monkeypatch):
    client = _TagMutationClient(handles={_H1: _NODE_A, _H2: _NODE_B, _H3: _NODE_C})
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = _run(client, _spec([
        {"handle": _H1, "operation": "add", "tags": ["alpha", "#beta"]},
        {"handle": _H2, "operation": "remove", "tags": ["gamma"]},
        {"handle": _H3, "operation": "rename", "old_tag": "x", "new_tag": "y"},
    ]))
    assert res["success"] is True
    assert res["partial_success"] is False
    assert [r["status"] for r in res["results"]] == ["committed"] * 3
    assert [r["operation"] for r in res["results"]] == ["add", "remove", "rename"]
    assert [c[0] for c in client.tag_calls] == ["add", "remove", "rename"]
    assert client.tag_calls[0][2] == ["#alpha", "#beta"]
    assert client.tag_calls[2][2] == ("#x", "#y")
    assert res["nodes_changed"] == sorted([_NODE_A, _NODE_B, _NODE_C])
    assert res["summary"]["committed"] == 3
    assert res["summary"]["requested"] == 3
    # Per-op ledger detail mapping.
    assert res["results"][0]["tags_added"] == ["#alpha", "#beta"]
    assert res["results"][1]["tags_removed"] == ["#gamma"]
    assert res["results"][2]["renamed"] is True
    assert res["results"][2]["old_occurrences_removed"] == 1
    assert res["results"][0]["old_name"] and res["results"][0]["new_name"]
    # Resolution used the universal resolver with symbols DISABLED.
    assert all(c[1].get("allow_symbol") is False for c in client.resolve_calls)
    assert all(c[1].get("allow_anchor_point") is True for c in client.resolve_calls)


def test_noop_rows_when_primitive_makes_no_api_call(monkeypatch):
    client = _TagMutationClient(handles={_H1: _NODE_A}, noop_ops={("add", _NODE_A)})
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = _run(client, _spec([{"handle": _H1, "operation": "add", "tags": ["already"]}]))
    assert res["success"] is True
    row = res["results"][0]
    assert row["status"] == "noop"
    assert row["tags_already_present"] == ["#already"]
    assert res["summary"]["noop"] == 1
    assert res["nodes_changed"] == []


def test_commit_exception_marks_row_batch_continues(monkeypatch):
    client = _TagMutationClient(
        handles={_H1: _NODE_A, _H2: _NODE_B, _H3: _NODE_C},
        fail_ops={("remove", _NODE_B)},
    )
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = _run(client, _spec([
        {"handle": _H1, "operation": "add", "tags": ["a"]},
        {"handle": _H2, "operation": "remove", "tags": ["b"]},
        {"handle": _H3, "operation": "rename", "old_tag": "x", "new_tag": "y"},
    ]))
    assert [r["status"] for r in res["results"]] == ["committed", "error", "committed"]
    assert res["results"][1]["error_kind"] == "commit_error"
    assert "injected remove failure" in res["results"][1]["error"]
    assert res["success"] is False
    assert res["partial_success"] is True
    assert res["summary"]["commit_failures"] == 1
    assert res["nodes_changed"] == sorted([_NODE_A, _NODE_C])


def test_commit_error_result_marks_row(monkeypatch):
    client = _TagMutationClient(handles={_H1: _NODE_A}, fail_result={("add", _NODE_A)})
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = _run(client, _spec([{"handle": _H1, "operation": "add", "tags": ["a"]}]))
    row = res["results"][0]
    assert row["status"] == "error"
    assert row["error_kind"] == "api_error"
    assert res["success"] is False
    assert res["partial_success"] is False  # no ok rows -> not partial


def test_unresolved_handle_aborts_batch_zero_mutations(monkeypatch):
    client = _TagMutationClient(handles={_H1: _NODE_A})  # _H2 missing
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = _run(client, _spec([
        {"handle": _H1, "operation": "add", "tags": ["a"]},
        {"handle": _H2, "operation": "remove", "tags": ["b"]},
    ]))
    assert res["success"] is False
    assert res["partial_success"] is False
    # Ledger order == input order; the resolved innocent row is not_run.
    assert [r["status"] for r in res["results"]] == ["not_run_due_to_batch_preflight_failure", "unresolved"]
    assert res["results"][1]["error_kind"] == "unresolved_handle"
    assert client.tag_calls == []
    assert res["summary"]["preflight_failures"] == 1
    assert res["summary"]["committed"] == 0


def test_ambiguous_handle_aborts_with_candidates(monkeypatch):
    client = _TagMutationClient(handles={
        _H1: [{"uuid": _NODE_A, "name": "a"}, {"uuid": _NODE_B, "name": "b"}],
    })
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = _run(client, _spec([{"handle": _H1, "operation": "add", "tags": ["a"]}]))
    row = res["results"][0]
    assert row["status"] == "ambiguous"
    assert len(row["candidates"]) == 2
    assert client.tag_calls == []
    assert res["success"] is False
    assert res["partial_success"] is False


def test_duplicate_resolved_uuid_aborts_complete_ledger(monkeypatch):
    # Two DIFFERENT handles (a UUID and an [ap:CORE]) naming the SAME node.
    client = _TagMutationClient(handles={_H1: _NODE_A, _AP1: _NODE_A})
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = _run(client, _spec([
        {"handle": _H1, "operation": "add", "tags": ["a"]},
        {"handle": _AP1, "operation": "rename", "old_tag": "x", "new_tag": "y"},
    ]))
    assert res["success"] is False
    assert res["partial_success"] is False
    assert [r["status"] for r in res["results"]] == ["duplicate_resolved_uuid", "duplicate_resolved_uuid"]
    assert res["summary"]["preflight_failures"] == 2
    assert client.tag_calls == []


def test_ap_id_handle_resolves_and_commits(monkeypatch):
    # Dan's endorsed use case: drive by the stable [ap:XXXXXX] handle.
    client = _TagMutationClient(handles={_AP1: _NODE_A})
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = _run(client, _spec([{"handle": _AP1, "operation": "add", "tags": ["build-v1--expand"]}]))
    assert res["success"] is True
    assert client.tag_calls == [("add", _NODE_A, ["#build-v1--expand"])]
    assert res["results"][0]["uuid"] == _NODE_A


def test_rename_applies_postfix_end_to_end(monkeypatch):
    # The headline rename use case: #foo -> #foo--expand.
    client = _TagMutationClient(handles={_AP1: _NODE_A})
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = _run(client, _spec([
        {"handle": _AP1, "operation": "rename", "old_tag": "#foo", "new_tag": "#foo--expand"},
    ]))
    assert res["success"] is True
    assert client.tag_calls == [("rename", _NODE_A, ("#foo", "#foo--expand"))]
    row = res["results"][0]
    assert row["status"] == "committed"
    assert row["renamed"] is True
    assert row["new_tag"] == "#foo--expand"


def test_from_cache_fallback_normalization_dispatch(monkeypatch):
    # A direct internal caller (no mutation_spec) funnels through the SAME
    # normalizer at the orchestrator boundary (invariant 2).
    client = _TagMutationClient(handles={_H1: _NODE_A})
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = asyncio.run(srv._lightning_rod_from_cache(
        mode="mutate_node_tags",
        root_uuid="",
        operations=[{"handle": _H1, "operation": "add", "tags": ["x"]}],
    ))
    assert res["success"] is True
    assert res["mode"] == "mutate_node_tags"
    assert res["results"][0]["status"] == "committed"


def test_from_cache_fallback_normalization_error_surfaces(monkeypatch):
    client = _TagMutationClient(handles={_H1: _NODE_A})
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = asyncio.run(srv._lightning_rod_from_cache(
        mode="mutate_node_tags",
        root_uuid="",
        operations=[],
    ))
    assert res["success"] is False
    assert "non-empty list" in res["error"]


# ---------------------------------------------------------------------------
# §9 WEBSOCKET WIRE PATH (_handle_mutate_node_tags)
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


def test_wire_handler_replies_with_full_ledger(monkeypatch):
    client = _TagMutationClient(handles={_H1: _NODE_A, _AP1: _NODE_B})
    monkeypatch.setattr(srv, "get_client", lambda: client)
    _stub_coherence_mutation_gate(monkeypatch)
    ws = _FakeWebSocket()
    asyncio.run(srv._handle_mutate_node_tags({
        "action": "mutate_node_tags",
        "request_id": "r1",
        "endpoint_id": "ep",
        "v": 1,
        "operations": [
            {"handle": _H1, "operation": "add", "tags": ["x"]},
            {"handle": _AP1, "operation": "rename", "old_tag": "a", "new_tag": "a--expand"},
        ],
    }, ws))
    assert len(ws.sent) == 1
    reply = ws.sent[0]
    assert reply["action"] == "mutate_node_tags_result"
    assert reply["request_id"] == "r1"
    assert reply["success"] is True
    assert [r["status"] for r in reply["results"]] == ["committed", "committed"]
    assert reply["nodes_changed"] == sorted([_NODE_A, _NODE_B])


def test_wire_handler_invalid_frame_replies_error(monkeypatch):
    client = _TagMutationClient(handles={})
    monkeypatch.setattr(srv, "get_client", lambda: client)
    _stub_coherence_mutation_gate(monkeypatch)
    ws = _FakeWebSocket()
    asyncio.run(srv._handle_mutate_node_tags({
        "action": "mutate_node_tags",
        "request_id": "r2",
        "v": 1,
        "operations": [],
    }, ws))
    assert len(ws.sent) == 1
    reply = ws.sent[0]
    assert reply["action"] == "mutate_node_tags_result"
    assert reply["request_id"] == "r2"
    assert reply["success"] is False
    assert "non-empty list" in reply["error"]
    assert client.tag_calls == []


def test_wire_handler_drops_frames_without_request_id(monkeypatch):
    # S03-B: the WS front wraps the handler in a cache_operation_boundary;
    # install a boundary-capable client double (the frame drop itself needs
    # no cache).
    from types import SimpleNamespace

    from coherence_harness import make_boundary_capable

    monkeypatch.setattr(
        srv,
        "get_client",
        lambda: make_boundary_capable(SimpleNamespace(), nodes=[]),
    )
    ws = _FakeWebSocket()
    asyncio.run(srv._handle_mutate_node_tags({
        "action": "mutate_node_tags",
        "v": 1,
        "operations": [{"handle": "u", "operation": "add", "tags": ["a"]}],
    }, ws))
    assert ws.sent == []
