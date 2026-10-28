"""N03 G1 fake HTTP, real client/governor; recorded P0-prime wire provenance.

Fixtures transcribed from p0prime_s6.out (2026-10-23), rows 1-34, and result
shapes from p0prime_s1.out rows 8-13. No user scratch dependency or live API.
"""
from __future__ import annotations

import asyncio
import html
import inspect
import json
import os
import textwrap

import httpx
import pytest

from test_carto_write_governor_phase_b import _real_governed_client
from workflowy_mcp.client import api_client_core as core
from workflowy_mcp.client import create_transport as transport
from workflowy_mcp.client.create_transport import (
    LLM_DOC_EDIT_PATH, LlmInsertAfterTransport, LlmResultKind,
    PlacementIntent, PublicV1CreateTransport, parse_llm_insert_response,
)
from workflowy_mcp.models import APIResponseError, NodeCreateRequest, RateLimitError

PARENT = "e17fb70d-5308-41c1-813d-df966ac674fc"
PREDECESSOR = "325b64e4-1bb3-4d0c-ab65-f291aaf19c9c"
CREATED = "c02cdc51-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def source_copy_mutant(monkeypatch):
    """Counterfactuals mutate only process-local source copies, never disk."""
    mutant = os.getenv("N03_TEST_MUTANT")
    if not mutant:
        return
    if mutant in {"redirect_follow", "redirect_retry", "breaker_recheck"}:
        owner, name, namespace = core.WorkFlowyClientCore, "_send_workflowy_write", vars(core)
        old, new = {
            "redirect_follow": ('kwargs["follow_redirects"] = False', 'kwargs["follow_redirects"] = True'),
            "redirect_retry": ('if 300 <= status < 400:', 'if False:'),
            "breaker_recheck": ('await governor.recheck_before_send(ticket, cancel_check=cancel_check)', 'pass'),
        }[mutant]
    else:
        owner, name, namespace = LlmInsertAfterTransport, "insert_after", vars(transport)
        old, new = {
            "name_decode": ('html.unescape(self.client.encode_for_workflowy_api(request.name, "name", operation="create"))', 'self.client.encode_for_workflowy_api(request.name, "name", operation="create")'),
            "note_decode": ('item["d"] = self.client.encode_for_workflowy_api(request.note, "note", operation="create")', 'item["d"] = html.unescape(self.client.encode_for_workflowy_api(request.note, "note", operation="create"))'),
            "sender_bypass": ('self.client._send_workflowy_write(', 'self.client.client.request('),
        }[mutant]
    source = textwrap.dedent(inspect.getsource(getattr(owner, name)))
    assert source.count(old) == 1
    source = source.replace(old, new, 1)
    if mutant == "sender_bypass":
        source = source.replace('operation="create", before_send=before_send, ', '')
    namespace = dict(namespace)
    exec(compile(source, "<n03-test-only-mutant>", "exec"), namespace)
    monkeypatch.setattr(owner, name, namespace[name])


# semantic, v1 name wire, LLM wire AND independently recorded stored name.
P2 = [
    ('P0′-P2-A plain <angle> & ampersand "quote"',
     'P0′-P2-A plain &amp;lt;angle&amp;gt; &amp;amp; ampersand "quote"',
     'P0′-P2-A plain &lt;angle&gt; &amp; ampersand "quote"'),
    ('P0′-P2-B markup <b>bold &amp; raw</b> tail',
     'P0′-P2-B markup <b>bold &amp;amp;amp; raw</b> tail',
     'P0′-P2-B markup <b>bold &amp;amp; raw</b> tail'),
    ('P0′-P2-C <code>x &lt; y</code> and <i>ital</i> & bare <tag>',
     'P0′-P2-C <code>x &amp;amp;lt; y</code> and <i>ital</i> &amp;amp; bare &amp;lt;tag&amp;gt;',
     'P0′-P2-C <code>x &amp;lt; y</code> and <i>ital</i> &amp; bare &lt;tag&gt;'),
    ('P0′-P2-D emoji ⚡🔥 and ünïcødé — dash',) * 3,
    ('literal &lt; &amp;', 'literal &amp;amp;lt; &amp;amp;amp;', 'literal &amp;lt; &amp;amp;'),
] + [
    (f"https://example.test/?x=1&{key}=2",
     f"https://example.test/?x=1&amp;amp;{key}=2",
     f"https://example.test/?x=1&amp;{key}=2")
    for key in ("param", "section", "timestamp", "notebook", "copy", "region")
]
NOTE = 'note with <angle> & "q" and <b>bold</b> & &amp; ⚡'
NOTE_WIRE = 'note with &lt;angle&gt; & "q" and <b>bold</b> & &amp;amp; ⚡'


@pytest.mark.parametrize("semantic,v1_wire,stored", P2)
@pytest.mark.parametrize("note,note_wire", [(None, None), ("", ""), (NOTE, NOTE_WIRE), ("?x=1&param=2", "?x=1&param=2")])
def test_recorded_wire_and_independent_storage_parity(tmp_path, semantic, v1_wire, stored, note, note_wire):
    async def run():
        requests, observed = [], []

        def server(request):
            body = json.loads(request.content)
            requests.append((request.url.path, body))
            if request.url.path.endswith("/nodes/"):
                # Independent P0 server model: one name decode on the v1 path;
                # LLM stores n directly; note wire is stored without decoding.
                observed.append((html.unescape(body["name"]), body.get("note")))
                return httpx.Response(200, json={"item_id": CREATED})
            item = body["operations"][0]["items"][0]
            observed.append((item["n"], item.get("d", "")))
            return httpx.Response(200, json={"results": [{"op": "insert", "status": "ok", "created": 1}]})

        client, _, _ = _real_governed_client(tmp_path, server)
        async with client._client:
            request = NodeCreateRequest(parent_id=PARENT, name=semantic, note=note)
            original = request.model_dump()
            v1 = await PublicV1CreateTransport(client).create(request, PlacementIntent.after(PREDECESSOR))
            llm = await LlmInsertAfterTransport(client).create(request, PlacementIntent.after(PREDECESSOR))
            assert request.model_dump() == original
            assert requests[0][1]["name"] == v1_wire
            assert requests[1][1]["operations"][0]["items"] == [{"n": stored, **({"d": note_wire} if note else {})}]
            assert observed[0][0] == observed[1][0] == stored
            assert (observed[0][1] or "") == (observed[1][1] or "") == (note_wire or "")
            assert v1.reorder_required and v1.physical_placement == PlacementIntent.bottom()
            assert tuple(n.id for n in v1.returned_nodes) == (CREATED,)
            assert not llm.returned_nodes and not llm.reorder_required
            assert llm.llm_response.kind == LlmResultKind.ACKNOWLEDGED
            assert len(requests) == 2  # No trailing item GET or doc/read.
    asyncio.run(run())


def test_origin_headers_timeout_one_client_one_ticket_and_order(tmp_path, monkeypatch):
    async def run():
        seen, history = [], []
        def server(request):
            history.append("send")
            seen.append(request)
            return httpx.Response(200, json={"results": [{"op": "insert", "status": "ok", "created": 1, "message": "extra"}]})
        client, governor, _ = _real_governed_client(tmp_path, server)
        await client._client.aclose()
        client._client = None
        client.base_url = "https://configured.example:9443/prefix/api/v1?ignored=yes#fragment"
        original_factory = httpx.AsyncClient
        constructions = []
        def factory(**kwargs):
            constructions.append(kwargs)
            return original_factory(**kwargs, transport=httpx.MockTransport(server))
        monkeypatch.setattr(core.httpx, "AsyncClient", factory)
        acquire, recheck = governor.acquire_write_slot, governor.recheck_before_send
        async def ticket(*args, **kwargs):
            history.append("ticket")
            return await acquire(*args, **kwargs)
        async def final(*args, **kwargs):
            history.append("recheck")
            return await recheck(*args, **kwargs)
        async def preflight():
            history.append("preflight")
        monkeypatch.setattr(governor, "acquire_write_slot", ticket)
        monkeypatch.setattr(governor, "recheck_before_send", final)
        try:
            await LlmInsertAfterTransport(client).insert_after(PARENT, PREDECESSOR, [NodeCreateRequest(name="Name")], before_send=preflight)
            assert history == ["ticket", "preflight", "recheck", "send"]
            assert len(constructions) == len(seen) == 1
            assert str(seen[0].url) == "https://configured.example:9443" + LLM_DOC_EDIT_PATH
            assert seen[0].headers["authorization"] == "Bearer phase-b-test-key"
            assert constructions[0]["timeout"] == httpx.Timeout(client.config.timeout)
            assert constructions[0]["follow_redirects"] is True  # Read policy unchanged.
            assert json.loads(seen[0].content)["root"] == "df966ac674fc"
            assert json.loads(seen[0].content)["operations"][0]["after"] == PREDECESSOR
        finally:
            await client._client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("kind,physical", [(PlacementIntent.top(), "top"), (PlacementIntent.bottom(), "bottom"), (PlacementIntent.after(PREDECESSOR), "bottom")])
def test_v1_thin_owned_delegation(kind, physical):
    async def run():
        from types import SimpleNamespace
        request = NodeCreateRequest(name="Name & stuff", note="Note", layoutMode="todo")
        calls = []
        hint = object()
        async def create(owned, **kwargs):
            calls.append((owned.model_dump(), kwargs))
            owned.name = "mutated by legacy encoder"
            return SimpleNamespace(id=CREATED)
        result = await PublicV1CreateTransport(SimpleNamespace(create_node=create)).create(request, kind, governance_coverage_hint=hint)
        assert calls == [({**request.model_dump(), "position": physical}, {"_internal_call": True, "fetch_created_node": False, "_governance_coverage_hint": hint})]
        assert request.name == "Name & stuff"
        assert result.reorder_required == (kind.kind == "after")
    asyncio.run(run())


@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("adapter", ["v1", "llm", "explicit_true"])
def test_redirect_is_one_nonretrying_request(tmp_path, status, adapter):
    async def run():
        seen = []
        def server(request):
            seen.append(str(request.url))
            if request.url.host == "elsewhere.example":
                return httpx.Response(200, json={"item_id": CREATED})
            return httpx.Response(status, headers={"Location": "https://elsewhere.example/steal"})
        client, _, _ = _real_governed_client(tmp_path, server)
        # Intentionally use today's shared read/client redirect default.
        client._client.follow_redirects = True
        async with client._client:
            with pytest.raises(APIResponseError) as caught:
                if adapter == "explicit_true":
                    await client._send_workflowy_write("POST", "/nodes/", operation="create", follow_redirects=True)
                else:
                    cls = PublicV1CreateTransport if adapter == "v1" else LlmInsertAfterTransport
                    await cls(client).create(NodeCreateRequest(parent_id=PARENT, name="Name"), PlacementIntent.after(PREDECESSOR))
            assert caught.value.status_code == status
            assert len(seen) == 1
            assert "elsewhere.example" not in seen[0]
    asyncio.run(run())


def test_llm_ordered_vector_has_no_count_cap(tmp_path):
    async def run():
        bodies = []
        def server(request):
            body = json.loads(request.content)
            bodies.append(body)
            return httpx.Response(200, json={"results": [{"op": "insert", "status": "ok", "created": 151}]})
        client, _, _ = _real_governed_client(tmp_path, server)
        async with client._client:
            result = await LlmInsertAfterTransport(client).insert_after(PARENT, PREDECESSOR, [NodeCreateRequest(name=f"item-{i}") for i in range(151)])
            assert result.kind == LlmResultKind.ACKNOWLEDGED
            assert bodies[0]["operations"][0]["items"] == [{"n": f"item-{i}"} for i in range(151)]
            assert len(bodies) == 1
    asyncio.run(run())


@pytest.mark.parametrize("payload,kind", [
    ({"results": [{"op": "insert", "status": "ok", "created": 1, "message": "hi", "new": {}}], "new": 2}, LlmResultKind.ACKNOWLEDGED),
    ({"results": [{"op": "insert", "status": "error", "error": "not_found", "message": "unknown ref"}]}, LlmResultKind.OPERATION_ERROR),
    ({"results": [{"op": "insert", "status": "error", "error": "invalid"}]}, LlmResultKind.OPERATION_ERROR),
    ({}, LlmResultKind.SCHEMA_DRIFT),
    ({"results": []}, LlmResultKind.SCHEMA_DRIFT),
    ({"results": [{"status": "ok", "created": 1}]}, LlmResultKind.SCHEMA_DRIFT),
    ({"results": [{"op": "move", "status": "ok", "created": 1}]}, LlmResultKind.SCHEMA_DRIFT),
    ({"results": [{"op": "insert", "status": "ok", "created": 2}]}, LlmResultKind.SCHEMA_DRIFT),
    ({"results": [{"op": "insert", "status": "ok", "created": True}]}, LlmResultKind.SCHEMA_DRIFT),
    ({"results": [{"op": "insert", "status": "maybe", "created": 1}]}, LlmResultKind.SCHEMA_DRIFT),
    ({"results": [{"op": "insert", "status": "error"}]}, LlmResultKind.SCHEMA_DRIFT),
])
def test_parser_acknowledgement_not_identity(payload, kind):
    result = parse_llm_insert_response(httpx.Response(200, json=payload), 1)
    assert result.kind == kind
    assert not hasattr(result, "created_ids")


@pytest.mark.parametrize("status,payload,kind", [
    (400, {"code": "read_required", "message": "Read first"}, LlmResultKind.READ_REQUIRED),
    (500, {}, LlmResultKind.HTTP_ERROR),
] + [(s, {}, LlmResultKind.UNSUPPORTED_ENDPOINT) for s in (401, 403, 404, 405, 410, 501, 307, 308)])
def test_parser_http_axes(status, payload, kind):
    assert parse_llm_insert_response(httpx.Response(status, json=payload), 1).kind == kind


def test_malformed_json_is_schema_evidence_not_identity():
    assert parse_llm_insert_response(httpx.Response(200, content=b"not json"), 1).kind == LlmResultKind.SCHEMA_DRIFT


def test_llm_429_uses_shared_lane_without_retry(tmp_path):
    async def run():
        seen = []
        def server(request):
            seen.append(request)
            return httpx.Response(429, headers={"Retry-After": "7"})
        client, governor, clock = _real_governed_client(tmp_path, server)
        async with client._client:
            with pytest.raises(RateLimitError) as caught:
                await LlmInsertAfterTransport(client).create(NodeCreateRequest(parent_id=PARENT, name="Name"), PlacementIntent.after(PREDECESSOR))
            assert caught.value.retry_after == 7
            before = clock()
            ticket = await governor.acquire_write_slot("later-v1", sleep_fn=client._sleep_with_operation_cancel)
            assert ticket.slot_at >= before + 7
            assert len(seen) == 1
    asyncio.run(run())


def test_llm_final_breaker_recheck_prevents_send(tmp_path, monkeypatch):
    async def run():
        seen = []
        client, governor, _ = _real_governed_client(tmp_path, lambda req: seen.append(req) or httpx.Response(200, json={}))
        async def deny(*args, **kwargs):
            raise RuntimeError("new breaker evidence")
        monkeypatch.setattr(governor, "recheck_before_send", deny)
        async with client._client:
            with pytest.raises(RuntimeError, match="new breaker evidence"):
                await LlmInsertAfterTransport(client).create(NodeCreateRequest(parent_id=PARENT, name="Name"), PlacementIntent.after(PREDECESSOR))
            assert not seen
    asyncio.run(run())
