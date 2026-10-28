"""The per-operation archive context (Archive Reads U1).

One small context object rides the per-operation cache snapshot: it names
the operation's ARCHIVE MODE -- ``"fallback"`` (a read; archives may answer
a handle that misses live Workflowy), ``"include"`` (a read running on an
augmented view that already carries the overlay records), or ``"none"``
(everything else; archives never answer) -- plus WHY, the overlay when the
operation is augmented, and one mutable per-run state object.

Everything here is pure bookkeeping around that context: the marker that
partitions live records from overlay records (Axiom 1 holds even in plain
threads where no context is visible), the side-band record of what an
archive answered, the scope-root note (consumed by Unit 3's footer), the
fallback gate, and the three-site write guard.  No I/O, no Workflowy
access, no second ContextVar: the context lives on the ONE operation-cache
snapshot (``OperationCacheSnapshot.archive``).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

# The one archive marker key.  Overlay records are shallow copies of parsed
# archive records with this key ADDED; live records never carry it.  The
# marker -- not the operation context -- is the authority for the live/archive
# partition, so two-phase resolution stays correct in any thread (E39).
ARCHIVE_RECORD_KEY = "_nexus_archive"

# The leading-emoji mark every overlay record carries (Axiom 10).
ARCHIVE_MARK = "📦"

# The archive modes (§4.3).
ARCHIVE_MODE_FALLBACK = "fallback"
ARCHIVE_MODE_INCLUDE = "include"
ARCHIVE_MODE_NONE = "none"

# The context `reason` vocabulary (§4.3.1).  ``automatic`` marks the re-run
# a fallback answer causes; ``flag`` marks an ``include_archives: true`` run.
ARCHIVE_REASONS = (
    "write",
    "export",
    "purge_preview",
    "beacon",
    "system",
    "sink",
    "automatic",
    "flag",
)


@dataclass(frozen=True)
class ArchiveServed:
    """One handle an archive answered (drained into the §5.1 banner by U2)."""

    handle: str
    uuid: str
    archive_file: str
    archive_name: str
    exported_at: str
    archive_root: str


class ArchiveOperationState:
    """Mutable per-run state, guarded by a lock (one per handler invocation).

    ``rerun_requested`` -- set by the fallback gate when an archive would
    answer a handle the live phase missed; the invoker re-runs the handler
    once on an augmented view (§4.5).  ``served`` -- the archive-answered
    handles, deduplicated by uuid.  ``scope_roots`` -- the operation's
    resolved scope roots (noted by Unit 3's footer seams).  ``vault_scope``
    -- set by :func:`note_archive_scope_vault` when the operation's scope
    is the whole vault (``anchor.dock.list``): the §5.5 footer then counts
    every active/orphaned archive instead of chain matches.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rerun_requested = False
        self._served: list[ArchiveServed] = []
        self._served_uuids: set[str] = set()
        self._scope_roots: list[str] = []
        self._vault_scope = False

    @property
    def rerun_requested(self) -> bool:
        with self._lock:
            return self._rerun_requested

    def request_rerun(self) -> None:
        with self._lock:
            self._rerun_requested = True

    @property
    def served(self) -> list[ArchiveServed]:
        with self._lock:
            return list(self._served)

    def record_served(self, entry: ArchiveServed) -> bool:
        """Record one archive-answered handle; dedup by uuid.  True when new."""
        with self._lock:
            if entry.uuid in self._served_uuids:
                return False
            self._served_uuids.add(entry.uuid)
            self._served.append(entry)
            return True

    @property
    def scope_roots(self) -> list[str]:
        with self._lock:
            return list(self._scope_roots)

    def note_scope_roots(self, root_ids: list[str]) -> None:
        with self._lock:
            for root_id in root_ids or []:
                if root_id and root_id not in self._scope_roots:
                    self._scope_roots.append(root_id)

    @property
    def vault_scope(self) -> bool:
        with self._lock:
            return self._vault_scope

    def note_vault_scope(self) -> None:
        with self._lock:
            self._vault_scope = True


@dataclass(frozen=True)
class ArchiveOperationContext:
    """The per-operation archive decision (carried on the snapshot)."""

    mode: str
    reason: "str | None" = None
    overlay: Any = None
    state: ArchiveOperationState = field(default_factory=ArchiveOperationState)
    operation: str = ""


def current_archive_context() -> "ArchiveOperationContext | None":
    """The current task's archive context, or None outside any boundary."""
    from .coherence.operation_cache import current_operation_cache

    snapshot = current_operation_cache(required=False)
    if snapshot is None:
        return None
    return getattr(snapshot, "archive", None)


def is_archive_record(node: Any) -> bool:
    """True iff this node mapping is an overlay (archive) record.

    The partition authority (§4.4.1): reads the marker key off the record
    itself, never the operation context, so the partition is correct in any
    thread and everywhere the map travels.
    """
    if not isinstance(node, dict):
        return False
    return ARCHIVE_RECORD_KEY in node


def record_archive_served(supplied: str, uuid: str, provenance: Any) -> bool:
    """Note that an archive answered ``supplied`` with ``uuid``.

    ``provenance`` is the record's ``ARCHIVE_RECORD_KEY`` payload (the
    plain-dict ArchiveProvenance).  No-op (False) without a context or on a
    duplicate uuid.  A speculative resolution its caller discards can leave
    an entry; the banner may then name an archive that served nothing
    visible -- tolerated, never the reverse (§4.4.4).
    """
    context = current_archive_context()
    if context is None or context.state is None or not isinstance(provenance, dict):
        return False
    entry = ArchiveServed(
        handle=str(supplied),
        uuid=str(uuid),
        archive_file=str(provenance.get("archive_file") or ""),
        archive_name=str(provenance.get("archive_name") or ""),
        exported_at=str(provenance.get("exported_at") or ""),
        archive_root=str(provenance.get("archive_root") or ""),
    )
    return context.state.record_served(entry)


def note_archive_scope(root_ids: "list[str] | None") -> None:
    """Record the operation's resolved scope roots (§U3 footer, deliverable 3).

    Unit 3's footer consumes the recorded roots to count the archived
    subtrees left out of a non-augmented result (§5.5): an archive counts
    when its planted-parent chain contains one of these roots.  No-op
    without a context; never raises.
    """
    context = current_archive_context()
    if context is None or context.state is None:
        return
    context.state.note_scope_roots(list(root_ids or []))


def note_archive_scope_vault() -> None:
    """Mark the operation's scope as the whole vault (§U3, ``anchor.dock.list``).

    The §5.5 footer then counts every active/orphaned archive, not just
    chain matches.  No-op without a context; never raises.
    """
    context = current_archive_context()
    if context is None or context.state is None:
        return
    context.state.note_vault_scope()


# The refusal reasons (§4.3.2 / §4.7.2): the mode-none reasons under which a
# site that reports a handle as not found must first ask the archive-aware
# miss and refuse an archived node.  Reads (reason None / "flag") and a
# fallback/include context never refuse here — the gate and the re-run serve
# archived handles instead.  One vocabulary, shared by every miss seam, so
# no per-site reason list can drift.
ARCHIVE_REFUSAL_REASONS = frozenset(
    {"write", "export", "purge_preview", "sink"}
)


# @beacon[
#   id=nexus-archives@is-exact-archived-id,
#   role=is_exact_archived_id(supplied, nodes_by_id),
#   slice_labels=nexus--archives,handle-resolution,
#   kind=ast,
#   comment=THE exact-archive test (E2 at every handle, rev 2 §4.4.3): is the supplied string — bracket-unwrapped and hyphen-normalized as the prefix kernel compares — EXACTLY the full id of a VISIBLE archived record? Asked by the Layer-1 UUID primitive whenever its live kernel outcome is SHAVED, in EVERY mode and with no operation context alike, so an exact archived full UUID never becomes a wrong-target shave (F2); a beacon operation never asks it (Axiom 12 — its shave stands, as before this program). Answered from the map's archive partition when the map carries one (the record marker is the authority), else through the store's ONE lookup with exact_only=True against the active overlay for the operation's live view (lazy load; no second match algorithm). Side-effect-free: never raises, never resolves, never records, never requests the re-run; with no folder configured the answer is always no (G1). Only an exact full UUID counts — a prefix that also matches an archived record never displaces a shave.
# ]
def is_exact_archived_id(supplied: str, nodes_by_id: Any = None) -> bool:
    """Is ``supplied`` exactly the full id of a visible archived record?

    The exact-archive test (rev 2 §4.4.3, E2): asked by the Layer-1 UUID
    primitive whenever its live kernel outcome is a SHAVE, in every mode
    and with no operation context alike — an exact archived full UUID
    displaces the shave everywhere (the beacon family excepted, Axiom 12).
    Side-effect-free: never raises, never resolves, never records anything,
    never requests the re-run (the gate keeps that job, in the archive
    phase only).  With no archive folder configured the answer is always
    ``False`` (G1).
    """
    context = current_archive_context()
    if context is not None and context.reason == "beacon":
        # Axiom 12: the beacon family never consults archives — its handle
        # resolves exactly as before this program, a shave included.
        return False
    from .client.api_client_nexus import unwrap_bracketed_uuid_handle

    unwrapped = unwrap_bracketed_uuid_handle(
        supplied if isinstance(supplied, str) else str(supplied or "")
    )
    needle = str(unwrapped).replace("-", "")
    if len(needle) < 8:
        return False
    # The map's archive partition when the map carries one (an augmented
    # operation): the record marker is the authority, so the answer holds
    # without a visible operation context.
    if isinstance(nodes_by_id, Mapping) and nodes_by_id:
        for nid, node in nodes_by_id.items():
            if is_archive_record(node) and str(nid).replace("-", "") == needle:
                return True
        # The map is live-only (no partition): fall through to the store.
    try:
        from .archive_store import get_archive_store

        hit = get_archive_store().lookup(
            str(unwrapped),
            "uuid",
            live_view=_snapshot_live_view(),
            exact_only=True,
        )
    except Exception:  # noqa: BLE001 - the test never raises (§4.4.3)
        return False
    return bool(hit)


# @beacon[
#   id=nexus-archives@archive-fallback-gate,
#   role=archive_fallback_gate,
#   slice_labels=nexus--archives,handle-resolution,
#   kind=ast,
#   comment=THE fallback gate (§4.4.3): called by the two resolution primitives ONLY in their archive phase, when the map carries no archive partition — the re-run request and nothing else (rev 2: the exact-archive case moved to the side-effect-free is_exact_archived_id, asked at the shave in every mode; the gate never fires from a live phase). Inert unless the current operation context's mode is 'fallback'; on a store hit it sets state.rerun_requested so the invoker re-runs the whole handler ONCE on the augmented view (§4.5) and the primitive returns unresolved. Never raises, never resolves, never returns an archived node itself: the gate is a decision, not a resolver.,
# ]
def archive_fallback_gate(
    supplied: str,
    kind: str,
    *,
    exact_only: bool = False,
) -> bool:
    """Would an active archive answer this handle?  Side effect only.

    Called by the two resolution primitives in their ARCHIVE phase when the
    map carries no archive partition — the re-run request and nothing else
    (rev 2 §4.4.3: the exact-archive-vs-shave case is the side-effect-free
    :func:`is_exact_archived_id`, asked at the shave in every mode; the gate
    runs only in an archive phase, never from a live one).  Does nothing --
    and returns False -- unless the current context's mode is ``fallback``.
    On a store hit it sets ``state.rerun_requested`` so the invoker re-runs
    the whole handler on the augmented view, and the primitive returns
    unresolved.  Never raises, never resolves, never returns an archived
    node itself (§4.4.3).
    """
    context = current_archive_context()
    if context is None or context.mode != ARCHIVE_MODE_FALLBACK:
        return False
    from .archive_store import get_archive_store

    store = get_archive_store()
    from .coherence.operation_cache import current_operation_cache

    snapshot = current_operation_cache(required=False)
    view = snapshot.view if snapshot is not None else None
    if view is None:
        return False
    try:
        hit = store.lookup(supplied, kind, live_view=view, exact_only=exact_only)
    except Exception:  # noqa: BLE001 - the gate never raises (§4.4.3)
        return False
    if not hit:
        return False
    context.state.request_rerun()
    return True


class ArchiveReadOnlyViolation(RuntimeError):
    """Defence-in-depth guard: a write ran inside an augmented operation.

    The guards can only fire on a bug -- no write operation ever carries an
    overlay (§4.7.1).  Never caught anywhere: it propagates to the
    dispatcher's ``operation_failed``.
    """


def archive_write_guard(operation: str) -> None:
    """Raise :class:`ArchiveReadOnlyViolation` inside an augmented operation.

    Called as the FIRST statement of the three write chokepoints:
    ``WorkFlowyClientCore._send_workflowy_write`` (the sole mutating HTTP
    transport), ``publish_local_cache_view`` (the COW bridge every eager
    cache patch uses), and ``WorkFlowyClientCore.patch_nodes_export_snapshot_on_disk``
    (the compat epoch publication).  No-op without a context or without an
    overlay (every ordinary write operation runs context-free of overlays).
    """
    context = current_archive_context()
    if context is None or context.overlay is None:
        return
    raise ArchiveReadOnlyViolation(
        "archive_write_guard: operation "
        f"{operation or context.operation or '<unknown>'!r} attempted a write "
        "while the operation runs on an augmented (archive-including) view. "
        "Archived material is read-only; no write may ever happen in an "
        "augmented operation (Axiom 5)."
    )


# ---------------------------------------------------------------------------
# The archive-aware miss (§4.7.2) and the light hint (§5.4) — Unit 2
# ---------------------------------------------------------------------------


class ArchivedNodeReadOnlyError(RuntimeError):
    """A write (or export/sink/preview) aimed at an archived node.

    Raised by the archive-aware miss sites with the exact §5.2–§5.3 text;
    ``_mcp_exception_mapper`` turns it into the universal
    ``archived_node_read_only`` error envelope (the
    ``MutationSerializationTimeoutError`` precedent — §4.7.3).
    """

    code = "archived_node_read_only"

    def __init__(self, message: str, extra: "dict[str, Any] | None" = None) -> None:
        super().__init__(message)
        self.extra = dict(extra or {})


def _is_uuid_shaped_handle(supplied: Any) -> bool:
    """True for a UUID-shaped handle: after
    :func:`unwrap_bracketed_uuid_handle`, a hyphen-stripped pure-hex string
    of >= 8 characters (§5.4)."""

    text = supplied if isinstance(supplied, str) else str(supplied or "")
    from .client.api_client_nexus import unwrap_bracketed_uuid_handle

    unwrapped = unwrap_bracketed_uuid_handle(text.strip())
    compact = unwrapped.replace("-", "").lower()
    if len(compact) < 8:
        return False
    return all(c in "0123456789abcdef" for c in compact)


def _is_ap_shaped_handle(supplied: Any) -> bool:
    """§5.4: AP-shaped = ``_normalize_ap_id_input`` returns a core for it
    AND it is not UUID-shaped."""

    if _is_uuid_shaped_handle(supplied):
        return False
    from .client.api_client_nexus import _normalize_ap_id_input

    return _normalize_ap_id_input(str(supplied or "")) is not None


def _handle_lookup_kind(supplied: Any) -> str:
    """The store lookup kind for one handle: ``"ap"`` when AP-shaped, else
    ``"uuid"`` (the gate's own split, read from the handle's shape)."""

    return "ap" if _is_ap_shaped_handle(supplied) else "uuid"


def _snapshot_live_view() -> Any:
    """The current operation's live view, or None outside any boundary."""

    from .coherence.operation_cache import current_operation_cache

    snapshot = current_operation_cache(required=False)
    return snapshot.view if snapshot is not None else None


# @beacon[
#   id=nexus-archives@archive-miss-text,
#   role=archive_miss_text(supplied, *, verb, role, copy_hint),
#   slice_labels=nexus--archives,handle-resolution,
#   kind=ast,
#   comment=THE archive-aware miss (§4.7.2): every site that reports a node handle as not found calls this FIRST. Returns the exact §5.2–§5.3 text (chosen by the context's reason: write -> §5.2.1 with the §5.2.2 how-to-copy sentence for extract / promote-move; export -> §5.3.1; sink -> §5.3.2; purge_preview -> §5.3.3; no context -> §5.3.4) when the store's lookup — against the active overlay for the operation's live view, exactly as the gate does — HITS, else None (the site then appends the light hint when the handle is AP-shaped, ap_miss_hint). The texts come only from archive_texts.py; the site raises ArchivedNodeReadOnlyError(message, extra=provenance) with what this returns — or, in a batch ledger, writes the skip row. The beacon family never calls it (Axiom 12). An unexpected reason (a read's fallback/flag context) returns None: a read never refuses an archived node — its gate hit is served by the re-run before any not-found text is built.,
# ]
def archive_miss_text(
    supplied: str,
    *,
    verb: "str | None" = None,
    role: str = "handle",
    copy_hint: "str | None" = None,
) -> "str | None":
    """The §5.2–§5.3 text for one archived-handle miss, or ``None``.

    Thin text-only front over :func:`archive_miss` (the spec's named
    signature); sites that need the record's provenance for the error's
    ``extra`` call :func:`archive_miss` directly.
    """
    text, _provenance = archive_miss(
        supplied, verb=verb, role=role, copy_hint=copy_hint
    )
    return text


def archive_miss(
    supplied: str,
    *,
    verb: "str | None" = None,
    role: str = "handle",
    copy_hint: "str | None" = None,
) -> "tuple[str | None, dict[str, Any] | None]":
    """``(text, provenance)`` for one archived-handle miss (§4.7.2).

    ``(None, None)`` when there is no folder, or the store's lookup (against
    the active overlay for the operation's live view, exactly as the gate
    does) misses — the site then keeps its own not-found text, appending the
    light hint (§5.4) when the handle is AP-shaped.  On a hit the text is
    the exact §5.2–§5.3 message chosen by the context's reason and the
    provenance dict is the matched record's (the raised
    :class:`ArchivedNodeReadOnlyError`'s ``extra``).  Sites that also need
    the matched archived id call :func:`archive_miss_hit`.  Never raises.
    """
    text, provenance, _uuid = archive_miss_hit(
        supplied, verb=verb, role=role, copy_hint=copy_hint
    )
    return text, provenance


def archive_miss_hit(
    supplied: str,
    *,
    verb: "str | None" = None,
    role: str = "handle",
    copy_hint: "str | None" = None,
) -> "tuple[str | None, dict[str, Any] | None, str | None]":
    """``(text, provenance, archived_uuid)`` for one archived-handle miss.

    The full form of :func:`archive_miss` (the promote branch needs the
    matched archived id to read the file's verbatim records).  Never raises.
    """
    from .archive_store import get_archive_store
    from .archive_texts import (
        archived_export_refusal_text,
        archived_node_read_only_text,
        archived_no_context_text,
        archived_parent_read_only_text,
        archived_purge_preview_text,
        archived_sink_refusal_text,
        EXTRACT_COPY_SENTENCE,
        PROMOTE_COPY_SENTENCE,
    )

    context = current_archive_context()
    kind = _handle_lookup_kind(supplied)
    try:
        store = get_archive_store()
        uuid_hit, provenance = store.miss_lookup(
            supplied, kind, live_view=_snapshot_live_view()
        )
    except Exception:  # noqa: BLE001 - the miss text never raises
        return None, None, None
    if not uuid_hit or not isinstance(provenance, dict):
        return None, None, None
    path = str(provenance.get("archive_file") or "")
    exported_at = str(provenance.get("exported_at") or "")
    verb_name = verb or (context.operation if context is not None else "") or "operation"
    if context is None:
        return (
            archived_no_context_text(verb_name, supplied, path, exported_at),
            provenance,
            uuid_hit,
        )
    reason = context.reason
    if reason == "write":
        if role in ("parent", "destination"):
            return (
                archived_parent_read_only_text(
                    verb_name, role, supplied, path, exported_at
                ),
                provenance,
                uuid_hit,
            )
        text = archived_node_read_only_text(verb_name, supplied, path, exported_at)
        if copy_hint == "promote_move":
            return text + PROMOTE_COPY_SENTENCE, provenance, uuid_hit
        if copy_hint == "extract" or (not copy_hint and verb_name == "anchor.point.extract"):
            return text + EXTRACT_COPY_SENTENCE, provenance, uuid_hit
        return text, provenance, uuid_hit
    if reason == "export":
        return (
            archived_export_refusal_text(verb_name, supplied, path, exported_at),
            provenance,
            uuid_hit,
        )
    if reason == "sink":
        return (
            archived_sink_refusal_text(verb_name, supplied, path, exported_at),
            provenance,
            uuid_hit,
        )
    if reason == "purge_preview":
        return (
            archived_purge_preview_text(verb_name, supplied, path, exported_at),
            provenance,
            uuid_hit,
        )
    # A read context (fallback / include / none-with-no-reason): a read
    # never refuses an archived node — the gate's hit is served by the
    # re-run before any not-found text would be built.  Fall through.
    return None, None, None


def ap_miss_hint(supplied: str, *, beacon: bool = False) -> "str | None":
    """The §5.4 light hint for one AP-shaped not-found, or ``None``.

    ``beacon=True`` (the beacon family's resolve and scope-root surfaces)
    returns the beacon line without consulting the store (Axiom 12);
    otherwise the hint state comes from the store's facts for the current
    live view: A (no folder), C (folder not fully readable) or B (searched,
    ``n`` active archives, not found).  ``None`` when the handle is not
    AP-shaped — UUID-shaped misses keep today's text exactly ([ap:O8R74F]).
    """
    from .archive_texts import (
        ap_hint_beacon,
        ap_hint_no_folder,
        ap_hint_not_found,
        ap_hint_unreadable,
    )

    if not _is_ap_shaped_handle(supplied):
        return None
    if beacon:
        return ap_hint_beacon()
    from .archive_store import get_archive_store

    try:
        facts = get_archive_store().ap_hint_facts(_snapshot_live_view())
    except Exception:  # noqa: BLE001 - the hint never raises
        return ap_hint_no_folder()
    state = facts.get("state")
    folder = str(facts.get("folder") or "")
    if state == "A":
        return ap_hint_no_folder()
    if state == "C":
        return ap_hint_unreadable(folder)
    return ap_hint_not_found(int(facts.get("n") or 0), folder)
