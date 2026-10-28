"""N02 family interleavings through real client/sender, regions and dispatch.

The N02 controlled-cost witness counts GETs and advances the governor's fake
clock, so it does not depend on idle-machine speed.
"""

import asyncio
import json

import httpx
import pytest

from test_concurrent_edit_detected import PassGovernor, isolated_neutralization
from test_mutation_regions_n05_fences import real_region_probe
from test_mutation_serialization import dispatch, report_header
from workflowy_mcp import server
from workflowy_mcp.mutation_conflicts import ConcurrentEditDetectedError


CASES = [
    "edit_name", "edit_whole_seams", "edit_whole_seams_full", "edit_whole_full", "edit_whole_none",
    "edit_file", "edit_replace", "edit_edits", "edit_all", "edit_combined", "edit_empty",
    "update_emoji", "prepend_emoji", "append_emoji",
    "append_note_inline", "prepend_note_inline", "append_note_file", "prepend_note_file",
    "tag_add", "tag_remove", "tag_rename", "batch_add", "batch_remove", "batch_rename",
    "dock_topic", "dock_description", "dock_local", "dock_combined",
    "assign_id", "remove_id", "touch", "extract", "promote",
]


def _invocation(case, ids, text_path):
    target = ids["source"]
    family, verb, fields = "node", "edit", ["note"]
    params = {"node_handle": target}
    if case.startswith("edit_"):
        if case == "edit_name":
            params.update(name="New title"); fields = ["name"]
        elif case.startswith("edit_whole_"):
            params.update(note="replacement", diff=case.removeprefix("edit_whole_"))
        elif case == "edit_file":
            params.update(note_file=str(text_path))
        elif case == "edit_replace":
            params.update(note_search="two", note_replace="TWO")
        elif case == "edit_edits":
            params.update(note_edits=[{"search": "one", "replace": "ONE"}, {"search": "three", "replace": "THREE"}])
        elif case == "edit_all":
            params.update(note_search="o", note_replace="O", replace_all=True)
        elif case == "edit_combined":
            params.update(name="New title", note="replacement"); fields = ["name", "note"]
        else:
            params.update(note="")
    elif case.endswith("emoji"):
        verb = case; fields = ["name"]; params.update(emoji="✅")
    elif "_note_" in case:
        verb = "append_note" if case.startswith("append") else "prepend_note"
        params.update({"text_file": str(text_path)} if case.endswith("file") else {"text": "tail"})
        params["separator"] = "\n---\n"
    elif case.startswith(("tag_", "batch_")):
        family = "tag"; operation = case.split("_")[1]; fields = ["name"]
        def operation_params(handle):
            if operation == "rename":
                return {"handle": handle, "operation": operation, "old_tag": "old", "new_tag": "new"}
            return {"handle": handle, "operation": operation, "tags": ["new" if operation == "add" else "old"]}
        if case.startswith("batch"):
            verb = "batch"; params = {"operations": [operation_params(target), operation_params(ids["plain"])]}
        else:
            verb = operation; item = operation_params(target)
            params = {"node_handle": target, **{key: value for key, value in item.items() if key not in ("handle", "operation")}}
    else:
        family = "anchor"
        if case.startswith("dock_"):
            target = ids["dest"]; verb = "dock.update"; params = {"dock_handle": target}
            if case in ("dock_topic", "dock_combined"):
                params["topic"] = "New dock"; fields = ["name"]
            if case in ("dock_description", "dock_combined"):
                params["description"] = "New description"
                fields = ["name", "note"] if case == "dock_combined" else ["note"]
            if case == "dock_local":
                params["local"] = True; fields = ["name"]
        elif case == "assign_id":
            target = ids["plain"]; verb = "point.assign_id"; fields = ["name"]
            params = {"node_handle": target, "ap_id": "NEW701"}
        elif case == "remove_id":
            target = ids["ap"]; verb = "point.remove_id"; fields = ["name"]
            params = {"node_handle": target}
        elif case == "touch":
            verb = "point.touch"; fields = ["name"]
            params = {"handles": [ids["plain"], target]}
        elif case == "extract":
            verb = "point.extract"
            params = {"node_handle": target, "parent_handle": ids["dest"],
                      "start_line": 2, "end_line": 2, "name": "Extracted", "ap_id": "NEW701"}
        elif case == "promote":
            verb = "point.promote"
            params = {"handles": [target, ids["plain"]], "destination": ids["dest"], "tags": ["new"]}
    return family, verb, params, target, fields


def _configure(tmp_path, monkeypatch):
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    history = []
    client._write_governor = PassGovernor(client._write_governor, history)
    async def no_sleep(*args):
        pass
    monkeypatch.setattr(client, "_sleep_with_operation_cancel", no_sleep)
    return client, probe, ids, history


def _ledger_error(case, result, stage):
    """Keep the existing per-item versus top-level boundaries visible to tests."""
    code = ConcurrentEditDetectedError.code
    if case.startswith("batch"):
        rows = result.get("detail", result)["results"]
        assert rows[0]["status"] == "error" and rows[0]["error_kind"] == rows[0]["error_code"] == code
        assert rows[1]["status"] == "committed"
        return rows[0]["extra"], rows
    if case in ("touch", "promote") and stage == "preflight":
        ledger = result.get("detail", result)
        rows = ledger["results"]
        failed = next(row for row in rows if row["status"] == "failed")
        assert failed["reason"] == failed["error_code"] == code
        assert not ledger["summary"]["stopped"]
        assert any(row["status"] in ("touched", "promoted") for row in rows)
        return failed["extra"], rows
    assert result["success"] is False and result["error"]["code"] == code, result
    extra = result["error"]["extra"]
    if case in ("touch", "promote"):
        ledger = extra[case]
        assert ledger["summary"]["stopped"]
        return extra, ledger["results"]
    return extra, None


def _isolate_touch_written_fingerprint(monkeypatch):
    """Isolate N02 from the OLD name-preservation detector in both test arms.

    Otherwise neutralizing postflight still trips content_changed, so the fake
    cannot quietly succeed. Unwritten/structural fingerprint checks stay real;
    the separate normalization witness below runs the whole real fingerprint.
    """
    original = server._touch_content_fingerprint
    def fingerprint(node):
        fields = original(node)
        fields["name"] = "written-name checked by N02 under test"
        return fields
    monkeypatch.setattr(server, "_touch_content_fingerprint", fingerprint)


def _gui_name_with_same_protected_tokens(raw):
    """Change title text while retaining tag/AP/leading-emoji identity.

    In particular a tag-set-only or emoji-only comparison must miss this GUI
    edit, whereas comparing the whole written name detects it.
    """
    if raw.startswith("[ap:"):
        end = raw.index("]") + 1
        return raw[:end] + " GUI" + raw[end:]
    if raw.startswith("✅"):
        return "✅ GUI" + raw[1:]
    return "GUI " + raw


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("stage", ["preflight", "postflight", "success"])
def test_family_interleaving(tmp_path, monkeypatch, isolated_neutralization, case, stage):
    async def run():
        client, probe, ids, history = _configure(tmp_path, monkeypatch)
        text_path = tmp_path / "text.txt"
        text_path.write_bytes(b"replacement\r\nfile tail\r\n")
        family, verb, params, target, fields = _invocation(case, ids, text_path)
        original = dict(probe.nodes[target])
        if case == "touch":
            _isolate_touch_written_fingerprint(monkeypatch)
        old_view = client._cache_view_publisher.capture()
        unrelated = old_view.nodes_by_id[ids["root"]]
        gui = {field: _gui_name_with_same_protected_tokens(original[field]) if field == "name" else "GUI note" for field in fields}
        # Promote requests both name/tags and note/trail; modify only note.
        if case == "promote":
            gui = {"note": "GUI note"}
        fired = False
        content_posts = []
        tie_sleeps = []
        async def tie_sleep(seconds):
            tie_sleeps.append(seconds)
        monkeypatch.setattr(server, "_anchor_touch_sleep", tie_sleep)
        acquire = client._write_governor.acquire_write_slot
        async def ticket(operation, **kwargs):
            nonlocal fired
            value = await acquire(operation, **kwargs)
            if operation == "update_node" and stage == "preflight" and not fired:
                probe.nodes[target].update(gui); fired = True
            return value
        client._write_governor.acquire_write_slot = ticket
        async def transport(request):
            nonlocal fired
            response = await probe.request(request)
            if request.method == "POST" and request.url.path.endswith("/" + target):
                content_posts.append(json.loads(request.content))
                if stage == "postflight" and not fired:
                    if "name" in gui:
                        gui["name"] = _gui_name_with_same_protected_tokens(probe.nodes[target]["name"])
                    probe.nodes[target].update(gui); fired = True
            # Extract's GUI actor changes the source WHILE a capture is in
            # flight; its original source basis must survive that whole window.
            if case == "extract" and stage == "preflight" and request.method == "POST" and request.url.path.endswith("/nodes"):
                probe.nodes[target].update(gui); fired = True
            return response
        await client._client.aclose()
        client._client = httpx.AsyncClient(base_url="https://workflowy.com/api/v1", transport=httpx.MockTransport(transport))
        registry = {"node": server.build_node_registry, "tag": server.build_tag_registry,
                    "anchor": server.build_anchor_registry}[family]()
        try:
            result = await dispatch(registry, verb, params)
            assert not probe.violations, probe.violations
            assert not tie_sleeps, "a conflict or successful first touch reached the tie retry"
            if stage == "success":
                assert not (isinstance(result, dict) and result.get("error")), result
                assert len(content_posts) == 1
                if case.endswith("emoji"):
                    assert "note" not in content_posts[0] and "✅" in content_posts[0]["name"]
                if case.startswith("edit_") and fields == ["note"]:
                    metadata = report_header(result)
                    assert metadata["success"] is True and metadata["no_op"] is False
                    assert metadata["note_changed"] is True
                if case == "edit_empty":
                    assert content_posts[0]["note"] == ""
                return
            extra, rows = _ledger_error(case, result, stage)
            assert extra["node_id"] == target and extra["detected_at"] == stage
            assert extra["fields"] == list(gui) and extra["live"] == gui
            assert extra["retry_after"] == 0.0
            expected = {field: original[field] for field in gui} if stage == "preflight" else {field: content_posts[0][field] for field in gui}
            assert extra["expected"] == expected
            assert len(content_posts) == (0 if stage == "preflight" else 1)
            current = client._cache_view_publisher.capture()
            assert all(current.nodes_by_id[target][field] == value for field, value in gui.items())
            assert current.nodes_by_id[ids["root"]] is unrelated
            assert old_view.nodes_by_id[target]["name"] == original["name"]
            if case == "touch":
                failed = next(row for row in rows if row["status"] == "failed")
                assert failed["writes"] == (0 if stage == "preflight" else 1)
            if case == "extract":
                ledger = extra["extraction"]
                assert len(probe.created) == 1
                assert ledger["extracted"]["status"] == "created_verified"
                assert "re-read" in ledger["partial_failure"]["recovery"]
                assert "line numbers" in ledger["partial_failure"]["recovery"]
            if case == "promote":
                failed = next(row for row in rows if row["status"] == "failed")
                assert failed["moved"] is True and failed["trail_written"] is False
                assert not failed.get("tags_applied")
                assert probe.nodes[target]["parent_id"] == ids["dest"]
                assert failed["writes"] == (1 if stage == "preflight" else 2)
            if case in ("edit_combined", "dock_combined"):
                reads = [path for path, _ in probe.reads if path.endswith(target)]
                assert len(reads) == (2 if stage == "preflight" else 3)
        finally:
            await client.close()
    asyncio.run(run())


def test_n02_one_direct_get_increment(tmp_path, capsys):
    """Controlled transport cost, not a live probe or a wall-clock product bound."""
    import statistics
    import time
    from test_carto_write_governor_phase_b import _real_governed_client
    from test_concurrent_edit_detected import NODE, OTHER, WireState
    from workflowy_mcp.models import NodeUpdateRequest

    async def run():
        samples = []
        for pair in range(3):
            measurements = []
            for checked in (False, True):
                state = WireState()
                async def transport(request):
                    assert not governor._schedule.lock.locked()
                    if request.method == "GET":
                        await asyncio.sleep(0.35)
                    return await state.request(request)
                client, governor, clock = _real_governed_client(tmp_path / f"pair-{pair}-{checked}", transport)
                reservations = []
                acquire = governor.acquire_write_slot
                async def reserve(*args, **kwargs):
                    ticket = await acquire(*args, **kwargs)
                    reservations.append(ticket.slot_at)
                    return ticket
                governor.acquire_write_slot = reserve
                sleeps = []
                async def pacing(seconds, operation):
                    sleeps.append((seconds, operation)); clock.advance(seconds)
                client._sleep_with_operation_cancel = pacing
                basis = {key: state.node[key] for key in ("name", "note")}  # outside the differential
                try:
                    started = time.perf_counter()
                    await client.update_node(NODE, NodeUpdateRequest(name="Agent", note="Agent note"),
                                             basis=basis if checked else None)
                    elapsed = time.perf_counter() - started
                    assert state.gets == (2 if checked else 1) and len(state.posts) == 1
                    assert not sleeps and reservations == [1000.0]
                    assert governor._schedule.next_slot_at == 1001.0
                    measurements.append({"elapsed_s": elapsed, "gets": state.gets, "posts": len(state.posts),
                                         "pacing_sleeps": sleeps, "reservations": reservations,
                                         "reservation_cursor": governor._schedule.next_slot_at})
                finally:
                    await client.close()
            samples.append({"without_basis": measurements[0], "with_basis": measurements[1],
                            "increment_s": measurements[1]["elapsed_s"] - measurements[0]["elapsed_s"]})

        # Block the first checked writer inside its final GET; an independent,
        # unhooked next ticket remains grantable without that GET's completion.
        state = WireState()
        entered, release = asyncio.Event(), asyncio.Event()
        control_sends = []
        async def disjoint_transport(request):
            assert not governor._schedule.lock.locked()
            if request.url.path.endswith(OTHER):
                control_sends.append(request.method)
                return httpx.Response(200, json={"status": "ok"})
            if request.method == "GET":
                if state.gets == 0:
                    entered.set(); await release.wait()
                await asyncio.sleep(0.35)
            return await state.request(request)
        client, governor, clock = _real_governed_client(tmp_path / "disjoint", disjoint_transport)
        waits = []
        async def scheduled_sleep(seconds, operation):
            waits.append((seconds, operation)); clock.advance(seconds)
        client._sleep_with_operation_cancel = scheduled_sleep
        try:
            task = asyncio.create_task(client.update_node(NODE, NodeUpdateRequest(name="Agent", note="Agent note"),
                                                         basis={"name": "Old", "note": "before"}))
            await entered.wait()
            assert governor._schedule.next_slot_at == 1001.0
            await client._send_workflowy_write("POST", f"/nodes/{OTHER}", operation="disjoint-control", json={})
            assert control_sends == ["POST"] and not task.done() and not state.posts
            assert waits == [(1.0, "disjoint-control")]
            assert governor._schedule.next_slot_at == 1002.0
            release.set(); await task
            assert governor._schedule.next_slot_at == 1002.0
            assert state.gets == 2 and len(state.posts) == 1
        finally:
            await client.close()
        report = {"kind": "controlled transport-cost measurement", "get_delay_s": 0.35, "pairs": samples,
                  "median_increment_s": statistics.median(row["increment_s"] for row in samples),
                  "P0_empirical_GET_listing_s": [0.3, 0.44],
                  "disjoint_control": {"cursor_after_first_grant": 1001.0, "cursor_after_next_grant": 1002.0,
                                       "cursor_after_first_completion": 1002.0, "completed_while_GET_blocked": True},
                  "note": "Ordinary scheduling noise; no wall-clock ceiling. Tag shape: basis + final + postflight = three GETs vs two."}
        with capsys.disabled():
            print("N02_CONTROLLED_COST=" + json.dumps(report, sort_keys=True))
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["preflight", "postflight"])
def test_touch_tie_collision_stops_with_truthful_writes(tmp_path, monkeypatch, isolated_neutralization, stage):
    async def run():
        client, probe, ids, _ = _configure(tmp_path, monkeypatch)
        _isolate_touch_written_fingerprint(monkeypatch)
        target = ids["source"]
        sent = 0
        async def transport(request):
            nonlocal sent
            response = await probe.request(request)
            if request.method == "POST" and request.url.path.endswith(target):
                sent += 1
                if sent == 1:
                    probe.nodes[target]["modifiedAt"] = 100  # existing tie path
                elif stage == "postflight":
                    probe.nodes[target]["name"] = "GUI tie"
            return response
        sleeps = []
        async def tie_sleep(seconds):
            sleeps.append(seconds)
            if stage == "preflight":
                probe.nodes[target]["name"] = "GUI tie"
        monkeypatch.setattr(server, "_anchor_touch_sleep", tie_sleep)
        await client._client.aclose()
        client._client = httpx.AsyncClient(base_url="https://workflowy.com/api/v1", transport=httpx.MockTransport(transport))
        try:
            result = await dispatch(server.build_anchor_registry(), "point.touch", {"handles": [target]})
            assert result["error"]["code"] == ConcurrentEditDetectedError.code
            ledger = result["error"]["extra"]["touch"]
            assert ledger["summary"]["stopped"] and len(sleeps) == 1
            assert ledger["results"][0]["writes"] == sent == (1 if stage == "preflight" else 2)
            assert client._cache_view_publisher.capture().nodes_by_id[target]["name"] == "GUI tie"
        finally:
            await client.close()
    asyncio.run(run())


def test_touch_normalization_does_not_defeat_preservation(tmp_path, monkeypatch):
    async def run():
        client, probe, ids, _ = _configure(tmp_path, monkeypatch)
        target = ids["source"]
        probe.nodes[target].update(name="✈️ Source  title #<u>old</u> 🔱", note="https://example.test/?a=1&b=2")
        async def transport(request):
            response = await probe.request(request)
            if request.method == "POST" and request.url.path.endswith(target):
                probe.nodes[target]["note"] = '<a href="https://example.test/?a=1&amp;b=2">https://example.test/?a=1&amp;b=2</a>'
            return response
        await client._client.aclose()
        client._client = httpx.AsyncClient(base_url="https://workflowy.com/api/v1", transport=httpx.MockTransport(transport))
        try:
            result = await dispatch(server.build_anchor_registry(), "point.touch", {"handles": [target]})
            assert result["success"] is True, result
            assert result.get("detail", result)["results"][0]["status"] == "touched"
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["layout", "touch_layout", "tag_noop", "remove_id_noop", "search_noop"])
def test_no_text_send_has_no_n02_read(tmp_path, monkeypatch, case):
    async def run():
        client, probe, ids, history = _configure(tmp_path, monkeypatch)
        target = ids["plain"]
        if case == "layout":
            family, verb, params, reads, writes = "node", "edit", {"node_handle": target, "layout_mode": "h1"}, 1, 1
        elif case == "touch_layout":
            probe.nodes[target]["data"] = {"layoutMode": "todo"}
            family, verb, params, reads, writes = "anchor", "point.touch", {"handles": [target]}, 2, 1
        elif case == "tag_noop":
            family, verb, params, reads, writes = "tag", "add", {"node_handle": target, "tags": ["old"]}, 1, 0
        elif case == "remove_id_noop":
            family, verb, params, reads, writes = "anchor", "point.remove_id", {"node_handle": target}, 1, 0
        else:
            family, verb, params, reads, writes = "node", "edit", {"node_handle": target, "note_search": "absent", "note_replace": "x"}, 1, 0
        registry = {"node": server.build_node_registry, "tag": server.build_tag_registry, "anchor": server.build_anchor_registry}[family]()
        try:
            result = await dispatch(registry, verb, params)
            assert not (isinstance(result, dict) and result.get("error")), result
            assert len(probe.sends) == writes
            assert len([path for path, _ in probe.reads if path.endswith(target)]) == reads
            if writes == 0:
                assert history == []
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["preflight", "postflight", "success"])
def test_ws_dock_collision_keeps_typed_code_and_correlation(tmp_path, monkeypatch, isolated_neutralization, stage):
    from test_anchor_dock_update_wire import _FakeWebSocket
    async def run():
        client, probe, ids, _ = _configure(tmp_path, monkeypatch)
        target = ids["dest"]
        posts = []
        acquire = client._write_governor.acquire_write_slot
        async def ticket(operation, **kwargs):
            value = await acquire(operation, **kwargs)
            if operation == "update_node" and stage == "preflight":
                probe.nodes[target]["name"] = "GUI " + probe.nodes[target]["name"]
            return value
        client._write_governor.acquire_write_slot = ticket
        async def transport(request):
            response = await probe.request(request)
            if request.method == "POST" and request.url.path.endswith(target):
                posts.append(json.loads(request.content))
                if stage == "postflight":
                    probe.nodes[target]["name"] = "GUI " + probe.nodes[target]["name"]
            return response
        await client._client.aclose()
        client._client = httpx.AsyncClient(base_url="https://workflowy.com/api/v1", transport=httpx.MockTransport(transport))
        ws = _FakeWebSocket()
        try:
            await server._handle_anchor_dock_update(
                {"action": "anchor_dock_update", "request_id": "n02-wire", "v": 1,
                 "dock_handle": target, "favorite": True, "families": "wfy-ns"}, ws,
            )
            assert len(ws.sent) == 1
            reply = ws.sent[0]
            assert reply["request_id"] == "n02-wire" and reply["action"] == "anchor_dock_update_result"
            if stage == "success":
                assert reply["success"] is True and len(posts) == 1
            else:
                assert reply["success"] is False and reply["error_code"] == ConcurrentEditDetectedError.code
                assert reply["extra"]["node_id"] == target and reply["extra"]["detected_at"] == stage
                assert reply["extra"]["fields"] == ["name"]
                assert reply["extra"]["live"]["name"].startswith("GUI ")
                assert len(posts) == (0 if stage == "preflight" else 1)
                assert client._cache_view_publisher.capture().nodes_by_id[target]["name"].startswith("GUI ")
        finally:
            await client.close()
    asyncio.run(run())
