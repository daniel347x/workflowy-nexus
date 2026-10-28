"""THE ONE managed-metadata inventory for Workflowy notes and names.

Dan's to-do [ap:BNNUZ4]: one place owns the list of metadata blocks the
system writes into Workflowy notes or names, treated as MANAGED DATA, so a
mutation verb can consult it before it writes. First consumer:
``anchor.point.promote`` -- a node whose note carries a recognised block
still moves/copies, but the provenance trail line (``'↩ moved from …'`` /
``'↩ derived from …'``) is SKIPPED and the ledger row's ``info`` field says
so, so the system never prepends prose to its own machine-readable blocks.

Three queries over one registry (deterministic order = the registry order):

- ``detect_managed_metadata(note, name=…)`` -- the first matching kind of
  ANY guard: the general "is this managed data, and as what" answer.
- ``detect_trail_skip_metadata(note, name=…)`` -- the SKIP-TRAIL kinds only:
  point.promote's question (its mapped-node refusal has already run by then).
- ``first_refuse_guard_hit(first_line)`` -- the REFUSE-MAPPED first-line
  kinds only: the text behind ``server._promote_mapped_metadata_reason``'s
  refusal detail (byte-identical: the same prefixes the old
  ``_PROMOTE_MAPPED_METADATA_RE`` always matched).
- ``is_managed_source_line(note)`` -- the SOURCE-MAP MARKER question
  ([ap:UH0UOU]): does the note's FIRST line carry a Cartographer/beacon
  source-map marker, i.e. is this line source code? The headings-only
  end-of-line metadata is suppressed on such lines.

Guards:

- ``refuse_mapped`` -- orphaned Cartographer metadata on line 1; the node
  itself is mapped material ("disk is truth") and promote REFUSES it.
- ``skip_trail`` -- machine-managed content the system writes and re-reads;
  the node may move, but its bytes must not gain a trail line.

Format ownership: the skip-trail detectors RECOGNISE formats parsed
elsewhere -- opencode-companion scripts (``registry_from_note`` /
``extract_yaml_block`` for ``program_interface``, ``standing_*_from_note`` /
``seats_from_note`` / ``cast_from_note`` for the StageConfiguration,
``summarize_state`` for ConductorState, ``parse_manifest`` for seat-base
manifest lines, ``oc_sessions`` ``SNAPSHOT_PREFIX``/``SHELF_SNAPSHOT_PREFIX``
for snapshot nodes), and the daemon's own estate payload grammar
(``glimpse_estate_payload._parse_note_candidates`` with its existing
candidate extractor). Estate classification requires a valid grammar entry;
there is no private estate key list or recogniser here. The other detectors
are deliberately recognition-only (fence/key/line shapes), never full parsers:
their managed verdict does not depend on the block parsing cleanly.
Detection is biased toward recall on purpose -- a false "managed" only
skips a trail line (the provenance lives on in the ledger row), while a
miss would prepend prose to a managed note.

Documented-but-structural kinds (no detector here): a Cartographer-mapped
FILE/FOLDER/AST node is ``cartographer_mapped_node`` data detected by the
ancestor walk (``_classify_node_map_state``), not by note content -- promote
keeps that refusal. An orphaned ``@beacon[`` first line is related residue
but is neither refused nor skip-trail today (no behaviour change here).

Pure functions; no I/O. The ``ledger`` detector lazily imports the repo-root
``nexus_map_codebase.split_name_and_tags`` (the ONE trailing-tag splitter,
the same lazy pattern the anchor policy uses).
"""
from __future__ import annotations

import re
from typing import Callable, NamedTuple, Optional

__all__ = [
    "ManagedMetadataKind",
    "detect_managed_metadata",
    "detect_trail_skip_metadata",
    "first_refuse_guard_hit",
    "is_managed_source_line",
]


# ---------------------------------------------------------------------------
# Detectors -- one pure function per kind. Each takes (note, name) in SEMANTIC
# form (wire-decoded; the caller owns the decode) and returns a Boolean.
# ---------------------------------------------------------------------------

# Orphaned Cartographer metadata on line 1 of a note (F12+4 residue whose
# mapped ancestor is gone). The ancestor walk (_classify_node_map_state) is
# the real test; this prefix set is belt-and-suspenders. Byte-identical to
# the retired server regex ``^(AST_QUALNAME:|MD_PATH:|BEACON \()``.
_REFUSE_FIRST_LINE_RE = re.compile(r"^(AST_QUALNAME:|MD_PATH:|BEACON \()")

_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)

_PI_KEY_RE = re.compile(r"^\s*program_interface\s*:", re.M)
_PI_BARE_RE = re.compile(r"^program_interface\s*:\s*\n((?:[ \t]+.*\n?)+)", re.M)

_STAGE_CFG_KEY_RE = re.compile(
    r"^\s*(standing_[a-z_]+|required_conductor_worker_clauses|seats|cast)\s*:", re.M
)

_CONDUCTOR_STATE_KEY_RE = re.compile(r"^\s*(pending_halt|next_step|in_flight)\s*:", re.M)
_CONDUCTOR_STATE_STAGE_RE = re.compile(r"^\s*stage\s*:", re.M)
_CONDUCTOR_STATE_VERSION_RE = re.compile(r"^\s*version\s*:", re.M)

_DISPATCH_FENCED_RE = re.compile(r"^\s*dispatch\s*:", re.M)
_DISPATCH_BARE_RE = re.compile(r"^dispatch\s*:\s*$", re.M)

_SEAT_MANIFEST_LINE_RE = re.compile(r"^(?:map|skyview)\s+\[ap:", re.M)

# oc_sessions.py: SNAPSHOT_PREFIX / SHELF_SNAPSHOT_PREFIX. The legacy
# unmarked snapshot names it also deletes are unrecognisable by design.
_SNAPSHOT_PREFIXES = ("📸 Snapshot ·", "📸 Shelf Snapshot ·")

_LEDGER_TAG = "#prog-ledger"

def _fences(note: str) -> list[str]:
    return [m.group(1) for m in _FENCE_RE.finditer(note or "")]


def _first_line_startswith(prefixes: tuple[str, ...]) -> Callable[[str, Optional[str]], bool]:
    """One refuse-kind detector per prefix (so the returned KIND is exact);
    together the three cover exactly the retired combined regex's first line."""

    def _detect(note: str, name: Optional[str]) -> bool:
        lines = (note or "").splitlines()
        first = lines[0] if lines else ""
        return first.startswith(prefixes)

    return _detect


def _detect_program_interface(note: str, name: Optional[str]) -> bool:
    text = note or ""
    for body in _fences(text):
        if _PI_KEY_RE.search(body):
            return True
    # Bare form (no fence): the key line followed by an indented block --
    # extract_yaml_block's bare grammar.
    return _PI_BARE_RE.search(text) is not None


def _detect_stage_configuration(note: str, name: Optional[str]) -> bool:
    return any(_STAGE_CFG_KEY_RE.search(body) for body in _fences(note or ""))


def _detect_conductor_state(note: str, name: Optional[str]) -> bool:
    for body in _fences(note or ""):
        if _CONDUCTOR_STATE_KEY_RE.search(body):
            return True
        if _CONDUCTOR_STATE_STAGE_RE.search(body) and _CONDUCTOR_STATE_VERSION_RE.search(body):
            return True
    return False


def _detect_dispatch_block(note: str, name: Optional[str]) -> bool:
    text = note or ""
    if _DISPATCH_BARE_RE.search(text):
        return True
    return any(_DISPATCH_FENCED_RE.search(body) for body in _fences(text))


def _detect_estate_payload(note: str, name: Optional[str]) -> bool:
    from .glimpse_estate_payload import (  # noqa: PLC0415 -- local: keeps this module import-light
        _parse_note_candidates,
        extract_estate_json_candidates,
    )

    # Preserve the existing conservative metadata posture for ambiguous
    # group-name strings; route (a) makes their leading provenance inert in
    # the grammar itself. All structured detection is grammar-owned too.
    entries, _error = _parse_note_candidates(
        extract_estate_json_candidates(note or ""), allow_group_strings=False,
    )
    return any(entry.valid for entry in entries or [])


def _detect_seat_manifest(note: str, name: Optional[str]) -> bool:
    return _SEAT_MANIFEST_LINE_RE.search(note or "") is not None


def _detect_snapshot_node(note: str, name: Optional[str]) -> bool:
    subject = (name or "").strip()
    return subject.startswith(_SNAPSHOT_PREFIXES)


def _detect_ledger(note: str, name: Optional[str]) -> bool:
    if not name:
        return False
    try:
        from nexus_map_codebase import split_name_and_tags  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover -- the repo-root module is always present here
        return _LEDGER_TAG in name
    _base, name_tags = split_name_and_tags(name)
    return _LEDGER_TAG in name_tags


# ---------------------------------------------------------------------------
# The registry -- one row per managed kind, in deterministic query order.
# ``owner`` names the component that PARSES the block for real (documentation,
# not a dependency). ``cartographer_mapped_node`` has no detector: it is
# structural (the ancestor walk), documented here so the inventory is complete.
# ---------------------------------------------------------------------------


class ManagedMetadataKind(NamedTuple):
    kind: str
    guard: str  # "refuse_mapped" | "skip_trail" | "structural"
    zone: str  # "first_line" | "note" | "name" | "structural"
    detect: Optional[Callable[[str, Optional[str]], bool]]
    owner: str


_KINDS: tuple[ManagedMetadataKind, ...] = (
    ManagedMetadataKind(
        "ast_header", "refuse_mapped", "first_line", _first_line_startswith(("AST_QUALNAME:",)),
        "Cartographer AST-node metadata (AST_QUALNAME:) -- refusal shared with "
        "server._promote_mapped_metadata_reason / _classify_node_map_state",
    ),
    ManagedMetadataKind(
        "md_path_header", "refuse_mapped", "first_line", _first_line_startswith(("MD_PATH:",)),
        "Markdown file dummy block (MD_PATH:) -- refusal shared with "
        "server._promote_mapped_metadata_reason / _classify_node_map_state",
    ),
    ManagedMetadataKind(
        "beacon_block", "refuse_mapped", "first_line", _first_line_startswith(("BEACON (",)),
        "Beacon block residue (BEACON (...) -- refusal shared with "
        "server._promote_mapped_metadata_reason / _classify_node_map_state",
    ),
    ManagedMetadataKind(
        "program_interface", "skip_trail", "note", _detect_program_interface,
        "opencode-companion registry_from_note / extract_yaml_block",
    ),
    ManagedMetadataKind(
        "stage_configuration", "skip_trail", "note", _detect_stage_configuration,
        "opencode-companion oc_preflight standing_*_from_note / seats_from_note / cast_from_note",
    ),
    ManagedMetadataKind(
        "conductor_state", "skip_trail", "note", _detect_conductor_state,
        "opencode-companion oc_view.summarize_state",
    ),
    ManagedMetadataKind(
        "dispatch_block", "skip_trail", "note", _detect_dispatch_block,
        "dispatch YAML in dispatched-worker sockets (no script parser; read by agents)",
    ),
    ManagedMetadataKind(
        "estate_payload", "skip_trail", "note", _detect_estate_payload,
        "daemon glimpse_estate_payload._parse_note_candidates / parse_estate_payload",
    ),
    ManagedMetadataKind(
        "seat_manifest", "skip_trail", "note", _detect_seat_manifest,
        "opencode-companion oc_pack.parse_manifest (map / skyview lines)",
    ),
    ManagedMetadataKind(
        "snapshot_node", "skip_trail", "name", _detect_snapshot_node,
        "opencode-companion oc_sessions SNAPSHOT_PREFIX / SHELF_SNAPSHOT_PREFIX",
    ),
    ManagedMetadataKind(
        "ledger", "skip_trail", "name", _detect_ledger,
        "the program Ledger node's #prog-ledger name tag",
    ),
    ManagedMetadataKind(
        "cartographer_mapped_node", "structural", "structural", None,
        "server._classify_node_map_state -- the ancestor walk refuses the node outright",
    ),
)


# ---------------------------------------------------------------------------
# The three queries.
# ---------------------------------------------------------------------------


# @beacon[
#   id=mcp-api@managed-metadata-detect,
#   role=ƒ detect_managed_metadata(note, name),
#   slice_labels=nexus--anchors,nexus--mcp-api,
#   kind=ast,
#   comment=THE ONE managed-metadata inventory ([ap:BNNUZ4]): one deterministic registry for detect_managed_metadata, detect_trail_skip_metadata and first_refuse_guard_hit. Skip-trail kinds include program_interface, StageConfiguration, ConductorState, dispatch, estate payloads, manifests and snapshot/ledger names; mapped kinds retain the existing refusal. Estate classification delegates entirely to glimpse_estate_payload's candidate parser/extractor and requires a valid entry -- no private estate keys or shape recogniser. Ambiguous bare group names remain outside metadata recognition; the estate grammar itself treats leading promote provenance as inert, so move/copy cannot change their parcels. Other kind detectors and mapped refusal are unchanged. Pure; no I/O.,
# ]
def detect_managed_metadata(
    note: Optional[str] = None, *, name: Optional[str] = None
) -> Optional[str]:
    """The first matching managed kind of ANY guard, or None.

    ``note``/``name`` are SEMANTIC text (wire-decoded by the caller). Registry
    order is the answer order: deterministic, refuse kinds first.
    """
    for kind in _KINDS:
        if kind.detect is not None and kind.detect(note or "", name):
            return kind.kind
    return None


def detect_trail_skip_metadata(
    note: Optional[str] = None, *, name: Optional[str] = None
) -> Optional[str]:
    """The first matching SKIP-TRAIL kind, or None -- point.promote's question.

    Refuse-mapped kinds are deliberately excluded: by the time a caller asks
    this, its mapped-node refusal has already run (an AST_QUALNAME:/MD_PATH:/
    ``BEACON (`` first line is refused, never trail-skipped).
    """
    for kind in _KINDS:
        if kind.guard == "skip_trail" and kind.detect is not None and kind.detect(note or "", name):
            return kind.kind
    return None


def first_refuse_guard_hit(first_line: str) -> Optional[str]:
    """The matched refuse-mapped first-line prefix, or None.

    Byte-identical to the retired server regex: returns the matched prefix
    (``AST_QUALNAME:`` / ``MD_PATH:`` / ``BEACON (``) so refusal detail text
    is unchanged. Callers pass the note's FIRST semantic line.
    """
    match = _REFUSE_FIRST_LINE_RE.match(first_line or "")
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# The source-map marker query ([ap:UH0UOU]).
# ---------------------------------------------------------------------------

# A note whose FIRST line opens with one of these is a Cartographer/beacon
# source-map artifact: the display stripper
# (WorkFlowyClientNexus._strip_cartographer_metadata_from_note_for_preview)
# strips exactly these block shapes. ``@beacon[`` is the orphaned-residue
# first line the refuse guard deliberately excludes but the display stripper
# strips; here it is the same "this is source code" signal.
_SOURCE_MARKER_PREFIXES = ("AST_QUALNAME:", "MD_PATH:", "BEACON (", "@beacon[")


# @beacon[
#   id=mcp-api@managed-source-line,
#   role=ƒ is_managed_source_line(note),
#   slice_labels=nexus--anchors,nexus--mcp-api,glimpse-render,
#   kind=ast,
#   comment=THE source-map marker test ([ap:UH0UOU]): True iff the note's
#   FIRST line carries a Cartographer/beacon source-map marker --
#   AST_QUALNAME: / MD_PATH: / BEACON ( / @beacon[ prefixes (the shapes the
#   display stripper strips; the first three are also the refuse guard's
#   first-line set) or a Path:/Root: header whose first whitespace-delimited
#   token is exactly "Path:" or "Root:" (byte-identical to
#   parse_path_or_root_from_note's first-line grammar, mirrored here to keep
#   this module import-light -- the same mirror discipline as
#   _REFUSE_FIRST_LINE_RE). This is the "the line is source code" signal the
#   headings-only end-of-line metadata exemption keys on: the composer's ONE
#   heading-metadata emitter suppresses the whole four-field vocabulary on
#   such lines. Markers below line 1 are prose, never managed. Pure; no I/O.,
# ]
def is_managed_source_line(note: Optional[str]) -> bool:
    """Does the note's FIRST line carry a Cartographer/beacon source marker?

    The [ap:UH0UOU] exemption test: a mapped-source line (an AST node, a
    mapped FILE/FOLDER/ROOT node, a Markdown dummy block, beacon residue)
    carries none of the headings-only end-of-line metadata. Only line 1
    counts -- the same first-line-only discipline as the refuse guard and
    ``parse_path_or_root_from_note``.
    """
    lines = (note or "").splitlines()
    first = lines[0].strip() if lines else ""
    if not first:
        return False
    if first.startswith(_SOURCE_MARKER_PREFIXES):
        return True
    token = first.split(None, 1)[0]
    return token in ("Path:", "Root:")
