"""WorkFlowy API client - Core CRUD operations and validation."""

import json
import os
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Literal

import httpx

from ..carto_write_governor import CartoWriteGovernor, WriteGovernorSettings
from ..coherence.cache_view import is_unpopulated_view, unpopulated_cache_view
from ..coherence.cache_view import CacheView, CacheViewPublisher
from ..coherence.canonical_node import (
    _BARE_UPDATE_AMPERSAND_RE,
    _dewhiten_text as _coherence_dewhiten_text,
)
from ..coherence.canonical_node import (
    _semantic_name_non_cascading,
)
from ..coherence.errors import (
    CacheViewError,
    CoherenceAccountMismatchError,
    JournalIntegrityError,
)
from ..coherence.transition_effects import (
    TransitionEffect,
    TransitionEffectPolicy,
    merge_offer_with_transition_effects,
)
from ..config import get_runtime_subdir
from ..models import (
    APIConfiguration,
    APIResponseError,
    AuthenticationError,
    NetworkError,
    NodeCreateRequest,
    NodeListRequest,
    NodeNotFoundError,
    NodeUpdateRequest,
    OperationCancelledError,
    RateLimitError,
    TimeoutError,
    WorkFlowyNode,
    parse_retry_after_header,
)
from ..models.config import GovernanceConfiguration, WriteGovernorConfiguration
from ..models.errors import AccountWriteBlockedError

# U01 same-node mutation sequencing (spec [ap:XKQQUO] §6.3): the three tag
# primitives below own their whole live-read → compose → write window under
# the {node} key — the client-primitive seam (NOT the transport: update_node
# itself stays unlocked; the read it must serialize with lives above it).
from ..mutation_locks import mutation_region_locks
from ..mutation_lease_governance import build_region_exact_node
from ..mutation_conflicts import ConcurrentEditDetectedError

# Rate limit protection: delay between API operations (seconds)
# Workflowy's rate limits are generous but undefined.
#
# RESTORED TO 1.0s (2026-08-20) after a write-volume incident: an agent fired
# parallel F12 refreshes exceeded the account's write rate and Workflowy began
# returning `403 ... code WFB(3)` on every WRITE while reads still worked.
# Every daemon task using the shared publisher now uses the same account
# scheduler; this legacy read-pacing constant is not a second write lane.
#
# Phase B supplies the one in-process write scheduler and persisted breaker.
# This legacy constant is
# retained only for the two per-process CRUD read loops and /nodes-export read
# pacing; mutating sites must never reintroduce it alongside governed tickets.
API_RATE_LIMIT_DELAY = 1.0

# Task-local cancellation for a daemon task using the shared publisher:
# its probe never cancels unrelated work sharing that client. The client-level
# callback remains available to ordinary API callers.
_AMBIENT_OPERATION_CANCEL_PROBE: ContextVar[Callable[[], Any] | None] = ContextVar(
    "workflowy_operation_cancel_probe", default=None
)


@contextmanager
def operation_cancel_scope(callback: Callable[[], Any] | None):
    """Install one task-local cancellation probe for nested client waits."""

    token = _AMBIENT_OPERATION_CANCEL_PROBE.set(callback)
    try:
        yield
    finally:
        _AMBIENT_OPERATION_CANCEL_PROBE.reset(token)


# @beacon[
#   id=core-crud@_resolve_split_name_and_tags,
#   slice_labels=nexus-core-v1,nexus--tags,
#   kind=ast,
#   comment=THE single trailing-tag splitter used by the add/remove/rename_tag tools. Delegates to the canonical emoji/trident-aware nexus_map_codebase.split_name_and_tags; only if that import fails (rare -- module-load-order safety net) does it fall back to an INLINE COPY that mirrors the canonical emoji-aware semantics byte-for-behavior (trident/marker/generic-decoration trailing region peeled, content-emoji kept, tags returned '#'-only). One helper => the three tag tools can never drift from canonical.
# ]
def _resolve_split_name_and_tags(raw_name: str) -> tuple[str, list[str]]:
    """Split a node NAME into (base, trailing '#'-tags), emoji/trident-aware.

    Prefers the CANONICAL ``nexus_map_codebase.split_name_and_tags`` (imported
    lazily to avoid the documented module-load cycle: ``api_client_core`` is
    the foundation layer and ``nexus_map_codebase`` imports higher layers). If
    that import ever fails, the inline fallback below reproduces the SAME
    emoji/trident-aware trailing-region semantics (the fix for the piled-up
    trident bug), so add/remove/rename_tag behave identically whether or not
    the canonical module is importable -- no silent degrade to the old
    ``#``-only parser.

    This is the ONE place the three tag tools call, so they can never drift
    from each other or from canonical.
    """
    try:
        from nexus_map_codebase import split_name_and_tags  # type: ignore[import-not-found]
        return split_name_and_tags(raw_name)
    except Exception:
        log_event(
            "split_name_and_tags import from nexus_map_codebase FAILED; using "
            "inline emoji-aware fallback (behavior matches canonical).",
            "TAGS",
        )

    # Inline fallback -- mirrors canonical emoji/trident-aware semantics.
    name = (raw_name or "").strip()
    if not name:
        return "", []
    tokens = name.split()
    trident = "\U0001F531"

    def _is_marker(tok: str) -> bool:
        t = tok.strip()
        return t == trident or t.startswith("\U0001FA9A")

    def _is_pure_decoration(tok: str) -> bool:
        return bool(tok) and not any(ch.isalnum() for ch in tok)

    region_start = len(tokens)
    i = len(tokens) - 1
    while i >= 0:
        cand = tokens[i]
        if cand.startswith("#") or _is_marker(cand):
            region_start = i
            i -= 1
            continue
        if _is_pure_decoration(cand):
            i -= 1
            continue
        break
    if region_start >= len(tokens):
        return name, []
    base = " ".join(tokens[:region_start]).strip()
    tags = [t for t in tokens[region_start:] if t.startswith("#")]
    return base, tags


# @beacon[
#   id=core-crud@_semantic_name_for_tag_merge,
#   slice_labels=nexus-core-v1,nexus--tags,
#   kind=ast,
#   comment=Wire->semantic NAME normalizer for the add/remove/rename_tag merge path (the '&' -> stored '&amp;amp;' double-encode fix). Decodes EXACTLY ONE layer of &amp;/&lt;/&gt; from the raw GET /nodes/{id} name form via a non-cascading single pass -- the output is never re-scanned, so '&amp;lt;' -> '&lt;' (a user-visible literal entity stays a literal entity), NEVER '<'. Scope: the tag tools are the ONLY name-writers that read a raw wire-form name (live GET) and rewrite it; every other name-writer sources semantic text (caller-supplied, the dewhitened /nodes-export cache, disk content, or a synthesized constant). Do NOT substitute _dewhiten_text here: its chained str.replace calls cascade ('&amp;lt;' -> '&lt;' -> '<'), decoding two layers. The inverse of this helper is exactly encode_for_workflowy_api(field='name'), whose plain-text alphabet is only these three entities.,
# ]
def _semantic_name_for_tag_merge(raw_name: str) -> str:
    """Decode the raw wire-form NAME from ``GET /nodes/{id}`` to semantic text.

    Exactly ONE entity layer is decoded -- the same decode depth the
    Workflowy GUI applies once on display -- and the pass is deliberately
    NON-CASCADING: replacement output is never re-scanned, so a stored
    ``&amp;lt;`` (semantic: the literal characters ``&lt;``) decodes to
    ``&lt;``, never to ``<``. Only ``&amp;`` / ``&lt;`` / ``&gt;`` are
    decoded, matching the exact inverse alphabet of
    ``encode_for_workflowy_api(field='name')``; every other ``&`` sequence
    (``&nbsp;``, numeric entities, a bare ``&``) passes through untouched,
    as does literal whitelisted markup (``<b>``/``<i>``/``<s>``/``<code>``/
    colored spans carry no entities).

    Used ONLY by the add/remove/rename_tag merge path (and to normalize its
    post-update response). Do not use it as a general read-side decode --
    the /nodes-export cache has its own dewhitening, and notes use
    ``_note_wire_to_semantic``.
    """
    # SINGLE-SOURCE (W1/F-1): the implementation lives in
    # workflowy_mcp.coherence.canonical_node._semantic_name_non_cascading;
    # this seam delegates verbatim (falsy-input contract identical).  Do NOT
    # re-implement here — and per the beacon above, NEVER substitute
    # _dewhiten_text into this merge path.
    return _semantic_name_non_cascading(raw_name)


def _canonical_name_spacing(text: str) -> str:
    """Canonical protected-token joins and decoration, retaining title interiors."""
    import re
    from .api_client_nexus import _split_leading_ap_handle, _split_leading_name_emoji_run

    if "\t" in text:
        return text  # TAB is content, never a protected-token space join.
    base, tags = _resolve_split_name_and_tags(text)
    if tags or len(base.split()) < len(text.split()):
        # The canonical splitter owns which trailing glyphs are decoration,
        # not content (a lone right-side content emoji stays in the base).
        # This is the same protected-name form the identity writer emits.
        tokens = list(re.finditer(r"\S+", text))
        boundary = len(base.split())
        if boundary < len(tokens):
            # Keep original text tokens: the grammar's returned tag strings
            # may strip a decoded literal '<u>' which was NOT actual markup.
            tag_tokens = [match.group() for match in tokens[boundary:]
                          if _resolve_split_name_and_tags(match.group())[1]]
            # In this protected-name form the existing writer also joins the
            # base's ASCII space runs. That encoder spelling is presentation;
            # the title's characters, TABs and literal-markup tokens still count.
            text = " ".join(value for value in (
                base, " ".join(tag_tokens),
            ) if value)
    parts = _split_leading_ap_handle(text)
    emoji, title = _split_leading_name_emoji_run(parts["title"])
    if parts["token"] or emoji:
        prefix = " ".join(value.strip() for value in (
            parts["decoration"], parts["token"], emoji,
        ) if value.strip())
        return prefix + (" " if prefix and title else "") + title
    return text


# @beacon[
#   id=node-ops@canonical-update-value,
#   slice_labels=node-ops,
#   kind=ast,
#   comment=Pure wire comparison for name and note: one-layer entities with literal-markup provenance, auto-links merged before URL folding, inline presentation, protected-name joins and field-specific whitespace; never a write codec.,
# ]
def _canonical_update_value(field: str, wire_value: str | None) -> tuple:
    """Project wire text lexically, preserving content and literal markup provenance.

    Parse literal tags BEFORE decoding entities; decoded text is never parsed
    again. Adjacent text is merged after presentation wrappers are removed,
    before terminal URL slashes are folded, regardless of following punctuation.
    This comparison does not modify requests, live models or the cache codec.
    """
    import re
    from html.parser import HTMLParser

    def text_value(text: str) -> str:
        text = re.sub(r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]", " ", text)
        if field == "name":
            text = re.sub(r"[\n\r\u2028\u2029\v\f]", " ", text)
            from .api_client_nexus import _emoji_component_end, _EMOJI_BASES_REQUIRING_VS16_FALLBACK
            text = "".join(ch for index, ch in enumerate(text) if not (
                ch in ("\ufe0e", "\ufe0f") and index > 0 and (
                    _emoji_component_end(text, index - 1) is not None
                    or ord(text[index - 1]) in _EMOJI_BASES_REQUIRING_VS16_FALLBACK
                )
            ))
        else:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        # A scheme added to bare www is display. Keep the label's slash until
        # auto-links have been removed and their surrounding text merged.
        return re.sub(r"https?://(?=www\.)", "", text, flags=re.I)

    def url_value(text: str) -> str:
        return re.sub(r"(?:https?://|www\.)[^\s<>]+", lambda m: m.group().rstrip("/"), text)

    class WireTokens(HTMLParser):
        def __init__(self):
            # Decode data once inside the literal-tag parser. In particular,
            # keep a bare '&b=2' verbatim: entity callbacks cannot tell whether
            # the original reference ended in a semicolon.
            super().__init__(convert_charrefs=True)
            self.root = []
            self.stack = [(None, (), self.root)]

        def handle_data(self, data):
            self.stack[-1][2].append(data)

        def handle_starttag(self, tag, attrs):
            # HTMLParser decodes attribute entities exactly once, independently
            # of data. Canonicalize quote/order/case, not attribute content.
            tag = {"strong": "b", "em": "i", "del": "s"}.get(tag, tag)
            item = (tag, tuple(sorted(attrs, key=lambda item: (item[0], item[1] or ""))), [])
            self.stack[-1][2].append(item)
            self.stack.append(item)

        def handle_endtag(self, tag):
            tag = {"strong": "b", "em": "i", "del": "s"}.get(tag, tag)
            if len(self.stack) > 1 and self.stack[-1][0] == tag:
                self.stack.pop()
            else:
                self.handle_data("</" + tag + ">")

        def handle_startendtag(self, tag, attrs):
            self.stack[-1][2].append(("self:" + tag, tuple(sorted(attrs, key=lambda item: (item[0], item[1] or ""))), []))

        def handle_comment(self, data):
            self.stack[-1][2].append(("comment", (), [data]))

        def handle_decl(self, decl):
            self.stack[-1][2].append(("declaration", (), [decl]))

    def project(items):
        result = []

        def append(item):
            if isinstance(item, str) and result and isinstance(result[-1], str):
                result[-1] += item
            elif item != "":
                result.append(item)

        for item in items:
            if isinstance(item, str):
                append(text_value(item))
                continue
            tag, attrs, children = item
            children = project(children)
            attributes = dict(attrs)
            if tag == "a" and len(children) == 1 and isinstance(children[0], str):
                href = attributes.get("href")
                if href is not None and url_value(text_value(href)) == url_value(children[0]):
                    append(children[0])
                    continue
            if field == "name" and tag == "u" and len(children) == 1 and isinstance(children[0], str):
                previous = result[-1] if result and isinstance(result[-1], str) else ""
                candidate = (previous.rsplit(None, 1)[-1] if previous and not previous[-1].isspace() else "") + children[0]
                _, tags = _resolve_split_name_and_tags(candidate)
                if tags and len(candidate.split()) == 1:
                    append(children[0])
                    continue
            if tag == "b" and not attrs and len(children) == 1 and isinstance(children[0], tuple):
                child = children[0]
                if child[0] == "span" and "colored" in (dict(child[1]).get("class") or "").split():
                    append(child)
                    continue
            append((tag, attrs, tuple(children)))
        return result

    def fold_merged_urls(items):
        # Fold once on each complete text run, not on an auto-link's isolated
        # child: punctuation outside the anchor must be seen on both sides.
        return [url_value(item) if isinstance(item, str) else
                (item[0], item[1], tuple(fold_merged_urls(item[2])))
                for item in items]

    parser = WireTokens()
    parser.feed(_BARE_UPDATE_AMPERSAND_RE.sub("&amp;", wire_value or ""))
    parser.close()
    tokens = fold_merged_urls(project(parser.root))
    if field == "note":
        # Only the end of the whole note: never trim indentation or an inner
        # text token merely because a formatting boundary follows it.
        while tokens and isinstance(tokens[-1], str):
            tokens[-1] = tokens[-1].rstrip()
            if tokens[-1]:
                break
            tokens.pop()
    elif len(tokens) == 1 and isinstance(tokens[0], str):
        tokens[0] = _canonical_name_spacing(tokens[0])
        if not tokens[0]:
            tokens.pop()
    return tuple(tokens)


# @beacon[
#   id=core-log@log_event,
#   role=log_event,
#   slice_labels=ra-logging,
#   kind=ast,
# ]
def log_event(message: str, component: str = "CLIENT") -> None:
    """Log an event to stderr with timestamp and consistent formatting."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 🗡️ prefix makes it easy to grep/spot in the console
    print(f"[{timestamp}] 🗡️ [{component}] {message}", file=sys.stderr, flush=True)


# @beacon[
#   id=core-log@_log,
#   role=_log,
#   slice_labels=ra-logging,
#   kind=ast,
# ]
def _log(message: str, component: str = "CLIENT") -> None:
    """Unified log wrapper used throughout this client.

    This ensures ALL logging goes through the same DAGGER+DATETIME+TAG
    prefix and uses plain print(..., file=sys.stderr), which reliably
    surfaces in the MCP connector console (unlike the standard
    logging module, which FastMCP eats).
    """
    log_event(message, component)


class _ClientLogger:
    """Lightweight logger that delegates to _log / log_event.

    This replaces logger.info/logger.warning/logger.error in this file
    without relying on Python's logging module (which is swallowed by
    FastMCP). Methods accept arbitrary *args/**kwargs for compatibility
    but only the first message argument is used.
    """

    def __init__(self, component: str = "CLIENT") -> None:
        self._component = component

    def _msg(self, msg: object) -> str:
        try:
            return str(msg)
        except Exception:
            return repr(msg)

    def info(self, msg: object, *args: object, **kwargs: object) -> None:  # noqa: D401
        """Info-level log (no explicit level tag; message already descriptive)."""
        _log(self._msg(msg), self._component)

    def warning(self, msg: object, *args: object, **kwargs: object) -> None:
        _log(f"WARNING: {self._msg(msg)}", self._component)

    def error(self, msg: object, *args: object, **kwargs: object) -> None:
        _log(f"ERROR: {self._msg(msg)}", self._component)

    def debug(self, msg: object, *args: object, **kwargs: object) -> None:
        _log(f"DEBUG: {self._msg(msg)}", self._component)

    def exception(self, msg: object, *args: object, **kwargs: object) -> None:
        _log(f"EXCEPTION: {self._msg(msg)}", self._component)


def _assert_lineage_identity_compatible(current: CacheView, offered: CacheView) -> None:
    """Seal item 1 binding for lineage adoption (client adapter side).

    A populated current view and an offered lineage view must agree on
    account scope and complete epoch identity before the equal-marker /
    regression rules run.  Violations fail closed; they can never arise
    from a stale lock-free manifest read (an older artifact has an older
    epoch number AND its own id).
    """

    if (
        current.account_scope_id is not None
        and offered.account_scope_id is not None
        and current.account_scope_id != offered.account_scope_id
    ):
        raise CoherenceAccountMismatchError(
            "offered lineage view belongs to a different account scope",
            account_scope_id=current.account_scope_id,
            epoch_id=current.epoch_id,
        )
    if current.epoch_id == offered.epoch_id and current.epoch_number != offered.epoch_number:
        raise JournalIntegrityError(
            "offered lineage shares the current epoch id but not its epoch number",
            epoch_id=current.epoch_id,
            sequence=current.adopted_sequence,
        )
    if current.epoch_number == offered.epoch_number and current.epoch_id != offered.epoch_id:
        raise JournalIntegrityError(
            "offered lineage epoch number collides with a different epoch identity",
            epoch_id=current.epoch_id,
            sequence=current.adopted_sequence,
        )


# ARCHITECTURE NOTE - CLIENT INHERITANCE "NAMESPACE" CHAIN
# ---------------------------------------------------------------------------
# The Workflowy client is ONE logical object split across three files, chained
# by single inheritance purely to keep each file focused:
#
#     WorkFlowyClientCore   (this file)        - connection, low-level CRUD,
#                                                 cache, validation
#         ^
#     WorkFlowyClientEtch(Core)  (api_client_etch.py)  - ETCH node creation,
#                                                 hierarchy build, shared
#                                                 preview-tree renderer
#         ^
#     WorkFlowyClientNexus(Etch) (api_client_nexus.py) - GLIMPSE, SCRY,
#                                                 Cartographer, snippets, MD
#                                                 preview
#
# server.py instantiates the most-derived class (Nexus) and thereby gets every
# method on a single `self`. NOTE: this is inheritance-as-organization, not
# polymorphism (no overriding, no alternative subclasses, no Liskov story) -
# a mild anti-pattern. Shared STATELESS helpers therefore live at the LOWEST
# COMMON ANCESTOR of their callers (e.g. the preview-tree renderer sits on
# Etch because both ETCH previews and GLIMPSE full-mode call it). It is NOT
# ideal, but it works and is deliberately NOT worth refactoring right now; a
# future cleanup would peel the stateless render/preview helpers into a
# free-function module. This file is the BASE of that chain.
# ---------------------------------------------------------------------------
# @beacon[
#   id=auto-beacon@WorkFlowyClientCore-c455,
#   role=WorkFlowyClientCore,
#   slice_labels=node-ops,
#   kind=ast,
# ]
class WorkFlowyClientCore:
    """Core WorkFlowy API client – CRUD operations, cache, and validation.

    Cache semantics (per-process):
        Each WorkFlowyClientCore instance owns its own in-memory /nodes-export
        cache. Mutating methods (create_node, update_node, delete_node,
        complete_node, uncomplete_node, move_node, etc.) eagerly patch this
        cache **for that instance only** when possible and otherwise mark
        affected UUIDs as dirty.

        A daemon task using the shared publisher sees that publisher's current
        view. Independently constructed clients still own separate publishers.
    """

    def __init__(
        self,
        config: APIConfiguration,
        *,
        write_governor_config: WriteGovernorConfiguration | None = None,
        governance_config: GovernanceConfiguration | None = None,
    ):
        """Initialize one API client and its inert, memory-only insert controller."""
        self.config = config
        self.base_url = config.base_url
        self._client: httpx.AsyncClient | None = None

        # The governor derives the retained breaker/deferral paths through the
        # authoritative jobs-root and account-scope helpers; ordinary ticket
        # accounting is memory-only. Credentials are never persisted.
        write_config = write_governor_config or WriteGovernorConfiguration()
        mutex_config = governance_config or GovernanceConfiguration()
        self._write_governor = CartoWriteGovernor(
            api_base_url=self.base_url,
            api_key=config.api_key.get_secret_value(),
            settings=WriteGovernorSettings(
                interval_seconds=write_config.interval_seconds,
                max_wait_before_refusal_seconds=(
                    write_config.max_wait_before_refusal_seconds
                ),
                breaker_initial_cooldown_seconds=(
                    write_config.breaker_initial_cooldown_seconds
                ),
                breaker_max_cooldown_seconds=(
                    write_config.breaker_max_cooldown_seconds
                ),
                probe_lease_seconds=write_config.probe_lease_seconds,
                retry_after_fallback_seconds=(
                    write_config.retry_after_fallback_seconds
                ),
                mutex_wait_seconds=mutex_config.mutex_wait_seconds,
            ),
        )

        from .insert_capability import InsertCapabilityController

        self._insert_capability = InsertCapabilityController(
            self._write_governor.paths.account_scope_id
        )

        # /nodes-export cache and dirty tracking.
        #
        # S03 (Axiom 5 / H3): the CacheViewPublisher is the SOLE authoritative
        # root for this process's CURRENT cache state.  The legacy
        # ``_nodes_export_cache`` / ``_nodes_export_cache_timestamp`` names are
        # now READ-ONLY compatibility DERIVATIONS of the publisher's current
        # view (see the properties below) — they are never independently
        # assigned or separately synchronized.  The publisher starts on the
        # UNPOPULATED sentinel view ("no cache"); warm start and local refresh
        # publication install real lineage views through ``_publish_lineage_view``,
        # and the surviving
        # legacy eager patchers publish copy-on-write successors through
        # ``publish_local_cache_view`` (S03 §6.2).
        #
        # _nodes_export_dirty_ids holds UUIDs whose subtrees/ancestors have been
        # mutated since the last refresh. A "*" entry means "treat everything
        # as dirty".  Every mutation of this set that is coupled to a cache
        # transform is serialized under the publisher mutex (S03 §6.2).
        self._nodes_export_dirty_ids: set[str] = set()
        # S03-CR-02 (sparse repair — SUPERSEDES the 4fb06a1 ID-only
        # `_transition_cow_overlay_ids`): the transition-effect policy —
        # one per-publisher store of SPARSE, field-authoritative local
        # effects (before/after field masks over the owned vocabulary,
        # canonical created records, tombstones) accumulated from legacy
        # eager-patch COW publications.  EVERY epoch/lineage publication
        # route through this client's `_publish_lineage_view` and its
        # publisher-attached policy rebases strict-newer offers through the
        # ONE shared merge
        # authority, so a refresh/epoch publication can never silently
        # erase a just-verified local write, and a later legitimate server
        # value can never be masked forever (every non-clean resolution
        # leaves legacy-dirty evidence).  Transition-only bookkeeping
        # (retires with the bridge in S16); every mutation is welded under
        # the publisher mutex.
        self._transition_effect_policy = TransitionEffectPolicy(
            on_dirty=self._nodes_export_dirty_ids.update
        )
        # S02 §10.3: malformed/mismatched coherence lineage at warm start is
        # recorded DISTINCTLY here (never silently reduced to best-effort
        # cache-missing).  None means no lineage fault observed.
        self._nodes_export_lineage_error: str | None = None
        # Cache maintenance mode: "lazy" (mark-dirty only) vs "eager" (patch cache when possible).
        # Currently mainly used for logging/diagnostics; helpers below always
        # respect the presence/absence of an actual cache snapshot.
        self._nodes_export_cache_mode: str = "eager"
        # Snapshot timestamp accompanying the publisher's current lineage view
        # (written only inside _publish_lineage_view's atomic commit).
        self._cache_view_snapshot_timestamp: str | None = None
        self._cache_view_publisher: CacheViewPublisher = CacheViewPublisher(
            unpopulated_cache_view(account_scope_id=self._safe_account_scope_id()),
            transition_policy=self._transition_effect_policy,
        )

        # @beacon[
        #   id=api-client-core@load_nodes_export_cache_from_disk,
        #   role=API Client Core – load_nodes_export_cache_from_disk,
        #   slice_labels=ra-workflowy-cache,
        #   kind=span,
        # ]
        # Best-effort warm start: attempt to load a cached /nodes-export snapshot
        # from disk on client initialization so MCP restarts do not always pay the
        # full /nodes-export cost. Failures are logged but never fatal; callers
        # can still trigger an explicit refresh via refresh_nodes_export_cache().
        try:
            loaded = self._load_nodes_export_cache_from_disk()
            if loaded:
                self._log_debug("Warm-started /nodes-export cache from disk snapshot")
        except Exception as exc:  # pragma: no cover - defensive logging only
            self._log_debug(f"Failed to warm-start /nodes-export cache from disk: {exc}")
        # @beacon-close[
        #   id=api-client-core@load_nodes_export_cache_from_disk,
        # ]

    def _safe_account_scope_id(self) -> str | None:
        """Best-effort account scope for the unpopulated sentinel view."""

        try:
            return self._write_governor.paths.account_scope_id
        except Exception:  # noqa: BLE001 - credential-less test doubles
            return None

    # ------------------------------------------------------------------
    # S03 cache-view integration (H2 classes b/c/d/e)
    # ------------------------------------------------------------------

    @property
    def _nodes_export_cache(self) -> dict[str, Any] | None:
        """READ-ONLY compatibility derivation of the publisher's current view.

        S03 (H3): the CacheViewPublisher is the sole authoritative root for
        the process's current cache state; this legacy attribute now simply
        DERIVES from it (``None`` while no cache is populated) and is never
        independently assigned.  The returned payload is the immutable
        view's own structure — readers may iterate it, but no caller may
        mutate it in place (Axiom 5; the S03-B alias fence enforces).
        """

        publisher = getattr(self, "_cache_view_publisher", None)
        if publisher is None:
            return None
        view = publisher.capture()
        if is_unpopulated_view(view):
            return None
        return view.nodes_export

    @property
    def _nodes_export_cache_timestamp(self) -> Any:
        """READ-ONLY derivation: the snapshot timestamp of the current view's
        base lineage (``None`` while no cache is populated)."""

        publisher = getattr(self, "_cache_view_publisher", None)
        if publisher is None or is_unpopulated_view(publisher.capture()):
            return None
        return self._cache_view_snapshot_timestamp

    @property
    def _transition_cow_effects(self) -> Mapping[str, TransitionEffect]:
        """READ-ONLY derivation: the current sparse transition-effect store.

        Introspection for tests/diagnostics — one consistent store
        reference (the store is copy-on-write replaced under the publisher
        mutex, never mutated in place).  S03-CR-02 sparse repair; supersedes
        the removed 4fb06a1 ``_transition_cow_overlay_ids`` ID set.
        """

        return self._transition_effect_policy.snapshot_effects()

    # @beacon[
    #   id=coherence@client-lineage-adoption,
    #   slice_labels=ra-coherence,ra-workflowy-cache,mci-cache-seams,
    #   kind=ast,
    #   comment=One local lineage publisher: forward explicit payload ownership into CacheView construction, preserve account/equal-marker/no-regression rules and identity-CAS. Strict-newer offers use the one sparse transition merge; timestamp, dirty evidence and surviving effect store commit atomically with the swap, with publication-kind CREATE evidence unchanged.,
    # ]
    def _publish_lineage_view(
        self,
        nodes_export: dict[str, Any],
        *,
        epoch_number: int,
        epoch_id: str,
        adopted_sequence: int,
        snapshot_timestamp: str | None,
        account_scope_id: str | None = None,
        publication_kind: str | None = None,
        clear_dirty: bool = False,
        discard_dirty: tuple[str, ...] = (),
        ownership: Literal["borrowed", "transferred"] = "borrowed",
    ) -> CacheView:
        """Publish a local lineage view with NEW markers.

        The one publisher for warm start and S02 refresh / compat-patch
        publication, shared by every daemon task using the shared publisher.
        Publication is regression-guarded: a locally
        newer view (higher ``(epoch_number, adopted_sequence)``) is never
        overwritten by an older lineage payload.  The snapshot timestamp
        and any dirty-set bookkeeping commit atomically with the swap.
        Ownership is passed unchanged to the one builder: production callers
        surrender their private materializations; arbitrary borrowed callers
        retain the defensive deep-isolation default.
        """

        # Seal-2 item 2: bind the offer to THIS CLIENT's authoritative
        # account before any view construction or publication.  Contract for
        # the optional argument: ``account_scope_id=None`` means
        # "derive from this client's account" — NEVER "unbound".  An
        # explicit foreign account fails closed.  Only a credential-less
        # scaffold double (no derivable client account) may publish an
        # unbound view; production clients always resolve an account.
        client_account = self._safe_account_scope_id()
        if account_scope_id is None:
            account_scope_id = client_account
        if client_account is not None and account_scope_id != client_account:
            raise CoherenceAccountMismatchError(
                "offered lineage account does not match this client's account scope",
                account_scope_id=client_account,
                epoch_id=epoch_id,
                sequence=adopted_sequence,
            )

        new_view = CacheView.build(
            epoch_number=epoch_number,
            epoch_id=epoch_id,
            adopted_sequence=adopted_sequence,
            nodes_export=nodes_export,
            account_scope_id=account_scope_id,
            ownership=ownership,
        )
        publisher = self._cache_view_publisher

        def commit() -> None:
            self._cache_view_snapshot_timestamp = snapshot_timestamp
            if clear_dirty:
                self._nodes_export_dirty_ids.clear()
            else:
                for dirty_id in discard_dirty:
                    self._nodes_export_dirty_ids.discard(dirty_id)

        last_error: Exception | None = None
        for _attempt in range(4):
            current = publisher.capture()
            offer = new_view
            merged = None
            if not is_unpopulated_view(current):
                # Seal item 1: account/epoch identity binding precedes the
                # equal-marker rule; "equal" means the COMPLETE identity.
                # Seal-2 item 2: when this client has a known account, the
                # CURRENT populated view must also be bound to it (never
                # null, never foreign) before any comparison.
                if (
                    client_account is not None
                    and current.account_scope_id != client_account
                ):
                    raise CoherenceAccountMismatchError(
                        "current populated view is not bound to this client's "
                        "account scope",
                        account_scope_id=client_account,
                        epoch_id=current.epoch_id,
                        sequence=current.adopted_sequence,
                    )
                _assert_lineage_identity_compatible(current, new_view)
                if (
                    current.epoch_number == new_view.epoch_number
                    and current.epoch_id == new_view.epoch_id
                    and current.adopted_sequence == new_view.adopted_sequence
                ):
                    # Seal item 3 (equal-marker rule): the current view is
                    # the cumulative local COW chain for this generation —
                    # an equal-marker lineage offer NEVER replaces it,
                    # never erases its COW overlays, never clears dirty
                    # state, never touches task-local snapshots.
                    return current
                if (current.epoch_number, current.adopted_sequence) > (
                    new_view.epoch_number,
                    new_view.adopted_sequence,
                ):
                    # Never regress a locally newer view with an older lineage.
                    return current
                # S03-CR-02 (sparse repair): a strict-newer lineage offer
                # replacing a local transition-COW chain must carry the
                # chain's verified local writes forward SPARSELY (per-field
                # authority — never whole-node, never silently erased) and
                # must never mask a later legitimate server value forever
                # (the merge's three-way discriminator + dirty evidence).
                effects = self._transition_effect_policy.snapshot_effects()
                if effects:
                    merged = merge_offer_with_transition_effects(
                        offer, effects, publication_kind=publication_kind
                    )
                    offer = merged.rebased
            effective_commit = commit
            if merged is not None:

                def effective_commit(_base=commit, _merged=merged) -> None:
                    # Inside the same critical section as the swap: run the
                    # lineage bookkeeping, then commit the merge outcome —
                    # the surviving effect store replaces the prior store
                    # and the merge's legacy-dirty evidence is marked AFTER
                    # the base clear/discard (post-cut dirt survives).
                    _base()
                    self._transition_effect_policy.commit_merge(
                        _merged.surviving_effects, _merged.dirty_ids
                    )

            try:
                return publisher.publish_with_commit(
                    offer, expected_current=current, commit=effective_commit
                )
            except CacheViewError as exc:  # stale CAS -> re-capture and retry
                last_error = exc
        raise CacheViewError(
            "_publish_lineage_view: publication raced beyond 4 attempts"
        ) from last_error

    # @beacon[
    #   id=coherence@client-capture-view-and-dirty,
    #   slice_labels=ra-coherence,ra-workflowy-cache,mci-cache-seams,
    #   kind=ast,
    #   comment=S03-B B1.1 welded view+dirty READ capture: ONE publisher-synchronized section returns (current view, frozenset(dirty_ids)) so an operation boundary never pins half-transitioned content/bookkeeping; legacy dirt retires in S16.,
    # ]
    def _capture_view_and_dirty(self) -> tuple[CacheView, frozenset]:
        """B1.1: ONE welded (current view, dirty-ID snapshot) read.

        Both halves are captured inside a SINGLE publisher-synchronized
        section, so an operation boundary never pins a view whose dirty-ID
        bookkeeping belongs to a different local transition (a reader must
        never observe content/dirty half-transitioned, §6.2).  The dirty
        set is copied out as an immutable frozenset; legacy dirt itself
        retires in S16.
        """

        def _read(current: CacheView) -> tuple[CacheView, frozenset]:
            return current, frozenset(self._nodes_export_dirty_ids)

        return self._cache_view_publisher.synchronized(_read)

    def _cache_populated(self) -> bool:
        """True iff the publisher's current view holds a populated cache.

        The single sanctioned cache-presence check for client-internal
        legacy eager-patch guards (H2-b/d adapter territory) — replaces the
        pre-S03 ``self._nodes_export_cache is (not) None`` property reads so
        no ``._nodes_export_cache`` reads survive outside the property.
        """

        return not is_unpopulated_view(self._cache_view_publisher.capture())

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore.publish_local_cache_view-uxs3,
    #   role=WorkFlowyClientCore.publish_local_cache_view,
    #   slice_labels=mci-cache-seams,
    #   kind=ast,
    #   comment=Delegate legacy patch transforms to the one COW bridge: detached changed fragments, unchanged nodes shared read-only, transferred build on every CAS attempt and atomic winning effect/dirty commit.,
    # ]
    def publish_local_cache_view(
        self,
        transform: Any,
        *,
        expected_view: CacheView | None = None,
        reason: str,
        commit: Any = None,
    ) -> CacheView:
        """S03 §6.2 transition COW bridge (client-bound).

        Runs one legacy eager-cache transform against private outer containers;
        the bridge detaches changed fragments and shares unchanged nodes, then
        atomically publishes the successor view with UNCHANGED adopted
        markers (see ``coherence.operation_cache.publish_local_cache_view``).
        Raises ``RuntimeError`` when no cache is populated — the exact
        legacy ``_get_nodes_export_cache_nodes`` failure the callers already
        catch and convert into dirty marking.
        """

        from ..coherence.operation_cache import publish_local_cache_view as _publish

        publisher = self._cache_view_publisher
        if is_unpopulated_view(publisher.capture()):
            raise RuntimeError("nodes-export cache is not initialized")

        def _commit_with_overlay(effects: Mapping[str, TransitionEffect]) -> None:
            # S03-CR-02 (sparse repair): weld the caller's legacy dirty
            # bookkeeping AND the sparse transition-effect accumulation
            # into the same critical section as the view swap (the bridge
            # derives the effects per attempt and supplies the WINNING
            # attempt's set; composition keeps earliest-prior /
            # latest-verified per field).
            if commit is not None:
                commit()
            if effects:
                self._transition_effect_policy.compose_into(effects)

        return _publish(
            publisher,
            transform,
            expected_view=expected_view,
            reason=reason,
            commit=_commit_with_overlay,
            snapshot_dirty_ids=lambda: frozenset(self._nodes_export_dirty_ids),
            # A2: this client's publisher IS the B1.2 authority token (the
            # exact identity _authority_identity derives at capture), so a
            # publication from a DIFFERENT client/publisher can never
            # rewrite this client's task-local snapshot on equal markers.
            publishing_authority=publisher,
        )

    def _coherence_paths(self):
        """Account-scoped coherence paths derived from the write governor.

        The governor already derives the exact account scope and jobs root
        from the client credentials through the authoritative chain, so the
        coherence subtree is guaranteed co-located with the audited
        governance artifacts.  Raw credentials are never persisted.
        """
        from ..coherence import coherence_paths

        gov_paths = self._write_governor.paths
        return coherence_paths(gov_paths.account_scope_id, jobs_root=gov_paths.jobs_root)

    def _coherence_boundary_status(self):
        """S04 §7.6: the operation-boundary boundary-status capture hook.

        One lock-free (complete-or-old) read of the hard barrier + external
        dirt through the ONE boundary-status authority.  Consumed by
        ``coherence.operation_cache.capture_operation_cache`` at each
        operation's adoption boundary; read-advisory shaping never queries
        the manifest mid-operation (H4).  Returns None when no manifest
        exists yet (pre-bootstrap).
        """
        from ..coherence.barrier import read_coherence_boundary_status

        return read_coherence_boundary_status(self._coherence_paths(), required=False)

    def _refresh_settle_seconds(self, override: float | None) -> float:
        """Resolve the refresh settle delay: explicit override, else config,
        else the S02 default (2.0s)."""
        if override is not None:
            return max(0.0, float(override))
        try:
            from ..config import get_server_config

            return float(get_server_config().timing.refresh_settle_seconds)
        except Exception:  # noqa: BLE001 - config-less test doubles
            return 2.0

    def _log_debug(self, message: str) -> None:
        """Log debug messages to stderr (unified logging)."""
        # Console Visibility ONLY - keep reconcile_debug.log clean for weaves
        log_event(message, "CLIENT_DEBUG")

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore._get_nodes_export_snapshot_dir-3t4u,
    #   role=WorkFlowyClientCore._get_nodes_export_snapshot_dir,
    #   slice_labels=ra-workflowy-cache,
    #   kind=ast,
    # ]
    def _get_nodes_export_snapshot_dir(self) -> str:
        """Return directory path for persisted /nodes-export cache snapshots.

        Resolves to a portable per-user runtime location via
        ``get_runtime_subdir('nodes_export_cache')`` (see ``config.py``).
        The directory is created on demand.
        """
        snapshot_dir = str(get_runtime_subdir("nodes_export_cache"))
        os.makedirs(snapshot_dir, exist_ok=True)
        return snapshot_dir

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore._save_nodes_export_cache_to_disk-6mk2,
    #   role=WorkFlowyClientCore._save_nodes_export_cache_to_disk,
    #   slice_labels=ra-workflowy-cache,
    #   kind=ast,
    # ]
    def _save_nodes_export_cache_to_disk(self) -> str:
        """RETIRED (S02 §11.1) — fail closed, always.

        The legacy whole-cache dump created LINEAGE-ORPHAN snapshots: files
        that could become authoritative without manifest lineage.  Snapshot
        candidate serialization now lives in
        ``coherence.snapshot_store.write_snapshot_candidate`` and ONLY
        ``coherence.epoch_publisher.publish_epoch`` may make a candidate the
        authoritative base.  No production caller may remain.
        """
        raise RuntimeError(
            "_save_nodes_export_cache_to_disk is retired (S02): an uncontextualized "
            "in-memory cache dump can never become authoritative lineage. Use the "
            "coherence refresh authority (refresh_nodes_export_cache)."
        )

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore._load_nodes_export_cache_from_disk-qd8q,
    #   role=WorkFlowyClientCore._load_nodes_export_cache_from_disk,
    #   slice_labels=ra-workflowy-cache,mci-epoch-seams,
    #   kind=ast,
    #   comment=Manifest-only warm start replays via the one indexed S2 workspace and explicitly transfers its owned materialization to local lineage adoption; existing bootstrap and error taxonomy remain.,
    # ]
    def _load_nodes_export_cache_from_disk(self) -> bool:
        """Manifest-aware warm start (S02 §10.2/§10.3).

        Once a coherence manifest exists, ONLY its referenced base (plus the
        active journal tail) may warm-start this process — never a
        newest-timestamp file.  When no manifest exists, the one-time §10.1
        bootstrap adopts the newest USABLE legacy wrapper (or initializes a
        bootstrap manifest).  Malformed/mismatched lineage is recorded
        distinctly in ``_nodes_export_lineage_error`` and returns False —
        never a silent timestamp fallback.
        S2 returns private owned containers, surrendered to lineage adoption.
        """
        try:
            paths = self._coherence_paths()
        except Exception as exc:  # noqa: BLE001 - no credentials/config in test doubles
            self._log_debug(f"Warm start: coherence paths unavailable: {exc}")
            return False
        try:
            from ..coherence.refresh_authority import bootstrap_coherence_from_legacy
            from ..coherence.schema import read_manifest
            from ..coherence.snapshot_store import materialize_current_payload

            manifest = read_manifest(paths, required=False)
            if manifest is None:
                manifest = bootstrap_coherence_from_legacy(
                    paths, snapshot_dir=self._get_nodes_export_snapshot_dir()
                )
            if manifest.base_snapshot_path is None:
                # Bootstrap state: no base — ordinary cache-missing.
                return False
            view = materialize_current_payload(paths, manifest)
        except Exception as exc:  # noqa: BLE001 - recorded distinctly, never raised
            self._nodes_export_lineage_error = f"{type(exc).__name__}: {exc}"
            self._log_debug(
                f"Warm start: coherence lineage fault (no timestamp fallback): {exc}"
            )
            return False

        self._publish_lineage_view(
            view.payload,
            epoch_number=view.epoch_number,
            epoch_id=view.epoch_id,
            adopted_sequence=view.adopted_sequence,
            snapshot_timestamp=view.snapshot_timestamp,
            account_scope_id=manifest.account_scope_id,
            clear_dirty=True,
            ownership="transferred",
        )
        self._nodes_export_lineage_error = None
        self._log_debug(
            f"Loaded /nodes-export cache from manifest base {view.base_snapshot_path} "
            f"(epoch {view.epoch_number}, adopted seq {view.adopted_sequence})"
        )
        return True

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore._gc_nodes_export_snapshots-gc1a,
    #   role=WorkFlowyClientCore._gc_nodes_export_snapshots,
    #   slice_labels=ra-workflowy-cache,
    #   kind=ast,
    # ]
    def _gc_nodes_export_snapshots(
        self,
        max_keep: int = 3,
        min_age_seconds: int = 3600,
    ) -> None:
        """Best-effort garbage collection for /nodes-export snapshots on disk.

        Keeps at least the newest `max_keep` snapshots and only deletes files
        older than `min_age_seconds`. Failures are logged but never raised.
        """
        try:
            self._gc_nodes_export_snapshots_diagnostic(
                max_keep=max_keep, min_age_seconds=min_age_seconds
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, never raised
            self._log_debug(f"_gc_nodes_export_snapshots failed: {exc}")

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore._gc_nodes_export_snapshots_diagnostic-0akl,
    #   role=WorkFlowyClientCore._gc_nodes_export_snapshots_diagnostic,
    #   slice_labels=mci-epoch-seams,
    #   kind=ast,
    #   comment=Protect manifest bases and the immutable active-candidate projection under existing age/count policy; no job-basis or publication-ownership file is read.,
    # ]
    def _gc_nodes_export_snapshots_diagnostic(
        self,
        max_keep: int = 3,
        min_age_seconds: int = 3600,
    ) -> dict[str, Any]:
        """Manifest-aware snapshot GC with a diagnostic return (S02 §11.2).

        Protected (never deleted, regardless of age): every account
        manifest's referenced active base and active memory candidates.
        A malformed/unreadable manifest
        aborts the sweep
        conservatively (nothing is deleted).  Unreferenced candidates remain
        subject to the ordinary max-count/min-age rules.  GC never edits
        manifests, journals, job JSON, or external dirt.
        """
        result: dict[str, Any] = {
            "deleted": [],
            "protected": [],
            "kept_recent": [],
            "skipped_young": [],
            "aborted": False,
            "errors": [],
        }
        snapshot_dir = self._get_nodes_export_snapshot_dir()
        try:
            names = [
                name
                for name in os.listdir(snapshot_dir)
                if name.endswith("_nodes_export_snapshot.json")
            ]
        except FileNotFoundError:
            return result

        if len(names) <= max_keep:
            result["kept_recent"] = sorted(names)
            return result

        names.sort()
        recent: set[str] = set(names[-max_keep:])

        # Manifest-aware protected set (all accounts under this jobs root).
        try:
            from ..coherence.refresh_authority import _active_candidate_snapshot_paths
            from ..coherence.snapshot_store import (
                collect_protected_snapshot_paths,
                normalize_snapshot_path,
            )

            protected_paths, gc_diag = collect_protected_snapshot_paths(
                self._write_governor.paths.jobs_root,
                active_candidate_paths=_active_candidate_snapshot_paths(
                    self._write_governor.paths.jobs_root
                ),
            )
            if gc_diag.get("abort"):
                result["aborted"] = True
                result["errors"] = list(gc_diag.get("errors") or [])
                return result
        except Exception as exc:  # noqa: BLE001 - conservative no-delete
            result["aborted"] = True
            result["errors"].append(f"protected-set computation failed: {exc}")
            return result

        now_ts = datetime.now().timestamp()
        for name in names:
            path = os.path.join(snapshot_dir, name)
            if name in recent:
                result["kept_recent"].append(name)
                continue
            if normalize_snapshot_path(path) in protected_paths:
                result["protected"].append(name)
                continue
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue
            age = now_ts - st.st_mtime
            if age < min_age_seconds:
                result["skipped_young"].append(name)
                continue
            try:
                os.remove(path)
                result["deleted"].append(name)
            except OSError:
                # Best-effort only; the OS may refuse to delete open files.
                continue
        return result

    @staticmethod
    def _segment_whitelisted_markup(text: str) -> list[dict[str, str]]:
        """Segment text into plain-text vs whitelisted markup ranges.

        Only *properly matched* whitelisted tags are treated as markup:
        - <b>..</b>, <i>..</i>, <s>..</s>, <code>..</code> with NO attributes
        - <span class="colored c-...">..</span> (exact class pattern)

        Any unmatched tags, unknown tags, or spans with other attributes are
        treated as plain text and will be escaped by the normal whitener.

        Returns a list of segments like:
            [{"kind": "text", "value": "..."},
             {"kind": "markup", "value": "<b>foo</b>"}, ...]
        """
        import re

        if not text:
            return [{"kind": "text", "value": text}]

        tag_pattern = re.compile(r'<(/?)(span|code|b|i|s)([^>]*)>', re.IGNORECASE)
        stack: list[dict[str, object]] = []
        candidates: list[tuple[int, int]] = []

        for m in tag_pattern.finditer(text):
            slash, tag, attrs = m.group(1), m.group(2).lower(), m.group(3) or ""
            start, end = m.start(), m.end()

            if slash:  # closing tag
                if not stack:
                    continue
                top = stack[-1]
                if top["tag"] != tag:
                    continue
                top = stack.pop()
                if top.get("allowed"):
                    # Record full range from opening '<tag>' start to closing '</tag>' end
                    candidates.append((int(top["start"]), end))
            else:  # opening tag
                allowed = False
                attrs_stripped = attrs.trim() if hasattr(attrs, 'trim') else attrs.strip()
                if tag in {"b", "i", "s", "code"}:
                    # Only accept markup if there are *no* attributes
                    allowed = attrs_stripped == ""
                elif tag == "span":
                    # Allow any span that carries a 'colored' class; precise color
                    # matching / auto-wrap is handled later in the whitening stage.
                    if "colored" in attrs_stripped:
                        allowed = True
                stack.append({"tag": tag, "start": start, "allowed": allowed})

        if not candidates:
            segments = [{"kind": "text", "value": text}]
        else:
            # Reduce to non-overlapping outermost ranges
            candidates.sort(key=lambda r: r[0])
            merged: list[tuple[int, int]] = []
            last_end = -1
            for start, end in candidates:
                if start >= last_end:
                    merged.append((start, end))
                    last_end = end
                else:
                    # Nested or overlapping range; outermost already captured, so skip
                    continue

            segments: list[dict[str, str]] = []
            pos = 0
            for start, end in merged:
                if start > pos:
                    segments.append({"kind": "text", "value": text[pos:start]})
                segments.append({"kind": "markup", "value": text[start:end]})
                pos = end
            if pos < len(text):
                segments.append({"kind": "text", "value": text[pos:]})

        return segments

    @staticmethod
    def _extract_whitener_mode(value: str | None) -> tuple[str | None, str | None]:
        """Detect and strip a per-value WORKFLOWY_WHITENER_MODE directive.

        STRICT LEADING-PREFIX DIRECTIVE (Dan, 2026-06)
        ----------------------------------------------
        This is a debug/A-B escape hatch, used effectively never. It MUST be
        impossible to trip over accidentally, because a false match here
        bypasses the whitener (mode=raw) and ships un-whitened, HTML-significant
        bytes straight to Workflowy — which blanks/breaks the node's render.
        That is exactly the bug this strict form fixes: the directive now fires
        ONLY when the value *begins* with the token (after optional leading
        whitespace), mirroring the sibling ``<<<LITERAL_BACKSLASH_N_INTENTIONAL>>>``
        ``startswith`` hatch in ``encode_for_workflowy_api``.

        Recognized form (must be the LEADING token of the value)::

            WORKFLOWY_WHITENER_MODE=<mode><rest of value>

        Recognized modes:
            raw    – bypass whitener for this value only
            normal – explicit no-op (whitener still runs)

        Behavior:
            - If the value does NOT start with the directive, it is returned
              COMPLETELY UNTOUCHED — mid-prose mentions of the token (e.g. in
              documentation about the encoder) are never matched or stripped.
            - If it DOES start with the directive, only that leading directive
              token is stripped; the remaining value is returned for normal
              encoding (or bypassed when mode=raw).

        Returns:
            (mode, cleaned_value)
            mode is None when no leading directive is present (or the mode is
            unrecognized); cleaned_value is the original value with the leading
            directive removed (or the original value verbatim when absent).
        """
        if value is None:
            return (None, None)

        import re

        # Anchored at the START of the value (after optional leading
        # whitespace) only. NOT a search-anywhere match: prose that merely
        # MENTIONS the token is left byte-for-byte intact.
        directive = re.compile(
            r"^\s*WORKFLOWY_WHITENER_MODE\s*=\s*([A-Za-z0-9_\-]+)\s*"
        )
        m = directive.match(value)
        if m is None:
            # No leading directive -> return the value untouched.
            return (None, value)

        mode = m.group(1).strip().lower()
        # Strip ONLY the leading directive token; preserve the rest verbatim.
        cleaned = value[m.end():]

        if mode not in {"raw", "normal"}:
            # Unrecognized leading directive: no special behavior, but still
            # remove the directive token so it doesn't leak into Workflowy.
            return (None, cleaned)

        return (mode, cleaned)

    # ------------------------------------------------------------------
    # Encoding for Workflowy API (replaces the legacy _validate_*_field
    # functions that mixed validation with non-idempotent encoding).
    #
    # ARCHITECTURE (Dan, 2026-05):
    #
    # Workflowy's NAME and NOTE fields require DIFFERENT encoding rules
    # to survive the API + GUI decode pipeline. The old _validate_name_field
    # / _validate_note_field functions did both validation and encoding in
    # a single non-idempotent operation, which led to subtle bugs where
    # different code paths (ETCH vs F12 forward-pass) encoded different
    # numbers of times and produced different on-wire bytes.
    #
    # The new architecture separates concerns cleanly:
    #
    #   1. _validate_name(name)             -> (ok, error)   PURE VALIDATION
    #   2. _validate_note(note, ...)        -> (ok, error)   PURE VALIDATION
    #   3. encode_for_workflowy_api(text, field, operation=...) -> str | None
    #                                                   ONE-WAY ENCODER
    #
    # The encoder is called EXACTLY ONCE per value, inside create_node
    # and update_node (the only places that POST to Workflowy). Upstream
    # callers (ETCH, F12 reconcile wrappers, etc.) pass semantic text
    # (i.e., the text the user wants displayed in the GUI) and never
    # call the encoder themselves.
    #
    # This makes the encoder a true one-way function from semantic text
    # to wire format. It is NOT idempotent by design: calling it twice
    # would double-encode. The architectural invariant is enforced by
    # call-site discipline, not by recursive decoding inside the encoder
    # (which would prevent users from authoring literal entity text like
    # documentation about HTML).
    #
    # Per-field / per-operation encoding rules (empirically verified):
    #
    #   NAME field on CREATE (double-decode pipeline: API + GUI both decode once):
    #     - Whitelisted markup tags preserved verbatim.
    #     - Literal '&' -> '&amp;amp;'  (double-encoded)
    #     - Literal '<' -> '&amp;lt;'   (double-encoded)
    #     - Literal '>' -> '&amp;gt;'   (double-encoded)
    #
    #   NAME field on UPDATE (single-decode pipeline: GUI decodes once):
    #     - Whitelisted markup tags preserved verbatim.
    #     - Literal '&' -> '&amp;'  (single-encoded)
    #     - Literal '<' -> '&lt;'   (single-encoded)
    #     - Literal '>' -> '&gt;'   (single-encoded)
    #
    #   NOTE field on CREATE or UPDATE (single-decode pipeline: only GUI decodes):
    #     - Whitelisted markup tags preserved verbatim.
    #     - Literal '<' -> '&lt;'  (single-encoded)
    #     - Literal '>' -> '&gt;'  (single-encoded)
    #     - Literal '&' -> '&'     (NOT encoded; Workflowy GUI fixed the
    #                              over-render bug for raw '&' in notes.
    #                              Confirmed via test etch/update May 2026.)
    #
    # Escape hatches preserved from the legacy validators:
    #   - 'WORKFLOWY_WHITENER_MODE=raw' token bypasses the encoder entirely.
    #   - '<<<LITERAL_BACKSLASH_N_INTENTIONAL>>>' prefix on note allows
    #     literal newline characters in a way the legacy validator tolerated;
    #     stripped before encoding so the remaining text is encoded normally.
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_name(name: str | None) -> tuple[bool, str | None]:
        """Pure validation for Workflowy NAME field. No encoding.

        Returns (ok, error_message). When ok=True, error_message is None.
        """
        if name is None:
            return (True, None)
        if not isinstance(name, str):
            return (False, f"Name must be a string, got {type(name).__name__}")
        if not name.strip():
            return (False, "Name must be a non-empty string")
        return (True, None)

    @staticmethod
    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore._validate_note-5msj,
    #   role=WorkFlowyClientCore._validate_note,
    #   slice_labels=node-ops,
    #   kind=ast,
    # ]
    def _validate_note(
        note: str | None,
        skip_newline_check: bool = False,
    ) -> tuple[bool, str | None]:
        """Pure validation for Workflowy NOTE field. No encoding.

        Returns (ok, error_message). When ok=True, error_message is None.

        skip_newline_check is reserved for future newline-policy enforcement;
        currently a no-op (the legacy validator also did not enforce it).
        """
        if note is None:
            return (True, None)
        if not isinstance(note, str):
            return (False, f"Note must be a string, got {type(note).__name__}")
        return (True, None)

    @staticmethod
    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore.encode_for_workflowy_api-x7tw,
    #   role=WorkFlowyClientCore.encode_for_workflowy_api,
    #   slice_labels=node-ops,
    #   kind=ast,
    # ]
    def encode_for_workflowy_api(
        text: str | None,
        field: str,
        *,
        operation: str = "create",
    ) -> str | None:
        """One-way encoder from semantic text to Workflowy API wire format.

        Args:
            text: Semantic text (the text the user wants displayed in GUI),
                or None.
            field: Either 'name' or 'note'. Selects the field family.
            operation: Either 'create' or 'update'. Workflowy's NAME field
                empirically has different decode depth on create vs update;
                NOTE uses the same rule for both operations.

        Returns:
            Encoded wire-format string, or None if input was None.

        IMPORTANT: Call this EXACTLY ONCE per value, in create_node /
        update_node. Calling it twice will double-encode and produce
        incorrect output. Do not call from ETCH pre-validation, F12
        reconcile wrappers, or anywhere else upstream of the API POST.
        """
        if text is None:
            return None
        if field not in ("name", "note"):
            raise ValueError(
                f"encode_for_workflowy_api: unknown field {field!r} "
                f"(expected 'name' or 'note')"
            )
        if operation not in ("create", "update"):
            raise ValueError(
                f"encode_for_workflowy_api: unknown operation {operation!r} "
                f"(expected 'create' or 'update')"
            )

        # Escape hatch 1: WORKFLOWY_WHITENER_MODE=raw bypasses encoding.
        mode, cleaned = WorkFlowyClientCore._extract_whitener_mode(text)
        if mode == "raw":
            return cleaned
        if cleaned is not None:
            text = cleaned

        # Escape hatch 2 (note only): <<<LITERAL_BACKSLASH_N_INTENTIONAL>>>
        # prefix stripped before encoding so the literal text behind it is
        # encoded normally. Legacy code stripped this AFTER the validator;
        # we now strip it before the encoder for clarity.
        literal_backslash_n_sentinel = "<<<LITERAL_BACKSLASH_N_INTENTIONAL>>>"
        if field == "note" and text.startswith(literal_backslash_n_sentinel):
            text = text[len(literal_backslash_n_sentinel):]

        # Segment into whitelisted markup vs plain text.
        segments = WorkFlowyClientCore._segment_whitelisted_markup(text)

        # Auto-wrap bare colored spans with <b>...</b> (legacy behavior
        # preserved from the old validators).
        import re
        wrapped_segments = []
        for seg in segments:
            if seg["kind"] == "markup":
                v = seg["value"]
                if re.fullmatch(
                    r'<span\s+class="colored\s+(?:c|bc)-[^"]+">.*?</span>',
                    v,
                ):
                    seg = {"kind": "markup", "value": f"<b>{v}</b>"}
            wrapped_segments.append(seg)
        segments = wrapped_segments

        # Apply per-field encoding rule. Plain-text segments are encoded
        # directly; markup segments have their OUTER opening/closing tag
        # bytes preserved verbatim (so formatting like <code>/<b>/<i>/<s>/
        # colored <span> still renders), while their INNER content is
        # recursively re-segmented and encoded by the same rules. This
        # ensures e.g. <code>x < y</code> ships as <code>x &lt; y</code>
        # for the NOTE field (preserving the code formatting but escaping
        # the inner raw '<' that would otherwise cause Workflowy's note
        # renderer to blank the entire note).
        return WorkFlowyClientCore._render_segments(
            segments,
            field,
            operation=operation,
        )

    @staticmethod
    def _render_segments(
        segments: list[dict[str, str]],
        field: str,
        *,
        operation: str,
    ) -> str:
        """Render a list of {kind, value} segments to wire-format text.

        Plain-text segments get the per-field/per-operation escape rule
        applied. Markup segments have their outer opening/closing tag
        preserved verbatim and their inner content recursively re-segmented
        + rendered, so raw '<' / '>' (and '&' for NAME) inside e.g.
        <code>...</code> get escaped without disturbing the surrounding
        markup.
        """
        import re

        result_chars: list[str] = []

        def encode_plain(s: str) -> str:
            chars: list[str] = []
            # NAME is single-line by contract. A Workflowy node name is always
            # ONE node -- a heading, an AST signature, etc. Workflowy's create
            # endpoint, however, treats embedded newlines in a name as a paste
            # affordance and SPLITS the name into a parent node (first line)
            # plus one child node per remaining line. That UI behavior must
            # never be triggered implicitly by an MCP tool call. So for the
            # NAME field we collapse every line break (LF, CR, CRLF, and the
            # Unicode line/paragraph separators) to a single space before
            # encoding. This is a universal safety net: no matter which
            # upstream producer (JS/TS parser, paste, agent) emits a
            # newline-bearing name, it can no longer explode into child nodes.
            # The NOTE field is intentionally left untouched -- notes are
            # genuinely multi-line.
            if field == "name":
                s = " ".join(s.split("\n")) if "\n" in s else s
                for _nl in ("\r", "\u2028", "\u2029", "\v", "\f"):
                    if _nl in s:
                        s = s.replace(_nl, " ")
            if field == "name" and operation == "create":
                # NAME create: double-encode <, >, & to survive API+GUI decode.
                for ch in s:
                    if ch == '&':
                        chars.append('&amp;amp;')
                    elif ch == '<':
                        chars.append('&amp;lt;')
                    elif ch == '>':
                        chars.append('&amp;gt;')
                    else:
                        chars.append(ch)
            elif field == "name" and operation == "update":
                # NAME update: single-encode <, >, & (update endpoint has
                # one fewer decode than create; verified via Workflowy GUI).
                for ch in s:
                    if ch == '&':
                        chars.append('&amp;')
                    elif ch == '<':
                        chars.append('&lt;')
                    elif ch == '>':
                        chars.append('&gt;')
                    else:
                        chars.append(ch)
            else:
                # NOTE create/update: single-encode < and >.
                #
                # '&' handling (Dan, 2026-05, &nbsp; blank-note fix):
                #   A *bare* '&' (e.g. "Tom & Jerry") is left raw -- Workflowy
                #   fixed the over-render bug for bare '&' in notes, and leaving
                #   it raw keeps the on-wire bytes minimal and round-trips
                #   cleanly.
                #
                #   BUT a '&' that begins a recognizable HTML entity
                #   (&name; / &#nnn; / &#xhhh;) -- e.g. '&nbsp;', '&copy;',
                #   '&mdash;' -- is a LIVE entity to Workflowy's note renderer.
                #   A live entity (especially '&nbsp;') inside an otherwise
                #   HTML-ambiguous note body (e.g. a Markdown table) makes the
                #   renderer treat the whole note as HTML and BLANK the entire
                #   note field (no clickable note at all). So we escape ONLY the
                #   leading '&' of an entity to '&amp;', which Workflowy renders
                #   back as the literal text (e.g. shows '&nbsp;') -- visible,
                #   correct, non-blanking. The reverse path (clean_html_entities
                #   in markdown_roundtrip.py) decodes a narrow whitelist
                #   (&amp;/&lt;/&gt;/&quot; plus numeric double-quote spellings), so
                #   '&amp;nbsp;' -> '&nbsp;' on F12+3 write-back, byte-identical
                #   to the original Markdown source on disk.
                _entity_re = re.compile(
                    r'&(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#[xX][0-9A-Fa-f]+);'
                )
                i = 0
                n = len(s)
                while i < n:
                    ch = s[i]
                    if ch == '<':
                        chars.append('&lt;')
                        i += 1
                    elif ch == '>':
                        chars.append('&gt;')
                        i += 1
                    elif ch == '&':
                        # Escape the '&' only if it starts an HTML entity.
                        if _entity_re.match(s, i):
                            chars.append('&amp;')
                        else:
                            chars.append('&')
                        i += 1
                    else:
                        chars.append(ch)
                        i += 1
            return ''.join(chars)

        # Matches the opening/closing pair for any whitelisted markup
        # segment. Group 1 is the opening tag (verbatim bytes), group 2
        # is the inner content, group 3 is the closing tag (verbatim).
        markup_split = re.compile(
            r'^(<(?:b|i|s|code)>|<span\s+class="colored[^"]*">)'
            r'(.*)'
            r'(</(?:b|i|s|code|span)>)$',
            re.IGNORECASE | re.DOTALL,
        )

        for seg in segments:
            if seg["kind"] != "markup":
                result_chars.append(encode_plain(seg["value"]))
                continue

            m = markup_split.match(seg["value"])
            if not m:
                # Defensive: segmenter said this was markup but the shape
                # doesn't match what we expect. Treat as plain text rather
                # than risk shipping raw '<' to Workflowy.
                result_chars.append(encode_plain(seg["value"]))
                continue

            open_tag, inner, close_tag = m.group(1), m.group(2), m.group(3)
            inner_segments = WorkFlowyClientCore._segment_whitelisted_markup(
                inner
            )
            result_chars.append(open_tag)
            result_chars.append(
                WorkFlowyClientCore._render_segments(
                    inner_segments,
                    field,
                    operation=operation,
                )
            )
            result_chars.append(close_tag)

        return ''.join(result_chars)

    @staticmethod
    def _dewhiten_text(value: str | None) -> str | None:
        """Single-pass dewhitening for SCRY (/nodes-export) names and notes.

        Decodes &amp;, &lt;, &gt; once to recover semantic text. We *only* touch
        these three entities so that other entities (e.g. &nbsp;) remain literal.

        SINGLE-SOURCE (W1/F-1): the implementation lives in
        ``workflowy_mcp.coherence.canonical_node._dewhiten_text``; this seam
        delegates verbatim.  Do NOT re-implement here.
        """
        return _coherence_dewhiten_text(value)

    @property
    def client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            headers = {
                "Authorization": f"Bearer {self.config.api_key.get_secret_value()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self.config.timeout),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "WorkFlowyClientCore":
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    def set_operation_cancel_callback(self, callback: Any | None) -> None:
        """Client-level cancellation probe retained for ordinary API callers.

        A daemon task using the shared publisher uses operation_cancel_scope
        with its own user probe, never this shared setter. Ordinary MCP/API
        calls leave it unset, preserving the existing retry behavior.
        """
        self._operation_cancel_callback = callback

    def _operation_cancel_requested(self) -> bool:
        """Consult client-level and task-local probes without sharing job state."""

        callbacks = (
            getattr(self, "_operation_cancel_callback", None),
            _AMBIENT_OPERATION_CANCEL_PROBE.get(),
        )
        for callback in callbacks:
            if not callable(callback):
                continue
            try:
                if bool(callback()):
                    return True
            except Exception:
                # Cancellation remains best-effort: one transient probe read
                # failure must not abort an otherwise valid API operation.
                continue
        return False

    def _raise_if_operation_cancelled(self, operation: str) -> None:
        if self._operation_cancel_requested():
            raise OperationCancelledError(operation)

    async def _sleep_with_operation_cancel(
        self, seconds: float, operation: str
    ) -> None:
        """Sleep in short slices so CARTO cancellation interrupts backoff promptly."""
        import asyncio

        delay = max(0.0, float(seconds or 0.0))
        has_probe = callable(
            getattr(self, "_operation_cancel_callback", None)
        ) or callable(_AMBIENT_OPERATION_CANCEL_PROBE.get())
        if not has_probe:
            await asyncio.sleep(delay)
            return

        self._raise_if_operation_cancelled(operation)
        remaining = delay
        while remaining > 0:
            step = min(0.25, remaining)
            await asyncio.sleep(step)
            remaining -= step
            self._raise_if_operation_cancelled(operation)

    # @beacon[
    #   id=carto-governance@write-send-authority,
    #   slice_labels=ra-carto-jobs,nexus-core-v1,
    #   kind=ast,
    #   comment=The sole mutating HTTP authority: archive guard then ticket then one preflight then breaker/cancellation recheck and one non-redirecting send. Optional caller-owned dispatch evidence is set only at HTTP task creation; 3xx is non-retrying. Retains WFB/probe/429 outcomes.,
    # ]
    async def _send_workflowy_write(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        diagnostic_label: str | None = None,
        diagnostic_context: str = "",
        before_send: Callable[[], Awaitable[None]] | None = None,
        _send_stage: Any = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Acquire, preflight, recheck, record optional dispatch evidence, send once.

        No redirect replay. The caller-owned stage is untouched before dispatch.
        """

        # Archive Reads (§4.7.1): write guard #1 -- the sole mutating HTTP
        # transport refuses loudly when the running operation is augmented
        # (archive-including).  The guard can only fire on a bug: no write
        # operation ever carries an overlay.  Never caught; it propagates to
        # the dispatcher's operation_failed.
        from ..archive_context import archive_write_guard

        archive_write_guard(operation or "_send_workflowy_write")

        import asyncio
        import time

        governor = self._write_governor

        def cancel_check() -> bool:
            self._raise_if_operation_cancelled(operation)
            return False

        ticket = await governor.acquire_write_slot(
            operation,
            sleep_fn=self._sleep_with_operation_cancel,
            cancel_check=cancel_check,
        )
        if before_send is not None:
            await before_send()
        await governor.recheck_before_send(ticket, cancel_check=cancel_check)

        logger = _ClientLogger()
        started = time.monotonic()
        kwargs["follow_redirects"] = False
        if _send_stage is not None:
            _send_stage.dispatch_started = True
        send_task = asyncio.create_task(self.client.request(method, url, **kwargs))
        try:
            if diagnostic_label is None:
                response = await send_task
            else:
                while True:
                    done, _pending = await asyncio.wait({send_task}, timeout=10.0)
                    if done:
                        response = await send_task
                        break
                    elapsed = time.monotonic() - started
                    context = f" {diagnostic_context}" if diagnostic_context else ""
                    logger.warning(
                        f"{diagnostic_label}: still waiting elapsed={elapsed:.1f}s{context}"
                    )
        except BaseException as exc:
            if not send_task.done():
                send_task.cancel()
                try:
                    await send_task
                except BaseException:
                    pass
            probe_outcome = None
            if isinstance(exc, httpx.TimeoutException):
                probe_outcome = "timeout"
            elif isinstance(exc, httpx.TransportError):
                probe_outcome = "network_error"
            if ticket.is_probe and probe_outcome is not None:
                try:
                    await governor.record_probe_result(
                        ticket.probe_claim_id,
                        probe_outcome,
                        reason=f"recovery probe {probe_outcome}",
                        evidence_excerpt=str(exc),
                        cancel_check=cancel_check,
                    )
                except Exception as transition_exc:
                    logger.error(
                        "Failed to persist recovery-probe transport outcome "
                        f"{probe_outcome}: {transition_exc}"
                    )
            raise

        elapsed = time.monotonic() - started
        if diagnostic_label is not None and elapsed >= 1.0:
            context = f" {diagnostic_context}" if diagnostic_context else ""
            logger.warning(
                f"{diagnostic_label}: completed elapsed={elapsed:.2f}s "
                f"status={getattr(response, 'status_code', None)}{context}"
            )

        status = int(response.status_code)
        body_preview = ""
        if status == 403:
            try:
                body_preview = str(response.text or "").strip()[:1000]
            except Exception:
                body_preview = ""

        if 200 <= status < 300:
            if ticket.is_probe:
                await governor.record_probe_result(
                    ticket.probe_claim_id,
                    "success",
                    reason="matching recovery probe succeeded",
                    cancel_check=cancel_check,
                )
            return response

        if status == 429:
            raw_retry_after = response.headers.get("Retry-After")
            parsed = parse_retry_after_header(
                raw_retry_after,
                now_epoch=governor.now_epoch(),
                fallback_seconds=governor.settings.retry_after_fallback_seconds,
            )
            delay = float(parsed["delay_seconds"])
            if ticket.is_probe:
                transition = await governor.record_probe_result(
                    ticket.probe_claim_id,
                    "rate_limited",
                    retry_after_seconds=delay,
                    reason="matching recovery probe received 429; WFB recovered",
                    cancel_check=cancel_check,
                )
                resulting_deadline = transition.next_slot_at
            else:
                resulting_deadline = await governor.defer_global_writes_until(
                    delay,
                    operation=operation,
                    cancel_check=cancel_check,
                )
            deadline_text = (
                datetime.fromtimestamp(resulting_deadline, tz=timezone.utc).isoformat()
                if resulting_deadline is not None
                else "stale-probe-token-no-transition"
            )
            logger.warning(
                "Workflowy write 429 account defer: "
                f"raw_retry_after={raw_retry_after!r} parsed_delay={delay:g}s "
                f"used_fallback={bool(parsed['used_fallback'])} "
                f"diagnostic={parsed['diagnostic']!r} next_slot_utc={deadline_text} "
                f"account_scope={governor.paths.account_scope_id[:8]} "
                f"operation={operation}"
            )
            return response

        if status == 403 and "WFB(3)" in body_preview.upper():
            transition = await governor.trip_wfb(
                probe_claim_id=ticket.probe_claim_id,
                reason="Workflowy account write block WFB(3)",
                evidence_excerpt=body_preview,
                cancel_check=cancel_check,
            )
            blocked_until = float(transition.breaker["blocked_until"])
            retry_after = max(0.0, blocked_until - governor.now_epoch())
            blocked_utc = datetime.fromtimestamp(
                blocked_until, tz=timezone.utc
            ).isoformat()
            if ticket.is_probe:
                reason = (
                    "your write was the recovery probe; the account is still "
                    f"blocked until {blocked_utc}"
                )
            else:
                reason = f"account write-block detected; writes suspended until {blocked_utc}"
            raise AccountWriteBlockedError(
                retry_after=retry_after,
                blocked_until=blocked_until,
                reason=reason,
                message=reason,
            )

        if 400 <= status < 500:
            if status == 403:
                logger.error(
                    "Workflowy write returned non-WFB 403; breaker body-pattern "
                    f"drift tripwire operation={operation} response={body_preview!r}"
                )
            if ticket.is_probe:
                await governor.record_probe_result(
                    ticket.probe_claim_id,
                    "client_error",
                    reason=f"matching recovery probe received ordinary {status}",
                    evidence_excerpt=body_preview,
                    cancel_check=cancel_check,
                )
            return response

        if status >= 500:
            if ticket.is_probe:
                await governor.record_probe_result(
                    ticket.probe_claim_id,
                    "server_error",
                    reason=f"matching recovery probe received server error {status}",
                    evidence_excerpt=body_preview,
                    cancel_check=cancel_check,
                )
            return response

        if 300 <= status < 400:
            raise APIResponseError(status, f"Workflowy write redirect refused: {status}")

        return response

    # @beacon-close[
    #   id=carto-governance@write-send-authority,
    # ]

    # @beacon[
    #   id=carto-safety@bounded-crud-retry,
    #   slice_labels=ra-carto-jobs,nexus-core-v1,
    #   kind=ast,
    #   comment=Shared bounded selector for all eight CRUD loops: configured caller-wait ceiling, valid zero retains exponential local backoff, and excessive server delays re-raise promptly.,
    # ]
    def _bounded_rate_limit_retry_delay(
        self,
        error: RateLimitError,
        exponential_delay: float,
        operation: str,
    ) -> float:
        """Select a local retry delay without permitting multi-hour callers."""

        retry_after = getattr(error, "retry_after", None)
        if retry_after is None or retry_after == 0:
            return max(0.0, float(exponential_delay))

        delay = max(0.0, float(retry_after))
        ceiling = float(
            getattr(
                getattr(self, "_write_governor", None),
                "settings",
                WriteGovernorSettings(),
            ).max_wait_before_refusal_seconds
        )
        if delay > ceiling:
            _ClientLogger().warning(
                f"{operation}: server Retry-After {delay:g}s exceeds the "
                f"{ceiling:g}s local caller-wait ceiling; re-raising without sleeping."
            )
            raise error
        return delay

    async def _handle_response(self, response: httpx.Response) -> dict[str, Any]:
        """Handle API response and errors."""
        if response.status_code == 401:
            raise AuthenticationError("Invalid API key or unauthorized access")

        if response.status_code == 404:
            raise NodeNotFoundError(
                node_id=response.request.url.path.split("/")[-1], message="Resource not found"
            )

        if response.status_code == 429:
            raw_retry_after = response.headers.get("Retry-After")
            parsed_retry_after = parse_retry_after_header(
                raw_retry_after,
                fallback_seconds=float(
                    getattr(
                        getattr(self, "_write_governor", None),
                        "settings",
                        WriteGovernorSettings(),
                    ).retry_after_fallback_seconds
                ),
            )
            retry_after = float(parsed_retry_after["delay_seconds"])
            if parsed_retry_after["used_fallback"]:
                _ClientLogger().warning(
                    "Workflowy 429 carried a missing or malformed Retry-After; "
                    f"raw={raw_retry_after!r} diagnostic={parsed_retry_after['diagnostic']!r}; "
                    f"using the configured conservative {retry_after:g}s fallback."
                )
            raise RateLimitError(retry_after=retry_after)

        if response.status_code >= 500:
            raise NetworkError(f"Server error: {response.status_code}")

        if response.status_code >= 400:
            body_preview = ""
            try:
                body_preview = str(response.text or "").strip()[:1000]
            except Exception:
                body_preview = ""

            message = ""
            try:
                error_data = response.json()
                if isinstance(error_data, dict):
                    message = str(
                        error_data.get("error")
                        or error_data.get("message")
                        or error_data.get("detail")
                        or ""
                    ).strip()
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                message = ""

            if not message:
                message = f"API error: {response.status_code}"
                if body_preview:
                    message += f"; response={body_preview}"

            # Ordinary 4xx responses are request/auth/permission failures, not
            # transport failures. They must fail immediately instead of entering
            # the NetworkError exponential-backoff loops (10 attempts previously
            # meant ~17 minutes, and incremental F12 could then repeat the same
            # wait in its legacy fallback). 401/404/429 retain their specialized
            # exception types above; 5xx remains retryable NetworkError.
            raise APIResponseError(
                response.status_code,
                message,
                response_body=body_preview or None,
            )

        try:
            return response.json()  # type: ignore[no-any-return]
        except json.JSONDecodeError as err:
            raise NetworkError("Invalid response format from API") from err

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore._mark_nodes_export_dirty-4dnc,
    #   role=WorkFlowyClientCore._mark_nodes_export_dirty,
    #   slice_labels=ra-workflowy-cache,
    #   kind=ast,
    # ]
    def _mark_nodes_export_dirty(self, node_ids: list[str] | None = None) -> None:
        """Mark parts of the cached /nodes-export snapshot as dirty.

        When the cache is populated, this is used by mutating operations to
        record which UUIDs (or entire regions via "*") have changed since the
        last refresh. Subsequent export_nodes(...) calls can decide whether
        they can safely reuse the cached snapshot for a given subtree or must
        re-fetch from the API.
        """
        # node_ids=None is the conservative "everything is dirty" sentinel.
        def _mark(current: CacheView) -> None:
            # Seal item 10: the populated/unpopulated DECISION and the
            # dirty-set MUTATION both execute inside the publisher-synchronized
            # section, so they always refer to the same current publisher
            # state (no outside-lock stale check).  S03-A guarantee: atomic
            # publication-plus-dirty WRITES; the welded view+dirty READ
            # capture for consumers is the named S03-B1 deliverable (B1.1).
            if is_unpopulated_view(current):
                # If there is no cache, there's nothing to mark.
                return
            if node_ids is None:
                self._nodes_export_dirty_ids.add("*")
                return
            for nid in node_ids:
                if nid:
                    self._nodes_export_dirty_ids.add(nid)

        # S03 §6.2: dirty bookkeeping is serialized under the publisher
        # mutex.  Dirty IDs never change adopted markers.
        self._cache_view_publisher.synchronized(_mark)

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore._get_nodes_export_cache_nodes-5uaq,
    #   role=WorkFlowyClientCore._get_nodes_export_cache_nodes,
    #   slice_labels=ra-workflowy-cache,mci-cache-seams,
    #   kind=ast,
    # ]
    def _get_nodes_export_cache_nodes(self, *, view: CacheView | None = None) -> list[dict[str, Any]]:
        """Return the flat `nodes` list from an already-captured cache view.

        S03 §6.4: a synchronous accessor over an already-captured
        operation/current view — it NEVER reads the manifest, adopts, or
        refreshes.  With no explicit ``view``, the task-local operation
        snapshot is used; with NO operation boundary active this is a LOUD
        ``CacheViewError`` (S03-B loud-boundary tightening): a cache
        consumer outside an operation boundary is a migration bug, and the
        S03-A publisher-current compatibility fallback is retired.  The
        returned list is the immutable view's own: read-only by contract
        (Axiom 5).

        These helpers are intentionally strict: if the cache is missing or
        malformed, callers should fall back to marking regions dirty and let
        export_nodes_impl(...) refresh from the API.
        """
        if view is None:
            from ..coherence.operation_cache import current_operation_cache

            snapshot = current_operation_cache(required=False)
            if snapshot is not None and snapshot.owner is (
                getattr(self, "_cache_view_publisher", None) or self
            ):
                view = snapshot.view
            else:
                from ..coherence.errors import CacheViewError

                raise CacheViewError(
                    "_get_nodes_export_cache_nodes: no operation cache "
                    "snapshot is active in this task (cache-backed code must "
                    "run inside a cache_operation_boundary; pass an explicit "
                    "view= for sanctioned bootstrap/coherence internals)"
                )
        if is_unpopulated_view(view):
            raise RuntimeError("nodes-export cache is not initialized")
        nodes = view.nodes_export.get("nodes")
        if not isinstance(nodes, list):
            raise RuntimeError("nodes-export cache is missing 'nodes' list")
        return nodes

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore.update_cached_node_name-c84o,
    #   role=⚡ update_cached_node_name(self, node_id: str, new_name: str),
    #   slice_labels=ra-reconcile,ra-workflowy-cache,nexus--tags,
    #   kind=ast,
    # ]
    async def update_cached_node_name(self, node_id: str, new_name: str) -> bool:
        """Update the name for a single node in the cached /nodes-export snapshot.

        This does NOT call the Workflowy API. It only patches the in-memory
        cache so that subsequent operations (auto-beacon, Cartographer, etc.)
        see the up-to-date name/tags without requiring a full /nodes-export
        refresh.

        Returns True if a node was updated, False if the cache is missing
        or node_id was not found.
        """
        if not self._cache_populated():
            self._log_debug(
                f"update_cached_node_name: no cache present, cannot update {node_id}"
            )
            return False

        outcome: dict[str, Any] = {"found": False}

        def _transform(working: dict[str, Any]) -> None:
            # Seal item 2: the outcome belongs to the attempt whose CAS
            # actually wins — reset at EVERY attempt so a stale losing
            # attempt can never leak its result into a later one.
            outcome["found"] = False
            for index, node in enumerate(working["nodes"]):
                if str(node.get("id")) == str(node_id):
                    # Clone-before-mutate (S03 H4): never touch a published
                    # node dict; the view swap installs the clone.
                    patched = dict(node)
                    # Cache stores dewhitened semantic text; node_name from DOM is already semantic.
                    patched["name"] = new_name
                    if "nm" in patched:
                        patched["nm"] = new_name
                    working["nodes"][index] = patched
                    outcome["found"] = True
                    return
            self._log_debug(
                f"update_cached_node_name: node_id {node_id} not found in cache"
            )

        try:
            self.publish_local_cache_view(
                _transform, reason="update_cached_node_name"
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            self._log_debug(
                f"update_cached_node_name: cache malformed when updating {node_id}: {exc}"
            )
            return False
        return outcome["found"]

    # @beacon[
    #   id=carto-rev-mat@update_cached_node_note,
    #   slice_labels=ra-reconcile,ra-workflowy-cache,carto-rev-markdown-materialize,
    #   kind=ast,
    #   comment=Patch a single node's NOTE in the in-memory /nodes-export cache WITHOUT calling the Workflowy API. Sibling of update_cached_node_name. Used by F12+4 so a daemon task using the shared publisher sees the freshly-written 'Path:' FILE header without a /nodes-export refresh. Returns True iff a node was patched.,
    # ]
    async def update_cached_node_note(self, node_id: str, new_note: str) -> bool:
        """Update the note for a single node in the cached /nodes-export snapshot.

        Like ``update_cached_node_name`` but for the note field. Does NOT call
        the Workflowy API; only patches the in-memory cache so that subsequent
        operations, including a daemon task using the shared publisher, see
        the up-to-date note without a full /nodes-export refresh.

        Returns True if a node was updated, False if the cache is missing or
        node_id was not found.
        """
        if not self._cache_populated():
            self._log_debug(
                f"update_cached_node_note: no cache present, cannot update {node_id}"
            )
            return False

        outcome: dict[str, Any] = {"found": False}

        def _transform(working: dict[str, Any]) -> None:
            # Seal item 2: the outcome belongs to the attempt whose CAS
            # actually wins — reset at EVERY attempt so a stale losing
            # attempt can never leak its result into a later one.
            outcome["found"] = False
            for index, node in enumerate(working["nodes"]):
                if str(node.get("id")) == str(node_id):
                    # Clone-before-mutate (S03 H4): never touch a published
                    # node dict; the view swap installs the clone.
                    patched = dict(node)
                    # Cache stores dewhitened semantic text; new_note is already semantic.
                    patched["note"] = new_note
                    if "no" in patched:
                        patched["no"] = new_note
                    working["nodes"][index] = patched
                    outcome["found"] = True
                    return
            self._log_debug(
                f"update_cached_node_note: node_id {node_id} not found in cache"
            )

        try:
            self.publish_local_cache_view(
                _transform, reason="update_cached_node_note"
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            self._log_debug(
                f"update_cached_node_note: cache malformed when updating {node_id}: {exc}"
            )
            return False
        return outcome["found"]

    # @beacon[
    #   id=carto-rev-mat@_nodes_export_snapshot_lock,
    #   slice_labels=ra-workflowy-cache,nexus--broker,carto-rev-markdown-materialize,
    #   kind=ast,
    #   comment=RETIRED cross-process advisory lock (S02). Formerly guarded the read-latest -> patch -> write-new critical section for on-disk /nodes-export snapshots with a FAIL-OPEN posture; S02 excised both proceed-unlocked branches — acquisition trouble now raises (fail-closed). The transitional patch bridge it once served now serializes through the coherence epoch-publication claim. No production callers remain; kept only so misuse fails loudly.,
    # ]
    @contextmanager
    def _nodes_export_snapshot_lock(self, *, stale_seconds: float = 30.0):
        """Cross-process advisory lock for snapshot patch-writes (S02-retired).

        DEPRECATED / FAIL-CLOSED (S02 §12.2, H3-d): the transitional bridge
        (``patch_nodes_export_snapshot_on_disk``) now serializes through the
        coherence epoch-publication claim, and BOTH legacy proceed-unlocked
        branches (timeout / lock error) are excised — acquisition trouble now
        raises rather than ever running lock-free.  No production caller
        remains; the symbol stays only so misuse fails loudly.
        """
        import time as _time

        lock_path = os.path.join(
            self._get_nodes_export_snapshot_dir(), ".nodes_export_patch.lock"
        )
        acquired = False
        deadline = _time.monotonic() + 5.0  # bounded wait for a live holder
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, f"{os.getpid()}:{datetime.now().isoformat()}".encode())
                finally:
                    os.close(fd)
                acquired = True
                break
            except FileExistsError:
                # Someone holds it. Take over if it's stale; else briefly wait.
                try:
                    st = os.stat(lock_path)
                    if (_time.time() - st.st_mtime) > stale_seconds:
                        # Stale holder (crashed mid-patch). Reclaim by removing.
                        os.unlink(lock_path)
                        continue
                except FileNotFoundError:
                    continue  # holder released between our open and stat; retry
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        "_nodes_export_snapshot_lock: stat failed; refusing to "
                        f"proceed unlocked (S02 fail-closed): {exc}"
                    ) from exc
                if _time.monotonic() >= deadline:
                    raise RuntimeError(
                        "_nodes_export_snapshot_lock: timed out waiting for the lock; "
                        "refusing to proceed unlocked (S02 fail-closed)"
                    ) from None
                _time.sleep(0.1)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "_nodes_export_snapshot_lock: lock error; refusing to proceed "
                    f"unlocked (S02 fail-closed): {exc}"
                ) from exc
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    os.unlink(lock_path)
                except Exception:  # noqa: BLE001
                    pass

    # @beacon[
    #   id=carto-rev-mat@_find_latest_nodes_export_snapshot_path,
    #   slice_labels=ra-workflowy-cache,carto-rev-markdown-materialize,
    #   kind=ast,
    #   comment=Return the path to the newest on-disk /nodes-export snapshot (lexicographically maximal, which equals chronological by the timestamp+pid filename scheme), or None when none exist. Shared discovery used by both warm-start loading and the patch-latest critical section.,
    # ]
    def _find_latest_nodes_export_snapshot_path(self) -> str | None:
        """Return the newest snapshot path on disk, or None.

        S02 (H4-b): pure read-only timestamp discovery with ZERO production
        callers — newest-timestamp selection can never become authoritative
        again.  The one-time no-manifest bootstrap uses the coherence-owned
        equivalent (``snapshot_store.select_legacy_bootstrap_candidate``);
        warm start, CARTO selection, and patching are all manifest-based.
        """
        snapshot_dir = self._get_nodes_export_snapshot_dir()
        try:
            candidates = [
                os.path.join(snapshot_dir, name)
                for name in os.listdir(snapshot_dir)
                if name.endswith("_nodes_export_snapshot.json")
            ]
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            self._log_debug(f"_find_latest_nodes_export_snapshot_path: list failed: {exc}")
            return None
        if not candidates:
            return None
        return sorted(candidates)[-1]

    # @beacon[
    #   id=carto-rev-mat@patch_nodes_export_snapshot_on_disk,
    #   role=⚡ patch_nodes_export_snapshot_on_disk(self, patches),
    #   slice_labels=ra-workflowy-cache,nexus--broker,carto-rev-markdown-materialize,mci-epoch-seams,
    #   kind=ast,
    #   comment=Transitional narrow name/note epoch publication: one indexed S2 materialization plus sparse strict reduction, then explicit transfer of the owned result into local lineage publication. Existing compat in-memory record, all-or-none semantics, barrier/dirt non-clearing, archive guard and refresh_required taxonomy remain unchanged.,
    # ]
    async def patch_nodes_export_snapshot_on_disk(
        self,
        patches: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Transitional mini epoch publication for narrow patches (S02 §12).

        The legacy lineage-blind patcher is gone.  This bridge acquires the
        same in-memory publication authority as a full refresh (``compat_patch``
        kind, never deduped), materializes the active manifest base PLUS the
        journal through the captured head, applies the validated narrow
        name/note patches all-or-none through the S01 effect reducer, and
        publishes the result as epoch E+1 — clearing NO barrier or
        external-dirt generations.  Fail-closed: any failure returns a
        structured ``refresh_required`` result so the still-active transition
        terminal-refresh healer repairs lineage.
        The owned materialized result is explicitly transferred at local lineage publication.

        ``patches`` is a list of ``{"id": <uuid>, "note"?: str, "name"?: str}``
        dicts.  Does NOT call the Workflowy API.

        Archive Reads (§4.7.1): write guard #3 -- the compat epoch
        publication refuses loudly when the running operation is augmented
        (archive-including); the guard can only fire on a bug.
        """
        from ..archive_context import archive_write_guard

        archive_write_guard("patch_nodes_export_snapshot_on_disk")

        if not patches:
            return {"success": True, "patched": 0, "note": "no patches"}

        try:
            from ..coherence.refresh_authority import run_compat_patch

            outcome = await run_compat_patch(
                paths=self._coherence_paths(),
                patches=patches,
                snapshot_dir=self._get_nodes_export_snapshot_dir(),
            )
        except Exception as exc:  # noqa: BLE001 - structured refresh_required
            self._log_debug(f"patch_nodes_export_snapshot_on_disk bridge failed: {exc}")
            return {
                "success": False,
                "error": str(exc),
                "error_kind": "compat_patch_failed",
                "refresh_required": True,
            }

        # Surrender the owned materialized lineage to local lineage publication (H3-f).
        details = dict(outcome.details or {})
        self._publish_lineage_view(
            outcome.nodes_export,
            epoch_number=outcome.epoch_number,
            epoch_id=outcome.epoch_id,
            adopted_sequence=outcome.adopted_sequence,
            snapshot_timestamp=outcome.snapshot_timestamp or datetime.now().isoformat(),
            account_scope_id=self._safe_account_scope_id(),
            publication_kind="compat_patch",
            ownership="transferred",
            discard_dirty=tuple(str(pid) for pid in details.get("patched_ids", [])),
        )
        return {
            "success": True,
            "patched": details.get("patched", 0),
            "patched_ids": details.get("patched_ids", []),
            "missing_ids": details.get("missing_ids", []),
            "locked": True,
            "loaded_latest": details.get("loaded_latest"),
            "snapshot_path": details.get("snapshot_path"),
            "epoch_number": outcome.epoch_number,
            "epoch_id": outcome.epoch_id,
            "incorporated_through_seq": outcome.incorporated_through_seq,
            "journal_head_seq": outcome.journal_head_seq,
        }

    def _normalize_node_for_nodes_export_cache(self, node_dict: dict[str, Any]) -> dict[str, Any]:
        """Normalize a node dict for insertion into the /nodes-export cache.

        - Ensures `id` is a string.
        - Dewhitens name/nm and note/no so cache stays in semantic space,
          matching the behavior of export_nodes_impl(...).
        """
        normalized: dict[str, Any] = dict(node_dict) if node_dict is not None else {}
        node_id = normalized.get("id")
        if node_id is not None:
            normalized["id"] = str(node_id)
        for key in ("name", "nm", "note", "no"):
            if key in normalized:
                normalized[key] = self._dewhiten_text(normalized.get(key))  # type: ignore[arg-type]
        return normalized

    def _apply_create_to_nodes_export_cache(
        self, node: WorkFlowyNode, parent_id: str | None = None
    ) -> None:
        """Eagerly insert a newly created node into the /nodes-export cache.

        S03 §6.2: the insertion publishes ONE copy-on-write successor view
        through the transition bridge (unchanged adopted markers; the dirty
        discard commits atomically with the swap).
        """
        node_dict = self._normalize_node_for_nodes_export_cache(node.model_dump())
        if parent_id:
            node_dict["parent_id"] = parent_id
            node_dict["parentId"] = parent_id

        def _transform(working: dict[str, Any]) -> None:
            # Append a fresh outer dict; the bridge detaches its changed
            # nested fragments before explicitly transferring the candidate.
            working["nodes"].append(dict(node_dict))

        self.publish_local_cache_view(
            _transform,
            reason="_apply_create_to_nodes_export_cache",
            commit=lambda: self._nodes_export_dirty_ids.discard(node.id),
        )

    def _apply_update_to_nodes_export_cache(
        self, node: WorkFlowyNode, *, observed_text_fields: frozenset[str] | None = None,
    ) -> None:
        """Eagerly update an existing node inside the /nodes-export cache.

        Raises RuntimeError if the node cannot be found so callers can mark
        the path dirty and fall back to a full /nodes-export refresh.

        S03 §6.2: ONE copy-on-write successor via the transition bridge.
        N02 observations optionally retain field presence: a missing wire text
        field must not synthesize a null compatibility alias. None keeps the
        existing non-N02 patch semantics unchanged.
        """
        node_id = node.id
        # Use exclude_none=True so partial updates (e.g. name/note-only beacon
        # refreshes) do not clobber existing metadata like parent_id/priority
        # with None.
        node_dict = node.model_dump(exclude_none=True)
        if observed_text_fields is not None:
            for field, alias in (("name", "nm"), ("note", "no")):
                if field not in observed_text_fields:
                    node_dict.pop(field, None)
                    node_dict.pop(alias, None)
        updated = self._normalize_node_for_nodes_export_cache(node_dict)

        def _transform(working: dict[str, Any]) -> None:
            for index, existing in enumerate(working["nodes"]):
                if str(existing.get("id")) == node_id:
                    # Preserve any extraneous keys from the existing snapshot
                    # but overwrite core fields from the fresh node state.
                    # Clone-before-mutate (S03 H4).
                    merged = dict(existing)
                    merged.update(updated)
                    working["nodes"][index] = merged
                    return
            # Node not present in cache – signal caller to fall back to dirty flag.
            self._log_debug(f"_apply_update_to_nodes_export_cache: node {node_id} not in cache; marking dirty")
            raise RuntimeError(f"nodes-export cache missing node {node_id}")

        self.publish_local_cache_view(
            _transform,
            reason="_apply_update_to_nodes_export_cache",
            commit=lambda: self._nodes_export_dirty_ids.discard(node_id),
        )

    def _apply_delete_to_nodes_export_cache(self, node_id: str) -> None:
        """Eagerly remove a deleted node (and its descendants) from the cache.

        S03 §6.2: ONE copy-on-write successor via the transition bridge.
        """

        def _transform(working: dict[str, Any]) -> None:
            nodes = working["nodes"]
            # Build parent->children index once for this snapshot.
            children_by_parent: dict[str, list[str]] = {}
            for n in nodes:
                nid = n.get("id")
                if not nid:
                    continue
                pid = n.get("parent_id") or n.get("parentId")
                if pid:
                    children_by_parent.setdefault(str(pid), []).append(str(nid))

            # Collect all descendants of node_id.
            target_id = str(node_id)
            to_remove: set[str] = {target_id}
            stack = [target_id]
            visited: set[str] = set()
            while stack:
                cur = stack.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                for child_id in children_by_parent.get(cur, []):
                    if child_id not in to_remove:
                        to_remove.add(child_id)
                        stack.append(child_id)

            if len(to_remove) == 1 and all(str(n.get("id")) != target_id for n in nodes):
                # Nothing to remove; treat as soft failure so caller can mark dirty.
                self._log_debug(f"_apply_delete_to_nodes_export_cache: node {node_id} not in cache; marking dirty")
                raise RuntimeError(f"nodes-export cache missing node {node_id}")

            # Container replacement on the PRIVATE working payload (the
            # published list is never touched).
            working["nodes"] = [n for n in nodes if str(n.get("id")) not in to_remove]

        self.publish_local_cache_view(
            _transform,
            reason="_apply_delete_to_nodes_export_cache",
            commit=lambda: self._nodes_export_dirty_ids.discard(node_id),
        )

    def _apply_move_to_nodes_export_cache(self, node_id: str, new_parent_id: str | None) -> None:
        """Eagerly update parent_id for a moved node in the cache.

        S03 §6.2: ONE copy-on-write successor via the transition bridge.
        """

        def _transform(working: dict[str, Any]) -> None:
            target_id = str(node_id)
            for index, n in enumerate(working["nodes"]):
                if str(n.get("id")) == target_id:
                    # Clone-before-mutate (S03 H4).
                    moved = dict(n)
                    moved["parent_id"] = new_parent_id
                    if "parentId" in moved:
                        moved["parentId"] = new_parent_id
                    working["nodes"][index] = moved
                    return
            self._log_debug(f"_apply_move_to_nodes_export_cache: node {node_id} not in cache; marking dirty")
            raise RuntimeError(f"nodes-export cache missing node {node_id}")

        self.publish_local_cache_view(
            _transform,
            reason="_apply_move_to_nodes_export_cache",
            commit=lambda: self._nodes_export_dirty_ids.discard(node_id),
        )

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore.refresh_nodes_export_cache-kejn,
    #   role=WorkFlowyClientCore.refresh_nodes_export_cache,
    #   slice_labels=ra-workflowy-cache,mci-epoch-seams,
    #   kind=ast,
    #   comment=Own or join the existing full-refresh authority, replay through indexed S2, and explicitly transfer the materialized result to local lineage publication; require_new_fetch and final publication receipt semantics are preserved.,
    # ]
    async def refresh_nodes_export_cache(
        self,
        max_retries: int = 10,
        *,
        refresh_kind: str = "system",
        settle_seconds: float | None = None,
        require_new_fetch: bool = False,
    ) -> dict[str, Any]:
        """Force a fresh /nodes-export call and update the in-memory cache.

        S02: this is the coherence refresh-authority path.  The download is
        canonicalized and replayed through one captured contiguous journal
        head, published as a new manifest epoch by the single epoch
        publisher, and only THEN published locally from the fully materialized
        result — never an eager pre-publication assignment.
        Local lineage publication consumes the owned materialization without copying it.
        """
        from ..coherence.refresh_authority import run_full_refresh
        from .api_client_etch import fetch_nodes_export_uncached

        _refresh_started = datetime.now()
        paths = self._coherence_paths()
        outcome = await run_full_refresh(
            paths=paths,
            fetch=lambda: fetch_nodes_export_uncached(self, max_retries=max_retries),
            refresh_kind=refresh_kind,
            settle_seconds=self._refresh_settle_seconds(settle_seconds),
            snapshot_dir=self._get_nodes_export_snapshot_dir(),
            require_new_fetch=require_new_fetch,
        )
        # H3(f): surrender the owned materialized result through the one
        # lineage-view adapter; subsequent uses are read-only metadata.
        adopted_view = self._publish_lineage_view(
            outcome.nodes_export,
            epoch_number=outcome.epoch_number,
            epoch_id=outcome.epoch_id,
            adopted_sequence=outcome.adopted_sequence,
            snapshot_timestamp=outcome.snapshot_timestamp or datetime.now().isoformat(),
            account_scope_id=self._safe_account_scope_id(),
            publication_kind="full_refresh",
            ownership="transferred",
            clear_dirty=True,
        )
        self._nodes_export_lineage_error = None

        # Opportunistically garbage-collect older snapshots (manifest-aware).
        try:
            self._gc_nodes_export_snapshots()
        except Exception as exc:  # pragma: no cover - defensive logging only
            self._log_debug(f"_gc_nodes_export_snapshots failed: {exc}")

        result = outcome.to_result_dict()
        if require_new_fetch:
            if outcome.status != "published" or outcome.deduplicated:
                raise RuntimeError("final cache request did not own a new full publication")
            if (adopted_view.epoch_id != outcome.epoch_id or
                    adopted_view.epoch_number != outcome.epoch_number or
                    adopted_view.adopted_sequence < outcome.adopted_sequence):
                raise RuntimeError("final publication was not adopted by this daemon")
            # Construct only AFTER _publish_lineage_view returned. This is
            # execution evidence, never a request/scheduling acknowledgment.
            result["final_publication_receipt"] = {
                "claim_id": outcome.claim_id,
                "account_scope_id": self._safe_account_scope_id(),
                "status": outcome.status, "publication_kind": "full_refresh",
                "new_fetch": True, "adopted": True,
                "epoch_id": outcome.epoch_id, "epoch_number": outcome.epoch_number,
                "base_snapshot_path": outcome.base_snapshot_path,
                "adopted_sequence": outcome.adopted_sequence,
            }
        # Console visibility for a completed refresh (Dan, Manager-Session-036): the
        # warm-start path logs 'Loaded ... from manifest base ... (epoch N)', but a
        # refresh adopts its new epoch in memory and used to log nothing, so the
        # daemon window never showed that a refresh landed. Same channel, same shape.
        try:
            elapsed = (datetime.now() - _refresh_started).total_seconds()
            self._log_debug(
                f"Refreshed /nodes-export cache ({refresh_kind}): adopted epoch "
                f"{outcome.epoch_number} (adopted seq {outcome.adopted_sequence}), "
                f"{result.get('node_count')} nodes, base {result.get('base_snapshot_path')}"
                f"{' [joined an in-flight refresh]' if result.get('deduplicated') else ''}"
                f" in {elapsed:.1f}s"
            )
        except Exception:  # noqa: BLE001 - logging must never fail a refresh
            pass
        return result

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore.save_nodes_export_cache-vwv9,
    #   role=WorkFlowyClientCore.save_nodes_export_cache,
    #   slice_labels=ra-workflowy-cache,
    #   kind=ast,
    # ]
    async def save_nodes_export_cache(self) -> dict[str, Any]:
        """Structured transition refusal (S02 §11.1).

        The legacy eager cache-dump surface is disabled: an uncontextualized
        in-memory cache dump must never become authoritative lineage.  The
        coherence refresh authority owns snapshot publication.  This returns
        a structured failure (never pretends success while writing an
        unregistered snapshot).
        """
        self._log_debug(
            "save_nodes_export_cache called: disabled in coherence transition mode (S02)"
        )
        return {
            "success": False,
            "error_kind": "disabled_in_transition",
            "error": (
                "save_nodes_export_cache is disabled by the mutation-coherence "
                "transition (S02): snapshot publication is owned by the coherence "
                "refresh authority. Use system.cache.refresh instead."
            ),
        }

    # @beacon[
    #   id=core-crud@create_node,
    #   role=WorkFlowyClientCore.create_node,
    #   slice_labels=nexus-core-v1,ra-workflowy-cache,mci-lease-seams,node-ops,
    #   kind=ast,
    # ]
    async def create_node(
        self,
        request: NodeCreateRequest,
        _internal_call: bool = False,
        max_retries: int = 10,
        *,
        fetch_created_node: bool = True,
        _governance_coverage_hint=None,
    ) -> WorkFlowyNode:
        """Create a new node in WorkFlowy with exponential backoff retry.

        Args:
            request: Node creation request
            _internal_call: Internal flag - bypasses single-node forcing function (not exposed to MCP)
            max_retries: Maximum retry attempts (default 10)

        Cache semantics:
            This method eagerly updates the /nodes-export cache **for this
            WorkFlowyClientCore instance only**. A daemon task using the shared
            publisher sees the updated view; independently constructed clients
            retain their own publishers.
        """
        import asyncio
        import time

        logger = _ClientLogger()

        # Check for single-node override token (skip if internal call)
        if not _internal_call:
            SINGLE_NODE_TOKEN = "<<<I_REALLY_NEED_SINGLE_NODE>>>"

            if request.name and request.name.startswith(SINGLE_NODE_TOKEN):
                # Strip token and proceed
                request.name = request.name.replace(SINGLE_NODE_TOKEN, "", 1)
            else:
                # Suggest ETCH instead
                raise NetworkError("""⚠️ PREFER ETCH - Use workflowy_etch for consistency and capability

You called workflowy_create_single_node, but workflowy_etch has identical performance.

✅ RECOMMENDED (same speed, more capability):
  workflowy_etch(
    parent_id="...",
    nodes=[{"name": "Your node", "note": "...", "children": []}]
  )

📚 Benefits of ETCH:
  - Same 1 tool call (no performance difference)
  - Validation and auto-escaping built-in
  - Works for 1 node or 100 nodes (consistent pattern)
  - Trains you to think in tree structures

⚙️ OVERRIDE (if you truly need single-node operation):
  workflowy_create_single_node(
    name="<<<I_REALLY_NEED_SINGLE_NODE>>>Your node",
    ...
  )

🎯 Build the ETCH habit - it's your go-to tool!
""")

        # Validate name and note (pure validation, no encoding).
        name_ok, name_err = self._validate_name(request.name)
        if not name_ok:
            raise NetworkError(name_err or "Invalid NAME field")
        note_ok, note_err = self._validate_note(
            request.note,
            skip_newline_check=_internal_call,
        )
        if not note_ok:
            raise NetworkError(note_err or "Invalid NOTE field")

        # S05 §14 (H5c): transition-mode governance-coverage diagnostic,
        # invoked by create_node ITSELF on every production entry BEFORE the
        # send path.  Diagnostic only — a passed hint suppresses the warning
        # but confers NO authorization; the helper NEVER scans the
        # governance directory.  S07 replaces the hint with the
        # transaction-bound MutationContext; S11 flips to hard enforcement.
        try:
            import sys as _sys

            from ..mutation_lease_governance import warn_create_governance_coverage

            _frame = _sys._getframe(1)
            _caller = f"{_frame.f_code.co_filename.rsplit(chr(92), 1)[-1]}:{_frame.f_lineno} ({_frame.f_code.co_name})"
            try:
                _account = self._safe_account_scope_id
            except Exception:  # noqa: BLE001 — config-less test doubles
                _account = None
            warn_create_governance_coverage(
                operation_label="create_node",
                caller_summary=_caller,
                account_scope_id=_account,
                parent_uuid=getattr(request, "parent_id", None),
                coverage_hint=_governance_coverage_hint,
            )
        except Exception as _coverage_exc:  # noqa: BLE001 — never breaks a create
            logger.warning(
                "create_node: coverage-warning diagnostic failed (create proceeds): "
                f"{_coverage_exc}"
            )

        # Encode for Workflowy API exactly once. This is the single point of
        # NAME/NOTE encoding for all create paths (ETCH, F12, direct calls).
        if request.name is not None:
            request.name = self.encode_for_workflowy_api(
                request.name,
                "name",
                operation="create",
            )
        if request.note is not None:
            request.note = self.encode_for_workflowy_api(
                request.note,
                "note",
                operation="create",
            )

        retry_count = 0
        base_delay = 1.0

        while retry_count < max_retries:
            try:
                name_for_log = getattr(request, "name", None)
                parent_for_log = getattr(request, "parent_id", None)

                response = await self._send_workflowy_write(
                    "POST",
                    "/nodes/",
                    json=request.model_dump(exclude_none=True),
                    operation="create_node",
                    diagnostic_label="create_node[POST]",
                    diagnostic_context=(f"name={name_for_log!r} parent={parent_for_log!r}"),
                )

                data = await self._handle_response(response)
                # Create endpoint returns just {"item_id": "..."}
                item_id = data.get("item_id")
                if not item_id:
                    raise NetworkError(f"Invalid response from create endpoint: {data}")

                # Optional GET /nodes/{id}
                #
                # For many bulk create paths (F12/WEAVE) we don't *need* a round-trip
                # to fetch the canonical node, and doing so doubles API calls and can
                # create the appearance of a hang if Workflowy stalls a single request.
                if fetch_created_node:
                    t0_get = time.monotonic()
                    get_task = asyncio.create_task(self.client.get(f"/nodes/{item_id}"))
                    while True:
                        try:
                            get_response = await asyncio.wait_for(get_task, timeout=10.0)
                            break
                        except asyncio.TimeoutError:
                            elapsed = time.monotonic() - t0_get
                            logger.warning(
                                "create_node[GET]: still waiting "
                                f"elapsed={elapsed:.1f}s id={item_id!r} name={name_for_log!r}"
                            )

                    get_dt = time.monotonic() - t0_get
                    if get_dt >= 1.0:
                        logger.warning(
                            "create_node[GET]: completed "
                            f"elapsed={get_dt:.2f}s status={getattr(get_response, 'status_code', None)} "
                            f"id={item_id!r} name={name_for_log!r}"
                        )

                    node_data = await self._handle_response(get_response)
                    node = WorkFlowyNode(**node_data["node"])
                else:
                    # Best-effort local node reconstruction (sufficient for cache patching).
                    node = WorkFlowyNode(
                        id=str(item_id),
                        name=getattr(request, "name", None),
                        note=getattr(request, "note", None),
                        parent_id=getattr(request, "parent_id", None),
                        data={"layoutMode": getattr(request, "layoutMode", None)}
                        if getattr(request, "layoutMode", None)
                        else None,
                    )

                # Best-effort: eagerly maintain the /nodes-export cache when present.
                try:
                    if self._cache_populated():
                        try:
                            # Insert the newly created node into the cached /nodes-export snapshot,
                            # explicitly wiring its parent_id/parentId from the creation request so
                            # that subtree exports (export_nodes_impl) see it under the correct
                            # folder/file parent.  S03 §6.2: ONE COW successor via the bridge.
                            self._apply_create_to_nodes_export_cache(
                                node, parent_id=request.parent_id
                            )

                            msg = (
                                f"create_node: cache insert success id={node.id} "
                                f"name={getattr(node, 'nm', None) or getattr(node, 'name', None)!r} "
                                f"parent={request.parent_id!r}"
                            )
                            logger.info(msg)
                        except Exception as cache_exc:  # noqa: BLE001
                            err_msg = (
                                "create_node: cache maintenance failure for "
                                f"id={node.id}: {cache_exc}"
                            )
                            logger.warning(err_msg)
                            # Fall back to marking this node id as dirty so a future
                            # explicit cache refresh can repair the snapshot.
                            self._mark_nodes_export_dirty([node.id])
                    else:
                        msg = (
                            "create_node: no nodes-export cache present; marking dirty for "
                            f"id={node.id}"
                        )
                        logger.info(msg)
                        self._mark_nodes_export_dirty([node.id])
                except Exception as exc:  # noqa: BLE001
                    # Cache maintenance must never affect API behavior; fall back to dirty flag.
                    outer_msg = (
                        "create_node: cache maintenance outer failure for "
                        f"id={node.id}: {exc}"
                    )
                    logger.warning(outer_msg)
                    try:
                        self._mark_nodes_export_dirty([node.id])
                    except Exception:
                        pass

                return node

            except RateLimitError as e:
                retry_count += 1
                retry_after = self._bounded_rate_limit_retry_delay(
                    e,
                    base_delay * (2 ** retry_count),
                    "Workflowy CRUD retry",
                )
                logger.warning(
                    f"Rate limited on create_node. Retry after {retry_after}s. "
                    f"Attempt {retry_count}/{max_retries}"
                )
                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(retry_after, "Workflowy API retry")
                else:
                    raise

            except NetworkError as e:
                retry_count += 1
                _log(
                    f"Network error on create_node: {e}. Retry {retry_count}/{max_retries}"
                )
                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    raise

            # httpx.TransportError covers timeouts (a subclass) AND dropped
            # connections (RemoteProtocolError "Server disconnected without
            # sending a response", ConnectError, ReadError, WriteError).  Before
            # Manager-Session-129 only timeouts were retried, so a single dropped
            # connection killed whole Cartographer refresh jobs.  MCI S07/S12
            # (Axiom 6) later replaces this blind retry with classified outcomes.
            except httpx.TransportError as err:
                retry_count += 1

                logger.warning(
                    f"Transport error ({type(err).__name__}): {err}. "
                    f"Retry {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    if isinstance(err, httpx.TimeoutException):
                        raise TimeoutError("create_node") from err
                    raise

        raise NetworkError("create_node failed after maximum retries")

    # @beacon[
    #   id=core-crud@update_node,
    #   role=WorkFlowyClientCore.update_node,
    #   slice_labels=nexus-core-v1,ra-workflowy-cache,node-ops,
    #   kind=ast,
    #   comment=Encode once and update through the governed sender; optional raw live basis checks observed final/returned written fields, never fabricates missing text, publishes observations before typed conflicts, and accommodates only whole prior-attempt vectors in the inherited retry loop.,
    # ]
    async def update_node(
        self, node_id: str, request: NodeUpdateRequest, max_retries: int = 10,
        *, basis: Mapping[str, str | None] | None = None,
    ) -> WorkFlowyNode:
        """Update with inherited retries and optional final/read-back collision detection.

        Args:
            node_id: The ID of the node to update
            request: Node update request
            max_retries: Maximum retry attempts (default 10)
            basis: Original raw live written fields, copied before encoding.
                None retains the transitional unchecked caller path. A checked
                send makes one direct final GET after its ticket; detected
                changes publish observed values and propagate without retry.

        Cache semantics:
            Best-effort patch of the /nodes-export cache **for this
            WorkFlowyClientCore instance only**. A daemon task using the shared
            publisher sees the updated view; independently constructed clients
            retain their own publishers.
        """
        wire_basis = dict(basis) if basis is not None else None
        logger = _ClientLogger()

        # Validate name and note (pure validation, no encoding).
        if request.name is not None:
            name_ok, name_err = self._validate_name(request.name)
            if not name_ok:
                raise NetworkError(name_err or "Invalid NAME field")
        if request.note is not None:
            note_ok, note_err = self._validate_note(request.note)
            if not note_ok:
                raise NetworkError(note_err or "Invalid NOTE field")

        # Encode for Workflowy API exactly once. Single point of NAME/NOTE
        # encoding for all update paths.
        if request.name is not None:
            request.name = self.encode_for_workflowy_api(
                request.name,
                "name",
                operation="update",
            )
        if request.note is not None:
            request.note = self.encode_for_workflowy_api(
                request.note,
                "note",
                operation="update",
            )

        sent_request = request.model_dump(exclude_none=True)
        fields = [field for field in ("name", "note") if getattr(request, field) is not None]
        sent_values = {field: sent_request[field] for field in fields}
        prior_send_capable = False

        def _different_fields(node, expected):
            # Missing is not explicit null and is not a detected text change.
            # Incomplete responses supply no value to invent or refuse on.
            return [field for field in fields if
                    field in node.model_fields_set and
                    _canonical_update_value(field, getattr(node, field)) !=
                    _canonical_update_value(field, expected[field])]

        def _publish_observed(node):
            # Preserve today's cache-maintenance policy on both outcomes (A1).
            # Explicit null text is an observed clear, not an omitted update.
            patch_node = node
            if wire_basis is not None:
                clears = {field: "" for field in ("name", "note")
                          if field in node.model_fields_set and getattr(node, field) is None}
                if clears:
                    patch_node = node.model_copy(update=clears)
            try:
                if self._cache_populated():
                    if wire_basis is not None:
                        self._apply_update_to_nodes_export_cache(
                            patch_node, observed_text_fields=frozenset(node.model_fields_set),
                        )
                    else:
                        self._apply_update_to_nodes_export_cache(patch_node)
                else:
                    self._mark_nodes_export_dirty([node_id])
            except Exception:
                try:
                    self._mark_nodes_export_dirty([node_id])
                except Exception:
                    pass

        async def _preflight():
            nonlocal prior_send_capable
            response = await self.client.get(f"/nodes/{node_id}")
            data = await self._handle_response(response)
            live_node = WorkFlowyNode(**data["node"])
            differences = _different_fields(live_node, wire_basis)
            matches_basis = not differences
            matches_sent = prior_send_capable and not _different_fields(live_node, sent_values)
            if not matches_basis and not matches_sent:
                _publish_observed(live_node)
                raise ConcurrentEditDetectedError(
                    node_id=node_id, fields=differences,
                    live={field: getattr(live_node, field) for field in differences},
                    expected=wire_basis, detected_at="preflight",
                    previously_sent=sent_values if prior_send_capable else None,
                )
            # All three existing catches share this invocation-local fact;
            # including postflight 429 is ruling [ap:3CB093], not a new retry.
            prior_send_capable = True

        before_send = _preflight if wire_basis is not None and fields else None

        retry_count = 0
        base_delay = 1.0

        while retry_count < max_retries:
            try:
                response = await self._send_workflowy_write(
                    "POST",
                    f"/nodes/{node_id}",
                    json=sent_request,
                    operation="update_node",
                    before_send=before_send,
                )
                data = await self._handle_response(response)
                # API returns {"status": "ok"} - fetch updated node
                if isinstance(data, dict) and data.get("status") == "ok":
                    get_response = await self.client.get(f"/nodes/{node_id}")
                    node_data = await self._handle_response(get_response)
                    node = WorkFlowyNode(**node_data["node"])
                else:
                    # Fallback for unexpected format
                    node = WorkFlowyNode(**data)

                post_differences = _different_fields(node, sent_values) if before_send is not None else []
                _publish_observed(node)
                if post_differences:
                    raise ConcurrentEditDetectedError(
                        node_id=node_id, fields=post_differences,
                        live={field: getattr(node, field) for field in post_differences},
                        expected=sent_values, detected_at="postflight",
                    )

                return node

            except RateLimitError as e:
                retry_count += 1
                retry_after = self._bounded_rate_limit_retry_delay(
                    e,
                    base_delay * (2 ** retry_count),
                    "Workflowy CRUD retry",
                )
                logger.warning(
                    f"Rate limited on update_node. Retry after {retry_after}s. "
                    f"Attempt {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(retry_after, "Workflowy API retry")
                else:
                    raise

            except NetworkError as e:
                retry_count += 1
                logger.warning(
                    f"Network error on update_node: {e}. Retry {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    raise

            # httpx.TransportError covers timeouts (a subclass) AND dropped
            # connections (RemoteProtocolError "Server disconnected without
            # sending a response", ConnectError, ReadError, WriteError).  Before
            # Manager-Session-129 only timeouts were retried, so a single dropped
            # connection killed whole Cartographer refresh jobs.  MCI S07/S12
            # (Axiom 6) later replaces this blind retry with classified outcomes.
            except httpx.TransportError as err:
                retry_count += 1

                logger.warning(
                    f"Transport error ({type(err).__name__}): {err}. "
                    f"Retry {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    if isinstance(err, httpx.TimeoutException):
                        raise TimeoutError("update_node") from err
                    raise

        raise NetworkError("update_node failed after maximum retries")

    # @beacon[
    #   id=core-crud@_normalize_tag,
    #   role=WorkFlowyClientCore._normalize_tag,
    #   slice_labels=nexus-core-v1,ra-workflowy-cache,
    #   kind=ast,
    #   comment=Lenient tag normalizer used by add_tag/remove_tag. Accepts "foo" or "#foo"; returns the canonical "#foo" form. Internal whitespace in a tag is invalid and raised as ValueError.,
    # ]
    @staticmethod
    def _normalize_tag(tag: str) -> str:
        """Normalize a single tag string to canonical ``#foo`` form.

        Accepts ``"foo"`` or ``"#foo"`` (or with whitespace around the token).
        Returns the canonical ``"#foo"`` form.

        Raises ValueError if the tag is empty after stripping, or if it
        contains internal whitespace (a tag is a single whitespace-
        delimited token by definition in Workflowy).
        """
        if not isinstance(tag, str):
            raise ValueError(f"tag must be a string, got {type(tag).__name__}")
        stripped = tag.strip()
        if not stripped:
            raise ValueError("tag is empty")
        # Strip ALL leading '#' so '##foo' canonicalizes to '#foo'.
        body = stripped.lstrip("#")
        if not body:
            raise ValueError(f"tag has no body after leading '#': {tag!r}")
        if any(ch.isspace() for ch in body):
            raise ValueError(
                f"tag contains internal whitespace (must be a single token): {tag!r}"
            )
        return "#" + body

    @staticmethod
    def _coerce_tag_list(tags: "str | list[str]") -> list[str]:
        """Coerce either a single string or a list of strings into a list."""
        if isinstance(tags, str):
            return [tags]
        if isinstance(tags, list):
            return tags
        raise ValueError(
            f"tags must be a string or a list of strings, got {type(tags).__name__}"
        )

    # @beacon[
    #   id=core-crud@workflowy_add_tag,
    #   role=⚡ workflowy_add_tag(self, node_id: str, tag, max_retries: int),
    #   slice_labels=nexus-core-v1,ra-workflowy-cache,nexus--tags,
    #   kind=ast,
    #   comment=Append trailing name tags under one exact-node region, passing the original raw name basis through final/read-back collision checks; preserve idempotent no-ops and note/beacon bytes; batch purge merge WS and fresh capture tags inherit this path per item.,
    # ]
    async def workflowy_add_tag(
        self,
        node_id: str,
        tag: "str | list[str]",
        max_retries: int = 10,
    ) -> dict[str, Any]:
        """Append trailing name tags with exact-node ownership across live RMW.

        The original raw live name is the UPDATE basis; a detected written-name
        collision propagates without recomposing or retrying the tag operation.

        Idempotency semantics:
          - Tags already present in the trailing tag region are SKIPPED
            (recorded in the response under ``tags_already_present``).
          - The caller may pass duplicates; they are deduplicated.
          - If every requested tag is already present, NO API call is
            made and the node is returned unchanged.

        Input flexibility:
          - ``tag`` may be a single string or a list of strings.
          - Each tag may be passed as ``"foo"`` or ``"#foo"``; the helper
            canonicalizes to ``"#foo"`` internally.

        Preservation guarantees:
          - The base (non-tag) part of the NAME is preserved BYTE-FOR-BYTE
            (no whitespace normalization, no character escaping).
          - The relative order of existing trailing tags is preserved.
          - New tags are appended AFTER any existing trailing tags, in the
            order they were provided (after dedup against existing tags).
          - The NOTE field and any embedded BEACON metadata block are
            NEVER touched.

        Under the new name-#tag-as-source-of-truth paradigm, this is the
        canonical way for an agent to modify the tag state of a node
        without risking hallucinated changes to the surrounding name text.
        Disk-side synchronization remains orthogonal and is the user's
        F12+2 / F12+3 responsibility.

        Semantic-name contract: the raw GET name is decoded ONE entity
        layer (``&amp;``/``&lt;``/``&gt;``, non-cascading) into semantic
        form before the merge, so the update encode round-trips punctuation
        exactly once. The trailing tag region is normalized to canonical
        bare ``#tag`` tokens: Workflowy's presentation pill/underline
        markup is stripped (Workflowy re-renders it on display).

        Args:
            node_id: The Workflowy UUID of the node to modify.
            tag: A single tag string or a list of tag strings. Each
                may be passed with or without the leading ``#``.
            max_retries: Forwarded to the underlying update_node call.

        Returns:
            Dict with keys:
              ``success`` (bool),
              ``node_id`` (str),
              ``old_name`` (str),
              ``new_name`` (str),
              ``tags_added`` (list[str]) — newly appended tags,
              ``tags_already_present`` (list[str]) — tags skipped because
                  they were already in the trailing tag region,
              ``no_op`` (bool) — True iff no Workflowy API call was made.
        """
        # Trailing-tag split via the ONE shared, emoji/trident-aware helper
        # (_resolve_split_name_and_tags -> canonical
        # nexus_map_codebase.split_name_and_tags, with an emoji-aware inline
        # fallback). No local #-only reimplementation here -- that was the
        # drift that let the three tag tools diverge from canonical.
        # Normalize + dedup the requested tags (preserve first-seen order).
        requested_raw = self._coerce_tag_list(tag)
        seen_requested: set[str] = set()
        requested: list[str] = []
        for raw in requested_raw:
            normalized = self._normalize_tag(raw)
            if normalized not in seen_requested:
                seen_requested.add(normalized)
                requested.append(normalized)

        # Fetch the current node to read its authoritative name. We use
        # get_node here rather than the cache so we never risk operating
        # on a stale name (the user may have edited the node in the
        # Workflowy GUI between cache refreshes).
        # U01 §6.3 seam: {node} — each tag primitive owns its WHOLE
        # live-read → compose → write window under the node's key (the
        # client-primitive seam; tag.batch / tag.purge.apply and the WS tag
        # front inherit it per item). The legacy _tag_*_impl fronts take
        # _rate_limiter BEFORE calling in, so only pacing runs outside this
        # hold (§6.2/A8) — pacing waits are sized into the acquire budget.
        async with mutation_region_locks(
            client=self, operation_label="tag.add",
            region_factory=lambda snapshot: (build_region_exact_node(node_id, cache_by_id=snapshot.nodes_by_id),),
        ):
            current_node = await self.get_node(node_id, max_retries=max_retries)
            # Raw wire form -> semantic (ONE non-cascading entity layer) BEFORE
            # the merge, so update_node's single encode re-encodes exactly once
            # (the double-encode fix; see _semantic_name_for_tag_merge).
            old_name = _semantic_name_for_tag_merge(current_node.name or "")

            base, existing_tags = _resolve_split_name_and_tags(old_name)
            existing_set = set(existing_tags)

            tags_added: list[str] = []
            tags_already_present: list[str] = []
            for t in requested:
                if t in existing_set:
                    tags_already_present.append(t)
                else:
                    tags_added.append(t)
                    existing_set.add(t)  # protect against caller-side dupes

            # If nothing to add, skip the API call entirely (no-op fast path).
            if not tags_added:
                return {
                    "success": True,
                    "node_id": node_id,
                    "old_name": old_name,
                    "new_name": old_name,
                    "tags_added": [],
                    "tags_already_present": tags_already_present,
                    "no_op": True,
                }

            # Reconstruct the new name. We deliberately reconstruct as
            # "<base> <existing_tags...> <new_tags...>" to keep tag ordering
            # predictable and stable. The base text comes from split which
            # already strips outer whitespace; existing tag tokens come from
            # split which preserves their canonical "#foo" form.
            all_tags = existing_tags + tags_added
            if base:
                new_name = base + " " + " ".join(all_tags)
            else:
                new_name = " ".join(all_tags)

            request = NodeUpdateRequest(name=new_name)  # type: ignore[call-arg]
            updated = await self.update_node(node_id, request, max_retries=max_retries, basis={"name": current_node.name})

            # updated.name is the raw wire form (post-update GET); normalize it
            # into the same semantic domain as old_name so the response is
            # coherent. The computed new_name fallback is ALREADY semantic --
            # never re-decode it (a visible literal '&lt;' must not become '<').
            updated_name = (
                _semantic_name_for_tag_merge(updated.name)
                if updated.name is not None
                else new_name
            )

            return {
                "success": True,
                "node_id": node_id,
                "old_name": old_name,
                "new_name": updated_name,
                "tags_added": tags_added,
                "tags_already_present": tags_already_present,
                "no_op": False,
            }

    # @beacon[
    #   id=core-crud@workflowy_remove_tag,
    #   role=⚡ workflowy_remove_tag(self, node_id: str, tag, max_retries: int),
    #   slice_labels=nexus-core-v1,ra-workflowy-cache,nexus--tags,
    #   kind=ast,
    #   comment=Remove trailing name tags under one exact-node region with the original raw name as UPDATE basis and final/read-back collision checks; missing tags remain no-write no-ops and note/beacon bytes stay untouched; inherited fronts acquire per item.,
    # ]
    async def workflowy_remove_tag(
        self,
        node_id: str,
        tag: "str | list[str]",
        max_retries: int = 10,
    ) -> dict[str, Any]:
        """Remove trailing name tags with exact-node ownership across live RMW.

        Pass the original raw name basis to the shared UPDATE checks; propagate
        a detected collision without recomposing or retrying this operation.

        Idempotency semantics:
          - Tags not present in the trailing tag region are SILENTLY
            no-op'd (recorded in the response under ``tags_not_present``).
          - The caller may pass duplicates; they are deduplicated.
          - If every requested tag is already absent, NO API call is
            made and the node is returned unchanged.

        Input flexibility:
          - ``tag`` may be a single string or a list of strings.
          - Each tag may be passed as ``"foo"`` or ``"#foo"``; the helper
            canonicalizes to ``"#foo"`` internally.

        Preservation guarantees:
          - The base (non-tag) part of the NAME is preserved BYTE-FOR-BYTE
            (no whitespace normalization, no character escaping).
          - The relative order of any remaining trailing tags is
            preserved.
          - If the removal empties the trailing tag region, no trailing
            whitespace is left behind.
          - The NOTE field and any embedded BEACON metadata block are
            NEVER touched.

        Semantic-name contract: same as :meth:`workflowy_add_tag` -- the
        raw GET name is decoded ONE non-cascading entity layer before the
        merge, and the trailing tag region normalizes to bare ``#tag``
        tokens (Workflowy re-renders pill markup on display).

        Args:
            node_id: The Workflowy UUID of the node to modify.
            tag: A single tag string or a list of tag strings. Each
                may be passed with or without the leading ``#``.
            max_retries: Forwarded to the underlying update_node call.

        Returns:
            Dict with keys:
              ``success`` (bool),
              ``node_id`` (str),
              ``old_name`` (str),
              ``new_name`` (str),
              ``tags_removed`` (list[str]) — tags that were present and
                  have now been stripped from the trailing tag region,
              ``tags_not_present`` (list[str]) — tags that were not in
                  the trailing tag region and were silently ignored,
              ``no_op`` (bool) — True iff no Workflowy API call was made.
        """
        # Trailing-tag split via the ONE shared, emoji/trident-aware helper
        # (see add_tag). No local #-only reimplementation -- that was drift.
        # Normalize + dedup the requested tags (preserve first-seen order).
        requested_raw = self._coerce_tag_list(tag)
        seen_requested: set[str] = set()
        requested: list[str] = []
        for raw in requested_raw:
            normalized = self._normalize_tag(raw)
            if normalized not in seen_requested:
                seen_requested.add(normalized)
                requested.append(normalized)

        # U01 §6.3 seam: {node} — each tag primitive owns its WHOLE
        # live-read → compose → write window under the node's key (the
        # client-primitive seam; tag.batch / tag.purge.apply and the WS tag
        # front inherit it per item). The legacy _tag_*_impl fronts take
        # _rate_limiter BEFORE calling in, so only pacing runs outside this
        # hold (§6.2/A8) — pacing waits are sized into the acquire budget.
        async with mutation_region_locks(
            client=self, operation_label="tag.remove",
            region_factory=lambda snapshot: (build_region_exact_node(node_id, cache_by_id=snapshot.nodes_by_id),),
        ):
            current_node = await self.get_node(node_id, max_retries=max_retries)
            # Raw wire form -> semantic (ONE non-cascading entity layer) BEFORE
            # the merge; see _semantic_name_for_tag_merge.
            old_name = _semantic_name_for_tag_merge(current_node.name or "")

            base, existing_tags = _resolve_split_name_and_tags(old_name)
            existing_set = set(existing_tags)

            tags_removed: list[str] = []
            tags_not_present: list[str] = []
            for t in requested:
                if t in existing_set:
                    tags_removed.append(t)
                else:
                    tags_not_present.append(t)

            # If nothing to remove, skip the API call entirely.
            if not tags_removed:
                return {
                    "success": True,
                    "node_id": node_id,
                    "old_name": old_name,
                    "new_name": old_name,
                    "tags_removed": [],
                    "tags_not_present": tags_not_present,
                    "no_op": True,
                }

            # Filter the existing trailing tag list, preserving the order of
            # surviving tags. Set-membership is the canonical "is this tag
            # present" check; preservation of order is purely cosmetic.
            removed_set = set(tags_removed)
            surviving_tags = [t for t in existing_tags if t not in removed_set]

            if surviving_tags:
                if base:
                    new_name = base + " " + " ".join(surviving_tags)
                else:
                    new_name = " ".join(surviving_tags)
            else:
                # All tags removed — leave the base alone, no trailing space.
                new_name = base

            request = NodeUpdateRequest(name=new_name)  # type: ignore[call-arg]
            updated = await self.update_node(node_id, request, max_retries=max_retries, basis={"name": current_node.name})

            # updated.name is raw wire form -> semantic (see add_tag).
            updated_name = (
                _semantic_name_for_tag_merge(updated.name)
                if updated.name is not None
                else new_name
            )

            return {
                "success": True,
                "node_id": node_id,
                "old_name": old_name,
                "new_name": updated_name,
                "tags_removed": tags_removed,
                "tags_not_present": tags_not_present,
                "no_op": False,
            }

    # @beacon[
    #   id=core-crud@workflowy_rename_tag,
    #   role=WorkFlowyClientCore.workflowy_rename_tag,
    #   slice_labels=nexus-core-v1,ra-workflowy-cache,nexus--tags,
    #   kind=ast,
    #   comment=Rename a trailing tag under one exact-node region, retaining the original raw name basis for final/read-back collision checks; preserve base/note bytes and first-occurrence position while collapsing duplicates; inherited fronts acquire per item.,
    # ]
    async def workflowy_rename_tag(
        self,
        node_id: str,
        old_tag: str,
        new_tag: str,
        max_retries: int = 10,
    ) -> dict[str, Any]:
        """Rename a trailing name tag with exact-node ownership across live RMW.

        The original raw live name supplies the UPDATE basis, never the merged
        name; a detected collision propagates without a tag-operation retry.

        This is the per-node sibling of :meth:`workflowy_add_tag` and
        :meth:`workflowy_remove_tag`. It renames the tag in the trailing
        tag region of the node's NAME **for this node only** — it is NOT a
        document-wide find/replace across the whole Workflowy tree. (That
        is why the verb is ``rename`` and not ``update``: ``update`` would
        wrongly imply a global mutation.)

        Sanity-checked edge cases:
          - ``old_tag`` not present in the trailing tag region -> NO-OP
            (``renamed`` is False, no API call).
          - ``old_tag`` == ``new_tag`` (after normalization) -> NO-OP.
          - ``old_tag`` present 1+ times, ``new_tag`` absent -> the FIRST
            occurrence of ``old_tag`` is replaced by ``new_tag`` IN PLACE
            (position preserved); any ADDITIONAL occurrences of
            ``old_tag`` are dropped (collapse to a single renamed tag).
          - BOTH ``old_tag`` and ``new_tag`` already present -> ALL
            occurrences of ``old_tag`` are removed and ``new_tag`` is left
            where it already was (``new_tag`` is never duplicated).
          - ``new_tag`` is never allowed to appear more than once in the
            resulting trailing tag region.

        Input flexibility:
          - ``old_tag`` / ``new_tag`` may each be passed as ``"foo"`` or
            ``"#foo"``; both are canonicalized to ``"#foo"`` internally.

        Preservation guarantees (identical to add/remove_tag):
          - The base (non-tag) part of the NAME is preserved
            BYTE-FOR-BYTE.
          - The relative order of all OTHER trailing tags is preserved;
            the renamed tag keeps the position of the first ``old_tag``
            occurrence.
          - The NOTE field and any embedded BEACON metadata block are
            NEVER touched.

        Disk-side synchronization remains orthogonal and is the user's
        F12+2 / F12+3 responsibility.

        Semantic-name contract: same as :meth:`workflowy_add_tag` -- the
        raw GET name is decoded ONE non-cascading entity layer before the
        merge, and the trailing tag region normalizes to bare ``#tag``
        tokens (Workflowy re-renders pill markup on display).

        Args:
            node_id: The Workflowy UUID of the node to modify.
            old_tag: The existing tag to rename (with or without ``#``).
            new_tag: The replacement tag (with or without ``#``).
            max_retries: Forwarded to the underlying update_node call.

        Returns:
            Dict with keys:
              ``success`` (bool),
              ``node_id`` (str),
              ``old_name`` (str),
              ``new_name`` (str),
              ``old_tag`` (str) — normalized old tag,
              ``new_tag`` (str) — normalized new tag,
              ``renamed`` (bool) — True iff a rename actually occurred,
              ``old_occurrences_removed`` (int) — number of ``old_tag``
                  tokens that were removed from the trailing tag region,
              ``new_tag_already_present`` (bool) — True iff ``new_tag``
                  was already in the trailing tag region before the call,
              ``no_op`` (bool) — True iff no Workflowy API call was made.
        """
        # Trailing-tag split via the ONE shared, emoji/trident-aware helper
        # (see add_tag). No local #-only reimplementation -- that was drift.
        old_norm = self._normalize_tag(old_tag)
        new_norm = self._normalize_tag(new_tag)

        # U01 §6.3 seam: {node} — each tag primitive owns its WHOLE
        # live-read → compose → write window under the node's key (the
        # client-primitive seam; tag.batch / tag.purge.apply and the WS tag
        # front inherit it per item). The legacy _tag_*_impl fronts take
        # _rate_limiter BEFORE calling in, so only pacing runs outside this
        # hold (§6.2/A8) — pacing waits are sized into the acquire budget.
        async with mutation_region_locks(
            client=self, operation_label="tag.rename",
            region_factory=lambda snapshot: (build_region_exact_node(node_id, cache_by_id=snapshot.nodes_by_id),),
        ):
            current_node = await self.get_node(node_id, max_retries=max_retries)
            # Raw wire form -> semantic (ONE non-cascading entity layer) BEFORE
            # the merge; see _semantic_name_for_tag_merge.
            old_name = _semantic_name_for_tag_merge(current_node.name or "")
            base, existing_tags = _resolve_split_name_and_tags(old_name)

            old_occurrences = sum(1 for t in existing_tags if t == old_norm)
            new_already_present = new_norm in existing_tags

            def _noop_result() -> dict[str, Any]:
                return {
                    "success": True,
                    "node_id": node_id,
                    "old_name": old_name,
                    "new_name": old_name,
                    "old_tag": old_norm,
                    "new_tag": new_norm,
                    "renamed": False,
                    "old_occurrences_removed": 0,
                    "new_tag_already_present": new_already_present,
                    "no_op": True,
                }

            # No-op fast paths: nothing to rename, or rename to itself.
            if old_occurrences == 0:
                return _noop_result()
            if old_norm == new_norm:
                return _noop_result()

            # Rebuild the trailing tag list:
            #   - Replace the FIRST occurrence of old_norm with new_norm,
            #     UNLESS new_norm already exists somewhere (then just drop old).
            #   - Drop every ADDITIONAL occurrence of old_norm (collapse dupes).
            #   - Never allow new_norm to appear more than once.
            new_tags: list[str] = []
            replaced_once = False
            for t in existing_tags:
                if t == old_norm:
                    if not replaced_once and not new_already_present and new_norm not in new_tags:
                        new_tags.append(new_norm)
                    # else: drop this old occurrence entirely.
                    replaced_once = True
                    continue
                new_tags.append(t)

            # Reconstruct the name (mirror add/remove_tag reconstruction).
            if new_tags:
                if base:
                    new_name = base + " " + " ".join(new_tags)
                else:
                    new_name = " ".join(new_tags)
            else:
                new_name = base

            # Defensive: if reconstruction yielded an identical name (should
            # not happen given the guards above), skip the API call.
            if new_name == old_name:
                return _noop_result()

            request = NodeUpdateRequest(name=new_name)  # type: ignore[call-arg]
            updated = await self.update_node(node_id, request, max_retries=max_retries, basis={"name": current_node.name})

            # updated.name is raw wire form -> semantic (see add_tag).
            updated_name = (
                _semantic_name_for_tag_merge(updated.name)
                if updated.name is not None
                else new_name
            )

            return {
                "success": True,
                "node_id": node_id,
                "old_name": old_name,
                "new_name": updated_name,
                "old_tag": old_norm,
                "new_tag": new_norm,
                "renamed": True,
                "old_occurrences_removed": old_occurrences,
                "new_tag_already_present": new_already_present,
                "no_op": False,
            }

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore.get_node-9ovh,
    #   role=WorkFlowyClientCore.get_node,
    #   slice_labels=node-ops,
    #   kind=ast,
    # ]
    async def get_node(self, node_id: str, max_retries: int = 10) -> WorkFlowyNode:
        """Retrieve a specific node by ID with exponential backoff retry.

        Args:
            node_id: The ID of the node to retrieve
            max_retries: Maximum retry attempts (default 10)
        """
        logger = _ClientLogger()
        retry_count = 0
        base_delay = 1.0

        while retry_count < max_retries:
            # Force delay at START of each iteration (rate limit protection)
            await self._sleep_with_operation_cancel(API_RATE_LIMIT_DELAY, "Workflowy API operation")

            try:
                response = await self.client.get(f"/nodes/{node_id}")
                data = await self._handle_response(response)
                # API returns {"node": {...}} structure
                if isinstance(data, dict) and "node" in data:
                    return WorkFlowyNode(**data["node"])
                else:
                    # Fallback for unexpected format
                    return WorkFlowyNode(**data)

            except RateLimitError as e:
                retry_count += 1
                retry_after = self._bounded_rate_limit_retry_delay(
                    e,
                    base_delay * (2 ** retry_count),
                    "Workflowy CRUD retry",
                )
                logger.warning(
                    f"Rate limited on get_node. Retry after {retry_after}s. "
                    f"Attempt {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(retry_after, "Workflowy API retry")
                else:
                    raise

            except NetworkError as e:
                retry_count += 1
                logger.warning(
                    f"Network error on get_node: {e}. Retry {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    raise

            # httpx.TransportError covers timeouts (a subclass) AND dropped
            # connections (RemoteProtocolError "Server disconnected without
            # sending a response", ConnectError, ReadError, WriteError).  Before
            # Manager-Session-129 only timeouts were retried, so a single dropped
            # connection killed whole Cartographer refresh jobs.  MCI S07/S12
            # (Axiom 6) later replaces this blind retry with classified outcomes.
            except httpx.TransportError as err:
                retry_count += 1

                logger.warning(
                    f"Transport error ({type(err).__name__}): {err}. "
                    f"Retry {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    if isinstance(err, httpx.TimeoutException):
                        raise TimeoutError("get_node") from err
                    raise

        raise NetworkError("get_node failed after maximum retries")

    # @beacon[
    #   id=core-crud@list_nodes,
    #   role=WorkFlowyClientCore.list_nodes,
    #   slice_labels=nexus-core-v1,node-ops,
    #   kind=ast,
    #   comment=Lists children with unchanged pacing/retries and two-tuple return; an optional private carrier records raw envelope and row presence for held-parent create verification.,
    # ]
    async def list_nodes(self, request: NodeListRequest, max_retries: int = 10, *, _listing_evidence: dict[str, Any] | None = None) -> tuple[list[WorkFlowyNode], int]:
        """List nodes with optional filtering and exponential backoff retry.

        Args:
            request: Node list request
            max_retries: Maximum retry attempts (default 10)
            _listing_evidence: Private caller-owned raw envelope/row evidence;
                ordinary callers retain the same result and parsing behavior.
        """
        logger = _ClientLogger()
        retry_count = 0
        base_delay = 1.0

        while retry_count < max_retries:
            # Force delay at START of each iteration (rate limit protection)
            await self._sleep_with_operation_cancel(API_RATE_LIMIT_DELAY, "Workflowy API operation")

            try:
                # exclude_none=True ensures parent_id is omitted entirely for root nodes
                # (API requires absence of parameter, not null value)
                # Build params manually to ensure snake_case (API expects parent_id not parentId)
                params = {}
                if request.parentId is not None:
                    params['parent_id'] = request.parentId
                response = await self.client.get("/nodes", params=params)
                response_data: list[Any] | dict[str, Any] = await self._handle_response(response)

                if _listing_evidence is not None:
                    import copy
                    raw_rows = response_data if isinstance(response_data, list) else (
                        response_data.get("nodes") if isinstance(response_data, dict) else None
                    )
                    _listing_evidence.clear()
                    _listing_evidence.update(
                        recognized=isinstance(raw_rows, list),
                        rows=copy.deepcopy(raw_rows) if isinstance(raw_rows, list) else (),
                    )

                # Assuming API returns an array of nodes directly
                # (Need to verify actual response structure)
                nodes: list[WorkFlowyNode] = []
                if isinstance(response_data, dict):
                    if "nodes" in response_data:
                        nodes = [WorkFlowyNode(**node_data) for node_data in response_data["nodes"]]
                elif isinstance(response_data, list):
                    nodes = [WorkFlowyNode(**node_data) for node_data in response_data]

                total = len(nodes)  # API doesn't provide a total count
                return nodes, total

            except RateLimitError as e:
                retry_count += 1
                retry_after = self._bounded_rate_limit_retry_delay(
                    e,
                    base_delay * (2 ** retry_count),
                    "Workflowy CRUD retry",
                )
                logger.warning(
                    f"Rate limited on list_nodes. Retry after {retry_after}s. "
                    f"Attempt {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(retry_after, "Workflowy API retry")
                else:
                    raise

            except NetworkError as e:
                retry_count += 1
                logger.warning(
                    f"Network error on list_nodes: {e}. Retry {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    raise

            # httpx.TransportError covers timeouts (a subclass) AND dropped
            # connections (RemoteProtocolError "Server disconnected without
            # sending a response", ConnectError, ReadError, WriteError).  Before
            # Manager-Session-129 only timeouts were retried, so a single dropped
            # connection killed whole Cartographer refresh jobs.  MCI S07/S12
            # (Axiom 6) later replaces this blind retry with classified outcomes.
            except httpx.TransportError as err:
                retry_count += 1

                logger.warning(
                    f"Transport error ({type(err).__name__}): {err}. "
                    f"Retry {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    if isinstance(err, httpx.TimeoutException):
                        raise TimeoutError("list_nodes") from err
                    raise

        raise NetworkError("list_nodes failed after maximum retries")

    # @beacon[
    #   id=core-crud@delete_node,
    #   role=WorkFlowyClientCore.delete_node,
    #   slice_labels=nexus-core-v1,ra-workflowy-cache,node-ops,
    #   kind=ast,
    # ]
    async def delete_node(self, node_id: str, max_retries: int = 10) -> bool:
        """Delete a node and all its children with exponential backoff retry.

        Args:
            node_id: The ID of the node to delete
            max_retries: Maximum retry attempts (default 10)

        Cache semantics:
            Attempts to remove the node from the in-memory /nodes-export cache
            **for this WorkFlowyClientCore instance only**. Other processes do
            not share this cache and must refresh independently.
        """
        from .api_client_etch import _log_to_file_helper

        logger = _ClientLogger()
        retry_count = 0
        base_delay = 1.0

        while retry_count < max_retries:
            try:
                response = await self._send_workflowy_write(
                    "DELETE",
                    f"/nodes/{node_id}",
                    operation="delete_node",
                )
                # Delete endpoint returns just a message, not nested data
                await self._handle_response(response)
                # If we reached here after one or more retries, log success to reconcile log
                if retry_count > 0:
                    success_msg = (
                        f"delete_node {node_id} succeeded after {retry_count + 1}/{max_retries} attempts "
                        f"following rate limiting or transient errors."
                    )
                    logger.info(success_msg)
                    _log_to_file_helper(success_msg, "reconcile")

                # Best-effort: eagerly maintain the /nodes-export cache when possible.
                try:
                    if self._cache_populated():
                        self._apply_delete_to_nodes_export_cache(node_id)
                    else:
                        self._mark_nodes_export_dirty([node_id])
                except Exception:
                    try:
                        self._mark_nodes_export_dirty([node_id])
                    except Exception:
                        pass

                return True

            except NodeNotFoundError:
                # Node no longer exists in Workflowy – treat as already deleted and
                # clean up the /nodes-export cache so future operations don't keep
                # seeing a stale FILE node.
                not_found_msg = (
                    f"delete_node {node_id} received 404 NodeNotFoundError: treating as already deleted."
                )
                logger.warning(not_found_msg)
                _log_to_file_helper(not_found_msg, "reconcile")

                try:
                    if self._cache_populated():
                        self._apply_delete_to_nodes_export_cache(node_id)
                    else:
                        self._mark_nodes_export_dirty([node_id])
                except Exception:
                    try:
                        self._mark_nodes_export_dirty([node_id])
                    except Exception:
                        pass

                return True

            except RateLimitError as e:
                retry_count += 1
                retry_after = self._bounded_rate_limit_retry_delay(
                    e,
                    base_delay * (2 ** retry_count),
                    "Workflowy CRUD retry",
                )
                retry_msg = (
                    f"Rate limited on delete_node {node_id}. Retry after {retry_after}s. "
                    f"Attempt {retry_count}/{max_retries}"
                )
                logger.warning(retry_msg)
                _log_to_file_helper(retry_msg, "reconcile")

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(retry_after, "Workflowy API retry")
                else:
                    final_msg = (
                        f"delete_node {node_id} exhausted retries ({retry_count}/{max_retries}) "
                        f"due to rate limiting – aborting."
                    )
                    logger.error(final_msg)
                    _log_to_file_helper(final_msg, "reconcile")
                    raise

            except NetworkError as e:
                retry_count += 1
                logger.warning(
                    f"Network error on delete_node: {e}. Retry {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    raise

            # httpx.TransportError covers timeouts (a subclass) AND dropped
            # connections (RemoteProtocolError "Server disconnected without
            # sending a response", ConnectError, ReadError, WriteError).  Before
            # Manager-Session-129 only timeouts were retried, so a single dropped
            # connection killed whole Cartographer refresh jobs.  MCI S07/S12
            # (Axiom 6) later replaces this blind retry with classified outcomes.
            except httpx.TransportError as err:
                retry_count += 1

                logger.warning(
                    f"Transport error ({type(err).__name__}): {err}. "
                    f"Retry {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2**retry_count),
                        "Workflowy API retry",
                    )
                else:
                    if isinstance(err, httpx.TimeoutException):
                        raise TimeoutError("delete_node") from err
                    raise

        raise NetworkError("delete_node failed after maximum retries")

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore.complete_node-xe19,
    #   role=WorkFlowyClientCore.complete_node,
    #   slice_labels=node-ops,
    #   kind=ast,
    # ]
    async def complete_node(self, node_id: str, max_retries: int = 10) -> WorkFlowyNode:
        """Mark a node as completed with exponential backoff retry.

        Cache semantics:
            Best-effort patch of completion state into the /nodes-export cache
            **for this WorkFlowyClientCore instance only**. A daemon task using
            the shared publisher sees that view; independently constructed
            clients retain separate publishers.
        """
        logger = _ClientLogger()
        retry_count = 0
        base_delay = 1.0

        while retry_count < max_retries:
            try:
                response = await self._send_workflowy_write(
                    "POST",
                    f"/nodes/{node_id}/complete",
                    operation="complete_node",
                )
                data = await self._handle_response(response)
                # API returns {"status": "ok"} - fetch updated node
                if isinstance(data, dict) and data.get("status") == "ok":
                    get_response = await self.client.get(f"/nodes/{node_id}")
                    node_data = await self._handle_response(get_response)
                    node = WorkFlowyNode(**node_data["node"])
                else:
                    # Fallback for unexpected format
                    node = WorkFlowyNode(**data)

                # Best-effort: eagerly patch completion state into /nodes-export cache.
                try:
                    if self._cache_populated():
                        self._apply_update_to_nodes_export_cache(node)
                    else:
                        self._mark_nodes_export_dirty([node_id])
                except Exception:
                    try:
                        self._mark_nodes_export_dirty([node_id])
                    except Exception:
                        pass

                return node

            except RateLimitError as e:
                retry_count += 1
                retry_after = self._bounded_rate_limit_retry_delay(
                    e,
                    base_delay * (2 ** retry_count),
                    "Workflowy CRUD retry",
                )
                logger.warning(
                    f"Rate limited on complete_node. Retry after {retry_after}s. "
                    f"Attempt {retry_count}/{max_retries}"
                )
                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(retry_after, "Workflowy API retry")
                else:
                    raise

            except NetworkError as e:
                retry_count += 1
                logger.warning(
                    f"Network error on complete_node: {e}. Retry {retry_count}/{max_retries}"
                )
                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    raise

            # httpx.TransportError covers timeouts (a subclass) AND dropped
            # connections (RemoteProtocolError "Server disconnected without
            # sending a response", ConnectError, ReadError, WriteError).  Before
            # Manager-Session-129 only timeouts were retried, so a single dropped
            # connection killed whole Cartographer refresh jobs.  MCI S07/S12
            # (Axiom 6) later replaces this blind retry with classified outcomes.
            except httpx.TransportError as err:
                retry_count += 1

                logger.warning(
                    f"Transport error ({type(err).__name__}): {err}. "
                    f"Retry {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    if isinstance(err, httpx.TimeoutException):
                        raise TimeoutError("complete_node") from err
                    raise

        raise NetworkError("complete_node failed after maximum retries")

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore.uncomplete_node-z1ah,
    #   role=WorkFlowyClientCore.uncomplete_node,
    #   slice_labels=node-ops,
    #   kind=ast,
    # ]
    async def uncomplete_node(self, node_id: str, max_retries: int = 10) -> WorkFlowyNode:
        """Mark a node as not completed with exponential backoff retry.

        Cache semantics:
            Best-effort patch of completion state into the /nodes-export cache
            **for this WorkFlowyClientCore instance only**. A daemon task using
            the shared publisher sees that view; independently constructed
            clients retain separate publishers.
        """
        logger = _ClientLogger()
        retry_count = 0
        base_delay = 1.0

        while retry_count < max_retries:
            try:
                response = await self._send_workflowy_write(
                    "POST",
                    f"/nodes/{node_id}/uncomplete",
                    operation="uncomplete_node",
                )
                data = await self._handle_response(response)
                # API returns {"status": "ok"} - fetch updated node
                if isinstance(data, dict) and data.get("status") == "ok":
                    get_response = await self.client.get(f"/nodes/{node_id}")
                    node_data = await self._handle_response(get_response)
                    node = WorkFlowyNode(**node_data["node"])
                else:
                    # Fallback for unexpected format
                    node = WorkFlowyNode(**data)

                # Best-effort: eagerly patch completion state into /nodes-export cache.
                try:
                    if self._cache_populated():
                        self._apply_update_to_nodes_export_cache(node)
                    else:
                        self._mark_nodes_export_dirty([node_id])
                except Exception:
                    try:
                        self._mark_nodes_export_dirty([node_id])
                    except Exception:
                        pass

                return node

            except RateLimitError as e:
                retry_count += 1
                retry_after = self._bounded_rate_limit_retry_delay(
                    e,
                    base_delay * (2**retry_count),
                    "Workflowy CRUD retry",
                )
                logger.warning(
                    f"Rate limited on uncomplete_node. Retry after {retry_after}s. "
                    f"Attempt {retry_count}/{max_retries}"
                )
                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(retry_after, "Workflowy API retry")
                else:
                    raise

            except NetworkError as e:
                retry_count += 1
                logger.warning(
                    f"Network error on uncomplete_node: {e}. Retry {retry_count}/{max_retries}"
                )
                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    raise

            # httpx.TransportError covers timeouts (a subclass) AND dropped
            # connections (RemoteProtocolError "Server disconnected without
            # sending a response", ConnectError, ReadError, WriteError).  Before
            # Manager-Session-129 only timeouts were retried, so a single dropped
            # connection killed whole Cartographer refresh jobs.  MCI S07/S12
            # (Axiom 6) later replaces this blind retry with classified outcomes.
            except httpx.TransportError as err:
                retry_count += 1

                logger.warning(
                    f"Transport error ({type(err).__name__}): {err}. "
                    f"Retry {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    if isinstance(err, httpx.TimeoutException):
                        raise TimeoutError("uncomplete_node") from err
                    raise

        raise NetworkError("uncomplete_node failed after maximum retries")

    # @beacon[
    #   id=auto-beacon@WorkFlowyClientCore.move_node-a4k4,
    #   role=WorkFlowyClientCore.move_node,
    #   slice_labels=node-ops,
    #   kind=ast,
    # ]
    async def move_node(
        self,
        node_id: str,
        parent_id: str | None = None,
        position: str = "top",
        max_retries: int = 10,
    ) -> bool:
        """Move a node to a new parent with exponential backoff retry.

        Args:
            node_id: The ID of the node to move
            parent_id: The new parent node ID (UUID, target key like 'inbox', or None for root)
            position: Where to place the node ('top' or 'bottom', default 'top')
            max_retries: Maximum retry attempts (default 10)

        Returns:
            True if move was successful

        Cache semantics:
            Attempts to update parent/position in the /nodes-export cache
            **for this WorkFlowyClientCore instance only**. Other processes
            have separate cache instances that must be refreshed separately.
        """
        logger = _ClientLogger()
        retry_count = 0
        base_delay = 1.0

        while retry_count < max_retries:
            try:
                payload = {"position": position}
                if parent_id is not None:
                    payload["parent_id"] = parent_id

                response = await self._send_workflowy_write(
                    "POST",
                    f"/nodes/{node_id}/move",
                    json=payload,
                    operation="move_node",
                )
                data = await self._handle_response(response)
                # API returns {"status": "ok"}
                success = data.get("status") == "ok"

                if success:
                    # Best-effort: eagerly maintain the /nodes-export cache when possible.
                    try:
                        if self._cache_populated():
                            self._apply_move_to_nodes_export_cache(node_id, parent_id)
                        else:
                            ids: list[str] = [node_id]
                            if parent_id is not None:
                                ids.append(parent_id)
                            self._mark_nodes_export_dirty(ids)
                    except Exception:
                        # Cache maintenance must never affect API behavior; fall back to dirty flags.
                        try:
                            ids = [node_id]
                            if parent_id is not None:
                                ids.append(parent_id)
                            self._mark_nodes_export_dirty(ids)
                        except Exception:
                            pass

                return success

            except RateLimitError as e:
                retry_count += 1
                retry_after = self._bounded_rate_limit_retry_delay(
                    e,
                    base_delay * (2 ** retry_count),
                    "Workflowy CRUD retry",
                )
                logger.warning(
                    f"Rate limited on move_node. Retry after {retry_after}s. "
                    f"Attempt {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(retry_after, "Workflowy API retry")
                else:
                    raise

            except NetworkError as e:
                retry_count += 1
                logger.warning(
                    f"Network error on move_node: {e}. Retry {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    raise

            # httpx.TransportError covers timeouts (a subclass) AND dropped
            # connections (RemoteProtocolError "Server disconnected without
            # sending a response", ConnectError, ReadError, WriteError).  Before
            # Manager-Session-129 only timeouts were retried, so a single dropped
            # connection killed whole Cartographer refresh jobs.  MCI S07/S12
            # (Axiom 6) later replaces this blind retry with classified outcomes.
            except httpx.TransportError as err:
                retry_count += 1

                logger.warning(
                    f"Transport error ({type(err).__name__}): {err}. "
                    f"Retry {retry_count}/{max_retries}"
                )

                if retry_count < max_retries:
                    await self._sleep_with_operation_cancel(
                        base_delay * (2 ** retry_count),
                        "Workflowy API retry",
                    )
                else:
                    if isinstance(err, httpx.TimeoutException):
                        raise TimeoutError("move_node") from err
                    raise

        raise NetworkError("move_node failed after maximum retries")
