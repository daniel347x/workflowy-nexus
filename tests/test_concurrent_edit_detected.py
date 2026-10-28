"""N02 G1 real-client witnesses; no live account and no replacement UPDATE funnel."""

import asyncio
import inspect
import json
import os
import textwrap

import httpx
import pytest

from test_coherence_s03_closure_repairs import _make_client, _populate
from workflowy_mcp.carto_write_governor import WriteSlotTicket
from workflowy_mcp.client import api_client_core as core
from workflowy_mcp.models import APIResponseError, NetworkError, NodeUpdateRequest
from workflowy_mcp.mutation_conflicts import ConcurrentEditDetectedError

NODE = "f3889711-3f62-411d-af5c-6010f068789b"
OTHER = "fbbead46-2a5e-4ec5-91c7-391826d641a5"


class PassGovernor:
    def __init__(self, original, history):
        self.paths = original.paths
        self.settings = original.settings
        self.history = history
        self.deny = None
        self.recheck_error = None

    async def acquire_write_slot(self, operation, **kwargs):
        self.history.append("acquire")
        if self.deny:
            raise self.deny
        return WriteSlotTicket(0.0, 0.0, operation)

    async def recheck_before_send(self, ticket, **kwargs):
        self.history.append("recheck")
        kwargs["cancel_check"]()
        if self.recheck_error:
            raise self.recheck_error


class WireState:
    """External GUI truth is separate from the client's immutable cache."""
    def __init__(self, **fields):
        self.node = {"id": NODE, "name": "Old", "note": "before", **fields}
        self.history = []
        self.gets = 0
        self.posts = []
        self.final_change = None
        self.after_change = None
        self.get_errors = {}
        self.get_omit = {}
        self.full_response = False

    async def request(self, request):
        if request.method == "GET":
            self.history.append("GET")
            self.gets += 1
            if self.gets == 1 and self.final_change:
                self.node.update(self.final_change)
            error = self.get_errors.get(self.gets)
            if error == "network":
                return httpx.Response(503, json={"error": "unavailable"})
            if error:
                return httpx.Response(error, headers={"Retry-After": "0"}, json={})
            observed = dict(self.node)
            for field in self.get_omit.get(self.gets, ()):
                observed.pop(field, None)
            return httpx.Response(200, json={"node": observed})
        assert request.method == "POST"
        self.history.append("POST")
        body = json.loads(request.content)
        self.posts.append(body)
        self.node.update({key: value for key, value in body.items() if key in ("name", "note")})
        if self.after_change:
            self.node.update(self.after_change)
        return httpx.Response(200, json=dict(self.node) if self.full_response else {"status": "ok"})


@pytest.fixture(autouse=True)
def isolated_neutralization(monkeypatch):
    """Damage only a source COPY inside this test process, never production bytes."""
    mutation = os.environ.get("N02_TEST_NEUTRALIZE")
    if not mutation:
        return
    source = textwrap.dedent(inspect.getsource(core.WorkFlowyClientCore.update_node))
    old, new = {
        "preflight": ("if not matches_basis and not matches_sent:", "if False:"),
        "postflight": ("if post_differences:", "if False:"),
        "429_prior": ("except RateLimitError as e:\n", "except RateLimitError as e:\n    prior_send_capable = False\n"),
    }[mutation]
    if mutation == "429_prior":
        # The inherited catch is indented within while/try, not at module level.
        old = "        except RateLimitError as e:\n"
        new = "        except RateLimitError as e:\n            prior_send_capable = False\n"
    assert source.count(old) == 1
    namespace = dict(vars(core))
    exec(compile(source.replace(old, new, 1), "<n02-test-only-mutant>", "exec"), namespace)
    monkeypatch.setattr(core.WorkFlowyClientCore, "update_node", namespace["update_node"])


@pytest.fixture
def engine(tmp_path, monkeypatch):
    clients = []

    def make(state, *, populated=True, missing_row=False):
        client, _, _ = _make_client(tmp_path, monkeypatch)
        if populated:
            nodes = [{"id": OTHER, "name": "Untouched"}]
            if not missing_row:
                nodes.insert(0, dict(state.node))
            _populate(client, nodes=nodes)
        client._write_governor = PassGovernor(client._write_governor, state.history)
        client._client = httpx.AsyncClient(base_url="https://workflowy.com/api/v1", transport=httpx.MockTransport(state.request))
        sleeps = []

        async def sleep(seconds, operation):
            sleeps.append((seconds, operation))

        monkeypatch.setattr(client, "_sleep_with_operation_cancel", sleep)
        client.test_sleeps = sleeps
        clients.append(client)
        return client

    yield make
    for client in clients:
        asyncio.run(client._client.aclose())


@pytest.mark.parametrize("field", ["name", "note"])
def test_preflight_change_is_no_send_and_publishes_before_error(engine, field):
    async def run():
        state = WireState()
        client = engine(state)
        predecessor = client._cache_view_publisher.capture()
        state.final_change = {field: "GUI"}
        basis = {field: state.node[field]}
        with pytest.raises(ConcurrentEditDetectedError) as caught:
            await client.update_node(NODE, NodeUpdateRequest(**{field: "Agent"}), basis=basis)
        error = caught.value
        assert error.extra == {"node_id": NODE, "fields": [field], "live": {field: "GUI"},
                               "expected": basis, "retry_after": 0.0, "detected_at": "preflight"}
        assert "This attempt did not send" in str(error)
        assert state.posts == [] and state.gets == 1
        assert state.history == ["acquire", "GET"]
        current = client._cache_view_publisher.capture()
        assert current.nodes_by_id[NODE][field] == "GUI"
        assert predecessor.nodes_by_id[NODE][field] == basis[field]
        assert current.nodes_by_id[OTHER] is predecessor.nodes_by_id[OTHER]
        assert not client.test_sleeps
    asyncio.run(run())


@pytest.mark.parametrize("full_response", [False, True])
@pytest.mark.parametrize("field", ["name", "note"])
def test_postflight_change_is_sent_and_publishes_before_error(engine, field, full_response):
    async def run():
        state = WireState()
        state.full_response = full_response
        state.after_change = {field: "GUI after"}
        client = engine(state)
        with pytest.raises(ConcurrentEditDetectedError) as caught:
            await client.update_node(NODE, NodeUpdateRequest(**{field: "Agent"}), basis={field: state.node[field]})
        error = caught.value
        assert error.detected_at == "postflight" and error.fields == [field]
        assert error.expected == {field: "Agent"} and error.live == {field: "GUI after"}
        assert "was sent" in str(error)
        assert len(state.posts) == 1 and state.gets == (1 if full_response else 2)
        assert client._cache_view_publisher.capture().nodes_by_id[NODE][field] == "GUI after"
        assert not client.test_sleeps
    asyncio.run(run())


BARE_AMPERSAND_WIRE = [
    (f"https://example.test/?x=1&{key}=2", returned)
    for key in ("param", "section", "timestamp", "notebook", "copy", "region")
    for returned in (
        f"https://example.test/?x=1&amp;{key}=2",
        f'<a href="https://example.test/?x=1&amp;{key}=2">https://example.test/?x=1&amp;{key}=2</a>',
    )
] + [("Terms &copy 2026", "Terms &amp;copy 2026")]


AUTO_LINK_BOUNDARY_WIRE = [
    ("[docs](https://example.test/docs/)", '[docs](<a href="https://example.test/docs/">https://example.test/docs/</a>)'),
    ("See https://example.test/docs/.", 'See <a href="https://example.test/docs/">https://example.test/docs/</a>.'),
    ("See https://example.test/docs/, then", 'See <a href="https://example.test/docs/">https://example.test/docs/</a>, then'),
    ("(https://example.test/)", '(<a href="https://example.test/">https://example.test/</a>)'),
    ("(www.example.test/)", '(<a href="https://www.example.test/">www.example.test/</a>)'),
] + [
    (f"See {url}{suffix}", f'See <a href="{url}">{url}</a>{suffix}')
    for url in ("https://example.test/docs/", "https://example.test/docs")
    for suffix in (";", ":", "!", "?", "]", '"', "'", "*", " next")
] + [
    ("First (https://one.test/), then [two](https://two.test/docs/).",
     'First (<a href="https://one.test/">https://one.test/</a>), then [two](<a href="https://two.test/docs/">https://two.test/docs/</a>).'),
]


EQUAL_WIRE = [
    ("A &amp; B &lt; C", "A &#38; B &#x3c; C"),
    ("literal &amp;lt; &amp;amp; &amp;nbsp;", "literal &#38;lt; &#38;amp; &#38;nbsp;"),
    ("`&lt;a href=\"x\"&gt;x&lt;/a&gt;`", "`&#60;a href=\"x\"&#62;x&#60;/a&#62;`"),
    ("https://example.test/?a=1&amp;b=2", '<A HREF=\'https://example.test/?a=1&#38;b=2\'>https://example.test/?a=1&amp;b=2</A>'),
    ("https://example.test/?a=1&b=2", '<a href="https://example.test/?a=1&amp;b=2">https://example.test/?a=1&amp;b=2</a>'),
    ("https://example.test/?a=&amp;lt;", '<a href="https://example.test/?a=&amp;lt;">https://example.test/?a=&amp;lt;</a>'),
    ('<b><span class="colored c-red">red</span></b>', "<SPAN class='colored c-red'>red</SPAN>"),
    ('<b title="x" class="y">bold</b>', "<B class='y' title='x'>bold</B>"),
    ("www.example.test", '<a href="https://www.example.test/">www.example.test</a>'),
] + BARE_AMPERSAND_WIRE + AUTO_LINK_BOUNDARY_WIRE


@pytest.mark.parametrize("field", ["name", "note"])
@pytest.mark.parametrize("left,right", EQUAL_WIRE)
def test_comparison_equal_wire_provenance(field, left, right):
    assert core._canonical_update_value(field, left) == core._canonical_update_value(field, right)


@pytest.mark.parametrize("field", ["name", "note"])
@pytest.mark.parametrize("left,right", [
    ("&amp;lt;", "&lt;"),
    ('&lt;a href="target"&gt;label&lt;/a&gt;', '<a href="target">label</a>'),
    ('<a href="one">label</a>', '<a href="two">label</a>'),
    ('<span class="colored c-red">x</span>', '<span class="colored c-blue">x</span>'),
    ("<b>x</b>", "x"), ("<i>x</i>", "<s>x</s>"),
    ("https://one.test", "https://two.test"),
    ("word", "words"),
])
def test_comparison_content_inequality(field, left, right):
    assert core._canonical_update_value(field, left) != core._canonical_update_value(field, right)


@pytest.mark.parametrize("left,right", [
    ("[ap:ABC123] 🚀 Title #topic", "[ap:ABC123]  🚀  Title  #<u>topic</u>"),
    ("Title #tag-suffix more", "Title #tag<u>-suffix</u> more"),
    ("✈ Title #tag", "✈️ Title #tag"),
    ("A\nB", "A B"),
    ("Title #topic 🔱", "Title #topic"),
    ("Title 🔱", "Title"),
    ("Title  internal #topic", "Title internal #topic"),
])
def test_name_presentation_equality(left, right):
    assert core._canonical_update_value("name", left) == core._canonical_update_value("name", right)


@pytest.mark.parametrize("left,right", [
    ("[ap:ABC123] 🚀 Title #topic", "[ap:ABC124] 🚀 Title #topic"),
    ("🚀 Title #topic", "🚁 Title #topic"),
    ("Title #topic", "Title #other"),
    ("Title #a #b", "Title #b #a"),
    ("Title #&lt;u&gt;tag&lt;/u&gt;", "Title #<u>tag</u>"),
    ("Title  internal", "Title internal"),
    ("👩‍💻 Title", "👩 Title"),
    ("👍🏻 Title", "👍🏿 Title"),
    ("A\ufe0f", "A"),
    ("A\tB", "A B"),
    ("[ap:ABC123] Title", "[ap:abc123] Title"),
])
def test_name_content_inequality(left, right):
    assert core._canonical_update_value("name", left) != core._canonical_update_value("name", right)


def test_note_whitespace_equivalence_and_content():
    project = lambda value: core._canonical_update_value("note", value)
    assert project("  first\r\n\tsecond\r\n") == project("\u00a0 first\n\tsecond\n")
    assert project(None) == project("")
    for other in (" first\n\tsecond", "  first\nsecond", "  first\n\tsecond\nthird"):
        assert project("  first\n\tsecond") != project(other)


def test_unknown_attribute_fragments_remain_comparison_data():
    for field in ("name", "note"):
        value = '<b><span class>text</span></b>'
        assert core._canonical_update_value(field, value) == core._canonical_update_value(field, value)
        assert core._canonical_update_value(field, value) != core._canonical_update_value(field, "text")


@pytest.mark.parametrize("field", ["name", "note"])
@pytest.mark.parametrize("semantic,returned", [
    ("A & B < C", "A &#38; B &#60; C"),
    ("&lt; &amp; &nbsp;", "&#38;lt; &#38;amp; &#38;nbsp;"),
    ("https://example.test/?a=1&b=2", '<a href="https://example.test/?a=1&amp;b=2">https://example.test/?a=1&amp;b=2</a>'),
    ('<span class="colored c-red">red</span>', '<span class="colored c-red">red</span>'),
    ('`<a href="x">x</a>`', '`&#60;a href="x"&#62;x&#60;/a&#62;`'),
] + BARE_AMPERSAND_WIRE + AUTO_LINK_BOUNDARY_WIRE)
def test_real_encoder_postflight_normalization_and_next_basis(engine, field, semantic, returned):
    async def run():
        state = WireState()
        state.after_change = {field: returned}
        client = engine(state)
        await client.update_node(NODE, NodeUpdateRequest(**{field: semantic}), basis={field: state.node[field]})
        assert len(state.posts) == 1 and state.gets == 2
        # The new basis is the preceding raw read-back, not the submitted value.
        await client.update_node(NODE, NodeUpdateRequest(**{field: semantic}), basis={field: returned})
        assert len(state.posts) == 2 and state.gets == 4
    asyncio.run(run())


@pytest.mark.parametrize("field", ["name", "note"])
def test_unwritten_fields_and_modified_at_never_refuse(engine, field):
    async def run():
        state = WireState()
        other = "note" if field == "name" else "name"
        state.final_change = {other: "GUI other", "modifiedAt": 200}
        client = engine(state)
        await client.update_node(NODE, NodeUpdateRequest(**{field: "Agent"}), basis={field: state.node[field]})
        assert len(state.posts) == 1 and state.gets == 2
        current = client._cache_view_publisher.capture().nodes_by_id[NODE]
        assert current[other] == "GUI other" and current["modifiedAt"] == 200
    asyncio.run(run())


def test_combined_mask_reports_only_changed_field_and_one_get(engine):
    async def run():
        state = WireState()
        state.final_change = {"note": "GUI"}
        client = engine(state)
        with pytest.raises(ConcurrentEditDetectedError) as caught:
            await client.update_node(NODE, NodeUpdateRequest(name="Agent", note="Agent note"), basis={"name": "Old", "note": "before"})
        assert caught.value.fields == ["note"] and state.gets == 1 and not state.posts
    asyncio.run(run())


@pytest.mark.parametrize("null_field", ["name", "note"])
@pytest.mark.parametrize("stage", ["preflight", "postflight"])
def test_explicit_null_observation_clears_both_cache_keys(engine, null_field, stage):
    async def run():
        state = WireState()
        setattr(state, "final_change" if stage == "preflight" else "after_change", {null_field: None})
        client = engine(state)
        with pytest.raises(ConcurrentEditDetectedError) as caught:
            await client.update_node(NODE, NodeUpdateRequest(**{null_field: "Agent"}), basis={null_field: state.node[null_field]})
        assert caught.value.live == {null_field: None}
        current = client._cache_view_publisher.capture().nodes_by_id[NODE]
        assert current[null_field] == "" and current["nm" if null_field == "name" else "no"] == ""
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["publisher", "unpopulated", "missing_row"])
def test_conflict_keeps_typed_error_and_dirty_fallback(engine, monkeypatch, fault):
    async def run():
        state = WireState()
        state.final_change = {"note": "GUI"}
        client = engine(state, populated=fault != "unpopulated", missing_row=fault == "missing_row")
        dirty_calls = []
        mark_dirty = client._mark_nodes_export_dirty
        def record_dirty(node_ids):
            dirty_calls.append(node_ids)
            mark_dirty(node_ids)
        monkeypatch.setattr(client, "_mark_nodes_export_dirty", record_dirty)
        if fault == "publisher":
            def broken(*args, **kwargs):
                raise RuntimeError("broken publisher")
            monkeypatch.setattr(client, "publish_local_cache_view", broken)
        elif fault == "missing_row":
            assert NODE not in client._cache_view_publisher.capture().nodes_by_id
        with pytest.raises(ConcurrentEditDetectedError) as caught:
            await client.update_node(NODE, NodeUpdateRequest(note="Agent"), basis={"note": "before"})
        assert caught.value.live == {"note": "GUI"}
        assert dirty_calls == [[NODE]] and not state.posts
        if fault != "unpopulated":
            assert NODE in client._nodes_export_dirty_ids
    asyncio.run(run())


@pytest.mark.parametrize("failure", [429, "network"])
def test_inherited_retry_accepts_own_sent_whole_vector(engine, failure):
    async def run():
        state = WireState()
        state.get_errors[2] = failure
        client = engine(state)
        await client.update_node(NODE, NodeUpdateRequest(name="New", note="new note"), max_retries=2,
                                 basis={"name": "Old", "note": "before"})
        assert state.posts == [{"name": "New", "note": "new note"}] * 2
        assert state.gets == 4 and len(client.test_sleeps) == 1
        assert state.history == ["acquire", "GET", "recheck", "POST", "GET"] * 2
    asyncio.run(run())


@pytest.mark.parametrize("mixed", [False, True])
def test_inherited_429_retry_third_vector_suppresses_next_post(engine, mixed):
    async def run():
        state = WireState()
        state.get_errors[2] = 429
        client = engine(state)
        async def retry_sleep(seconds, operation):
            client.test_sleeps.append((seconds, operation))
            state.node.update(name="Old" if mixed else "GUI", note="new note")
        client._sleep_with_operation_cancel = retry_sleep
        with pytest.raises(ConcurrentEditDetectedError) as caught:
            await client.update_node(NODE, NodeUpdateRequest(name="New", note="new note"), max_retries=2,
                                     basis={"name": "Old", "note": "before"})
        error = caught.value
        assert error.detected_at == "preflight"
        assert error.previously_sent == {"name": "New", "note": "new note"}
        assert "earlier attempt" in str(error)
        assert len(state.posts) == 1 and state.gets == 3
        assert client._cache_view_publisher.capture().nodes_by_id[NODE]["note"] == "new note"
    asyncio.run(run())


def test_raw_basis_is_copied_before_ticket_and_encoding(engine):
    async def run():
        state = WireState()
        basis = {"name": "Old"}
        client = engine(state)
        acquire = client._write_governor.acquire_write_slot
        async def grant(*args, **kwargs):
            basis["name"] = "Changed caller dict"
            return await acquire(*args, **kwargs)
        client._write_governor.acquire_write_slot = grant
        await client.update_node(NODE, NodeUpdateRequest(name="A & B"), basis=basis)
        assert state.posts == [{"name": "A &amp; B"}]
    asyncio.run(run())


@pytest.mark.parametrize("basis,updates", [(None, {"note": "Agent"}), ({}, {"layoutMode": "bullets"})])
def test_transitional_and_layout_only_paths_have_no_n02_get(engine, basis, updates):
    async def run():
        state = WireState()
        state.after_change = {"note": "Different"}
        client = engine(state)
        await client.update_node(NODE, NodeUpdateRequest(**updates), basis=basis)
        assert state.gets == 1 and len(state.posts) == 1
        assert state.history == ["acquire", "recheck", "POST", "GET"]
    asyncio.run(run())


@pytest.mark.parametrize("point", ["deny", "hook_cancel", "hook_error", "recheck"])
def test_hook_failure_or_final_recheck_never_creates_write(engine, point):
    async def run():
        state = WireState()
        client = engine(state)
        async def hook():
            state.history.append("hook")
            if point == "hook_cancel":
                raise asyncio.CancelledError()
            if point == "hook_error":
                raise RuntimeError("hook")
            client._write_governor.recheck_error = RuntimeError("breaker")
        if point == "deny":
            client._write_governor.deny = RuntimeError("deny")
        with pytest.raises(asyncio.CancelledError if point == "hook_cancel" else RuntimeError):
            await client._send_workflowy_write("POST", f"/nodes/{NODE}", operation="test", before_send=hook, json={})
        assert not state.posts and state.gets == 0
        assert state.history == (["acquire"] if point == "deny" else ["acquire", "hook", "recheck"] if point == "recheck" else ["acquire", "hook"])
    asyncio.run(run())


def test_preflight_ordinary_4xx_keeps_existing_error(engine):
    async def run():
        state = WireState()
        state.get_errors[1] = 400
        client = engine(state)
        with pytest.raises(APIResponseError):
            await client.update_node(NODE, NodeUpdateRequest(note="Agent"), basis={"note": "before"})
        assert not state.posts and state.gets == 1
    asyncio.run(run())


def test_error_is_not_a_retryable_transport_error():
    assert not issubclass(ConcurrentEditDetectedError, (NetworkError, httpx.TransportError))
    from workflowy_mcp.models import RateLimitError
    assert not issubclass(ConcurrentEditDetectedError, RateLimitError)


@pytest.mark.parametrize("field", ["name", "note"])
@pytest.mark.parametrize("left,right", EQUAL_WIRE)
def test_preflight_presentation_changes_are_equal(engine, field, left, right):
    async def run():
        state = WireState(**{field: left})
        state.final_change = {field: right}
        client = engine(state)
        await client.update_node(NODE, NodeUpdateRequest(**{field: "Agent"}), basis={field: left})
        assert len(state.posts) == 1 and state.gets == 2
    asyncio.run(run())


@pytest.mark.parametrize("field,semantic,returned", [
    ("name", "[ap:ABC123] 🚀 Title #topic", "[ap:ABC123] 🚀 Title #<u>topic</u>"),
    ("note", "  first\r\n\tsecond\r\n", "\u00a0 first\n\tsecond\n"),
])
def test_field_specific_encoder_normalization_and_next_basis(engine, field, semantic, returned):
    async def run():
        state = WireState(**{field: semantic})
        state.final_change = {field: returned}
        state.after_change = {field: returned}
        client = engine(state)
        await client.update_node(NODE, NodeUpdateRequest(**{field: semantic}), basis={field: state.node[field]})
        await client.update_node(NODE, NodeUpdateRequest(**{field: semantic}), basis={field: returned})
        assert len(state.posts) == 2 and state.gets == 4
    asyncio.run(run())


def test_empty_note_is_written_and_null_readback_is_success(engine):
    async def run():
        state = WireState()
        state.after_change = {"note": None}
        client = engine(state)
        result = await client.update_node(NODE, NodeUpdateRequest(note=""), basis={"note": "before"})
        assert result.note is None and state.posts == [{"note": ""}]
        current = client._cache_view_publisher.capture().nodes_by_id[NODE]
        assert current["note"] == current["no"] == ""
    asyncio.run(run())


def test_cancellation_during_direct_final_get_suppresses_post(engine):
    async def run():
        state = WireState()
        arrived = asyncio.Event()
        cancelled = []
        original = state.request
        async def delayed(request):
            if request.method == "GET":
                arrived.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)
            return await original(request)
        state.request = delayed
        client = engine(state)
        task = asyncio.create_task(client.update_node(NODE, NodeUpdateRequest(note="Agent"), basis={"note": "before"}))
        await arrived.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled == [True] and not state.posts
        assert state.history == ["acquire"]
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["preflight", "postflight"])
def test_partial_observation_does_not_invent_unwritten_null_alias(engine, stage):
    async def run():
        state = WireState()
        setattr(state, "final_change" if stage == "preflight" else "after_change", {"note": "GUI"})
        state.get_omit[1 if stage == "preflight" else 2] = {"name"}
        client = engine(state)
        before = client._cache_view_publisher.capture().nodes_by_id[NODE]
        missing = object()
        with pytest.raises(ConcurrentEditDetectedError) as caught:
            await client.update_node(NODE, NodeUpdateRequest(note="Agent"), basis={"note": "before"})
        assert caught.value.fields == ["note"] and caught.value.live == {"note": "GUI"}
        after = client._cache_view_publisher.capture().nodes_by_id[NODE]
        assert after["name"] == before["name"]
        assert after.get("nm", missing) == before.get("nm", missing)
    asyncio.run(run())


def test_absent_written_field_is_not_an_invented_change(engine):
    async def run():
        state = WireState()
        state.get_omit = {1: {"note"}, 2: {"note"}}
        client = engine(state)
        before = client._cache_view_publisher.capture().nodes_by_id[NODE]
        result = await client.update_node(NODE, NodeUpdateRequest(note="Agent"), basis={"note": "before"})
        assert "note" not in result.model_fields_set and len(state.posts) == 1
        after = client._cache_view_publisher.capture().nodes_by_id[NODE]
        assert after["note"] == before["note"]  # no observed value, never intent
        missing = object()
        assert after.get("no", missing) == before.get("no", missing)
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["success", "preflight", "postflight"])
def test_observation_advances_current_task_before_reply(engine, stage):
    """The conflict caller sees observed truth in its own pinned operation too."""
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache

    async def run():
        state = WireState()
        if stage != "success":
            setattr(state, "final_change" if stage == "preflight" else "after_change", {"note": "GUI"})
        client = engine(state)
        async with cache_operation_boundary(client, "n02-observation") as predecessor:
            old_other = predecessor.nodes_by_id[OTHER]
            if stage == "success":
                await client.update_node(NODE, NodeUpdateRequest(note="Agent"), basis={"note": "before"})
            else:
                with pytest.raises(ConcurrentEditDetectedError) as caught:
                    await client.update_node(NODE, NodeUpdateRequest(note="Agent"), basis={"note": "before"})
                assert caught.value.detected_at == stage
            current = current_operation_cache()
            expected = "Agent" if stage == "success" else "GUI"
            assert current.nodes_by_id[NODE]["note"] == expected
            assert client._cache_view_publisher.capture().nodes_by_id[NODE]["note"] == expected
            assert current.operation_id == predecessor.operation_id and current.owner is predecessor.owner
            assert predecessor.nodes_by_id[NODE]["note"] == "before"
            assert current.nodes_by_id[OTHER] is old_other
            assert state.gets == (1 if stage == "preflight" else 2)
            assert len(state.posts) == (0 if stage == "preflight" else 1)
        assert current_operation_cache(required=False) is None
    asyncio.run(run())


@pytest.mark.parametrize("fault", [429, "network", "transport"])
def test_final_get_fault_uses_only_inherited_retry(engine, fault):
    """A failed final GET sends nothing that attempt; no separate read loop."""
    async def run():
        state = WireState()
        original = state.request
        if fault == "transport":
            async def transport(request):
                if request.method == "GET" and state.gets == 0:
                    state.gets += 1
                    state.history.append("GET")
                    raise httpx.ConnectError("final GET", request=request)
                return await original(request)
            state.request = transport
        else:
            state.get_errors[1] = fault
        client = engine(state)
        await client.update_node(NODE, NodeUpdateRequest(note="Agent"), max_retries=2, basis={"note": "before"})
        assert state.history == ["acquire", "GET", "acquire", "GET", "recheck", "POST", "GET"]
        assert len(state.posts) == 1 and state.gets == 3 and len(client.test_sleeps) == 1
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["preflight", "postflight"])
def test_target_gone_keeps_ordinary_error_and_send_truth(engine, stage):
    # 404 already has this specialization; other ordinary 4xx use APIResponseError.
    from workflowy_mcp.models import NodeNotFoundError
    async def run():
        state = WireState()
        state.get_errors[1 if stage == "preflight" else 2] = 404
        client = engine(state)
        with pytest.raises(NodeNotFoundError):
            await client.update_node(NODE, NodeUpdateRequest(note="Agent"), basis={"note": "before"})
        assert state.gets == (1 if stage == "preflight" else 2)
        assert len(state.posts) == (0 if stage == "preflight" else 1)
        assert not client.test_sleeps
    asyncio.run(run())


def test_postflight_broken_publisher_keeps_typed_conflict_without_refresh_claim(engine, monkeypatch):
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    async def run():
        state = WireState()
        state.after_change = {"note": "GUI"}
        client = engine(state)
        predecessor = client._cache_view_publisher.capture()
        def broken(*args, **kwargs):
            raise RuntimeError("publisher unavailable")
        monkeypatch.setattr(client, "publish_local_cache_view", broken)
        async with cache_operation_boundary(client, "n02-broken-publisher") as pinned:
            with pytest.raises(ConcurrentEditDetectedError) as caught:
                await client.update_node(NODE, NodeUpdateRequest(note="Agent"), basis={"note": "before"})
            assert caught.value.detected_at == "postflight" and caught.value.live == {"note": "GUI"}
            assert current_operation_cache().nodes_by_id[NODE]["note"] == pinned.nodes_by_id[NODE]["note"] == "before"
        assert client._cache_view_publisher.capture() is predecessor
        assert predecessor.nodes_by_id[NODE]["note"] == "before"
        assert NODE in client._nodes_export_dirty_ids
        assert len(state.posts) == 1 and state.gets == 2 and not client.test_sleeps
        assert "refresh" not in str(caught.value).lower()
    asyncio.run(run())


@pytest.mark.parametrize("point", ["deny", "breaker", "cancel", "half_open_conflict", "half_open_cancel"])
def test_real_governor_final_get_fault_boundaries(tmp_path, point):
    """No short mutex spans GET; real denial/recheck and unused probe stay intact."""
    from test_carto_write_governor_phase_b import _real_governed_client
    from workflowy_mcp.models.errors import AccountWriteBlockedError, OperationCancelledError

    async def run():
        state = WireState()
        arrived = asyncio.Event()
        release = asyncio.Event()
        observed_breaker = []
        async def transport(request):
            assert not governor._schedule.lock.locked()
            if request.method == "GET" and state.gets == 0:
                observed_breaker.append(governor._load_breaker())
                arrived.set()
                await release.wait()
            return await state.request(request)
        client, governor, clock = _real_governed_client(tmp_path, transport)
        sleeps = []
        async def sleep(seconds, operation):
            sleeps.append((seconds, operation)); clock.advance(seconds)
        client._sleep_with_operation_cancel = sleep
        cancelled = False
        client._operation_cancel_callback = lambda: cancelled
        if point == "deny" or point.startswith("half_open"):
            await governor.trip_wfb(reason="test")
            if point.startswith("half_open"):
                clock.advance(11)
        probe_results = []
        original_record = governor.record_probe_result
        async def record(*args, **kwargs):
            probe_results.append(args)
            return await original_record(*args, **kwargs)
        governor.record_probe_result = record
        try:
            if point == "deny":
                with pytest.raises(AccountWriteBlockedError):
                    await client.update_node(NODE, NodeUpdateRequest(note="Agent"), basis={"note": "before"})
                assert state.gets == 0 and not arrived.is_set()
            else:
                task = asyncio.create_task(client.update_node(NODE, NodeUpdateRequest(note="Agent"), basis={"note": "before"}))
                await arrived.wait()
                if point == "breaker":
                    await governor.trip_wfb(reason="tripped during final GET")
                elif point == "half_open_conflict":
                    state.final_change = {"note": "GUI"}
                else:
                    cancelled = True
                release.set()
                expected = (AccountWriteBlockedError if point == "breaker" else
                            ConcurrentEditDetectedError if point == "half_open_conflict" else OperationCancelledError)
                with pytest.raises(expected):
                    await task
                assert state.gets == 1
                if point.startswith("half_open"):
                    assert observed_breaker[0]["state"] == "half_open"
                    assert observed_breaker[0]["probe_claim_id"]
                    assert governor._load_breaker() == observed_breaker[0]
            assert not state.posts and not sleeps and not probe_results
        finally:
            await client.close()
    asyncio.run(run())


def test_final_read_to_send_gap_is_honestly_last_writer_wins(engine):
    """An edit invisible to both observations is not claimed detected/prevented."""
    async def run():
        state = WireState()
        original = state.request
        async def transport(request):
            response = await original(request)
            if request.method == "GET" and state.gets == 1:
                # The returned response already contains B; GUI edits immediately
                # afterward, before the actual POST, outside the daemon's plane.
                state.node["note"] = "GUI in the final gap"
            return response
        state.request = transport
        client = engine(state)
        result = await client.update_node(NODE, NodeUpdateRequest(note="Agent"), basis={"note": "before"})
        assert result.note == state.node["note"] == "Agent"
        assert state.posts == [{"note": "Agent"}] and state.gets == 2
        assert client._cache_view_publisher.capture().nodes_by_id[NODE]["note"] == "Agent"
        assert not client.test_sleeps
    asyncio.run(run())


@pytest.mark.parametrize("field", ["name", "note"])
@pytest.mark.parametrize("stage", ["success", "preflight", "postflight"])
def test_null_observation_in_task_view_uses_no_barrier_dirt_or_export(engine, monkeypatch, field, stage):
    from workflowy_mcp import coherence
    from workflowy_mcp.client import api_client_etch
    from workflowy_mcp.coherence import barrier
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache

    async def run():
        state = WireState()
        setattr(state, "final_change" if stage == "preflight" else "after_change", {field: None})
        client = engine(state)
        forbidden_calls = []
        def forbidden(*args, **kwargs):
            forbidden_calls.append((args, kwargs))
            raise AssertionError("N02 must not call a barrier, export or dirty fallback on normal publication")
        for module in (barrier, coherence):
            for name in ("raise_global_dirty_barrier", "raise_global_dirty_barrier_async"):
                if hasattr(module, name):
                    monkeypatch.setattr(module, name, forbidden)
        monkeypatch.setattr(api_client_etch, "export_nodes_impl", forbidden)
        monkeypatch.setattr(api_client_etch, "fetch_nodes_export_uncached", forbidden)
        monkeypatch.setattr(client, "_mark_nodes_export_dirty", forbidden)
        async with cache_operation_boundary(client, "n02-null-observation") as pinned:
            request = NodeUpdateRequest(**{field: "" if stage == "success" else "Agent"})
            if stage == "success":
                # Empty NAME submissions are already invalid. Observe its clear
                # on a valid note-only success, without inventing a new writer.
                if field == "name":
                    request = NodeUpdateRequest(note="Agent")
                    basis = {"note": "before"}
                else:
                    basis = {field: state.node[field]}
                await client.update_node(NODE, request, basis=basis)
            else:
                with pytest.raises(ConcurrentEditDetectedError) as caught:
                    await client.update_node(NODE, request, basis={field: state.node[field]})
                assert caught.value.live == {field: None} and caught.value.detected_at == stage
            task_row = current_operation_cache().nodes_by_id[NODE]
            published_row = client._cache_view_publisher.capture().nodes_by_id[NODE]
            alias = "nm" if field == "name" else "no"
            assert task_row[field] == task_row[alias] == published_row[field] == published_row[alias] == ""
            assert pinned.nodes_by_id[NODE][field] == ("Old" if field == "name" else "before")
            assert current_operation_cache().nodes_by_id[OTHER] is pinned.nodes_by_id[OTHER]
            assert len(state.posts) == (0 if stage == "preflight" else 1)
            assert state.gets == (1 if stage == "preflight" else 2)
            assert not forbidden_calls and not client.test_sleeps
    asyncio.run(run())


@pytest.mark.parametrize("field", ["name", "note"])
@pytest.mark.parametrize("kind,alias", [("b", "strong"), ("i", "em"), ("s", "del")])
def test_admitted_formatting_aliases_preserve_content_and_literal_provenance(engine, field, kind, alias):
    """Explicit controls for existing lexical aliases; no new equivalence added."""
    async def run():
        original = f"<{kind}>old</{kind}>"
        semantic = f"<{kind}>Agent</{kind}>"
        state = WireState(**{field: original})
        state.final_change = {field: f"<{alias}>old</{alias}>"}
        state.after_change = {field: f"<{alias}>Agent</{alias}>"}
        client = engine(state)
        await client.update_node(NODE, NodeUpdateRequest(**{field: semantic}), basis={field: original})
        assert len(state.posts) == 1 and state.gets == 2
        project = core._canonical_update_value
        assert project(field, semantic) != project(field, f"<{alias}>GUI</{alias}>")
        assert project(field, semantic) != project(field, f"&lt;{alias}&gt;Agent&lt;/{alias}&gt;")
        different_kind = "i" if kind == "b" else "b"
        assert project(field, semantic) != project(field, f"<{different_kind}>Agent</{different_kind}>")
    asyncio.run(run())
