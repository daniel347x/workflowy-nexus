"""N03 wire execution only; Layer C owns selection, identity and fallback.

The v1 adapter retains create_node's legacy retry/publication lifecycle.
LLM acknowledgements contain counts, never created identities.
"""
from __future__ import annotations

import html
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx

from ..models import NodeCreateRequest, RateLimitError, WorkFlowyNode

LLM_DOC_EDIT_PATH = "/api/llm/doc/edit"


@dataclass
class SendStage:
    """One caller-owned dispatch fact, written only by the governed sender."""
    dispatch_started: bool = False


@dataclass(frozen=True)
class PlacementIntent:
    """Endpoint-neutral physical placement intent."""
    kind: Literal["top", "bottom", "after"]
    predecessor_uuid: str | None = None

    @classmethod
    def top(cls) -> PlacementIntent:
        return cls("top")

    @classmethod
    def bottom(cls) -> PlacementIntent:
        return cls("bottom")

    @classmethod
    def after(cls, predecessor_uuid: str) -> PlacementIntent:
        return cls("after", predecessor_uuid)


class LlmResultKind(str, Enum):
    ACKNOWLEDGED = "acknowledged"
    OPERATION_ERROR = "operation_error"
    READ_REQUIRED = "read_required"
    UNSUPPORTED_ENDPOINT = "unsupported_endpoint"
    SCHEMA_DRIFT = "schema_drift"
    HTTP_ERROR = "http_error"


@dataclass(frozen=True)
class LlmInsertResponse:
    """Transport evidence only; even ACKNOWLEDGED is not a commit verdict."""
    kind: LlmResultKind
    response: httpx.Response
    operation_error: str | None = None


def parse_llm_insert_response(response: httpx.Response, expected_count: int) -> LlmInsertResponse:
    """Read recorded result shapes, ignoring additive informational fields."""
    status = response.status_code
    if status in {401, 403, 404, 405, 410, 501} or 300 <= status < 400:
        # The governed sender has already intercepted a WFB(3) 403.
        return LlmInsertResponse(LlmResultKind.UNSUPPORTED_ENDPOINT, response)
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        data = None
    if status == 400 and isinstance(data, dict) and data.get("code") == "read_required":
        return LlmInsertResponse(LlmResultKind.READ_REQUIRED, response)
    if not 200 <= status < 300:
        return LlmInsertResponse(LlmResultKind.HTTP_ERROR, response)
    if isinstance(data, dict) and isinstance(data.get("results"), list) and len(data["results"]) == 1:
        row = data["results"][0]
        if isinstance(row, dict) and row.get("op") == "insert":
            if row.get("status") == "ok" and type(row.get("created")) is int and row["created"] == expected_count:
                return LlmInsertResponse(LlmResultKind.ACKNOWLEDGED, response)
            if row.get("status") == "error" and isinstance(row.get("error"), str) and row["error"]:
                return LlmInsertResponse(LlmResultKind.OPERATION_ERROR, response, row["error"])
    return LlmInsertResponse(LlmResultKind.SCHEMA_DRIFT, response)


@dataclass(frozen=True)
class TransportCreateResult:
    """One invocation's response, explicitly separate from verified identity."""
    transport: Literal["v1", "llm"]
    physical_placement: PlacementIntent
    reorder_required: bool = False
    returned_nodes: tuple[WorkFlowyNode, ...] = ()
    llm_response: LlmInsertResponse | None = None


class CreateTransport(Protocol):
    """One invocation; no selection, cache authority or outer retry loop."""
    async def create(
        self, request: NodeCreateRequest, placement: PlacementIntent, *,
        before_send: Callable[[], Awaitable[None]] | None = None,
        governance_coverage_hint: Any = None,
    ) -> TransportCreateResult: ...


# @beacon[
#   id=mci-n03@public-v1-create-transport,
#   slice_labels=mci-create-transport,
#   kind=ast,
#   comment=Delegates an owned request to legacy create_node without a trailing GET; after creates at bottom and reports order debt. No new retry or publication.,
# ]
class PublicV1CreateTransport:
    """Thin adapter; legacy v1 retries/cache maintenance intentionally unchanged."""
    def __init__(self, client: Any):
        self.client = client

    async def create(
        self, request: NodeCreateRequest, placement: PlacementIntent, *,
        before_send: Callable[[], Awaitable[None]] | None = None,
        governance_coverage_hint: Any = None,
    ) -> TransportCreateResult:
        """Create at top/bottom; after is explicit order debt, not an LLM guess.

        before_send is LLM-only: no hook is added to the unchanged legacy
        create_node lifecycle (CF-69 remains narrow).
        """
        physical = PlacementIntent.bottom() if placement.kind == "after" else placement
        owned = request.model_copy(deep=True, update={"position": physical.kind})
        node = await self.client.create_node(
            owned, _internal_call=True, fetch_created_node=False,
            _governance_coverage_hint=governance_coverage_hint,
        )
        return TransportCreateResult("v1", physical, placement.kind == "after", (node,))


# @beacon[
#   id=mci-n03@llm-insert-after-transport,
#   slice_labels=mci-create-transport,
#   kind=ast,
#   comment=One ordered insert through the sole governed sender and configured origin; one name decode and no note decode. Carries dispatch evidence and tags only the endpoint's own 429; acknowledgement never supplies identity.,
# ]
class LlmInsertAfterTransport:
    """One governed insert-after, without retries, sessions or cache mutation."""
    def __init__(self, client: Any):
        self.client = client

    async def create(
        self, request: NodeCreateRequest, placement: PlacementIntent, *,
        before_send: Callable[[], Awaitable[None]] | None = None,
        governance_coverage_hint: Any = None,
        send_stage: SendStage | None = None,
    ) -> TransportCreateResult:
        """Execute once, passing the attempt's owned cancellation-stage evidence."""
        response = await self.insert_after(
            request.parent_id, placement.predecessor_uuid, [request], before_send=before_send,
            send_stage=send_stage,
        )
        return TransportCreateResult("llm", placement, llm_response=response)

    async def insert_after(
        self, parent_id: str, predecessor_uuid: str, items: Sequence[NodeCreateRequest], *,
        before_send: Callable[[], Awaitable[None]] | None = None,
        send_stage: SendStage | None = None,
    ) -> LlmInsertResponse:
        """Serialize ordered items once; Layer C supplies eligible references.

        N04 owns multi-item acceptance. No count cap or recursive c payload.
        The holder remains visible to the caller even when cancellation propagates.
        """
        origin = urlsplit(str(self.client.base_url))
        url = urlunsplit((origin.scheme, origin.netloc, LLM_DOC_EDIT_PATH, "", ""))
        wire_items = []
        for request in items:
            item = {"n": html.unescape(self.client.encode_for_workflowy_api(request.name, "name", operation="create"))}
            if request.note:
                item["d"] = self.client.encode_for_workflowy_api(request.note, "note", operation="create")
            wire_items.append(item)
        payload = {
            "root": UUID(parent_id).hex[-12:],
            "operations": [{"op": "insert", "after": predecessor_uuid, "items": wire_items}],
        }
        response = await self.client._send_workflowy_write(
            "POST", url, operation="create", before_send=before_send,
            _send_stage=send_stage if send_stage is not None else SendStage(), json=payload,
        )
        if response.status_code == 429:
            # Keep the shared rate result, without adding the legacy v1 loop.
            try:
                await self.client._handle_response(response)
            except RateLimitError as error:
                # N04 A1: origin is established here, not inferred from a
                # generic lane error. Preserve the exact handler exception.
                error.llm_endpoint_429 = True
                error.llm_dispatch_started = bool(send_stage and send_stage.dispatch_started)
                raise
        return parse_llm_insert_response(response, len(items))
