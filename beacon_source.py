"""Immutable source observations and physical coordinates for beacon consumers.

The original bytes are the authority. Decoding never translates newlines;
only a leading UTF-8 BOM is absent from the parse view. Physical lines are
CRLF, CR or LF, not the extra Unicode separators recognized by splitlines.
This module uses only the standard library and has no mutation machinery.
"""
from __future__ import annotations

from dataclasses import dataclass
import bisect
import collections
import copy
import hashlib
import io
import os
import re
import threading
import tokenize

from beacon_mutation_contract import parse_beacon_field_lines, _beacon_quote_end


# @beacon[
#   id=beacon-source@SourceSnapshot,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=One immutable observation: original UTF-8 bytes/hash and BOM-free physical parse coordinates; translates normalized lexer byte indexes back to disk offsets without IO or mutation.,
# ]
@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """One source version; public line numbers are 1-based.

    ``physical_lines`` excludes terminators; ``line_endings`` retains each
    exact terminator (or an empty string at an unterminated EOF). A trailing
    newline does not invent another physical line. ``byte_offsets`` contains
    one original-byte start per physical line, plus the EOF offset. Its first
    element is 3 with a BOM, otherwise 0, including for an empty parse view.

    Consumers needing normalized newlines use ``normalized_text`` while
    retaining original-byte coordinates for every eventual edit. Character
    columns use ``byte_offset``; normalized UTF-8 parser boundaries use
    ``normalized_byte_offset`` rather than treating parse indexes as disk bytes.
    """

    canonical_path: str
    raw_bytes: bytes
    sha256: str
    had_bom: bool
    parse_text: str
    physical_lines: tuple[str, ...]
    line_endings: tuple[str, ...]
    byte_offsets: tuple[int, ...]

    @property
    def normalized_text(self) -> str:
        """The same parse view with CRLF/CR represented as LF, for lexers."""
        return self.parse_text.replace("\r\n", "\n").replace("\r", "\n")

    def byte_offset(self, line: int, column: int = 0) -> int:
        """Translate a valid 1-based physical line / character column to bytes.

        ``column`` is zero-based within the line, not a UTF-8 byte column.
        The end-of-content column addresses the start of that line's ending.
        """
        return self.byte_offsets[line - 1] + len(self.physical_lines[line - 1][:column].encode("utf-8"))

    def normalized_byte_offset(self, offset: int) -> int:
        """Translate a normalized UTF-8 boundary (e.g. tree-sitter) to disk.

        Normalization removes only the file BOM and replaces each physical
        ending with LF. Interior Unicode separators and UTF-8 columns retain
        their byte widths. EOF maps to the original EOF, including its ending.
        """
        remaining = offset
        for index, (line, ending) in enumerate(zip(self.physical_lines, self.line_endings)):
            width = len(line.encode("utf-8"))
            if remaining <= width:
                return self.byte_offsets[index] + remaining
            remaining -= width + bool(ending)
        return len(self.raw_bytes)


# @beacon[
#   id=beacon-source@source_snapshot_from_bytes,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Builds a source observation entirely in memory from one immutable byte string: strict UTF-8, one leading BOM removed only from the parse view, the same physical CRLF/CR/LF rule used by extracted text views, and original-byte offsets. Decode errors stay typed.,
# ]
def source_snapshot_from_bytes(raw_bytes: bytes, canonical_path: str = "") -> SourceSnapshot:
    """Hash and decode the same bytes; retain their exact line endings and EOF.

    ``canonical_path`` is an already-resolved identity supplied by the caller;
    the pure constructor performs no filesystem lookup. Invalid UTF-8 raises
    ``UnicodeDecodeError`` rather than manufacturing an empty observation.
    Extracted text views share its physical-line splitter, not a second rule.
    """
    raw = bytes(raw_bytes)
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    bom_size = 3 if had_bom else 0
    text = raw[bom_size:].decode("utf-8")
    lines, endings = _physical_source_parts(text)
    offsets = [bom_size]
    for line, ending in zip(lines, endings):
        offsets.append(offsets[-1] + len((line + ending).encode("utf-8")))
    return SourceSnapshot(
        canonical_path=str(canonical_path), raw_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(), had_bom=had_bom,
        parse_text=text, physical_lines=tuple(lines),
        line_endings=tuple(endings), byte_offsets=tuple(offsets),
    )


# @beacon[
#   id=beacon-source@read_source_snapshot,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Reads a canonical file once in binary mode and delegates all hashing/decoding/coordinates to the pure snapshot constructor; read/decode errors remain typed and no file bytes or mtime are changed.,
# ]
def read_source_snapshot(path: str | os.PathLike[str]) -> SourceSnapshot:
    """Observe a file once; propagate ordinary typed IO and decoding failures."""
    canonical_path = os.path.realpath(os.fspath(path))
    with open(canonical_path, "rb") as stream:
        raw = stream.read()
    return source_snapshot_from_bytes(raw, canonical_path=canonical_path)


def _physical_source_parts(text: str) -> tuple[list[str], list[str]]:
    """The physical-line rule, also used for extracted in-memory source views."""
    lines: list[str] = []
    endings: list[str] = []
    cursor = 0
    for match in re.finditer(r"\r\n|\r|\n", text):
        lines.append(text[cursor:match.start()])
        endings.append(match.group())
        cursor = match.end()
    if cursor < len(text):
        lines.append(text[cursor:])
        endings.append("")
    return lines, endings


def physical_source_lines(text: str) -> list[str]:
    """Physical lines of a text view; an interior BOM remains ordinary data.

    Whole-file callers use SourceSnapshot to strip the file's leading BOM.
    Extracted sections and Markdown parse buffers must not strip another one.
    """
    return _physical_source_parts(text)[0]


def _source_line_view(lines: list[str]) -> SourceSnapshot:
    """Adapt legacy physical rows without Unicode line splitting or source IO."""
    # Rows may have LF/CRLF endings (readlines) or none (the parser surface).
    text = "".join(line if line.endswith(("\r", "\n")) else line + "\n" for line in lines)
    # A legacy caller may hand us an unpaired surrogate (not valid UTF-8
    # source). Keep that content local/total; binary snapshot IO stays strict.
    return source_snapshot_from_bytes(text.encode("utf-8", errors="replace"))


def _lexical_original_ranges(snapshot: SourceSnapshot, spans: list) -> list[dict]:
    """Translate normalized character ranges to original half-open byte ranges."""
    starts = []
    cursor = 0
    for line, ending in zip(snapshot.physical_lines, snapshot.line_endings):
        starts.append(cursor)
        cursor += len(line) + bool(ending)

    def original(position):
        if position >= cursor:
            return len(snapshot.raw_bytes)
        index = bisect.bisect_right(starts, position) - 1
        return snapshot.byte_offset(index + 1, position - starts[index])

    return [{"start": original(a), "end": original(b), "certainty": certainty}
            for a, b, certainty in spans if b > a]


def _lexical_quoted_end(text: str, start: int, delimiter: str, *, doubled=False, multiline=True, backslash=True) -> tuple[int, bool]:
    """A delimiter walk with explicit escape rules, never a syntax parser."""
    cursor = start + len(delimiter)
    while cursor < len(text):
        if backslash and text[cursor] == "\\":
            cursor += 2
        elif text.startswith(delimiter, cursor):
            cursor += len(delimiter)
            if doubled and text.startswith(delimiter, cursor):
                cursor += len(delimiter)
            else:
                return cursor, True
        elif not multiline and text[cursor] == "\n":
            return cursor, False
        else:
            cursor += 1
    return len(text), False


# @beacon[
#   id=beacon-source@python_lexical_evidence,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Uses Python STRING tokens and a syntax-independent lexical continuation to exclude strings/docstrings; tokenizer failure never drops definite comments and unresolved quotes affect only their own range.,
# ]
def python_lexical_evidence(snapshot: SourceSnapshot) -> list[dict]:
    """STRING/comment evidence survives tokenizer errors without an AST gate.

    The fallback is only a quote/comment lexer. It supplies the part a failed
    tokenizer could not reach (including indentation errors), not another
    structural grammar. A broken single-quoted line recovers at its newline;
    an unclosed triple quote has genuinely uncertain context to EOF.
    """
    text = snapshot.normalized_text
    starts = [0]
    starts.extend(match.end() for match in re.finditer("\n", text))
    tokens = []
    literal_tokens = {tokenize.STRING}
    literal_tokens.update(getattr(tokenize, name) for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END") if hasattr(tokenize, name))
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in literal_tokens or token.type == tokenize.COMMENT:
                a = starts[token.start[0] - 1] + token.start[1]
                b = starts[token.end[0] - 1] + token.end[1]
                tokens.append((a, b, "comment" if token.type == tokenize.COMMENT else "literal"))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    # A token cannot be made less certain by fallback evidence. The walk also
    # handles f-string token streams on Python versions that split them.
    spans = []
    cursor = 0
    while cursor < len(text):
        char = text[cursor]
        if char == "#":
            end = text.find("\n", cursor)
            end = len(text) if end < 0 else end
            spans.append((cursor, end, "comment"))
            cursor = end
        elif char in "\"'":
            delimiter = char * 3 if text.startswith(char * 3, cursor) else char
            end, complete = _lexical_quoted_end(text, cursor, delimiter, multiline=len(delimiter) == 3)
            spans.append((cursor, end, "literal" if complete else "uncertain"))
            cursor = end
        else:
            cursor += 1
    # Token ranges have priority, with fallback only filling non-token gaps.
    remainder = []
    token_index = 0
    for a, b, certainty in spans:
        while token_index < len(tokens) and tokens[token_index][1] <= a:
            token_index += 1
        position = a
        index = token_index
        while index < len(tokens) and tokens[index][0] < b:
            x, y, _ = tokens[index]
            if position < x:
                remainder.append((position, min(x, b), certainty))
            position = max(position, y)
            index += 1
        if position < b:
            remainder.append((position, b, certainty))
    return _lexical_original_ranges(snapshot, sorted(tokens + remainder))


# @beacon[
#   id=beacon-source@js_lexical_evidence,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Lexes JS/TS comments strings regexes and template text with an iterative interpolation stack; real expression comments stay live through nested templates and malformed quotes create only local uncertainty.,
# ]
def js_lexical_evidence(snapshot: SourceSnapshot) -> list[dict]:
    """Lexical ranges only; nested ${...} code is not template literal text.

    No tree-sitter/AST prerequisite. Stack frames avoid recursion on arbitrary
    content. Broken single-line strings/regexes recover at the newline; an
    unresolved raw template segment remains uncertain to its actual boundary.
    """
    text = snapshot.normalized_text
    spans = []
    stack = [{"mode": "code", "depth": 0, "regex": True, "parens": [], "control": False}]
    cursor = 0
    while cursor < len(text):
        frame = stack[-1]
        if frame["mode"] == "template":
            start = cursor
            while cursor < len(text):
                if text[cursor] == "\\":
                    cursor += 2
                elif text[cursor] == "`":
                    cursor += 1
                    spans.append((start, cursor, "literal"))
                    stack.pop()
                    stack[-1]["regex"] = False
                    break
                elif text.startswith("${", cursor):
                    cursor += 2
                    spans.append((start, cursor, "literal"))
                    stack.append({"mode": "expression", "depth": 1, "regex": True, "parens": [], "control": False})
                    break
                else:
                    cursor += 1
            else:
                spans.append((start, len(text), "uncertain"))
            continue
        char = text[cursor]
        if text.startswith("//", cursor):
            end = text.find("\n", cursor)
            end = len(text) if end < 0 else end
            spans.append((cursor, end, "comment"))
            cursor = end
        elif text.startswith("/*", cursor):
            end = text.find("*/", cursor + 2)
            end = len(text) if end < 0 else end + 2
            spans.append((cursor, end, "comment"))
            cursor = end
        elif char in "\"'":
            end, complete = _lexical_quoted_end(text, cursor, char, multiline=False)
            spans.append((cursor, end, "literal" if complete else "uncertain"))
            cursor = end
            frame["regex"] = False
        elif char == "`":
            spans.append((cursor, cursor + 1, "literal"))
            cursor += 1
            stack.append({"mode": "template"})
        elif char == "/" and frame["regex"]:
            start = cursor
            cursor += 1
            bracket = False
            complete = False
            while cursor < len(text) and text[cursor] != "\n":
                c = text[cursor]
                if c == "\\":
                    cursor += 2
                    continue
                if c == "[":
                    bracket = True
                elif c == "]":
                    bracket = False
                elif c == "/" and not bracket:
                    cursor += 1
                    complete = True
                    break
                cursor += 1
            spans.append((start, cursor, "literal" if complete else "uncertain"))
            frame["regex"] = False
        elif char.isalpha() or char in "_$":
            start = cursor
            cursor += 1
            while cursor < len(text) and (text[cursor].isalnum() or text[cursor] in "_$"):
                cursor += 1
            word = text[start:cursor]
            frame["control"] = word in {"if", "while", "for", "with", "switch", "catch"}
            frame["regex"] = frame["control"] or word in {"return", "throw", "case", "delete", "typeof", "void", "new", "yield", "await", "else", "do", "in", "of", "instanceof"}
        elif char == "(":
            frame["parens"].append(frame["control"])
            frame["control"] = False
            frame["regex"] = True
            cursor += 1
        elif char == ")":
            frame["regex"] = frame["parens"].pop() if frame["parens"] else False
            cursor += 1
        elif char == "{":
            if frame["mode"] == "expression":
                frame["depth"] += 1
            frame["regex"] = True
            cursor += 1
        elif char == "}":
            cursor += 1
            if frame["mode"] == "expression":
                frame["depth"] -= 1
                if not frame["depth"]:
                    spans.append((cursor - 1, cursor, "literal"))
                    stack.pop()
            frame["regex"] = False
        elif not char.isspace():
            # Numeric tokens, array closes and postfix ++/-- end expressions.
            if char in "+-" and text.startswith(char * 2, cursor):
                cursor += 2
            else:
                frame["regex"] = not (char.isdigit() or char in "].")
                cursor += 1
        else:
            cursor += 1
    return _lexical_original_ranges(snapshot, spans)


_LEXICAL_EVIDENCE_MEMO = collections.OrderedDict()
_LEXICAL_EVIDENCE_LOCK = threading.RLock()


# @beacon[
#   id=beacon-source@source_lexical_evidence,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Selects a language lexer through a four-entry content/path/language memo shared by worker threads; computes each resident key once and returns a deep copy so caller edits never affect later evidence; no time expiry or beacon grammar.,
# ]
def source_lexical_evidence(snapshot: SourceSnapshot, language: str) -> list[dict]:
    """Reuse pure lexical ranges for four content keys, with caller-owned returns.

    The lock covers lookup and computation so concurrent misses cannot compute
    the same resident key twice. Eviction is content/LRU only, never time-based.
    Stored values remain private; even a first caller receives a deep copy.
    None of these adapters recognizes a beacon.
    """
    key = (snapshot.sha256, snapshot.canonical_path, language)
    with _LEXICAL_EVIDENCE_LOCK:
        cached = _LEXICAL_EVIDENCE_MEMO.get(key)
        if cached is None:
            adapter = {"python": python_lexical_evidence, "js": js_lexical_evidence,
                       "js_ts": js_lexical_evidence, "vue": js_lexical_evidence,
                       "sql": sql_lexical_evidence, "sh": shell_lexical_evidence,
                       "shell": shell_lexical_evidence, "yaml": yaml_lexical_evidence}.get(language)
            cached = adapter(snapshot) if adapter else []
            _LEXICAL_EVIDENCE_MEMO[key] = cached
            if len(_LEXICAL_EVIDENCE_MEMO) > 4:
                _LEXICAL_EVIDENCE_MEMO.popitem(last=False)
        else:
            _LEXICAL_EVIDENCE_MEMO.move_to_end(key)
    return copy.deepcopy(cached)


def _beacon_marker(text: str) -> tuple[str, int] | None:
    if text.startswith("@beacon-close["):
        return "close", len("@beacon-close[")
    if text.startswith("@beacon["):
        return "open", len("@beacon[")
    return None


def _beacon_inline_bracket(text: str) -> int | None:
    """A final bracket outside matching quotes and a field's nested brackets."""
    quote_until = -1
    depth = 0
    for index, char in enumerate(text):
        if index <= quote_until:
            continue
        if char in "\"'":
            end = _beacon_quote_end(text, index)
            if end is not None:
                quote_until = end
        elif char == "[":
            depth += 1
        elif char == "]":
            if depth:
                depth -= 1
            elif not text[index + 1:].strip(" ,\t"):
                return index
    return None


# @beacon[
#   id=beacon-source@_beacon_comment_rows,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Builds physical comment-body geometry with original columns and C/HTML wrapper ownership; subtracts lexical evidence, excludes Markdown fences and clips sections; normalized legacy context uses the same marker/field authority and leaves unprefixed prose unowned.,
# ]
def _beacon_comment_rows(snapshot: SourceSnapshot, language: str, start: int, end: int, evidence=(), byte_start=None, byte_end=None) -> list[dict]:
    """Comment evidence/ownership only; marker and field grammar live elsewhere.

    Lexical literals cannot open a wrapper or supply structural rows. Markdown
    fences exclude examples here. Incomplete wrappers stop locally at
    a new wrapper, a heading/fence, or a new independently prefixed marker.
    The legacy ``context`` view accepts already stripped comment bodies;
    unprefixed prose is not promoted to a comment or a field continuation.
    """
    rows = []
    lines = snapshot.physical_lines
    prefix = {"python": "#", "shell": "#", "sh": "#", "yaml": "#", "sql": "--",
              "js": "//", "js_ts": "//", "vue": "//"}.get(language)
    wrapped = ("<!--", "-->", "html") if language == "markdown" else (
        ("/*", "*/", "c") if language in {"js", "js_ts", "vue"} else None)
    active = None
    fence_char, fence_len = "", 0
    literals = sorted((r for r in evidence if r["certainty"] == "literal"), key=lambda r: r["start"])
    literal_index = 0
    for index in range(start - 1, end):
        raw = lines[index]
        if byte_start is not None or byte_end is not None:
            left = max(snapshot.byte_offsets[index], byte_start or 0)
            right = min(snapshot.byte_offset(index + 1, len(raw)), len(snapshot.raw_bytes) if byte_end is None else byte_end)
            if right <= left:
                rows.append({"line": index + 1, "text": "", "column": 0, "form": None, "wrapper": None})
                continue
            column_start = len(snapshot.raw_bytes[snapshot.byte_offsets[index]:left].decode("utf-8"))
            raw = " " * column_start + snapshot.raw_bytes[left:right].decode("utf-8")
        stripped = raw.lstrip()
        column = len(raw) - len(stripped)
        if language == "context":
            prefix = next((p for p in ("//", "--", "#", "*") if stripped.startswith(p)), None)
            wrapped = (("<!--", "-->", "html") if stripped.startswith("<!--") else ("/*", "*/", "c"))
        offset = snapshot.byte_offset(index + 1, column)
        while literal_index < len(literals) and literals[literal_index]["end"] <= offset:
            literal_index += 1
        if literal_index < len(literals) and literals[literal_index]["start"] <= offset < literals[literal_index]["end"]:
            rows.append({"line": index + 1, "text": stripped, "column": column, "form": None, "wrapper": None})
            continue
        fm = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", raw) if language == "markdown" else None
        if active is not None:
            # A new wrapper or definite line-comment marker is not owned by
            # a previous missing wrapper. Reprocess it as a fresh region.
            fresh = stripped.startswith(("<!--", "/*")) if language == "context" else stripped.startswith(("<!--",)) if language == "markdown" else stripped.startswith("/*")
            line_marker = prefix and stripped.startswith(prefix) and _beacon_marker(stripped[len(prefix):].lstrip())
            boundary = (language == "markdown" and not active["end_hint"]
                        and (fm or re.match(r"^ {0,3}#{1,6}\s", raw)))
            if fresh or line_marker or boundary:
                active["boundary_line"] = index + 1
                active = None
        if active is None and fm:
            run, tail = fm.groups()
            if not fence_char:
                fence_char, fence_len = run[0], len(run)
            elif run[0] == fence_char and len(run) >= fence_len and not tail.strip():
                fence_char, fence_len = "", 0
            rows.append({"line": index + 1, "text": "", "column": 0, "form": None, "wrapper": None})
            continue
        if active is None and fence_char:
            rows.append({"line": index + 1, "text": "", "column": 0, "form": None, "wrapper": None})
            continue
        if active is None and wrapped and stripped.startswith(wrapped[0]):
            opening, closing, form = wrapped
            end_hint = False
            for later in range(index, end):
                view = lines[later][column + len(opening):] if later == index else lines[later]
                if later != index and view.lstrip().startswith(opening):
                    break
                if closing in view:
                    end_hint = True
                    break
            active = {"form": form, "start_line": index + 1,
                      "start": snapshot.byte_offset(index + 1, column),
                      "open_range": (snapshot.byte_offset(index + 1, column),
                                     snapshot.byte_offset(index + 1, column + len(opening))),
                      "end": None, "end_line": None, "close_range": None,
                      "boundary_line": None, "end_hint": end_hint, "rows": []}
            column += len(opening)
        if active is not None:
            closing = "-->" if active["form"] == "html" else "*/"
            pos = raw.find(closing, column)
            stop = pos if pos >= 0 else len(raw)
            body = raw[column:stop]
            if active["form"] == "c":
                star = re.match(r"\s*\*(?!/)", body)
                if star:
                    column += star.end()
                    body = raw[column:stop]
            trim = len(body) - len(body.lstrip())
            column += trim
            row = {"line": index + 1, "text": body.lstrip().rstrip(), "column": column,
                   "form": active["form"], "wrapper": active}
            rows.append(row)
            active["rows"].append(row)
            if pos >= 0:
                active["end_line"] = index + 1
                active["end"] = snapshot.byte_offset(index + 1, pos + len(closing))
                active["close_range"] = (snapshot.byte_offset(index + 1, pos), active["end"])
                active["suffix"] = raw[pos + len(closing):]
                active = None
            continue
        if prefix and stripped.startswith(prefix):
            # Preserve the established '#'/'-' run sugar and exactly '//'.
            cut = len(stripped) - len(stripped.lstrip(prefix[0])) if prefix in {"#", "--"} else len(prefix)
            column += cut
            body = raw[column:]
            column += len(body) - len(body.lstrip())
            rows.append({"line": index + 1, "text": body.strip(), "column": column,
                         "form": "line", "wrapper": None})
        else:
            rows.append({"line": index + 1, "text": stripped, "column": column,
                         "form": ("line" if language == "context" and
                                  (_beacon_marker(stripped) or stripped.startswith("]") or
                                   parse_beacon_field_lines([stripped])) else None), "wrapper": None})
    return rows


# @beacon[
#   id=beacon-source@beacon_context_lines,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Projects recorded metadata ranges out of a source view without changing physical rows; mixed wrappers keep ordinary bytes and incomplete tails keep prose/unknown continuations. An optional Markdown parse projection releases an unowned wrapper opener without repairing disk.,
# ]
def beacon_context_lines(snapshot: SourceSnapshot, records: list[dict], *, release_incomplete_wrappers=False) -> list[str]:
    """Blank recorded metadata bytes while retaining all physical coordinates.

    Complete dedicated wrappers are metadata; mixed wrappers keep their shell.
    An incomplete record's continuation is evidence, not proof that following
    human prose is metadata. Only its marker and recognized field occurrences
    are removed. A Markdown outline may also blank an unowned wrapper opener
    so the HTML parser cannot hide following headings. This is a read
    projection, never a write ownership or delimiter-repair decision.
    """
    ranges = []
    for record in records:
        if record["complete"]:
            owned = (record["wrapper_range"] if record["wrapper_dedicated"] and record["wrapper_range"]
                     else (record["opener_range"][0], record["read_end"]))
            ranges.append(owned)
        else:
            ranges.append(record["opener_range"])
            ranges.extend(o["byte_range"] for o in record["occurrences"] if o.get("byte_range"))
            if record["bracket_range"]:
                ranges.append(record["bracket_range"])
            if release_incomplete_wrappers and record["wrapper_open_range"] and not record["wrapper_range"]:
                ranges.append(record["wrapper_open_range"])
    ranges.sort()
    result, range_index = [], 0
    for line, raw in enumerate(snapshot.physical_lines, 1):
        start = snapshot.byte_offsets[line - 1]
        end = snapshot.byte_offset(line, len(raw))
        while range_index < len(ranges) and ranges[range_index][1] <= start:
            range_index += 1
        cursor, parts = start, []
        current = range_index
        while current < len(ranges) and ranges[current][0] < end:
            a, b = ranges[current]
            current += 1
            a, b = max(start, a), min(end, b)
            if b <= cursor:
                continue
            a = max(a, cursor)
            parts.append(snapshot.raw_bytes[cursor:a].decode("utf-8"))
            parts.append(" " * len(snapshot.raw_bytes[a:b].decode("utf-8")))
            cursor = b
        parts.append(snapshot.raw_bytes[cursor:end].decode("utf-8"))
        result.append("".join(parts))
    return result


# @beacon[
#   id=beacon-source@_beacon_record,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Collects one bounded open/close record with located fields and section-clipped original-byte ownership; reprocesses neighbours and preserves incomplete fields/repair candidates without inventing a whole-line deletion extent.,
# ]
def _beacon_record(snapshot: SourceSnapshot, rows: list[dict], index: int, section: dict) -> tuple[dict, int]:
    """Collect a bounded block, owning neither neighbours nor section tags."""
    first = rows[index]
    marker, marker_size = _beacon_marker(first["text"])
    wrapper = first["wrapper"]
    line = first["line"]
    start_byte = snapshot.byte_offset(line, first["column"])
    opener_range = (start_byte, snapshot.byte_offset(line, first["column"] + marker_size))
    field_rows = []
    bracket_range = None
    end_line = None
    reason = "section_end" if section["end_line"] < len(snapshot.physical_lines) or section.get("end_byte", len(snapshot.raw_bytes)) < len(snapshot.raw_bytes) else "eof"
    offending = None
    read_end = opener_range[1]
    cursor = index
    last = index
    while cursor < len(rows):
        row = rows[cursor]
        text = row["text"]
        if cursor != index:
            if row["form"] and _beacon_marker(text):
                reason, offending = "next_opener", row["line"]
                break
            same_region = (row["wrapper"] is wrapper) if wrapper is not None else row["form"] == "line"
            if not same_region and text.strip():
                reason = "wrapper_end" if wrapper and wrapper["end"] is not None else "non_comment_line"
                offending = row["line"]
                break
            if wrapper and row["wrapper"] is not wrapper:
                following = row["wrapper"]
                first_text = next((r["text"] for r in following["rows"] if r["text"]), "") if following else text
                reason = ("wrapper_end" if wrapper["end"] is not None else
                          "next_opener" if _beacon_marker(first_text) else "non_comment_line")
                offending = row["line"]
                break
        content = text[marker_size:].lstrip() if cursor == index else text
        content_column = row["column"] + (len(text) - len(content) if cursor == index else 0)
        bracket_index = _beacon_inline_bracket(content) if cursor == index else (0 if content.startswith("]") else None)
        if bracket_index is not None:
            bracket_range = (snapshot.byte_offset(row["line"], content_column + bracket_index),
                             snapshot.byte_offset(row["line"], content_column + bracket_index + 1))
            content = content[:bracket_index].rstrip()
            end_line, reason = row["line"], "bracket"
        if content:
            field_rows.append({"text": content, "line": row["line"], "column": content_column,
                               "byte_range": (snapshot.byte_offset(row["line"], content_column),
                                              snapshot.byte_offset(row["line"], content_column + len(content))),
                               "inline": cursor == index})
        read_end = bracket_range[1] if bracket_range else snapshot.byte_offset(row["line"], row["column"] + len(text))
        last = cursor
        cursor += 1
        if bracket_range:
            break
        if wrapper and wrapper["end_line"] == row["line"]:
            reason, offending = "wrapper_end", row["line"]
            break
    occurrences, findings = [], []
    fields = parse_beacon_field_lines(field_rows, occurrences=occurrences, diagnostics=findings)
    ids = [v["value"] for v in occurrences if v["key"] == "id"]
    id_conflict = len(set(ids)) > 1
    wrapper_complete = wrapper is None or wrapper["end"] is not None
    # A distant HTML arrow is not a delimiter owned by this metadata. Only
    # blank gaps to the wrapper's end give a dedicated comment ownership.
    dedicated = True
    wrapper_range = None
    if wrapper:
        before = [r["text"] for r in wrapper["rows"] if r["line"] < line]
        after = [r["text"] for r in wrapper["rows"] if r["line"] > (end_line or rows[last]["line"])]
        end_row = rows[last]
        tail = ""
        if bracket_range and end_row["line"] == end_line:
            raw = snapshot.physical_lines[end_line - 1]
            tail = snapshot.raw_bytes[bracket_range[1]:snapshot.byte_offset(end_line, len(raw))].decode("utf-8")
            closing = "-->" if wrapper["form"] == "html" else "*/"
            tail = tail.split(closing, 1)[0].strip(" ,\t")
        dedicated = not any(v.strip() for v in before + after) and not tail
        if wrapper["form"] == "html" and any(re.match(r"^(?:#{1,6}\s|`{3,}|~{3,})", v) for v in after):
            wrapper_complete = False
            findings.append({"code": "wrapper_ownership_ambiguity", "line": line,
                             "wrapper_end_candidate": wrapper["close_range"],
                             "metadata_end_candidate": read_end})
        if wrapper_complete:
            wrapper_range = (wrapper["start"], wrapper["end"])
        elif bracket_range:
            findings.append({"code": "missing_wrapper", "line": line})
    complete = bracket_range is not None and wrapper_complete
    if not bracket_range:
        findings.append({"code": "missing_bracket", "line": line, "terminated_by": reason,
                         "offending_line": offending})
    write_ranges = None
    whole_line_extent = None
    if complete and not id_conflict and not any(f["code"] == "inline_field_ambiguity" for f in findings):
        owned = wrapper_range if wrapper and dedicated else (start_byte, read_end)
        write_ranges = [owned]
        first_line = wrapper["start_line"] if wrapper and dedicated else line
        last_line = wrapper["end_line"] if wrapper and dedicated else end_line
        right = snapshot.physical_lines[last_line - 1]
        line_start = snapshot.byte_offsets[first_line - 1]
        content_end = snapshot.byte_offset(last_line, len(right))
        prefix_bytes = snapshot.raw_bytes[line_start:owned[0]]
        suffix_bytes = snapshot.raw_bytes[owned[1]:content_end]
        # Line-comment sugar is part of its own whole-line write ownership.
        if not wrapper:
            left = max(line_start, section.get("start_byte", line_start))
            right = min(snapshot.byte_offsets[last_line], section.get("end_byte", snapshot.byte_offsets[last_line]))
            if left == line_start and right == snapshot.byte_offsets[last_line]:
                whole_line_extent = (first_line - 1, last_line - 1)
            write_ranges = [(left, right)]
        elif not prefix_bytes.strip() and not suffix_bytes.strip() and dedicated:
            whole_line_extent = (first_line - 1, last_line - 1)
            write_ranges = [(line_start, snapshot.byte_offsets[last_line])]
    kind = (fields.get("kind") or "span").strip().lower() or "span"
    # The missing-bracket finding is structural; it does not itself make a
    # one-line-field-only repair ambiguous.
    clean_repair = (not bracket_range and not wrapper and bool(fields.get("id")) and bool(fields.get("kind"))
                    and kind in {"ast", "span"} and not id_conflict
                    and all(f["code"] == "missing_bracket" or (f["code"] == "duplicate_field" and not f["conflicting"])
                            for f in findings))
    return {
        "marker": marker, "comment_form": first["form"], "line": line, "end_line": end_line,
        "read_end_line": rows[last]["line"], "read_end": read_end,
        "opener_range": opener_range, "field_ranges": [f["byte_range"] for f in field_rows],
        "bracket_range": bracket_range, "wrapper_range": wrapper_range,
        "wrapper_open_range": wrapper["open_range"] if wrapper else None,
        "wrapper_close_range": wrapper["close_range"] if wrapper else None,
        "wrapper_dedicated": dedicated, "complete": complete, "write_ranges": write_ranges,
        "line_extent": whole_line_extent, "terminated_by": reason, "offending_line": offending,
        "fields": fields, "field_texts": [f["text"] for f in field_rows], "occurrences": occurrences,
        "id_conflicting": id_conflict, "literal_certainty": "comment", "diagnostics": findings,
        "section": dict(section), "repair_unambiguous": clean_repair,
        "repair_candidates": {"body_end": read_end, "field_end": field_rows[-1]["byte_range"][1] if field_rows else opener_range[1]},
        "pair_state": "unpaired", "closed": False, "close_line": None, "pair_candidates": [],
        "inner_range": None, "inner_lines": None,
    }, cursor


# @beacon[
#   id=beacon-source@_pair_beacon_records,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Processes complete exact-id closes once within their section; retains distinct nested/interleaved and empty pairs, bounds open-only byte ranges to the actual section and never pairs a lexically uncertain close.,
# ]
def _pair_beacon_records(records: list[dict], snapshot: SourceSnapshot, section: dict) -> None:
    """Pair definite closes once; keep ambiguous edges and actual section EOF."""
    pending: dict[str, list[dict]] = {}
    for record in records:
        bid = record["fields"].get("id")
        if record["marker"] == "open":
            if (record["fields"].get("kind") or "span").strip().lower() != "span" or not bid:
                continue
            uncertain = record["literal_certainty"] == "uncertain"
            record["pair_state"] = "ambiguous" if record["id_conflicting"] or uncertain else "open_only"
            if not record["id_conflicting"] and not uncertain:
                pending.setdefault(bid, []).append(record)
            continue
        if not record["complete"] or record["literal_certainty"] == "uncertain" or record["id_conflicting"] or not bid:
            record["pair_state"] = "incomplete" if not record["complete"] else "ambiguous"
            continue
        candidates = pending.get(bid, [])
        if len(candidates) == 1:
            opening = candidates[0]
            opening["closed"], opening["pair_state"] = True, "closed"
            opening["close_line"] = record["line"]
            record["pair_state"] = "matched"
            record["opener_line"] = opening["line"]
            a = (snapshot.byte_offsets[opening["line_extent"][1] + 1] if opening["line_extent"]
                 else opening["wrapper_range"][1] if opening["wrapper_range"] else opening["read_end"])
            b = (snapshot.byte_offsets[record["line_extent"][0]] if record["line_extent"]
                 else record["wrapper_range"][0] if record["wrapper_range"] else record["opener_range"][0])
            opening["inner_range"] = (a, b)
            opening["inner_lines"] = (opening["line_extent"][1] + 2 if opening["line_extent"] else opening["read_end_line"] + 1,
                                      record["line_extent"][0] if record["line_extent"] else record["line"] - 1)
            pending[bid] = []
        elif candidates:
            record["pair_state"] = "ambiguous"
            record["pair_candidates"] = [r["line"] for r in candidates]
            for opening in candidates:
                opening["pair_state"] = "ambiguous"
                opening["pair_candidates"].append(record["line"])
                opening["write_ranges"] = opening["line_extent"] = None
            record["write_ranges"] = record["line_extent"] = None
        else:
            record["pair_state"] = "orphan"
    for record in records:
        if record["pair_state"] == "open_only":
            a = (snapshot.byte_offsets[record["line_extent"][1] + 1] if record["line_extent"]
                 else record["wrapper_range"][1] if record["wrapper_range"] else record["read_end"])
            b = section.get("end_byte", snapshot.byte_offsets[section["end_line"]])
            record["inner_range"] = (a, b)
            record["inner_lines"] = (record["line_extent"][1] + 2 if record["line_extent"] else record["read_end_line"] + 1,
                                      section["end_line"])


# @beacon[
#   id=beacon-source@scan_beacon_structure,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Single in-memory structural authority builds one lexical range index per call while retaining original overlap priority; bounded original-byte records and exact SFC limits exclude literals and localize uncertainty; definite exact-id closes pair without a file-wide gate.,
# ]
def scan_beacon_structure(
    snapshot: SourceSnapshot, language: str, *, evidence: list | None = None,
    section_type: str | None = None, section_start: int = 1, section_end: int | None = None,
    section_byte_start: int | None = None, section_byte_end: int | None = None,
) -> list[dict]:
    """Return bounded records; no IO, syntax prerequisite or content exceptions.

    ``evidence`` contains half-open original-byte ranges with certainty literal
    or uncertain. Adapters supply language lexical evidence, not block grammar.
    Section coordinates are absolute physical lines and optional original-byte
    limits in the supplied snapshot (never normalized tree-sitter indexes).
    A start-sorted index bounds each marker's evidence window; original evidence
    ordinals retain first-match semantics even for overlapping unsorted ranges.
    """
    end = len(snapshot.physical_lines) if section_end is None else min(section_end, len(snapshot.physical_lines))
    start = max(1, section_start)
    section = {"type": section_type or language, "start_line": start, "end_line": end}
    if section_byte_start is not None:
        section["start_byte"] = section_byte_start
    if section_byte_end is not None:
        section["end_byte"] = section_byte_end
    rows = _beacon_comment_rows(snapshot, language, start, end, evidence or (), section_byte_start, section_byte_end)
    indexed_evidence = sorted(enumerate(evidence or ()), key=lambda item: item[1]["start"])
    evidence_starts = [region["start"] for _order, region in indexed_evidence]
    longest = max((region["end"] - region["start"] for _order, region in indexed_evidence), default=0)

    def evidence_window(left, right):
        first = bisect.bisect_left(evidence_starts, left - longest)
        stop = bisect.bisect_left(evidence_starts, right)
        return indexed_evidence[first:stop]

    records = []
    index = 0
    while index < len(rows):
        row = rows[index]
        if row["form"] and _beacon_marker(row["text"]):
            offset = snapshot.byte_offset(row["line"], row["column"])
            certainty = min(((order, region.get("certainty")) for order, region in evidence_window(offset, offset + 1)
                             if region["start"] <= offset < region["end"]), default=(len(indexed_evidence), "comment"))[1]
            if certainty == "literal":
                index += 1
                continue
            record, index = _beacon_record(snapshot, rows, index, section)
            # Any uncertainty in owned metadata affects this block only. It
            # cannot authorize a close or a whole-block replacement.
            if any(region.get("certainty") == "uncertain" and region["start"] < record["read_end"] and offset < region["end"]
                   for _order, region in evidence_window(offset, record["read_end"])):
                certainty = "uncertain"
            record["literal_certainty"] = certainty
            if certainty == "uncertain":
                record["write_ranges"] = record["line_extent"] = None
                record["repair_unambiguous"] = False
                record["diagnostics"].append({"code": "lexical_uncertainty", "line": record["line"]})
            records.append(record)
        else:
            index += 1
    _pair_beacon_records(records, snapshot, section)
    return records


# ---------------------------------------------------------------------------
# SQL, shell and YAML lexical evidence
# ---------------------------------------------------------------------------


def _sql_line_leading(text: str, index: int) -> bool:
    """True when only line-start whitespace precedes this position."""
    cursor = index - 1
    while cursor >= 0 and text[cursor] in " \t":
        cursor -= 1
    return cursor < 0 or text[cursor] == "\n"


def _sql_dollar_quote(text: str, index: int) -> str | None:
    """The $tag$ delimiter of a PostgreSQL dollar-quoted body, or None.

    A dollar sign inside an identifier and a positional parameter ($1) are
    not openers; the tag obeys identifier rules and contains no dollar.
    """
    if index and (text[index - 1].isalnum() or text[index - 1] in "_$"):
        return None
    cursor = index + 1
    if cursor < len(text) and text[cursor] == "$":
        return "$$"
    if cursor >= len(text) or not (text[cursor].isalpha() or text[cursor] == "_"):
        return None
    while cursor < len(text) and (text[cursor].isalnum() or text[cursor] == "_"):
        cursor += 1
    if cursor < len(text) and text[cursor] == "$":
        return text[index:cursor + 1]
    return None


def _lexical_escaped_delimiter(text: str, start: int, end: int, delimiter: str) -> bool:
    """True when an escaped delimiter sits inside one completed region.

    A delimiter character behind an odd backslash run is the one place a
    completed region's extent can depend on which dialect's escape rule
    applies, so such a region is never claimed as definitely literal.
    """
    for index in range(start, min(end, len(text))):
        if text[index] != delimiter:
            continue
        back = 0
        cursor = index - 1
        while cursor >= start and text[cursor] == "\\":
            back += 1
            cursor -= 1
        if back % 2:
            return True
    return False


# @beacon[
#   id=beacon-source@sql_lexical_evidence,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=One bounded lexical pass over the normalized source: SQL line and nested block comments are recognized first, then single/double/backtick strings with doubled quotes, E-strings and $tag$ bodies. A completed region is literal; an unterminated one or a dialect-dependent backslash-escaped delimiter stays locally uncertain, so examples never become live blocks and no later comment is suppressed.,
# ]
def sql_lexical_evidence(snapshot: SourceSnapshot) -> list[dict]:
    """Comment and string certainty only; never a block, field or dialect parser."""
    text = snapshot.normalized_text
    size = len(text)
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    while cursor < size:
        char = text[cursor]
        if text.startswith("--", cursor):
            end = text.find("\n", cursor)
            spans.append((cursor, size if end < 0 else end, "comment"))
            cursor = size if end < 0 else end
        elif char == "#" and _sql_line_leading(text, cursor):
            # A line-leading '#' comment (MySQL/MariaDB); elsewhere '#' is an
            # ordinary operator character and never opens one.
            end = text.find("\n", cursor)
            spans.append((cursor, size if end < 0 else end, "comment"))
            cursor = size if end < 0 else end
        elif text.startswith("/*", cursor):
            # PostgreSQL block comments nest; the scan stays total either way.
            end = cursor + 2
            depth = 1
            while depth and end < size:
                if text.startswith("/*", end):
                    depth += 1
                    end += 2
                elif text.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            spans.append((cursor, end, "comment"))
            cursor = end
        elif char == "$":
            delimiter = _sql_dollar_quote(text, cursor)
            if delimiter is None:
                cursor += 1
                continue
            close = text.find(delimiter, cursor + len(delimiter))
            end = size if close < 0 else close + len(delimiter)
            spans.append((cursor, end, "literal" if close >= 0 else "uncertain"))
            cursor = end
        elif char in "'\"`":
            # E'...' makes backslash escapes definite; a plain quote's
            # backslash rule is dialect-dependent.
            prefix = text[cursor - 1:cursor].lower()
            definite = (prefix == "e"
                        and (cursor < 2 or not (text[cursor - 2].isalnum() or text[cursor - 2] == "_")))
            end, complete = _lexical_quoted_end(text, cursor, char, doubled=True, multiline=True)
            if complete and not definite and _lexical_escaped_delimiter(text, cursor, end, char):
                spans.append((cursor, end, "uncertain"))
            else:
                spans.append((cursor, end, "literal" if complete else "uncertain"))
            cursor = end
        else:
            cursor += 1
    return _lexical_original_ranges(snapshot, sorted(spans))


def _shell_word_break(text: str, index: int) -> bool:
    """True when the '#' at this position begins a comment word."""
    if index == 0:
        return True
    return text[index - 1] in " \t\n;|&()<>"


def _shell_heredoc_header(text: str, index: int):
    """(delimiter, strip_tabs, end) for a << or <<- operator; None otherwise.

    A here-string (<<<) is not a here-document. Arithmetic shifts are excluded
    by the caller's arithmetic frame, not by forbidding legal delimiter words
    such as 123 or $END. Quote removal follows shell single/double rules,
    including escaped quotes and physical line continuations in a word.
    """
    size = len(text)
    cursor = index + 2
    if cursor < size and text[cursor] == "<":
        return None
    strip_tabs = False
    if cursor < size and text[cursor] == "-":
        strip_tabs = True
        cursor += 1
    while cursor < size and text[cursor] in " \t":
        cursor += 1
    if cursor >= size or text[cursor] in "\n;&|()<>`":
        return None
    pieces: list[str] = []
    while cursor < size:
        char = text[cursor]
        if char in " \t\n;&|()<>`":
            break
        if char in "'\"":
            end, complete = _lexical_quoted_end(text, cursor, char, multiline=True, backslash=char != "'")
            if not complete:
                return None
            piece = text[cursor + 1:end - 1]
            if char == '"':
                piece = re.sub(r'\\([$`"\\\n])', lambda match: "" if match.group(1) == "\n" else match.group(1), piece)
            pieces.append(piece)
            cursor = end
        elif char == "\\":
            escaped = text[cursor + 1:cursor + 2]
            if escaped != "\n":
                pieces.append(escaped)
            cursor += 2
        else:
            pieces.append(char)
            cursor += 1
    if not pieces:
        return None
    return "".join(pieces), strip_tabs, cursor


def _shell_heredoc_bodies(text: str, cursor: int, pending: list, spans: list) -> int:
    """Consume every queued here-document body; return the cursor after them.

    Bodies queue in source order and each one ends at its own delimiter line
    (leading tabs stripped only for <<-). A body with no delimiter line stays
    uncertain to EOF rather than being claimed as literal.
    """
    size = len(text)
    for delimiter, strip_tabs in pending:
        if cursor > size:
            break
        start = cursor
        found = None
        while cursor < size:
            line_end = text.find("\n", cursor)
            stop = size if line_end < 0 else line_end
            line = text[cursor:stop]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                found = cursor
                cursor = stop if line_end < 0 else line_end + 1
                break
            cursor = stop if line_end < 0 else line_end + 1
        if found is None:
            cursor = size
        spans.append((start, found if found is not None else size,
                      "literal" if found is not None else "uncertain"))
    pending.clear()
    return cursor


# @beacon[
#   id=beacon-source@shell_lexical_evidence,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Lexes shell comment words and single/ANSI/double quotes with command-substitution nesting; consumes queued << / <<- bodies with legal quoted or numeric delimiters, excluding <<< and arithmetic shifts. Unresolved regions stay local evidence, not a file-wide gate.,
# ]
def shell_lexical_evidence(snapshot: SourceSnapshot) -> list[dict]:
    """Comment/quote/heredoc evidence, including arithmetic-vs-redirection context."""
    text = snapshot.normalized_text
    size = len(text)
    spans: list[tuple[int, int, str]] = []
    stack: list[dict] = [{"kind": "code", "end": None}]
    pending: list[tuple[str, bool]] = []
    cursor = 0

    def resume() -> None:
        """Restart an interrupted double-quoted literal after nested code."""
        if stack and stack[-1]["kind"] == "double" and stack[-1]["literal_start"] is None:
            stack[-1]["literal_start"] = cursor

    while cursor < size:
        frame = stack[-1]
        kind = frame["kind"]
        if kind == "code":
            char = text[cursor]
            end_kind = frame["end"]
            if char == "\\":
                cursor += 2
            elif char == "\n":
                cursor += 1
                if pending:
                    cursor = _shell_heredoc_bodies(text, cursor, pending, spans)
            elif char == "#" and end_kind not in {"arith", "arith_bracket"} and _shell_word_break(text, cursor):
                end = text.find("\n", cursor)
                spans.append((cursor, size if end < 0 else end, "comment"))
                cursor = size if end < 0 else end
            elif char == "'":
                # Shell single quotes never take a backslash escape.
                end = text.find("'", cursor + 1)
                spans.append((cursor, size if end < 0 else end + 1,
                              "literal" if end >= 0 else "uncertain"))
                cursor = size if end < 0 else end + 1
            elif text.startswith("$'", cursor):
                walk = cursor + 2
                while walk < size and text[walk] != "'":
                    walk += 2 if text[walk] == "\\" else 1
                closed = walk < size
                spans.append((cursor, walk + 1 if closed else size,
                              "literal" if closed else "uncertain"))
                cursor = walk + 1 if closed else size
            elif char == '"':
                stack.append({"kind": "double", "start": cursor, "literal_start": cursor})
                cursor += 1
            elif text.startswith('$"', cursor):
                stack.append({"kind": "double", "start": cursor, "literal_start": cursor})
                cursor += 2
            elif char == "`":
                if end_kind == "backtick":
                    stack.pop()
                    cursor += 1
                    resume()
                else:
                    stack.append({"kind": "code", "end": "backtick"})
                    cursor += 1
            elif text.startswith("$((", cursor):
                stack.append({"kind": "code", "end": "arith", "depth": 2})
                cursor += 3
            elif text.startswith("$[", cursor):
                stack.append({"kind": "code", "end": "arith_bracket", "depth": 1})
                cursor += 2
            elif text.startswith("$(", cursor):
                stack.append({"kind": "code", "end": "paren", "depth": 1})
                cursor += 2
            elif text.startswith("((", cursor) and end_kind != "arith":
                stack.append({"kind": "code", "end": "arith", "depth": 2})
                cursor += 2
            elif char == "(" and end_kind in ("paren", "arith"):
                frame["depth"] = frame["depth"] + 1
                cursor += 1
            elif char == ")" and end_kind in ("paren", "arith"):
                frame["depth"] = frame["depth"] - 1
                cursor += 1
                if frame["depth"] <= 0:
                    stack.pop()
                    resume()
            elif char in "[]" and end_kind == "arith_bracket":
                frame["depth"] += 1 if char == "[" else -1
                cursor += 1
                if frame["depth"] <= 0:
                    stack.pop()
                    resume()
            elif text.startswith("<<<", cursor) and end_kind not in {"arith", "arith_bracket"}:
                cursor += 3          # a here-string operator, never a heredoc
            elif text.startswith("<<", cursor) and end_kind not in {"arith", "arith_bracket"}:
                header = _shell_heredoc_header(text, cursor)
                if header is None:
                    cursor += 1
                else:
                    delimiter, strip_tabs, cursor = header
                    pending.append((delimiter, strip_tabs))
            else:
                cursor += 1
        elif kind == "double":
            literal_start = frame["literal_start"]
            while cursor < size:
                char = text[cursor]
                if char == "\\":
                    cursor += 2
                elif char == '"':
                    spans.append((literal_start, cursor + 1, "literal"))
                    stack.pop()
                    cursor += 1
                    break
                elif text.startswith("$((", cursor):
                    spans.append((literal_start, cursor, "literal"))
                    frame["literal_start"] = None
                    stack.append({"kind": "code", "end": "arith", "depth": 2})
                    cursor += 3
                    break
                elif text.startswith("$[", cursor):
                    spans.append((literal_start, cursor, "literal"))
                    frame["literal_start"] = None
                    stack.append({"kind": "code", "end": "arith_bracket", "depth": 1})
                    cursor += 2
                    break
                elif text.startswith("$(", cursor):
                    spans.append((literal_start, cursor, "literal"))
                    frame["literal_start"] = None
                    stack.append({"kind": "code", "end": "paren", "depth": 1})
                    cursor += 2
                    break
                elif char == "`":
                    spans.append((literal_start, cursor, "literal"))
                    frame["literal_start"] = None
                    stack.append({"kind": "code", "end": "backtick"})
                    cursor += 1
                    break
                else:
                    cursor += 1
        else:
            cursor += 1
    for frame in reversed(stack):
        if frame["kind"] == "double" and frame["literal_start"] is not None and frame["literal_start"] < size:
            spans.append((frame["literal_start"], size, "uncertain"))
    return _lexical_original_ranges(snapshot, sorted(spans))


def _yaml_dash_position(text: str, index: int) -> bool:
    """True when the '-' at this position is a sequence dash."""
    if text[index] != "-":
        return False
    cursor = index - 1
    while cursor >= 0 and text[cursor] in " \t":
        cursor -= 1
    return cursor < 0 or text[cursor] in "\n-"


def _yaml_node_position(text: str, index: int, flow: bool = False) -> bool:
    """True when a |, >, ' or " here begins a node value.

    A line start, mapping colon or sequence dash opens a node, optionally
    followed by tag/anchor properties. Flow punctuation opens a node only in
    an actual collection, not when it belongs to a plain scalar.
    """
    before = index - 1
    while before >= 0 and text[before] in " \t":
        before -= 1
    while before >= 0:
        start = text.rfind("\n", 0, before + 1) + 1
        prop = re.search(r"(?:![^\s]*|&[^\s]+)$", text[start:before + 1])
        if prop is None:
            break
        before = start + prop.start() - 1
        while before >= 0 and text[before] in " \t":
            before -= 1
    return (before < 0 or text[before] in "\n:"
            or (flow and text[before] in "[{,?")
            or (text[before] == "-" and _yaml_dash_position(text, before)))


def _yaml_block_header(text: str, index: int):
    """The end of a |/> indicator line after chomp/indent digits, or None."""
    size = len(text)
    cursor = index + 1
    while cursor < size and text[cursor] in "+-0123456789":
        cursor += 1
    while cursor < size and text[cursor] in " \t":
        cursor += 1
    if cursor >= size:
        return size
    if text[cursor] == "#":
        line_end = text.find("\n", cursor)
        return size if line_end < 0 else line_end
    if text[cursor] == "\n":
        return cursor
    return None


def _yaml_block_body(text: str, index: int, line_end: int, spans: list) -> int:
    """Mark one block scalar's actual indented body; return the next cursor.

    Honor an explicit indent relative to the containing mapping/sequence;
    otherwise the first nonblank content line determines it. Extra indentation
    on that first line must not hide later, less-indented scalar content.
    """
    size = len(text)
    line_start = text.rfind("\n", 0, index) + 1
    base = 0
    while line_start + base < size and text[line_start + base] in " \t":
        base += 1
    prefix = text[line_start:index]
    last_dash = None
    while True:
        dash = re.match(r"-[ \t]+", prefix[base:])
        if dash is None:
            break
        last_dash = base
        base += dash.end()
    if last_dash is not None and not re.search(r":(?:\s|$)", prefix[base:]):
        base = last_dash
    first = size if line_end >= size else line_end + 1
    cursor = first
    header = text[index:line_end].split("#", 1)[0].strip()
    explicit = next((int(char) for char in header[1:] if char in "123456789"), None)
    indent = base + explicit if explicit is not None else None
    body_end = first
    while cursor < size:
        stop = text.find("\n", cursor)
        stop = size if stop < 0 else stop
        content = text[cursor:stop].lstrip(" \t")
        if content:
            current = stop - cursor - len(content)
            if indent is None:
                if current <= base:
                    return first
                indent = current
            elif current < indent:
                break
            body_end = size if stop >= size else stop + 1
        elif indent is not None:
            body_end = size if stop >= size else stop + 1
        cursor = size if stop >= size else stop + 1
    if indent is None:
        return first
    spans.append((first, body_end, "literal"))
    return body_end


# @beacon[
#   id=beacon-source@yaml_lexical_evidence,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Lexes YAML comments before tagged/anchored block or flow quoted scalars and |/> bodies with real escape/indentation rules; plain-scalar punctuation stays text and unresolved quotes create only local uncertainty.,
# ]
def yaml_lexical_evidence(snapshot: SourceSnapshot) -> list[dict]:
    """Scalar evidence honors tags/anchors, indentation and escapes without a syntax gate."""
    text = snapshot.normalized_text
    size = len(text)
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    flow = []
    while cursor < size:
        char = text[cursor]
        if char == "#" and (cursor == 0 or text[cursor - 1] in " \t\n"):
            end = text.find("\n", cursor)
            spans.append((cursor, size if end < 0 else end, "comment"))
            cursor = size if end < 0 else end
        elif char in "[{" and _yaml_node_position(text, cursor, bool(flow)):
            flow.append(char)
            cursor += 1
        elif char in "]}" and flow:
            flow.pop()
            cursor += 1
        elif char in "|>" and not flow and _yaml_node_position(text, cursor):
            line_end = _yaml_block_header(text, cursor)
            if line_end is None:
                cursor += 1
            else:
                cursor = _yaml_block_body(text, cursor, line_end, spans)
        elif char in "'\"" and _yaml_node_position(text, cursor, bool(flow)):
            end, complete = _lexical_quoted_end(text, cursor, char, doubled=char == "'", multiline=True, backslash=char != "'")
            spans.append((cursor, end, "literal" if complete else "uncertain"))
            cursor = end
        else:
            cursor += 1
    return _lexical_original_ranges(snapshot, sorted(spans))
