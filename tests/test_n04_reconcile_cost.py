"""N04 D11 injected-clock model: no live API, benchmark, or production switch.

Every modelled second comes from the injected clock, so the accounting is
independent of idle-machine speed.
"""
import json
import inspect

import httpx
import pytest

import test_n04_reconcile_insert_after as h


def _assert_accounting(record):
    writes = record["llm_insert_ops"] + record["v1_create_invocations"] + record["reorder_moves"] + record["structural_moves"]
    assert record["write_tickets"] == writes
    assert record["child_gets"] == record["pre_listings"] + record["post_listings"]
    assert record["list_sleep_seconds"] == pytest.approx(record["child_gets"])
    charged = (record["llm_insert_ops"] * .35 +
               (record["v1_create_invocations"] + record["reorder_moves"] + record["structural_moves"]) * .25 +
               record["child_gets"] * .35 + record["list_sleep_seconds"] + record["ticket_wait_seconds"])
    assert record["modelled_seconds"] == pytest.approx(charged)
    starts = [event["start"] for event in record["events"] if event["kind"] == "write"]
    assert all(right - left >= 1.0 - 1e-9 for left, right in zip(starts, starts[1:]))


def test_t12_d11_serial_clock_model(tmp_path, monkeypatch, _negative=False):
    """Both exact geometries, warm/cold healthy and forced-v1, one serial cell."""
    records = []
    original_build = h._build_client
    for geometry_name, factory, baseline_children, added, forced_moves in (
            ("app_js_scattered", h.app_js_scattered, 102, 9, 111),
            ("server_py_group", h.server_py_group, 259, 1, 260)):
        for cold in (False, True):
            for forced in (False, True):
                account = h.FakeAccount()
                events, tickets = [], []
                measurement = {}

                def build(path, fake):
                    values = original_build(path, fake)
                    client, governor, clock = values[:3]
                    measurement["start"] = clock.value
                    measurement["clock"] = clock
                    old_sleep = client._sleep_with_operation_cancel

                    async def sleep(seconds, operation):
                        start = clock.value
                        await old_sleep(seconds, operation)
                        events.append(dict(kind="sleep", operation=operation, seconds=seconds,
                                           start=start, end=clock.value))

                    client._sleep_with_operation_cancel = sleep
                    acquire = governor.acquire_write_slot

                    async def ticket(*args, **kwargs):
                        value = await acquire(*args, **kwargs)
                        tickets.append(dict(slot_at=value.slot_at, issued_at=value.issued_at, operation=value.operation))
                        return value

                    monkeypatch.setattr(governor, "acquire_write_slot", ticket)

                    async def http(request):
                        start = clock.value
                        response = await fake(request)
                        read = request.method == "GET"
                        llm = request.url.path.endswith(h.LLM_PATH)
                        duration = .35 if read or llm else .25
                        clock.advance(duration)
                        events.append(dict(kind="read" if read else "write", method=request.method,
                                           path=request.url.path, start=start, end=clock.value, duration=duration))
                        return response

                    client._client = httpx.AsyncClient(base_url=h.BASE_URL, transport=httpx.MockTransport(http))
                    return values

                with pytest.MonkeyPatch.context() as local:
                    local.setattr(h, "_build_client", build)
                    geometry = factory()
                    run = h.run_refresh(tmp_path / f"{geometry_name}-{cold}-{forced}", local,
                        geometry=geometry, account=account, v1_only=forced, prime_available=not cold,
                        dirty_before=(h.FILE,) if cold else ())
                run.assertion_prelude()
                assert account.contents(h.FILE) == [(name, "") for name in geometry.expected_names]
                counts = account.counts
                reads = counts["list"]
                record = dict(geometry=geometry_name, baseline="cold" if cold else "warm",
                    profile="forced_v1" if forced else "healthy", initial_children=baseline_children,
                    added_children=added, final_children=geometry.final_children,
                    llm_insert_ops=counts["llm_ops"], llm_items=counts["llm_items"],
                    v1_create_invocations=counts["v1_create"], reorder_moves=counts["move_top"],
                    structural_moves=counts["move_bottom"], write_tickets=len(tickets), child_gets=reads,
                    pre_listings=int(cold and not forced), post_listings=0 if forced else added,
                    modelled_seconds=measurement["clock"].value - measurement["start"],
                    list_sleep_seconds=sum(event["seconds"] for event in events if event["kind"] == "sleep" and event["operation"] == "Workflowy API operation"),
                    ticket_wait_seconds=sum(event["seconds"] for event in events if event["kind"] == "sleep" and event["operation"] != "Workflowy API operation"),
                    latency_model_version="N04-D11-v1: v1=.25 llm=.35 get=.35 list_sleep=1 write_start_spacing=1",
                    first_ticket_convention="Immediate first slot; no prior write reservation",
                    common_snapshot_setup_seconds=0, terminal_full_refresh="excluded; unchanged",
                    final_ordered_ids=account.order(h.FILE), final_ordered_content=account.contents(h.FILE),
                    tickets=tickets, events=events)
                assert record["reorder_moves"] == (forced_moves if forced else 0)
                assert record["llm_insert_ops"] == (0 if forced else added)
                assert record["v1_create_invocations"] == (added if forced else 0)
                _assert_accounting(record)
                # Source-copy accounting negatives: moves and inherited sleeps
                # cannot disappear from the measurement and leave it GREEN.
                for field in ("list_sleep_seconds", "reorder_moves"):
                    if record[field]:
                        with pytest.raises(AssertionError):
                            _assert_accounting({**record, field: 0})
                records.append(record)
    output = tmp_path / "d11-model.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(records, indent=2) + "\n")
    if not _negative:
        source = inspect.getsource(test_t12_d11_serial_clock_model).split("    if not _negative:", 1)[0]
        for old, new in (
                ('reorder_moves=counts["move_top"]', 'reorder_moves=0'),
                ('list_sleep_seconds=sum(event["seconds"]', 'list_sleep_seconds=0 * sum(event["seconds"]')):
            assert source.count(old) == 1
            namespace = dict(globals())
            exec(compile(source.replace(old, new, 1), "<n04-d11-source-copy>", "exec"), namespace)
            with pytest.MonkeyPatch.context() as local:
                with pytest.raises(AssertionError):
                    namespace["test_t12_d11_serial_clock_model"](tmp_path / "negative", local, True)
