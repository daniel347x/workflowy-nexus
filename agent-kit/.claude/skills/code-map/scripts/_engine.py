# Generated file: do not edit by hand. Regenerate it with the build script from the upstream source.

import ast
import bisect
import collections
import copy
import hashlib
import io
import json
import ntpath
import os
import random
import re
import shutil
import string
import tempfile
import threading
import tokenize
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Sequence

try:
    from tree_sitter import Parser
    from tree_sitter_language_pack import get_language, get_parser
    _HAVE_TREE_SITTER = True
except Exception:
    Parser = None
    get_language = None
    get_parser = None
    _HAVE_TREE_SITTER = False

NODE_DECORATION_SYMBOLS = frozenset({
    "🔱",
    "📂",
    "📄",
    "🐍",
    "📘",
    "🗄️",
    "🐚",
    "📦",
    "ƒ",
    "⚡",
    "💎",
    "🟨",
    "🟦",
    "🟩",
})


def strip_leading_decoration_symbols(text_value):
    if not text_value or not isinstance(text_value, str):
        return text_value or ""
    s = text_value
    symbols_longest_first = sorted(NODE_DECORATION_SYMBOLS, key=len, reverse=True)
    while True:
        s = s.lstrip()
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


_FORBIDDEN_COMMENT_TOKENS = ("/*", "*/", "<!--", "-->", "@beacon[", "BEACON (")


_TRANSPORT_ENTITY_RE = re.compile(
    r"&(?:lt|gt|amp|quot);|&#\d+;|&#[xX][0-9a-fA-F]+;",
    re.IGNORECASE,
)


def validate_beacon_comment_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    comment = str(value).strip()
    if not comment:
        return None
    comment = strip_leading_decoration_symbols(comment)
    if not comment:
        return None
    for ch in comment:
        code = ord(ch)
        if (
            code < 0x20
            or code == 0x7F
            or 0x80 <= code <= 0x9F
            or code in (0x2028, 0x2029)
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
            f"unsafe beacon comment {comment!r}: contains an HTML entity pattern"
        )
    return comment


def _parser_normalize_comment_value(value: Optional[str]) -> str:
    return value or ""


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


def parse_beacon_field_lines(
    texts: Any, *, occurrences: list | None = None, diagnostics: list | None = None,
) -> dict[str, str]:
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
                m = None
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


def normalize_beacon_id(value: Any) -> str:
    text = str(value or "").strip()
    while text and text[-1] in ",]":
        text = text[:-1].rstrip()
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        return text[1:-1]
    return text


def parse_beacon_note_metadata(note: Any) -> list[dict]:
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
            while index < len(lines) and lines[index].strip() != "---":
                index += 1
        if markdown and index < len(lines) and lines[index].strip() == "---":
            index += 1
            while index < len(lines) and not lines[index].strip():
                index += 1
    return records


def _beacon_inline_field_chunks(text: str, split_comment: bool = False) -> list[tuple[int, str]]:
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


def beacon_field_unknown_keys(texts: Any) -> list[str]:
    unknown: list[str] = []
    for raw in texts or ():
        text = (raw or "").strip()
        if not text:
            continue
        m = _BEACON_FIELD_START_RE.match(text)
        if m and m.group(1) not in BEACON_FIELD_KEYS:
            unknown.append(m.group(1))
    return unknown


def _kept_conflict_warning(existing: Optional[str], requested: str, source: str) -> str:
    return (
        f"existing {source} comment {existing!r} kept; requested comment "
        f"{requested!r} was NOT applied (comment_mode 'fill' never overwrites). "
        "Re-run with comment_mode 'replace' to overwrite, or 'append' to extend."
    )


@dataclass(frozen=True)
class BeaconCommentIntent:

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
class CommentResolution:

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


def resolve_beacon_comment(
    *,
    intent: BeaconCommentIntent,
    note_comment: Optional[str],
    disk_comment: Optional[str],
    beacon_exists_on_disk: bool,
    note_authoritative: bool = False,
    note_comment_present: Optional[bool] = None,
) -> CommentResolution:
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
        return CommentResolution(outcome="fill", resolved=norm_requested or None)

    if intent.mode == "replace":
        if norm_disk and norm_disk == norm_requested:
            return CommentResolution(outcome="idempotent", resolved=norm_disk)
        return CommentResolution(outcome="replace", resolved=norm_requested or None)

    if intent.mode == "append":
        if not norm_disk:
            return CommentResolution(outcome="fill", resolved=norm_requested or None)
        if norm_disk == norm_requested or norm_disk.endswith(" " + norm_requested):
            return CommentResolution(outcome="idempotent", resolved=norm_disk)
        return CommentResolution(
            outcome="append",
            resolved=f"{norm_disk} {norm_requested}",
        )

    raise ValueError(f"unknown comment intent mode {intent.mode!r}")


@dataclass(frozen=True, slots=True)
class SourceSnapshot:

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
        return self.parse_text.replace("\r\n", "\n").replace("\r", "\n")

    def byte_offset(self, line: int, column: int = 0) -> int:
        return self.byte_offsets[line - 1] + len(self.physical_lines[line - 1][:column].encode("utf-8"))

    def normalized_byte_offset(self, offset: int) -> int:
        remaining = offset
        for index, (line, ending) in enumerate(zip(self.physical_lines, self.line_endings)):
            width = len(line.encode("utf-8"))
            if remaining <= width:
                return self.byte_offsets[index] + remaining
            remaining -= width + bool(ending)
        return len(self.raw_bytes)


def source_snapshot_from_bytes(raw_bytes: bytes, canonical_path: str = "") -> SourceSnapshot:
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


def read_source_snapshot(path: str | os.PathLike[str]) -> SourceSnapshot:
    canonical_path = os.path.realpath(os.fspath(path))
    with open(canonical_path, "rb") as stream:
        raw = stream.read()
    return source_snapshot_from_bytes(raw, canonical_path=canonical_path)


def _physical_source_parts(text: str) -> tuple[list[str], list[str]]:
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
    return _physical_source_parts(text)[0]


def _source_line_view(lines: list[str]) -> SourceSnapshot:
    text = "".join(line if line.endswith(("\r", "\n")) else line + "\n" for line in lines)
    return source_snapshot_from_bytes(text.encode("utf-8", errors="replace"))


def _lexical_original_ranges(snapshot: SourceSnapshot, spans: list) -> list[dict]:
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


def python_lexical_evidence(snapshot: SourceSnapshot) -> list[dict]:
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


def js_lexical_evidence(snapshot: SourceSnapshot) -> list[dict]:
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


def source_lexical_evidence(snapshot: SourceSnapshot, language: str) -> list[dict]:
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


def _beacon_comment_rows(snapshot: SourceSnapshot, language: str, start: int, end: int, evidence=(), byte_start=None, byte_end=None) -> list[dict]:
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


def beacon_context_lines(snapshot: SourceSnapshot, records: list[dict], *, release_incomplete_wrappers=False) -> list[str]:
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


def _beacon_record(snapshot: SourceSnapshot, rows: list[dict], index: int, section: dict) -> tuple[dict, int]:
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


def _pair_beacon_records(records: list[dict], snapshot: SourceSnapshot, section: dict) -> None:
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


def scan_beacon_structure(
    snapshot: SourceSnapshot, language: str, *, evidence: list | None = None,
    section_type: str | None = None, section_start: int = 1, section_end: int | None = None,
    section_byte_start: int | None = None, section_byte_end: int | None = None,
) -> list[dict]:
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


def _sql_line_leading(text: str, index: int) -> bool:
    cursor = index - 1
    while cursor >= 0 and text[cursor] in " \t":
        cursor -= 1
    return cursor < 0 or text[cursor] == "\n"


def _sql_dollar_quote(text: str, index: int) -> str | None:
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


def sql_lexical_evidence(snapshot: SourceSnapshot) -> list[dict]:
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
            end = text.find("\n", cursor)
            spans.append((cursor, size if end < 0 else end, "comment"))
            cursor = size if end < 0 else end
        elif text.startswith("/*", cursor):
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
    if index == 0:
        return True
    return text[index - 1] in " \t\n;|&()<>"


def _shell_heredoc_header(text: str, index: int):
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


def shell_lexical_evidence(snapshot: SourceSnapshot) -> list[dict]:
    text = snapshot.normalized_text
    size = len(text)
    spans: list[tuple[int, int, str]] = []
    stack: list[dict] = [{"kind": "code", "end": None}]
    pending: list[tuple[str, bool]] = []
    cursor = 0

    def resume() -> None:
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
                cursor += 3
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
    if text[index] != "-":
        return False
    cursor = index - 1
    while cursor >= 0 and text[cursor] in " \t":
        cursor -= 1
    return cursor < 0 or text[cursor] in "\n-"


def _yaml_node_position(text: str, index: int, flow: bool = False) -> bool:
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


def yaml_lexical_evidence(snapshot: SourceSnapshot) -> list[dict]:
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


@dataclass(frozen=True)
class BeaconEdit:
    start_byte: int
    end_byte: int
    replacement: bytes
    owner: int | str


@dataclass(frozen=True)
class BeaconEditPlan:
    snapshot: SourceSnapshot
    language: str
    edits: tuple[BeaconEdit, ...]
    target_witness: tuple[int, ...] = ()
    intended_state: tuple[dict, ...] = ()
    permitted_structural_effects: tuple[str, ...] = ()
    insertion_slots: tuple[int, ...] = ()
    source_exists: bool = True

    @property
    def source_sha256(self) -> str:
        return self.snapshot.sha256


@dataclass(frozen=True)
class EditOffsetMap:
    edits: tuple[BeaconEdit, ...]

    def map_offset(self, offset: int, *, bias: str = "left") -> int | None:
        delta = 0
        for edit in self.edits:
            a, b, size = edit.start_byte, edit.end_byte, len(edit.replacement)
            if offset < a:
                break
            if a == b == offset:
                return offset + delta + (size if bias == "right" else 0)
            if offset == a:
                return a + delta
            if a < offset < b:
                return None
            delta += size - (b - a)
        return offset + delta

    def map_range(self, start: int, end: int) -> tuple[int, int] | None:
        a = self.map_offset(start, bias="right")
        b = self.map_offset(end, bias="left")
        return (a, b) if a is not None and b is not None else None


def source_line_edit(
    snapshot: SourceSnapshot, start: int, stop: int, rows: list[str], owner: int | str,
) -> BeaconEdit:
    endings = snapshot.line_endings
    local = endings[start:stop]
    neighbour = (*endings[start:start + 1], *endings[max(0, start - 1):start])
    ending = next((s for s in (*local, *neighbour, *endings) if s), "\n")
    a, b = snapshot.byte_offsets[start], snapshot.byte_offsets[stop]
    text = ending.join(rows)
    if rows:
        at_eof = b == len(snapshot.raw_bytes)
        terminated = bool(endings and endings[-1])
        if not at_eof or terminated:
            text += ending
        if start == stop == len(snapshot.physical_lines) and endings and not terminated:
            text = ending + text
    return BeaconEdit(a, b, text.encode("utf-8"), owner)


def _scan_edit_snapshot(snapshot: SourceSnapshot, language: str) -> list[dict]:
    return scan_beacon_structure(snapshot, language, evidence=source_lexical_evidence(snapshot, language))


def _metadata_range(record: dict) -> tuple[int, int]:
    ranges = record.get("write_ranges")
    return (ranges[0][0], ranges[-1][1]) if ranges else (record["opener_range"][0], record["read_end"])


def _without_metadata(raw: bytes, interval: tuple[int, int], records: list[dict], extra_ranges=()) -> bytes:
    a, b = interval
    pieces, cursor = [], a
    for left, right in sorted([*(_metadata_range(r) for r in records), *extra_ranges]):
        left, right = max(a, left), min(b, right)
        if right <= cursor or left >= b:
            continue
        pieces.append(raw[cursor:max(cursor, left)])
        cursor = max(cursor, right)
    pieces.append(raw[cursor:b])
    return b"".join(pieces)


def _following_source(snapshot: SourceSnapshot, record: dict, records: list[dict]) -> tuple[int, int] | None:
    cursor = _metadata_range(record)[1]
    raw = snapshot.raw_bytes
    end = record["section"].get("end_byte", snapshot.byte_offsets[record["section"]["end_line"]])
    ranges = sorted(_metadata_range(r) for r in records)
    while cursor < end:
        containing = next((b for a, b in ranges if a <= cursor < b), None)
        if containing is not None:
            cursor = containing
        elif raw[cursor] in b" \t\r\n":
            cursor += 1
        else:
            return cursor, cursor + 1
    return None


def _proof_failure(reason: str, expected, actual) -> dict:
    return {"success": False, "error_kind": "plan_mismatch", "error": reason,
            "evidence": {"expected": expected, "actual": actual}}


def verify_beacon_edit_plan(
    plan: BeaconEditPlan, *, scanner: Callable | None = None,
) -> dict:
    raw = plan.snapshot.raw_bytes
    edits = tuple(sorted(plan.edits, key=lambda e: (e.start_byte, e.end_byte)))
    previous = None
    for edit in edits:
        if not 0 <= edit.start_byte <= edit.end_byte <= len(raw):
            return _proof_failure("edit range outside snapshot", [0, len(raw)], [edit.start_byte, edit.end_byte])
        if previous and (edit.start_byte < previous.end_byte or edit.start_byte == previous.start_byte):
            return _proof_failure("overlapping edits", "nonoverlapping byte intervals", [previous.start_byte, edit.start_byte])
        if plan.snapshot.had_bom and edit.start_byte < 3:
            return _proof_failure("edit includes file BOM", "BOM at byte zero untouched", edit.start_byte)
        previous = edit
    scan = scanner or _scan_edit_snapshot
    before = scan(plan.snapshot, plan.language)
    owners = {r["opener_range"][0]: r for r in before}
    document = "whole_document" in plan.permitted_structural_effects
    for edit in edits:
        if document and edit.owner == "document":
            continue
        record = owners.get(edit.owner)
        allowed = []
        if record and edit.owner in plan.target_witness:
            allowed = list(record.get("write_ranges") or record.get("field_ranges") or ())
        owned = any(a <= edit.start_byte <= edit.end_byte <= b for a, b in allowed)
        inserted = edit.start_byte == edit.end_byte and edit.start_byte in plan.insertion_slots
        if not owned and not inserted:
            return _proof_failure("edit crosses unowned source bytes", allowed or list(plan.insertion_slots),
                                  [edit.start_byte, edit.end_byte, edit.owner])
    candidate = raw
    for edit in reversed(edits):
        candidate = candidate[:edit.start_byte] + edit.replacement + candidate[edit.end_byte:]
    offsets = EditOffsetMap(edits)
    cursor, new_cursor = 0, 0
    for edit in edits:
        count = edit.start_byte - cursor
        if raw[cursor:edit.start_byte] != candidate[new_cursor:new_cursor + count]:
            return _proof_failure("untouched bytes differ", [cursor, edit.start_byte], [new_cursor, new_cursor + count])
        cursor = edit.end_byte
        new_cursor += count + len(edit.replacement)
    if raw[cursor:] != candidate[new_cursor:]:
        return _proof_failure("untouched tail differs", raw[cursor:].hex(), candidate[new_cursor:].hex())
    try:
        after_snapshot = source_snapshot_from_bytes(candidate, plan.snapshot.canonical_path)
    except UnicodeDecodeError as error:
        return _proof_failure("candidate is not UTF-8 source", "valid UTF-8 replacement",
                              {"start": error.start, "end": error.end, "reason": error.reason})
    after = scan(after_snapshot, plan.language)
    effects = []
    if not document:
        replacement_ranges = []
        for edit in edits:
            start = offsets.map_offset(edit.start_byte, bias="left")
            replacement_ranges.append((start, start + len(edit.replacement)))
            introduced_source = _without_metadata(candidate, (start, start + len(edit.replacement)), after)
            if introduced_source.strip(b" \t\r\n"):
                return _proof_failure("replacement introduces non-metadata source", "metadata or separator whitespace", introduced_source.hex())
        by_position = {r["opener_range"][0]: r for r in after}
        for record in before:
            owner = record["opener_range"][0]
            if owner in plan.target_witness:
                continue
            mapped = offsets.map_offset(owner, bias="right")
            current = by_position.get(mapped)
            interval = _metadata_range(record)
            mapped_range = offsets.map_range(*interval)
            if not mapped_range or raw[interval[0]:interval[1]] != candidate[mapped_range[0]:mapped_range[1]]:
                return _proof_failure("untouched block bytes changed", interval, mapped_range)
            if current is None:
                return _proof_failure("untouched block lost its identity", record["fields"], None)
            valid = record["complete"] and not record["id_conflicting"] and record["literal_certainty"] != "uncertain"
            if valid:
                expected = (record["marker"], record["fields"], record["section"]["type"], record["complete"])
                actual = (current["marker"], current["fields"], current["section"]["type"], current["complete"])
                if expected != actual:
                    return _proof_failure("untouched block identity changed", expected, actual)
                if record["pair_state"] in {"closed", "matched"} and current["pair_state"] != record["pair_state"]:
                    return _proof_failure("untouched block lost its span attachment", record["pair_state"], current["pair_state"])
                if record["marker"] == "close" and record["pair_state"] == "matched":
                    opening = next(r for r in before if r["line"] == record["opener_line"] and r["marker"] == "open")
                    attached = next(r for r in after if r["line"] == current["opener_line"] and r["marker"] == "open")
                    old_owner, new_owner = opening["opener_range"][0], attached["opener_range"][0]
                    mapped_owner = offsets.map_offset(old_owner, bias="right")
                    selected_replacement = any(
                        e.owner == old_owner and offsets.map_offset(e.start_byte, bias="left") <= new_owner <
                        offsets.map_offset(e.start_byte, bias="left") + len(e.replacement) for e in edits
                    )
                    if mapped_owner != new_owner and not selected_replacement:
                        return _proof_failure("untouched close attached to a different opener", mapped_owner, new_owner)
                if record["marker"] == "open" and record["fields"].get("kind") == "ast":
                    old_anchor = _following_source(plan.snapshot, record, before)
                    expected_anchor = offsets.map_range(*old_anchor) if old_anchor is not None else None
                    actual_anchor = _following_source(after_snapshot, current, after)
                    if expected_anchor != actual_anchor:
                        return _proof_failure("untouched block physical attachment changed", expected_anchor, actual_anchor)
                if record.get("inner_range") is not None:
                    old_body = _without_metadata(raw, record["inner_range"], before)
                    new_body = (_without_metadata(candidate, current["inner_range"], after, replacement_ranges)
                                if current.get("inner_range") is not None else None)
                    if old_body != new_body:
                        return _proof_failure("untouched span coverage changed", old_body.hex(), None if new_body is None else new_body.hex())
            else:
                for key in ("id", "kind"):
                    if record["fields"].get(key) != current["fields"].get(key):
                        return _proof_failure("damaged neighbour lost its identity", record["fields"], current["fields"])
            if (record["complete"], record["pair_state"], record["terminated_by"]) != (
                current["complete"], current["pair_state"], current["terminated_by"]
            ):
                effects.append({"owner": owner, "effect": "neighbour_interpretation_changed",
                                "before": {k: record[k] for k in ("complete", "pair_state", "terminated_by")},
                                "after": {k: current[k] for k in ("complete", "pair_state", "terminated_by")}})
        for intent in plan.intended_state:
            owner = intent["owner"]
            owned_edits = [e for e in edits if e.owner == owner]
            regions = []
            for edit in owned_edits:
                start = offsets.map_offset(edit.start_byte, bias="left")
                regions.append((start, start + len(edit.replacement)))
            matches = [r for r in after if any(a <= r["opener_range"][0] < b for a, b in regions)
                       and r["marker"] == intent.get("marker", "open")]
            if owner in owners:
                mapped = offsets.map_offset(owner, bias="right")
                remaining = by_position.get(mapped)
                opener_replaced = any(e.start_byte <= owner < e.end_byte for e in edits)
                if not opener_replaced and remaining is not None and remaining["marker"] == intent.get("marker", "open") and remaining not in matches:
                    matches.append(remaining)
            expected_fields = intent.get("fields", {})
            if intent["operation"] == "absent":
                ok = not matches
            else:
                ok = len(matches) == 1 and all(matches[0]["fields"].get(k) == v for k, v in expected_fields.items())
                if ok and "complete" in intent:
                    ok = matches[0]["complete"] == intent["complete"]
            if not ok:
                return _proof_failure("intended target state not produced", intent, [r["fields"] for r in matches])
    return {"success": True, "candidate_bytes": candidate, "offset_map": offsets,
            "structural_effects": effects}


def commit_beacon_edit_plan(
    plan: BeaconEditPlan, *, scanner: Callable | None = None,
    before_final_hash: Callable[[], None] | None = None,
) -> dict:
    proof = verify_beacon_edit_plan(plan, scanner=scanner)
    if not proof["success"]:
        return proof
    candidate = proof["candidate_bytes"]
    target = os.path.realpath(plan.snapshot.canonical_path)
    before_hash = plan.source_sha256 if plan.source_exists else None
    after_hash = hashlib.sha256(candidate).hexdigest()
    result = {"success": False, "changed": False, "before_sha256": before_hash,
              "candidate_sha256": after_hash,
              "edited_ranges": [{"start_byte": e.start_byte, "end_byte": e.end_byte,
                                 "replacement_bytes": len(e.replacement), "owner": e.owner} for e in plan.edits],
              "structural_effects": proof["structural_effects"]}
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".beacon_atomic_", dir=os.path.dirname(target) or ".")
        with os.fdopen(fd, "wb") as stream:
            stream.write(candidate)
            stream.flush()
            os.fsync(stream.fileno())
        if plan.source_exists:
            try:
                shutil.copymode(target, tmp_path)
            except FileNotFoundError:
                if os.path.exists(target):
                    raise
        if before_final_hash is not None:
            before_final_hash()
        try:
            with open(target, "rb") as stream:
                actual_hash = hashlib.sha256(stream.read()).hexdigest()
            actual_exists = True
        except FileNotFoundError:
            actual_hash, actual_exists = None, False
        if actual_exists != plan.source_exists or actual_hash != before_hash:
            result.update(error_kind="source_changed_since_plan", error="source_changed_since_plan: final source fingerprint differs; zero bytes written",
                          evidence={"expected": {"exists": plan.source_exists, "sha256": before_hash},
                                    "actual": {"exists": actual_exists, "sha256": actual_hash}})
        else:
            os.replace(tmp_path, target)
            tmp_path = None
            result.update(success=True, changed=True, after_sha256=after_hash)
    except OSError as error:
        result.update(error_kind="disk_write_failed", error=f"disk_write_failed: {error}",
                      evidence={"expected": "whole atomic source publication", "actual": repr(error)})
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError as error:
                result["cleanup_error"] = repr(error)
    return result


def _code(severity, view, authority):
    return {"severity": severity, "view": view, "authority": authority}


BEACON_CHECK_CODES = {
    "file_unreadable": _code("error", "file", "_parse_disk_beacons_for_language / map_codebase (strict UTF-8 read)"),
    "file_missing": _code("warning", "file", "code-map repository file inventory"),
    "mapper_parse_error": _code("error", "mapper", "code-map outline parser diagnostics"),
    "unterminated_block": _code("error", "parser", "parse_*_beacon_blocks"),
    "unknown_kind": _code("error", "parser", "parse_*_beacon_blocks"),
    "missing_id": _code("error", "parser", "parse_*_beacon_blocks"),
    "unknown_field_key": _code("warning", "parser", "parse_beacon_field_lines / beacon_field_unknown_keys"),
    "legacy_field": _code("info", "parser", "parse_beacon_field_lines"),
    "duplicate_id": _code("error", "mapper", "_duplicate_beacon_ids"),
    "block_not_mapped": _code("error", "mapper", "map_codebase + parse_mapped_note"),
    "two_blocks_one_symbol": _code("error", "mapper", "map_codebase + parse_mapped_note"),
    "kind_changed_by_mapper": _code("warning", "mapper", "map_codebase"),
    "no_slice_labels": _code("warning", "mapper", "map_codebase (_canonicalize_slice_labels)"),
    "slice_labels_derived": _code("info", "mapper", "map_codebase vs parse_*_beacon_blocks"),
    "span_without_close": _code("info", "parser", "parse_*_beacon_blocks"),
    "orphan_close": _code("info", "parser", "parse_*_beacon_blocks"),
    "block_end_mismatch": _code("error", "writer", "_beacon_block_extent_for_language vs parse_*_beacon_blocks"),
    "writer_anchor_mismatch": _code("error", "writer", "beacon_attachment_index via _find_anchor_matched_beacons vs map_codebase"),
    "writer_anchor_ambiguous": _code("error", "writer", "beacon_attachment_index via _find_anchor_matched_beacons"),
    "writer_target_ambiguous": _code("error", "writer", "_resolve_anchor_target_line"),
    "comment_rejected": _code("info", "comment", "validate_beacon_comment_text"),
    "empty_comment": _code("info", "comment", "beacon grammar (comment recommended)"),
    "check_failed": _code("error", "file", "check_beacon_file"),
    "noncanonical_terminator": _code("info", "parser", "scan_beacon_structure bracket_range"),
    "missing_wrapper": _code("error", "parser", "scan_beacon_structure wrapper ownership"),
    "mixed_comment_ownership": _code("warning", "parser", "scan_beacon_structure write_ranges"),
    "duplicate_field": _code("warning", "parser", "parse_beacon_field_lines occurrences"),
    "inline_field_ambiguity": _code("warning", "parser", "parse_beacon_field_lines diagnostics"),
    "pair_ambiguous": _code("warning", "parser", "scan_beacon_structure exact pairing"),
    "lexical_uncertainty": _code("warning", "parser", "scan_beacon_structure lexical evidence"),
    "writer_anchor_foreign": _code("error", "writer", "beacon_attachment_index vs mapper projection"),
    "implicit_kind_promoted": _code("info", "mapper", "beacon_attachment_index effective_kind"),
    "id_unlike_symbol": _code("info", "attachment", "beacon_id_resemblance"),
    "vue_capability": _code("info", "writer", "beacon_attachment_index section capability"),
}


BEACON_REPAIR_CATALOGUE = {
    "normalization": {"default": "auto", "resolutions": ["normalize", "keep"]},
    "missing_bracket": {"default": "choice", "resolutions": ["insert_bracket", "replace_metadata", "keep"]},
    "wrapper": {"default": "choice", "resolutions": ["insert_wrapper", "edit_metadata", "delete_metadata", "keep"]},
    "fields": {"default": "choice", "resolutions": ["select_alternative", "set_field", "preserve_opaque", "rename_field", "delete_field", "keep"]},
    "duplicate_id": {"default": "choice", "resolutions": ["assign_ids", "keep"]},
    "stack": {"default": "choice", "resolutions": ["merge", "convert_to_span", "delete_metadata", "keep"]},
    "attachment": {"default": "choice", "resolutions": ["relocate", "create_block", "convert_to_span", "delete_metadata", "keep"]},
    "resemblance": {"default": "choice", "resolutions": ["rename_id", "relocate", "edit_descriptive_field", "keep"]},
    "pair": {"default": "choice", "resolutions": ["pair_close", "retag_close", "add_close", "delete_metadata", "keep"]},
    "module": {"default": "choice", "resolutions": ["convert_to_span", "relocate", "delete_metadata", "keep"]},
    "comment": {"default": "choice", "resolutions": ["replace_comment", "append_comment", "keep"]},
    "lexical": {"default": "choice", "resolutions": ["edit_metadata", "keep"]},
    "capability": {"default": "choice", "resolutions": ["relocate", "edit_metadata", "keep"]},
    "failure": {"default": "manual", "resolutions": ["retry", "keep"]},
}


_CATEGORY = {
    "file_unreadable": "failure", "file_missing": "failure", "check_failed": "failure",
    "mapper_parse_error": "failure", "unterminated_block": "missing_bracket",
    "missing_wrapper": "wrapper", "mixed_comment_ownership": "wrapper",
    "unknown_kind": "fields", "missing_id": "fields", "unknown_field_key": "fields",
    "duplicate_field": "fields", "inline_field_ambiguity": "fields", "legacy_field": "fields",
    "noncanonical_terminator": "normalization", "duplicate_id": "duplicate_id",
    "two_blocks_one_symbol": "stack", "writer_anchor_ambiguous": "stack",
    "block_not_mapped": "module", "kind_changed_by_mapper": "module",
    "writer_anchor_mismatch": "attachment", "writer_anchor_foreign": "attachment",
    "writer_target_ambiguous": "attachment", "block_end_mismatch": "wrapper",
    "no_slice_labels": "fields", "slice_labels_derived": "fields",
    "span_without_close": "pair", "orphan_close": "pair", "pair_ambiguous": "pair",
    "comment_rejected": "comment", "empty_comment": "comment",
    "lexical_uncertainty": "lexical", "implicit_kind_promoted": "module",
    "id_unlike_symbol": "resemblance", "vue_capability": "capability",
}


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_beacon_file(path):
    path = str(path)
    mounted = re.match(r"^/mnt/([a-zA-Z])(?:/|$)(.*)", path)
    if mounted:
        path = mounted[1] + ":/" + mounted[2]
    if re.match(r"^[a-zA-Z]:[/\\]", path) or path.startswith("\\\\"):
        return ntpath.normcase(ntpath.normpath(path)).replace("\\", "/")
    return path


def _semantic_anchor(anchor):
    if not anchor:
        return None
    return {key: anchor.get(key) for key in ("ast_qualname", "md_path", "header")}


def beacon_member_key(member):
    return {"block_hash": member.get("block_hash"),
            "owned_hashes": [r["sha256"] for r in member.get("owned_range_hashes") or []],
            "anchor": _semantic_anchor(member.get("anchor")),
            "section_type": (member.get("section") or {}).get("type")}


def beacon_finding_id(canonical_file, code, kind, members=(), *, file_sha256=None,
                      unreadable_identity=None, anchor=None, ordinal=None):
    keys = sorted((beacon_member_key(m) for m in members), key=_canonical_json)
    payload = {"version": 1, "canonical_file": canonical_beacon_file(canonical_file),
               "code": code, "kind": kind, "members": keys,
               "anchor": _semantic_anchor(anchor)}
    if not keys and not anchor:
        payload["file_identity"] = file_sha256 or {"unreadable": unreadable_identity or "unobserved"}
    if ordinal is not None:
        payload["occurrence_ordinal"] = ordinal
    return "bf1:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _params(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


def _resolution(name, summary, effect, properties=None, required=(), **extra):
    return {"resolution": name, "summary": summary, "effect": effect,
            "params_schema": _params(properties, required), **extra}


def beacon_finding_fix(code, members, detail):
    category = _CATEGORY[code]
    entry = BEACON_REPAIR_CATALOGUE[category]
    klass = entry["default"]
    keep = _resolution("keep", "Keep the observed state.", "No write and no persistent waiver.")
    ranges = []
    for member in members:
        for bounds in (member.get("write_ranges") or []) + (member.get("field_ranges") or []):
            if list(bounds) not in ranges:
                ranges.append(list(bounds))
    member_param = {"type": "integer", "enum": list(range(len(members)))}
    text = {"type": "string"}
    field = {"type": "string", "enum": ["id", "kind", "slice_labels", "comment", "comments", "role", "start_snippet", "end_snippet", "show_span"]}
    named_field = {"unknown_kind": "kind", "missing_id": "id", "no_slice_labels": "slice_labels",
                   "slice_labels_derived": "slice_labels"}.get(code)
    if code == "duplicate_field":
        named_field = detail.get("key")
    if named_field:
        field = {"type": "string", "enum": [named_field]}
    selected = {"member": member_param}
    pair_choice = {"close": {"type": "integer", "minimum": 0},
                   "open_only": {"type": "boolean"}}
    bounded = {**selected, "range": {"enum": ranges}, "text": text}
    target = {"type": "object", "properties": {"ast_qualname": text,
              "md_path": {"type": "array", "items": text}, "header": text},
              "anyOf": [{"required": ["ast_qualname"]}, {"required": ["md_path"]}],
              "additionalProperties": False}
    options = {
        "normalize": _resolution("normalize", "Normalize only the selected metadata syntax.", "Preserve all code, prose and unrequested fields.", selected, ["member"]),
        "insert_bracket": _resolution("insert_bracket", "Insert the missing bracket at a recorded candidate.", "Insert a delimiter; never consume following source.", {**selected, "boundary": {"enum": detail.get("boundaries", [])}}, ["member", "boundary"]),
        "replace_metadata": _resolution("replace_metadata", "Replace explicitly bounded metadata.", "Replace only the chosen metadata bytes.", bounded, ["member", "range", "text"]),
        "insert_wrapper": _resolution("insert_wrapper", "Close the wrapper at the shown metadata boundary.", "Retain ordinary comment text and reveal the recorded headings.", {**selected, "boundary": {"enum": detail.get("boundaries", [])}}, ["member", "boundary"]),
        "edit_metadata": _resolution("edit_metadata", "Rewrite the selected bounded metadata text.", "No source outside the supplied metadata range changes.", bounded, ["member", "range", "text"]),
        "delete_metadata": _resolution("delete_metadata", "Delete only selected metadata, not its body.", "Retain ordinary comments, code and prose.", {**selected, "range": {"enum": ranges}}, ["member", "range"]),
        "select_alternative": _resolution("select_alternative", "Choose a recorded parse alternative.", "Make the selected field interpretation explicit.", {**selected, "alternative": {"enum": detail.get("alternatives", [])}}, ["member", "alternative"]),
        "set_field": _resolution("set_field", "Supply an explicit field value.", "Splice that field alone; preserve opaque text and other occurrences.", {**selected, "field": field, "value": text}, ["member", "field", "value"]),
        "preserve_opaque": _resolution("preserve_opaque", "Keep the selected opaque line literal.", "Preserve its bytes rather than guessing a field value.", {**selected, "range": {"enum": ranges}}, ["member", "range"]),
        "rename_field": _resolution("rename_field", "Rename a selected field key.", "Change the selected key only.", {**selected, "range": {"enum": ranges}, "field": field}, ["member", "range", "field"]),
        "delete_field": _resolution("delete_field", "Delete a selected field occurrence.", "Delete only its recorded metadata range.", {**selected, "range": {"enum": ranges}}, ["member", "range"]),
        "assign_ids": _resolution("assign_ids", "Choose a keeper and new ids for the other members.", "Rename selected metadata and chosen or uniquely paired closes; empty new_id allocates with advisory identity consequences; open_only is explicit, never an automatic keeper.", {"keeper": member_param, "new_ids": {"type": "array", "items": _params({"member": member_param, "new_id": text, **pair_choice}, ["member", "new_id"])}}, ["keeper", "new_ids"]),
        "merge": _resolution("merge", "Choose a survivor and field values; union labels.", "Merge only selected metadata; retain the target source.", {"survivor": member_param, "fields": {"type": "object", "additionalProperties": text}}, ["survivor", "fields"]),
        "convert_to_span": _resolution("convert_to_span", "Make the selected block explicitly a span.", "Change metadata kind only; no FILE-node capability expansion.", selected, ["member"]),
        "relocate": _resolution("relocate", "Relocate metadata to a chosen current target.", "Move metadata only; inspect positional, gap and name evidence first.", {**selected, "target": target}, ["member", "target"]),
        "create_block": _resolution("create_block", "Create a separate block on a current target.", "Retain the observed orphan; insert new metadata only.", {"target": target, "id": text}, ["target", "id"]),
        "rename_id": _resolution("rename_id", "Choose a new id (spelling is advisory).", "Rename metadata and its unique or selected close, or explicitly leave an ambiguous span open; empty new_id allocates and reports identity consequences.", {**selected, "new_id": text, **pair_choice}, ["member", "new_id"]),
        "edit_descriptive_field": _resolution("edit_descriptive_field", "Describe the intended concept instead of renaming it.", "Change the chosen comment/role field only.", {**selected, "field": {"enum": ["comment", "role"]}, "value": text}, ["member", "field", "value"]),
        "pair_close": _resolution("pair_close", "Select a structural pair or an explicit open-only outcome.", "Persist a chosen ambiguous pair with an explicit new_id (empty allocates); no hidden rename, ordinal or edit-distance pairing.", {"opener": member_param, "close": member_param, "open_only": {"type": "boolean"}, "new_id": text}, ["opener"]),
        "retag_close": _resolution("retag_close", "Supply the intended exact close id.", "Change the selected close metadata only.", {**selected, "new_id": text}, ["member", "new_id"]),
        "add_close": _resolution("add_close", "Add an exact close at a supplied valid boundary.", "Insert close metadata without deleting span content.", {**selected, "boundary": {"type": "integer", "minimum": 0, "description": "boundary must be a physical line start within the span section, or EOF, after the opener."}}, ["member", "boundary"]),
        "replace_comment": _resolution("replace_comment", "Replace the comment explicitly.", "Apply comment_mode=replace; independent label work remains valid.", {**selected, "comment": text}, ["member", "comment"], via="tags.ensure where addressable; otherwise shared fix intent"),
        "append_comment": _resolution("append_comment", "Append to the existing comment explicitly.", "Apply comment_mode=append; independent label work remains valid.", {**selected, "comment": text}, ["member", "comment"], via="tags.ensure where addressable; otherwise shared fix intent"),
        "retry": _resolution("retry", "Recover the source/reader and re-list current findings.", "Read-only retry; no structural edit is authorized without parseable evidence."),
    }
    names = list(entry["resolutions"])
    if category == "pair":
        openers = [i for i, m in enumerate(members) if m.get("kind") != "close"]
        closes = [i for i, m in enumerate(members) if m.get("kind") == "close"]
        schema = options["pair_close"]["params_schema"]
        schema["properties"].update(opener={"enum": openers}, close={"enum": closes})
        schema["anyOf"] = [{"required": ["close"]},
                           {"properties": {"open_only": {"const": True}}, "required": ["open_only"]}]
        options["retag_close"]["params_schema"]["properties"]["member"] = {"enum": closes}
        options["add_close"]["params_schema"]["properties"]["member"] = {"enum": openers}
    verdict = detail.get("planner") or {}
    if code == "legacy_field" and verdict.get("status") == "planned":
        klass, names = "auto", ["normalize", "keep"]
    if code == "duplicate_field" and detail.get("identical") and all(m.get("complete") for m in members):
        klass, names = "auto", ["normalize", "keep"]
    if code == "unterminated_block" and members and all(m.get("repair_unambiguous") for m in members):
        klass = "auto"
    if category == "stack" and verdict.get("status") == "planned":
        klass = "auto"
        options["merge"]["params"] = {"survivor": len(members) - 1,
                                      "fields": {key: value for key, value in
                                          {"slice_labels": verdict.get("labels"), "comment": verdict.get("comment")}.items()
                                          if value is not None}}
    safe = bool(ranges or any(m.get("repair_candidates") for m in members))
    if not safe and category != "failure":
        klass, names = "manual", ["keep"]
    resolutions = []
    for name in names:
        if name == "keep":
            resolutions.append(keep)
            continue
        option = options[name]
        props = option["params_schema"]["properties"]
        field_only = name in {"rename_field", "delete_field"}
        if field_only:
            props["range"] = {"enum": [list(r) for member in members for r in member.get("field_ranges") or []]}
        if any("enum" in value and not value["enum"] for key, value in props.items()
               if key != "close" or name != "pair_close"):
            continue
        if "member" in props and "range" in props:
            option["params_schema"]["allOf"] = [
                {"if": {"properties": {"member": {"const": index}}, "required": ["member"]},
                 "then": {"properties": {"range": {"enum": [list(r) for r in
                     ([] if field_only else member.get("write_ranges") or []) + (member.get("field_ranges") or [])]}}}}
                for index, member in enumerate(members)]
        resolutions.append(option)
    if all(r["resolution"] == "keep" for r in resolutions):
        klass = "manual"
    result = {"class": klass, "resolutions": resolutions}
    if klass == "auto":
        result["recommended"] = next(r["resolution"] for r in resolutions if r["resolution"] != "keep")
    if klass == "manual":
        result["instructions"] = ("Inspect the file and the reported exception; restore/read UTF-8 source and retry. "
                                  "No parseable metadata extent is available for structural repair."
                                  if category == "failure" else
                                  "Supply current parseable metadata field/extent evidence, or edit the intended metadata in source manually; "
                                  "the recorded evidence does not establish a safe metadata range.")
    return result


def make_beacon_finding(file, line, code, beacon_id, message, detail=None, *,
                        snapshot=None, canonical_file=None, file_sha256=None,
                        members=(), anchor=None, producer="beacon.check", kind=None,
                        unreadable_identity=None):
    members = copy.deepcopy(list(members))
    detail = copy.deepcopy(detail or {})
    canonical = canonical_beacon_file(snapshot.canonical_path if snapshot is not None else canonical_file or file)
    sha = snapshot.sha256 if snapshot is not None else file_sha256
    kind = kind or code
    observed = {"canonical_file": canonical, "file_sha256": sha,
                "block_ranges": [m.get("read_range") for m in members],
                "field_ranges": [r for m in members for r in m.get("field_ranges") or []],
                "member_fingerprints": [beacon_member_key(m) for m in members],
                "identity_ambiguous": False}
    equal_members = {}
    for index, member in enumerate(members):
        equal_members.setdefault(_canonical_json(beacon_member_key(member)), []).append(index)
    ambiguous_members = [indices for indices in equal_members.values() if len(indices) > 1]
    if ambiguous_members:
        observed.update(identity_ambiguous=True, identical_member_groups=ambiguous_members)
    if anchor:
        observed["anchor"] = copy.deepcopy(anchor)
    if unreadable_identity is not None:
        observed["unreadable_identity"] = copy.deepcopy(unreadable_identity)
    return {"file": file, "line": int(line or 0), "code": code, **BEACON_CHECK_CODES[code],
            "beacon_id": beacon_id if beacon_id not in ("", None) else None,
            "message": message, "detail": detail, "kind": kind, "producer": producer,
            "observed": observed, "members": members, "fix": beacon_finding_fix(code, members, detail),
            "finding_id": beacon_finding_id(canonical, code, kind, members, file_sha256=sha,
                                            unreadable_identity=unreadable_identity, anchor=anchor)}


def finalize_beacon_findings(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row["finding_id"], []).append(row)
    for group in groups.values():
        if len(group) < 2:
            continue
        for ordinal, row in enumerate(group):
            witness = row["observed"]
            witness.update(identity_ambiguous=True, occurrence_ordinal=ordinal,
                           identical_occurrences=len(group))
            row["finding_id"] = beacon_finding_id(
                witness["canonical_file"], row["code"], row["kind"], row["members"],
                file_sha256=witness["file_sha256"], unreadable_identity=witness.get("unreadable_identity"),
                anchor=witness.get("anchor"), ordinal=ordinal)
    return rows


def _bid(value):
    return str(value or "").strip()


def _mapped_blocks(blocks, mapped_nodes, language):
    available = [(node, rec) for node in mapped_nodes for rec in node["records"]]
    result = {}
    for block in blocks:
        beacon = block.get("beacon")
        if not beacon:
            continue
        anchor, kind = block.get("anchor"), block["effective_kind"]
        candidates = [(i, node, rec) for i, (node, rec) in enumerate(available)
                      if _bid(rec.get("id")) == _bid(beacon.get("id")) and
                      ((anchor and node["line"] == anchor["outline_line"] and rec.get("kind") == "ast") or
                       (not anchor and rec.get("kind") == "span" and
                        (kind == "span" or language == "markdown") and
                        (not node.get("section_type") or node["section_type"] == block["record"]["section"]["type"]))) ]
        if not candidates:
            continue
        exact = [c for c in candidates if str(c[2].get("comment") or "").strip() == str(block.get("mapper_comment", beacon.get("comment")) or "").strip()
                 and str(c[2].get("slice_labels") or "") == str(block.get("mapper_labels", beacon.get("slice_labels")) or "")]
        index, node, rec = (exact or candidates)[0]
        result[block["owner"]] = {"node": node, "record": rec}
        available.pop(index)
    return result


def _finding_outline_nodes(roots):
    for node in roots:
        yield node
        yield from _finding_outline_nodes(node.get("children") or [])


def _finding_node_line(node):
    for key in ("orig_lineno_start_unused", "_md_heading_line"):
        value = node.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 0


def beacon_snapshot_analysis_inputs(snapshot, language, source, *, outline, parse_note,
                                   mapped_roots=None, mapper_errors=()):
    if language == "vue":
        try:
            sections = [s for s, _r in source._vue_section_records(snapshot)]
        except Exception as error:
            return {"section_error": f"{type(error).__name__}: {error}"}
    else:
        sections = None
    _lines, beacons = source._parse_snapshot_beacons_for_language(snapshot, language)
    records = source._beacon_edit_records(snapshot, language)
    structures = {"language": language, "records": records, "beacons": beacons, "sections": sections}
    index = source.beacon_attachment_index(snapshot, outline, structures)
    for block in index["blocks"]:
        beacon = block.get("beacon")
        if beacon:
            bid = (beacon.get("id") or "").strip()
            role = (beacon.get("role") or "").strip() or (bid.split("@", 1)[0] if "@" in bid else "")
            block["mapper_labels"] = source._canonicalize_slice_labels(beacon.get("slice_labels") or "", role)
            block["mapper_comment"] = source.strip_leading_decoration_symbols(beacon.get("comment"))
    for anchor in index["anchors"]:
        anchor["descriptor"] = source._beacon_anchor_descriptor(anchor)
    members = {b["owner"]: source._hygiene_member(snapshot, b) for b in index["blocks"]}
    for record in records:
        owner = record["opener_range"][0]
        if owner not in members:
            members[owner] = source._hygiene_member(snapshot, {
                "record": record, "owner": owner, "effective_kind": "close",
                "anchor": None, "gap": {},
            })
    mapped_nodes = []
    for node in _finding_outline_nodes(outline if mapped_roots is None else mapped_roots):
        recs = [payload for kind, payload in parse_note(str(node.get("note") or "")) if kind == "beacon"]
        if recs:
            note = str(node.get("note") or "")
            section = ("template_element" if "BEACON (VUE TEMPLATE" in note else
                       "style_element" if "BEACON (VUE STYLE" in note else "script_element") if language == "vue" else None
            mapped_nodes.append({"line": _finding_node_line(node), "symbol": node.get("ast_qualname"),
                                 "records": recs, "section_type": section})
    resemblance = [e for block in index["blocks"] for e in source.beacon_id_resemblance(
        snapshot, block, block["anchor"], index["anchors"], index["blocks"])]
    duplicate_ids = source._duplicate_beacon_ids([
        {"id": r["fields"].get("id")} for r in records if r["marker"] == "open"])
    taken = {r["fields"].get("id") for r in records if r["fields"].get("id")}
    id_suggestions = {}
    for bid in duplicate_ids:
        suggestions = []
        for block in index["blocks"]:
            if (block["record"]["fields"].get("id") or "").strip() == bid:
                new_id = source.allocate_beacon_id(taken, base_id=bid)
                taken.add(new_id)
                suggestions.append({"opener_start_byte": block["owner"], "new_id": new_id})
        id_suggestions[bid] = suggestions
    return {"structures": structures, "outline": outline, "attachment_index": index, "members": members,
            "duplicate_ids": duplicate_ids, "id_suggestions": id_suggestions,
            "mapped_nodes": mapped_nodes, "mapper_errors": list(mapper_errors),
            "hygiene": source.plan_beacon_hygiene(snapshot, structures, index),
            "aliases": source.plan_beacon_comment_aliases(snapshot, language), "resemblance": resemblance}


def analyze_beacon_snapshot(snapshot, structures, outline, attachment_index, *,
                            members, duplicate_ids, mapped_nodes=(), mapper_errors=(),
                            hygiene=None, aliases=None, resemblance=(),
                            id_suggestions=None,
                            display_path=None, producer="beacon.check", writer_authority=None):
    rows = []
    file = display_path or snapshot.canonical_path
    records, blocks = structures["records"], attachment_index["blocks"]
    mapped = _mapped_blocks(blocks, mapped_nodes, structures["language"])
    block_by_line = {b["record"]["line"]: b for b in blocks}
    groups = list((hygiene or {}).get("applied_groups", [])) + list((hygiene or {}).get("skipped_groups", []))
    alias_rows = list((aliases or {}).get("applied_aliases", [])) + list((aliases or {}).get("skipped_aliases", []))

    def emit(code, record=None, detail=None, message=None, operands=None, anchor=None, line=None, bid=None, kind=None):
        detail = detail or {}
        if operands is None:
            operands = [members[record["opener_range"][0]]] if record is not None else []
        row = make_beacon_finding(file, record["line"] if record and line is None else line or 0,
                                  code, record["fields"].get("id") if record and bid is None else bid,
                                  message or code.replace("_", " ").capitalize() + ".", detail,
                                  snapshot=snapshot, members=operands, anchor=anchor, producer=producer, kind=kind)
        if writer_authority and row["view"] == "writer":
            row["authority"] = writer_authority + ": " + row["authority"]
        rows.append(row)

    for error in mapper_errors:
        emit("mapper_parse_error", detail=error,
             message="The outline builder failed for this " + ("section" if error.get("section") else "file") +
                     "; inspect the recorded exception and retry; independent files/sections remain checkable.")

    for record in records:
        fields, ds = record["fields"], record["diagnostics"]
        owner, bid = record["opener_range"][0], _bid(fields.get("id"))
        boundaries = sorted(set(record.get("repair_candidates", {}).values()))
        structural = {"boundaries": boundaries, "terminated_by": record["terminated_by"],
                      "offending_line": record.get("offending_line")}
        if not record["bracket_range"]:
            emit("unterminated_block", record, structural,
                 "This record has no closing bracket; its own bounded fields remain visible and later independent blocks are still analyzed.")
        if record["wrapper_open_range"] and not record["wrapper_range"]:
            a = record["read_end_line"]
            close = record.get("wrapper_close_range")
            b = (bisect.bisect_right(snapshot.byte_offsets, close[1]) if close else
                 record["section"]["end_line"])
            retained = list(snapshot.physical_lines[a:b])
            wrapper_boundaries = [boundary for boundary in boundaries
                                  if record["bracket_range"] and boundary >= record["bracket_range"][1]]
            emit("missing_wrapper", record, {**structural, "boundaries": wrapper_boundaries, "retained_text": retained,
                 "hidden_or_revealed_headings": [target["header"] for target in attachment_index["anchors"]
                                                if a < target["line"] <= b],
                 "diagnostics": ds},
                 "The wrapper has no owned close at this metadata boundary; later prose, headings and arrows are not a beacon extent.")
        elif record["wrapper_range"] and not record["wrapper_dedicated"]:
            a, b = record["wrapper_range"]
            emit("mixed_comment_ownership", record, {**structural,
                 "ordinary_comment_before": snapshot.raw_bytes[a:record["opener_range"][0]].decode("utf-8"),
                 "ordinary_comment_after": snapshot.raw_bytes[record["read_end"]:b].decode("utf-8")},
                 "Metadata shares an ordinary comment; only the recorded metadata subranges may change, not the surrounding text.")
        if record["literal_certainty"] != "comment":
            emit("lexical_uncertainty", record, {"diagnostics": ds},
                 "Lexical ownership is uncertain for this record; select bounded metadata evidence or edit that metadata manually.")
        if record["complete"] and not record["wrapper_open_range"]:
            tail = snapshot.raw_bytes[record["bracket_range"][1]:snapshot.byte_offset(record["end_line"], len(snapshot.physical_lines[record["end_line"] - 1]))]
            if tail and not tail.strip(b" ,\t"):
                emit("noncanonical_terminator", record, {},
                     "A complete comma/space terminator is accepted; canonical normalization changes metadata only.")
        for diagnostic in ds:
            code = diagnostic["code"]
            if code in {"duplicate_field", "inline_field_ambiguity"}:
                emit(code, record, {**diagnostic, "occurrences": record["occurrences"],
                     "raw": record["field_texts"], "identical": code == "duplicate_field" and not diagnostic["conflicting"],
                     "alternatives": diagnostic.get("canonical_multiline", diagnostic.get("alternatives", []))},
                     "This metadata field has repeated or ambiguous interpretations; choose its value explicitly and preserve independent fields.",
                     kind=code + ":" + diagnostic.get("key", "inline"))
        if record["pair_state"] == "ambiguous":
            peer_lines = set(record["pair_candidates"])
            peers = [members[r["opener_range"][0]] for r in records if r["line"] in peer_lines]
            emit("pair_ambiguous", record, {"pair_candidates": record["pair_candidates"], "exact_id": bid},
                 "Several exact-id span operands are possible; select a physical pair, never an edit-distance guess.",
                 operands=[members[owner]] + peers)
        if record["marker"] == "close":
            if record["pair_state"] != "matched":
                candidates = [members[r["opener_range"][0]] for r in records if r["marker"] == "open" and
                              r["section"] == record["section"] and not r["closed"] and
                              (r["fields"].get("kind") or "span").strip().lower() == "span"]
                emit("orphan_close", record, {"exact_id": bid, "opener_candidates": candidates},
                     "This structural close matches no unique eligible exact-id span opener; ordinary marker mentions are not close candidates.",
                     operands=[members[owner]] + candidates)
            continue
        kind = (fields.get("kind") or "span").strip().lower() or "span"
        if kind not in {"ast", "span"}:
            emit("unknown_kind", record, {"kind_raw": fields.get("kind")},
                 f"Block {bid or '(no id)'} has kind={fields.get('kind')!r}; the parser skips this block, not its neighbours.")
        if not bid:
            emit("missing_id", record, {},
                 "This block has no id; give it an explicit id to make it addressable (Markdown drops an id-less block).")
        legacy = [key for key in ("comments", "role") if key in fields]
        if legacy:
            planner = next((a for a in alias_rows if a.get("owner") == owner), {})
            detail = {"keys": legacy}
            if planner:
                detail["planner"] = {k: v for k, v in planner.items() if k not in {"members", "candidates"}}
            emit("legacy_field", record, detail,
                 "Legacy fields remain readable: comments aliases comment; deprecated role is preserved unless explicitly changed.")
        unknown = list(dict.fromkeys(d["key"] for d in ds if d["code"] == "unknown_field"))
        if unknown:
            emit("unknown_field_key", record, {"keys": unknown},
                 "Unknown field keys remain opaque; correct a selected key or explicitly retain its text without changing independent fields.")
        block = block_by_line.get(record["line"])
        beacon = block.get("beacon") if block else None
        if not beacon:
            continue
        comment = beacon.get("comment")
        if comment is None or not str(comment).strip():
            emit("empty_comment", record, {"existing": comment, "requested": None, "note": None},
                 "This block has no comment; labels still work, and a description can be supplied explicitly.")
        else:
            try:
                validate_beacon_comment_text(comment)
            except ValueError as error:
                emit("comment_rejected", record, {"existing": comment, "requested": None, "note": None, "exception": str(error)},
                     f"The comment would be refused as new input ({error}); existing disk text is kept and label work may proceed.")
        projection = mapped.get(owner)
        if not projection:
            if not any(not e.get("section") for e in mapper_errors):
                emit("block_not_mapped", record, {"gap": block["gap"], "next_code_line": block["next_code_line"],
                     "candidates": [a.get("descriptor") for a in attachment_index["anchors"]]},
                     "This physical block reaches no mapped node, even if another copy of its id maps; relocate or convert metadata, never add FILE capability implicitly.")
        else:
            rec = projection["record"]
            if fields.get("kind") and kind != rec.get("kind"):
                emit("kind_changed_by_mapper", record, {"block_kind": kind, "mapped_kind": rec.get("kind")},
                     f"The block declares {kind} but maps as {rec.get('kind')}; choose span conversion or a current target rather than silently relocating it.")
            labels = str(rec.get("slice_labels") or "").strip()
            if not labels:
                emit("no_slice_labels", record, {}, f"Block {bid or '(no id)'} maps with no slice label, so it is in no slice.")
            elif not str(fields.get("slice_labels") or "").strip():
                emit("slice_labels_derived", record, {"slice_labels": labels},
                     "The mapper derived labels from the role/id prefix; supply explicit slice_labels to make the intended slice unambiguous.")
        effective_kind = projection["record"].get("kind") if projection else block["effective_kind"]
        if bid and effective_kind == "span" and not record["closed"] and not fields.get("start_snippet"):
            close_candidates = [members[r["opener_range"][0]] for r in records if r["marker"] == "close" and r["section"] == record["section"]]
            emit("span_without_close", record, {"exact_id": bid, "section": record["section"],
                 "close_candidates": close_candidates},
                 "This legal open-only span runs to its file/section end; keep it or explicitly choose an exact close.",
                 operands=[members[owner]] + close_candidates)
        if not fields.get("kind") and block["effective_kind"] == "ast":
            emit("implicit_kind_promoted", record, {},
                 "The shared attachment promotes this implicit block to AST; make the kind explicit or retain that interpretation.")
        if record["end_line"] is not None and record["line_extent"] is None:
            emit("block_end_mismatch", record, {"parser_end_line": record["end_line"], "writer_end_line": None},
                 "The bounded record has no whole-line writer extent; field/subrange work may still proceed without rewriting ordinary source.")
        if structures["language"] == "vue" and record["section"].get("type") != "script_element":
            emit("vue_capability", record, {"section": record["section"], "tags_delta_supported": False,
                 "legacy_absolute_supported": True},
                 "This actual Vue section is not script: tags deltas are script-only; legacy section metadata actions remain available.")

    for bid in duplicate_ids:
        copies = [b for b in blocks if _bid(b["record"]["fields"].get("id")) == bid]
        operands = [members[b["owner"]] for b in copies]
        lines = [b["record"]["line"] for b in copies]
        emit("duplicate_id", detail={"lines": lines, "mapped_records": sum(b["owner"] in mapped for b in copies),
              "suggested_ids": (id_suggestions or {}).get(bid, []),
              "close_candidates": [members[r["opener_range"][0]] for r in records
                                   if r["marker"] == "close" and any(
                                       r["section"] == b["record"]["section"] and
                                       (r["line"] == b["record"].get("close_line") or
                                        r["line"] in b["record"].get("pair_candidates", [])) for b in copies)]},
             message=f"Id {bid} has {len(copies)} physical copies; independently anchored copies remain writable, and an id-only request needs a member choice.",
             operands=operands, line=lines[1] if len(lines) > 1 else 0, bid=bid)

    for anchor in attachment_index["anchors"]:
        attached = [b for b in blocks if b.get("anchor") is anchor]
        operands = [members[b["owner"]] for b in attached]
        witness = next((m["anchor"] for m in operands if m.get("anchor")), None)
        if witness is None:
            witness = anchor.get("descriptor")
        symbol = anchor.get("ast_qualname") or anchor["header"]
        twins = [a for a in attachment_index["anchors"] if a.get("ast_qualname") and a["ast_qualname"] == anchor.get("ast_qualname")]
        if len(twins) > 1:
            emit("writer_target_ambiguous", detail={"symbol": symbol, "candidates": [a.get("descriptor") for a in twins]}, operands=operands,
                 anchor=witness, line=anchor["line"],
                 message="This qualified name has several current targets; an independent physical witness can still select a member.")
        recs = next((n["records"] for n in mapped_nodes if n["line"] == anchor["outline_line"] and n.get("symbol") == anchor.get("ast_qualname")), [])
        if len(recs) > 1:
            planner = next((g for g in groups if any(m.get("opener_start_byte") in {b["owner"] for b in attached} for m in g["members"])), {})
            emit("two_blocks_one_symbol", detail={"ids": [_bid(r.get("id")) for r in recs],
                 "planner": {k: v for k, v in planner.items() if k not in {"members", "candidates"}}},
                 operands=operands, anchor=witness, line=anchor["line"],
                 message="Several physical blocks map to this target; only a shared-planner compatible same-id merge is automatic, otherwise choose the survivor/fields.")
        complete = [b for b in attached if b["record"]["line_extent"]]
        if len(complete) > 1:
            planner = next((g for g in groups if any(m.get("opener_start_byte") in {b["owner"] for b in attached} for m in g["members"])), {})
            emit("writer_anchor_ambiguous", detail={"ids": [b["record"]["fields"].get("id") for b in complete],
                 "planner": {k: v for k, v in planner.items() if k not in {"members", "candidates"}}}, operands=operands,
                 anchor=witness, line=anchor["line"],
                 message="Several complete blocks attach to this target; physical members remain selectable and incompatible merges need explicit choices.")
        for block in attached:
            record = block["record"]
            if block["owner"] not in mapped:
                emit("writer_anchor_foreign", record, {"target_line": anchor["line"], "symbol": symbol},
                     "The attachment index sees metadata on this target that the mapper does not carry; inspect the physical member and capability separately.")
            elif record["line_extent"] is None:
                emit("writer_anchor_mismatch", record, {"target_line": anchor["line"], "symbol": symbol, "writer_extent": None},
                     "The mapper attaches this bounded block but no whole-line writer tuple exists; inspect field/subrange repair instead of treating it as permission to create a second block.")
    for evidence in resemblance:
        block = block_by_line.get(evidence["line"])
        if block:
            emit("id_unlike_symbol", block["record"], evidence,
                 "This id resembles a different symbol or enclosing path; this is advisory only: keep, rename, relocate or edit its description explicitly.")
    return finalize_beacon_findings(rows)


EMOJI_CLASS = "📦"


EMOJI_FUNC = "ƒ"


EMOJI_ASYNC = "⚡"


EMOJI_CONST = "💎"


TRIDENT = "🔱"


def _beacon_block_diagnostic(
    line: int,
    end_line: Optional[int],
    outcome: str,
    fields: dict[str, str],
    field_texts: list[str],
    structure: dict | None = None,
) -> dict[str, Any]:
    result = {
        "record": "block",
        "line": line,
        "end_line": end_line,
        "outcome": outcome,
        "kind_raw": fields.get("kind"),
        "id": fields.get("id"),
        "field_keys": sorted(fields),
        "unknown_keys": beacon_field_unknown_keys(field_texts),
    }
    if structure is not None:
        result.update({key: structure[key] for key in (
            "terminated_by", "offending_line", "complete", "read_end_line",
            "pair_state", "closed", "pair_candidates", "diagnostics",
        )})
    return result


def _parse_structural_beacons(language: str, lines: list[str], diagnostics: list | None = None, *, snapshot=None, records=None) -> list[dict]:
    if records is None:
        snapshot = snapshot if snapshot is not None else _source_line_view(lines)
        records = _scan_source_beacons(snapshot, language)
    beacons = []
    for record in records:
        if record["marker"] != "open":
            continue
        fields = record["fields"]
        kind_raw = fields.get("kind")
        kind = (kind_raw or "span").strip().lower() or "span"
        outcome = "kept" if kind in {"ast", "span"} else "skipped_unknown_kind"
        if language == "markdown" and not fields.get("id"):
            outcome = "skipped_empty_id"
        if diagnostics is not None:
            diagnostics.append(_beacon_block_diagnostic(
                record["line"], record["end_line"], outcome, fields, record["field_texts"], record,
            ))
        if outcome != "kept":
            continue
        show_span, explicit = _parse_show_span_fields(fields)
        beacon = {key: fields.get(key) for key in ("id", "role", "slice_labels", "start_snippet", "end_snippet")}
        beacon.update({"kind": kind,
                       "comment": fields.get("comment") if fields.get("comment") not in (None, "") else fields.get("comments"),
                       "comment_line": record["line"], "show_span": show_span, "show_span_explicit": explicit})
        if language in {"python", "js_ts", "markdown"}:
            beacon["kind_explicit"] = bool(kind_raw)
        if not record["complete"]:
            beacon["incomplete"] = True
        if record["id_conflicting"]:
            beacon["id_conflicting"] = True
        if language == "markdown":
            beacon["span_lineno_start"] = beacon["span_lineno_end"] = None
            _markdown_beacon_snippet_range(beacon, lines, records)
        if kind == "span" and record["closed"] and beacon.get("span_lineno_start") is None:
            beacon["span_lineno_start"], beacon["span_lineno_end"] = record["inner_lines"]
            if beacon["span_lineno_start"] > beacon["span_lineno_end"]:
                beacon["closed"] = True
        beacons.append(beacon)
    if diagnostics is not None:
        for record in records:
            if record["marker"] == "close":
                diagnostics.append({"record": "close", "line": record["line"], "id": record["fields"].get("id"),
                                    "pair_state": record["pair_state"], "complete": record["complete"],
                                    "terminated_by": record["terminated_by"], "pair_candidates": record["pair_candidates"]})
                if record["pair_state"] == "matched":
                    diagnostics.append({"record": "close_match", "line": record["line"], "opener_line": record["opener_line"]})
    return beacons


def _scan_source_beacons(snapshot: SourceSnapshot, language: str, **kwargs) -> list[dict]:
    if any(key in kwargs for key in ("section_start", "section_end", "section_byte_start", "section_byte_end")):
        left = kwargs.get("section_byte_start", snapshot.byte_offsets[max(0, kwargs.get("section_start", 1) - 1)])
        right = kwargs.get("section_byte_end", snapshot.byte_offsets[kwargs.get("section_end", len(snapshot.physical_lines))])
        local = source_snapshot_from_bytes(snapshot.raw_bytes[left:right])
        evidence = [{**r, "start": r["start"] + left, "end": r["end"] + left} for r in source_lexical_evidence(local, language)]
    else:
        evidence = source_lexical_evidence(snapshot, language)
    return scan_beacon_structure(snapshot, language, evidence=evidence, **kwargs)


def _markdown_beacon_snippet_range(beacon: dict, lines: list[str], records: list[dict]) -> None:
    if beacon["kind"] != "span" or not beacon.get("start_snippet"):
        return
    norm = normalize_for_match(beacon["start_snippet"])
    if not norm:
        return
    metadata = {line for r in records for line in range(r["line"], r["read_end_line"] + 1)}
    candidates = [i for i, line in enumerate(lines, 1) if i not in metadata and norm in normalize_for_match(line)]
    after = [i for i in candidates if i > beacon["comment_line"]]
    start = candidates[0] if len(candidates) == 1 else after[0] if len(after) == 1 else None
    if start is None:
        return
    if beacon.get("end_snippet"):
        end_norm = normalize_for_match(beacon["end_snippet"])
        end = next((i for i in range(start, len(lines) + 1) if end_norm in normalize_for_match(lines[i - 1])), None)
        if end is None:
            return
    else:
        end = next((i - 1 for i in range(start + 1, len(lines) + 1) if re.match(r"^#{1,64}\s", lines[i - 1])), len(lines))
    beacon["span_lineno_start"], beacon["span_lineno_end"] = start, end


def normalize_for_match(s: str) -> str:
    if not isinstance(s, str):
        return ""
    return "".join(ch for ch in s if not ch.isspace())


_TAG_LIKE_PATTERN = re.compile(r"<[A-Za-z_/][-A-Za-z0-9_/]*>")


def whiten_text_for_header_compare(text: str | None, *, collapse_whitespace: bool = True) -> str:
    if not isinstance(text, str):
        return ""
    s = text

    s = _TAG_LIKE_PATTERN.sub("", s)

    s = s.replace("`", "")

    html_entity_map = {
        "&quot;": '"',
        "&lt;": "<",
        "&gt;": ">",
        "&amp;": "&",
    }
    for ent, ch in html_entity_map.items():
        if ent in s:
            s = s.replace(ent, ch)

    s = s.replace('"', "")

    _PRESERVE_SO = {"🔱"}
    s = "".join(ch for ch in s if unicodedata.category(ch) != "So" or ch in _PRESERVE_SO)

    if collapse_whitespace:
        s = re.sub(r"\s+", "", s)
    return s


def _is_meaningful_js_local_name(var_name: str | None) -> bool:
    raw = (var_name or "").strip()
    if not raw:
        return False

    compact = re.sub(r"[_$]", "", raw)
    if len(compact) < 3:
        return False
    return True


def _new_js_scope_state(*, is_lambda: bool) -> dict[str, Any]:
    return {
        "is_lambda": bool(is_lambda),
        "anon_counter": 0,
        "local_name_counts": {},
    }


def _js_next_anonymous_scope_name(scope_state: dict[str, Any] | None) -> str:
    if not isinstance(scope_state, dict):
        return "__lambdao_1"

    next_n = int(scope_state.get("anon_counter") or 0) + 1
    scope_state["anon_counter"] = next_n
    return f"__lambdao_{next_n}"


def _js_next_lambda_local_suffix(scope_state: dict[str, Any] | None, var_name: str) -> str:
    if not isinstance(scope_state, dict):
        return "1"

    counts = scope_state.get("local_name_counts")
    if not isinstance(counts, dict):
        counts = {}
        scope_state["local_name_counts"] = counts

    next_n = int(counts.get(var_name) or 0) + 1
    counts[var_name] = next_n
    return str(next_n)


def _extract_md_path_from_note(note: str | None) -> list[str] | None:
    if not isinstance(note, str):
        return None
    lines = note.splitlines()
    in_block = False
    path_lines: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not in_block:
            if stripped == "MD_PATH:":
                in_block = True
            continue
        if stripped == "---":
            break
        if not stripped:
            continue
        if not re.match(r"^#{1,32}\s+\S", stripped):
            continue
        path_lines.append(stripped)
    return path_lines or None


def _markdown_path_identity(path_lines) -> tuple:
    identity = []
    for raw in path_lines or ():
        text = (raw or "").strip()
        if not text:
            continue
        match = re.match(r"^(#{1,64})\s*(.*)$", text)
        if not match:
            return ()
        identity.append((len(match[1]), whiten_text_for_header_compare(match[2].strip()).casefold()))
    return tuple(identity)


def _slice_label_tags(slice_labels: str) -> str:
    if not isinstance(slice_labels, str):
        return ""
    labels = []
    for part in re.split(r"[\s,]+", slice_labels):
        part = part.strip()
        if not part:
            continue
        labels.append(f"#{part}")
    return " ".join(labels)


def _strip_tag_underline_markup(text: str) -> str:
    if not isinstance(text, str):
        return ""
    s = text
    s = re.sub(r"(?i)&lt;\s*/?\s*u\s*&gt;", "", s)
    s = re.sub(r"(?i)<\s*/?\s*u\s*>", "", s)
    s = re.sub(r"(?i)&lt;\s*-\s*u\s*&gt;", "", s)
    s = re.sub(r"(?i)<\s*-\s*u\s*>", "", s)
    return s


def _sanitize_slice_label_token(part: str) -> str:
    if not isinstance(part, str):
        return ""
    sanitized = re.sub(r"[\x00-\x1f\x7f]+", "", part)
    sanitized = _strip_tag_underline_markup(sanitized)
    sanitized = re.sub(r"[\s/\\:.,#|]+", "-", sanitized)
    return sanitized.strip("-")


def _canonicalize_slice_labels(slice_labels: str | None, role: str | None) -> str:
    raw = (slice_labels or "").strip()
    base_role = (role or "").strip()
    tokens: list[str] = []

    if raw:
        for part in re.split(r"[\s,]+", raw):
            part = part.strip()
            if not part:
                continue
            sanitized = _sanitize_slice_label_token(part)
            if sanitized and sanitized not in tokens:
                tokens.append(sanitized)
    elif base_role:
        sanitized = _sanitize_slice_label_token(base_role)
        if sanitized:
            tokens.append(sanitized)

    return ",".join(tokens)


def _coerce_boolish(val: Any | None) -> Optional[bool]:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if not s:
        return None
    if s in {"false", "0", "no", "off"}:
        return False
    if s in {"true", "1", "yes", "on"}:
        return True
    return None


def _parse_show_span_fields(fields: dict[str, str]) -> tuple[bool, bool]:
    raw = fields.get("show_span")
    if raw is None:
        return True, False
    b = _coerce_boolish(raw)
    if b is None:
        return True, True
    return b, True


def _beacon_show_span(beacon: dict[str, Any]) -> tuple[bool, bool]:
    explicit = bool(beacon.get("show_span_explicit"))
    raw = beacon.get("show_span")
    if isinstance(raw, bool):
        return raw, explicit
    b = _coerce_boolish(raw)
    if b is None:
        return True, explicit
    return b, True


def _strip_beacon_meta_blocks_from_context(context_lines: list[str]) -> list[str]:
    if not context_lines:
        return []
    snapshot = _source_line_view(context_lines)
    records = scan_beacon_structure(snapshot, "context")
    stripped = beacon_context_lines(snapshot, records)
    directives = _context_directive_lines([v.strip().lstrip("#*/- ") for v in stripped])
    out: list[str] = []
    for raw, visible, directive in zip(context_lines, stripped, directives):
        if directive and visible.strip().strip("#*/- "):
            out.append(raw if visible == raw.rstrip("\r\n") else visible.strip())
    return out


def _context_directive_lines(cleaned):
    result = list(cleaned)
    return result


def _beacon_context_view(lines: list[str], language: str, records=None):
    snapshot = _source_line_view(lines)
    records = _scan_source_beacons(snapshot, language) if records is None else records
    visible = beacon_context_lines(snapshot, records, release_incomplete_wrappers=language == "markdown")
    if language in {"js", "js_ts"}:
        mask, _ = _build_js_comment_mask(lines)
        _, cleaned = _build_js_comment_mask(visible)
        return records, mask, _context_directive_lines(cleaned)
    mask, cleaned = [False] * len(lines), [""] * len(lines)
    if language == "markdown":
        rows = _beacon_comment_rows(snapshot, language, 1, len(lines))
        visible_rows = _beacon_comment_rows(_source_line_view(visible), language, 1, len(lines))
        for original, row in zip(rows, visible_rows):
            if row["form"] or (original["form"] and not visible[row["line"] - 1].strip()):
                index = row["line"] - 1
                mask[index] = True
                cleaned[index] = visible[index][row["column"]:row["column"] + len(row["text"])].strip()
        return records, mask, _context_directive_lines(cleaned)
    ranges = [r for r in source_lexical_evidence(snapshot, language) if r["certainty"] == "comment"]
    range_index = 0
    for index, raw in enumerate(snapshot.physical_lines):
        left, right = snapshot.byte_offsets[index], snapshot.byte_offset(index + 1, len(raw))
        parts = []
        while range_index < len(ranges) and ranges[range_index]["end"] <= left:
            range_index += 1
        current = range_index
        while current < len(ranges) and ranges[current]["start"] < right:
            region = ranges[current]
            current += 1
            a, b = max(left, region["start"]), min(right, region["end"])
            if a >= b or (region["start"] >= left and snapshot.raw_bytes[left:a].strip()):
                continue
            mask[index] = True
            ca, cb = len(snapshot.raw_bytes[left:a].decode("utf-8")), len(snapshot.raw_bytes[left:b].decode("utf-8"))
            body = visible[index][ca:cb].lstrip()
            prefix = snapshot.raw_bytes[region["start"]:region["start"] + 2]
            if a == region["start"]:
                body = body[2:].lstrip() if prefix in {b"--", b"/*"} else body[1:].lstrip()
                if language == "python":
                    body = body.lstrip("#").lstrip()
            if prefix == b"/*" and body.endswith("*/"):
                body = body[:-2].strip()
            if language in {"sh", "shell", "yaml"} and not body.strip("# "):
                body = ""
            parts.append(body)
        cleaned[index] = " ".join(parts)
    return records, mask, _context_directive_lines(cleaned)


def _collect_beacon_context(lines, comment_line, records, mask, cleaned, *, stop_at_pair=True):
    context = []
    for step, first in ((-1, comment_line - 1), (1, comment_line)):
        j = first
        while 1 <= j <= len(lines):
            if step > 0 and stop_at_pair and any(r.get("opener_line") == comment_line and r["line"] == j for r in records):
                break
            if not lines[j - 1].strip():
                j += step
                continue
            if not mask[j - 1]:
                break
            if cleaned[j - 1].strip():
                if step < 0:
                    context.insert(0, cleaned[j - 1])
                else:
                    context.append(cleaned[j - 1])
            j += step
    return context


def parse_python_beacon_blocks(
    lines: list[str], *, diagnostics: list | None = None
) -> list[dict[str, Any]]:
    return _parse_structural_beacons("python", lines, diagnostics)


def _split_csvish_tokens(raw: str | None) -> list[str]:
    if not isinstance(raw, str):
        return []
    parts = re.split(r"[\s,]+", raw.strip())
    return [p.strip() for p in parts if p and p.strip()]


def _merge_unique_strings(values: list[str], *, sep: str = " | ") -> str:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        s = (v or "").strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return sep.join(out)


def _canonicalize_slice_labels_union(labels: list[str]) -> str:
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in labels:
        for part in _split_csvish_tokens(raw):
            sanitized = _sanitize_slice_label_token(part)
            if not sanitized:
                continue
            if sanitized in seen:
                continue
            seen.add(sanitized)
            tokens.append(sanitized)
    return ",".join(tokens)


def _hygiene_member(snapshot: SourceSnapshot, block: dict) -> dict:
    record = block["record"]
    fields = record["fields"]
    return {**_beacon_candidate(block), **_beacon_physical_witness(snapshot, block),
            "comment": fields.get("comment") or fields.get("comments"),
            "fields": dict(fields), "field_ranges": list(record["field_ranges"]),
            "opener_range": record["opener_range"], "read_range": (record["opener_range"][0], record["read_end"]),
            "bracket_range": record["bracket_range"], "wrapper_range": record["wrapper_range"],
            "wrapper_open_range": record["wrapper_open_range"], "wrapper_close_range": record["wrapper_close_range"],
            "line_extent": record["line_extent"], "comment_form": record["comment_form"],
            "literal_certainty": record["literal_certainty"],
            "owned_range_hashes": [{"range": (a, b), "sha256": hashlib.sha256(snapshot.raw_bytes[a:b]).hexdigest()}
                                   for a, b in record["write_ranges"] or ()],
            "occurrences": record["occurrences"], "diagnostics": record["diagnostics"],
            "repair_unambiguous": record["repair_unambiguous"]}


def _hygiene_field_conflicts(group: list[dict]) -> list[dict]:
    conflicts = []
    records = [b["record"] for b in group]
    for key in ("role", "start_snippet", "end_snippet", "show_span"):
        values = [r["fields"].get(key) for r in records]
        if key == "show_span":
            values = [(_coerce_boolish(v) if _coerce_boolish(v) is not None else ("opaque", v))
                      if v not in (None, "") else None for v in values]
        nonempty = {v for v in values if v not in (None, "")}
        if len(nonempty) > 1 or (nonempty and values[-1] not in nonempty):
            conflicts.append({"field": key, "values": values})
    opaque = []
    for record in records:
        located = {loc["line"] for o in record["occurrences"] for loc in o["locations"]}
        opaque.append([d["raw"] for d in record["diagnostics"]
                       if d["code"] == "unknown_field" or
                       d["code"] == "field_continuation" and d["line"] not in located])
        ambiguous = [d for d in record["diagnostics"] if d["code"] == "inline_field_ambiguity" or
                     d["code"] == "duplicate_field" and
                     (d["conflicting"] or d["key"] in {"slice_labels", "comment", "comments"})]
        comments = [o for o in record["occurrences"] if o["key"] in {"comment", "comments"}]
        if ambiguous or len(comments) > 1:
            conflicts.append({"field": "ambiguous_fields", "line": record["line"],
                              "diagnostics": ambiguous, "comment_occurrences": comments})
    if any(value != opaque[-1] for value in opaque):
        conflicts.append({"field": "unknown_fields", "values": opaque})
    return conflicts


def _hygiene_edit_ranges(edits) -> list[dict]:
    return [{"start_byte": e.start_byte, "end_byte": e.end_byte,
             "replacement_bytes": len(e.replacement), "owner": e.owner} for e in edits]


def plan_beacon_hygiene(snapshot: SourceSnapshot, structures: dict, attachment_index: dict) -> dict:
    language = structures["language"]
    groups = {}
    diagnostics = [{"owner": r["opener_range"][0], **d} for r in structures["records"]
                   for d in r["diagnostics"]]
    for block in attachment_index["blocks"]:
        record = block["record"]
        if (record["fields"].get("kind") or "").strip().lower() != "ast":
            continue
        anchor = block["anchor"]
        section = record["section"]
        key = (section.get("type"), section.get("start_byte", section["start_line"]),
               anchor["line"] if anchor else ("unattached", block["owner"]))
        groups.setdefault(key, []).append(block)
    edits, witnesses, states, slots, applied, skipped = [], [], [], [], [], []
    for members in groups.values():
        group = sorted(members, key=lambda b: b["owner"])
        records = [b["record"] for b in group]
        invalid = [r for r in records if not r["complete"] or not r["write_ranges"] or
                   not r["fields"].get("id") or r["literal_certainty"] != "comment" or r["id_conflicting"]]
        if len(group) < 2 and not invalid:
            continue
        evidence = {"operation": "merge_duplicate_ast", "status": "skipped",
                    "target": _beacon_anchor_descriptor(group[-1]["anchor"]),
                    "members": [_hygiene_member(snapshot, b) for b in group],
                    "before_sha256": snapshot.sha256, "retained_ids": [], "dropped_ids": [],
                    "edited_ranges": []}

        def skip(reason, **extra):
            row = {**evidence, "reason": reason, "candidates": evidence["members"], **extra}
            skipped.append(row)

        if invalid:
            skip("incomplete_or_uncertain_member", invalid_lines=[r["line"] for r in invalid])
            continue
        if group[-1]["anchor"] is None:
            skip("no_physical_attachment")
            continue
        ids = [r["fields"]["id"] for r in records]
        if len(set(ids)) != 1:
            skip("distinct_ids")
            continue
        conflicts = _hygiene_field_conflicts(group)
        if conflicts:
            skip("conflicting_fields", conflicts=conflicts)
            continue
        labels = _canonicalize_slice_labels_union([r["fields"].get("slice_labels") or "" for r in records])
        comment = _merge_unique_strings([r["fields"].get("comment") or r["fields"].get("comments") or ""
                                         for r in records])
        try:
            validate_beacon_comment_text(comment)
        except ValueError as error:
            skip("invalid_merged_comment", error=str(error))
            continue
        survivor = records[-1]
        field_errors = []
        updates = {"slice_labels": labels, "comment": comment or None}
        candidate_edits = _build_beacon_block_for_language(
            language, snapshot=snapshot, record=survivor, field_updates=updates, field_errors=field_errors)
        if field_errors:
            skip("ambiguous_fields", field_errors=field_errors)
            continue
        for record in records[:-1]:
            candidate_edits.extend(BeaconEdit(a, b, b"", record["opener_range"][0])
                                   for a, b in record["write_ranges"])
        owners = [b["owner"] for b in group]
        expected = {"id": ids[-1], "kind": survivor["fields"]["kind"],
                    "slice_labels": labels or survivor["fields"].get("slice_labels")}
        comment_key = "comments" if "comments" in survivor["fields"] and "comment" not in survivor["fields"] else "comment"
        if comment or comment_key in survivor["fields"]:
            expected[comment_key] = comment
        candidate_states = [{"owner": b["owner"], "operation": "absent"} for b in group[:-1]] + [
            {"owner": owners[-1], "operation": "present", "complete": True, "fields": expected}]
        candidate_slots = [e.start_byte for e in candidate_edits if e.start_byte == e.end_byte]
        candidate_plan = BeaconEditPlan(snapshot, language, tuple(edits + candidate_edits),
                                        tuple(witnesses + owners), tuple(states + candidate_states),
                                        insertion_slots=tuple(slots + candidate_slots))
        proof = verify_beacon_edit_plan(candidate_plan, scanner=_beacon_edit_records)
        if not proof["success"]:
            skip("plan_mismatch", proof={k: v for k, v in proof.items() if k != "success"})
            continue
        edits.extend(candidate_edits)
        witnesses.extend(owners)
        states.extend(candidate_states)
        slots.extend(candidate_slots)
        applied.append({**evidence, "status": "planned", "reason": "same_id_compatible",
                        "retained_ids": [ids[-1]], "dropped_ids": ids[:-1],
                        "survivor": _hygiene_member(snapshot, group[-1]),
                        "edited_ranges": _hygiene_edit_ranges(candidate_edits),
                        "labels": labels, "comment": comment or None})
    plan = BeaconEditPlan(snapshot, language, tuple(edits), tuple(witnesses), tuple(states),
                          insertion_slots=tuple(slots))
    return {"edits": list(edits), "edit_plan": plan, "applied_groups": applied,
            "skipped_groups": skipped, "diagnostics": diagnostics}


def _find_enclosing_ast_node_for_line(
    nodes: list[dict[str, Any]], start_line: int
) -> Optional[dict[str, Any]]:
    best: Optional[dict[str, Any]] = None
    best_depth = -1

    def _recurse(node: dict[str, Any], depth: int) -> None:
        nonlocal best, best_depth
        if not isinstance(node, dict):
            return
        start = node.get("orig_lineno_start_unused")
        end = node.get("orig_lineno_end_unused")
        ast_type = node.get("ast_type")
        if (
            ast_type
            and isinstance(start, int)
            and isinstance(end, int)
            and start <= start_line <= end
        ):
            if depth > best_depth:
                best = node
                best_depth = depth
        for ch in node.get("children") or []:
            if isinstance(ch, dict):
                _recurse(ch, depth + 1)

    for root in nodes:
        _recurse(root, 0)
    return best


def apply_python_beacons(
    file_path: str,
    lines: list[str],
    outline_nodes: list[dict[str, Any]],
    *,
    decorator_ranges_by_definition: Optional[dict[int, list[tuple[int, int]]]] = None,
) -> None:
    records = _scan_source_beacons(_source_line_view(lines), "python")
    beacons = _parse_structural_beacons("python", lines, records=records)
    if not beacons:
        return
    _prepend_duplicate_beacon_id_warning_nodes(outline_nodes, beacons, language="PY")

    if decorator_ranges_by_definition is None:
        decorator_ranges_by_definition = _python_decorator_ranges_by_definition(lines)
    attachment = beacon_attachment_index(_source_line_view(lines), outline_nodes, {
        "language": "python", "records": records, "beacons": beacons,
        "decorator_ranges_by_definition": decorator_ranges_by_definition,
    })
    attachment_by_line = {b["record"]["line"]: b for b in attachment["blocks"]}

    _, context_mask, context_cleaned = _beacon_context_view(lines, "python", records)
    triple_spans: List[tuple[int, int]] = []
    context_snapshot = _source_line_view(lines)
    for region in source_lexical_evidence(context_snapshot, "python"):
        raw = context_snapshot.raw_bytes[region["start"]:region["end"]].decode("utf-8")
        if region["certainty"] == "literal" and re.match(r'''(?i:[rubf]*)(?:"""|\x27\x27\x27)''', raw):
            owned_lines = [i + 1 for i in range(len(lines)) if context_snapshot.byte_offsets[i] < region["end"]
                           and context_snapshot.byte_offsets[i + 1] > region["start"]]
            if owned_lines:
                triple_spans.append((owned_lines[0], owned_lines[-1]))

    def _find_triple_span_for_line(line_no: int) -> Optional[tuple[int, int]]:
        for s, e in triple_spans:
            if s <= line_no <= e:
                return (s, e)
        return None

    span_sort_lines: dict[int, int] = {}

    def _node_source_order_line(node: dict[str, Any]) -> int:
        cached = span_sort_lines.get(id(node))
        if isinstance(cached, int):
            return cached
        ln = node.get("orig_lineno_start_unused") if isinstance(node, dict) else None
        if isinstance(ln, int):
            return ln
        return 10**9

    def _insert_child_in_source_order(
        siblings: List[Dict[str, Any]],
        child_node: Dict[str, Any],
        sort_line: int,
    ) -> None:
        span_sort_lines[id(child_node)] = sort_line
        insert_at = len(siblings)
        for idx, existing in enumerate(siblings):
            if _node_source_order_line(existing) > sort_line:
                insert_at = idx
                break
        siblings.insert(insert_at, child_node)

    def _extract_python_beacon_context(
        comment_line: int,
        decor_start: Optional[int] = None,
        beacon_id: Optional[str] = None,
    ) -> List[str]:
        context: List[str] = []
        seen_spans: set[tuple[int, int]] = set()

        j = comment_line - 1
        while j >= 1:
            raw = lines[j - 1]
            stripped = raw.lstrip()
            if not stripped:
                j -= 1
                continue
            if decor_start is not None and decor_start <= j <= comment_line:
                j -= 1
                continue
            if context_mask[j - 1]:
                body = context_cleaned[j - 1]
                if body:
                    context.insert(0, body)
                j -= 1
                continue
            span = _find_triple_span_for_line(j)
            if span and span not in seen_spans:
                s, e = span
                for k in range(e, s - 1, -1):
                    context.insert(0, lines[k - 1])
                seen_spans.add(span)
                j = s - 1
                continue
            if stripped.startswith("@"):
                j -= 1
                continue
            break

        j = comment_line
        while j <= len(lines):
            if any(r.get("opener_line") == comment_line and r["line"] == j for r in records):
                break
            raw = lines[j - 1]
            stripped = raw.lstrip()
            if not stripped:
                j += 1
                continue
            if context_mask[j - 1]:
                body = context_cleaned[j - 1]
                if body:
                    context.append(body)
                j += 1
                continue
            span = _find_triple_span_for_line(j)
            if span and span not in seen_spans:
                s, e = span
                for k in range(s, e + 1):
                    context.append(lines[k - 1])
                seen_spans.add(span)
                j = e + 1
                continue
            if stripped.startswith("@"):
                break
            break

        return context

    for beacon in beacons:
        attached = attachment_by_line.get(beacon.get("comment_line"))
        if attached:
            beacon["kind"] = attached["effective_kind"]
            beacon["_anchor_lineno"] = attached["next_code_line"]

    for beacon in beacons:
        if beacon.get("kind") != "ast":
            continue
        attached = attachment_by_line.get(beacon.get("comment_line"))
        chosen = attached["anchor"]["node"] if attached and attached["anchor"] else None
        if chosen is None:
            continue

        b_id = (beacon.get("id") or "").strip()
        role = (beacon.get("role") or "").strip()
        raw_slice_labels = (beacon.get("slice_labels") or "").strip()
        if not role and b_id and "@" in b_id:
            role = b_id.split("@", 1)[0]
        slice_labels = _canonicalize_slice_labels(raw_slice_labels, role)

        display_role = strip_leading_decoration_symbols(role) or b_id or "ast-beacon"
        display_slice = slice_labels or "-"

        tag_suffix = _slice_label_tags(slice_labels or display_slice)
        name = chosen.get("name") or "Untitled"
        if "🔱" not in name:
            name = f"{name} 🔱"
        if tag_suffix:
            name = f"{name} {tag_suffix}"
        chosen["name"] = name

        comment = strip_leading_decoration_symbols(beacon.get("comment"))
        meta_lines = [
            "BEACON (AST)",
            f"id: {b_id}",
            f"role: {role}",
            f"slice_labels: {slice_labels}",
            "kind: ast",
        ]
        if comment:
            meta_lines.append(f"comment: {comment}")
        note = chosen.get("note") or ""
        if note:
            note_lines = note.splitlines()
            insert_idx = (
                1
                if (note_lines and note_lines[0].strip().startswith("AST_QUALNAME:"))
                else 0
            )
            new_note_lines: list[str] = []
            new_note_lines.extend(note_lines[:insert_idx])
            new_note_lines.extend(meta_lines)
            if insert_idx < len(note_lines):
                if note_lines[insert_idx].strip():
                    new_note_lines.append("")
                new_note_lines.extend(note_lines[insert_idx:])
            chosen["note"] = "\n".join(new_note_lines)
        else:
            chosen["note"] = "\n".join(meta_lines)

    for beacon in beacons:
        if beacon.get("kind") != "span":
            continue
        start_snippet = beacon.get("start_snippet")
        comment_line = beacon.get("comment_line") or 0

        attachment_line: Optional[int] = None

        if start_snippet:
            norm_snip = normalize_for_match(start_snippet)
            if not norm_snip:
                continue

            start_candidates: list[int] = []
            for idx, line in enumerate(lines, start=1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if norm_snip in normalize_for_match(line):
                    start_candidates.append(idx)

            if not start_candidates:
                continue

            if len(start_candidates) == 1:
                attachment_line = start_candidates[0]
            else:
                after = [ln for ln in start_candidates if ln > comment_line]
                if len(after) == 1:
                    attachment_line = after[0]
                else:
                    continue
        else:
            span_start = beacon.get("span_lineno_start")
            if isinstance(span_start, int):
                attachment_line = span_start
            elif isinstance(comment_line, int) and comment_line > 0:
                attachment_line = comment_line
            else:
                anchor = beacon.get("_anchor_lineno")
                attachment_line = anchor if isinstance(anchor, int) else None

        if attachment_line is None:
            continue

        enclosing = _find_enclosing_ast_node_for_line(outline_nodes, attachment_line)

        b_id = (beacon.get("id") or "").strip()
        role = (beacon.get("role") or "").strip()
        raw_slice_labels = (beacon.get("slice_labels") or "").strip()
        if not role and b_id and "@" in b_id:
            role = b_id.split("@", 1)[0]
        slice_labels = _canonicalize_slice_labels(raw_slice_labels, role)

        show_span, show_span_explicit = _beacon_show_span(beacon)

        display_role = strip_leading_decoration_symbols(role) or b_id or "span-beacon"
        display_slice = slice_labels or "-"

        tag_suffix = _slice_label_tags(slice_labels or display_slice)
        name = f"🔱 {display_role}"
        if tag_suffix:
            name = f"{name} {tag_suffix}"

        enclosing_decor_start: Optional[int] = None
        if enclosing is not None:
            dsl = enclosing.get("decor_start_lineno")
            if isinstance(dsl, int):
                enclosing_decor_start = dsl
        context_lines = _extract_python_beacon_context(
            comment_line,
            enclosing_decor_start,
            beacon_id=b_id,
        )
        comment = strip_leading_decoration_symbols(beacon.get("comment"))

        note_lines = [
            "BEACON (SPAN)",
            f"id: {b_id}",
            f"role: {role}",
            f"slice_labels: {slice_labels}",
            "kind: span",
        ]
        if show_span_explicit:
            note_lines.append(f"show_span: {'true' if show_span else 'false'}")
        if comment:
            note_lines.append(f"comment: {comment}")
        if context_lines:
            note_lines.append("")
            note_lines.append("CONTEXT COMMENTS (PYTHON):")
            note_lines.extend(context_lines)

        span_start = beacon.get("span_lineno_start")
        span_end = beacon.get("span_lineno_end")
        span_text_lines: List[str] = []
        if isinstance(span_start, int) and isinstance(span_end, int):
            span_start = max(1, span_start)
            span_end = min(len(lines), span_end)
            if span_start <= span_end:
                span_text_lines = lines[span_start - 1 : span_end]
                while span_text_lines and not span_text_lines[0].strip():
                    span_text_lines.pop(0)
                while span_text_lines and not span_text_lines[-1].strip():
                    span_text_lines.pop()

        if show_span and span_text_lines:
            note_lines.append("")
            note_lines.append("SPAN TEXT:")
            note_lines.extend(span_text_lines)

        span_node = {
            "name": name,
            "note": "\n".join(note_lines),
            "children": [],
            "orig_lineno_start_unused": attachment_line,
            "orig_lineno_end_unused": attachment_line,
        }

        if enclosing is not None:
            children = enclosing.get("children") or []
            _insert_child_in_source_order(children, span_node, attachment_line)
            enclosing["children"] = children
        else:
            _insert_child_in_source_order(outline_nodes, span_node, attachment_line)


def get_docstring(node: ast.AST) -> str:
    return ast.get_docstring(node) or ""


def get_function_signature(node: ast.FunctionDef) -> str:
    args = []
    
    for arg in node.args.args:
        arg_str = arg.arg
        if arg.annotation:
            if isinstance(arg.annotation, ast.Name):
                arg_str += f": {arg.annotation.id}"
            elif isinstance(arg.annotation, ast.Subscript):
                try:
                    val = getattr(arg.annotation.value, 'id', '')
                    slice_val = getattr(arg.annotation.slice, 'id', '')
                    if not slice_val and hasattr(arg.annotation.slice, 'value'):
                         if isinstance(arg.annotation.slice, ast.Name):
                             slice_val = arg.annotation.slice.id
                    if val and slice_val:
                        arg_str += f": {val}[{slice_val}]"
                except:
                    pass
            elif isinstance(arg.annotation, ast.Attribute):
                arg_str += f": {arg.annotation.attr}"
        args.append(arg_str)
        
    return f"{node.name}({', '.join(args)})"


def _dedupe_python_const_ast_qualnames(outline_nodes: list[dict[str, Any]]) -> None:
    buckets: dict[str, list[dict[str, Any]]] = {}

    def _walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            qual = node.get("ast_qualname")
            if node.get("ast_type") == "const" and isinstance(qual, str) and qual:
                buckets.setdefault(qual, []).append(node)
            _walk([c for c in (node.get("children") or []) if isinstance(c, dict)])

    _walk(outline_nodes)

    for qual, nodes in buckets.items():
        if len(nodes) <= 1:
            continue
        for ordinal, node in enumerate(sorted(nodes, key=lambda n: int(n.get("orig_lineno_start_unused") or 10**9)), start=1):
            line = node.get("orig_lineno_start_unused")
            suffix = f"line{line}" if isinstance(line, int) else f"occ{ordinal}"
            new_qual = f"{qual}@{suffix}"
            node["ast_qualname"] = new_qual
            note = node.get("note")
            if isinstance(note, str):
                lines = note.splitlines()
                for idx, raw in enumerate(lines):
                    if raw.strip().startswith("AST_QUALNAME:"):
                        prefix = raw.split("AST_QUALNAME:", 1)[0]
                        lines[idx] = f"{prefix}AST_QUALNAME: {new_qual}"
                        node["note"] = "\n".join(lines)
                        break


def parse_file_outline_core(
    file_path: str, *, snapshot: SourceSnapshot | None = None,
) -> tuple[List[Dict[str, Any]], List[str]]:
    snapshot = snapshot if snapshot is not None else read_source_snapshot(file_path)
    tree = ast.parse(snapshot.normalized_text, filename=file_path)
    lines = list(snapshot.physical_lines)
    decorator_ranges_by_definition = _python_decorator_ranges_by_definition(lines, tree=tree)
    context_records = _scan_source_beacons(_source_line_view(lines), "python")
    _, context_mask, context_cleaned = _beacon_context_view(lines, "python", context_records)

    def _extract_ast_comment_context(start_lineno: int, decor_start: Optional[int]) -> list[str]:
        context: list[str] = []
        j = start_lineno - 1
        while j >= 1:
            raw = lines[j - 1]
            stripped = raw.lstrip()
            if not stripped:
                j -= 1
                continue
            if decor_start is not None and decor_start <= j < start_lineno:
                j -= 1
                continue
            if context_mask[j - 1]:
                body = context_cleaned[j - 1]
                if body:
                    context.insert(0, body)
                j -= 1
                continue
            break

        j = start_lineno + 1
        while j <= len(lines):
            raw = lines[j - 1]
            stripped = raw.lstrip()
            if not stripped:
                j += 1
                continue
            if context_mask[j - 1]:
                body = context_cleaned[j - 1]
                if body:
                    context.append(body)
                j += 1
                continue
            break

        return context

    priority_counter = [100]

    def _should_map_python_assigned_name(name: str, *, qual_prefix: Optional[str]) -> bool:
        if not isinstance(name, str) or not name:
            return False
        if name.isupper():
            return True
        if qual_prefix is None and len(name) > 4 and name.startswith("__") and name.endswith("__"):
            return True
        return False

    def _assigned_name_note_label(name: str, *, qual_prefix: Optional[str]) -> str:
        if qual_prefix is None and len(name) > 4 and name.startswith("__") and name.endswith("__"):
            return "Module global"
        return "Constant"

    def _iter_nested_statement_bodies(stmt: ast.AST) -> List[List[ast.AST]]:
        candidates: List[tuple[int, int, List[ast.AST]]] = []
        ordinal = 0

        def _add(body: Any) -> None:
            nonlocal ordinal
            if not isinstance(body, list) or not body:
                return
            first_stmt = body[0]
            first_lineno = getattr(first_stmt, "lineno", None)
            if not isinstance(first_lineno, int):
                first_lineno = 10**9
            candidates.append((first_lineno, ordinal, body))
            ordinal += 1

        for attr in ("body", "orelse", "finalbody"):
            _add(getattr(stmt, attr, None))

        handlers = getattr(stmt, "handlers", None)
        if isinstance(handlers, list):
            for handler in handlers:
                _add(getattr(handler, "body", None))

        cases = getattr(stmt, "cases", None)
        if isinstance(cases, list):
            for case in cases:
                _add(getattr(case, "body", None))

        candidates.sort(key=lambda item: (item[0], item[1]))
        return [body for _, _, body in candidates]

    def walk_body(
        body_items: List[ast.AST],
        qual_prefix: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for item in body_items:
            decor_ranges = decorator_ranges_by_definition.get(getattr(item, "lineno", 0) - 1, [])
            decor_start: Optional[int] = decor_ranges[0][0] + 1 if decor_ranges else None
            if isinstance(item, ast.ClassDef):
                qual = item.name if not qual_prefix else f"{qual_prefix}.{item.name}"
                start = getattr(item, "lineno", None)
                end = getattr(item, "end_lineno", start)
                doc = get_docstring(item)
                note_lines: list[str] = [f"AST_QUALNAME: {qual}"]
                if doc:
                    note_lines.append("---")
                    note_lines.append(doc)
                context_lines = _extract_ast_comment_context(start, decor_start) if isinstance(start, int) else []
                if context_lines:
                    note_lines.append("")
                    note_lines.append("CONTEXT COMMENTS (PYTHON):")
                    note_lines.extend(context_lines)
                note_text = "\n".join(note_lines)

                node = {
                    "name": f"{EMOJI_CLASS} class {item.name}",
                    "priority": priority_counter[0],
                    "note": note_text,
                    "children": walk_body(item.body, qual_prefix=qual),
                    "ast_type": "class",
                    "ast_name": item.name,
                    "ast_qualname": qual,
                    "file_path": file_path,
                    "orig_lineno_start_unused": start,
                    "orig_lineno_end_unused": end,
                    "decor_start_lineno": decor_start,
                }
                priority_counter[0] += 100
                results.append(node)
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = EMOJI_ASYNC if isinstance(item, ast.AsyncFunctionDef) else EMOJI_FUNC
                sig = get_function_signature(item)
                qual = item.name if not qual_prefix else f"{qual_prefix}.{item.name}"
                start = getattr(item, "lineno", None)
                end = getattr(item, "end_lineno", start)

                doc = get_docstring(item)
                note_lines: list[str] = [f"AST_QUALNAME: {qual}"]
                if doc:
                    note_lines.append("---")
                    note_lines.append(doc)
                context_lines = _extract_ast_comment_context(start, decor_start) if isinstance(start, int) else []
                if context_lines:
                    note_lines.append("")
                    note_lines.append("CONTEXT COMMENTS (PYTHON):")
                    note_lines.extend(context_lines)
                note_text = "\n".join(note_lines)

                node = {
                    "name": f"{prefix} {sig}",
                    "priority": priority_counter[0],
                    "note": note_text,
                    "children": walk_body(item.body, qual_prefix=qual),
                    "ast_type": "async_function" if isinstance(item, ast.AsyncFunctionDef) else "function",
                    "ast_name": item.name,
                    "ast_qualname": qual,
                    "file_path": file_path,
                    "orig_lineno_start_unused": start,
                    "orig_lineno_end_unused": end,
                    "decor_start_lineno": decor_start,
                }
                priority_counter[0] += 100
                results.append(node)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and _should_map_python_assigned_name(
                        target.id,
                        qual_prefix=qual_prefix,
                    ):
                        start = getattr(item, "lineno", None)
                        end = getattr(item, "end_lineno", start)
                        qual_const = target.id if not qual_prefix else f"{qual_prefix}.{target.id}"
                        note_label = _assigned_name_note_label(target.id, qual_prefix=qual_prefix)
                        note_text = f"AST_QUALNAME: {qual_const}\n---\n{note_label}"
                        node = {
                            "name": f"{EMOJI_CONST} {target.id}",
                            "priority": priority_counter[0],
                            "note": note_text,
                            "children": [],
                            "ast_type": "const",
                            "ast_name": target.id,
                            "ast_qualname": qual_const,
                            "file_path": file_path,
                            "orig_lineno_start_unused": start,
                            "orig_lineno_end_unused": end,
                        }
                        priority_counter[0] += 100
                        results.append(node)
            elif isinstance(item, ast.AnnAssign):
                target = item.target
                if isinstance(target, ast.Name) and _should_map_python_assigned_name(
                    target.id,
                    qual_prefix=qual_prefix,
                ):
                    start = getattr(item, "lineno", None)
                    end = getattr(item, "end_lineno", start)
                    qual_const = target.id if not qual_prefix else f"{qual_prefix}.{target.id}"
                    note_label = _assigned_name_note_label(target.id, qual_prefix=qual_prefix)
                    note_text = f"AST_QUALNAME: {qual_const}\n---\n{note_label}"
                    node = {
                        "name": f"{EMOJI_CONST} {target.id}",
                        "priority": priority_counter[0],
                        "note": note_text,
                        "children": [],
                        "ast_type": "const",
                        "ast_name": target.id,
                        "ast_qualname": qual_const,
                        "file_path": file_path,
                        "orig_lineno_start_unused": start,
                        "orig_lineno_end_unused": end,
                    }
                    priority_counter[0] += 100
                    results.append(node)
            else:
                for nested_body in _iter_nested_statement_bodies(item):
                    results.extend(walk_body(nested_body, qual_prefix=qual_prefix))
        return results

    outline_nodes = walk_body(tree.body)
    _dedupe_python_const_ast_qualnames(outline_nodes)
    apply_python_beacons(
        file_path, lines, outline_nodes,
        decorator_ranges_by_definition=decorator_ranges_by_definition,
    )
    return outline_nodes, lines


def _build_js_comment_mask(src_lines: list[str]) -> tuple[list[bool], list[str]]:
    snapshot = _source_line_view(src_lines)
    ranges = [r for r in js_lexical_evidence(snapshot) if r["certainty"] == "comment"]
    mask = [False] * len(src_lines)
    cleaned = [""] * len(src_lines)
    range_index = 0
    for index, raw in enumerate(snapshot.physical_lines):
        if not raw.strip():
            continue
        start = snapshot.byte_offsets[index]
        end = snapshot.byte_offset(index + 1, len(raw))
        parts = []
        while range_index < len(ranges) and ranges[range_index]["end"] <= start:
            range_index += 1
        current = range_index
        while current < len(ranges) and ranges[current]["start"] < end:
            region = ranges[current]
            current += 1
            a, b = max(start, region["start"]), min(end, region["end"])
            if a >= b:
                continue
            comment_prefix = snapshot.raw_bytes[region["start"]:region["start"] + 2]
            if (comment_prefix == b"//"
                    and region["start"] > snapshot.byte_offset(index + 1, len(raw) - len(raw.lstrip()))):
                continue
            mask[index] = True
            body = snapshot.raw_bytes[a:b].decode("utf-8").lstrip()
            if a == region["start"]:
                body = body[2:].strip() if comment_prefix == b"/*" else body[2:].lstrip()
            if comment_prefix == b"/*" and body.endswith("*/"):
                body = body[:-2].strip()
            parts.append(body)
        cleaned[index] = " ".join(parts)
    return mask, cleaned


def _extract_js_ast_comment_context(
    start_line: int,
    lines: list[str],
    n: int,
    comment_mask: list[bool],
    cleaned_comments: list[str],
    *, records=None,
) -> list[str]:
    if records is None:
        records, comment_mask, cleaned_comments = _beacon_context_view(lines, "js_ts")
    ctx: list[str] = []

    j = start_line - 1
    while j >= 1:
        raw = lines[j - 1]
        stripped = raw.lstrip()
        if not stripped:
            j -= 1
            continue
        if stripped.startswith("@"):
            j -= 1
            continue
        if comment_mask[j - 1]:
            text = cleaned_comments[j - 1]
            if text:
                ctx.insert(0, text)
            j -= 1
            continue
        break

    j = start_line + 1
    while j <= n:
        raw = lines[j - 1]
        stripped = raw.lstrip()
        if not stripped:
            j += 1
            continue
        if comment_mask[j - 1]:
            text = cleaned_comments[j - 1]
            if text:
                ctx.append(text)
            j += 1
            continue
        break

    return ctx


def _js_collapse_signature_ws(text: str | None) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())


def _js_node_text(node: Any, source_bytes: bytes) -> str:
    if node is None:
        return ""
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")


def _js_is_function_like(v: Any) -> bool:
    return bool(v) and getattr(v, "type", None) in {"arrow_function", "function"}


def _js_callee_simple_name(v: Any, source_bytes: bytes) -> str:
    if not v or getattr(v, "type", None) != "call_expression":
        return ""
    fn = v.child_by_field_name("function")
    if fn is None:
        return ""
    fn_text = _js_node_text(fn, source_bytes).strip()
    m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", fn_text)
    return m.group(1) if m else ""


def _js_find_function_wrapper_body(v: Any, source_bytes: bytes) -> Any:
    if not v or getattr(v, "type", None) != "call_expression":
        return None
    callee = _js_callee_simple_name(v, source_bytes)
    if callee not in {"computed"}:
        return None
    args = v.child_by_field_name("arguments")
    if args is None:
        return None
    for a in getattr(args, "named_children", []) or []:
        if _js_is_function_like(a):
            body = a.child_by_field_name("body")
            if body is not None:
                return body
    return None


def _js_find_define_store_setup_body(v: Any, source_bytes: bytes) -> Any:
    if not v or getattr(v, "type", None) != "call_expression":
        return None
    fn = v.child_by_field_name("function")
    if fn is None:
        return None
    fn_text = _js_node_text(fn, source_bytes).strip()
    if not fn_text.endswith("defineStore"):
        return None
    args = v.child_by_field_name("arguments")
    if args is None:
        return None
    for a in getattr(args, "named_children", []) or []:
        if getattr(a, "type", None) in {"arrow_function", "function"}:
            body = a.child_by_field_name("body")
            if body is not None:
                return body
    return None


def _js_find_define_store_options_object(v: Any, source_bytes: bytes) -> Any:
    if not v or getattr(v, "type", None) != "call_expression":
        return None
    fn = v.child_by_field_name("function")
    if fn is None:
        return None
    fn_text = _js_node_text(fn, source_bytes).strip()
    if not fn_text.endswith("defineStore"):
        return None
    args = v.child_by_field_name("arguments")
    if args is None:
        return None
    for a in getattr(args, "named_children", []) or []:
        if getattr(a, "type", None) == "object":
            return a
    return None


def _js_key_text(n: Any, source_bytes: bytes) -> str:
    if n is None:
        return ""
    txt = _js_node_text(n, source_bytes).strip()
    return txt.strip("\"'")


def _js_params_text(fn_node: Any, source_bytes: bytes) -> str:
    if fn_node is None:
        return "()"
    p = fn_node.child_by_field_name("parameters")
    if p is None:
        p = fn_node.child_by_field_name("parameter")
    if p is None:
        return "()"
    t = _js_collapse_signature_ws(_js_node_text(p, source_bytes))
    return t if t.startswith("(") else f"({t})"


def _js_body_node(fn_node: Any) -> Any:
    if fn_node is None:
        return None
    return fn_node.child_by_field_name("body")


@dataclass
class JsTsOutlineContext:

    file_path: str
    source_text: str
    source_bytes: bytes
    language_name: str
    root: Any
    lines: list[str]
    n: int
    comment_mask: list[bool]
    cleaned_comments: list[str]
    beacon_records: list[dict] = field(default_factory=list)
    line_offset0: int = 0
    qual_prefix: Optional[str] = None
    priority_counter: list[int] = field(default_factory=lambda: [100])


    @classmethod
    def from_file(cls, file_path: str, language_name: str) -> "JsTsOutlineContext":
        snapshot = read_source_snapshot(file_path)
        return cls.from_source(
            source_text=snapshot.parse_text, file_path=file_path,
            language_name=language_name, line_offset0=0, qual_prefix=None,
        )

    @classmethod
    def from_source(
        cls,
        *,
        source_text: str,
        file_path: str,
        language_name: str,
        line_offset0: int,
        qual_prefix: str | None,
    ) -> "JsTsOutlineContext":
        source_text = source_text.replace("\r\n", "\n").replace("\r", "\n")
        source_bytes = source_text.encode("utf-8")
        parser = Parser()
        language = get_language(language_name)
        parser.language = language
        tree = parser.parse(source_bytes)
        root = tree.root_node
        lines = physical_source_lines(source_text)
        n = len(lines)
        beacon_records, comment_mask, cleaned_comments = _beacon_context_view(lines, "js_ts")
        return cls(
            file_path=file_path,
            source_text=source_text,
            source_bytes=source_bytes,
            language_name=language_name,
            root=root,
            lines=lines,
            n=n,
            comment_mask=comment_mask,
            cleaned_comments=cleaned_comments,
            beacon_records=beacon_records,
            line_offset0=line_offset0,
            qual_prefix=qual_prefix,
        )


    def abs_line(self, local_line: int) -> int:
        return int(local_line) + int(self.line_offset0 or 0)

    def qualify(self, inner_qual: str) -> str:
        pref = (self.qual_prefix or "").strip()
        if pref and not pref.endswith(":"):
            pref = pref + ":"
        return f"{pref}{inner_qual}" if pref else inner_qual


    def make_note(
        self,
        ast_qual: str,
        start_line_local: Optional[int] = None,
        doc: Optional[str] = None,
    ) -> str:
        note_lines: List[str] = [f"AST_QUALNAME: {ast_qual}"]
        if doc:
            note_lines.append("---")
            note_lines.append(doc)
        if isinstance(start_line_local, int):
            ctx_comments = _extract_js_ast_comment_context(
                start_line_local,
                self.lines,
                self.n,
                self.comment_mask,
                self.cleaned_comments,
                records=self.beacon_records,
            )
            if ctx_comments:
                note_lines.append("")
                note_lines.append("CONTEXT COMMENTS (JS/TS):")
                note_lines.extend(ctx_comments)
        return "\n".join(note_lines)


    def next_priority(self) -> int:
        p = self.priority_counter[0]
        self.priority_counter[0] += 100
        return p


    def node_text(self, node: Any) -> str:
        return _js_node_text(node, self.source_bytes)

    def is_function_like(self, v: Any) -> bool:
        return _js_is_function_like(v)

    def callee_simple_name(self, v: Any) -> str:
        return _js_callee_simple_name(v, self.source_bytes)

    def find_function_wrapper_body(self, v: Any) -> Any:
        return _js_find_function_wrapper_body(v, self.source_bytes)

    def find_define_store_setup_body(self, v: Any) -> Any:
        return _js_find_define_store_setup_body(v, self.source_bytes)

    def find_define_store_options_object(self, v: Any) -> Any:
        return _js_find_define_store_options_object(v, self.source_bytes)

    def key_text(self, n: Any) -> str:
        return _js_key_text(n, self.source_bytes)

    def params_text(self, fn_node: Any) -> str:
        return _js_params_text(fn_node, self.source_bytes)

    def body_node(self, fn_node: Any) -> Any:
        return _js_body_node(fn_node)


def _js_walk_extract_define_store_options_children(
    ctx: "JsTsOutlineContext",
    *,
    options_obj: Any,
    store_inner_qual: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def _emit_obj_functions(*, section: str, obj_node: Any) -> None:
        if obj_node is None:
            return
        for prop in getattr(obj_node, "children", []) or []:
            pt = getattr(prop, "type", None)
            if pt == "method_definition":
                name_n = prop.child_by_field_name("name")
                fname = ctx.key_text(name_n) or "anonymous"
                inner_qual = f"{store_inner_qual}.{section}.{fname}"
                ast_qual = ctx.qualify(inner_qual)
                start_line_local = prop.start_point[0] + 1
                end_line_local = prop.end_point[0] + 1
                start_line_abs = ctx.abs_line(start_line_local)
                end_line_abs = ctx.abs_line(end_line_local)
                sig = f"{fname}{ctx.params_text(prop)}"
                note_text = ctx.make_note(ast_qual, start_line_local)
                body = ctx.body_node(prop)
                children_local = _js_walk(
                    body,
                    ctx,
                    qual_prefix=inner_qual,
                    scope_state=_new_js_scope_state(is_lambda=False),
                ) if body is not None else []
                out.append({
                    "name": f"{EMOJI_FUNC} {section} {sig}",
                    "priority": ctx.next_priority(),
                    "note": note_text,
                    "children": children_local,
                    "ast_type": "method",
                    "ast_name": fname,
                    "ast_qualname": ast_qual,
                    "file_path": ctx.file_path,
                    "orig_lineno_start_unused": start_line_abs,
                    "orig_lineno_end_unused": end_line_abs,
                })
                continue
            if pt == "pair":
                k = prop.child_by_field_name("key")
                v = prop.child_by_field_name("value")
                fname = ctx.key_text(k) or "anonymous"
                if v is None or getattr(v, "type", None) not in {"arrow_function", "function"}:
                    continue
                inner_qual = f"{store_inner_qual}.{section}.{fname}"
                ast_qual = ctx.qualify(inner_qual)
                start_line_local = prop.start_point[0] + 1
                end_line_local = prop.end_point[0] + 1
                start_line_abs = ctx.abs_line(start_line_local)
                end_line_abs = ctx.abs_line(end_line_local)
                sig = f"{fname}{ctx.params_text(v)}"
                note_text = ctx.make_note(ast_qual, start_line_local)
                body = ctx.body_node(v)
                children_local = _js_walk(
                    body,
                    ctx,
                    qual_prefix=inner_qual,
                    scope_state=_new_js_scope_state(is_lambda=False),
                ) if body is not None else []
                out.append({
                    "name": f"{EMOJI_FUNC} {section} {sig}",
                    "priority": ctx.next_priority(),
                    "note": note_text,
                    "children": children_local,
                    "ast_type": "function",
                    "ast_name": fname,
                    "ast_qualname": ast_qual,
                    "file_path": ctx.file_path,
                    "orig_lineno_start_unused": start_line_abs,
                    "orig_lineno_end_unused": end_line_abs,
                })
                continue

    for prop in getattr(options_obj, "children", []) or []:
        if getattr(prop, "type", None) != "pair":
            continue
        k = prop.child_by_field_name("key")
        v = prop.child_by_field_name("value")
        key = ctx.key_text(k)
        if key not in {"actions", "getters"}:
            continue
        if v is None or getattr(v, "type", None) != "object":
            continue
        _emit_obj_functions(section=key[:-1] if key.endswith("s") else key, obj_node=v)

    return out


def _js_walk(
    node: Any,
    ctx: "JsTsOutlineContext",
    qual_prefix: Optional[str] = None,
    scope_state: Optional[dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    if scope_state is None:
        scope_state = _new_js_scope_state(is_lambda=False)

    results: List[Dict[str, Any]] = []

    for child in node.children:
        t = child.type

        if t == "class_declaration":
            name_node = child.child_by_field_name("name")
            class_name = _js_collapse_signature_ws(ctx.node_text(name_node)) or "AnonymousClass"
            inner_qual = class_name if not qual_prefix else f"{qual_prefix}.{class_name}"
            ast_qual = ctx.qualify(inner_qual)
            start_line_local = child.start_point[0] + 1
            end_line_local = child.end_point[0] + 1
            start_line_abs = ctx.abs_line(start_line_local)
            end_line_abs = ctx.abs_line(end_line_local)

            note_text = ctx.make_note(ast_qual, start_line_local)

            class_node: Dict[str, Any] = {
                "name": f"{EMOJI_CLASS} class {class_name}",
                "priority": ctx.next_priority(),
                "note": note_text,
                "children": [],
                "ast_type": "class",
                "ast_name": class_name,
                "ast_qualname": ast_qual,
                "file_path": ctx.file_path,
                "orig_lineno_start_unused": start_line_abs,
                "orig_lineno_end_unused": end_line_abs,
            }

            body = child.child_by_field_name("body")
            if body is not None:
                class_node["children"] = _js_walk(
                    body,
                    ctx,
                    qual_prefix=inner_qual,
                    scope_state=_new_js_scope_state(is_lambda=False),
                )

            results.append(class_node)
            continue

        if t in {"interface_declaration", "type_alias_declaration", "enum_declaration"}:
            name_node = child.child_by_field_name("name")
            type_name = _js_collapse_signature_ws(ctx.node_text(name_node)) or "anonymous"
            inner_qual = type_name if not qual_prefix else f"{qual_prefix}.{type_name}"
            ast_qual = ctx.qualify(inner_qual)
            start_line_local = child.start_point[0] + 1
            end_line_local = child.end_point[0] + 1
            start_line_abs = ctx.abs_line(start_line_local)
            end_line_abs = ctx.abs_line(end_line_local)
            note_text = ctx.make_note(ast_qual, start_line_local)

            kind_label = (
                "interface" if t == "interface_declaration" else
                "type" if t == "type_alias_declaration" else
                "enum"
            )
            type_node: Dict[str, Any] = {
                "name": f"{EMOJI_CLASS} {kind_label} {type_name}",
                "priority": ctx.next_priority(),
                "note": note_text,
                "children": [],
                "ast_type": kind_label,
                "ast_name": type_name,
                "ast_qualname": ast_qual,
                "file_path": ctx.file_path,
                "orig_lineno_start_unused": start_line_abs,
                "orig_lineno_end_unused": end_line_abs,
            }
            results.append(type_node)
            continue

        if t == "function_declaration":
            name_node = child.child_by_field_name("name")
            func_name = _js_collapse_signature_ws(ctx.node_text(name_node)) or "anonymous"
            inner_qual = func_name if not qual_prefix else f"{qual_prefix}.{func_name}"
            ast_qual = ctx.qualify(inner_qual)
            start_line_local = child.start_point[0] + 1
            end_line_local = child.end_point[0] + 1
            start_line_abs = ctx.abs_line(start_line_local)
            end_line_abs = ctx.abs_line(end_line_local)

            signature = func_name
            params_node = child.child_by_field_name("parameters")
            if params_node is not None:
                params_text = _js_collapse_signature_ws(ctx.node_text(params_node))
                signature = f"{func_name}{params_text}"

            note_text = ctx.make_note(ast_qual, start_line_local)

            func_node: Dict[str, Any] = {
                "name": f"{EMOJI_FUNC} function {signature}",
                "priority": ctx.next_priority(),
                "note": note_text,
                "children": [],
                "ast_type": "function",
                "ast_name": func_name,
                "ast_qualname": ast_qual,
                "file_path": ctx.file_path,
                "orig_lineno_start_unused": start_line_abs,
                "orig_lineno_end_unused": end_line_abs,
            }

            body = child.child_by_field_name("body")
            if body is not None:
                func_node["children"] = _js_walk(
                    body,
                    ctx,
                    qual_prefix=inner_qual,
                    scope_state=_new_js_scope_state(is_lambda=False),
                )

            results.append(func_node)
            continue

        if t == "method_definition":
            name_node = child.child_by_field_name("name")
            method_name = _js_collapse_signature_ws(ctx.node_text(name_node)) or "anonymous"
            inner_qual = method_name if not qual_prefix else f"{qual_prefix}.{method_name}"
            ast_qual = ctx.qualify(inner_qual)
            start_line_local = child.start_point[0] + 1
            end_line_local = child.end_point[0] + 1
            start_line_abs = ctx.abs_line(start_line_local)
            end_line_abs = ctx.abs_line(end_line_local)

            params_node = child.child_by_field_name("parameters")
            signature = method_name
            if params_node is not None:
                params_text = _js_collapse_signature_ws(ctx.node_text(params_node))
                signature = f"{method_name}{params_text}"

            note_text = ctx.make_note(ast_qual, start_line_local)

            method_node: Dict[str, Any] = {
                "name": f"{EMOJI_FUNC} method {signature}",
                "priority": ctx.next_priority(),
                "note": note_text,
                "children": [],
                "ast_type": "method",
                "ast_name": method_name,
                "ast_qualname": ast_qual,
                "file_path": ctx.file_path,
                "orig_lineno_start_unused": start_line_abs,
                "orig_lineno_end_unused": end_line_abs,
            }

            body = child.child_by_field_name("body")
            if body is not None:
                method_node["children"] = _js_walk(
                    body,
                    ctx,
                    qual_prefix=inner_qual,
                    scope_state=_new_js_scope_state(is_lambda=False),
                )

            results.append(method_node)
            continue

        if t in {"lexical_declaration", "variable_declaration"}:
            decl_text = ctx.node_text(child).lstrip()
            decl_kind = (decl_text.split(None, 1)[0] if decl_text else "const").strip()
            if decl_kind not in {"const", "let", "var"}:
                decl_kind = "const"

            for decl_child in child.children:
                if decl_child.type != "variable_declarator":
                    continue
                name_node = decl_child.child_by_field_name("name")
                if name_node is None:
                    continue
                if getattr(name_node, "type", None) != "identifier":
                    continue
                var_name = ctx.node_text(name_node).strip()
                if not var_name:
                    continue

                value_node = decl_child.child_by_field_name("value")
                setup_body = None
                options_obj = None
                if value_node is not None:
                    if ctx.is_function_like(value_node):
                        setup_body = value_node.child_by_field_name("body")
                    else:
                        setup_body = ctx.find_function_wrapper_body(value_node)
                        if setup_body is None:
                            setup_body = ctx.find_define_store_setup_body(value_node)
                            if setup_body is None:
                                options_obj = ctx.find_define_store_options_object(value_node)

                in_lambda_scope = bool(isinstance(scope_state, dict) and scope_state.get("is_lambda"))
                emit_here = (not qual_prefix) or in_lambda_scope or (setup_body is not None) or (options_obj is not None)
                if not emit_here:
                    continue

                start_line_local = decl_child.start_point[0] + 1
                end_line_local = decl_child.end_point[0] + 1
                start_line_abs = ctx.abs_line(start_line_local)
                end_line_abs = ctx.abs_line(end_line_local)

                if in_lambda_scope and setup_body is None and options_obj is None:
                    if not _is_meaningful_js_local_name(var_name):
                        continue

                if in_lambda_scope:
                    local_suffix = _js_next_lambda_local_suffix(scope_state, var_name)
                    inner_qual = f"{qual_prefix}.{var_name}@{local_suffix}"
                else:
                    inner_qual = var_name if not qual_prefix else f"{qual_prefix}.{var_name}"
                ast_qual = ctx.qualify(inner_qual)
                note_text = ctx.make_note(ast_qual, start_line_local)

                children_local: list[dict[str, Any]] = []
                if setup_body is not None:
                    children_local = _js_walk(
                        setup_body,
                        ctx,
                        qual_prefix=inner_qual,
                        scope_state=_new_js_scope_state(is_lambda=False),
                    )
                elif options_obj is not None:
                    children_local = _js_walk_extract_define_store_options_children(
                        ctx,
                        options_obj=options_obj,
                        store_inner_qual=inner_qual,
                    )

                node_dict: Dict[str, Any] = {
                    "name": f"{EMOJI_FUNC} {decl_kind} {var_name}",
                    "priority": ctx.next_priority(),
                    "note": note_text,
                    "children": children_local,
                    "ast_type": f"{decl_kind}_decl",
                    "ast_name": var_name,
                    "ast_qualname": ast_qual,
                    "file_path": ctx.file_path,
                    "orig_lineno_start_unused": start_line_abs,
                    "orig_lineno_end_unused": end_line_abs,
                }
                results.append(node_dict)

            continue

        next_prefix = qual_prefix
        next_scope_state = scope_state
        if t in {"arrow_function", "function", "function_expression"}:
            anon_scope = _js_next_anonymous_scope_name(scope_state)
            next_prefix = f"{qual_prefix}.{anon_scope}" if qual_prefix else anon_scope
            next_scope_state = _new_js_scope_state(is_lambda=True)

        if child.children:
            results.extend(_js_walk(child, ctx, qual_prefix=next_prefix, scope_state=next_scope_state))

    return results


def parse_js_ts_outline_core(
    file_path: str, *, snapshot: SourceSnapshot | None = None,
) -> tuple[List[Dict[str, Any]], List[str]]:
    ext = os.path.splitext(file_path)[1].lower()
    language_name = "javascript"
    if ext == ".ts":
        language_name = "typescript"
    elif ext == ".tsx":
        language_name = "tsx"
    snapshot = snapshot if snapshot is not None else read_source_snapshot(file_path)
    ctx = JsTsOutlineContext.from_source(
        source_text=snapshot.parse_text, file_path=file_path,
        language_name=language_name, line_offset0=0, qual_prefix=None,
    )

    outline_nodes = _js_walk(ctx.root, ctx)

    js_beacons = parse_js_beacon_blocks(ctx.lines)
    apply_js_beacons(file_path, ctx.lines, outline_nodes, js_beacons)
    return outline_nodes, ctx.lines


def _vue_compact_start_tag_text(raw: str) -> str:
    s = (raw or "").replace("\t", " ").replace("\r", "").replace("\n", " ")
    while "  " in s:
        s = s.replace("  ", " ")
    return s.strip()


def _vue_rebase_beacons(
    beacons: list[dict[str, Any]],
    line_offset0: int,
    *,
    keys: Optional[tuple[str, ...]] = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in beacons or []:
        if not isinstance(b, dict):
            continue
        b2 = dict(b)
        for k in (keys if keys is not None else ("comment_line", "span_lineno_start", "span_lineno_end")):
            v = b2.get(k)
            if isinstance(v, int) and v > 0:
                b2[k] = v + line_offset0
        out.append(b2)
    return out


def _parse_vue_sfc_tree(source_bytes: bytes) -> Any:
    parser = Parser()
    parser.language = get_language("vue")
    tree = parser.parse(source_bytes)
    return tree.root_node


def _vue_sfc_sections(snapshot: SourceSnapshot | bytes, root: Any) -> list[dict[str, Any]]:
    legacy_bytes = isinstance(snapshot, (bytes, bytearray))
    legacy_source = bytes(snapshot) if legacy_bytes else None
    if legacy_bytes:
        snapshot = source_snapshot_from_bytes(snapshot)
    norm_bytes = legacy_source if legacy_bytes else snapshot.normalized_text.encode("utf-8")
    blocks: list[dict[str, Any]] = []
    for ch in root.children:
        if ch.type not in {"template_element", "script_element", "style_element"}:
            continue

        start_tag = None
        raw_text = None
        end_tag = None
        for c2 in ch.children:
            if c2.type == "start_tag":
                start_tag = c2
            elif c2.type == "raw_text":
                raw_text = c2
            elif c2.type == "end_tag":
                end_tag = c2

        if start_tag is None:
            continue

        start_tag_text = norm_bytes[start_tag.start_byte:start_tag.end_byte].decode("utf-8", errors="ignore")
        start_tag_compact = _vue_compact_start_tag_text(start_tag_text)

        if raw_text is not None:
            content_row0 = int(raw_text.start_point[0])
            norm_start, norm_end = int(raw_text.start_byte), int(raw_text.end_byte)
        else:
            if end_tag is None:
                continue
            content_row0 = int(start_tag.end_point[0])
            norm_start, norm_end = int(start_tag.end_byte), int(end_tag.start_byte)

        content_start = norm_start if legacy_bytes else snapshot.normalized_byte_offset(norm_start)
        content_end = norm_end if legacy_bytes else snapshot.normalized_byte_offset(norm_end)
        content_start_line = content_row0 + 1
        content_end_line = (
            _vue_content_end_line(snapshot, content_start_line, content_end)
            if content_end > content_start else content_start_line - 1
        )

        blocks.append({
            "type": ch.type,
            "start_tag": start_tag_compact,
            "content_row0": content_row0,
            "raw_text": snapshot.raw_bytes[content_start:content_end].decode("utf-8", errors="ignore"),
            "content_start_line": content_start_line,
            "content_end_line": content_end_line,
            "content_byte_start": content_start,
            "content_byte_end": content_end,
        })
    if legacy_bytes:
        return [{key: section[key] for key in ("type", "start_tag", "content_row0", "raw_text")} for section in blocks]
    return blocks


def _vue_content_end_line(snapshot: SourceSnapshot, start_line: int, content_end: int) -> int:
    line = start_line
    for index in range(start_line, len(snapshot.byte_offsets)):
        if snapshot.byte_offsets[index] > content_end - 1:
            break
        line = index + 1
    return line


_VUE_SECTION_GRAMMAR = {
    "template_element": "markdown",
    "script_element": "js_ts",
    "style_element": "js_ts",
}


def _vue_section_beacons(
    section: dict[str, Any], *, snapshot: SourceSnapshot | None = None, diagnostics: list | None = None,
) -> list[dict[str, Any]]:
    section_type = section.get("type")
    grammar = _VUE_SECTION_GRAMMAR.get(section_type)
    if grammar is None:
        raise ValueError(f"unknown Vue section type {section_type!r}")
    if snapshot is None:
        local_lines = physical_source_lines(str(section.get("raw_text") or ""))
        offset = int(section.get("content_row0") or 0)
        local_diagnostics = [] if diagnostics is not None else None
        beacons = _parse_structural_beacons(grammar, local_lines, diagnostics=local_diagnostics)
        if diagnostics is not None:
            diagnostics.extend(_vue_rebase_beacons(local_diagnostics, offset, keys=("line", "end_line", "opener_line", "read_end_line", "offending_line")))
        return _vue_rebase_beacons(beacons, offset)
    start = int(section.get("content_start_line") or 0)
    end = int(section.get("content_end_line") or 0)
    if end < start:
        return []
    return _scan_source_beacons(
        snapshot,
        grammar,
        section_type=section_type,
        section_start=start,
        section_end=end,
        section_byte_start=section.get("content_byte_start"),
        section_byte_end=section.get("content_byte_end"),
    )


def _vue_section_records(
    snapshot: SourceSnapshot, *, root: Any = None,
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    if root is None:
        root = _parse_vue_sfc_tree(snapshot.normalized_text.encode("utf-8"))
    return [
        (section, _vue_section_beacons(section, snapshot=snapshot))
        for section in _vue_sfc_sections(snapshot, root)
    ]


def _vue_records_for_lines(lines: list[str]) -> list[dict[str, Any]]:
    try:
        pairs = _vue_section_records(_source_line_view(lines))
    except Exception:
        return []
    return [record for _section, records in pairs for record in records]


def parse_sql_beacon_blocks(
    lines: list[str], *, diagnostics: list | None = None
) -> list[dict[str, Any]]:
    return _parse_structural_beacons("sql", lines, diagnostics)


def parse_js_beacon_blocks(
    lines: list[str], *, diagnostics: list | None = None
) -> list[dict[str, Any]]:
    return _parse_structural_beacons("js_ts", lines, diagnostics)


def parse_sh_beacon_blocks(
    lines: list[str], *, diagnostics: list | None = None
) -> list[dict[str, Any]]:
    return _parse_structural_beacons("shell", lines, diagnostics)


def parse_yaml_beacon_blocks(
    lines: list[str], *, diagnostics: list | None = None
) -> list[dict[str, Any]]:
    return _parse_structural_beacons("yaml", lines, diagnostics)


def _extract_sql_beacon_context(
    lines: list[str],
    comment_line: int,
    beacon_id: Optional[str] = None,
    *,
    records: list | None = None,
) -> List[str]:
    records, mask, cleaned = _beacon_context_view(lines, "sql", records=records)
    return _collect_beacon_context(lines, comment_line, records, mask, cleaned)


def apply_sql_beacons(
    lines: list[str],
    file_children: List[Dict[str, Any]],
    beacons: list[dict[str, Any]],
) -> None:
    if not beacons:
        return

    n = len(lines)
    _prepend_duplicate_beacon_id_warning_nodes(file_children, beacons, language="SQL")

    records = _scan_source_beacons(_source_line_view(lines), "sql")

    for beacon in beacons:
        if beacon.get("kind") != "span":
            continue

        b_id = (beacon.get("id") or "").strip()
        role = (beacon.get("role") or "").strip()
        raw_slice_labels = (beacon.get("slice_labels") or "").strip()

        if not role and b_id and "@" in b_id:
            role = b_id.split("@", 1)[0]
        slice_labels = _canonicalize_slice_labels(raw_slice_labels, role)

        show_span, show_span_explicit = _beacon_show_span(beacon)

        display_role = strip_leading_decoration_symbols(role) or b_id or "sql-span-beacon"
        display_slice = slice_labels or "-"

        tag_suffix = _slice_label_tags(slice_labels or display_slice)
        name = f"🔱 {display_role}"
        if tag_suffix:
            name = f"{name} {tag_suffix}"

        context_lines = _extract_sql_beacon_context(
            lines,
            beacon.get("comment_line") or 0,
            beacon_id=b_id,
            records=records,
        )
        comment = strip_leading_decoration_symbols(beacon.get("comment"))

        note_lines = [
            "BEACON (SQL SPAN)",
            f"id: {b_id}",
            f"role: {role}",
            f"slice_labels: {slice_labels}",
            "kind: span",
        ]
        if show_span_explicit:
            note_lines.append(f"show_span: {'true' if show_span else 'false'}")
        if comment:
            note_lines.append(f"comment: {comment}")
        if context_lines:
            note_lines.append("")
            note_lines.append("CONTEXT COMMENTS (SQL):")
            note_lines.extend(context_lines)

        span_text_lines: List[str] = []
        span_start = beacon.get("span_lineno_start")
        span_end = beacon.get("span_lineno_end")
        if isinstance(span_start, int) and isinstance(span_end, int):
            span_start = max(1, span_start)
            span_end = min(n, span_end)
            if span_start <= span_end:
                span_text_lines = lines[span_start - 1 : span_end]
                while span_text_lines and not span_text_lines[0].strip():
                    span_text_lines.pop(0)
                while span_text_lines and not span_text_lines[-1].strip():
                    span_text_lines.pop()

        if show_span and span_text_lines:
            note_lines.append("")
            note_lines.append("SPAN TEXT:")
            note_lines.extend(span_text_lines)

        span_node = {
            "name": name,
            "note": "\n".join(note_lines),
            "children": [],
        }

        file_children.append(span_node)


def _extract_sh_beacon_context(
    lines: list[str],
    comment_line: int,
    beacon_id: Optional[str] = None,
    *,
    records: list | None = None,
) -> List[str]:
    records, mask, cleaned = _beacon_context_view(lines, "shell", records=records)
    return _collect_beacon_context(lines, comment_line, records, mask, cleaned)


def _prepend_duplicate_beacon_id_warning_nodes(
    children: List[Dict[str, Any]],
    beacons: list[dict[str, Any]],
    *,
    language: str,
) -> None:
    if not beacons:
        return
    duplicate_beacon_ids = _duplicate_beacon_ids(beacons)
    if not duplicate_beacon_ids:
        return
    warning_nodes: List[Dict[str, Any]] = []
    for dup_id in duplicate_beacon_ids:
        warning_nodes.append(
            {
                "name": f"🚨 Duplicate beacon ID ({language}): {dup_id}",
                "note": (
                    "This file contains multiple @beacon blocks with the same id.\n\n"
                    "Why this matters:\n"
                    "- Blocks are matched by their id.\n"
                    "- Duplicate ids create ambiguous matches and may cause reorder storms.\n\n"
                    "Fix:\n"
                    "- Make beacon ids unique (or correct @beacon-close blocks).\n"
                    "- Then run the check again."
                ),
                "children": [],
                "_position_hint": "top",
            }
        )
    children[:0] = warning_nodes


def _duplicate_beacon_ids(beacons: list[dict[str, Any]]) -> list[str]:
    seen_beacon_ids: set[str] = set()
    duplicate_beacon_ids: set[str] = set()
    for b in beacons:
        bid = (b.get("id") or "").strip()
        if not bid:
            continue
        if bid in seen_beacon_ids:
            duplicate_beacon_ids.add(bid)
        seen_beacon_ids.add(bid)
    return sorted(duplicate_beacon_ids)


def apply_sh_beacons(
    lines: list[str],
    file_children: List[Dict[str, Any]],
    beacons: list[dict[str, Any]],
) -> None:
    if not beacons:
        return

    n = len(lines)
    _prepend_duplicate_beacon_id_warning_nodes(file_children, beacons, language="SH")

    records = _scan_source_beacons(_source_line_view(lines), "shell")

    for beacon in beacons:
        if beacon.get("kind") != "span":
            continue

        b_id = (beacon.get("id") or "").strip()
        role = (beacon.get("role") or "").strip()
        raw_slice_labels = (beacon.get("slice_labels") or "").strip()

        if not role and b_id and "@" in b_id:
            role = b_id.split("@", 1)[0]
        slice_labels = _canonicalize_slice_labels(raw_slice_labels, role)

        show_span, show_span_explicit = _beacon_show_span(beacon)

        display_role = strip_leading_decoration_symbols(role) or b_id or "sh-span-beacon"
        display_slice = slice_labels or "-"

        tag_suffix = _slice_label_tags(slice_labels or display_slice)
        name = f"🔱 {display_role}"
        if tag_suffix:
            name = f"{name} {tag_suffix}"

        context_lines = _extract_sh_beacon_context(
            lines,
            beacon.get("comment_line") or 0,
            beacon_id=b_id,
            records=records,
        )
        comment = strip_leading_decoration_symbols(beacon.get("comment"))

        note_lines = [
            "BEACON (SH SPAN)",
            f"id: {b_id}",
            f"role: {role}",
            f"slice_labels: {slice_labels}",
            "kind: span",
        ]
        if show_span_explicit:
            note_lines.append(f"show_span: {'true' if show_span else 'false'}")
        if comment:
            note_lines.append(f"comment: {comment}")
        if context_lines:
            note_lines.append("")
            note_lines.append("CONTEXT COMMENTS (SH):")
            note_lines.extend(context_lines)

        span_text_lines: List[str] = []
        span_start = beacon.get("span_lineno_start")
        span_end = beacon.get("span_lineno_end")
        if isinstance(span_start, int) and isinstance(span_end, int):
            span_start = max(1, span_start)
            span_end = min(n, span_end)
            if span_start <= span_end:
                span_text_lines = lines[span_start - 1 : span_end]
                while span_text_lines and not span_text_lines[0].strip():
                    span_text_lines.pop(0)
                while span_text_lines and not span_text_lines[-1].strip():
                    span_text_lines.pop()

        if show_span and span_text_lines:
            note_lines.append("")
            note_lines.append("SPAN TEXT:")
            note_lines.extend(span_text_lines)

        span_node = {
            "name": name,
            "note": "\n".join(note_lines),
            "children": [],
        }

        file_children.append(span_node)


def _extract_yaml_beacon_context(
    lines: list[str],
    comment_line: int,
    beacon_id: Optional[str] = None,
    *,
    records: list | None = None,
) -> List[str]:
    records, mask, cleaned = _beacon_context_view(lines, "yaml", records=records)
    return _collect_beacon_context(lines, comment_line, records, mask, cleaned)


def apply_yaml_beacons(
    lines: list[str],
    file_children: List[Dict[str, Any]],
    beacons: list[dict[str, Any]],
) -> None:
    if not beacons:
        return

    n = len(lines)
    _prepend_duplicate_beacon_id_warning_nodes(file_children, beacons, language="YAML")

    records = _scan_source_beacons(_source_line_view(lines), "yaml")

    for beacon in beacons:
        if beacon.get("kind") != "span":
            continue

        b_id = (beacon.get("id") or "").strip()
        role = (beacon.get("role") or "").strip()
        raw_slice_labels = (beacon.get("slice_labels") or "").strip()

        if not role and b_id and "@" in b_id:
            role = b_id.split("@", 1)[0]
        slice_labels = _canonicalize_slice_labels(raw_slice_labels, role)

        show_span, show_span_explicit = _beacon_show_span(beacon)

        display_role = strip_leading_decoration_symbols(role) or b_id or "yaml-span-beacon"
        display_slice = slice_labels or "-"

        tag_suffix = _slice_label_tags(slice_labels or display_slice)
        name = f"🔱 {display_role}"
        if tag_suffix:
            name = f"{name} {tag_suffix}"

        context_lines = _extract_yaml_beacon_context(
            lines,
            beacon.get("comment_line") or 0,
            beacon_id=b_id,
            records=records,
        )
        comment = strip_leading_decoration_symbols(beacon.get("comment"))

        note_lines = [
            "BEACON (YAML SPAN)",
            f"id: {b_id}",
            f"role: {role}",
            f"slice_labels: {slice_labels}",
            "kind: span",
        ]
        if show_span_explicit:
            note_lines.append(f"show_span: {'true' if show_span else 'false'}")
        if comment:
            note_lines.append(f"comment: {comment}")
        if context_lines:
            note_lines.append("")
            note_lines.append("CONTEXT COMMENTS (YAML):")
            note_lines.extend(context_lines)

        span_text_lines: List[str] = []
        span_start = beacon.get("span_lineno_start")
        span_end = beacon.get("span_lineno_end")
        if isinstance(span_start, int) and isinstance(span_end, int):
            span_start = max(1, span_start)
            span_end = min(n, span_end)
            if span_start <= span_end:
                span_text_lines = lines[span_start - 1 : span_end]
                while span_text_lines and not span_text_lines[0].strip():
                    span_text_lines.pop(0)
                while span_text_lines and not span_text_lines[-1].strip():
                    span_text_lines.pop()

        if show_span and span_text_lines:
            note_lines.append("")
            note_lines.append("SPAN TEXT:")
            note_lines.extend(span_text_lines)

        span_node = {
            "name": name,
            "note": "\n".join(note_lines),
            "children": [],
        }

        file_children.append(span_node)


def apply_js_beacons(
    file_path: str,
    lines: list[str],
    outline_nodes: List[Dict[str, Any]],
    beacons: list[dict[str, Any]],
    *, report_duplicates: bool = True, attachment_structures: Optional[dict] = None,
) -> None:
    if not beacons:
        return

    n = len(lines)
    context_records, context_mask, context_cleaned = _beacon_context_view(lines, "js_ts")
    if attachment_structures is None:
        snapshot = source_snapshot_from_bytes(_source_line_view(lines).raw_bytes, canonical_path=file_path)
        if file_path.lower().endswith(".vue"):
            pairs = _vue_section_records(snapshot)
            attachment_structures = {"language": "vue", "snapshot": snapshot,
                                     "records": [r for _s, rs in pairs for r in rs],
                                     "sections": [s for s, _rs in pairs]}
        else:
            attachment_structures = {"language": "js_ts", "snapshot": snapshot, "records": context_records}
    attachment = beacon_attachment_index(attachment_structures["snapshot"], outline_nodes,
                                        {**attachment_structures, "beacons": beacons})
    attachment_by_line = {b["record"]["line"]: b for b in attachment["blocks"]}
    if report_duplicates:
        _prepend_duplicate_beacon_id_warning_nodes(outline_nodes, beacons, language="JS/TS")

    span_sort_lines: dict[int, int] = {}

    def _node_source_order_line(node: dict[str, Any]) -> int:
        cached = span_sort_lines.get(id(node))
        if isinstance(cached, int):
            return cached
        ln = node.get("orig_lineno_start_unused") if isinstance(node, dict) else None
        if isinstance(ln, int):
            return ln
        return 10**9

    def _insert_child_in_source_order(
        siblings: List[Dict[str, Any]],
        child_node: Dict[str, Any],
        sort_line: int,
    ) -> None:
        span_sort_lines[id(child_node)] = sort_line
        insert_at = len(siblings)
        for idx, existing in enumerate(siblings):
            if _node_source_order_line(existing) > sort_line:
                insert_at = idx
                break
        siblings.insert(insert_at, child_node)

    def _extract_js_beacon_context(comment_line: int) -> List[str]:
        return _collect_beacon_context(lines, comment_line, context_records, context_mask, context_cleaned, stop_at_pair=False)

    for beacon in beacons:
        attached = attachment_by_line.get(beacon.get("comment_line"))
        if attached:
            beacon["kind"] = attached["effective_kind"]
            beacon["_anchor_lineno"] = attached["next_code_line"]

    for beacon in beacons:
        if beacon.get("kind") != "ast":
            continue
        attached = attachment_by_line.get(beacon.get("comment_line"))
        chosen = attached["anchor"]["node"] if attached and attached["anchor"] else None
        if chosen is None:
            continue

        b_id = (beacon.get("id") or "").strip()
        role = (beacon.get("role") or "").strip()
        raw_slice_labels = (beacon.get("slice_labels") or "").strip()
        if not role and b_id and "@" in b_id:
            role = b_id.split("@", 1)[0]
        slice_labels = _canonicalize_slice_labels(raw_slice_labels, role)

        display_role = strip_leading_decoration_symbols(role) or b_id or "js-ast-beacon"
        display_slice = slice_labels or "-"
        tag_suffix = _slice_label_tags(slice_labels or display_slice)

        name = chosen.get("name") or "Untitled"
        if "🔱" not in name:
            name = f"{name} 🔱"
        if tag_suffix:
            name = f"{name} {tag_suffix}"
        chosen["name"] = name

        comment = strip_leading_decoration_symbols(beacon.get("comment"))
        meta_lines = [
            "BEACON (JS/TS AST)",
            f"id: {b_id}",
            f"role: {role}",
            f"slice_labels: {slice_labels}",
            "kind: ast",
        ]
        if comment:
            meta_lines.append(f"comment: {comment}")
        note = chosen.get("note") or ""
        if note:
            note_lines = note.splitlines()
            insert_idx = (
                1
                if (note_lines and note_lines[0].strip().startswith("AST_QUALNAME:"))
                else 0
            )
            new_note_lines: list[str] = []
            new_note_lines.extend(note_lines[:insert_idx])
            new_note_lines.extend(meta_lines)
            if insert_idx < len(note_lines):
                if note_lines[insert_idx].strip():
                    new_note_lines.append("")
                new_note_lines.extend(note_lines[insert_idx:])
            chosen["note"] = "\n".join(new_note_lines)
        else:
            chosen["note"] = "\n".join(meta_lines)

    for beacon in beacons:
        if beacon.get("kind") != "span":
            continue
        start_snippet = beacon.get("start_snippet")
        comment_line = beacon.get("comment_line") or 0

        attachment_line: Optional[int] = None

        if start_snippet:
            norm_snip = normalize_for_match(start_snippet)
            if not norm_snip:
                continue
            start_candidates: list[int] = []
            for idx, line in enumerate(lines, start=1):
                stripped = line.lstrip()
                if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*") or stripped.startswith("*/"):
                    continue
                if norm_snip in normalize_for_match(line):
                    start_candidates.append(idx)
            if not start_candidates:
                continue
            if len(start_candidates) == 1:
                attachment_line = start_candidates[0]
            else:
                after = [ln for ln in start_candidates if ln > comment_line]
                if len(after) == 1:
                    attachment_line = after[0]
                else:
                    continue
        else:
            span_start = beacon.get("span_lineno_start")
            if isinstance(span_start, int):
                attachment_line = span_start
            elif isinstance(comment_line, int) and comment_line > 0:
                attachment_line = comment_line
            else:
                anchor = beacon.get("_anchor_lineno")
                attachment_line = anchor if isinstance(anchor, int) else None

        if attachment_line is None:
            continue

        enclosing = _find_enclosing_ast_node_for_line(outline_nodes, attachment_line)

        b_id = (beacon.get("id") or "").strip()
        role = (beacon.get("role") or "").strip()
        raw_slice_labels = (beacon.get("slice_labels") or "").strip()
        if not role and b_id and "@" in b_id:
            role = b_id.split("@", 1)[0]
        slice_labels = _canonicalize_slice_labels(raw_slice_labels, role)

        show_span, show_span_explicit = _beacon_show_span(beacon)

        display_role = strip_leading_decoration_symbols(role) or b_id or "js-span-beacon"
        display_slice = slice_labels or "-"
        tag_suffix = _slice_label_tags(slice_labels or display_slice)

        name = f"🔱 {display_role}"
        if tag_suffix:
            name = f"{name} {tag_suffix}"

        context_lines = _extract_js_beacon_context(comment_line)
        comment = strip_leading_decoration_symbols(beacon.get("comment"))

        note_lines = [
            "BEACON (JS/TS SPAN)",
            f"id: {b_id}",
            f"role: {role}",
            f"slice_labels: {slice_labels}",
            "kind: span",
        ]
        if show_span_explicit:
            note_lines.append(f"show_span: {'true' if show_span else 'false'}")
        if comment:
            note_lines.append(f"comment: {comment}")
        if context_lines:
            note_lines.append("")
            note_lines.append("CONTEXT COMMENTS (JS/TS):")
            note_lines.extend(context_lines)

        span_start = beacon.get("span_lineno_start")
        span_end = beacon.get("span_lineno_end")
        span_text_lines: List[str] = []
        if isinstance(span_start, int) and isinstance(span_end, int):
            span_start = max(1, span_start)
            span_end = min(n, span_end)
            if span_start <= span_end:
                span_text_lines = lines[span_start - 1 : span_end]
                while span_text_lines and not span_text_lines[0].strip():
                    span_text_lines.pop(0)
                while span_text_lines and not span_text_lines[-1].strip():
                    span_text_lines.pop()

        if show_span and span_text_lines:
            note_lines.append("")
            note_lines.append("SPAN TEXT:")
            note_lines.extend(span_text_lines)

        span_node = {
            "name": name,
            "note": "\n".join(note_lines),
            "children": [],
            "orig_lineno_start_unused": attachment_line,
            "orig_lineno_end_unused": attachment_line,
        }

        if enclosing is not None:
            children = enclosing.get("children") or []
            _insert_child_in_source_order(children, span_node, attachment_line)
            enclosing["children"] = children
        else:
            _insert_child_in_source_order(outline_nodes, span_node, attachment_line)


def _token_is_pure_decoration(token: str) -> bool:
    if not token:
        return False
    return not any(ch.isalnum() for ch in token)


def split_name_and_tags(raw_name: str) -> tuple[str, list[str]]:
    name = (raw_name or "").strip()
    if not name:
        return "", []
    tokens = name.split()
    tags: list[str] = []
    trident = TRIDENT

    def _is_marker_decoration(tok: str) -> bool:
        t = tok.strip()
        return t == trident or t.startswith("\U0001FA9A")

    region_start = len(tokens)
    i = len(tokens) - 1
    while i >= 0:
        candidate = _strip_tag_underline_markup(tokens[i])
        if candidate.startswith("#") or _is_marker_decoration(candidate):
            region_start = i
            i -= 1
            continue
        if _token_is_pure_decoration(candidate):
            i -= 1
            continue
        break

    if region_start >= len(tokens):
        return name, []
    base = " ".join(tokens[:region_start]).strip()
    for tok in tokens[region_start:]:
        candidate = _strip_tag_underline_markup(tok)
        if candidate.startswith("#"):
            tags.append(candidate)
    return base, tags


def plan_beacon_comment_aliases(snapshot: SourceSnapshot, language: str) -> dict:
    records = _beacon_edit_records(snapshot, language)
    edits, witnesses, intents, applied, skipped = [], [], [], [], []
    diagnostics = [{"owner": r["opener_range"][0], **d} for r in records for d in r["diagnostics"]]
    for record in records:
        aliases = [o for o in record["occurrences"] if o["key"] == "comments"]
        uncertain = record["literal_certainty"] != "comment"
        if not aliases and record["complete"] and not uncertain:
            continue
        owner = record["opener_range"][0]
        bid = record["fields"].get("id")
        member = {"owner": owner, "line": record["line"], "id": bid,
                  "fields": dict(record["fields"]), "occurrences": record["occurrences"],
                  "opener_range": record["opener_range"], "field_ranges": record["field_ranges"],
                  "read_range": (owner, record["read_end"]), "write_ranges": record["write_ranges"],
                  "complete": record["complete"], "literal_certainty": record["literal_certainty"],
                  "diagnostics": record["diagnostics"], "repair_candidates": record["repair_candidates"]}
        evidence = {"operation": "rename_comment_alias", "status": "skipped", "owner": owner,
                    "file_path": snapshot.canonical_path, "line": record["line"], "id": bid, "members": [member],
                    "target": {"owner": owner, "section": record["section"]},
                    "retained_ids": [bid] if bid else [], "dropped_ids": [],
                    "before_sha256": snapshot.sha256, "edited_ranges": []}
        ambiguity = [d for d in record["diagnostics"] if d["code"] == "inline_field_ambiguity" or
                     d["code"] == "duplicate_field" and d["conflicting"]]
        reason = ("uncertain_record" if uncertain else "incomplete_record" if not record["complete"] else
                  "canonical_and_alias" if any(o["key"] == "comment" for o in record["occurrences"]) else
                  "ambiguous_keys" if ambiguity or record["id_conflicting"] else None)
        if reason:
            skipped.append({**evidence, "reason": reason, "error": None, "candidates": [member]})
            continue
        witnesses.append(owner)
        intents.append({"owner": owner, "operation": "present", "marker": record["marker"],
                        "fields": {"comment": aliases[-1]["value"], "comments": None}})
        for occurrence in aliases:
            start = occurrence["byte_range"][0] + len("comment")
            edit = BeaconEdit(start, start + 1, b"", owner)
            edits.append(edit)
            applied.append({**evidence, "status": "planned", "line": occurrence["line"],
                            "occurrence_range": occurrence["byte_range"],
                            "edited_ranges": _hygiene_edit_ranges([edit])})
    if not records:
        skipped.append({"operation": "rename_comment_alias", "status": "skipped",
                        "reason": "no_recognized_alias", "candidates": [], "edited_ranges": []})
    return {"edits": edits, "edit_plan": BeaconEditPlan(snapshot, language, tuple(edits), tuple(witnesses), tuple(intents)),
            "applied_aliases": applied, "skipped_aliases": skipped, "diagnostics": diagnostics}


def _indent_beacon_block(block_lines: list[str], source_lines: list[str], insert_idx: int) -> list[str]:
    indent = ""
    for idx in range(insert_idx, len(source_lines)):
        line = source_lines[idx]
        if line.strip():
            indent = line[:len(line) - len(line.lstrip())]
            break
    return [indent + line for line in block_lines]


def _generate_auto_beacon_hash() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


def allocate_beacon_id(taken_ids, *, base_id: str, random_parts: int = 0) -> str:
    taken = set(taken_ids)
    if random_parts:
        for _attempt in range(4):
            candidate = base_id + "".join(_generate_auto_beacon_hash() for _ in range(random_parts))
            if candidate not in taken:
                return candidate
    fallback = base_id + "0000" * random_parts
    if fallback not in taken:
        return fallback
    for suffix in range(2, len(taken) + 3):
        candidate = f"{fallback}-{suffix}"
        if candidate not in taken:
            return candidate


def _beacon_edit_records(snapshot: SourceSnapshot, language: str) -> list[dict]:
    if language == "vue":
        return [record for _section, records in _vue_section_records(snapshot) for record in records]
    return _scan_source_beacons(snapshot, language)


def _parse_snapshot_beacons_for_language(
    snapshot: SourceSnapshot, language: str, *, diagnostics: list | None = None,
    line_view: list[str] | None = None,
) -> tuple[list[str], list[dict]]:
    lines = list(snapshot.physical_lines) if line_view is None else line_view
    if language == "vue":
        beacons: list[dict] = []
        for section, records in _vue_section_records(snapshot):
            beacons.extend(_parse_structural_beacons(
                _VUE_SECTION_GRAMMAR[section["type"]], lines,
                diagnostics=diagnostics, records=records, snapshot=snapshot,
            ))
        return lines, beacons
    return lines, _parse_structural_beacons(language, lines, diagnostics, snapshot=snapshot)


def _beacon_block_extent_for_language(
    language: str,
    lines: list[str],
    beacon: dict,
) -> Optional[tuple[int, int]]:
    if language == "vue":
        records = _vue_records_for_lines(lines)
    else:
        records = _scan_source_beacons(_source_line_view(lines), language)
    return next((r["line_extent"] for r in records
                 if r["marker"] == "open" and r["line"] == beacon.get("comment_line")), None)


def _python_decorator_ranges_by_definition(
    lines: list[str], *, tree: Optional[ast.AST] = None,
) -> dict[int, list[tuple[int, int]]]:
    import io
    import tokenize

    source = "\n".join(lines)
    try:
        if tree is None:
            tree = ast.parse(source)
        targets = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.decorator_list
        ]
        if not targets:
            return {}

        candidates: list[tuple[int, int]] = []
        last_definition_line = max(node.lineno for node in targets)
        first = None
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.start[0] >= last_definition_line:
                break
            if token.type in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT):
                continue
            if token.type == tokenize.NEWLINE:
                if first is not None and first.type == tokenize.OP and first.string == "@":
                    candidates.append((first.start[0] - 1, token.end[0] - 1))
                first = None
            elif first is None:
                first = token

        by_definition: dict[int, list[tuple[int, int]]] = {}
        for target in targets:
            ranges: list[tuple[int, int]] = []
            for decorator in target.decorator_list:
                owned = [
                    (start, end) for start, end in candidates
                    if start <= decorator.lineno - 1
                    and (decorator.end_lineno or decorator.lineno) - 1 <= end
                    and end < target.lineno - 1
                ]
                if len(owned) != 1 or owned[0] in ranges:
                    break
                ranges.append(owned[0])
            else:
                by_definition[target.lineno - 1] = ranges
        return by_definition
    except (SyntaxError, ValueError, tokenize.TokenError):
        return {}


def _attachment_comment_lines(snapshot: SourceSnapshot, language: str, sections=None, records=()) -> list[bool]:
    if language == "vue":
        comments = []
        if sections is None:
            sections = [section for section, _records in _vue_section_records(snapshot)]
        for section in sections:
            if section["type"] != "script_element":
                continue
            a, b = section["content_byte_start"], section["content_byte_end"]
            local = source_snapshot_from_bytes(snapshot.raw_bytes[a:b])
            comments.extend({"start": r["start"] + a, "end": r["end"] + a}
                            for r in source_lexical_evidence(local, "js_ts") if r["certainty"] == "comment")
    elif language == "markdown":
        rows = _beacon_comment_rows(snapshot, language, 1, len(snapshot.physical_lines))
        wrappers = {row["wrapper"]["start"]: row["wrapper"] for row in rows if row["wrapper"]}
        comments = [{"start": w["start"], "end": w["end"] if w["end"] is not None else
                     snapshot.byte_offsets[w["boundary_line"] - 1] if w["boundary_line"] else len(snapshot.raw_bytes)}
                    for w in wrappers.values()]
    else:
        comments = [r for r in source_lexical_evidence(snapshot, language)
                    if r["certainty"] == "comment"]
    for record in records:
        opening = record.get("wrapper_open_range")
        if opening and not record.get("wrapper_range"):
            for region in comments:
                if region["start"] <= opening[0] < region["end"]:
                    region["end"] = min(region["end"], record["read_end"])
    comments.extend({"start": r["wrapper_range"][0], "end": r["wrapper_range"][1]}
                    for r in records if r.get("wrapper_range"))
    comments.extend({"start": a, "end": b} for r in records if r["complete"]
                    for a, b in (r.get("write_ranges") or []))
    comments.sort(key=lambda r: (r["start"], r["end"]))
    result = []
    first = 0
    for index, line in enumerate(snapshot.physical_lines):
        left = snapshot.byte_offsets[index]
        right = snapshot.byte_offset(index + 1, len(line))
        cursor = left
        clean = True
        while first < len(comments) and comments[first]["end"] <= left:
            first += 1
        for region_index in range(first, len(comments)):
            region = comments[region_index]
            if region["end"] <= cursor:
                continue
            if region["start"] >= right:
                break
            if snapshot.raw_bytes[cursor:min(region["start"], right)].strip():
                clean = False
                break
            cursor = min(right, max(cursor, region["end"]))
            if cursor == right:
                break
        result.append(clean and not snapshot.raw_bytes[cursor:right].strip())
    return result


def _js_owned_decorator_syntax(snapshot: SourceSnapshot, language: str, sections=None) -> dict:
    if not _HAVE_TREE_SITTER:
        return {}
    if language == "vue":
        parts = []
        if sections is None:
            sections = [section for section, _records in _vue_section_records(snapshot)]
        for section in sections:
            if section["type"] != "script_element":
                continue
            a, b = section["content_byte_start"], section["content_byte_end"]
            tag = str(section.get("start_tag") or "").lower()
            syntax = "tsx" if re.search(r"lang\s*=\s*['\"]?tsx", tag) else "typescript"
            parts.append((source_snapshot_from_bytes(snapshot.raw_bytes[a:b]),
                          section["content_start_line"] - 1, syntax))
    else:
        ext = os.path.splitext(snapshot.canonical_path)[1].lower()
        syntax = "tsx" if ext in {".tsx", ".jsx"} else "typescript"
        parts = [(snapshot, 0, syntax)]
    result = {}
    for part, offset, syntax in parts:
        parser = Parser()
        parser.language = get_language(syntax)
        root = parser.parse(part.normalized_text.encode("utf-8")).root_node

        def visit(parent):
            pending = []
            for node in parent.named_children:
                if node.type == "decorator":
                    pending.append(node)
                    continue
                owned = [c for c in node.named_children if c.type == "decorator"]
                if node.type in {"class_declaration", "method_definition", "export_statement"}:
                    owned = pending + owned
                    if owned and not any(d.has_error for d in owned):
                        declaration = node.child_by_field_name("declaration") if node.type == "export_statement" else node
                        if declaration is not None:
                            tokens = [c for c in declaration.children if c.type not in {"decorator", "comment"}]
                            if tokens:
                                definition = tokens[0].start_point[0] + offset
                                ranges = [(d.start_point[0] + offset, d.end_point[0] + offset) for d in owned]
                                ranges = [(a, min(b, definition - 1)) for a, b in ranges if a < definition]
                                result[declaration.start_point[0] + offset] = {
                                    "definition": definition, "ranges": ranges,
                                }
                if node.type != "comment":
                    pending = []
                visit(node)
        visit(root)
    return result


def beacon_attachment_index(snapshot: SourceSnapshot, outline: list[dict], structures: dict) -> dict:
    language = structures["language"]
    records = structures["records"]
    lines = list(snapshot.physical_lines)
    beacons = structures.get("beacons")
    if beacons is None:
        beacons = _parse_structural_beacons(language, lines, records=records)
    projected = {b["comment_line"]: b for b in beacons}
    comments = _attachment_comment_lines(snapshot, language, structures.get("sections"), records)
    decorators = structures.get("decorator_ranges_by_definition")
    js_syntax = {}
    if language == "python":
        if decorators is None:
            decorators = _python_decorator_ranges_by_definition(lines)
    elif language in {"js_ts", "vue"}:
        js_syntax = _js_owned_decorator_syntax(snapshot, language, structures.get("sections"))
        decorators = {key: value["ranges"] for key, value in js_syntax.items()}
    decorators = decorators or {}
    decorator_lines = {i for spans in decorators.values() for a, b in spans for i in range(a, b + 1)}
    eligible = ({"class", "function", "async_function", "const", "const_decl"}
                if language == "python" else
                {"class", "function", "method", "const_decl", "let_decl", "var_decl"})
    anchors = []

    def walk(nodes):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            original = node.get("_md_heading_line") if language == "markdown" else node.get("orig_lineno_start_unused")
            valid = language == "markdown" or node.get("ast_type") in eligible
            if valid and isinstance(original, int) and 0 < original <= len(lines):
                definition = js_syntax.get(original - 1, {}).get("definition", original - 1)
                anchors.append({"line": definition + 1, "outline_line": original,
                                "ast_qualname": node.get("ast_qualname"),
                                "node": node, "decorator_ranges": decorators.get(original - 1, []),
                                "header": lines[definition]})
            walk(node.get("children") or [])
    walk(outline)
    anchors.sort(key=lambda a: a["line"])
    blocks = []
    for record in records:
        if record["marker"] != "open":
            continue
        beacon = projected.get(record["line"])
        fields = record["fields"]
        kind = (fields.get("kind") or "span").strip().lower() or "span"
        explicit = bool(fields.get("kind"))
        section = record.get("section") or {}
        first = None
        gap = {"comment_lines": [], "decorator_lines": [], "blank_lines": [], "prose_lines": []}
        embedded = record["line"] - 1 in decorator_lines
        stop = min(len(lines), section.get("end_line", len(lines)))
        for index in range(record["read_end_line"], stop):
            if index in decorator_lines:
                gap["decorator_lines"].append(index + 1)
            elif not lines[index].strip():
                gap["blank_lines"].append(index + 1)
            elif comments[index]:
                gap["comment_lines"].append(index + 1)
            else:
                first = index + 1
                break
        anchor = None
        effective = kind
        can_promote = (not explicit and not record["closed"] and not record["pair_candidates"]
                       and not fields.get("start_snippet"))
        ast_allowed = kind == "ast" or (kind == "span" and can_promote)
        if language == "markdown":
            following = [a for a in anchors if a["line"] > record["line"]]
            if ast_allowed and following:
                candidate = following[0]
                if explicit or candidate["line"] == first:
                    anchor = candidate
                    effective = "ast"
                    gap["prose_lines"] = [i + 1 for i in range(record["read_end_line"], candidate["line"] - 1)
                                           if lines[i].strip() and not comments[i]]
        elif language in {"python", "js_ts", "vue"} and ast_allowed and not embedded:
            candidates = [a for a in anchors if a["line"] == first]
            if len(candidates) == 1:
                anchor, effective = candidates[0], "ast"
        blocks.append({"owner": record["opener_range"][0], "record": record,
                       "beacon": beacon, "anchor": anchor, "effective_kind": effective,
                       "next_code_line": first, "embedded_in_decorator": embedded, "gap": gap})
    return {"language": language, "anchors": anchors, "blocks": blocks,
            "by_opener": {b["owner"]: b for b in blocks}}


def _beacon_anchor_descriptor(anchor: dict | None) -> dict | None:
    if anchor is None:
        return None
    node = anchor["node"]
    return {"line": anchor["line"], "outline_line": anchor["outline_line"],
            "ast_qualname": anchor.get("ast_qualname"),
            "md_path": _extract_md_path_from_note(node.get("note")) if not anchor.get("ast_qualname") else None,
            "header": anchor["header"],
            "decorator_ranges": [list(r) for r in anchor.get("decorator_ranges", [])]}


def _beacon_physical_witness(snapshot: SourceSnapshot, block: dict | None, anchor=None) -> dict:
    record = block["record"] if block else None
    return {"section": dict(record.get("section") or {}) if record else {},
            "opener_start_byte": record["opener_range"][0] if record else None,
            "block_hash": hashlib.sha256(snapshot.raw_bytes[record["opener_range"][0]:record["read_end"]]).hexdigest()
            if record else None,
            "anchor": _beacon_anchor_descriptor(block.get("anchor") if block else anchor)}


def _beacon_candidate(block: dict) -> dict:
    record = block["record"]
    fields = record["fields"]
    return {"line": record["line"], "kind": block["effective_kind"], "id": fields.get("id"),
            "labels": fields.get("slice_labels"), "comment": fields.get("comment"),
            "section": dict(record.get("section") or {}), "opener_start_byte": block["owner"],
            "anchor": _beacon_anchor_descriptor(block.get("anchor")),
            "complete": record["complete"], "write_ranges": record.get("write_ranges"),
            "terminated_by": record.get("terminated_by"), "gap": block["gap"],
            "repair_candidates": record.get("repair_candidates", [])}


def _beacon_header_identity(name: str) -> str:
    return strip_leading_decoration_symbols(split_name_and_tags(name)[0]).strip()


def beacon_id_resemblance(snapshot: SourceSnapshot, block: dict | None, anchor: dict | None,
                          anchors: list[dict], blocks: list[dict]) -> list[dict]:
    if block is None or anchor is None:
        return []
    fields = block["record"]["fields"]
    bid = fields.get("id") or ""
    symbol = anchor.get("ast_qualname")
    if not symbol:
        return []
    import difflib

    def fold(value):
        value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
        value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
        return ".".join(part.replace("-", "_").casefold().strip("_") for part in value.split("."))

    tail = bid.split("@", 1)[-1]
    core = re.sub(r"-[a-z0-9]{4}$", "", tail)
    leaf = symbol.rsplit(".", 1)[-1]
    full, short = fold(symbol), fold(leaf)
    if any(value in {symbol, leaf} or fold(value) in {full, short} for value in (tail, core)):
        return []
    folded = fold(core)
    core_leaf = folded.rsplit(".", 1)[-1]
    others = [a for a in anchors if a.get("ast_qualname") and a["ast_qualname"] != symbol
              and (fold(a["ast_qualname"]) == folded or
                   ("." not in folded and fold(a["ast_qualname"].rsplit(".", 1)[-1]) == folded))]
    relation = None
    if others:
        relation = "R8"
    elif "." in core and core_leaf == short:
        relation = "R3"
    elif len(short) >= 3 and (core_leaf.startswith(short) or core_leaf.endswith(short)):
        return []
    else:
        words, symbol_words = set(core_leaf.split("_")), set(short.split("_"))
        if words <= symbol_words or len(words & symbol_words) / max(1, len(words | symbol_words)) >= 0.34:
            return []
        if difflib.SequenceMatcher(None, core_leaf, short).ratio() >= 0.8:
            relation = "R7"
    if relation is None:
        return []
    named = []
    for other in others:
        attached = [b for b in blocks if b.get("anchor") is other]
        named.append({**_beacon_anchor_descriptor(other), "has_beacon": bool(attached),
                      "beacon_ids": [b["record"]["fields"].get("id") for b in attached]})
    return [{"type": "id_symbol_resemblance", "relation": relation, "advisory": True,
             "file": snapshot.canonical_path, "beacon_id": bid, "line": block["record"]["line"],
             "id_symbol": core, "attached_symbol": _beacon_anchor_descriptor(anchor),
             "named_symbols": named, "role": fields.get("role"),
             "enclosing_path": {"id": core.rpartition(".")[0], "current": symbol.rpartition(".")[0]}
             if relation == "R3" else None,
             "suggestions": ["rename_id", "relocate", "edit_descriptive_field", "keep"]}]


def select_beacon_block_for_node(snapshot: SourceSnapshot, structures: dict, target: dict) -> dict:
    language = structures["language"]
    index = beacon_attachment_index(snapshot, structures.get("outline", []), structures)
    blocks = index["blocks"]
    section = target.get("section")
    if section is not None:
        blocks = [b for b in blocks if (b["record"].get("section") or {}) == section]
    bid = (target.get("note_beacon_id") or "").strip()
    ids = [b for b in blocks if bid and b["record"]["fields"].get("id") == bid]
    anchors = index["anchors"]
    qualname, md_path = target.get("ast_qualname"), target.get("md_path")
    carried = bool(qualname and language in {"python", "js_ts", "vue"}) or bool(md_path and language == "markdown")
    if qualname:
        anchors = [a for a in anchors if a.get("ast_qualname") == qualname]
    elif md_path:
        desired = _markdown_path_identity(md_path)
        anchors = [a for a in anchors if desired and
                   _markdown_path_identity(_extract_md_path_from_note(a["node"].get("note"))) == desired]
    else:
        anchors = []
    if section is not None:
        anchors = [a for a in anchors if section.get("start_line", 1) <= a["line"] <= section.get("end_line", len(snapshot.physical_lines))]

    evidence = [_beacon_candidate(b) for b in blocks]
    answer = {"target": dict(target), "candidates": evidence, "diagnostics": [], "anchor": None,
              "block": None, "physical_witness": None, "status": "candidates"}

    def finish(status, block=None, anchor=None, error_kind=None, error=None):
        answer.update(status=status, block=block, anchor=anchor,
                      physical_witness=_beacon_physical_witness(snapshot, block, anchor)
                      if block is not None or anchor is not None else None)
        if block is None and anchor is not None and language == "vue":
            sections = structures.get("sections") or [s for s, _records in _vue_section_records(snapshot)]
            answer["physical_witness"]["section"] = next(
                ({"type": s["type"], "start_line": s["content_start_line"], "end_line": s["content_end_line"],
                  "start_byte": s["content_byte_start"], "end_byte": s["content_byte_end"]}
                 for s in sections if s["content_start_line"] <= anchor["line"] <= s["content_end_line"]), {})
        elif block is None and anchor is not None:
            answer["physical_witness"]["section"] = {"type": language, "start_line": 1,
                                                       "end_line": len(snapshot.physical_lines)}
        if error_kind:
            answer.update(error_kind=error_kind, error=error)
        answer["resemblance_evidence"] = beacon_id_resemblance(snapshot, block, anchor, index["anchors"], index["blocks"])
        return answer

    def choose(block, anchor=None, adopted=False):
        same_ids = [b for b in index["blocks"] if b["record"]["fields"].get("id") == block["record"]["fields"].get("id")]
        if len(same_ids) > 1:
            answer["diagnostics"].append({"type": "duplicate_id", "candidates": [_beacon_candidate(b) for b in same_ids]})
        if adopted:
            answer['diagnostics'].append({'type': 'beacon_id_adopted', 'note_beacon_id': bid or None,
                                          'disk_beacon_id': block['record']['fields'].get('id'),
                                          'candidate': _beacon_candidate(block)})
        return finish("adopt" if adopted else "selected", block, anchor)

    if target.get('operation') == 'rehydrate' and not (language == 'markdown' and md_path):
        return finish('candidates', error_kind='no_beacon_anchor_for_node',
                      error='Rehydration needs the generated heading MD_PATH and occurrence, never an id-only target.')
    if not carried:
        if len(ids) == 1:
            return choose(ids[0])
        if len(ids) > 1:
            return finish("candidates", error_kind="ambiguous_beacon_id", error="Exact-id target has several physical members; choose a member from candidates.")
        if target.get("operation") == "delta_remove":
            return finish("satisfied_noop")
        if bid:
            return finish("conflict", error_kind="beacon_identity_conflict", error="The note's beacon id is absent on disk; inspect the current candidates and the cached identity.")
        return finish("create")

    if not anchors:
        return finish("candidates", error_kind="no_beacon_anchor_for_node", error="The carried source anchor does not resolve on disk; no id-only retarget is authorized.")
    anchor = anchors[0] if len(anchors) == 1 else None
    if anchor is None:
        attached_ids = [b for b in ids if b.get("anchor") in anchors]
        if len(attached_ids) == 1 and target.get('operation') != 'rehydrate':
            anchor = attached_ids[0]["anchor"]
        elif language == 'markdown' and isinstance(target.get('occurrence'), int):
            ordinal = target['occurrence']
            anchor = anchors[ordinal - 1] if 1 <= ordinal <= len(anchors) else None
        if anchor is None:
            witness = target.get("twin_witness") or {}
            ordinal = witness.get("ordinal")
            current = [_beacon_header_identity(a["node"].get("name") or "") for a in anchors]
            matching_identity = (witness.get("ast_qualname") == qualname if qualname
                                 else witness.get("md_path") == md_path)
            backed = (matching_identity and witness.get("count") == len(anchors)
                      and witness.get("headers") == current and len(set(current)) == len(current))
            pinned = (witness.get("source_sha256") == snapshot.sha256
                      and witness.get("count") == len(anchors)
                      and witness.get("source_headers") == [a["header"] for a in anchors]
                      and matching_identity)
            if isinstance(ordinal, int) and 1 <= ordinal <= len(anchors) and (backed or pinned):
                anchor = anchors[ordinal - 1]
        if anchor is None:
            answer["anchor_candidates"] = [_beacon_anchor_descriptor(a) for a in anchors]
            answer["candidates"] += [{"line": a["line"], "kind": "ast", "id": None, "labels": None,
                                       "comment": None, "anchor": _beacon_anchor_descriptor(a)}
                                      for a in anchors if not any(b.get("anchor") is a for b in blocks)]
            return finish("candidates", error_kind="ambiguous_beacon_anchor", error="Several source anchors remain; an ordinal or old line alone cannot choose a twin.")
    attached = [b for b in blocks if b.get("anchor") is anchor]
    physical = [b for b in ids if b in attached]
    if len(physical) == 1:
        return choose(physical[0], anchor)
    if len(physical) > 1:
        return finish("candidates", anchor=anchor, error_kind="ambiguous_beacon_id", error="Several equal-id blocks attach to this same anchor.")
    if target.get('operation') == 'rehydrate':
        if ids:
            answer['diagnostics'].append({'type': 'duplicate_id', 'candidates': [_beacon_candidate(b) for b in ids]})
        if len(attached) > 1:
            return finish('candidates', anchor=anchor, error_kind='ambiguous_beacon_anchor',
                          error='Several blocks attach to the generated heading; choose its metadata member.')
        if attached:
            return choose(attached[0], anchor, adopted=True)
        claimed = [b for b in ids if not b['record']['complete'] and
                   b.get('next_code_line') == anchor['line']]
        if claimed:
            return finish('candidates', anchor=anchor, error_kind='beacon_extent_unreadable',
                          error='Incomplete claimed metadata at the generated heading needs its extent resolved; candidates retain its owned ranges.')
        return finish('create', anchor=anchor)
    damaged = [b for b in ids if not b["record"]["complete"]
               and (b.get("anchor") is None or b.get("anchor") is anchor)]
    elsewhere = [b for b in ids if b.get("anchor") is not None and b.get("anchor") is not anchor]
    if elsewhere:
        return finish("conflict", anchor=anchor, error_kind="beacon_identity_conflict", error="The note id is attached at a different source anchor; inspect both physical candidates rather than refreshing or guessing a target.")
    if len(damaged) == 1:
        return choose(damaged[0], anchor)
    if len(damaged) > 1 or len(attached) > 1:
        return finish("candidates", anchor=anchor, error_kind="ambiguous_beacon_anchor", error="Several claimed or attached blocks remain at this source anchor.")
    if attached:
        return choose(attached[0], anchor, adopted=True)
    orphans = [b for b in blocks if b.get("anchor") is None]
    if orphans:
        answer["diagnostics"].append({"type": "detached_candidates", "candidates": [_beacon_candidate(b) for b in orphans]})
    return finish("satisfied_noop" if target.get("operation") == "delta_remove" else "create", anchor=anchor)


def _beacon_selection_target_key(snapshot: SourceSnapshot, selection: dict, bid=None) -> tuple | None:
    witness = selection.get("physical_witness")
    if not witness:
        return None
    target = selection["target"]
    identity = f"id::{bid}" if selection.get("block") else (
        f"md::{target.get('occurrence') or 1}::{'|'.join(target.get('md_path') or [])}"
        if target.get("md_path") else f"ast::{target.get('ast_qualname')}")
    return (os.path.normcase(os.path.realpath(snapshot.canonical_path)), identity,
            json.dumps(witness["section"], sort_keys=True), witness["opener_start_byte"],
            witness["block_hash"], json.dumps(witness["anchor"], sort_keys=True))


def plan_beacon_node_mutation(snapshot: SourceSnapshot, selection: dict, intent: dict) -> dict:
    target = selection["target"]
    language = target["language"]
    block = selection.get("block")
    record = block["record"] if block else None
    beacon = (block.get("beacon") or record["fields"]) if block else {}
    result = {"success": True, "edit_plan": None, "selection": selection,
              "candidates": selection["candidates"], "diagnostics": list(selection["diagnostics"]),
              "physical_witness": selection.get("physical_witness"), "beacon_id": beacon.get("id"),
              "disk_comment": beacon.get("comment"), "noop_reason": None}
    result["resemblance_evidence"] = list(selection.get("resemblance_evidence", ()))
    result["diagnostics"].extend(result["resemblance_evidence"])

    def fail(kind, error):
        result.update(success=False, error_kind=kind, error=error, planned_operation=None)
        return result

    if selection["status"] in {"conflict", "candidates"}:
        return fail(selection["error_kind"], selection["error"])
    create_capable = language in {"python", "js_ts", "markdown"} or (
        language == "vue" and str(target.get("ast_qualname") or "").startswith("vue:script"))
    if block and language == "vue" and intent["kind"] != "absolute_labels" and (record.get("section") or {}).get("type") != "script_element":
        return fail("operation_not_supported_for_target", "Vue tags delta supports script targets only; template/style legacy capability is unchanged.")
    rehydrate = intent['kind'] == 'rehydrate'
    absolute = intent["kind"] in {'absolute_labels', 'rehydrate'}
    tags = intent.get("tags")
    if tags is None:
        tags = split_name_and_tags(intent.get("name") or "")[1] if absolute else ()
    requested = [t for t in _canonicalize_slice_labels(",".join(tags), None).split(",") if t]
    disk_labels = [t for t in _canonicalize_slice_labels(beacon.get("slice_labels") or "", None).split(",") if t]
    note_records = intent.get("note_metadata")
    if note_records is None:
        note_records = parse_beacon_note_metadata(intent.get("note"))
    label_occurrences = [o for o in (record or {}).get("occurrences", ()) if o["key"] == "slice_labels"]
    label_ambiguous = len(label_occurrences) > 1 and not (absolute and not requested)
    field_errors = []
    if label_ambiguous:
        field_errors.append({"field": "slice_labels", "error_kind": "ambiguous_beacon_field",
                             "candidates": label_occurrences})
    if "intended_labels" in intent:
        labels = list(intent["intended_labels"])
    elif absolute:
        labels = (requested if rehydrate or note_records or not requested else
                  disk_labels + [t for t in requested if t not in disk_labels])
    elif intent["kind"] == "delta_ensure":
        labels = disk_labels + [t for t in requested if t not in disk_labels]
    else:
        labels = [t for t in disk_labels if t not in set(requested)]
    if label_ambiguous:
        labels = disk_labels
    header_id = target.get('note_beacon_id') if rehydrate else beacon.get('id')
    headers = [r for r in note_records if r.get('id') == header_id] if header_id else []
    headers = headers or note_records
    note_fields = {}
    if language == 'markdown' and (rehydrate or selection['status'] == 'create'):
        for key in ('id', 'kind', 'show_span'):
            occurrences = [v for r in headers for v in r['occurrences'] if v['key'] == key]
            if len(occurrences) > 1:
                field_errors.append({'field': key, 'error_kind': 'ambiguous_beacon_field', 'candidates': occurrences})
            elif occurrences:
                value = occurrences[0]['value']
                if (key == 'kind' and value.lower() not in {'ast', 'span'} or
                        key == 'show_span' and value.lower() not in {'true', 'false'}):
                    field_errors.append({'field': key, 'error_kind': 'invalid_beacon_field', 'candidates': occurrences})
                else:
                    note_fields[key] = value.lower() if key in {'kind', 'show_span'} else value
    resolution = intent.get("comment_resolution")
    if "resolved_comment" in intent:
        comment = intent["resolved_comment"]
    else:
        note_comments = [v for r in headers for v in r["occurrences"] if v["key"] == "comment"]
        if len(note_comments) > 1:
            field_errors.append({"field": "comment", "error_kind": "ambiguous_beacon_field",
                                 "candidates": note_comments})
        resolved = resolve_beacon_comment(
            intent=intent.get("comment_intent") or BeaconCommentIntent.preserve(),
            note_comment=note_comments[0]["value"] if len(note_comments) == 1 else None,
            note_comment_present=len(note_comments) == 1,
            disk_comment=beacon.get("comment"), beacon_exists_on_disk=bool(block),
            note_authoritative=absolute)
        comment, resolution = resolved.resolved, resolved.to_dict()
    if resolution:
        if resolution.get("field_error"):
            field_errors.append(resolution["field_error"])
        result.update(comment_outcome=resolution["outcome"], comment_conflict=resolution.get("conflict"),
                      comment_warning=resolution.get("warning"))
    result.update(removed_labels=[t for t in disk_labels if t not in labels] if absolute else [],
                  adoption_additive=bool(absolute and not rehydrate and not note_records and block), field_errors=field_errors)
    updates = dict(note_fields) if rehydrate else {}
    if not label_ambiguous:
        updates["slice_labels"] = ",".join(labels)
    explicit_comment = getattr(intent.get("comment_intent"), "mode", "preserve") != "preserve"
    if comment != beacon.get("comment") or explicit_comment:
        updates["comment"] = comment
    field_edits = (_build_beacon_block_for_language(language, snapshot=snapshot, record=record,
                                                  field_updates=updates, field_errors=field_errors) if record else [])
    if any(e["field"] == "comment" for e in field_errors):
        comment = beacon.get("comment")
    if any(e["field"] == "slice_labels" for e in field_errors):
        labels = disk_labels
        result["removed_labels"] = []
    result["diagnostics"].extend(field_errors)
    operation = intent.get("planned_operation")
    if operation is None:
        if selection["status"] == "create":
            if absolute and not requested:
                result.update(planned_operation="noop", intended_slice_labels=(), resolved_comment=comment,
                              noop_reason="no_beacon_at_target", target_key=None)
                return result
            if not create_capable:
                return fail("operation_not_supported_for_target", f"Beacon creation is not supported for {language} targets.")
            if selection.get("anchor") is None:
                return fail("no_beacon_anchor_for_node", "No current source anchor exists for beacon creation.")
            operation = "create"
        elif selection["status"] == "satisfied_noop":
            operation = "noop"
            result["noop_reason"] = "no_beacon_at_target"
        elif (intent["kind"] == "delta_remove" and not labels and not label_ambiguous) or (absolute and not requested):
            operation = "delete"
        elif not field_edits:
            operation = "noop"
            result["noop_reason"] = ("field_error" if field_errors else
                                     "already_satisfied" if intent["kind"] == "delta_ensure" else "labels_already_absent")
        else:
            operation = "update"
    result.update(planned_operation=operation, intended_slice_labels=tuple(labels), resolved_comment=comment,
                  target_key=_beacon_selection_target_key(snapshot, selection, beacon.get("id")))
    if operation == "noop":
        return result
    if operation == "delete" and record and record.get("line_extent") is None:
        return fail("beacon_extent_unreadable", "Selected physical block has incomplete owned extent; candidates retain its repair and byte-range evidence.")
    lines = list(snapshot.physical_lines)
    edits, witnesses, states, slots = [], [], [], []
    bid = beacon.get("id")
    section = "script"
    if operation == "delete":
        selected_records = [record]
        if record.get("closed"):
            selected_records += [r for r in _beacon_edit_records(snapshot, language)
                                 if r["marker"] == "close" and r["line"] == record.get("close_line")
                                 and r.get("section") == record.get("section") and r.get("line_extent") is not None]
        elif record.get("pair_candidates"):
            result["diagnostics"].append({"type": "uncertain_close", "candidates": record["pair_candidates"]})
        for member in selected_records:
            a, b = member["line_extent"]
            owner = member["opener_range"][0]
            edits.append(source_line_edit(snapshot, a, b + 1, [], owner))
            witnesses.append(owner)
            states.append({"owner": owner, "operation": "absent", "marker": member["marker"]})
    elif operation == "update":
        owner = record["opener_range"][0]
        edits.extend(field_edits)
        witnesses.append(owner)
        blocked = {e["field"] for e in field_errors}
        expected_fields = {k: v if v is not None else "" for k, v in updates.items() if k not in blocked}
        if "comment" in expected_fields and "comments" in record["fields"] and "comment" not in record["fields"]:
            expected_fields["comments"] = expected_fields.pop("comment")
        states.append({"owner": owner, "operation": "present", "complete": record["complete"],
                       "fields": expected_fields})
        slots.extend(e.start_byte for e in field_edits if e.start_byte == e.end_byte)
    else:
        if any(e['field'] == 'id' for e in field_errors):
            return fail('ambiguous_beacon_field', 'The create header has no unambiguous supplied id; inspect its field candidates.')
        bid = intent.get("create_id") or note_fields.get('id')
        if not bid:
            return result
        anchor = selection["anchor"]
        a = anchor["line"] - 1
        rows = _build_beacon_block_for_language(language, bid=bid, role=None,
                    slice_labels=",".join(labels), kind=note_fields.get('kind', 'ast'),
                    show_span=(note_fields['show_span'] == 'true') if 'show_span' in note_fields else None,
                    comment_text=comment, section=section)
        rows = _indent_beacon_block(rows, lines, a)
        edits.append(source_line_edit(snapshot, a, a, rows, "insertion"))
        slots.append(snapshot.byte_offsets[a])
        states.append({"owner": "insertion", "operation": "present", "complete": True,
                       "fields": {**note_fields, "id": bid, "slice_labels": ",".join(labels), "comment": comment}})
    if operation == 'update' and 'id' in updates and not any(e['field'] == 'id' for e in field_errors):
        bid = updates['id']
    result.update(beacon_id=bid, edit_plan=BeaconEditPlan(snapshot, language, tuple(edits), tuple(witnesses),
                                                      tuple(states), insertion_slots=tuple(slots)))
    return result


def _build_beacon_block_for_language(
    language: str,
    *,
    bid: str = "",
    role: Optional[str] = None,
    slice_labels: Optional[str] = None,
    kind: Optional[str] = None,
    show_span: Optional[bool] = None,
    comment_text: Optional[str] = None,
    start_snippet: Optional[str] = None,
    end_snippet: Optional[str] = None,
    section: Optional[str] = None,
    snapshot=None,
    record: Optional[dict] = None,
    field_updates: Optional[dict] = None,
    field_errors: Optional[list] = None,
) -> list:
    def value_text(key, value):
        text = str(value) if value is not None else ""
        if key in {"comment", "comments"} and text and (
                text.endswith((",", "]")) or (len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0])):
            return '"' + text + '"'
        return text

    if record is not None:
        errors = field_errors if field_errors is not None else []
        edits, missing = [], []
        raw = snapshot.raw_bytes
        owner = record["opener_range"][0]
        for key, value in (field_updates or {}).items():
            keys = {"comment", "comments"} if key == "comment" else {key}
            found = [o for o in record["occurrences"] if o["key"] in keys]
            ambiguous = [d for d in record["diagnostics"] if d["code"] == "inline_field_ambiguity"
                         and re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=", d["raw"]).group(1) in keys]
            if len(found) > 1 or ambiguous:
                errors.append({"field": key, "error_kind": "ambiguous_beacon_field",
                               "candidates": found, "diagnostics": ambiguous})
                continue
            if not found:
                if value not in (None, ""):
                    missing.append((key, value_text(key, value)))
                continue
            occurrence = found[0]
            if occurrence["value"] == (value if value is not None else ""):
                continue
            locations = occurrence["locations"]
            first, last = locations[0], locations[-1]
            a, first_end = first["byte_range"]
            text = raw[a:first_end].decode("utf-8")
            head = re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*=\s*", text).group()
            start = a + len(head.encode("utf-8"))
            end = last["byte_range"][1]
            suffix = re.search(r",*\s*$", text[len(head):]).group()
            pieces = [value_text(key, value).encode("utf-8") + suffix.encode("utf-8")]
            cursor = first_end
            for location in locations[1:]:
                left, right = location["byte_range"]
                pieces.append(raw[cursor:left])
                cursor = right
            pieces.append(raw[cursor:end])
            edits.append(BeaconEdit(start, end, b"".join(pieces), owner))
        if missing:
            bracket = record.get("bracket_range")
            if bracket is None:
                errors.extend({"field": k, "error_kind": "beacon_extent_unreadable",
                               "candidates": record["repair_candidates"]} for k, _ in missing)
            else:
                line = next(i for i in range(len(snapshot.physical_lines))
                            if snapshot.byte_offsets[i] <= bracket[0] < snapshot.byte_offsets[i + 1])
                line_start = snapshot.byte_offsets[line]
                before = raw[line_start:bracket[0]].decode("utf-8")
                locations = [o["locations"][0] for o in record["occurrences"]
                             if o["line"] != record["line"]]
                if locations:
                    loc = locations[0]
                    prefix = raw[snapshot.byte_offsets[loc["line"] - 1]:loc["byte_range"][0]].decode("utf-8")
                else:
                    opener_line = snapshot.physical_lines[record["line"] - 1]
                    lead = opener_line[:len(opener_line) - len(opener_line.lstrip())]
                    form = record["comment_form"]
                    prefix = lead + ({"python": "#   ", "shell": "#   ", "yaml": "#   ",
                                      "sql": "--   "}.get(language, "//   ") if form == "line"
                                     else " *   " if form == "block" else "  ")
                ending = snapshot.line_endings[line] or next((v for v in snapshot.line_endings if v), "\n")
                rows = ending.join(prefix + k + "=" + v + "," for k, v in missing) + ending
                standalone = not before.strip().strip("#*/- ")
                position = line_start if standalone else bracket[0]
                replacement = rows if standalone else ending + rows + before[:len(before) - len(before.lstrip())] + (
                    {"python": "# ", "shell": "# ", "yaml": "# ", "sql": "-- "}.get(language, "// ")
                    if record["comment_form"] == "line" else "")
                edits.append(BeaconEdit(position, position, replacement.encode("utf-8"), owner))
        return edits
    style = language
    if language == "vue":
        style = {"template": "html", "style": "css", "script": "js_line"}.get(
            section or "script", "js_line"
        )
    elif language == "markdown":
        style = "html"
    elif language == "js_ts":
        style = "js_line"

    fields: list[tuple[str, str]] = []
    if role:
        fields.append(("role", str(role)))
    if slice_labels:
        fields.append(("slice_labels", str(slice_labels)))
    if kind:
        fields.append(("kind", str(kind)))
    if start_snippet:
        fields.append(("start_snippet", f'"{start_snippet}"'))
    if end_snippet:
        fields.append(("end_snippet", f'"{end_snippet}"'))
    if show_span is not None:
        fields.append(("show_span", "true" if show_span else "false"))
    if comment_text:
        fields.append(("comment", value_text("comment", comment_text)))

    if style == "html":
        lines = ["<!--", "@beacon[", f"  id={bid},"]
        lines += [f"  {k}={v}," for k, v in fields]
        lines += ["]", "-->"]
        return lines
    if style == "css":
        lines = ["/* @beacon[", f"   id={bid},"]
        lines += [f"   {k}={v}," for k, v in fields]
        lines += ["] */"]
        return lines
    prefix = {"js_line": "//", "sql": "--"}.get(style) or (
        "--" if language == "sql" else "#"
    )
    lines = [f"{prefix} @beacon[", f"{prefix}   id={bid},"]
    lines += [f"{prefix}   {k}={v}," for k, v in fields]
    lines += [f"{prefix} ]"]
    return lines


def _find_beacon_close_block_extent(
    language: str,
    lines: list[str],
    bid: str,
    search_from_idx: int = 0,
) -> Optional[tuple[int, int]]:
    wanted = normalize_beacon_id(bid)
    if language == "vue":
        records = _vue_records_for_lines(lines)
    else:
        records = _scan_source_beacons(_source_line_view(lines), language)
    openings = [r for r in records if r["marker"] == "open" and r["fields"].get("id") == wanted]
    if openings:
        preceding = [r for r in openings if r["read_end_line"] <= search_from_idx]
        candidates = ([max(preceding, key=lambda r: r["line"])] if preceding else openings if search_from_idx == 0 else [])
        if len(candidates) != 1:
            return None
        if not candidates[0]["closed"]:
            return None
        target_line = candidates[0]["close_line"]
        return next((r["line_extent"] for r in records if r["marker"] == "close" and r["line"] == target_line), None)
    return next((r["line_extent"] for r in records if r["marker"] == "close" and r["complete"] and r["line_extent"] is not None
                 and r["fields"].get("id") == wanted and r["line"] - 1 >= max(0, search_from_idx)), None)


BYTES_PER_TOKEN_ESTIMATE = 3


def format_snippet_header(
    header_prefix: str,
    lines: Sequence[str],
    span_start: int,
    span_end: int,
    *,
    relative_start: int | None = None,
) -> str:
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


def clamp_span_to_head_tail(
    span_start: int, span_end: int, max_lines: int
) -> tuple[int, int, int, int, int]:
    tail = max_lines // 4
    head = max_lines - tail
    return (
        span_start + head - 1,
        span_end - tail + 1,
        span_start + head,
        span_end - tail,
        (span_end - span_start + 1) - max_lines,
    )


_TIER_FENCE_BACKTICK_RE = re.compile(r"^ {0,3}`{3,}")


_TIER_FENCE_TILDE_RE = re.compile(r"^ {0,3}~{3,}")


def _tier_fence_for(body_lines: Sequence[str]) -> str:
    if any(_TIER_FENCE_BACKTICK_RE.match(line) for line in body_lines):
        if not any(_TIER_FENCE_TILDE_RE.match(line) for line in body_lines):
            return "~~~~"
        return "`" * 12
    return "```"


POSTFIX_SEP = "--"


def slice_tag_variant_match(
    tag_stripped: str,
    prefix: str,
    *,
    postfix_words: Iterable[str],
) -> dict[str, Any] | None:
    if not tag_stripped or not prefix:
        return None
    if not tag_stripped.startswith(prefix):
        return None
    remainder = tag_stripped[len(prefix):]

    matched_postfix = ""
    middle = remainder
    for w in postfix_words:
        if not w:
            continue
        suf = POSTFIX_SEP + w
        if remainder.endswith(suf) and len(remainder) >= len(suf):
            matched_postfix = w
            middle = remainder[: -len(suf)]
            break

    if middle and not middle.startswith("-"):
        return None

    return {"matched_prefix": prefix, "middle": middle, "postfix": matched_postfix}


def parse_mapped_note(note: str) -> list[tuple[str, Any]]:
    if not isinstance(note, str) or not note:
        return []
    lines = note.splitlines()
    items: list[tuple[str, Any]] = []
    body_started = False
    i = 0
    n = len(lines)
    _block_keys = ("id", "role", "slice_labels", "kind", "show_span",
                   "comment", "comments")

    def _is_block_key(text: str) -> bool:
        head = re.split(r"[:=]", text, 1)[0].strip()
        return head in _block_keys

    while i < n:
        s = lines[i].strip()

        if s == "MD_PATH:" or s.startswith("MD_PATH:"):
            open_line = i
            i += 1
            found_sep_md = False
            while i < n:
                if lines[i].strip() == "---":
                    i += 1
                    found_sep_md = True
                    break
                i += 1
            if body_started:
                items.append(("md_path_body", lines[open_line:i]))
            else:
                block_end = (i - 1) if found_sep_md else i
                items.append(("md_path", lines[open_line + 1:block_end]))
                if found_sep_md:
                    _j = i
                    while _j < n and not lines[_j].strip():
                        _j += 1
                    if not (
                        _j < n
                        and (
                            lines[_j].strip().startswith("BEACON (")
                            or lines[_j].strip().startswith("@beacon[")
                        )
                    ):
                        body_started = True
            continue

        if s.startswith("AST_QUALNAME:"):
            if body_started:
                raw = [lines[i]]
                i += 1
                if i < n and lines[i].strip() == "---":
                    raw.append(lines[i])
                    i += 1
                items.append(("ast_qualname_body", raw))
                continue
            i += 1
            if i < n and lines[i].strip() == "---":
                _j = i + 1
                _beacon_flip = True
                while _j < n and not lines[_j].strip():
                    _j += 1
                if _j < n and (
                    lines[_j].strip().startswith("BEACON (")
                    or lines[_j].strip().startswith("@beacon[")
                ):
                    _k = _j + 1
                    _closer = -1
                    while _k < n:
                        _t = lines[_k].strip()
                        if _t == "---":
                            _closer = _k
                            break
                        if _t and not _is_block_key(_t):
                            _closer = _k
                            break
                        _k += 1
                    if _closer < 0:
                        _beacon_flip = False
                    else:
                        _closer_text = lines[_closer].strip()
                        if _closer_text == "---":
                            _beacon_flip = False
                        elif _closer_text in (")", "]"):
                            _after = _closer + 1
                            if _after < n and lines[_after].strip() == "---":
                                _beacon_flip = False
                            elif _after >= n:
                                _beacon_flip = False
                        else:
                            _beacon_flip = False
                i += 1
                if _beacon_flip:
                    body_started = True
            continue

        if s.startswith("BEACON (") or s.startswith("@beacon["):
            open_line = i
            i += 1
            found_sep = False
            record: dict[str, str] = {}
            while i < n:
                t = lines[i].strip()
                if t == "---":
                    i += 1
                    found_sep = True
                    break
                if t.startswith("comment:"):
                    record["comment"] = t.split(":", 1)[1].strip()
                elif t.startswith("comments:"):
                    record["comment"] = t.split(":", 1)[1].strip()
                if t and not any(
                    t.startswith(prefix)
                    for prefix in (
                        "id:",
                        "role:",
                        "slice_labels:",
                        "kind:",
                        "show_span:",
                        "comment:",
                        "comments:",
                    )
                ):
                    break
                if t.startswith("id:"):
                    record["id"] = t.split(":", 1)[1].strip()
                elif t.startswith("role:"):
                    record["role"] = t.split(":", 1)[1].strip()
                elif t.startswith("slice_labels:"):
                    record["slice_labels"] = t.split(":", 1)[1].strip()
                elif t.startswith("kind:"):
                    record["kind"] = t.split(":", 1)[1].strip()
                elif t.startswith("show_span:"):
                    record["show_span"] = t.split(":", 1)[1].strip()
                i += 1
            if found_sep:
                if i < n and not lines[i].strip():
                    i += 1
            if body_started:
                record["_raw_lines"] = lines[open_line:i]
                items.append(("beacon_body", record))
            else:
                items.append(("beacon", record))
                if found_sep:
                    body_started = True
            continue


        items.append(("line", lines[i]))
        if lines[i].strip():
            body_started = True
        i += 1
    return items


def _repository_view_span(text: str) -> str:
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            if run > longest:
                longest = run
        else:
            run = 0
    fence = "`" * (longest + 1)
    pad = " " if (text.startswith("`") or text.endswith("`")) else ""
    return f"{fence}{pad}{text}{pad}{fence}"


REPOSITORY_VIEW_ENTRY_MARKERS = ("AST_QUALNAME:", "BEACON (", "MD_PATH:")


def repository_view_note_is_entry(note: str) -> bool:
    if not note:
        return False
    return note.split("\n", 1)[0].strip().startswith(REPOSITORY_VIEW_ENTRY_MARKERS)


def repository_view_section_title(
    items: list[tuple[str, Any]], strip_text: Callable[[str], str]
) -> str:
    for _kind, payload in items:
        if _kind != "md_path":
            continue
        hash_lines = [
            str(line).strip()
            for line in payload
            if str(line).strip().startswith("#")
        ]
        if hash_lines:
            title = hash_lines[-1].lstrip("#")
            if title.startswith(" "):
                title = title[1:]
            return strip_text(title).strip()
    return ""


def repository_view_body_parts(
    items: list[tuple[str, Any]], strip_text: Callable[[str], str]
) -> list[list[str]]:
    parts: list[list[str]] = []
    current: list[str] = []

    def _feed(raw_line: str) -> None:
        nonlocal current
        stripped = raw_line.strip()
        if stripped == "SPAN TEXT:" or re.fullmatch(
            r"CONTEXT COMMENTS \(.*\):", stripped
        ):
            if current:
                parts.append(current)
                current = []
            return
        current.append(raw_line)

    for kind, payload in items:
        if kind == "line":
            _feed(str(payload))
        elif kind == "ast_qualname_body":
            for _line in payload:
                _feed(str(_line))
        elif kind == "md_path_body":
            for _line in payload:
                _feed(str(_line))
        elif kind == "beacon_body":
            for _line in (payload.get("_raw_lines") or []):
                _feed(str(_line))
    if current:
        parts.append(current)
    trimmed: list[list[str]] = []
    for part in parts:
        while part and not part[0].strip():
            part.pop(0)
        while part and not part[-1].strip():
            part.pop()
        if part:
            trimmed.append([strip_text(line) for line in part])
    return trimmed


def repository_view_labels_and_comment(
    items: list[tuple[str, Any]], strip_text: Callable[[str], str]
) -> tuple[list[str], str]:
    beacon_records = [payload for kind, payload in items if kind == "beacon"]
    first_record = beacon_records[0] if beacon_records else {}
    raw_labels = first_record.get("slice_labels") or ""
    labels = (
        [t[1:] for t in _slice_label_tags(raw_labels).split()]
        if raw_labels
        else []
    )
    comment_text = strip_text(first_record.get("comment") or "").strip()
    return labels, comment_text


def repository_view_entry_display(
    name: str,
    note: str,
    parent_entry_kind: str | None,
    *,
    headings_only: bool,
    strip_text: Callable[[str], str],
) -> tuple[str, str]:

    items = parse_mapped_note(note)
    base, _tags = split_name_and_tags(str(name or ""))
    base = strip_text(base).strip()
    tokens = base.split()
    first = tokens[0] if tokens else ""
    rest = tokens[1:]

    labels, comment_text = repository_view_labels_and_comment(items, strip_text)

    marker = note.split("\n", 1)[0].strip()
    if marker.startswith("MD_PATH:"):
        title = repository_view_section_title(items, strip_text) or base or "section"
        heading = f"section {title}"
    elif marker.startswith("BEACON ("):
        title = " ".join(rest).strip() or "span-beacon"
        heading = f"region {_repository_view_span(title)}"
    elif first == EMOJI_CLASS:
        if rest and rest[0] in ("class", "interface", "type", "enum"):
            kind = rest[0]
            title = " ".join(rest[1:]).strip()
        else:
            kind = "class"
            title = " ".join(rest).strip()
        heading = f"{kind} {_repository_view_span(title)}"
    elif first == EMOJI_FUNC:
        if rest and rest[0] in ("function", "method", "const", "let", "var"):
            kind = rest[0]
            title = " ".join(rest[1:]).strip()
        elif parent_entry_kind in ("class", "interface"):
            kind = "method"
            title = " ".join(rest).strip()
        else:
            kind = "function"
            title = " ".join(rest).strip()
        heading = f"{kind} {_repository_view_span(title)}"
    elif first == EMOJI_ASYNC:
        if parent_entry_kind in ("class", "interface"):
            kind = "async method"
        else:
            kind = "async function"
        title = " ".join(rest).strip()
        heading = f"{kind} {_repository_view_span(title)}"
    elif first == EMOJI_CONST:
        heading = f"constant {_repository_view_span(' '.join(rest).strip())}"
    else:
        heading = f"symbol {_repository_view_span(base)}"

    pieces: list[str] = []
    if labels:
        pieces.append("Labels: " + ", ".join(labels))
    if comment_text:
        pieces.append(comment_text)
    if not headings_only:
        for part_lines in repository_view_body_parts(items, strip_text):
            fence = _tier_fence_for(part_lines)
            pieces.append(
                fence + "text\n" + "\n".join(part_lines) + "\n" + fence
            )
    body = "\n\n".join(pieces)
    return heading, body


def repository_view_heading_kind(heading: str) -> str | None:
    word = heading.split(" ", 1)[0] if heading else ""
    return word or None
