"""beacon_mutation_contract.py -- pure, stdlib-only contracts for the beacon mutation seam.

THE DEPENDENCE-GRAPH INVARIANT (load-bearing, SPEC v5 §2):
nexus_map_codebase.py imports higher workflowy_mcp layers and must be lazily
imported from foundation code (the documented module-load cycle). Therefore
THIS module must NEVER import nexus_map_codebase, and nexus_map_codebase must
NEVER import the workflowy_mcp package from these types. Both sides import
THIS ONE tiny stdlib-only contracts module. There is deliberately NO edge
between the mutation-spec module (workflowy_mcp.lightning_rod_mutation_spec)
and nexus_map_codebase.

PLACEMENT (forced, not a choice): this module lives at the REPO ROOT -- it
cannot live inside src/workflowy_mcp/ because nexus_map_codebase must never
import the workflowy_mcp package, and both consumers must be able to import
it. As a repo-root module it is one of the modules NOT covered by the
editable install's live-source link: after editing it, re-run
``pip install -e .`` BEFORE every pytest run (the SPEC v5 §12 stale-venv
rule), and keep it listed in pyproject.toml's py-modules packaging config
(force-include) or packaged (uvx) installs will ImportError while the
editable venv passes.

Contents (all pure; no I/O):
  * ``NODE_DECORATION_SYMBOLS`` + ``strip_leading_decoration_symbols`` -- THE
    SINGLE HOME for the decoration-strip machinery (moved out of
    nexus_map_codebase, which imports + re-exports both names). The symbol
    set is NEVER duplicated in two modules (drift).
  * ``validate_beacon_comment_text`` -- the ONE beacon-comment validator
    (SPEC v5 §3).
  * ``BeaconCommentIntent`` / ``BeaconWriteIntent`` -- frozen comment-intent
    types passed AS COMMIT OPERANDS through the commit seam.
  * ``CommentResolution`` + ``resolve_beacon_comment`` -- the ONE comment
    resolver (SPEC v5 §5 step 7 ordered contract; all comparisons in
    decoded semantic form without punctuation loss).
  * ``BEACON_FIELD_KEYS`` + ``parse_beacon_field_lines`` -- THE ONE beacon
    field parser (key/value logic) shared by every per-language block parser
    in nexus_map_codebase and beacon_obtain_code_snippet; supports multi-line
    (wrapped) field values. Opaque key-led assignments bound preceding fields;
    ``beacon_field_unknown_keys`` reports their raw spelling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Optional


# ---------------------------------------------------------------------------
# Decoration-strip machinery -- THE SINGLE HOME (moved from nexus_map_codebase)
# ---------------------------------------------------------------------------
#
# The trident plus every AST/file-type emoji Cartographer applies
# DETERMINISTICALLY when generating a node NAME. These must never be embedded
# in a beacon's role= / comment= field: when they are, the name builder
# ("f'{TRIDENT} {display_role}'" or "f'{display_role}'") would re-emit them,
# compounding e.g. into "🔱 🔱 🔱 name" on successive refreshes.
#
# ⚠ SINGLE-HOME RULE (SPEC v5 §2): this set lives HERE and ONLY here.
# nexus_map_codebase imports + re-exports it so every existing call site is
# untouched; the EMOJI_* / TRIDENT constants there remain for name-building,
# and their VALUES must stay in lockstep with this set. NEVER duplicate the
# symbol set in two modules (drift).
NODE_DECORATION_SYMBOLS = frozenset({
    "🔱",   # TRIDENT
    "📂",   # EMOJI_FOLDER
    "📄",   # EMOJI_FILE
    "🐍",   # EMOJI_PYTHON
    "📘",   # EMOJI_MARKDOWN
    "🗄️",  # EMOJI_SQL (base glyph + U+FE0F variation selector)
    "🐚",   # EMOJI_SHELL
    "📦",   # EMOJI_CLASS
    "ƒ",    # EMOJI_FUNC
    "⚡",   # EMOJI_ASYNC
    "💎",   # EMOJI_CONST
    "🟨",   # EMOJI_JS
    "🟦",   # EMOJI_TS
    "🟩",   # EMOJI_VUE
})


# @beacon[
#   id=auto-beacon@strip_leading_decoration_symbols-strip,
#   slice_labels=nexus--tags,carto-fwd-source,
#   kind=ast,
#   comment=Strip any leading run of NODE_DECORATION_SYMBOLS (and surrounding whitespace) from a beacon role/comment value before it is used to build a Workflowy node NAME. Prevents trident/AST-emoji from being baked into role=/comment= and then compounding on each deterministic F12 refresh. Whitespace-and-symbol-only input collapses to "". SINGLE HOME: moved from nexus_map_codebase to beacon_mutation_contract (SPEC v5 §2); nexus_map_codebase imports + re-exports it.,
# ]
def strip_leading_decoration_symbols(text_value):
    """Remove a leading run of Cartographer decoration symbols from a string.

    Cartographer deterministically prepends the trident (and AST-type emoji)
    to generated node NAMEs. Agents sometimes accidentally include those same
    symbols inside a beacon's ``role=`` / ``comment=`` field; if left in, the
    name builder re-emits them and they compound (e.g. ``🔱 🔱
    🔱 name``) on every refresh.

    This strips any leading sequence of whitespace and/or symbols drawn from
    ``NODE_DECORATION_SYMBOLS`` from the front of ``text_value``. Symbols that
    appear later in the string (legitimate content) are left untouched. A value
    consisting only of decoration symbols / whitespace collapses to ``""``.
    """
    if not text_value or not isinstance(text_value, str):
        return text_value or ""
    s = text_value
    # Prefix-based stripping (NOT char-by-char): some decoration symbols are
    # multi-codepoint (e.g. EMOJI_SQL "🗄️" is a base glyph plus a
    # U+FE0F variation selector). A char-by-char "ch in NODE_DECORATION_SYMBOLS"
    # test can never match a multi-codepoint symbol, so we match whole symbols
    # by prefix, longest-first, looping until no leading decoration remains.
    symbols_longest_first = sorted(NODE_DECORATION_SYMBOLS, key=len, reverse=True)
    while True:
        s = s.lstrip()
        # Drop a stray leading variation selector / ZWJ left behind by an
        # already-stripped base glyph (defensive; keeps output clean).
        while s and s[0] in ("️", "‍"):
            s = s[1:]
        matched = False
        for sym in symbols_longest_first:
            if sym and s.startswith(sym):
                s = s[len(sym):]
                matched = True
                break
        if not matched:
            break
    return s.strip()


# ---------------------------------------------------------------------------
# §3 Comment validation -- THE ONE validator
# ---------------------------------------------------------------------------

# Block/beacon delimiters can never appear inside a one-line beacon comment.
_FORBIDDEN_COMMENT_TOKENS = ("/*", "*/", "<!--", "-->", "@beacon[", "BEACON (")

# FIXPOINT RULE B (§3 transport entities): the Markdown beacon builder applies
# decode_workflowy_transport_entities_for_markdown_source on every subsequent
# F12 write, so an entity-containing comment would drift on cycle 2. Reject
# the named HTML entities (lt, gt, amp, quot) AND both numeric character
# reference forms (the &#-plus-digits decimal form AND the &#x-hex form) --
# one regex; total determinism; trivial expressiveness loss.
_TRANSPORT_ENTITY_RE = re.compile(
    r"&(?:lt|gt|amp|quot);|&#\d+;|&#[xX][0-9a-fA-F]+;",
    re.IGNORECASE,
)


# @beacon[
#   id=auto-beacon@validate_beacon_comment_text-val1,
#   slice_labels=nexus--tags,beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=One ingress comment validator with unchanged control/delimiter/entity limits and decoration normalization; decoded note comments use it before writing. Accepted commas/brackets and whole quote pairs are preserved by renderer encoding rather than destructively normalized for comparison.,
# ]
def validate_beacon_comment_text(value: Any) -> Optional[str]:
    """Validate and normalize a caller-supplied beacon comment (THE ONE validator).

    The on-disk beacon grammars write this onto a single metadata line inside
    Python/JS block comments, HTML comments, SQL comments, shell/YAML
    comments, etc. Deliberately strict: one line, no control characters, no
    block/beacon delimiters, no Workflowy transport entities.

    REJECTIONS (SPEC v5 §3):
      * LF, CR, NUL -- plus ALL other C0 controls, DEL, C1 controls,
        U+2028 (LINE SEPARATOR), U+2029 (PARAGRAPH SEPARATOR);
      * ``/*``, ``*/``, ``<!--``, ``-->``, ``@beacon[``, ``BEACON (``;
      * transport-entity patterns (fixpoint rule B): the named entities
        ``&lt; &gt; &amp; &quot;`` and BOTH numeric reference forms
        (``&#123;`` decimal, ``&#x1F;`` hex) -- anything
        ``decode_workflowy_transport_entities_for_markdown_source``-shaped
        would drift on the next F12 write cycle.

    FIXPOINT RULE A (decoration): ingress applies the SAME leading-decoration
    normalization that F12 re-reads apply (the single-home
    ``strip_leading_decoration_symbols``), so the accepted value is a
    fixpoint of the round-trip. Explicit API comments then SKIP the strip and
    the transport-entity decode at write time (those remain for note-derived
    comments only).

    COMMAS / '=' / ']' are SAFE AND NEVER REJECTED, internal AND trailing.
    The shared renderer protects punctuation and whole quote pairs with one
    enclosing quote pair; parsed semantic text is compared without loss.

    Empty/whitespace normalizes to ``None``. Raises ``ValueError`` on any
    rejection.
    """
    if value is None:
        return None
    comment = str(value).strip()
    if not comment:
        return None
    # Fixpoint rule A: leading-decoration normalization at ingress.
    comment = strip_leading_decoration_symbols(comment)
    if not comment:
        return None
    for ch in comment:
        code = ord(ch)
        if (
            code < 0x20  # C0 controls (incl. \n, \r, \x00)
            or code == 0x7F  # DEL
            or 0x80 <= code <= 0x9F  # C1 controls
            or code in (0x2028, 0x2029)  # LINE / PARAGRAPH SEPARATOR
        ):
            raise ValueError(
                f"unsafe beacon comment {comment!r}: contains forbidden "
                f"control character U+{code:04X}"
            )
    for token in _FORBIDDEN_COMMENT_TOKENS:
        if token in comment:
            raise ValueError(
                f"unsafe beacon comment {comment!r}: contains forbidden token {token!r}"
            )
    if _TRANSPORT_ENTITY_RE.search(comment):
        raise ValueError(
            f"unsafe beacon comment {comment!r}: contains a Workflowy "
            "transport-entity pattern (would drift on the next F12 write cycle)"
        )
    return comment


# @beacon[
#   id=auto-beacon@_parser_normalize_comment_value-l0ro,
#   role=_parser_normalize_comment_value,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Compare already decoded semantic comments without stripping punctuation or whole quote pairs; historical unquoted parser loss is not reconstructible.,
# ]
def _parser_normalize_comment_value(value: Optional[str]) -> str:
    """Normalize semantic input, not source syntax; never guess old lost text."""
    return value or ""


# ---------------------------------------------------------------------------
# THE ONE beacon field parser (key/value logic shared by every block parser)
# ---------------------------------------------------------------------------

# Every field name any beacon block parser reads. A line starts a NEW field
# only when it begins with one of these names followed by '=' (optional
# spaces before the '='); every other line CONTINUES the previous field.
BEACON_FIELD_KEYS = frozenset({
    "id",
    "role",
    "slice_labels",
    "kind",
    "start_snippet",
    "end_snippet",
    "comment",
    "comments",
    "show_span",
})

_BEACON_FIELD_START_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")


# @beacon[
#   id=auto-beacon@parse_beacon_field_lines-mlbf,
#   role=parse_beacon_field_lines,
#   slice_labels=beacon-writeback,carto-fwd-source,
#   kind=ast,
#   comment=One field authority: known assignments start fields and opaque unknown assignments end the preceding value; other text continues it. Located occurrences retain raw evidence and duplicate/inline ambiguity; reads remain last-wins with exact quote-aware syntax normalization.,
# ]
def parse_beacon_field_lines(
    texts: Any, *, occurrences: list | None = None, diagnostics: list | None = None,
) -> dict[str, str]:
    """Read fields, optionally retaining every located occurrence and raw line.

    Key-led lines bound fields, including opaque unknown keys; other lines
    retain the historical continuation/last-wins rules.
    The structural scanner supplies dictionaries with text, line, byte_range
    and inline: only opener-line fields opt into comma-separated key splitting.
    Ambiguous unquoted inline comments remain whole, with canonical alternatives
    in diagnostics, rather than silently inventing subsequent fields.
    """
    fields: dict[str, str] = {}
    found: list[dict] = []
    current = None

    def flush():
        if current is None:
            return
        val = " ".join(current["parts"]).strip()
        val = normalize_beacon_id(val)
        current["value"] = val
        fields[current["key"]] = val
        found.append(current.copy())

    for index, raw in enumerate(texts or ()):
        item = raw if isinstance(raw, dict) else {"text": raw, "line": index + 1}
        text = (item.get("text") or "").strip()
        chunks = _beacon_inline_field_chunks(text) if item.get("inline") else [(0, text)]
        for column, text in chunks:
            if not text:
                continue
            m = _BEACON_FIELD_START_RE.match(text)
            if m and m.group(1) not in BEACON_FIELD_KEYS and text[m.end():].lstrip().startswith("="):
                m = None  # prose describing equality is still continuation
            if m and m.group(1) in BEACON_FIELD_KEYS:
                flush()
                located = dict(item, text=text, column=item.get("column", 0) + column)
                if item.get("byte_range") is not None:
                    origin = item["byte_range"][0]
                    prefix_size = len((item.get("text") or "")[:column].encode("utf-8", errors="surrogatepass"))
                    located["byte_range"] = (origin + prefix_size, origin + prefix_size + len(text.encode("utf-8", errors="surrogatepass")))
                current = {
                    "key": m.group(1), "line": item.get("line", index + 1),
                    "column": item.get("column", 0) + column,
                    "byte_range": located.get("byte_range"), "parts": [text[m.end():].strip()],
                    "raw": [text], "locations": [located],
                }
                if item.get("inline") and current["key"] in {"comment", "comments"}:
                    alternatives = _beacon_inline_field_chunks(text, split_comment=True)
                    if len(alternatives) > 1 and diagnostics is not None:
                        diagnostics.append({"code": "inline_field_ambiguity", "line": item.get("line"),
                                            "raw": text, "alternatives": [v for _, v in alternatives],
                                            "canonical_multiline": [[text], [v for _, v in alternatives]]})
            else:
                if diagnostics is not None:
                    diagnostics.append({"code": "unknown_field" if m else "field_continuation",
                                        "line": item.get("line"), "raw": text,
                                        "key": m.group(1) if m else None})
                if m:
                    # Unknown assignments are opaque fields, not continuation
                    # text belonging to the previous id/comment/label value.
                    flush()
                    current = None
                elif current is not None:
                    current["parts"].append(text)
                    current["raw"].append(text)
                    current["locations"].append(dict(item, text=text))
    flush()
    by_key: dict[str, list] = {}
    for occurrence in found:
        by_key.setdefault(occurrence["key"], []).append(occurrence)
    if diagnostics is not None:
        for key, values in by_key.items():
            if len(values) > 1:
                diagnostics.append({"code": "duplicate_field", "key": key,
                                    "conflicting": len({v["value"] for v in values}) > 1,
                                    "occurrences": values})
    if occurrences is not None:
        occurrences.extend(found)
    return fields


# @beacon[
#   id=beacon-fields@normalize_beacon_id,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=One exact id/value normalization: remove trailing syntax separators outside a matching quote pair then unwrap that pair; case and quoted interior bytes stay intact.,
# ]
def normalize_beacon_id(value: Any) -> str:
    """Trim syntax, not identity; quoted trailing commas/brackets remain data."""
    text = str(value or "").strip()
    while text and text[-1] in ",]":
        # A closing matching quote protects all of its interior separators.
        text = text[:-1].rstrip()
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        return text[1:-1]
    return text


# @beacon[
#   id=beacon-fields@parse_beacon_note_metadata,
#   slice_labels=beacon-writeback,carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Read the positional header run only: note start or the AST_QUALNAME/MD_PATH prefix; retain adjacent records and skip MD context through its separator but never restart in body text.,
# ]
def parse_beacon_note_metadata(note: Any) -> list[dict]:
    """Read separate records only in the note's positional metadata run.

    A run starts at line one, immediately after AST_QUALNAME, or at the first
    nonblank line after the MD_PATH heading separator. Blank-separated headers
    continue it; Markdown records also own their closing separator and optional
    CONTEXT COMMENTS (MD) section through that separator. Any other boundary
    ends the run permanently, including the separator before an AST docstring.
    Notes contain semantic values, not source field syntax: quotes, trailing
    commas and brackets are content here. Decode transport entities once only.
    ``occurrences`` retains repeated fields for field-local ambiguity answers.
    """
    records, current = [], None
    decode = {"&lt;": "<", "&gt;": ">", "&amp;": "&", "&quot;": '"',
              "&#34;": '"', "&#x22;": '"', "&#X22;": '"'}
    lines = str(note or "").splitlines()
    index = 0
    if lines and lines[0].strip().startswith("AST_QUALNAME:"):
        index = 1
    elif lines and lines[0].strip() == "MD_PATH:":
        index = next((i + 1 for i, row in enumerate(lines) if row.strip() == "---"), len(lines))
        while index < len(lines) and not lines[index].strip():
            index += 1
    while index < len(lines) and lines[index].strip().startswith("BEACON ("):
        text = lines[index].strip()
        current = {"header": text, "line": index + 1, "occurrences": []}
        records.append(current)
        markdown = text.startswith("BEACON (MD ")
        index += 1
        while index < len(lines):
            text = lines[index].strip()
            if not text or text == "---" or text.startswith("BEACON ("):
                break
            number = index + 1
            index += 1
            if ":" not in text:
                continue
            key, value = text.split(":", 1)
            key = key.strip().lower()
            if key not in {"id", "kind", "role", "show_span", "comment", "comments", "slice_labels"}:
                continue
            key = "comment" if key == "comments" else key
            value = re.sub(r"&(?:lt|gt|amp|quot|#34|#[xX]22);", lambda m: decode[m.group()], value.strip())
            current["occurrences"].append({"key": key, "value": value, "line": number})
            current[key] = value
        while index < len(lines) and not lines[index].strip():
            index += 1
        if markdown and index < len(lines) and lines[index].strip() == "CONTEXT COMMENTS (MD):":
            # Example headers inside this section are context, not records.
            while index < len(lines) and lines[index].strip() != "---":
                index += 1
        if markdown and index < len(lines) and lines[index].strip() == "---":
            index += 1
            while index < len(lines) and not lines[index].strip():
                index += 1
    return records


def _beacon_inline_field_chunks(text: str, split_comment: bool = False) -> list[tuple[int, str]]:
    """Split only unquoted commas followed by a known key, never label commas."""
    starts = [0]
    quote_until = -1
    for i, ch in enumerate(text):
        if i <= quote_until:
            continue
        if ch in "\"'":
            end = _beacon_quote_end(text, i)
            if end is not None:
                quote_until = end
        elif ch == ",":
            m = _BEACON_FIELD_START_RE.match(text[i + 1:].lstrip())
            if m and m.group(1) in BEACON_FIELD_KEYS:
                prior = text[starts[-1]:i].lstrip()
                pm = _BEACON_FIELD_START_RE.match(prior)
                if not split_comment and pm and pm.group(1) in {"comment", "comments"}:
                    # A quoted value has an explicit end; an unquoted comment
                    # could itself be describing a field assignment.
                    value = prior[pm.end():].strip()
                    if not (len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]):
                        continue
                starts.append(i + 1)
    chunks = []
    for k, start in enumerate(starts):
        raw = text[start:(starts[k + 1] - 1 if k + 1 < len(starts) else len(text))]
        chunks.append((start + len(raw) - len(raw.lstrip()), raw.strip()))
    return chunks


def _beacon_quote_end(text: str, start: int) -> int | None:
    """Matching quotes protect delimiters; word apostrophes are punctuation."""
    quote = text[start]
    if quote == "'" and start > 0 and start + 1 < len(text) and text[start - 1].isalnum() and text[start + 1].isalnum():
        return None
    escaped = False
    for index in range(start + 1, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            if quote == "'" and index + 1 < len(text) and text[index - 1].isalnum() and text[index + 1].isalnum():
                continue
            return index
    return None


# @beacon[
#   id=auto-beacon@beacon_field_unknown_keys-ufk1,
#   slice_labels=beacon-writeback,carto-fwd-source,
#   kind=ast,
#   comment=The field parser's silent decision, said out loud: the keys of lines that look like key=value but name no BEACON_FIELD_KEYS key, which parse_beacon_field_lines glues onto the previous field (or ignores before the first). Same start regex, same key set; pure. Called by the block parsers only when a diagnostics sink is passed.,
# ]
def beacon_field_unknown_keys(texts: Any) -> list[str]:
    """Return, in input order, the key of every text that starts like a field
    (``_BEACON_FIELD_START_RE``) but whose key is not in
    ``BEACON_FIELD_KEYS``: the lines ``parse_beacon_field_lines`` treats as a
    continuation of the previous field, or ignores before the first field."""
    unknown: list[str] = []
    for raw in texts or ():
        text = (raw or "").strip()
        if not text:
            continue
        m = _BEACON_FIELD_START_RE.match(text)
        if m and m.group(1) not in BEACON_FIELD_KEYS:
            unknown.append(m.group(1))
    return unknown


# ---------------------------------------------------------------------------
# Frozen comment-intent types (commit operands -- no synthetic note text)
# ---------------------------------------------------------------------------


# THE ONE comment_mode vocabulary (2R4E1P): shared by the MCP mutation
# normalizer, the intent classmethods and the resolver, so the three can
# never drift. 'fill' is the default (today's fill-if-empty semantics, with
# conflicts KEPT as warnings); 'replace' overwrites; 'append' extends.
COMMENT_MUTATION_MODES: tuple = ("fill", "replace", "append")


def _kept_conflict_warning(existing: Optional[str], requested: str, source: str) -> str:
    """The ONE kept-conflict warning text (both texts, plus the remedy)."""
    return (
        f"existing {source} comment {existing!r} kept; requested comment "
        f"{requested!r} was NOT applied (comment_mode 'fill' never overwrites). "
        "Re-run with comment_mode 'replace' to overwrite, or 'append' to extend."
    )


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@BeaconCommentIntent-le7f,
#   role=BeaconCommentIntent,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Typed API comment intent: preserve existing disk text (note fallback only for creates); fill empty disk with requested text and keep conflicts as warnings; replace and append are explicit changes independent of label work.,
# ]
class BeaconCommentIntent:
    """The typed comment intent carried by a mutation operation.

    ``mode='preserve'``       -- no comment requested: keep the pre-existing
                                 comment (disk authoritative, note fallback
                                 only for creates); never rejects; a label-only
                                 mutation never erases a comment.
    ``mode='fill_if_empty'``  -- a comment was requested: fill ONLY when the
                                 disk field is empty; a DIFFERENT existing
                                 comment is KEPT (kept_existing_conflict
                                 resolution + warning), never an error and
                                 never an overwrite (2R4E1P).
    ``mode='replace'``        -- explicit opt-in overwrite: write the
                                 requested comment even when one exists
                                 (equal text stays idempotent).
    ``mode='append'``         -- keep the existing comment and extend it
                                 with the requested text (empty disk fills;
                                 already-appended text stays idempotent).
    """

    mode: Literal["preserve", "fill_if_empty", "replace", "append"]
    value: Optional[str] = None

    @classmethod
    def preserve(cls) -> "BeaconCommentIntent":
        return cls(mode="preserve", value=None)

    @classmethod
    def fill_if_empty(cls, value: Any) -> "BeaconCommentIntent":
        validated = validate_beacon_comment_text(value)
        if validated is None:
            raise ValueError("fill_if_empty requires a non-empty comment value")
        return cls(mode="fill_if_empty", value=validated)

    @classmethod
    def replace(cls, value: Any) -> "BeaconCommentIntent":
        validated = validate_beacon_comment_text(value)
        if validated is None:
            raise ValueError("replace requires a non-empty comment value")
        return cls(mode="replace", value=validated)

    @classmethod
    def append(cls, value: Any) -> "BeaconCommentIntent":
        validated = validate_beacon_comment_text(value)
        if validated is None:
            raise ValueError("append requires a non-empty comment value")
        return cls(mode="append", value=validated)

    def to_dict(self) -> dict:
        return {"mode": self.mode, "value": self.value}


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@BeaconWriteIntent-w15i,
#   role=BeaconWriteIntent,
#   slice_labels=beacon-writeback,
#   kind=ast,
# ]
class BeaconWriteIntent:
    """The authoritative write intent populated at plan construction.

    Passed AS A COMMIT OPERAND through the commit seam -- commit NEVER
    reverse-engineers intent from derived outcome fields. ``source`` records
    which ingress produced it (extensible; 'legacy_f12' marks the legacy
    F12-per-node path, which expresses its comment-from-note behavior as
    ``write_intent=None`` rather than as an intent).
    """

    comment: BeaconCommentIntent
    source: Literal["legacy_f12", "lightning_rod"]

    def to_dict(self) -> dict:
        return {"comment": self.comment.to_dict(), "source": self.source}


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@CommentResolution-nr8j,
#   role=CommentResolution,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Semantic resolved text and explicit API or note-authoritative outcome; carries kept-conflict warnings, old/new changes and field-local validation errors without blocking independent labels.,
# ]
class CommentResolution:
    """Semantic comment result with field-local errors and old/new conflicts.

    ``outcome`` is one of 'preserve' | 'fill' | 'idempotent' |
    'replace' | 'append' | 'kept_existing_conflict' | 'note_authoritative' |
    'invalid_note_comment'. ``resolved`` is the
    value to write (decoded semantic text; ``None`` when there is
    nothing to write). ``existing_comment`` carries the pre-existing
    comment a resolution kept. ``warning`` is the agent-facing message for
    a 'kept_existing_conflict' outcome (both texts: existing vs requested)
    -- a fill-mode conflict is a WARNING, never an error (2R4E1P);
    ``conflict_detail`` carries the same text for continuity.
    ``conflict`` records a note-authoritative change as field/old/new;
    ``field_error`` records a rejected note comment, whose disk text stays
    while independent label changes remain applicable.
    """

    outcome: str
    resolved: Optional[str] = None
    conflict_detail: Optional[str] = None
    existing_comment: Optional[str] = None
    warning: Optional[str] = None
    conflict: Optional[dict] = None
    field_error: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "resolved": self.resolved,
            "conflict_detail": self.conflict_detail,
            "existing_comment": self.existing_comment,
            "warning": self.warning,
            "conflict": self.conflict,
            "field_error": self.field_error,
        }


# @beacon[
#   id=auto-beacon@resolve_beacon_comment-res1,
#   slice_labels=nexus--tags,beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=One semantic comment policy: API existing-block preserve is disk-only and fill uses requested text; creates may seed scoped note text; explicit note-authoritative intent reports old/new conflicts and invalid note comments leave disk text while independent labels proceed.,
# ]
def resolve_beacon_comment(
    *,
    intent: BeaconCommentIntent,
    note_comment: Optional[str],
    disk_comment: Optional[str],
    beacon_exists_on_disk: bool,
    note_authoritative: bool = False,
    note_comment_present: Optional[bool] = None,
) -> CommentResolution:
    """Resolve decoded semantic text, without source-syntax normalization.

    Existing API edits never consult stale note text. Note-authoritative
    absolute/rehydration intents distinguish absence from explicit emptiness.
    Validate note-derived input only when used, after the header reader's
    transport decode. Errors concern the comment, not independent labels.
    """
    norm_requested = (
        _parser_normalize_comment_value(intent.value)
        if intent.mode in ("fill_if_empty", "replace", "append")
        else ""
    )
    norm_note = _parser_normalize_comment_value(note_comment)
    norm_disk = (
        _parser_normalize_comment_value(disk_comment) if beacon_exists_on_disk else ""
    )

    present = note_comment is not None if note_comment_present is None else note_comment_present
    if (note_authoritative and present) or (not beacon_exists_on_disk and intent.mode == "preserve" and present):
        try:
            value = validate_beacon_comment_text(norm_note)
        except ValueError as error:
            return CommentResolution(outcome="invalid_note_comment", resolved=norm_disk or None,
                                     field_error={"field": "comment", "error_kind": "invalid_comment",
                                                  "error": str(error), "requested": note_comment})
        conflict = ({"field": "comment", "old": norm_disk or None, "new": value}
                    if beacon_exists_on_disk and (norm_disk or None) != value else None)
        return CommentResolution(outcome="note_authoritative" if conflict else "preserve",
                                 resolved=value, conflict=conflict)

    if intent.mode == "preserve":
        resolved = norm_disk or None
        return CommentResolution(outcome="preserve", resolved=resolved)

    # fill_if_empty: conflicts are KEPT with a warning, never errors (2R4E1P).
    if intent.mode == "fill_if_empty":
        if norm_disk and norm_disk == norm_requested:
            return CommentResolution(outcome="idempotent", resolved=norm_disk)
        if norm_disk and norm_disk != norm_requested:
            warning = _kept_conflict_warning(norm_disk, norm_requested, "on-disk")
            return CommentResolution(
                outcome="kept_existing_conflict",
                resolved=norm_disk,
                conflict_detail=warning,
                existing_comment=norm_disk,
                warning=warning,
            )
        # Empty disk -> requested text, never stale cached note text.
        return CommentResolution(outcome="fill", resolved=norm_requested or None)

    # replace: explicit opt-in overwrite.
    if intent.mode == "replace":
        if norm_disk and norm_disk == norm_requested:
            return CommentResolution(outcome="idempotent", resolved=norm_disk)
        return CommentResolution(outcome="replace", resolved=norm_requested or None)

    # append: keep the existing comment and extend it.
    if intent.mode == "append":
        if not norm_disk:
            return CommentResolution(outcome="fill", resolved=norm_requested or None)
        if norm_disk == norm_requested or norm_disk.endswith(" " + norm_requested):
            return CommentResolution(outcome="idempotent", resolved=norm_disk)
        return CommentResolution(
            outcome="append",
            resolved=f"{norm_disk} {norm_requested}",
        )

    # Unreachable today (the mode Literal is closed); fail closed loudly.
    raise ValueError(f"unknown comment intent mode {intent.mode!r}")
