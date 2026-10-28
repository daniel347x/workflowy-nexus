"""Regression locks for F12 fail-fast, cancellation, and duplicate admission."""

import asyncio

import httpx
import pytest

from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp import server
from workflowy_mcp.client import api_client_core
from workflowy_mcp.client.api_client_core import WorkFlowyClientCore
from workflowy_mcp.models import (
    APIConfiguration,
    APIResponseError,
    NodeCreateRequest,
    OperationCancelledError,
)


def test_handle_response_classifies_plain_403_as_non_retryable_api_error():
    async def run():
        client = WorkFlowyClientCore(APIConfiguration(api_key="test-dummy-key"))
        request = httpx.Request("POST", "https://workflowy.com/api/v1/nodes/")
        response = httpx.Response(403, text="forbidden by policy", request=request)

        with pytest.raises(APIResponseError) as exc_info:
            await client._handle_response(response)

        err = exc_info.value
        assert err.status_code == 403
        assert "forbidden by policy" in str(err)
        assert err.details["status_code"] == 403
        assert err.details["response_body"] == "forbidden by policy"

    asyncio.run(run())


def test_create_node_does_not_backoff_retry_permanent_403(monkeypatch):
    async def run():
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(403, text="permanent denial", request=request)

        monkeypatch.setattr(api_client_core, "API_RATE_LIMIT_DELAY", 0.0)
        client = WorkFlowyClientCore(APIConfiguration(api_key="test-dummy-key"))

        class PassGovernor:
            async def acquire_write_slot(self, *_args, **_kwargs):
                return type("Ticket", (), {"is_probe": False, "probe_claim_id": None})()

            async def recheck_before_send(self, *_args, **_kwargs):
                return None

        client._write_governor = PassGovernor()
        client._client = httpx.AsyncClient(
            base_url=client.base_url,
            transport=httpx.MockTransport(handler),
        )

        try:
            with pytest.raises(APIResponseError):
                await client.create_node(
                    NodeCreateRequest(name="x", parent_id="parent", position="bottom"),
                    _internal_call=True,
                    max_retries=10,
                    fetch_created_node=False,
                )
        finally:
            await client.close()

        assert calls == 1

    asyncio.run(run())


def test_retry_sleep_observes_task_local_user_cancellation_immediately():
    async def run():
        client = WorkFlowyClientCore(APIConfiguration(api_key="test-dummy-key"))
        with api_client_core.operation_cancel_scope(lambda: True):
            with pytest.raises(OperationCancelledError):
                await client._sleep_with_operation_cancel(512.0, "CARTO refresh retry")

    asyncio.run(run())


def test_start_carto_refresh_reuses_exact_active_claim_before_freshness(tmp_path):
    """Dedupe reads the actual registry reservation before freshness.

    The prior fixture manufactured a claim file. CUT-2B makes the in-process
    reservation the only ownership authority, so this witness admits a real
    job through the production decision and then asks the negative gate for
    its exact-intent answer.
    """
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    account = "a" * 32
    dedupe = gov.derive_dedupe_key(
        account_scope_id=account,
        job_type="CARTO_REFRESH",
        mode="folder",
        root_uuid="root-1",
    )
    footprint = gov.compute_footprint(
        tmp_path / "repo",
        "root-1",
        {"root-1": {"id": "root-1"}},
        kind="folder",
    )
    admitted = gov.admit_carto_job_locked(
        job_file=jobs / "active.json",
        job_payload={
            "id": "carto-refresh-folder-existing",
            "type": "CARTO_REFRESH",
            "mode": "folder",
            "logs_path": str(jobs / "active.log"),
        },
        job_type="CARTO_REFRESH",
        dedupe_key=dedupe,
        account_scope_id=account,
        footprint=footprint,
        launcher="system",
        parent_job_id=None,
        prepared=False,
        jobs_root=jobs,
        now_epoch=1700000000.0,
    )
    assert admitted["success"] is True
    assert admitted["claim_path"] is None
    assert admitted["claim"]["record_kind"] == "carto"
    assert admitted["claim"]["finalized"] is False

    result = asyncio.run(
        gov.negative_exact_intent_gate(
            dedupe_key=dedupe, jobs_root=jobs, now_epoch=1700000000.0,
        )
    )
    assert result["success"] is True
    assert result["deduplicated"] is True
    assert result["job_id"] == "carto-refresh-folder-existing"
    assert result["detached"] is False
    assert "no second preparation ran" in result["note"]
