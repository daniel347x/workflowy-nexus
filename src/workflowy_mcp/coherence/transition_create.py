"""Held-parent singleton and k-run verification, not N08's durable lifecycle.

The Cartographer caller retains its real N05 grant until
conclusion/unwind, including joined cancellation verification. Legacy v1 retries
remain inherited; this layer never repeats an LLM insert or an ambiguous create.
"""
from __future__ import annotations

import asyncio
import copy
import html
import logging
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError as PydanticValidationError

from ..client.create_transport import (
    LlmInsertAfterTransport, LlmResultKind, PlacementIntent,
    PublicV1CreateTransport, SendStage,
)
from ..models import (
    APIResponseError, AuthenticationError, NetworkError, NodeCreateRequest,
    NodeListRequest, OperationCancelledError, RateLimitError, WorkFlowyNode,
)
from ..models.errors import TimeoutError as WorkflowyTimeoutError
from ..mutation_locks import MutationRegionGrant, MUTATION_LOCK_STATE, _regions_cover_request
from .admission import require_coherence_mutation_admission
from .barrier import raise_global_dirty_barrier_async
from .canonical_node import IngestionProfile, _decode_text
from .errors import CoherenceBarrierActiveError
from .operation_cache import current_operation_cache
from .paths import coherence_lock_async
from .schema import read_external_dirt, write_external_dirt

LOGGER = logging.getLogger(__name__)


class CreateClassification(str, Enum):
    COMMITTED = "COMMITTED"
    CONCLUSIVELY_ABSENT = "CONCLUSIVELY_ABSENT"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class ParentOrderWitness:
    parent_uuid: str | None
    ordered_child_ids: tuple[str, ...]
    priority_by_id: Mapping[str, int | float]
    rows: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class AttemptClassification:
    classification: CreateClassification
    witness: ParentOrderWitness | None = None
    created_ids: tuple[str, ...] = ()
    external_ids: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class TransitionCreateResult:
    classification: CreateClassification
    transport: str
    created_nodes: tuple[WorkFlowyNode, ...]
    order_witness: ParentOrderWitness
    reorder_required: bool

    @property
    def created_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.created_nodes)


def _order(parent_id, rows) -> ParentOrderWitness:
    """Validate only membership/order evidence here; addition content below."""
    owned, ids, priorities = [], set(), set()
    for row in rows:
        if not isinstance(row, Mapping) or "id" not in row or "priority" not in row:
            raise ValueError("missing membership/order evidence")
        identifier = str(UUID(row["id"]))
        priority = row["priority"]
        if type(priority) not in (int, float) or not math.isfinite(priority):
            raise ValueError("missing numeric priority")
        if identifier in ids or priority in priorities:
            raise ValueError("duplicate id or unresolved priority order")
        ids.add(identifier)
        priorities.add(priority)
        item = copy.deepcopy(dict(row))
        item["id"] = identifier
        owned.append(item)
    owned.sort(key=lambda row: row["priority"])
    return ParentOrderWitness(parent_id, tuple(row["id"] for row in owned),
                              {row["id"]: row["priority"] for row in owned}, tuple(owned))


def _content(name, note):
    profile = IngestionProfile.CHILD_LISTING_MERGE
    return (_decode_text(name, profile, field="name"),
            _decode_text(note, profile, field="note") or "")


# @beacon[
#   id=mci-n03@classify-create-attempt,
#   slice_labels=mci-create-transport,
#   kind=ast,
#   comment=Singleton compatibility delegate to the shared k-item fingerprint and adjacency classifier; omitted note alone means null.,
# ]
def classify_create_attempt(
    parent_id: str | None, baseline: ParentOrderWitness,
    evidence: Mapping[str, Any], expected_name: str, expected_note: str | None,
    physical_placement: PlacementIntent, *, returned_id: str | None = None,
) -> AttemptClassification:
    """The singleton name uses exactly the k-item identity rule."""
    return classify_create_run(parent_id, baseline, evidence, ((expected_name, expected_note),),
                               physical_placement, returned_id=returned_id)


# @beacon[
#   id=mci-n04@classify-create-run,
#   slice_labels=mci-create-transport,
#   kind=ast,
#   comment=Counts every complete fingerprint before request-order and adjacency checks; nonmatching GUI additions may intervene but partial or duplicate matches never permit fallback.,
# ]
def classify_create_run(parent_id, baseline, evidence, expected_items, physical_placement, *, returned_id=None):
    """Classify the whole uniquely fingerprinted run from authoritative evidence."""
    ambiguous = CreateClassification.AMBIGUOUS
    try:
        if not evidence.get("recognized") or baseline.parent_uuid != parent_id:
            raise ValueError("unrecognized listing envelope or foreign baseline")
        post = _order(parent_id, evidence["rows"])
        old_ids = set(baseline.ordered_child_ids)
        additions = [row for row in post.rows if row["id"] not in old_ids]
        expected = tuple(_content(name, note) for name, note in expected_items)
        matching = [[] for _ in expected]
        for row in additions:
            # A2: absence of note alone is the declared null/empty equivalence.
            if "name" not in row or not isinstance(row["name"], str):
                raise ValueError("missing addition name evidence")
            note = row.get("note")
            if note is not None and not isinstance(note, str):
                raise ValueError("invalid addition note evidence")
            observed_parent = row.get("parent_id", row.get("parentId", parent_id))
            if observed_parent != parent_id:
                raise ValueError("foreign listing parent")
            observed = _content(row["name"], note)
            for index, fingerprint in enumerate(expected):
                if observed == fingerprint:
                    matching[index].append(row["id"])
        if not additions:
            return AttemptClassification(CreateClassification.CONCLUSIVELY_ABSENT, post)
        if not matching or any(len(matches) != 1 for matches in matching):
            return AttemptClassification(ambiguous, post, detail="nonunique_or_unexpected_additions")
        created_ids = tuple(matches[0] for matches in matching)
        if len(set(created_ids)) != len(created_ids):
            return AttemptClassification(ambiguous, post, detail="repeated_requested_fingerprint")
        created = created_ids[0]
        order = post.ordered_child_ids
        projected = tuple(identifier for identifier in order if identifier in old_ids or identifier in created_ids)
        first = projected.index(created)
        if projected[first:first + len(created_ids)] != created_ids:
            return AttemptClassification(ambiguous, post, detail="run_order_or_adjacency_conflict")
        index = order.index(created)
        if physical_placement.kind == "after":
            predecessor = physical_placement.predecessor_uuid
            placed = (predecessor in order and order.index(predecessor) < index
                      and not any(i in old_ids for i in order[order.index(predecessor) + 1:index]))
        elif physical_placement.kind == "top":
            placed = not any(i in old_ids for i in order[:index])
        else:
            placed = not any(i in old_ids for i in order[order.index(created_ids[-1]) + 1:])
        if not placed or (returned_id is not None and (len(created_ids) != 1 or returned_id != created)):
            return AttemptClassification(ambiguous, post, detail="placement_or_returned_id_conflict")
        external = tuple(row["id"] for row in additions if row["id"] not in created_ids)
        return AttemptClassification(CreateClassification.COMMITTED, post, created_ids, external)
    except (KeyError, ValueError, TypeError, AttributeError):
        return AttemptClassification(ambiguous, detail="unreliable_listing_evidence")


async def record_external_additions(paths, parent_id) -> None:
    """Advance GUI dirt under the existing mutex; never clear it here."""
    async with coherence_lock_async(paths):
        previous = read_external_dirt(paths, required=False)
        updated = replace(previous, active=True, generation=previous.generation + 1,
                          observed_at=datetime.now(timezone.utc).isoformat(),
                          mutation_kind="structural", parent_uuid=parent_id,
                          detail="N03 nonmatching sibling addition")
        write_external_dirt(paths, updated)


async def _request_recovery() -> None:
    """Lazy bridge: schedule the one existing coalescer, never await export."""
    from ..server import _request_nodes_export_cache_refresh_async
    await _request_nodes_export_cache_refresh_async(
        reason="ambiguous_commit_outcome", source="mci_n03", refresh_kind="recovery",
        toggle_eligible=False, require_new_fetch=True,
    )


# @beacon[
#   id=mci-n03@transition-create-transaction,
#   slice_labels=mci-create-transport,
#   kind=ast,
#   comment=Caller-held singleton and k-run transaction on one verifier; shares pure fingerprint partitioning with the caller's preselection, retains current-call prefixes and joins verification before cancellation or fallback.,
# ]
class TransitionCreateTransaction:
    """Shared singleton/k handoff and pure identity partitions for caller selection."""
    def __init__(self, client, parent_id, held_region: MutationRegionGrant, *, transaction_id=None,
                 parent_region, governance_coverage_hint=None):
        """Receive an explicit child-set request and its caller-owned covering grant."""
        self.client, self.parent_id, self.held_region = client, parent_id, held_region
        self.parent_region = parent_region
        self.paths = client._coherence_paths()
        self.transaction_id = transaction_id or uuid4().hex
        self.governance_coverage_hint = governance_coverage_hint
        self._latest = None
        self._committed = False
        self._external = False
        self._affected = set()
        self.verified_prefix = ()

    def _require_hold(self):
        """Check the supplied child-set identity and active same-operation coverage."""
        from ..coherence.cache_view import CacheViewError
        grant = self.held_region
        if (not isinstance(grant, MutationRegionGrant) or not grant.is_active
                or grant._state is not MUTATION_LOCK_STATE.get()):
            raise CacheViewError("N03 requires the caller's active parent region grant")
        description = self.parent_region.to_dict()
        parent_field, parent = (("parent_special", "root") if self.parent_id is None else
                                ("parent_special", "inbox") if self.parent_id == "inbox" else
                                ("parent_uuid", self.parent_id))
        if description.get("kind") != "child_set" or description.get(parent_field) != parent:
            raise CacheViewError("N03 supplied region is not the parent's child set")
        requested = (self.parent_region,)
        if not _regions_cover_request(grant.regions, requested, cache_by_id=grant.snapshot.nodes_by_id):
            raise CacheViewError("N03 grant does not cover the parent's child set")

    async def _listing(self):
        self._require_hold()
        evidence = {}
        await self.client.list_nodes(NodeListRequest(parentId=self.parent_id), _listing_evidence=evidence)
        return evidence

    async def _baseline(self):
        """Reuse local evidence or a currently clean parent, not unrelated dirt."""
        current_status = self.client._coherence_boundary_status()
        external_dirty = getattr(current_status, "external_dirty", False)
        if self._latest is not None and not self._external and not external_dirty:
            return self._latest
        snapshot = current_operation_cache(required=False)
        if self._latest is None and snapshot is not None and snapshot is self.held_region.snapshot:
            current_view, dirty_ids = self.client._capture_view_and_dirty()
            rows = [row for row in snapshot.nodes_by_id.values()
                    if row.get("parent_id", row.get("parentId")) == self.parent_id]
            parent_ids = {self.parent_id, "*", *(row.get("id") for row in rows)}
            parent_clean_now = (current_view is snapshot.view
                                and parent_ids.isdisjoint(dirty_ids) and not external_dirty)
            status = snapshot.boundary_status
            if (not getattr(status, "external_dirty", False)
                    and parent_clean_now and self.parent_id in snapshot.nodes_by_id):
                try:
                    return _order(self.parent_id, rows)
                except (ValueError, TypeError, AttributeError):
                    pass
        evidence = await self._listing()
        if not evidence.get("recognized"):
            raise NetworkError("N03 baseline listing has no membership evidence")
        return _order(self.parent_id, evidence["rows"])

    def _eligible(self, request, placement, baseline):
        try:
            UUID(self.parent_id)
        except (ValueError, TypeError, AttributeError):
            return False
        return (placement.kind == "after" and placement.predecessor_uuid in baseline.ordered_child_ids
                and request.layoutMode in (None, "bullets"))

    async def _preflight(self):
        self._require_hold()
        await require_coherence_mutation_admission(self.paths, operation="create")

    async def _ambiguous(self, detail):
        publication = await raise_global_dirty_barrier_async(
            self.paths, reason_code="ambiguous_commit_outcome", transaction_id=self.transaction_id,
            affected_region={"kind": "child_set", "parent_uuid": self.parent_id}, detail="N03 " + detail,
        )
        try:
            await _request_recovery()
        except Exception:
            LOGGER.exception("N03 barrier recovery scheduling failed; manual system.cache.refresh required")
        raise CoherenceBarrierActiveError(
            "Create outcome is unknown; no fallback was sent. A full system.cache.refresh is required.",
            dirty_generation=publication.state.generation, reason_code="ambiguous_commit_outcome",
            transaction_id=self.transaction_id, operation="create",
        )

    def _dirty(self):
        if self._committed:
            self.client._mark_nodes_export_dirty([self.parent_id, *self._affected])

    async def _verify(self, baseline, requests, physical, transport, returned_id, original_auth):
        """Verify the entire attempt; publish each listed addition in request order."""
        try:
            evidence = await self._listing()
        except (AuthenticationError, APIResponseError) as exc:
            if original_auth is not None and (isinstance(exc, AuthenticationError) or exc.status_code in (401, 403)):
                # A1: endpoint and listing both refused authentication, no barrier.
                await self.client._handle_response(original_auth)
            return await self._ambiguous("listing_authentication_failure")
        except (OperationCancelledError, asyncio.CancelledError):
            raise
        except Exception:
            return await self._ambiguous("listing_failure")
        expected = tuple((html.unescape(self.client.encode_for_workflowy_api(request.name, "name", operation="create")),
                          self.client.encode_for_workflowy_api(request.note, "note", operation="create"))
                         for request in requests)
        verdict = classify_create_run(self.parent_id, baseline, evidence, expected, physical,
                                           returned_id=returned_id)
        if verdict.classification == CreateClassification.AMBIGUOUS:
            return await self._ambiguous(verdict.detail)
        self._latest = verdict.witness
        if verdict.classification == CreateClassification.COMMITTED:
            self._committed = True
            self._affected.update(verdict.witness.ordered_child_ids)
            if transport == "llm" or returned_id is None:
                # Acknowledged v1 already used the legacy patcher. A final
                # unknown v1 error did not deliver that ID; publish its verified
                # addition here through the same existing COW door as LLM.
                for identifier in verdict.created_ids:
                    row = next(row for row in verdict.witness.rows if row["id"] == identifier)
                    try:
                        self.client._apply_create_to_nodes_export_cache(WorkFlowyNode(**row), self.parent_id)
                    except Exception:
                        LOGGER.exception("N03 verified create patch failed; retaining UUID and dirty fallback")
            # Mark before any asynchronous dirt publication or cancellation returns.
            self._dirty()
            self.verified_prefix += tuple(
                WorkFlowyNode(**next(row for row in verdict.witness.rows if row["id"] == identifier))
                for identifier in verdict.created_ids)
            if verdict.external_ids:
                self._external = True
                await record_external_additions(self.paths, self.parent_id)
        return verdict

    async def _joined_verification(self, *args, cancelled=None):
        """Retain and join verification through raw/task-local cancellation."""
        from ..client.api_client_core import operation_cancel_scope
        pending_cancel = cancelled
        while True:
            async def verify():
                if pending_cancel is not None:
                    with operation_cancel_scope(None):
                        return await self._verify(*args)
                return await self._verify(*args)
            task = asyncio.create_task(verify())
            while True:
                try:
                    verdict = await asyncio.shield(task)
                    return verdict, pending_cancel
                except asyncio.CancelledError as exc:
                    if task.done() and task.cancelled():
                        pending_cancel = pending_cancel or exc
                        break
                    pending_cancel = pending_cancel or exc
                except OperationCancelledError as exc:
                    pending_cancel = pending_cancel or exc
                    break

    async def create(self, request: NodeCreateRequest, placement: PlacementIntent, *, selection=None) -> TransitionCreateResult:
        """Singleton compatibility name; no independent execution or verifier."""
        return await self.create_many((request,), placement, selection=selection)

    @staticmethod
    def identifiable_runs(client, requests):
        """Pure canonical repeat partition, shared with the loop-side selector."""
        requests = tuple(requests)
        if len(requests) <= 1:
            return (requests,) if requests else ()
        groups, start, seen = [], 0, set()
        for index, request in enumerate(requests):
            fingerprint = _content(
                html.unescape(client.encode_for_workflowy_api(request.name, "name", operation="create")),
                client.encode_for_workflowy_api(request.note, "note", operation="create"))
            if fingerprint in seen:
                groups.append(requests[start:index])
                start, seen = index, set()
            seen.add(fingerprint)
        groups.append(requests[start:])
        return tuple(groups)

    async def create_many(self, requests, placement: PlacementIntent, *, selection=None) -> TransitionCreateResult:
        """Send maximal unique-fingerprint subruns; retain only this call's prefix.

        A repeated complete fingerprint is the only batch boundary. Each next
        subrun follows the preceding verified UUID, never a predicted identity.
        """
        controller = self.client._insert_capability
        self.verified_prefix = ()
        try:
            requests = tuple(request.model_copy(deep=True) for request in requests)
            if not requests:
                raise ValueError("N04 create_many requires a nonempty run")
            result, debt, transport = None, False, "llm"
            for group in self.identifiable_runs(self.client, requests):
                selected, selection = selection, None
                result = await self._create_run(group, placement, selection=selected)
                debt = debt or result.reorder_required
                if result.transport == "v1":
                    transport = "v1"
                if placement.kind == "after":
                    placement = PlacementIntent.after(result.created_ids[-1])
            return TransitionCreateResult(CreateClassification.COMMITTED, transport, self.verified_prefix,
                                          result.order_witness, debt)
        finally:
            if selection is not None:
                controller.record_neutral(selection)
            self._dirty()

    async def _create_run(self, requests, placement, *, selection=None):
        """The one attempt engine for singleton/k runs and per-item v1 fallback."""
        controller = self.client._insert_capability
        prefix_start = len(self.verified_prefix)
        try:
            self._require_hold()
            if any(request.parent_id != self.parent_id for request in requests):
                raise ValueError("N03 request parent differs from held transaction")
            await self._preflight()
            self.client._raise_if_operation_cancelled("create")
            baseline = await self._baseline()
            eligible = all(self._eligible(request, placement, baseline) for request in requests)
            if selection is None and eligible:
                selection = controller.select_for_insert()
            llm = eligible and selection is not None and selection.transport == "llm"
            # Two explicit attempts at most: LLM, then legacy v1 only after absence.
            transports = ["llm", "v1"] if llm else ["v1"]
            for transport in transports:
                groups = (requests,) if transport == "llm" else tuple((request,) for request in requests)
                for group in groups:
                    verdict, cancelled, result, original_v1_error = await self._attempt(
                        group, placement, baseline, transport, selection)
                    if cancelled is not None:
                        raise cancelled
                    self.client._raise_if_operation_cancelled("create")
                    if verdict.classification == CreateClassification.COMMITTED:
                        if transport == "llm" and (result is None or result.kind == LlmResultKind.ACKNOWLEDGED):
                            controller.record_success(selection)
                        baseline = verdict.witness
                        continue
                    baseline = verdict.witness
                    if transport == "v1":
                        if original_v1_error is not None:
                            raise original_v1_error
                        raise NetworkError("N03 v1 invocation was conclusively absent; no outer retry was sent")
                    break
                else:
                    return TransitionCreateResult(CreateClassification.COMMITTED, transport,
                        self.verified_prefix[prefix_start:], baseline,
                        transport == "v1" and placement.kind == "after")
        finally:
            if selection is not None:
                controller.record_neutral(selection)
            self._dirty()

    async def _attempt(self, requests, placement, baseline, transport, selection):
                """Send once and join the same mandatory verifier on either transport."""
                controller = self.client._insert_capability
                request = requests[0]
                stage, result, cancelled, original_auth = SendStage(), None, None, None
                physical = placement if transport == "llm" else (
                    PlacementIntent.bottom() if placement.kind == "after" else placement)
                capability_reason = None
                original_v1_error = None
                try:
                    if transport == "llm":
                        result = await LlmInsertAfterTransport(self.client).insert_after(
                            self.parent_id, placement.predecessor_uuid, requests,
                            before_send=self._preflight, send_stage=stage)
                        response = result
                        if response.kind in (LlmResultKind.UNSUPPORTED_ENDPOINT, LlmResultKind.SCHEMA_DRIFT):
                            capability_reason = response.kind.value
                        if response.response.status_code in (401, 403):
                            original_auth = response.response
                    else:
                        await self._preflight()
                        result = await PublicV1CreateTransport(self.client).create(
                            request, placement, governance_coverage_hint=self.governance_coverage_hint)
                except (OperationCancelledError, asyncio.CancelledError) as exc:
                    if transport != "llm" or not stage.dispatch_started:
                        raise
                    cancelled = exc
                except APIResponseError as exc:
                    if transport != "llm" or not 300 <= exc.status_code < 400:
                        raise
                    capability_reason = "unsupported_endpoint"
                except (httpx.TransportError, httpx.DecodingError, NetworkError, WorkflowyTimeoutError,
                        AttributeError, TypeError, UnicodeDecodeError, PydanticValidationError) as exc:
                    if transport == "llm" and not stage.dispatch_started:
                        raise
                    if transport == "llm" and not isinstance(exc, (httpx.TransportError, httpx.DecodingError, NetworkError, WorkflowyTimeoutError)):
                        raise
                    if transport == "v1":
                        # One legacy invocation, not its hidden internal sends.
                        # Reuse its PURE validators on the unchanged owned input:
                        # legacy input validation also uses NetworkError, but is
                        # definitively before HTTP. Do not mistake it for a lost
                        # reply. No validator policy or rejection is added here.
                        if not (self.client._validate_name(request.name)[0]
                                and self.client._validate_note(request.note, skip_newline_check=True)[0]):
                            raise
                        # Valid-input shape/model errors arise from malformed
                        # accepted acknowledgements, not authoritative identity.
                        # Definitive 4xx/lane/auth/cancel outcomes bypass this.
                        original_v1_error = exc
                if capability_reason:
                    controller.record_capability_failure(selection, capability_reason)
                returned_id = (result.returned_nodes[0].id
                               if transport == "v1" and result and result.returned_nodes else None)
                verdict, cancelled = await self._joined_verification(
                    baseline, requests, physical, transport, returned_id, original_auth, cancelled=cancelled)
                return verdict, cancelled, result, original_v1_error

    async def conclude(self, *, relist=False) -> ParentOrderWitness | None:
        """Return the last observed order, optionally after caller-owned reorder."""
        self._require_hold()
        try:
            if relist:
                evidence = await self._listing()
                if not evidence.get("recognized"):
                    raise NetworkError("N03 conclusion listing lacks order evidence")
                self._latest = _order(self.parent_id, evidence["rows"])
                self._affected.update(self._latest.ordered_child_ids)
            return self._latest
        finally:
            self._dirty()
            self._latest = None
