"""Estate payload parsing — the server-side port of the widget's tolerant
saved-tabs import grammar (Saved Tabs program unit E1, [ap:ARDOF6] §E1).

An ESTATE is the set of views a session stands in: concretely the saved-tabs
JSON payload (``[{alias, title, url, spec}, ...]`` — the exact Tier-0 export
format ``buildSavedTabsExportJson`` emits) held in the note of an ordinary
Anchor Point. This module is the PURE (no I/O, no rendering, no resolution)
parse half of ``glimpse(verb='estate')``:

  * :func:`extract_estate_json_candidates` — locate JSON candidates in a
    note's Markdown (fenced ```json blocks first, then balanced bare values);
  * :func:`parse_estate_payload` — the tolerant grammar ported from the
    widget's ``parseSavedTabsImport`` (canonical array / single wrapper /
    bare render_spec blob), EXTENDED with the estate verb's own entry form:
    a NAMED SLICE GROUP — a bare string (whatever ``group_handle`` accepts)
    or ``{"group": "...", "alias": "..."}`` — because an estate must not
    require a JSON blob per entry ([ap:ARDOF6] §E1 item 1);
  * :func:`parse_estate_note` — extract + parse + first-parseable-wins over
    the candidates, with loud, actionable errors on every failure shape.

The render half lives in ``server.py`` (``_glimpse_estate_impl``): every
parsed entry normalizes into the ONE ``RenderProjectionSpec`` and rides the
EXISTING ``_glimpse_dispatch_spec`` funnel — this module never renders.
Entries are classified here, never validated against live config or cache
(an unresolvable group or root is a loud PER-ENTRY failure at render time,
[ap:ARDOF6] §E1 item 5).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

from .simple_form import (
    SimpleFormResult,
    normalize_simple_form,
    # CF-10: the ONE bare-blob recogniser (and its kind vocabulary) now lives
    # in simple_form, the module this one already imports. The names are
    # re-exported here so existing importers of the payload module keep
    # working; there is exactly one definition site.
    _HANDLE,
    _SPEC_KINDS,
    _looks_like_spec_blob,
)

__all__ = [
    "EstateEntry",
    "extract_estate_json_candidates",
    "parse_estate_note",
    "parse_estate_payload",
]

# A bare-note JSON scan is bounded so a pathological note (thousands of
# unbalanced braces in prose) can never make extraction quadratic-for-ever.
_MAX_BARE_CANDIDATES = 24

# Fenced code block: ```<optional language tag>\n<body>``` (body non-greedy).
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+.-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@EstateEntry-2rtc,
#   role=EstateEntry,
#   slice_labels=saved-tabs,glimpse-render,
#   kind=ast,
# ]
class EstateEntry:
    """One parsed estate entry, in payload order.

    Two mutually exclusive payload forms (``spec`` wins when a dict carries
    both, mirroring the widget grammar's spec-first coercion):

      * ``spec`` — a render-spec blob dict, VERBATIM as saved (never
        re-minted, re-prefixed or normalized here; the funnel owns that).
      * ``group`` — a named slice-group handle string (bare-string entry or
        ``{"group": ...}`` wrapper): whatever ``group_handle`` accepts (exact
        id, unique ``grp-`` prefix, full name, or the semantic short name
        after the last ``|``). Resolution happens at render time through the
        SAME ``_resolve_slice_group_handle`` an agent ``group_handle``
        GLIMPSE uses.

    ``valid=False`` entries are KEPT (never silently dropped): the estate
    verb renders a loud per-entry error block for them and continues with
    the others ([ap:ARDOF6] §E1 item 5).
    """

    index: int  # 1-based position in the payload (stable across filtering)
    alias: str = ""
    title: str = ""
    url: str = ""
    spec: dict[str, Any] | None = None
    group: str | None = None
    valid: bool = False
    invalid_reason: str = ""
    simple_form: SimpleFormResult | None = None
    error_code: str = ""
    # QBCTWU U3-2: True ONLY on a bare-NAME line of a per-line named estate
    # (a bare string line is ambiguous: prose vs slice-group name). The
    # bare-handle caller requires flagged names to RESOLVE before treating
    # the note as an estate (the estate-vs-plain-node classification);
    # entries from legacy JSON payloads are never flagged — a saved-tabs
    # export stays an estate with loud per-entry errors.
    bare_name_line: bool = False

    @property
    def is_group(self) -> bool:
        """True for the named-slice-group entry form."""
        return self.group is not None

    @property
    def display_name(self) -> str:
        """Heading/ack display name: alias → title → group → url → index."""
        for candidate in (self.alias, self.title, self.group, self.url):
            text = str(candidate or "").strip()
            if text:
                return text
        return f"entry {self.index}"


# CF-10: _looks_like_spec_blob and _SPEC_KINDS are DEFINED in simple_form.py
# (the one recogniser home, which carries the beacon block) and imported
# above; no local copy remains.


# @beacon[
#   id=auto-beacon@_coerce_entry-64up,
#   role=_coerce_entry,
#   slice_labels=saved-tabs,
#   kind=ast,
# ]
def _coerce_entry(raw: Any, index: int) -> EstateEntry:
    """Coerce ONE payload element into an :class:`EstateEntry` (never raises).

    Accepts the widget grammar's ``{alias, title, url, spec}`` object, the
    estate extension's bare group-handle string and ``{"group", "alias"}``
    wrapper, the qualified parcels ``{"node": H}`` / ``{"subtree": H}`` /
    the long ``{"ref": H, "kind": K}`` form, and — as a hand-authored
    convenience consistent with the top-level bare-blob form — an array
    element that IS a bare spec blob. ``{"lens": L}`` (optionally
    alias/title) is the GPLENH estate sugar and lands as its lowered
    ``{lightning: {mode: render_lens, lens: L}}`` spec. Anything else is
    kept as an INVALID entry with an actionable reason.
    """
    form = normalize_simple_form(raw, surface="estate_note")
    if not form.ok:
        return EstateEntry(index=index, valid=False, invalid_reason=form.error_message,
                           error_code=form.error_code,
                           alias=raw.get("alias", "") if isinstance(raw, dict) else "")
    if form.form in {"dock", "tags", "symbols", "node", "subtree", "bare_handle"}:
        return EstateEntry(index=index, alias=form.alias, spec=form.spec, valid=True,
                           simple_form=form)
    if form.form == "formal_spec" and isinstance(raw, dict) and "lens" in raw \
            and isinstance(form.spec, dict):
        # GPLENH lens sugar: the grammar lowered {lens: L} to the exact
        # render_lens spec. No handle resolution rides on the entry (the
        # lens engine resolves its own atoms against the Stage-0 snapshot).
        title = raw.get("title") if isinstance(raw.get("title"), str) else ""
        return EstateEntry(index=index, alias=form.alias, title=title,
                           spec=form.spec, valid=True)
    if isinstance(raw, str):
        group = raw.strip()
        if group:
            return EstateEntry(index=index, group=group, valid=True)
        return EstateEntry(
            index=index,
            valid=False,
            invalid_reason=(
                "entry is an empty string (expected a slice-group name or an "
                "{alias, title, url, spec} object)"
            ),
        )
    if isinstance(raw, list):
        return EstateEntry(
            index=index,
            valid=False,
            invalid_reason="entry is an array (expected an object or a slice-group name string)",
        )
    if not isinstance(raw, dict):
        return EstateEntry(
            index=index,
            valid=False,
            invalid_reason=(
                f"entry is {type(raw).__name__} (expected an object or a "
                "slice-group name string)"
            ),
        )

    alias = raw.get("alias") if isinstance(raw.get("alias"), str) else ""
    title = raw.get("title") if isinstance(raw.get("title"), str) else ""
    url = raw.get("url") if isinstance(raw.get("url"), str) else ""

    spec = raw.get("spec")
    if isinstance(spec, dict):
        return EstateEntry(
            index=index, alias=alias, title=title, url=url, spec=spec, valid=True,
        )

    group = raw.get("group")
    if isinstance(group, str) and group.strip():
        return EstateEntry(
            index=index, alias=alias, title=title, url=url,
            group=group.strip(), valid=True,
        )

    # Bare spec blob as an array element (hand-authored shorthand; the
    # wrapper fields of this same dict are NOT inventored — a spec blob's
    # keys are scope fields, not display fields).
    if _looks_like_spec_blob(raw):
        return EstateEntry(index=index, spec=raw, valid=True)

    return EstateEntry(
        index=index, alias=alias, title=title, url=url,
        valid=False,
        invalid_reason=(
            'no "spec" object or "group" name on this entry (expected '
            "{alias, title, url, spec}, a bare render_spec blob, or a "
            "slice-group name / {group, alias} wrapper)"
        ),
    )


# @beacon[
#   id=auto-beacon@parse_estate_payload-vbdl,
#   role=parse_estate_payload,
#   slice_labels=saved-tabs,glimpse-render,
#   kind=ast,
# ]
def parse_estate_payload(data: Any) -> "tuple[list[EstateEntry] | None, str | None]":
    """Parse an already-JSON-decoded estate payload into entries.

    The tolerant top-level grammar ported from the widget's
    ``parseSavedTabsImport`` (canonical array / single ``{spec}`` wrapper /
    bare render_spec blob — plus the wrong-file guards that redirect a
    slice-groups or anchor-views export to its own import surface), extended
    with the estate-only forms: a bare group-handle STRING payload, a
    ``{"group": ...}`` wrapper, the qualified ``{"node": H}`` /
    ``{"subtree": H}`` parcels (long ``{ref, kind}`` included), and the
    GPLENH ``{"lens": L}`` sugar.

    Returns ``(entries, None)`` on success — entries may include INVALID
    ones (kept for loud per-entry failure at render time) — or ``(None,
    error)`` with an actionable message. PURE; never raises.
    """
    form = normalize_simple_form(data, surface="estate_note")
    if not form.ok:
        if form.is_simple:
            return [_coerce_entry(data, 1)], None
        return None, form.error_message
    if form.form in {"dock", "tags", "symbols", "node", "subtree", "bare_handle"}:
        return [_coerce_entry(data, 1)], None
    if isinstance(data, str):
        group = data.strip()
        if group:
            return [EstateEntry(index=1, group=group, valid=True)], None
        return None, (
            "estate payload is an empty string (expected a slice-group name, "
            "a saved-tabs JSON array, a single entry, or a bare render_spec blob)"
        )
    if isinstance(data, list):
        if not data:
            return None, (
                "estate payload is an EMPTY array — nothing to render. Expected "
                "[{alias, title, url, spec}, ...] (a Saved-Tabs export), whose "
                "entries may also be slice-group names, a single such entry, or "
                "one bare render_spec blob."
            )
        return [_coerce_entry(raw, i + 1) for i, raw in enumerate(data)], None
    if isinstance(data, dict):
        # Wrong-file guards (the widget's targeted redirects, ported): the
        # sibling export formats are NOT estate payloads — say which surface
        # they belong to instead of a generic shape error.
        kind_tag = data.get("kind") if isinstance(data.get("kind"), str) else ""
        if kind_tag == "glimpse-slice-groups-export" or isinstance(
            data.get("expand_groups"), list
        ):
            return None, (
                "this is a SLICE-GROUPS export (saved group DEFINITIONS), not an "
                "estate / saved-tabs payload — import it via the widget's "
                "slice-groups Import button, or point glimpse estate at a note "
                "holding [{alias, title, url, spec}, ...]."
            )
        if kind_tag == "glimpse_anchor_views_export" or isinstance(
            data.get("anchor_views"), list
        ):
            return None, (
                "this is an ANCHOR-VIEWS export, not an estate / saved-tabs "
                "payload."
            )
        if isinstance(data.get("spec"), dict):
            return [_coerce_entry(data, 1)], None
        group = data.get("group")
        if isinstance(group, str) and group.strip():
            return [_coerce_entry(data, 1)], None
        if isinstance(data.get("lens"), dict):
            # GPLENH sugar at the top level: {lens: L} (optionally
            # alias/title) as the whole payload. Checked AFTER the
            # wrong-file guards so a saved-tabs export still redirects.
            return [_coerce_entry(data, 1)], None
        if _looks_like_spec_blob(data):
            return [EstateEntry(index=1, spec=data, valid=True)], None
        return None, (
            "expected a JSON array of { alias, title, url, spec } entries (or a "
            "single such entry, one bare render_spec blob, or a slice-group "
            "name); the object given carries none of those shapes"
        )
    return None, (
        f"estate payload top level is {type(data).__name__}; expected a JSON "
        "array of { alias, title, url, spec } entries (or a single such entry, "
        "one bare render_spec blob, or a slice-group name)"
    )


# @beacon[
#   id=auto-beacon@_scan_bare_json_values-15vk,
#   role=_scan_bare_json_values,
#   slice_labels=saved-tabs,
#   kind=ast,
# ]
def _scan_bare_json_values(text: str) -> list[tuple[str, str]]:
    """Scan ``text`` for balanced top-level JSON values.

    A small string-aware bracket matcher (not a parser): finds each top-level
    ``[`` or ``{`` outside a JSON string, walks to its matching close, and
    yields ``(kind, substring)`` for :func:`json.loads` to accept or reject.
    A top-level bare JSON STRING (the one-group-name estate shorthand) is
    admitted ONLY when it occupies its own line (whitespace aside) — prose
    quotations must never parse as group names. Returned in two classes:
    ``value`` (array/object) first, ``string`` last — the most ambiguous
    form is tried last. Bounded by ``_MAX_BARE_CANDIDATES``.
    """
    values: list[tuple[str, str]] = []
    strings: list[tuple[str, str]] = []
    i = 0
    n = len(text)
    while i < n and (len(values) + len(strings)) < _MAX_BARE_CANDIDATES:
        ch = text[i]
        if ch == '"':
            # Whole-line bare JSON string only (see docstring).
            bol = text.rfind("\n", 0, i) + 1
            if not text[bol:i].strip():
                j = i + 1
                escaped = False
                while j < n:
                    c = text[j]
                    if escaped:
                        escaped = False
                    elif c == "\\":
                        escaped = True
                    elif c == '"':
                        break
                    j += 1
                if j < n:
                    eol = text.find("\n", j + 1)
                    if eol == -1:
                        eol = n
                    if not text[j + 1 : eol].strip():
                        strings.append(("string", text[i : j + 1]))
                    i = j + 1
                    continue
            i += 1
            continue
        if ch not in "[{":
            i += 1
            continue
        # Prose filter: an estate payload always carries JSON string quotes
        # (its keys, a group name, or a bare "string" payload). Bracketed
        # prose that never opens a quote — an [ap:CORE] handle, a [1]
        # footnote — is skipped without a pointless parse attempt.
        if text.find('"', i) == -1:
            break
        close = "]" if ch == "[" else "}"
        depth = 0
        in_string = False
        escaped = False
        j = i
        while j < n:
            c = text[j]
            if in_string:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_string = False
            elif c == '"':
                in_string = True
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    if c == close:
                        values.append(("value", text[i : j + 1]))
                    i = j + 1
                    break
            j += 1
        else:
            # Unbalanced through EOF: no further top-level value can close.
            break
        if j >= n:
            break
        # Advance past the extracted value (loop's i was set on break).
        i = max(i, j + 1)
    return values + strings


# @beacon[
#   id=auto-beacon@extract_estate_json_candidates-a49f,
#   role=extract_estate_json_candidates,
#   slice_labels=saved-tabs,
#   kind=ast,
# ]
def extract_estate_json_candidates(note_text: str) -> list[tuple[str, str]]:
    """Ordered ``(label, candidate_json_text)`` pairs from a note's Markdown.

    Extraction order (first parseable estate payload wins, so the most
    deliberate form is tried first):

      1. fenced ```json blocks, in document order;
      2. every other fenced block, in document order;
      3. balanced bare JSON array/object values scanned from the raw note
         text, in document order (a pasted compact payload with no fence);
      4. whole-line bare JSON strings (the one-group-name shorthand — the
         most ambiguous form, tried last).
    """
    text = note_text or ""
    fenced = [
        (m.group(1).strip().lower(), m.group(2))
        for m in _FENCE_RE.finditer(text)
    ]
    candidates: list[tuple[str, str]] = []
    for lang, body in [f for f in fenced if f[0] == "json"]:
        candidates.append(("fenced ```json block", body))
    for lang, body in [f for f in fenced if f[0] != "json"]:
        candidates.append(
            (f"fenced ```{lang} block" if lang else "fenced code block", body)
        )
    for _kind, bare in _scan_bare_json_values(text):
        candidates.append(("bare JSON value in the note", bare))
    return candidates


# @beacon[
#   id=auto-beacon@_json_loads_tolerant-colq,
#   role=_json_loads_tolerant,
#   slice_labels=saved-tabs,
#   kind=ast,
# ]
def _json_loads_tolerant(text: str) -> Any:
    """``json.loads`` with ONE NBSP-mangling fallback.

    The Workflowy UI note editor converts every LEADING indent space of a
    pasted multi-line payload to U+00A0, which strict JSON rejects — the
    reason the widget's ONE serializer emits compact single-line bytes
    (Ledger ST-007). A NOTE-held payload is exactly where a hand-pasted
    pretty payload picks up that mangling, so on a first parse failure ONLY
    we retry once with NBSP→space. Valid JSON never reaches the fallback, so
    a string value's interior is never silently altered. Raises the second
    failure unchanged.
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        if "\u00a0" in text:
            return json.loads(text.replace("\u00a0", " "))
        raise


# @beacon[
#   id=saved-tabs@estate-payload-parser,
#   slice_labels=nexus--mcp-api,nexus--md-preview,saved-tabs,glimpse-render,
#   kind=ast,
#   comment=Pure estate note extraction: leading promote provenance ('↩ moved from ' / '↩ derived from ', including stacked lines and their blank separators) is inert BEFORE every grammar rule. Fenced/bare JSON retains Saved-Tabs grammar; single-line names/handles and per-line parcels use the ONE simple-form grammar, with bare-name flags unchanged. Tolerant candidate parsing also serves managed_metadata's structured-estate detector; no second recogniser. No I/O or Dock classification; resolution happens before rendering.,
# ]
def parse_estate_note(note_text: str) -> "tuple[list[EstateEntry] | None, str | None]":
    """Extract + parse the estate payload from a note's Markdown text.

    Three grammars, in order (QBCTWU U3-2 · named estates one parcel per
    line):

      1. A SINGLE-LINE note: today's fast paths, unchanged (a bare handle or
         group name; else the tolerant candidate scan).
      2. A MULTI-LINE note whose every non-blank line — of the whole note,
         or of the FIRST fenced block when the note fences its estate — is a
         parcel: a NAMED ESTATE, one entry per line, in order. A bare-name
         line is flagged (``EstateEntry.bare_name_line``) so the bare-handle
         caller can require the name to resolve (prose must never masquerade
         as an estate).
      3. Anything else: today's tolerant candidate scan (fenced ```json
         blocks, then bare JSON values) — prose notes with an embedded
         example keep working, and a note/fence that parses whole as ONE
         JSON value is the legacy Saved-Tabs payload path, unchanged.

    Returns ``(entries, None)`` on success, or ``(None, error)`` with a loud,
    actionable message when no candidate parses. An all-INVALID entry list is
    only returned when NO candidate yields at least one valid entry (the
    genuine-estate-of-broken-entries case — the caller renders loud per-entry
    error blocks); a candidate that parses but is all-invalid is otherwise
    skipped in favour of a later candidate (an unrelated fenced JSON example
    earlier in the note must not shadow the real payload).
    """
    text = note_text or ""
    # Curation provenance is framing, not a parcel. Strip only a LEADING
    # run of the two exact promote prefixes (and blank separators), never
    # such text inside a payload or later prose. With no prefix, keep bytes.
    lines = text.splitlines(keepends=True)
    first_payload = 0
    has_provenance = False
    for line in lines:
        if line.startswith(("↩ moved from ", "↩ derived from ")):
            has_provenance = True
        elif line.strip():
            break
        first_payload += 1
    if has_provenance:
        text = "".join(lines[first_payload:])
    stripped = text.strip()
    # ── single-line notes: the fast paths, unchanged ─────────────────────
    # AP handles contain brackets which the JSON extractor also sees.
    # Classify the whole single line FIRST so [ap:X] is not mistaken for a
    # malformed JSON array. Bracketed prose still goes to the old extractor.
    if stripped and "\n" not in stripped and "\r" not in stripped:
        simple = normalize_simple_form(stripped, surface="estate_note")
        if simple.form == "bare_handle":
            return parse_estate_payload(stripped)
        candidates = extract_estate_json_candidates(text)
        if not candidates:
            # CF-14: the gate excludes only the JSON STRUCTURE openers ({ and [).
            # A double quote is a legal character inside an UNQUOTED group name
            # (spec §6.3 names only brace and bracket), so a one-line note like
            # ‘Scratch | Estate "Test"’ is a group-name candidate, not prose to
            # refuse; if the name matches nothing the resolver fails loudly.
            if not any(c in stripped for c in "{["):
                entries, err = parse_estate_payload(stripped)
                if err is None and entries:
                    # The single-line group-name lane is prose-ambiguous in
                    # exactly the same way a per-line bare name is: flag it
                    # so the bare-handle classification can require the name
                    # to resolve (QBCTWU U3-2).
                    entries = [
                        replace(e, bare_name_line=True)
                        if (e.valid and e.is_group) else e
                        for e in entries
                    ]
                return entries, err
            return None, _NO_JSON_PAYLOAD_ERROR
        return _parse_note_candidates(candidates)

    # ── multi-line: the named-estate grammar, one parcel per line ────────
    fence = _FENCE_RE.search(text)
    parcel_text = fence.group(2) if fence else text
    # (i) A note/fence that parses whole as ONE JSON value is the legacy
    # Saved-Tabs payload path, unchanged — and it must win BEFORE the
    # per-line attempt, or a pretty-printed payload's `[`/`]`/`},` lines
    # would masquerade as parcel lines.
    try:
        whole = _json_loads_tolerant(parcel_text)
    except Exception:  # noqa: BLE001 - the tolerant loader raises on prose
        whole = _NOT_JSON
    if whole is not _NOT_JSON:
        entries, err = parse_estate_payload(whole)
        if err is None and entries and any(e.valid for e in entries):
            return entries, None
    # (ii) One parcel per line.
    per_line, per_line_ok = _parse_note_lines(parcel_text)
    if per_line_ok:
        return per_line, None
    # (iii) Fallback: today's tolerant candidate scan over the whole note.
    candidates = extract_estate_json_candidates(text)
    if not candidates:
        return None, _NO_JSON_PAYLOAD_ERROR
    return _parse_note_candidates(candidates)


# Sentinel for "the whole parcel text is not one JSON value" (rule i).
_NOT_JSON = object()

_NO_JSON_PAYLOAD_ERROR = (
    "no JSON payload found in the note: expected a fenced ```json block "
    "or a bare JSON array of { alias, title, url, spec } entries (a "
    "Saved-Tabs export, whose entries may also be slice-group names), "
    "a single such entry, one bare render_spec blob, or a slice-group "
    "name string."
)


def _parse_note_lines(parcel_text: str) -> "tuple[list[EstateEntry] | None, bool]":
    """One parcel per line — the NAMED-ESTATE line grammar (QBCTWU U3-2).

    Every non-blank line must coerce to a VALID entry (JSON lines decode
    first; a line carrying ``{`` or ``[`` is JSON-or-fail, never a group
    name — mirroring the single-line gate). Returns ``(entries, True)`` when
    EVERY line is a parcel, else ``(None, False)`` so the caller falls back
    to the tolerant candidate scan. Bare-name lines (a string value — quoted
    or not) are flagged ``bare_name_line``: prose-ambiguous, resolved later.
    """
    lines = [ln.strip() for ln in parcel_text.splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return None, False
    entries: list[EstateEntry] = []
    for line in lines:
        decoded = True
        try:
            value = _json_loads_tolerant(line)
        except Exception:  # noqa: BLE001 - a non-JSON line is a string lane
            value, decoded = line, False
        if not decoded and any(c in value for c in "{[") and not _HANDLE.fullmatch(value):
            # A brace/bracket line that failed JSON is prose-shaped junk,
            # never a group name (same gate as the single-line path) — but a
            # HANDLE-shaped line ([ap:CORE], bracketed/plain hex) is a
            # parcel, whatever brackets it carries. A line that DECODED —
            # even to a string holding braces — was explicit JSON and keeps
            # the string lane.
            return None, False
        entry = _coerce_entry(value, len(entries) + 1)
        if not entry.valid:
            return None, False
        if entry.is_group and isinstance(value, str):
            entry = replace(entry, bare_name_line=True)
        entries.append(entry)
    return entries, True


def _parse_note_candidates(
    candidates: "list[tuple[str, str]]",
    *, allow_group_strings: bool = True,
) -> "tuple[list[EstateEntry] | None, str | None]":
    """First-valid-candidate grammar, also the structured metadata authority.

    Metadata recognition excludes ambiguous standalone group-name strings;
    ordinary estate reads keep that shorthand (the default), unchanged.
    """
    first_error: str | None = None
    all_invalid_fallback: list[EstateEntry] | None = None
    all_invalid_label: str = ""
    for label, candidate in candidates:
        try:
            data = _json_loads_tolerant(candidate)
        except Exception as exc:  # noqa: BLE001 - parser is total by contract
            if first_error is None:
                first_error = f"{label}: invalid JSON ({exc})"
            continue
        if not allow_group_strings and isinstance(data, str):
            continue
        entries, err = parse_estate_payload(data)
        if err is not None:
            if first_error is None:
                first_error = f"{label}: {err}"
            continue
        if any(e.valid for e in (entries or [])):
            return entries, None
        # Parses, but every entry is invalid: remember the FIRST such
        # candidate as the fallback and keep scanning — a later candidate
        # with at least one valid entry is the more likely real payload.
        if all_invalid_fallback is None:
            all_invalid_fallback = entries
            all_invalid_label = label
    if all_invalid_fallback is not None:
        return all_invalid_fallback, None
    return None, (
        "could not parse a saved-tabs/estate payload from the note — "
        f"{first_error}. Expected a fenced ```json block or a bare JSON array "
        "of { alias, title, url, spec } entries (a Saved-Tabs export, whose "
        "entries may also be slice-group names), a single such entry, one bare "
        "render_spec blob, or a slice-group name string."
        + (f" (last candidate tried: {all_invalid_label})" if all_invalid_label else "")
    )
