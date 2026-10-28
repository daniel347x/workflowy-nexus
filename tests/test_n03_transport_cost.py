"""Controlled N03 verification cost, not a wall-clock acceptance threshold.

The injected clock advances every simulated latency, so the measured cost does
not depend on idle-machine speed.
"""
import asyncio
import json
import statistics
from collections import Counter

from test_n03_transition_create import Account, PARENT, PREDECESSOR, setup_client, transaction
from workflowy_mcp.client.create_transport import PlacementIntent
from workflowy_mcp.models import NodeCreateRequest


def test_n03_verified_insert_cost(tmp_path, capsys):
    async def sample(path, profile, live):
        account, holder = Account("absent" if profile == "fallback" else "ok"), {}
        async def server(request):
            delay = 0.35 if request.method == "GET" or "/api/llm/" in request.url.path else 0.25
            holder["clock"].advance(delay)
            return await account(request)
        client, governor, clock, sleeps = setup_client(path, account, forced=profile == "v1", dirty=live)
        holder["clock"] = clock
        await client._client.aclose()
        import httpx
        client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(server))
        acquire, tickets = governor.acquire_write_slot, []
        async def counted(*args, **kwargs):
            ticket = await acquire(*args, **kwargs);tickets.append(ticket);return ticket
        governor.acquire_write_slot = counted
        async with client._client, transaction(client) as tx:
            started = clock.value
            result = await tx.create(NodeCreateRequest(parent_id=PARENT, name="cost"), PlacementIntent.after(PREDECESSOR))
            elapsed = clock.value - started
            await tx.conclude()
        return dict(seconds=round(elapsed, 6), methods_paths=dict(Counter(f"{m} {p}" for m, p in account.calls)),
                    tickets=len(tickets), pre_listings=int(live), post_listings=account.list_count-int(live),
                    requested_sleeps=[seconds for seconds, _ in sleeps], reorder_required=result.reorder_required)
    async def run():
        output = {}
        for live in (False, True):
            for profile in ("v1", "llm", "fallback"):
                records = [await sample(tmp_path / f"{live}-{profile}-{i}", profile, live) for i in range(3)]
                assert all(r["tickets"] == (2 if profile == "fallback" else 1) for r in records)
                assert all(r["post_listings"] == (2 if profile == "fallback" else 1) for r in records)
                assert all(r["requested_sleeps"].count(1.0) >= r["pre_listings"] + r["post_listings"] for r in records)
                output[f"{'live' if live else 'warm'}-{profile}"] = dict(raw=records, median=statistics.median(r["seconds"] for r in records))
        with capsys.disabled():
            print("N03_COST=" + json.dumps(output, sort_keys=True), flush=True)
    asyncio.run(run())
