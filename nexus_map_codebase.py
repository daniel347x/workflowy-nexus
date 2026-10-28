import ast
import copy
import hashlib
import json
import logging
import os
import shutil
import sys
import argparse
import tempfile
import random
import string
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

import re
import unicodedata

# SINGLE HOME (SPEC v5 §2): NODE_DECORATION_SYMBOLS +
# strip_leading_decoration_symbols MOVED to beacon_mutation_contract.py (the
# stdlib-only contracts module) so the Lightning Rod mutation ingress can
# normalize comments against the SAME machinery F12 re-reads apply, without
# importing nexus_map_codebase (the documented module-load cycle). This
# module imports + re-exports both names so every existing call site is
# untouched; the symbol set is NEVER duplicated in two modules (drift).
from beacon_mutation_contract import (  # noqa: F401
    NODE_DECORATION_SYMBOLS,
    strip_leading_decoration_symbols,
    parse_beacon_field_lines,
    beacon_field_unknown_keys,
    normalize_beacon_id,
    validate_beacon_comment_text,
    parse_beacon_note_metadata,
    resolve_beacon_comment,
    BeaconCommentIntent,
)
from beacon_source import (SourceSnapshot, physical_source_lines, read_source_snapshot, source_snapshot_from_bytes,
                           _source_line_view, scan_beacon_structure, source_lexical_evidence,
                            js_lexical_evidence, beacon_context_lines, _beacon_comment_rows)
from beacon_edits import BeaconEdit, BeaconEditPlan, source_line_edit, verify_beacon_edit_plan, commit_beacon_edit_plan
from markdown_it import MarkdownIt
import mdformat

try:
    from tree_sitter import Parser
    from tree_sitter_language_pack import get_language, get_parser
    _HAVE_TREE_SITTER = True
except Exception:
    Parser = None  # type: ignore[assignment]
    get_language = None  # type: ignore[assignment]
    get_parser = None  # type: ignore[assignment]
    _HAVE_TREE_SITTER = False

# Force UTF-8 output for Windows console
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')


# CARTOGRAPHER CORE PLAN:
# This file is the heart of Cartographer: portable file-note generation,
# multi-language outline parsing, beacon parsing/application, and on-disk beacon
# update helpers. Preserve it in Core v1 alongside the snippet engine.
# @beacon[
#   id=nmcv1@file-core,
#   role=core keeper: cartographer mapping, beacon parsing, and beacon-update engine,
#   slice_labels=nexus-portability,nexus-core-v1,
#   kind=span,
#   show_span=false,
# ]
# CORE V1 KEEPER:
# Preserve the portable note/header model, MD_PATH identity machinery, semantic
# whitening, and Cartographer-aware reconcile semantics. These are central to
# transferable installs and stable F12 refresh behavior.
# @beacon[
#   id=nmcv1@portable-note-and-identity-core,
#   role=core keeper: portable note format plus markdown and identity reconciliation semantics,
#   slice_labels=nexus-portability,nexus-core-v1,
#   kind=span,
#   show_span=false,
# ]
# @beacon[
#   id=auto-beacon@_warn_name-ts8n,
#   role=ƒ _warn_name(base: str),
#   slice_labels=ra-reconcile,carto-js-ts,carto-fwd-source,
#   kind=ast,
# ]
def _warn_name(base: str) -> str:
    """Append generation timestamp so stale warning nodes are visibly old."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"{base} [{ts}]"

# --- Configuration (Synced) ---

EMOJI_FOLDER = "📂"

EMOJI_FILE = "📄"

EMOJI_PYTHON = "🐍"

EMOJI_MARKDOWN = "📘"

EMOJI_SQL = "🗄️"

EMOJI_SHELL = "🐚"

EMOJI_CLASS = "📦"

EMOJI_FUNC = "ƒ"

EMOJI_ASYNC = "⚡"

EMOJI_CONST = "💎"

EMOJI_JS = "🟨"

EMOJI_TS = "🟦"

EMOJI_VUE = "🟩"

# @beacon[
#   id=auto-beacon@TRIDENT-tridnt,
#   slice_labels=nexus--tags,carto-fwd-source,
#   kind=span,
#   comment=Canonical trident decoration symbol + the single blacklist of leading node-type/decoration symbols that must never be carried inside a beacon role/comment into a generated Workflowy node NAME. Cartographer adds the trident (and AST-type emoji) deterministically; agents sometimes paste them into role=/comment=, which then compounds on every F12 refresh. strip_leading_decoration_symbols() removes any leading run of these symbols so only the deterministic decoration survives.,
# ]
# The trident is the "this node carries a beacon" marker Cartographer adds
# deterministically to every beacon-tagged node NAME. It is NOT an AST-type
# emoji, so it has no EMOJI_* constant of its own; define it here so the
# blacklist below has a single source of truth.
TRIDENT = "🔱"

# NODE_DECORATION_SYMBOLS + strip_leading_decoration_symbols now live in
# beacon_mutation_contract.py (SINGLE HOME, SPEC v5 §2) and are re-exported
# above; the EMOJI_* / TRIDENT constants remain HERE for name-building and
# their values must stay in lockstep with that set.

# Debug flags (controlled via environment variables)
DEBUG_MD_BEACONS = bool(os.environ.get("CARTOGRAPHER_MD_BEACONS"))


# @beacon[
#   id=auto-beacon@_get_line_count-8i2t,
#   role=ƒ _get_line_count(path: str),
#   slice_labels=test-beacon,carto-fwd-source,
#   kind=ast,
# ]
def _get_line_count(path: str) -> Optional[int]:
    """Return number of lines in a text file, or None on error.

    Used to annotate FILE nodes with LINE COUNT in their Workflowy notes.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except Exception:
        return None


# @beacon[
#   id=auto-beacon@_portable_relpath-cg4h,
#   role=_portable_relpath,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _portable_relpath(abs_path: str, repo_root: str) -> str | None:
    """Return a portable (forward-slash) relpath, or None if not safe/possible."""
    try:
        # Normalize separators but keep semantics.
        abs_norm = os.path.normpath(abs_path)
        root_norm = os.path.normpath(repo_root)

        # os.path.commonpath throws on Windows when drives differ.
        common = os.path.commonpath([abs_norm, root_norm])
        if os.path.normcase(common) != os.path.normcase(root_norm):
            return None

        rel = os.path.relpath(abs_norm, root_norm)
        rel = rel.replace("\\", "/")
        if not rel or rel.startswith(".."):
            return None
        return rel
    except Exception:
        return None


# @beacon[
#   id=carto-path@format_file_note,
#   role=format_file_note,
#   slice_labels=nexus-md-header-path,nexus-path-resolution-logic,
#   kind=ast,
#   comment=Emit Cartographer FILE-node Path headers under the current portability and segments model,
# ]
def format_file_note(
    path: str,
    line_count: int | None = None,
    sha1: str | None = None,
    *,
    repo_root: str | None = None,
) -> str:
    """Format the standard FILE-node note header.

    Shared between Cartographer (map_codebase) and per-file beacon refresh
    (refresh_file_node_beacons) so that FILE nodes always expose a consistent
    header block.

    Portability policy (PR2 - segments mode):
    - When repo_root is provided and `path` is under repo_root, store ONLY the
      final path segment in the note (folder name or filename). This makes
      Cartographer Workflowy trees portable and robust under folder moves.
    - Otherwise (no Root context), store the raw absolute path.

        Path: ...
        LINE COUNT: N
        Source-SHA1: ...

    LINE COUNT and Source-SHA1 are optional and omitted when not provided.
    """
    used_path = path

    # Segments mode: when we are in a Root-mapped tree (repo_root provided)
    # and this path is under repo_root, store ONLY the final segment.
    if repo_root and isinstance(path, str) and isinstance(repo_root, str):
        try:
            if os.path.isabs(path) and os.path.isabs(repo_root):
                root_norm = os.path.normcase(os.path.normpath(repo_root))
                path_norm = os.path.normcase(os.path.normpath(path))
                if os.path.commonpath([path_norm, root_norm]) == root_norm:
                    # Strip trailing separators so basename works for directories.
                    seg = os.path.basename(path.rstrip("\\/"))
                    if seg:
                        used_path = seg
        except Exception:
            # Fail closed: if any path math goes sideways, keep raw path.
            used_path = path

    lines: list[str] = [f"Path: {used_path}"]
    if line_count is not None:
        lines.append(f"LINE COUNT: {line_count}")
    if sha1 is not None:
        lines.append(f"Source-SHA1: {sha1}")
    return "\n".join(lines)


# --- Parsers ---

# Sentinel node name for pre-first-heading "leading text" in a Markdown file.
# Mirrors the "⚙️ YAML Frontmatter" pattern: content that lives ABOVE the
# first heading has no heading to anchor to, so it would be dropped by the
# heading-only tree builder. We represent it as a dedicated, recognizable node
# (NOT a synthetic heading) so it round-trips verbatim above the first heading
# via markdown_roundtrip.nexus_to_tokens — exactly like frontmatter. Keep this
# string in sync with markdown_roundtrip.MD_LEADING_TEXT_NODE_NAME.
MD_LEADING_TEXT_NODE_NAME = "📄 Leading text"


# @beacon[
#   id=auto-beacon@extract_markdown_leading_text-ld1x,
#   role=_extract_markdown_leading_text(tokens, body_source_lines),
#   slice_labels=nexus-md-header-path,carto-fwd-markdown,nexus--md-preview,
#   kind=ast,
#   comment=F12+1 leading-text fix (forward/disk->Workflowy twin of the frontmatter detach): returns verbatim body text appearing BEFORE the first real (non-blockquote, non-list-item) heading token, or None. Boundary uses the SAME blockquote/list-item guards as tokens_to_nexus_tree so the two never disagree about what the first heading is.,
# ]
def _extract_markdown_leading_text(
    tokens,
    body_source_lines: Optional[List[str]] = None,
) -> Optional[str]:
    """Return the verbatim leading text before the first document heading.

    "Leading text" is any non-blank content that appears in the Markdown body
    BEFORE the first real heading. A heading nested inside a blockquote or a
    list item is body content, NOT document structure, so it does not count as
    "the first heading" — mirroring the exact guards in tokens_to_nexus_tree.

    The returned string is the raw body lines ``[0 : first_heading_line)`` with
    leading/trailing blank lines trimmed, or ``None`` when there is no leading
    text (the body opens directly with a heading, or is empty/blank).

    This is the forward (disk -> Workflowy) twin of
    markdown_roundtrip.detach_yaml_frontmatter_child's role for frontmatter:
    it lets parse_markdown_structure synthesize a dedicated
    ``MD_LEADING_TEXT_NODE_NAME`` node so pre-heading prose is preserved
    instead of silently dropped, and round-trips cleanly back out.
    """
    if not body_source_lines:
        return None

    # Find the 0-based source line of the first REAL heading (respecting the
    # same blockquote / list-item containment guards as tokens_to_nexus_tree).
    blockquote_depth = 0
    list_item_depth = 0
    first_heading_line0: Optional[int] = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        ttype = token.type
        if ttype == "blockquote_open":
            blockquote_depth += 1
            i += 1
            continue
        if ttype == "blockquote_close":
            if blockquote_depth > 0:
                blockquote_depth -= 1
            i += 1
            continue
        if ttype == "list_item_open":
            list_item_depth += 1
            i += 1
            continue
        if ttype == "list_item_close":
            if list_item_depth > 0:
                list_item_depth -= 1
            i += 1
            continue
        if ttype == "heading_open" and blockquote_depth == 0 and list_item_depth == 0:
            token_map = getattr(token, "map", None)
            if token_map:
                first_heading_line0 = int(token_map[0])
            else:
                first_heading_line0 = 0
            break
        i += 1

    if first_heading_line0 is None:
        # No real heading anywhere: the entire body is "leading text".
        lead_lines = list(body_source_lines)
    elif first_heading_line0 <= 0:
        return None
    else:
        lead_lines = list(body_source_lines[:first_heading_line0])

    # Trim leading/trailing blank lines (same policy tokens_to_nexus_tree uses
    # for heading body extraction).
    while lead_lines and not lead_lines[0].strip():
        lead_lines.pop(0)
    while lead_lines and not lead_lines[-1].strip():
        lead_lines.pop()

    if not lead_lines:
        return None
    return "\n".join(lead_lines)


# @beacon[
#   id=auto-beacon@tokens_to_nexus_tree-fcan,
#   role=tokens_to_nexus_tree(tokens, source_lines, source_line_offset: int),
#   slice_labels=nexus-md-header-path,ra-snippet-range-ast-md,carto-fwd-markdown,nexus--md-preview,
#   kind=ast,
# ]
def tokens_to_nexus_tree(
    tokens,
    source_lines: Optional[List[str]] = None,
    source_line_offset: int = 0,
) -> List[Dict[str, Any]]:
    """Convert markdown-it-py token stream to NEXUS hierarchical tree.

    Assigns priority values based on document order (100, 200, 300, ...).
    Lower priority = appears first in document.

    v2.2: For each Markdown heading node, writes an explicit MD_PATH block into
    the note:

        MD_PATH:
        # Top
        ## Section
        ### Subsection
        ---
        <existing heading content>

    v2.3: When ``source_lines`` are provided, each heading node's note body is
    taken from the exact raw Markdown lines that belong *directly* to that
    heading (after the heading line and before the next heading of any level).
    This preserves nested ordered/bullet lists, indentation, fenced code
    blocks, and numbering verbatim instead of reconstructing them token by
    token.

    This mirrors Python's AST_QUALNAME header and is consumed by
    get_snippet_for_md_path in beacon_obtain_code_snippet.py.
    """
    root_children: List[Dict[str, Any]] = []
    # Stack of (heading_level, nexus_node)
    stack: List[tuple[int, Dict[str, Any]]] = []
    priority_counter = 100
    heading_entries: List[Dict[str, Any]] = []
    excluded_body_lines: set[int] = set()

    # Bug fix (Dan, May 2026): track containment depth so we ignore
    # headings that occur INSIDE a Markdown construct that represents body
    # content rather than document structure. Specifically:
    #
    #   - Blockquotes ('>'-prefixed lines, e.g. quoted PDFs, emails, or any
    #     verbatim transcript). A heading inside a blockquote is part of the
    #     quoted material, not part of the enclosing document's outline.
    #
    #   - List items. A heading nested inside a bullet or numbered list item
    #     is body content belonging to the enclosing real heading, not its
    #     own document-level section.
    #
    # Without these guards, a quoted/listed '# Some Title' line was being
    # promoted to a top-level Workflowy heading node, gathering subsequent
    # real document content as its 'children', and corrupting the MD_PATH
    # ancestor chain for everything that followed.
    #
    # Markdown-it-py emits matched open/close token pairs around blockquoted
    # and list-item content; nested instances nest the pairs cleanly, so
    # simple integer depth counters are sufficient. Note that we track
    # ``list_item_*`` rather than ``bullet_list_*`` / ``ordered_list_*``
    # because the meaningful containment is the individual list item, not
    # the surrounding list wrapper.
    blockquote_depth: int = 0
    list_item_depth: int = 0

    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token.type == "blockquote_open":
            blockquote_depth += 1
            i += 1
            continue
        if token.type == "blockquote_close":
            if blockquote_depth > 0:
                blockquote_depth -= 1
            i += 1
            continue
        if token.type == "list_item_open":
            list_item_depth += 1
            i += 1
            continue
        if token.type == "list_item_close":
            if list_item_depth > 0:
                list_item_depth -= 1
            i += 1
            continue

        if token.type == "html_block" and (
            "@beacon[" in (token.content or "") or "@beacon-close[" in (token.content or "")
        ):
            token_map = getattr(token, "map", None)
            if token_map:
                start0, end0 = token_map
                for line_no in range(start0 + 1, end0 + 1):
                    excluded_body_lines.add(line_no)
            i += 1
            continue

        if token.type != "heading_open":
            i += 1
            continue

        # Skip headings that live inside a blockquote OR list item: they are
        # body content, not document structure. Advance the loop counter by
        # ONE so the natural token-by-token walk handles the following
        # ``inline`` and ``heading_close`` tokens through their own normal
        # type checks. (We deliberately do NOT advance by 3 to skip the
        # whole heading triple; that would assume markdown-it always emits
        # heading_open + inline + heading_close as a strict three-token
        # group, which is true today but a fragile contract to depend on.
        # Letting the loop fall through is strictly more robust.)
        if blockquote_depth > 0 or list_item_depth > 0:
            i += 1
            continue

        # Start of a heading
        heading_level = int(token.tag[1:])  # h1 -> 1, h12 -> 12, etc.

        # Get the heading text from next token (should be inline)
        heading_text = ""
        if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
            heading_text = tokens[i + 1].content

        # Pop stack until we find the correct parent level for this heading
        while stack and stack[-1][0] >= heading_level:
            stack.pop()

        # Build MD_PATH lines from current ancestor stack + this heading.
        #
        # Layer (3) sanitizer (Dan, 2026-05-01, Bug #3 hardening):
        # Collapse any internal whitespace/newlines in ancestor names and
        # in the current heading text to a single line before embedding
        # into MD_PATH. Also replace meta-blocky names (BEACON metadata
        # that may have leaked into a node's name field) with a clearly
        # flagged placeholder so the MD_PATH structure survives even if
        # the in-memory tree was corrupted upstream. The level (number of
        # '#'s) is always preserved; only the visible text is sanitized.
        def _md_path_sanitize(name: str) -> str:
            single = " ".join((name or "").split()).strip()
            if not single:
                return "..."
            head = single.lstrip()
            if head.startswith("BEACON ("):
                return "⚠️ corrupt-heading"
            meta_markers = ("id:", "role:", "slice_labels:", "kind:")
            hits = sum(1 for m in meta_markers if m in single)
            if hits >= 2:
                return "⚠️ corrupt-heading"
            return single

        md_path_lines: List[str] = []
        for lvl, ancestor_node in stack:
            ancestor_name = _md_path_sanitize(ancestor_node.get("name") or "")
            md_path_lines.append(f"{('#' * lvl)} {ancestor_name}".rstrip())
        this_heading_name = _md_path_sanitize(heading_text or "")
        md_path_lines.append(f"{('#' * heading_level)} {this_heading_name}".rstrip())

        note_lines: List[str] = ["MD_PATH:"]
        note_lines.extend(md_path_lines)
        note_lines.append("---")
        note_text = "\n".join(note_lines)

        # Determine source line span for the heading itself so we can attach the
        # raw body that follows it verbatim and stamp the parsed node with a
        # concrete full-file heading line for beacon attachment.
        token_map = getattr(token, "map", None)
        if token_map:
            heading_start0, heading_end0 = token_map
        elif i + 1 < len(tokens) and getattr(tokens[i + 1], "map", None):
            heading_start0, heading_end0 = tokens[i + 1].map
        else:
            heading_start0 = 0
            heading_end0 = 0

        # Create NEXUS node.
        # _md_heading_line is an internal Cartographer-only source coordinate
        # used by apply_markdown_beacons(...) to attach Markdown AST beacons
        # by actual parser line number rather than by raw heading-name search.
        # This removes duplicate-heading ambiguity while keeping MD_PATH as
        # the durable identity shown in Workflowy notes.
        nexus_node: Dict[str, Any] = {
            "name": this_heading_name,
            "priority": priority_counter,
            "note": note_text,
            "children": [],
            "_md_heading_line": int(heading_start0) + 1 + int(source_line_offset or 0),
        }
        priority_counter += 100

        # Add to parent (or root if stack empty)
        if stack:
            stack[-1][1]["children"].append(nexus_node)
        else:
            root_children.append(nexus_node)

        heading_entries.append(
            {
                "node": nexus_node,
                "heading_start_line": heading_start0 + 1,
                "body_start_line": heading_end0 + 1,
            }
        )

        # Push this heading onto stack
        stack.append((heading_level, nexus_node))

        # Skip the inline and heading_close tokens
        i += 3  # heading_open, inline, heading_close

    if source_lines is not None and heading_entries:
        total_lines = len(source_lines)
        for idx, entry in enumerate(heading_entries):
            body_start_line = int(entry["body_start_line"])
            next_heading_start = (
                int(heading_entries[idx + 1]["heading_start_line"])
                if idx + 1 < len(heading_entries)
                else total_lines + 1
            )
            body_end_line = next_heading_start - 1

            body_lines: List[str] = []
            if body_end_line >= body_start_line:
                for line_no in range(body_start_line, body_end_line + 1):
                    if line_no in excluded_body_lines:
                        continue
                    body_lines.append(source_lines[line_no - 1])

                while body_lines and not body_lines[0].strip():
                    body_lines.pop(0)
                while body_lines and not body_lines[-1].strip():
                    body_lines.pop()

            if body_lines:
                body_text = "\n".join(body_lines)
                # F12+1 read side of the F12+3 injection guard: if this body
                # was wrapped by F12+3 in a sentinel-marked protective fence
                # (because the body itself contains Markdown headings), strip
                # the sentinel + outer fence here so the Workflowy note shows
                # the clean content (no visible fences). Only OUR sentinel-
                # marked fence is stripped; a user's own fenced content is left
                # intact. No-op for unfenced bodies. See markdown_roundtrip
                # ._maybe_fence_sentinel_note / _unfence_sentinel_note.
                try:
                    import importlib
                    _mr = importlib.import_module("markdown_roundtrip")
                    body_text = _mr._unfence_sentinel_note(body_text)
                except Exception:
                    pass
                entry["node"]["note"] = (
                    f"{entry['node']['note']}\n\n" + body_text
                )

    return root_children


# @beacon[
#   id=auto-beacon@markdown_it_heading_arbitrary_depth-7a3t,
#   role=markdown_it_heading_arbitrary_depth(state, startLine: int, endLine: int, silent: bool),
#   slice_labels=carto-fwd-markdown,nexus--md-preview,
#   kind=ast,
# ]
def markdown_it_heading_arbitrary_depth(state, startLine: int, endLine: int, silent: bool) -> bool:
    """markdown-it block rule for ATX headings with arbitrary hash depth.

    This is a minimal fork of markdown_it.rules_block.heading.heading with the
    CommonMark 6-hash cap lifted to 64. Keeping this as a markdown-it rule lets
    the parser retain its existing block-context intelligence (fences, indented
    code, HTML blocks, blockquote recursion, source maps, etc.) instead of
    replacing the whole outline parse with a raw line scanner.
    """
    from markdown_it.common.utils import isStrSpace

    pos = state.bMarks[startLine] + state.tShift[startLine]
    maximum = state.eMarks[startLine]

    if pos >= maximum:
        return False
    if state.is_code_block(startLine):
        return False
    if state.src[pos] != "#":
        return False

    level = 0
    while pos < maximum and state.src[pos] == "#" and level < 64:
        level += 1
        pos += 1

    if level == 0 or level > 64:
        return False
    if pos < maximum and not isStrSpace(state.src[pos]):
        return False

    if silent:
        return True

    maximum = state.skipSpacesBack(maximum, pos)
    tmp = state.skipCharsStrBack(maximum, "#", pos)
    if tmp > pos and isStrSpace(state.src[tmp - 1]):
        maximum = tmp

    state.line = startLine + 1

    token = state.push("heading_open", "h" + str(level), 1)
    token.markup = "#" * level
    token.map = [startLine, state.line]

    token = state.push("inline", "", 0)
    token.content = state.src[pos:maximum].strip()
    token.map = [startLine, state.line]
    token.children = []

    token = state.push("heading_close", "h" + str(level), -1)
    token.markup = "#" * level

    return True


# @beacon[
#   id=auto-beacon@arbitrary_hash_markdown_lines_to_nexus_tree-kj1r,
#   role=arbitrary_hash_markdown_lines_to_nexus_tree(source_lines: List[str], source_line_offset: int),
#   slice_labels=carto-fwd-markdown,nexus--md-preview,
#   kind=ast,
# ]
def arbitrary_hash_markdown_lines_to_nexus_tree(
    source_lines: List[str],
    source_line_offset: int = 0,
    *,
    max_hash_depth: int = 64,
) -> List[Dict[str, Any]]:
    """Parse Markdown headings with arbitrary hash depth into a NEXUS tree.

    This is the Workflowy-first Markdown heading parser used by Cartographer.
    CommonMark / markdown-it only recognizes ATX headings with 1..6 hashes;
    Dan's 99-file curation workflow deliberately uses 7, 8, 9+ hash headings
    as real Workflowy child nodes. Therefore heading recognition here is a
    simple source-line rule: any line matching ``^#{1,max_hash_depth}\\s+`` is
    a heading node, regardless of the official Markdown 6-level cap.

    The function preserves the same shape as tokens_to_nexus_tree(...):
    each heading node gets name/note/priority/children and an internal
    ``_md_heading_line`` full-file coordinate for apply_markdown_beacons(...).
    The durable Workflowy-visible identity remains MD_PATH in the note.
    """
    root_children: List[Dict[str, Any]] = []
    stack: List[tuple[int, Dict[str, Any]]] = []
    priority_counter = 100
    heading_entries: List[Dict[str, Any]] = []
    excluded_body_lines: set[int] = set()

    # Exclude @beacon[...] / @beacon-close[...] HTML comment blocks from the
    # body text attached to heading notes. They are metadata, not prose.
    i = 0
    while i < len(source_lines):
        raw = source_lines[i]
        if "<!--" in raw:
            j = i
            block_lines: list[str] = []
            while j < len(source_lines):
                block_lines.append(source_lines[j])
                if "-->" in source_lines[j]:
                    break
                j += 1
            if any("@beacon[" in x or "@beacon-close[" in x for x in block_lines):
                for line_no in range(i + 1, min(j + 1, len(source_lines)) + 1):
                    excluded_body_lines.add(line_no)
            i = j + 1
            continue
        i += 1

    heading_re = re.compile(rf"^(#{{1,{int(max_hash_depth)}}})\s+(.*)$")

    def _md_path_sanitize(name: str) -> str:
        single = " ".join((name or "").split()).strip()
        if not single:
            return "..."
        head = single.lstrip()
        if head.startswith("BEACON ("):
            return "⚠️ corrupt-heading"
        meta_markers = ("id:", "role:", "slice_labels:", "kind:")
        hits = sum(1 for m in meta_markers if m in single)
        if hits >= 2:
            return "⚠️ corrupt-heading"
        return single

    for idx, raw in enumerate(source_lines, start=1):
        if idx in excluded_body_lines:
            continue
        stripped = raw.strip()
        m = heading_re.match(stripped)
        if not m:
            continue

        heading_level = len(m.group(1))
        heading_text = m.group(2).strip()
        # Trim optional closing ATX hashes ("### Title ###") when present.
        heading_text = re.sub(r"\s+#+\s*$", "", heading_text).strip()

        while stack and stack[-1][0] >= heading_level:
            stack.pop()

        md_path_lines: List[str] = []
        for lvl, ancestor_node in stack:
            ancestor_name = _md_path_sanitize(ancestor_node.get("name") or "")
            md_path_lines.append(f"{('#' * lvl)} {ancestor_name}".rstrip())
        this_heading_name = _md_path_sanitize(heading_text or "")
        md_path_lines.append(f"{('#' * heading_level)} {this_heading_name}".rstrip())

        note_lines: List[str] = ["MD_PATH:"]
        note_lines.extend(md_path_lines)
        note_lines.append("---")
        note_text = "\n".join(note_lines)

        nexus_node: Dict[str, Any] = {
            "name": this_heading_name,
            "priority": priority_counter,
            "note": note_text,
            "children": [],
            "_md_heading_line": int(idx) + int(source_line_offset or 0),
        }
        priority_counter += 100

        if stack:
            stack[-1][1]["children"].append(nexus_node)
        else:
            root_children.append(nexus_node)

        heading_entries.append(
            {
                "node": nexus_node,
                "heading_start_line": idx,
                "body_start_line": idx + 1,
            }
        )
        stack.append((heading_level, nexus_node))

    if heading_entries:
        total_lines = len(source_lines)
        for idx, entry in enumerate(heading_entries):
            body_start_line = int(entry["body_start_line"])
            next_heading_start = (
                int(heading_entries[idx + 1]["heading_start_line"])
                if idx + 1 < len(heading_entries)
                else total_lines + 1
            )
            body_end_line = next_heading_start - 1

            body_lines: List[str] = []
            if body_end_line >= body_start_line:
                for line_no in range(body_start_line, body_end_line + 1):
                    if line_no in excluded_body_lines:
                        continue
                    body_lines.append(source_lines[line_no - 1])

                while body_lines and not body_lines[0].strip():
                    body_lines.pop(0)
                while body_lines and not body_lines[-1].strip():
                    body_lines.pop()

            if body_lines:
                entry["node"]["note"] = (
                    f"{entry['node']['note']}\n\n" + "\n".join(body_lines)
                )

    return root_children


def _beacon_block_diagnostic(
    line: int,
    end_line: Optional[int],
    outcome: str,
    fields: dict[str, str],
    field_texts: list[str],
    structure: dict | None = None,
) -> dict[str, Any]:
    """Report semantic eligibility and the scanner's bounded structural facts."""
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


# @beacon[
#   id=beacon-source@_parse_structural_beacons,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Compatibility projection of lexical-aware structural records: preserves language list shapes/defaults and local incomplete reads; supplied snapshot/section records avoid rescanning or losing original-byte geometry.,
# ]
def _parse_structural_beacons(language: str, lines: list[str], diagnostics: list | None = None, *, snapshot=None, records=None) -> list[dict]:
    """Project lexical-aware records, optionally supplied by an SFC section."""
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
    """Feed section-local lexical evidence to the original-coordinate scanner.

    An SFC section never inherits quote state from another language's section.
    Only the lexical evidence view is sliced; structural records keep the
    original snapshot, byte ranges and physical line numbers throughout.
    """
    if any(key in kwargs for key in ("section_start", "section_end", "section_byte_start", "section_byte_end")):
        left = kwargs.get("section_byte_start", snapshot.byte_offsets[max(0, kwargs.get("section_start", 1) - 1)])
        right = kwargs.get("section_byte_end", snapshot.byte_offsets[kwargs.get("section_end", len(snapshot.physical_lines))])
        local = source_snapshot_from_bytes(snapshot.raw_bytes[left:right])
        evidence = [{**r, "start": r["start"] + left, "end": r["end"] + left} for r in source_lexical_evidence(local, language)]
    else:
        evidence = source_lexical_evidence(snapshot, language)
    return scan_beacon_structure(snapshot, language, evidence=evidence, **kwargs)


def _markdown_beacon_snippet_range(beacon: dict, lines: list[str], records: list[dict]) -> None:
    """Keep the existing explicit text-anchor feature, excluding owned metadata."""
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


# @beacon[
#   id=carto-js-ts@parse_markdown_beacon_blocks,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,nexus-md-header-path,carto-fwd-source,
#   kind=span,
# ]
# Phase 2 JS/TS: Markdown beacon parser template.
# Reference for JS/TS block-comment beacons (/* @beacon[...] */),
# which use the same metadata fields and parsing pattern.
# @beacon[
#   id=auto-beacon@parse_markdown_beacon_blocks-t69i,
#   role=parse_markdown_beacon_blocks,
#   slice_labels=carto-fwd-markdown,
#   kind=ast,
#   comment=Thin delegate to _parse_structural_beacons: reads Markdown/HTML-comment blocks through the shared bounded scanner and exact pairs; the optional keyword-only diagnostics list passes through unchanged.,
# ]
def parse_markdown_beacon_blocks(
    lines: list[str], *, diagnostics: list | None = None
) -> list[dict[str, Any]]:
    """Read markdown metadata through the shared bounded scanner and exact pairs."""
    return _parse_structural_beacons("markdown", lines, diagnostics)


# @beacon[
#   id=auto-beacon@_extract_markdown_beacon_context-4hqr,
#   role=_extract_markdown_beacon_context,
#   slice_labels=carto-fwd-markdown,
#   kind=ast,
#   comment=Optional Markdown comment slurp uses the original source structural records and metadata subranges; remains disabled by default so section HTML comments are not duplicated into notes.,
# ]
def _extract_markdown_beacon_context(
    lines: list[str],
    comment_line: int,
    enabled: bool = False,
    *, records=None,
) -> List[str]:
    """Extract nearby HTML comment lines (non-beacon) around a Markdown beacon.

    When enabled, original-source records remove only metadata before nearby
    HTML comment bodies are selected. No private marker/end rule is used.

    DISABLED BY DEFAULT (Dan, May 2026)
    -----------------------------------
    Unlike Python/JS-TS/SQL/Shell, where comment-slurping pulls genuine
    documentation (docstrings, adjacent comments) into the Workflowy node
    note as a free benefit, in Markdown an HTML comment is part of the
    section's *body content* rather than metadata about the section.
    Slurping it into the node note duplicates it in an awkward
    ``CONTEXT COMMENTS (MD):`` block sandwiched between the BEACON
    metadata and the section body.

    The comments are not lost when slurping is disabled: they remain in
    the source ``.md`` file on disk and are visible via ``read_text_file``
    or ``read_text_snippet`` on the heading's beacon UUID. Only the
    duplicate copy in the Workflowy note is suppressed.

    A caller can opt in with ``enabled=True`` without changing the grammar.
    """
    if not enabled:
        return []

    records, mask, cleaned = _beacon_context_view(lines, "markdown", records)
    return _collect_beacon_context(lines, comment_line, records, mask, cleaned)


# @beacon[
#   id=carto-js-ts@apply_markdown_beacons,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-snippet-range-ast-beacon-md,nexus-md-header-path,carto-fwd-source,
#   kind=span,
# ]
# Phase 2 JS/TS: Markdown beacon application template.
# Reference for apply_js_beacons(...) when attaching JS/TS span
# and AST beacons under headings / AST nodes with context comments.
# @beacon[
#   id=auto-beacon@apply_markdown_beacons-mdast,
#   role=ƒ apply_markdown_beacons(lines: list[str], root_children, beacons),
#   slice_labels=nexus-md-header-path,ra-snippet-range-ast-md,ra-reconcile,carto-js-ts-beacons,carto-fwd-markdown,
#   kind=ast,
#   comment=Renders Markdown beacons from the shared attachment index: explicit AST selects the strictly-next heading with gap evidence while implicit promotion requires adjacency; span display and note metadata ordering remain separate.,
# ]
def apply_markdown_beacons(
    lines: list[str],
    root_children: List[Dict[str, Any]],
    beacons: list[dict[str, Any]],
    *, records=None,
) -> None:
    """Render shared heading attachments and existing enclosing-span display.

    Explicit AST attaches strictly forward; implicit promotion uses adjacency
    after blank/comment rows. Neither a span nor a snippet independently picks
    an AST owner in this rendering pass.
    """
    if not beacons or not root_children:
        return
    _prepend_duplicate_beacon_id_warning_nodes(root_children, beacons, language="MD")
    records, context_mask, _ = _beacon_context_view(lines, "markdown", records)

    # @beacon[
    #   id=auto-beacon@apply_markdown_beacons.iter_nodes-rva1,
    #   role=apply_markdown_beacons.iter_nodes,
    #   slice_labels=test-beacon,
    #   kind=ast,
    # ]
    def iter_nodes(nodes: List[Dict[str, Any]]):
        for node in nodes:
            yield node
            for ch in node.get("children") or []:
                if isinstance(ch, dict):
                    yield from iter_nodes([ch])

    all_nodes = list(iter_nodes(root_children))

    # Precompute heading positions (line numbers) in the Markdown source so
    # we can attach beacons relative to concrete headings.
    #
    # Architecturally correct Markdown attachment rule (Dan, May 2026): do
    # NOT infer heading identity by searching raw heading names. Duplicate
    # headings are common in long 99 files. tokens_to_nexus_tree(...) stamps
    # every parsed heading node with _md_heading_line from markdown-it's token
    # map, so we can attach AST beacons by concrete source line number.
    heading_positions: list[tuple[Dict[str, Any], int]] = []
    for node in all_nodes:
        ln_raw = node.get("_md_heading_line")
        try:
            ln = int(ln_raw) if ln_raw is not None else 0
        except Exception:
            ln = 0
        if ln > 0:
            heading_positions.append((node, ln))
    heading_positions.sort(key=lambda pair: pair[1])
    attachment = beacon_attachment_index(_source_line_view(lines), root_children, {
        "language": "markdown", "records": records, "beacons": beacons,
    })
    attachment_by_line = {b["record"]["line"]: b for b in attachment["blocks"]}
    for beacon in beacons:
        attached = attachment_by_line.get(beacon.get("comment_line"))
        if attached:
            beacon["kind"] = attached["effective_kind"]

    if DEBUG_MD_BEACONS and heading_positions:
        print("[MD-HEAD-LIST] headings:")
        for h_node, h_ln in heading_positions:
            print(f"  - name={h_node.get('name')!r} lineno={h_ln}")

    # PASS 1: AST beacons – decorate heading nodes.
    for beacon in beacons:
        if beacon.get("kind") != "ast":
            continue

        b_id = (beacon.get("id") or "").strip()
        role = (beacon.get("role") or "").strip()
        raw_slice_labels = (beacon.get("slice_labels") or "").strip()
        # Defaults: derive role from id prefix (before '@')
        if not role and b_id and "@" in b_id:
            role = b_id.split("@", 1)[0]
        slice_labels = _canonicalize_slice_labels(raw_slice_labels, role)

        display_role = strip_leading_decoration_symbols(role) or b_id or "md-ast-beacon"
        display_slice = slice_labels or "-"
        # Never synthesize a '#-' tag from the '-' display placeholder: feed
        # ONLY real slice_labels into tag generation (Dan, May 2026).
        tag_suffix = _slice_label_tags(slice_labels) if slice_labels else ""

        attached = attachment_by_line.get(beacon.get("comment_line"))
        owner = attached["anchor"]["node"] if attached and attached["anchor"] else None

        if owner is None:
            # If we couldn't find a heading to decorate, fall back to SPAN
            # semantics by resetting kind; the SPAN pass will handle it.
            beacon["kind"] = "span"
            continue

        name = owner.get("name") or "Untitled"
        if "🔱" not in name:
            name = f"{name} 🔱"
        if tag_suffix:
            name = f"{name} {tag_suffix}"
        owner["name"] = name

        context_lines = _extract_markdown_beacon_context(
            lines, beacon.get("comment_line") or 0, records=records
        )
        comment = strip_leading_decoration_symbols(beacon.get("comment"))

        meta_lines = [
            "BEACON (MD AST)",
            f"id: {b_id}",
            f"role: {role}",
            f"slice_labels: {slice_labels}",
            "kind: ast",
        ]
        if comment:
            meta_lines.append(f"comment: {comment}")
        if context_lines:
            meta_lines.append("")
            meta_lines.append("CONTEXT COMMENTS (MD):")
            meta_lines.extend(context_lines)
        # Visual separator between beacon metadata and body text
        meta_lines.append("---")

        note = owner.get("note") or ""
        meta_block = "\n".join(meta_lines)
        if not note:
            owner["note"] = meta_block
        else:
            # Insert beacon metadata block just after the MD_PATH header
            # (the first line that is exactly '---' following 'MD_PATH:').
            lines_note = note.splitlines()
            insert_idx = len(lines_note)
            saw_md_path = False
            for idx, line in enumerate(lines_note):
                if line.strip() == "MD_PATH:":
                    saw_md_path = True
                elif saw_md_path and line.strip() == "---":
                    insert_idx = idx + 1
                    break
            new_lines: list[str] = []
            new_lines.extend(lines_note[:insert_idx])
            # Ensure a blank line before the beacon block when joining
            if insert_idx < len(lines_note) and lines_note[insert_idx].strip():
                new_lines.append("")
            new_lines.append(meta_block)
            # Blank line between beacon block and any existing body text
            if insert_idx < len(lines_note):
                if lines_note[insert_idx].strip():
                    new_lines.append("")
                new_lines.extend(lines_note[insert_idx:])
            owner["note"] = "\n".join(new_lines)

    # PASS 2: SPAN beacons – unchanged behavior, with optional comment support.
    for beacon in beacons:
        if beacon.get("kind") != "span":
            continue

        start_snippet = beacon.get("start_snippet") or ""
        span_start = beacon.get("span_lineno_start")
        span_end = beacon.get("span_lineno_end")
        # Fallback for snippet-less beacons: anchor to the line after the beacon block
        if span_start is None:
            comment_line = beacon.get("comment_line") or 0
            if isinstance(comment_line, int) and comment_line > 0:
                span_start = comment_line + 1

        # Derive heading text from start_snippet if it looks like a Markdown header
        header_text = None
        m = re.match(r"^#{1,6}\\s*(.*)", start_snippet.strip()) if start_snippet else None
        if m:
            header_text = m.group(1).strip()

        # Prefer the nearest preceding heading in the actual Markdown source,
        # using span_lineno_start as the anchor. This makes placement robust even
        # if start_snippet is not a header line.
        owner_header_text = None
        if isinstance(span_start, int):
            for idx in range(span_start, 0, -1):
                line = lines[idx - 1].strip()
                m2 = re.match(r"^#{1,6}\\s*(.*)", line)
                if m2:
                    owner_header_text = m2.group(1).strip()
                    break

        target_header = owner_header_text or header_text

        owner: Optional[Dict[str, Any]] = None

        # Preferred: line-number based nearest-previous-heading resolution.
        if isinstance(span_start, int) and heading_positions:
            best_node = None
            best_lineno = -1
            for node, h_lineno in heading_positions:
                if h_lineno <= span_start and h_lineno > best_lineno:
                    best_lineno = h_lineno
                    best_node = node
            if best_node is not None:
                owner = best_node

        # Fallback: header-text based resolution when line-based match fails.
        if owner is None and target_header:
            norm_target = normalize_for_match(target_header)
            for node in all_nodes:
                node_name = (node.get("name") or "")
                node_norm = normalize_for_match(node_name)
                if node_norm == norm_target or (
                    norm_target and norm_target in node_norm
                ):
                    owner = node
                    break

        b_id = (beacon.get("id") or "").strip()
        role = (beacon.get("role") or "").strip()
        raw_slice_labels = (beacon.get("slice_labels") or "").strip()
        # Defaults: derive role from id prefix (before '@')
        if not role and b_id and "@" in b_id:
            role = b_id.split("@", 1)[0]
        slice_labels = _canonicalize_slice_labels(raw_slice_labels, role)

        if DEBUG_MD_BEACONS:
            print(
                "[MD-BEACON] id=%r span_start=%r target_header=%r owner=%r" % (
                    b_id,
                    span_start,
                    target_header,
                    (owner.get("name") if owner else None),
                )
            )

        show_span, show_span_explicit = _beacon_show_span(beacon)

        display_role = strip_leading_decoration_symbols(role) or b_id or "md-span-beacon"
        display_slice = slice_labels or "-"

        # Never synthesize a '#-' tag from the '-' display placeholder: feed
        # ONLY real slice_labels into tag generation (Dan, May 2026).
        tag_suffix = _slice_label_tags(slice_labels) if slice_labels else ""
        name = f"🔱 {display_role}"
        if tag_suffix:
            name = f"{name} {tag_suffix}"

        context_lines = _extract_markdown_beacon_context(
            lines, beacon.get("comment_line") or 0, records=records
        )
        comment = strip_leading_decoration_symbols(beacon.get("comment"))

        note_lines = [
            "BEACON (MD SPAN)",
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
            note_lines.append("CONTEXT COMMENTS (MD):")
            note_lines.extend(context_lines)

        # Raw Markdown span text between @beacon[...] and @beacon-close[...]
        span_text_lines: list[str] = []
        if isinstance(span_start, int) and isinstance(span_end, int):
            if 1 <= span_start <= span_end <= len(lines):
                span_text_lines = lines[span_start - 1 : span_end]
                # Trim leading/trailing blank lines inside the span
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

        if owner is not None:
            children = owner.get("children") or []
            children.append(span_node)
            owner["children"] = children
        else:
            root_children.append(span_node)


# @beacon[
#   id=auto-beacon@_split_frontmatter_if_any-3p1h,
#   role=ƒ _split_frontmatter_if_any(text),
#   slice_labels=nexus-md-header-path,ra-snippet-range-ast-md,ra-reconcile,carto-fwd-markdown,
#   kind=ast,
#   comment=Splits frontmatter and body on the shared physical-line model; Unicode separators within values never move subsequent heading coordinates.,
# ]
def _split_frontmatter_if_any(text: str | None) -> tuple[str | None, str]:
    """If text starts with valid YAML frontmatter, return (frontmatter, body).

    Frontmatter is defined as:
    - First line exactly '---' (no leading/trailing spaces)
    - A matching closing '---' on a later line

    If not present, returns (None, original_text). Only CRLF/CR/LF split rows.
    """
    if not isinstance(text, str):
        return None, ""

    lines = physical_source_lines(text)
    if not lines or lines[0].strip() != "---":
        return None, text

    frontmatter_lines: list[str] = []
    body_lines: list[str] = []
    found_closing = False

    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            found_closing = True
            body_lines = lines[idx + 1 :]
            break
        frontmatter_lines.append(line)

    if not found_closing:
        # Not a well-formed frontmatter block; treat as normal text.
        return None, text

    frontmatter = "\n".join(frontmatter_lines).strip("\n")
    body = "\n".join(body_lines)
    return frontmatter or None, body


# @beacon[
#   id=auto-beacon@_frontmatter_has_ignore_mdformat-y9k2,
#   role=ƒ _frontmatter_has_ignore_mdformat(frontmatter),
#   slice_labels=nexus-md-header-path,ra-snippet-range-ast-md,ra-reconcile,carto-fwd-markdown,
#   kind=ast,
#   comment=Reads the compatibility-only IGNORE_MDFORMAT frontmatter flag without splitting Unicode characters into source rows.,
# ]
def _frontmatter_has_ignore_mdformat(frontmatter: str | None) -> bool:
    """Return True if frontmatter contains IGNORE_MDFORMAT set to a truthy value.

    - Field name is matched case-insensitively: IGNORE_MDFORMAT
    - Values treated as true (case-insensitive): true, yes, on, 1
    - Stops on first occurrence of the key; only physical newlines split rows.
    """
    if not isinstance(frontmatter, str):
        return False

    for raw in physical_source_lines(frontmatter):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        if key.strip().lower() != "ignore_mdformat":
            continue
        v = val.strip().lower()
        if v in {"true", "yes", "on", "1"}:
            return True
        return False

    return False


_MARKDOWN_DISALLOWED_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def decode_workflowy_transport_entities_for_markdown_source(text: str | None) -> str:
    """Decode only Workflowy transport/readability entities for Markdown source.

    This helper is deliberately NOT ``html.unescape``. F12+3 regenerates
    Markdown SOURCE, not rendered HTML; full entity decoding would corrupt
    source-level entities such as ``&nbsp;`` into raw U+00A0 characters.

    Decode only the narrow set that represents Workflowy/API transport or
    source-readability normalization:
      - ``&amp;`` / ``&lt;`` / ``&gt;`` back to semantic ``&`` / ``<`` / ``>``
      - ``&quot;`` plus numeric double-quote spellings back to literal ``\"``

    The replacement is a SINGLE regex pass. That means authored deeper
    literals are not recursively collapsed: ``&amp;lt;`` becomes ``&lt;`` (not
    ``<``), while ``&nbsp;`` remains ``&nbsp;``.
    """
    if not isinstance(text, str) or not text:
        return "" if text is None else str(text or "")

    decode_map = {
        "&lt;": "<",
        "&gt;": ">",
        "&amp;": "&",
        "&quot;": '"',
        "&#34;": '"',
        "&#x22;": '"',
        "&#X22;": '"',
    }
    return re.sub(
        r"&(?:lt|gt|amp|quot|#34|#[xX]22);",
        lambda m: decode_map[m.group(0)],
        text,
    )


def _strip_disallowed_markdown_control_chars(text: str | None) -> str:
    """Repair/remove C0 controls that must never appear in Markdown content.

    We intentionally preserve TAB, LF, and CR. Everything else in the C0
    control range is treated as corruption. For the common accidental Python
    escape cases, preserve likely user intent by converting the invisible
    control back to a visible literal backslash sequence:

      - BEL (\x07) -> ``\\a``
      - BS  (\x08) -> ``\\b``
      - VT  (\x0b) -> ``\\v``
      - FF  (\x0c) -> ``\\f``

    Other illegal C0 controls are stripped. This is especially important for
    vertical tab (\x0b), which can appear when a Windows path like
    ``\agentic\visualizations\run_...`` is accidentally interpreted as
    Python/JSON-style escapes. Python ``str.splitlines()`` treats VT as a line
    boundary, while markdown-it token maps are LF-oriented; allowing VT into
    the parse stream desynchronizes heading maps from raw ``source_lines`` and
    can make a node's body start with its own heading line.
    """
    if not isinstance(text, str):
        return ""

    visible_escape_repairs = {
        "\x07": r"\a",
        "\x08": r"\b",
        "\x0b": r"\v",
        "\x0c": r"\f",
    }

    def _replace(match: re.Match[str]) -> str:
        return visible_escape_repairs.get(match.group(0), "")

    return _MARKDOWN_DISALLOWED_CONTROL_CHARS_RE.sub(_replace, text)


# @beacon[
#   id=auto-beacon@_sanitize_markdown_lines-4tow,
#   role=_sanitize_markdown_lines,
#   slice_labels=beacon-writeback,
#   kind=ast,
# ]
def _sanitize_markdown_lines(lines: list[str]) -> list[str]:
    """Strip illegal C0 controls from already-split Markdown lines."""
    return [_strip_disallowed_markdown_control_chars(line) for line in (lines or [])]


# @beacon[
#   id=carto-governance@claimed-markdown-c0-repair,
#   slice_labels=carto-governance,ra-reconcile,ra-carto-jobs,carto-fwd-markdown,
#   kind=ast,
#   comment=Explicit claimed whole-Markdown C0 repair: preserves BOM/CR with one snapshot and U02 publication; returns typed not_utf8/io_error read findings or commit evidence without stopping the independent map; claim and cancellation exceptions still propagate.,
# ]
def repair_markdown_c0_controls_on_disk(
    file_path: str,
    *,
    claim_id: str,
    cancel_check: "Callable[[], bool] | None" = None,
    log_callback: "Callable[[str], Any] | None" = None,
) -> dict[str, Any]:
    """Repair whole Markdown, returning content/IO failures as per-file data.

    Strict decode/read failures are not_utf8/io_error findings, never a lossy
    repair. Noop keeps mtime; BOM and every CR survive the existing C0 policy.
    Missing claim and cancellation retain their exception contracts. U02 alone
    proves and publishes; protected result propagation remains U09/J's hook.
    """
    if not str(claim_id or "").strip():
        raise ValueError("A live CARTO claim_id is required for on-disk C0 repair.")
    abs_path = os.path.realpath(file_path)
    try:
        snapshot = read_source_snapshot(file_path)
    except (UnicodeDecodeError, OSError) as error:
        kind = "not_utf8" if isinstance(error, UnicodeDecodeError) else "io_error"
        finding = {"operation": "repair_markdown_c0", "reason": kind, "file_path": abs_path,
                   "error": str(error), "candidates": []}
        message = f"Claimed Markdown C0 repair path={abs_path!r} claim={claim_id} counts={{}} failed: {kind}: {error}"
        if log_callback is not None:
            log_callback(message)
        else:
            logging.getLogger(__name__).info(message)
        return {"success": False, "changed": False, "path": abs_path, "counts": {},
                "error": str(error), "error_kind": kind,
                "before_sha256": None, "after_sha256": None, "edited_ranges": [],
                "events": [], "skipped_repairs": [finding], "findings": [finding], "diagnostics": []}
    abs_path = snapshot.canonical_path
    result = {"success": True, "changed": False, "path": abs_path, "counts": {},
              "before_sha256": snapshot.sha256, "after_sha256": snapshot.sha256,
              "edited_ranges": [], "events": [], "skipped_repairs": [], "findings": [], "diagnostics": []}
    text = snapshot.parse_text
    repaired = _strip_disallowed_markdown_control_chars(text)
    counts: dict[str, int] = {}
    names = {"\x07": "BEL", "\x08": "BS", "\x0b": "VT", "\x0c": "FF"}
    for char in _MARKDOWN_DISALLOWED_CONTROL_CHARS_RE.findall(text):
        key = names.get(char, f"C0-{ord(char):02X}")
        counts[key] = counts.get(key, 0) + 1
    if repaired == text:
        return result
    def check_cancelled():
        if cancel_check is not None and cancel_check():
            raise RuntimeError("C0 repair cancelled before atomic commit; source unchanged.")
    check_cancelled()
    plan = BeaconEditPlan(snapshot, "markdown", (
        BeaconEdit(snapshot.byte_offsets[0], len(snapshot.raw_bytes), repaired.encode("utf-8"), "document"),
    ), permitted_structural_effects=("whole_document",))
    committed = commit_beacon_edit_plan(plan, before_final_hash=check_cancelled)
    result.update(committed, counts=counts)
    evidence = {"operation": "repair_markdown_c0", "target": "document", "counts": counts,
                "original_range": (snapshot.byte_offsets[0], len(snapshot.raw_bytes)),
                "before_sha256": snapshot.sha256, "edited_ranges": _hygiene_edit_ranges(plan.edits),
                "members": [], "retained_ids": [], "dropped_ids": []}
    if committed["success"]:
        result["events"] = [{**evidence, "status": "committed", "after_sha256": committed["after_sha256"]}]
    else:
        finding = {**evidence, "status": "skipped", "reason": committed.get("error_kind", "c0_failed"),
                   "file_path": abs_path, "error": committed.get("error", "C0 repair failed"),
                   "candidates": [evidence], "commit_evidence": committed}
        result.update(after_sha256=None, edited_ranges=[], skipped_repairs=[finding], findings=[finding])
    message = f"Claimed Markdown C0 repair path={abs_path!r} claim={claim_id} " f"counts={counts}"
    if not committed["success"]:
        message += f" failed: {committed['error']} evidence={committed.get('evidence')}"
    if log_callback is not None:
        log_callback(message)
    else:
        logging.getLogger(__name__).info(message)
    return result


# @beacon[
#   id=auto-beacon@SourceChangedSincePlanError-cas1,
#   role=SourceChangedSincePlanError,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Compatibility exception for the explicit Markdown generation adapter when its expected snapshot or the shared byte commit final hash differs; ordinary plan commits return the same source_changed_since_plan result directly.,
# ]
class SourceChangedSincePlanError(Exception):
    """Compatibility exception carrying optional expected/actual hash evidence."""

    def __init__(self, *args, evidence=None):
        super().__init__(*args)
        self.evidence = evidence


# @beacon[
#   id=auto-beacon@_write_markdown_lines_sanitized-atom,
#   role=_write_markdown_lines_sanitized,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Compatibility serializer reserved for explicit whole-Markdown generation: sanitizer and exactly-one-final-LF semantics retained; all IO delegates to beacon_edits with a final raw hash. No ordinary beacon writer calls it.,
# ]
def _write_markdown_lines_sanitized(
    file_path: str,
    lines: list[str],
    *,
    expected_sha256: "str | None" = None,
) -> None:
    """Compatibility whole-document Markdown serializer, with delegated IO.

    Explicit generation keeps sanitizer/exactly-one-final-LF behavior. All
    ordinary plan and no-plan metadata writers bypass this compatibility
    adapter and use owned byte edits; no private publication IO lives here.
    """
    safe_lines = _sanitize_markdown_lines(lines)
    body = "\n".join(safe_lines).rstrip("\n") + "\n"
    try:
        snapshot = read_source_snapshot(file_path)
        exists = True
    except FileNotFoundError:
        snapshot = source_snapshot_from_bytes(b"", os.path.realpath(file_path))
        exists = False
    if expected_sha256 is not None and (not exists or snapshot.sha256 != expected_sha256):
        raise SourceChangedSincePlanError("source_changed_since_plan: expected snapshot differs; zero bytes written",
            evidence={"expected": {"exists": True, "sha256": expected_sha256},
                      "actual": {"exists": exists, "sha256": snapshot.sha256 if exists else None}})
    if snapshot.had_bom and body.startswith("\ufeff"):
        body = body[1:]
    plan = BeaconEditPlan(snapshot, "markdown", (
        BeaconEdit(snapshot.byte_offsets[0], len(snapshot.raw_bytes), body.encode("utf-8"), "document"),
    ), permitted_structural_effects=("whole_document",), source_exists=exists)
    result = commit_beacon_edit_plan(plan)
    if not result["success"]:
        if result["error_kind"] == "source_changed_since_plan":
            raise SourceChangedSincePlanError(result["error"], evidence=result.get("evidence"))
        raise OSError(result["error"])


def _markdown_source_lines(text: str | None) -> list[str]:
    """Markdown source lines over the shared physical-line rule.

    The scanner keeps EOF fields itself, so no terminal parse-buffer empty
    sentinel is appended. Extracted views never strip an interior BOM a
    second time.
    """
    if not isinstance(text, str):
        return []
    return physical_source_lines(text)


# @beacon[
#   id=auto-beacon@parse_markdown_structure-z918,
#   role=parse_markdown_structure(file_path: str),
#   slice_labels=nexus-md-header-path,ra-snippet-range-ast-md,ra-reconcile,carto-fwd-markdown,nexus--md-preview,
#   kind=ast,
#   comment=Parses Markdown from one snapshot with physical-line coordinates and shared beacon records; incomplete unowned HTML wrappers are blanked only in the heading parse view so later headings survive, while original note/source bytes remain untouched.,
# ]
def parse_markdown_structure(
    file_path: str, *, snapshot: SourceSnapshot | None = None, diagnostics: list | None = None,
) -> List[Dict[str, Any]]:
    """Parse Markdown into a NEXUS node tree using markdown-it-py.

    Parsing is a pure read: illegal C0 controls are sanitized only in the
    in-memory parse buffer, and mdformat is globally disabled. The preserved
    ``IGNORE_MDFORMAT`` frontmatter flag is currently a compatibility no-op.
    A supplied snapshot is never reopened. Document order supplies priorities and the markdown-it token stream is
    converted into the hierarchical NEXUS tree. Recorded incomplete HTML
    wrapper openers are released only in the heading parse buffer, not on
    disk or in source-note bodies; they cannot swallow later headings.
    """
    try:
        # STEP 1: All source views belong to the same binary observation.
        snapshot = snapshot if snapshot is not None else read_source_snapshot(file_path)
        original_content_raw = snapshot.normalized_text
        original_content = _strip_disallowed_markdown_control_chars(original_content_raw)
        # Parsing is a pure read. Illegal C0 controls are repaired only in the
        # in-memory parse buffer; a claimed CARTO owner will gain an explicit
        # atomic repair primitive in Phase C.

        # STEP 1.1: Detect YAML frontmatter and per-file IGNORE_MDFORMAT flag.
        # NOTE: this detection is preserved as suspenders, but mdformat is now
        # globally disabled below regardless of the flag value.
        frontmatter, _body = _split_frontmatter_if_any(original_content)
        ignore_mdformat = _frontmatter_has_ignore_mdformat(frontmatter)

        # MDFORMAT GLOBALLY DISABLED (Dan, 2026-04-30):
        # mdformat doesn't work well with agents. Agents reflexively add YAML
        # frontmatter to files that don't need it (e.g. 99 files), and mdformat
        # mangles YAML frontmatter and rewrites the source file unconditionally
        # on every F12 refresh, locking in any corruption. The IGNORE_MDFORMAT
        # flag is preserved above for backward compatibility but is now a noop:
        # the skip-mdformat branch is taken unconditionally.
        #
        # Original gated behavior is preserved below in commented form for
        # reference. To re-enable mdformat on a per-file basis in the future,
        # restore the if/else and ensure agents respect the IGNORE_MDFORMAT
        # opt-out invariant.
        #
        # if ignore_mdformat:
        #     body_for_parse = _body
        #     full_content_for_parse = original_content
        # else:
        #     body_for_parse = mdformat.text(_body)
        #     full_content_for_parse = (f"---\n{frontmatter}\n---\n" if frontmatter else "") + body_for_parse
        #     with open(file_path, 'w', encoding='utf-8') as f:
        #         f.write(full_content_for_parse)

        body_for_parse = _body
        full_content_for_parse = original_content
        if DEBUG_MD_BEACONS:
            print(
                f"[MD-FILE] path={file_path!r} (mdformat globally disabled; "
                f"IGNORE_MDFORMAT={ignore_mdformat} preserved as no-op)",
            )

        lines = _markdown_source_lines(full_content_for_parse)
        beacon_snapshot = _source_line_view(lines)
        md_records = _scan_source_beacons(beacon_snapshot, "markdown")
        md_beacons = _parse_structural_beacons("markdown", lines, records=md_records)
        unowned_wrappers = [r for r in md_records if not r["complete"] and r["wrapper_open_range"] and not r["wrapper_range"]]
        heading_content = full_content_for_parse
        if unowned_wrappers:
            projected = beacon_context_lines(beacon_snapshot, unowned_wrappers, release_incomplete_wrappers=True)
            heading_content = "\n".join(projected) + ("\n" if full_content_for_parse.endswith("\n") else "")
        _, heading_body = _split_frontmatter_if_any(heading_content)

        # STEP 2: Parse ONLY the body with markdown-it-py to avoid misinterpreting frontmatter.
        #
        # We keep markdown-it-py's block parser because it already knows the
        # hard cases (fenced code, indented code, HTML blocks, blockquotes,
        # source maps). The only thing we override is the ATX heading rule's
        # CommonMark 6-hash cap. Dan's Workflowy-first 99-file methodology
        # explicitly supports 7, 8, 9+ hash headings as real child nodes.
        md = MarkdownIt("commonmark")
        try:
            md.block.ruler.at("heading", markdown_it_heading_arbitrary_depth)
        except Exception:
            # Fail loudly enough to be visible in the parse-error node rather
            # than silently falling back to the 6-hash CommonMark rule.
            raise
        tokens = md.parse(heading_body)

        if DEBUG_MD_BEACONS:
            print("[MD-TOKENS-BEGIN]")
            for t in tokens:
                # t.map is (start_line, end_line) 0-based; add 1 for human-readable
                line_span = t.map if hasattr(t, 'map') and t.map is not None else None
                if line_span:
                    span_str = f"[{line_span[0]+1},{line_span[1]+1}]"
                else:
                    span_str = "[]"
                print(f"  type={t.type!r} tag={t.tag!r} map={span_str} content={t.content!r}")
            print("[MD-TOKENS-END]")

        # STEP 3: Convert token stream to NEXUS hierarchical tree with priorities.
        # Use the exact body lines from the parsed Markdown so list/code-block
        # structure is preserved verbatim inside heading notes.
        # When YAML frontmatter exists, markdown-it token line maps are relative
        # to body_for_parse, while apply_markdown_beacons receives full-file
        # line numbers. Carry the body offset into each heading node's internal
        # _md_heading_line coordinate so beacon attachment is line-accurate.
        body_line_offset = 0
        if frontmatter is not None:
            # Opening '---' + frontmatter lines + closing '---'.
            body_line_offset = len(physical_source_lines(frontmatter)) + 2
        root_children = tokens_to_nexus_tree(
            tokens,
            source_lines=_markdown_source_lines(body_for_parse),
            source_line_offset=body_line_offset,
        )
        
        if frontmatter is not None:
            # Preserve frontmatter at the top of the tree so it survives round-trips
            root_children.insert(0, {
                "name": "⚙️ YAML Frontmatter",
                "priority": 0,
                "note": frontmatter,
                "children": []
            })

        # Preserve any "leading text" — prose that appears in the body BEFORE the
        # first heading. The heading-only tree builder (tokens_to_nexus_tree)
        # has no heading to anchor such content to, so without this it would be
        # silently dropped from the Workflowy view (the F12+1 leading-text bug).
        # We represent it exactly like frontmatter: a dedicated, recognizable
        # top-of-tree node (NOT a synthetic heading), so markdown_roundtrip
        # emits it verbatim above the first heading on F12+3 and the round-trip
        # F12+1 -> F12+3 -> F12+1 is idempotent (it never becomes a heading, so
        # nothing accumulates). priority=1 places it after frontmatter
        # (priority 0) and before the first real heading (priority 100+).
        leading_text = _extract_markdown_leading_text(
            tokens,
            body_source_lines=_markdown_source_lines(body_for_parse),
        )
        if leading_text:
            insert_idx = 1 if frontmatter is not None else 0
            root_children.insert(insert_idx, {
                "name": MD_LEADING_TEXT_NODE_NAME,
                "priority": 1,
                "note": leading_text,
                "children": []
            })

        if md_beacons:
            apply_markdown_beacons(lines, root_children, md_beacons, records=md_records)
        return root_children
        
    except Exception as e:
        if diagnostics is not None:
            diagnostics.append({"record": "outline_error", "file": file_path, "language": "markdown", "exception": str(e)})
        return [{"name": _warn_name("⚠️ MD Parse Error"), "note": str(e), "children": []}]

# @beacon[
#   id=auto-beacon@normalize_for_match-z5hh,
#   role=normalize_for_match,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def normalize_for_match(s: str) -> str:
    """Normalize a snippet or line for matching.

    V1: remove all whitespace characters so that `x=3` matches `x = 3`.
    This intentionally trades a tiny false-positive risk for robustness
    against formatting-only changes.
    """
    if not isinstance(s, str):
        return ""
    return "".join(ch for ch in s if not ch.isspace())


_TAG_LIKE_PATTERN = re.compile(r"<[A-Za-z_/][-A-Za-z0-9_/]*>")


# @beacon[
#   id=auto-beacon@whiten_text_for_header_compare-56c5,
#   role=whiten_text_for_header_compare(text),
#   slice_labels=nexus-md-header-path,ra-snippet-range-ast-md,carto-fwd-markdown,nexus--md-preview,agent-kit-vendor,
#   kind=ast,
#   comment=Shared junk-stripper for structural comparison (markup/backtick/HTML-entity/quote/emoji handling in ONE place). collapse_whitespace gates ONLY the final whitespace removal: True (default) = the original header/name comparison behavior, byte-for-byte unchanged; False = preserve whitespace so a whitespace-only content change is still seen as a difference (used by whiten_note_for_compare for the note no-op-suppression path).,
# ]
def whiten_text_for_header_compare(text: str | None, *, collapse_whitespace: bool = True) -> str:
    """Whiten heading-like text for structural comparison.

    Used by Cartographer/F12/MD_PATH flows to decide whether two headings are
    "semantically the same" while preserving trailing #tags.

    Rules:
    - Strip simple HTML tags like <code>, </code>, <span>, </span> by pattern:
      <[A-Za-z_/][-A-Za-z0-9_/]*>
      (no whitespace or digits at the start, so "a < 5" is untouched).
    - Drop all backticks.
    - Drop emoji / Symbol, Other (So) codepoints.
    - Drop all whitespace (ONLY when ``collapse_whitespace=True``, the default).
    - Do NOT strip '#' or words, so tag changes still show up as differences.

    ``collapse_whitespace`` (keyword-only, default True):
        When True, all whitespace is removed at the end (the original,
        heavily-vetted header/name comparison behavior -- every existing
        caller relies on this and is unchanged).

        When False, every other transformation above is applied identically,
        but whitespace is PRESERVED. This exists so that NOTE comparisons in
        ``reconcile_trees_cartographer`` (via the ``whiten_note_for_compare``
        wrapper) can treat a whitespace-only edit to a slurped comment /
        docstring as a real change. The header/name path is whitespace-
        insensitive on purpose (Workflowy re-wraps NAME-field whitespace, and
        we must not churn the tree over that); but a slurped NOTE is content,
        where a lone inserted space (e.g. ``#nexus`` -> ``# nexus`` inside a
        comment) is a genuine on-disk change that F12+1 must propagate.

        The junk-stripping (markup, backticks, HTML entities, quotes, emoji)
        is shared across both modes precisely so the two paths can never
        drift -- only the whitespace policy differs.
    """
    if not isinstance(text, str):
        return ""
    s = text

    # Remove simple tag-like markup such as <code>, </code>, <span>, </span>.
    s = _TAG_LIKE_PATTERN.sub("", s)

    # Drop backticks used for inline code in Markdown.
    s = s.replace("`", "")

    # Normalize a small set of common HTML entities so textual comparisons
    # are robust to &lt;/&gt;/&amp;/&quot; vs their literal forms.
    html_entity_map = {
        "&quot;": '"',
        "&lt;": "<",
        "&gt;": ">",
        "&amp;": "&",
    }
    for ent, ch in html_entity_map.items():
        if ent in s:
            s = s.replace(ent, ch)

    # Drop double quotes entirely (including those produced from &quot;).
    s = s.replace('"', "")

    # Remove emoji / other symbolic decoration.
    #
    # NOTE: We *preserve* the trident (🔱) because it is a semantic UI marker
    # for Cartographer beacon-decorated nodes. If we strip it here, the
    # Cartographer no-op suppression in reconcile_trees_cartographer(...) will
    # treat names with/without 🔱 as equal and may "restore" the old Ether name,
    # causing the trident to vanish after a file refresh.
    _PRESERVE_SO = {"🔱"}
    s = "".join(ch for ch in s if unicodedata.category(ch) != "So" or ch in _PRESERVE_SO)

    # Collapse and remove whitespace entirely -- but ONLY when the caller wants
    # the whitespace-insensitive header/name behavior. When collapse_whitespace
    # is False (the note-comparison path), whitespace is meaningful content and
    # is preserved so whitespace-only edits are detected as real differences.
    if collapse_whitespace:
        s = re.sub(r"\s+", "", s)
    return s


# @beacon[
#   id=auto-beacon@whiten_note_for_compare-9f2a,
#   role=whiten_note_for_compare(text),
#   slice_labels=nexus-md-header-path,ra-reconcile,carto-fwd-source,nexus--md-preview,
#   kind=ast,
#   comment=Note-comparison twin of whiten_text_for_header_compare: identical junk-stripping (markup/backtick/HTML-entity/quote/emoji), but whitespace is PRESERVED (collapse_whitespace=False). Used for NOTE no-op suppression in reconcile_trees_cartographer so a whitespace-only edit to a slurped comment/docstring (e.g. '#nexus' -> '# nexus') is still seen as a real on-disk change and propagated by F12+1. Fixes the silent skipped-update where the header whitener collapsed the lone inserted space and declared the note unchanged.,
# ]
def whiten_note_for_compare(text: str | None) -> str:
    """Whiten a NOTE for no-op-suppression comparison, preserving whitespace.

    Thin wrapper over :func:`whiten_text_for_header_compare` with
    ``collapse_whitespace=False``. All the shared junk-stripping (HTML tag-like
    markup, backticks, HTML entities, double quotes, emoji with trident
    preserved) is applied exactly as for header/name comparisons -- the ONLY
    difference is that whitespace is NOT collapsed away.

    Rationale: a slurped comment/docstring is real file content. A purely
    whitespace change on disk (the classic example: editing ``#nexus`` to
    ``# nexus`` inside a context comment so the tag stops being picked up by a
    Workflowy tag-click) is a genuine change that an F12+1 refresh must push
    into the Workflowy node note. The header/name whitener removes all
    whitespace and is therefore blind to such edits; using it for the note
    no-op-suppression branch caused F12+1 to silently restore the stale note
    and emit no update. This wrapper is the fix.
    """
    return whiten_text_for_header_compare(text, collapse_whitespace=False)


# @beacon[
#   id=auto-beacon@_extract_beacon_id_from_note-ovd8,
#   role=_extract_beacon_id_from_note,
#   slice_labels=ra-reconcile,
#   kind=ast,
#   comment=Compatibility projection of the leading mapped header id through the shared record reader; the complete list remains available to intent consumers and no span/context text supplies an id.,
# ]
def _extract_beacon_id_from_note(note: str | None) -> str | None:
    """Project the leading mapped header id; never read span/context text."""
    records = parse_beacon_note_metadata(note)
    ids = {v["value"] for r in records[:1]
           for v in r["occurrences"] if v["key"] == "id" and v["value"]}
    return next(iter(ids)) if len(ids) == 1 else None


# @beacon[
#   id=auto-beacon@_extract_slice_labels_from_beacon_block-2nx9,
#   role=ƒ _extract_slice_labels_from_beacon_block(note),
#   slice_labels=ra-reconcile,ra-bulk-visible-apply,nexus--tags,
#   kind=ast,
#   comment=Project one unambiguous slice_labels value from the shared scoped note-header reader for the bulk-apply pre-filter; span/context text is never metadata.,
# ]
def _extract_slice_labels_from_beacon_block(note: str | None) -> str | None:
    """Project a unique header value, retaining empty labels versus absence."""
    values = {v["value"] for r in parse_beacon_note_metadata(note)
              for v in r["occurrences"] if v["key"] == "slice_labels"}
    return next(iter(values)) if len(values) == 1 else None


# @beacon[
#   id=auto-beacon@_extract_role_from_beacon_block-7d4q,
#   role=_extract_role_from_beacon_block,
#   slice_labels=ra-reconcile,ra-bulk-visible-apply,
#   kind=ast,
#   comment=Sibling of _extract_beacon_id_from_note. Reads the role: line out of the BEACON (...) block in a node note. Used by the F12+3 step [3] bulk-apply pre-filter to detect nodes whose decoration-stripped name matches the on-disk beacon block's role (e.g. catches Workflowy heading renames that need a disk write).,
# ]
def _extract_role_from_beacon_block(note: str | None) -> str | None:
    """Extract `role:` field from a node's BEACON (...) block, if any.

    Returns None if the note has no BEACON block or no role line inside it.
    Returns the raw value otherwise.
    """
    if not isinstance(note, str) or "BEACON (" not in note:
        return None
    in_block = False
    for line in note.splitlines():
        stripped = line.strip()
        if not in_block:
            if stripped.startswith("BEACON ("):
                in_block = True
            continue
        if not stripped or stripped == "---":
            return None
        if stripped.startswith("role:"):
            val = stripped.split(":", 1)[1].strip()
            return val
    return None


# @beacon[
#   id=auto-beacon@_extract_ast_qualname_from_note-gzzl,
#   role=_extract_ast_qualname_from_note,
#   slice_labels=ra-reconcile,
#   kind=ast,
# ]
def _extract_ast_qualname_from_note(note: str | None) -> str | None:
    """Extract AST_QUALNAME from a node's note, if present."""
    if not isinstance(note, str):
        return None
    for line in note.splitlines():
        stripped = line.strip()
        if stripped.startswith("AST_QUALNAME:"):
            val = stripped.split(":", 1)[1].strip()
            return val or None
    return None


# @beacon[
#   id=carto-js-ts@_is_meaningful_js_local_name-p7m2,
#   role=ƒ _is_meaningful_js_local_name(var_name),
#   slice_labels=carto-js-ts,ra-reconcile,carto-fwd-source,
#   kind=ast,
#   comment=Heuristic filter for noisy JS/TS local variables so F12 maps meaningful locals without single-letter spam.,
# ]
def _is_meaningful_js_local_name(var_name: str | None) -> bool:
    """Return True when a JS/TS local variable name is worth mapping.

    Current heuristic is intentionally simple and conservative:
    - Drop names with fewer than 3 substantive chars after removing
      underscores/$.
      Examples: i, j, _, _i, $x, me, el

    We keep longer names for now because some locals are genuinely meaningful.
    """
    raw = (var_name or "").strip()
    if not raw:
        return False

    compact = re.sub(r"[_$]", "", raw)
    if len(compact) < 3:
        return False
    return True


# @beacon[
#   id=carto-js-ts@_new_js_scope_state-w1f9,
#   role=ƒ _new_js_scope_state(),
#   slice_labels=carto-js-ts,ra-reconcile,carto-fwd-source,
#   kind=ast,
#   comment=Create per-scope JS/TS parser state so anonymous lambda identities and local occurrence counters stay stable across formatting-only edits.,
# ]
def _new_js_scope_state(*, is_lambda: bool) -> dict[str, Any]:
    """Return mutable parser state for one JS/TS scope."""
    return {
        "is_lambda": bool(is_lambda),
        "anon_counter": 0,
        "local_name_counts": {},
    }


# @beacon[
#   id=carto-js-ts@_js_next_anonymous_scope_name-x6c2,
#   role=ƒ _js_next_anonymous_scope_name(scope_state),
#   slice_labels=carto-js-ts,ra-reconcile,carto-fwd-source,
#   kind=ast,
#   comment=Assign stable parent-local ordinal identities to anonymous JS/TS lambda scopes instead of using file line numbers.,
# ]
def _js_next_anonymous_scope_name(scope_state: dict[str, Any] | None) -> str:
    """Return the next anonymous-scope name for the current parent scope."""
    if not isinstance(scope_state, dict):
        return "__lambdao_1"

    next_n = int(scope_state.get("anon_counter") or 0) + 1
    scope_state["anon_counter"] = next_n
    return f"__lambdao_{next_n}"


# @beacon[
#   id=carto-js-ts@_js_next_lambda_local_suffix-r3d7,
#   role=ƒ _js_next_lambda_local_suffix(scope_state, var_name: str),
#   slice_labels=carto-js-ts,ra-reconcile,carto-fwd-source,
#   kind=ast,
#   comment=Assign stable occurrence-index suffixes to meaningful lambda-local declarations so comments and whitespace do not churn AST_QUALNAMEs.,
# ]
def _js_next_lambda_local_suffix(scope_state: dict[str, Any] | None, var_name: str) -> str:
    """Return the next occurrence index for a lambda-local variable name."""
    if not isinstance(scope_state, dict):
        return "1"

    counts = scope_state.get("local_name_counts")
    if not isinstance(counts, dict):
        counts = {}
        scope_state["local_name_counts"] = counts

    next_n = int(counts.get(var_name) or 0) + 1
    counts[var_name] = next_n
    return str(next_n)


# @beacon[
#   id=auto-beacon@_extract_md_path_from_note-eihl,
#   role=ƒ _extract_md_path_from_note(note),
#   slice_labels=ra-snippet-range-ast-md,ra-reconcile,carto-fwd-markdown,agent-kit-vendor,
#   kind=ast,
# ]
def _extract_md_path_from_note(note: str | None) -> list[str] | None:
    """Extract MD_PATH heading lines from a Markdown node's note, if any.

    Returns the raw heading lines (including leading '#' characters) between
    the 'MD_PATH:' sentinel and the terminating '---' line.

    Bug #3 hardening (Dan, 2026-05-01): the MD_PATH section is supposed to
    contain only Markdown heading lines (1-6 '#' characters followed by
    whitespace and heading text). An earlier corruption mode could leave
    BEACON metadata fields ('id: ...', 'role: ...', 'slice_labels: ...',
    'kind: ast') interleaved inside the MD_PATH block when the closing
    '---' separator was missing or misplaced. To make F12+3 phase [7]
    (update_beacon_from_node_markdown) and other MD_PATH consumers
    resilient against such cache poisoning, this extractor now skips any
    line inside the MD_PATH block that does NOT match the Markdown
    heading shape ``^#{1,32}\\s+\\S``. The block still terminates on the
    first '---' as before. This is layer (1) of the defense-in-depth
    fix; layers (2)/(3) live in markdown_roundtrip.nexus_to_tokens and
    in tokens_to_nexus_tree's MD_PATH builder below.
    """
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
        # Layer (1) filter: only accept actual Markdown heading lines.
        # Drops stray BEACON metadata fields that may have leaked into
        # the MD_PATH block due to historical corruption.
        if not re.match(r"^#{1,32}\s+\S", stripped):
            continue
        # Preserve the heading text as written; callers may whiten it.
        path_lines.append(stripped)
    return path_lines or None


# @beacon[
#   id=beacon-writeback@_markdown_path_identity,
#   slice_labels=beacon-writeback,carto-fwd-markdown,agent-kit-vendor,
#   kind=ast,
#   comment=One pure level and whitened casefolded-text identity for requested or mapped Markdown heading paths; shared by compatibility lookup and the physical selector so the kit needs no Markdown parser dependency.,
# ]
def _markdown_path_identity(path_lines) -> tuple:
    """Normalize heading levels and semantic text without parsing source or IO."""
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


# @beacon[
#   id=auto-beacon@_find_markdown_heading_insert_idx-6t3r,
#   role=ƒ _find_markdown_heading_insert_idx(lines: list[str], md_path_lines: list[str]),
#   slice_labels=nexus-md-header-path,ra-snippet-range-ast-md,ra-reconcile,carto-fwd-markdown,
#   kind=ast,
#   comment=Compatibility source lookup parses structural headings then compares the shared pure Markdown path identity; preserves all-heading occurrence behavior while the selector consumes its already parsed outline instead of reparsing.,
# ]
def _find_markdown_heading_insert_idx(
    lines: list[str],
    md_path_lines: list[str],
    occurrence: Optional[int] = None,
) -> Optional[int]:
    """Return the 0-based insertion index immediately above the heading for MD_PATH.

    This is the reverse/export twin of Markdown heading identity resolution:
    given raw file lines and an MD_PATH sequence, locate the heading line whose
    ancestor chain matches exactly.
    Path normalization is shared with the selector's already mapped headings.

    DUPLICATE-SIBLING DISAMBIGUATION (Option C, Dan June 2026)
    ---------------------------------------------------------
    MD_PATH is a *semantic path of heading texts*, NOT a unique key: Markdown
    legally allows two sibling headings with the same text under the same
    parent (e.g. two ``## Dump Bay`` under one Curation Capsule), so the same
    MD_PATH chain can match N>1 headings in document order. This is the
    on-disk/write-side twin of ``get_snippet_for_md_path``'s read-side fix.

    When the caller knows WHICH duplicate it wants (because it started from a
    specific Workflowy UUID and computed that node's 1-based ordinal among
    same-name siblings, ordered by the cache ``priority`` field, OR derived the
    occurrence from the live F12+3 tree walk), it passes ``occurrence`` and we
    return the ``occurrence``-th match (in document order) instead of failing.
    Workflowy sibling order == disk document order is maintained by F12
    reconcile, so the cache/tree ordinal equals the disk occurrence.

    Returns None on:
      - zero matches, OR
      - multiple matches with no (or an out-of-range) ``occurrence`` ordinal.
    The None-on-ambiguity-without-ordinal behaviour preserves the historic
    fail-closed contract (callers refuse to write rather than guess).
    """
    if not lines or not md_path_lines:
        return None

    desired = _markdown_path_identity(md_path_lines)
    if not desired:
        return None

    md_text = "\n".join(lines)
    md = MarkdownIt("commonmark")
    md.block.ruler.at("heading", markdown_it_heading_arbitrary_depth)
    tokens = md.parse(md_text)

    stack: list[tuple[int, str]] = []
    matches: list[int] = []
    # Track blockquote AND list-item nesting depth so we ignore '#' lines
    # that live inside Markdown body-content containers. Mirrors the same
    # guards added to tokens_to_nexus_tree: blockquoted or list-item-nested
    # headings are body content (e.g. quoted PDFs, transcripts, list-item
    # subheadings), not document structure, and must not be candidates for
    # MD_PATH-based insertion-point resolution. (Dan, May 2026.)
    blockquote_depth: int = 0
    list_item_depth: int = 0
    for idx, token in enumerate(tokens):
        if token.type == "blockquote_open":
            blockquote_depth += 1
            continue
        if token.type == "blockquote_close":
            if blockquote_depth > 0:
                blockquote_depth -= 1
            continue
        if token.type == "list_item_open":
            list_item_depth += 1
            continue
        if token.type == "list_item_close":
            if list_item_depth > 0:
                list_item_depth -= 1
            continue
        if token.type != "heading_open":
            continue
        if blockquote_depth > 0 or list_item_depth > 0:
            continue

        try:
            level = int(token.tag[1:]) if token.tag and len(token.tag) > 1 else 1
        except Exception:
            level = 1

        text = ""
        if idx + 1 < len(tokens) and tokens[idx + 1].type == "inline":
            text = tokens[idx + 1].content or ""
        norm = whiten_text_for_header_compare(text).casefold()

        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, norm))

        start0 = None
        if getattr(token, "map", None):
            start0 = token.map[0]
        elif idx + 1 < len(tokens) and getattr(tokens[idx + 1], "map", None):
            start0 = tokens[idx + 1].map[0]
        if start0 is None:
            start0 = 0

        if len(stack) != len(desired):
            continue
        if all(stack[j][0] == desired[j][0] and stack[j][1] == desired[j][1] for j in range(len(desired))):
            matches.append(start0)

    # `matches` is built in markdown-it token order == document order, so
    # matches[k] is the (k+1)-th occurrence of this MD_PATH chain on disk.
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    # Ambiguous: same MD_PATH chain matches multiple sibling headings.
    # Option C: if the caller supplied a 1-based document-order occurrence,
    # select that match. Otherwise preserve the historic fail-closed contract
    # (return None so the caller refuses to write rather than guess).
    if isinstance(occurrence, int) and 1 <= occurrence <= len(matches):
        return matches[occurrence - 1]
    return None


def _extract_vue_section_key_from_note(note: str | None) -> str | None:
    """Extract a stable identity key for Vue section nodes.

    Section nodes are virtual wrappers (template/script/style). They do not
    naturally have Beacon IDs or AST_QUALNAMEs, so name-only matching can
    cause churn when we adjust display formatting (e.g. '<script>' → '[script]').

    We therefore match section nodes by a key derived from their note fields.
    """
    if not isinstance(note, str):
        return None

    section: str | None = None
    content_row0: str | None = None
    script_index: str | None = None
    style_index: str | None = None

    for raw in note.splitlines():
        s = raw.strip()
        if s.startswith("VUE_SECTION:"):
            section = s.split(":", 1)[1].strip() or None
        elif s.startswith("VUE_CONTENT_ROW0:"):
            content_row0 = s.split(":", 1)[1].strip() or None
        elif s.startswith("VUE_SCRIPT_INDEX:"):
            script_index = s.split(":", 1)[1].strip() or None
        elif s.startswith("VUE_STYLE_INDEX:"):
            style_index = s.split(":", 1)[1].strip() or None

    if not section:
        return None

    # Prefer explicit indices when present.
    if script_index is not None:
        return f"{section}:script:{script_index}"
    if style_index is not None:
        return f"{section}:style:{style_index}"
    if content_row0 is not None:
        return f"{section}:row0:{content_row0}"

    return section


# @beacon[
#   id=carto-virtual-groups@identity-and-grouping,
#   role=Cartographer virtual groups: stable source-authored grouping wrappers,
#   slice_labels=carto-js-ts,ra-reconcile,carto-fwd-source,
#   kind=span,
#   comment=Shared helpers for source-comment-driven virtual grouping. Comments like '# @carto-group id=x label="X"' or '// @carto-group id=x label="X"' create synthetic non-AST Workflowy wrapper nodes while preserving runtime source semantics. Also supplies group identity extraction and any-depth UUID seeding so first migration is MOVE-not-delete/create.,
# ]
def _extract_carto_group_key_from_note(note: str | None) -> str | None:
    """Extract CARTO_GROUP_ID from a synthetic Cartographer virtual group note."""
    if not isinstance(note, str):
        return None
    saw_virtual = False
    group_id: str | None = None
    for raw in note.splitlines():
        s = raw.strip()
        if s.startswith("CARTOGRAPHER_VIRTUAL_GROUP:"):
            saw_virtual = True
        elif s.startswith("CARTO_GROUP_ID:"):
            group_id = s.split(":", 1)[1].strip() or None
    return group_id if saw_virtual and group_id else None


def _parse_carto_group_marker_body(body: str, *, line_no: int) -> dict[str, Any] | None:
    """Parse the body after '@carto-group' into a stable id + display label.

    Supported forms (intentionally small grammar):
        @carto-group id=uuid-nav label="UUID Navigator"
        @carto-group id=uuid-nav label=UUID-Navigator
        @carto-group uuid-nav | UUID Navigator

    The id is the durable source-authored identity; the label is display-only.
    """
    raw = (body or "").strip()
    if raw.startswith("@carto-group"):
        raw = raw[len("@carto-group"):].strip()

    group_id: str | None = None
    label: str | None = None

    def _field(name: str) -> str | None:
        # Quoted value or a single non-whitespace token.
        m = re.search(rf"(?:^|\s){re.escape(name)}\s*=\s*(\"([^\"]*)\"|'([^']*)'|([^\s|]+))", raw)
        if not m:
            return None
        return (m.group(2) if m.group(2) is not None else m.group(3) if m.group(3) is not None else m.group(4) or "").strip() or None

    group_id = _field("id")
    label = _field("label")

    if group_id is None and "|" in raw:
        left, right = raw.split("|", 1)
        left = left.strip()
        right = right.strip()
        if left and "=" not in left:
            group_id = left
            label = right or left

    if group_id is None:
        return {
            "error": "missing_id",
            "line": line_no,
            "raw": body,
        }

    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$", group_id):
        return {
            "error": "invalid_id",
            "line": line_no,
            "raw": body,
            "group_id": group_id,
        }

    if not label:
        label = group_id

    return {
        "id": group_id,
        "label": label,
        "line": line_no,
        "raw": body,
    }


def _iter_carto_group_markers(
    lines: list[str],
    *,
    comment_style: str,
    line_offset0: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (markers, errors) parsed from source lines for one language style.

    For Python, v1 only accepts indentation-zero ``# @carto-group`` markers.
    For JS/TS, v1 accepts ``//`` and one-line ``/* ... */`` markers; callers
    separately validate that accepted markers are not inside top-level AST spans.
    Returned line numbers are absolute file line numbers (1-based).
    """
    markers: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for local_ix, raw in enumerate(lines, start=1):
        abs_line = local_ix + int(line_offset0 or 0)
        text = raw.rstrip("\n\r")
        body: str | None = None

        if comment_style == "python":
            # Indentation-zero only. ``#`` may be followed by whitespace.
            if text.startswith("#"):
                candidate = text[1:].lstrip()
                if candidate.startswith("@carto-group"):
                    body = candidate
        elif comment_style == "js_ts":
            stripped = text.lstrip()
            if stripped.startswith("//"):
                candidate = stripped[2:].lstrip()
                if candidate.startswith("@carto-group"):
                    body = candidate
            elif stripped.startswith("/*") and "*/" in stripped:
                candidate = stripped[2: stripped.find("*/")].strip()
                if candidate.startswith("@carto-group"):
                    body = candidate

        if body is None:
            continue

        parsed = _parse_carto_group_marker_body(body, line_no=abs_line)
        if not parsed:
            continue
        if parsed.get("error"):
            errors.append(parsed)
        else:
            markers.append(parsed)

    seen: dict[str, dict[str, Any]] = {}
    for marker in markers:
        gid = str(marker.get("id") or "")
        if gid in seen:
            errors.append({
                "error": "duplicate_id",
                "group_id": gid,
                "line": marker.get("line"),
                "first_line": seen[gid].get("line"),
            })
        else:
            seen[gid] = marker

    if errors:
        # Do not group when the identity contract is invalid. The caller keeps
        # the original ungrouped outline and surfaces a warning node. This is
        # fail-safe: no mass parent migration occurs on malformed markers.
        return ([], errors)

    return (markers, [])


def _carto_group_warning_node(errors: list[dict[str, Any]], *, language: str) -> dict[str, Any]:
    lines = [
        "CARTOGRAPHER_VIRTUAL_GROUP_ERRORS",
        f"language: {language}",
        "---",
        "Virtual grouping was skipped because one or more @carto-group markers are invalid.",
        "Fix the marker(s) and refresh again. The source outline was otherwise left ungrouped to avoid destructive parent churn.",
        "",
        "Errors:",
    ]
    for e in errors:
        lines.append(json.dumps(e, ensure_ascii=False, sort_keys=True))
    return {
        "name": _warn_name("⚠️ Cartographer virtual group marker error"),
        "note": "\n".join(lines),
        "children": [],
        "_position_hint": "top",
    }


def _marker_inside_top_level_outline_node(marker_line: int, outline_nodes: list[dict[str, Any]]) -> bool:
    """True if marker_line lies inside (not before) a top-level AST node span."""
    for node in outline_nodes or []:
        if not isinstance(node, dict):
            continue
        start = node.get("orig_lineno_start_unused")
        end = node.get("orig_lineno_end_unused")
        if isinstance(start, int) and isinstance(end, int) and start < marker_line <= end:
            return True
    return False


def group_outline_by_carto_markers(
    *,
    lines: list[str],
    outline_nodes: list[dict[str, Any]],
    language: str,
    comment_style: str,
    line_offset0: int = 0,
) -> list[dict[str, Any]]:
    """Wrap top-level outline nodes under source-comment virtual groups.

    This is deliberately a PRESENTATION-STRUCTURE pass over the already-built
    Cartographer outline. It never changes source semantics, never descends into
    AST children, and never assigns AST identity to the group wrappers.
    """
    markers, errors = _iter_carto_group_markers(
        lines,
        comment_style=comment_style,
        line_offset0=line_offset0,
    )
    if errors:
        return [_carto_group_warning_node(errors, language=language)] + list(outline_nodes or [])
    if not markers:
        return outline_nodes

    valid_markers: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for marker in markers:
        line = marker.get("line")
        if isinstance(line, int) and _marker_inside_top_level_outline_node(line, outline_nodes):
            skipped.append({
                "error": "marker_inside_top_level_ast_node",
                "group_id": marker.get("id"),
                "line": line,
            })
        else:
            valid_markers.append(marker)

    if not valid_markers:
        if skipped:
            return [_carto_group_warning_node(skipped, language=language)] + list(outline_nodes or [])
        return outline_nodes

    marker_by_line = sorted(valid_markers, key=lambda m: int(m.get("line") or 0))
    out: list[dict[str, Any]] = []
    node_events = []
    for idx, node in enumerate(outline_nodes or []):
        if not isinstance(node, dict):
            continue
        ln = node.get("orig_lineno_start_unused")
        if not isinstance(ln, int):
            # Keep warning/diagnostic/unpositioned nodes at file/script scope.
            out.append(node)
            continue
        node_events.append((int(ln), idx, node))
    node_events.sort(key=lambda x: (x[0], x[1]))

    current_group: dict[str, Any] | None = None
    marker_ix = 0

    def _emit_group(marker: dict[str, Any]) -> dict[str, Any]:
        line = int(marker.get("line") or 0)
        gid = str(marker.get("id") or "")
        label = str(marker.get("label") or gid)
        note = "\n".join([
            f"CARTOGRAPHER_VIRTUAL_GROUP: {language}",
            f"CARTO_GROUP_ID: {gid}",
            f"CARTO_GROUP_LABEL: {label}",
            f"CARTO_GROUP_MARKER_LINE: {line}",
            "---",
            "Synthetic Cartographer grouping node. Runtime source semantics unchanged.",
        ])
        group_node = {
            "name": f"📦 {label}",
            "note": note,
            "children": [],
            "orig_lineno_start_unused": line,
            "orig_lineno_end_unused": line,
            "priority": line * 100,
            "carto_virtual_type": "group",
            "carto_group_id": gid,
        }
        out.append(group_node)
        return group_node

    for node_line, _idx, node in node_events:
        while marker_ix < len(marker_by_line) and int(marker_by_line[marker_ix].get("line") or 0) <= node_line:
            current_group = _emit_group(marker_by_line[marker_ix])
            marker_ix += 1
        if current_group is None:
            out.append(node)
        else:
            current_group.setdefault("children", []).append(node)

    while marker_ix < len(marker_by_line):
        current_group = _emit_group(marker_by_line[marker_ix])
        marker_ix += 1

    if skipped:
        out.insert(0, _carto_group_warning_node(skipped, language=language))

    return out


def _carto_identity_key_for_node(node: dict[str, Any]) -> tuple[Any, ...] | None:
    """Return the primary stable Cartographer identity key for a node."""
    if not isinstance(node, dict):
        return None
    note = node.get("note") or ""

    b_id = _extract_beacon_id_from_note(note)
    if b_id:
        return ("beacon", b_id)

    qual = _extract_ast_qualname_from_note(note)
    if qual:
        return ("ast", qual)

    md_path = _extract_md_path_from_note(note)
    if md_path:
        key = tuple(whiten_text_for_header_compare(line) for line in md_path if line.strip())
        if key:
            return ("mdpath", key)

    vue_key = _extract_vue_section_key_from_note(note)
    if vue_key:
        return ("vue", vue_key)

    group_key = _extract_carto_group_key_from_note(note)
    if group_key:
        return ("carto_group", group_key)

    return None


def _tree_has_carto_virtual_group(node: dict[str, Any]) -> bool:
    """True when a tree contains at least one synthetic Cartographer group."""
    if not isinstance(node, dict):
        return False
    if _extract_carto_group_key_from_note(node.get("note") or ""):
        return True
    for ch in node.get("children") or []:
        if isinstance(ch, dict) and _tree_has_carto_virtual_group(ch):
            return True
    return False


def seed_cartographer_ids_by_identity_any_depth(
    source_root: dict[str, Any],
    ether_root: dict[str, Any],
) -> dict[str, Any]:
    """Seed Workflowy UUIDs onto source nodes by stable identity across all depths.

    This pre-pass makes virtual-group migrations safe: existing AST nodes can move
    under newly-created group wrappers while retaining their UUIDs. Ambiguous
    Ether nodes whose key is present in source are returned as preserve_ids so
    reconcile_tree's existing delete guard protects them and their ancestors.
    """
    source_by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    ether_by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = {}

    def _walk(n: dict[str, Any], acc: dict[tuple[Any, ...], list[dict[str, Any]]]) -> None:
        key = _carto_identity_key_for_node(n)
        if key:
            acc.setdefault(key, []).append(n)
        for ch in n.get("children") or []:
            if isinstance(ch, dict):
                _walk(ch, acc)

    _walk(source_root, source_by_key)
    _walk(ether_root, ether_by_key)

    seeded: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    preserve_ids: set[str] = set()

    for key, s_nodes in source_by_key.items():
        e_nodes = ether_by_key.get(key) or []
        if not e_nodes:
            continue
        if len(s_nodes) == 1 and len(e_nodes) == 1:
            e_id = e_nodes[0].get("id")
            if e_id and not s_nodes[0].get("id"):
                s_nodes[0]["id"] = str(e_id)
                seeded.append({"key": repr(key), "id": str(e_id), "name": s_nodes[0].get("name")})
            continue

        row = {
            "key": repr(key),
            "source_count": len(s_nodes),
            "ether_count": len(e_nodes),
            "ether_ids": [str(e.get("id")) for e in e_nodes if e.get("id")],
        }
        ambiguous.append(row)
        for e in e_nodes:
            e_id = e.get("id")
            if e_id:
                preserve_ids.add(str(e_id))

    return {
        "seeded": seeded,
        "ambiguous": ambiguous,
        "preserve_ids": sorted(preserve_ids),
        "seeded_count": len(seeded),
        "ambiguous_count": len(ambiguous),
        "preserve_count": len(preserve_ids),
    }
# @beacon-close[
#   id=carto-virtual-groups@identity-and-grouping,
# ]


# @beacon[
#   id=auto-beacon@reconcile_trees_cartographer-2hsq,
#   role=reconcile_trees_cartographer,
#   slice_labels=ra-snippet-range-ast-md,ra-reconcile,nexus-md-header-path,
#   kind=ast,
#   comment=Keeps unique-id refresh matching unchanged; shared ids resolve by unique composite or independent sibling anchors, never first-copy order, and retain unresolved ambiguity data without extending preservation.,
# ]
def reconcile_trees_cartographer(
    source_node: Dict[str, Any],
    ether_node: Dict[str, Any],
    *,
    _run_identity_seed: bool = True,
) -> None:
    """Reconcile a Cartographer-derived subtree against an existing ETHER tree.

    This variant is used for F12 / per-file Cartographer refresh. It prefers
    identity anchors over raw name matching:

      1. Beacon id (`id:` under BEACON (AST|SPAN)).
      2. AST_QUALNAME (Python AST nodes).
      3. MD_PATH header sequence for Markdown headings (each line whitened via
         ``whiten_text_for_header_compare``).
      4. Fallback: raw name equality.

    A shared sibling id is not an identity by itself. It resolves by a unique
    id+structural anchor, then unique independent structural/name evidence on
    BOTH sides. Unresolved copies remain unassigned, with candidates aggregated
    on the file root as ``_beacon_reconcile_report``; this data never extends
    the virtual-group seeder's ``preserve_ids`` or blocks another match.

    For nodes that are matched (i.e., share identity and thus an id), we then
    compare name and note using ``whiten_text_for_header_compare`` and, when
    they are semantically equal, we *restore* the ETHER value into the source
    tree. This lets the downstream WEAVE engine skip spurious updates caused
    purely by Workflowy whitening or formatting differences, while still
    propagating real changes (e.g. new tags or beacon metadata).
    """
    # Seed id on the root when present.
    if isinstance(ether_node, dict) and "id" in ether_node:
        source_node["id"] = ether_node["id"]

    # Virtual-group migration safety (Dan, 2026): before the normal sibling-
    # scoped recursive matcher runs, seed UUIDs by stable Cartographer identity
    # across the WHOLE file subtree. This lets an existing function move under a
    # newly-created synthetic group wrapper as a MOVE, not delete+create. The
    # report is attached to the source root; refresh_file_node_beacons threads
    # report['preserve_ids'] into reconcile_tree's explicitly_preserved_ids.
    if _run_identity_seed and (
        _tree_has_carto_virtual_group(source_node) or _tree_has_carto_virtual_group(ether_node)
    ):
        try:
            source_node["_carto_identity_seed_report"] = seed_cartographer_ids_by_identity_any_depth(
                source_node,
                ether_node,
            )
        except Exception as e:  # noqa: BLE001
            source_node["_carto_identity_seed_report"] = {
                "seeded": [],
                "ambiguous": [],
                "preserve_ids": [],
                "seeded_count": 0,
                "ambiguous_count": 0,
                "preserve_count": 0,
                "error": str(e),
            }

    source_children = [c for c in (source_node.get("children") or []) if isinstance(c, dict)]
    ether_children = [c for c in (ether_node.get("children") or []) if isinstance(c, dict)]

    if not source_children or not ether_children:
        return

    # Build Ether maps by identity anchor.
    ether_by_beacon: dict[str, list[dict[str, Any]]] = {}
    ether_by_ast: dict[str, list[dict[str, Any]]] = {}
    ether_by_mdpath: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    ether_by_vuekey: dict[str, dict[str, Any]] = {}
    ether_by_carto_group: dict[str, dict[str, Any]] = {}
    ether_by_name: dict[str, list[dict[str, Any]]] = {}
    # Anchor 5 (Dan, post-2026-04-30): tag-stripped raw name index for
    # tag-tolerant matching. Keyed by base name with trailing '#tag' tokens
    # and ' 🔱' decoration removed. This allows freshly-ETCHed Workflowy
    # nodes named e.g. 'My new section #aal--design' to match the on-disk
    # 'My new section' that nexus_to_tokens emits (which strips trailing
    # tags from heading text).
    ether_by_name_stripped: dict[str, list[dict[str, Any]]] = {}
    matched_ether_obj_ids: set[int] = set()

    def _strip_tags_and_trident(raw: str) -> str:
        """Return the raw name with the trailing #tag + 🔱 decoration region removed.

        DELEGATES to the canonical emoji/trident-aware
        :func:`split_name_and_tags` (returns just its ``base``) so the reconcile
        base-name comparison uses the EXACT same trailing-region definition as
        every tag tool / anchor path. This is the fix for the piled-up-trident
        bug: previously this stripper handled trident-AFTER-tags while the
        canonical splitter left trident-BEFORE-tags glued to the base, so the
        two disagreed on where the base ended and the merge writer kept
        re-adding a trident onto a base that still carried one. One splitter =>
        they can never disagree again.

        A glued ``base🔱`` (trident with NO separating whitespace) is not a
        separate token, so canonical leaves it on the base; we peel that
        residual glued trident here to preserve this helper's prior
        byte-for-byte base-name contract with the on-disk heading text.
        """
        if not isinstance(raw, str):
            return ""
        base, _tags = split_name_and_tags(raw)
        # Peel a residual glued trident (no whitespace before it), which the
        # whitespace-tokenizing splitter cannot see as its own token.
        while base.endswith("🔱"):
            base = base[:-1].rstrip()
        return base

    def _candidate_if_unmatched(candidate: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(candidate, dict):
            return None
        if id(candidate) in matched_ether_obj_ids:
            return None
        return candidate

    def _first_unmatched(candidates: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        for candidate in candidates or []:
            if id(candidate) in matched_ether_obj_ids:
                continue
            return candidate
        return None

    def _sibling_keys(node: dict[str, Any]) -> list[tuple[Any, ...]]:
        note = node.get("note") or ""
        keys = []
        qual = _extract_ast_qualname_from_note(note)
        if qual:
            keys.append(("ast", qual))
        path = _extract_md_path_from_note(note)
        if path:
            key = tuple(whiten_text_for_header_compare(line) for line in path if line.strip())
            if key:
                keys.append(("md", key))
        for kind, value in (("vue", _extract_vue_section_key_from_note(note)),
                            ("group", _extract_carto_group_key_from_note(note))):
            if value:
                keys.append((kind, value))
        name = node.get("name") or ""
        if isinstance(name, str):
            keys.append(("name", name))
            stripped = _strip_tags_and_trident(name)
            if stripped:
                keys.append(("base_name", stripped))
        return keys

    source_by_beacon: dict[str, list[dict[str, Any]]] = {}
    source_by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    ether_by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for siblings, by_key in ((source_children, source_by_key), (ether_children, ether_by_key)):
        for child in siblings:
            for key in _sibling_keys(child):
                by_key.setdefault(key, []).append(child)
    for child in source_children:
        bid = _extract_beacon_id_from_note(child.get("note") or "")
        if bid:
            source_by_beacon.setdefault(bid, []).append(child)
    ambiguities = []

    def _shared_beacon_match(source: dict[str, Any], bid: str) -> dict[str, Any] | None:
        keys = _sibling_keys(source)
        # Count the complete sibling observations, not just unmatched leftovers:
        # consuming one twin cannot make the next twin uniquely identifiable.
        for key in keys:
            if key[0] not in {"ast", "md", "vue"}:
                continue
            sources = [n for n in source_by_key[key] if n in source_by_beacon[bid]]
            ethers = [n for n in ether_by_key.get(key, []) if n in ether_by_beacon.get(bid, [])]
            if len(sources) == len(ethers) == 1:
                candidate = _candidate_if_unmatched(ethers[0])
                if candidate is not None:
                    return candidate
        for key in keys:
            sources, ethers = source_by_key[key], ether_by_key.get(key, [])
            if len(sources) == len(ethers) == 1:
                candidate = _candidate_if_unmatched(ethers[0])
                if candidate is not None:
                    return candidate
        candidates = ether_by_beacon.get(bid, [])
        ambiguities.append({
            "beacon_id": bid, "source_name": source.get("name"),
            "source_names": [n.get("name") for n in source_by_beacon[bid]],
            "ether_ids": [n.get("id") for n in candidates if n.get("id")],
            "candidates": [{"id": n.get("id"), "name": n.get("name"),
                            "anchors": _sibling_keys(n)} for n in candidates],
        })
        return None

    # CARTO-DEBUG targeting is disabled by default; set this to a real
    # function name when focused instrumentation is needed.
    TARGET_FUNC = "__CARTO_DEBUG_TARGET_UNUSED__"
    import sys as _carto_debug_sys

    for e in ether_children:
        note_e = e.get("note") or ""
        name_e = e.get("name") or ""
        # 1) Beacon id
        b_id = _extract_beacon_id_from_note(note_e)
        if b_id:
            ether_by_beacon.setdefault(b_id, []).append(e)
        # 2) AST_QUALNAME
        qual = _extract_ast_qualname_from_note(note_e)
        if qual:
            ether_by_ast.setdefault(qual, []).append(e)
        # 3) MD_PATH
        md_path = _extract_md_path_from_note(note_e)
        if md_path:
            key = tuple(whiten_text_for_header_compare(line) for line in md_path if line.strip())
            if key:
                ether_by_mdpath.setdefault(key, []).append(e)
        # 3.5) Vue section key
        vue_key = _extract_vue_section_key_from_note(note_e)
        if vue_key and vue_key not in ether_by_vuekey:
            ether_by_vuekey[vue_key] = e
        # 3.6) Cartographer virtual group key
        carto_group_key = _extract_carto_group_key_from_note(note_e)
        if carto_group_key and carto_group_key not in ether_by_carto_group:
            ether_by_carto_group[carto_group_key] = e
        # 4) Raw name fallback
        if isinstance(name_e, str):
            ether_by_name.setdefault(name_e, []).append(e)
            # 5) Tag-stripped raw name fallback (Dan, post-2026-04-30).
            stripped_e = _strip_tags_and_trident(name_e)
            if stripped_e and stripped_e != name_e:
                ether_by_name_stripped.setdefault(stripped_e, []).append(e)

        # Debug: log any ETHER child that looks like our target function
        if (
            isinstance(name_e, str)
            and TARGET_FUNC in name_e
        ) or (
            isinstance(qual, str)
            and qual == TARGET_FUNC
        ):
            print(
                "[CARTO-DEBUG] ether child candidate for TARGET_FUNC:",
                {"name": name_e, "id": e.get("id"), "qual": qual},
                file=_carto_debug_sys.stderr,
                flush=True,
            )

    for s in source_children:
        note_s = s.get("note") or ""
        name_s = s.get("name") or ""
        match: dict[str, Any] | None = None

        b_id_s = _extract_beacon_id_from_note(note_s)
        qual_s = _extract_ast_qualname_from_note(note_s)
        debug_hit = (
            (isinstance(name_s, str) and TARGET_FUNC in name_s)
            or (isinstance(qual_s, str) and qual_s == TARGET_FUNC)
        )

        if debug_hit:
            print("[CARTO-DEBUG] SOURCE child before matching:", file=_carto_debug_sys.stderr, flush=True)
            print(
                "[CARTO-DEBUG]   name_s=", repr(name_s),
                "qual_s=", repr(qual_s),
                "b_id_s=", repr(b_id_s),
                file=_carto_debug_sys.stderr,
                flush=True,
            )
            print(
                "[CARTO-DEBUG]   ether_by_ast keys=",
                list(ether_by_ast.keys()),
                file=_carto_debug_sys.stderr,
                flush=True,
            )
            print(
                "[CARTO-DEBUG]   ether_by_beacon keys=",
                list(ether_by_beacon.keys()),
                file=_carto_debug_sys.stderr,
                flush=True,
            )

        # 1) Beacon id
        if b_id_s and (len(source_by_beacon[b_id_s]) > 1 or len(ether_by_beacon.get(b_id_s, [])) > 1):
            match = _shared_beacon_match(s, b_id_s)
        elif b_id_s and b_id_s in ether_by_beacon:
            match = _candidate_if_unmatched(ether_by_beacon[b_id_s][0])
            if debug_hit and match is not None:
                print(
                    "[CARTO-DEBUG]   MATCH via BEACON id:",
                    {"b_id_s": b_id_s, "match_id": match.get("id")},
                    file=_carto_debug_sys.stderr,
                    flush=True,
                )
        else:
            # 2) AST_QUALNAME
            if qual_s and qual_s in ether_by_ast:
                match = _first_unmatched(ether_by_ast.get(qual_s))
                if debug_hit and match is not None:
                    print(
                        "[CARTO-DEBUG]   MATCH via AST_QUALNAME:",
                        {"qual_s": qual_s, "match_id": match.get("id")},
                        file=_carto_debug_sys.stderr,
                        flush=True,
                    )
            else:
                # 3) MD_PATH (whitened per-line comparison)
                md_path_s = _extract_md_path_from_note(note_s)
                if md_path_s:
                    key_s = tuple(
                        whiten_text_for_header_compare(line) for line in md_path_s if line.strip()
                    )
                    if key_s:
                        match = _first_unmatched(ether_by_mdpath.get(key_s))
                        if debug_hit and match is not None:
                            print(
                                "[CARTO-DEBUG]   MATCH via MD_PATH:",
                                {"key_s": key_s, "match_id": match.get("id")},
                                file=_carto_debug_sys.stderr,
                                flush=True,
                            )
                # 3.5) Vue section key
                if match is None:
                    vue_key_s = _extract_vue_section_key_from_note(note_s)
                    if vue_key_s and vue_key_s in ether_by_vuekey:
                        match = _candidate_if_unmatched(ether_by_vuekey[vue_key_s])
                        if debug_hit and match is not None:
                            print(
                                "[CARTO-DEBUG]   MATCH via VUE_SECTION key:",
                                {"vue_key_s": vue_key_s, "match_id": match.get("id")},
                                file=_carto_debug_sys.stderr,
                                flush=True,
                            )

                # 3.6) Cartographer virtual group key
                if match is None:
                    carto_group_key_s = _extract_carto_group_key_from_note(note_s)
                    if carto_group_key_s and carto_group_key_s in ether_by_carto_group:
                        match = _candidate_if_unmatched(ether_by_carto_group[carto_group_key_s])
                        if debug_hit and match is not None:
                            print(
                                "[CARTO-DEBUG]   MATCH via CARTO_GROUP_ID:",
                                {"carto_group_key_s": carto_group_key_s, "match_id": match.get("id")},
                                file=_carto_debug_sys.stderr,
                                flush=True,
                            )

                # 4) Raw name fallback
                if match is None and isinstance(name_s, str) and name_s in ether_by_name:
                    match = _first_unmatched(ether_by_name.get(name_s))
                    if debug_hit and match is not None:
                        print(
                            "[CARTO-DEBUG]   MATCH via raw name:",
                            {"name_s": name_s, "match_id": match.get("id")},
                            file=_carto_debug_sys.stderr,
                            flush=True,
                        )

                # 5) Tag-tolerant raw-name fallback (Dan, post-2026-04-30).
                # Handles freshly-ETCHed Workflowy nodes with trailing #tags
                # whose corresponding heading on disk has the tags stripped
                # by nexus_to_tokens. Without this, the entire tagged
                # subtree would be DELETE+CREATE'd by reconcile_tree (loss
                # of tag, UUID, and recursive subtree wipe).
                #
                # Strategy: strip trailing '#tag' tokens and '🔱' from BOTH
                # the source name and ether names; match if the bases agree.
                # When matched, we additionally preserve the ETHER name on
                # the source so the downstream WEAVE engine does not
                # UPDATE the Workflowy node to remove the tags.
                if match is None and isinstance(name_s, str):
                    stripped_s = _strip_tags_and_trident(name_s)
                    if stripped_s:
                        # Try direct lookup against pre-built tag-stripped index.
                        candidate = _first_unmatched(
                            ether_by_name_stripped.get(stripped_s)
                        )
                        # If no hit, try the case where the SOURCE was already
                        # bare and an ether node has the same bare name in
                        # ether_by_name (covers stripped_s == name_e).
                        if candidate is None and stripped_s in ether_by_name:
                            candidate = _first_unmatched(
                                ether_by_name.get(stripped_s)
                            )
                        if candidate is not None:
                            match = candidate
                            if debug_hit:
                                print(
                                    "[CARTO-DEBUG]   MATCH via tag-tolerant name:",
                                    {
                                        "name_s": name_s,
                                        "stripped_s": stripped_s,
                                        "match_id": match.get("id"),
                                        "match_name": match.get("name"),
                                    },
                                    file=_carto_debug_sys.stderr,
                                    flush=True,
                                )

        if match is None:
            if debug_hit:
                print("[CARTO-DEBUG]   NO MATCH for source child", file=_carto_debug_sys.stderr, flush=True)
            # No identity match at this level; we do not attempt to force ids.
            continue

        matched_ether_obj_ids.add(id(match))

        # Seed id from the matched Ether node.
        if "id" in match:
            s["id"] = match["id"]
            if debug_hit:
                print(
                    "[CARTO-DEBUG]   Seeded id from match:",
                    {"seed_id": s["id"]},
                    file=_carto_debug_sys.stderr,
                    flush=True,
                )

        # Whiten-based no-op suppression: if name/note are semantically equal
        # after whitening, preserve the ETHER values so WEAVE sees no change.
        name_e = match.get("name")
        if isinstance(name_s, str) and isinstance(name_e, str):
            if whiten_text_for_header_compare(name_s) == whiten_text_for_header_compare(name_e):
                # Preserve Workflowy/Ether's canonical name when the disk and
                # Ether names are semantically equal, but remove stale
                # transport/readability entities that should not persist in
                # Workflowy NAME text (notably legacy &quot; fossils). This keeps
                # the original no-op-suppression policy while healing escaped
                # names on the next F12+1. The helper is deliberately narrow:
                # it decodes &amp;/&lt;/&gt;/&quot; plus numeric double-quote spellings,
                # but does NOT full-html-unescape source entities like &nbsp;.
                #
                # ALSO heal Workflowy's presentation-only <u>...</u> tag
                # underline markup here (Dan, 2026-06). Workflowy underlines
                # rendered tags and can surface that styling in the NAME via
                # /nodes-export (e.g. '#<u>nexus--curation-🏷️</u>'). For an
                # EXISTING tagged node, whiten_text_for_header_compare strips
                # <u>/</u> (and the emoji), so name_s and name_e compare EQUAL
                # and this no-op-suppression branch is taken -- which would
                # otherwise restore the <u>-laden ether name verbatim and make
                # the underline markup persist forever. (A newly-created beacon
                # takes the disk-as-truth else-branch instead, which is why it
                # was already clean.) Stripping it here closes that asymmetry so
                # the existing-tag update path renders the tag cleanly, exactly
                # like the create path.
                healed_name_e = decode_workflowy_transport_entities_for_markdown_source(name_e)
                s["name"] = _strip_workflowy_tag_underline_markup(healed_name_e)
            else:
                # Disk-as-truth name reconciliation (Dan, post-2026-05).
                #
                # When source and ether match by an identity anchor (beacon
                # id, AST_QUALNAME, MD_PATH, vue key, raw name, or tag-stripped
                # name) but their full names differ AFTER stripping trailing
                # '#tag' tokens and '🔱' decoration, we rebuild the final
                # Workflowy name purely from disk-derived information. We do
                # NOT consult Workflowy / cache metadata to decide which tags
                # to keep or drop, and we do NOT use the literal '🔱'
                # character as a behavioral signal (an agent could write a
                # stray trident into source text; that must not be treated
                # as legitimate beacon state).
                #
                # Rebuild rule (when stripped bases are equal):
                #
                #   final.base   = stripped_s (= stripped_e by branch guard)
                #   final.tags   = right-edge '#tag' tokens of name_s, but
                #                  ONLY when the source/disk node note
                #                  carries a freshly-parsed BEACON block
                #                  (b_id_s is non-empty). Otherwise the
                #                  managed-system rule applies: any trailing
                #                  '#tag' tokens on the heading text without
                #                  a backing beacon block are illegitimate
                #                  decoration and are stripped.
                #   final.trident = present iff final.tags is non-empty.
                #                   Trident is a purely cosmetic UI marker
                #                   that mirrors "this node has tags from a
                #                   real disk-side beacon". It never controls
                #                   any other behavior.
                #
                # Consequences (intentional under Dan's F12+1 = pull-from-disk
                # policy):
                #
                # - Agent removed beacon block from disk → name_s has no
                #   trailing tags and b_id_s is empty → ether-side trailing
                #   tags + trident are dropped on the next F12+1.
                # - Agent changed slice_labels on disk → name_s carries the
                #   new tag set → ether-side stale tags are replaced wholesale
                #   with the new disk tags.
                # - User added a free-floating '#tag' to a heading in
                #   Workflowy without a backing disk beacon → that tag is
                #   stripped on F12+1. Per Dan's managed-system principle:
                #   under F12 there are no legitimate free-floating tags.
                # - User edited tags in Workflowy and ran F12+1 instead of
                #   F12+2 first → user's Workflowy edits are wiped. This is
                #   user error; F12+2/F12+3 are the push-to-disk paths.
                #
                # This replaces the previous union/superset merge policy that
                # tried to preserve ether-side trailing tags. That policy was
                # symmetric between source and ether for tag tokens, which
                # made the BEACON-removed case impossible to express: ether's
                # stale beacon-derived tags survived forever even after the
                # disk beacon was deleted. Disk-as-truth is the correct model
                # for F12+1.
                stripped_s = _strip_tags_and_trident(name_s)
                stripped_e = _strip_tags_and_trident(name_e)
                if stripped_s and stripped_s == stripped_e:
                    def _trailing_hash_tags(raw: str) -> list[str]:
                        """Return ONLY the trailing-region '#tag' tokens of raw.

                        DELEGATES to the canonical emoji/trident-aware
                        :func:`split_name_and_tags` (returns just its ``tags``)
                        so the disk tags this preserves are computed with the
                        SAME trailing-region definition the base strip uses --
                        trident/marker decoration interleaved with the tags is
                        transparent, and embedded mid-name '#AI'-style tokens
                        are still NOT picked up. Prevents the writer below from
                        splitting on a different tag boundary than the strip.
                        """
                        if not isinstance(raw, str):
                            return []
                        _base, trailing = split_name_and_tags(raw)
                        return trailing

                    source_has_beacon = bool(b_id_s)
                    disk_tags = _trailing_hash_tags(name_s) if source_has_beacon else []

                    merged_parts: list[str] = [stripped_s]
                    if disk_tags:
                        merged_parts.append("🔱")
                    merged_parts.extend(disk_tags)
                    s["name"] = " ".join(merged_parts)
        note_e = match.get("note")
        if isinstance(note_s, str) and isinstance(note_e, str):
            # NOTE no-op suppression uses the whitespace-PRESERVING whitener
            # (whiten_note_for_compare) rather than the header/name whitener.
            # A slurped comment/docstring is content: a whitespace-only edit on
            # disk (e.g. '#nexus' -> '# nexus') is a real change F12+1 must
            # propagate. The header whitener collapses all whitespace and would
            # declare the note unchanged, silently restoring the stale ether
            # note and emitting no update. See whiten_note_for_compare.
            if whiten_note_for_compare(note_s) == whiten_note_for_compare(note_e):
                s["note"] = note_e

        if debug_hit:
            print(
                "[CARTO-DEBUG]   FINAL source child after matching:",
                {"name": s.get("name"), "id": s.get("id"), "note": s.get("note")},
                file=_carto_debug_sys.stderr,
                flush=True,
            )

        # Recurse into children with the same Cartographer-aware logic.
        reconcile_trees_cartographer(s, match, _run_identity_seed=False)

        ambiguities.extend((s.get("_beacon_reconcile_report") or {}).get("ambiguous", []))

    if ambiguities:
        source_node["_beacon_reconcile_report"] = {"ambiguous": ambiguities, "ambiguous_count": len(ambiguities)}


# @beacon[
#   id=auto-beacon@_slice_label_tags-vxd4,
#   role=ƒ _slice_label_tags(slice_labels: str),
#   slice_labels=ra-reconcile,carto-fwd-source,nexus--tags,
#   kind=ast,
# ]
def _slice_label_tags(slice_labels: str) -> str:
    """Convert a slice_labels string into a space-delimited #tag list.

    Examples:
        "DOC"        -> "#DOC"
        "FP,DA"      -> "#FP #DA"
        "doc-intro"  -> "#doc-intro"

    We split on commas and whitespace to support "FP, DA" or "FP DA" forms.
    """
    if not isinstance(slice_labels, str):
        return ""
    labels = []
    for part in re.split(r"[\s,]+", slice_labels):
        part = part.strip()
        if not part:
            continue
        labels.append(f"#{part}")
    return " ".join(labels)


# @beacon[
#   id=auto-beacon@_strip_workflowy_tag_underline_markup,
#   role=ƒ _strip_workflowy_tag_underline_markup(text: str),
#   slice_labels=ra-reconcile,carto-fwd-source,nexus--tags,
#   kind=ast,
#   comment=Strip Workflowy presentation-only underline markup from tag tokens. Workflowy may expose rendered tags through /nodes-export as #<u>tag</u> or #tag<u>-emoji</u>; those <u> wrappers are visual styling, not tag content. Also removes the interim malformed <-u> artifact created by the emoji-preserving sanitizer before this fix.,
# ]
def _strip_workflowy_tag_underline_markup(text: str) -> str:
    """Remove Workflowy's presentation-only ``<u>`` wrappers from tag text.

    Workflowy visually underlines tags and can expose that styling through
    /nodes-export as literal or entity-encoded ``<u>...</u>`` fragments
    inside the NAME field. For slice labels, this markup is never semantic:
    the tag ``#nexus--curation-🏷️`` must not become
    ``#<u>nexus--curation-🏷️</u>`` on disk.

    We also strip the malformed ``<-u>`` close marker produced by the brief
    interim state where the emoji-preserving sanitizer converted ``/`` to
    ``-`` but left angle brackets intact.

    ⚖️ TWIN / NO-DRIFT CONTRACT: this is the CANONICAL tag-markup stripper,
    used by ``split_name_and_tags`` (-> ``list_anchors`` / ``update_anchor`` /
    the add/remove/rename_tag tools). There is a deliberate DUPLICATE,
    ``_strip_tag_markup`` in ``f8_slice_policy.py`` (feeding ``extract_name_
    tags`` -> F8 / cache slice-group membership). That module is intentionally
    import-free / transport-free, so it cannot import this function; the two
    copies are TWINNED on purpose. The f8 copy is a strict SUPERSET (same
    ``<u>``/``</u>`` + ``<-u>`` handling, plus broader tag + entity stripping),
    so the two can never DISAGREE on a node's tags. If you change EITHER,
    update BOTH. A proper consolidation (one shared dependency-free primitive)
    is logged as a TODO; until then, this comment is the guard.
    """
    if not isinstance(text, str):
        return ""
    s = text
    # Catch entity-encoded forms before generic dewhitening and literal forms
    # after it. Keep this deliberately narrow: only Workflowy's underline tag
    # styling is stripped here, not arbitrary user text.
    s = re.sub(r"(?i)&lt;\s*/?\s*u\s*&gt;", "", s)
    s = re.sub(r"(?i)<\s*/?\s*u\s*>", "", s)
    s = re.sub(r"(?i)&lt;\s*-\s*u\s*&gt;", "", s)
    s = re.sub(r"(?i)<\s*-\s*u\s*>", "", s)
    return s


# @beacon[
#   id=auto-beacon@_sanitize_slice_label_token,
#   role=ƒ _sanitize_slice_label_token(part: str),
#   slice_labels=ra-reconcile,carto-fwd-source,nexus--tags,agent-kit-vendor,
#   kind=ast,
#   comment=Per-token slice-label sanitizer. Converts STRUCTURAL separators/punctuation (whitespace, : . / \ , # |) to hyphens but PRESERVES Unicode letters and emoji, because Workflowy tags fully support emoji (e.g. #nexus--curation-🏷️). Also strips Workflowy's presentation-only <u> tag-underlining wrappers when they leak through /nodes-export. The previous ASCII-only allow-list [^A-Za-z0-9_-] silently ate every emoji/Unicode codepoint, which mangled or deleted emoji-bearing tags on F12+2/F12+3 write and the following F12+1 reconcile. Single source of truth shared by _canonicalize_slice_labels and _canonicalize_slice_labels_union.,
# ]
def _sanitize_slice_label_token(part: str) -> str:
    """Sanitize ONE slice-label token into a Workflowy-tag-safe form.

    Policy (Dan, 2026): a Workflowy tag may contain Unicode letters and
    emoji, but NOT whitespace or the structural punctuation we use as
    separators. So we convert only genuinely-problematic separator/
    punctuation characters to a single '-' and PRESERVE everything else
    (Unicode letters, digits, '_', '-', emoji, combining marks).

    This replaces the legacy ASCII-only allow-list ``[^A-Za-z0-9_-]``,
    which mapped every non-ASCII codepoint (i.e. every emoji) to '-' and
    thereby silently destroyed emoji-bearing tags such as
    ``#nexus--curation-🏷️``. It also strips Workflowy's presentation-only
    ``<u>`` wrappers when they leak into tag text via /nodes-export.
    """
    if not isinstance(part, str):
        return ""
    # Strip control characters outright (they have no place in a tag).
    sanitized = re.sub(r"[\x00-\x1f\x7f]+", "", part)
    sanitized = _strip_workflowy_tag_underline_markup(sanitized)
    # Convert structural separators/punctuation to a single hyphen. This
    # keeps the historical ':' '.' '/' space behavior (model:mert ->
    # model-mert, Class.method -> Class-method) while leaving Unicode
    # letters and emoji untouched.
    sanitized = re.sub(r"[\s/\\:.,#|]+", "-", sanitized)
    return sanitized.strip("-")


# @beacon[
#   id=auto-beacon@_canonicalize_slice_labels-zpcy,
#   role=ƒ _canonicalize_slice_labels(slice_labels, role),
#   slice_labels=ra-reconcile,carto-fwd-source,nexus--tags,agent-kit-vendor,
#   kind=ast,
#   comment=Canonicalize Unicode label tokens and deduplicate after sanitization in first-occurrence order; exact-token delta semantics remain separate from rendering families.,
# ]
def _canonicalize_slice_labels(slice_labels: str | None, role: str | None) -> str:
    """Canonicalize slice_labels for tags and display.

    - If slice_labels is empty and role is provided, derive labels from role.
    - Replaces ':', '.', and other structural separator characters with '-'
      to ensure Workflowy tags render correctly (e.g. model:mert:production →
      model-mert-production, Class.method → Class-method).
    - PRESERVES Unicode letters and emoji (Workflowy tags support emoji, e.g.
      #nexus--curation-🏷️). See _sanitize_slice_label_token.
    - Accepts comma/whitespace separated labels, normalizes and deduplicates
      canonical tokens, keeping their first occurrence.
    """
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
        # Apply same sanitization to role-derived labels.
        sanitized = _sanitize_slice_label_token(base_role)
        if sanitized:
            tokens.append(sanitized)

    return ",".join(tokens)


# @beacon[
#   id=carto-js-ts@_coerce_boolish-jb5p,
#   role=ƒ _coerce_boolish(val),
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,nexus-md-header-path,carto-fwd-source,
#   kind=ast,
# ]
def _coerce_boolish(val: Any | None) -> Optional[bool]:
    """Coerce common Workflowy/beacon boolean spellings to a bool.

    Returns:
      - True/False when val looks like a boolean
      - None when val is missing/empty/unknown

    Accepted false values (case-insensitive): false, 0, no, off
    Accepted true values  (case-insensitive): true, 1, yes, on
    """
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


# @beacon[
#   id=carto-js-ts@_parse_show_span_fields-5i6q,
#   role=ƒ _parse_show_span_fields(fields),
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,nexus-md-header-path,carto-fwd-source,
#   kind=ast,
# ]
def _parse_show_span_fields(fields: dict[str, str]) -> tuple[bool, bool]:
    """Return (show_span, show_span_explicit) from parsed beacon fields."""
    raw = fields.get("show_span")
    if raw is None:
        return True, False
    b = _coerce_boolish(raw)
    if b is None:
        # Fail-open: if malformed, behave like legacy (show span) but keep
        # explicitness so it round-trips and is visible to humans.
        return True, True
    return b, True


# @beacon[
#   id=carto-js-ts@_beacon_show_span-8v9u,
#   role=ƒ _beacon_show_span(beacon),
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
#   kind=ast,
# ]
def _beacon_show_span(beacon: dict[str, Any]) -> tuple[bool, bool]:
    """Return (show_span, explicit) for a parsed beacon dict."""
    explicit = bool(beacon.get("show_span_explicit"))
    raw = beacon.get("show_span")
    if isinstance(raw, bool):
        return raw, explicit
    b = _coerce_boolish(raw)
    if b is None:
        return True, explicit
    return b, True


# @beacon[
#   id=carto-js-ts@_extract_show_span_from_note-1q2w,
#   role=ƒ _extract_show_span_from_note(note),
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
#   kind=ast,
#   show_span=false,
#   comment=Project an unambiguous show_span value through the shared scoped header reader; span/context/docstring lines never override metadata.,
# ]
def _extract_show_span_from_note(note: str | None) -> Optional[bool]:
    """Project an unambiguous scoped header boolean, never span/docstring text."""
    values = {v["value"] for r in parse_beacon_note_metadata(note)
              for v in r["occurrences"] if v["key"] == "show_span"}
    return _coerce_boolish(next(iter(values))) if len(values) == 1 else None


# @beacon[
#   id=carto-js-ts@_strip_beacon_meta_blocks_from_context-m8k3,
#   role=ƒ _strip_beacon_meta_blocks_from_context(context_lines: list[str]),
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,nexus-md-header-path,carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Legacy normalized-context adapter delegates metadata geometry to the shared bounded scanner; preserves unowned prose after malformed blocks and filters virtual-group directives without a private end rule.,
# ]
def _strip_beacon_meta_blocks_from_context(context_lines: list[str]) -> list[str]:
    """Strip a legacy, already-normalized context view through the one scanner.

    Structured callers use their original source records instead. Unprefixed
    prose in this view is not source-comment evidence and is never swallowed
    by an incomplete metadata block.
    """
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
    """Retain the existing virtual-group context policy, not beacon grammar."""
    result = list(cleaned)
    for index, norm in enumerate(cleaned):
        if norm.startswith("@carto-group"):
            result[index] = ""
    return result


# @beacon[
#   id=beacon-source@_beacon_context_view,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Builds comment context from supplied structural records and shared lexical comment evidence; subtracts recorded metadata ranges without reinterpreting delimiters or ids and keeps mixed-wrapper prose and malformed-tail context.,
# ]
def _beacon_context_view(lines: list[str], language: str, records=None):
    """Return records, original comment mask and metadata-free comment bodies."""
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
    """Collect neighbouring comments; only a recorded pair can stop a slurp."""
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


# @beacon-close[
#   id=nmcv1@portable-note-and-identity-core,
# ]
# CORE V1 KEEPER:
# This cluster is the parser and beacon engine proper: Python/JS/TS/Vue outline
# builders, beacon parsing, duplicate-beacon hygiene, comment slurping, and AST/
# span attachment logic. It is core functionality, not optional decoration.
# @beacon[
#   id=nmcv1@parser-and-outline-core,
#   role=core keeper: parser, outline, and beacon-attachment engine,
#   slice_labels=nexus-portability,nexus-core-v1,
#   kind=span,
#   show_span=false,
# ]
# @beacon[
#   id=carto-js-ts@parse_python_beacon_blocks,
#   role=carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,
#   kind=span,
# ]
# Phase 2 JS/TS: Python beacon parser template.
# Reference for parse_js_beacon_blocks(...) (JS/TS @beacon[...] parser)
# so JS/TS beacon metadata matches the existing schema (id, role,
# slice_labels, kind, start_snippet, end_snippet, comment, comment_line).
# @beacon[
#   id=auto-beacon@parse_python_beacon_blocks-rqao,
#   role=parse_python_beacon_blocks,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Python compatibility projection of bounded structural records and exact span pairs; incomplete own fields remain readable and diagnostics never change returned values.,
# ]
def parse_python_beacon_blocks(
    lines: list[str], *, diagnostics: list | None = None
) -> list[dict[str, Any]]:
    """Read Python comment metadata through the shared structural authority."""
    return _parse_structural_beacons("python", lines, diagnostics)


# ---------------------------------------------------------------------------
# Duplicate AST beacon normalization (hygiene)
# ---------------------------------------------------------------------------

# @beacon[
#   id=auto-beacon@_split_csvish_tokens-x60r,
#   role=_split_csvish_tokens,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _split_csvish_tokens(raw: str | None) -> list[str]:
    if not isinstance(raw, str):
        return []
    parts = re.split(r"[\s,]+", raw.strip())
    return [p.strip() for p in parts if p and p.strip()]


# @beacon[
#   id=auto-beacon@_merge_unique_strings-mso4,
#   role=_merge_unique_strings,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
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


# @beacon[
#   id=auto-beacon@_canonicalize_slice_labels_union-up6e,
#   role=_canonicalize_slice_labels_union,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _canonicalize_slice_labels_union(labels: list[str]) -> str:
    # Use the same per-token sanitization as _canonicalize_slice_labels, but
    # do NOT derive from role (we're unioning explicit tokens).
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
    """Project scanner/attachment evidence without losing aliases or raw fields."""
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
    """Compare shared semantic fields and opaque evidence; never parse a line."""
    conflicts = []
    records = [b["record"] for b in group]
    for key in ("role", "start_snippet", "end_snippet", "show_span"):
        values = [r["fields"].get(key) for r in records]
        if key == "show_span":
            # The display parser intentionally defaults invalid values to true;
            # that is not permission to discard distinct opaque source values.
            values = [(_coerce_boolish(v) if _coerce_boolish(v) is not None else ("opaque", v))
                      if v not in (None, "") else None for v in values]
        nonempty = {v for v in values if v not in (None, "")}
        # Only labels/comments are rewritten. A value present solely in a
        # deleted member would be lost even if no second value contradicted it.
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


# @beacon[
#   id=beacon-hygiene@plan_beacon_hygiene,
#   slice_labels=beacon-writeback,carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Pure same-snapshot hygiene verdicts shared by check and kit: groups explicit AST records by physical attachment and coalesces only same-id compatible groups at their lowest member with field splices; local choices retain every witness and no publication occurs here.,
# ]
def plan_beacon_hygiene(snapshot: SourceSnapshot, structures: dict, attachment_index: dict) -> dict:
    """Return shared check/kit edits and group verdicts, with no IO or execution.

    This consumes U01 geometry and U03 attachment, not tuple projections or a
    private next-line scan. Incomplete records stay choices (including their
    scanner-supplied delimiter candidates); no destructive extent is invented.
    A survivor is never rebuilt. Every candidate is proved through U02 before
    adding it to the file plan, so a wrong after-state costs only that group.
    """
    language = structures["language"]
    groups = {}
    diagnostics = [{"owner": r["opener_range"][0], **d} for r in structures["records"]
                   for d in r["diagnostics"]]
    for block in attachment_index["blocks"]:
        record = block["record"]
        if (record["fields"].get("kind") or "").strip().lower() != "ast":
            continue  # explicit/implicit spans and implicit ASTs are not operands
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
        ids = [r["fields"]["id"] for r in records]  # already normalized by THE field parser
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
            # Validate existing parsed content without applying ingress/name
            # decoration normalization to bytes this merge must preserve.
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
        # Delete exactly the scanner's owned metadata, including a dedicated
        # wrapper only when it owns it. Mixed comments keep their ordinary text.
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


# @beacon[
#   id=beacon-hygiene@_normalize_duplicate_ast_beacons,
#   slice_labels=beacon-writeback,carto-fwd-source,
#   kind=ast,
#   comment=One optional hygiene IO adapter builds shared section and attachment inputs from a snapshot and publishes the pure planner edits in one U02 commit; returns truthful committed or skipped group evidence without gating the map.,
# ]
def _normalize_duplicate_ast_beacons(file_path: str, language: str) -> dict:
    """One IO adapter: observe, project shared inputs, plan, publish once."""
    snapshot = read_source_snapshot(file_path)
    if language == "vue":
        pairs = _vue_section_records(snapshot)
        records = [r for _section, rs in pairs for r in rs]
        sections = [s for s, _records in pairs]
    else:
        records, sections = _beacon_edit_records(snapshot, language), None
    structures = {"language": language, "records": records, "sections": sections}
    index = beacon_attachment_index(snapshot, _beacon_attachment_outline(snapshot, language), structures)
    planned = plan_beacon_hygiene(snapshot, structures, index)
    result = {"success": True, "changed": False, "file_path": file_path,
              "kind": "js_ts" if language == "vue" else language,
              "merged_groups": 0, "removed_blocks": 0,
              "before_sha256": snapshot.sha256, "after_sha256": snapshot.sha256,
              "edited_ranges": [], "applied_groups": [],
              "skipped_groups": list(planned["skipped_groups"]),
              "diagnostics": planned["diagnostics"], "events": [],
              "findings": list(planned["skipped_groups"])}
    if not planned["edits"]:
        return result
    committed = commit_beacon_edit_plan(planned["edit_plan"], scanner=_beacon_edit_records)
    result.update(committed)
    if committed["success"]:
        for group in planned["applied_groups"]:
            event = {**group, "status": "committed", "after_sha256": committed["after_sha256"]}
            result["applied_groups"].append(event)
            result["events"].append(event)
        result["merged_groups"] = len(result["applied_groups"])
        result["removed_blocks"] = sum(len(g["dropped_ids"]) for g in result["applied_groups"])
    else:
        # The file's optional tidy-up missed CAS/IO/proof, not its independent
        # map. Retain planned witnesses without claiming a committed repair.
        result["after_sha256"] = None
        for group in planned["applied_groups"]:
            row = {**group, "status": "skipped", "reason": committed.get("error_kind", "hygiene_failed"),
                   "candidates": group["members"], "commit_evidence": committed}
            result["skipped_groups"].append(row)
            result["findings"].append(row)
    return result


# @beacon[
#   id=carto-hygiene@normalize_duplicate_ast_beacons_in_file,
#   role=ƒ normalize_duplicate_ast_beacons_in_file(file_path: str),
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
#   kind=ast,
#   comment=Optional duplicate-AST hygiene uses the shared grammar and physical attachment; only same-id compatible groups merge at the nearest member while choices and file-local failures remain data.,
# ]
def normalize_duplicate_ast_beacons_in_file(file_path: str) -> dict[str, Any]:
    """Coalesce unambiguous explicit AST groups; never choose between ids.

    The language adapters observe once, consume U01/U03 and publish one U02
    plan. Broken or conflicting groups are returned with every member while
    independent groups proceed. Existing claimed-real invocation gates remain
    the caller's responsibility; this function adds no refresh gate.
    """
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".py":
            return _normalize_duplicate_ast_beacons_python(file_path)
        if ext in {".js", ".jsx", ".ts", ".tsx", ".vue"}:
            return _normalize_duplicate_ast_beacons_js_ts(file_path)
        if ext in {".md", ".markdown"}:
            return _normalize_duplicate_ast_beacons_markdown(file_path)
        return {"success": True, "changed": False, "file_path": file_path, "reason": "unsupported_ext",
                "findings": [], "events": [], "applied_groups": [], "skipped_groups": []}
    except Exception as e:  # noqa: BLE001
        kind = "not_utf8" if isinstance(e, UnicodeDecodeError) else "io_error" if isinstance(e, OSError) else "hygiene_failed"
        finding = {"operation": "duplicate_ast_hygiene", "reason": kind, "file_path": file_path,
                   "error": str(e), "candidates": []}
        return {"success": False, "changed": False, "file_path": file_path, "error": str(e),
                "error_kind": kind, "findings": [finding], "events": [],
                "applied_groups": [], "skipped_groups": [finding]}


# @beacon[
#   id=carto-hygiene@_normalize_duplicate_ast_beacons_markdown,
#   role=ƒ _normalize_duplicate_ast_beacons_markdown(file_path: str),
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,nexus-md-header-path,carto-fwd-markdown,
#   kind=ast,
#   comment=Markdown adapter delegates grouping extents field splices and choices to the shared hygiene planner and publishes one verified byte plan; no private heading or wrapper scan.,
# ]
def _normalize_duplicate_ast_beacons_markdown(file_path: str) -> dict[str, Any]:
    """Adapt Markdown to the one snapshot/attachment hygiene recipe."""
    return _normalize_duplicate_ast_beacons(file_path, "markdown")


# @beacon[
#   id=carto-hygiene@_normalize_duplicate_ast_beacons_python,
#   role=ƒ _normalize_duplicate_ast_beacons_python(file_path: str),
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
#   kind=ast,
#   comment=Python adapter delegates all duplicate grouping and field policy to the shared hygiene planner using the map attachment and one atomic byte commit.,
# ]
def _normalize_duplicate_ast_beacons_python(file_path: str) -> dict[str, Any]:
    """Adapt Python to the one snapshot/attachment hygiene recipe."""
    return _normalize_duplicate_ast_beacons(file_path, "python")


# @beacon[
#   id=carto-hygiene@_normalize_duplicate_ast_beacons_js_ts,
#   role=ƒ _normalize_duplicate_ast_beacons_js_ts(file_path: str),
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
#   kind=ast,
#   comment=JS/TS/Vue adapter consumes real section records and shared physical attachments; compatible groups splice the lowest member without changing its comment form.,
# ]
def _normalize_duplicate_ast_beacons_js_ts(file_path: str) -> dict[str, Any]:
    """Adapt JS/TS or section-aware Vue without a private dialect parser."""
    language = "vue" if os.path.splitext(file_path)[1].lower() == ".vue" else "js_ts"
    return _normalize_duplicate_ast_beacons(file_path, language)


# @beacon[
#   id=carto-js-ts@_iter_ast_outline_nodes-nf4x,
#   role=ƒ _iter_ast_outline_nodes(nodes),
#   slice_labels=carto-js-ts,ra-snippet-range-ast-py,carto-fwd-source,
#   kind=ast,
# ]
def _iter_ast_outline_nodes(nodes: list[dict[str, Any]]):
    """Yield all AST-backed outline nodes in a pre-order traversal.

    Nodes are expected to carry 'ast_type' when they correspond to
    Python AST elements (class/function/async/const).
    """
    for node in nodes:
        if isinstance(node, dict) and node.get("ast_type"):
            yield node
        children = (node.get("children") or []) if isinstance(node, dict) else []
        for ch in children:
            if isinstance(ch, dict):
                for sub in _iter_ast_outline_nodes([ch]):
                    yield sub


def _find_enclosing_ast_node_for_line(
    nodes: list[dict[str, Any]], start_line: int
) -> Optional[dict[str, Any]]:
    """Find the deepest AST node whose [start,end] span contains start_line."""
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


# @beacon[
#   id=carto-js-ts@apply_python_beacons,
#   role=carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-snippet-range-ast-beacon-py,ra-reconcile,
#   kind=span,
# ]
# Phase 2 JS/TS: Python beacon attachment template.
# Reference for apply_js_beacons(...), which will decorate JS/TS
# Tree-sitter AST nodes (kind=ast) and attach span children
# (kind=span) in the same way.
# @beacon[
#   id=auto-beacon@apply_python_beacons-xzvd,
#   role=apply_python_beacons,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Renders Python AST and span beacons using the shared physical attachment index and bounded metadata/context records; only display and span-body projection remain mapper-specific.,
# ]
def apply_python_beacons(
    file_path: str,
    lines: list[str],
    outline_nodes: list[dict[str, Any]],
    *,
    decorator_ranges_by_definition: Optional[dict[int, list[tuple[int, int]]]] = None,
) -> None:
    """Apply Python @beacon[...] metadata to an existing AST outline tree.

    - AST beacons (kind=ast): decorate existing AST nodes (no new nodes).
    - SPAN beacons (kind=span): create child nodes under the enclosing AST node
      (or at file-level if no enclosing AST node is found).

    AST ownership and implicit promotion come only from
    ``beacon_attachment_index``: real comments and owned decorators may be
    crossed, code and bare strings may not. Explicit/paired spans stay spans.

    Comment context:
    - Nearby comments and lexical triple-string ranges are slurped using the
      scanner's metadata projection and pair relations, never private ends.
    """
    # IMPORTANT: do not bail out just because the Python AST outline is empty.
    # File-level SPAN beacons are still valid for AST-less modules such as
    # package __init__.py files that only contain imports/re-exports/constants.
    records = _scan_source_beacons(_source_line_view(lines), "python")
    beacons = _parse_structural_beacons("python", lines, records=records)
    if not beacons:
        return
    # Warn early if there are duplicate beacon IDs in the file.
    _prepend_duplicate_beacon_id_warning_nodes(outline_nodes, beacons, language="PY")

    # Reuse the syntax index built by the outline's AST observation.
    if decorator_ranges_by_definition is None:
        decorator_ranges_by_definition = _python_decorator_ranges_by_definition(lines)
    attachment = beacon_attachment_index(_source_line_view(lines), outline_nodes, {
        "language": "python", "records": records, "beacons": beacons,
        "decorator_ranges_by_definition": decorator_ranges_by_definition,
    })
    attachment_by_line = {b["record"]["line"]: b for b in attachment["blocks"]}

    # Context uses the same lexical evidence as structural parsing. A quoted
    # delimiter in a comment cannot invent a triple-string span.
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

    # @beacon[
    #   id=auto-beacon@apply_python_beacons._find_triple_span_for_line-3dh3,
    #   role=apply_python_beacons._find_triple_span_for_line,
    #   slice_labels=carto-fwd-source,
    #   kind=ast,
    # ]
    def _find_triple_span_for_line(line_no: int) -> Optional[tuple[int, int]]:
        for s, e in triple_spans:
            if s <= line_no <= e:
                return (s, e)
        return None

    span_sort_lines: dict[int, int] = {}

    # @beacon[
    #   id=carto-beacon-order@py-node-source-order-line,
    #   role=ƒ _node_source_order_line(node),
    #   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
    #   kind=ast,
    # ]
    def _node_source_order_line(node: dict[str, Any]) -> int:
        cached = span_sort_lines.get(id(node))
        if isinstance(cached, int):
            return cached
        ln = node.get("orig_lineno_start_unused") if isinstance(node, dict) else None
        if isinstance(ln, int):
            return ln
        return 10**9

    # @beacon[
    #   id=carto-beacon-order@py-insert-child-in-source-order,
    #   role=ƒ _insert_child_in_source_order(siblings, child_node, sort_line: int),
    #   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
    #   kind=ast,
    # ]
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

    # @beacon[
    #   id=carto-js-ts@_extract_python_beacon_context-7u3n,
    #   role=ƒ _extract_python_beacon_context(comment_line: int, decor_start: Optional[int], beacon_id: Optional[str]),
    #   slice_labels=carto-js-ts,ra-snippet-range-span-beacon,carto-fwd-source,
    #   kind=ast,
    #   comment=Slurps projected ordinary Python comments and lexical triple strings; recorded pair relations stop only this span, while damaged neighbouring metadata never swallows following context.,
    # ]
    def _extract_python_beacon_context(
        comment_line: int,
        decor_start: Optional[int] = None,
        beacon_id: Optional[str] = None,
    ) -> List[str]:
        """Collect comments/triple strings using recorded metadata and pairs."""
        context: List[str] = []
        seen_spans: set[tuple[int, int]] = set()

        # Above: walk past decorators to reach true leading comments, and
        # skip any decorator region attached to the enclosing AST node
        # (when known) so multiline decorators do not block comment capture.
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
                # IMPORTANT: preserve original order when prepending a multi-line
                # triple-quoted block. Using insert(0, ...) in a forward loop
                # reverses the block.
                for k in range(e, s - 1, -1):
                    context.insert(0, lines[k - 1])
                seen_spans.add(span)
                j = s - 1
                continue
            if stripped.startswith("@"):
                # Skip decorator lines but keep walking upward so that
                # comments above a decorator are still captured as
                # CONTEXT COMMENTS (PYTHON).
                j -= 1
                continue
            break

        # Below
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

    # The authority owns attachment and promotion; rendering does not decide.
    for beacon in beacons:
        attached = attachment_by_line.get(beacon.get("comment_line"))
        if attached:
            beacon["kind"] = attached["effective_kind"]
            beacon["_anchor_lineno"] = attached["next_code_line"]

    # Pass 1: AST beacons
    for beacon in beacons:
        if beacon.get("kind") != "ast":
            continue
        attached = attachment_by_line.get(beacon.get("comment_line"))
        chosen = attached["anchor"]["node"] if attached and attached["anchor"] else None
        if chosen is None:
            continue

        # Decorate chosen AST node
        b_id = (beacon.get("id") or "").strip()
        role = (beacon.get("role") or "").strip()
        raw_slice_labels = (beacon.get("slice_labels") or "").strip()
        # Defaults: derive role from id prefix (before '@')
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

        # Merge beacon metadata into note. For AST beacons, we rely on the
        # AST node itself to carry CONTEXT COMMENTS (PYTHON) so we do not
        # duplicate comment blocks in the BEACON section.
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
            # Keep the identifier (AST_QUALNAME) at the top, then insert the
            # BEACON metadata block, then the rest of the content.
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

    # Pass 2: SPAN beacons
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

            # Find candidate start lines, but IGNORE lines that are comments
            # (including the @beacon block itself) so we never anchor spans to
            # their own metadata.
            start_candidates: list[int] = []
            for idx, line in enumerate(lines, start=1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if norm_snip in normalize_for_match(line):
                    start_candidates.append(idx)

            if not start_candidates:
                # No match – skip for now
                continue

            if len(start_candidates) == 1:
                attachment_line = start_candidates[0]
            else:
                after = [ln for ln in start_candidates if ln > comment_line]
                if len(after) == 1:
                    attachment_line = after[0]
                else:
                    # Ambiguous span; require beacon refinement
                    continue
        else:
            # Snippet-less spans attach based on the beacon's own location,
            # not the next code line. This keeps file-level/documentation spans
            # at file scope even when the next AST node is a function/class.
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

        # Decide enclosing AST node (if any)
        enclosing = _find_enclosing_ast_node_for_line(outline_nodes, attachment_line)

        b_id = (beacon.get("id") or "").strip()
        role = (beacon.get("role") or "").strip()
        raw_slice_labels = (beacon.get("slice_labels") or "").strip()
        # Defaults: derive role from id prefix (before '@')
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

        # Raw Python span text between @beacon[...] and @beacon-close[...] (if available)
        span_start = beacon.get("span_lineno_start")
        span_end = beacon.get("span_lineno_end")
        span_text_lines: List[str] = []
        if isinstance(span_start, int) and isinstance(span_end, int):
            span_start = max(1, span_start)
            span_end = min(len(lines), span_end)
            if span_start <= span_end:
                span_text_lines = lines[span_start - 1 : span_end]
                # Trim leading/trailing blank lines inside the span
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
            # File-level span: attach directly under top-level outline
            _insert_child_in_source_order(outline_nodes, span_node, attachment_line)


# @beacon[
#   id=carto-js-ts@get_docstring-u9fp,
#   role=ƒ get_docstring(node: AST),
#   slice_labels=carto-js-ts,ra-snippet-range-ast-py,carto-fwd-source,
#   kind=ast,
# ]
def get_docstring(node: ast.AST) -> str:
    """Extract docstring from an AST node if present. (Synced Update)"""
    return ast.get_docstring(node) or ""


# @beacon[
#   id=auto-beacon@get_function_signature-3y03,
#   role=get_function_signature,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def get_function_signature(node: ast.FunctionDef) -> str:
    """Reconstruct function signature for display."""
    args = []
    
    # Handle positional arguments
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
    """Make duplicate Python const AST_QUALNAMEs unique with a line suffix.

    Module-level constants can legitimately be assigned in mutually-exclusive
    branches, e.g. ``try: FLAG=True`` / ``except: FLAG=False``. Cartographer used
    to emit both as ``AST_QUALNAME: FLAG``, which makes identity seeding
    ambiguous. For const nodes only, append a deterministic ``@lineN`` suffix
    when the same qualname appears more than once. Function/class identities are
    deliberately left untouched.
    """
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

# @beacon[
#   id=agent-kit@parse_file_outline_core,
#   slice_labels=agent-kit-vendor,
#   kind=ast,
#   comment=Ungrouped Python outline from a snapshot or one binary read: BOM-free normalized AST input, physical rows and recorded metadata context drive beacon attachment; errors propagate to the public wrapper.,
# ]
def parse_file_outline_core(
    file_path: str, *, snapshot: SourceSnapshot | None = None,
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Ungrouped AST walk and beacon attachment from one immutable observation.

    A supplied snapshot performs no IO; the path-only call reads once. Returns
    ``(outline_nodes, physical_lines)``. Errors propagate to the wrapper.
    """
    snapshot = snapshot if snapshot is not None else read_source_snapshot(file_path)
    tree = ast.parse(snapshot.normalized_text, filename=file_path)
    lines = list(snapshot.physical_lines)
    decorator_ranges_by_definition = _python_decorator_ranges_by_definition(lines, tree=tree)
    context_records = _scan_source_beacons(_source_line_view(lines), "python")
    _, context_mask, context_cleaned = _beacon_context_view(lines, "python", context_records)

    # @beacon[
    #   id=auto-beacon@parse_file_outline._extract_ast_comment_context-h4xx,
    #   role=parse_file_outline._extract_ast_comment_context,
    #   slice_labels=carto-fwd-source,
    #   kind=ast,
    #   comment=Collects Python AST-adjacent comments from the source-record context projection, crossing only owned decorators; no normalized-text rescan can swallow human context after damaged metadata.,
    # ]
    def _extract_ast_comment_context(start_lineno: int, decor_start: Optional[int]) -> list[str]:
        """Collect AST comments after subtracting recorded metadata ranges."""
        context: list[str] = []
        # Above: skip decorator region between decor_start and the AST node.
        j = start_lineno - 1
        while j >= 1:
            raw = lines[j - 1]
            stripped = raw.lstrip()
            if not stripped:
                j -= 1
                continue
            if decor_start is not None and decor_start <= j < start_lineno:
                # Inside decorator region (including multiline decorators);
                # skip these lines entirely so we can reach comments that
                # truly lead the AST node.
                j -= 1
                continue
            if context_mask[j - 1]:
                body = context_cleaned[j - 1]
                if body:
                    context.insert(0, body)
                j -= 1
                continue
            break

        # Below: immediate top-of-body comments (before docstring or code)
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

    priority_counter = [100]  # Mutable list to share across recursion

    # @beacon[
    #   id=carto-parser@should-map-python-assigned-name,
    #   role=ƒ _should_map_python_assigned_name(name: str),
    #   slice_labels=carto-js-ts,ra-reconcile,carto-fwd-source,
    #   kind=ast,
    #   comment=Select Python assigned names that should become Cartographer outline nodes, including module-level dunder globals like __version__ and __all__,
    # ]
    def _should_map_python_assigned_name(name: str, *, qual_prefix: Optional[str]) -> bool:
        """Return True when an assigned Name should appear in the Python outline."""
        if not isinstance(name, str) or not name:
            return False
        if name.isupper():
            return True
        # Map module-level dunder globals like __version__ / __all__ so
        # package __init__.py and similar files remain structurally visible.
        if qual_prefix is None and len(name) > 4 and name.startswith("__") and name.endswith("__"):
            return True
        return False

    # @beacon[
    #   id=carto-parser@assigned-name-note-label,
    #   role=ƒ _assigned_name_note_label(name: str),
    #   slice_labels=carto-js-ts,ra-reconcile,carto-fwd-source,
    #   kind=ast,
    #   comment=Choose the note label for mapped Python assigned names, distinguishing module-level dunder globals from regular constants,
    # ]
    def _assigned_name_note_label(name: str, *, qual_prefix: Optional[str]) -> str:
        if qual_prefix is None and len(name) > 4 and name.startswith("__") and name.endswith("__"):
            return "Module global"
        return "Constant"

    # @beacon[
    #   id=carto-parser@iter-nested-statement-bodies-py,
    #   role=ƒ _iter_nested_statement_bodies(stmt: AST),
    #   slice_labels=carto-js-ts,ra-reconcile,carto-fwd-source,
    #   kind=ast,
    # ]
    def _iter_nested_statement_bodies(stmt: ast.AST) -> List[List[ast.AST]]:
        """Return nested statement lists for control-flow containers.

        We intentionally do NOT create outline nodes for statements like
        if/try/with/for/match, but we *do* need to descend into their
        bodies so nested defs/classes become visible to Cartographer/F12.

        The returned bodies are sorted by their first source line so nested
        AST nodes appear in lexical order regardless of construct type.
        """
        candidates: List[tuple[int, int, List[ast.AST]]] = []
        ordinal = 0

        # @beacon[
        #   id=auto-beacon@parse_file_outline._iter_nested_statement_bodies._add-ym6p,
        #   role=parse_file_outline._iter_nested_statement_bodies._add,
        #   slice_labels=carto-fwd-source,
        #   kind=ast,
        # ]
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

    # @beacon[
    #   id=auto-beacon@parse_file_outline.walk_body-moke,
    #   role=parse_file_outline.walk_body,
    #   slice_labels=carto-fwd-source,
    #   kind=ast,
    # ]
    def walk_body(
        body_items: List[ast.AST],
        qual_prefix: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for item in body_items:
            # ONE shared physical-start interpretation for classes and
            # functions, also used by forward attachment and reverse writes.
            decor_ranges = decorator_ranges_by_definition.get(getattr(item, "lineno", 0) - 1, [])
            decor_start: Optional[int] = decor_ranges[0][0] + 1 if decor_ranges else None
            if isinstance(item, ast.ClassDef):
                qual = item.name if not qual_prefix else f"{qual_prefix}.{item.name}"
                start = getattr(item, "lineno", None)
                end = getattr(item, "end_lineno", start)
                # Build note: AST_QUALNAME header, then optional separator and docstring.
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
                # Optional: Capture constants and selected module globals at this structural level.
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




# @beacon[
#   id=carto-js-ts@parse_file_outline,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,ra-snippet-range-ast-py,ra-reconcile,carto-fwd-source,
#   kind=span,
# ]
# Phase 1 JS/TS: Python AST outline template.
# Used as the reference shape for the new JS/TS Tree-sitter-based
# outline builder (parse_js_ts_outline) so JS/TS nodes match the
# same NEXUS schema (ast_type, ast_name, ast_qualname, file_path,
# line ranges, priority, children).
# @beacon[
#   id=auto-beacon@parse_file_outline-tfh6,
#   role=parse_file_outline,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Public Python snapshot/path wrapper: one ungrouped parse plus virtual grouping; preserves a distinguishable Parse Error sentinel and optional outline-error data instead of an authoritative empty outline.,
# ]
def parse_file_outline(
    file_path: str, *, snapshot: SourceSnapshot | None = None, diagnostics: list | None = None,
) -> List[Dict[str, Any]]:
    """Parse a single Python file into a NEXUS outline tree + beacon decorations.

    Steps (V1 Beacon-aware):
    A supplied snapshot is never reopened. Parse failures retain a sentinel.

    1. Parse the file with `ast.parse` and build an AST-backed outline tree that
       records `ast_type`, `ast_name`, `ast_qualname`, `file_path`, and
       `orig_lineno_start_unused` / `orig_lineno_end_unused` for each node.
    2. Scan the raw source lines for `@beacon[...]` comment blocks.
    3. Attach AST beacons (kind=ast) to existing outline nodes (no new nodes).
    4. Attach SPAN beacons (kind=span) as child nodes under the enclosing AST
       node (or at file-level if no enclosing node is found).

    PRIORITY HANDLING: Existing class/function/const nodes still receive
    sequential priorities (100, 200, 300, ...) based on order in source code.
    Span beacon nodes do not currently participate in the priority sequence.
    """
    try:
        outline_nodes, lines = parse_file_outline_core(file_path, snapshot=snapshot)
        return group_outline_by_carto_markers(
            lines=lines,
            outline_nodes=outline_nodes,
            language="python",
            comment_style="python",
        )
    except Exception as e:
        if diagnostics is not None:
            diagnostics.append({"record": "outline_error", "file": file_path, "language": "python", "exception": str(e)})
        return [{
            "name": _warn_name("⚠️ Parse Error"),
            "note": str(e),
            "children": []
        }]

# @beacon[
#   id=carto-js-ts@parse_js_ts_outline,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,ra-snippet-range-ast-js-ts,ra-reconcile,carto-fwd-source,
#   kind=span,
# ]
# Phase 1 JS/TS: placeholder anchor for the future parse_js_ts_outline(...)
# function. The next agent will define the JS/TS Tree-sitter-based outline
# builder immediately above or below this beacon so Cartographer can
# produce JS/TS AST nodes in the same shape as Python.


# @beacon[
#   id=auto-beacon@_build_js_comment_mask-shrd,
#   role=_build_js_comment_mask,
#   slice_labels=carto-js-ts,ra-snippet-range-ast-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=Projects shared JS/TS lexical comments into the established mask/body surface: line comments must lead the line, inline C comments keep their old projection, and literal/template text never opens comment state; real interpolation comments remain live.,
# ]
def _build_js_comment_mask(src_lines: list[str]) -> tuple[list[bool], list[str]]:
    """Project shared lexical comments, without a private beacon grammar.

    The existing mask/body call shape is kept for outline and snippet callers.
    A quoted ``/*`` cannot start a comment; template expressions can contain
    actual comments. Preserve the existing line-leading // and inline C-body
    projection: a trailing // on a code line must not make that code a context
    comment. Metadata remains ordinary comment text for later stripping.
    """
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


# @beacon[
#   id=auto-beacon@_extract_js_ast_comment_context-shrd,
#   role=_extract_js_ast_comment_context,
#   slice_labels=carto-js-ts,ra-snippet-range-ast-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=Collects AST-adjacent JS/TS comment bodies already projected from structural records; legacy callers obtain the same source-record projection, never a normalized private end scan.,
# ]
def _extract_js_ast_comment_context(
    start_line: int,
    lines: list[str],
    n: int,
    comment_mask: list[bool],
    cleaned_comments: list[str],
    *, records=None,
) -> list[str]:
    """Extract contiguous JS/TS comments around a JS/TS AST node line.

    Walks upward from ``start_line - 1`` collecting contiguous JS/TS comment
    lines (both ``//`` line comments and interior lines of ``/* ... */``
    block comments, as marked by ``comment_mask``), skipping blank lines and
    decorator-like lines starting with ``@``. Then walks downward from
    ``start_line + 1`` collecting the immediate top-of-body comments before
    code begins.

    Module-level shared helper. Used by both JS/TS outline builders:
      - parse_js_ts_outline (whole-file JS/TS outline)
      - _parse_js_ts_outline_from_source (Vue script-block outline)

    Args:
        start_line: 1-based file line of the AST node header.
        lines: Full file/source line array (1-based via ``lines[idx - 1]``).
        n: Total number of lines.
        comment_mask: Per-line bool array indicating comment-ness (from
            ``_build_js_comment_mask``).
        cleaned_comments: Per-line cleaned comment text (from
            ``_build_js_comment_mask``).

    ``records`` accompanies a caller's already-projected comment bodies.
    Legacy mask/body callers get the identical source-record projection here.
    Metadata is removed before selection, so a truncated selection cannot
    change a block's extent or consume following human comments.
    """
    if records is None:
        records, comment_mask, cleaned_comments = _beacon_context_view(lines, "js_ts")
    ctx: list[str] = []

    # Above: walk upward through contiguous comment lines, skipping
    # decorator-like lines that start with '@'.
    j = start_line - 1
    while j >= 1:
        raw = lines[j - 1]
        stripped = raw.lstrip()
        if not stripped:
            j -= 1
            continue
        if stripped.startswith("@"):  # decorator-like
            j -= 1
            continue
        if comment_mask[j - 1]:
            text = cleaned_comments[j - 1]
            if text:
                ctx.insert(0, text)
            j -= 1
            continue
        break

    # Below: immediate top-of-body comments (before code).
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


# ---------------------------------------------------------------------------
# Phase A (May 2026): module-level leaf helpers for JS/TS outline builders.
#
# These three helpers are byte-for-byte identical between parse_js_ts_outline
# (whole-file path) and _parse_js_ts_outline_from_source (Vue <script> path),
# so they sit at module level as siblings of _build_js_comment_mask and
# _extract_js_ast_comment_context. Both outline builders consume them as a
# single source of truth, preventing future drift.
#
# Each takes its previously-closed-over data (source_bytes) as an explicit
# parameter, matching the rest of the file's module-level helper conventions.
# ---------------------------------------------------------------------------


# @beacon[
#   id=carto-js-ts@_js_node_text-phaseA,
#   role=ƒ _js_node_text(node, source_bytes),
#   slice_labels=carto-js-ts,ra-snippet-range-ast-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=Module-level helper lifted in Phase A consolidation: decode a tree-sitter node's source bytes to text. Shared by parse_js_ts_outline and _parse_js_ts_outline_from_source.,
# ]
def _js_collapse_signature_ws(text: str | None) -> str:
    """Collapse all internal whitespace (including newlines) to single spaces.

    JS/TS AST signatures (function/method parameter lists, class/type
    headers) are emitted into the Workflowy node NAME field. The NAME field
    is single-line by contract: a heading or an AST signature is always one
    node, never a subtree. Tree-sitter, however, returns the verbatim source
    text of a node -- so a multi-line parameter list (e.g. a destructured
    options object with one default per line) carries embedded newlines.

    If those newlines reach the Workflowy API, Workflowy's create endpoint
    splits the name into a parent node (line 1) plus one child node per
    remaining line -- exactly the spurious node-explosion this collapses
    away. The full multi-line source remains available on disk via
    read_text_snippet; only the Workflowy-visible signature is single-lined.

    Mirrors what Python's get_function_signature already does implicitly by
    reconstructing a one-line signature from the AST.
    """
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())


def _js_node_text(node: Any, source_bytes: bytes) -> str:
    """Decode a tree-sitter node's source bytes to a UTF-8 string.

    Module-level twin of the previously-nested ``node_text`` closures inside
    ``parse_js_ts_outline`` and ``_parse_js_ts_outline_from_source``. Both
    outline builders close over their own ``source_bytes``; this helper takes
    it as an explicit parameter so the two callers share one implementation.

    Returns the empty string when ``node`` is None, matching the previous
    in-line behavior. UTF-8 decode errors are ignored (lossy) for robustness
    against minified or unusual sources.
    """
    if node is None:
        return ""
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")


# @beacon[
#   id=carto-js-ts@_js_is_function_like-phaseA,
#   role=ƒ _js_is_function_like(v),
#   slice_labels=carto-js-ts,ra-snippet-range-ast-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=Module-level helper lifted in Phase A consolidation: True when v is a tree-sitter arrow_function or function node. Shared by parse_js_ts_outline and _parse_js_ts_outline_from_source.,
# ]
def _js_is_function_like(v: Any) -> bool:
    """Return True when ``v`` is a tree-sitter function-like node.

    A function-like node is either an ``arrow_function`` or a ``function``
    expression. Module-level twin of the previously-nested
    ``_is_function_like`` closures inside the two outline builders.
    """
    return bool(v) and getattr(v, "type", None) in {"arrow_function", "function"}


# @beacon[
#   id=carto-js-ts@_js_callee_simple_name-phaseA,
#   role=ƒ _js_callee_simple_name(v, source_bytes),
#   slice_labels=carto-js-ts,ra-snippet-range-ast-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=Module-level helper lifted in Phase A consolidation: extract the trailing identifier of a call_expression's callee (e.g. computed in computed(...)). Shared by parse_js_ts_outline and _parse_js_ts_outline_from_source.,
# ]
def _js_callee_simple_name(v: Any, source_bytes: bytes) -> str:
    """Return the trailing identifier of a ``call_expression``'s callee.

    For an expression like ``computed(() => ...)`` the callee text is
    ``computed`` and this function returns ``"computed"``. For a
    member-access callee like ``foo.bar.baz(...)`` it returns ``"baz"`` (the
    final dotted segment). Returns the empty string when ``v`` is not a
    call_expression or has no resolvable callee.

    Module-level twin of the previously-nested ``_callee_simple_name``
    closures inside the two outline builders. Takes ``source_bytes`` as an
    explicit parameter (delegated to ``_js_node_text``).
    """
    if not v or getattr(v, "type", None) != "call_expression":
        return ""
    fn = v.child_by_field_name("function")
    if fn is None:
        return ""
    fn_text = _js_node_text(fn, source_bytes).strip()
    m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", fn_text)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Phase B.1 (May 2026): module-level Vue/defineStore-recognition helpers.
#
# Continuation of the Phase A consolidation. These six helpers were
# previously duplicated as nested closures inside both parse_js_ts_outline
# (whole-file path) and _parse_js_ts_outline_from_source (Vue <script>
# path). Their bodies were byte-identical between the two outline builders,
# differing only in that they closed over each builder's local source_bytes.
#
# By lifting them to module level with source_bytes as an explicit
# parameter (and using the Phase A helpers _js_node_text /
# _js_is_function_like / _js_callee_simple_name internally), both outline
# builders now share one source of truth for:
#   - computed(() => ...) wrapper recognition
#   - defineStore setup-function recognition
#   - defineStore options-object recognition
#   - options-object key text extraction
#   - function-parameter text extraction
#   - function-body node access
#
# _emit_obj_functions and _extract_define_store_options_children itself
# are NOT lifted in B.1 because they are structurally divergent between
# the two outline builders (Vue version applies line_offset0 + _qual()
# prefix at every emission point, whole-file version does not). Their
# consolidation belongs to Phase D's shared-walker architecture.
# ---------------------------------------------------------------------------


# @beacon[
#   id=carto-js-ts@_js_find_function_wrapper_body-phaseB1,
#   role=ƒ _js_find_function_wrapper_body(v, source_bytes),
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
#   kind=ast,
#   comment=Module-level helper lifted in Phase B.1 consolidation: detect wrapper calls like computed(() => ...) so nested computed declarations appear in Workflowy AST maps. Shared by parse_js_ts_outline and _parse_js_ts_outline_from_source.,
# ]
def _js_find_function_wrapper_body(v: Any, source_bytes: bytes) -> Any:
    """Recognize wrapper calls that carry a function body in their args.

    Returns the tree-sitter ``body`` node of the first function-like
    argument of a recognized wrapper call (currently: ``computed(...)``).
    Returns ``None`` if ``v`` is not a wrapper call or has no function-like
    argument.

    Used by both JS/TS outline builders to map ``computed(() => { ... })``
    declarations into the Workflowy AST tree as nested function-like
    scopes. Module-level twin of the previously-nested
    ``_find_function_wrapper_body`` closures.
    """
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


# @beacon[
#   id=carto-js-ts@_js_find_define_store_setup_body-phaseB1,
#   role=ƒ _js_find_define_store_setup_body(v, source_bytes),
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
#   kind=ast,
#   comment=Module-level helper lifted in Phase B.1 consolidation: return the BODY node of the setup function passed to defineStore (Pinia setup-store form), else None. Shared by parse_js_ts_outline and _parse_js_ts_outline_from_source.,
# ]
def _js_find_define_store_setup_body(v: Any, source_bytes: bytes) -> Any:
    """Return the BODY node of the setup function passed to ``defineStore``.

    Recognizes the Pinia setup-store form::

        defineStore('id', () => { ... })
        defineStore('id', function () { ... })

    Returns the function-body tree-sitter node if found, ``None``
    otherwise. Module-level twin of the previously-nested
    ``_find_define_store_setup_body`` closures inside both JS/TS outline
    builders.

    Note the callee-name check uses ``endswith("defineStore")`` so
    namespaced imports like ``pinia.defineStore(...)`` are also matched.
    """
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


# @beacon[
#   id=carto-js-ts@_js_find_define_store_options_object-phaseB1,
#   role=ƒ _js_find_define_store_options_object(v, source_bytes),
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
#   kind=ast,
#   comment=Module-level helper lifted in Phase B.1 consolidation: return the options object passed to defineStore (Pinia options-store form), else None. Shared by parse_js_ts_outline and _parse_js_ts_outline_from_source.,
# ]
def _js_find_define_store_options_object(v: Any, source_bytes: bytes) -> Any:
    """Return the options object passed to ``defineStore``.

    Recognizes the Pinia options-store form::

        defineStore('id', { actions: {...}, getters: {...} })

    Returns the options-object tree-sitter node if found, ``None``
    otherwise. Module-level twin of the previously-nested
    ``_find_define_store_options_object`` closures inside both JS/TS
    outline builders.

    Note: when both the setup-function and options-object forms are
    syntactically possible, callers should check
    ``_js_find_define_store_setup_body`` FIRST and fall back to this
    helper only if the former returns None. The two forms are mutually
    exclusive in real Pinia code, but the recognizer here is
    intentionally narrow (no setup-vs-options preference logic).
    """
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


# @beacon[
#   id=carto-js-ts@_js_key_text-phaseB1,
#   role=ƒ _js_key_text(n, source_bytes),
#   slice_labels=carto-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=Module-level helper lifted in Phase B.1 consolidation: extract the text of an object-literal property key, stripping surrounding quotes. Shared by both outline builders inside _extract_define_store_options_children.,
# ]
def _js_key_text(n: Any, source_bytes: bytes) -> str:
    """Return the text of an object-literal property key.

    Extracts the tree-sitter node's source text via ``_js_node_text`` and
    strips surrounding single or double quotes (so both ``foo`` and
    ``"foo"`` resolve to ``foo``). Returns the empty string when ``n`` is
    None.

    Module-level twin of the previously-nested ``_key_text`` closures
    inside ``_extract_define_store_options_children`` in both JS/TS
    outline builders.
    """
    if n is None:
        return ""
    txt = _js_node_text(n, source_bytes).strip()
    return txt.strip("\"'")


# @beacon[
#   id=carto-js-ts@_js_params_text-phaseB1,
#   role=ƒ _js_params_text(fn_node, source_bytes),
#   slice_labels=carto-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=Module-level helper lifted in Phase B.1 consolidation: extract a function-like node's parameter list as a parenthesized string (e.g. (a, b)). Shared by both outline builders inside _extract_define_store_options_children.,
# ]
def _js_params_text(fn_node: Any, source_bytes: bytes) -> str:
    """Return a function-like node's parameter list as ``(a, b, ...)``.

    Looks for a ``parameters`` or ``parameter`` child field on ``fn_node``
    and returns its source text, wrapping in parentheses if the raw text
    doesn't already start with ``(``. Returns ``"()"`` when ``fn_node`` is
    None or has no parameter field.

    Module-level twin of the previously-nested ``_params_text`` closures
    inside ``_extract_define_store_options_children`` in both JS/TS
    outline builders.
    """
    if fn_node is None:
        return "()"
    p = fn_node.child_by_field_name("parameters")
    if p is None:
        p = fn_node.child_by_field_name("parameter")
    if p is None:
        return "()"
    # Collapse multi-line parameter lists to a single line: AST signatures
    # land in the Workflowy NAME field, which is single-line by contract.
    # (Multi-line params would otherwise be split into child nodes by the
    # Workflowy create endpoint.)
    t = _js_collapse_signature_ws(_js_node_text(p, source_bytes))
    return t if t.startswith("(") else f"({t})"


# @beacon[
#   id=carto-js-ts@_js_body_node-phaseB1,
#   role=ƒ _js_body_node(fn_node),
#   slice_labels=carto-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=Module-level helper lifted in Phase B.1 consolidation: return the body field of a function-like tree-sitter node, or None. Shared by both outline builders inside _extract_define_store_options_children.,
# ]
def _js_body_node(fn_node: Any) -> Any:
    """Return the ``body`` field of a function-like tree-sitter node.

    Returns ``None`` when ``fn_node`` is None or has no ``body`` field.
    Module-level twin of the previously-nested ``_body_node`` closures
    inside ``_extract_define_store_options_children`` in both JS/TS
    outline builders. Has no closure dependencies of its own; lifted
    purely for the single-source-of-truth invariant.
    """
    if fn_node is None:
        return None
    return fn_node.child_by_field_name("body")


# ---------------------------------------------------------------------------
# Phase D (May 2026): JsTsOutlineContext + shared _js_walk.
#
# The crown-jewel consolidation. Both parse_js_ts_outline (whole-file path)
# and _parse_js_ts_outline_from_source (Vue <script> path) now construct a
# JsTsOutlineContext that captures all file/source-level state and the
# Vue-vs-non-Vue configuration in one object, then call the SAME module-level
# _js_walk function.
#
# Architecture:
#
#   - JsTsOutlineContext holds:
#     * Immutable per-parse data: file_path, source_bytes, lines, n,
#       comment_mask, cleaned_comments
#     * Configuration that captures the Vue divergence:
#       line_offset0 (0 for whole-file, >0 for Vue script blocks)
#       qual_prefix (None/empty for whole-file, e.g. 'script_setup_0' for Vue)
#     * Shared mutable state: priority_counter
#
#   - JsTsOutlineContext exposes methods that EACH OUTLINE BUILDER USES THE
#     SAME WAY but that internally apply Vue rebasing/prefixing when needed:
#     * ctx.abs_line(local) -> int        # local + line_offset0
#     * ctx.qualify(inner_qual) -> str    # prefix + inner_qual (or inner_qual)
#     * ctx.make_note(ast_qual, local_line, doc) -> str
#     * ctx.next_priority() -> int        # encapsulates the [100] counter
#     * ctx.node_text(node), ctx.find_*, ctx.key_text, ctx.params_text,
#       ctx.body_node -> thin wrappers over the Phase A / B.1 helpers
#
#   - _js_walk(node, ctx, qual_prefix, scope_state) is a FREE FUNCTION:
#     scope_state and qual_prefix vary by recursive call;
#     ctx is threaded unchanged through recursion.
#
# Naming discipline (enforced throughout _js_walk):
#   - start_line_local / end_line_local: Tree-sitter local coordinates
#   - start_line_abs   / end_line_abs:   file-absolute coordinates
#   - inner_qual: the recursive, un-prefixed name (passed to child walks)
#   - ast_qual:   the final external AST_QUALNAME emitted on the node dict
# Even in the non-Vue case where abs == local and ast == inner, the
# distinct names document intent and prevent the kind of confusion that
# caused subtle Vue bugs historically.
# ---------------------------------------------------------------------------


# (dataclass/field imported at top of module)


# @beacon[
#   id=carto-js-ts@JsTsOutlineContext-phaseD,
#   role=JsTsOutlineContext,
#   slice_labels=carto-js-ts,ra-snippet-range-ast-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=Per-parse JS/TS context from one physical-line source view: one recorded metadata projection supplies every AST comment slurp, text construction normalizes only CRLF/CR, and the walker shares Vue offsets and priorities.,
# ]
@dataclass
class JsTsOutlineContext:
    """Per-parse JS/TS outline context shared by both outline builders.

    Constructed once per outline-builder invocation from the snapshot parse
    view (or an extracted section), including one recorded metadata/comment
    projection reused by all AST note builders. The same context
    object is threaded unchanged through every recursive call into
    ``_js_walk``; only ``qual_prefix`` (the recursive ancestry) and
    ``scope_state`` (per-scope mutable parser state) vary by recursion.

    Configuration that captures the Vue-vs-non-Vue divergence:

    - ``line_offset0``: 0 for whole-file JS/TS; ``>0`` for Vue ``<script>``
      blocks (the 0-based file row where ``source_text`` line 1 lives).
      Applied by ``abs_line(local)``.

    - ``qual_prefix``: None or empty for whole-file JS/TS; a prefix string
      like ``"script_setup_0"`` for Vue script blocks (so AST_QUALNAMEs
      from different ``<script>`` blocks don't collide). Applied by
      ``qualify(inner_qual)`` which composes ``"{qual_prefix}:{inner}"``
      when ``qual_prefix`` is non-empty, and is a no-op otherwise.

    Critical invariant for non-Vue callers: with ``line_offset0=0`` and
    ``qual_prefix=None`` (the defaults), ``abs_line(x) == x`` and
    ``qualify(x) == x``. This is what guarantees byte-identical behavior
    for whole-file JS/TS after the Phase D refactor.
    """

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

    # -- Constructors -------------------------------------------------------

    @classmethod
    def from_file(cls, file_path: str, language_name: str) -> "JsTsOutlineContext":
        """Read once in binary mode and delegate to the text-input constructor."""
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
        """Build a context from a whole-file parse view or extracted Vue text.

        CRLF/CR become LF for tree-sitter row coordinates, and only physical
        newlines split ``lines``. One source-record projection strips context
        metadata before any node selects adjacent comments. BOM removal belongs to the file snapshot;
        an interior BOM in an extracted section remains ordinary source data.

        ``source_text`` is the extracted ``<script>`` block content,
        ``line_offset0`` is the 0-based file row where source_text line 1
        lives, and ``qual_prefix`` is prepended to every AST_QUALNAME via
        ``qualify(...)`` so script-block qualnames don't collide between
        multiple ``<script>`` blocks in the same .vue file.
        """
        source_text = source_text.replace("\r\n", "\n").replace("\r", "\n")
        source_bytes = source_text.encode("utf-8")
        parser = Parser()
        language = get_language(language_name)  # type: ignore[operator]
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

    # -- Coordinate transforms (the Vue divergence, localized) --------------

    def abs_line(self, local_line: int) -> int:
        """Translate a Tree-sitter local line into a file-absolute line.

        For whole-file JS/TS this is the identity (``line_offset0=0``).
        For Vue script blocks this adds ``line_offset0`` so the emitted
        ``orig_lineno_start_unused`` / ``orig_lineno_end_unused`` fields
        reference the .vue file's line space, not the extracted script
        block's local line space.
        """
        return int(local_line) + int(self.line_offset0 or 0)

    def qualify(self, inner_qual: str) -> str:
        """Compose the externally-visible AST_QUALNAME from a recursive inner name.

        For whole-file JS/TS (``qual_prefix=None`` or empty) this is the
        identity. For Vue script blocks this prepends ``qual_prefix:`` (a
        colon separator is auto-appended if not already present) so
        AST_QUALNAMEs from multiple ``<script>`` blocks in the same .vue
        file don't collide.

        The ``inner_qual`` should still be used as the ``qual_prefix=``
        argument when recursing into child scopes; only the externally
        emitted ``ast_qualname`` field uses ``qualify(inner_qual)``.
        """
        pref = (self.qual_prefix or "").strip()
        if pref and not pref.endswith(":"):
            pref = pref + ":"
        return f"{pref}{inner_qual}" if pref else inner_qual

    # -- Note construction --------------------------------------------------

    def make_note(
        self,
        ast_qual: str,
        start_line_local: Optional[int] = None,
        doc: Optional[str] = None,
    ) -> str:
        """Build the Workflowy note text for an emitted JS/TS AST node.

        Always starts with ``AST_QUALNAME: {ast_qual}``. If ``doc`` is
        provided, appends a ``---`` separator and the docstring. If
        ``start_line_local`` is provided, slurps adjacent JS/TS comments
        from the local source view (uses ``self.lines`` / ``self.n`` /
        ``self.comment_mask`` / metadata-projected ``self.cleaned_comments``
        and their ``self.beacon_records``) and appends
        them under a ``CONTEXT COMMENTS (JS/TS):`` block.

        Note: ``start_line_local`` is intentionally the LOCAL line (used
        to look up comments in the local source view, which for Vue is
        the extracted script block). For node emission in the outline
        tree, use ``self.abs_line(start_line_local)`` to compute
        ``orig_lineno_start_unused``.
        """
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

    # -- Shared mutable state -----------------------------------------------

    def next_priority(self) -> int:
        """Return the next priority value (100, 200, 300, ...) and advance.

        Encapsulates the ``[100]`` mutable list that was previously
        threaded as a closure variable. Each emitted outline node should
        set ``"priority": ctx.next_priority()`` exactly once.
        """
        p = self.priority_counter[0]
        self.priority_counter[0] += 100
        return p

    # -- Convenience wrappers around Phase A / B.1 module-level helpers ----

    def node_text(self, node: Any) -> str:
        """Decode a tree-sitter node's source bytes to a UTF-8 string.

        Wrapper around the Phase A module-level ``_js_node_text``.
        """
        return _js_node_text(node, self.source_bytes)

    def is_function_like(self, v: Any) -> bool:
        """Return True when ``v`` is an arrow_function or function expression.

        Wrapper around the Phase A module-level ``_js_is_function_like``.
        """
        return _js_is_function_like(v)

    def callee_simple_name(self, v: Any) -> str:
        """Return the trailing identifier of a ``call_expression``'s callee.

        Wrapper around the Phase A module-level ``_js_callee_simple_name``.
        """
        return _js_callee_simple_name(v, self.source_bytes)

    def find_function_wrapper_body(self, v: Any) -> Any:
        """Detect ``computed(() => ...)`` wrapper calls and return inner body.

        Wrapper around the Phase B.1 module-level
        ``_js_find_function_wrapper_body``.
        """
        return _js_find_function_wrapper_body(v, self.source_bytes)

    def find_define_store_setup_body(self, v: Any) -> Any:
        """Return the BODY node of the setup function passed to ``defineStore``.

        Wrapper around the Phase B.1 module-level
        ``_js_find_define_store_setup_body``.
        """
        return _js_find_define_store_setup_body(v, self.source_bytes)

    def find_define_store_options_object(self, v: Any) -> Any:
        """Return the options object passed to ``defineStore``.

        Wrapper around the Phase B.1 module-level
        ``_js_find_define_store_options_object``.
        """
        return _js_find_define_store_options_object(v, self.source_bytes)

    def key_text(self, n: Any) -> str:
        """Return the text of an object-literal property key.

        Wrapper around the Phase B.1 module-level ``_js_key_text``.
        """
        return _js_key_text(n, self.source_bytes)

    def params_text(self, fn_node: Any) -> str:
        """Return a function-like node's parameter list as ``(a, b, ...)``.

        Wrapper around the Phase B.1 module-level ``_js_params_text``.
        """
        return _js_params_text(fn_node, self.source_bytes)

    def body_node(self, fn_node: Any) -> Any:
        """Return the ``body`` field of a function-like tree-sitter node.

        Wrapper around the Phase B.1 module-level ``_js_body_node``.
        """
        return _js_body_node(fn_node)


# @beacon[
#   id=carto-js-ts@_js_walk_extract_define_store_options_children-phaseD,
#   role=ƒ _js_walk_extract_define_store_options_children(ctx, options_obj, store_inner_qual),
#   slice_labels=carto-js-ts,ra-snippet-range-ast-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=Module-level Phase D helper: extract method/getter functions from a defineStore options object. Calls back into _js_walk for nested scopes. Behaves identically for whole-file JS/TS and Vue script blocks via ctx.qualify and ctx.abs_line.,
# ]
def _js_walk_extract_define_store_options_children(
    ctx: "JsTsOutlineContext",
    *,
    options_obj: Any,
    store_inner_qual: str,
) -> list[dict[str, Any]]:
    """Parse a Pinia ``defineStore`` options object and emit action/getter functions.

    Walks the ``actions`` and ``getters`` properties of the options
    object, emitting one outline node per method/getter function. Each
    emitted node's children are computed by recursively calling
    ``_js_walk`` into the function body, with the function's inner
    qualname threaded as ``qual_prefix=``.

    Behaves identically for whole-file JS/TS and Vue script blocks:
    line numbers go through ``ctx.abs_line`` (identity for non-Vue,
    rebased for Vue) and AST_QUALNAMEs go through ``ctx.qualify``
    (identity for non-Vue, prefixed for Vue).

    Args:
        ctx: The shared JS/TS outline context.
        options_obj: The tree-sitter object node containing the
            ``actions`` / ``getters`` properties.
        store_inner_qual: The inner (un-prefixed) qualname of the
            enclosing store declarator, used to build each emitted
            function's inner qualname (``{store_inner_qual}.{section}.{fname}``).
            Note: this is the INNER name; the externally-emitted
            ``ast_qualname`` is computed by ``ctx.qualify(inner_qual)``
            for each function.
    """
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

    # locate actions/getters objects
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


# @beacon[
#   id=carto-js-ts@_js_walk-phaseD,
#   role=ƒ _js_walk(node, ctx, qual_prefix, scope_state),
#   slice_labels=carto-js-ts,ra-snippet-range-ast-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=The crown-jewel Phase D consolidation. Single shared recursive walker for both whole-file JS/TS (parse_js_ts_outline) and Vue script blocks (_parse_js_ts_outline_from_source). All Vue-vs-non-Vue divergence is encapsulated in the JsTsOutlineContext's abs_line/qualify methods so the walker body is identical.,
# ]
def _js_walk(
    node: Any,
    ctx: "JsTsOutlineContext",
    qual_prefix: Optional[str] = None,
    scope_state: Optional[dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Recursive JS/TS Tree-sitter AST walker, shared by both outline builders.

    Emits one outline node per recognized JS/TS construct (class,
    function, method, type/interface/enum, variable declaration with
    function-like RHS or defineStore container, etc.) and recurses into
    nested scopes with an updated ``qual_prefix`` and fresh
    ``scope_state``.

    Naming discipline enforced throughout:
        - ``start_line_local`` / ``end_line_local``: Tree-sitter local
          coordinates (matches Tree-sitter's row+1 convention).
        - ``start_line_abs`` / ``end_line_abs``: file-absolute
          coordinates via ``ctx.abs_line(...)``. For non-Vue
          (``ctx.line_offset0=0``) these equal the local coordinates.
        - ``inner_qual``: the recursive, un-prefixed qualname; passed as
          ``qual_prefix=`` when recursing into child scopes.
        - ``ast_qual``: the externally-visible AST_QUALNAME emitted on
          the outline node dict; computed via ``ctx.qualify(inner_qual)``.
          For non-Vue this equals ``inner_qual``.

    The two outline builders
    (``parse_js_ts_outline`` / ``_parse_js_ts_outline_from_source``)
    invoke this function on their respective tree roots. The only
    difference between the two cases is the configuration baked into
    ``ctx`` at construction time.

    Args:
        node: Tree-sitter node to walk. Children are traversed in order.
        ctx: Shared per-parse context (file/source data + Vue config).
        qual_prefix: Recursive qualname ancestry; ``None`` at top level.
            This is the INNER qualname (un-prefixed by ``ctx.qualify``).
        scope_state: Per-scope mutable parser state (anon-counter,
            lambda-local counts). Constructed fresh per scope; do NOT
            share across sibling scopes.

    Returns:
        List of outline node dicts in source order (one per recognized
        construct in ``node``'s children).
    """
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

            # TODO: in future, harvest JSDoc comments into doc
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

        # TypeScript surface types (top-level or nested in namespaces/modules).
        # These are important for navigation but should not explode into deep trees.
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

        # const/let/var declarations.
        #
        # We ALWAYS parse variable declarations so we can represent common
        # JS/TS patterns like Pinia/Vue stores:
        #
        #   export const useXStore = defineStore('x', () => {
        #       const doThing = () => { ... }
        #       function helper() { ... }
        #       return { doThing }
        #   })
        #
        # Policy to avoid outline explosion:
        # - At module scope (qual_prefix is None): emit nodes for simple
        #   identifier declarators (existing behavior).
        # - Inside an existing qual_prefix (nested scope): emit ONLY
        #   declarators whose RHS is function-like (arrow/function) or a
        #   known "container" call like defineStore(...) that contains a
        #   function body.
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
                # Skip destructuring patterns (object_pattern/array_pattern)
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

        # Recurse into other nodes to discover nested declarations.
        #
        # IMPORTANT: anonymous function scopes (arrow/function expressions)
        # can contain local declarations with repeated names (e.g. two
        # separate callbacks each declaring `const tintOpts`). If we recurse
        # with the same qual_prefix, those locals collide on AST_QUALNAME.
        #
        # We therefore derive a deterministic pseudo-scope suffix from source
        # coordinates for function-like anonymous scopes.
        next_prefix = qual_prefix
        next_scope_state = scope_state
        if t in {"arrow_function", "function", "function_expression"}:
            anon_scope = _js_next_anonymous_scope_name(scope_state)
            next_prefix = f"{qual_prefix}.{anon_scope}" if qual_prefix else anon_scope
            next_scope_state = _new_js_scope_state(is_lambda=True)

        if child.children:
            results.extend(_js_walk(child, ctx, qual_prefix=next_prefix, scope_state=next_scope_state))

    return results

# @beacon[
#   id=agent-kit@parse_js_ts_outline_core,
#   slice_labels=agent-kit-vendor,
#   kind=ast,
#   comment=Ungrouped JS/TS outline from a supplied snapshot or one binary read: one normalized physical-line parse view feeds tree-sitter and beacon attachment; errors propagate to the wrapper.,
# ]
def parse_js_ts_outline_core(
    file_path: str, *, snapshot: SourceSnapshot | None = None,
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Ungrouped core of parse_js_ts_outline (R1): the extension-to-language
    mapping, the tree-sitter parse via ``JsTsOutlineContext``, the outline
    walk and JS/TS beacon attachment, all from the same snapshot (a supplied
    observation is never reopened). Returns ``(outline_nodes, ctx.lines)``; assumes tree-sitter
    is importable; no virtual grouping and no parse-error handling.
    """
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

    # Phase 2: JS/TS beacon parsing + attachment (unchanged from
    # pre-Phase-D; uses the same ctx.lines list that the walker did).
    js_beacons = parse_js_beacon_blocks(ctx.lines)
    apply_js_beacons(file_path, ctx.lines, outline_nodes, js_beacons)
    return outline_nodes, ctx.lines




# @beacon[
#   id=auto-beacon@parse_js_ts_outline-7s4t,
#   role=parse_js_ts_outline,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Public JS/TS snapshot/path wrapper: checks parser availability then groups one ungrouped snapshot parse; failures retain the Parse Error node and optional outline-error data.,
# ]
def parse_js_ts_outline(
    file_path: str, *, snapshot: SourceSnapshot | None = None, diagnostics: list | None = None,
) -> List[Dict[str, Any]]:
    """Parse a single JavaScript / TypeScript file into a NEXUS outline tree.

    A supplied snapshot performs no source read. This mirrors parse_file_outline for Python but uses Tree-sitter to
    discover classes, functions, and methods and produces nodes with the
    same schema (ast_type, ast_name, ast_qualname, file_path,
    orig_lineno_start_unused, orig_lineno_end_unused, children, priority).
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in {".js", ".jsx", ".ts", ".tsx"}:
        return [{
            "name": _warn_name("⚠️ Unsupported JS/TS extension"),
            "note": f"File {file_path!r} does not look like a JS/TS source file.",
            "children": [],
        }]

    if not _HAVE_TREE_SITTER:
        return [{
            "name": _warn_name("⚠️ JS/TS parsing not available"),
            "note": (
                "Tree-sitter / tree_sitter_language_pack could not be imported. "
                "Install 'tree_sitter' and 'tree_sitter-language-pack' "
                "(plus language wheels like 'tree-sitter-javascript' and 'tree-sitter-typescript') "
                "in the Windows Python environment to enable JS/TS Cartographer support."
            ),
            "children": [],
        }]

    language_name = "javascript"
    if ext == ".ts":
        language_name = "typescript"
    elif ext == ".tsx":
        language_name = "tsx"

    try:
        outline_nodes, lines = parse_js_ts_outline_core(file_path, snapshot=snapshot)
        return group_outline_by_carto_markers(
            lines=lines,
            outline_nodes=outline_nodes,
            language="js_ts",
            comment_style="js_ts",
        )
    except Exception as e:  # pragma: no cover - safety net
        if diagnostics is not None:
            diagnostics.append({"record": "outline_error", "file": file_path, "language": "js_ts", "exception": str(e)})
        return [{
            "name": _warn_name("⚠️ Parse Error (JS/TS)"),
            "note": str(e),
            "children": [],
        }]


# @beacon[
#   id=auto-beacon@_vue_compact_start_tag_text-12au,
#   role=_vue_compact_start_tag_text,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _vue_compact_start_tag_text(raw: str) -> str:
    """Return a stable single-line representation of a Vue start tag."""
    s = (raw or "").replace("\t", " ").replace("\r", "").replace("\n", " ")
    while "  " in s:
        s = s.replace("  ", " ")
    return s.strip()


# @beacon[
#   id=auto-beacon@_vue_rebase_beacons-oyyv,
#   role=_vue_rebase_beacons,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Rebases a Vue section's parsed beacons (or, with keys=, the parser's diagnostics records) from section-local lines to .vue file lines.,
# ]
def _vue_rebase_beacons(
    beacons: list[dict[str, Any]],
    line_offset0: int,
    *,
    keys: Optional[tuple[str, ...]] = None,
) -> list[dict[str, Any]]:
    """Return a shallow-copied beacon list with line numbers rebased to file lines.

    line_offset0 is 0-based file row of the first line in the section's raw_text.
    ``keys`` names the line-number keys to rebase (default: the beacon dict's
    ``comment_line`` / ``span_lineno_start`` / ``span_lineno_end``).
    """
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


# @beacon[
#   id=auto-beacon@_parse_js_ts_outline_from_source,
#   role=ƒ _parse_js_ts_outline_from_source(),
#   slice_labels=carto-js-ts,ra-snippet-range-ast-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=Builds a Vue script outline entirely from the section's physical-line parse view; one JS/TS context rebases coordinates and returns local failure data without reopening the file.,
# ]
def _parse_js_ts_outline_from_source(
    *,
    source_text: str,
    file_path: str,
    language_name: str,
    line_offset0: int,
    vue_qual_prefix: str,
    diagnostics: list | None = None,
) -> List[Dict[str, Any]]:
    """Parse JS/TS AST from a physical-line text view and rebase node lines.

    The file path is identity only; this adapter never reopens it. A local
    parser failure remains a diagnostic node and optionally structured data.

    Used by ``parse_vue_outline`` to outline each ``<script>`` block in a
    Vue Single-File Component. The two arguments that make this distinct
    from whole-file JS/TS parsing are both threaded through the shared
    ``JsTsOutlineContext`` and applied automatically by ``_js_walk``:

    - ``line_offset0``: 0-based file row where ``source_text`` line 1
      lives. The context's ``abs_line(local)`` adds this to every
      emitted ``orig_lineno_start_unused`` / ``orig_lineno_end_unused``
      so the outline tree references .vue file coordinates, not the
      extracted script block's local line space.

    - ``vue_qual_prefix``: prefix to prepend to every emitted
      AST_QUALNAME via the context's ``qualify(inner_qual)``. Allows
      multiple ``<script>`` blocks (e.g. ``<script>`` +
      ``<script setup>``) in the same .vue file to have non-colliding
      qualnames in the outline tree.

    Phase D (May 2026): both outline builders now share the same
    ``_js_walk`` function via the ``JsTsOutlineContext`` abstraction.
    See ``parse_js_ts_outline`` for the whole-file twin.
    """
    if not _HAVE_TREE_SITTER:
        return [{
            "name": _warn_name("⚠️ JS/TS parsing not available"),
            "note": (
                "Tree-sitter / tree_sitter_language_pack could not be imported. "
                "Install 'tree_sitter' and 'tree_sitter-language-pack' "
                "(plus language wheels like 'tree-sitter-javascript' and 'tree-sitter-typescript') "
                "in the Windows Python environment to enable JS/TS Cartographer support."
            ),
            "children": [],
        }]

    try:
        # Phase D (May 2026): build a Vue-configured context and delegate
        # to the shared module-level _js_walk. The line_offset0 and
        # vue_qual_prefix parameters become ctx.line_offset0 and
        # ctx.qual_prefix respectively; the walker invokes ctx.abs_line()
        # and ctx.qualify() at every emission point, so the Vue rebasing
        # is transparent to the walker body. This is the architectural
        # win of the JsTsOutlineContext abstraction: one walker, two
        # configurations.
        ctx = JsTsOutlineContext.from_source(
            source_text=source_text,
            file_path=file_path,
            language_name=language_name,
            line_offset0=line_offset0,
            qual_prefix=vue_qual_prefix,
        )
        return _js_walk(ctx.root, ctx)

    except Exception as e:  # pragma: no cover
        if diagnostics is not None:
            diagnostics.append({"record": "outline_error", "file": file_path, "language": "vue_script", "exception": str(e), "line_offset0": line_offset0})
        return [{
            "name": _warn_name("⚠️ Parse Error (VUE script JS/TS)"),
            "note": str(e),
            "children": [],
        }]


def _parse_vue_sfc_tree(source_bytes: bytes) -> Any:
    """Parse a Vue SFC with tree-sitter and return its root node (raises on
    failure; the caller decides what a failure becomes).

    ``source_bytes`` is the observation's normalized UTF-8 parse view
    (``snapshot.normalized_text.encode("utf-8")``); the routes translate the
    resulting normalized-view indexes back through that snapshot.
    """
    parser = Parser()
    parser.language = get_language("vue")  # type: ignore[operator]
    tree = parser.parse(source_bytes)
    return tree.root_node


# @beacon[
#   id=auto-beacon@_vue_sfc_sections-sfcs,
#   slice_labels=carto-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=THE Vue SFC split: top-level sections with physical-line and original-byte windows translated through SourceSnapshot; the legacy bytes call projects the same split to its original four-key read shape, while snapshot consumers retain exact geometry.,
# ]
def _vue_sfc_sections(snapshot: SourceSnapshot | bytes, root: Any) -> list[dict[str, Any]]:
    """Return the top-level SFC sections of a Vue file, in order of appearance.

    For snapshot input, ``root`` is the tree of its normalized UTF-8 view.
    The legacy bytes call retains a root indexing exactly those supplied bytes.
    Each section is ``{"type", "start_tag", "content_row0", "raw_text",
    "content_start_line", "content_end_line", "content_byte_start",
    "content_byte_end"}``: the element type, the compacted start tag, the
    0-based file row of the content's first line, the content text, and the
    absolute 1-based physical-line plus half-open ORIGINAL-byte window the
    content occupies. Tree-sitter indexes address the normalized UTF-8 view,
    so both byte bounds pass through ``snapshot.normalized_byte_offset``; the
    physical rows are the same in both views. A template without an end tag
    has no bounded content and is skipped, as before. The existing bytes call
    shape remains a read-only compatibility projection with its four original
    keys; production geometry consumers supply the original snapshot.
    """
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

        # Content bounds. A script/style section owns a raw_text child; a
        # template expands its inner HTML into child nodes, so its bounded
        # content runs between the tags.
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
    """The last physical line holding a non-empty content window's bytes."""
    line = start_line
    for index in range(start_line, len(snapshot.byte_offsets)):
        if snapshot.byte_offsets[index] > content_end - 1:
            break
        line = index + 1
    return line


# The one comment grammar each SFC section's scanner call uses: a template
# section supplies HTML-comment (Markdown) forms, a script or style section
# JS forms. The section type is still recorded as the SFC element type.
_VUE_SECTION_GRAMMAR = {
    "template_element": "markdown",
    "script_element": "js_ts",
    "style_element": "js_ts",
}


# @beacon[
#   id=auto-beacon@_vue_section_beacons-sfcb,
#   slice_labels=carto-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=THE per-section Vue structural scan: snapshot callers receive absolute section-clipped records from one lexical-aware scan; legacy section-only callers project the same scanner through bounded local text and rebase read locators, preserving their existing list/diagnostics surface.,
# ]
def _vue_section_beacons(
    section: dict[str, Any], *, snapshot: SourceSnapshot | None = None, diagnostics: list | None = None,
) -> list[dict[str, Any]]:
    """Return one SFC section's structural records from the one shared scanner.

    The section's grammar is chosen by its type (template: HTML-comment /
    Markdown forms; script and style: JS forms) and the scanner is clipped to
    the section's absolute physical-line and original-byte window, so a record
    never crosses into a neighbouring section and no template block is counted
    twice. The returned records are scanner records (``line``, ``fields``,
    ``line_extent``, ``section`` ...), not the projected legacy beacon dicts;
    ``_parse_structural_beacons`` projects them per section. Without a snapshot
    the existing section-only call returns its legacy beacon list, delegating
    entirely to that projector/scanner over the bounded section text. It has
    no original-file byte ownership to advertise. Unknown section types raise
    ``ValueError``, as before.
    """
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


# @beacon[
#   id=auto-beacon@_vue_section_records-vsct,
#   slice_labels=carto-js-ts,carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=THE snapshot Vue route: one SFC split and one scan per actual section in document order; disk parse, outline, extent/close selection and classification select these absolute section-bounded records, not a whole-file dialect parse.,
# ]
def _vue_section_records(
    snapshot: SourceSnapshot, *, root: Any = None,
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """One section split and one scan per section: ``(section, records)`` pairs.

    The route snapshot/geometry consumers start from, so none parses a
    dialect over the whole file. A tree-sitter failure propagates from the
    split; each caller decides what it becomes (the map keeps its Parse Error
    node, the writer helpers refuse the one block).
    """
    if root is None:
        root = _parse_vue_sfc_tree(snapshot.normalized_text.encode("utf-8"))
    return [
        (section, _vue_section_beacons(section, snapshot=snapshot))
        for section in _vue_sfc_sections(snapshot, root)
    ]


def _vue_records_for_lines(lines: list[str]) -> list[dict[str, Any]]:
    """The Vue section route over a writer's physical rows, flattened and total.

    The writer helpers (whole-block extent, close selection, section
    classification) receive rows rather than a snapshot; this observes them
    through ``_source_line_view`` (the one legacy adapter) and returns the same
    absolute section-bounded records the disk parse and the outline read. A
    failed SFC split (for example no tree-sitter) yields no records, so the one
    block is refused locally instead of being located by a whole-file grammar.
    """
    try:
        pairs = _vue_section_records(_source_line_view(lines))
    except Exception:  # noqa: BLE001 -- a split failure refuses the block alone
        return []
    return [record for _section, records in pairs for record in records]


# @beacon[
#   id=auto-beacon@parse_vue_outline,
#   role=ƒ parse_vue_outline(file_path: str),
#   slice_labels=carto-js-ts,ra-snippet-range-ast-js-ts,carto-fwd-source,
#   kind=ast,
#   comment=Builds Vue section nodes from one snapshot and passes its section records into shared script attachment without rescanning the SFC; one whole-file duplicate aggregation remains separate from local source/script diagnostics.,
# ]
def parse_vue_outline(
    file_path: str, *, snapshot: SourceSnapshot | None = None, diagnostics: list | None = None,
) -> List[Dict[str, Any]]:
    """Parse a Vue SFC (.vue) file into deterministic section subnodes.

    Structure:
      - ⦿ <template ...>  (span beacons only, MVP)
      - ⦿ <script ...> / <script setup ...> (AST + span beacons)
      - ⦿ <style ...> (span beacons only, MVP)

    A supplied snapshot is never reopened. The SFC split and each section's
    scan happen once for the file through ``_vue_section_records``, whose
    records already carry absolute file coordinates; script AST nodes are
    built by running the existing JS/TS outline builder against the script
    section's own text with those coordinates threaded through the shared
    context. The template uses HTML-comment grammar; script/style use their
    own bounded JS/C-comment views. No section inherits a neighbour's lexical
    state and no block is counted twice. Script attachment receives these same
    section records and bounds rather than asking for a second SFC split.
    """
    if not _HAVE_TREE_SITTER:
        return [{
            "name": _warn_name("⚠️ Vue parsing not available"),
            "note": (
                "Tree-sitter / tree_sitter_language_pack could not be imported. "
                "Install 'tree_sitter' and 'tree_sitter-language-pack' "
                "in the Windows Python environment to enable .vue Cartographer support."
            ),
            "children": [],
        }]

    try:
        snapshot = snapshot if snapshot is not None else read_source_snapshot(file_path)
    except Exception as e:  # noqa: BLE001
        if diagnostics is not None:
            diagnostics.append({"record": "source_error", "file": file_path, "language": "vue", "exception": str(e)})
        return [{
            "name": _warn_name("⚠️ Parse Error (VUE)"),
            "note": f"failed_to_read_file: {e}",
            "children": [],
        }]

    file_lines = list(snapshot.physical_lines)

    try:
        root = _parse_vue_sfc_tree(snapshot.normalized_text.encode("utf-8"))
    except Exception as e:  # pragma: no cover
        if diagnostics is not None:
            diagnostics.append({"record": "outline_error", "file": file_path, "language": "vue", "exception": str(e)})
        return [{
            "name": _warn_name("⚠️ Parse Error (VUE)"),
            "note": str(e),
            "children": [],
        }]

    # One section route for the whole file: the SFC split once, one scanner
    # call per section, then the ONE legacy projector per section. Every later
    # use in this function reads these records/beacons instead of re-parsing.
    section_beacons: list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    all_beacons: list[dict[str, Any]] = []
    for _section, _records in _vue_section_records(snapshot, root=root):
        _beacons = _parse_structural_beacons(
            _VUE_SECTION_GRAMMAR[_section["type"]], file_lines, records=_records, snapshot=snapshot,
        )
        section_beacons.append((_section, _beacons, _records))
        all_beacons.extend(_beacons)

    # Build section nodes.
    out_nodes: list[dict[str, Any]] = []
    priority = 100

    script_ix = 0
    style_ix = 0

    for blk, beacons_abs, section_records in section_beacons:
        btype = blk.get("type")
        start_tag_compact = str(blk.get("start_tag") or "").strip() or "<unknown>"
        raw_text_str = str(blk.get("raw_text") or "")
        content_row0 = int(blk.get("content_row0") or 0)

        # Section node
        # NOTE: Workflowy treats '<...>' as tag-like markup and may render it as blank.
        # Use square brackets to ensure section headings are visible.
        display_tag = start_tag_compact.replace("<", "[").replace(">", "]")
        section_name = f"⦿ {display_tag}"
        section_note_lines = [
            f"VUE_SECTION: {btype}",
            f"VUE_START_TAG: {start_tag_compact}",
            f"VUE_CONTENT_ROW0: {content_row0}",
        ]

        section_children: list[dict[str, Any]] = []

        if btype == "template_element":
            # Template: HTML-comment beacons (Markdown-style) as SPAN nodes.
            for b in beacons_abs:
                if (b.get("kind") or "").strip().lower() != "span":
                    continue
                bid = (b.get("id") or "").strip()
                role = (b.get("role") or "").strip() or (bid.split("@", 1)[0] if "@" in bid else "")
                slice_labels = _canonicalize_slice_labels((b.get("slice_labels") or "").strip(), role)
                show_span, show_span_explicit = _beacon_show_span(b)

                tag_suffix = _slice_label_tags(slice_labels)
                name = f"🔱 {role or bid or 'vue-template-span'}"
                if tag_suffix:
                    name = f"{name} {tag_suffix}"

                note_lines = [
                    "BEACON (VUE TEMPLATE SPAN)",
                    f"id: {bid}",
                    f"role: {role}",
                    f"slice_labels: {slice_labels}",
                    "kind: span",
                ]
                if show_span_explicit:
                    note_lines.append(f"show_span: {'true' if show_span else 'false'}")
                comment = b.get("comment")
                if comment:
                    note_lines.append(f"comment: {comment}")

                span_node = {"name": name, "note": "\n".join(note_lines), "children": []}
                section_children.append(span_node)

        elif btype == "script_element":
            # Determine language for this script block (ts/tsx vs js).
            lang = "javascript"
            start_tag_low = start_tag_compact.lower()
            if "lang=\"ts\"" in start_tag_low or "lang='ts'" in start_tag_low or "lang=ts" in start_tag_low:
                lang = "typescript"
            if "lang=\"tsx\"" in start_tag_low or "lang='tsx'" in start_tag_low or "lang=tsx" in start_tag_low:
                lang = "tsx"

            vue_prefix = f"vue:script{script_ix}"
            section_note_lines.append(f"VUE_SCRIPT_INDEX: {script_ix}")
            section_note_lines.append(f"VUE_SCRIPT_LANG: {lang}")

            outline_nodes = _parse_js_ts_outline_from_source(
                source_text=raw_text_str,
                file_path=file_path,
                language_name=lang,
                line_offset0=content_row0,
                vue_qual_prefix=vue_prefix,
                diagnostics=diagnostics,
            )

            # Attach JS/TS beacons inside this script block.
            try:
                local_lines = physical_source_lines(raw_text_str)
                # The SFC parent aggregates duplicates once across all its
                # sections; do not manufacture then filter local warnings.
                apply_js_beacons(file_path, file_lines, outline_nodes, beacons_abs, report_duplicates=False,
                                 attachment_structures={"language": "vue", "snapshot": snapshot,
                                                        "records": section_records, "sections": [blk]})
                outline_nodes = group_outline_by_carto_markers(
                    lines=local_lines,
                    outline_nodes=outline_nodes,
                    language="vue_script_js_ts",
                    comment_style="js_ts",
                    line_offset0=content_row0,
                )
            except Exception:
                pass

            section_children.extend(outline_nodes)
            script_ix += 1

        elif btype == "style_element":
            section_note_lines.append(f"VUE_STYLE_INDEX: {style_ix}")

            # Style: CSS comment beacons (block-comment syntax) treated as span-only.
            for b in beacons_abs:
                if (b.get("kind") or "").strip().lower() != "span":
                    continue
                bid = (b.get("id") or "").strip()
                role = (b.get("role") or "").strip() or (bid.split("@", 1)[0] if "@" in bid else "")
                slice_labels = _canonicalize_slice_labels((b.get("slice_labels") or "").strip(), role)
                show_span, show_span_explicit = _beacon_show_span(b)

                tag_suffix = _slice_label_tags(slice_labels)
                name = f"🔱 {role or bid or 'vue-style-span'}"
                if tag_suffix:
                    name = f"{name} {tag_suffix}"

                note_lines = [
                    "BEACON (VUE STYLE SPAN)",
                    f"id: {bid}",
                    f"role: {role}",
                    f"slice_labels: {slice_labels}",
                    "kind: span",
                ]
                if show_span_explicit:
                    note_lines.append(f"show_span: {'true' if show_span else 'false'}")
                comment = b.get("comment")
                if comment:
                    note_lines.append(f"comment: {comment}")

                span_node = {"name": name, "note": "\n".join(note_lines), "children": []}
                section_children.append(span_node)

            style_ix += 1

        # Default: section node
        out_nodes.append(
            {
                "name": section_name,
                "priority": priority,
                "note": "\n".join(section_note_lines),
                "children": section_children,
            }
        )
        priority += 100

    # THE duplicate-id rule, once over every section's beacons: an id used by
    # two sections (script + template) was previously reported nowhere, and a
    # section-local warning would name the same id twice. The ONE builder
    # renders the aggregate at the file top.
    warning_nodes: list[dict[str, Any]] = []
    _prepend_duplicate_beacon_id_warning_nodes(warning_nodes, all_beacons, language="JS/TS")
    out_nodes[:0] = warning_nodes

    return out_nodes


# @beacon-close[
#   id=nmcv1@parser-and-outline-core,
# ]
# SPLIT BOUNDARY:
# Full recursive codebase initialization is valuable and may still belong in Core,
# but if the minimal public surface must shrink, the parsers and F12/update helpers
# matter more than exposing this full directory-walk entrypoint directly.
# @beacon[
#   id=nmcv1@recursive-map-boundary,
#   role=boundary split: full recursive codebase mapping and initialization entrypoint,
#   slice_labels=nexus-portability,nexus-split-boundary,
#   kind=span,
#   show_span=false,
# ]
# Phase 1 JS/TS: directory + single-file dispatcher. This will be extended
# to call the JS/TS outline builder (parse_js_ts_outline) for .js/.ts/.tsx
# files alongside parse_file_outline for Python.
_SOURCE_FILE_KINDS = {
    ".py": ("python", EMOJI_PYTHON),
    ".md": ("markdown", EMOJI_MARKDOWN),
    ".markdown": ("markdown", EMOJI_MARKDOWN),
    ".js": ("js_ts", EMOJI_JS), ".jsx": ("js_ts", EMOJI_JS),
    ".ts": ("js_ts", EMOJI_TS), ".tsx": ("js_ts", EMOJI_TS),
    ".vue": ("vue", EMOJI_VUE), ".sql": ("sql", EMOJI_SQL),
    ".sh": ("shell", EMOJI_SHELL),
    ".bash": ("shell", EMOJI_SHELL),
    ".yml": ("yaml", EMOJI_FILE), ".yaml": ("yaml", EMOJI_FILE),
}


def _source_file_error(
    path: str, error: Exception, *, repo_root: str | None, diagnostics: list | None,
    line_count: int | None = None, record: str = "source_error",
) -> dict[str, Any]:
    """Keep one failed source visible without inventing an empty parse result."""
    language, icon = _SOURCE_FILE_KINDS.get(os.path.splitext(path)[1].lower(), (None, EMOJI_FILE))
    if diagnostics is not None:
        diagnostics.append({"record": record, "file": path, "language": language, "exception": str(error)})
    # Generic binary files were and remain leaves, not failed semantic outlines.
    children = [] if language is None else [{
        "name": _warn_name("⚠️ Parse Error"), "note": str(error), "children": [],
    }]
    return {"name": f"{icon} {os.path.basename(path)}",
            "note": format_file_note(path, line_count=line_count, repo_root=repo_root),
            "children": children}


# @beacon[
#   id=beacon-source@map_source_snapshot,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Maps one immutable source observation entirely in memory: outline, beacon attachment and FILE line count share its BOM-free physical-line view; a failed outline stays a diagnostic node and never an authoritative empty tree.,
# ]
def map_source_snapshot(
    snapshot: SourceSnapshot, repo_root: str | None = None, diagnostics: list | None = None,
) -> Dict[str, Any]:
    """Single-file mapping from supplied bytes, without reopening the source.

    Path identity is supplied by the snapshot. Ignore discovery belongs to the
    path/directory adapter, not this pure mapping seam. Generic files remain
    leaves; supported language failures remain visible diagnostic nodes/data.
    """
    path = snapshot.canonical_path
    language, icon = _SOURCE_FILE_KINDS.get(os.path.splitext(path)[1].lower(), (None, EMOJI_FILE))
    lines = list(snapshot.physical_lines)
    try:
        if language == "python":
            children = parse_file_outline(path, snapshot=snapshot, diagnostics=diagnostics)
        elif language == "markdown":
            children = parse_markdown_structure(path, snapshot=snapshot, diagnostics=diagnostics)
        elif language == "js_ts":
            children = parse_js_ts_outline(path, snapshot=snapshot, diagnostics=diagnostics)
        elif language == "vue":
            children = parse_vue_outline(path, snapshot=snapshot, diagnostics=diagnostics)
        elif language in {"sql", "shell", "yaml"}:
            parser, apply = {"sql": (parse_sql_beacon_blocks, apply_sql_beacons),
                             "shell": (parse_sh_beacon_blocks, apply_sh_beacons),
                             "yaml": (parse_yaml_beacon_blocks, apply_yaml_beacons)}[language]
            beacons = parser(lines, diagnostics=diagnostics)
            children = []
            apply(lines, children, beacons)
        else:
            children = []
    except Exception as error:
        return _source_file_error(path, error, repo_root=repo_root, diagnostics=diagnostics,
                                  line_count=len(lines), record="outline_error")
    return {"name": f"{icon} {os.path.basename(path)}",
            "note": format_file_note(path, line_count=len(lines), repo_root=repo_root),
            "children": children}


# @beacon[
#   id=auto-beacon@map_codebase,
#   role=ƒ map_codebase(root_path: str, include_exts: List[str], exclude_patterns: List[str]),
#   slice_labels=carto-js-ts,nexus-path-resolution-logic,carto-fwd-source,agent-kit,
#   kind=ast,
#   comment=Path adapter for snapshot mapping including markdown and bash aliases and YAML: reads each file once in binary mode retains folder ignore/filter policy and contains decode/outline failures per entry; explicit files remain targetable.,
# ]
def map_codebase(
    root_path: str,
    include_exts: List[str] = None,
    exclude_patterns: List[str] = None,
    *,
    repo_root: str | None = None,
    diagnostics: list | None = None,
) -> Dict[str, Any]:
    """
    Read each source once and delegate to map_source_snapshot. Directory errors
    are entry-local and optionally recorded; a bad file never drops siblings.
    Markdown and shell aliases share their ordinary dispatch, alongside YAML;
    folder ignore rules do not remove an explicitly targeted file.
    Returns a single root NEXUS node representing the codebase tree.

    NOTE: This function is used to initialize a Cartographer codebase map
    via MCP tool calls, but this full-codebase mapping is not considered
    part of the minimal NEXUS Core v1 surface. It remains here for
    convenience and advanced workflows.
    """

    # Load .nexusignore patterns (optional) with ancestor walk.
    #
    # Semantics:
    # - Start at root_path (directory mapping root).
    # - Walk upward, loading any .nexusignore files found.
    # - Stop when we reach repo_root (when provided) or the filesystem root.
    #
    # Patterns are basename-only globs (no path separators).
    ignore_patterns: List[str] | None = None
    ignore_sources: List[str] = []

    # @beacon[
    #   id=auto-beacon@map_codebase._collect_nexusignore_patterns-3h3e,
    #   role=ƒ _collect_nexusignore_patterns(),
    #   slice_labels=nexus-path-resolution-logic,carto-fwd-source,
    #   kind=ast,
    # ]
    def _collect_nexusignore_patterns(
        *,
        start_dir: str,
        stop_dir: str | None,
    ) -> tuple[List[str], List[str]]:
        patterns_out: List[str] = []
        sources_out: List[str] = []

        cur = os.path.normpath(start_dir)
        stop_norm = os.path.normcase(os.path.normpath(stop_dir)) if stop_dir else None

        visited: set[str] = set()
        while cur and cur not in visited:
            visited.add(cur)

            ig_path = os.path.join(cur, ".nexusignore")
            if os.path.isfile(ig_path):
                sources_out.append(ig_path)
                invalid: List[str] = []
                with open(ig_path, "r", encoding="utf-8") as ig:
                    for line in ig:
                        raw = line.rstrip("\n\r")
                        stripped = raw.strip()
                        if not stripped or stripped.startswith("#"):
                            continue
                        if "/" in stripped or "\\" in stripped:
                            invalid.append(stripped)
                            continue
                        patterns_out.append(stripped)

                if invalid:
                    print(
                        "[NEXUS MAP] Ignoring .nexusignore patterns with path separators in "
                        f"{ig_path!r}: {invalid!r}",
                        file=sys.stderr,
                    )

            if stop_norm and os.path.normcase(cur) == stop_norm:
                break

            parent = os.path.dirname(cur)
            if not parent or parent == cur:
                break
            cur = parent

        # De-dupe while preserving order
        seen: set[str] = set()
        deduped: List[str] = []
        for p in patterns_out:
            if p not in seen:
                seen.add(p)
                deduped.append(p)

        return (deduped, sources_out)

    try:
        start_dir = root_path if os.path.isdir(root_path) else os.path.dirname(root_path)
        if start_dir and os.path.isdir(start_dir):
            ignore_patterns, ignore_sources = _collect_nexusignore_patterns(
                start_dir=str(start_dir),
                stop_dir=str(repo_root) if repo_root else None,
            )
            if ignore_patterns:
                print(
                    f"[NEXUS MAP] Using .nexusignore patterns={ignore_patterns!r} from sources={ignore_sources!r}",
                    file=sys.stderr,
                )
            else:
                ignore_patterns = None
    except Exception as e:  # pragma: no cover - safety net
        print(
            f"[NEXUS MAP] Failed to discover .nexusignore via ancestor walk from {root_path!r}: {e}",
            file=sys.stderr,
        )
        ignore_patterns = None

    # @beacon[
    #   id=auto-beacon@map_codebase._is_ignored_name-e2df,
    #   role=map_codebase._is_ignored_name,
    #   slice_labels=carto-fwd-source,
    #   kind=ast,
    # ]
    def _is_ignored_name(name: str) -> bool:
        if name == "__pycache__":
            return True
        if not ignore_patterns:
            return False
        # Simple fnmatch-style globbing over basenames (no path semantics).
        import fnmatch

        for pat in ignore_patterns:
            if fnmatch.fnmatch(name, pat):
                return True
        return False

    # Portability: descendant FILE/FOLDER nodes should usually store relative Path:
    # values. When repo_root is supplied, we use it; otherwise, for directory maps
    # we default to root_path as the note root.
    repo_root_for_notes: str | None = None
    if repo_root and isinstance(repo_root, str):
        repo_root_for_notes = repo_root
    elif os.path.isdir(root_path):
        repo_root_for_notes = root_path

    def map_path(path: str) -> Dict[str, Any]:
        try:
            return map_source_snapshot(read_source_snapshot(path), repo_root=repo_root_for_notes,
                                       diagnostics=diagnostics)
        except Exception as error:
            return _source_file_error(path, error, repo_root=repo_root_for_notes, diagnostics=diagnostics)

    # @beacon[
    #   id=auto-beacon@map_codebase.scan_dir-k123,
    #   role=ƒ scan_dir(current_path: str),
    #   slice_labels=carto-fwd-source,
    #   kind=ast,
    #   comment=Walks one directory with independent item containment: a read/decode/parse failure adds its own diagnostic node and never skips later files or subdirectories.,
    # ]
    def scan_dir(current_path: str) -> List[Dict[str, Any]]:
        """Contain each directory entry independently, not the entire loop."""
        nodes = []
        try:
            items = sorted(os.listdir(current_path))
        except Exception as error:
            if diagnostics is not None:
                diagnostics.append({"record": "source_error", "file": current_path,
                                    "language": None, "exception": str(error)})
            return [{"name": _warn_name("⚠️ Parse Error"), "note": str(error), "children": []}]
        for item in items:
            full_path = os.path.join(current_path, item)
            try:

                # Ignore hidden/system dirs
                if item.startswith('.') or item == "__pycache__":
                    continue

                # Apply .nexusignore semantics (directories and files)
                if _is_ignored_name(item):
                    continue

                if exclude_patterns and any(pat in item for pat in exclude_patterns):
                    continue

                if os.path.isdir(full_path):
                    # Directories are filtered only by exclude_patterns and .nexusignore;
                    # include_exts apply to files, not folders.
                    children = scan_dir(full_path)
                    if children:  # Only add non-empty directories
                        folder_note = format_file_note(full_path, repo_root=repo_root_for_notes)
                        nodes.append({
                            "name": f"{EMOJI_FOLDER} {item}",
                            "note": folder_note,
                            "children": children,
                        })

                elif os.path.isfile(full_path):
                    # Extension Check for files only
                    _, ext = os.path.splitext(item)
                    ext = ext.lower()

                    if include_exts and ext not in include_exts:
                        continue

                    nodes.append(map_path(full_path))
            except Exception as error:
                nodes.append(_source_file_error(full_path, error, repo_root=repo_root_for_notes,
                                                diagnostics=diagnostics))

        return nodes

    root_name = os.path.basename(root_path) or "Source Code"
    if os.path.isfile(root_path):
        return map_path(root_path)
    
    # Directory mode
    return {
        "name": f"🗺️ Source Map: {root_name}",
        "note": f"Root: {repo_root_for_notes or root_path}",
        "children": scan_dir(root_path)
    }

# @beacon-close[
#   id=nmcv1@recursive-map-boundary,
# ]
# CORE V1 KEEPER:
# Multi-language beacon parsers plus update_beacon_from_node_* helpers are a core
# part of the F12 experience. Keep these with Cartographer so per-node beacon edits
# remain language-aware and machine-portable.
# @beacon[
#   id=nmcv1@multilang-update-core,
#   role=core keeper: multi-language beacon parsing and update-from-node helpers,
#   slice_labels=nexus-portability,nexus-core-v1,
#   kind=span,
#   show_span=false,
# ]
# @beacon[
#   id=auto-beacon@parse_sql_beacon_blocks-xpb1,
#   role=parse_sql_beacon_blocks,
#   slice_labels=ra-reconcile,agent-kit-vendor,
#   kind=ast,
#   comment=Thin delegate to _parse_structural_beacons: reads SQL '--' comment blocks through the shared bounded scanner and exact pairs; the optional keyword-only diagnostics list passes through unchanged.,
# ]
def parse_sql_beacon_blocks(
    lines: list[str], *, diagnostics: list | None = None
) -> list[dict[str, Any]]:
    """Read sql metadata through the shared bounded scanner and exact pairs."""
    return _parse_structural_beacons("sql", lines, diagnostics)


# @beacon[
#   id=carto-js-ts@parse_js_beacon_blocks,
#   role=carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,
#   kind=span,
# ]
# Phase 2 JS/TS: JS/TS beacon parser.
# Supports both line-comment (//) and block-comment (/* ... */) forms
# with the same metadata schema as Python/Markdown beacons.
# @beacon[
#   id=auto-beacon@parse_js_beacon_blocks-czo0,
#   role=parse_js_beacon_blocks,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=JS/TS compatibility projection of the shared bounded comment scanner and exact pairs; line/C/JSDoc ownership and incomplete fields use the same records as extents.,
# ]
def parse_js_beacon_blocks(
    lines: list[str], *, diagnostics: list | None = None
) -> list[dict[str, Any]]:
    """Read JS/TS line/C-style metadata through the shared structural scanner."""
    return _parse_structural_beacons("js_ts", lines, diagnostics)


# @beacon[
#   id=carto-js-ts@parse_sh_beacon_blocks,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
#   kind=span,
# ]
# Phase 2 JS/TS: shell beacon parser example.
# Another line-comment @beacon[...] parser useful for cross-checking
# JS/TS behavior.
# @beacon[
#   id=auto-beacon@parse_sh_beacon_blocks-7qwa,
#   role=parse_sh_beacon_blocks,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Thin delegate to _parse_structural_beacons: reads shell '#' comment blocks through the shared bounded scanner and exact pairs; the optional keyword-only diagnostics list passes through unchanged.,
# ]
def parse_sh_beacon_blocks(
    lines: list[str], *, diagnostics: list | None = None
) -> list[dict[str, Any]]:
    """Read shell metadata through the shared bounded scanner and exact pairs."""
    return _parse_structural_beacons("shell", lines, diagnostics)


# @beacon[
#   id=carto-js-ts@parse_yaml_beacon_blocks,
#   role=carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,
#   kind=span,
# ]
# @beacon[
#   id=auto-beacon@parse_yaml_beacon_blocks-ku8p,
#   role=parse_yaml_beacon_blocks,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Thin delegate to _parse_structural_beacons: reads YAML '#' comment blocks through the shared bounded scanner and exact pairs; the optional keyword-only diagnostics list passes through unchanged.,
# ]
def parse_yaml_beacon_blocks(
    lines: list[str], *, diagnostics: list | None = None
) -> list[dict[str, Any]]:
    """Read yaml metadata through the shared bounded scanner and exact pairs."""
    return _parse_structural_beacons("yaml", lines, diagnostics)


# @beacon[
#   id=auto-beacon@_extract_sql_beacon_context-9w82,
#   role=ƒ _extract_sql_beacon_context(lines: list[str], comment_line: int, beacon_id: Optional[str]),
#   slice_labels=ra-reconcile,ra-reconcile,carto-fwd-source,
#   kind=ast,
#   comment=Thin delegate to the shared context view/collector: SQL comment masking, recorded metadata ranges and the paired close all come from one structural scan, and beacon_id is kept for call compatibility only, never as a pairing grammar.,
# ]
def _extract_sql_beacon_context(
    lines: list[str],
    comment_line: int,
    beacon_id: Optional[str] = None,
    *,
    records: list | None = None,
) -> List[str]:
    """Extract nearby SQL comments (non-beacon) around a SQL span beacon.

    Thin delegate to the shared comment view and collector. The SQL comment
    mask/cleaning (``--`` lines and ``/* ... */`` regions), the recorded
    metadata ranges and the paired close all come from the same structural
    records as the beacon scan: no private close scan, id grammar or metadata
    stripping remains here. ``beacon_id`` is kept only for positional call
    compatibility. Supplying ``records`` (one scan for a whole file) avoids
    rescanning structure per beacon.
    """
    records, mask, cleaned = _beacon_context_view(lines, "sql", records=records)
    return _collect_beacon_context(lines, comment_line, records, mask, cleaned)


# @beacon[
#   id=carto-js-ts@apply_sql_beacons,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
#   kind=span,
# ]
# Phase 2 JS/TS: SQL beacon attachment example.
# Secondary template for attaching span-only beacons as children
# under a FILE node for non-AST languages.
# @beacon[
#   id=auto-beacon@apply_sql_beacons-0rsg,
#   role=apply_sql_beacons,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Attaches SQL span nodes; one shared structural scan per file feeds every span beacon's context helper, so no per-beacon rescan and no private close/id grammar.,
# ]
def apply_sql_beacons(
    lines: list[str],
    file_children: List[Dict[str, Any]],
    beacons: list[dict[str, Any]],
) -> None:
    """Attach SQL span beacons as child nodes under the file node.

    For now, SQL beacons are purely location-centric annotations on the file.
    We do not compute or store explicit line ranges; future tools can re-scan
    the file to recover spans if needed. One shared structural scan per file
    supplies the context records for every span node.
    """
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

        # Defaults: derive role from id prefix (before '@')
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

        # Raw SQL span text between opener/closer (if Cartographer computed it)
        span_text_lines: List[str] = []
        span_start = beacon.get("span_lineno_start")
        span_end = beacon.get("span_lineno_end")
        if isinstance(span_start, int) and isinstance(span_end, int):
            span_start = max(1, span_start)
            span_end = min(n, span_end)
            if span_start <= span_end:
                span_text_lines = lines[span_start - 1 : span_end]
                # Trim leading/trailing blank lines inside the span
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


# @beacon[
#   id=auto-beacon@_extract_sh_beacon_context-s7r8,
#   role=ƒ _extract_sh_beacon_context(lines: list[str], comment_line: int, beacon_id: Optional[str]),
#   slice_labels=ra-reconcile,ra-reconcile,carto-fwd-source,
#   kind=ast,
#   comment=Thin delegate to the shared context view/collector: shell '#' comment masking, recorded metadata ranges and the paired close all come from one structural scan, and beacon_id is kept for call compatibility only, never as a pairing grammar.,
# ]
def _extract_sh_beacon_context(
    lines: list[str],
    comment_line: int,
    beacon_id: Optional[str] = None,
    *,
    records: list | None = None,
) -> List[str]:
    """Extract nearby shell comments (non-beacon) around a shell @beacon block.

    Thin delegate to the shared comment view and collector. The shell '#'
    comment mask/cleaning, the recorded metadata ranges and the paired close
    all come from the same structural records as the beacon scan: no private
    close scan, id grammar, hash-prefix stripping or metadata stripping
    remains here. ``beacon_id`` is kept only for positional call
    compatibility. Supplying ``records`` (one scan for a whole file) avoids
    rescanning structure per beacon.
    """
    records, mask, cleaned = _beacon_context_view(lines, "shell", records=records)
    return _collect_beacon_context(lines, comment_line, records, mask, cleaned)


# @beacon[
#   id=carto-js-ts@apply_sh_beacons,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
#   kind=span,
# ]
# Phase 2 JS/TS: shell beacon attachment example.
# Secondary template similar to SQL for span-only beacons.
# @beacon[
#   id=auto-beacon@_prepend_duplicate_beacon_id_warning_nodes-f830,
#   role=_prepend_duplicate_beacon_id_warning_nodes,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Prepends one 🚨 warning node per duplicated beacon id at the top of a file's children; the duplicate rule itself is _duplicate_beacon_ids (extracted, shared with the beacon check).,
# ]
def _prepend_duplicate_beacon_id_warning_nodes(
    children: List[Dict[str, Any]],
    beacons: list[dict[str, Any]],
    *,
    language: str,
) -> None:
    """Prepend warning nodes when duplicate beacon IDs appear in a single file.
    This is a UX guardrail. Duplicate beacon ids (often due to a mistaken
    `@beacon[` opener where `@beacon-close[` was intended) cause ambiguous
    id-based matching during F12 refresh.
    We emit an explicit disk-derived diagnostic warning node so the user sees
    the problem at the top of the file subtree. The duplicate rule is
    ``_duplicate_beacon_ids``.
    """
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
                    "- F12 refresh matches nodes by beacon id.\n"
                    "- Duplicate ids create ambiguous matches and may cause reorder storms.\n\n"
                    "Fix:\n"
                    "- Make beacon ids unique (or correct @beacon-close blocks).\n"
                    "- Then press F12 again."
                ),
                "children": [],
                "_position_hint": "top",
            }
        )
    # Insert at the TOP so it is immediately visible in Workflowy.
    children[:0] = warning_nodes


# @beacon[
#   id=auto-beacon@_duplicate_beacon_ids-dupi,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=THE duplicate-id rule of one file: the non-empty stripped ids seen two or more times among the parsed blocks, sorted. Extracted from the refresh's duplicate warning builder, which calls it; the beacon check calls the same function.,
# ]
def _duplicate_beacon_ids(beacons: list[dict[str, Any]]) -> list[str]:
    """Return ``sorted()`` of the non-empty stripped beacon ids that occur two
    or more times in ``beacons`` (each listed once)."""
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
# @beacon[
#   id=auto-beacon@apply_sh_beacons-chdk,
#   role=apply_sh_beacons,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Attaches shell span nodes; one shared structural scan per file feeds every span beacon's context helper, so no per-beacon rescan and no private close/id grammar.,
# ]
def apply_sh_beacons(
    lines: list[str],
    file_children: List[Dict[str, Any]],
    beacons: list[dict[str, Any]],
) -> None:
    """Attach shell span beacons as child nodes under the file node.

    Behavior mirrors SQL: purely location-centric annotations; explicit spans
    are left to snippet tools. One shared structural scan per file supplies
    the context records for every span node.
    """
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

        # Raw shell span text between opener/closer (if Cartographer computed it)
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


# @beacon[
#   id=carto-js-ts@apply_yaml_beacon_context,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
#   kind=span,
# ]
# @beacon[
#   id=auto-beacon@_extract_yaml_beacon_context-i1o0,
#   role=_extract_yaml_beacon_context,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Thin delegate to the shared context view/collector: YAML '#' comment masking, recorded metadata ranges and the paired close all come from one structural scan, and beacon_id is kept for call compatibility only, never as a pairing grammar.,
# ]
def _extract_yaml_beacon_context(
    lines: list[str],
    comment_line: int,
    beacon_id: Optional[str] = None,
    *,
    records: list | None = None,
) -> List[str]:
    """Extract nearby YAML comments (non-beacon) around a YAML @beacon block.

    Thin delegate to the shared comment view and collector (the semantics
    formerly mirrored from ``_extract_sh_beacon_context``). The YAML '#'
    comment mask/cleaning, the recorded metadata ranges and the paired close
    all come from the same structural records as the beacon scan: no private
    close scan, id grammar, hash-prefix stripping or metadata stripping
    remains here. ``beacon_id`` is kept only for positional call
    compatibility. Supplying ``records`` (one scan for a whole file) avoids
    rescanning structure per beacon.
    """
    records, mask, cleaned = _beacon_context_view(lines, "yaml", records=records)
    return _collect_beacon_context(lines, comment_line, records, mask, cleaned)


# @beacon[
#   id=carto-js-ts@apply_yaml_beacons,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
#   kind=span,
# ]
# YAML beacon attachment example.
# Span-only beacons attached as children under a FILE node for YAML/Helm configs.
# @beacon[
#   id=auto-beacon@apply_yaml_beacons-gi3p,
#   role=apply_yaml_beacons,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Attaches YAML span nodes; one shared structural scan per file feeds every span beacon's context helper, so no per-beacon rescan and no private close/id grammar.,
# ]
def apply_yaml_beacons(
    lines: list[str],
    file_children: List[Dict[str, Any]],
    beacons: list[dict[str, Any]],
) -> None:
    """Attach YAML span beacons as child nodes under the file node.

    Behavior mirrors SQL/SH: purely location-centric annotations; explicit
    spans are left to snippet tools. One shared structural scan per file
    supplies the context records for every span node.
    """
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

        # Raw YAML span text between opener/closer (if Cartographer computed it)
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


# @beacon[
#   id=carto-js-ts@apply_js_beacons,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-snippet-range-ast-beacon-js-ts,ra-reconcile,carto-fwd-source,
#   kind=span,
# ]
# Phase 2 JS/TS: JS/TS beacon attachment.
# Decorates JS/TS AST nodes (kind=ast) and attaches span children
# (kind=span) similarly to Python beacons.
# @beacon[
#   id=auto-beacon@apply_js_beacons-yaw6,
#   role=apply_js_beacons,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Renders JS/TS AST and span beacons through the shared attachment index with real comment state and owned decorators; ordinary callers report duplicates here while Vue delegates one aggregation to its SFC parent.,
# ]
def apply_js_beacons(
    file_path: str,
    lines: list[str],
    outline_nodes: List[Dict[str, Any]],
    beacons: list[dict[str, Any]],
    *, report_duplicates: bool = True, attachment_structures: Optional[dict] = None,
) -> None:
    """Render shared physical attachments, context and optional SFC aggregation.

    Generator methods are code; only real comments and syntax-owned decorators
    grant leading-block gap permission. AST target decisions are not repeated
    by this display pass.
    """
    if not beacons:
        return

    # NOTE: outline_nodes may be empty for "flat" modules (no class/function/method
    # declarations). We still want to attach SPAN beacons at file-level in that case.
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

    # @beacon[
    #   id=carto-beacon-order@js-node-source-order-line,
    #   role=ƒ _node_source_order_line(node),
    #   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
    #   kind=ast,
    # ]
    def _node_source_order_line(node: dict[str, Any]) -> int:
        cached = span_sort_lines.get(id(node))
        if isinstance(cached, int):
            return cached
        ln = node.get("orig_lineno_start_unused") if isinstance(node, dict) else None
        if isinstance(ln, int):
            return ln
        return 10**9

    # @beacon[
    #   id=carto-beacon-order@js-insert-child-in-source-order,
    #   role=ƒ _insert_child_in_source_order(siblings, child_node, sort_line: int),
    #   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,carto-fwd-source,
    #   kind=ast,
    # ]
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

    # @beacon[
    #   id=auto-beacon@apply_js_beacons._extract_js_beacon_context-lls9,
    #   role=apply_js_beacons._extract_js_beacon_context,
    #   slice_labels=carto-fwd-source,
    #   kind=ast,
    #   comment=Collects JS/TS span-adjacent comments from recorded metadata ranges and shared lexical evidence, retaining ordinary mixed-comment bytes without a private delimiter or close scan.,
    # ]
    def _extract_js_beacon_context(comment_line: int) -> List[str]:
        """Preserve JS adjacency policy after subtracting recorded metadata."""
        return _collect_beacon_context(lines, comment_line, context_records, context_mask, context_cleaned, stop_at_pair=False)

    # Attachment and implicit promotion belong to the shared authority.
    for beacon in beacons:
        attached = attachment_by_line.get(beacon.get("comment_line"))
        if attached:
            beacon["kind"] = attached["effective_kind"]
            beacon["_anchor_lineno"] = attached["next_code_line"]

    # Pass 1: AST beacons
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

        # Merge beacon metadata into note. For AST beacons, we rely on the
        # AST node itself to carry CONTEXT COMMENTS (JS/TS) (emitted by
        # make_note via _extract_js_ast_comment_context), so we do NOT also
        # append a CONTEXT COMMENTS (JS/TS) section in the BEACON block here.
        # Doing so used to produce duplicate, overlapping context-comments
        # sections in the AST node's note. (Python's apply_python_beacons
        # AST-beacon pass has the same intentional omission for the same
        # reason.)
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
            # Keep the identifier (AST_QUALNAME) at the top, then insert the
            # BEACON metadata block, then the rest of the content.
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

    # Pass 2: SPAN beacons
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
            # Snippet-less spans attach based on beacon location so root-level
            # documentation spans stay at file scope instead of slipping under
            # the next AST node.
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

        # Raw JS/TS span text between @beacon[...] and @beacon-close[...] (if available)
        span_start = beacon.get("span_lineno_start")
        span_end = beacon.get("span_lineno_end")
        span_text_lines: List[str] = []
        if isinstance(span_start, int) and isinstance(span_end, int):
            span_start = max(1, span_start)
            span_end = min(n, span_end)
            if span_start <= span_end:
                span_text_lines = lines[span_start - 1 : span_end]
                # Trim leading/trailing blank lines inside the span
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


# @beacon[
#   id=carto-js-ts@split_name_and_tags,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,nexus--tags,
#   kind=span,
# ]
# Utility: split a Workflowy node name into base text and trailing #tags.
# Used by update_beacon_from_node_* helpers to derive slice_labels from
# name suffixes while keeping the canonical AST/beacon node name clean.
# @beacon[
#   id=auto-beacon@split_name_and_tags-t17m,
#   role=split_name_and_tags,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _token_is_pure_decoration(token: str) -> bool:
    """True iff ``token`` is a pure trailing-region DECORATION glyph run.

    Decoration = the Cartographer trident (\U0001F531 🔱), the marker/anchor
    emoji family (\U0001FA9A 🪚 and friends), and any token that contains NO
    alphanumeric character at all (i.e. it is entirely emoji / symbol /
    punctuation). This is what lets the trailing-tag walker treat a
    ``base 🔱 #tag`` region as ``base`` + ``[#tag]`` (trident absorbed as a
    delimiter, NOT left glued onto the base) -- the fix for the piled-up
    trident bug where two parsers disagreed on where the base ends and the
    reconcile writer kept re-adding a trident onto a base that still carried
    one.

    A token that mixes alphanumerics with emoji (e.g. real content words, or a
    heading that legitimately ends in a word) is NOT decoration and is left in
    the base.
    """
    if not token:
        return False
    return not any(ch.isalnum() for ch in token)


# @beacon[
#   id=agent-kit@split_name_and_tags,
#   slice_labels=agent-kit-vendor,
#   kind=ast,
#   comment=The one name splitter: peels the trailing tag region (and any leading decoration) off a mapped node name, shared by NEXUS's selection and the repository view's titles; an agent-kit build root,
# ]

def split_name_and_tags(raw_name: str) -> tuple[str, list[str]]:
    name = (raw_name or "").strip()
    if not name:
        return "", []
    tokens = name.split()
    tags: list[str] = []
    # Walk from the end, collect tokens that look like tags (start with '#').
    # Workflowy may expose rendered tags through /nodes-export with visual
    # underline markup embedded in the NAME (e.g. '#<u>tag</u>' or
    # '#tag<u>-🏷️</u>'). That markup is not semantic tag content, so strip it
    # before deciding whether the token is a trailing #tag and before passing
    # it to slice-label canonicalization.
    #
    # EMOJI/TRIDENT-AWARE TRAILING REGION (Dan, 2026 -- the piled-up-trident
    # fix). The trailing tag REGION is a contiguous run of (a) '#'-tags and
    # (b) pure-decoration glyph tokens (the trident 🔱, marker emoji, or any
    # non-alphanumeric-only token) that sits AMONG / BESIDE those tags. We peel
    # that whole run, but:
    #   * ``tags`` returns ONLY the '#'-tags -- NEVER any emoji/trident. The
    #     trident is presentation the normalizer re-synthesizes on write; it
    #     must never survive as tag content.
    #   * a trailing decoration token is peeled ONLY when the region also
    #     contains at least one real '#'-tag OR a trident/marker glyph. A bare
    #     trailing CONTENT emoji with no tags in the run (e.g. 'Ship it 🚀',
    #     'Done ✅') is LEFT in the base -- we must not silently eat a
    #     deliberate emoji suffix on a heading. (Adjacency rule: decoration is
    #     only decoration when it's decorating a tag/trident region.)
    # A leading run of decoration/trident on the base is NOT touched here (that
    # is a separate concern handled by the name normalizer); this function owns
    # only the TRAILING region.
    #
    # ALGORITHM (single right-to-left scan; simple + obviously correct):
    # Walk tokens from the end. Track ``region_start`` = the index of the
    # last CONFIRMED trailing-region token (a '#'-tag, the trident, or a
    # marker emoji). Pure-decoration tokens that are NOT markers (generic
    # non-alnum tokens) are provisionally skipped but only COMMITTED to the
    # region if a confirmed tag/marker is found deeper toward the base. A
    # real content token ends the scan. At the end, the region is
    # ``tokens[region_start:]``; if nothing was confirmed, there is no
    # trailing region and the whole name is the base (content-emoji safety:
    # 'Ship it 🚀' has no tag/marker, so 🚀 stays in the base).
    # Use the module-global TRIDENT constant (defined above) -- never a local
    # literal -- so the glyph has exactly one home in this file.
    trident = TRIDENT

    def _is_marker_decoration(tok: str) -> bool:
        t = tok.strip()
        return t == trident or t.startswith("\U0001FA9A")

    region_start = len(tokens)  # exclusive-of-region default (no region yet)
    i = len(tokens) - 1
    while i >= 0:
        candidate = _strip_workflowy_tag_underline_markup(tokens[i])
        if candidate.startswith("#") or _is_marker_decoration(candidate):
            # Confirmed region member: the region extends at least to here,
            # AND everything between here and the previously-confirmed start
            # (any provisional generic decoration we skipped) is now committed.
            region_start = i
            i -= 1
            continue
        if _token_is_pure_decoration(candidate):
            # Provisional: part of the region ONLY if a confirmed tag/marker
            # lies deeper. Keep scanning without moving region_start; a later
            # confirmed member will pull region_start past this token.
            i -= 1
            continue
        # Real content token -> the trailing region ends here.
        break

    if region_start >= len(tokens):
        # No confirmed tag/marker anywhere in the trailing run -> no region.
        return name, []
    base = " ".join(tokens[:region_start]).strip()
    for tok in tokens[region_start:]:
        candidate = _strip_workflowy_tag_underline_markup(tok)
        if candidate.startswith("#"):
            tags.append(candidate)
        # decoration tokens (trident / markers / generic emoji) are DISCARDED
        # -- never returned as tags (presentation, re-synthesized on write).
    return base, tags


# @beacon[
#   id=beacon-hygiene@plan_beacon_comment_aliases,
#   slice_labels=beacon-writeback,carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Pure alias verdicts shared by check and kit consume scanner occurrences from one snapshot and plan only each unambiguous comments key's s; incomplete or uncertain records and canonical/alias collisions retain candidates while independent records proceed.,
# ]
def plan_beacon_comment_aliases(snapshot: SourceSnapshot, language: str) -> dict:
    """Share alias verdicts and field-token plans, without IO or execution.

    Literal examples are invisible to U01. An empty scanner selection records
    an informational no_recognized_alias skip, not an integrity finding or a
    fabricated literal candidate. Refusals are per record, never per file.
    """
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


# @beacon[
#   id=auto-beacon@normalize_beacon_comment_field_aliases_in_file-9q2m,
#   role=ƒ normalize_beacon_comment_field_aliases_in_file(file_path: str),
#   slice_labels=ra-reconcile,carto-js-ts-beacons,carto-fwd-source,
#   kind=ast,
#   comment=Optional alias normalization observes once and delegates selection to the pure shared-scanner planner; publishes one-byte key deletions through U02 and returns committed occurrences or local skips/findings without gating other repairs or mapping.,
# ]
def normalize_beacon_comment_field_aliases_in_file(file_path: str) -> Dict[str, Any]:
    """Rename only scanner-owned unambiguous comments field tokens.

    One snapshot feeds the pure planner and U02 publication. There is no open
    line mode: code, literals and incomplete tails cannot select bytes. Old
    keys/signature remain; structured findings await protected U09/J projection.
    """
    result = {"success": True, "changed": False, "file_path": file_path,
              "before_sha256": None, "after_sha256": None, "edited_ranges": [],
              "events": [], "skipped_aliases": [], "findings": [], "diagnostics": []}
    try:
        snapshot = read_source_snapshot(file_path)
        result.update(before_sha256=snapshot.sha256, after_sha256=snapshot.sha256)
        ext = os.path.splitext(file_path)[1].lower()
        language = {".py": "python", ".sh": "shell", ".bash": "shell", ".yml": "yaml", ".yaml": "yaml",
                    ".sql": "sql", ".js": "js_ts", ".jsx": "js_ts", ".ts": "js_ts", ".tsx": "js_ts",
                    ".vue": "vue", ".md": "markdown", ".markdown": "markdown"}.get(ext)
        if language is None:
            return {**result, "reason": "unsupported_ext"}
        planned = plan_beacon_comment_aliases(snapshot, language)
        result.update(skipped_aliases=list(planned["skipped_aliases"]), diagnostics=planned["diagnostics"])
        result["findings"] = [s for s in planned["skipped_aliases"] if s["reason"] != "no_recognized_alias"]
        if not planned["edits"]:
            return result
        committed = commit_beacon_edit_plan(planned["edit_plan"], scanner=_beacon_edit_records)
        result.update(committed)
        if committed["success"]:
            result["events"] = [{**row, "status": "committed", "after_sha256": committed["after_sha256"]}
                                for row in planned["applied_aliases"]]
        else:
            result.update(after_sha256=None, edited_ranges=[])
            # One skipped record, even if it had several identical aliases.
            for owner in planned["edit_plan"].target_witness:
                row = next(r for r in planned["applied_aliases"] if r["owner"] == owner)
                finding = {**row, "status": "skipped", "reason": committed.get("error_kind", "alias_failed"),
                           "file_path": file_path, "error": committed.get("error", "Alias repair failed"),
                           "candidates": row["members"], "commit_evidence": committed}
                result["skipped_aliases"].append(finding)
                result["findings"].append(finding)
    except Exception as error:  # noqa: BLE001
        kind = "not_utf8" if isinstance(error, UnicodeDecodeError) else "io_error" if isinstance(error, OSError) else "alias_failed"
        finding = {"operation": "rename_comment_alias", "reason": kind, "file_path": file_path,
                   "error": str(error), "candidates": []}
        result.update(success=False, changed=False, error=str(error), error_kind=kind, after_sha256=None,
                      edited_ranges=[], events=[])
        result["findings"].append(finding)
        result["skipped_aliases"].append(finding)
    return result


# @beacon[
#   id=auto-beacon@_indent_beacon_block-6lcl,
#   role=_indent_beacon_block,
#   slice_labels=carto-fwd-source,agent-kit-vendor,
#   kind=ast,
# ]
def _indent_beacon_block(block_lines: list[str], source_lines: list[str], insert_idx: int) -> list[str]:
    """Indent beacon block to match the following non-blank line's indentation.
    
    Args:
        block_lines: The beacon comment block lines (unindented).
        source_lines: The full source file lines array.
        insert_idx: The line index where the beacon will be inserted.
    
    Returns:
        The beacon block with indentation applied.
    """
    # Scan forward from insert_idx to find first non-blank line.
    indent = ""
    for idx in range(insert_idx, len(source_lines)):
        line = source_lines[idx]
        if line.strip():
            # Measure leading whitespace.
            indent = line[:len(line) - len(line.lstrip())]
            break
    # Apply indent to every line in the block.
    return [indent + line for line in block_lines]


# @beacon[
#   id=auto-beacon@_generate_auto_beacon_hash-22qn,
#   role=_generate_auto_beacon_hash,
#   slice_labels=carto-fwd-source,beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Samples one four-character suffix; the shared explicit-taken-set allocator owns collision checks and finite fallback.,
# ]
def _generate_auto_beacon_hash() -> str:
    """Sample a suffix; allocate_beacon_id owns collision checks and termination.
    
    Returns a 4-character lowercase alphanumeric string.
    """
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


# @beacon[
#   id=beacon-writeback@allocate_beacon_id,
#   slice_labels=beacon-writeback,carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Allocates against an explicit file+planned id set without IO or mutating it; preserves random four/eight-character and plain kit conventions, then uses a deterministic finite numeric fallback even if every random draw repeats.,
# ]
def allocate_beacon_id(taken_ids, *, base_id: str, random_parts: int = 0) -> str:
    """Allocate a new id, never validate or replace a caller-supplied id.

    ``taken_ids`` includes every current record (complete or not, all sections)
    and already planned creates. Callers retain returned ids in their own plan
    set. ``base_id`` includes the random separator, if any: code uses
    ``auto-beacon@qualname-`` with one part, Markdown ``auto-beacon@`` with two,
    and the kit ``label@qualname`` with none. No file/repository read occurs.
    Four random attempts retain today's convention; zero-token fallback and
    then numeric suffixes depend only on the inputs, not the exhausted draws.
    """
    taken = set(taken_ids)
    if random_parts:
        for _attempt in range(4):
            candidate = base_id + "".join(_generate_auto_beacon_hash() for _ in range(random_parts))
            if candidate not in taken:
                return candidate
    fallback = base_id + "0000" * random_parts
    if fallback not in taken:
        return fallback
    # At most len(taken) candidates can be occupied. This finite range always
    # contains a free suffix, including when a patched random source repeats.
    for suffix in range(2, len(taken) + 3):
        candidate = f"{fallback}-{suffix}"
        if candidate not in taken:
            return candidate


# ---------------------------------------------------------------------------
# Lightning Rod beacon mutation -- PLAN SEAM disk helpers (SPEC v5 §5)
# ---------------------------------------------------------------------------
#
# PURE readers only: these helpers NEVER write to disk (invariant 5 --
# zero preliminary writes) and NEVER call a parser entry point that may
# rewrite its input (§7: parse_markdown_structure is forbidden on this path).


# @beacon[
#   id=auto-beacon@_parse_disk_beacons_for_language-pdsk,
#   role=_parse_disk_beacons_for_language(language, source_path),
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Compatibility disk read from one binary snapshot; delegates parsing to the pure snapshot seam then retains the legacy BOM/Markdown-sanitized row view for callers not yet migrated to byte edits.,
# ]
def _parse_disk_beacons_for_language(
    language: str, source_path: str, *, diagnostics: list | None = None
) -> tuple[list[str], list[dict]]:
    """Read + parse a file's beacon blocks PURELY (no writes, no mutating parsers).

    Returns ``(lines, beacons)``. The Markdown control-char sanitize is
    IN-MEMORY ONLY on this path (SPEC v5 §7 zero-preliminary-writes), and
    ``parse_markdown_structure`` is not called here.

    ``diagnostics``, when a list, is passed to the language's block parser;
    for ``vue`` it receives each section's projected block/close records.

    Every file is read once in binary mode: the parse view is the source
    snapshot and ``lines`` is the writer's physical text view, in which a
    leading BOM stays a character of row 1 so a later same-bytes write keeps
    it. Each actual SFC section is scanned once by ``_vue_section_beacons``
    and projected by the one legacy projector, so a template block is never
    counted twice and no beacon record crosses a section. An SFC split
    failure propagates as a typed error: callers refuse the file, item-local.
    """
    if language not in {"python", "js_ts", "markdown", "sql", "shell", "yaml", "vue"}:
        raise ValueError(f"unknown beacon language {language!r}")
    snapshot = read_source_snapshot(source_path)
    lines = list(snapshot.physical_lines)
    if language == "markdown":
        lines = _markdown_source_lines(_strip_disallowed_markdown_control_chars(snapshot.parse_text))
    if snapshot.had_bom and lines:
        lines[0] = "\ufeff" + lines[0]
    return _parse_snapshot_beacons_for_language(snapshot, language, diagnostics=diagnostics, line_view=lines)


# @beacon[
#   id=beacon-writeback@_beacon_edit_records,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Projects the shared scanner records for byte edits, retaining actual Vue section geometry.,
# ]
def _beacon_edit_records(snapshot: SourceSnapshot, language: str) -> list[dict]:
    """Supply the one scanner's original-byte records, including Vue sections."""
    if language == "vue":
        return [record for _section, records in _vue_section_records(snapshot) for record in records]
    return _scan_source_beacons(snapshot, language)


# @beacon[
#   id=beacon-writeback@_parse_snapshot_beacons_for_language,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Pure writer and shared check-input parse from the snapshot supplying its hash: physical BOM-free rows with no sanitizer or source reread; projects shared original-byte records once per actual section and is vendored for kit parity.,
# ]
def _parse_snapshot_beacons_for_language(
    snapshot: SourceSnapshot, language: str, *, diagnostics: list | None = None,
    line_view: list[str] | None = None,
) -> tuple[list[str], list[dict]]:
    """Parse supplied bytes for writers and both checks without IO or cleanup.

    Only the legacy disk adapter supplies ``line_view`` for its existing
    BOM/sanitized snippet projection. Byte-edit callers use the raw physical
    default; structural fields and geometry always come from the snapshot.
    """
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


# @beacon[
#   id=auto-beacon@_beacon_block_extent_for_language-ext1,
#   role=_beacon_block_extent_for_language(language, lines, beacon),
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Delegate whole-line replacement geometry to the structural record at this opener; a Vue block selects that record from the SFC section route (never a whole-file dialect pass) and a split failure yields no extent, so incomplete or mixed-byte ownership never falls back to an opener-only or EOF deletion.,
# ]
def _beacon_block_extent_for_language(
    language: str,
    lines: list[str],
    beacon: dict,
) -> Optional[tuple[int, int]]:
    """Return only a complete, unambiguous record's owned whole-line extent.

    A Vue file selects from its SFC section route -- the same absolute,
    section-bounded records the disk parse and the outline read -- so a
    template block's geometry can never come from a script grammar and no
    extent crosses a section. A failed split yields ``None``: the one block is
    refused, nothing wider.
    """
    if language == "vue":
        records = _vue_records_for_lines(lines)
    else:
        records = _scan_source_beacons(_source_line_view(lines), language)
    return next((r["line_extent"] for r in records
                 if r["marker"] == "open" and r["line"] == beacon.get("comment_line")), None)


# @beacon[
#   id=carto-python@decorator-ranges-by-definition,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=ONE pure Python decorator-syntax authority for the shared attachment index and outline/context starts; AST ownership plus tokenizer logical-line boundaries covers stacked multiline and parenthesized decorators per definition including twins without IO evaluation or mutation.,
# ]
def _python_decorator_ranges_by_definition(
    lines: list[str], *, tree: Optional[ast.AST] = None,
) -> dict[int, list[tuple[int, int]]]:
    """Map 0-based def/class lines to their owned inclusive decorator ranges.

    This is syntax interpretation, NOT beacon attachment policy. Mapping and
    mutation use these same owned ranges through the shared attachment index.

    ``tree``, when supplied, must come from the same ``lines`` snapshot.
    Expression linenos can omit the leading @ and wrapping parentheses, so
    complete tokenizer logical statements supply the physical boundaries.
    Parse/tokenization failure returns no extra gap-crossing permission.
    """
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

        # A decorator is a whole logical statement starting with @, not any
        # physical line starting with @ (which could be a continuation).
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
                    break  # Never guess ownership or cross unrelated syntax.
                ranges.append(owned[0])
            else:
                by_definition[target.lineno - 1] = ranges
        return by_definition
    except (SyntaxError, ValueError, tokenize.TokenError):
        return {}


def _python_decorator_line_ranges(
    lines: list[str], target_idx0: int,
) -> list[tuple[int, int]]:
    """Single-target adapter over the shared whole-file syntax authority."""
    return _python_decorator_ranges_by_definition(lines).get(target_idx0, [])


def _attachment_comment_lines(snapshot: SourceSnapshot, language: str, sections=None, records=()) -> list[bool]:
    """Whole physical rows containing only whitespace and real comments.

    Comment-looking text in strings, a generator's star, and code sharing a
    comment's row are not gap permission. HTML uses the shared comment-row
    lexer (including fences); source languages use the lexical byte ranges.
    The start-sorted region walk resumes monotonically across physical lines;
    a covering region ends that line's walk, including nested/overlapping spans.
    """
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
    # A missing wrapper's lexical token may run into a later independent
    # block. Its unowned tail is not gap permission to cross real code.
    # Recovered complete records restore only their own comment extents.
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
    """Tree-owned decorators and true declaration rows, including SFC scripts.

    Decorators may be declaration children, export children, or class-body
    siblings of their method. Only the syntax tree's ownership supplies gap
    permission. A parse error in one declaration grants no permission there;
    independent declarations remain available. No source is read or evaluated.
    """
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
                                # An inline decorator never hides the declaration on that row.
                                ranges = [(a, min(b, definition - 1)) for a, b in ranges if a < definition]
                                result[declaration.start_point[0] + offset] = {
                                    "definition": definition, "ranges": ranges,
                                }
                if node.type != "comment":
                    pending = []
                visit(node)
        visit(root)
    return result


# @beacon[
#   id=beacon-writeback@beacon_attachment_index,
#   slice_labels=beacon-writeback,carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=One pure per-language attachment authority over a supplied snapshot outline and structural records; returns physical anchors effective kinds and gap evidence without writes or id-spelling decisions and never adopts explicit or paired spans as AST.,
# ]
def beacon_attachment_index(snapshot: SourceSnapshot, outline: list[dict], structures: dict) -> dict:
    """Index every opener's physical AST/heading attachment and gap evidence.

    ``structures`` is a bundle with ``language`` and the scanner's ``records``;
    optional ``beacons`` and ``decorator_ranges_by_definition`` reuse projections
    already computed from these bytes. Lines in the result are 1-based; block
    keys are original opener byte offsets. ``anchor`` is an outline descriptor
    (with its node for rendering), not proof that an id belongs to a symbol.
    Explicit spans and paired implicit spans never attach as AST. Markdown
    explicit AST retains strictly-next-heading attachment, recording prose;
    implicit promotion still requires an adjacent heading after comments.

    Mapping and writer-view adapters consume this index; selectors and hygiene
    can use its richer records without reproducing attachment. No outline
    builder is called here, so creating the index cannot recurse into mapping.
    """
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
        # Never step out of this section or out of an owning decorator.
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


def _beacon_attachment_outline(snapshot: SourceSnapshot, language: str) -> list[dict]:
    """Build a same-snapshot outline for non-mapper attachment adapters."""
    path = snapshot.canonical_path or {
        "python": "source.py", "js_ts": "source.ts", "vue": "source.vue",
        "markdown": "source.md",
    }.get(language, "source")
    builder = {"python": parse_file_outline, "js_ts": parse_js_ts_outline,
               "vue": parse_vue_outline, "markdown": parse_markdown_structure}.get(language)
    if builder is None:
        return []
    outline = builder(path, snapshot=snapshot)
    return outline.get("children", []) if isinstance(outline, dict) else outline


_BEACON_SELECTION_MEMO = OrderedDict()
_BEACON_SELECTION_LOCK = threading.RLock()


# @beacon[
#   id=beacon-writeback@_beacon_selection_structures,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Collects selection inputs once per resident content/path/language key in a locked four-entry LRU; every implicit-outline caller gets one whole-dict deep copy while explicit outlines keep their supplied-observation behavior.,
# ]
def _beacon_selection_structures(snapshot: SourceSnapshot, language: str, *, outline=None) -> dict:
    """Reuse four same-observation selections, with caller-owned whole-dict copies.

    Lookup and computation share a lock; eviction is LRU, never time-based.
    The sha/path/language key makes JIT plans and executor re-selection share
    unchanged bytes without skipping either check. Copy the whole dict once,
    including on the first return, to preserve intra-structure references.
    A supplied outline bypasses the memo and retains its original ownership.
    """
    def collect():
        """Build the complete selection from this snapshot and optional outline."""
        lines, beacons = _parse_snapshot_beacons_for_language(snapshot, language)
        return {"language": language, "records": _beacon_edit_records(snapshot, language),
                "beacons": beacons, "outline": outline if outline is not None else
                _beacon_attachment_outline(snapshot, language)}

    if outline is not None:
        return collect()
    key = (snapshot.sha256, snapshot.canonical_path, language)
    with _BEACON_SELECTION_LOCK:
        cached = _BEACON_SELECTION_MEMO.get(key)
        if cached is None:
            cached = collect()
            _BEACON_SELECTION_MEMO[key] = cached
            if len(_BEACON_SELECTION_MEMO) > 4:
                _BEACON_SELECTION_MEMO.popitem(last=False)
        else:
            _BEACON_SELECTION_MEMO.move_to_end(key)
    return copy.deepcopy(cached)


# @beacon[
#   id=beacon-writeback@_beacon_anchor_descriptor,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Projects an attachment anchor into serializable identity and location evidence without its mutable node.,
# ]
def _beacon_anchor_descriptor(anchor: dict | None) -> dict | None:
    """Serializable source identity, without the mapper's mutable node object."""
    if anchor is None:
        return None
    node = anchor["node"]
    return {"line": anchor["line"], "outline_line": anchor["outline_line"],
            "ast_qualname": anchor.get("ast_qualname"),
            "md_path": _extract_md_path_from_note(node.get("note")) if not anchor.get("ast_qualname") else None,
            "header": anchor["header"],
            "decorator_ranges": [list(r) for r in anchor.get("decorator_ranges", [])]}


# @beacon[
#   id=beacon-writeback@_beacon_physical_witness,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Pins the current section opener byte raw block hash and independent anchor for a selected physical operand.,
# ]
def _beacon_physical_witness(snapshot: SourceSnapshot, block: dict | None, anchor=None) -> dict:
    """Pin section, opener byte, raw block hash and independent anchor."""
    record = block["record"] if block else None
    return {"section": dict(record.get("section") or {}) if record else {},
            "opener_start_byte": record["opener_range"][0] if record else None,
            "block_hash": hashlib.sha256(snapshot.raw_bytes[record["opener_range"][0]:record["read_end"]]).hexdigest()
            if record else None,
            "anchor": _beacon_anchor_descriptor(block.get("anchor") if block else anchor)}


# @beacon[
#   id=beacon-writeback@_beacon_candidate,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Projects physical candidate fields attachment gap and incomplete-range repair evidence.,
# ]
def _beacon_candidate(block: dict) -> dict:
    """Actionable member evidence, including damaged records and their extent."""
    record = block["record"]
    fields = record["fields"]
    return {"line": record["line"], "kind": block["effective_kind"], "id": fields.get("id"),
            "labels": fields.get("slice_labels"), "comment": fields.get("comment"),
            "section": dict(record.get("section") or {}), "opener_start_byte": block["owner"],
            "anchor": _beacon_anchor_descriptor(block.get("anchor")),
            "complete": record["complete"], "write_ranges": record.get("write_ranges"),
            "terminated_by": record.get("terminated_by"), "gap": block["gap"],
            "repair_candidates": record.get("repair_candidates", [])}


# @beacon[
#   id=beacon-writeback@_beacon_header_identity,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Compares source-derived signatures after the shared decoration and trailing-label split.,
# ]
def _beacon_header_identity(name: str) -> str:
    """Compare the source-derived signature, not decorations or slice tags."""
    return strip_leading_decoration_symbols(split_name_and_tags(name)[0]).strip()


# @beacon[
#   id=beacon-writeback@beacon_id_resemblance,
#   slice_labels=beacon-writeback,carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Returns file-local R3 enclosing-path R7 near-spelling and R8 other-symbol suggestions from an already observed attachment index; folds CamelCase case edge underscores and hyphens; evidence never selects refuses or renames a target and performs no IO.,
# ]
def beacon_id_resemblance(snapshot: SourceSnapshot, block: dict | None, anchor: dict | None,
                          anchors: list[dict], blocks: list[dict]) -> list[dict]:
    """Advisory resemblance evidence, not an attachment or mutation policy.

    Uses only the same snapshot's existing outline and structures. Compatible
    exact/folded/variant/word-slug ids and unrelated concept ids are quiet.
    Cross-file history/dependants require a preview, not a mutation-loop scan.
    """
    if block is None or anchor is None:
        return []
    fields = block["record"]["fields"]
    bid = fields.get("id") or ""
    symbol = anchor.get("ast_qualname")
    if not symbol:
        return []  # no inferred AST identity for a span or a heading slug
    import difflib

    def fold(value):
        value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
        value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
        return ".".join(part.replace("-", "_").casefold().strip("_") for part in value.split("."))

    tail = bid.split("@", 1)[-1]
    core = re.sub(r"-[a-z0-9]{4}$", "", tail)
    leaf = symbol.rsplit(".", 1)[-1]
    full, short = fold(symbol), fold(leaf)
    # Keep the unsuffixed interpretation too: a real four-letter variant word
    # is not necessarily a random token.
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


def _beacon_cache_twin_witness(nodes_by_id: dict, node: dict) -> dict | None:
    """Project ordered cached AST signatures; an ordinal alone proves nothing.

    The selector checks every signature, count and order against current source.
    Indistinguishable signatures cannot witness a twin without a pinned source
    observation. A unique physically attached note id needs no ordinal witness.
    """
    qualname = _extract_ast_qualname_from_note(node.get("note") or node.get("no") or "")
    parent = node.get("parent_id") or node.get("parentId")
    if not qualname or not parent:
        return None
    siblings = sorted((n for n in nodes_by_id.values()
                       if (n.get("parent_id") or n.get("parentId")) == parent
                       and _extract_ast_qualname_from_note(n.get("note") or n.get("no") or "") == qualname),
                      key=lambda n: n.get("priority", 999999))
    ids = [str(n.get("id")) for n in siblings]
    if len(siblings) < 2 or str(node.get("id")) not in ids:
        return None
    return {"ast_qualname": qualname, "count": len(siblings),
            "ordinal": ids.index(str(node.get("id"))) + 1,
            "headers": [_beacon_header_identity(str(n.get("name") or n.get("nm") or "")) for n in siblings]}


# @beacon[
#   id=beacon-writeback@select_beacon_block_for_node,
#   slice_labels=beacon-writeback,carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Pure anchor-first physical selector over the shared attachment index; AST witnesses or already parsed Markdown path+occurrence identify twins without reparsing; rehydrate names its heading first; stale identities conflict and incomplete records never become absence; resemblance is advisory only.,
# ]
def select_beacon_block_for_node(snapshot: SourceSnapshot, structures: dict, target: dict) -> dict:
    """Return selected/adopt/create/satisfied_noop/conflict/candidates.

    ``target`` carries language, optional section, ast_qualname or md_path,
    occurrence, note_beacon_id, operation and optional twin_witness. This pure
    decision retains the original scanner record even when it has no writable
    extent. Consumers must not turn that record into a create authorization.
    Markdown's caller-supplied occurrence resolves against every current disk
    heading, using the same path matcher as the plan path. ``rehydrate`` is
    heading-authoritative: a supplied id elsewhere never redirects that write.
    File-local id resemblance is returned after selection, never used to choose.
    Markdown consumes the supplied outline's paths with the same normalization
    as compatibility lookup; the selector needs no external Markdown parser.
    """
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
        # The index already holds every structural heading (including untagged
        # siblings), so share path identity rather than reparse these bytes.
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
        return finish("create")  # the planner applies the language capability/anchor gate

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
            # Distinct source-derived signatures identify order without relying
            # on old line numbers. Equal signatures require an exact pinned
            # observation and the raw header sequence, not just an ordinal.
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
            # Include blockless twins as candidates too, not only the blocks.
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
        # U08 supplies this generated heading's own metadata. Equal ids at
        # other headings are findings, not identity conflicts or keeper choices.
        if ids:
            answer['diagnostics'].append({'type': 'duplicate_id', 'candidates': [_beacon_candidate(b) for b in ids]})
        if len(attached) > 1:
            return finish('candidates', anchor=anchor, error_kind='ambiguous_beacon_anchor',
                          error='Several blocks attach to the generated heading; choose its metadata member.')
        if attached:
            return choose(attached[0], anchor, adopted=True)
        # A malformed id-claim immediately at this heading is not absence.
        # Use the index's current gap evidence, never borrow a damaged record
        # belonging to another heading merely because its id is equal.
        claimed = [b for b in ids if not b['record']['complete'] and
                   b.get('next_code_line') == anchor['line']]
        if claimed:
            return finish('candidates', anchor=anchor, error_kind='beacon_extent_unreadable',
                          error='Incomplete claimed metadata at the generated heading needs its extent resolved; candidates retain its owned ranges.')
        return finish('create', anchor=anchor)
    # Incomplete id evidence must survive even if damage prevents attachment.
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


# @beacon[
#   id=beacon-writeback@_beacon_selection_target_key,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Projects a selection witness into a hashable physical claim; distinct duplicate-id blocks remain distinct operands.,
# ]
def _beacon_selection_target_key(snapshot: SourceSnapshot, selection: dict, bid=None) -> tuple | None:
    """Hashable physical claims distinguish copies, but collide for one block."""
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


# @beacon[
#   id=beacon-writeback@plan_beacon_node_mutation,
#   slice_labels=beacon-writeback,carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=One pure delta/absolute/rehydrate field planner preserves unrequested fields and supplied ids, returns advisory resemblance without affecting edits, and emits bounded field splices selected-owned deletion or create and U02 plans without IO or reselection.,
# ]
def plan_beacon_node_mutation(snapshot: SourceSnapshot, selection: dict, intent: dict) -> dict:
    """Plan exact-token deltas, absolute/adoption or rehydrate fields without IO.

    API comment resolution may be supplied by the caller; absolute intent
    derives it from scoped note records here. A create can be classified
    before its commit-time id is supplied; materialization with ``create_id``
    returns the same U02 plan shape as updates/deletes. ``intended_labels`` and
    ``planned_operation`` consume an already classified executable operand.
    A rehydrate selection names the generated Markdown heading first. Its
    scoped header supplies id/kind/show_span/comment; labels come from name
    tags, and no role is minted. Ordinary updates preserve id/kind/show_span;
    explicit scoped note comments retain their established authority.
    Resemblance suggestions are carried as data and never affect these decisions.
    """
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
    # A rehydrate header is selected by the supplied note identity, not the
    # old disk id it is explicitly restoring. Retain located occurrences so
    # ambiguity withholds only the requested field, never independent labels.
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
        # Use only this opener's scanner pairing, never a second id finder.
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
            return result  # commit-time minting remains at the existing seam
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


# @beacon[
#   id=auto-beacon@_find_anchor_matched_beacons-anch,
#   role=_find_anchor_matched_beacons(language, lines, beacons, target_idx0),
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Compatibility projection of the shared attachment index into legacy beacon/start/end tuples; only complete AST-attached blocks at this physical target are returned and no private gap or kind rule remains.,
# ]
def _find_anchor_matched_beacons(
    language: str,
    lines: list[str],
    beacons: list[dict],
    target_idx0: int,
    *,
    decorator_ranges_by_definition: Optional[dict[int, list[tuple[int, int]]]] = None,
) -> list[tuple[dict, int, int]]:
    """Project complete AST attachments, preserving the public tuple shape.

    The index also retains incomplete claimed blocks and gap evidence; this
    old tuple API cannot express them. Physical selection must use those
    richer records rather than treating an absent tuple as absence.
    ``decorator_ranges_by_definition`` must belong to the supplied rows.
    """
    if not beacons:
        return []
    snapshot = _source_line_view(lines)
    if language == "vue":
        pairs = _vue_section_records(snapshot)
        records = [r for _s, rs in pairs for r in rs]
        sections = [s for s, _rs in pairs]
    else:
        records, sections = _beacon_edit_records(snapshot, language), None
    index = beacon_attachment_index(snapshot, _beacon_attachment_outline(snapshot, language), {
        "language": language, "records": records, "beacons": beacons, "sections": sections,
        "decorator_ranges_by_definition": decorator_ranges_by_definition,
    })
    by_line = {b["record"]["line"]: b for b in index["blocks"]}
    result = []
    for beacon in beacons:
        block = by_line.get(beacon.get("comment_line"))
        anchor = block["anchor"] if block else None
        extent = block["record"]["line_extent"] if block else None
        if anchor and extent and target_idx0 + 1 in {anchor["line"], anchor["outline_line"]}:
            result.append((beacon, extent[0], extent[1]))
    return result


# @beacon[
#   id=auto-beacon@_resolve_anchor_target_line-resa,
#   role=_resolve_anchor_target_line(language, source_path, lines, ast_qualname, md_path, occurrence),
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Compatibility line projection of the shared physical selector; supplied outline and snapshot retain observation identity while callers without a member witness receive the selector's anchor ambiguity rather than a private qualname rule.,
# ]
def _resolve_anchor_target_line(
    language: str,
    source_path: str,
    lines: list[str],
    ast_qualname: Optional[str],
    md_path: Optional[list[str]],
    occurrence: Optional[int],
    *,
    outline: Optional[list[dict[str, Any]]] = None,
    snapshot: SourceSnapshot | None = None,
) -> tuple[Optional[int], bool]:
    """Project the selector's anchor to the historic ``(line, ambiguous)`` API.

    A caller with only a qualname cannot distinguish twins. Plan consumers pass
    their richer target directly to the selector instead. No competing anchor
    resolution or extent policy lives in this compatibility adapter.
    """
    try:
        snapshot = snapshot or _source_line_view(lines)
        if isinstance(outline, dict):
            outline = outline.get("children", [])
        structures = _beacon_selection_structures(snapshot, language, outline=outline)
        selected = select_beacon_block_for_node(snapshot, structures, {
            "language": language, "ast_qualname": ast_qualname, "md_path": md_path,
            "occurrence": occurrence, "operation": "delta_ensure"})
        anchor = selected.get("anchor")
        return (anchor["line"] - 1 if anchor else None,
                selected.get("error_kind") == "ambiguous_beacon_anchor" and not anchor)
    except Exception:
        return None, False


# ---------------------------------------------------------------------------
# Lightning Rod beacon mutation -- COMMIT SEAM plan-branch engine (SPEC v5 §6)
# ---------------------------------------------------------------------------


def _assert_write_intent_plan_pair(write_intent, write_plan) -> None:
    """BOTH-OR-NEITHER contract for the write_intent/write_plan kwargs.

    The legacy F12 paths pass NEITHER (byte-for-byte legacy behavior); the
    Lightning Rod mutation commit seam passes BOTH. An intent without a plan
    is an UNDEFINED combination (reserved for a future mode) -- reject loudly.
    """
    assert (write_intent is None) == (write_plan is None), (
        "write_intent and write_plan are BOTH-OR-NEITHER: an intent without "
        "a plan is an UNDEFINED combination (reserved for a future mode)"
    )


# @beacon[
#   id=auto-beacon@_build_beacon_block_for_language-bbfl,
#   role=_build_beacon_block_for_language(language, bid, role, slice_labels, kind, show_span, comment_text, start_snippet, end_snippet, section),
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=One renderer: ordinary edits return scanner-located field splices preserving every other byte and field-local ambiguity; creates/explicit whole-block repairs render the language wrapper and quote semantic comment punctuation for exact parse-back fidelity.,
# ]
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
    """Render creates, or return one bounded byte edit per changed field.

    With a scanner record, only requested values are spliced. Continuation
    content belongs to that field; all intervening prefixes/blank rows stay.
    Missing fields use the block's own line prefix before its terminator.
    Duplicate/ambiguous fields return candidates independently of safe fields.
    Full rendering remains for creates and explicitly authorized repairs.
    """
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
            # Preserve existing delimiter spelling. Historical unquoted loss
            # cannot be reconstructed; only the explicitly requested value is new.
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
                # Prefer an existing field's exact indentation/comment prefix.
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
                # Standalone terminator: insert a whole prefixed metadata line.
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


# @beacon[
#   id=auto-beacon@_find_beacon_close_block_extent-fbce,
#   role=_find_beacon_close_block_extent(language, lines, bid, search_from_idx),
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Delegate span-close selection and geometry to the scanner's exact pairing records; a Vue file selects from its SFC section route, so a close is never read across a section boundary and a split failure yields no close: no second structural grammar and no ambiguous destructive choice.,
# ]
def _find_beacon_close_block_extent(
    language: str,
    lines: list[str],
    bid: str,
    search_from_idx: int = 0,
) -> Optional[tuple[int, int]]:
    """Select the scanner's exact paired close (or an isolated close lookup).

    The latter preserves the public helper's close-only input shape. When
    openings exist, only their already-established pair relation is used. A
    Vue file selects from its SFC section route, whose records are bounded by
    their own section; a failed split yields no close.
    """
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


# @beacon[
#   id=auto-beacon@_vue_beacon_section_for_extent-rjjx,
#   role=_vue_beacon_section_for_extent,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Classify a Vue beacon block's SFC section from the same section-bounded records the disk parse and the extent/close helpers select, instead of guessing from the opener line's text; it returns script, template, style or unresolved, and an unresolved block is never treated as a writable script block.,
# ]
def _vue_beacon_section_for_extent(lines: list[str], start_idx: int) -> str:
    """Return the SFC section (``script``/``template``/``style``) owning a block.

    ``start_idx`` is the 0-based file row of the block's first owned line (its
    opener line, or a dedicated wrapper's first line when one is owned). The
    answer comes from the record's own section type, never from a text guess
    about that line, so a template block whose marker sits on its own line can
    no longer be classified as a script block. ``"unresolved"`` (no record, or
    a failed SFC split) is deliberately not a writable section: the block is
    refused alone rather than mutated on a guess.
    """
    target_line = start_idx + 1
    for record in _vue_records_for_lines(lines):
        if record["marker"] != "open":
            continue
        first = record["line_extent"][0] + 1 if record["line_extent"] else record["line"]
        if first <= target_line <= record["read_end_line"]:
            section_type = (record.get("section") or {}).get("type")
            return {"template_element": "template", "style_element": "style"}.get(section_type, "script")
    return "unresolved"


# @beacon[
#   id=auto-beacon@_execute_beacon_write_plan-exec,
#   role=_execute_beacon_write_plan(language, file_path, name, note, write_intent, write_plan, occurrence),
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Shared executor revalidates the physical witness, collision-checks minted creates against every snapshot id, and consumes the one planner's U02 edits; projects field and advisory resemblance data without changing selection and delegates proof and publication to beacon_edits.,
# ]
def _execute_beacon_write_plan(
    *,
    language: str,
    file_path: str,
    name: str,
    note: str,
    write_intent,
    write_plan,
    occurrence: Optional[int] = None,
) -> Dict[str, Any]:
    """Revalidate a physical operand, allocate creates and publish field edits.

    Mismatch semantics (contract): a raw-bytes SHA divergence is
    ``source_changed_since_plan``; EVERY OTHER divergence (source path,
    planned operation, selected physical target) is ``plan_mismatch``.
    Either way ``{success: False, ...}`` with ZERO bytes written.
    Minting checks all snapshot record ids; supplied ids are never reallocated.
    Resemblance evidence is projected without changing the executable operand.
    """
    result: Dict[str, Any] = {
        "language": language,
        "file_path": file_path,
        "operation": "noop",
        "planned_operation": getattr(write_plan, "planned_operation", None),
    }
    planned_op = getattr(write_plan, "planned_operation", None)
    if planned_op in (None, "noop"):
        return result
    if not getattr(write_plan, "success", False):
        result["error"] = getattr(write_plan, "error", None) or "invalid write plan"
        result["error_kind"] = getattr(write_plan, "error_kind", None) or "plan_mismatch"
        return result

    planned_sha = getattr(write_plan, "source_sha256", None)
    planned_path = getattr(write_plan, "source_path", None)
    if planned_path and os.path.normcase(os.path.realpath(file_path)) != os.path.normcase(
        os.path.realpath(str(planned_path))
    ):
        result["error"] = (
            f"plan_mismatch: planned source path {planned_path!r} != "
            f"dispatch path {file_path!r}"
        )
        result["error_kind"] = "plan_mismatch"
        return result

    try:
        snapshot = read_source_snapshot(file_path)
        if planned_sha and snapshot.sha256 != planned_sha:
            result.update(error="source_changed_since_plan: raw-bytes sha256 mismatch; zero bytes written",
                          error_kind="source_changed_since_plan",
                          evidence={"expected": planned_sha, "actual": snapshot.sha256})
            return result
        structures = _beacon_selection_structures(snapshot, language)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"failed_to_read_file: {e}"
        result["error_kind"] = "disk_read_failed"
        return result

    intended_labels = list(getattr(write_plan, "intended_slice_labels", ()) or ())
    resolved_comment = getattr(write_plan, "resolved_comment", None)
    bid = (getattr(write_plan, "beacon_id", None) or "").strip() or None
    target = getattr(write_plan, "selection_target", None)
    physical_witness = getattr(write_plan, "physical_witness", None)
    if not target or not physical_witness:
        result.update(error="plan_mismatch: executable operand has no physical witness", error_kind="plan_mismatch")
        return result
    selection = select_beacon_block_for_node(snapshot, structures, target)
    valid_status = {"create"} if planned_op == "create" else {"selected", "adopt"}
    if (selection["status"] not in valid_status or selection["physical_witness"] != physical_witness
            or (planned_op != "create" and selection["block"]["record"]["fields"].get("id") != bid)):
        result.update(error="plan_mismatch: planned physical section/opener/hash/anchor no longer matches",
                      error_kind="plan_mismatch", candidates=selection["candidates"])
        return result
    if planned_op == "update" and getattr(getattr(write_intent, "comment", None), "mode", None) == "fill_if_empty":
        live_comment = (selection["block"].get("beacon") or selection["block"]["record"]["fields"]).get("comment")
        if (live_comment or "").strip() and (resolved_comment or "") != live_comment:
            result.update(error="existing_comment_present", existing_comment=live_comment, error_kind="plan_mismatch")
            return result
    if planned_op == "create" and not bid:
        taken = {o["value"] for r in structures["records"] for o in r.get("occurrences", ()) if o["key"] == "id"}
        bid = allocate_beacon_id(taken, base_id=("auto-beacon@" if language == "markdown" else
                                 f"auto-beacon@{target.get('ast_qualname')}-"),
                                 random_parts=2 if language == "markdown" else 1)
    mutation = plan_beacon_node_mutation(snapshot, selection, {
        "kind": target["operation"], "planned_operation": planned_op,
        "intended_labels": intended_labels, "resolved_comment": resolved_comment,
        "create_id": bid, "name": name})
    if not mutation["success"]:
        result.update(error=mutation["error"], error_kind=mutation["error_kind"],
                      candidates=mutation["candidates"], diagnostics=mutation["diagnostics"])
        return result
    operation = {"create": "created_beacon", "update": "updated_beacon", "delete": "deleted_beacon"}[planned_op]

    try:
        committed = commit_beacon_edit_plan(mutation["edit_plan"], scanner=_beacon_edit_records)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"failed_to_write_file: {e}"
        result["error_kind"] = "disk_write_failed"
        return result
    result.update(committed)
    if not committed["success"]:
        return result

    result.update(
        {
            "success": True,
            "operation": operation,
            "beacon_id": bid,
            "slice_labels": ",".join(intended_labels),
            "comment": resolved_comment,
            "diagnostics": list(getattr(write_plan, "diagnostics", ())) + [
                d for d in mutation["diagnostics"] if d not in getattr(write_plan, "diagnostics", ())],
            "candidates": mutation["candidates"],
            "resemblance_evidence": mutation["resemblance_evidence"],
        }
    )
    return result


# @beacon[
#   id=beacon-writeback@_update_beacon_from_node_absolute,
#   slice_labels=beacon-writeback,carto-fwd-source,
#   kind=ast,
#   comment=One shared recipe for all seven legacy writers: one snapshot selection and absolute/rehydrate field plan, supplied Markdown ids or collision-checked language-convention creates, and one U02 commit; projects field and advisory resemblance evidence with no private policy.,
# ]
def _update_beacon_from_node_absolute(
    language: str,
    file_path: str,
    name: str,
    note: str,
    *,
    occurrence: Optional[int] = None,
    intent: str = 'absolute_labels',
) -> Dict[str, Any]:
    """Run the shared snapshot -> selection -> plan -> commit writeback.

    All seven language adapters delegate here after their both-or-neither plan
    branch. Sequence:
      1. one ``read_source_snapshot`` plus ``_beacon_selection_structures``;
      2. ``select_beacon_block_for_node`` for the note's beacon id and AST
         qualname or Markdown path+occurrence, using Vue's section records;
      3. ``plan_beacon_node_mutation`` with absolute or opt-in rehydrate intent;
         creates retain Markdown's supplied note id, otherwise mint the current
          language convention collision-free against all snapshot record ids,
          then re-plan on the SAME snapshot and selection;
      4. project the plan's evidence (never its edit plan or selection
         objects), then ``commit_beacon_edit_plan`` the non-noop plan.

    The recipe owns no field policy: role comes from the selected disk fields
    and is never minted from the name, and a note comment applies only when
    the scoped BEACON header explicitly states it. Errors are reported locally
    (``disk_read_failed`` / ``disk_write_failed`` / planner ``error_kind``)
    with ``operation`` left at ``noop``. Vue script create/update successes
    retain the legacy ``language=js_ts`` result spelling; their witness still
    carries the actual Vue section.
    """
    base_name, tags = split_name_and_tags(name)
    note_beacon_id = _extract_beacon_id_from_note(note)
    ast_qualname = _extract_ast_qualname_from_note(note)
    md_path = _extract_md_path_from_note(note) if language == 'markdown' else None
    intent_kind = intent

    result: Dict[str, Any] = {
        "language": language,
        "file_path": file_path,
        "base_name": base_name,
        "tags": tags,
        "beacon_id": note_beacon_id,
        "ast_qualname": ast_qualname,
        "operation": "noop",
    }
    if language == 'markdown':
        result.update(md_path=md_path, occurrence=occurrence)

    try:
        snapshot = read_source_snapshot(file_path)
        structures = _beacon_selection_structures(snapshot, language)
    except Exception as e:  # noqa: BLE001
        result.update(error=f"failed_to_read_file: {e}", error_kind="disk_read_failed", success=False)
        return result

    target = {
        "language": language,
        "ast_qualname": ast_qualname,
        'md_path': md_path,
        'occurrence': occurrence,
        "note_beacon_id": note_beacon_id,
        "operation": 'rehydrate' if intent_kind == 'rehydrate' else "absolute_labels" if tags else "delta_remove",
    }
    intent = {"kind": intent_kind, "name": name, "note": note}
    selection = select_beacon_block_for_node(snapshot, structures, target)
    mutation = plan_beacon_node_mutation(snapshot, selection, intent)
    if mutation.get("success") and mutation.get("planned_operation") == "create":
        create_id = note_beacon_id if language == 'markdown' else None
        if not create_id:
            taken = {o["value"] for r in structures["records"] for o in r.get("occurrences", ()) if o["key"] == "id"}
            create_id = allocate_beacon_id(taken, base_id=('auto-beacon@' if language == 'markdown' else
                                          f'auto-beacon@{ast_qualname}-'),
                                          random_parts=2 if language == 'markdown' else 1)
        mutation = plan_beacon_node_mutation(snapshot, selection, {**intent, "create_id": create_id})

    selected_block = selection.get("block")
    selected_fields: Dict[str, Any] = {}
    if selected_block:
        selected_fields = selected_block.get("beacon") or selected_block["record"]["fields"] or {}

    result["success"] = bool(mutation.get("success"))
    result["beacon_id"] = mutation.get("beacon_id") or note_beacon_id
    result["role"] = selected_fields.get("role") or ""
    if mutation.get("planned_operation") == "delete":
        result["slice_labels"] = ""
    else:
        result["slice_labels"] = ",".join(mutation.get("intended_slice_labels") or ())
    result["comment"] = mutation.get("resolved_comment", selected_fields.get("comment"))
    for key in ("candidates", "diagnostics", "physical_witness", "field_errors", "resemblance_evidence",
                "removed_labels", "adoption_additive", "comment_outcome",
                "comment_conflict", "comment_warning", "noop_reason",
                "error_kind", "error"):
        if key in mutation:
            result[key] = mutation[key]

    if not mutation.get("success"):
        if not (ast_qualname or md_path) and (not note_beacon_id
                                 or mutation.get("error_kind") == "beacon_identity_conflict"):
            result["noop_reason"] = "missing_beacon_id"
        else:
            result["noop_reason"] = mutation.get("error_kind")
        return result

    if mutation.get("planned_operation") == "noop":
        if intent["kind"] in {'absolute_labels', 'rehydrate'} and result.get("noop_reason") == "labels_already_absent":
            result["noop_reason"] = "already_satisfied"
        return result

    try:
        committed = commit_beacon_edit_plan(mutation["edit_plan"], scanner=_beacon_edit_records)
    except Exception as e:  # noqa: BLE001
        result.update(error=f"failed_to_write_file: {e}", error_kind="disk_write_failed", success=False)
        return result
    result.update(committed)
    if not committed.get("success"):
        result["success"] = False
        return result
    result["success"] = True
    result["operation"] = {"create": "created_beacon", "update": "updated_beacon",
                            "delete": "deleted_beacon"}[mutation["planned_operation"]]
    if (language == 'vue' and mutation['planned_operation'] in {'create', 'update'} and
            (mutation.get('physical_witness') or {}).get('section', {}).get('type') == 'script_element'):
        result['language'] = 'js_ts'
    return result


# @beacon[
#   id=carto-js-ts@update_beacon_from_node_python,
#   role=🔱 🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,nexus--tags,
#   kind=span,
# ]
# Python: update/create/delete a beacon for a single AST/beacon node
# based on Workflowy name/note fields.
#
# This is the Cartographer-side helper invoked by the MCP server when F12
# is pressed on a Python AST/beacon node. It does not talk to Workflowy
# directly; instead it focuses on:
#   • interpreting Workflowy name/note as desired beacon metadata
#   • computing canonical slice_labels from tags + note
#   • deciding whether to update, create, or delete @beacon[...] blocks
#   • returning a small summary of what should be reflected back into
#     the local cache.
# @beacon[
#   id=auto-beacon@update_beacon_from_node_python-1svv,
#   role=update_beacon_from_node_python,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Python writer as a thin public adapter: the both-or-neither plan branch still executes, everything else delegates one snapshot, physical selection, absolute name/note intent and commit to _update_beacon_from_node_absolute; role is disk-owned, never minted from the name.,
# ]
def update_beacon_from_node_python(
    file_path: str,
    name: str,
    note: str,
    *,
    write_intent=None,
    write_plan=None,
) -> Dict[str, Any]:
    """Update/create/delete Python @beacon[...] blocks for a single node.

    A thin public adapter. The both-or-neither ``write_intent`` + ``write_plan``
    branch still executes through ``_execute_beacon_write_plan``; otherwise it
    delegates snapshot reading, physical selection, absolute field intent and
    the commit to ``_update_beacon_from_node_absolute``.

    Delegated policy: role is disk-owned and never minted from the node name;
    slice_labels are the name's #tags, absolute when the note carries a BEACON
    header (removals are reported) and additive for headerless adoption; a note
    comment applies only when the scoped BEACON header explicitly states it.

    On-disk edits ONLY: it does not touch Workflowy; the MCP client reflects
    the returned state into the local /nodes-export cache.
    """
    _assert_write_intent_plan_pair(write_intent, write_plan)
    if write_plan is not None:
        return _execute_beacon_write_plan(
            language="python",
            file_path=file_path,
            name=name,
            note=note,
            write_intent=write_intent,
            write_plan=write_plan,
        )
    return _update_beacon_from_node_absolute(
        language="python", file_path=file_path, name=name, note=note
    )


# @beacon[
#   id=carto-js-ts@update_beacon_from_node_js_ts,
#   role=🔱 🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,nexus--tags,
#   kind=span,
# ]
# JS/TS: update/create/delete a beacon for a single AST/beacon node based
# on Workflowy name/note fields. Mirrors the Python helper but uses
# JS/TS-specific outline + beacon parsing.
# @beacon[
#   id=auto-beacon@update_beacon_from_node_js_ts-d5kl,
#   role=update_beacon_from_node_js_ts,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=JS/TS writer as a thin public adapter: the both-or-neither plan branch still executes, everything else delegates one snapshot, physical selection, absolute name/note intent and commit to _update_beacon_from_node_absolute; role is disk-owned, never minted from the name.,
# ]
def update_beacon_from_node_js_ts(
    file_path: str,
    name: str,
    note: str,
    *,
    write_intent=None,
    write_plan=None,
) -> Dict[str, Any]:
    """Update/create/delete JS/TS @beacon[...] blocks for a single node.

    A thin public adapter. The both-or-neither ``write_intent`` + ``write_plan``
    branch still executes through ``_execute_beacon_write_plan``; otherwise it
    delegates snapshot reading, physical selection, absolute field intent and
    the commit to ``_update_beacon_from_node_absolute``.

    Delegated policy: role is disk-owned and never minted from the node name;
    slice_labels are the name's #tags, absolute when the note carries a BEACON
    header (removals are reported) and additive for headerless adoption; a note
    comment applies only when the scoped BEACON header explicitly states it.
    """
    _assert_write_intent_plan_pair(write_intent, write_plan)
    if write_plan is not None:
        return _execute_beacon_write_plan(
            language="js_ts",
            file_path=file_path,
            name=name,
            note=note,
            write_intent=write_intent,
            write_plan=write_plan,
        )
    return _update_beacon_from_node_absolute(
        language="js_ts", file_path=file_path, name=name, note=note
    )


# @beacon[
#   id=auto-beacon@update_beacon_from_node_vue,
#   role=ƒ update_beacon_from_node_vue(file_path: str, name: str, note: str),
#   slice_labels=ra-reconcile,nexus--tags,beacon-writeback,
#   kind=ast,
#   comment=Vue writer as a thin public adapter: the both-or-neither plan branch still executes, everything else delegates one snapshot, the section-aware selection, absolute name/note intent and the commit to _update_beacon_from_node_absolute; template/style absolute update/delete stay, creates are script AST only, and role is disk-owned, never minted from the name.,
# ]
def update_beacon_from_node_vue(
    file_path: str,
    name: str,
    note: str,
    *,
    write_intent=None,
    write_plan=None,
) -> Dict[str, Any]:
    """Update/create/delete Vue (.vue) @beacon[...] blocks for a single node.

    A thin public adapter. The both-or-neither ``write_intent`` + ``write_plan``
    branch still executes through ``_execute_beacon_write_plan``; otherwise it
    delegates one snapshot, the shared section-aware selection, the absolute
    name/note field intent and the commit to
    ``_update_beacon_from_node_absolute``.

    Delegated policy: script blocks attach through the section records with the
    ``vue:script{N}:`` AST_QUALNAME; template (``<!-- -->``) and style
    (``/* */``) spans keep their legacy absolute update and delete, while
    creates are script AST only; existing beacon metadata fields are preserved
    and role is disk-owned, never minted from the node name.

    On-disk edits ONLY: it does not touch Workflowy or patch its cache.
    """
    _assert_write_intent_plan_pair(write_intent, write_plan)
    if write_plan is not None:
        return _execute_beacon_write_plan(
            language="vue",
            file_path=file_path,
            name=name,
            note=note,
            write_intent=write_intent,
            write_plan=write_plan,
        )

    return _update_beacon_from_node_absolute("vue", file_path, name, note)


# @beacon[
#   id=carto-js-ts@update_beacon_from_node_markdown,
#   role=🔱 🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-beacons,ra-reconcile,nexus-md-header-path,nexus--tags,
#   kind=span,
# ]
# Markdown: update/create/delete a beacon for a single heading/beacon node
# based on Workflowy name/note fields. Uses MD_PATH to locate the heading
# and mirrors the Python/JS/TS behavior for slice_labels and comments.
# @beacon[
#   id=auto-beacon@update_beacon_from_node_markdown-mdast,
#   role=ƒ update_beacon_from_node_markdown(file_path: str, name: str, note: str),
#   slice_labels=ra-reconcile,ra-bulk-visible-apply,nexus-md-header-path,carto-js-ts-beacons,carto-fwd-markdown,nexus--tags,beacon-writeback,
#   kind=ast,
#   comment=Markdown writer as a thin public adapter: the both-or-neither plan branch still executes, everything else delegates one snapshot, the shared MD_PATH+occurrence physical selection, the name/note intent (absolute labels by default, rehydrate opt-in) and the commit to _update_beacon_from_node_absolute; creates reuse the note-carried id, and role is disk-owned, never minted from the name.,
# ]
def update_beacon_from_node_markdown(
    file_path: str,
    name: str,
    note: str,
    occurrence: Optional[int] = None,
    *,
    write_intent=None,
    write_plan=None,
    intent: str = "absolute_labels",
) -> Dict[str, Any]:
    """Update/create/delete Markdown beacons for a single heading/beacon node.

    A thin public adapter. The both-or-neither ``write_intent`` + ``write_plan``
    branch still executes through ``_execute_beacon_write_plan``; otherwise it
    delegates one snapshot, the shared MD_PATH + ``occurrence`` physical
    selector, the planner and the commit to
    ``_update_beacon_from_node_absolute``.

    Delegated policy: ``intent`` defaults to ``"absolute_labels"``; the opt-in
    ``"rehydrate"`` runs note-authoritative, heading-anchored writes. Creates
    retain the beacon id carried by the note (F12+3 phase 7 depends on it) and
    mint a fresh id only when the note has none; role is disk-owned and never
    minted from the node name.

    Both caller shapes are retained: ``occurrence`` (1-based, document order)
    disambiguates a heading chain shared by N>1 sibling headings and stays a
    keyword for the protected dispatcher and mdr phase 7. Name and note reach
    the shared scoped field policy unchanged; no per-writer sanitising happens here.
    """
    _assert_write_intent_plan_pair(write_intent, write_plan)
    if write_plan is not None:
        return _execute_beacon_write_plan(
            language="markdown",
            file_path=file_path,
            name=name,
            note=note,
            write_intent=write_intent,
            write_plan=write_plan,
            occurrence=occurrence,
        )

    return _update_beacon_from_node_absolute("markdown", file_path, name, note, occurrence=occurrence, intent=intent)


# @beacon[
#   id=auto-beacon@update_beacon_from_node_sql-eybf,
#   role=ƒ update_beacon_from_node_sql(file_path: str, name: str, note: str),
#   slice_labels=ra-reconcile,nexus--tags,beacon-writeback,
#   kind=ast,
#   comment=SQL writer as a thin public adapter: the both-or-neither plan branch still executes, everything else delegates one snapshot, physical selection, absolute name/note intent and commit to _update_beacon_from_node_absolute; role is disk-owned, never minted from the name.,
# ]
def update_beacon_from_node_sql(
    file_path: str,
    name: str,
    note: str,
    *,
    write_intent=None,
    write_plan=None,
) -> Dict[str, Any]:
    """Update/delete SQL @beacon[...] blocks for a single node.

    A thin public adapter. The both-or-neither ``write_intent`` + ``write_plan``
    branch still executes through ``_execute_beacon_write_plan``; otherwise it
    delegates snapshot reading, physical selection, absolute field intent and
    the commit to ``_update_beacon_from_node_absolute`` (creation is refused by
    the planner's language capability gate for SQL).

    Delegated policy: role is disk-owned and never minted from the node name;
    slice_labels are the name's #tags, absolute when the note carries a BEACON
    header (removals are reported) and additive for headerless adoption; a note
    comment applies only when the scoped BEACON header explicitly states it.
    """
    _assert_write_intent_plan_pair(write_intent, write_plan)
    if write_plan is not None:
        return _execute_beacon_write_plan(
            language="sql",
            file_path=file_path,
            name=name,
            note=note,
            write_intent=write_intent,
            write_plan=write_plan,
        )
    return _update_beacon_from_node_absolute(
        language="sql", file_path=file_path, name=name, note=note
    )


# @beacon[
#   id=auto-beacon@update_beacon_from_node_shell-vult,
#   role=ƒ update_beacon_from_node_shell(file_path: str, name: str, note: str),
#   slice_labels=ra-reconcile,nexus--tags,beacon-writeback,
#   kind=ast,
#   comment=Shell writer as a thin public adapter: the both-or-neither plan branch still executes, everything else delegates one snapshot, physical selection, absolute name/note intent and commit to _update_beacon_from_node_absolute; role is disk-owned, never minted from the name.,
# ]
def update_beacon_from_node_shell(
    file_path: str,
    name: str,
    note: str,
    *,
    write_intent=None,
    write_plan=None,
) -> Dict[str, Any]:
    """Update/delete shell @beacon[...] blocks for a single node.

    A thin public adapter. The both-or-neither ``write_intent`` + ``write_plan``
    branch still executes through ``_execute_beacon_write_plan``; otherwise it
    delegates snapshot reading, physical selection, absolute field intent and
    the commit to ``_update_beacon_from_node_absolute`` (creation is refused by
    the planner's language capability gate for shell).

    Delegated policy: role is disk-owned and never minted from the node name;
    slice_labels are the name's #tags, absolute when the note carries a BEACON
    header (removals are reported) and additive for headerless adoption; a note
    comment applies only when the scoped BEACON header explicitly states it.
    """
    _assert_write_intent_plan_pair(write_intent, write_plan)
    if write_plan is not None:
        return _execute_beacon_write_plan(
            language="shell",
            file_path=file_path,
            name=name,
            note=note,
            write_intent=write_intent,
            write_plan=write_plan,
        )
    return _update_beacon_from_node_absolute(
        language="shell", file_path=file_path, name=name, note=note
    )


# @beacon[
#   id=auto-beacon@update_beacon_from_node_yaml-ykv1,
#   role=ƒ update_beacon_from_node_yaml(file_path: str, name: str, note: str),
#   slice_labels=ra-reconcile,nexus--tags,beacon-writeback,
#   kind=ast,
#   comment=YAML writer as a thin public adapter: the both-or-neither plan branch still executes, everything else delegates one snapshot, physical selection, absolute name/note intent and commit to _update_beacon_from_node_absolute; role is disk-owned, never minted from the name.,
# ]
def update_beacon_from_node_yaml(
    file_path: str,
    name: str,
    note: str,
    *,
    write_intent=None,
    write_plan=None,
) -> Dict[str, Any]:
    """Update/delete YAML @beacon[...] blocks for a single node.

    A thin public adapter. The both-or-neither ``write_intent`` + ``write_plan``
    branch still executes through ``_execute_beacon_write_plan``; otherwise it
    delegates snapshot reading, physical selection, absolute field intent and
    the commit to ``_update_beacon_from_node_absolute`` (creation is refused by
    the planner's language capability gate for YAML).

    Delegated policy: role is disk-owned and never minted from the node name;
    slice_labels are the name's #tags, absolute when the note carries a BEACON
    header (removals are reported) and additive for headerless adoption; a note
    comment applies only when the scoped BEACON header explicitly states it.
    """
    _assert_write_intent_plan_pair(write_intent, write_plan)
    if write_plan is not None:
        return _execute_beacon_write_plan(
            language="yaml",
            file_path=file_path,
            name=name,
            note=note,
            write_intent=write_intent,
            write_plan=write_plan,
        )
    return _update_beacon_from_node_absolute(
        language="yaml", file_path=file_path, name=name, note=note
    )


# @beacon[
#   id=auto-beacon@reconcile_trees-sbk1,
#   role=reconcile_trees,
#   slice_labels=ra-reconcile,
#   kind=ast,
# ]
def reconcile_trees(source_node: Dict[str, Any], ether_node: Dict[str, Any]) -> None:
    """
    Recursively match source nodes with ether nodes to preserve UUIDs.
    Modifies source_node in-place to add 'id' fields from ether_node.
    """
    # 1. Match self (if not root wrapper)
    if "id" in ether_node:
        source_node["id"] = ether_node["id"]
        
    # 2. Build map of Ether children by Name
    ether_children_map = {}
    if "children" in ether_node:
        for child in ether_node["children"]:
            # We match by Name. 
            # Limitation: If multiple siblings have same name, behavior is undefined (first match).
            # Workflowy allows duplicates, but code structures usually don't have duplicate classes/funcs in same scope.
            ether_children_map[child["name"]] = child
            
    # 3. Iterate Source children and try to match
    if "children" in source_node:
        for source_child in source_node["children"]:
            name = source_child["name"]
            if name in ether_children_map:
                # Match found! Recurse.
                reconcile_trees(source_child, ether_children_map[name])
                # Remove from map to handle duplicates/prevent re-matching? 
                # For now, let's leave it. Standard code shouldn't have dups.

# @beacon[
#   id=auto-beacon@load_existing_map-kat3,
#   role=load_existing_map,
#   slice_labels=ra-reconcile,
#   kind=ast,
# ]
def load_existing_map(file_path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # Handle NEXUS wrapper or raw node
            if "nodes" in data and isinstance(data["nodes"], list):
                # Wrapper format. Return the first root node if matches context?
                # Actually, usually we map to a single root.
                if len(data["nodes"]) > 0:
                    return data["nodes"][0]
            elif "name" in data:
                # Single node
                return data
            elif isinstance(data, list) and len(data) > 0:
                 return data[0]
    except Exception as e:
        print(f"⚠️ Failed to load existing map: {e}")
    return None


def validate_nonempty_names(node: Dict[str, Any], path: str = "root") -> Optional[str]:
    """Ensure all nodes in a NEXUS tree have non-empty, non-whitespace names.

    This enforces the same ETHER invariant used by the Workflowy MCP: any
    prospective node that will be woven into Workflowy must have a valid name.
    """
    name = node.get("name")
    if not isinstance(name, str) or not name.strip():
        return f"{path} has empty or whitespace-only 'name'"

    children = node.get("children") or []
    for idx, child in enumerate(children):
        child_label = child.get("name") or f"child[{idx}]"
        err = validate_nonempty_names(child, f"{path} -> {child_label}")
        if err:
            return err
    return None


# @beacon-close[
#   id=nmcv1@multilang-update-core,
# ]
# @beacon-close[
#   id=nmcv1@file-core,
# ]
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Map source code to NEXUS JSON.")
    parser.add_argument("path", help="Root directory to map")
    parser.add_argument("--parent-id", "-p", help="Target Workflowy Parent ID (for weaving)", default="00000000-0000-0000-0000-000000000000")
    parser.add_argument("--existing-map", "-e", help="Existing NEXUS JSON map (from Scry) for synchronization", default=None)
    parser.add_argument("--include", "-i", nargs="+", help="Whitelist of file extensions (e.g. .py .md .js)")
    parser.add_argument("--exclude", "-x", nargs="+", help="Blacklist of filename patterns (e.g. test_ venv)")
    parser.add_argument(
        "--nexus-tag",
        "-t",
        required=True,
        help="NEXUS tag; enchanted_terrain.json will be written under temp/nexus_runs/<timestamp>__<tag>/"
    )
    
    args = parser.parse_args()
    
    abs_path = os.path.abspath(args.path)
    print(f"🗺️ Mapping codebase at: {abs_path}")
    
    # Normalize extensions
    includes = [ext.lower() if ext.startswith('.') else f'.{ext.lower()}' for ext in args.include] if args.include else None
    
    nexus_tree = map_codebase(abs_path, include_exts=includes, exclude_patterns=args.exclude)

    # Validate newly generated tree: no blank or whitespace-only names
    err = validate_nonempty_names(nexus_tree)
    if err:
        print("❌ Generated codebase map contains invalid node names:")
        print(err)
        raise SystemExit(1)
    
    # Reconciliation Step
    if args.existing_map:
        print(f"🔄 Reconciling with existing map: {args.existing_map}")
        ether_root = load_existing_map(args.existing_map)
        if ether_root:
            # Ensure existing map also respects the non-empty-name invariant
            err_existing = validate_nonempty_names(ether_root)
            if err_existing:
                print("❌ Existing map contains invalid node names:")
                print(err_existing)
                raise SystemExit(1)

            # We assume nexus_tree matches the ether_root in structure (Root -> Children)
            # If names differ (e.g. root name change), we might fail to match top level,
            # but usually we care about children.
            # Use Cartographer-aware reconciliation so beacon/AST/MD_PATH anchors
            # drive ID preservation, with name-based matching as a fallback.
            reconcile_trees_cartographer(nexus_tree, ether_root)
            print("✅ Reconciliation complete (Cartographer-aware). UUIDs preserved.")
        else:
             print("⚠️ Existing map loaded but empty/invalid.")

    # Decide output location (always NEXUS run directory for this tag)
    # TIMESTAMP PREFIX: YYYY-MM-DD_HH-MM-SS__<tag>
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    timestamped_tag = f"{timestamp}__{args.nexus_tag}"
    
    project_root = os.path.dirname(os.path.abspath(__file__))
    nexus_runs_root = os.path.join(project_root, "temp", "nexus_runs")
    tag_dir = os.path.join(nexus_runs_root, timestamped_tag)
    os.makedirs(tag_dir, exist_ok=True)
    output_path = os.path.join(tag_dir, "enchanted_terrain.json")

    # Wrap in NEXUS metadata wrapper (enchanted terrain T2-style header)
    import datetime
    
    wrapper = {
        "export_root_id": args.parent_id,
        "export_root_name": nexus_tree["name"],
        "export_timestamp": datetime.datetime.now().isoformat(),
        "export_root_children_status": "complete",
        "nodes": [nexus_tree],
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(wrapper, f, indent=2)
        
    print(f"✅ Codebase map generated at: {output_path}")
