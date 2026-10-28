"""Pure read view: the numbered, length-guarded ``beacon.snippet`` output and
the shared note-read flag set for ``node.read`` and ``anchor.point.read``.

ONE guard core (``render_guarded_lines``) and its wrappers: the snippet
renderer (``render_snippet_view``), the shared note-guard policy
(``render_guarded_note_view``) used by both public note fronts, and the
note-read plan/normalizer they share. They read only the client's snippet
result dict, pure parameter mappings, or a semantic note — no I/O, no
caching, no transport; numbered rows are produced only by
``note_lines.format_note_lines`` — the same formatter ``node.read`` uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .mcp_api_registry import OperationValidationError
from .note_lines import (
    format_note_lines,
    split_note_lines,
    validate_line_integer,
)

DEFAULT_MAX_LINES = 64
# node.read / anchor.point.read guard threshold (Dan, 2026-10-01): 300 head +
# 100 tail. beacon.snippet keeps its own function-sized default (64).
DEFAULT_NOTE_MAX_LINES = 400
BYTES_PER_TOKEN_ESTIMATE = 3


@dataclass(frozen=True)
class SnippetViewOptions:
    """The validated view knobs for one ``beacon.snippet`` render."""

    numbering: str
    coordinates: str
    start_line: int | None
    end_line: int | None
    limit: int | None
    context: int
    max_lines: int
    size_only: bool


# @beacon[
#   id=snippet-view@normalize,
#   slice_labels=nexus--mcp-api,ra-read-text-snippet,
#   kind=ast,
#   comment=THE one snippet-side normalizer: validates and defaults the beacon.snippet view knobs (numbering, coordinates, explicit window, context, max_lines guard, size_only) into one frozen SnippetViewOptions; the note-read twin for node.read/anchor.point.read is normalize_note_view_params. Pure, no I/O.
# ]
def normalize_snippet_view_params(params: Mapping[str, Any]) -> SnippetViewOptions:
    """Validate and default the ``beacon.snippet`` view parameters."""
    numbering = params.get("numbering") or "file"
    if numbering not in ("file", "relative", "none"):
        raise OperationValidationError(
            "invalid_enum_value",
            "numbering must be one of: file, relative, none",
        )
    coordinates = params.get("coordinates") or (
        "relative" if numbering == "relative" else "file"
    )
    if coordinates not in ("file", "relative"):
        raise OperationValidationError(
            "invalid_enum_value",
            "coordinates must be one of: file, relative",
        )

    start_line = params.get("start_line")
    end_line = params.get("end_line")
    for name, value in (("start_line", start_line), ("end_line", end_line)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise OperationValidationError(
                "invalid_parameter_type", f"{name} must be an integer"
            )
        if coordinates == "file":
            validate_line_integer(value, name, 1)

    limit = params.get("limit")
    if limit is not None:
        validate_line_integer(limit, "limit", 1)

    context = params.get("context", 0)
    validate_line_integer(context, "context", 0)

    max_lines = params.get("max_lines", DEFAULT_MAX_LINES)
    validate_line_integer(max_lines, "max_lines", 0)

    size_only = params.get("size_only", False)
    if not isinstance(size_only, bool):
        raise OperationValidationError(
            "invalid_parameter_type", "size_only must be a boolean"
        )

    if end_line is not None and limit is not None:
        raise OperationValidationError(
            "invalid_parameter_combination",
            "pass end_line or limit, not both",
        )
    if size_only and any(value is not None for value in (start_line, end_line, limit)):
        raise OperationValidationError(
            "invalid_parameter_combination",
            "size_only cannot be combined with start_line, end_line, or limit",
        )
    if start_line is not None and end_line is not None and end_line < start_line:
        raise OperationValidationError(
            "invalid_parameter_value",
            "end_line must be >= start_line",
        )

    return SnippetViewOptions(
        numbering=numbering,
        coordinates=coordinates,
        start_line=start_line,
        end_line=end_line,
        limit=limit,
        context=context,
        max_lines=max_lines,
        size_only=size_only,
    )


# @beacon[
#   id=snippet-view@header-format,
#   slice_labels=nexus--mcp-api,ra-read-text-snippet,glimpse-render,agent-kit-vendor,
#   kind=ast,
#   comment=THE one snippet size header — one format, never two: "[<prefix>lines S-E of N | L lines, B bytes, ~T tokens]" (bytes counted over the span, one newline per line; ~3 bytes/token; the " | relative 1 = line S" suffix when relative_start is given). Extracted verbatim out of render_guarded_lines so beacon.snippet and render_symbols' metadata tier (54e8356f(ii)) print the SAME line; the guard core delegates here and render_symbol_tier_note calls it directly. Pure, no I/O.
# ]
def format_snippet_header(
    header_prefix: str,
    lines: Sequence[str],
    span_start: int,
    span_end: int,
    *,
    relative_start: int | None = None,
) -> str:
    """Compose THE one snippet size header over an already-clamped span.

    ``header_prefix`` is the exact text inside the brackets before
    ``lines`` — ``"snippet: ‹path› | "`` for beacon.snippet, ``"note: "``
    for a note view. ``relative_start`` (the span's first file line) adds
    the ``| relative 1 = line N`` suffix exactly when the caller renders
    relative numbering. This is the single format authority: the shared
    guard core delegates here, and ``render_symbols``' metadata tier reuses
    it verbatim so the header can never drift between the two surfaces.
    """
    total_lines = len(lines)
    span_length = span_end - span_start + 1
    byte_count = sum(
        len(line.encode("utf-8")) + 1 for line in lines[span_start - 1 : span_end]
    )
    token_estimate = -(-byte_count // BYTES_PER_TOKEN_ESTIMATE)
    header = (
        f"[{header_prefix}lines {span_start}-{span_end} of {total_lines} | "
        f"{span_length} lines, {byte_count} bytes, ~{token_estimate} tokens]"
    )
    if relative_start is not None:
        header = header[:-1] + f" | relative 1 = line {relative_start}]"
    return header


# @beacon[
#   id=snippet-view@guard,
#   slice_labels=nexus--mcp-api,ra-read-text-snippet,ra-snippet-range,node-ops,
#   kind=ast,
#   comment=THE one head+tail guard core behind beacon.snippet AND node.read (and anchor.point.read): clamps a span to max_lines rows (head + tail, ONE elision row naming the hidden range and the start_line/end_line window that reads it back) through the ONE clamp_span_to_head_tail arithmetic (shared with the note-edit diff renderer, [ap:4JSBO5]), honors explicit start_line/end_line/limit windows in file or span-relative coordinates, and delegates the one-line size header to format_snippet_header (the one format render_symbols' metadata tier reuses verbatim). Pure, no I/O; render_snippet_view and the note fronts are thin wrappers over this.
# ]
def render_guarded_lines(
    lines: Sequence[str],
    span_start: int,
    span_end: int,
    header_prefix: str,
    options: SnippetViewOptions,
    *,
    with_header: bool,
) -> str:
    """THE shared head+tail guard core.

    ``lines`` is the whole document; ``span_start``/``span_end`` are the
    1-based inclusive span to show (clamped here, exactly as the snippet
    renderer always clamped them). ``header_prefix`` is the exact text of the
    size header inside the brackets before ``lines`` — ``"snippet: ‹path› | "``
    for beacon.snippet, ``"note: "`` for a note view. ``with_header`` decides
    whether the size header prefixes the rows: beacon.snippet hides it in
    ``numbering: "none"`` mode, while a guarded or size-only note view always
    shows it. Byte-for-byte, this is the body that lived in
    ``render_snippet_view``; the wrappers only choose the label and the
    header policy.
    """
    total_lines = len(lines)

    span_start = min(max(1, int(span_start)), total_lines)
    span_end = min(max(span_start, int(span_end)), total_lines)
    span_length = span_end - span_start + 1

    def to_file(value: int) -> int:
        return span_start - 1 + value if options.coordinates == "relative" else value

    def display(file_line: int) -> int:
        return (
            file_line - span_start + 1
            if options.numbering == "relative"
            else file_line
        )

    def hint_value(file_line: int) -> int:
        # The inverse of to_file: express a file line back in the window's
        # coordinate system, so the elision hint is valid when passed back
        # with the same options.
        return (
            file_line - span_start + 1
            if options.coordinates == "relative"
            else file_line
        )

    explicit = (
        options.start_line is not None
        or options.end_line is not None
        or options.limit is not None
    )
    if options.start_line is not None or options.end_line is not None:
        window_start = (
            to_file(options.start_line) if options.start_line is not None else span_start
        )
        if options.end_line is not None:
            window_end = to_file(options.end_line)
        elif options.limit is not None:
            window_end = window_start + options.limit - 1
        else:
            window_end = max(window_start, span_end)
    elif options.limit is not None:
        window_start = span_start
        window_end = min(span_end, span_start + options.limit - 1)
    else:
        window_start, window_end = span_start, span_end

    if window_start > total_lines:
        raise OperationValidationError(
            "invalid_parameter_value",
            f"start_line is past the end of the file ({total_lines} lines)",
        )
    if window_end < 1:
        raise OperationValidationError(
            "invalid_parameter_value",
            "the window ends above line 1",
        )
    if window_end < window_start:
        raise OperationValidationError(
            "invalid_parameter_value",
            "end_line must be >= start_line",
        )
    window_start = max(1, window_start)
    window_end = min(total_lines, window_end)

    guarded = (
        not explicit
        and options.max_lines > 0
        and span_length > options.max_lines
    )
    if guarded:
        # THE one clamp arithmetic (clamp_span_to_head_tail): tail =
        # max_lines // 4, head = the rest. Extracted for [ap:4JSBO5] so the
        # note-edit diff renderer shares this exact split; the elision row
        # wording stays each surface's own (the read names its readback).
        head_end, tail_start, elided_start, elided_end, elided_count = (
            clamp_span_to_head_tail(span_start, span_end, options.max_lines)
        )
        head = head_end - span_start + 1
        tail = span_end - tail_start + 1
        runs = [
            (max(1, span_start - options.context), span_start + head - 1),
            (span_end - tail + 1, min(total_lines, span_end + options.context)),
        ]
        runs = [(start, end) for start, end in runs if start <= end]
    else:
        elided_start = elided_end = elided_count = 0
        head = tail = 0
        runs = [
            (
                max(1, window_start - options.context),
                min(total_lines, window_end + options.context),
            )
        ]

    header = format_snippet_header(
        header_prefix,
        lines,
        span_start,
        span_end,
        relative_start=span_start if options.numbering == "relative" else None,
    )

    if options.size_only:
        return header if with_header else ""

    displayed_numbers = [
        number
        for run_start, run_end in runs
        for number in range(display(run_start), display(run_end) + 1)
    ]
    width = max((len(str(number)) for number in displayed_numbers), default=1)
    if guarded:
        width = max(width, 3)

    pieces: list[str] = []
    for index, (run_start, run_end) in enumerate(runs):
        if guarded and index == 1:
            sentence = (
                f"... {elided_count} lines not shown "
                f"({display(elided_start)}-{display(elided_end)}); "
                f"read them with start_line={hint_value(elided_start)}, "
                f"end_line={hint_value(elided_end)}"
            )
            if options.numbering == "none":
                pieces.append(sentence)
            else:
                pieces.append(f"{'...':>{width}} | {sentence}")
        if options.numbering == "none":
            pieces.append("\n".join(lines[run_start - 1 : run_end]))
        else:
            pieces.append(
                format_note_lines(
                    lines[run_start - 1 : run_end],
                    start_line=display(run_start),
                    width=width,
                )
            )

    if not with_header:
        return "\n".join(pieces)
    return header + "\n" + "\n".join(pieces)


# @beacon[
#   id=snippet-view@clamp-span,
#   slice_labels=nexus--mcp-api,ra-read-text-snippet,ra-snippet-range,node-ops,agent-kit-vendor,
#   kind=ast,
#   comment=THE one head+tail clamp arithmetic behind every guarded view ([ap:4JSBO5]): the exact split render_guarded_lines has always applied (tail = max_lines // 4, head = the rest), extracted so the note-edit diff renderer can share it and no second clamp can exist. Returns the 1-based span split (head_end, tail_start, elided_start, elided_end, elided_count); each caller renders its OWN elision row (the read names the start_line/end_line readback, the diff names the omitted hunks and the +/- totals). Pure; total_lines = span_end - span_start + 1 <= max_lines is the caller's guard.
# ]
def clamp_span_to_head_tail(
    span_start: int, span_end: int, max_lines: int
) -> tuple[int, int, int, int, int]:
    """Split a 1-based inclusive span into head + tail with one elided middle.

    Returns ``(head_end, tail_start, elided_start, elided_end, elided_count)``:
    the head run is ``span_start .. head_end``, the tail run is
    ``tail_start .. span_end``, and the middle ``elided_start .. elided_end``
    (``elided_count`` lines) is what a guarded view hides. The split is the
    exact arithmetic ``render_guarded_lines`` has always applied; the caller
    must guarantee ``span_end - span_start + 1 > max_lines`` before calling.
    """
    tail = max_lines // 4
    head = max_lines - tail
    return (
        span_start + head - 1,
        span_end - tail + 1,
        span_start + head,
        span_end - tail,
        (span_end - span_start + 1) - max_lines,
    )


# @beacon[
#   id=snippet-view@render,
#   slice_labels=nexus--mcp-api,ra-read-text-snippet,
#   kind=ast,
#   comment=THE snippet-side wrapper over the one shared guard core (render_guarded_lines): labels the size header "snippet: <file>" and hides it in numbering='none' mode exactly as before; the same core serves node.read's note view under a "note:" label, so the two verbs can never drift. Pure, no I/O, never re-reads the file.
# ]
def render_snippet_view(result: Mapping[str, Any], options: SnippetViewOptions) -> str:
    """Render the client snippet dict into the agent-facing snippet view.

    Reads exactly the carrier keys ``file_lines``, ``core_start_line``,
    ``core_end_line`` and ``file_path``; a missing or wrongly-typed key raises
    ``KeyError``/``TypeError`` (the caller maps that to ``source_read_failed``).
    An explicit window outside the file raises ``OperationValidationError``.
    """
    file_lines = result["file_lines"]
    core_start_line = result["core_start_line"]
    core_end_line = result["core_end_line"]
    file_path = result["file_path"]
    total_lines = len(file_lines)

    if total_lines == 0:
        if options.numbering == "none" and not options.size_only:
            return ""
        return f"[snippet: {file_path} | empty file]"

    return render_guarded_lines(
        file_lines,
        core_start_line,
        core_end_line,
        f"snippet: {file_path} | ",
        options,
        with_header=options.size_only or options.numbering != "none",
    )


# ---------------------------------------------------------------------------
# render_symbols tier bodies (54e8356f item ii) — same carrier dict, same
# header format, one presentation authority.
# ---------------------------------------------------------------------------

# Per-language comment markers for the def-line fallback scan (a symbol whose
# cached node carries no line metadata). A Markdown heading has no comment
# marker: the first non-blank span line IS the "def".
_TIER_COMMENT_MARKERS = {
    ".py": "#",
    ".sh": "#",
    ".bash": "#",
    ".yaml": "#",
    ".yml": "#",
    ".toml": "#",
    ".js": "//",
    ".jsx": "//",
    ".ts": "//",
    ".tsx": "//",
    ".vue": "//",
    ".sql": "--",
}
# The composer's fence grammar (api_client_nexus fence_re): "^ {0,3}(`{3,}|~{3,})"
# — only a line STARTING with the marker (after <=3 spaces) can open or close
# a fence, so the chooser only has to avoid those shapes.
_TIER_FENCE_BACKTICK_RE = re.compile(r"^ {0,3}`{3,}")
_TIER_FENCE_TILDE_RE = re.compile(r"^ {0,3}~{3,}")


def _tier_fence_for(body_lines: Sequence[str]) -> str:
    """Pick a fence the one composer parses and the body cannot close early."""
    if any(_TIER_FENCE_BACKTICK_RE.match(line) for line in body_lines):
        if not any(_TIER_FENCE_TILDE_RE.match(line) for line in body_lines):
            return "~~~~"
        return "`" * 12
    return "```"


def _symbol_context_span_lines(
    file_lines: Sequence[str],
    core_start: int,
    core_end: int,
    *,
    file_path: str,
    def_line: int | None,
    context_lines: int,
    tail_lines: int,
) -> list[str]:
    """The 'context' tier body: span head through the def line plus the first
    ``context_lines`` body lines, then ONE trailer row naming the hidden
    remainder, then the optional tail. The span itself always starts with the
    beacon comment block when there is one, so 'comment + def + body' is
    simply the span's head."""
    marker = _TIER_COMMENT_MARKERS.get(Path(file_path).suffix.lower())
    resolved_def = def_line if (
        isinstance(def_line, int) and core_start <= def_line <= core_end
    ) else None
    if resolved_def is None:
        resolved_def = core_start
        for offset in range(core_start, core_end + 1):
            text = file_lines[offset - 1].strip()
            if not text:
                continue
            if marker and text.startswith(marker):
                continue
            resolved_def = offset
            break
    head_end = min(core_end, resolved_def + max(0, context_lines))
    shown: list[str] = list(file_lines[core_start - 1 : head_end])
    tail_start = core_end - max(0, tail_lines) + 1
    if max(0, tail_lines) <= 0 or tail_start <= head_end:
        tail_start = core_end + 1  # no tail (or the head already reaches it)
    hidden_start = head_end + 1
    hidden_end = tail_start - 1
    hidden = hidden_end - hidden_start + 1
    if hidden > 0:
        shown.append(
            f"… {hidden} more lines; start_line={hidden_start}, end_line={hidden_end}"
        )
    if tail_start <= core_end:
        shown.extend(file_lines[tail_start - 1 : core_end])
    return shown


# @beacon[
#   id=snippet-view@symbol-tier-note,
#   slice_labels=nexus--mcp-api,ra-read-text-snippet,glimpse-render,
#   kind=ast,
#   comment=THE one tier-body presentation authority for lightning_rod render_symbols (54e8356f(ii)): consumes the SAME carrier dict render_snippet_view does (file_path/file_lines/core_start_line/core_end_line) and returns the complete fenced note for tier 'metadata' (exactly the format_snippet_header line — the same numbers beacon.snippet prints) or 'context' (beacon comment + def line + first context_lines body lines, one trailer row naming the rest, optional tail_lines footer). The def line comes from the cached node's mapped line when it lies inside the span, else the first non-comment non-blank span line. Everything rides in a composer-parseable fence so source '#' lines are never promoted to headings. Pure, no I/O.
# ]
def render_symbol_tier_note(
    tier: str,
    result: Mapping[str, Any],
    *,
    def_line: int | None = None,
    context_lines: int = 5,
    tail_lines: int = 0,
) -> str:
    """Render one symbol's tier body as a complete fenced note.

    ``tier`` is ``'metadata'`` or ``'context'`` (the caller validates the
    vocabulary and refuses the default ``'slice'`` before calling).
    ``result`` is the client snippet dict (the same carrier keys
    ``render_snippet_view`` reads); a missing key raises ``KeyError`` /
    ``TypeError`` for the caller to report as a per-symbol tier failure.
    """
    file_lines = result["file_lines"]
    core_start = int(result["core_start_line"])
    core_end = int(result["core_end_line"])
    file_path = result["file_path"]
    total_lines = len(file_lines)
    if total_lines == 0:
        raise OperationValidationError(
            "invalid_parameter_value",
            f"source file {file_path!r} is empty; nothing to disclose",
        )
    core_start = min(max(1, core_start), total_lines)
    core_end = min(max(core_start, core_end), total_lines)

    header = format_snippet_header(f"snippet: {file_path} | ", file_lines, core_start, core_end)
    if tier == "metadata":
        body: list[str] = [header]
    elif tier == "context":
        body = _symbol_context_span_lines(
            file_lines,
            core_start,
            core_end,
            file_path=str(file_path),
            def_line=def_line,
            context_lines=context_lines,
            tail_lines=tail_lines,
        )
    else:
        raise OperationValidationError(
            "invalid_parameter_value",
            f"unknown tier {tier!r}; use 'slice', 'metadata', or 'context'",
        )
    fence = _tier_fence_for(body)
    return "\n".join([fence, *body, fence])


# @beacon[
#   id=snippet-view@note-guard,
#   slice_labels=nexus--mcp-api,ra-read-text-snippet,node-ops,
#   kind=ast,
#   comment=THE one note-guard policy behind BOTH public note fronts (node.read and anchor.point.read): decides when the shared guard core applies to a whole note (size_only always renders the "[note: ...]" size header, "[note: empty]" for an empty note; an explicit window is never guarded; otherwise guard iff max_lines > 0 and the note is longer) and renders it via render_guarded_lines under the "note: " label. Returns the guarded body, or None when the caller's own banner/header is followed by unguarded rows. Pure, no I/O.
# ]
def render_guarded_note_view(
    note: str, options: SnippetViewOptions, *, windowed: bool
) -> str | None:
    """Apply the shared note guard to one semantic note; None when it does not fire.

    ``note`` is the semantic (decoded) note text; ``windowed`` is the
    caller's explicit-window flag (a windowed read is never guarded). The
    return is exactly the body both public fronts prepend their own
    banner/header to — the guard policy lives here once, so ``node.read``
    and ``anchor.point.read`` cannot drift.
    """
    lines = split_note_lines(note)
    if options.size_only:
        return (
            "[note: empty]"
            if not lines
            else render_guarded_lines(
                lines, 1, len(lines), "note: ", options, with_header=True
            )
        )
    if not windowed and options.max_lines > 0 and len(lines) > options.max_lines:
        return render_guarded_lines(
            lines, 1, len(lines), "note: ", options, with_header=True
        )
    return None


def validate_note_read_lines(
    offset: int | None, limit: int | None, numbered: bool
) -> int:
    """Validate both note fronts' line options before I/O; None skips zero."""
    offset = 0 if offset is None else offset
    validate_line_integer(offset, "offset", 0)
    if limit is not None:
        validate_line_integer(limit, "limit", 1)
    if not isinstance(numbered, bool):
        raise OperationValidationError(
            "invalid_parameter_type", "with_line_numbers must be a boolean"
        )
    return offset


# @beacon[
#   id=snippet-view@note-body,
#   slice_labels=nexus--mcp-api,node-ops,ra-snippet-range,
#   kind=ast,
#   comment=THE one pure note-body renderer behind node.read and anchor.point.read: shared guard/size header first, otherwise windowed or numbered rows through split_note_lines and format_note_lines, otherwise the semantic note EXACTLY (including CRLF and a final terminator). The two fronts only add their own banner or metadata header; validation happens before I/O through validate_note_read_lines. Internal callers with view=None stay unguarded.
# ]
def render_note_body(
    note: str,
    offset: int,
    limit: int | None,
    numbered: bool,
    view: SnippetViewOptions | None,
    windowed: bool,
) -> str:
    """Render a validated semantic note body; never fetch, decode or frame it."""
    if view is not None:
        body = render_guarded_note_view(note, view, windowed=windowed)
        if body is not None:
            return body
    if windowed or numbered:
        lines = split_note_lines(note)
        selected = lines[offset:] if limit is None else lines[offset:offset + limit]
        return (
            format_note_lines(selected, start_line=offset + 1)
            if numbered else "\n".join(selected)
        )
    return note


@dataclass(frozen=True)
class NoteReadPlan:
    """ONE validated public note-read flag set (``node.read`` and
    ``anchor.point.read`` share it).

    ``offset``/``limit`` are the merged window in the exact shape
    ``_node_read_impl`` already takes (``start_line``/``end_line`` fold into
    them); ``with_line_numbers`` merges the ``numbering`` alias; ``view``
    carries the shared guard knobs in the one ``SnippetViewOptions`` shape
    (note profile: whole-document span, no context, no explicit window).
    """

    offset: int | None
    limit: int | None
    with_line_numbers: bool
    view: SnippetViewOptions


# @beacon[
#   id=snippet-view@note-read,
#   slice_labels=nexus--mcp-api,ra-read-text-snippet,node-ops,
#   kind=ast,
#   comment=THE one note-read flag set shared by node.read and anchor.point.read: validates and merges the two window spellings (offset XOR start_line, limit XOR end_line), the numbering/with_line_numbers alias pair, and the shared guard knobs (max_lines default 400 = 300 head + 100 tail, 0 = off; size_only) into one NoteReadPlan for the public read fronts. default_numbered selects the verb's numbered-rows default (node.read False, anchor.point.read True) when neither spelling is given. Internal callers never pass through here, so they read unguarded. Pure, no I/O.
# ]
def normalize_note_view_params(
    params: Mapping[str, Any], *, default_numbered: bool = False
) -> NoteReadPlan:
    """Validate and default the shared note-read view parameters.

    One guard, one flag set with ``beacon.snippet``: ``max_lines`` /
    ``size_only`` / ``start_line`` / ``end_line`` / ``limit`` / ``numbering``
    / ``coordinates`` mean the same here (a note's span is the whole
    document, so ``relative`` numbering and coordinates coincide with
    ``file``); ``offset`` and ``with_line_numbers`` are node.read's native
    spellings of the window and the numbered gutter, and ``context`` is
    snippet-only (refused by the read schemas before this runs).
    ``default_numbered`` is the calling verb's numbered-rows default,
    applied when neither ``numbering`` nor ``with_line_numbers`` is given
    (False for ``node.read``, True for ``anchor.point.read``).
    """
    offset = params.get("offset")
    limit = params.get("limit")
    start_line = params.get("start_line")
    end_line = params.get("end_line")
    numbering = params.get("numbering")
    coordinates = params.get("coordinates")
    max_lines = params.get("max_lines", DEFAULT_NOTE_MAX_LINES)
    size_only = params.get("size_only", False)
    with_line_numbers = params.get("with_line_numbers")

    if with_line_numbers is not None and not isinstance(with_line_numbers, bool):
        raise OperationValidationError(
            "invalid_parameter_type", "with_line_numbers must be a boolean"
        )
    if offset is not None and start_line is not None:
        raise OperationValidationError(
            "invalid_parameter_combination",
            "pass offset or start_line, not both",
        )
    if end_line is not None and limit is not None:
        raise OperationValidationError(
            "invalid_parameter_combination",
            "pass end_line or limit, not both",
        )

    for name, value in (("start_line", start_line), ("end_line", end_line)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise OperationValidationError(
                "invalid_parameter_type", f"{name} must be an integer"
            )
        validate_line_integer(value, name, 1)
    if offset is not None:
        validate_line_integer(offset, "offset", 0)
    if limit is not None:
        validate_line_integer(limit, "limit", 1)
    validate_line_integer(max_lines, "max_lines", 0)
    if not isinstance(size_only, bool):
        raise OperationValidationError(
            "invalid_parameter_type", "size_only must be a boolean"
        )
    if coordinates is not None and coordinates not in ("file", "relative"):
        raise OperationValidationError(
            "invalid_enum_value", "coordinates must be one of: file, relative"
        )
    if numbering is not None and numbering not in ("file", "relative", "none"):
        raise OperationValidationError(
            "invalid_enum_value", "numbering must be one of: file, relative, none"
        )

    if start_line is not None:
        offset = start_line - 1
        if end_line is not None:
            if end_line < start_line:
                raise OperationValidationError(
                    "invalid_parameter_value", "end_line must be >= start_line"
                )
            limit = end_line - start_line + 1
    elif end_line is not None:
        limit = end_line

    # numbering is the shared alias of with_line_numbers ('relative' is a
    # note's 'file': the span is the whole document). Both given and
    # disagreeing is loud; both agreeing, or either alone, merges.
    numbered_by_numbering: bool | None = None
    if numbering is not None:
        numbered_by_numbering = numbering != "none"
    if numbered_by_numbering is not None and with_line_numbers is not None:
        if bool(with_line_numbers) != numbered_by_numbering:
            raise OperationValidationError(
                "invalid_parameter_combination",
                "pass numbering or with_line_numbers, not both",
            )
    numbered = (
        numbered_by_numbering
        if numbered_by_numbering is not None
        else bool(with_line_numbers)
        if with_line_numbers is not None
        else default_numbered
    )

    if size_only and any(
        value is not None for value in (offset, limit, start_line, end_line)
    ):
        raise OperationValidationError(
            "invalid_parameter_combination",
            "size_only cannot be combined with offset, start_line, end_line, or limit",
        )

    return NoteReadPlan(
        offset=offset,
        limit=limit,
        with_line_numbers=numbered,
        view=SnippetViewOptions(
            numbering="file" if numbered else "none",
            coordinates="file",
            start_line=None,
            end_line=None,
            limit=None,
            context=0,
            max_lines=max_lines,
            size_only=size_only,
        ),
    )
