"""Canonical cache-node codec (S01 §7, audit amendment W1).

The ONE semantic ingestion authority later used by full ``/nodes-export``,
live v1 GET, child listings/LLM recovery, journal serialization, and replay.
The runtime cache stays JSON-shaped plain dictionaries — this module is a
codec applied at ingestion seams, not a runtime node wrapper.

W1 per-profile decoder pinning (LOAD-BEARING).  The repository deliberately
contains TWO wire→semantic decoders with DIFFERENT nested-entity behavior;
both are hard-won, working, and LOCKED:

- ``_dewhiten_text`` (cache population / SCRY): chained ``str.replace``
  calls whose outputs CASCADE across the chained calls — a stored
  ``&amp;lt;`` decodes to ``<`` (two layers collapsed).  Extracted here
  VERBATIM (chained-replace order untouched) for ``FULL_EXPORT_REPLACE``;
  byte-compatible with today's cache.
- ``_semantic_name_for_tag_merge`` / ``_note_wire_to_semantic`` (live GET
  names/notes): NON-CASCADING single-pass decoders — ``&amp;lt;`` decodes
  to ``&lt;``, never ``<``.  They are the engineered EXACT INVERSE of
  ``encode_for_workflowy_api``.  The three merge profiles pin THESE
  semantics.  Substituting ``_dewhiten_text`` into any live-GET/merge seam
  is FORBIDDEN, and neither decoder's implementation may be 'corrected'
  inside this program, except for Unit N02′ / CF-101's bare-ampersand
  protection in the live note decoder (entity decode depth stays unchanged).
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any

from .errors import CoherenceSchemaError, EffectValidationError

# ---------------------------------------------------------------------------
# Pinned wire→semantic decoders (CF-101 / Unit N02′ protects bare note ampersands)
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@_dewhiten_text-kkot,
#   role=_dewhiten_text,
#   slice_labels=node-ops,
#   kind=ast,
# ]
def _dewhiten_text(value: str | None) -> str | None:
    """VERBATIM extraction of ``WorkFlowyClientCore._dewhiten_text``.

    Chained replaces CASCADE: ``&amp;lt;`` -> ``&lt;`` -> ``<``.  Only these
    three entities are touched so others (e.g. ``&nbsp;``) remain literal.
    """

    if value is None or not isinstance(value, str):
        return value
    return value.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def _semantic_name_non_cascading(raw_name: str) -> str:
    """VERBATIM extraction of ``_semantic_name_for_tag_merge``.

    Exactly ONE entity layer, NON-CASCADING: output is never re-scanned, so
    ``&amp;lt;`` -> ``&lt;``, never ``<``.  Only ``&amp;`` / ``&lt;`` /
    ``&gt;`` are decoded (the exact inverse alphabet of the name encoder);
    every other ``&`` sequence passes through untouched.
    """

    if not raw_name:
        return ""
    out: list[str] = []
    i = 0
    n = len(raw_name)
    while i < n:
        ch = raw_name[i]
        if ch == "&":
            if raw_name.startswith("&amp;", i):
                out.append("&")
                i += 5
                continue
            if raw_name.startswith("&lt;", i):
                out.append("<")
                i += 4
                continue
            if raw_name.startswith("&gt;", i):
                out.append(">")
                i += 4
                continue
        out.append(ch)
        i += 1
    return "".join(out)


_AUTOLINK_RE = re.compile(r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>')


# Shared with the UPDATE comparator; only semicolon-terminated entities decode.
_BARE_UPDATE_AMPERSAND_RE = re.compile(
    r"&(?!(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#[xX][0-9A-Fa-f]+);)"
)


def _unescape_note_entities(text: str) -> str:
    """Decode one entity layer without interpreting bare ampersands (CF-101)."""
    return html.unescape(_BARE_UPDATE_AMPERSAND_RE.sub("&amp;", text))


def _note_wire_to_semantic(note: str) -> str:
    """Decode live notes, preserving bare ampersands (CF-101 / Unit N02′).

    1. Unwrap Workflowy AUTO-LINK wrappers in the WIRE form (literal
       ``<a href="URL">`` tags; entity-encoded source is invisible to the
       literal-tag regex).  Fingerprint: one-layer-decoded anchor text ==
       one-layer-decoded href.  NAMED links are preserved.
    2. ONE layer of entity decoding (single-pass: ``&amp;lt;`` ->
       ``&lt;`` and ``&lt;`` -> ``<`` — the GUI's decode depth).
       Protect bare ampersands before EVERY unescape, including the anchor
       comparison and returned href, using the UPDATE comparator's one rule.
    """

    def _unwrap_autolink(m: re.Match[str]) -> str:
        href = m.group(1)
        anchor_text = m.group(2)
        if _unescape_note_entities(anchor_text) == _unescape_note_entities(href):
            return _unescape_note_entities(href)  # bare URL
        return m.group(0)  # named link — preserve

    text = _AUTOLINK_RE.sub(_unwrap_autolink, note)
    return _unescape_note_entities(text)


# ---------------------------------------------------------------------------
# Canonical field vocabulary and the MISSING sentinel — §7.2
# ---------------------------------------------------------------------------

CANONICAL_KEYS: tuple[str, ...] = (
    "id",
    "name",
    "note",
    "parent_id",
    "priority",
    "data",
    "createdAt",
    "modifiedAt",
    "completedAt",
    "completed",
)

# Alias chains in precedence order (canonical key first).  Presence, never
# truthiness, selects the alias — explicit null is meaningful.
_ALIAS_CHAINS: dict[str, tuple[str, ...]] = {
    "name": ("name", "nm"),
    "note": ("note", "no"),
    "parent_id": ("parent_id", "parentId"),
    "priority": ("priority",),
    "data": ("data",),
    "createdAt": ("createdAt", "created"),
    "modifiedAt": ("modifiedAt", "modified"),
    "completedAt": ("completedAt",),
    # EXPLICIT SCHEMA DECISION (S01 amendment): `completed` (bool|null) is a
    # first-class canonical key — the v1 wire carries it alongside
    # `completedAt` (P6) — and the legacy `cp` flag is its alias.  The alias
    # mapping is a RAW COPY WITH NO COERCION: whatever value `cp` carries
    # (bool or otherwise) lands verbatim in the canonical `completed` key
    # (F-3: boolean coercion policy is deliberately an S12 concern;
    # `completedAt` is NEVER fabricated from the flag either way).
    # `completed` is canonical but NOT a maskable content field in S01
    # effects (completion flows through the production mechanism; masking
    # is a deliberate S12 decision).
    "completed": ("completed", "cp"),
}
_KNOWN_ALIAS_KEYS = frozenset(
    alias for chain in _ALIAS_CHAINS.values() for alias in chain[1:]
)


class _Missing:
    """Private field-absence sentinel.  NEVER serialized."""

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


# ---------------------------------------------------------------------------
# Ingestion profiles — §7.3 (closed enum; no caller-supplied arbitrary flags)
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@IngestionProfile-c6pn,
#   role=IngestionProfile,
#   slice_labels=mci-cache-codec,
#   kind=ast,
# ]
class IngestionProfile(Enum):
    """Closed ingestion-profile vocabulary."""

    FULL_EXPORT_REPLACE = "full_export_replace"
    LIVE_NODE_MERGE = "live_node_merge"
    CHILD_LISTING_MERGE = "child_listing_merge"
    LLM_RECOVERY_MERGE = "llm_recovery_merge"
    JOURNAL_SEMANTIC = "journal_semantic"


class _ProfileSpec:
    def __init__(
        self,
        *,
        wire_encoded: bool,
        replace: bool,
        authority: frozenset[str],
    ) -> None:
        self.wire_encoded = wire_encoded
        self.replace = replace
        self.authority = authority


_ALL_NODE_FIELDS = frozenset(key for key in CANONICAL_KEYS if key != "id")
# The P6-probed child-listing field set (every canonical field except id).
_LISTING_FIELDS = frozenset(
    {"name", "note", "parent_id", "priority", "data", "createdAt", "modifiedAt", "completedAt", "completed"}
)

_PROFILE_TABLE: dict[IngestionProfile, _ProfileSpec] = {
    # Full /nodes-export snapshot records: cascading dewhiten; authoritative
    # replacement for all fields the record returns.
    IngestionProfile.FULL_EXPORT_REPLACE: _ProfileSpec(
        wire_encoded=True, replace=True, authority=_ALL_NODE_FIELDS
    ),
    # Live single-node GET: NON-CASCADING one-layer decode; partial merge.
    IngestionProfile.LIVE_NODE_MERGE: _ProfileSpec(
        wire_encoded=True, replace=False, authority=_ALL_NODE_FIELDS
    ),
    # Authoritative parent listing: NON-CASCADING; only endpoint-declared
    # child-listing fields may become authoritative.
    IngestionProfile.CHILD_LISTING_MERGE: _ProfileSpec(
        wire_encoded=True, replace=False, authority=_LISTING_FIELDS
    ),
    # LLM identity recovery uses the same wire/text semantics and field
    # authority as the authoritative v1 listing.
    IngestionProfile.LLM_RECOVERY_MERGE: _ProfileSpec(
        wire_encoded=True, replace=False, authority=_LISTING_FIELDS
    ),
    # Journal after-states are already semantic: ZERO additional dewhitening.
    IngestionProfile.JOURNAL_SEMANTIC: _ProfileSpec(
        wire_encoded=False, replace=False, authority=_ALL_NODE_FIELDS
    ),
}


def profile_spec(profile: IngestionProfile) -> _ProfileSpec:
    if not isinstance(profile, IngestionProfile):
        raise CoherenceSchemaError(f"unknown ingestion profile: {profile!r}")
    return _PROFILE_TABLE[profile]


# ---------------------------------------------------------------------------
# Canonicalization — §7.2
# ---------------------------------------------------------------------------


def _select_alias(raw: Mapping[str, Any], chain: Iterable[str]) -> Any:
    """Select the first PRESENT key in an alias chain (presence, not truthiness)."""

    for key in chain:
        if key in raw:
            return raw[key]
    return MISSING


def _decode_text(value: Any, profile: IngestionProfile, *, field: str) -> Any:
    """Apply the profile's pinned text decoding to a name/note value."""

    if value is MISSING or value is None:
        return value
    spec = profile_spec(profile)
    if not spec.wire_encoded:
        # JOURNAL_SEMANTIC: semantic literal text such as '&' survives
        # byte-for-byte; zero additional dewhitening.
        return value
    if not isinstance(value, str):
        return value
    if profile is IngestionProfile.FULL_EXPORT_REPLACE:
        return _dewhiten_text(value)
    if field == "name":
        return _semantic_name_non_cascading(value)
    return _note_wire_to_semantic(value)


# @beacon[
#   id=coherence@canonical-codec,
#   slice_labels=ra-workflowy-cache,mci-cache-codec,
#   kind=ast,
#   comment=S01 canonical cache-node codec: closed ingestion profiles with per-profile pinned decoders (W1), alias precedence by presence, MISSING-vs-null distinction, unknown-key preservation, never-mutates-input.,
# ]
def canonicalize_cache_node(
    raw: Mapping[str, Any], profile: IngestionProfile
) -> dict[str, Any]:
    """Canonicalize one raw node record under a closed ingestion profile.

    Output is a sparse plain dict: canonical keys appear only when present
    (explicit null included) in the source; unknown source keys are
    preserved verbatim; consumed alias keys are not echoed.  The input
    mapping is never mutated.
    """

    if not isinstance(raw, Mapping):
        raise CoherenceSchemaError("canonical node: input must be a mapping")
    profile_spec(profile)  # validates the profile

    node_id = raw.get("id")
    if isinstance(node_id, bool) or node_id is None or not str(node_id).strip():
        raise CoherenceSchemaError("canonical node: id is required and nonblank")
    canonical: dict[str, Any] = {"id": str(node_id)}

    for key, chain in _ALIAS_CHAINS.items():
        value = _select_alias(raw, chain)
        if value is MISSING:
            continue
        if key in ("name", "note"):
            value = _decode_text(value, profile, field=key)
        canonical[key] = value

    for key, value in raw.items():
        if key in CANONICAL_KEYS or key in _KNOWN_ALIAS_KEYS or key == "id":
            continue
        canonical[key] = value
    return canonical


# @beacon[
#   id=auto-beacon@merge_canonical_cache_node-upab,
#   role=merge_canonical_cache_node,
#   slice_labels=mci-cache-codec,
#   kind=ast,
# ]
def merge_canonical_cache_node(
    existing: Mapping[str, Any] | None,
    incoming: Mapping[str, Any],
    *,
    profile: IngestionProfile,
) -> dict[str, Any]:
    """Field-preserving canonical merge for PARTIAL endpoint records.

    REPLACE profiles are rejected (full-snapshot replacement is
    ``canonicalize_cache_node`` itself).  Absent fields preserve existing
    cache values; explicit null clears a field only when that field is
    authoritative for the profile; unknown/cache-only metadata on both
    sides is preserved; a partial endpoint record can never erase a field
    the endpoint omitted.  Inputs are never mutated.
    """

    spec = profile_spec(profile)
    if spec.replace:
        raise EffectValidationError(
            f"profile {profile.value} uses replace semantics; merge is not applicable"
        )
    canonical_incoming = canonicalize_cache_node(incoming, profile)
    incoming_id = canonical_incoming["id"]

    result: dict[str, Any] = dict(existing) if existing is not None else {}
    existing_id = result.get("id")
    if existing_id is not None and str(existing_id) != incoming_id:
        raise EffectValidationError(
            f"canonical merge id mismatch: existing {existing_id!r} vs incoming {incoming_id!r}"
        )
    result["id"] = incoming_id

    for key, value in canonical_incoming.items():
        if key == "id":
            continue
        if key in _ALIAS_CHAINS and key not in spec.authority:
            # The endpoint may not authoritatively supply this field; keep
            # the existing cache value.
            continue
        result[key] = value
    return result


def canonicalize_nodes_export(
    payload: Mapping[str, Any], *, profile: IngestionProfile = IngestionProfile.FULL_EXPORT_REPLACE
) -> dict[str, Any]:
    """Canonicalize a whole ``/nodes-export`` payload (``{"nodes": [...]}``).

    Returns a NEW payload with every node canonicalized.  Duplicate or
    missing node IDs fail closed.  The input is never mutated.
    """

    if not isinstance(payload, Mapping):
        raise CoherenceSchemaError("nodes-export payload must be a mapping")
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        raise CoherenceSchemaError("nodes-export payload is missing 'nodes' list")
    seen: set[str] = set()
    canonical_nodes: list[dict[str, Any]] = []
    for raw in nodes:
        node = canonicalize_cache_node(raw, profile)
        node_id = node["id"]
        if node_id in seen:
            raise CoherenceSchemaError(f"nodes-export payload has duplicate id {node_id!r}")
        seen.add(node_id)
        canonical_nodes.append(node)
    result = dict(payload)
    result["nodes"] = canonical_nodes
    return result
