"""GLIMPSE timestamp-sort + `since` recent-tail / curation-cursor policy.

PURE, dependency-light tree transforms shared by BOTH the server-side
cache-sourced GLIMPSE paths (``server.py``: slice_group, root_uuid, Form-B
``since="curated"``) AND the client-side live-DOM GLIMPSE path
(``api_client_nexus.py``). Every `since` call site on both sides routes through
the single orchestrator ``apply_since_to_forest`` below.

This module imports NOTHING from ``server.py``, ``api_client_nexus.py``, or
``f8_slice_policy.py``; both sides import from HERE. Keeping the shared logic
in one dependency-free module is what lets the server and client share ONE
implementation without a client<-server import cycle (which is why the
transforms live here rather than on either caller).

Design notes
------------
* NO FastMCP, NO WebSocket, NO Workflowy API, NO server globals, NO client
  class. Just pure functions over ``list[dict]`` node forests + a
  ``cache_by_id`` timestamp map.
* ``branch_timestamp_key`` is THE single branch-date rule (created = leaf's
  own ``createdAt``; modified = ``max(modifiedAt)`` over the whole branch), so
  the SORT and the `since` PIVOT can never disagree on what a branch's date
  means. ``reorder_tree_by_timestamp`` and every `since` helper call it.
* ``compute_curated_high_water_mark`` (Form B) takes a ``curated_predicate``
  CALLABLE rather than importing ``f8_slice_policy.node_is_curated_for_prefixes``
  directly — this keeps the module free of any ``f8_slice_policy`` import (the
  server passes the predicate in) and makes the curation rule injectable/
  testable in isolation.

Back-compat: ``WorkFlowyClientNexus._branch_timestamp_key_static`` and
``._reorder_tree_by_timestamp`` remain as THIN DELEGATING wrappers (many other
call sites — the docked modal, ``_render_slice_row_view`` — call them by those
names), and ``server.py`` keeps its ``_parse_since_window`` /
``_compute_since_window_pivot`` / ``_prune_tree_since`` /
``_compute_curated_high_water_mark`` names as thin wrappers too. The IMPLEMENTATION
now lives here, once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable


# @beacon[
#   id=since-policy@unit-seconds,
#   slice_labels=nexus--md-preview,
#   kind=ast,
#   comment=Duration-unit -> seconds table for the Form-A `since` window token grammar (s|m|h|d|w). Single source of truth now that the transform lives in the pure glimpse_since_policy module; server.py re-exports it for back-compat.,
# ]
SINCE_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


# The set of valid `since` forms + the two axis/direction enums. The axis is
# the SINCE object's OWN membership timestamp axis -- deliberately NOT borrowed
# from the presentation sort (that borrowing was the coupling this decouple
# removes). See SinceSpec.
SINCE_FORMS = ("window", "item_count", "curated")
SINCE_AXES = ("created", "modified")
SINCE_DIRECTIONS = ("asc", "desc")

# [ap:TVIIBN] 4d(a): what an item-count `since` selects. 'newest' keeps the N
# NEWEST leaves by the recency basis (recent_by, resolved against the
# presentation sort; see resolve_item_count_basis) -- the Dan ruling "last N
# means the newest N, everywhere". 'bottom' keeps the POSITIONAL tail of the
# current order -- exactly the legacy item-count behavior, and the meaning of
# every OLD wire blob that predates the selection field (wire back-compat).
SINCE_ITEM_SELECTIONS = ("newest", "bottom")

# [ap:TVIIBN] 4d(a): the item-count keyword -> selection mapping (the ONE
# token-grammar table). 'last N' and 'newest N' are synonyms selecting the
# newest; 'bottom N' is the positional tail.
_ITEM_COUNT_SELECTION_BY_KEYWORD = {
    "last": "newest",
    "newest": "newest",
    "bottom": "bottom",
}


# @beacon[
#   id=since-policy@since-spec,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=THE opaque `since` handle: a normalized, immutable membership-filter request carrying its OWN timestamp axis (created|modified) + direction (asc|desc), so the date-form pivots (window / curated) measure branch-dates against the axis the SINCE object names rather than borrowing the PRESENTATION sort. form is 'window'|'item_count'|'curated'; window_seconds is set for a window, count for item_count, both None for curated. axis/direction default to created/asc and are INERT for form=='item_count' (a pure tail-count over the forest's current order has no date pivot). item_count also carries selection ([ap:TVIIBN] 4d(a)): 'newest' keeps the N newest leaves by the recency basis (resolve_item_count_basis over recent_by/sort_by), 'bottom' keeps the positional tail of the current order; the FIELD default is 'bottom' (the legacy positional meaning, so a hand-built / old-wire item_count spec behaves exactly as before), and the token codec sets 'newest' for 'last N'/'newest N' and 'bottom' for 'bottom N' (since_from_wire defaults an object without the key to 'bottom' -- wire back-compat). A raw human/API token string is parsed ONLY at the edge by since_from_edge, which delegates to SinceCodec.from_token; interior code carries SinceSpec or None only. since_to_wire serializes SinceSpec to an object dict or None; since_from_wire reads that object dict back and rejects strings. Display token text is produced only by SinceCodec.to_token.,
# ]
@dataclass(frozen=True)
class SinceSpec:
    """One normalized, immutable `since` membership-filter request.

    The OPAQUE `since` handle. It carries its OWN timestamp ``axis`` +
    ``direction`` so the date-form pivots (window / curated) measure
    branch-dates against the axis the SINCE object names — never the
    presentation ``sort_by``. This is what lets a date-form ``since`` work
    under any presentation sort, including plain document order.

    Fields
    ------
    form : ``'window'`` | ``'item_count'`` | ``'curated'``.
    window_seconds : the Form-A window length in seconds (``form=='window'``
        only; ``None`` otherwise).
    count : the Form-C item count N (``form=='item_count'`` only; ``None``
        otherwise).
    axis : ``'created'`` | ``'modified'`` — the SINCE object's own membership
        timestamp axis. Defaults to ``'created'``.
    direction : ``'asc'`` | ``'desc'`` — the SINCE object's own direction.
        Defaults to ``'asc'``.
    selection : ``'newest'`` | ``'bottom'`` — what an item-count ``since``
        SELECTS ([ap:TVIIBN] 4d(a)). ``'newest'`` keeps the N newest leaves by
        the recency basis (Dan's ruling: ``'last N'`` means the newest N, and
        ``sort_direction`` never changes which rows were chosen);
        ``'bottom'`` keeps the positional tail of the current order — exactly
        the legacy item-count behavior, and the meaning of every old wire
        blob that predates this field. The FIELD default is ``'bottom'`` so a
        hand-built or legacy-decoded item_count spec keeps yesterday's
        semantics; the token codec and the wire writer always set it
        explicitly.

    ⚠ ``axis`` / ``direction`` are LOAD-BEARING only for the DATE forms
    (``window`` + ``curated``). For ``form=='item_count'`` they are INERT: an
    item count never pivots on a date it carries itself — a ``'newest'``
    selection's basis comes from ``recent_by`` / the presentation sort
    (see :func:`resolve_item_count_basis`), and a ``'bottom'`` selection is
    positional. They still default cleanly (created/asc) so the object is
    uniform.

    Immutable + normalized-at-construction, mirroring
    :class:`cache_render_spec.RenderProjectionSpec`: only
    :func:`since_from_edge` / :func:`since_from_wire` build one, so no caller can produce an
    unnormalized instance.
    """

    form: str
    window_seconds: int | None = None
    count: int | None = None
    axis: str = "created"
    direction: str = "asc"
    selection: str = "bottom"

    def is_default_chronology(self) -> bool:
        """True iff axis=created AND direction=asc.

        The display-token writer may use this to choose the compact human
        spelling. The wire writer does not use it to collapse shape: interior
        wire form is always an object dict for a non-None SinceSpec.
        """
        return self.axis == "created" and self.direction == "asc"


# @beacon[
#   id=since-policy@render-is-chronological,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=THE chronological-render predicate (U6, ap:E03BN3 -- Dan's ruling 'any chronological sort of any form => never display ancestor notes'): True iff a render is CHRONOLOGICAL -- a resolved presentation sort_by of 'created' or 'modified' (EITHER direction) OR ANY since (window / curated / item_count). An item-count since ALONE counts since CF-13 = Option A (Dan's ruling 2026-09-22, relayed Manager-Session-033; supersedes U6 ap:UXYCID §2 invariant 6 + §9 tests 3/10) -- under [ap:TVIIBN] 4d(a) of EITHER selection: 'last N'/'newest N' is now the genuine newest-N recency cut, and the positional 'bottom N' (also the legacy-blob reading) keeps CF-13's tail behavior; both withhold ancestor notes exactly like the date forms while their excerpt order STAYS document order unless the caller sorts (the predicate classifies NOTE PRESENTATION only, never ordering). Callers evaluate it on the RESOLVED sort_by (after slice-group default inheritance; None/'none' = document order) and on the SinceSpec actually handed to Stage-2 prep. THE single definition of 'chronological' every materializer-fed render surface consults to decide ancestor-note body suppression (the third RenderPolicy cause, occurrence-keyed, computed ONLY inside materialize_from_document_forest). Direction is deliberately NOT a parameter: created OR modified, asc OR desc -- one rule, no created/modified distinction.,
# ]
def render_is_chronological(
    sort_by: str | None,
    since_spec: "SinceSpec | None",
) -> bool:
    """True iff this render is CHRONOLOGICAL (U6 ancestor-note rule trigger).

    THE one predicate behind Dan's ruling (ap:E03BN3): *if it is a
    chronological sort of any form — ``recent``, the curated tail, an explicit
    ``sort_by`` created **or** modified, ascending **or** descending — then
    automatically do not display ancestor notes.*

    Chronological iff EITHER:

      * the RESOLVED presentation ``sort_by`` is ``'created'`` or
        ``'modified'`` (either direction — direction is not an input to this
        rule), OR
      * ``since_spec`` is not None — ANY form counts: ``form in {'window',
        'curated', 'item_count'}`` (the ``recent`` window / curated-tail /
        item-count-tail selections).

    ``form == 'item_count'`` counts since CF-13 = Option A
    (Dan's ruling 2026-09-22, relayed Manager-Session-033; supersedes the
    LOCKED U6 spec's §2 invariant 6 and §9 test-3 expectation) -- and under
    [ap:TVIIBN] 4d(a) that holds for EITHER selection: ``'last N'`` /
    ``'newest N'`` is the newest-N recency cut; the positional ``'bottom N'``
    (and every old wire blob, which decode positional) keeps the tail trim.
    A BARE
    item-count selection — no sort, no date window — withholds ancestor notes
    exactly like the date forms, the Dock's own long description included.
    What the ruling does NOT change: the excerpt's order. An item-count
    selection stays a trim of the CURRENT order (document order when
    unsorted, whatever the direction) — this predicate classifies NOTE
    PRESENTATION only; membership, ordering and leaf identity are never its
    concern.

    ``sort_by`` must be the RESOLVED value: ``None`` (internal natural order)
    and the wire sentinel ``'none'`` both mean document order => False. Only
    ``'created'`` / ``'modified'`` count — callers resolve slice-group default
    sort inheritance BEFORE consulting (``_resolve_slice_group_effective_sort``
    / ``sort_by_internal()``), so an inherited chronological default counts
    exactly like an explicit one.

    Pure; no I/O. Membership, ordering and leaf identity are never this
    predicate's concern — it classifies the PRESENTATION only.
    """
    if sort_by in ("created", "modified"):
        return True
    if since_spec is not None and getattr(since_spec, "form", None) in (
        "window",
        "curated",
        "item_count",
    ):
        return True
    return False


# @beacon[
#   id=since-policy@since-codec,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=THE Python since token codec. Owns the human/API token grammar only: SinceCodec.from_token parses tokens like '1w' / 'last 10' / 'curated' into SinceSpec, and SinceCodec.to_token renders a SinceSpec as the canonical display token. It does NOT read or write the interior wire shape; wire paths use since_from_wire / since_to_wire.,
# ]
class SinceCodec:
    """Codec for the human/API since token grammar.

    This class owns token strings only. It is used by public edge ingestion and
    display formatting. Interior wire/blob/render-spec paths use
    since_from_wire / since_to_wire and must not call this codec as a bridge.
    """

    @staticmethod
    def _split_token_and_chronology(token: str) -> tuple[str, str, str]:
        """Split a human token into base token + axis/direction suffix."""
        base, sep, suffix = token.partition("@")
        axis = "created"
        direction = "asc"
        if sep:
            parts = suffix.strip().lower().split()
            if parts and parts[0] in SINCE_AXES:
                axis = parts[0]
            if len(parts) > 1 and parts[1] in SINCE_DIRECTIONS:
                direction = parts[1]
        return base.strip(), axis, direction

    @staticmethod
    def _append_chronology_suffix(token: str, spec: "SinceSpec") -> str:
        """Append the canonical non-default axis/direction suffix if needed."""
        if spec.form == "item_count" or spec.is_default_chronology():
            return token
        return f"{token}@{spec.axis} {spec.direction}"

    @staticmethod
    def from_token_result(token: str) -> tuple["SinceSpec | None", str | None]:
        """Parse a human/API token and return the object plus any error."""
        tok, axis, direction = SinceCodec._split_token_and_chronology(token.strip())
        if not tok:
            return None, None
        cls = classify_since_token(tok)
        if cls["error"]:
            return None, cls["error"]
        if cls["form"] is None:
            return None, None
        if cls["form"] == "item_count":
            n = parse_since_item_count(tok)
            if n is None:
                return None, cls["error"]
            # [ap:TVIIBN] 4d(a): the FIRST keyword decides the selection --
            # 'last N'/'newest N' select the newest N (Dan's ruling), 'bottom N'
            # keeps the positional tail. The @axis/direction suffix stays INERT
            # for item_count (selection is never direction-dependent).
            first = tok.split()[0].lower()
            selection = _ITEM_COUNT_SELECTION_BY_KEYWORD.get(first, "bottom")
            return (
                SinceSpec(form="item_count", count=n, selection=selection),
                None,
            )
        if cls["form"] == "curated":
            return SinceSpec(form="curated", axis=axis, direction=direction), None
        ws = parse_since_window(tok)
        return (SinceSpec(form="window", window_seconds=ws, axis=axis, direction=direction), None) if ws is not None else (None, cls["error"])

    @staticmethod
    def from_token(token: str) -> "SinceSpec | None":
        """Parse a human/API since token into SinceSpec."""
        spec, _error = SinceCodec.from_token_result(token)
        return spec

    @staticmethod
    def to_token(spec: "SinceSpec | None") -> str:
        """Render a SinceSpec as a canonical human/display token."""
        if spec is None:
            return ""
        if spec.form == "item_count":
            # [ap:TVIIBN] 4d(a): the positional tail spells 'bottom N'; the
            # newest selection keeps the established 'last N' display spelling
            # (now truthfully meaning the newest N).
            if str(getattr(spec, "selection", "bottom") or "bottom") == "bottom":
                return f"bottom {int(spec.count)}"
            return f"last {int(spec.count)}"
        if spec.form == "curated":
            return SinceCodec._append_chronology_suffix("curated", spec)
        return SinceCodec._append_chronology_suffix(f"{int(spec.window_seconds)}s", spec)


# @beacon[
#   id=since-policy@since-from-edge,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=EDGE-ONLY Python ingester for public/human/API input. Accepts only the public edge shape (a token string or None), delegates token parsing to SinceCodec.from_token, and returns SinceSpec|None. Interior wire/blob/render-spec paths must call since_from_wire instead, which rejects strings.,
# ]
def since_from_edge(value: str | None) -> "SinceSpec | None":
    """Normalize the public edge since argument to SinceSpec/None.

    EDGE ONLY. The public edge shape is a human/API token string or None.
    Interior object wire/blob/render-spec paths must call since_from_wire
    instead, and display paths must call SinceCodec.to_token.
    """
    spec, _error = since_from_edge_result(value)
    return spec


def since_from_edge_result(value: str | None) -> tuple["SinceSpec | None", str | None]:
    """Normalize the public edge since argument, returning parse errors."""
    if value is None:
        return None, None
    return SinceCodec.from_token_result(value)

# @beacon[
#   id=since-policy@since-from-wire,
#   slice_labels=nexus--md-preview,
#   kind=ast,
#   comment=Strict interior wire reader for `since`. Accepts None, SinceSpec, or an object dict and returns SinceSpec|None. A string on this path is invalid and is rejected rather than parsed; public token strings belong only to since_from_edge / SinceCodec.from_token.,
# ]
def since_from_wire(value: Any) -> "SinceSpec | None":
    """Decode interior wire since shape into SinceSpec/None.

    INTERIOR ONLY. Accepted shapes are None, an existing SinceSpec, or an
    object dict. A string on this path is invalid and is not parsed.
    """
    if value is None:
        return None
    if isinstance(value, SinceSpec):
        return value
    if isinstance(value, str):
        raise TypeError("since wire value must be an object or null, not a string")
    if not isinstance(value, dict):
        return None

    form = value.get("form")
    if form not in SINCE_FORMS:
        return None
    axis = value.get("axis")
    axis = axis if axis in SINCE_AXES else "created"
    direction = value.get("direction")
    direction = direction if direction in SINCE_DIRECTIONS else "asc"
    if form == "window":
        ws = value.get("window_seconds")
        return SinceSpec(form="window", window_seconds=ws, axis=axis, direction=direction) if isinstance(ws, int) and ws > 0 else None
    if form == "item_count":
        n = value.get("count")
        if not (isinstance(n, int) and n > 0):
            return None
        # [ap:TVIIBN] 4d(a) WIRE BACK-COMPAT: an old item_count object with no
        # `selection` field keeps its POSITIONAL meaning ('bottom') -- the
        # selection is an ADDED field, never a re-reading of existing blobs.
        selection = value.get("selection")
        if selection not in SINCE_ITEM_SELECTIONS:
            selection = "bottom"
        return SinceSpec(form="item_count", count=n, axis=axis, direction=direction, selection=selection)
    return SinceSpec(form="curated", axis=axis, direction=direction)



# @beacon[
#   id=since-policy@since-to-wire,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=INTERIOR Py wire writer for `since`. Serializes SinceSpec|None to object-dict-or-None only; it never emits a token string. Human-readable token strings belong solely to SinceCodec.to_token for display.,
# ]

def since_to_wire(spec: "SinceSpec | None") -> dict[str, Any] | None:
    """Serialize SinceSpec to the interior wire object form.

    The wire is interior. Therefore every non-None SinceSpec becomes an object
    dict, including default created/asc chronology. Human-readable strings are
    produced only by SinceCodec.to_token for display.
    """
    if spec is None:
        return None
    out: dict[str, Any] = {
        "form": spec.form,
        "axis": spec.axis,
        "direction": spec.direction,
    }
    if spec.form == "window":
        out["window_seconds"] = int(spec.window_seconds)
    elif spec.form == "item_count":
        out["count"] = int(spec.count)
        # [ap:TVIIBN] 4d(a): the selection rides the wire EXPLICITLY so every
        # reader (server blob decode, JS twin) re-derives the same selection;
        # an old blob without the key still decodes to 'bottom' (positional).
        out["selection"] = (
            spec.selection if spec.selection in SINCE_ITEM_SELECTIONS else "bottom"
        )
    return out

# @beacon[
#   id=since-policy@parse-since-window,
#   slice_labels=nexus--md-preview,
#   kind=ast,
#   comment=Parse a Form-A `since` window token (<number><unit? s|m|h|d|w>; a BARE number means DAYS) into SECONDS. Case-insensitive, whitespace-tolerant. Returns None for a blank / malformed / non-positive token (caller raises). THE single token grammar for every GLIMPSE `since` path; moved into the pure module so server + client agree.,
# ]
def parse_since_window(token: Any) -> int | None:
    """Parse a Form-A `since` window token into seconds (epoch-second units).

    Grammar: ``<number><unit?>`` where unit is one of ``s|m|h|d|w`` (seconds,
    minutes, hours, days, weeks). A bare number (no unit) means DAYS -- the
    common ``since="7"`` == one week case. Case-insensitive, whitespace-
    tolerant. Returns the window length in SECONDS, or ``None`` for a
    blank / malformed / non-positive token (caller raises).
    """
    if token is None:
        return None
    s = str(token).strip().lower()
    if not s:
        return None
    unit = "d"  # bare number => days
    if s[-1] in SINCE_UNIT_SECONDS:
        unit = s[-1]
        s = s[:-1].strip()
    if not s:
        return None
    try:
        qty = float(s)
    except ValueError:
        return None
    if qty <= 0:
        return None
    return int(round(qty * SINCE_UNIT_SECONDS[unit]))


# @beacon[
#   id=since-policy@parse-since-item-count,
#   slice_labels=nexus--md-preview,
#   kind=ast,
#   comment=Parse a Form-C `since` ITEM-COUNT token: 'last N' / its synonym 'newest N' / the positional 'bottom N' (optionally '... N item[s]'), N a positive integer. Case-insensitive, whitespace-tolerant. Returns the integer N, or None when the token is not an item-count token at all (so the caller can fall through to the Form-A window grammar) OR is a malformed 'last ...' (N missing / non-integer / <= 0) -- the caller distinguishes 'not mine' from 'mine but bad' by first checking is_item_count_token. WHAT the N selects is decided by the FIRST keyword via _ITEM_COUNT_SELECTION_BY_KEYWORD (last/newest -> the newest N, [ap:TVIIBN] 4d(a); bottom -> the positional tail); this function owns only the count. THE single item-count grammar for every GLIMPSE `since` path; the readable 'last 10' spelling was chosen over a '10i' pseudo-unit so it never collides with the bare-number=days Form-A rule.,
# ]
_ITEM_COUNT_KEYWORDS = ("last", "newest", "bottom")


def parse_since_item_count(token: Any) -> int | None:
    """Parse a Form-C `since` item-count token into an int.

    Grammar: ``last <N>`` | its synonym ``newest <N>`` | the positional
    ``bottom <N>``, each with an optional trailing ``item`` / ``items`` (e.g.
    ``"last 10"``, ``"newest 10"``, ``"bottom 10 items"``). ``N`` must be a
    positive integer. Case-insensitive, whitespace-tolerant.

    Returns the integer ``N`` on a well-formed token, else ``None``. NOTE the
    ``None`` return is INTENTIONALLY ambiguous between 'this is not an
    item-count token at all' (so a caller may fall through to
    :func:`parse_since_window`) and 'this IS an item-count token but
    malformed'. Callers that need to tell those apart test
    :func:`is_item_count_token` first (a cheap prefix check), then treat a
    ``None`` from *this* function as a malformed count -> loud error, exactly
    as :func:`parse_since_window` returning ``None`` on a non-blank token is
    treated as a malformed window.

    The keyword decides WHAT is selected (``_ITEM_COUNT_SELECTION_BY_KEYWORD``):
    ``last`` / ``newest`` select the NEWEST N ([ap:TVIIBN] 4d(a), Dan's
    ruling — ``sort_direction`` only orders, never chooses);
    ``bottom`` keeps the POSITIONAL tail of the current order. This function
    owns only the count grammar.

    The readable ``last N`` spelling is deliberate: a bare number already means
    DAYS in :func:`parse_since_window` (``since="7"`` == one week), so an
    item-count token MUST carry an unambiguous marker. ``last`` reads clearly
    and cannot collide with the ``<number><unit>`` window grammar.
    """
    if token is None:
        return None
    s = str(token).strip().lower()
    if not s:
        return None
    parts = s.split()
    # Shape: ('last' | 'newest') <N> ['item'|'items'].
    if len(parts) < 2 or parts[0] not in _ITEM_COUNT_KEYWORDS:
        return None
    if len(parts) == 3 and parts[2] not in ("item", "items"):
        return None
    if len(parts) > 3:
        return None
    num = parts[1]
    if not num.isdigit():
        return None
    n = int(num)
    if n <= 0:
        return None
    return n


# @beacon[
#   id=since-policy@is-item-count-token,
#   slice_labels=nexus--md-preview,
#   kind=ast,
#   comment=Cheap prefix classifier: does this `since` token START the item-count family ('last ...' | 'newest ...' | 'bottom ...')? Lets callers ROUTE a token to the item-count grammar (parse_since_item_count) vs the window grammar (parse_since_window) BEFORE parsing, so a malformed 'last banana' is reported as a bad item-count rather than silently falling through to the window parser. Pure; no I/O.,
# ]
def is_item_count_token(token: Any) -> bool:
    """True iff ``token`` is (syntactically) an item-count token.

    A cheap prefix check used to ROUTE a `since` token to the item-count
    grammar rather than the window grammar. It matches the ``last`` /
    ``newest`` / ``bottom`` keyword followed by whitespace (so ``"last"``
    alone, ``"lastly"``, ``"7"`` do NOT match). It does NOT validate ``N`` --
    that is :func:`parse_since_item_count` -- so ``"last banana"`` returns
    True here and then parses to ``None``, which the caller treats as a
    MALFORMED item-count (loud error) instead of silently trying the window
    grammar.
    """
    if token is None:
        return False
    s = str(token).strip().lower()
    return any(s == word or s.startswith(word + " ") for word in _ITEM_COUNT_KEYWORDS)


def _id_of(node: dict[str, Any]) -> str:
    return str(node.get("id") or node.get("uuid") or "")


# @beacon[
#   id=since-policy@collect-leaf-branches,
#   slice_labels=nexus--md-preview,
#   kind=ast,
#   comment=Shared DFS that decomposes a node forest into its root->leaf BRANCHES, each a list of (uuid, node) from a top-level node down to a leaf (a node with no dict children). This is the ONE branch-collection walk reused by reorder_tree_by_timestamp, prune_tree_since, compute_since_window_pivot, AND prune_tree_to_last_leaf_count -- the definition of 'leaf' (no VISIBLE/dict children; hidden descendants are absent from the final forest) is therefore single-sourced, which is exactly what makes item-count's 'count only leaves' rule fall out for free. Preserves DFS/document order of leaves. Pure; inputs never mutated.,
# ]
def collect_leaf_branches(
    nodes: list[dict[str, Any]] | None,
) -> list[list[tuple[str, dict[str, Any]]]]:
    """Decompose a forest into root->leaf branches in DFS (document) order.

    Each branch is a list of ``(uuid, node)`` pairs from a top-level node down
    to a LEAF -- a node with no ``dict`` children in ``children``. Because the
    final render forest never contains hidden descendants, 'leaf' here is
    exactly 'no visible descendants', which is the definition item-count needs.

    The ONE branch-collection walk shared by the reorder, the window pivot, the
    since prune, and the item-count prune, so the leaf definition + branch
    order can never drift between them. Pure; ``nodes`` are never mutated.
    """
    branches: list[list[tuple[str, dict[str, Any]]]] = []

    def walk(node: dict[str, Any], spine: list[tuple[str, dict[str, Any]]]) -> None:
        if not isinstance(node, dict):
            return
        here = spine + [(_id_of(node), node)]
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        if not children:
            branches.append(here)
            return
        for ch in children:
            walk(ch, here)

    for top in (nodes or []):
        walk(top, [])
    return branches


# @beacon[
#   id=since-policy@rebuild-forest-from-kept-leaves,
#   slice_labels=nexus--md-preview,
#   kind=ast,
#   comment=Shared prune-rebuild: given a forest and a set of KEPT leaf node object ids (id(node)), rebuild a NEW forest keeping only root->leaf paths that terminate in a kept leaf (connective ancestors preserved; dead branches dropped). Returns shadow dicts (inputs never mutated). Reused by prune_tree_since AND prune_tree_to_last_leaf_count so the 'keep the paths to these surviving leaves' mechanic is single-sourced; the two differ ONLY in HOW they choose the kept-leaf set (date-vs-pivot vs the last-N-in-order).,
# ]
def _rebuild_forest_from_kept_leaves(
    nodes: list[dict[str, Any]],
    kept_leaf_objs: set[int],
) -> list[dict[str, Any]]:
    """Rebuild a forest keeping only paths terminating in a kept leaf.

    ``kept_leaf_objs`` is a set of ``id(node)`` identities for the LEAF node
    objects to keep (identity, not uuid, so repeated-uuid straddled ancestors
    are irrelevant -- we key on the physical leaf object the branch walk saw).
    Connective ancestors of a surviving leaf are kept; a subtree with no
    surviving leaf is dropped. Returns NEW shadow dicts; inputs untouched.
    """

    def prune(node: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(node, dict):
            return None
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        if not children:
            return dict(node) if id(node) in kept_leaf_objs else None
        kept_children: list[dict[str, Any]] = []
        for ch in children:
            pr = prune(ch)
            if pr is not None:
                kept_children.append(pr)
        if not kept_children:
            return None
        out = dict(node)
        out["children"] = kept_children
        return out

    out_forest: list[dict[str, Any]] = []
    for top in nodes:
        pr = prune(top)
        if pr is not None:
            out_forest.append(pr)
    return out_forest


# @beacon[
#   id=since-policy@cap-forest-first-node-count,
#   slice_labels=nexus--md-preview,slices,glimpse-render,
#   kind=ast,
#   comment=THE ONE node-limit cap for the search verbs' headings tier ([ap:O3MDOY] half 2, unit 5e): slice_group.sift / lightning_rod.render_search with max_nodes truncate the FINAL render forest (after the sort and the since prune, so selection -> order -> cap compose in that order, mirroring the grep tier) to the first max_nodes MATCHING nodes in display order, counted through the caller's member predicate (the lens's sift predicate / the search predicate); connective ancestors never consume budget and survive only while a kept descendant does. Not a since form and not a ranking: a positional display cap beside the prune family -- the count-and-stop mechanic mirrors prune_tree_to_last_leaf_count's positional tail at the node level. Returns (new_forest, echo|None); echo is None when nothing was hidden (the whole forest fit), so callers stay byte-identical otherwise. Pure; inputs never mutated.,
# ]
def cap_forest_to_first_node_count(
    nodes: list[dict[str, Any]] | None,
    max_nodes: int,
    *,
    member_predicate: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Cap a final render forest to the first ``max_nodes`` matching nodes.

    Display order (preorder, parents before children) decides which matching
    nodes survive: each node satisfying ``member_predicate`` (every node when
    the predicate is None) consumes one unit of budget until it is exhausted;
    a matching node with no budget left is dropped WITH its subtree, and a
    connective ancestor is kept only while it still has a kept descendant.
    Composition is the ONE rule ([ap:O3MDOY]): recency selection first, then
    the presentation sort, then this display cap -- so ``recent='last 10'
    max_nodes=5`` shows the 5 oldest-displayed of the 10 newest.

    Returns ``(forest, echo)``; ``echo`` is ``None`` when nothing was hidden,
    else ``{"max_nodes", "matched", "shown", "hidden"}`` where ``matched``
    counts the matching nodes the WHOLE forest carried (connective excluded)
    and ``shown`` the ones retained.
    """

    def counts(node: dict[str, Any]) -> int:
        total = 1 if (member_predicate is None or member_predicate(node)) else 0
        for child in (node.get("children") or []):
            if isinstance(child, dict):
                total += counts(child)
        return total

    budget = max(0, int(max_nodes))
    total_matched = sum(counts(top) for top in (nodes or []) if isinstance(top, dict))
    if total_matched <= budget:
        return (list(nodes or []), None)
    hidden = total_matched - budget

    def prune(node: dict[str, Any]) -> dict[str, Any] | None:
        """Return the kept copy of ``node`` (or None when dropped)."""
        nonlocal budget
        consumes = member_predicate is None or bool(member_predicate(node))
        if consumes:
            # Display order is parents before children: this node consumes
            # its budget unit BEFORE its descendants get theirs.
            if budget <= 0:
                return None
            budget -= 1
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        kept_children: list[dict[str, Any]] = []
        for child in children:
            if budget <= 0:
                # Budget exhausted: nothing further can be kept -- further
                # matching nodes drop with their subtrees, and connective
                # ancestors survive only while a kept descendant does.
                break
            kept = prune(child)
            if kept is not None:
                kept_children.append(kept)
        if not children:
            return dict(node) if consumes else None
        if not kept_children and not consumes:
            return None
        out = dict(node)
        out["children"] = kept_children
        return out

    out_forest: list[dict[str, Any]] = []
    for top in (nodes or []):
        if not isinstance(top, dict):
            continue
        kept = prune(top)
        if kept is not None:
            out_forest.append(kept)
    shown = total_matched - hidden
    echo = {
        "max_nodes": int(max_nodes),
        "matched": total_matched,
        "shown": shown,
        "hidden": hidden,
    }
    return (out_forest, echo)


# @beacon[
#   id=since-policy@branch-timestamp-key,
#   slice_labels=nexus--md-preview,
#   kind=ast,
#   comment=THE single branch-date rule shared by the timestamp SORT (reorder_tree_by_timestamp) and every GLIMPSE `since` pivot (window + curation cursor). Given a root->leaf branch as a list of (uuid, node) and sort_by ('created' | 'modified'), returns the branch's comparison timestamp: created = the LEAF's own createdAt; modified = max(modifiedAt) over EVERY node on the branch. None when unavailable (uuid absent from cache / no int value), matching the sort's missing-key-sinks-last handling. Factored into the pure module so the sort and the `since` filter can NEVER drift on a branch's date; WorkFlowyClientNexus._branch_timestamp_key_static delegates here.,
# ]
def branch_timestamp_key(
    branch: list[tuple[str, dict[str, Any]]],
    *,
    sort_by: str | None,
    cache_by_id: dict[str, dict[str, Any]] | None,
) -> int | None:
    """Comparison timestamp for one root->leaf branch (created / modified).

    THE single source of truth for "what date does this branch sort by",
    shared by both :func:`reorder_tree_by_timestamp` (the sort) and the GLIMPSE
    ``since`` pivot (the recent-tail window + the curation cursor), so the two
    can never disagree on a branch's date.

    * ``created``  -> the LEAF's own ``createdAt`` (``branch[-1]``).
    * ``modified`` -> ``max(modifiedAt)`` over EVERY node on the branch
      (a branch is "recently touched" iff anything on it was).

    Returns ``None`` when the relevant timestamp is unavailable (uuid not in
    cache, or no integer epoch value), matching the sort's
    missing-key-sinks-last handling.
    """
    if sort_by not in ("created", "modified") or not branch:
        return None
    cache = cache_by_id or {}
    field = "createdAt" if sort_by == "created" else "modifiedAt"

    def _ts(node_id: str) -> int | None:
        c = cache.get(node_id)
        if not isinstance(c, dict):
            return None
        v = c.get(field)
        return v if isinstance(v, int) else None

    if sort_by == "created":
        return _ts(branch[-1][0])
    vals = [_ts(nid) for (nid, _) in branch]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


# @beacon[
#   id=since-policy@reorder-tree-by-timestamp,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=SHARED timestamp-sort pre-pass for the Markdown modal AND as_markdown GLIMPSE (and the `since` prune's re-merge). Reorders a visible node forest by leaf createdAt (created) or branch-max modifiedAt (modified), then trie-merges the sorted root->leaf branches so a shared ancestor straddled by an out-of-cluster branch RE-EMITS its file leader/breadcrumb. sort_by=None is identity passthrough. Returns a NEW shadow tree (inputs never mutated); every leaf keeps its FULL ancestor path so the composer's regime-threading + per-file H1 rebase + md-in-md all stay correct by construction. Keys join the /nodes-export cache by UUID (DOM payload has no timestamps); a UUID absent from the cache sinks last. Branch-date rule delegated to branch_timestamp_key so the SORT and the `since` pivot share ONE definition. WorkFlowyClientNexus._reorder_tree_by_timestamp delegates here.,
# ]
def reorder_tree_by_timestamp(
    nodes: list[dict[str, Any]] | None,
    *,
    sort_by: str | None,
    direction: str = "asc",
    cache_by_id: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]] | None:
    """Reorder a visible tree by leaf created / branch-max modified time.

    ``sort_by`` is ``None`` (identity passthrough -- returns ``nodes``
    unchanged, the current behavior), ``"created"`` (key = the leaf's own
    ``createdAt``), or ``"modified"`` (key = ``max(modifiedAt)`` over every
    node on the leaf's root->leaf branch). ``direction`` is ``"asc"`` or
    ``"desc"``.

    ALGORITHM (validated in isolation before wiring):
      1. DFS collect every LEAF with its full root->leaf branch.
      2. Per-branch key (created = leaf time; modified = branch max).
      3. Stable-sort branches by (key, original_dfs_index); a missing key
         (UUID absent from cache) sinks to the END regardless of direction.
         Equal keys keep ORIGINAL order (even under desc).
      4. Trie-merge in sorted order against the running RIGHTMOST SPINE by
         UUID: graft each branch's remainder at the first divergence. A shared
         ancestor straddled by an out-of-cluster branch re-emits a fresh node
         instance -> the composer renders the repeated file leader / breadcrumb
         (the desired "context on context-switch").

    Timestamps live on the /nodes-export CACHE node
    (``createdAt``/``modifiedAt``, integer epoch-seconds), joined by UUID; the
    browser DOM payload carries none. Returns a NEW tree of shadow dicts;
    inputs are never mutated.
    """
    if not nodes or sort_by not in ("created", "modified"):
        return nodes
    cache = cache_by_id or {}

    # 1. DFS: collect leaves as full root->leaf branches of (id, node).
    branches: list[list[tuple[str, dict[str, Any]]]] = []

    def walk(node: dict[str, Any], spine: list[tuple[str, dict[str, Any]]]) -> None:
        if not isinstance(node, dict):
            return
        here = spine + [(_id_of(node), node)]
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        if not children:
            branches.append(here)
            return
        for ch in children:
            walk(ch, here)

    for top in nodes:
        walk(top, [])
    if not branches:
        return nodes

    # 2. Per-branch sort key -- delegated to the SHARED branch-date rule so the
    # SORT and the GLIMPSE `since` pivot can never drift.
    def branch_key(branch: list[tuple[str, dict[str, Any]]]) -> int | None:
        return branch_timestamp_key(branch, sort_by=sort_by, cache_by_id=cache)

    # 3. Stable sort by (key, original_index). Missing key sinks LAST under
    # either direction; equal keys keep original DFS order even under desc (we
    # negate the index when descending so reverse=True does not flip the
    # tiebreak).
    descending = str(direction).lower() == "desc"
    NEG_INF = float("-inf")
    POS_INF = float("inf")
    indexed = list(enumerate(branches))

    def sort_key(item: tuple[int, list[tuple[str, dict[str, Any]]]]):
        idx, branch = item
        k = branch_key(branch)
        if k is None:
            primary = NEG_INF if descending else POS_INF
        else:
            primary = k
        return (primary, -idx if descending else idx)

    indexed.sort(key=sort_key, reverse=descending)
    sorted_branches = [b for (_, b) in indexed]

    # 4. Trie-merge against the rightmost spine, by UUID.
    forest: list[dict[str, Any]] = []
    rightmost: list[tuple[str, dict[str, Any]]] = []

    def _make_shadow(node: dict[str, Any]) -> dict[str, Any]:
        sh = dict(node)
        sh["children"] = []
        return sh

    for branch in sorted_branches:
        i = 0
        while (
            i < len(rightmost)
            and i < len(branch)
            and rightmost[i][0] == branch[i][0]
            and rightmost[i][0] != ""
        ):
            i += 1
        if i == 0:
            new_spine: list[tuple[str, dict[str, Any]]] = []
            parent_children = forest
            for (nid, node) in branch:
                sh = _make_shadow(node)
                parent_children.append(sh)
                new_spine.append((nid, sh))
                parent_children = sh["children"]
            rightmost = new_spine
        else:
            new_spine = rightmost[:i]
            parent_node = rightmost[i - 1][1]
            parent_children = parent_node["children"]
            for (nid, node) in branch[i:]:
                sh = _make_shadow(node)
                parent_children.append(sh)
                new_spine.append((nid, sh))
                parent_children = sh["children"]
            rightmost = new_spine

    return forest


# @beacon[
#   id=since-policy@compute-since-window-pivot,
#   slice_labels=nexus--md-preview,
#   kind=ast,
#   comment=GLIMPSE `since` Form-A (time-window) PIVOT. Data-relative leading edge: pivot = the branch-date of the MOST RECENT leaf branch in the ALREADY-FINAL forest (ascending) MINUS the window -- so 'last 1 week' on data whose newest branch is 2 years old means the week ENDING at that 2-year-old edge, NOT wall-clock now. Descending flips to the OLDEST edge PLUS the window (the commensurate corner). Uses the SHARED branch_timestamp_key so the branch-date matches the sort's. Returns None if no branch has a resolvable date (caller then returns the tree unfiltered). Both the server and client `since` paths reach this through apply_since_to_forest.,
# ]
def compute_since_window_pivot(
    nodes: list[dict[str, Any]] | None,
    *,
    window_seconds: int,
    axis: str,
    axis_direction: str,
    cache_by_id: dict[str, dict[str, Any]] | None,
) -> int | None:
    """Form-A pivot: data leading-edge branch-date offset by the window.

    The window is anchored to the DATA, not wall-clock: ascending -> the
    newest branch-date in the final forest minus ``window_seconds``;
    descending -> the oldest branch-date plus ``window_seconds``. Returns
    ``None`` when no branch has a resolvable date.

    The branch-date ``axis`` (created / modified) + its ``axis_direction`` come
    from the SINCE object, NOT the presentation sort, so the pivot is
    computable regardless of whether (or how) the render is presentation-sorted
    -- the early guard is on the AXIS, which the SINCE object always supplies.
    """
    if not nodes or axis not in ("created", "modified"):
        return None
    cache = cache_by_id or {}
    descending = str(axis_direction or "asc").lower() == "desc"

    branch_dates: list[int] = []

    def walk(node: dict[str, Any], spine: list[tuple[str, dict[str, Any]]]) -> None:
        if not isinstance(node, dict):
            return
        here = spine + [(_id_of(node), node)]
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        if not children:
            k = branch_timestamp_key(here, sort_by=axis, cache_by_id=cache)
            if k is not None:
                branch_dates.append(k)
            return
        for ch in children:
            walk(ch, here)

    for top in nodes:
        walk(top, [])
    if not branch_dates:
        return None
    if descending:
        return min(branch_dates) + window_seconds
    return max(branch_dates) - window_seconds


# @beacon[
#   id=since-policy@prune-tree-since,
#   slice_labels=nexus--md-preview,
#   kind=ast,
#   comment=GLIMPSE `since` PRUNE -- the shared leaf-branch filter run LAST (after Phase1/2/3 AND after the timestamp reorder), on the already-final sorted forest. Decomposes the forest into root->leaf branches (same DFS the sort uses), keeps a branch iff its branch-date (via the SHARED branch_timestamp_key) is beyond the pivot: ascending -> STRICTLY GREATER than pivot (most-recent tail; pivot node excluded); descending -> STRICTLY LESS (the commensurate 'oldest tail'). A branch with no resolvable date is DROPPED. Then re-merges surviving branches via reorder_tree_by_timestamp so straddled ancestors re-emit exactly as in the unfiltered render. Returns a NEW forest (inputs untouched); [] when nothing survives; the input unchanged when no prune applies. Shared by the server (_prune_tree_since delegates here) and, historically, a client twin (_prune_forest_since) that has since been DELETED -- the client now reaches `since` only through apply_since_to_forest.,
# ]
def prune_tree_since(
    nodes: list[dict[str, Any]] | None,
    *,
    pivot_ts: int | None,
    axis: str,
    axis_direction: str,
    sort_by: str | None,
    direction: str,
    cache_by_id: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Prune an already-final, already-sorted forest to the `since` window.

    Runs LAST -- after Phase 1/2/3 built the final visible set and the
    timestamp reorder ordered it.

    THE FILTER/SORT SPLIT. Two independent axes:

    * The MEMBERSHIP FILTER uses the SINCE object's OWN ``axis`` (created /
      modified) + ``axis_direction`` (asc / desc): keeps only leaf branches
      whose branch-date (via the SHARED :func:`branch_timestamp_key`) lies
      beyond the pivot -- ascending keeps ``branch_date > pivot`` (the
      most-recent tail, pivot node excluded); descending keeps
      ``branch_date < pivot`` (the commensurate oldest tail). A branch whose
      date can't be resolved is dropped. This runs UNCONDITIONALLY -- it does
      NOT depend on whether a PRESENTATION sort is active.
    * The final RE-MERGE ORDER uses the PRESENTATION ``sort_by`` / ``direction``
      -- when a presentation sort is active the surviving branches are
      re-merged via :func:`reorder_tree_by_timestamp` (so straddled ancestors
      re-emit their leaders exactly as in the full render); when NO presentation
      sort is active (``sort_by`` None -> DOCUMENT ORDER) the surviving branches
      keep their original document order via the SHARED rebuild, with no
      reorder. This is what makes 'window + document order' work.

    Returns a NEW forest; ``[]`` when nothing survives; the input unchanged when
    no prune applies.
    """
    # The FILTER needs a pivot + an axis; it no longer needs a PRESENTATION
    # sort. (The axis is always supplied by the SINCE object.)
    if not nodes or pivot_ts is None or axis not in ("created", "modified"):
        return nodes
    cache = cache_by_id or {}
    descending = str(axis_direction or "asc").lower() == "desc"

    # 1) Decompose into root->leaf branches (the SHARED walk), keeping the LEAF
    #    node objects whose branch-date (on the SINCE AXIS) survives the pivot.
    kept_leaf_objs: set[int] = set()
    for branch in collect_leaf_branches(nodes):
        k = branch_timestamp_key(branch, sort_by=axis, cache_by_id=cache)
        if k is None:
            continue
        keep = (k < pivot_ts) if descending else (k > pivot_ts)
        if keep:
            kept_leaf_objs.add(id(branch[-1][1]))
    if not kept_leaf_objs:
        return []

    # 2) Rebuild the pruned forest keeping only paths to a surviving leaf (the
    #    SHARED rebuild). This preserves the input forest's DOCUMENT ORDER for
    #    the kept branches -- the document-order answer when no presentation
    #    sort is active.
    pruned_forest = _rebuild_forest_from_kept_leaves(nodes, kept_leaf_objs)

    # 3) Apply the PRESENTATION order CONDITIONALLY: only when a presentation
    #    sort is active do we re-merge/trie-order the survivors. With no
    #    presentation sort (document order) we keep the rebuild's document
    #    order verbatim -- the filter and the order are now fully independent.
    if sort_by in ("created", "modified"):
        reordered = reorder_tree_by_timestamp(
            pruned_forest,
            sort_by=sort_by,
            direction=direction,
            cache_by_id=cache,
        )
        return reordered if reordered else pruned_forest
    return pruned_forest


# @beacon[
#   id=since-policy@compute-curated-high-water-mark,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=GLIMPSE `since="curated"` FORM-B PIVOT (the self-maintaining curation cursor). Walks the /nodes-export cache subtree under root_uuid, finds every CURATED node (curated_predicate(name, note) True -- the caller injects f8_slice_policy.node_is_curated_for_prefixes, deliberately looser than slice MEMBERSHIP on the triage axis: --deprecated variants still count, because deprecating a tag is itself a curation act; the --suppress/--expand PRESENTATION variants do NOT count and never move the mark -- Session-019 curated-cursor fix), computes each curated node's branch-date via the SHARED branch_timestamp_key over its root->leaf cache path, and returns the MAX (ascending; MIN descending) = the high-water mark. Returns None when NOTHING under root is curated (whole subtree is the uncurated tail). curated_predicate is passed IN (not imported) so this module needs no f8_slice_policy dependency -> zero import-cycle risk; the walk itself is postfix-agnostic -- the curation semantics live entirely in the injected predicate. server._compute_curated_high_water_mark delegates here.,
# ]
def compute_curated_high_water_mark(
    *,
    cache_by_id: dict[str, dict[str, Any]],
    root_uuid: str,
    curated_predicate: Callable[[str, str], bool],
    axis: str,
    axis_direction: str,
) -> int | None:
    """Form-B pivot: branch-date of the most-recent CURATED node under root.

    The branch-date ``axis`` + ``axis_direction`` come from the SINCE object
    (default created/asc), NOT the presentation sort -- the curation
    high-water mark is computed on the SINCE axis independent of how the render
    is presentation-sorted.

    A node is "curated" iff ``curated_predicate(name, note)`` is True. The
    caller injects that predicate (in production, a partial over
    ``f8_slice_policy.node_is_curated_for_prefixes`` bound to the active slice
    prefixes) so this pure module needs no ``f8_slice_policy`` import — which is
    what keeps it free of any import cycle. The curation predicate is
    deliberately LOOSER than slice membership on the triage axis:
    ``--deprecated`` variants still count as curated, because deprecating a
    tag is itself a curation act the user must not re-encounter. The
    ``--suppress`` / ``--expand`` PRESENTATION variants do NOT count and
    never move the mark (Session-019 curated-cursor fix). The walk itself is
    postfix-agnostic -- that semantics lives entirely in the injected
    predicate.

    For each curated node we compute its branch-date with the SAME rule the
    sort/prune use (:func:`branch_timestamp_key` over its root->leaf cache path)
    and return the extremum (max ascending / min descending). Returns ``None``
    when nothing under root is curated (no high-water mark -> the whole subtree
    is the uncurated tail).
    """
    if axis not in ("created", "modified"):
        return None
    root = str(root_uuid or "").strip()
    if not root or root not in cache_by_id:
        return None
    descending = str(axis_direction or "asc").lower() == "desc"

    # Build child lists once so we can (a) DFS the subtree and (b) reconstruct
    # each curated node's root->leaf-style ancestor path for the branch-date.
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for n in cache_by_id.values():
        if not isinstance(n, dict):
            continue
        pid = n.get("parent_id") or n.get("parentId")
        if pid:
            children_by_parent.setdefault(str(pid), []).append(n)

    def _note_of(node: dict[str, Any]) -> str:
        return str(node.get("note") or node.get("no") or "")

    def _name_of(node: dict[str, Any]) -> str:
        return str(node.get("name") or node.get("nm") or "")

    curated_dates: list[int] = []
    visited: set[str] = set()

    # DFS from root, carrying the (id, node) spine so a curated node's
    # branch-date is computed over its true root->node path (matches the sort's
    # branch semantics for the 'modified' branch-max case; for 'created' only
    # the endpoint's own createdAt is used anyway).
    def walk(node: dict[str, Any], spine: list[tuple[str, dict[str, Any]]]) -> None:
        nid = _id_of(node)
        if not nid or nid in visited:
            return
        visited.add(nid)
        here = spine + [(nid, node)]
        if curated_predicate(_name_of(node), _note_of(node)):
            k = branch_timestamp_key(here, sort_by=axis, cache_by_id=cache_by_id)
            if k is not None:
                curated_dates.append(k)
        for ch in children_by_parent.get(nid, []):
            if isinstance(ch, dict):
                walk(ch, here)

    walk(cache_by_id[root], [])
    if not curated_dates:
        return None
    return min(curated_dates) if descending else max(curated_dates)


# @beacon[
#   id=since-policy@prune-tree-to-last-leaf-count,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=GLIMPSE `since` FORM-C POSITIONAL prune ([ap:TVIIBN] 4d(a): the selection spelled 'bottom N', and the legacy meaning of every old wire blob) -- the ITEM-COUNT tail filter over the forest's CURRENT order. Runs LAST, exactly like the date prune, on the already-final (optionally sorted) forest, but is orthogonal to the sort: 'the bottom N' is defined purely by the forest's CURRENT leaf order (the new 'newest N' SELECTION lives beside it in prune_tree_to_newest_leaf_count). Counts LEAVES only (a node with no dict children in the final forest; hidden descendants are already absent) via the SHARED collect_leaf_branches, keeps the LAST N leaf branches in that order (the tail: most-recent under an ascending chronological sort, earliest under descending, last-in-document-order when unsorted), and rebuilds via the SHARED _rebuild_forest_from_kept_leaves. When N >= leaf count the whole forest is returned unchanged (by identity of the kept set). Does NOT re-sort/re-merge -- the input order IS the truth (first pass: no nestedness weighting; a subtree with K leaves consumes K of the count). Returns a NEW forest; [] only if the forest has no leaves at all. Pure; inputs never mutated.,
# ]
def prune_tree_to_last_leaf_count(
    nodes: list[dict[str, Any]] | None,
    *,
    count: int,
) -> list[dict[str, Any]] | None:
    """Keep only the LAST ``count`` leaf branches of the final forest.

    The Form-C POSITIONAL tail prune -- the selection spelled ``'bottom N'``
    ([ap:TVIIBN] 4d(a)), and the legacy meaning of every old wire blob.
    Unlike the date forms this is ORTHOGONAL to
    the sort -- 'the bottom N' is defined by the forest's CURRENT leaf order,
    which is:
      * the most-recent tail under an ascending created/modified sort,
      * the earliest tail under a descending sort,
      * the last N leaves in document order when unsorted.
    So it does NOT require an active ``sort_by`` (the guard is relaxed for it).
    The RECENCY selection (``'last N'`` / ``'newest N'`` = the N newest) lives
    beside it in :func:`prune_tree_to_newest_leaf_count`.

    Counting is by LEAF only (first pass: no nestedness weighting), using the
    SHARED :func:`collect_leaf_branches` so 'leaf' means exactly 'no visible
    descendants in the final forest' -- consistent with every other transform.
    A subtree with K leaves therefore consumes K of the count. When ``count``
    is >= the number of leaves the whole forest is returned unchanged.

    We deliberately do NOT re-sort or re-merge: the caller has already produced
    the final order (natural or timestamp-sorted), and the positional tail is
    a pure SELECTION over that order. We keep the surviving-leaf paths via the
    SHARED :func:`_rebuild_forest_from_kept_leaves`, which preserves the
    forest's existing structure/order for the kept branches. Returns a NEW
    forest; the input is never mutated; ``[]`` only when there are no leaves.
    """
    if not nodes:
        return nodes
    try:
        n = int(count)
    except (TypeError, ValueError):
        return nodes
    if n <= 0:
        return []
    branches = collect_leaf_branches(nodes)
    if not branches:
        return []
    if n >= len(branches):
        # Nothing to trim: the whole forest is the tail. Return it unchanged
        # (no rebuild needed -- identical structure + order).
        return nodes
    kept_leaf_objs = {id(branch[-1][1]) for branch in branches[-n:]}
    return _rebuild_forest_from_kept_leaves(nodes, kept_leaf_objs)


# @beacon[
#   id=since-policy@resolve-item-count-basis,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=[ap:TVIIBN] 4d(a) THE ONE recency-basis resolution for an item-count 'newest' selection: recent_by when the caller supplied one, else the PRESENTATION sort_by axis when it is a timestamp (so 'last 10 by modified' selects on the same axis it displays), else 'modified' (current attention -- dock.peek's default basis, the same rule). Pure; the ONE definition every caller (apply_since_to_forest and any direct prune_tree_to_newest_leaf_count caller) shares so the selection basis can never drift from the describe rule.,
# ]
def resolve_item_count_basis(
    recent_by: str | None,
    sort_by: str | None,
) -> str:
    """Resolve the recency BASIS for an item-count ``'newest'`` selection.

    THE one rule ([ap:TVIIBN] 4d(a), the DECISION's default-basis clause):
    ``recent_by`` wins when the caller supplied one; otherwise the
    presentation ``sort_by`` axis when it is a timestamp (the selection then
    measures the same axis the render is displayed by); otherwise
    ``'modified'`` — current attention, the same default basis dock.peek's
    recency view uses. Pure; no I/O.
    """
    if recent_by in SINCE_AXES:
        return recent_by
    if sort_by in SINCE_AXES:
        return sort_by
    return "modified"


# @beacon[
#   id=since-policy@prune-tree-to-newest-leaf-count,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=[ap:TVIIBN] 4d(a) the 'newest N' SELECTION prune (beside prune_tree_to_last_leaf_count, never instead of it): keeps the N NEWEST leaf branches by branch_timestamp_key on the resolved recency basis (the SAME branch-date rule the sort and the date pivots share, so selection and sort can never disagree on a branch's date), whatever the presentation sort direction -- sort_direction only orders, never chooses (Dan's ruling). Ranking is newest-first with ties keeping document order; a branch with no resolvable timestamp ranks AFTER every dated branch (and is selected only when N exceeds the dated count), mirroring dock.peek's missing-timestamps-sink-last rule. Rebuilds via the SHARED _rebuild_forest_from_kept_leaves so the SURVIVORS keep the forest's CURRENT order (selection never reorders; display order stays with the caller's sort). When N >= leaf count the whole forest is returned unchanged; counts LEAVES only via the SHARED collect_leaf_branches. Pure; inputs never mutated.,
# ]
def prune_tree_to_newest_leaf_count(
    nodes: list[dict[str, Any]] | None,
    *,
    count: int,
    basis: str,
    cache_by_id: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Keep only the NEWEST ``count`` leaf branches of the final forest.

    The ``'newest N'`` selection prune ([ap:TVIIBN] 4d(a)). Unlike the
    positional :func:`prune_tree_to_last_leaf_count`, this prune is defined by
    RECENCY, not by order: each leaf branch is keyed by
    :func:`branch_timestamp_key` on the resolved ``basis`` (``'created'`` =
    the leaf's own createdAt, ``'modified'`` = branch-max modifiedAt — the
    SAME branch-date rule the sort and the date pivots use), the branches are
    ranked newest-first (ties keep document order; a branch whose timestamp
    cannot be resolved ranks after every dated branch, mirroring dock.peek's
    missing-timestamps-sink-last rule), and the top ``count`` survive.

    The SURVIVORS keep the forest's CURRENT structure and order (rebuilt via
    the SHARED :func:`_rebuild_forest_from_kept_leaves`): the selection never
    reorders — under the one recency rule, ``sort_direction`` only orders what
    is shown, never which rows were chosen. Counting is by LEAF only, via the
    SHARED :func:`collect_leaf_branches` (a subtree with K leaves consumes K
    of the count). When ``count`` >= the number of leaves the whole forest is
    returned unchanged. Returns a NEW forest; inputs never mutated; ``[]``
    only when the forest has no leaves at all.
    """
    if not nodes:
        return nodes
    try:
        n = int(count)
    except (TypeError, ValueError):
        return nodes
    if n <= 0:
        return []
    branches = collect_leaf_branches(nodes)
    if not branches:
        return []
    if n >= len(branches):
        # Nothing to trim: every leaf survives the selection. Return the
        # forest unchanged (identical structure + order, same as the
        # positional prune's identity short-circuit).
        return nodes
    cache = cache_by_id or {}
    dated: list[tuple[int, int, list[tuple[str, dict[str, Any]]]]] = []
    undated: list[list[tuple[str, dict[str, Any]]]] = []
    for doc_index, branch in enumerate(branches):
        key = branch_timestamp_key(branch, sort_by=basis, cache_by_id=cache)
        if key is None:
            undated.append(branch)
        else:
            # Negate the key so a plain ascending sort ranks NEWEST first;
            # the doc_index tiebreak keeps document order among equal keys.
            dated.append((-key, doc_index, branch))
    dated.sort(key=lambda item: (item[0], item[1]))
    ranked = [branch for (_k, _i, branch) in dated] + undated
    kept_leaf_objs = {id(branch[-1][1]) for branch in ranked[:n]}
    return _rebuild_forest_from_kept_leaves(nodes, kept_leaf_objs)


# @beacon[
#   id=since-policy@classify-since-token,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=THE single classifier for a `since` token's FORM + PRECONDITIONS, consulted by BOTH the cheap upstream fail-fast guard (glimpse() dispatcher + the modal handlers' early reject) AND the final apply_since_to_forest application, so the rule set (which form, does it need an axis, does it require a slice group, is the token malformed) lives in ONE place and the two layers only differ in WHEN they consult it and WHICH dispatch facts they supply (only the dispatcher knows 'is a slice_group present?'). Returns {form, requires_axis, requires_slice_group, error}. requires_axis (renamed from the former requires_sort in the since<->sort DECOUPLE) means 'this form's pivot is a branch-DATE so it needs a timestamp AXIS' -- and the axis is ALWAYS supplied by the SinceSpec object (default created/asc), so it is satisfied-by-construction and callers no longer reject a date-form since for lacking a presentation sort (that was Seam B). form is 'item_count'|'window'|'curated'|None (None => no `since`). error is a human string for a MALFORMED token (bad 'last N' / bad window); the axis/slice-group PRECONDITIONS are reported as booleans (the caller combines requires_slice_group with the facts it owns to decide whether to reject; requires_axis needs no rejection anymore). Pure; no I/O; adding a new `since` value = one edit here + its prune in apply_since_to_forest, and the upstream guards inherit the new preconditions for free.,
# ]
def classify_since_token(token: Any) -> dict[str, Any]:
    """Classify a `since` token's form + preconditions (the shared rule set).

    The ONE place the `since` grammar/preconditions live, consulted by both
    the cheap upstream fail-fast guard and the final :func:`apply_since_to_forest`
    application, so neither re-decides. Returns a dict::

        {
          "form": "item_count" | "window" | "curated" | None,
          "requires_axis": bool,       # this form's pivot is a branch-DATE
          "requires_slice_group": bool,  # this form needs slice membership
          "error": str | None,         # set iff the token is MALFORMED
        }

    * ``form=None`` (and all flags False, no error) means no `since` was
      requested (blank/None token) -- a clean passthrough.
    * ``error`` is set ONLY for a malformed token (a bad ``last N`` count or an
      unparseable window). Preconditions (needs an axis / needs a slice group)
      are NOT errors here -- they are reported as booleans so the caller can
      combine them with the facts IT owns (e.g. 'is a slice_group present?',
      which this pure module cannot know) and decide whether to reject.

    ``requires_axis`` asserts 'this form's pivot is a branch-DATE, so it needs
    a timestamp AXIS'. The axis is ALWAYS supplied by the :class:`SinceSpec`
    object (default created/asc), so ``requires_axis`` is SATISFIED-BY-
    CONSTRUCTION for every well-formed date-form ``since`` -- it does NOT mean
    'needs the PRESENTATION sort'. The truth values are window=True,
    curated=True, item_count=False. Because the object carries its own axis, a
    date-form ``since`` is never rejected for lacking a created/modified
    presentation sort.

    Precondition truth table:
      * item_count (``last N``): requires_axis=False, requires_slice_group=False
        (the tail is defined by the forest's current order, no date needed).
      * window (``1w`` / ``3d`` / bare number = days): requires_axis=True
        (pivot is a branch-date), requires_slice_group=False.
      * curated: requires_axis=True AND requires_slice_group=True (the slice
        group's tag set is what DEFINES 'curated').
    """
    tok = str(token).strip() if token is not None else ""
    if not tok:
        return {
            "form": None,
            "requires_axis": False,
            "requires_slice_group": False,
            "error": None,
        }

    # Form C: item-count ("last N" / "newest N" / positional "bottom N") --
    # orthogonal to the sort AND the axis. The keyword's SELECTION routing is
    # the codec's job ([ap:TVIIBN] 4d(a)); this classifier only cares about
    # the form + the count being well-formed.
    if is_item_count_token(tok):
        n = parse_since_item_count(tok)
        return {
            "form": "item_count",
            "requires_axis": False,
            "requires_slice_group": False,
            "error": (
                None if n is not None else (
                    f"Could not parse recent={tok!r} as an item count. Use "
                    "'last N' or 'newest N' (the N newest, a positive "
                    "integer), the positional 'bottom N', or '... N items', "
                    "e.g. 'last 10', 'newest 10', 'bottom 10 items'."
                )
            ),
        }

    # Form B: curated cursor -- date pivot (needs an axis) + a slice group.
    if tok.lower() == "curated":
        return {
            "form": "curated",
            "requires_axis": True,
            "requires_slice_group": True,
            "error": None,
        }

    # Form A: time window -- date pivot (needs an axis); malformed = loud error.
    window = parse_since_window(tok)
    return {
        "form": "window",
        "requires_axis": True,
        "requires_slice_group": False,
        "error": (
            None if window is not None else (
                f"Could not parse recent={tok!r}. Use a duration like '1w', "
                "'3d', '12h', '90m', '45s' (a bare number means days), an item "
                "count 'last N' / 'newest N', or 'curated'."
            )
        ),
    }


# @beacon[
#   id=since-policy@apply-since-to-forest,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=Object-only `since` orchestrator: given an already-final forest + SinceSpec|None, it reads the object's form/axis/direction/count/window_seconds/selection fields, computes the pivot when needed, runs the matching prune, and builds the since_applied echo. recent_by (optional, [ap:TVIIBN] 4d(a)) is the caller's explicit recency basis for a 'newest' item_count selection -- resolve_item_count_basis resolves it against the presentation sort. Token grammar is edge-only and is not visible here. Pure; no I/O (curated pivot is injected, not computed here).,
# ]
def apply_since_to_forest(
    nodes: list[dict[str, Any]] | None,
    *,
    since_spec: SinceSpec | None,
    sort_by: str | None,
    direction: str,
    cache_by_id: dict[str, dict[str, Any]] | None,
    curated_pivot_ts: int | None = None,
    recent_by: str | None = None,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None, str | None]:
    """Apply a SinceSpec to an already-final, already-sorted forest.

    THE single entry point every GLIMPSE render surface calls for `since` after
    edge ingestion has produced a SinceSpec. It reads object fields only; token
    grammar and string parsing are not visible here.

    Parameters
    ----------
    nodes : the FINAL, already-sorted forest (Phase 1-4 + timestamp sort done).
    since_spec : the object-form since request. None means no since filter.
    sort_by / direction / cache_by_id : the ACTIVE sort context (needed by the
        date forms; a 'bottom' item-count ignores them; a 'newest' item-count
        borrows only the sort AXIS as its default basis).
    curated_pivot_ts : for Form B only -- the curation high-water mark the
        SURFACE precomputed over its slice group's tag set (this pure module
        does not know slice membership). Ignored by Forms A / C. ``None`` here
        with a ``curated`` token means 'nothing curated yet' (whole forest is
        the uncurated tail -> no prune, honest echo).
    recent_by : explicit recency basis ('created' | 'modified') for a
        ``'newest'`` item_count selection ([ap:TVIIBN] 4d(a)). ``None`` (the
        default) resolves to the presentation ``sort_by`` axis when it is a
        timestamp, else ``'modified'`` -- the ONE
        :func:`resolve_item_count_basis` rule. Ignored by the other forms.

    Returns
    -------
    ``(out_nodes, since_applied, error)``:
      * ``error`` (str) set -> a semantic precondition failed; ``out_nodes`` is
        the input unchanged and ``since_applied`` is None.
      * ``error`` None, ``since_applied`` None -> no `since` was requested
        (blank token); ``out_nodes`` is the input unchanged.
      * ``error`` None, ``since_applied`` a dict -> the form fired (or was a
        valid no-op, e.g. an unresolvable pivot / N>=leafcount); ``out_nodes``
        is the pruned forest (possibly ``[]`` for an empty window, a VALID
        answer). ``since_applied`` always honestly reports the requested form.
    """
    # Object-only interior: callers pass SinceSpec/None after edge ingestion or
    # strict wire decoding. This orchestrator never parses or tolerates strings.
    since = since_spec
    if since is None:
        return nodes, None, None

    # The pivot + filter measure branch-dates against the SINCE object's OWN
    # axis/direction, NOT the presentation sort_by/direction. The presentation
    # sort_by/direction is used ONLY for the final re-merge ORDER of the
    # surviving branches (or document order when no sort is active).
    since_axis = since.axis
    since_dir = since.direction

    # ---- Form C: item-count -- the ONE recency rule ([ap:TVIIBN] 4d(a)) ----
    # 'last N' / 'newest N' (selection='newest') SELECT the N newest leaves by
    # the recency basis (recent_by, else the presentation sort axis when it is
    # a timestamp, else 'modified') -- whatever the presentation direction;
    # sort_direction only orders, never chooses. 'bottom N'
    # (selection='bottom') keeps the POSITIONAL tail of the current order --
    # the legacy behavior, and the meaning of every old wire blob.
    if since.form == "item_count":
        n = since.count
        selection = (
            since.selection
            if getattr(since, "selection", None) in SINCE_ITEM_SELECTIONS
            else "bottom"
        )
        if selection == "newest":
            basis = resolve_item_count_basis(recent_by, sort_by)
            pruned = prune_tree_to_newest_leaf_count(
                nodes, count=n, basis=basis, cache_by_id=cache_by_id
            )
            out = pruned if pruned else []
            since_applied = {
                "form": "item_count",
                "count": n,
                "selection": "newest",
                "basis": basis,
            }
            return out, since_applied, None
        pruned = prune_tree_to_last_leaf_count(nodes, count=n)
        out = pruned if pruned else []
        since_applied = {"form": "item_count", "count": n, "selection": "bottom"}
        return out, since_applied, None

    # ---- Forms A / B: date pivot -- axis comes from the SINCE object, which
    # ALWAYS carries one (default created/asc). So there is no 'requires an
    # active presentation sort' rejection here: a window/curated `since` works
    # with any presentation sort, INCLUDING document order (sort_by=None).

    # ---- Form B: curated cursor -- pivot precomputed upstream & injected ----
    if since.form == "curated":
        pivot = curated_pivot_ts
        if pivot is None:
            # Nothing curated yet -> the whole forest IS the uncurated tail.
            # Not an error: honest echo, no prune.
            return (
                nodes,
                {
                    "form": "curated",
                    "pivot_ts": None,
                    "note": (
                        "nothing under root is curated yet; whole subtree IS "
                        "the uncurated tail (no high-water mark to prune "
                        "against)"
                    ),
                },
                None,
            )
        pruned = prune_tree_since(
            nodes,
            pivot_ts=pivot,
            axis=since_axis,
            axis_direction=since_dir,
            sort_by=sort_by,
            direction=direction,
            cache_by_id=cache_by_id,
        )
        out = pruned if pruned else []
        return out, {"form": "curated", "pivot_ts": pivot}, None

    # ---- Form A: time window ------------------------------------------------
    pivot = compute_since_window_pivot(
        nodes,
        window_seconds=since.window_seconds,
        axis=since_axis,
        axis_direction=since_dir,
        cache_by_id=cache_by_id,
    )
    if pivot is None:
        # No dateable branch -> no prune (honest echo, not an error).
        return (
            nodes,
            {"form": "window", "pivot_ts": None, "note": "no resolvable pivot; not pruned"},
            None,
        )
    pruned = prune_tree_since(
        nodes,
        pivot_ts=pivot,
        axis=since_axis,
        axis_direction=since_dir,
        sort_by=sort_by,
        direction=direction,
        cache_by_id=cache_by_id,
    )
    out = pruned if pruned else []
    return out, {"form": "window", "pivot_ts": pivot}, None
