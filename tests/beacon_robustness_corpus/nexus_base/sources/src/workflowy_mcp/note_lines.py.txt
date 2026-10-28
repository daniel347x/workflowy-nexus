"""Pure node-as-a-file line domain and grep presentation.

Inputs are already SEMANTIC notes. Live callers decode once at acquisition;
canonical CacheView callers must NOT decode again. No cache, transport,
render/composer, filesystem, or mutation authority lives here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .mcp_api_registry import OperationValidationError


# @beacon[
#   id=note-lines@split,
#   slice_labels=nexus--mcp-api,node-ops,
#   kind=ast,
#   comment=ONE semantic note line authority for windowed reads and grep (and future range extraction). Normalize CRLF/CR to LF then splitlines; empty text has zero lines and a final terminator creates no phantom line. No decoding or I/O here; callers acquire semantic text through the existing live decoder or pinned canonical cache.,
# ]
def split_note_lines(note: str) -> list[str]:
    """Return the canonical logical lines; never strip content within a line."""
    return note.replace("\r\n", "\n").replace("\r", "\n").splitlines()


# @beacon[
#   id=auto-beacon@validate_line_integer-h7zb,
#   role=validate_line_integer,
#   slice_labels=node-ops,
#   kind=ast,
# ]
def validate_line_integer(value: int, name: str, minimum: int, maximum: int | None = None) -> None:
    """Bounds are checked here: the family schema validator checks types only."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperationValidationError("invalid_parameter_type", f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise OperationValidationError("invalid_parameter_value", f"{name} must be {bounds}")


# @beacon[
#   id=auto-beacon@format_note_lines-ti4y,
#   role=format_note_lines,
#   slice_labels=node-ops,
#   kind=ast,
# ]
def format_note_lines(
    lines: Sequence[str], *, start_line: int = 1, matched_lines: set[int] | None = None,
    width: int | None = None, numbers: Sequence[int] | None = None,
) -> str:
    """ONE numbered-row format; grep adds a separate two-column hit marker.

    Unmarked reads: ``42 | text``. Grep: ``> 42 | hit`` / ``  42 | context``.
    The line-number field fits the largest displayed number, never a fixed
    width. ``numbers`` overrides the implicit ``start_line`` numbering so a
    cropped line can render several match windows under its own number.
    Prefixes are presentation only, never mutation input.
    """
    if not lines:
        return ""
    if numbers is None:
        numbers = range(start_line, start_line + len(lines))
    width = width or len(str(max(numbers)))
    rows = []
    for number, line in zip(numbers, lines):
        marker = "" if matched_lines is None else ("> " if number in matched_lines else "  ")
        rows.append(f"{marker}{number:>{width}} | {line}")
    return "\n".join(rows)


# @beacon[
#   id=auto-beacon@compile_note_query-9mrw,
#   role=compile_note_query,
#   slice_labels=node-ops,
#   kind=ast,
#   comment=THE ONE note-line query matcher (literal or Python regex, IGNORECASE|DOTALL defaulting as before) behind node.search -- sift's grep mode and anchor.search adopt it rather than compiling a second matcher. Returns the line's MATCH SPANS: [] is no hit, otherwise every non-overlapping (start, end) character span left to right (end exclusive), so presentation can centre crop windows on the match; a plain hit test is bool(spans). Not Lightning Rod's smart/Boolean grammar; whitespace is valid literal text; only the empty string is rejected; regex anchors are line-local.,
# ]
def compile_note_query(
    query: str, *, is_regex: bool, case_sensitive: bool,
) -> Callable[[str], list[tuple[int, int]]]:
    """Literal or Python regex, applied independently to EACH logical line.

    Returns the line's match spans: ``[]`` is no hit, otherwise every
    non-overlapping ``(start, end)`` character span, left to right (end
    exclusive). The ONE query matcher for every note-line grep surface; a
    plain hit test is ``bool(spans)``. This is not Lightning Rod's
    smart/Boolean grammar. Whitespace is valid literal search text; only
    the empty string is rejected. Regex anchors are line-local.
    """
    if not isinstance(query, str):
        raise OperationValidationError("invalid_parameter_type", "query must be a string")
    if not query:
        raise OperationValidationError("invalid_query", "query must not be empty")
    try:
        pattern = re.compile(query if is_regex else re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise OperationValidationError("invalid_query", f"Invalid regular expression: {exc}") from exc

    def find(line: str) -> list[tuple[int, int]]:
        return [(match.start(), match.end()) for match in pattern.finditer(line)]

    return find


# @beacon[
#   id=auto-beacon@compile_note_name_filter-9po8,
#   role=compile_note_name_filter,
#   slice_labels=node-ops,
#   kind=ast,
# ]
def compile_note_name_filter(name_filter: str | None) -> Callable[[str], bool]:
    """Case-insensitive substring, or whole-title glob with only * and ?.

    Brackets stay literal so ``[ap:*`` filters AP-bearing titles naturally;
    there is no shell character-class or Lightning Rod Boolean grammar.
    """
    if name_filter is None:
        return lambda name: True
    if not isinstance(name_filter, str) or not name_filter:
        raise OperationValidationError("invalid_parameter_value", "name_filter must be a non-empty string")
    if "*" not in name_filter and "?" not in name_filter:
        folded = name_filter.casefold()
        return lambda name: folded in name.casefold()
    expression = re.escape(name_filter).replace(r"\*", ".*").replace(r"\?", ".")
    pattern = re.compile(expression, re.IGNORECASE | re.DOTALL)
    return lambda name: pattern.fullmatch(name) is not None


def validate_crop_width(value: int | None, name: str) -> None:
    """A crop width is a non-negative integer or null; null and 0 mean no crop."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperationValidationError("invalid_parameter_type", f"{name} must be an integer or null")
    if value < 0:
        raise OperationValidationError("invalid_parameter_value", f"{name} must be >= 0")


def _crop_context_row(line: str, width: int | None) -> str:
    """A context row keeps its first ``width`` characters, then an ellipsis."""
    if not width or len(line) <= width:
        return line
    return line[:width] + "…"


def _match_row_texts(line: str, spans: Sequence[tuple[int, int]], width: int | None) -> list[str]:
    """Hit-row texts for one line: the whole line, or per match a centred window.

    Only a line longer than ``width`` is cropped: each match gets one window
    of at most ``width`` characters of the line, centred on the match's
    midpoint and clamped to the line, with ``…`` at whichever end was cut
    (both when the match itself exceeds the width). Windows that would
    overlap -- or touch -- merge into one wider window, so two close matches
    never print the same text twice. The caller repeats the row's line
    number for every returned text. ``width`` 0 means no crop, like null.
    """
    if not width or len(line) <= width or not spans:
        return [line]
    windows: list[list[int]] = []
    for start, end in spans:
        centre = (start + end) // 2
        window_start = max(0, min(centre - width // 2, len(line) - width))
        window_end = window_start + width
        if windows and window_start < windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], window_end)
        else:
            windows.append([window_start, window_end])
    rows = []
    for window_start, window_end in windows:
        text = line[window_start:window_end]
        if window_start > 0:
            text = "…" + text
        if window_end < len(line):
            text += "…"
        rows.append(text)
    return rows


def format_pagination_banner(
    *, offset: int, returned: int, truncated: bool, max_matches: int,
) -> str | None:
    """The all-caps pagination banner for a paged report, else None.

    Informational, never an error (Dan, 092): a frontier agent read the
    ``has_more`` footer at the bottom, did not page, and later made a
    negative claim the unread pages contradicted. The banner opens the
    result INSTEAD: it names the page, the shown match range and the exact
    next offset. A page number is stated only when ``offset`` is a multiple
    of ``max_matches`` (the even pages of a repeated call); otherwise only
    the match range is shown. The bottom pagination block is unchanged.
    """
    if not truncated:
        return None
    shown = f"matches {offset + 1}-{offset + returned}"
    if max_matches > 0 and offset % max_matches == 0:
        shown = f"page {offset // max_matches + 1} ({shown})"
    return (
        "INFORMATIONAL, CRITICAL DETAIL: PAGINATION ACTIVE. DO NOT IGNORE. "
        f"These results are {shown}; more exist. If you need the full set, "
        f"keep paging: offset={offset + returned} (see the bottom for paging details)."
    )


# @beacon[
#   id=note-lines@match-controls,
#   slice_labels=nexus--mcp-api,node-ops,slices,glimpse-render,
#   kind=ast,
#   comment=THE ONE node-level match-control policy for every note-line grep surface ([ap:O3MDOY] half 2, unit 5e): node.search, anchor.search, slice_group.sift tier 'context' and lightning_rod.render_search tier 'context' all pass the SAME normalized controls (sort_by none|created|modified, sort_direction asc|desc, recent = the N newest matching NODES, recent_by, max_nodes = a node cap distinct from the matched-line cap). build_search_node_controls assembles/normalizes and returns None when everything is inactive so the report stays byte-identical; select_note_documents_for_report identifies the matching documents and delegates ALL ranking to the ONE recency idiom dock_peek_metrics.apply_dock_peek_recency (recent keeps the N newest by the resolved basis, survivors keep document order unless sorted, missing timestamps sink last, ties keep document order -- the peek/render rule; include_completed=True because each search owns its completed semantics upstream), then applies max_nodes as a POSITIONAL cap on the (optionally sorted) candidate order. Selection never reorders: without sort_by the report stays traversal order. Justification for the two helpers: assembly/facts only -- the ranking itself is apply_dock_peek_recency's, reused verbatim, not a third idiom (CF-25). Pure; no I/O.,
# ]
def build_search_node_controls(
    *,
    sort_by: str | None = None,
    sort_direction: str | None = None,
    recent: int | None = None,
    recent_by: str | None = None,
    max_nodes: int | None = None,
    timestamps: Mapping[str, Mapping[str, int | None]] | None = None,
) -> dict[str, Any] | None:
    """Assemble the ONE node-level match-control payload, or None when inactive.

    ``timestamps`` maps document UUID -> ``{"created_at": int|None,
    "modified_at": int|None}`` (the caller extracts them from its pinned map
    via ``dock_peek_metrics.node_timestamp``). Returns None when every knob is
    inactive so ``search_note_documents`` keeps its byte-identical single-pass
    path. The recency basis resolves ONCE here through the ONE render-side
    authority ``resolve_item_count_basis`` (recent_by, else the sort axis when
    it is a timestamp, else 'modified') -- the same rule the render verbs use,
    so the four searches can never drift from it.
    """
    from .glimpse_since_policy import resolve_item_count_basis

    effective_sort = sort_by if sort_by in ("created", "modified") else None
    effective_direction = sort_direction if sort_direction in ("asc", "desc") else "asc"
    active = (
        effective_sort is not None
        or recent is not None
        or (max_nodes is not None and int(max_nodes) > 0)
    )
    if not active:
        return None
    basis = resolve_item_count_basis(
        recent_by if recent_by in ("created", "modified") else None,
        effective_sort,
    )
    return {
        "sort_by": effective_sort,
        "sort_direction": effective_direction,
        "recent": int(recent) if recent is not None else None,
        "recent_by": basis,
        "max_nodes": int(max_nodes) if max_nodes is not None else None,
        "timestamps": dict(timestamps or {}),
    }


def select_note_documents_for_report(
    documents: Sequence[tuple[str, str, str] | tuple[str, str, str, str]],
    matches: Callable[[str], Sequence[tuple[int, int]]],
    node_controls: Mapping[str, Any],
) -> tuple[
    list[tuple[str, str, str] | tuple[str, str, str, str]],
    dict[int, list[int]],
    dict[str, int],
]:
    """Pass A of the controlled report: select and order the MATCHING nodes.

    A document is a candidate when the span matcher hit any of its note lines
    OR it carries a D6 marker (a node-level match that produced no single
    matching line still counts as a matching node). Ranking is the ONE recency
    idiom ``apply_dock_peek_recency`` over synthesized rows; ``max_nodes`` is
    a positional cap on the resulting order. Returns ``(ordered_documents,
    hit_numbers_by_index, facts)`` where the indices index the returned list
    and ``facts`` carries ``matching_nodes`` / ``returned_nodes`` /
    ``nodes_hidden_by_recent`` / ``nodes_hidden_by_max_nodes``.
    """
    from .dock_peek_metrics import apply_dock_peek_recency

    candidates: list[tuple[int, tuple, list[int]]] = []
    hit_numbers: dict[int, list[int]] = {}
    for index, document in enumerate(documents):
        note = document[2]
        numbers = [
            number
            for number, line in enumerate(split_note_lines(note), 1)
            if matches(line)
        ]
        marker = document[3] if len(document) > 3 else None
        if numbers or marker:
            candidates.append((index, document, numbers))
    rows = []
    timestamps = node_controls.get("timestamps") or {}
    for position, (_index, document, _numbers) in enumerate(candidates):
        entry = timestamps.get(str(document[0])) or {}
        rows.append({
            # The policy returns owned row copies without their origin; the
            # index maps the selected rows back to the candidate order.
            "index": position,
            "created_at": entry.get("created_at"),
            "modified_at": entry.get("modified_at"),
        })
    selected, selection, _sort = apply_dock_peek_recency(
        rows,
        recent=node_controls.get("recent"),
        recent_by=node_controls.get("recent_by"),
        sort_by=node_controls.get("sort_by"),
        sort_direction=node_controls.get("sort_direction"),
        # Each search owns its completed semantics upstream (the scope walk);
        # the recency policy must never re-filter them.
        include_completed=True,
    )
    ordered: list[tuple] = []
    keep_numbers: dict[int, list[int]] = {}
    # apply_dock_peek_recency returns owned row copies in final order; each
    # copy carries the candidate index it was built from.
    ordered_candidates = [candidates[row["index"]] for row in selected]
    max_nodes = node_controls.get("max_nodes")
    hidden_by_cap = 0
    if max_nodes is not None and len(ordered_candidates) > int(max_nodes):
        hidden_by_cap = len(ordered_candidates) - int(max_nodes)
        ordered_candidates = ordered_candidates[: int(max_nodes)]
    for new_index, (_old_index, document, numbers) in enumerate(ordered_candidates):
        ordered.append(document)
        keep_numbers[new_index] = numbers
    hidden_by_recent = 0
    if node_controls.get("recent") is not None:
        hidden_by_recent = int(selection.get("candidates", len(rows))) - int(
            selection.get("selected", len(selected))
        )
    facts = {
        "matching_nodes": len(candidates),
        "returned_nodes": len(ordered),
        "nodes_hidden_by_recent": hidden_by_recent,
        "nodes_hidden_by_max_nodes": hidden_by_cap,
    }
    return ordered, keep_numbers, facts


# @beacon[
#   id=note-lines@grep,
#   slice_labels=nexus--mcp-api,node-ops,
#   kind=ast,
#   comment=Pure paged COMPACT grep report over semantic (uuid, title, note) documents -- the ONE formatter every note-line grep surface shares (node.search today; sift's grep tier and anchor.search adopt it; the all-caps pagination banner format_pagination_banner, one implementation, opens every truncated report, then the optional header_line slot for a caller's Source line). One 'Matched N lines in  :' header per node, then ripgrep-style rows through the shared format_note_lines: right-aligned 1-based number, ' | ', text; '> ' marks hits only when context is shown; context windows merge and a single '...' row separates windows whose line numbers jump (context only -- a bare hit list has no separators); no blank lines inside a node, one between nodes. match_width crops hit rows to match-centred windows (_match_row_texts: one row per distant match, same line number repeated; 0 = off like null); context_width crops context rows head-first (_crop_context_row); null/0 = no crop. matches is the ONE span matcher (compile_note_query or lightning_rod_search.build_line_span_matcher): a hit is a non-empty span list. A document may carry a 4th MARKER element (D6, grep tiers): a node the node-level predicate admitted whose note has no single matching line still appears -- its header row alone, marked '(title match)' or '(match spans lines)', zero lines for paging. Optional collect dict (out-param) reports the page facts (has_more/next_offset/returned_matches/returned_occurrences) for structured variants; omitted = byte-identical string. Paging stays in matched LINES (global offset, per-page cap, one-hit lookahead); the footer also reports returned_occurrences. NODE-LEVEL MATCH CONTROLS ([ap:O3MDOY] 5e): an optional ``node_controls`` payload (built by build_search_node_controls) runs a first pass that selects/orders the MATCHING documents -- recent keeps the N newest matching nodes (hidden ones counted in the footer), sort_by orders the node groups, max_nodes positionally caps them -- BEFORE the matched-line offset/max_matches paging, so the paging unit stays the line and the controls compose orthogonally; the footer/collect then also report returned_nodes, matching_nodes and the two hidden counts. node_controls=None keeps the single pass byte-identical. Stateless, no tree composer, no I/O.,
# ]
def search_note_documents(
    documents: Iterable[tuple[str, str, str] | tuple[str, str, str, str]],
    matches: Callable[[str], Sequence[tuple[int, int]]], *,
    context_lines: int, max_matches: int, offset: int = 0,
    header_line: str | None = None,
    context_width: int | None = 160, match_width: int | None = 1000,
    collect: dict[str, Any] | None = None,
    node_controls: Mapping[str, Any] | None = None,
) -> str:
    """Compact paged grep report grouped by full UUID/title in document order.

    One ``Matched N lines in <uuid> <title>:`` header per node, then
    ripgrep-style rows: right-aligned 1-based note-line number, `` | ``, the
    text. With ``context_lines`` > 0 hit rows are marked ``> `` and context
    rows carry two spaces; context windows merge, and (only when context is
    shown) a single ``...`` row separates windows whose line numbers jump --
    a bare hit list prints no separators (no blank lines inside a node; one
    blank line between nodes). ``context_width`` crops context rows to
    their first characters and ``match_width`` crops hit rows to a
    match-centred window -- null or 0 disables each crop; a cropped line
    with several distant matches renders one row per match under the same
    line number. Skip ``offset`` matching lines globally, then select at
    most max_matches LINES (the paging unit; the footer also reports the
    returned lines' match occurrences). Stop after the first extra matching
    line: has_more is exact without a full scan for a total. Context can
    overlap other pages, but only this page's selected hits are
    marked/counted. No snapshot or cursor is retained. When more pages exist
    the FIRST line is the all-caps pagination banner (never worded as an
    error); ``header_line`` (a caller's Source line) sits directly below the
    banner and above the body.

    A document may carry a 4th MARKER element (a short suffix such as
    ``(title match)``): a node whose note matched no single line still
    appears, as its header row alone carrying the marker, and counts zero
    lines for paging (D6 -- a matched node never silently vanishes).
    ``collect``, when a dict, is filled with the page facts -- ``has_more``,
    ``next_offset``, ``returned_matches``, ``returned_occurrences`` -- for a
    caller's structured variant; omitted, the string return is
    byte-identical to the pre-collect shape.

    ``node_controls`` (built by :func:`build_search_node_controls`, None by
    default) runs the ONE node-level first pass first: the MATCHING documents
    are selected (``recent`` keeps the N newest by the resolved basis, hidden
    ones counted in the footer), ordered (``sort_by``/``sort_direction``) and
    positionally capped (``max_nodes``), and only then does the matched-LINE
    offset/max_matches paging run -- line paging composes orthogonally on top
    of the node selection, exactly the [ap:O3MDOY] composition order. With
    controls active the footer (and ``collect``) also reports
    ``returned_nodes``, ``matching_nodes``, ``nodes_hidden_by_recent`` and
    ``nodes_hidden_by_max_nodes`` -- trimmed-away nodes are counted, never
    lost silently. Without controls the single pass is byte-identical to the
    pre-control shape.
    """
    # Controls active: the ONE node-level first pass materializes and
    # re-orders the document list. No controls: keep the caller's iterable
    # LAZY -- the single pass must still stop consuming documents the moment
    # the page fills (has_more is exact without a full scan).
    if node_controls is not None:
        document_list, precomputed_hits, node_facts = select_note_documents_for_report(
            list(documents), matches, node_controls,
        )
    else:
        document_list, precomputed_hits, node_facts = documents, None, None
    groups: list[
        tuple[str, str, list[str], list[int], dict[int, list[tuple[int, int]]], str | None]
    ] = []
    skipped = 0
    total = 0
    occurrences = 0
    truncated = False
    for doc_position, document in enumerate(document_list):
        node_id, name, note = document[0], document[1], document[2]
        marker = document[3] if len(document) > 3 else None
        lines = split_note_lines(note)
        hits: list[int] = []
        spans_by_line: dict[int, list[tuple[int, int]]] = {}
        if precomputed_hits is not None:
            for number in precomputed_hits.get(doc_position, ()):
                if skipped < offset:
                    skipped += 1
                    continue
                if total == max_matches:
                    truncated = True
                    break
                spans = matches(lines[number - 1])
                hits.append(number)
                spans_by_line[number] = spans
                total += 1
                occurrences += len(spans)
        else:
            for number, line in enumerate(lines, 1):
                spans = matches(line)
                if not spans:
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                if total == max_matches:
                    truncated = True
                    break
                hits.append(number)
                spans_by_line[number] = spans
                total += 1
                occurrences += len(spans)
        if hits:
            groups.append((node_id, name, lines, hits, spans_by_line, None))
        elif marker:
            groups.append((node_id, name, [], [], {}, str(marker)))
        if truncated:
            break

    sections = []
    banner = format_pagination_banner(
        offset=offset, returned=total, truncated=truncated, max_matches=max_matches,
    )
    if banner:
        sections.append(banner)
    if header_line is not None:
        sections.append(header_line)
    if not groups:
        sections.append(
            f"No matching note lines at or after offset={offset}."
            if offset else "No matching note lines."
        )
    for node_id, name, lines, hits, spans_by_line, marker in groups:
        header = (
            f"Matched {len(hits)} lines in {node_id} "
            f"{json.dumps(name, ensure_ascii=False)}"
        )
        if marker:
            header += f" {marker}"
        if not hits:
            # A marker document (D6): the header row alone, no line rows.
            sections.append(header + ":")
            continue
        windows: list[tuple[int, int]] = []
        for number in hits:
            start, end = max(1, number - context_lines), min(len(lines), number + context_lines)
            if windows and start <= windows[-1][1] + 1:
                windows[-1] = (windows[-1][0], max(end, windows[-1][1]))
            else:
                windows.append((start, end))
        width = len(str(windows[-1][1]))
        hit_set = set(hits)
        # The `> ` marker distinguishes hits from context rows, so it exists
        # only when context is shown; a bare hit list prints unmarked rows.
        matched_lines = hit_set if context_lines > 0 else None
        chunks = []
        for start, end in windows:
            row_texts: list[str] = []
            row_numbers: list[int] = []
            for number in range(start, end + 1):
                line = lines[number - 1]
                if number in hit_set:
                    texts = _match_row_texts(line, spans_by_line[number], match_width)
                else:
                    texts = [_crop_context_row(line, context_width)]
                row_texts.extend(texts)
                row_numbers.extend([number] * len(texts))
            chunks.append(format_note_lines(
                row_texts, numbers=row_numbers, matched_lines=matched_lines, width=width,
            ))
        # The `...` separator is a CONTEXT-window affordance (rg -C): with no
        # context the rows are a bare hit list -- no separators at all.
        separator = "\n...\n" if context_lines > 0 else "\n"
        sections.append(header + ":\n" + separator.join(chunks))
    next_offset = str(offset + total) if truncated else "null"
    if collect is not None:
        collect["has_more"] = truncated
        collect["next_offset"] = offset + total if truncated else None
        collect["returned_matches"] = total
        collect["returned_occurrences"] = occurrences
    footer_facts = ""
    if node_facts is not None:
        if collect is not None:
            collect.update(node_facts)
        footer_facts = f"; returned_nodes={node_facts['returned_nodes']}"
        if node_facts["matching_nodes"] != node_facts["returned_nodes"]:
            footer_facts += f"; matching_nodes={node_facts['matching_nodes']}"
        if node_facts["nodes_hidden_by_recent"]:
            footer_facts += (
                f"; nodes_hidden_by_recent={node_facts['nodes_hidden_by_recent']}"
            )
        if node_facts["nodes_hidden_by_max_nodes"]:
            footer_facts += (
                f"; nodes_hidden_by_max_nodes={node_facts['nodes_hidden_by_max_nodes']}"
            )
    sections.append(
        f"Pagination: offset={offset}; returned_matches={total}; "
        f"returned_occurrences={occurrences}; "
        f"has_more={'true' if truncated else 'false'}; next_offset={next_offset}"
        f"{footer_facts}."
    )
    if truncated:
        sections.append(
            f"Truncated at max_matches={max_matches}; additional matching lines exist. "
            f"Repeat the same search with offset={next_offset} to continue."
        )
    return "\n\n".join(sections)


# @beacon[
#   id=note-lines@extract-ranges,
#   slice_labels=nexus--mcp-api,
#   kind=ast,
#   comment=ONE closed extract-range normalizer: single form is a one-element batch, 1-based inclusive bounds, copied input rows, no per-item destination overrides. Shape/bounds/overlap errors precede all writes; capture retains ownership of name/AP-ID/tag semantics.,
# ]
def normalize_extract_ranges(
    *, start_line: int | None = None, end_line: int | None = None,
    name: str | None = None, ranges: list[dict] | None = None,
    ap_id: str | None = "auto",
) -> tuple[bool, list[dict]]:
    """Normalize single/batch syntax without reading notes or reserving IDs."""
    batch = ranges is not None
    if batch and any(value is not None for value in (start_line, end_line, name)):
        raise OperationValidationError(
            "invalid_parameter_combination", "ranges cannot be combined with start_line, end_line, or name",
        )
    if batch and (not isinstance(ranges, list) or not ranges):
        raise OperationValidationError("invalid_parameter_combination", "ranges must be a non-empty array")
    if ap_id is not None and not isinstance(ap_id, str):
        raise OperationValidationError("invalid_parameter_type", "ap_id must be a string or null")
    items = ranges if batch else [{"start_line": start_line, "end_line": end_line, "name": name}]
    normalized = []
    for index, item in enumerate(items):
        label = f"ranges[{index}]"
        if not isinstance(item, dict):
            raise OperationValidationError("invalid_parameter_type", f"{label} must be an object")
        unknown = sorted(set(item) - {"start_line", "end_line", "name", "ap_id"})
        if unknown:
            raise OperationValidationError("forbidden_parameter", f"{label}: unknown fields {unknown}")
        start, end = item.get("start_line"), item.get("end_line")
        validate_line_integer(start, f"{label}.start_line", 1)
        validate_line_integer(end, f"{label}.end_line", 1)
        if end < start:
            raise OperationValidationError("line_range_out_of_bounds", f"{label}: end_line must be >= start_line")
        title = item.get("name")
        if not isinstance(title, str) or not title.strip():
            raise OperationValidationError("invalid_parameter_type", f"{label}.name must be a non-blank string")
        policy = item.get("ap_id", ap_id)
        if policy is not None and not isinstance(policy, str):
            raise OperationValidationError("invalid_parameter_type", f"{label}.ap_id must be a string or null")
        normalized.append({"start_line": start, "end_line": end, "name": title.strip(), "ap_id": policy})
    normalized.sort(key=lambda row: row["start_line"])
    for previous, following in zip(normalized, normalized[1:]):
        if following["start_line"] <= previous["end_line"]:
            raise OperationValidationError("invalid_parameter_combination", "Extraction ranges must not overlap; adjacent ranges are allowed")
    return batch, normalized


# @beacon[
#   id=note-lines@extract-plan,
#   slice_labels=nexus--mcp-api,
#   kind=ast,
#   comment=Pure extraction plan from ONE semantic source snapshot through split_note_lines: validate ALL ranges before capture, compute one new point per range and one sorted deletion splice. No seam cleanup, alternate splitter, cache, or mutation. Report actual post-join logical line count, including terminal-empty-line normalization.,
# ]
def plan_note_extraction(note: str, ranges: Sequence[dict]) -> tuple[list[dict], str, dict]:
    """Return capture rows, replacement note, and source counts; zero I/O.

    ``ranges`` is the sorted, non-overlapping output of normalize_extract_ranges.
    Empty extracted text is refused; non-empty whitespace is preserved.
    Deletion rejoins with LF as specified, without adding a final terminator.
    """
    lines = split_note_lines(note)
    planned, retained = [], []
    cursor = 0
    for row in ranges:
        start, end = row["start_line"], row["end_line"]
        if end > len(lines):
            raise OperationValidationError(
                "line_range_out_of_bounds", f"Range {start}..{end} exceeds the live note's {len(lines)} lines",
            )
        content = "\n".join(lines[start - 1:end])
        if not content:
            raise OperationValidationError("line_range_out_of_bounds", f"Range {start}..{end} contains empty text")
        planned.append({**row, "content": content, "line_count": end - start + 1, "char_count": len(content)})
        retained.extend(lines[cursor:start - 1])
        cursor = end
    retained.extend(lines[cursor:])
    replacement = "\n".join(retained)
    return planned, replacement, {
        "lines_before": len(lines),
        "lines_after": len(split_note_lines(replacement)),
        "deleted_line_count": sum(row["line_count"] for row in planned),
    }
