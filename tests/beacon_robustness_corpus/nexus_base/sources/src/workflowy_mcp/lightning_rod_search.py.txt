"""Lightning Rod free-text SEARCH predicate (pure, no I/O).

The Stage-1 SELECTION predicate for Lightning Rod ``render_search`` mode. This
module owns ONE thing: turning an agent-supplied ``query`` string into a pure
``(node) -> bool`` membership predicate over a /nodes-export cache node dict
(which carries ``name`` and ``note``), suitable for
``f8_slice_policy.build_selected_subtree_from_cache``.

WHY IT LIVES ALONE (mirrors ``glimpse_since_policy``): it is a small, pure,
dependency-light grammar with no upstream dependency on the server or the
client, so there is no import cycle and it is trivially unit-testable in
isolation.

AGENT-IN-THE-MIDDLE (the design driver). The consumer of ``query`` is NOT a
human at a search bar -- it is an LLM constructing the argument from what the
user said in natural language. So the grammar is deliberately SMALL, EXPLICIT,
and DETERMINISTIC (no fuzzy human-forgiveness leniency), and it is documented
with concrete examples in :func:`build_search_predicate` (that docstring is the
interface the agent programs against). ``shlex`` is used purely as a robust,
stdlib, quote-aware TOKENIZER -- not as a UX feature.

TWO MODES:
  * ``smart`` (default) -- ``shlex``-tokenized boolean substring matching:
      - bare terms are ANDed (all must appear),
      - the bareword ``OR`` (case-insensitive) makes the terms on either side
        of it alternatives,
      - a leading ``-`` or ``!`` on a term NEGATES it (must NOT appear),
      - ``"quoted phrases"`` match as an exact (case-insensitive) substring,
      - a term containing ``*`` or ``?`` is a glob wildcard (``fnmatch`` over
        the whole target text),
      - all matching is case-insensitive substring by default.
  * ``regex`` -- the whole ``query`` is compiled ONCE
    (``re.IGNORECASE | re.DOTALL``) and matched with ``re.search`` over the
    target text.

TARGET TEXT: each node's ``name`` + ``"\n"`` + ``note`` (so a match in either
field admits the node, consistent with Workflowy search semantics and the tag
path's name+note reading)...
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from typing import Any, Callable


class LightningRodQueryError(ValueError):
    """Raised when a Lightning Rod search query cannot be parsed/compiled.

    The orchestrator catches this and returns a structured, actionable error to
    the agent (never a silent empty result), so a malformed query is loud.
    """


def _node_target_text(node: dict[str, Any], match_scope: str = "name_and_note") -> str:
    """Return the search target text for a cache node, honoring ``match_scope``.

    ``match_scope`` is the SELECTION-stage scope knob (see the module-level
    contract shared with the tag predicates in ``f8_slice_policy``):

      * ``"name_and_note"`` (default -- byte-for-byte prior behavior) returns
        ``name + '\\n' + note`` (either may be empty), so a match in EITHER
        field admits the node.
      * ``"name"`` returns ONLY the node's NAME, so the search is restricted to
        the node title and never peeks into the note body.

    This is the SINGLE choke point for search scope: both the ``smart`` and
    ``regex`` predicates match against whatever this returns, so honoring the
    scope here covers both query modes for free. Any unrecognized value is
    treated as the default (name+note) rather than raising -- the surface-level
    tool validates the enum; this pure helper fails safe to the broader scope.
    """
    name = node.get("name")
    parts = []
    if name:
        parts.append(str(name))
    if match_scope != "name":
        note = node.get("note")
        if note:
            parts.append(str(note))
    return "\n".join(parts)


def _smart_terms(query: str) -> list[list[dict[str, Any]]]:
    """Tokenize a ``smart`` query into OR-groups of AND-terms.

    Returns a list (OR) of lists (AND) of term dicts
    ``{"text": str, "negate": bool, "is_glob": bool}``. Raises
    :class:`LightningRodQueryError` if the query yields no usable term.
    """
    try:
        tokens = shlex.split(query)
    except ValueError as e:
        # Unbalanced quotes, etc. -- surface it as a loud, actionable error.
        raise LightningRodQueryError(
            f"Could not parse search query (check your quotes): {e}"
        ) from e

    or_groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for tok in tokens:
        if tok.upper() == "OR":
            # Boundary between alternative groups. An empty group (leading /
            # doubled OR) is simply skipped rather than erroring -- forgiving on
            # this one structural nicety, since it can't change semantics.
            if current:
                or_groups.append(current)
                current = []
            continue
        negate = False
        text = tok
        if text[:1] in ("-", "!"):
            negate = True
            text = text[1:]
        if not text:
            # A bare "-" / "!" with nothing after it is meaningless; skip it.
            continue
        is_glob = ("*" in text) or ("?" in text)
        current.append({"text": text, "negate": negate, "is_glob": is_glob})
    if current:
        or_groups.append(current)

    # A query that produced no positive/negative term at all is degenerate.
    if not or_groups or all(not grp for grp in or_groups):
        raise LightningRodQueryError(
            "Search query is empty after parsing; supply at least one term."
        )
    return or_groups


def _term_matches(term: dict[str, Any], text_lower: str) -> bool:
    """True iff ``term``'s POSITIVE sense matches ``text_lower`` (pre-lowered)."""
    t = term["text"].lower()
    if term["is_glob"]:
        # Wildcard anywhere in the text: bracket the pattern with * so fnmatch
        # (which anchors) behaves like a "contains" glob.
        pat = t if t.startswith("*") else "*" + t
        if not pat.endswith("*"):
            pat = pat + "*"
        return fnmatch.fnmatch(text_lower, pat)
    return t in text_lower


def _smart_group_matches(group: list[dict[str, Any]], text_lower: str) -> bool:
    """True iff EVERY term in an AND-group is satisfied (negation honored)."""
    for term in group:
        hit = _term_matches(term, text_lower)
        if term["negate"]:
            if hit:
                return False
        else:
            if not hit:
                return False
    return True


def _glob_span_pattern(text: str) -> str:
    """Translate a ``#``-free glob body into an UNANCHORED span regex.

    ``*`` becomes a lazy join between the concrete segments and ``?`` any one
    character, so ``re.search`` finds the leftmost CONCRETE match -- the span
    a crop window can centre on. A leading/trailing ``*`` (the ``contains``
    bracketing ``_term_matches`` applies) is dropped first, so ``*term*``
    spans exactly ``term``. Pure; no I/O.
    """
    pieces: list[str] = []
    for ch in text:
        if ch == "*":
            pieces.append("\x00")
        elif ch == "?":
            pieces.append(".")
        else:
            pieces.append(re.escape(ch))
    segments = [s for s in "".join(pieces).split("\x00") if s]
    return ".*?".join(segments)


# @beacon[
#   id=slices@line-span-matcher,
#   role=ƒ build_line_span_matcher(query, query_mode),
#   slice_labels=slices,glimpse-render,node-ops,
#   kind=ast,
#   comment=THE ONE per-line span matcher for a note-line grep over an already node-level-ADMITTED document set (slice_group.sift's tier='context' report today; render_search's and anchor.search's grep tiers adopt it rather than compiling a second matcher). Same query grammar as build_search_predicate -- it shares _smart_terms -- but applied to ONE logical line: smart mode hits on any POSITIVE term/phrase of any OR-group (negations and AND-grouping were enforced at the node level) with globs matched through a lazy unanchored span regex (_glob_span_pattern) so the span can centre a crop window; regex mode compiles IGNORECASE per line (line-local, so no DOTALL -- the same flags compile_note_query uses). Returns the ONE span contract: [] is no hit, otherwise every (start, end) character span left to right (end exclusive). Raises LightningRodQueryError exactly when the node-level predicate build would, so the two can never disagree on a valid query.,
# ]
def build_line_span_matcher(
    query: str,
    query_mode: str = "smart",
) -> Callable[[str], list[tuple[int, int]]]:
    """Build the per-line span matcher for a note-line grep tier.

    The sibling of :func:`build_search_predicate` for the grep tiers: the
    node-level predicate has ALREADY decided which documents are in scope;
    this matcher locates the matching lines inside them. Same grammar, same
    validation, one shared ``_smart_terms`` tokenizer:

    * ``smart`` (default) -- a line hits on any POSITIVE term or phrase of
      any OR-group. Negations and AND-grouping were enforced at the node
      level and are deliberately NOT re-applied per line. A glob term
      matches through a lazy unanchored span translation (``fnmatch``
      semantics; leading/trailing ``*`` dropped), so the reported span is
      the concrete match, not the whole line.
    * ``regex`` -- the pattern compiled ONCE with ``re.IGNORECASE``
      (line-local: no ``DOTALL``, the same flags
      ``note_lines.compile_note_query`` uses) and run with ``finditer`` per
      line.

    Returns the line's match spans -- ``[]`` is no hit, otherwise every
    ``(start, end)`` character span left to right (end exclusive) -- the
    exact contract :func:`note_lines.search_note_documents` consumes. A
    match found only ACROSS lines (a ``DOTALL`` regex or a spanning glob
    admitted the node at the node level) hits no single line; the caller's
    marker document (D6) keeps that node visible.

    Raises :class:`LightningRodQueryError` exactly when
    :func:`build_search_predicate` would, so the two builders can never
    disagree on a valid query.
    """
    mode = (query_mode or "smart").strip().lower()
    q = query if isinstance(query, str) else ""
    if not q.strip():
        raise LightningRodQueryError("Search query must be a non-empty string.")

    if mode == "regex":
        try:
            rx = re.compile(q, re.IGNORECASE)
        except re.error as e:
            raise LightningRodQueryError(
                f"Invalid regular expression: {e}"
            ) from e

        def _regex_line(line: str) -> list[tuple[int, int]]:
            return [(match.start(), match.end()) for match in rx.finditer(line)]

        return _regex_line

    if mode != "smart":
        raise LightningRodQueryError(
            f"Unknown query_mode {query_mode!r}; use 'smart' or 'regex'."
        )

    or_groups = _smart_terms(q)
    finders: list[Callable[[str], list[tuple[int, int]]]] = []
    for group in or_groups:
        for term in group:
            if term["negate"]:
                # Node-level constraint only: a negated term never marks a line.
                continue
            body = term["text"].lower()
            if term["is_glob"]:
                pattern = _glob_span_pattern(body)
                if not pattern:
                    # A degenerate all-wildcard glob matches the whole line.
                    finders.append(
                        lambda line: [(0, len(line))]
                    )
                else:
                    rx = re.compile(pattern, re.IGNORECASE)
                    finders.append(
                        lambda line, _rx=rx: (
                            [m.span()] if (m := _rx.search(line)) else []
                        )
                    )
            else:
                rx = re.compile(re.escape(body), re.IGNORECASE)
                finders.append(
                    lambda line, _rx=rx: [
                        m.span() for m in _rx.finditer(line)
                    ]
                )

    def _smart_line(line: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for find in finders:
            spans.extend(find(line))
        spans.sort()
        return spans

    return _smart_line


def build_search_predicate(
    query: str,
    query_mode: str = "smart",
    *,
    match_scope: str = "name_and_note",
) -> Callable[[dict[str, Any]], bool]:
    """Build a Lightning-Rod free-text search membership predicate.

    The returned predicate is a pure ``(node) -> bool`` over a cache node dict,
    matching against the node's search target text (see ``match_scope``).
    Suitable directly as the ``member_predicate`` of
    :func:`f8_slice_policy.build_selected_subtree_from_cache`.

    ``match_scope`` is the SELECTION-stage scope knob (keyword-only), threaded
    straight into :func:`_node_target_text`:
      * ``"name_and_note"`` (default -- prior behavior) matches ``name`` +
        ``"\\n"`` + ``note``, so a hit in EITHER field admits the node.
      * ``"name"`` matches ONLY the node's NAME (title), never the note body.
    Because both query modes below match against ``_node_target_text``, the
    scope is honored for ``smart`` AND ``regex`` alike from this ONE param.

    query_mode:
      * ``"smart"`` (default) -- ``shlex``-tokenized boolean substring matching.
        Grammar (this is the AGENT-FACING interface -- examples are normative):
          - ``monitor dell``            -> must contain BOTH ``monitor`` AND
            ``dell`` (bare terms are ANDed).
          - ``invoice OR receipt``      -> contains ``invoice`` OR ``receipt``.
          - ``report -draft``           -> contains ``report`` AND NOT ``draft``
            (leading ``-`` or ``!`` negates a term).
          - ``"27 inch monitor"``       -> contains that exact phrase (quotes).
          - ``inv*2026``                -> glob (``*`` / ``?`` wildcard, matched
            anywhere in the text).
          - ``invoice OR receipt -draft`` -> ``(invoice) OR (receipt AND NOT
            draft)`` -- ``OR`` separates AND-groups; negation binds within its
            group.
        All matching is case-insensitive.
      * ``"regex"`` -- the ENTIRE ``query`` is a Python regular expression,
        compiled once with ``re.IGNORECASE | re.DOTALL`` and applied via
        ``re.search`` to the target text. Example:
          - ``query_mode="regex", query=r"inv\\d{4}"``.

    Raises :class:`LightningRodQueryError` on a malformed or empty query (an
    invalid regex, unbalanced quotes, or a query with no usable term), so the
    caller can surface a loud, actionable error instead of silently returning an
    empty slice.
    """
    mode = (query_mode or "smart").strip().lower()
    q = query if isinstance(query, str) else ""
    if not q.strip():
        raise LightningRodQueryError("Search query must be a non-empty string.")

    if mode == "regex":
        try:
            rx = re.compile(q, re.IGNORECASE | re.DOTALL)
        except re.error as e:
            raise LightningRodQueryError(
                f"Invalid regular expression: {e}"
            ) from e

        def _regex_pred(node: dict[str, Any]) -> bool:
            return rx.search(_node_target_text(node, match_scope)) is not None

        return _regex_pred

    if mode != "smart":
        raise LightningRodQueryError(
            f"Unknown query_mode {query_mode!r}; use 'smart' or 'regex'."
        )

    or_groups = _smart_terms(q)

    def _smart_pred(node: dict[str, Any]) -> bool:
        text_lower = _node_target_text(node, match_scope).lower()
        # OR across groups; AND within a group.
        for group in or_groups:
            if group and _smart_group_matches(group, text_lower):
                return True
        return False

    return _smart_pred
